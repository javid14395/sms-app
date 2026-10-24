"""Tkinter desktop application; all blocking work is delegated to worker threads."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .adb import AdbClient, AdbError, Device
from .crash import CrashReporter, install_thread_excepthook
from .dispatch import DispatchWorker, WorkerControls
from .dispatch import RunState, StateMachine
from .storage import HistoryStore
from .validation import (MAX_SMS_CHARS, ValidationError, bounded_number,
                        normalize_phone, parse_contacts_csv, safe_truncate_message,
                        sms_encoding_usage, validate_ip_port, validate_message)


COLORS = {"bg": "#0b1220", "panel": "#111c2e", "panel_alt": "#17243a",
          "text": "#edf5ff", "muted": "#b4c5da", "accent": "#35d5ed",
          "electric": "#3b82f6",
          "accent_text": "#07121d", "good": "#64e6b0",
          "warn": "#ffd166", "bad": "#ff808a", "console": "#080e18",
          "border": "#29415d"}


class SmsDeskApp:
    def __init__(self, root: tk.Tk, db_path: str | Path = "sms_history_pro.db",
                 adb: AdbClient | None = None,
                 crash_dir: str | Path = "crash_reports"):
        self.root = root
        self.root.title("Consent-first SMS Desk")
        self.root.geometry("1180x860")
        self.root.minsize(760, 620)
        self.store = HistoryStore(db_path, initialize=False)
        self.database_ready = False
        self.adb = adb or AdbClient()
        self.events: queue.Queue = queue.Queue()
        self.devices: dict[str, Device] = {}
        # Retain friendly identities for history rows while a device is
        # temporarily offline; the serial remains visible for exact targeting.
        self.device_names: dict[str, str] = {}
        self.recipients: list[str] = []
        self.batch_ids: list[int] = []
        self.campaign_sent = 0
        self.active_worker: DispatchWorker | None = None
        self.controls: WorkerControls | None = None
        self.background_threads: set[threading.Thread] = set()
        self._background_lock = threading.Lock()
        self._history_refresh_pending = False
        self._after_ids: set[str] = set()
        self.machine = StateMachine()
        self.closing = False
        self._layout_two_columns: bool | None = None
        self.reporter = CrashReporter(crash_dir, notify=lambda text: self.events.put(("crash_notice", text)))
        self.reporter.install()
        install_thread_excepthook(self.reporter)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.root.report_callback_exception = self._callback_exception
        self.root.bind("<Configure>", self._resize_layout, add="+")
        self._after(100, self._poll_events)
        self._spawn_background(self._initialize_database, "database-startup")
        self.discover_devices()

    def _initialize_database(self):
        try:
            self.store.initialize()
            interrupted = self.store.recover_interrupted()
            self.events.put(("database_ready", interrupted))
        except Exception as exc:
            self.events.put(("background_error", "Database initialization failed", exc))

    def _build(self):
        self.root.configure(bg=COLORS["bg"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"],
                        fieldbackground=COLORS["panel"], font=("Segoe UI", 10))
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("Status.TLabel", foreground=COLORS["accent"], font=("Segoe UI", 10, "bold"))
        style.configure("TLabelFrame", background=COLORS["bg"], foreground=COLORS["accent"])
        style.configure("TLabelframe.Label", background=COLORS["bg"], foreground=COLORS["accent"],
                        font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=(10, 7), background=COLORS["panel_alt"],
                        foreground=COLORS["text"], bordercolor=COLORS["border"], focusthickness=2)
        style.map("TButton", background=[("active", "#243a57"), ("disabled", COLORS["panel"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground=COLORS["accent_text"],
                        font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#6be5f5"), ("disabled", COLORS["panel_alt"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("Hover.TButton", background="#243a57", foreground=COLORS["text"],
                        bordercolor=COLORS["electric"])
        style.configure("AccentHover.TButton", background=COLORS["electric"],
                        foreground=COLORS["accent_text"], font=("Segoe UI", 10, "bold"),
                        bordercolor="#75b7ff")
        style.configure("Danger.TButton", background="#512a3a", foreground="#ffe9ed")
        style.map("Danger.TButton", background=[("active", "#70374b")])
        style.configure("DangerHover.TButton", background="#70374b", foreground="#ffffff",
                        bordercolor=COLORS["bad"])
        style.configure("TEntry", padding=5, insertcolor=COLORS["text"])
        style.configure("TCombobox", padding=5, fieldbackground=COLORS["panel"],
                        foreground=COLORS["text"], arrowcolor=COLORS["accent"])
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["panel"])],
                  foreground=[("readonly", COLORS["text"])])
        style.configure("Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"],
                        foreground=COLORS["text"], rowheight=27, bordercolor=COLORS["border"])
        style.map("Treeview", background=[("selected", "#15556a")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=COLORS["panel_alt"],
                        foreground=COLORS["text"], padding=(6, 7))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Consent-first SMS Desk", font=("Segoe UI", 20, "bold"),
                  foreground=COLORS["accent"]).pack(side="left")
        ttk.Label(header, text="Human-confirmed · opt-in only", style="Muted.TLabel").pack(side="left", padx=16)
        self.state_label = ttk.Label(header, text="IDLE", style="Status.TLabel")
        self.state_label.pack(side="right")

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 4))
        self.top_panels = top
        top.grid_columnconfigure(0, weight=1, uniform="top-panels")
        top.grid_columnconfigure(1, weight=1, uniform="top-panels")
        left = ttk.LabelFrame(top, text="Recipients & consent", padding=10)
        self.recipient_frame = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=3)
        ttk.Label(left, text="Import CSV: required phone and per-row consent or opt_in columns").pack(anchor="w")
        ttk.Label(left, text="Only yes / true / 1 / opted-in rows are eligible; suppressed numbers stay blocked.",
                  foreground=COLORS["muted"]).pack(anchor="w", pady=(2, 5))
        row = ttk.Frame(left)
        row.pack(fill="x")
        self.import_button = ttk.Button(row, text="Import consented CSV…", command=self.import_csv)
        self.import_button.configure(state="disabled")
        self.import_button.pack(side="left")
        self._bind_button_hover(self.import_button)
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

        right = ttk.LabelFrame(top, text="Device & operational timing", padding=10)
        self.device_frame = right
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=3)
        devrow = ttk.Frame(right)
        devrow.pack(fill="x")
        ttk.Label(devrow, text="ADB device:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(devrow, textvariable=self.device_var, state="readonly", width=34)
        self.device_box.pack(side="left", padx=5)
        self.discover_button = ttk.Button(devrow, text="Discover", command=self.discover_devices)
        self.discover_button.pack(side="left")
        self._bind_button_hover(self.discover_button)
        wifi = ttk.Frame(right)
        wifi.pack(fill="x", pady=(6, 0))
        ttk.Label(wifi, text="Wi-Fi endpoint:").pack(side="left")
        self.wifi_entry = ttk.Entry(wifi, width=22)
        self.wifi_entry.pack(side="left", padx=5)
        self.connect_button = ttk.Button(wifi, text="Connect", command=self.connect_wifi)
        self.connect_button.pack(side="left")
        self._bind_button_hover(self.connect_button)
        pace = ttk.Frame(right)
        pace.pack(fill="x", pady=(8, 0))
        self.delay_var = tk.StringVar(value="2")
        self.batch_var = tk.StringVar(value="25")
        self.cooldown_every_var = tk.StringVar(value="25")
        self.cooldown_var = tk.StringVar(value="60")
        for label, var, width in (("Delay (s)", self.delay_var, 6), ("Batch size", self.batch_var, 6),
                                  ("Cooldown every", self.cooldown_every_var, 6), ("Cooldown (s)", self.cooldown_var, 6)):
            ttk.Label(pace, text=label).pack(side="left", padx=(0, 3))
            ttk.Entry(pace, textvariable=var, width=width).pack(side="left", padx=(0, 8))

        compose = ttk.LabelFrame(outer, text="Single SMS (max 160 characters; no multipart messages)", padding=10)
        compose.pack(fill="x", pady=10)
        self.message = tk.Text(compose, height=3, wrap="word", bg=COLORS["panel"], fg=COLORS["text"],
                               insertbackground="white", undo=True, relief="flat")
        self.message.pack(fill="x")
        self.message.bind("<KeyRelease>", self._message_changed)
        self.message.bind("<<Paste>>", lambda _e: self._after(20, self._message_changed))
        bar = ttk.Frame(compose)
        bar.pack(fill="x", pady=(6, 0))
        self.counter = ttk.Label(bar, text=f"0 / {MAX_SMS_CHARS}")
        self.counter.pack(side="left")
        ttk.Label(bar, text="ADB opens the compose screen; a person must press Send and confirm every recipient.",
                  foreground=COLORS["warn"], wraplength=760).pack(side="left", padx=12)
        buttons = ttk.Frame(compose)
        buttons.pack(fill="x", pady=(8, 0))
        self.start_button = ttk.Button(buttons, text="Start staged batch", command=self.start, style="Accent.TButton")
        self.start_button.configure(state="disabled")
        self.start_button.pack(side="left", padx=3)
        self._bind_button_hover(self.start_button, accent=True)
        self.pause_button = ttk.Button(buttons, text="Pause / resume", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=3)
        self._bind_button_hover(self.pause_button)
        self.stop_button = ttk.Button(buttons, text="Stop safely", command=self.stop, state="disabled",
                                      style="Danger.TButton")
        self.stop_button.pack(side="left", padx=3)
        self._bind_button_hover(self.stop_button, danger=True)

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)
        history_frame = ttk.LabelFrame(panes, text="Auditable local history (latest 300)", padding=5)
        console_frame = ttk.LabelFrame(panes, text="Execution console", padding=5)
        panes.add(history_frame, weight=3)
        panes.add(console_frame, weight=2)
        self.retry_button = ttk.Button(history_frame, text="Retry failed items in selected batch…",
                                        command=self.retry_failed, state="disabled")
        self.retry_button.pack(anchor="e", pady=(0, 4))
        self._bind_button_hover(self.retry_button)
        self.resume_button = ttk.Button(history_frame, text="Resume pending selected batch…",
                                         command=self.resume_pending, state="disabled")
        self.resume_button.pack(anchor="e", pady=(0, 4))
        self._bind_button_hover(self.resume_button)
        cols = ("status", "phone", "device", "batch", "created", "updated", "message", "verification", "error")
        self.history_tree = ttk.Treeview(history_frame, columns=cols, show="headings", height=9)
        for col, width in (("status", 75), ("phone", 125), ("device", 130), ("batch", 55),
                           ("created", 145), ("updated", 145), ("message", 220),
                           ("verification", 125), ("error", 220)):
            self.history_tree.heading(col, text=col.title())
            self.history_tree.column(col, width=width, minwidth=55, stretch=col in ("error", "device"))
        scroll = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_tree.yview)
        hscroll = ttk.Scrollbar(history_frame, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        hscroll.pack(side="bottom", fill="x")
        self.console = tk.Text(console_frame, height=8, state="disabled", wrap="word",
                               bg=COLORS["console"], fg=COLORS["text"], insertbackground=COLORS["accent"],
                               font=("Consolas", 9), relief="flat")
        self.console.tag_configure("INFO", foreground=COLORS["text"])
        self.console.tag_configure("SUCCESS", foreground=COLORS["good"])
        self.console.tag_configure("WARN", foreground=COLORS["warn"])
        self.console.tag_configure("ERROR", foreground=COLORS["bad"])
        console_scroll = ttk.Scrollbar(console_frame, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=console_scroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_scroll.pack(side="right", fill="y")
        self._log("INFO", "Ready. Import only recipients with documented prior opt-in; honor opt-outs.")
        self._resize_layout()

    def _resize_layout(self, _event=None):
        """Use side-by-side cards on wide windows and stack them on narrow ones."""
        if not hasattr(self, "top_panels"):
            return
        width = self.root.winfo_width()
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
        name = device.friendly_name
        return f"{name} ({device.serial})" if include_serial else name

    def _history_device_label(self, serial: str) -> str:
        device = self.devices.get(serial)
        if device and device.friendly_name != "Android device":
            self.device_names[serial] = device.friendly_name
        name = self.device_names.get(serial)
        return f"{name} ({serial})" if name else serial

    def _bind_button_hover(self, button: ttk.Button, *, accent: bool = False,
                           danger: bool = False) -> None:
        base_style = ("Accent.TButton" if accent else "Danger.TButton" if danger
                      else "TButton")
        hover_style = ("AccentHover.TButton" if accent else "DangerHover.TButton"
                       if danger else "Hover.TButton")
        button.configure(style=base_style)

        def enter(_event):
            if not button.instate(("disabled",)):
                button.configure(style=hover_style)

        def leave(_event):
            button.configure(style=base_style)

        # Immediate reversible color changes are reliable across ttk themes;
        # no timer or animation can delay input or interfere with keyboard use.
        button.bind("<Enter>", enter, add="+")
        button.bind("<Leave>", leave, add="+")

    def _log(self, level: str, text: str):
        from datetime import datetime
        self.console.configure(state="normal")
        self.console.insert("end", f"{datetime.now().strftime('%H:%M:%S')} [{level}] {text}\n", level)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _message_changed(self, _event=None):
        text = self.message.get("1.0", "end-1c")
        truncated = False
        if len(text) > MAX_SMS_CHARS:
            self.message.delete("1.0", "end")
            self.message.insert("1.0", safe_truncate_message(text))
            text = self.message.get("1.0", "end-1c")
            truncated = True
        encoding, units, capacity = sms_encoding_usage(text)
        if units > capacity:
            self.message.delete("1.0", "end")
            self.message.insert("1.0", safe_truncate_message(text))
            text = self.message.get("1.0", "end-1c")
            encoding, units, capacity = sms_encoding_usage(text)
            truncated = True
        if truncated:
            self._log("WARN", "Message safely truncated at the one-SMS encoding limit; no multipart message will be created.")
        self.counter.configure(text=f"{len(text)} chars · {units}/{capacity} {encoding}",
                               foreground=COLORS["bad"] if units >= capacity else COLORS["text"])

    def import_csv(self):
        path = filedialog.askopenfilename(title="Import consented recipients", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        self.import_button.configure(state="disabled")
        self._log("INFO", "Reading CSV and checking per-row consent…")

        def task():
            try:
                csv_path = Path(path)
                if csv_path.stat().st_size > 20 * 1024 * 1024:
                    raise ValidationError("CSV exceeds the 20 MiB import limit.")
                text = csv_path.read_text(encoding="utf-8-sig")
                parsed = parse_contacts_csv(text)
                phones = [item.phone for item in parsed.recipients]
                inserted, suppressed = self.store.import_consented(phones, Path(path).name)
                eligible = self.store.eligible(phones)
                self.events.put(("imported", parsed, inserted, suppressed, eligible))
            except Exception as exc:
                self.events.put(("background_error", "CSV import failed", exc))
        self._spawn_background(task, "csv-import")

    def suppress_phone(self):
        try:
            phone = normalize_phone(self.suppress_entry.get())
            self.suppress_entry.delete(0, "end")
            self._log("INFO", "Saving suppression…")
            def task():
                try:
                    self.store.suppress(phone)
                    self.events.put(("suppressed", phone))
                except Exception as exc:
                    self.events.put(("background_error", "Could not save suppression", exc))
            self._spawn_background(task, "suppression-update")
        except Exception as exc:
            messagebox.showerror("Invalid phone", str(exc), parent=self.root)

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
                            # Hardware labels are helpful, not required for safe targeting.
                            pass
                    devices.append(device)
                self.events.put(("devices", devices))
            except Exception as exc:
                self.events.put(("background_error", "ADB discovery failed", exc))
        self._spawn_background(task, "adb-discovery")

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

    def start(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning("Batch active", "Finish or stop the active batch before starting another.", parent=self.root)
            return
        try:
            message = validate_message(self.message.get("1.0", "end-1c"))
            delay = bounded_number(self.delay_var.get(), label="Delay", minimum=0, maximum=3600)
            batch_size = int(self.batch_var.get())
            cooldown_every = int(self.cooldown_every_var.get())
            cooldown = bounded_number(self.cooldown_var.get(), label="Cooldown", minimum=0, maximum=86400)
            if not 1 <= batch_size <= 1000 or not 0 <= cooldown_every <= 1000:
                raise ValidationError("Batch size must be 1–1000 and cooldown interval 0–1000 (0 disables periodic cooldown).")
            selected = self.device_var.get()
            serial = getattr(self, "_device_display_to_serial", {}).get(selected, selected)
            if not serial or serial not in self.devices or not self.devices[serial].usable:
                raise ValidationError("Select an online, authorized ADB device.")
            if not self.recipients:
                raise ValidationError("Import at least one explicitly consented recipient.")
            if not messagebox.askyesno("Confirm consent and staged sending",
                    f"Create batches for {len(self.recipients)} opted-in recipients?\n\n"
                    "ADB will only open the Android compose screen. You must press Send and confirm each message. "
                    "No recipient is sent automatically.", parent=self.root):
                return
            self.start_button.configure(state="disabled")
            snapshot = list(self.recipients)
            self.campaign_sent = 0
            def create():
                try:
                    eligible = self.store.eligible(snapshot)
                    if len(eligible) != len(snapshot):
                        self.events.put(("recipients_changed", eligible))
                        return
                    chunks = [eligible[offset:offset + batch_size]
                              for offset in range(0, len(eligible), batch_size)]
                    ids = self.store.create_batches(serial, message, chunks, delay,
                                                    cooldown_every, cooldown)
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
        self.controls = WorkerControls.create()
        self.active_worker = DispatchWorker(batch_id, self.store, self.adb, self.events, self.controls,
                                             sent_before=self.campaign_sent,
                                             has_more_batches=bool(self.batch_ids))
        self.active_worker.start()
        self.start_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self._transition_ui(RunState.RUNNING)
        self.pause_button.configure(state="normal")
        self.stop_button.configure(state="normal")

    def toggle_pause(self):
        if not self.controls:
            return
        if self.controls.pause.is_set():
            self.controls.pause.clear()
            self._transition_ui(RunState.RUNNING)
        else:
            self.controls.pause.set()
            self._transition_ui(RunState.PAUSED)
            self._log("WARN", "Pause requested; current staged message remains open until confirmed or stopped.")

    def stop(self):
        if self.controls:
            self.controls.stop.set()
            self.batch_ids.clear()
            self._transition_ui(RunState.STOPPING)
            self._log("WARN", "Graceful stop requested; unconfirmed messages remain pending.")

    def _set_state(self, state: str):
        color = {"running": COLORS["good"], "completed": COLORS["good"],
                 "paused": COLORS["warn"], "stopping": COLORS["warn"],
                 "failed": COLORS["bad"], "idle": COLORS["accent"]}.get(state, COLORS["accent"])
        self.state_label.configure(text=state.upper(), foreground=color)

    def _transition_ui(self, target: RunState):
        if target == RunState.RUNNING and self.controls and self.controls.pause.is_set():
            return
        if self.machine.state != target:
            try:
                self.machine.transition(target)
            except RuntimeError:
                # Late worker state messages can arrive after Stop; retain the
                # state machine's invariant rather than letting UI events crash.
                return
        self._set_state(target.value)

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
                elif kind == "confirm":
                    _, item_id, phone = event
                    suffix = phone[-4:]
                    answer = messagebox.askyesno("Confirm recipient sent",
                        f"Android compose was opened for a consented recipient ending {suffix}.\n\n"
                        "After personally checking the number/message and pressing Send in Android, select Yes. "
                        "Select No if you did not send it. This confirmation is user-attested, not device-verified.",
                        parent=self.root)
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
                    # Keep internal serial separate from display.
                    self._device_display_to_serial = {
                        f"{self._device_label(d)} — {d.state}": d.serial for d in event[1]
                    }
                    self.device_box.configure(values=values)
                    if values and self.device_var.get() not in self._device_display_to_serial:
                        self.device_var.set(values[0])
                    self._log("INFO", f"ADB reports {len(values)} device(s): " +
                              (", ".join(f"{self._device_label(d)} — {d.state}" for d in event[1]) or "none"))
                    if self.database_ready:
                        self._refresh_history_async()
                elif kind == "discover_again":
                    self.discover_devices()
                elif kind == "database_ready":
                    self.database_ready = True
                    self.import_button.configure(state="normal")
                    self.suppress_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    self.retry_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self._log("SUCCESS", f"Local history is ready; recovered {event[1]} interrupted batch(es).")
                    self._refresh_history_async()
                elif kind == "imported":
                    _, parsed, inserted, suppressed, eligible = event
                    self.import_button.configure(state="normal")
                    self.recipients.extend(eligible)
                    self.recipients = list(dict.fromkeys(self.recipients))
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    self._log("SUCCESS", f"Imported {inserted} new opted-in recipient(s); {suppressed} suppressed entries blocked.")
                    for issue in parsed.rejected[:20]:
                        self._log("WARN", issue)
                    if len(parsed.rejected) > 20:
                        self._log("WARN", f"{len(parsed.rejected)-20} additional invalid/non-consented rows omitted from console.")
                elif kind == "suppressed":
                    phone = event[1]
                    self.recipients = [item for item in self.recipients if item != phone]
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    self._log("WARN", "Number suppressed; any already-open Android composer must be closed.")
                elif kind == "batches_created":
                    self.start_button.configure(state="normal")
                    self.campaign_sent = 0
                    self.batch_ids = event[1]
                    self._log("INFO", f"Created {len(self.batch_ids)} auditable batch(es); pending messages are not auto-retried.")
                    self._start_next_batch()
                elif kind == "recipients_changed":
                    self.start_button.configure(state="normal")
                    self.recipients = event[1]
                    self.recipient_label.configure(text=f"{len(self.recipients)} eligible recipients")
                    messagebox.showwarning("Recipient list changed",
                        "One or more recipients are now suppressed or ineligible. The list was updated; review and start again.",
                        parent=self.root)
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
                    self.stop_button.configure(state="disabled")
                    self.start_button.configure(state="normal")
                    self.retry_button.configure(state="normal")
                    self.resume_button.configure(state="normal")
                    self.batch_ids.clear()
                    self._after(100, self._complete_current)
                elif kind == "crash_notice":
                    messagebox.showerror("Application error", event[1], parent=self.root)
                elif kind == "history":
                    self.history_tree.delete(*self.history_tree.get_children())
                    for row in event[1]:
                        device_label = self._history_device_label(row["device_serial"])
                        self.history_tree.insert("", "end", iid=str(row["id"]), values=(
                            row["status"], row["phone"], device_label, row["batch_id"],
                            row["created_at"], row["updated_at"], row["message"],
                            row["verification"] or "", row["error_detail"] or ""))
                elif kind == "history_changed":
                    if not self._history_refresh_pending:
                        self._history_refresh_pending = True
                        self._after(200, self._refresh_history_debounced)
                elif kind == "batch_done":
                    self.campaign_sent += event[1]
                elif kind == "retry_info":
                    info = event[1]
                    failed = info["phones"]
                    if not failed:
                        self.retry_button.configure(state="normal")
                        messagebox.showinfo("Nothing to retry", "The selected batch has no failed messages.", parent=self.root)
                        continue
                    if not messagebox.askyesno("Explicit retry",
                        f"Create a new auditable batch for {len(failed)} failed item(s) only?\n"
                        "Previously sent and pending items will not be retried. Consent and suppression are checked again.",
                        parent=self.root):
                        self.retry_button.configure(state="normal")
                        continue
                    batch = info["batch"]
                    def create_retry():
                        try:
                            eligible = self.store.eligible(failed)
                            if len(eligible) != len(failed):
                                self.events.put(("background_error", "Retry blocked",
                                    ValidationError("At least one failed recipient is now suppressed or ineligible; no retry batch was created.")))
                                return
                            batch_id = self.store.create_batch(batch["device_serial"], batch["message"], eligible,
                                batch["delay_seconds"], batch["cooldown_every"], batch["cooldown_seconds"])
                            self.events.put(("batches_created", [batch_id]))
                        except Exception as exc:
                            self.events.put(("background_error", "Retry batch failed", exc))
                    self._spawn_background(create_retry, "explicit-retry")
                elif kind == "pending_info":
                    info = event[1]
                    phones = info["phones"]
                    if not phones:
                        self.resume_button.configure(state="normal")
                        messagebox.showinfo("No pending items", "The selected batch has no pending messages.", parent=self.root)
                        continue
                    proceed = messagebox.askyesno("Explicit pending recovery",
                        f"Resume {len(phones)} pending item(s) from this batch?\n\n"
                        "A pending item may have been sent just before a crash but not recorded. Check Android's "
                        "sent-message history and close any stale composer first. Previously sent and failed rows "
                        "will be skipped; only pending rows are considered.",
                        parent=self.root)
                    if not proceed:
                        self.resume_button.configure(state="normal")
                        continue
                    batch_id = info["batch"]["id"]
                    def validate_pending():
                        try:
                            eligible = self.store.eligible(phones)
                            if len(eligible) != len(phones):
                                self.events.put(("background_error", "Pending recovery blocked",
                                    ValidationError("A pending recipient is now suppressed or ineligible. No resume was started.")))
                                return
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
        self.stop_button.configure(state="disabled")
        self.start_button.configure(state="normal")
        self.retry_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.active_worker = None
        self.controls = None
        if self.batch_ids:
            self._start_next_batch()
        else:
            self._refresh_history_async()

    def refresh_history(self):
        self._refresh_history_async()

    def retry_failed(self):
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showwarning("Batch active", "Retry is available after the active batch completes or stops.", parent=self.root)
            return
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("Select a history item", "Select a row from the batch you want to retry.", parent=self.root)
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
            messagebox.showwarning("Batch active", "Resume is available after the active batch completes or stops.", parent=self.root)
            return
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("Select a history item", "Select a pending row from the batch to resume.", parent=self.root)
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
        def task():
            try:
                self.events.put(("history", self.store.history(300)))
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
        messagebox.showerror("Application error", "The UI encountered an error. A local report was saved when possible.", parent=self.root)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
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
            # Tk remains responsive while the bounded ADB timeout / cooperative wait completes.
            self._shutdown_worker = worker
            self._after(100, self._finish_shutdown, allow_closing=True)
        else:
            self.root.destroy()

    def _finish_shutdown(self):
        worker = getattr(self, "_shutdown_worker", None)
        with self._background_lock:
            background_alive = any(thread.is_alive() for thread in self.background_threads)
        if (worker and worker.is_alive()) or background_alive:
            self._after(100, self._finish_shutdown, allow_closing=True)
            return
        self.root.destroy()
