# sms-app — Consent-first SMS Desk (v2.0.0)

A Windows, macOS, and Linux Python desktop application for **human-confirmed** Android SMS composition workflows over USB or Wi-Fi Android Debug Bridge (`adb`). It is built for lawful, explicitly opted-in recipients only. It never simulates tapping Send, never bypasses Android or carrier permissions, and never marks a message sent or delivered without explicit evidence.

## Architecture & Repository Structure

```text
sms-app/
├── main.py
├── README.md
├── smsdesk/
│   ├── __init__.py
│   ├── app.py
│   ├── adb.py
│   ├── dispatch.py
│   ├── storage.py
│   ├── validation.py
│   └── crash.py
└── tests/
    └── test_core.py
```

- `main.py`: Tkinter desktop entry point.
- `smsdesk/app.py`: 8-tab navy/blue/cyan desktop interface (`#0F172A` / `#1E293B` / `#007ACC` / `#06B6D4`), non-blocking tooltips, background worker orchestration, and event queue polling.
- `smsdesk/adb.py`: Safe `subprocess.run(..., shell=False)` ADB execution, USB/Wi-Fi discovery, brand/model resolution (fixing the brand-prefix edge case), Android 11+ wireless pairing, legacy USB-initialized TCP/IP mode, bounded reconnect with backoff, messaging activity diagnostics, and `MockAdbClient` for dry-run testing.
- `smsdesk/dispatch.py`: Cooperative background worker enforcing one active worker per device, transactional job claiming, `composer_staged` state tracking, pause-freezing monotonic countdowns, and once-per-boundary cooldowns.
- `smsdesk/storage.py`: Thread-local SQLite persistence (`SCHEMA_VERSION = 3`), automatic pre-migration online backups, v1->v2->v3 schema migrations, campaigns, batches, saved templates, device registry, settings, and sanitized audit logs.
- `smsdesk/validation.py`: GSM 03.38 (basic + 2-septet extension table) and UCS-2 (UTF-16BE) segment accounting, `{variable}` personalization rendering and per-recipient limit checks, CSV header preview, column mapping, and rejected-row CSV export.
- `smsdesk/crash.py`: Main-thread, worker-thread, and Tkinter callback crash handlers with redaction of phone numbers, `smsto:` URIs, pairing codes, and secrets, automatic file rotation, and sanitized `.zip` support bundle creation.

## Requirements & Installation

- **Python**: Python 3.10 or newer (uses only the Python standard library; no third-party pip packages required).
- **Tkinter**: Included with standard Windows/macOS Python installers (`python3-tk` on Debian/Ubuntu Linux).
- **Android SDK Platform-Tools**: `adb` available on `PATH` or configured in the **7. Settings** tab.

## Launch & Test Commands

Run the desktop application from the repository root:

```bash
python main.py
```

Run the full unit and regression test suite (uses temporary SQLite databases and mocked ADB transports; never contacts real recipients or devices):

```bash
python -m unittest discover -s tests -v
```

## Database Migration & Backup Instructions

1. **Automatic Pre-Migration Backup**: When `HistoryStore.initialize()` detects an existing `sms_history_pro.db` at schema v1 or v2, it automatically uses SQLite's online `Connection.backup()` API to create a consistent backup in `db_backups/sms_history_pro-pre-v2-to-v3-<timestamp>.db` before executing `BEGIN IMMEDIATE` schema migrations to v3.
2. **Manual Backup & Restore**: Open **7. Settings -> Back Up Database Now…** or call `store.backup_database()`. To restore after a failed migration or disk issue, use **Restore Database from Backup…** (which verifies `PRAGMA integrity_check` and `schema_meta` before replacing the active file, keeping a safety copy if anything fails).

## Actual Sending vs. Manual Composer Staging

- **Current ADB Transport**: `AdbClient.stage_sms()` dispatches `am start -a android.intent.action.SENDTO -d smsto:<phone> --es sms_body <message>`. On modern stock Android (Android 10–15), unprivileged ADB shell cannot reliably or portably send SMS directly without opening the user-facing messaging app, and ADB exit code `0` only confirms that the `SENDTO` intent was handed to Android's ActivityManager.
- **Truthful Recording**: Opening the composer records `status='pending', verification='composer_staged'`. Only when the operator taps Send on the phone and clicks **Yes** in the desktop confirmation dialog does the record transition to `status='sent', verification='user_confirmed', delivery_status='unverified'`.
- **Why Key-Event Simulation Is Forbidden**: Sending `input keyevent KEYCODE_DPAD_RIGHT` + `KEYCODE_ENTER` is brittle across OEM messaging apps, screen locks, dual-SIM dialogs, and IME layouts, and can accidentally trigger unintended UI actions or mark unsent messages as sent.
- **Supported Alternatives (Require Separate Approval)**: True hands-free campaign dispatch after a single campaign-level approval requires either a dedicated, user-authorized Android companion service holding the `SEND_SMS` permission with `SmsManager` `sentIntent`/`deliveryIntent` callbacks, or an external carrier/CPaaS API with webhook delivery receipts.
