"""
services/scheduler.py
=====================
Status-posting scheduler (Thin wrapper around the task engine).

The old bespoke loop has been replaced by `services.dispatcher.py`. The
status-posting behaviour is now a recurring task created at boot. This
file remains so existing imports (`start_scheduler`, `stop_scheduler`,
`restart_scheduler`, `trigger_now`) and master-control commands keep
working unchanged.
"""

from __future__ import annotations

import os
import threading
import time

from core.config import cfg, log, TZ
from core.eventlog import event_log
from services.tasks import task_store
import services.natural_cron as natural_cron


STATUS_TASK_NAME = "auto_post_status"
_owner_jid = ""      # set at boot from env if available
_status_task_id: str | None = None


def _build_status_prompt(topics: list[str]) -> str:
    from datetime import datetime
    now = datetime.now(TZ).strftime("%A, %d %b %Y  %H:%M")
    if topics:
        topic_list = ", ".join(topics)
        return (
            f"It's {now}. You are Crimsonej.\n"
            f"You want to post a WhatsApp status update about one of these topics: {topic_list}.\n"
            "First, briefly search the web for the latest news or info on the most relevant topic right now.\n"
            "Then write a short, punchy status post (max 3 sentences) that sounds like a real person — "
            "savage, clever, with a personal opinion. Include relevant emojis. "
            "Do NOT sound like a news bot. Sound like yourself.\n"
            "Call the post_status tool with your final status text."
        )
    return (
        f"It's {now}. You are Crimsonej.\n"
        "You feel like posting a WhatsApp status update. "
        "Pick something interesting — a hot take, something you're thinking about, "
        "a roast, a tech opinion, a trading insight, or just a vibe check. "
        "Keep it short, punchy, and real (max 3 sentences). Use emojis naturally.\n"
        "Call the post_status tool with your final status text."
    )


def run_status_post_cycle() -> dict:
    """Single cycle: build prompt, call LLM with tools (uses post_status).

    Used as the action.fn of the recurring status-posting task. Returns a
    summary dict for the dispatcher to log."""
    from core.llm import call_llm
    from services.tools import ALL_TOOLS, execute_tool_calls
    import services.vision as vision_svc

    topics = cfg("status_scheduler_topics") or []
    prompt = _build_status_prompt(topics)
    messages = [
        {
            "role": "system",
            "content": (
                "You are Crimsonej. Post an authentic, engaging WhatsApp status update. "
                "Use web_search if you need live data, then call post_status to publish. "
                "DO NOT just describe what you'll do — actually call the tools."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def _tool_exec(tool_calls, msgs, uid, sjid):
        return execute_tool_calls(tool_calls, msgs, uid, sjid,
                                  vision_service=vision_svc)

    reply = call_llm(messages, tools=ALL_TOOLS, tool_executor_fn=_tool_exec,
                     user_id="scheduler", sender_jid="scheduler")
    reply_text = reply.get("reply", "") if isinstance(reply, dict) else str(reply)
    event_log.append("scheduler", "status_posted",
                     summary=f"status cycle reply={reply_text[:80]!r}",
                     payload={"topics": topics, "reply": reply_text[:300]})
    return {"posted": True, "reply": reply_text[:300]}


# ── Public API ────────────────────────────────────────────────────────────────
def start_scheduler() -> None:
    """Idempotent boot: ensure dispatcher is up and status task is registered."""
    from services.dispatcher import start_dispatcher
    start_dispatcher()
    _ensure_status_task()


def stop_scheduler() -> None:
    from services.dispatcher import stop_dispatcher
    stop_dispatcher()


def restart_scheduler() -> None:
    stop_scheduler()
    time.sleep(0.5)
    start_scheduler()


def trigger_now() -> str:
    """Fire a one-shot status-post task immediately (background)."""
    t = task_store.create(
        kind="one_shot",
        name="manual_status_post",
        action={"module": "services.scheduler", "fn": "run_status_post_cycle", "args": []},
        owner_user_id="scheduler",
        owner_jid=_owner_jid or "",
        notify_on="none",
        metadata={"trigger": "manual"},
    )
    return f"🚀 Triggering status post now (task #{t['id']})"


def _ensure_status_task() -> None:
    """Create (or refresh) the recurring status-post task."""
    global _status_task_id

    enabled = bool(cfg("status_scheduler_enabled"))
    if not enabled:
        log.info("[Scheduler] status posting disabled by config; no recurring task")
        _status_task_id = None
        return

    interval_hours = float(cfg("status_scheduler_interval_hours") or 4)
    topics = cfg("status_scheduler_topics") or []
    schedule_text = f"every {int(interval_hours)} hours" if interval_hours >= 1 else f"every {int(interval_hours * 60)} minutes"
    parsed = natural_cron.parse(schedule_text)
    if not parsed:
        log.warning("[Scheduler] could not parse %r; defaulting to 4h", schedule_text)
        parsed = natural_cron.parse("every 4 hours")

    sched, _next_dt = parsed
    # Update existing status task if present
    existing = [t for t in task_store.list(kind="recurring", status="pending")
                if t.get("name") == STATUS_TASK_NAME]
    if existing:
        t = existing[0]
        task_store.update(t["id"], schedule=sched, action={"module": "services.scheduler",
                                                             "fn": "run_status_post_cycle",
                                                             "args": []},
                          metadata={"topics": topics})
        _status_task_id = t["id"]
        log.info("[Scheduler] refreshed status-post task #%s every %s",
                 _status_task_id, schedule_text)
    else:
        t = task_store.create(
            kind="recurring",
            name=STATUS_TASK_NAME,
            action={"module": "services.scheduler", "fn": "run_status_post_cycle",
                    "args": []},
            schedule=sched,
            owner_user_id="scheduler",
            owner_jid=_owner_jid or "",
            notify_on="none",
            metadata={"topics": topics},
        )
        _status_task_id = t["id"]
        log.info("[Scheduler] registered status-post task #%s every %s",
                 _status_task_id, schedule_text)
