"""
services/memory.py
==================
User profiling, session store, permanent personal vault, and global knowledge base.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any

from core.config import (
    BASE_DIR, SESSIONS_FILE, VAULTS_DIR, TZ, cfg, load_json, save_json, log
)
from profiles import ProfileManager

profile_mgr = ProfileManager()

def _sanitize_session_content(role: str, content: str) -> str:
    """Drop raw tool payloads and placeholder links before storing them in session memory."""
    text = str(content or "").strip()
    if role != "assistant" or not text:
        return text
    lower = text.lower()
    if re.match(r'^\s*\{.*?"name"\s*:\s*".*?".*?"parameters"\s*:\s*\{', text, re.DOTALL):
        return "I’m not meant to send raw tool data. Tell me the exact track/version and I’ll sort it cleanly."
    if "example.com" in lower or "audio-download-link" in lower or "video-download-link" in lower:
        return "I sent the wrong thing there. Tell me the exact track/version and I’ll do it properly."
    if "download_video function" in lower or "download_audio function" in lower:
        return "I’m not supposed to expose the tool call. Tell me the exact track/version and I’ll sort it cleanly."
    return text

class Session:
    __slots__ = ("turns", "last_active", "_on_update", "_skip_next_user_add")
    def __init__(self, on_update=None) -> None:
        self.turns: list[dict[str, Any]] = []
        self.last_active: float = time.time()
        self._on_update = on_update
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
        if self._on_update:
            self._on_update()

    def update_last_user(self, new_content: str) -> bool:
        """Replace the most recent user turn's content in place. Returns True
        if a turn was updated, False if there was no user turn to update.
        Sets a flag so the next `add("user", ...)` is a no-op."""
        for t in reversed(self.turns):
            if t.get("role") == "user":
                t["content"] = new_content
                t["ts"] = time.time()
                self._skip_next_user_add = True
                if self._on_update:
                    self._on_update()
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
        if self._on_update:
            self._on_update()
        return True

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > cfg("session_ttl")

    def messages(self) -> list[dict[str, Any]]:
        return list(self.turns)

import threading

class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, Session] = {}
        self.load()

    def load(self) -> None:
        data = load_json(SESSIONS_FILE, {})
        with getattr(self, "_lock", threading.Lock()):
            for sender, turns in data.items():
                s = Session(on_update=self.save)
                cleaned_turns = []
                for turn in turns.get("turns", []):
                    if not isinstance(turn, dict):
                        continue
                    clean = dict(turn)
                    clean["content"] = _sanitize_session_content(clean.get("role", "user"), clean.get("content", ""))
                    cleaned_turns.append(clean)
                s.turns = cleaned_turns
                s.last_active = turns.get("last_active", time.time())
                self._store[sender] = s

    def save(self) -> None:
        with self._lock:
            data = {sender: {"turns": s.turns, "last_active": s.last_active} for sender, s in self._store.items()}
        save_json(SESSIONS_FILE, data)

    def get(self, sender: str) -> Session:
        with self._lock:
            self._evict_expired_locked()
            if sender not in self._store:
                self._store[sender] = Session(on_update=self.save)
            return self._store[sender]

    def _evict_expired(self) -> None:
        with self._lock:
            self._evict_expired_locked()

    def _evict_expired_locked(self) -> None:
        expired = [k for k, s in self._store.items() if s.is_expired()]
        if expired:
            for k in expired:
                del self._store[k]

    def clear(self, sender: str) -> None:
        with self._lock:
            if sender in self._store:
                del self._store[sender]
        self.save()

    @property
    def active_count(self) -> int:
        with self._lock:
            self._evict_expired_locked()
            return len(self._store)

sessions = SessionStore()

def get_vault_context(user_phone: str) -> str:
    """Retrieve permanent personal and global vault context for system prompt."""
    vault_str = ""
    personal_vault = os.path.join(VAULTS_DIR, f"vault_{user_phone}.txt")
    if os.path.exists(personal_vault):
        try:
            with open(personal_vault, "r", encoding="utf-8") as f:
                vdata = f.read()
            if len(vdata) > 50000:
                vdata = "[...older facts truncated...]\n" + vdata[-50000:]
            if vdata.strip():
                vault_str += f"\n\n--- PERMANENT MEMORY VAULT ---\nLearned facts about this user:\n{vdata}\n------------------------------\n"
        except Exception as e:
            log.error("[Vault] Error reading personal vault for %s: %s", user_phone, e)

    global_vault = os.path.join(VAULTS_DIR, "global_vault.txt")
    if os.path.exists(global_vault):
        try:
            with open(global_vault, "r", encoding="utf-8") as f:
                gdata = f.read()
            if len(gdata) > 30000:
                gdata = "[...older global facts truncated...]\n" + gdata[-30000:]
            if gdata.strip():
                vault_str += f"\n\n--- GLOBAL KNOWLEDGE BASE ---\nShared knowledge:\n{gdata}\n------------------------------\n"
        except Exception as e:
            log.error("[Vault] Error reading global vault: %s", e)

    return vault_str

def learn_task_background(user_phone: str, text_to_learn: str, doc_name: str | None = None, nvidia_scout_fn=None):
    """Background task to summarize and save facts to the permanent memory vault."""
    try:
        source_label = f"Document '{doc_name}'" if doc_name else "Text input"
        if not nvidia_scout_fn:
            log.warning("[Learn] No scout LLM available for learning task.")
            return

        verify_prompt = (
            f"Analyze this content for factual validity. Reply ONLY 'FAKE' if it is "
            f"gibberish, clearly fabricated nonsense, or contradicts well-known facts. "
            f"Personal statements, preferences, claims about the user or a chatbot, and "
            f"unverifiable but plausible statements are VALID.\n\nContent:\n\n{text_to_learn[:3000]}\n\n"
            f"Reply ONLY 'FAKE' or 'VALID'."
        )
        verify_res = nvidia_scout_fn([{"role": "user", "content": verify_prompt}], max_tokens=10)
        status = getattr(verify_res.choices[0].message, "content", "").strip().upper()
        if "FAKE" in status:
            log.warning("[Learn] Rejected invalid document from %s", user_phone)
            return

        prompt = (
            f"Analyze {source_label}.\nExtract core facts/concepts into dense bulleted list without filler:\n\n{text_to_learn}"
        )
        response = nvidia_scout_fn([{"role": "user", "content": prompt}], max_tokens=1024)
        facts = getattr(response.choices[0].message, "content", "").strip()

        categorize_prompt = (
            f"Categorize this info:\n'{facts[:200]}'\n"
            f"If about a specific person, reply 'PERSONAL'. If general/technical, reply 'GLOBAL'. Reply one word."
        )
        cat_res = nvidia_scout_fn([{"role": "user", "content": categorize_prompt}], max_tokens=10)
        category = getattr(cat_res.choices[0].message, "content", "").strip().upper()

        if "GLOBAL" in category:
            vault_path = os.path.join(VAULTS_DIR, "global_vault.txt")
        else:
            vault_path = os.path.join(VAULTS_DIR, f"vault_{user_phone}.txt")

        timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        with open(vault_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n--- Learned on {timestamp} (Source: {source_label}) ---\n{facts}")

        log.info("[Learn] Learned facts saved to %s", vault_path)
        # Also append learned facts to vectors.json with per-user metadata so
        # RAG can surface user-specific chunks later.
        try:
            # Use rag.append_text_to_vectors to split facts into chunks and add metadata
            from services.rag import append_text_to_vectors
            append_text_to_vectors(facts, owner=user_phone or "", group="", source=(doc_name or "learned"))
        except Exception as e:
            log.debug("[Learn] could not append to vectors.json via rag: %s", e)
    except Exception as e:
        log.error("[Learn] Background learn task failed: %s", e)


def extract_preferences_background(user_phone: str, text_sample: str, nvidia_scout_fn=None):
    """Extract likely user preferences from a short text sample using a scout LLM.

    This is intentionally lightweight and tolerant of failure. The extracted
    preferences are merged into the user's profile preferences.
    """
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
        # best-effort parse
        content = getattr(res.choices[0].message, "content", "") if res else ""
        content = content.strip()
        import json as _json
        prefs = {}
        try:
            # allow the model to return either bare JSON or a line with JSON
            if content.startswith('{'):
                prefs = _json.loads(content)
            else:
                # find first { ... }
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

