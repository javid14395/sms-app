"""Timestamped local crash reports that avoid recording message/phone contents."""

from __future__ import annotations

import sys
import re
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_PHONE_LIKE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]*){7,}\d(?!\w)")
_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|authorization|secret|api[_-]?key)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _safe_diagnostic(value: str) -> str:
    """Remove common recipient/credential values before writing local reports."""
    value = _PHONE_LIKE.sub("[redacted phone-like value]", value)
    return _SECRET.sub(r"\1\2[redacted]", value)


class CrashReporter:
    def __init__(self, directory: str | Path = "crash_reports",
                 notify: Callable[[str], None] | None = None):
        self.directory = Path(directory)
        self.notify = notify
        self._lock = threading.Lock()
        self._previous = sys.excepthook

    def install(self) -> None:
        sys.excepthook = self.handle

    def handle(self, exc_type, exc, tb) -> None:
        self.write(exc_type, exc, tb, context="uncaught main-thread exception")
        if self._previous and self._previous is not self.handle:
            self._previous(exc_type, exc, tb)

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
                    f"Context: {context}\n"
                    f"Exception type: {getattr(exc_type, '__name__', 'Exception')}\n"
                    f"Exception: {_safe_diagnostic(exception_text)}\n\n"
                    + _safe_diagnostic(formatted_traceback)
                )
                path.write_text(text, encoding="utf-8")
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


def install_thread_excepthook(reporter: CrashReporter) -> None:
    """Capture uncaught thread failures while retaining Python's standard reporting."""
    original = threading.excepthook

    def hook(args) -> None:
        reporter.write(args.exc_type, args.exc_value, args.exc_traceback,
                       context=f"uncaught worker exception in {args.thread.name}")
        original(args)

    threading.excepthook = hook
