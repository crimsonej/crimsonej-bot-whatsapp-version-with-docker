"""
services/automation.py
======================
Automated background tasks for the bot.
Runs scheduled checks, auto-moderation, auto-reactions, etc.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import cfg, log
from services.storage import get_conn, profile_get, profile_update
from services.bridge_api import bridge_group_admin_action, bridge_send_reaction, bridge_send
from services.group_intel import is_group_admin, get_group_context, update_group_context
from services.sandbox import execute_in_sandbox
from services.metrics import record_error


# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class AutomationEngine:
    """Background automation engine for scheduled and reactive tasks."""
    
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._interval = int(cfg("automation_interval_sec") or 30)  # seconds between cycles
        
        # Auto-moderation settings
        self._spam_threshold = int(cfg("auto_mod_spam_threshold") or 10)  # msgs per minute
        self._spam_window = int(cfg("auto_mod_spam_window_sec") or 60)  # seconds
        self._toxicity_threshold = float(cfg("auto_mod_toxicity_threshold") or 0.8)
        
        # Auto-reaction rules: keyword -> emoji
        self._reaction_rules = {
            "😂": ["lol", "lmao", "haha", "funny", "joke", "😂", "🤣"],
            "👍": ["good", "great", "nice", "awesome", "cool", "thanks", "thank you"],
            "❤️": ["love", "❤️", "heart", "cute", "sweet"],
            "🔥": ["fire", "lit", "🔥", "amazing", "incredible"],
            "🤔": ["hmm", "think", "wonder", "confused", "confusing"],
            "😢": ["sad", "sorry", "rip", "unfortunate"],
            "🎉": ["congrats", "congratulations", "yay", "woohoo", "party"],
        }
        
        # Spam tracking: group_jid -> [(timestamp, user_jid), ...]
        self._message_history: Dict[str, List[tuple]] = {}
        
        # Scheduled tasks from database
        self._scheduled_tasks: List[Dict] = []
        
    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AutomationEngine", daemon=True)
        self._thread.start()
        log.info("[Automation] Engine started")
        
    def stop(self):
        if not self._running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        log.info("[Automation] Engine stopped")
    
    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                cycle_start = time.time()
                
                # Run all automation tasks
                self._process_scheduled_messages()
                self._check_spam_and_moderate()
                self._check_auto_reactions()
                self._cleanup_old_data()
                
                # Sleep until next cycle
                elapsed = time.time() - cycle_start
                sleep_time = max(0, self._interval - elapsed)
                self._stop_event.wait(timeout=sleep_time)
                
            except Exception as e:
                log.error("[Automation] Loop error: %s", e)
                record_error("automation", "loop_error")
                self._stop_event.wait(timeout=5)
    
    # ─── Scheduled Messages ─────────────────────────────────────────────────
    
    def _process_scheduled_messages(self):
        """Process due scheduled messages from database."""
        try:
            now = time.time()
            conn = get_conn()
            
            # Get due scheduled messages
            cur = conn.execute("""
                SELECT id, jid, text, media_path, media_type, filename, scheduled_at
                FROM scheduled_messages 
                WHERE scheduled_at <= ? AND status = 'pending'
            """, (now,))
            
            for row in cur.fetchall():
                msg_id, jid, text, media_path, media_type, filename, scheduled_at = row
                
                if media_type == "system_action" or media_path == "ACTION:UNLOCK_GROUP":
                    from services.bridge_api import bridge_group_setting
                    result = bridge_group_setting(jid, "not_announcement")
                    if result.get("ok"):
                        conn.execute("UPDATE scheduled_messages SET status = 'sent', sent_at = ? WHERE id = ?", (time.time(), msg_id))
                        log.info("[Automation] System action (unlock group) executed for %s", jid)
                    else:
                        conn.execute("UPDATE scheduled_messages SET status = 'failed', error = ? WHERE id = ?", (result.get("error", "unknown"), msg_id))
                        log.warning("[Automation] System action (unlock group) failed for %s: %s", jid, result.get("error"))
                else:
                    # Send the normal message
                    from services.bridge_api import bridge_send
                    result = bridge_send(jid, text, media_path=media_path, media_type=media_type, filename=filename)
                    
                    if result.get("ok"):
                        # Mark as sent
                        conn.execute("UPDATE scheduled_messages SET status = 'sent', sent_at = ? WHERE id = ?", (time.time(), msg_id))
                        log.info("[Automation] Sent scheduled message %s to %s", msg_id, jid)
                    else:
                        # Mark as failed, allow retry
                        conn.execute("UPDATE scheduled_messages SET status = 'failed', error = ? WHERE id = ?", (result.get("error", "unknown"), msg_id))
                        log.warning("[Automation] Failed to send scheduled message %s: %s", msg_id, result.get("error"))
            
            conn.commit()
        except Exception as e:
            log.error("[Automation] Scheduled messages error: %s", e)
            record_error("automation", "scheduled_messages")
    
    def schedule_message(self, jid: str, text: str, schedule_at: float, media_path: str = "", media_type: str = "", filename: str = "") -> str:
        """Schedule a message for future delivery. Returns task_id."""
        try:
            conn = get_conn()
            cur = conn.execute("""
                INSERT INTO scheduled_messages (jid, text, media_path, media_type, filename, scheduled_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (jid, text, media_path, media_type, filename, schedule_at, time.time()))
            conn.commit()
            return str(cur.lastrowid)
        except Exception as e:
            log.error("[Automation] Failed to schedule message: %s", e)
            return ""
    
    def cancel_scheduled(self, task_id: str) -> bool:
        """Cancel a scheduled message."""
        try:
            conn = get_conn()
            cur = conn.execute("UPDATE scheduled_messages SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (task_id,))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False
    
    # ─── Auto-Reactions ─────────────────────────────────────────────────────
    
    def _check_auto_reactions(self):
        """Check recent messages and add auto-reactions based on rules."""
        # This would need a message store - for now we react to messages
        # as they come in via the reaction handler in bot.py
        pass
    
    def get_reaction_for_text(self, text: str) -> str | None:
        """Determine appropriate reaction emoji for text."""
        text_lower = text.lower()
        for emoji, keywords in self._reaction_rules.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return emoji
        return None
    
    def auto_react_to_message(self, jid: str, message_id: str, text: str):
        """Auto-react to a message based on content."""
        emoji = self.get_reaction_for_text(text)
        if emoji:
            from services.bridge_api import bridge_send_reaction
            result = bridge_send_reaction(jid, message_id, emoji)
            if result.get("ok"):
                log.info("[Automation] Auto-reacted with %s to message %s", emoji, message_id)
            else:
                log.warning("[Automation] Failed to auto-react: %s", result.get("error"))
    
    # ─── Auto-Moderation ────────────────────────────────────────────────────
    
    def _check_spam_and_moderate(self):
        """Check for spam and apply auto-moderation."""
        now = time.time()
        cutoff = now - self._spam_window
        
        for group_jid, messages in list(self._message_history.items()):
            # Filter recent messages
            recent = [(ts, uid) for ts, uid in messages if ts > cutoff]
            self._message_history[group_jid] = recent
            
            # Count per user
            user_counts: Dict[str, int] = {}
            for _, uid in recent:
                user_counts[uid] = user_counts.get(uid, 0) + 1
            
            # Check for spam
            for uid, count in user_counts.items():
                if count >= self._spam_threshold:
                    self._handle_spam(group_jid, uid, count)
    
    def record_message(self, group_jid: str, user_jid: str):
        """Record a message for spam tracking."""
        now = time.time()
        if group_jid not in self._message_history:
            self._message_history[group_jid] = []
        self._message_history[group_jid].append((now, user_jid))
    
    def _handle_spam(self, group_jid: str, user_jid: str, count: int):
        """Handle detected spam - mute user temporarily."""
        try:
            # Check if already muted recently
            group_ctx = get_group_context(group_jid)
            muted_users = group_ctx.get("muted_users", {})
            user_key = user_jid.replace("@", "").replace(".", "")
            
            if user_key in muted_users:
                return  # Already handled
            
            # Mute for 5 minutes
            mute_duration = 300  # 5 minutes
            mute_until = time.time() + mute_duration
            
            muted_users[user_key] = {
                "muted_until": mute_until,
                "reason": f"Auto-mute: spam detected ({count} msgs in {self._spam_window}s)",
                "muted_by": "auto_mod"
            }
            
            # Update group context
            update_group_context(group_jid, muted_users=muted_users)
            
            # Apply mute via bridge
            from services.bridge_api import bridge_group_admin_action
            result = bridge_group_admin_action(group_jid, "mute", user_jid)
            
            if result.get("ok"):
                log.info("[AutoMod] Muted %s in %s for spam (%d msgs)", user_jid, group_jid, count)
            else:
                log.warning("[AutoMod] Failed to mute %s: %s", user_jid, result.get("error"))
                
            # Auto-mute enforced silently (no bot chat message sent)
            log.info("[AutoMod] Silently auto-muted @%s in %s for spam (%d msgs)", user_jid.split('@')[0], group_jid, count)

            
        except Exception as e:
            log.error("[AutoMod] Error handling spam: %s", e)
            record_error("automation", "spam_handling")
    
    # ─── Cleanup ────────────────────────────────────────────────────────────
    
    def _cleanup_old_data(self):
        """Clean up old tracking data."""
        now = time.time()
        cutoff = now - 3600  # 1 hour
        
        # Clean message history
        for group_jid in list(self._message_history.keys()):
            recent = [(ts, uid) for ts, uid in self._message_history[group_jid] if ts > cutoff]
            if recent:
                self._message_history[group_jid] = recent
            else:
                del self._message_history[group_jid]
        
        # Clean expired mutes from group contexts
        # This would need a periodic scan of group contexts
    # ─── Group Moderation Rules Engine ─────────────────────────────────────
    
    def set_group_rule(self, group_jid: str, rule_name: str, enabled: bool) -> bool:
        """
        Configure group moderation rule.
        Supported rule_names: 'no_stickers', 'no_links', 'no_images', 'no_media', 'no_docs'.
        """
        valid_rules = {"no_stickers", "no_links", "no_images", "no_media", "no_docs"}
        if rule_name not in valid_rules:
            log.warning("[AutoMod] Invalid rule name: %s", rule_name)
            return False
            
        group_ctx = get_group_context(group_jid)
        rules = group_ctx.get("moderation_rules", {})
        rules[rule_name] = enabled
        update_group_context(group_jid, moderation_rules=rules)
        log.info("[AutoMod] Updated rule %s=%s for group %s", rule_name, enabled, group_jid)
        return True

    def get_group_rules(self, group_jid: str) -> Dict[str, bool]:
        """Get active moderation rules for a group."""
        group_ctx = get_group_context(group_jid)
        return group_ctx.get("moderation_rules", {})

    def evaluate_and_enforce_group_rules(
        self,
        group_jid: str,
        user_jid: str,
        message_id: str,
        text: str = "",
        is_sticker: bool = False,
        is_image: bool = False,
        is_doc: bool = False,
        is_media: bool = False,
    ) -> bool:
        """
        Evaluate incoming group message against active rules.
        If a rule is broken, immediately delete message for everyone via bridge.
        Returns True if message was deleted (violation), False otherwise.
        """
        rules = self.get_group_rules(group_jid)
        if not rules:
            return False

        import re
        has_link = bool(re.search(r"https?://|www\.[a-z0-9]+\.[a-z]", text, re.IGNORECASE))
        violation = None

        if is_sticker and rules.get("no_stickers"):
            violation = "no_stickers"
        elif has_link and rules.get("no_links"):
            violation = "no_links"
        elif is_image and rules.get("no_images"):
            violation = "no_images"
        elif is_doc and rules.get("no_docs"):
            violation = "no_docs"
        elif is_media and rules.get("no_media"):
            violation = "no_media"

        if violation:
            from services.bridge_api import bridge_delete
            res = bridge_delete(group_jid, message_id, from_me=False, participant=user_jid)
            log.info("[AutoMod] Deleted message %s from %s in %s due to rule %s: %s",
                     message_id, user_jid, group_jid, violation, res)
            return True

        return False

    # ─── Timed Group Lock / Unlock ──────────────────────────────────────────

    def lock_groups_timed(self, group_jids: List[str], duration_seconds: int = 3600) -> Dict[str, Any]:
        """
        Lock a list of groups (announcement mode: admins only), and schedule an automatic
        unlock timer after duration_seconds.
        """
        from services.bridge_api import bridge_group_setting
        lock_res = bridge_group_setting(group_jids, "announcement")
        
        revert_at = time.time() + duration_seconds
        conn = get_conn()
        for g_jid in group_jids:
            conn.execute("""
                INSERT INTO scheduled_messages (jid, text, media_path, media_type, filename, scheduled_at, status, created_at)
                VALUES (?, ?, 'ACTION:UNLOCK_GROUP', 'system_action', '', ?, 'pending', ?)
            """, (g_jid, "UNLOCK_GROUP", revert_at, time.time()))
        conn.commit()

        log.info("[AutoMod] Locked %d groups for %ds (revert scheduled)", len(group_jids), duration_seconds)
        return {"ok": True, "locked_groups": group_jids, "duration_sec": duration_seconds, "bridge": lock_res}

    # ─── Conditional Document Relay Engine ──────────────────────────────────

    def register_conditional_doc_relay(self, expected_from_phone_or_jid: str, deliver_to_phone_or_jid: str, deliver_at_ts: float) -> str:
        """
        Register expectation: when user `expected_from` sends a PDF/doc, store it
        and deliver to `deliver_to` at timestamp `deliver_at_ts`.
        """
        conn = get_conn()
        relay_id = f"doc_relay_{int(time.time() * 1000)}"
        conn.execute("""
            INSERT INTO tasks (id, kind, name, action, schedule, status, owner_user_id, owner_jid, notify_on, created_at)
            VALUES (?, 'conditional', ?, ?, ?, 'pending', 'system', ?, 'none', ?)
        """, (
            relay_id,
            f"Doc Relay from {expected_from_phone_or_jid}",
            json.dumps({
                "expected_from": expected_from_phone_or_jid,
                "deliver_to": deliver_to_phone_or_jid,
                "deliver_at": deliver_at_ts,
                "captured_doc_path": None,
                "captured_doc_name": None,
            }),
            json.dumps({"type": "timestamp", "deliver_at": deliver_at_ts}),
            deliver_to_phone_or_jid,
            time.time()
        ))
        conn.commit()
        log.info("[DocRelay] Registered conditional relay %s for %s -> %s at %s",
                 relay_id, expected_from_phone_or_jid, deliver_to_phone_or_jid, deliver_at_ts)
        return relay_id

    def check_and_process_incoming_doc(self, sender_jid_or_phone: str, doc_path: str, doc_name: str) -> bool:
        """
        Check if an incoming document matches any pending conditional doc relay.
        If matched, attach the doc to the task so it gets sent at the scheduled delivery time.
        """
        try:
            conn = get_conn()
            cur = conn.execute("SELECT id, action FROM tasks WHERE kind = 'conditional' AND status = 'pending'")
            rows = cur.fetchall()

            for task_id, action_json in rows:
                try:
                    act = json.loads(action_json or "{}")
                    expected = str(act.get("expected_from") or "").lower().split("@")[0]
                    sender_clean = sender_jid_or_phone.lower().split("@")[0]

                    if expected and (expected in sender_clean or sender_clean in expected):
                        act["captured_doc_path"] = doc_path
                        act["captured_doc_name"] = doc_name
                        conn.execute("UPDATE tasks SET action = ? WHERE id = ?", (json.dumps(act), task_id))
                        conn.commit()
                        log.info("[DocRelay] Captured doc %s from %s for task %s", doc_name, sender_clean, task_id)
                        return True
                except Exception:
                    pass
        except Exception as e:
            log.warning("[DocRelay] Error checking doc relay: %s", e)
        return False

    # ─── Public API ─────────────────────────────────────────────────────────
    
    def add_reaction_rule(self, emoji: str, keywords: List[str]):
        """Add a custom auto-reaction rule."""
        self._reaction_rules[emoji] = keywords
    
    def remove_reaction_rule(self, emoji: str):
        """Remove a reaction rule."""
        self._reaction_rules.pop(emoji, None)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get automation engine statistics."""
        return {
            "running": self._running,
            "interval_sec": self._interval,
            "spam_threshold": self._spam_threshold,
            "reaction_rules": len(self._reaction_rules),
            "groups_tracked": len(self._message_history),
        }


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA FOR SCHEDULED MESSAGES
# ─────────────────────────────────────────────────────────────────────────────

# Add to storage.py init_db():
# SCHEDULED_MESSAGES_TABLE = """
#     CREATE TABLE IF NOT EXISTS scheduled_messages (
#         id INTEGER PRIMARY KEY AUTOINCREMENT,
#         jid TEXT NOT NULL,
#         text TEXT NOT NULL,
#         media_path TEXT,
#         media_type TEXT,
#         filename TEXT,
#         scheduled_at REAL NOT NULL,
#         status TEXT DEFAULT 'pending',  -- pending, sent, failed, cancelled
#         error TEXT,
#         created_at REAL NOT NULL,
#         sent_at REAL
#     )
# """
# CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_messages(scheduled_at, status)


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL INSTANCE
# ─────────────────────────────────────────────────────────────────────────────

_automation_engine: AutomationEngine | None = None


def get_automation_engine() -> AutomationEngine:
    global _automation_engine
    if _automation_engine is None:
        _automation_engine = AutomationEngine()
    return _automation_engine


def start_automation():
    """Start the automation engine."""
    engine = get_automation_engine()
    engine.start()


def stop_automation():
    """Stop the automation engine."""
    global _automation_engine
    if _automation_engine:
        _automation_engine.stop()
        _automation_engine = None


def get_automation_stats() -> Dict[str, Any]:
    engine = get_automation_engine()
    return engine.get_stats()