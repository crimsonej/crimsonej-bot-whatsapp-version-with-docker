"""
services/memory.py
==================
User profiling, session store, permanent personal vault, and global knowledge base.
Now backed by SQLite (services/storage.py) for persistence and concurrency.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any

from core.config import (
    BASE_DIR, VAULTS_DIR, TZ, cfg, load_json, save_json, log
)
from services.storage import (
    session_get, session_save, session_delete, session_evict_expired,
    session_get_all_active,
    profile_get, profile_update, profile_increment_interaction,
    profile_add_fact, profile_add_interest, profile_set_name,
    profile_set_relationship, profile_set_ignore_status, profile_get_ignore_status,
    profile_get_context_string, profile_get_all_known_names,
    get_vault_context as storage_get_vault_context,
    learn_task_background,
)

# Keep ProfileManager for backward compatibility but delegate to storage
class ProfileManager:
    """Wrapper delegating to SQLite storage."""

    def get_profile(self, user_id: str) -> dict:
        return profile_get(user_id)

    def touch(self, user_id: str, push_name: str | None = None) -> dict:
        return profile_increment_interaction(user_id, push_name)

    def update_profile(self, user_id: str, **kwargs):
        profile_update(user_id, **kwargs)

    def add_fact(self, user_id: str, fact: str):
        profile_add_fact(user_id, fact)

    def add_interest(self, user_id: str, interest: str):
        profile_add_interest(user_id, interest)

    def set_name(self, user_id: str, name: str):
        profile_set_name(user_id, name)

    def set_relationship(self, user_id: str, relationship: str):
        profile_set_relationship(user_id, relationship)

    def set_ignore_status(self, user_id: str, ignore: bool):
        profile_set_ignore_status(user_id, ignore)

    def get_ignore_status(self, user_id: str) -> bool:
        return profile_get_ignore_status(user_id)

    def get_context_string(self, user_id: str) -> str:
        return profile_get_context_string(user_id)

    def set_preference(self, user_id: str, key: str, value):
        p = profile_get(user_id)
        prefs = p.get("preferences") or {}
        prefs[str(key)] = value
        profile_update(user_id, preferences=prefs)

    def get_preferences(self, user_id: str) -> dict:
        p = profile_get(user_id)
        return p.get("preferences", {}) or {}

    def merge_preferences(self, user_id: str, updates: dict) -> dict:
        p = profile_get(user_id)
        prefs = p.get("preferences") or {}
        for k, v in (updates or {}).items():
            prefs[str(k)] = v
        profile_update(user_id, preferences=prefs)
        return prefs

    def get_all_known_names(self) -> dict[str, str]:
        return profile_get_all_known_names()

    # Backward compat - no-op since storage is auto-persisted
    def load(self):
        pass

    def save(self):
        pass

profile_mgr = ProfileManager()

def _sanitize_session_content(role: str, content: str) -> str:
    """Drop raw tool payloads and placeholder links before storing them in session memory."""
    text = str(content or "").strip()
    if role != "assistant" or not text:
        return text
    lower = text.lower()
    if re.match(r'^\s*\{.*?"name"\s*:\s*".*?".*?"parameters"\s*:\s*\{', text, re.DOTALL):
        return "I'm not meant to send raw tool data. Tell me the exact track/version and I'll sort it cleanly."
    if "example.com" in lower or "audio-download-link" in lower or "video-download-link" in lower:
        return "I sent the wrong thing there. Tell me the exact track/version and I'll do it properly."
    if "download_video function" in lower or "download_audio function" in lower:
        return "I'm not supposed to expose the tool call. Tell me the exact track/version and I'll sort it cleanly."
    return text

class Session:
    __slots__ = ("sender", "turns", "last_active", "_skip_next_user_add")
    def __init__(self, sender: str) -> None:
        self.sender = sender
        data = session_get(sender)
        self.turns: list[dict[str, Any]] = data["turns"]
        self.last_active: float = data["last_active"]
        self._skip_next_user_add = False

    def add(self, role: str, content: str, *, message_id: str | None = None,
            ts: float | None = None) -> None:
        """Append a turn. `message_id` and `ts` are optional and used by the
        inbound-edit path to locate this turn later."""
        if role == "user" and self._skip_next_user_add:
            # The inbound-edit path already patched the previous user turn;
            # don't double-add the same content.
            self._skip_next_user_add = False
            return
        content = _sanitize_session_content(role, content)
        turn: dict[str, Any] = {"role": role, "content": content}
        if message_id:
            turn["id"] = message_id
        if ts is not None:
            turn["ts"] = ts
        self.turns.append(turn)
        self.last_active = time.time()
        max_msgs = cfg("session_max_turns") * 2
        if len(self.turns) > max_msgs:
            self.turns = self.turns[-max_msgs:]
        self._persist()

    def update_last_user(self, new_content: str) -> bool:
        """Replace the most recent user turn's content in place. Returns True
        if a turn was updated, False if there was no user turn to update.
        Sets a flag so the next `add("user", ...)` is a no-op."""
        for t in reversed(self.turns):
            if t.get("role") == "user":
                t["content"] = new_content
                t["ts"] = time.time()
                self._skip_next_user_add = True
                self._persist()
                return True
        return False

    def replace_turn(self, idx: int, new_role: str | None = None,
                     new_content: str | None = None) -> bool:
        if idx < 0 or idx >= len(self.turns):
            return False
        if new_role is not None:
            self.turns[idx]["role"] = new_role
        if new_content is not None:
            self.turns[idx]["content"] = new_content
        self._persist()
        return True

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > cfg("session_ttl")

    def messages(self) -> list[dict[str, Any]]:
        return list(self.turns)

    def _persist(self) -> None:
        session_save(self.sender, self.turns, self.last_active)

import threading

class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def load(self) -> None:
        # No-op: sessions are loaded on-demand from SQLite
        pass

    def save(self) -> None:
        # No-op: sessions are auto-persisted
        pass

    def get(self, sender: str) -> Session:
        with self._lock:
            self._evict_expired_locked()
        return Session(sender)

    def _evict_expired(self) -> None:
        with self._lock:
            self._evict_expired_locked()

    def _evict_expired_locked(self) -> None:
        deleted = session_evict_expired(cfg("session_ttl"))
        if deleted:
            log.info("[SessionStore] Evicted %d expired sessions", deleted)

    def clear(self, sender: str) -> None:
        session_delete(sender)

    @property
    def active_count(self) -> int:
        with self._lock:
            self._evict_expired_locked()
            sessions = session_get_all_active(cfg("session_ttl"))
            return len(sessions)

sessions = SessionStore()

def get_vault_context(user_phone: str) -> str:
    """Retrieve permanent personal and global vault context for system prompt."""
    return storage_get_vault_context(user_phone)


def extract_preferences_background(user_phone: str, text_sample: str, nvidia_scout_fn=None):
    """Extract likely user preferences from a short text sample using a scout LLM."""
    try:
        if not nvidia_scout_fn:
            log.debug("[Pref] no scout function provided; skipping preference extraction")
            return
        prompt = (
            "Extract simple preference key/value pairs from the following user text.\n"
            "Return JSON only, e.g. {\"music\": \"afrobeats\"}.\n\n"
            f"Text:\n{text_sample[:4000]}"
        )
        res = nvidia_scout_fn([{"role": "user", "content": prompt}], max_tokens=256)
        content = getattr(res.choices[0].message, "content", "") if res else ""
        content = content.strip()
        import json as _json
        prefs = {}
        try:
            if content.startswith('{'):
                prefs = _json.loads(content)
            else:
                import re as _re
                m = _re.search(r"\{.*\}", content, _re.DOTALL)
                if m:
                    prefs = _json.loads(m.group(0))
        except Exception:
            log.debug("[Pref] could not parse scout output: %s", content[:200])
            return

        if prefs and isinstance(prefs, dict):
            try:
                profile_mgr.merge_preferences(user_phone or "", prefs)
                log.info("[Pref] merged preferences for %s: %s", user_phone, list(prefs.keys()))
            except Exception as e:
                log.debug("[Pref] failed to merge prefs: %s", e)
    except Exception as e:
        log.debug("[Pref] extraction failed: %s", e)

