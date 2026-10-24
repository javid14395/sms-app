"""Cancellable worker that stages one opted-in recipient at a time for human send.

Enforces single-worker-per-device locking, transactional job claiming,
pause-aware monotonic countdowns (freezing remaining wait time on pause),
and once-per-boundary cooldown triggers.
"""

from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum

from .adb import AdbClient, AdbError
from .storage import HistoryStore

# Global registry of active device serials to prevent competing workers from
# controlling the same physical Android device simultaneously.
_DEVICE_LOCKS_GUARD = threading.Lock()
_ACTIVE_DEVICE_SERIALS: set[str] = set()


def acquire_device_slot(serial: str) -> bool:
    with _DEVICE_LOCKS_GUARD:
        if serial in _ACTIVE_DEVICE_SERIALS:
            return False
        _ACTIVE_DEVICE_SERIALS.add(serial)
        return True


def release_device_slot(serial: str) -> None:
    with _DEVICE_LOCKS_GUARD:
        _ACTIVE_DEVICE_SERIALS.discard(serial)


def active_device_serials() -> set[str]:
    with _DEVICE_LOCKS_GUARD:
        return set(_ACTIVE_DEVICE_SERIALS)


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

    def __init__(
        self,
        batch_id: int,
        store: HistoryStore,
        adb: AdbClient,
        events: queue.Queue,
        controls: WorkerControls,
        *,
        sent_before: int = 0,
        has_more_batches: bool = False,
    ):
        super().__init__(name=f"sms-batch-{batch_id}", daemon=False)
        self.batch_id, self.store, self.adb = batch_id, store, adb
        self.events, self.controls = events, controls
        self.sent_before = sent_before
        self.has_more_batches = has_more_batches
        self._triggered_cooldown_boundaries: set[int] = set()

    def _wait_if_paused(self) -> bool:
        was_paused = False
        while self.controls.pause.is_set() and not self.controls.stop.is_set():
            if not was_paused:
                self.store.set_batch_state(self.batch_id, "paused")
                self.events.put(("state", "paused", "Paused"))
                was_paused = True
            time.sleep(0.05)
        if was_paused and not self.controls.stop.is_set():
            self.store.set_batch_state(self.batch_id, "running")
            self.events.put(("state", "running", "Resumed"))
        return not self.controls.stop.is_set()

    def run(self) -> None:
        claimed_device: str | None = None
        try:
            batch = self._batch_config()
            device_serial = batch.get("device_serial", "")
            if device_serial:
                if not acquire_device_slot(device_serial):
                    raise RuntimeError(
                        f"Device {device_serial} already has an active dispatch worker."
                    )
                claimed_device = device_serial

            items = self.store.batch_items(self.batch_id)
            self.store.set_batch_state(self.batch_id, "running")
            self.events.put(("state", "running", "Batch started"))
            done = 0
            stop_reason = None
            pending_items = [item for item in items if item["status"] == "pending"]
            for index, item in enumerate(pending_items):
                if self.controls.stop.is_set() or not self._wait_if_paused():
                    break
                # Claim the item transactionally so concurrent workers cannot claim the same row
                if not self.store.claim_item(item["id"], self.name):
                    continue
                try:
                    if not self.store.eligible([item["phone"]]):
                        self.store.mark_item(
                            item["id"],
                            "failed",
                            error="Recipient is suppressed or no longer consented.",
                            error_category="skipped_ineligible",
                        )
                        self.events.put(("history_changed",))
                        self.events.put(("log", "WARN", "Suppressed recipient was blocked before staging."))
                        continue

                    # Recheck stop immediately before side-effecting ADB calls
                    if self.controls.stop.is_set():
                        self.store.release_unconfirmed_claim(item["id"])
                        break

                    self.adb.verify_sms_activity(item["device_serial"])
                    if self.controls.stop.is_set():
                        self.store.release_unconfirmed_claim(item["id"])
                        break

                    self.adb.stage_sms(item["device_serial"], item["phone"], item["message"])
                    self.store.mark_staged(item["id"])
                    self.events.put(("history_changed",))
                    self.events.put(("confirm", item["id"], item["phone"]))

                    while not self.controls.stop.is_set():
                        if self.controls.pause.is_set():
                            if not self._wait_if_paused():
                                break
                        try:
                            confirmed = self.controls.ack.get(timeout=0.1)
                            break
                        except queue.Empty:
                            pass
                    else:
                        confirmed = False

                    if self.controls.stop.is_set():
                        # Leave item in 'pending' (preserving existing stop-during-confirmation semantics)
                        break

                    confirmed_this_item = False
                    if confirmed:
                        # Confirmation is explicitly recorded as user-attested, not ADB-verified.
                        self.store.mark_item(
                            item["id"],
                            "sent",
                            verification="user_confirmed",
                            delivery_status="unverified",
                        )
                        self.events.put(("history_changed",))
                        done += 1
                        confirmed_this_item = True
                        self.events.put(
                            ("log", "SUCCESS", "User confirmed send for recipient ending " + item["phone"][-4:])
                        )
                    else:
                        self.store.mark_item(
                            item["id"],
                            "failed",
                            error="User declined or did not confirm send.",
                            verification="operator_declined",
                            error_category="operator_declined",
                        )
                        self.events.put(("history_changed",))

                    # Check whether any remaining items or batches exist before waiting
                    has_more_messages = index < len(pending_items) - 1 or self.has_more_batches
                    if has_more_messages and not self.controls.stop.is_set():
                        delay = float(batch["delay_seconds"] or 0.0)
                        jitter = float(batch.get("jitter_seconds") or 0.0)
                        if jitter > 0:
                            delay += random.uniform(0.0, jitter)
                        if delay > 0:
                            self._interruptible_wait(delay, phase="delay")

                        # Cooldown triggers ONLY when this item succeeded, total_sent is a positive
                        # multiple of cooldown_every, and this exact boundary hasn't triggered yet.
                        total_sent = self.sent_before + done
                        cooldown_every = int(batch["cooldown_every"] or 0)
                        cooldown_seconds = float(batch["cooldown_seconds"] or 0.0)
                        if (
                            confirmed_this_item
                            and cooldown_every > 0
                            and cooldown_seconds > 0
                            and total_sent > 0
                            and total_sent % cooldown_every == 0
                            and total_sent not in self._triggered_cooldown_boundaries
                        ):
                            self._triggered_cooldown_boundaries.add(total_sent)
                            self.events.put(
                                ("log", "INFO", f"Cooldown reached after {total_sent} confirmed send(s) ({cooldown_seconds:g}s).")
                            )
                            self._interruptible_wait(cooldown_seconds, phase="cooldown")
                except AdbError as exc:
                    self.store.mark_item(
                        item["id"],
                        "failed",
                        error=str(exc),
                        error_category="adb_transport_error",
                    )
                    self.events.put(("history_changed",))
                    self.events.put(("log", "ERROR", str(exc)))
                    stop_reason = "ADB error; remaining recipients were left pending."
                    break

            # Finalize database batch state BEFORE emitting completion events
            if self.controls.stop.is_set():
                self.store.set_batch_state(self.batch_id, "stopping", note="Graceful stop requested.")
                self.store.set_batch_state(
                    self.batch_id,
                    "completed",
                    finished=True,
                    note="Stopped by user; unprocessed recipients remain pending.",
                )
                self.events.put(("batch_done", done))
                self.events.put(("state", "completed", "Stopped safely; pending items were not resent."))
            else:
                self.store.set_batch_state(self.batch_id, "completed", finished=True, note=stop_reason)
                self.events.put(("batch_done", done))
                self.events.put(("state", "completed", stop_reason or "Batch complete"))
        except Exception as exc:
            try:
                self.store.set_batch_state(self.batch_id, "failed", finished=True, note=str(exc)[:300])
            finally:
                self.events.put(("worker_error", exc))
        finally:
            if claimed_device:
                release_device_slot(claimed_device)

    def _batch_config(self) -> dict:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT device_serial, delay_seconds, cooldown_every, cooldown_seconds, jitter_seconds FROM batches WHERE id=?",
                (self.batch_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Batch record disappeared.")
            return dict(row)

    def _interruptible_wait(self, seconds: float, *, phase: str = "delay") -> None:
        """Monotonic countdown wait that freezes remaining time while paused and never sleeps negatively."""
        remaining = max(0.0, float(seconds))
        if remaining <= 0.0:
            return
        last_tick = time.monotonic()
        while remaining > 0.0 and not self.controls.stop.is_set():
            if self.controls.pause.is_set():
                self.events.put(("countdown", phase, remaining, True))
                if not self._wait_if_paused():
                    self.events.put(("countdown", "idle", 0.0, False))
                    return
                # Reset monotonic anchor after resume so pause duration is frozen
                last_tick = time.monotonic()
                continue
            self.events.put(("countdown", phase, remaining, False))
            sleep_slice = max(0.0, min(0.1, remaining))
            if sleep_slice > 0.0:
                time.sleep(sleep_slice)
            now = time.monotonic()
            elapsed = max(0.0, now - last_tick)
            last_tick = now
            remaining = max(0.0, remaining - elapsed)
        self.events.put(("countdown", "idle", 0.0, False))
