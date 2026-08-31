"""
services/proactive.py
=====================
Proactive outreach system for Crimsonej.
Handles check-ins, follow-ups, scheduled reminders, and contextual outreach.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from core.config import cfg, log, TZ
from core.eventlog import event_log
from services.tasks import task_store
from services.memory import profile_mgr
from services.personality import get_relationship_level, Relationship
import services.bridge_api as bridge_api


# ─── Follow-up Triggers ───────────────────────────────────────────────────────

FOLLOWUP_TEMPLATES = {
    "post_trade": [
        "How'd that {symbol} trade go?",
        "Did that {symbol} setup play out?",
        "Still in that {symbol} position or out?",
    ],
    "post_song": [
        "Enjoying that track?",
        "That song hit different 🎵",
        "Found any more gems like that?",
    ],
    "post_roast": [
        "Still salty or we good? 😂",
        "You alive over there?",
        "Need a tissue or you straight?",
    ],
    "post_analysis": [
        "That {symbol} analysis help?",
        "You take that {symbol} trade?",
        "What's your read on {symbol} now?",
    ],
    "post_image": [
        "That image turn out right?",
        "Using that as wallpaper?",
        "Need any tweaks to it?",
    ],
    "inactive_checkin": [
        "Been quiet. You good?",
        "Haven't seen you in a bit. Everything cool?",
        "Yo, you still there?",
    ],
    "market_open": [
        "London open in 10. You watching anything?",
        "Market's waking up. Got a bias?",
        "Pre-London check — what's on the radar?",
    ],
    "market_close": [
        "Day's done. How'd it go?",
        "NY close — win or learn?",
        "Wrap it up. What's the damage?",
    ],
    "weekend": [
        "Weekend mode. Trading or chilling?",
        "Charts closed. What's the vibe?",
        "Off the screens. What you up to?",
    ],
}


def _get_relationship(user_id: str) -> Relationship:
    return get_relationship_level(user_id)


def _should_proactive(user_id: str, trigger_type: str) -> bool:
    """Check if we should send proactive message based on relationship."""
    rel = _get_relationship(user_id)
    
    # Never proactive to strangers
    if rel == Relationship.STRANGER:
        return False
    
    # Creator/Partner always
    if rel in [Relationship.CREATOR, Relationship.PARTNER]:
        return True
    
    # Close friends - high chance
    if rel == Relationship.CLOSE_FRIEND:
        return random.random() < 0.4
    
    # Friends - medium chance
    if rel == Relationship.FRIEND:
        return random.random() < 0.25
    
    # Acquaintances - low chance
    if rel == Relationship.ACQUAINTANCE:
        return random.random() < 0.1
    
    return False


def schedule_followup(user_id: str, trigger_type: str, context: dict = None, delay_minutes: int = 30) -> str | None:
    """Schedule a follow-up message after an interaction."""
    if not _should_proactive(user_id, trigger_type):
        return None
    
    templates = FOLLOWUP_TEMPLATES.get(trigger_type, [])
    if not templates:
        return None
    
    template = random.choice(templates)
    message = template.format(**(context or {}))
    
    # Get user's JID
    profile = profile_mgr.get_profile(user_id)
    jid = profile.get("jid") or f"{user_id}@s.whatsapp.net"
    
    # Create scheduled task
    from datetime import datetime, timedelta
    run_at = datetime.now(TZ) + timedelta(minutes=delay_minutes)
    
    task = task_store.create(
        kind="one_shot",
        name=f"followup_{trigger_type}",
        action={
            "module": "services.proactive",
            "fn": "send_proactive_message",
            "kwargs": {
                "user_id": user_id,
                "jid": jid,
                "message": message,
                "trigger_type": trigger_type,
            }
        },
        schedule={"next_run_at": run_at.timestamp()},
        owner_user_id=user_id,
        owner_jid=jid,
        notify_on="none",
        metadata={"trigger_type": trigger_type, "context": context or {}},
    )
    
    log.info("[Proactive] Scheduled %s followup for %s in %dm (task #%s)", 
             trigger_type, user_id, delay_minutes, task["id"])
    return task["id"]


def send_proactive_message(user_id: str, jid: str, message: str, trigger_type: str) -> dict:
    """Send a proactive message via bridge with dynamic LLM natural variation."""
    try:
        # Try dynamic fast LLM call for natural human variation
        try:
            from core.llm import scout_quick_call
            profile = profile_mgr.get_profile(user_id)
            name = profile.get("name") or "friend"
            prompt = [
                {"role": "system", "content": "You are Crimsonej. Generate a short, natural 1-sentence WhatsApp message to check in with a friend. No AI fluff, no bullet points, casual tone."},
                {"role": "user", "content": f"Friend's name: {name}. Topic context: {trigger_type}. Seed idea: '{message}'. Make it sound like a quick natural WhatsApp message."}
            ]
            res = scout_quick_call(prompt, max_tokens=60, timeout=3.0)
            if res and res.get("reply"):
                gen = res["reply"].strip().replace('"', '')
                if gen:
                    message = gen
        except Exception as llm_exc:
            log.debug("[Proactive] LLM synthesis fallback to template: %s", llm_exc)

        r = bridge_api.bridge_send(jid, message)
        if r.get("ok"):
            event_log.append("proactive", "sent", 
                           summary=f"Proactive {trigger_type} sent to {user_id}",
                           user_id=user_id, jid=jid,
                           payload={"message": message, "trigger": trigger_type})
            return {"ok": True, "message_id": r.get("message_id")}
        return {"ok": False, "error": r.get("error")}
    except Exception as e:
        log.error("[Proactive] Failed to send: %s", e)
        return {"ok": False, "error": str(e)}


def schedule_inactive_checkin(user_id: str, hours_inactive: int = 24) -> str | None:
    """Schedule a check-in if user hasn't messaged in X hours."""
    if not _should_proactive(user_id, "inactive_checkin"):
        return None
    
    profile = profile_mgr.get_profile(user_id)
    last_seen = profile.get("last_seen")
    if not last_seen:
        return None
    
    # Check if actually inactive
    last_dt = datetime.fromisoformat(last_seen)
    if datetime.now(TZ) - last_dt < timedelta(hours=hours_inactive):
        return None
    
    return schedule_followup(user_id, "inactive_checkin", delay_minutes=0)


def schedule_market_hours_checkin(user_id: str, session: str = "pre_london") -> str | None:
    """Schedule market hours check-ins (pre-London, NY open, close)."""
    if not _should_proactive(user_id, f"market_{session}"):
        return None
    
    # Get user's timezone preference or default to EAT
    delay_map = {
        "pre_london": 0,      # 07:30 EAT
        "london_open": 30,    # 08:00 EAT
        "ny_open": 0,         # 13:00 EAT
        "close": 0,           # 21:30 EAT
    }
    
    return schedule_followup(user_id, f"market_{session}", 
                           delay_minutes=delay_map.get(session, 0))


def get_pending_followups(user_id: str) -> list[dict]:
    """Get all pending followup tasks for a user."""
    tasks = task_store.list(owner_user_id=user_id, status="pending", limit=50)
    return [t for t in tasks if t.get("name", "").startswith("followup_")]


def cancel_followups(user_id: str, trigger_type: str = None) -> int:
    """Cancel pending followups for a user."""
    tasks = get_pending_followups(user_id)
    cancelled = 0
    for task in tasks:
        if trigger_type is None or trigger_type in task.get("name", ""):
            task_store.cancel(task["id"])
            cancelled += 1
    return cancelled


# ─── Background Checker ───────────────────────────────────────────────────────

_checker_thread: threading.Thread | None = None
_checker_stop = threading.Event()
_checker_lock = threading.Lock()


def _checker_loop() -> None:
    """Periodic background check for proactive opportunities."""
    while not _checker_stop.is_set():
        try:
            _run_proactive_checks()
        except Exception as e:
            log.error("[Proactive] Checker error: %s", e)
        _checker_stop.wait(timeout=300)  # Check every 5 minutes


def _run_proactive_checks() -> None:
    """Run all periodic proactive checks."""
    # Check inactive users
    for user_id, profile in profile_mgr.profiles.items():
        if user_id == "250203957407887":  # Skip creator (handled separately)
            continue
        schedule_inactive_checkin(user_id, hours_inactive=48)
    
    # Market hours checks could be added here
    # For now, handled by trading_scheduler


def start_proactive_checker() -> None:
    global _checker_thread
    with _checker_lock:
        if _checker_thread and _checker_thread.is_alive():
            return
        _checker_stop.clear()
        _checker_thread = threading.Thread(
            target=_checker_loop, name="ProactiveChecker", daemon=True
        )
        _checker_thread.start()
        log.info("[Proactive] Background checker started")


def stop_proactive_checker() -> None:
    global _checker_thread
    _checker_stop.set()
    if _checker_thread:
        _checker_thread.join(timeout=5)
        _checker_thread = None
        log.info("[Proactive] Background checker stopped")


# ─── Context-Aware Triggers ───────────────────────────────────────────────────

def trigger_post_interaction(user_id: str, interaction_type: str, context: dict = None) -> str | None:
    """Call after a significant interaction to schedule follow-up."""
    trigger_map = {
        "trade_logged": "post_trade",
        "song_downloaded": "post_song",
        "roast_delivered": "post_roast",
        "analysis_done": "post_analysis",
        "image_generated": "post_image",
    }
    
    trigger = trigger_map.get(interaction_type)
    if not trigger:
        return None
    
    return schedule_followup(user_id, trigger, context, delay_minutes=random.randint(15, 120))


# ─── Quick Test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test relationship check
    for uid in ["250203957407887", "21106022990028", "99999999999"]:
        rel = _get_relationship(uid)
        should = _should_proactive(uid, "test")
        print(f"User {uid[:8]}: rel={rel.name}, proactive={should}")
    
    # Test templates
    for trigger, templates in FOLLOWUP_TEMPLATES.items():
        print(f"\n{trigger}:")
        for t in templates[:2]:
            print(f"  - {t}")