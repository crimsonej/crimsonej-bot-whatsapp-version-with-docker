"""
services/feedback.py
====================
Explicit feedback learning system for Crimsonej.
Handles user ratings (good/bad) and adapts behavior accordingly.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime
from typing import Any

from core.config import BASE_DIR, TZ, log
from services.memory import profile_mgr


# ─── Persistent Feedback State ────────────────────────────────────────────────

_FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback_state.json")
_feedback_lock = threading.Lock()
_feedback_state: dict[str, dict] = {}  # user_id -> {positive, negative, patterns, last_updated}

def _load_feedback() -> None:
    global _feedback_state
    if os.path.exists(_FEEDBACK_FILE):
        try:
            with open(_FEEDBACK_FILE, "r", encoding="utf-8") as f:
                _feedback_state = json.load(f)
        except Exception:
            _feedback_state = {}

def _save_feedback() -> None:
    with _feedback_lock:
        with open(_FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(_feedback_state, f, ensure_ascii=False, indent=2)

_load_feedback()


# ─── Feedback Recording ──────────────────────────────────────────────────────

FEEDBACK_POSITIVE = {"good", "great", "nice", "perfect", "awesome", "sick", "fire", "love it", "thanks", "thank you", "helpful", "exactly", "spot on", "nailed it", "👍", "❤️", "🔥"}
FEEDBACK_NEGATIVE = {"bad", "wrong", "stupid", "useless", "garbage", "trash", "hate", "annoying", "wtf", "no", "nope", "not that", "incorrect", "failed", "flop", "👎", "💩", "🤬"}

def detect_feedback(message: str) -> str | None:
    """Detect if message contains explicit feedback. Returns 'positive', 'negative', or None."""
    msg_lower = message.lower()
    
    for phrase in FEEDBACK_POSITIVE:
        if phrase in msg_lower:
            return "positive"
    
    for phrase in FEEDBACK_NEGATIVE:
        if phrase in msg_lower:
            return "negative"
    
    return None


def record_feedback(user_id: str, feedback_type: str, context: dict = None) -> dict:
    """Record explicit feedback from user."""
    with _feedback_lock:
        if user_id not in _feedback_state:
            _feedback_state[user_id] = {
                "positive": 0,
                "negative": 0,
                "patterns": {},  # topic -> {pos, neg}
                "last_updated": time.time(),
            }
        
        state = _feedback_state[user_id]
        
        if feedback_type == "positive":
            state["positive"] += 1
        elif feedback_type == "negative":
            state["negative"] += 1
        
        # Track by topic/context
        if context and context.get("topic"):
            topic = context["topic"]
            if topic not in state["patterns"]:
                state["patterns"][topic] = {"positive": 0, "negative": 0}
            state["patterns"][topic][feedback_type] += 1
        
        state["last_updated"] = time.time()
        _save_feedback()
        
        return state


def get_feedback_summary(user_id: str) -> dict:
    """Get feedback summary for a user."""
    with _feedback_lock:
        return dict(_feedback_state.get(user_id, {"positive": 0, "negative": 0, "patterns": {}}))


def get_feedback_ratio(user_id: str) -> float:
    """Get positive feedback ratio (0-1)."""
    state = get_feedback_summary(user_id)
    total = state["positive"] + state["negative"]
    if total == 0:
        return 0.5
    return state["positive"] / total


def should_adapt_behavior(user_id: str) -> bool:
    """Check if bot should adapt based on feedback."""
    state = get_feedback_summary(user_id)
    total = state["positive"] + state["negative"]
    if total < 3:  # Need minimum feedback
        return False
    ratio = state["positive"] / total if total > 0 else 0.5
    return ratio < 0.4 or ratio > 0.75  # Adapt if strongly negative or positive


def get_adaptation_hint(user_id: str) -> str | None:
    """Get hint for how to adapt based on feedback."""
    if not should_adapt_behavior(user_id):
        return None
    
    state = get_feedback_summary(user_id)
    ratio = state["positive"] / (state["positive"] + state["negative"])
    
    if ratio < 0.4:
        return "User giving lots of negative feedback. Be more careful, ask clarifying questions, tone down roasts."
    elif ratio > 0.75:
        return "User loving the responses. Keep the energy, lean into current style."
    
    return None


# ─── Context-Aware Feedback ──────────────────────────────────────────────────

def extract_topic_from_context(context: dict = None) -> str | None:
    """Extract topic from conversation context."""
    if not context:
        return None
    return context.get("topic") or context.get("last_tool") or context.get("interaction_type")


def process_feedback_message(message: str, user_id: str, context: dict = None) -> dict | None:
    """Process a message for feedback and record if found."""
    feedback = detect_feedback(message)
    if not feedback:
        return None
    
    topic = extract_topic_from_context(context)
    return record_feedback(user_id, feedback, {"topic": topic} if topic else None)


# ─── Quick Test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test feedback detection
    test_messages = [
        "that's great!",
        "you're stupid",
        "thanks that helped",
        "this is garbage",
        "perfect thanks",
        "wtf is this",
        "exactly what I needed",
    ]
    
    for msg in test_messages:
        fb = detect_feedback(msg)
        print(f"'{msg}' -> {fb}")
    
    # Test recording
    record_feedback("test_user", "positive", {"topic": "trading"})
    record_feedback("test_user", "negative", {"topic": "roast"})
    print("\nFeedback summary:", get_feedback_summary("test_user"))
    print("Ratio:", get_feedback_ratio("test_user"))
    print("Adapt:", should_adapt_behavior("test_user"))
    print("Hint:", get_adaptation_hint("test_user"))