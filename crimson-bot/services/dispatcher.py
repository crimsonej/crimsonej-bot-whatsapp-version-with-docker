"""
services/dispatcher.py
======================
Single background loop that ticks the task store, runs due tasks with
retry + exponential backoff, and emits events.

This replaces the bespoke loop in `services/scheduler.py`. The status-posting
scheduler becomes just another recurring task registered at boot.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import traceback

from core.config import cfg, log
from core.eventlog import event_log
from services.tasks import task_store
import services.natural_cron as natural_cron
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_dispatcher_thread: threading.Thread | None = None
_dispatcher_stop = threading.Event()
_dispatcher_lock = threading.Lock()
_dispatcher_alive: bool = False


class Dispatcher:
    def __init__(self) -> None:
        self.concurrency = int(cfg("task_max_concurrent") or 3)
        self.tick_secs = 5
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._tz = ZoneInfo("Africa/Kampala")
        self.reporter_send = None  # set by reporter.start()

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        global _dispatcher_thread, _dispatcher_stop

        if _dispatcher_thread and _dispatcher_thread.is_alive():
            log.warning("[Dispatcher] already running, ignoring start")
            return

        _dispatcher_stop = threading.Event()
        _dispatcher_thread = threading.Thread(
            target=self._loop, name="CrimsonDispatcher", daemon=True
        )
        _dispatcher_thread.start()
        global _dispatcher_alive
        _dispatcher_alive = True
        log.info("[Dispatcher] started — tick=%ds concurrency=%d", self.tick_secs, self.concurrency)

    def stop(self) -> None:
        if _dispatcher_stop:
            _dispatcher_stop.set()

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    # ── Loop ─────────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not _dispatcher_stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.error("[Dispatcher] tick error: %s", exc)
                log.debug(traceback.format_exc())
            _dispatcher_stop.wait(timeout=self.tick_secs)

        log.info("[Dispatcher] stopped")

    def _tick(self) -> None:
        with self._inflight_lock:
            free_slots = max(0, self.concurrency - self._inflight)
        if free_slots <= 0:
            return
        due = task_store.claim_due(limit=free_slots)
        for task in due:
            with self._inflight_lock:
                # re-check at spawn time, in case concurrency filled up
                if self._inflight >= self.concurrency:
                    break
                self._inflight += 1
            t = threading.Thread(
                target=self._worker, args=(task,), daemon=True,
                name=f"Worker:{task.get('name','?')[:20]}"
            )
            t.start()

    # ── Worker ───────────────────────────────────────────────────────────────
    def _worker(self, task: dict) -> None:
        task_id = task["id"]
        name = task.get("name") or "task"
        owner_user_id = task.get("owner_user_id") or ""
        try:
            event_log.append("task", "task_start",
                             summary=f"[Task {task_id}] {name} started",
                             user_id=owner_user_id or None,
                             payload={"task_id": task_id, "name": name})

            action = task.get("action") or {}
            result = self._invoke(action, task=task)

            # Mark done
            task_store.done(task_id, result)

            event_log.append("task", "task_done",
                             summary=f"[Task {task_id}] {name} done",
                             user_id=owner_user_id or None,
                             payload={"task_id": task_id, "name": name,
                                      "summary": _short_result(result)})

            # Notify if requested
            notify_on = task.get("notify_on") or "done"
            if notify_on in ("done", "always") and self.reporter_send:
                delivered = bool(self.reporter_send(task, "done", result))
                task_store.mark_notification(task_id, sent=delivered)

            # Requeue only after notification handling so recurring tasks retain
            # a pending state and an undelivered result can still be retried.
            self._reschedule_if_recurring(task)

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.warning("[Dispatcher] task %s failed: %s", task_id, err)
            attempts = int(task.get("attempts") or 0)
            max_attempts = int(task.get("max_attempts") or 0)

            if attempts + 1 < max_attempts:
                # Requeue for retry with exponential backoff
                backoff = float(2 ** attempts) * 30  # 30s, 60s, 120s, 240s …
                task_store.requeue_for_retry(task_id, backoff_secs=backoff, error=err)
                event_log.append("task", "task_retry",
                                 summary=f"[Task {task_id}] retry #{attempts+1} in {int(backoff)}s: {err}",
                                 user_id=owner_user_id or None,
                                 payload={"task_id": task_id, "attempts": attempts + 1,
                                          "backoff_secs": backoff, "error": err})
                event_log.append("task", "task_fail",
                                 summary=f"[Task {task_id}] {err}",
                                 user_id=owner_user_id or None,
                                 jid=task.get("owner_jid") or None,
                                 payload={"task_id": task_id, "name": name,
                                          "error": err,
                                          "owner_jid": task.get("owner_jid") or ""})
                return
            else:
                task_store.fail(task_id, err)
                event_log.append("task", "task_fail",
                                 summary=f"[Task {task_id}] {name} failed: {err}",
                                 user_id=owner_user_id or None,
                                 jid=task.get("owner_jid") or None,
                                 payload={"task_id": task_id, "name": name,
                                          "error": err,
                                          "owner_jid": task.get("owner_jid") or ""})
                notify_on = task.get("notify_on") or "done"
                if notify_on in ("failed", "always") and self.reporter_send:
                    delivered = bool(self.reporter_send(task, "failed", err))
                    task_store.mark_notification(task_id, sent=delivered)

                # For recurring tasks, even if last run failed, reschedule the next.
                self._reschedule_if_recurring(task)

        finally:
            with self._inflight_lock:
                self._inflight = max(0, self._inflight - 1)

    # ── Action runner ────────────────────────────────────────────────────────
    def _invoke(self, action: dict, *, task: dict):
        if not action:
            raise ValueError("task has no action")
        module_name = action.get("module")
        fn_name = action.get("fn")
        if not (module_name and fn_name):
            raise ValueError("action requires 'module' and 'fn'")

        # Allow simple dotted paths like "media.download_youtube_task"
        if module_name in sys.modules:
            mod = sys.modules[module_name]
        else:
            mod = importlib.import_module(module_name)

        fn = getattr(mod, fn_name, None)
        if fn is None:
            raise ValueError(f"action function {module_name}.{fn_name} not found")

        args = action.get("args") or ()
        kwargs = dict(action.get("kwargs") or {})
        if "task_id" in kwargs:
            kwargs["task_id"] = task.get("id", kwargs["task_id"])

        # Build a ProgressSession if the task owner has a JID we can post to.
        progress = None
        jid = task.get("owner_jid")
        if jid:
            from services.progress import ProgressSession
            label = action.get("progress_label") or task.get("name") or "working"
            progress = ProgressSession(jid)
            progress.start(_format_progress_label(label))
            kwargs["progress"] = progress

            # Stash the message_id on the task so the cancel route can find it.
            if progress.message_id:
                try:
                    task_store.update(
                        task["id"],
                        progress={"message_id": progress.message_id,
                                  "pct": 5, "message": "running"},
                    )
                except Exception:
                    pass

        try:
            return fn(*args, **kwargs)
        finally:
            if progress is not None:
                try:
                    progress.finish()
                except Exception as exc:
                    log.debug("[Dispatcher] progress.finish failed: %s", exc)

    # ── Recurrence ───────────────────────────────────────────────────────────
    def _reschedule_if_recurring(self, task: dict) -> None:
        if task.get("kind") != "recurring":
            return
        sched = task.get("schedule") or {}
        nxt = natural_cron.next_after(sched)
        if not nxt:
            return
        task_store.update(
            task["id"],
            status="pending",
            started_at=None,
            finished_at=None,
            schedule={"next_run_at": nxt.timestamp()},
        )


def _short_result(result) -> str:
    if result is None:
        return ""
    s = str(result)
    return s if len(s) < 200 else s[:200] + "…"


def _format_progress_label(label: str) -> str:
    """Sanitise + lightly format a label into something nice as the progress
    header (e.g. "🎬 downloading", "🎨 generating", "⏰ reminder")."""
    label = (label or "").strip()
    if not label:
        return "working"
    # Strip redundant action verbs the task name already implies.
    label = label.replace("_", " ")
    if label.startswith("download "):
        return label  # already has "download …"
    if label.startswith("generate "):
        return label
    if label.startswith("reminder"):
        return f"⏰ {label}"
    return label


# ── Module singleton + helpers ────────────────────────────────────────────────
_dispatcher: Dispatcher | None = None
_pending_reporter_sender = None  # stored when the reporter plugs in before boot


def start_dispatcher() -> None:
    global _dispatcher, _pending_reporter_sender
    if _dispatcher is None:
        _dispatcher = Dispatcher()
        if _pending_reporter_sender is not None:
            _dispatcher.reporter_send = _pending_reporter_sender
            _pending_reporter_sender = None
    _dispatcher.start()


def stop_dispatcher() -> None:
    if _dispatcher:
        _dispatcher.stop()
        global _dispatcher_alive
        _dispatcher_alive = False


def set_reporter_sender(fn) -> None:
    """Reporter plugs in its send_wa() function here after it boots."""
    global _dispatcher, _pending_reporter_sender
    if _dispatcher:
        _dispatcher.reporter_send = fn
    else:
        # Dispatcher may not be created yet (lazy boot via health auto-heal).
        # Stash it so start_dispatcher() applies it once the singleton exists.
        _pending_reporter_sender = fn


def get_dispatcher() -> Dispatcher | None:
    return _dispatcher


def dispatcher_is_alive() -> bool:
    return _dispatcher_alive and bool(_dispatcher_thread) and _dispatcher_thread.is_alive()
