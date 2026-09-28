"""Access checks for creator-only operational tools."""

from __future__ import annotations

import os
import threading
import time

from core.config import cfg, log


def _digits(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def is_configured_creator(user_id: str = "", sender_jid: str = "") -> bool:
    """Authorize by configured identity, never by a mutable profile flag alone."""
    configured = _digits(cfg("owner_jid") or os.getenv("CREATOR_PHONE", ""))
    if not configured:
        return False
    return configured in {_digits(user_id), _digits(sender_jid)}


_attempt_lock = threading.Lock()
_restricted_attempts: dict[str, tuple[int, float]] = {}
_reported_attempts: dict[str, float] = {}


def record_restricted_attempt(user_id: str, sender_jid: str, feature: str) -> int:
    """Count attempts and relay repeated private-chat requests to the creator."""
    key = _digits(sender_jid or user_id) or "unknown"
    now = time.time()
    with _attempt_lock:
        count, first_seen = _restricted_attempts.get(key, (0, now))
        if now - first_seen > 24 * 60 * 60:
            count, first_seen = 0, now
        count += 1
        _restricted_attempts[key] = (count, first_seen)
        should_report = (
            count >= 3
            and not sender_jid.endswith("@g.us")
            and now - _reported_attempts.get(key, 0) >= 24 * 60 * 60
        )
        if should_report:
            _reported_attempts[key] = now

    if should_report:
        try:
            from services.contact_relay import execute_relay_request

            execute_relay_request(
                key,
                sender_jid,
                f"Repeated requests to use the creator-only {feature} feature (3+ attempts).",
                "Restricted feature request",
            )
        except Exception as exc:
            log.warning("[AccessControl] Could not report repeated restricted requests: %s", exc)
    return count