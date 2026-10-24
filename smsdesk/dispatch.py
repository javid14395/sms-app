"""Cancellable worker that stages one opted-in recipient at a time for human send."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

from .adb import AdbClient, AdbError
from .storage import HistoryStore


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


ALLOWED = {
    RunState.IDLE: {RunState.RUNNING},
    RunState.RUNNING: {RunState.PAUSED, RunState.STOPPING, RunState.COMPLETED, RunState.FAILED},
    RunState.PAUSED: {RunState.RUNNING, RunState.STOPPING, RunState.COMPLETED, RunState.FAILED},
    RunState.STOPPING: {RunState.COMPLETED, RunState.FAILED},
    RunState.COMPLETED: {RunState.IDLE},
    RunState.FAILED: {RunState.IDLE},
}


class StateMachine:
    def __init__(self):
        self.state = RunState.IDLE
        self.lock = threading.Lock()

    def transition(self, state: RunState) -> None:
        with self.lock:
            if state == self.state:
                return
            if state not in ALLOWED[self.state]:
                raise RuntimeError(f"Illegal state transition: {self.state.value} -> {state.value}")
            self.state = state


@dataclass
class WorkerControls:
    pause: threading.Event
    stop: threading.Event
    ack: queue.Queue

    @classmethod
    def create(cls) -> "WorkerControls":
        return cls(threading.Event(), threading.Event(), queue.Queue(maxsize=1))


class DispatchWorker(threading.Thread):
    """Stages SMS composition and requires affirmative user confirmation to advance.

    ADB cannot portably prove the Android user pressed Send. The application
    therefore never silently marks a staged intent as sent.
    """
    def __init__(self, batch_id: int, store: HistoryStore, adb: AdbClient,
                 events: queue.Queue, controls: WorkerControls, *,
                 sent_before: int = 0, has_more_batches: bool = False):
        super().__init__(name=f"sms-batch-{batch_id}", daemon=False)
        self.batch_id, self.store, self.adb = batch_id, store, adb
        self.events, self.controls = events, controls
        self.sent_before = sent_before
        self.has_more_batches = has_more_batches

    def _wait_if_paused(self) -> bool:
        was_paused = False
        while self.controls.pause.is_set() and not self.controls.stop.is_set():
            if not was_paused:
                self.store.set_batch_state(self.batch_id, "paused")
                self.events.put(("state", "paused", "Paused"))
                was_paused = True
            time.sleep(.1)
        if was_paused and not self.controls.stop.is_set():
            self.store.set_batch_state(self.batch_id, "running")
            self.events.put(("state", "running", "Resumed"))
        return not self.controls.stop.is_set()

    def run(self) -> None:
        try:
            items = self.store.batch_items(self.batch_id)
            batch = self._batch_config()
            self.store.set_batch_state(self.batch_id, "running")
            self.events.put(("state", "running", "Batch started"))
            done = 0
            stop_reason = None
            pending_items = [item for item in items if item["status"] == "pending"]
            for index, item in enumerate(pending_items):
                if not self._wait_if_paused():
                    break
                try:
                    if not self.store.eligible([item["phone"]]):
                        self.store.mark_item(item["id"], "failed", error="Recipient is suppressed or no longer consented.")
                        self.events.put(("history_changed",))
                        self.events.put(("log", "WARN", "Suppressed recipient was blocked before staging."))
                        continue
                    self.adb.verify_sms_activity(item["device_serial"])
                    self.adb.stage_sms(item["device_serial"], item["phone"], item["message"])
                    self.events.put(("confirm", item["id"], item["phone"]))
                    while not self.controls.stop.is_set():
                        if self.controls.pause.is_set():
                            if not self._wait_if_paused():
                                break
                        try:
                            confirmed = self.controls.ack.get(timeout=.2)
                            break
                        except queue.Empty:
                            pass
                    else:
                        confirmed = False
                    if self.controls.stop.is_set():
                        break
                    if confirmed:
                        # Confirmation is explicitly recorded as user-attested, not ADB-verified.
                        self.store.mark_item(item["id"], "sent", verification="user_confirmed")
                        self.events.put(("history_changed",))
                        done += 1
                        self.events.put(("log", "SUCCESS", "User confirmed send for recipient ending " + item["phone"][-4:]))
                    else:
                        self.store.mark_item(item["id"], "failed", error="User declined or did not confirm send.")
                        self.events.put(("history_changed",))
                    has_more_messages = index < len(pending_items) - 1 or self.has_more_batches
                    if has_more_messages:
                        delay = batch["delay_seconds"]
                        if delay:
                            self._interruptible_wait(delay)
                        total_sent = self.sent_before + done
                        if (batch["cooldown_every"] and done and
                                total_sent % batch["cooldown_every"] == 0):
                            self._interruptible_wait(batch["cooldown_seconds"])
                except AdbError as exc:
                    self.store.mark_item(item["id"], "failed", error=str(exc))
                    self.events.put(("history_changed",))
                    self.events.put(("log", "ERROR", str(exc)))
                    stop_reason = "ADB error; remaining recipients were left pending."
                    break
            self.events.put(("batch_done", done))
            if self.controls.stop.is_set():
                self.store.set_batch_state(self.batch_id, "stopping", note="Graceful stop requested.")
                self.store.set_batch_state(self.batch_id, "completed", finished=True, note="Stopped by user; unprocessed recipients remain pending.")
                self.events.put(("state", "completed", "Stopped safely; pending items were not resent."))
            else:
                self.store.set_batch_state(self.batch_id, "completed", finished=True, note=stop_reason)
                self.events.put(("state", "completed", stop_reason or "Batch complete"))
        except Exception as exc:
            try:
                self.store.set_batch_state(self.batch_id, "failed", finished=True, note=str(exc)[:300])
            finally:
                self.events.put(("worker_error", exc))

    def _batch_config(self) -> dict:
        with self.store.connection() as db:
            row = db.execute("SELECT delay_seconds,cooldown_every,cooldown_seconds FROM batches WHERE id=?",
                             (self.batch_id,)).fetchone()
            if row is None:
                raise RuntimeError("Batch record disappeared.")
            return dict(row)

    def _interruptible_wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.controls.stop.is_set():
            if not self._wait_if_paused():
                return
            time.sleep(min(.1, deadline - time.monotonic()))
