"""
services/self_correct.py
========================
Heuristic verification of bot replies. Detects obvious mismatches between the
LLM's claim and what actually happened (or didn't), so the caller can edit or
delete the offending message via the bridge.

Hard rules only in v1. Soft LLM-based consistency scoring is deferred.

Race safety: this module only ever touches `chat_reply_message_id` (the
message_id of the bot's last conversational text reply). It MUST NOT touch
`progress_message_id`, which is owned exclusively by the dispatcher / progress
session. The caller passes the message_id explicitly so the kinds can't mix.
"""

from __future__ import annotations

import re
from typing import Any

from core.config import log


# Phrases that strongly suggest the bot is acknowledging completion.
_DONE_PHRASES = (
    "got it", "got the song", "got the video", "got the image",
    "✅", "all set", "here it is", "here you go", "sent it",
    "downloaded it", "all done", "done!",
)

# Phrases that suggest the bot just enqueued work (and is waiting).
_ENQUEUED_PHRASES = (
    "on it", "on it 🎵", "on it 🎬", "on it 🎨", "on it ✨",
    "working on it", "give me a sec", "give me a minute",
    "give me a sec.",
)


def _last_tool_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent `{"role": "tool", ...}` message, or None."""
    for m in reversed(messages or []):
        if m.get("role") == "tool":
            return m
    return None


def _tool_content_is_failure(content: Any) -> bool:
    """True if the tool content (string or dict) signals a failed call."""
    if isinstance(content, dict):
        return content.get("ok") is False or "error" in content or content.get("error")
    if isinstance(content, str):
        s = content.lstrip()
        return (s.startswith('{"error"')
                or s.startswith('{"ok": false')
                or s.startswith('"Failed"')
                or s.startswith('Failed')
                or "Failed:" in s[:32])
    return False


def _last_tool_showed_task_id(messages: list[dict[str, Any]]) -> bool:
    """True if the most recent tool message contains a JSON task_id (meaning
    a background task was actually enqueued)."""
    last = _last_tool_message(messages)
    if last is None:
        return False
    c = last.get("content") or ""
    if isinstance(c, str) and '"task_id"' in c:
        return True
    return False


def _no_running_task_for(user_id: str) -> bool:
    """True if there is no currently-running task owned by this user."""
    try:
        from services.tasks import task_store
        running = task_store.list(owner_user_id=user_id, status="running", limit=1)
        return len(running) == 0
    except Exception as exc:
        log.debug("[Self-correct] task_store lookup failed: %s", exc)
        return True  # fail-open: assume no task so we still flag the mismatch


def verify_and_correct(reply: dict[str, Any], messages: list[dict[str, Any]],
                       user_id: str) -> dict[str, Any] | None:
    """Inspect the LLM reply + tool-call history. Return a correction dict
    `{"action": "edit"|"delete", "new_text": "..."}` if a mismatch is found,
    else None.

    Rules:
      REPLY_DELETE — reply starts with an "enqueued" phrase (e.g. "on it 🎵
                     (task #N)"), no tool message shows a real task_id, and
                     the user has no running task. The bot claimed to start
                     work but nothing actually started → delete.

      REPLY_EDIT   — reply contains a completion phrase ("got it", "✅",
                     "all set", …) AND the most recent tool message
                     indicates failure. The bot claimed success when the
                     tool flopped → edit to a short correction.
    """
    text = (reply.get("reply") or "").strip()
    if not text:
        return None

    lower = text.lower()

    # Rule 1: REPLY_DELETE
    task_exists = False
    m = re.search(r"#([0-9a-f]{6,})", text)
    if m:
        try:
            from services.tasks import task_store
            task_exists = task_store.get(m.group(1)) is not None
        except Exception:
            task_exists = True  # fail-closed: never delete on lookup errors
    if any(lower.startswith(p) for p in _ENQUEUED_PHRASES):
        if not task_exists and not _last_tool_showed_task_id(messages) and _no_running_task_for(user_id):
            log.info("[Self-correct] REPLY_DELETE user=%s text=%r",
                     user_id, text[:60])
            return {"action": "delete", "new_text": ""}

    # Rule 2: REPLY_EDIT
    if any(p in lower for p in _DONE_PHRASES):
        last = _last_tool_message(messages)
        if last is not None and _tool_content_is_failure(last.get("content")):
            correction = f"actually that flopped on me — {text[:80]}"
            log.info("[Self-correct] REPLY_EDIT user=%s reason=tool_failure",
                     user_id)
            return {"action": "edit", "new_text": correction}

    return None
