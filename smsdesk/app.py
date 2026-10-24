"""Tkinter desktop application; all blocking work is delegated to worker threads.

Features an 8-view navy/blue/cyan interface:
1. Dashboard
2. Devices (USB & Wireless ADB Pairing / Connect / TCP-IP / Aliases / Messaging Diagnostics)
3. Campaigns & Batches (CSV import preview, column mapping, rejected export, batch assignment)
4. Message Composer & Templates (GSM/UCS-2 segment accounting, personalization preview, non-silent shortening)
5. Live Execution (Start, Pause, Resume, Stop, countdowns, remaining time estimate, operator confirmation)
6. History & Reports (search, filter, sort, privacy-masked export, retry preview, pending recovery)
7. Settings (timing, country code, ADB path, dry-run mode, DB backup/restore)
8. Diagnostics & Help (runtime paths, first-run setup checklist, audit trail, support bundle, user guide)
"""

from __future__ import annotations

import csv
import io
import os
import platform
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import BUILD_ID, __version__
from .adb import AdbClient, AdbError, Device, MessagingAppInfo, MockAdbClient, format_device_labels
from .crash import CrashReporter, install_thread_excepthook
from .dispatch import DispatchWorker, RunState, StateMachine, WorkerControls
from .storage import SCHEMA_VERSION, HistoryStore, format_local_time
from .validation import (
    DEFAULT_MAX_CSV_BYTES,
    MAX_RECIPIENTS,
    MAX_SMS_CHARS,
    RejectedRow,
    ValidationError,
    analyze_sms_segments,
    bounded_number,
    extract_template_variables,
    format_rejected_rows_csv,
    normalize_phone,
    parse_contacts_csv,
    preview_csv_headers,
    safe_truncate_message,
    sms_encoding_usage,
    validate_ip_port,
    validate_message,
    validate_pairing_code,
    validate_personalized_batch,
    validate_port,
)

# Requested Navy / Blue / Cyan Palette
COLORS = {
    "bg": "#0F172A",          # Background
    "panel": "#1E293B",       # Panels
    "panel_alt": "#26354D",   # Elevated surface
    "electric": "#007ACC",    # Primary blue
    "accent": "#06B6D4",      # Cyan accent
    "accent_text": "#0F172A", # Dark contrast text on cyan
    "text": "#F8FAFC",        # Primary text
    "muted": "#CBD5E1",       # Secondary text
    "good": "#22C55E",        # Success
    "warn": "#F59E0B",        # Warning
    "bad": "#EF4444",         # Error
    "console": "#090E1A",     # Console background
    "border": "#334155",      # Borders
}

STATUS_PREFIX = {
    "sent": "[SENT - USER CONFIRMED] ",
    "pending": "[PENDING] ",
    "staged": "[STAGED IN COMPOSER] ",
    "failed": "[FAILED] ",
    "skipped": "[SKIPPED - INELIGIBLE] ",
    "uncertain": "[UNCERTAIN - INTERRUPTED] ",
}

MAX_CONSOLE_LINES = 600


class ToolTip:
    """Non-blocking hover tooltip using Tkinter `after` scheduling."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip_window: tk.Toplevel | None = None
        try:
            self.widget.bind("<Enter>", self._schedule, add="+")
            self.widget.bind("<Leave>", self._hide, add="+")
            self.widget.bind("<ButtonPress>", self._hide, add="+")
        except Exception:
            pass

    def _schedule(self, _event=None):
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip_window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            lbl = tk.Label(
                tw,
                text=self.text,
                justify="left",
                background=COLORS["panel_alt"],
                foreground=COLORS["text"],
                relief="solid",
                borderwidth=1,
                padx=8,
                pady=5,
                wraplength=340,
            )
            lbl.pack()
            self._tip_window = tw
        except Exception:
            self._tip_window = None

    def _hide(self, _event=None):
        self._cancel()
        if self._tip_window is not None:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None


class SmsDeskApp:
    def __init__(
        self,
        root: tk.Tk,
        db_path: str | Path = "sms_history_pro.db",
        adb: AdbClient | None = None,
        crash_dir: str | Path = "crash_reports",
    ):
        self.root = root
        self.root.title(f"Consent-first SMS Desk v{__version__} — Manual ADB Composer Staging")
        self.root.geometry("1260x880")
        self.root.minsize(820, 640)

        self.store = HistoryStore(db_path, initialize=False)
        self.database_ready = False
        self.real_adb = adb or AdbClient()
        self.adb: AdbClient = self.real_adb
        self.dry_run_active = False

        self.events: queue.Queue = queue.Queue()
        self.devices: dict[str, Device] = {}
        # Retain friendly identities for history rows while a device is offline
        self.device_names: dict[str, str] = {}
        self.device_aliases: dict[str, str] = {}
        self._device_display_to_serial: dict[str, str] = {}

        self.recipients: list[str] = []
        self.recipient_metadata: dict[str, dict] = {}
        self.last_rejected_rows: tuple[RejectedRow, ...] = ()
        self.batch_ids: list[int] = []
        self.campaign_sent = 0
        self.active_worker: DispatchWorker | None = None
        self.active_batch_id: int | None = None
        self.active_device_serial: str = ""
        self.controls: WorkerControls | None = None
        self.last_meaningful_op = "Application initialized"

        self.background_threads: set[threading.Thread] = set()
        self._background_lock = threading.Lock()
        self._history_refresh_pending = False
        self._after_ids: set[str] = set()
        self.machine = StateMachine()
        self.closing = False
        self._layout_two_columns: bool | None = None
        self._wifi_reconnect_cancel = threading.Event()
        self._health_poll_enabled = True

        self.reporter = CrashReporter(
            crash_dir,
            notify=lambda text: self.events.put(("crash_notice", text)),
        )
        self.reporter.install()
        install_thread_excepthook(self.reporter)

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.root.report_callback_exception = self._callback_exception
        self.root.bind("<Configure>", self._resize_layout, add="+")
        self._after(100, self._poll_events)
        self._after(30000, self._periodic_device_health_check)
        self._spawn_background(self._initialize_database, "database-startup")
        self.discover_devices()

    def _initialize_database(self):
        try:
            self.store.initialize()
            interrupted = self.store.recover_interrupted()
            aliases = {
                serial: meta.get("alias", "")
                for serial, meta in self.store.device_registry_map().items()
                if meta.get("alias")
            }
            saved_settings = {
                "delay": self.store.get_setting("delay_seconds", "2"),
                "batch_size": self.store.get_setting("batch_size", "100"),
                "cooldown_every": self.store.get_setting("cooldown_every", "25"),
                "cooldown_seconds": self.store.get_setting("cooldown_seconds", "60"),
                "jitter_seconds": self.store.get_setting("jitter_seconds", "0"),
                "country_code": self.store.get_setting("default_country_code", ""),
                "adb_path": self.store.get_setting("adb_executable", self.real_adb.executable),
            }
            self.events.put(("database_ready", interrupted, aliases, saved_settings))
        except Exception as exc:
            self.events.put(("background_error", "Database initialization failed", exc))

    def _select_fonts(self) -> tuple[str, str]:
        sys_name = platform.system().lower()
        if "windows" in sys_name:
            return "Segoe UI", "Consolas"
        if "darwin" in sys_name:
            return "SF Pro Text", "Menlo"
        return "DejaVu Sans", "DejaVu Sans Mono"

    def _build(self):
        ui_font, mono_font = self._select_fonts()
        self.ui_font = ui_font
        self.mono_font = mono_font
        self.root.configure(bg=COLORS["bg"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=COLORS["bg"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["panel"],
            font=(ui_font, 10),
        )
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Card.TFrame", background=COLORS["panel"], relief="solid", borderwidth=1)
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("CardTitle.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=(ui_font, 9))
        style.configure("CardValue.TLabel", background=COLORS["panel"], foreground=COLORS["accent"], font=(ui_font, 16, "bold"))
        style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
        style.configure("Warn.TLabel", background=COLORS["bg"], foreground=COLORS["warn"])
        style.configure("Good.TLabel", background=COLORS["bg"], foreground=COLORS["good"])
        style.configure("Bad.TLabel", background=COLORS["bg"], foreground=COLORS["bad"])
        style.configure("Status.TLabel", background=COLORS["bg"], foreground=COLORS["accent"], font=(ui_font, 10, "bold"))
        style.configure("TLabelFrame", background=COLORS["bg"], foreground=COLORS["accent"], bordercolor=COLORS["border"])
        style.configure(
            "TLabelframe.Label",
            background=COLORS["bg"],
            foreground=COLORS["accent"],
            font=(ui_font, 10, "bold"),
        )
        style.configure("TNotebook", background=COLORS["bg"], bordercolor=COLORS["border"])
        style.configure(
            "TNotebook.Tab",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            padding=(12, 7),
            font=(ui_font, 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["electric"]), ("active", COLORS["panel_alt"])],
            foreground=[("selected", COLORS["text"]), ("active", COLORS["text"])],
        )
        style.configure(
            "TButton",
            padding=(10, 6),
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            focusthickness=2,
            focuscolor=COLORS["accent"],
        )
        style.map(
            "TButton",
            background=[("active", COLORS["electric"]), ("disabled", COLORS["panel"])],
            foreground=[("disabled", "#64748B")],
        )
        style.configure(
            "Accent.TButton",
            background=COLORS["accent"],
            foreground=COLORS["accent_text"],
            font=(ui_font, 10, "bold"),
            focusthickness=2,
            focuscolor=COLORS["text"],
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#22D3EE"), ("disabled", COLORS["panel_alt"])],
            foreground=[("disabled", "#64748B")],
        )
        style.configure(
            "Hover.TButton",
            background=COLORS["electric"],
            foreground=COLORS["text"],
            bordercolor=COLORS["accent"],
        )
        style.configure(
            "AccentHover.TButton",
            background="#22D3EE",
            foreground=COLORS["accent_text"],
            font=(ui_font, 10, "bold"),
            bordercolor=COLORS["text"],
        )
        style.configure("Danger.TButton", background="#7F1D1D", foreground=COLORS["text"])
        style.map(
            "Danger.TButton",
            background=[("active", COLORS["bad"]), ("disabled", COLORS["panel"])],
            foreground=[("disabled", "#64748B")],
        )
        style.configure(
            "DangerHover.TButton",
            background=COLORS["bad"],
            foreground=COLORS["text"],
            bordercolor="#FCA5A5",
        )
        style.configure("TEntry", padding=5, insertcolor=COLORS["text"], fieldbackground=COLORS["panel"], foreground=COLORS["text"])
        style.map(
            "TEntry",
            fieldbackground=[("disabled", COLORS["bg"]), ("readonly", COLORS["panel"])],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure(
            "TCombobox",
            padding=5,
            fieldbackground=COLORS["panel"],
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["accent"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLORS["panel"]), ("disabled", COLORS["bg"])],
            foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["muted"])],
        )
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            rowheight=26,
            bordercolor=COLORS["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["electric"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            padding=(6, 6),
            font=(ui_font, 9, "bold"),
        )

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Top Application Header + Non-Blocking Notification Banner
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            header,
            text="Consent-first SMS Desk",
            font=(ui_font, 18, "bold"),
            foreground=COLORS["accent"],
        ).pack(side="left")
        self.mode_badge = ttk.Label(
            header,
            text="[MODE: MANUAL ADB COMPOSER STAGING · OPT-IN ONLY]",
            style="Muted.TLabel",
        )
        self.mode_badge.pack(side="left", padx=14)
        self.state_label = ttk.Label(header, text="[STATE: IDLE]", style="Status.TLabel")
        self.state_label.pack(side="right", padx=6)
        self.countdown_label = ttk.Label(header, text="Timer: Idle", style="Muted.TLabel")
        self.countdown_label.pack(side="right", padx=12)

        self.banner_var = tk.StringVar(
            value="Ready. Operating mode: Manual Composer Staging (ADB opens SMS app; operator must press Send on device and confirm)."
        )
        self.banner_label = ttk.Label(outer, textvariable=self.banner_var, style="Muted.TLabel", wraplength=1180)
        self.banner_label.pack(fill="x", pady=(0, 6))

        # Main 8-Tab Notebook + Persistent Bottom Console
        main_panes = ttk.Panedwindow(outer, orient="vertical")
        main_panes.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(main_panes)
        main_panes.add(self.notebook, weight=4)

        self.tab_dashboard = ttk.Frame(self.notebook, padding=8)
        self.tab_devices = ttk.Frame(self.notebook, padding=8)
        self.tab_campaigns = ttk.Frame(self.notebook, padding=8)
        self.tab_composer = ttk.Frame(self.notebook, padding=8)
        self.tab_execution = ttk.Frame(self.notebook, padding=8)
        self.tab_history = ttk.Frame(self.notebook, padding=8)
        self.tab_settings = ttk.Frame(self.notebook, padding=8)
        self.tab_diagnostics = ttk.Frame(self.notebook, padding=8)

        self.notebook.add(self.tab_dashboard, text="1. Dashboard")
        self.notebook.add(self.tab_devices, text="2. Devices")
        self.notebook.add(self.tab_campaigns, text="3. Campaigns & Batches")
        self.notebook.add(self.tab_composer, text="4. Composer & Templates")
        self.notebook.add(self.tab_execution, text="5. Live Execution")
        self.notebook.add(self.tab_history, text="6. History & Reports")
        self.notebook.add(self.tab_settings, text="7. Settings")
        self.notebook.add(self.tab_diagnostics, text="8. Diagnostics & Help")

        self._build_dashboard_And_execution_shared_vars()
        self._build_tab_dashboard()
        self._build_tab_devices()
        self._build_tab_campaigns()
        self._build_tab_composer()
        self._build_tab_execution()
        self._build_tab_history()
        self._build_tab_settings()
        self._build_tab_diagnostics()

        # Persistent Activity / Execution Console Panel
        console_frame = ttk.LabelFrame(main_panes, text="Persistent Activity & Execution Console", padding=5)
        main_panes.add(console_frame, weight=1)
        self.console = tk.Text(
            console_frame,
            height=6,
            state="disabled",
            wrap="word",
            bg=COLORS["console"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent"],
            font=(mono_font, 9),
            relief="flat",
        )
        self.console.tag_configure("INFO", foreground=COLORS["text"])
        self.console.tag_configure("SUCCESS", foreground=COLORS["good"])
        self.console.tag_configure("WARN", foreground=COLORS["warn"])
        self.console.tag_configure("ERROR", foreground=COLORS["bad"])
        console_scroll = ttk.Scrollbar(console_frame, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=console_scroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_scroll.pack(side="right", fill="y")

        self._log("INFO", "Ready. Import only recipients with documented prior opt-in; honor opt-outs immediately.")
        self._resize_layout()

    def _build_dashboard_And_execution_shared_vars(self):
        self.delay_var = tk.StringVar(value="2")
        self.batch_var = tk.StringVar(value="100")
        self.cooldown_every_var = tk.StringVar(value="25")
        self.cooldown_var = tk.StringVar(value="60")
        self.jitter_var = tk.StringVar(value="0")
        self.country_code_var = tk.StringVar(value="")
        self.adb_path_var = tk.StringVar(value=self.real_adb.executable)
        self.campaign_name_var = tk.StringVar(value=f"Campaign {datetime.now().strftime('%Y-%m-%d')}")
        self.device_var = tk.StringVar()
        self.eta_var = tk.StringVar(
            value="Approx. remaining time: 0s (Assumes ~5s manual operator review/tap per recipient + configured delay/cooldowns)"
        )
        self.last_op_var = tk.StringVar(value="Last operation: Application initialized")
        self.conn_warning_var = tk.StringVar(value="Device check pending…")

    def _build_tab_dashboard(self):
        # Summary KPI cards row
        kpi_frame = ttk.Frame(self.tab_dashboard)
        kpi_frame.pack(fill="x", pady=(0, 8))
        self.kpi_labels: dict[str, ttk.Label] = {}
        metrics = [
            ("usable_devices", "Usable Devices"),
            ("campaigns", "Campaigns"),
            ("batches", "Batches"),
            ("pending", "Pending"),
            ("staged", "Staged"),
            ("sent", "Sent (Confirmed)"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
            ("uncertain", "Uncertain"),
        ]
        for idx, (key, title) in enumerate(metrics):
            kpi_frame.grid_columnconfigure(idx, weight=1)
            card = ttk.Frame(kpi_frame, style="Card.TFrame", padding=8)
            card.grid(row=0, column=idx, sticky="nsew", padx=3, pady=2)
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            val_lbl = ttk.Label(card, text="0", style="CardValue.TLabel")
            val_lbl.pack(anchor="w", pady=(2, 0))
            self.kpi_labels[key] = val_lbl

        # Quick status & responsive workspace panels
        top = ttk.Frame(self.tab_dashboard)
        top.pack(fill="x", pady=(0, 6))
        self.top_panels = top
        top.grid_columnconfigure(0, weight=1, uniform="top-panels")
        top.grid_columnconfigure(1, weight=1, uniform="top-panels")

        left = ttk.LabelFrame(top, text="Recipients & Consent Quick Actions", padding=10)
        self.recipient_frame = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=3)
        ttk.Label(left, text="Import CSV: required phone and per-row consent or opt_in columns").pack(anchor="w")
        ttk.Label(
            left,
            text="Only yes / true / 1 / opted-in rows are eligible; suppressed numbers stay blocked.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 5))
        row = ttk.Frame(left)
        row.pack(fill="x")
        self.import_button = ttk.Button(row, text="Import Consented CSV…", command=self.import_csv, state="disabled")
        self.import_button.pack(side="left")
        self._bind_button_hover(self.import_button)
        ToolTip(self.import_button, "Load a UTF-8 CSV containing 'phone' and 'consent'/'opt_in' columns.")

        self.recipient_label = ttk.Label(row, text="0 eligible recipients", padding=(10, 5))
        self.recipient_label.pack(side="left")

        opt = ttk.Frame(left)
        opt.pack(fill="x", pady=(8, 0))
        ttk.Label(opt, text="Suppress / opt-out phone:").pack(side="left")
        self.suppress_entry = ttk.Entry(opt, width=22)
        self.suppress_entry.pack(side="left", padx=5)
        self.suppress_button = ttk.Button(opt, text="Suppress", command=self.suppress_phone, state="disabled")
        self.suppress_button.pack(side="left")
        self._bind_button_hover(self.suppress_button)
        ToolTip(self.suppress_button, "Immediately add a phone number to the persistent opt-out suppression list.")

        right = ttk.LabelFrame(top, text="Active Device, Timing & Status Summary", padding=10)
        self.device_frame = right
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=3)
        devrow = ttk.Frame(right)
        devrow.pack(fill="x")
        ttk.Label(devrow, text="Target ADB device:").pack(side="left")
        self.device_box = ttk.Combobox(devrow, textvariable=self.device_var, state="readonly", width=34)
        self.device_box.pack(side="left", padx=5)
        self.discover_button = ttk.Button(devrow, text="Discover", command=self.discover_devices)
        self.discover_button.pack(side="left")
        self._bind_button_hover(self.discover_button)

        pace = ttk.Frame(right)
        pace.pack(fill="x", pady=(8, 0))
        for label, var, width, tip in (
            ("Batch size", self.batch_var, 6, "Recipients per saved batch (default 100, range 1–1000)."),
            ("Delay (s)", self.delay_var, 6, "Fixed pause after operator acknowledgement before staging next item."),
            ("Cooldown every", self.cooldown_every_var, 6, "Number of user-confirmed sends before a longer cooldown pause."),
            ("Cooldown (s)", self.cooldown_var, 6, "Duration of periodic cooldown in seconds."),
        ):
            lbl = ttk.Label(pace, text=label)
            lbl.pack(side="left", padx=(0, 3))
            ent = ttk.Entry(pace, textvariable=var, width=width)
            ent.pack(side="left", padx=(0, 8))
            ToolTip(ent, tip)

        info_box = ttk.LabelFrame(self.tab_dashboard, text="Operational Status & Warnings", padding=10)
        info_box.pack(fill="x", pady=6)
        ttk.Label(info_box, textvariable=self.last_op_var).pack(anchor="w")
        ttk.Label(info_box, textvariable=self.eta_var, style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(info_box, textvariable=self.conn_warning_var, style="Warn.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(
            info_box,
            text="Stop notice: Stopping halts future staging immediately, but cannot recall an SMS already submitted on the phone.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

    def _build_tab_devices(self):
        top_bar = ttk.Frame(self.tab_devices)
        top_bar.pack(fill="x", pady=(0, 6))
        btn_refresh = ttk.Button(top_bar, text="Refresh Devices Now", command=self.discover_devices, style="Accent.TButton")
        btn_refresh.pack(side="left", padx=(0, 6))
        self._bind_button_hover(btn_refresh, accent=True)

        btn_inspect_msg = ttk.Button(top_bar, text="Check Selected Device Messaging App", command=self.inspect_selected_messaging_app)
        btn_inspect_msg.pack(side="left", padx=4)
        self._bind_button_hover(btn_inspect_msg)
        ToolTip(btn_inspect_msg, "Resolve android.intent.action.SENDTO handler and check default SMS app / chooser status.")

        btn_copy_serial = ttk.Button(top_bar, text="Copy Technical Serial", command=self.copy_selected_serial)
        btn_copy_serial.pack(side="left", padx=4)
        self._bind_button_hover(btn_copy_serial)

        # Device Table
        dev_table_frame = ttk.LabelFrame(self.tab_devices, text="Connected & Known ADB Devices", padding=6)
        dev_table_frame.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("friendly", "alias", "serial", "transport", "state", "category", "last_seen")
        self.device_tree = ttk.Treeview(dev_table_frame, columns=cols, show="headings", height=6)
        for col, title, width in (
            ("friendly", "Brand / Model", 180),
            ("alias", "Saved Alias", 130),
            ("serial", "Technical Serial", 170),
            ("transport", "Transport", 80),
            ("state", "ADB State", 100),
            ("category", "Usability Status", 120),
            ("last_seen", "Last Seen (Local)", 155),
        ):
            self.device_tree.heading(col, text=title)
            self.device_tree.column(col, width=width, minwidth=60, stretch=True)
        dscroll = ttk.Scrollbar(dev_table_frame, orient="vertical", command=self.device_tree.yview)
        self.device_tree.configure(yscrollcommand=dscroll.set)
        self.device_tree.pack(side="left", fill="both", expand=True)
        dscroll.pack(side="right", fill="y")

        # Alias & Messaging App Detail Row
        alias_row = ttk.Frame(self.tab_devices)
        alias_row.pack(fill="x", pady=(0, 8))
        ttk.Label(alias_row, text="Local Alias for Selected Device:").pack(side="left")
        self.alias_entry = ttk.Entry(alias_row, width=24)
        self.alias_entry.pack(side="left", padx=6)
        btn_save_alias = ttk.Button(alias_row, text="Save Alias", command=self.save_device_alias)
        btn_save_alias.pack(side="left")
        self._bind_button_hover(btn_save_alias)
        self.msg_app_diag_var = tk.StringVar(value="Messaging App Diagnostics: Select a device and click 'Check Selected Device Messaging App'.")
        ttk.Label(alias_row, textvariable=self.msg_app_diag_var, style="Muted.TLabel").pack(side="left", padx=14)

        # Wireless ADB Connection Manager (3 Distinct Workflows)
        wireless_box = ttk.LabelFrame(
            self.tab_devices,
            text="Wireless ADB Connection Manager (Hardware/Android version dependent — requires real-device verification)",
            padding=10,
        )
        wireless_box.pack(fill="x")

        # 1. Android 11+ Wireless Debugging Pairing
        pair_row = ttk.Frame(wireless_box)
        pair_row.pack(fill="x", pady=3)
        ttk.Label(pair_row, text="1. Wireless Pairing (Android 11+): IP:PairingPort").pack(side="left")
        self.pair_endpoint_entry = ttk.Entry(pair_row, width=22)
        self.pair_endpoint_entry.pack(side="left", padx=6)
        ToolTip(self.pair_endpoint_entry, "Enter IP and Pairing Port shown in 'Pair device with pairing code' on Android (e.g. 192.168.1.25:39123).")
        ttk.Label(pair_row, text="6-Digit Pairing Code (masked, never saved):").pack(side="left")
        self.pair_code_entry = ttk.Entry(pair_row, width=10, show="•")
        self.pair_code_entry.pack(side="left", padx=6)
        btn_pair = ttk.Button(pair_row, text="Pair Endpoint", command=self.pair_wifi)
        btn_pair.pack(side="left", padx=4)
        self._bind_button_hover(btn_pair)

        # 2. Connect to Paired Endpoint
        conn_row = ttk.Frame(wireless_box)
        conn_row.pack(fill="x", pady=3)
        ttk.Label(conn_row, text="2. Connect Paired Endpoint: IP:ConnectPort").pack(side="left")
        self.wifi_entry = ttk.Entry(conn_row, width=22)
        self.wifi_entry.pack(side="left", padx=6)
        ToolTip(self.wifi_entry, "Enter IP and main Wireless Debugging port (note: Android 11+ uses dynamic ports, not always 5555).")
        self.connect_button = ttk.Button(conn_row, text="Connect", command=self.connect_wifi)
        self.connect_button.pack(side="left", padx=4)
        self._bind_button_hover(self.connect_button)
        btn_reconn = ttk.Button(conn_row, text="Reconnect with Backoff", command=self.reconnect_wifi_backoff)
        btn_reconn.pack(side="left", padx=4)
        self._bind_button_hover(btn_reconn)
        btn_cancel_reconn = ttk.Button(conn_row, text="Cancel Reconnect", command=lambda: self._wifi_reconnect_cancel.set())
        btn_cancel_reconn.pack(side="left", padx=4)
        self._bind_button_hover(btn_cancel_reconn)

        # 3. Legacy USB-Initialized TCP/IP Mode
        tcp_row = ttk.Frame(wireless_box)
        tcp_row.pack(fill="x", pady=3)
        ttk.Label(tcp_row, text="3. Legacy USB-to-TCP/IP Initialization — Port:").pack(side="left")
        self.tcpip_port_var = tk.StringVar(value="5555")
        ttk.Entry(tcp_row, textvariable=self.tcpip_port_var, width=8).pack(side="left", padx=6)
        btn_tcpip = ttk.Button(tcp_row, text="Enable TCP/IP on Selected USB Device", command=self.enable_usb_tcpip)
        btn_tcpip.pack(side="left", padx=4)
        self._bind_button_hover(btn_tcpip)

    def _build_tab_campaigns(self):
        # Top CSV Import & Column Mapping Preview
        csv_frame = ttk.LabelFrame(self.tab_campaigns, text="CSV Contact Import, Column Mapping & Validation Preview", padding=8)
        csv_frame.pack(fill="x", pady=(0, 8))

        r1 = ttk.Frame(csv_frame)
        r1.pack(fill="x")
        btn_imp = ttk.Button(r1, text="Select & Import Consented CSV…", command=self.import_csv, style="Accent.TButton")
        btn_imp.pack(side="left", padx=(0, 8))
        self._bind_button_hover(btn_imp, accent=True)

        self.btn_export_rejected = ttk.Button(
            r1,
            text="Export Rejected Rows CSV…",
            command=self.export_rejected_rows,
            state="disabled",
        )
        self.btn_export_rejected.pack(side="left", padx=4)
        self._bind_button_hover(self.btn_export_rejected)

        self.import_summary_var = tk.StringVar(
            value="No CSV imported in this session yet. Required columns: phone + consent/opt_in. Optional: name, consent_source, consented_at, custom fields."
        )
        ttk.Label(csv_frame, textvariable=self.import_summary_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        # Campaign Creation & Batch Assignment Controls
        camp_bar = ttk.LabelFrame(self.tab_campaigns, text="Campaigns, Batch Partitioning & Device Assignment", padding=8)
        camp_bar.pack(fill="both", expand=True)

        c_top = ttk.Frame(camp_bar)
        c_top.pack(fill="x", pady=(0, 6))
        ttk.Label(c_top, text="Campaign Name:").pack(side="left")
        ttk.Entry(c_top, textvariable=self.campaign_name_var, width=26).pack(side="left", padx=6)
        ttk.Label(c_top, text="Default Batch Size:").pack(side="left")
        ttk.Entry(c_top, textvariable=self.batch_var, width=6).pack(side="left", padx=6)

        btn_dup_camp = ttk.Button(c_top, text="Duplicate Latest Campaign", command=self.duplicate_selected_campaign)
        btn_dup_camp.pack(side="left", padx=6)
        self._bind_button_hover(btn_dup_camp)

        btn_assign_dev = ttk.Button(c_top, text="Assign Selected Batch to Active Device", command=self.assign_selected_batch_device)
        btn_assign_dev.pack(side="left", padx=4)
        self._bind_button_hover(btn_assign_dev)

        btn_rr = ttk.Button(c_top, text="Preview Round-Robin Device Assignment…", command=self.round_robin_assign_batches)
        btn_rr.pack(side="left", padx=4)
        self._bind_button_hover(btn_rr)

        bcols = ("id", "campaign", "device", "state", "total", "pending", "staged", "sent", "failed", "skipped", "uncertain", "created")
        self.batches_tree = ttk.Treeview(camp_bar, columns=bcols, show="headings", height=10)
        for col, title, width in (
            ("id", "Batch #", 65),
            ("campaign", "Campaign", 150),
            ("device", "Assigned Device", 160),
            ("state", "Batch State", 100),
            ("total", "Total", 60),
            ("pending", "Pending", 65),
            ("staged", "Staged", 65),
            ("sent", "Sent", 65),
            ("failed", "Failed", 65),
            ("skipped", "Skipped", 65),
            ("uncertain", "Uncertain", 75),
            ("created", "Created (Local)", 150),
        ):
            self.batches_tree.heading(col, text=title)
            self.batches_tree.column(col, width=width, minwidth=50, stretch=col in ("campaign", "device"))
        bscroll = ttk.Scrollbar(camp_bar, orient="vertical", command=self.batches_tree.yview)
        self.batches_tree.configure(yscrollcommand=bscroll.set)
        self.batches_tree.pack(side="left", fill="both", expand=True)
        bscroll.pack(side="right", fill="y")

    def _build_tab_composer(self):
        # Saved Templates Bar
        tpl_frame = ttk.LabelFrame(self.tab_composer, text="Saved Message Templates", padding=8)
        tpl_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(tpl_frame, text="Template Name:").pack(side="left")
        self.template_name_var = tk.StringVar()
        ttk.Entry(tpl_frame, textvariable=self.template_name_var, width=22).pack(side="left", padx=6)
        btn_save_tpl = ttk.Button(tpl_frame, text="Save Current Message as Template", command=self.save_current_template)
        btn_save_tpl.pack(side="left", padx=4)
        self._bind_button_hover(btn_save_tpl)

        ttk.Label(tpl_frame, text="Load Saved:").pack(side="left", padx=(14, 4))
        self.template_select_var = tk.StringVar()
        self.template_box = ttk.Combobox(tpl_frame, textvariable=self.template_select_var, state="readonly", width=26)
        self.template_box.pack(side="left", padx=4)
        btn_load_tpl = ttk.Button(tpl_frame, text="Apply Template", command=self.apply_selected_template)
        btn_load_tpl.pack(side="left", padx=4)
        self._bind_button_hover(btn_load_tpl)

        # Single-SMS Composer
        compose = ttk.LabelFrame(
            self.tab_composer,
            text="Single-Segment SMS Composer & Personalization Validator (GSM 03.38 160 septets / UCS-2 70 UTF-16 units)",
            padding=10,
        )
        compose.pack(fill="both", expand=True)
        ttk.Label(
            compose,
            text="Use optional {name} or {custom_column} placeholders. Every recipient's final rendered message is validated individually before batch creation.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        self.message = tk.Text(
            compose,
            height=4,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            undo=True,
            relief="flat",
            font=(self.ui_font, 11),
        )
        self.message.pack(fill="x")
        self.message.bind("<KeyRelease>", self._message_changed)
        self.message.bind("<<Paste>>", lambda _e: self._after(20, self._message_changed))

        bar = ttk.Frame(compose)
        bar.pack(fill="x", pady=(8, 0))
        self.counter = ttk.Label(bar, text=f"0 / {MAX_SMS_CHARS} chars · 0/160 GSM · 0 segments")
        self.counter.pack(side="left")

        self.shorten_button = ttk.Button(
            bar,
            text="Shorten Safely to 1 Segment",
            command=self.shorten_message_explicitly,
            state="disabled",
        )
        self.shorten_button.pack(side="left", padx=10)
        self._bind_button_hover(self.shorten_button)

        btn_preview_pers = ttk.Button(
            bar,
            text="Validate & Preview Personalization",
            command=self.preview_personalization,
        )
        btn_preview_pers.pack(side="left", padx=4)
        self._bind_button_hover(btn_preview_pers)

        self.composer_validation_var = tk.StringVar(
            value="Single-segment enforcement active: messages exceeding 1 SMS segment will show an explicit error and block execution until shortened."
        )
        self.composer_validation_lbl = ttk.Label(compose, textvariable=self.composer_validation_var, style="Muted.TLabel", wraplength=1100)
        self.composer_validation_lbl.pack(anchor="w", pady=(6, 6))

        # Personalization Preview Box
        prev_box = ttk.LabelFrame(compose, text="Personalization & Per-Recipient Segment Check Preview", padding=6)
        prev_box.pack(fill="both", expand=True)
        self.preview_text = tk.Text(
            prev_box,
            height=6,
            state="disabled",
            wrap="word",
            bg=COLORS["console"],
            fg=COLORS["text"],
            font=(self.mono_font, 9),
            relief="flat",
        )
        self.preview_text.pack(fill="both", expand=True)

    def _build_tab_execution(self):
        exec_box = ttk.LabelFrame(
            self.tab_execution,
            text="Live Campaign & Batch Execution Controls (Manual ADB Composer Staging)",
            padding=12,
        )
        exec_box.pack(fill="x", pady=(0, 8))

        ttk.Label(
            exec_box,
            text="Transport Reality: ADB opens the Android SMS compose screen via SENDTO intent. "
            "A human operator must inspect the recipient/body, press Send on the phone, and confirm in this desktop app. "
            "ADB exit code 0 never marks a message sent or delivered.",
            style="Warn.TLabel",
            wraplength=1140,
        ).pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(exec_box)
        buttons.pack(fill="x", pady=4)
        self.start_button = ttk.Button(
            buttons,
            text="Start Staged Campaign / Batch",
            command=self.start,
            style="Accent.TButton",
            state="disabled",
        )
        self.start_button.pack(side="left", padx=4)
        self._bind_button_hover(self.start_button, accent=True)

        self.pause_button = ttk.Button(
            buttons,
            text="Pause",
            command=self.toggle_pause,
            state="disabled",
        )
        self.pause_button.pack(side="left", padx=4)
        self._bind_button_hover(self.pause_button)

        self.resume_exec_button = ttk.Button(
            buttons,
            text="Resume",
            command=self.resume_from_pause,
            state="disabled",
        )
        self.resume_exec_button.pack(side="left", padx=4)
        self._bind_button_hover(self.resume_exec_button)

        self.stop_button = ttk.Button(
            buttons,
            text="Stop Safely",
            command=self.stop,
            state="disabled",
            style="Danger.TButton",
        )
        self.stop_button.pack(side="left", padx=4)
        self._bind_button_hover(self.stop_button, danger=True)

        details = ttk.LabelFrame(self.tab_execution, text="Live Execution Telemetry & Timing", padding=10)
        details.pack(fill="both", expand=True)
        self.exec_active_batch_var = tk.StringVar(value="Active Batch: None | Active Device: None")
        ttk.Label(details, textvariable=self.exec_active_batch_var, font=(self.ui_font, 11, "bold")).pack(anchor="w", pady=2)
        ttk.Label(details, textvariable=self.last_op_var).pack(anchor="w", pady=2)
        ttk.Label(details, textvariable=self.eta_var, style="Muted.TLabel").pack(anchor="w", pady=2)
        ttk.Label(
            details,
            text="Timing & Cooldown Contract: Fixed delay starts AFTER operator acknowledgement. "
            "Pause freezes remaining wait countdowns. Each cooldown boundary triggers at most once per confirmed count.",
            style="Muted.TLabel",
            wraplength=1120,
        ).pack(anchor="w", pady=(8, 0))

    def _build_tab_history(self):
        # Search, Filter & Recovery Controls
        filter_bar = ttk.Frame(self.tab_history)
        filter_bar.pack(fill="x", pady=(0, 6))

        ttk.Label(filter_bar, text="Search:").pack(side="left")
        self.history_search_var = tk.StringVar()
        ent_search = ttk.Entry(filter_bar, textvariable=self.history_search_var, width=20)
        ent_search.pack(side="left", padx=4)
        ent_search.bind("<Return>", lambda _e: self.refresh_history())

        ttk.Label(filter_bar, text="Status:").pack(side="left", padx=(8, 2))
        self.history_status_var = tk.StringVar(value="all")
        status_cb = ttk.Combobox(
            filter_bar,
            textvariable=self.history_status_var,
            values=("all", "pending", "staged", "sent", "failed", "skipped", "uncertain"),
            state="readonly",
            width=12,
        )
        status_cb.pack(side="left", padx=4)
        status_cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_history())

        btn_filter = ttk.Button(filter_bar, text="Apply Filter", command=self.refresh_history)
        btn_filter.pack(side="left", padx=4)
        self._bind_button_hover(btn_filter)

        self.redact_phones_var = tk.BooleanVar(value=True)
        self.redact_msgs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_bar, text="Mask Phones on Export", variable=self.redact_phones_var).pack(side="left", padx=6)
        ttk.Checkbutton(filter_bar, text="Mask Bodies on Export", variable=self.redact_msgs_var).pack(side="left", padx=4)

        btn_exp_hist = ttk.Button(filter_bar, text="Export Filtered History CSV…", command=self.export_history_csv)
        btn_exp_hist.pack(side="left", padx=4)
        self._bind_button_hover(btn_exp_hist)

        action_bar = ttk.Frame(self.tab_history)
        action_bar.pack(fill="x", pady=(0, 6))
        self.retry_button = ttk.Button(
            action_bar,
            text="Retry Failed Items in Selected Batch (with Eligibility Preview)…",
            command=self.retry_failed,
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(0, 6))
        self._bind_button_hover(self.retry_button)

        self.resume_button = ttk.Button(
            action_bar,
            text="Resume Pending Items in Selected Batch…",
            command=self.resume_pending,
            state="disabled",
        )
        self.resume_button.pack(side="left", padx=4)
        self._bind_button_hover(self.resume_button)

        history_frame = ttk.LabelFrame(self.tab_history, text="Auditable Local Message History (UTC stored · Local time displayed)", padding=5)
        history_frame.pack(fill="both", expand=True)
        cols = ("status", "phone", "device", "batch", "created", "updated", "message", "verification", "error")
        self.history_tree = ttk.Treeview(history_frame, columns=cols, show="headings", height=14)
        for col, width in (
            ("status", 145),
            ("phone", 120),
            ("device", 140),
            ("batch", 60),
            ("created", 145),
            ("updated", 145),
            ("message", 210),
            ("verification", 130),
            ("error", 220),
        ):
            self.history_tree.heading(col, text=col.title())
            self.history_tree.column(col, width=width, minwidth=55, stretch=col in ("error", "device", "message"))
        scroll = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_tree.yview)
        hscroll = ttk.Scrollbar(history_frame, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        hscroll.pack(side="bottom", fill="x")

    def _build_tab_settings(self):
        cfg = ttk.LabelFrame(self.tab_settings, text="Operational Timing, Country Code & Transport Configuration", padding=12)
        cfg.pack(fill="x", pady=(0, 8))

        grid = ttk.Frame(cfg)
        grid.pack(fill="x")
        fields = [
            ("Recipients per saved batch (1–1000):", self.batch_var, "Default 100. Smaller final batches are handled automatically."),
            ("Fixed delay between operations (0–3600s):", self.delay_var, "Starts after operator confirmation."),
            ("Bounded scheduling variation / jitter (0–600s):", self.jitter_var, "Optional load-management variation added to fixed delay."),
            ("Successful confirmations before cooldown (0–1000):", self.cooldown_every_var, "0 disables periodic cooldown."),
            ("Cooldown duration (0–86400s):", self.cooldown_var, "Triggered once at each exact confirmation multiple."),
            ("Explicit default country code (optional, e.g. +1):", self.country_code_var, "Leave blank to preserve raw national numbers as-is."),
            ("ADB executable path:", self.adb_path_var, "Path to `adb` binary from Android SDK Platform-Tools."),
        ]
        for row_idx, (label_text, var, tip) in enumerate(fields):
            ttk.Label(grid, text=label_text).grid(row=row_idx, column=0, sticky="w", pady=4, padx=(0, 10))
            ent = ttk.Entry(grid, textvariable=var, width=28)
            ent.grid(row=row_idx, column=1, sticky="w", pady=4)
            ToolTip(ent, tip)
            ttk.Label(grid, text=tip, style="Muted.TLabel").grid(row=row_idx, column=2, sticky="w", padx=10, pady=4)

        btn_row = ttk.Frame(cfg)
        btn_row.pack(fill="x", pady=(10, 0))
        btn_save_cfg = ttk.Button(btn_row, text="Save Settings Locally", command=self.save_settings, style="Accent.TButton")
        btn_save_cfg.pack(side="left", padx=(0, 8))
        self._bind_button_hover(btn_save_cfg, accent=True)

        self.dry_run_var = tk.BooleanVar(value=False)
        chk_dry = ttk.Checkbutton(
            btn_row,
            text="Enable Dry-Run Mode (Mocked ADB Transport — No Real Device Needed)",
            variable=self.dry_run_var,
            command=self.toggle_dry_run_mode,
        )
        chk_dry.pack(side="left", padx=8)

        db_box = ttk.LabelFrame(self.tab_settings, text="SQLite Database Safety, Backup & Validated Restore", padding=12)
        db_box.pack(fill="x")
        ttk.Label(
            db_box,
            text="Backups use SQLite's online backup API for transactional consistency. Schema migrations automatically create a pre-migration backup.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        db_btns = ttk.Frame(db_box)
        db_btns.pack(fill="x")
        btn_bk = ttk.Button(db_btns, text="Back Up Database Now…", command=self.backup_database_action)
        btn_bk.pack(side="left", padx=(0, 8))
        self._bind_button_hover(btn_bk)
        btn_rs = ttk.Button(db_btns, text="Restore Database from Backup…", command=self.restore_database_action)
        btn_rs.pack(side="left", padx=4)
        self._bind_button_hover(btn_rs)

    def _build_tab_diagnostics(self):
        top_diag = ttk.LabelFrame(self.tab_diagnostics, text="Environment, Entry Point & First-Run Setup Checklist", padding=10)
        top_diag.pack(fill="x", pady=(0, 8))

        diag_text = (
            f"Application Version: {__version__} (Build {BUILD_ID})  |  Schema Version: v{SCHEMA_VERSION}\n"
            f"Launched Entry Point: {Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else 'interactive'}\n"
            f"Working Directory: {Path.cwd()}\n"
            f"Python Interpreter: {sys.executable} ({platform.python_version()})\n"
            f"Database Location: {self.store.path.resolve()}\n"
            f"Crash Reports Folder: {self.reporter.directory.resolve()}"
        )
        ttk.Label(top_diag, text=diag_text, font=(self.mono_font, 9)).pack(anchor="w")

        d_btns = ttk.Frame(top_diag)
        d_btns.pack(fill="x", pady=(8, 0))
        btn_check_adb = ttk.Button(d_btns, text="Run ADB & Setup Checklist Diagnostics", command=self.run_adb_diagnostics, style="Accent.TButton")
        btn_check_adb.pack(side="left", padx=(0, 6))
        self._bind_button_hover(btn_check_adb, accent=True)

        btn_open_diag = ttk.Button(d_btns, text="Open Diagnostics / Crash Reports Folder", command=self.open_diagnostics_folder)
        btn_open_diag.pack(side="left", padx=4)
        self._bind_button_hover(btn_open_diag)

        btn_bundle = ttk.Button(d_btns, text="Create Sanitized Support Bundle (.zip)…", command=self.export_support_bundle)
        btn_bundle.pack(side="left", padx=4)
        self._bind_button_hover(btn_bundle)

        guide_frame = ttk.LabelFrame(self.tab_diagnostics, text="First-Run Checklist, User Guide & Sanitized Operator Audit Trail", padding=8)
        guide_frame.pack(fill="both", expand=True)
        self.diag_output = tk.Text(
            guide_frame,
            height=12,
            state="normal",
            wrap="word",
            bg=COLORS["console"],
            fg=COLORS["text"],
            font=(self.mono_font, 9),
            relief="flat",
        )
        self.diag_output.insert(
            "1.0",
            "FIRST-RUN SETUP CHECKLIST & TROUBLESHOOTING GUIDE:\n"
            "1. Install Android SDK Platform-Tools (`adb`) and verify it is on PATH or configured in Settings.\n"
            "2. On your Android phone, enable Developer Options -> USB Debugging (or Wireless Debugging on Android 11+).\n"
            "3. Connect the phone and tap 'Allow USB debugging' on the phone screen when prompted.\n"
            "4. Prepare a UTF-8 CSV with required `phone` and `consent` (or `opt_in`) columns. Accepted opt-in values: yes, true, 1, opted-in.\n"
            "5. Compose a single-segment SMS (<=160 GSM septets or <=70 UCS-2 units) and review any {name} personalization.\n"
            "6. Click 'Start Staged Campaign / Batch'. For each recipient, inspect the opened Android compose screen, press Send on the phone, and confirm in the desktop prompt.\n"
            "7. If interrupted, use 'Resume Pending' only after checking Android's Sent history so you never double-send.\n",
        )
        self.diag_output.configure(state="disabled")
        self.diag_output.pack(fill="both", expand=True)

    def _resize_layout(self, _event=None):
        """Use side-by-side cards on wide windows and stack them on narrow ones."""
        if not hasattr(self, "top_panels"):
            return
        try:
            width = self.root.winfo_width()
        except Exception:
            return
        two_columns = width >= 1040
        if two_columns == self._layout_two_columns:
            return
        self._layout_two_columns = two_columns
        self.recipient_frame.grid_forget()
        self.device_frame.grid_forget()
        if two_columns:
            self.top_panels.grid_columnconfigure(0, weight=1, uniform="top-panels")
            self.top_panels.grid_columnconfigure(1, weight=1, uniform="top-panels")
            self.recipient_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=3)
            self.device_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=3)
        else:
            self.top_panels.grid_columnconfigure(0, weight=1)
            self.top_panels.grid_columnconfigure(1, weight=0)
            self.recipient_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0, pady=3)
            self.device_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=3)

    def _device_label(self, device: Device, *, include_serial: bool = True) -> str:
        aliases = getattr(self, "device_aliases", {})
        alias = aliases.get(device.serial, getattr(device, "alias", ""))
        name = alias.strip() if alias and alias.strip() else device.friendly_name
        return f"{name} ({device.serial})" if include_serial else name

    def _history_device_label(self, serial: str) -> str:
        device = self.devices.get(serial)
        if device and device.friendly_name != "Android device":
            self.device_names[serial] = device.friendly_name
        aliases = getattr(self, "device_aliases", {})
        alias = aliases.get(serial, "")
        name = alias or self.device_names.get(serial)
        return f"{name} ({serial})" if name else serial

    def _bind_button_hover(
        self,
        button: ttk.Button,
        *,
        accent: bool = False,
        danger: bool = False,
    ) -> None:
        base_style = "Accent.TButton" if accent else "Danger.TButton" if danger else "TButton"
        hover_style = (
            "AccentHover.TButton" if accent else "DangerHover.TButton" if danger else "Hover.TButton"
        )
        button.configure(style=base_style)

        def enter(_event):
            if not button.instate(("disabled",)):
                button.configure(style=hover_style)

        def leave(_event):
            button.configure(style=base_style)

        button.bind("<Enter>", enter, add="+")
        button.bind("<Leave>", leave, add="+")

    def _log(self, level: str, text: str):
        self.last_meaningful_op = text
        if hasattr(self, "last_op_var"):
            self.last_op_var.set(f"Last operation: {text}")
        if hasattr(self, "banner_var") and level in ("WARN", "ERROR"):
            self.banner_var.set(f"[{level}] {text}")
        self.console.configure(state="normal")
        self.console.insert("end", f"{datetime.now().strftime('%H:%M:%S')} [{level}] {text}\n", level)
        # Bound console lines to prevent unbounded memory growth
        try:
            line_count = int(self.console.index("end-1c").split(".")[0])
            if line_count > MAX_CONSOLE_LINES:
                self.console.delete("1.0", f"{line_count - MAX_CONSOLE_LINES}.0")
        except Exception:
            pass
        self.console.see("end")
        self.console.configure(state="disabled")

    def _message_changed(self, _event=None):
        """Validate live message typing/pasting without silently truncating user content."""
        text = self.message.get("1.0", "end-1c")
        metrics = analyze_sms_segments(text)
        encoding, units, capacity = metrics.encoding, metrics.units, metrics.single_capacity
        exceeds = metrics.exceeds_single

        status_tag = "[OVER LIMIT - BLOCKED]" if exceeds else "[SINGLE SMS OK]"
        self.counter.configure(
            text=f"{status_tag} {metrics.chars} chars · {units}/{capacity} {encoding} units · {metrics.segments} segment(s) (ext chars: {metrics.extension_chars})",
            foreground=COLORS["bad"] if exceeds else COLORS["text"],
        )
        if hasattr(self, "shorten_button"):
            self.shorten_button.configure(state="normal" if exceeds else "disabled")
        if hasattr(self, "composer_validation_var"):
            if exceeds:
                self.composer_validation_var.set(
                    f"ERROR: Message uses {units}/{capacity} {encoding} units ({metrics.chars} chars, {metrics.segments} segments). "
                    "Single-segment mode blocks execution above 1 segment. Click 'Shorten Safely to 1 Segment' or edit text."
                )
            else:
                vars_found = extract_template_variables(text)
                var_note = f" Detected placeholders: {', '.join(vars_found)}." if vars_found else ""
                self.composer_validation_var.set(
                    f"Single-segment validation passed ({units}/{capacity} {encoding} units).{var_note}"
                )

    def shorten_message_explicitly(self):
        """Explicit user-invoked action to truncate an oversized message to 1 SMS segment."""
        text = self.message.get("1.0", "end-1c")
        shortened = safe_truncate_message(text)
        if shortened != text:
            self.message.delete("1.0", "end")
            self.message.insert("1.0", shortened)
            self._message_changed()
            self._log("WARN", "Message explicitly shortened to fit within a single SMS segment.")

    def preview_personalization(self):
        template = self.message.get("1.0", "end-1c")
        if not template.strip():
            messagebox.showwarning("Blank template", "Enter a message or template before previewing.", parent=self.root)
            return
        sample_phones = self.recipients[:25] if self.recipients else ["+14155550100"]
        vars_map = self.recipient_metadata if self.recipients else {"+14155550100": {"variables": {"name": "Alex", "phone": "+14155550100"}}}
        pairs = [(p, (vars_map.get(p, {}).get("variables") or {"phone": p})) for p in sample_phones]
        try:
            report = validate_personalized_batch(template, pairs)
        except ValidationError as exc:
            messagebox.showerror("Validation error", str(exc), parent=self.root)
            return

        lines = [
            f"Checked {len(pairs)} recipient(s) | Valid single-SMS: {len(report['valid_messages'])} | "
            f"Missing placeholders: {len(report['missing_vars_by_phone'])} | Segment overflows: {len(report['overflow_by_phone'])}"
        ]
        for phone, rendered, metrics in report["previews"][:5]:
            lines.append(
                f"  • {phone}: [{metrics.encoding} {metrics.units}/{metrics.single_capacity} units, {metrics.segments} seg] -> {rendered}"
            )
        for phone, missing in list(report["missing_vars_by_phone"].items())[:5]:
            lines.append(f"  ! {phone}: Missing variable(s): {', '.join(missing)}")
        for phone, err in list(report["overflow_by_phone"].items())[:5]:
            lines.append(f"  ! {phone}: Overflow error: {err}")

        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", "\n".join(lines))
        self.preview_text.configure(state="disabled")

    def save_current_template(self):
        name = self.template_name_var.get().strip()
        body = self.message.get("1.0", "end-1c")
        try:
            validate_message(body)
            self.store.save_template(name, body)
            self.store.record_audit("save_template", f"Saved template '{name}'")
            self._refresh_templates()
            self._log("SUCCESS", f"Saved message template '{name}'.")
        except Exception as exc:
            messagebox.showerror("Could not save template", str(exc), parent=self.root)

    def apply_selected_template(self):
        selected = self.template_select_var.get()
        if not selected:
            return
        for tpl in self.store.list_templates():
            if tpl["name"] == selected:
                self.message.delete("1.0", "end")
                self.message.insert("1.0", tpl["body"])
                self._message_changed()
                self._log("INFO", f"Loaded template '{selected}'.")
                break

    def _refresh_templates(self):
        if not self.database_ready:
            return
        templates = self.store.list_templates()
        names = [t["name"] for t in templates]
        self.template_box.configure(values=names)
        if names and not self.template_select_var.get():
            self.template_select_var.set(names[0])

    def import_csv(self):
        path = filedialog.askopenfilename(
            title="Import consented recipients",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self.import_button.configure(state="disabled")
        self._log("INFO", "Reading CSV and checking per-row consent…")
        default_cc = self.country_code_var.get().strip()
        batch_sz_str = self.batch_var.get().strip() or "100"

        def task():
            try:
                csv_path = Path(path)
                if csv_path.stat().st_size > DEFAULT_MAX_CSV_BYTES:
                    raise ValidationError("CSV exceeds the 20 MiB import limit.")
                text = csv_path.read_text(encoding="utf-8-sig")
                parsed = parse_contacts_csv(text, default_country_code=default_cc)
                phones = [item.phone for item in parsed.recipients]
                meta = {
                    item.phone: {
                        "name": item.name,
                        "consent_source": item.consent_source or csv_path.name,
                        "consented_at": item.consented_at,
                        "variables": item.variables,
                    }
                    for item in parsed.recipients
                }
                inserted, suppressed = self.store.import_consented(phones, csv_path.name, metadata=meta)
                eligible = self.store.eligible(phones)
                self.store.record_audit(
                    "import_csv",
                    f"File={csv_path.name} valid={len(parsed.recipients)} inserted={inserted} suppressed={suppressed} rejected={len(parsed.rejected)}",
                )
                self.events.put(("imported", parsed, inserted, suppressed, eligible, meta, batch_sz_str))
            except Exception as exc:
                self.events.put(("background_error", "CSV import failed", exc))

        self._spawn_background(task, "csv-import")

    def export_rejected_rows(self):
        if not self.last_rejected_rows:
            messagebox.showinfo("No rejected rows", "There are no rejected CSV rows from the last import.", parent=self.root)
            return
        dest = filedialog.asksaveasfilename(
            title="Export rejected CSV rows",
            defaultextension=".csv",
            initialfile="rejected_recipients.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not dest:
            return
        try:
            content = format_rejected_rows_csv(self.last_rejected_rows)
            Path(dest).write_text(content, encoding="utf-8")
            self._log("SUCCESS", f"Exported {len(self.last_rejected_rows)} rejected row(s) to {dest}.")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.root)

    def suppress_phone(self):
        try:
            phone = normalize_phone(
                self.suppress_entry.get(),
                default_country_code=self.country_code_var.get().strip(),
            )
            self.suppress_entry.delete(0, "end")
            self._log("INFO", "Saving suppression…")

            def task():
                try:
                    self.store.suppress(phone)
                    self.store.record_audit("suppress_phone", "Suppressed recipient ending " + phone[-4:])
                    self.events.put(("suppressed", phone))
                except Exception as exc:
                    self.events.put(("background_error", "Could not save suppression", exc))

            self._spawn_background(task, "suppression-update")
        except Exception as exc:
            messagebox.showerror("Invalid phone", str(exc), parent=self.root)

    def _periodic_device_health_check(self):
        if self.closing:
            return
        if self._health_poll_enabled and not (self.active_worker and self.active_worker.is_alive()):
            self.discover_devices()
        self._after(30000, self._periodic_device_health_check)

    def discover_devices(self):
        def task():
            try:
                discovered = self.adb.devices()
                devices = []
                for device in discovered:
                    if device.usable:
                        try:
                            brand, model = self.adb.device_identity(device.serial)
                            device = replace(device, brand=brand, model=model)
                        except AdbError:
                            pass
                    if self.database_ready:
                        try:
                            self.store.save_device_metadata(
                                device.serial,
                                brand=device.brand,
                                model=device.model,
                                seen=True,
                                checked=device.usable,
                            )
                        except Exception:
                            pass
                    devices.append(device)
                self.events.put(("devices", devices))
            except Exception as exc:
                self.events.put(("background_error", "ADB discovery failed", exc))

        self._spawn_background(task, "adb-discovery")

    def save_device_alias(self):
        sel = self.device_tree.selection()
        if not sel:
            messagebox.showinfo("Select a device", "Select a device in the Devices table first.", parent=self.root)
            return
        serial = str(sel[0])
        alias = self.alias_entry.get().strip()
        self.device_aliases[serial] = alias
        if self.database_ready:
            self.store.save_device_metadata(serial, alias=alias)
            self.store.record_audit("set_device_alias", f"Device {serial} alias set to '{alias}'")
        self.discover_devices()

    def copy_selected_serial(self):
        sel = self.device_tree.selection()
        serial = str(sel[0]) if sel else self._resolve_selected_serial()
        if not serial:
            messagebox.showinfo("No device selected", "Select a device first.", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(serial)
        self._log("INFO", f"Copied technical ADB serial to clipboard: {serial}")

    def inspect_selected_messaging_app(self):
        serial = self._resolve_selected_serial()
        if not serial:
            messagebox.showinfo("Select a device", "Select a usable ADB device first.", parent=self.root)
            return

        def task():
            try:
                info = self.adb.inspect_messaging_app(serial)
                self.events.put(("messaging_app_info", serial, info))
            except Exception as exc:
                self.events.put(("background_error", "Messaging app inspection failed", exc))

        self._spawn_background(task, "adb-msg-inspect")

    def pair_wifi(self):
        try:
            endpoint = validate_ip_port(self.pair_endpoint_entry.get())
            code = validate_pairing_code(self.pair_code_entry.get())
        except ValidationError as exc:
            messagebox.showerror("Invalid pairing input", str(exc), parent=self.root)
            return
        # Clear pairing code immediately from entry widget so it is never retained
        self.pair_code_entry.delete(0, "end")

        def task():
            try:
                result = self.adb.pair(endpoint, code)
                self.events.put(("log", "SUCCESS", f"Wireless pairing result: {result}"))
                self.events.put(("discover_again",))
            except Exception as exc:
                self.events.put(("background_error", "Wireless ADB pairing failed", exc))

        self._spawn_background(task, "adb-pair")

    def connect_wifi(self):
        try:
            endpoint = validate_ip_port(self.wifi_entry.get())
        except ValidationError as exc:
            messagebox.showerror("Invalid Wi-Fi endpoint", str(exc), parent=self.root)
            return

        def task():
            try:
                result = self.adb.connect(endpoint)
                self.events.put(("log", "INFO", result or "ADB connect request completed"))
                self.events.put(("discover_again",))
            except Exception as exc:
                self.events.put(("background_error", "Wi-Fi ADB connection failed", exc))

        self._spawn_background(task, "adb-connect")

    def reconnect_wifi_backoff(self):
        try:
            endpoint = validate_ip_port(self.wifi_entry.get())
        except ValidationError as exc:
            messagebox.showerror("Invalid Wi-Fi endpoint", str(exc), parent=self.root)
            return
        self._wifi_reconnect_cancel.clear()
        self._log("INFO", f"Starting bounded reconnect with backoff to {endpoint} (does not resubmit uncertain jobs)…")

        def task():
            try:
                result = self.adb.reconnect_with_backoff(
                    endpoint,
                    max_attempts=3,
                    base_delay=1.0,
                    cancel_check=self._wifi_reconnect_cancel.is_set,
                )
                self.events.put(("log", "SUCCESS", f"Reconnected: {result}"))
                self.events.put(("discover_again",))
            except Exception as exc:
                self.events.put(("background_error", "Wi-Fi reconnect failed", exc))

        self._spawn_background(task, "adb-reconnect")

    def enable_usb_tcpip(self):
        serial = self._resolve_selected_serial()
        if not serial:
            messagebox.showerror("No USB device", "Select an authorized USB device first.", parent=self.root)
            return
        try:
            port = validate_port(self.tcpip_port_var.get(), label="TCP/IP port")
        except ValidationError as exc:
            messagebox.showerror("Invalid port", str(exc), parent=self.root)
            return

        def task():
            try:
                res = self.adb.enable_tcpip(serial, port)
                self.events.put(("log", "SUCCESS", f"Enabled TCP/IP port {port} on {serial}: {res}"))
            except Exception as exc:
                self.events.put(("background_error", "TCP/IP initialization failed", exc))

        self._spawn_background(task, "adb-tcpip")

    def _resolve_selected_serial(self) -> str:
        selected = self.device_var.get()
        return getattr(self, "_device_display_to_serial", {}).get(selected, selected)

    def duplicate_selected_campaign(self):
        if not self.database_ready:
            return
        campaigns = self.store.list_campaigns()
        if not campaigns:
            messagebox.showinfo("No campaigns", "Create a campaign first before duplicating.", parent=self.root)
            return
        latest = campaigns[0]
        new_id = self.store.duplicate_campaign(latest["id"])
        self.campaign_name_var.set(f"{latest['name']} (Copy)")
        self.message.delete("1.0", "end")
        self.message.insert("1.0", latest["template_text"])
        self._message_changed()
        self.store.record_audit("duplicate_campaign", f"Duplicated campaign #{latest['id']} -> #{new_id}")
        self._log("SUCCESS", f"Duplicated campaign #{latest['id']} as draft #{new_id}.")
        self._refresh_history_async()

    def assign_selected_batch_device(self):
        sel = self.batches_tree.selection()
        if not sel:
            messagebox.showinfo("Select a batch", "Select a batch in the Campaigns & Batches table first.", parent=self.root)
            return
        serial = self._resolve_selected_serial()
        if not serial:
            messagebox.showerror("Select a device", "Select a target ADB device first.", parent=self.root)
            return
        batch_id = int(sel[0])
        try:
            self.store.assign_batch_device(batch_id, serial)
            self.store.record_audit("assign_batch_device", f"Batch #{batch_id} assigned to {serial}")
            self._log("SUCCESS", f"Assigned batch #{batch_id} to device {serial}.")
            self._refresh_history_async()
        except Exception as exc:
            messagebox.showerror("Assignment failed", str(exc), parent=self.root)

    def round_robin_assign_batches(self):
        usable_serials = [d.serial for d in self.devices.values() if d.usable]
        if not usable_serials:
            messagebox.showwarning("No usable devices", "Connect at least one usable ADB device before round-robin assignment.", parent=self.root)
            return
        batches = [b for b in self.store.list_batches(100) if b["state"] == "pending"]
        if not batches:
            messagebox.showinfo("No pending batches", "There are no pending batches to assign.", parent=self.root)
            return
        plan_lines = []
        assignments: list[tuple[int, str]] = []
        for idx, b in enumerate(reversed(batches)):
            target_ser = usable_serials[idx % len(usable_serials)]
            assignments.append((b["id"], target_ser))
            plan_lines.append(f"Batch #{b['id']} ({b['total']} recipients) -> {self._history_device_label(target_ser)}")
        if not messagebox.askyesno(
            "Confirm Round-Robin Device Assignment",
            "Assign pending batches across usable devices as follows?\n\n" + "\n".join(plan_lines[:15]),
            parent=self.root,
        ):
            return
        for b_id, ser in assignments:
            self.store.assign_batch_device(b_id, ser)
        self.store.record_audit("round_robin_assign", f"Assigned {len(assignments)} pending batches across {len(usable_serials)} device(s)")
        self._log("SUCCESS", f"Assigned {len(assignments)} pending batch(es) across {len(usable_serials)} usable device(s).")
        self._refresh_history_async()

    def start(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning(
                "Batch active",
                "Finish or stop the active batch before starting another.",
                parent=self.root,
            )
            return
        try:
            raw_template = self.message.get("1.0", "end-1c")
            message = validate_message(raw_template)
            delay = bounded_number(self.delay_var.get(), label="Delay", minimum=0, maximum=3600)
            batch_size = int(self.batch_var.get())
            cooldown_every = int(self.cooldown_every_var.get())
            cooldown = bounded_number(self.cooldown_var.get(), label="Cooldown", minimum=0, maximum=86400)
            jitter = bounded_number(self.jitter_var.get() if hasattr(self, "jitter_var") else "0", label="Jitter", minimum=0, maximum=600)
            if not 1 <= batch_size <= 1000 or not 0 <= cooldown_every <= 1000:
                raise ValidationError(
                    "Batch size must be 1–1000 and cooldown interval 0–1000 (0 disables periodic cooldown)."
                )
            serial = self._resolve_selected_serial()
            if not serial or serial not in self.devices or not self.devices[serial].usable:
                raise ValidationError("Select an online, authorized ADB device.")
            if not self.recipients:
                raise ValidationError("Import at least one explicitly consented recipient.")

            # Validate personalization across all recipients if template contains placeholders
            per_recipient_messages: dict[str, str] | None = None
            if extract_template_variables(message):
                vars_by_phone = self.store.contact_variables(self.recipients)
                pairs = [
                    (p, (self.recipient_metadata.get(p, {}).get("variables") or vars_by_phone.get(p, {"phone": p})))
                    for p in self.recipients
                ]
                report = validate_personalized_batch(message, pairs)
                if report["missing_vars_by_phone"]:
                    sample_p, sample_m = next(iter(report["missing_vars_by_phone"].items()))
                    raise ValidationError(
                        f"Personalization failed for {len(report['missing_vars_by_phone'])} recipient(s) "
                        f"(e.g. {sample_p} missing {{{', '.join(sample_m)}}})."
                    )
                if report["overflow_by_phone"]:
                    sample_p, sample_err = next(iter(report["overflow_by_phone"].items()))
                    raise ValidationError(
                        f"Rendered personalized text exceeds 1 SMS segment for {len(report['overflow_by_phone'])} recipient(s) "
                        f"(e.g. {sample_p}: {sample_err})."
                    )
                per_recipient_messages = report["valid_messages"]

            num_batches = (len(self.recipients) + batch_size - 1) // batch_size
            if not messagebox.askyesno(
                "Confirm Consent & Manual Staged Execution",
                f"Create {num_batches} batch(es) (up to {batch_size} recipients/batch) for {len(self.recipients)} opted-in recipients on {self._history_device_label(serial)}?\n\n"
                "ADB will only open the Android compose screen. You must press Send on the device and confirm each message. "
                "No recipient is sent automatically.",
                parent=self.root,
            ):
                return

            self.start_button.configure(state="disabled")
            snapshot = list(self.recipients)
            camp_name = self.campaign_name_var.get().strip() if hasattr(self, "campaign_name_var") else "Campaign"
            self.campaign_sent = 0

            def create():
                try:
                    eligible = self.store.eligible(snapshot)
                    if len(eligible) != len(snapshot):
                        self.events.put(("recipients_changed", eligible))
                        return
                    campaign_id = self.store.create_campaign(camp_name, message, batch_size=batch_size)
                    chunks = [
                        eligible[offset : offset + batch_size]
                        for offset in range(0, len(eligible), batch_size)
                    ]
                    ids = self.store.create_batches(
                        serial,
                        message,
                        chunks,
                        delay,
                        cooldown_every,
                        cooldown,
                        campaign_id=campaign_id,
                        per_recipient_messages=per_recipient_messages,
                        jitter_seconds=jitter,
                    )
                    self.store.record_audit(
                        "create_campaign_batches",
                        f"Campaign #{campaign_id} created {len(ids)} batch(es) for {len(eligible)} recipient(s) on {serial}",
                    )
                    self.events.put(("batches_created", ids))
                except Exception as exc:
                    self.events.put(("background_error", "Could not create batch", exc))

            self._spawn_background(create, "batch-creation")
        except (ValidationError, ValueError) as exc:
            messagebox.showerror("Cannot start batch", str(exc), parent=self.root)
        except Exception as exc:
            self._report_error("Could not create batch", exc)

    def _start_next_batch(self):
        if not self.batch_ids or self.closing:
            self._refresh_history_async()
            return
        if self.machine.state in (RunState.COMPLETED, RunState.FAILED):
            self.machine.transition(RunState.IDLE)
        batch_id = self.batch_ids.pop(0)
        self.active_batch_id = batch_id
        self.controls = WorkerControls.create()
        self.active_worker = DispatchWorker(
            batch_id,
            self.store,
            self.adb,
            self.events,
            self.controls,
            sent_before=self.campaign_sent,
            has_more_batches=bool(self.batch_ids),
        )
        self.active_worker.start()
        self.start_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self._transition_ui(RunState.RUNNING)
        self.pause_button.configure(state="normal")
        if hasattr(self, "resume_exec_button"):
            self.resume_exec_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def toggle_pause(self):
        if not self.controls:
            return
        if self.controls.pause.is_set():
            self.resume_from_pause()
        else:
            self.controls.pause.set()
            self._transition_ui(RunState.PAUSED)
            self.pause_button.configure(state="disabled")
            if hasattr(self, "resume_exec_button"):
                self.resume_exec_button.configure(state="normal")
            self._log(
                "WARN",
                "Pause requested; remaining delay/cooldown timer is frozen and current staged message waits until confirmed or stopped.",
            )

    def resume_from_pause(self):
        if not self.controls:
            return
        if self.controls.pause.is_set():
            self.controls.pause.clear()
            self._transition_ui(RunState.RUNNING)
            self.pause_button.configure(state="normal")
            if hasattr(self, "resume_exec_button"):
                self.resume_exec_button.configure(state="disabled")
            self._log("INFO", "Execution resumed.")

    def stop(self):
        if self.controls:
            self.controls.stop.set()
            self.batch_ids.clear()
            self._transition_ui(RunState.STOPPING)
            self.pause_button.configure(state="disabled")
            if hasattr(self, "resume_exec_button"):
                self.resume_exec_button.configure(state="disabled")
            self._log(
                "WARN",
                "Graceful stop requested; unconfirmed messages remain pending. Note: stopping cannot recall any SMS already tapped Send on the phone.",
            )

    def _set_state(self, state: str):
        color = {
            "running": COLORS["good"],
            "completed": COLORS["good"],
            "paused": COLORS["warn"],
            "stopping": COLORS["warn"],
            "failed": COLORS["bad"],
            "idle": COLORS["accent"],
        }.get(state, COLORS["accent"])
        self.state_label.configure(text=f"[STATE: {state.upper()}]", foreground=color)

    def _transition_ui(self, target: RunState):
        if target == RunState.RUNNING and self.controls and self.controls.pause.is_set():
            return
        if self.machine.state != target:
            try:
                self.machine.transition(target)
            except RuntimeError:
                return
        self._set_state(target.value)

    def _update_dashboard_And_batches(self, counts: dict, batches: list[dict]):
        usable_cnt = sum(1 for d in self.devices.values() if d.usable)
        if hasattr(self, "kpi_labels"):
            self.kpi_labels["usable_devices"].configure(text=str(usable_cnt))
            for k in ("campaigns", "batches", "pending", "staged", "sent", "failed", "skipped", "uncertain"):
                if k in self.kpi_labels:
                    self.kpi_labels[k].configure(text=str(counts.get(k, 0)))

        # Approximate remaining time calculation with explicit assumptions
        pending_total = counts.get("pending", 0) + counts.get("staged", 0)
        try:
            delay_s = float(self.delay_var.get() or 0)
            cd_every = int(self.cooldown_every_var.get() or 0)
            cd_sec = float(self.cooldown_var.get() or 0)
        except ValueError:
            delay_s, cd_every, cd_sec = 2.0, 25, 60.0
        manual_op_sec = 5.0
        est_sec = pending_total * (manual_op_sec + delay_s)
        if cd_every > 0 and pending_total > 0:
            est_sec += (pending_total // cd_every) * cd_sec
        mins, secs = divmod(int(est_sec), 60)
        self.eta_var.set(
            f"Approx. remaining time for {pending_total} pending item(s): {mins}m {secs}s "
            f"(Assumes ~5s manual review/tap per item + {delay_s:g}s delay + {cd_sec:g}s cooldown every {cd_every or 'N/A'} sends)"
        )

        if hasattr(self, "exec_active_batch_var"):
            b_txt = f"#{self.active_batch_id}" if self.active_batch_id else "None"
            d_txt = self._history_device_label(self._resolve_selected_serial()) if self._resolve_selected_serial() else "None"
            self.exec_active_batch_var.set(f"Active Batch: {b_txt}  |  Selected Device: {d_txt}")

        if hasattr(self, "batches_tree"):
            self.batches_tree.delete(*self.batches_tree.get_children())
            for b in batches:
                self.batches_tree.insert(
                    "",
                    "end",
                    iid=str(b["id"]),
                    values=(
                        b["id"],
                        b.get("campaign_name") or f"Campaign #{b.get('campaign_id') or '-'}",
                        self._history_device_label(b["device_serial"]),
                        b["state"].upper(),
                        b["total"],
                        b.get("pending_count", 0),
                        b.get("staged_count", 0),
                        b.get("sent_count", 0),
                        b.get("failed_count", 0),
                        b.get("skipped_count", 0),
                        b.get("uncertain_count", 0),
                        format_local_time(b["created_at"]),
                    ),
                )

    def _poll_events(self):
        if self.closing:
            return
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "state":
                    _, state, message = event
                    target = RunState(state)
                    self._transition_ui(target)
                    self._log("INFO", message)
                    if state == "completed":
                        self._after(100, self._complete_current)
                elif kind == "countdown":
                    _, phase, remaining, is_paused = event
                    if phase == "idle" or remaining <= 0:
                        self.countdown_label.configure(text="Timer: Idle", foreground=COLORS["muted"])
                    else:
                        p_tag = " (PAUSED - FROZEN)" if is_paused else ""
                        self.countdown_label.configure(
                            text=f"Timer [{phase.upper()}]: {remaining:.1f}s{p_tag}",
                            foreground=COLORS["warn"] if is_paused else COLORS["accent"],
                        )
                elif kind == "confirm":
                    _, item_id, phone = event
                    suffix = phone[-4:]
                    answer = messagebox.askyesno(
                        "Confirm recipient sent",
                        f"Android compose was opened for a consented recipient ending {suffix}.\n\n"
                        "After personally checking the number/message and pressing Send in Android, select Yes. "
                        "Select No if you did not send it. This confirmation is user-attested, not device-verified.",
                        parent=self.root,
                    )
                    if self.controls:
                        try:
                            self.controls.ack.put_nowait(bool(answer))
                        except queue.Full:
                            pass
                elif kind == "log":
                    self._log(event[1], event[2])
                elif kind == "devices":
                    self.devices = {device.serial: device for device in event[1]}
                    for device in event[1]:
                        if device.friendly_name != "Android device":
                            self.device_names[device.serial] = device.friendly_name
                    values = [f"{self._device_label(d)} — {d.state}" for d in event[1]]
                    self._device_display_to_serial = {
                        f"{self._device_label(d)} — {d.state}": d.serial for d in event[1]
                    }
                    self.device_box.configure(values=values)
                    if values and self.device_var.get() not in self._device_display_to_serial:
                        self.device_var.set(values[0])

                    # Populate Devices Treeview
                    if hasattr(self, "device_tree"):
                        self.device_tree.delete(*self.device_tree.get_children())
                        reg = self.store.device_registry_map() if self.database_ready else {}
                        for d in event[1]:
                            meta = reg.get(d.serial, {})
                            alias = self.device_aliases.get(d.serial, meta.get("alias", ""))
                            seen_local = format_local_time(meta.get("last_seen")) or "Just now"
                            self.device_tree.insert(
                                "",
                                "end",
                                iid=d.serial,
                                values=(
                                    d.friendly_name,
                                    alias,
                                    d.serial,
                                    d.transport_type.upper(),
                                    d.state,
                                    f"[{d.status_category.upper()}]",
                                    seen_local,
                                ),
                            )

                    usable_list = [d for d in event[1] if d.usable]
                    unauth_list = [d for d in event[1] if d.status_category == "unauthorized"]
                    if unauth_list:
                        self.conn_warning_var.set(
                            f"WARNING: {len(unauth_list)} device(s) unauthorized ({', '.join(d.serial for d in unauth_list)}). Unlock phone and tap 'Allow USB debugging'."
                        )
                    elif not usable_list:
                        self.conn_warning_var.set(
                            "WARNING: No usable ADB devices connected. Connect an Android phone over USB/Wi-Fi or enable Dry-Run mode in Settings."
                        )
                    else:
                        self.conn_warning_var.set(
                            f"OK: {len(usable_list)} usable ADB device(s) ready for manual composer staging."
                        )

                    self._log(
                        "INFO",
                        f"ADB reports {len(values)} device(s): "
                        + (", ".join(f"{self._device_label(d)} — {d.state}" for d in event[1]) or "none"),
                    )
                    if self.database_ready:
                        self._refresh_history_async()
                elif kind == "messaging_app_info":
                    _, serial, info = event
                    chooser_txt = "Chooser prompt required" if info.chooser_required else "Direct SMS handler"
                    pkg_txt = info.default_sms_package or "System default"
                    self.msg_app_diag_var.set(
                        f"[{serial}] Activity: {info.resolved_activity} | Default Pkg: {pkg_txt} | {chooser_txt} | SIM slots: {info.sim_count}"
                    )
                    self._log(
                        "INFO",
                        f"Device {serial} SMS handler: {info.resolved_activity} ({chooser_txt}; SIM slots detected: {info.sim_count}).",
                    )
                elif kind == "discover_again":
                    self.discover_devices()
                elif kind == "database_ready":
                    self.database_ready = True
                    interrupted = event[1]
                    if len(event) >= 4:
                        self.device_aliases.update(event[2] or {})
                        saved = event[3] or {}
                        self.delay_var.set(str(saved.get("delay", "2")))
                        self.batch_var.set(str(saved.get("batch_size", "100")))
                        self.cooldown_every_var.set(str(saved.get("cooldown_every", "25")))
                        self.cooldown_var.set(str(saved.get("cooldown_seconds", "60")))
                        self.jitter_var.set(str(saved.get("jitter_seconds", "0")))
                        self.country_code_var.set(str(saved.get("country_code", "")))
                        adb_exec = str(saved.get("adb_path", "adb"))
                        self.adb_path_var.set(adb_exec)
                        self.real_adb.executable = adb_exec
                    self.import_button.configure(state="normal")
                    self.suppress_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    self.retry_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self._refresh_templates()
                    self._log(
                        "SUCCESS",
                        f"Local history is ready (Schema v{SCHEMA_VERSION}); recovered {interrupted} interrupted batch(es).",
                    )
                    self._refresh_history_async()
                elif kind == "imported":
                    parsed, inserted, suppressed, eligible = event[1], event[2], event[3], event[4]
                    meta = event[5] if len(event) > 5 else {}
                    batch_sz_str = event[6] if len(event) > 6 else "100"
                    self.import_button.configure(state="normal")
                    self.recipients.extend(eligible)
                    self.recipients = list(dict.fromkeys(self.recipients))
                    self.recipient_metadata.update(meta)
                    self.last_rejected_rows = getattr(parsed, "rejected_rows", ())
                    if hasattr(self, "btn_export_rejected"):
                        self.btn_export_rejected.configure(
                            state="normal" if self.last_rejected_rows else "disabled"
                        )
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    try:
                        bsz = max(1, int(batch_sz_str))
                    except ValueError:
                        bsz = 100
                    est_batches = (len( self.recipients) + bsz - 1) // bsz if self.recipients else 0
                    if hasattr(self, "import_summary_var"):
                        self.import_summary_var.set(
                            f"CSV Import Summary: {len(parsed.recipients)} valid consented | {inserted} newly added | "
                            f"{suppressed} suppressed blocked | {parsed.missing_consent_count} missing consent | "
                            f"{parsed.invalid_phone_count} invalid phone | {parsed.duplicate_count} file duplicates -> "
                            f"Total eligible: {len(self.recipients)} ({est_batches} batch(es) at {bsz}/batch)."
                        )
                    self._log(
                        "SUCCESS",
                        f"Imported {inserted} new opted-in recipient(s); {suppressed} suppressed entries blocked.",
                    )
                    for issue in parsed.rejected[:20]:
                        self._log("WARN", issue)
                    if len(parsed.rejected) > 20:
                        self._log(
                            "WARN",
                            f"{len(parsed.rejected)-20} additional invalid/non-consented rows omitted from console.",
                        )
                    self._refresh_history_async()
                elif kind == "suppressed":
                    phone = event[1]
                    self.recipients = [item for item in self.recipients if item != phone]
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    self._log("WARN", "Number suppressed; any already-open Android composer must be closed.")
                    self._refresh_history_async()
                elif kind == "batches_created":
                    self.start_button.configure(state="normal")
                    self.campaign_sent = 0
                    self.batch_ids = event[1]
                    self._log(
                        "INFO",
                        f"Created {len(self.batch_ids)} auditable batch(es); pending messages are not auto-retried.",
                    )
                    self._start_next_batch()
                elif kind == "recipients_changed":
                    self.start_button.configure(state="normal")
                    self.recipients = event[1]
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    messagebox.showwarning(
                        "Recipient list changed",
                        "One or more recipients are now suppressed or ineligible. The list was updated; review and start again.",
                        parent=self.root,
                    )
                elif kind == "background_error":
                    self._report_error(event[1], event[2])
                    if self.database_ready:
                        self.import_button.configure(state="normal")
                        self.start_button.configure(state="normal")
                        self.retry_button.configure(state="normal")
                        self.resume_button.configure(state="normal")
                elif kind == "worker_error":
                    self._report_error("Batch worker failed", event[1])
                    self._transition_ui(RunState.FAILED)
                    self.pause_button.configure(state="disabled")
                    if hasattr(self, "resume_exec_button"):
                        self.resume_exec_button.configure(state="disabled")
                    self.stop_button.configure(state="disabled")
                    self.start_button.configure(state="normal")
                    self.retry_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self.batch_ids.clear()
                    self._after(100, self._complete_current)
                elif kind == "crash_notice":
                    self._log("ERROR", event[1])
                elif kind == "history":
                    rows = event[1]
                    counts = event[2] if len(event) > 2 else {}
                    batches = event[3] if len(event) > 3 else []
                    self.history_tree.delete(*self.history_tree.get_children())
                    for row in rows:
                        device_label = self._history_device_label(row["device_serial"])
                        raw_status = row["status"]
                        ver = row.get("verification") or ""
                        err_cat = row.get("error_category") or ""
                        if raw_status == "pending" and ver == "composer_staged":
                            disp_status = f"{STATUS_PREFIX['staged']}pending"
                        elif raw_status == "failed" and err_cat == "skipped_ineligible":
                            disp_status = f"{STATUS_PREFIX['skipped']}failed"
                        elif raw_status == "failed" and err_cat == "uncertain_interrupted":
                            disp_status = f"{STATUS_PREFIX['uncertain']}failed"
                        else:
                            disp_status = f"{STATUS_PREFIX.get(raw_status, '')}{raw_status}"
                        self.history_tree.insert(
                            "",
                            "end",
                            iid=str(row["id"]),
                            values=(
                                disp_status,
                                row["phone"],
                                device_label,
                                row["batch_id"],
                                format_local_time(row["created_at"]),
                                format_local_time(row["updated_at"]),
                                row["message"],
                                ver,
                                row["error_detail"] or "",
                            ),
                        )
                    if counts or batches:
                        self._update_dashboard_And_batches(counts, batches)
                elif kind == "history_changed":
                    if not self._history_refresh_pending:
                        self._history_refresh_pending = True
                        self._after(200, self._refresh_history_debounced)
                elif kind == "batch_done":
                    self.campaign_sent += event[1]
                elif kind == "retry_info":
                    info = event[1]
                    failed = info["phones"]
                    eligible_failed = info.get("eligible_phones", failed)
                    ineligible_failed = info.get("ineligible_phones", [])
                    uncertain_failed = info.get("uncertain_phones", [])
                    if not failed:
                        self.retry_button.configure(state="normal")
                        msg = "The selected batch has no retryable failed messages."
                        if uncertain_failed:
                            msg += f"\n({len(uncertain_failed)} uncertain/interrupted item(s) are excluded from automatic retry.)"
                        messagebox.showinfo("Nothing to retry", msg, parent=self.root)
                        continue
                    preview_note = (
                        f"Retry Eligibility Preview for Batch #{info['batch']['id']}:\n"
                        f"• Total failed items: {len(failed)}\n"
                        f"• Currently eligible (consented & not suppressed): {len(eligible_failed)}\n"
                        f"• Newly suppressed/ineligible (will be blocked): {len(ineligible_failed)}\n"
                        f"• Uncertain/interrupted items (never auto-retried): {len(uncertain_failed)}\n\n"
                        "Create a new auditable batch for the eligible failed item(s) only?"
                    )
                    if not messagebox.askyesno("Explicit Retry Preview", preview_note, parent=self.root):
                        self.retry_button.configure(state="normal")
                        continue
                    batch = info["batch"]

                    def create_retry():
                        try:
                            eligible = self.store.eligible(failed)
                            if len(eligible) != len(failed):
                                self.events.put(
                                    (
                                        "background_error",
                                        "Retry blocked",
                                        ValidationError(
                                            "At least one failed recipient is now suppressed or ineligible; no retry batch was created."
                                        ),
                                    )
                                )
                                return
                            batch_id = self.store.create_batch(
                                batch["device_serial"],
                                batch["message"],
                                eligible,
                                batch["delay_seconds"],
                                batch["cooldown_every"],
                                batch["cooldown_seconds"],
                            )
                            self.store.record_audit("retry_failed_batch", f"Created retry batch #{batch_id} from batch #{batch['id']}")
                            self.events.put(("batches_created", [batch_id]))
                        except Exception as exc:
                            self.events.put(("background_error", "Retry batch failed", exc))

                    self._spawn_background(create_retry, "explicit-retry")
                elif kind == "pending_info":
                    info = event[1]
                    phones = info["phones"]
                    if not phones:
                        self.resume_button.configure(state="normal")
                        messagebox.showinfo(
                            "No pending items",
                            "The selected batch has no pending messages.",
                            parent=self.root,
                        )
                        continue
                    proceed = messagebox.askyesno(
                        "Explicit pending recovery",
                        f"Resume {len(phones)} pending item(s) from this batch?\n\n"
                        "A pending item may have been sent just before a crash but not recorded. Check Android's "
                        "sent-message history and close any stale composer first. Previously sent and failed rows "
                        "will be skipped; only pending rows are considered.",
                        parent=self.root,
                    )
                    if not proceed:
                        self.resume_button.configure(state="normal")
                        continue
                    batch_id = info["batch"]["id"]

                    def validate_pending():
                        try:
                            eligible = self.store.eligible(phones)
                            if len(eligible) != len(phones):
                                self.events.put(
                                    (
                                        "background_error",
                                        "Pending recovery blocked",
                                        ValidationError(
                                            "A pending recipient is now suppressed or ineligible. No resume was started."
                                        ),
                                    )
                                )
                                return
                            self.store.record_audit("resume_pending_batch", f"Resumed pending items for batch #{batch_id}")
                            self.events.put(("resume_ready", batch_id))
                        except Exception as exc:
                            self.events.put(("background_error", "Pending recovery failed", exc))

                    self._spawn_background(validate_pending, "pending-recovery-check")
                elif kind == "resume_ready":
                    self.campaign_sent = 0
                    self.batch_ids = [event[1]]
                    self._log("WARN", "Explicitly resuming pending recipients only; sent history is preserved.")
                    self._start_next_batch()
        except queue.Empty:
            pass
        self._after(100, self._poll_events)

    def _complete_current(self):
        if self.closing:
            return
        if self.active_worker and self.active_worker.is_alive():
            self._after(100, self._complete_current)
            return
        self.pause_button.configure(state="disabled")
        if hasattr(self, "resume_exec_button"):
            self.resume_exec_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.start_button.configure(state="normal")
        self.retry_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.active_worker = None
        self.active_batch_id = None
        self.controls = None
        if self.batch_ids and not self.closing:
            self._start_next_batch()
        else:
            self._refresh_history_async()

    def refresh_history(self):
        self._refresh_history_async()

    def export_history_csv(self):
        if not self.database_ready:
            return
        status_f = self.history_status_var.get() if hasattr(self, "history_status_var") else "all"
        search_q = self.history_search_var.get() if hasattr(self, "history_search_var") else ""
        rows = self.store.history(1000, status_filter=status_f, search_query=search_q)
        if not rows:
            messagebox.showinfo("No history rows", "No history rows match the current filter.", parent=self.root)
            return
        dest = filedialog.asksaveasfilename(
            title="Export filtered history",
            defaultextension=".csv",
            initialfile="sms_history_export.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not dest:
            return
        sanitized = self.store.export_history_rows(
            rows,
            redact_phones=bool(self.redact_phones_var.get()),
            redact_messages=bool(self.redact_msgs_var.get()),
        )
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=list(sanitized[0].keys()))
        writer.writeheader()
        writer.writerows(sanitized)
        Path(dest).write_text(buf.getvalue(), encoding="utf-8")
        self.store.record_audit("export_history", f"Exported {len(sanitized)} history rows (redact_phones={self.redact_phones_var.get()})")
        self._log("SUCCESS", f"Exported {len(sanitized)} history row(s) to {dest}.")

    def save_settings(self):
        try:
            delay = bounded_number(self.delay_var.get(), label="Delay", minimum=0, maximum=3600)
            batch_size = int(self.batch_var.get())
            cooldown_every = int(self.cooldown_every_var.get())
            cooldown = bounded_number(self.cooldown_var.get(), label="Cooldown", minimum=0, maximum=86400)
            jitter = bounded_number(self.jitter_var.get(), label="Jitter", minimum=0, maximum=600)
            if not 1 <= batch_size <= 1000 or not 0 <= cooldown_every <= 1000:
                raise ValidationError("Batch size must be 1–1000 and cooldown interval 0–1000.")
            cc = self.country_code_var.get().strip()
            if cc:
                normalize_phone("4155550101", default_country_code=cc)
            adb_exec = self.adb_path_var.get().strip() or "adb"
            self.real_adb.executable = adb_exec
            if self.database_ready:
                self.store.set_setting("delay_seconds", str(delay))
                self.store.set_setting("batch_size", str(batch_size))
                self.store.set_setting("cooldown_every", str(cooldown_every))
                self.store.set_setting("cooldown_seconds", str(cooldown))
                self.store.set_setting("jitter_seconds", str(jitter))
                self.store.set_setting("default_country_code", cc)
                self.store.set_setting("adb_executable", adb_exec)
                self.store.record_audit("save_settings", f"Updated operational settings (batch_size={batch_size}, delay={delay}s)")
            self._log("SUCCESS", "Settings validated and saved locally.")
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self.root)

    def toggle_dry_run_mode(self):
        self.dry_run_active = bool(self.dry_run_var.get())
        if self.dry_run_active:
            self.adb = MockAdbClient()
            self.mode_badge.configure(text="[MODE: DRY-RUN MOCK TRANSPORT · NO REAL DEVICE TRAFFIC]")
            self._log("WARN", "Dry-Run Mode enabled: using MockAdbClient. No real Android device will be contacted.")
        else:
            self.adb = self.real_adb
            self.mode_badge.configure(text="[MODE: MANUAL ADB COMPOSER STAGING · OPT-IN ONLY]")
            self._log("INFO", "Dry-Run Mode disabled: restored standard ADB transport.")
        self.discover_devices()

    def backup_database_action(self):
        dest = filedialog.asksaveasfilename(
            title="Save SQLite Database Backup",
            defaultextension=".db",
            initialfile=f"sms_history_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
            filetypes=[("SQLite Database", "*.db"), ("All files", "*.*")],
        )
        if not dest:
            return
        try:
            out = self.store.backup_database(dest)
            self.store.record_audit("backup_database", f"Backed up database to {out.name}")
            self._log("SUCCESS", f"Database backed up safely to {out}.")
        except Exception as exc:
            messagebox.showerror("Backup failed", str(exc), parent=self.root)

    def restore_database_action(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning("Batch active", "Stop active execution before restoring a database.", parent=self.root)
            return
        src = filedialog.askopenfilename(
            title="Select SQLite Backup to Restore",
            filetypes=[("SQLite Database", "*.db"), ("All files", "*.*")],
        )
        if not src:
            return
        if not messagebox.askyesno(
            "Confirm Database Restore",
            f"Restore database from {Path(src).name}?\nA safety backup of the current database will be created first.",
            parent=self.root,
        ):
            return
        try:
            self.store.restore_database(src)
            self.store.record_audit("restore_database", f"Restored database from {Path(src).name}")
            self._log("SUCCESS", f"Restored and validated database from {src}.")
            self._refresh_history_async()
        except Exception as exc:
            messagebox.showerror("Restore failed", str(exc), parent=self.root)

    def run_adb_diagnostics(self):
        info = self.adb.check_availability()
        audit = self.store.audit_logs(25) if self.database_ready else []
        lines = [
            "=== ADB & RUNTIME DIAGNOSTICS ===",
            f"ADB Available: {info['available']}",
            f"Configured Executable: {info['executable']} (Resolved: {info['resolved_path']})",
            f"Version / Detail: {info['version'] or info['error']}",
            f"Connected Devices: {len(self.devices)} ({sum(1 for d in self.devices.values() if d.usable)} usable)",
            f"Database Ready: {self.database_ready} (Last auto-backup: {self.store.last_backup_path or 'None'})",
            "",
            "=== RECENT SANITIZED OPERATOR AUDIT TRAIL ===",
        ]
        for ev in audit:
            lines.append(f"  {format_local_time(ev['occurred_at'])} | {ev['action']} | {ev['detail']}")
        self.diag_output.configure(state="normal")
        self.diag_output.delete("1.0", "end")
        self.diag_output.insert("1.0", "\n".join(lines))
        self.diag_output.configure(state="disabled")

    def open_diagnostics_folder(self):
        self.reporter.directory.mkdir(parents=True, exist_ok=True)
        folder = str(self.reporter.directory.resolve())
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(f"file://{folder}")
            self._log("INFO", f"Opened diagnostics folder: {folder}")
        except Exception as exc:
            messagebox.showinfo("Diagnostics Folder Path", f"Diagnostics folder is located at:\n{folder}\n({exc})", parent=self.root)

    def export_support_bundle(self):
        dest = filedialog.asksaveasfilename(
            title="Save Sanitized Support Bundle",
            defaultextension=".zip",
            initialfile=f"smsdesk_support_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            filetypes=[("ZIP archive", "*.zip")],
        )
        if not dest:
            return
        try:
            diag_info = self.adb.check_availability()
            audit = self.store.audit_logs(100) if self.database_ready else []
            out = self.reporter.create_support_bundle(dest, diagnostics_info=diag_info, audit_events=audit)
            self._log("SUCCESS", f"Created sanitized support bundle at {out}.")
        except Exception as exc:
            messagebox.showerror("Support bundle failed", str(exc), parent=self.root)

    def retry_failed(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning(
                "Batch active",
                "Retry is available after the active batch completes or stops.",
                parent=self.root,
            )
            return
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo(
                "Select a history item",
                "Select a row from the batch you want to retry.",
                parent=self.root,
            )
            return
        values = self.history_tree.item(selection[0], "values")
        try:
            batch_id = int(values[3])
        except (ValueError, IndexError):
            messagebox.showerror("History error", "Could not determine the selected batch.", parent=self.root)
            return
        self.retry_button.configure(state="disabled")

        def task():
            try:
                self.events.put(("retry_info", self.store.failed_retry_data(batch_id)))
            except Exception as exc:
                self.events.put(("background_error", "Could not inspect failed items", exc))

        self._spawn_background(task, "retry-inspection")

    def resume_pending(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning(
                "Batch active",
                "Resume is available after the active batch completes or stops.",
                parent=self.root,
            )
            return
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo(
                "Select a history item",
                "Select a pending row from the batch to resume.",
                parent=self.root,
            )
            return
        values = self.history_tree.item(selection[0], "values")
        try:
            batch_id = int(values[3])
        except (ValueError, IndexError):
            messagebox.showerror("History error", "Could not determine the selected batch.", parent=self.root)
            return
        self.resume_button.configure(state="disabled")

        def task():
            try:
                self.events.put(("pending_info", self.store.pending_recovery_data(batch_id)))
            except Exception as exc:
                self.events.put(("background_error", "Could not inspect pending items", exc))

        self._spawn_background(task, "pending-inspection")

    def _refresh_history_async(self):
        status_f = self.history_status_var.get() if hasattr(self, "history_status_var") else "all"
        search_q = self.history_search_var.get() if hasattr(self, "history_search_var") else ""

        def task():
            try:
                rows = self.store.history(300, status_filter=status_f, search_query=search_q)
                counts = self.store.dashboard_counts()
                batches = self.store.list_batches(100)
                self.events.put(("history", rows, counts, batches))
            except Exception as exc:
                self.events.put(("background_error", "History refresh failed", exc))

        self._spawn_background(task, "history-reader")

    def _refresh_history_debounced(self):
        self._history_refresh_pending = False
        if not self.closing:
            self._refresh_history_async()

    def _spawn_background(self, target, name: str):
        def run():
            try:
                target()
            finally:
                with self._background_lock:
                    self.background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, name=name, daemon=True)
        with self._background_lock:
            self.background_threads.add(thread)
        thread.start()

    def _after(self, delay_ms: int, callback, *, allow_closing: bool = False):
        holder = {}

        def invoke():
            callback_id = holder.get("id")
            if callback_id is not None:
                self._after_ids.discard(callback_id)
            if self.closing and not allow_closing:
                return
            callback()

        callback_id = self.root.after(delay_ms, invoke)
        holder["id"] = callback_id
        self._after_ids.add(callback_id)
        return callback_id

    def _report_error(self, title: str, exc: Exception):
        self._log("ERROR", f"{title}: {str(exc)[:300]}")
        expected = isinstance(exc, (AdbError, ValidationError))
        if not expected:
            self.reporter.write(type(exc), exc, exc.__traceback__, context=title)
        detail = str(exc)
        if not expected:
            detail += "\n\nA local crash report was written when possible."
        messagebox.showerror(title, detail, parent=self.root)

    def _callback_exception(self, exc_type, exc, tb):
        self.reporter.write(exc_type, exc, tb, context="Tkinter callback exception")
        self._log("ERROR", f"UI error: {str(exc)[:300]}")
        messagebox.showerror(
            "Application error",
            "The UI encountered an error. A local report was saved when possible.",
            parent=self.root,
        )

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self._wifi_reconnect_cancel.set()
        self.batch_ids.clear()
        for callback_id in tuple(self._after_ids):
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        if self.controls:
            self.controls.stop.set()
        worker = self.active_worker
        with self._background_lock:
            background_alive = any(thread.is_alive() for thread in self.background_threads)
        if (worker and worker.is_alive()) or background_alive:
            self._shutdown_worker = worker
            self._after(100, self._finish_shutdown, allow_closing=True)
        else:
            self.store.close_all()
            self.root.destroy()

    def _finish_shutdown(self):
        worker = getattr(self, "_shutdown_worker", None)
        with self._background_lock:
            background_alive = any(thread.is_alive() for thread in self.background_threads)
        if (worker and worker.is_alive()) or background_alive:
            self._after(100, self._finish_shutdown, allow_closing=True)
            return
        self.store.close_all()
        self.root.destroy()
