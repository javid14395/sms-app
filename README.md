# Consent-first SMS Desk

A Windows/macOS/Linux Python desktop utility for **human-confirmed** SMS workflows
with Android devices connected over USB or Wi-Fi ADB. It is designed for lawful,
explicitly opted-in recipients only. It does not automate tapping Send, bypass
Android/carrier controls, or claim that an ADB intent proves delivery.

## Requirements / launch

- Python 3.10 or newer (standard library only)
- Android platform-tools (`adb`) on `PATH`, or configure `AdbClient(executable=...)`
- An Android device with USB debugging enabled and an authorized computer, or
  Wireless debugging enabled

Run from the project directory:

```text
python main.py
```

The local database defaults to `sms_history_pro.db`; crash reports go to
`crash_reports/`. These paths are relative to the current working directory.
Keep the database directory access-controlled because it contains recipient
numbers and message content.

The desktop UI uses a dark navy/blue palette with cyan focus and action accents,
and stacks its recipient/device panels when the window is narrow. Device choices
show Android brand/model properties when available, while retaining the ADB
serial for unambiguous targeting.

## Consent and suppression contract

CSV must contain a `phone` column and a **per-row** `consent` or `opt_in` column.
Accepted affirmative values are `yes`, `true`, `1`, and `opted-in` (case-insensitive).
Every other row is rejected from the eligible set and listed with its row number.
An unchecked global checkbox is never treated as consent. Keep consent source and
proof in your own records; this app records the import filename and import time,
not the original evidence of consent. Phone formatting separators are removed;
the number itself is not guessed or rewritten.

Use “Suppress” immediately when a recipient opts out. Suppression is persistent,
wins over later imports, and is checked again immediately before staging. Maintain
an appropriate opt-out process outside this application too; it cannot monitor
incoming SMS or detect replies.

CSV example:

```csv
phone,consent
+14155550101,yes
+14155550102,no
```

## Sending behavior / operational timing

The app validates one message to a single SMS segment (no multipart messages):
up to 160 GSM septets, or up to 70 UTF-16 units for UCS-2 content, with an
additional 160-code-point input ceiling. Oversized input is visibly truncated
to a single-segment boundary. It validates device serial, recipient eligibility,
and bounded delay/batch/cooldown settings. It creates auditable batches and opens Android's `SENDTO` SMS composer
for one recipient at a time. A person must check the recipient/message, press
Send on the device, and then explicitly confirm in the desktop app. ADB cannot
reliably verify the send action across arbitrary messaging applications; records
therefore say `user_confirmed`, not device-verified or delivered. Declined,
interrupted, or failed entries remain auditable. Stop leaves unprocessed entries
pending; they are not silently resent after restart. “Retry failed” creates a
new batch for failed entries only, re-checks consent/suppression, and requires
confirmation. “Resume pending” is separately explicit, skips sent/failed rows,
and warns the operator to check Android sent history first because an interruption
may occur between sending on-device and recording the user's confirmation. This
is a deliberate anti-duplication safeguard.

Delay and cooldown are explicit operator-configured operational intervals, not
tools for mimicking human behavior or avoiding spam flags, carrier safeguards,
or enforcement. Set them only to meet documented organizational or legal
requirements. The application does not attempt to bypass sending limits or
controls. Comply with local law, carrier terms, platform rules, applicable
quiet-hour/identification requirements, and any stricter organizational limits.

## History / recovery

`sms_history_pro.db` stores contact consent/import source, suppression state,
batch metadata, message text, device serial, per-recipient pending/sent/failed
state, UTC timestamps, attempt count, user-attestation type, and errors. SQLite
uses foreign keys, transactions, indexes, a schema version with a v1-to-v2
migration, durable batch state-transition events, and isolated thread-local
connections. Interrupted batches are marked failed on startup while unprocessed
messages remain pending. Back up the database securely.

## Tests

```text
python -m unittest discover -s tests -v
```

Tests use temporary SQLite files and mocked ADB; no physical Android device is
required. A desktop Tk session is needed for the interactive UI.
