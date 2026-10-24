"""Timestamped local crash reports and support bundles that redact sensitive data."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import BUILD_ID, __version__

_PHONE_LIKE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]*){7,}\d(?!\w)")
_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|authorization|secret|api[_-]?key|pairing[_-]?code|sms_body|message)\b"
    r"(\s*[:=]\s*)(['\"]?[^\s,;'\"]+['\"]?)"
)
_SMSTO = re.compile(r"(?i)smsto:\+?[0-9 ()-]+")
_PAIR_CMD = re.compile(r"(?i)(\bpair\s+\S+\s+)([0-9]{4,8}\b)")


def _safe_diagnostic(value: str) -> str:
    """Remove common recipient/credential/message values before writing local reports."""
    value = _SMSTO.sub("smsto:[redacted-phone]", value)
    value = _PAIR_CMD.sub(r"\1[redacted-pairing-code]", value)
    value = _PHONE_LIKE.sub("[redacted phone-like value]", value)
    return _SECRET.sub(r"\1\2[redacted]", value)


class CrashReporter:
    def __init__(
        self,
        directory: str | Path = "crash_reports",
        notify: Callable[[str], None] | None = None,
        *,
        max_reports: int = 50,
    ):
        self.directory = Path(directory)
        self.notify = notify
        self.max_reports = max(5, int(max_reports))
        self._lock = threading.Lock()
        self._previous = sys.excepthook

    def install(self) -> None:
        sys.excepthook = self.handle

    def handle(self, exc_type, exc, tb) -> None:
        self.write(exc_type, exc, tb, context="uncaught main-thread exception")
        if self._previous and self._previous is not self.handle:
            self._previous(exc_type, exc, tb)

    def _rotate_locked(self) -> None:
        """Retain only the newest `max_reports` crash files."""
        try:
            reports = sorted(
                self.directory.glob("crash-*.txt"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old_path in reports[self.max_reports :]:
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def write(self, exc_type, exc, tb, *, context: str) -> Path | None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            try:
                exception_text = str(exc)[:1000]
            except Exception:
                exception_text = "<exception text unavailable>"
            try:
                formatted_traceback = "".join(traceback.format_exception(exc_type, exc, tb))[-12000:]
            except Exception:
                formatted_traceback = "<traceback formatting failed>"
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"crash-{timestamp}-{threading.get_ident()}.txt"
                text = (
                    f"Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}\n"
                    f"Application version: {__version__} ({BUILD_ID})\n"
                    f"Python version: {platform.python_version()} ({platform.platform()})\n"
                    f"Context: {_safe_diagnostic(context)}\n"
                    f"Exception type: {getattr(exc_type, '__name__', 'Exception')}\n"
                    f"Exception: {_safe_diagnostic(exception_text)}\n\n"
                    + _safe_diagnostic(formatted_traceback)
                )
                path.write_text(text, encoding="utf-8")
                self._rotate_locked()
            if self.notify:
                try:
                    self.notify(f"An unexpected error occurred. A report was saved to {path}.")
                except Exception:
                    # Reporting must not replace the original application error.
                    pass
            return path
        except Exception:
            # Crash handling is a last-resort path: filesystem or formatting
            # failures must not cascade into another uncaught exception.
            return None

    def create_support_bundle(
        self,
        destination: str | Path,
        *,
        diagnostics_info: dict | None = None,
        audit_events: list[dict] | None = None,
    ) -> Path:
        """Create a sanitized ZIP support bundle containing recent crash logs and non-PII diagnostics."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        safe_diag = {}
        for k, v in (diagnostics_info or {}).items():
            safe_diag[str(k)] = _safe_diagnostic(str(v))
        safe_audit = []
        for ev in (audit_events or [])[:200]:
            safe_audit.append({str(k): _safe_diagnostic(str(v)) for k, v in ev.items()})

        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            summary = {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "app_version": __version__,
                "build_id": BUILD_ID,
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "cwd": _safe_diagnostic(str(Path.cwd())),
                "pid": os.getpid(),
                "diagnostics": safe_diag,
                "audit_trail_sample": safe_audit,
            }
            zf.writestr("diagnostics_summary.json", json.dumps(summary, indent=2))
            if self.directory.exists():
                for report_path in sorted(self.directory.glob("crash-*.txt"))[-20:]:
                    try:
                        content = _safe_diagnostic(report_path.read_text(encoding="utf-8", errors="replace"))
                        zf.writestr(f"crash_reports/{report_path.name}", content)
                    except OSError:
                        pass
        return dest


def install_thread_excepthook(reporter: CrashReporter) -> None:
    """Capture uncaught thread failures while retaining Python's standard reporting."""
    original = threading.excepthook

    def hook(args) -> None:
        reporter.write(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            context=f"uncaught worker exception in {args.thread.name}",
        )
        original(args)

    threading.excepthook = hook
