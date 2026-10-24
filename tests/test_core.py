from __future__ import annotations

import csv
import io
import queue
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Provide a lightweight headless tkinter stub if _tkinter is not installed in the test runner VM
try:
    import tkinter as _tk  # noqa: F401
except ModuleNotFoundError:
    tk_stub = types.ModuleType("tkinter")
    tk_stub.Tk = object
    tk_stub.Text = object
    tk_stub.Toplevel = object
    tk_stub.Label = object
    tk_stub.Widget = object
    tk_stub.StringVar = object
    tk_stub.BooleanVar = object
    tk_stub.TclError = Exception
    tk_stub.filedialog = types.ModuleType("tkinter.filedialog")
    tk_stub.messagebox = types.ModuleType("tkinter.messagebox")
    tk_stub.ttk = types.ModuleType("tkinter.ttk")
    for attr in ("Frame", "Label", "Button", "Entry", "Combobox", "Treeview", "Scrollbar", "LabelFrame", "Panedwindow", "Notebook", "Checkbutton", "Style"):
        setattr(tk_stub.ttk, attr, object)
    sys.modules["tkinter"] = tk_stub
    sys.modules["tkinter.filedialog"] = tk_stub.filedialog
    sys.modules["tkinter.messagebox"] = tk_stub.messagebox
    sys.modules["tkinter.ttk"] = tk_stub.ttk

from smsdesk.adb import AdbClient, AdbError, Device, MockAdbClient, format_device_labels
from smsdesk.app import SmsDeskApp
from smsdesk.crash import CrashReporter, install_thread_excepthook
from smsdesk.dispatch import (
    DispatchWorker,
    RunState,
    StateMachine,
    WorkerControls,
    acquire_device_slot,
    release_device_slot,
)
from smsdesk.storage import SCHEMA_VERSION, HistoryStore
from smsdesk.validation import (
    ValidationError,
    analyze_sms_segments,
    bounded_number,
    extract_template_variables,
    format_rejected_rows_csv,
    normalize_phone,
    parse_contacts_csv,
    preview_csv_headers,
    render_template,
    safe_truncate_message,
    validate_ip_port,
    validate_message,
    validate_pairing_code,
    validate_personalized_batch,
    validate_port,
)


class ValidationTests(unittest.TestCase):
    def test_csv_requires_column_and_explicit_row_consent(self):
        with self.assertRaisesRegex(ValidationError, "per-recipient"):
            parse_contacts_csv("phone\n+14155550101\n")
        result = parse_contacts_csv(
            "phone,consent,name\n+1 (415) 555-0101,yes,A\n+14155550102,no,B\nbad,yes,C\n"
        )
        self.assertEqual([r.phone for r in result.recipients], ["+14155550101"])
        self.assertEqual(result.recipients[0].name, "A")
        self.assertEqual(len(result.rejected), 2)
        self.assertIn("Row 3", result.rejected[0])
        self.assertEqual(result.missing_consent_count, 1)
        self.assertEqual(result.invalid_phone_count, 1)

    def test_csv_utf8_bom_column_mapping_and_rejected_export(self):
        csv_text = "\ufeffMobile,OptIn,FirstName\n+14155550101,true,Alice\n+14155550101,1,AliceDup\n+14155550102,no,Bob\n"
        headers, sample = preview_csv_headers(csv_text)
        self.assertIn("OptIn", headers)
        self.assertEqual(len(sample), 3)
        res = parse_contacts_csv(
            csv_text,
            column_map={"phone": "Mobile", "consent": "OptIn", "name": "FirstName"},
        )
        self.assertEqual(len(res.recipients), 1)
        self.assertEqual(res.duplicate_count, 1)
        self.assertEqual(res.missing_consent_count, 1)
        exported_csv = format_rejected_rows_csv(res.rejected_rows)
        self.assertIn("duplicate_in_file", exported_csv)
        self.assertIn("missing_consent", exported_csv)

    def test_normalization_preserves_intent_and_validates(self):
        self.assertEqual(normalize_phone(" +1 (415) 555-0101 "), "+14155550101")
        self.assertEqual(normalize_phone("07433033031"), "07433033031")
        self.assertEqual(normalize_phone("4155550101", default_country_code="+1"), "+14155550101")
        with self.assertRaises(ValidationError):
            normalize_phone("123abc4567")
        with self.assertRaises(ValidationError):
            normalize_phone("4155550101", default_country_code="1")

    def test_message_boundary_and_settings(self):
        self.assertEqual(len(validate_message("x" * 160)), 160)
        with self.assertRaises(ValidationError):
            validate_message("x" * 161)
        self.assertEqual(len(safe_truncate_message("x" * 200)), 160)
        self.assertEqual(len(safe_truncate_message("^" * 100)), 80)
        with self.assertRaisesRegex(ValidationError, "UCS-2"):
            validate_message("🙂" * 71)
        with self.assertRaises(ValidationError):
            bounded_number("nan", label="delay", minimum=0, maximum=5)
        self.assertEqual(validate_ip_port("192.168.1.10:5555"), "192.168.1.10:5555")
        self.assertEqual(validate_ip_port("[2001:db8::1]:41234"), "[2001:db8::1]:41234")
        self.assertEqual(validate_pairing_code("123456"), "123456")
        self.assertEqual(validate_port("5555"), 5555)
        with self.assertRaises(ValidationError):
            validate_pairing_code("12ab56")
        with self.assertRaises(ValidationError):
            validate_ip_port("host:99999")
        with self.assertRaises(ValidationError):
            validate_ip_port("adb.example.com:5555")

    def test_personalized_segment_overflow_and_missing_vars(self):
        template = "Hello {name}, code: {code} - " + ("x" * 135)
        self.assertEqual(extract_template_variables(template), ["name", "code"])
        recipients_vars = [
            ("+14155550101", {"name": "Al", "code": "12"}),
            ("+14155550102", {"name": "Christopher-Alexander-Longname", "code": "999999999"}),
            ("+14155550103", {"name": "Sam"}),
        ]
        report = validate_personalized_batch(template, recipients_vars)
        self.assertIn("+14155550101", report["valid_messages"])
        self.assertIn("+14155550102", report["overflow_by_phone"])
        self.assertEqual(report["missing_vars_by_phone"].get("+14155550103"), ["code"])
        metrics = analyze_sms_segments("Euro € test")
        self.assertEqual(metrics.encoding, "GSM")
        self.assertEqual(metrics.extension_chars, 1)
        self.assertEqual(metrics.units, len("Euro € test") + 1)


class AdbTests(unittest.TestCase):
    def test_device_parsing_and_shell_argv(self):
        client = AdbClient()
        with patch.object(
            client,
            "run",
            return_value="List of devices attached\nabc123 device product:phone\nxyz unauthorized",
        ):
            devices = client.devices()
        self.assertEqual(
            devices,
            [Device("abc123", "device", "product:phone"), Device("xyz", "unauthorized")],
        )
        self.assertTrue(devices[0].usable)
        self.assertEqual(devices[0].status_category, "usable")
        self.assertEqual(devices[1].status_category, "unauthorized")
        self.assertEqual(Device("abc", "device", "model:Pixel_8").friendly_name, "Pixel 8")
        self.assertEqual(
            Device("abc", "device", "", "Google", "Pixel 8").friendly_name, "Google Pixel 8"
        )
        self.assertEqual(
            Device("abc", "device", "", "Google", "Pixel 8 Pro").friendly_name,
            "Google Pixel 8 Pro",
        )
        with patch.object(client, "run", side_effect=["Google", "Pixel_8"]) as identity_run:
            self.assertEqual(client.device_identity("abc123"), ("Google", "Pixel 8"))
        self.assertEqual(
            [call.args[0] for call in identity_run.call_args_list],
            [
                ["shell", "getprop", "ro.product.brand"],
                ["shell", "getprop", "ro.product.model"],
            ],
        )
        self.assertTrue(all(call.kwargs["serial"] == "abc123" for call in identity_run.call_args_list))
        dangerous = "hello'; touch /tmp/unwanted; '"
        with patch.object(client, "run", return_value="") as run:
            client.stage_sms("abc123", "+14155550101", dangerous)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "shell")
        self.assertEqual(
            shlex.split(args[1]),
            [
                "am",
                "start",
                "-a",
                "android.intent.action.SENDTO",
                "-d",
                "smsto:+14155550101",
                "--es",
                "sms_body",
                dangerous,
            ],
        )
        self.assertEqual(run.call_args.kwargs["serial"], "abc123")

    def test_friendly_name_when_model_starts_with_brand(self):
        # Regression test for known bug where model starting with brand returned only brand
        dev = Device("ser1", "device", "", "Samsung", "Samsung SM-S918B")
        self.assertEqual(dev.friendly_name, "Samsung SM-S918B")
        dev_case = Device("ser2", "device", "", "google", "Google Pixel 9")
        self.assertEqual(dev_case.friendly_name, "Google Pixel 9")

    def test_duplicate_device_labels_disambiguated(self):
        d1 = Device("SERIAL_AAA1", "device", "", "Google", "Pixel 8")
        d2 = Device("SERIAL_BBB2", "device", "", "Google", "Pixel 8")
        labels = format_device_labels([d1, d2])
        self.assertNotEqual(labels["SERIAL_AAA1"], labels["SERIAL_BBB2"])
        self.assertIn("SERIAL_AAA1", labels["SERIAL_AAA1"])
        self.assertIn("SERIAL_BBB2", labels["SERIAL_BBB2"])
        self.assertEqual(d1.display_label(include_serial=False, disambiguate=True), "Google Pixel 8 [AAA1]")

    def test_timeout_and_missing_adb_are_structured(self):
        client = AdbClient()
        with patch(
            "smsdesk.adb.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("adb", 1),
        ):
            with self.assertRaisesRegex(AdbError, "timed out"):
                client.run(["devices"])

    def test_wireless_pair_and_reconnect_backoff(self):
        client = AdbClient()
        with patch.object(client, "run", return_value="Successfully paired to 192.168.1.20:39111") as run:
            out = client.pair("192.168.1.20:39111", "654321")
        self.assertIn("Successfully paired", out)
        self.assertEqual(run.call_args.args[0], ["pair", "192.168.1.20:39111", "654321"])

        calls = []
        def flaky_connect(ep):
            calls.append(ep)
            if len(calls) < 2:
                raise AdbError("transient failure")
            return "connected"

        with patch.object(client, "connect", side_effect=flaky_connect):
            res = client.reconnect_with_backoff("192.168.1.20:41555", max_attempts=3, base_delay=0.01)
        self.assertEqual(res, "connected")
        self.assertEqual(len(calls), 2)


class DeviceLabelTests(unittest.TestCase):
    def test_history_label_uses_friendly_name_and_retains_it_offline(self):
        app = object.__new__(SmsDeskApp)
        app.devices = {"serial-1": Device("serial-1", "device", "", "Google", "Pixel 8")}
        app.device_names = {}
        app.device_aliases = {}
        self.assertEqual(app._history_device_label("serial-1"), "Google Pixel 8 (serial-1)")
        app.devices = {}
        self.assertEqual(app._history_device_label("serial-1"), "Google Pixel 8 (serial-1)")
        self.assertEqual(app._history_device_label("unknown"), "unknown")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.temp.name) / "history.db")

    def tearDown(self):
        self.store.close_all()
        self.temp.cleanup()

    def test_consent_suppression_audit_and_idempotence(self):
        phones = ["+14155550101", "+14155550102"]
        self.assertEqual(self.store.import_consented(phones, "unit-test.csv"), (2, 0))
        self.store.suppress(phones[1])
        self.assertEqual(self.store.eligible(phones), [phones[0]])
        batch = self.store.create_batch("device-1", "hello", phones[:1], 1.5, 10, 30)
        item = self.store.batch_items(batch)[0]
        self.store.mark_item(item["id"], "sent", verification="user_confirmed")
        self.assertEqual(self.store.mark_item(item["id"], "failed", error="duplicate"), None)
        history = self.store.history()
        self.assertEqual(history[0]["status"], "sent")
        self.assertEqual(history[0]["verification"], "user_confirmed")
        with self.assertRaises(ValueError):
            self.store.create_batch("device-1", "hello", phones[1:], 0, 0, 0)

    def test_interrupted_batches_recovered_without_sending_pending(self):
        phone = "+14155550103"
        self.store.import_consented([phone], "test")
        batch = self.store.create_batch("serial", "hello", [phone], 1, 0, 0)
        self.store.set_batch_state(batch, "running")
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.batch_items(batch)[0]["status"], "pending")
        with self.store.connection() as db:
            event = db.execute(
                "SELECT from_state,to_state FROM batch_events WHERE batch_id=? ORDER BY id DESC LIMIT 1",
                (batch,),
            ).fetchone()
        self.assertEqual(tuple(event), ("running", "failed"))

    def test_schema_v1_migrates_to_v3_with_auto_backup(self):
        path = Path(self.temp.name) / "legacy.db"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_meta VALUES (1)")
            db.execute(
                """CREATE TABLE contacts(phone TEXT PRIMARY KEY,consented INTEGER NOT NULL CHECK(consented=1),
                consent_source TEXT NOT NULL,consented_at TEXT NOT NULL,suppressed INTEGER NOT NULL DEFAULT 0,
                suppressed_at TEXT,suppression_note TEXT)"""
            )
            db.execute(
                """CREATE TABLE batches(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,finished_at TEXT,
                device_serial TEXT NOT NULL,message TEXT NOT NULL,state TEXT NOT NULL,total INTEGER NOT NULL,
                delay_seconds REAL NOT NULL,cooldown_every INTEGER NOT NULL,cooldown_seconds REAL NOT NULL,note TEXT)"""
            )
            db.execute(
                """CREATE TABLE message_history(id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL,
                phone TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,device_serial TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 1,
                error_detail TEXT,verification TEXT,UNIQUE(batch_id,phone))"""
            )
            db.commit()
        finally:
            db.close()
        migrated = HistoryStore(path)
        try:
            self.assertIsNotNone(migrated.last_backup_path)
            self.assertTrue(migrated.last_backup_path.exists())
            with migrated.connection() as conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], SCHEMA_VERSION)
                self.assertIsNotNone(
                    conn.execute("SELECT name FROM sqlite_master WHERE name='batch_events'").fetchone()
                )
                self.assertIsNotNone(
                    conn.execute("SELECT name FROM sqlite_master WHERE name='suppression_list'").fetchone()
                )
                self.assertIsNotNone(
                    conn.execute("SELECT name FROM sqlite_master WHERE name='campaigns'").fetchone()
                )
            migrated.suppress("+14155550108")
            self.assertEqual(migrated.import_consented(["+14155550108"], "later.csv"), (0, 1))
        finally:
            migrated.close_all()

    def test_backup_and_restore_validation(self):
        phone = "+14155550199"
        self.store.import_consented([phone], "backup-test.csv")
        backup_file = self.store.backup_database()
        self.assertTrue(backup_file.exists())
        self.store.suppress(phone)
        self.assertEqual(self.store.eligible([phone]), [])
        self.store.restore_database(backup_file)
        self.assertEqual(self.store.eligible([phone]), [phone])

    def test_concurrent_job_claims_allow_only_one_winner(self):
        phone = "+14155550188"
        self.store.import_consented([phone], "claim.csv")
        batch = self.store.create_batch("serial-1", "claim test", [phone], 0, 0, 0)
        item_id = self.store.batch_items(batch)[0]["id"]
        results = []

        def attempt_claim(worker_name: str):
            results.append(self.store.claim_item(item_id, worker_name))

        t1 = threading.Thread(target=attempt_claim, args=("worker-1",))
        t2 = threading.Thread(target=attempt_claim, args=("worker-2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(sorted(results), [False, True])

    def test_suppression_can_precede_consent_import(self):
        phone = "+14155550107"
        self.store.suppress(phone)
        self.assertEqual(self.store.import_consented([phone], "later.csv"), (0, 1))
        self.assertEqual(self.store.eligible([phone]), [])

    def test_store_rejects_unsafe_message_and_unbounded_settings(self):
        phone = "+14155550109"
        self.store.import_consented([phone], "test")
        with self.assertRaises(ValidationError):
            self.store.create_batch("serial", "🙂" * 71, [phone], 0, 0, 0)
        with self.assertRaises(ValueError):
            self.store.create_batch("serial", "hello", [phone], float("inf"), 0, 0)

    def test_multi_batch_creation_is_atomic_on_ineligible_row(self):
        valid = "+14155550110"
        unknown = "+14155550111"
        self.store.import_consented([valid], "test")
        with self.assertRaisesRegex(ValueError, "non-consented"):
            self.store.create_batches("serial", "hello", [[valid], [unknown]], 0, 0, 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    def test_retry_data_includes_only_failed_not_sent_or_pending_or_uncertain(self):
        phones = ["+14155550104", "+14155550105", "+14155550106", "+14155550190"]
        self.store.import_consented(phones, "test")
        batch = self.store.create_batch("serial", "hello", phones, 0, 0, 0)
        items = self.store.batch_items(batch)
        self.store.mark_item(items[0]["id"], "sent", verification="user_confirmed")
        self.store.mark_item(items[1]["id"], "failed", error="test failure")
        self.store.mark_item(
            items[3]["id"],
            "failed",
            error="interrupted after stage",
            error_category="uncertain_interrupted",
        )
        retry_info = self.store.failed_retry_data(batch)
        self.assertEqual(retry_info["phones"], [phones[1]])
        self.assertEqual(retry_info["uncertain_phones"], [phones[3]])
        retry = self.store.create_batch("serial", "hello", [phones[1]], 0, 0, 0)
        self.assertEqual(self.store.batch_items(retry)[0]["attempt"], 2)

    def test_connections_are_thread_local(self):
        identities = []

        def task():
            with self.store.connection() as db:
                identities.append(id(db))

        threads = [threading.Thread(target=task) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(identities), 2)


class StateAndCrashTests(unittest.TestCase):
    def test_state_machine_rejects_invalid_transitions(self):
        machine = StateMachine()
        machine.transition(RunState.RUNNING)
        machine.transition(RunState.PAUSED)
        machine.transition(RunState.RUNNING)
        machine.transition(RunState.STOPPING)
        machine.transition(RunState.COMPLETED)
        with self.assertRaises(RuntimeError):
            machine.transition(RunState.PAUSED)

    def test_crash_report_is_written_and_unwritable_path_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = CrashReporter(directory)
            try:
                raise RuntimeError("safe diagnostic")
            except RuntimeError as exc:
                path = reporter.write(type(exc), exc, exc.__traceback__, context="test")
            self.assertIsNotNone(path)
            self.assertIn("Timestamp (UTC)", path.read_text(encoding="utf-8"))
            self.assertIn("Context: test", path.read_text(encoding="utf-8"))

            # Unwritable path must return None without raising
            bad_file = Path(directory) / "not_a_dir.txt"
            bad_file.write_text("file", encoding="utf-8")
            bad_reporter = CrashReporter(bad_file / "subdir")
            try:
                raise RuntimeError("should not crash")
            except RuntimeError as exc:
                res = bad_reporter.write(type(exc), exc, exc.__traceback__, context="unwritable")
            self.assertIsNone(res)

    def test_crash_report_redacts_phone_and_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = CrashReporter(directory)
            try:
                raise RuntimeError("failed for +1 (415) 555-0101; token=abc123; pair 192.168.1.5:39999 654321")
            except RuntimeError as exc:
                path = reporter.write(type(exc), exc, exc.__traceback__, context="test")
            report = path.read_text(encoding="utf-8")
            self.assertNotIn("415) 555-0101", report)
            self.assertNotIn("abc123", report)
            self.assertNotIn("654321", report)
            self.assertIn("[redacted phone-like value]", report)
            self.assertIn("token=[redacted]", report)
            self.assertIn("[redacted-pairing-code]", report)

    def test_uncaught_background_exception_writes_report(self):
        with tempfile.TemporaryDirectory() as directory, patch("threading.excepthook") as previous:
            reporter = CrashReporter(directory)
            install_thread_excepthook(reporter)

            def fail():
                raise RuntimeError("background diagnostic")

            thread = threading.Thread(target=fail, name="test-background-crash")
            thread.start()
            thread.join(2)
            reports = list(Path(directory).glob("crash-*.txt"))
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(reports), 1)
            self.assertIn(
                "uncaught worker exception in test-background-crash",
                reports[0].read_text(encoding="utf-8"),
            )
            previous.assert_called_once()


class DispatchTests(unittest.TestCase):
    def test_staged_intent_requires_user_confirmation_before_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phone = "+14155550120"
            store.import_consented([phone], "test")
            batch = store.create_batch("serial", "hello", [phone], 0, 0, 0)
            item = store.batch_items(batch)[0]

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    self.verified = serial

                def stage_sms(self, serial, target, message):
                    self.staged = (serial, target, message)

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            worker.start()
            while True:
                event = events.get(timeout=2)
                if event[0] == "confirm":
                    self.assertEqual(event[1], item["id"])
                    staged_item = store.batch_items(batch)[0]
                    self.assertEqual(staged_item["status"], "pending")
                    self.assertEqual(staged_item["verification"], "composer_staged")
                    controls.ack.put(True)
                    break
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(store.batch_items(batch)[0]["status"], "sent")
            self.assertEqual(store.history()[0]["verification"], "user_confirmed")
            store.close_all()

    def test_declined_item_does_not_retrigger_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phones = ["+14155550151", "+14155550152", "+14155550153"]
            store.import_consented(phones, "test")
            # cooldown_every=1, cooldown_seconds=5
            batch = store.create_batch("serial", "hello", phones, 0, 1, 5.0)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    pass

                def stage_sms(self, serial, target, message):
                    pass

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            cooldown_waits = []
            orig_wait = worker._interruptible_wait

            def spy_wait(seconds, *, phase="delay"):
                if phase == "cooldown":
                    cooldown_waits.append(seconds)

            worker._interruptible_wait = spy_wait
            worker.start()
            # 1st item confirmed -> triggers cooldown at boundary 1
            # 2nd item declined -> total_sent stays 1, MUST NOT retrigger cooldown!
            # 3rd item declined -> last item
            responses = [True, False, False]
            idx = 0
            while idx < len(responses):
                ev = events.get(timeout=2)
                if ev[0] == "confirm":
                    controls.ack.put(responses[idx])
                    idx += 1
            worker.join(2)
            self.assertEqual(cooldown_waits, [5.0])
            store.close_all()

    def test_cross_batch_cooldown_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phones = ["+14155550161", "+14155550162"]
            store.import_consented(phones, "test")
            # cooldown_every=2, sent_before=1 -> 1st item in this batch reaches total_sent=2
            batch = store.create_batch("serial", "hello", phones, 0, 2, 4.0)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    pass

                def stage_sms(self, serial, target, message):
                    pass

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls, sent_before=1)
            cooldown_waits = []
            worker._interruptible_wait = lambda sec, *, phase="delay": cooldown_waits.append((phase, sec))
            worker.start()
            for _ in range(2):
                while events.get(timeout=2)[0] != "confirm":
                    pass
                controls.ack.put(True)
            worker.join(2)
            self.assertIn(("cooldown", 4.0), cooldown_waits)
            store.close_all()

    def test_pause_freezes_delay_and_stop_interrupts_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phones = ["+14155550171", "+14155550172"]
            store.import_consented(phones, "test")
            batch = store.create_batch("serial", "hello", phones, 1.5, 0, 0)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    pass

                def stage_sms(self, serial, target, message):
                    pass

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            # Negative duration boundary check: _interruptible_wait(-5) returns immediately
            t0 = time.monotonic()
            worker._interruptible_wait(-5.0)
            self.assertLess(time.monotonic() - t0, 0.2)

            worker.start()
            while events.get(timeout=2)[0] != "confirm":
                pass
            controls.ack.put(True)
            # Wait until worker enters delay countdown, then pause and stop
            while True:
                ev = events.get(timeout=2)
                if ev[0] == "countdown" and ev[1] == "delay":
                    controls.pause.set()
                    break
            while True:
                ev = events.get(timeout=2)
                if ev[0] == "state" and ev[1] == "paused":
                    controls.stop.set()
                    break
            worker.join(2)
            self.assertFalse(worker.is_alive())
            items = store.batch_items(batch)
            self.assertEqual([i["status"] for i in items], ["sent", "pending"])
            store.close_all()

    def test_competing_worker_for_same_device_is_rejected(self):
        self.assertTrue(acquire_device_slot("DEV-LOCK-1"))
        try:
            self.assertFalse(acquire_device_slot("DEV-LOCK-1"))
        finally:
            release_device_slot("DEV-LOCK-1")

    def test_suppressed_recipient_is_not_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phone = "+14155550121"
            store.import_consented([phone], "test")
            batch = store.create_batch("serial", "hello", [phone], 0, 0, 0)
            store.suppress(phone)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    raise AssertionError("suppressed contact must be blocked before ADB")

                def stage_sms(self, serial, target, message):
                    raise AssertionError("suppressed contact must never be staged")

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(store.batch_items(batch)[0]["status"], "failed")
            self.assertIn("suppressed", store.batch_items(batch)[0]["error_detail"])
            store.close_all()

    def test_adb_failure_leaves_later_recipients_pending_for_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phones = ["+14155550131", "+14155550132"]
            store.import_consented(phones, "test")
            batch = store.create_batch("serial", "hello", phones, 0, 0, 0)

            class FailingAdb:
                def verify_sms_activity(self, serial):
                    raise AdbError("device disconnected")

                def stage_sms(self, serial, target, message):
                    raise AssertionError("stage should not run after verification failure")

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FailingAdb(), events, controls)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            items = store.batch_items(batch)
            self.assertEqual([item["status"] for item in items], ["failed", "pending"])
            self.assertEqual(store.pending_recovery_data(batch)["phones"], [phones[1]])
            store.close_all()

    def test_stop_during_confirmation_leaves_item_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phone = "+14155550140"
            store.import_consented([phone], "test")
            batch = store.create_batch("serial", "hello", [phone], 0, 0, 0)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    return "package/.Activity"

                def stage_sms(self, serial, target, message):
                    return None

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            worker.start()
            while True:
                event = events.get(timeout=2)
                if event[0] == "confirm":
                    controls.stop.set()
                    break
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(store.batch_items(batch)[0]["status"], "pending")
            self.assertEqual(store.pending_recovery_data(batch)["phones"], [phone])
            store.close_all()

    def test_pause_is_cooperative_and_defers_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "db.sqlite")
            phone = "+14155550141"
            store.import_consented([phone], "test")
            batch = store.create_batch("serial", "hello", [phone], 0, 0, 0)

            class FakeAdb:
                def verify_sms_activity(self, serial):
                    pass

                def stage_sms(self, serial, target, message):
                    pass

            events, controls = queue.Queue(), WorkerControls.create()
            worker = DispatchWorker(batch, store, FakeAdb(), events, controls)
            worker.start()
            while events.get(timeout=2)[0] != "confirm":
                pass
            controls.pause.set()
            while True:
                event = events.get(timeout=2)
                if event[0] == "state" and event[1] == "paused":
                    break
            self.assertEqual(store.batch_items(batch)[0]["status"], "pending")
            controls.ack.put(True)
            controls.pause.clear()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(store.batch_items(batch)[0]["status"], "sent")
            store.close_all()



    def test_gui_event_payload_compatibility_and_dry_run_mock(self):
        mock_adb = MockAdbClient()
        avail = mock_adb.check_availability()
        self.assertTrue(avail["available"])
        devs = mock_adb.devices()
        self.assertEqual(len(devs), 1)
        self.assertTrue(devs[0].usable)
        mock_adb.stage_sms(devs[0].serial, "+14155550101", "dry run hello")
        self.assertEqual(len(mock_adb.staged_history), 1)


if __name__ == "__main__":
    unittest.main()
