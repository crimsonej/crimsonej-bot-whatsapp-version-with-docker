"""
services/summarizer.py
======================
Conversation summarization for long sessions.
Compresses history while preserving key facts, decisions, and personality context.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from core.config import BASE_DIR, TZ, log
from services.memory import sessions, profile_mgr
from core.llm import call_llm, scout_quick_call


# ─── Configuration ────────────────────────────────────────────────────────────

SUMMARY_TRIGGER_TURNS = 20  # Summarize after this many turns
SUMMARY_MAX_TURNS = 50      # Keep this many raw turns after summarization
SUMMARY_MODEL = "scout"     # Use scout model for speed


# ─── Persistent Summaries ────────────────────────────────────────────────────

_SUMMARIES_FILE = os.path.join(BASE_DIR, "conversation_summaries.json")
_summary_lock = threading.Lock()
_summaries: dict[str, dict] = {}  # session_key -> {summary, turn_count, key_facts, last_updated}

def _load_summaries() -> None:
    global _summaries
    if os.path.exists(_SUMMARIES_FILE):
        try:
            with open(_SUMMARIES_FILE, "r", encoding="utf-8") as f:
                _summaries = json.load(f)
        except Exception:
            _summaries = {}

def _save_summaries() -> None:
    with _summary_lock:
        with open(_SUMMARIES_FILE, "w", encoding="utf-8") as f:
            json.dump(_summaries, f, ensure_ascii=False, indent=2)

_load_summaries()


def get_conversation_summary(session_key: str) -> dict | None:
    """Get existing summary for session."""
    with _summary_lock:
        return dict(_summaries.get(session_key, {}))


def save_conversation_summary(session_key: str, summary: str, key_facts: list[str], turn_count: int) -> None:
    """Save conversation summary."""
    with _summary_lock:
        _summaries[session_key] = {
            "summary": summary,
            "key_facts": key_facts,
            "turn_count": turn_count,
            "last_updated": datetime.now(TZ).isoformat(),
        }
    _save_summaries()


# ─── Summarization Logic ────────────────────────────────────────────────────

SUMMARIZATION_PROMPT = """You are Crimsonej summarizing a conversation for your own memory.

Conversation history (oldest first):
{history}

Create a concise summary that captures:
1. Key topics discussed
2. Important facts learned about the user
3. Decisions made or actions taken
4. User's current mood/state
5. Any unresolved threads or promises

Output ONLY a JSON object:
{
  "summary": "2-3 sentence narrative summary",
  "key_facts": ["fact1", "fact2", "fact3"],
  "topics": ["topic1", "topic2"],
  "user_state": "brief description of user's current state",
  "open_threads": ["thread1", "thread2"]
}"""


def should_summarize(session_key: str) -> bool:
    """Check if session needs summarization."""
    session = sessions.get(session_key)
    if not session:
        return False
    
    turns = len(session.turns)
    if turns < SUMMARY_TRIGGER_TURNS:
        return False
    
    # Check if already summarized recently
    existing = get_conversation_summary(session_key)
    if existing and turns - existing.get("turn_count", 0) < SUMMARY_TRIGGER_TURNS:
        return False
    
    return True


def summarize_conversation(session_key: str, force: bool = False) -> dict | None:
    """Summarize conversation using LLM."""
    if not force and not should_summarize(session_key):
        return None
    
    session = sessions.get(session_key)
    if not session:
        return None
    
    # Get conversation history
    turns = session.turns
    if len(turns) < 4:
        return None
    
    # Build history text (last 30 turns max for context)
    recent_turns = turns[-30:]
    history_parts = []
    for turn in recent_turns:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        if content:
            history_parts.append(f"{role}: {content[:500]}")
    
    history = "\n".join(history_parts)
    
    # Call LLM for summarization
    messages = [
        {"role": "system", "content": "You are Crimsonej. Summarize conversations accurately and concisely."},
        {"role": "user", "content": SUMMARIZATION_PROMPT.format(history=history)}
    ]
    
    try:
        if SUMMARY_MODEL == "scout":
            result = scout_quick_call(messages, max_tokens=512, timeout=15.0)
        else:
            result = call_llm(messages, max_tokens=512, timeout=20.0)
        
        content = result.get("reply", "") if isinstance(result, dict) else str(result)
        
        # Parse JSON response
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(content)
        
        summary = data.get("summary", "")
        key_facts = data.get("key_facts", [])
        
        if summary:
            turn_count = len(turns)
            save_conversation_summary(session_key, summary, key_facts, turn_count)
            log.info("[Summarizer] Summarized session %s (%d turns)", session_key[:20], turn_count)
            return {"summary": summary, "key_facts": key_facts}
        
    except Exception as e:
        log.warning("[Summarizer] Failed to summarize %s: %s", session_key, e)
    
    return None


def get_summary_context(session_key: str) -> str:
    """Get formatted summary context for system prompt."""
    summary_data = get_conversation_summary(session_key)
    if not summary_data:
        return ""
    
    summary = summary_data.get("summary", "")
    key_facts = summary_data.get("key_facts", [])
    topics = summary_data.get("topics", [])
    user_state = summary_data.get("user_state", "")
    open_threads = summary_data.get("open_threads", [])
    
    if not summary:
        return ""
    
    parts = [
        f"\n[CONVERSATION SUMMARY]",
        f"Previous context: {summary}",
    ]
    
    if key_facts:
        parts.append(f"Key facts: {'; '.join(key_facts)}")
    if topics:
        parts.append(f"Topics covered: {', '.join(topics)}")
    if user_state:
        parts.append(f"User state: {user_state}")
    if open_threads:
        parts.append(f"Open threads: {'; '.join(open_threads)}")
    
    return "\n".join(parts) + "\n"


# ─── Auto-Summarize on Turn Add ────────────────────────────────────────────

def maybe_summarize(session_key: str) -> None:
    """Call after adding a turn to check if summarization needed."""
    try:
        summarize_conversation(session_key)
    except Exception as e:
        log.debug("[Summarizer] Auto-summarize failed: %s", e)


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test should_summarize logic
    print("Summarizer module loaded")
    print(f"Trigger at {SUMMARY_TRIGGER_TURNS} turns")
    print(f"Keep {SUMMARY_MAX_TURNS} raw turns")