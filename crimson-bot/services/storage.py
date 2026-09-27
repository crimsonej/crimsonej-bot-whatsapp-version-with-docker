"""
services/storage.py
===================
SQLite-backed persistent storage for Crimsonej.
Replaces: sessions.json, user_profiles.json, vaults/, vectors.json, cache.json

Uses WAL mode for concurrent readers/writers. Single file: crimson.db
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from core.config import BASE_DIR, TZ, cfg, log

DB_PATH = os.path.join(BASE_DIR, "crimson.db")
_SCHEMA_VERSION = 6

_thread_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def get_conn() -> sqlite3.Connection:
    """Get thread-local SQLite connection with WAL mode."""
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        _thread_local.conn = conn
    return _thread_local.conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Context manager for atomic transactions."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Initialize database schema. Idempotent."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        cur = conn.cursor()

        # Schema version tracking
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        cur.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (0)")
        cur.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row else 0

        if current_version < 1:
            # Sessions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    sender TEXT PRIMARY KEY,
                    turns TEXT NOT NULL,
                    last_active REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active)")

            # User profiles table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    nicknames TEXT,           -- JSON array
                    facts TEXT,               -- JSON array
                    interests TEXT,           -- JSON array
                    relationship TEXT,
                    preferences TEXT,         -- JSON object
                    relay_requests TEXT,      -- JSON array (contact relay requests)
                    escalations TEXT,         -- JSON array (bot escalations)
                    interaction_count INTEGER DEFAULT 0,
                    last_seen TEXT,
                    first_seen TEXT,
                    is_creator INTEGER DEFAULT 0,
                    ignore_status INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL
                )
            """)

            # Vaults table (personal + global)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vaults (
                    vault_key TEXT PRIMARY KEY,  -- 'global' or 'user:<phone>'
                    content TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # Vectors table (RAG chunks)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    owner TEXT DEFAULT '',
                    group_jid TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vectors_owner ON vectors(owner)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vectors_group ON vectors(group_jid)")

            # Dynamic tools table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dynamic_tools (
                    name TEXT PRIMARY KEY,
                    schema TEXT NOT NULL,           -- JSON schema
                    source TEXT NOT NULL,           -- Python source code
                    metadata TEXT,                  -- JSON metadata
                    created_at REAL NOT NULL
                )
            """)

            # Reactions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reactions (
                    message_id TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    reactor_jid TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (message_id, emoji, reactor_jid)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id)")

            # Cache table (key-value with TTL)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")

            if current_version < 1:
                cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")

        if current_version < 2:
            # Add FTS5 virtual table for vector search (if available)
            try:
                cur.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vectors_fts USING fts5(
                        text, owner, group_jid, source,
                        content='vectors', content_rowid='id'
                    )
                """)
                # Triggers to keep FTS in sync
                cur.execute("""
                    CREATE TRIGGER IF NOT EXISTS vectors_ai AFTER INSERT ON vectors BEGIN
                        INSERT INTO vectors_fts(rowid, text, owner, group_jid, source)
                        VALUES (new.id, new.text, new.owner, new.group_jid, new.source);
                    END
                """)
                cur.execute("""
                    CREATE TRIGGER IF NOT EXISTS vectors_ad AFTER DELETE ON vectors BEGIN
                        INSERT INTO vectors_fts(vectors_fts, rowid, text, owner, group_jid, source)
                        VALUES ('delete', old.id, old.text, old.owner, old.group_jid, old.source);
                    END
                """)
                cur.execute("""
                    CREATE TRIGGER IF NOT EXISTS vectors_au AFTER UPDATE ON vectors BEGIN
                        INSERT INTO vectors_fts(vectors_fts, rowid, text, owner, group_jid, source)
                        VALUES ('delete', old.id, old.text, old.owner, old.group_jid, old.source);
                        INSERT INTO vectors_fts(rowid, text, owner, group_jid, source)
                        VALUES (new.id, new.text, new.owner, new.group_jid, new.source);
                    END
                """)
            except Exception as e:
                log.warning("[Storage] FTS5 not available, falling back to LIKE search: %s", e)

            if current_version < 2:
                cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")

        if current_version < 3:
            # Add message_id column to sessions for edit tracking
            # We'll store it in the turns JSON, no schema change needed
            # But add a separate table for message_id mapping
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sent_messages (
                    sender TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    sent_text TEXT,
                    sent_at REAL NOT NULL
                )
            """)
            cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (3)")

        if current_version < 4:
            # Dynamic tools table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dynamic_tools (
                    name TEXT PRIMARY KEY,
                    schema TEXT NOT NULL,           -- JSON schema
                    source TEXT NOT NULL,           -- Python source code
                    metadata TEXT,                  -- JSON metadata
                    created_at REAL NOT NULL
                )
            """)
            cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (4)")

        if current_version < 5:
            # Reactions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reactions (
                    message_id TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    reactor_jid TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (message_id, emoji, reactor_jid)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id)")
            cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (5)")

        if current_version < 6:
            # Scheduled messages table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    jid TEXT NOT NULL,
                    text TEXT NOT NULL,
                    media_path TEXT,
                    media_type TEXT,
                    filename TEXT,
                    scheduled_at REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    error TEXT,
                    created_at REAL NOT NULL,
                    sent_at REAL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_messages(scheduled_at, status)")
            cur.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (6)")

        conn.commit()
        _initialized = True
        log.info("[Storage] Database initialized at %s (v%d)", DB_PATH, _SCHEMA_VERSION)


# ─────────────────────────────────────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────────────────────────────────────

def session_get(sender: str) -> dict:
    """Get session data, creating empty if not exists."""
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT turns, last_active FROM sessions WHERE sender = ?", (sender,))
        row = cur.fetchone()
        if row:
            return {"turns": json.loads(row[0]), "last_active": row[1]}
        return {"turns": [], "last_active": time.time()}


def session_save(sender: str, turns: list[dict], last_active: float) -> None:
    """Save session (upsert)."""
    init_db()
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (sender, turns, last_active, updated_at) VALUES (?, ?, ?, ?)",
            (sender, json.dumps(turns, ensure_ascii=False), last_active, time.time()),
        )


def session_delete(sender: str) -> None:
    """Delete a session."""
    init_db()
    with transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE sender = ?", (sender,))


def session_evict_expired(ttl_seconds: int) -> int:
    """Remove expired sessions. Returns count deleted."""
    init_db()
    cutoff = time.time() - ttl_seconds
    with transaction() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
        return cur.rowcount


def session_get_all_active(ttl_seconds: int) -> list[tuple[str, dict]]:
    """Get all non-expired sessions for memory stats."""
    init_db()
    cutoff = time.time() - ttl_seconds
    with transaction() as conn:
        cur = conn.execute("SELECT sender, turns, last_active FROM sessions WHERE last_active >= ?", (cutoff,))
        return [(row[0], {"turns": json.loads(row[1]), "last_active": row[2]}) for row in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# PROFILES
# ─────────────────────────────────────────────────────────────────────────────

def profile_get(user_id: str) -> dict:
    """Get or create profile."""
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return _row_to_profile(row)
        # Create default
        now = datetime.now().isoformat()
        default = {
            "user_id": user_id,
            "name": None,
            "nicknames": [],
            "facts": [],
            "interests": [],
            "relationship": None,
            "preferences": {},
            "relay_requests": [],
            "interaction_count": 0,
            "last_seen": now,
            "first_seen": now,
            "is_creator": 0,
            "ignore_status": 0,
        }
        _profile_insert(conn, default)
        return default


def _row_to_profile(row: sqlite3.Row) -> dict:
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "nicknames": json.loads(row["nicknames"] or "[]"),
        "facts": json.loads(row["facts"] or "[]"),
        "interests": json.loads(row["interests"] or "[]"),
        "relationship": row["relationship"],
        "preferences": json.loads(row["preferences"] or "{}"),
        "relay_requests": json.loads(row["relay_requests"] or "[]"),
        "escalations": json.loads(row["escalations"] or "[]"),
        "interaction_count": row["interaction_count"],
        "last_seen": row["last_seen"],
        "first_seen": row["first_seen"],
        "is_creator": bool(row["is_creator"]),
        "ignore_status": bool(row["ignore_status"]),
    }


def _profile_insert(conn: sqlite3.Connection, p: dict) -> None:
    conn.execute(
        """INSERT INTO profiles (user_id, name, nicknames, facts, interests, relationship,
           preferences, relay_requests, escalations, interaction_count, last_seen, first_seen, is_creator, ignore_status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            p["user_id"],
            p["name"],
            json.dumps(p["nicknames"]),
            json.dumps(p["facts"]),
            json.dumps(p["interests"]),
            p["relationship"],
            json.dumps(p["preferences"]),
            json.dumps(p.get("relay_requests", [])),
            json.dumps(p.get("escalations", [])),
            p["interaction_count"],
            p["last_seen"],
            p["first_seen"],
            int(p.get("is_creator", 0)),
            int(p.get("ignore_status", 0)),
            time.time(),
        ),
    )


def profile_update(user_id: str, **fields) -> dict:
    """Update profile fields atomically."""
    init_db()
    with transaction() as conn:
        p = profile_get(user_id)
        p.update(fields)
        p["last_seen"] = datetime.now().isoformat()
        conn.execute(
            """UPDATE profiles SET name=?, nicknames=?, facts=?, interests=?, relationship=?,
               preferences=?, relay_requests=?, escalations=?, interaction_count=?, last_seen=?, first_seen=?, is_creator=?,
               ignore_status=?, updated_at=? WHERE user_id=?""",
            (
                p["name"],
                json.dumps(p["nicknames"]),
                json.dumps(p["facts"]),
                json.dumps(p["interests"]),
                p["relationship"],
                json.dumps(p["preferences"]),
                json.dumps(p.get("relay_requests", [])),
                json.dumps(p.get("escalations", [])),
                p["interaction_count"],
                p["last_seen"],
                p["first_seen"],
                int(p.get("is_creator", 0)),
                int(p.get("ignore_status", 0)),
                time.time(),
                user_id,
            ),
        )
        return p


def profile_increment_interaction(user_id: str, push_name: Optional[str] = None) -> dict:
    """Record interaction, auto-learn name, periodic save."""
    p = profile_get(user_id)
    p["interaction_count"] = p.get("interaction_count", 0) + 1
    p["last_seen"] = datetime.now().isoformat()

    if push_name and push_name.strip():
        clean_name = push_name.strip()
        if not p.get("name"):
            p["name"] = clean_name
            log.info("[Profile] Learned name for %s: %s", user_id, clean_name)
        elif p.get("name") != clean_name:
            nicks = p.get("nicknames", [])
            if p["name"] not in nicks:
                nicks.append(p["name"])
            p["nicknames"] = nicks[-5:]
            p["name"] = clean_name
            log.info("[Profile] Updated name for %s: %s", user_id, clean_name)

    if p["interaction_count"] % 5 == 0:
        profile_update(user_id, **p)
    return p


def profile_add_fact(user_id: str, fact: str) -> None:
    p = profile_get(user_id)
    facts = p.get("facts", [])
    fact_lower = fact.lower().strip()
    if not any(fact_lower in f.lower() or f.lower() in fact_lower for f in facts):
        facts.append(fact.strip())
        if len(facts) > 50:
            facts = facts[-50:]
        profile_update(user_id, facts=facts)


def profile_add_interest(user_id: str, interest: str) -> None:
    p = profile_get(user_id)
    interests = p.get("interests", [])
    interest_lower = interest.lower().strip()
    if interest_lower not in [i.lower() for i in interests]:
        interests.append(interest.strip())
        if len(interests) > 20:
            interests = interests[-20:]
        profile_update(user_id, interests=interests)


def profile_set_name(user_id: str, name: str) -> None:
    profile_update(user_id, name=name)


def profile_set_relationship(user_id: str, relationship: str) -> None:
    profile_update(user_id, relationship=relationship)


def profile_set_ignore_status(user_id: str, ignore: bool) -> None:
    profile_update(user_id, ignore_status=int(ignore))


def profile_get_ignore_status(user_id: str) -> bool:
    p = profile_get(user_id)
    return bool(p.get("ignore_status"))


def profile_get_context_string(user_id: str) -> str:
    """Build context string for system prompt."""
    p = profile_get(user_id)
    parts = []

    name = p.get("name")
    if name:
        parts.append(f"Name: {name}")
        nicks = p.get("nicknames", [])
        if nicks:
            parts.append(f"Also known as: {', '.join(nicks)}")

    phone = p.get("user_id", "")
    if phone:
        parts.append(f"Phone: {phone}")

    relationship = p.get("relationship")
    if relationship:
        parts.append(f"Relationship: {relationship}")

    count = p.get("interaction_count", 0)
    if count > 0:
        if count < 5:
            parts.append("Familiarity: New contact (just met)")
        elif count < 20:
            parts.append("Familiarity: Getting to know each other")
        elif count < 100:
            parts.append("Familiarity: Regular contact")
        else:
            parts.append("Familiarity: Close / frequent contact")

    facts = p.get("facts", [])
    if facts:
        parts.append(f"Known facts: {' | '.join(facts[-15:])}")

    interests = p.get("interests", [])
    if interests:
        parts.append(f"Interests: {', '.join(interests[-10:])}")

    first_seen = p.get("first_seen")
    if first_seen:
        parts.append(f"First interaction: {first_seen[:10]}")

    if not parts:
        return ""
    return "\n[USER PROFILE]\n" + "\n".join(parts) + "\n[/USER PROFILE]\n"


def profile_get_all_known_names() -> dict[str, str]:
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT user_id, name FROM profiles WHERE name IS NOT NULL")
        return {row[0]: row[1] for row in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# VAULTS
# ─────────────────────────────────────────────────────────────────────────────

def vault_get(vault_key: str) -> str:
    """Get vault content. vault_key: 'global' or 'user:<phone>'"""
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT content FROM vaults WHERE vault_key = ?", (vault_key,))
        row = cur.fetchone()
        return row[0] if row else ""


def vault_append(vault_key: str, new_content: str) -> None:
    """Append to vault with timestamp."""
    init_db()
    timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    existing = vault_get(vault_key)
    separator = f"\n\n--- Learned on {timestamp} ---\n"
    combined = (existing + separator + new_content) if existing else new_content
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO vaults (vault_key, content, updated_at) VALUES (?, ?, ?)",
            (vault_key, combined, time.time()),
        )


def vault_clear(vault_key: str) -> bool:
    init_db()
    with transaction() as conn:
        cur = conn.execute("DELETE FROM vaults WHERE vault_key = ?", (vault_key,))
        return cur.rowcount > 0


def learn_task_background(user_phone: str, text_to_learn: str, doc_name: str | None = None, nvidia_scout_fn=None):
    """Background task to summarize and save facts to the permanent memory vault."""
    from core.config import TZ, log, cfg
    from datetime import datetime
    
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
            vault_key = "global"
        else:
            vault_key = f"user:{user_phone}"

        vault_append(vault_key, facts)

        log.info("[Learn] Learned facts saved to %s", vault_key)
    except Exception as e:
        log.error("[Learn] Background learn task failed: %s", e)


def get_vault_context(user_phone: str) -> str:
    """Retrieve permanent personal and global vault context for system prompt."""
    vault_str = ""

    personal = vault_get(f"user:{user_phone}")
    if personal:
        if len(personal) > 50000:
            personal = "[...older facts truncated...]\n" + personal[-50000:]
        vault_str += f"\n\n--- PERMANENT MEMORY VAULT ---\nLearned facts about this user:\n{personal}\n------------------------------\n"

    global_vault = vault_get("global")
    if global_vault:
        if len(global_vault) > 30000:
            global_vault = "[...older global facts truncated...]\n" + global_vault[-30000:]
        vault_str += f"\n\n--- GLOBAL KNOWLEDGE BASE ---\nShared knowledge:\n{global_vault}\n------------------------------\n"

    return vault_str


# ─────────────────────────────────────────────────────────────────────────────
# VECTORS (RAG)
# ─────────────────────────────────────────────────────────────────────────────

def vector_add(text: str, owner: str = "", group_jid: str = "", source: str = "") -> int:
    """Add a vector chunk. Returns rowid."""
    init_db()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO vectors (text, owner, group_jid, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, owner, group_jid, source, time.time()),
        )
        return cur.lastrowid


def vector_search_fts(query: str, k: int = 5, owner: str = "", group_jid: str = "") -> list[tuple[str, float]]:
    """Search using FTS5 if available, else fallback to LIKE."""
    init_db()
    try:
        with transaction() as conn:
            # Try FTS5 first
            sql = """
                SELECT v.text, rank
                FROM vectors_fts vfts
                JOIN vectors v ON v.id = vfts.rowid
                WHERE vfts.text MATCH ?
            """
            params = [query]
            if owner:
                sql += " AND v.owner = ?"
                params.append(owner)
            if group_jid:
                sql += " AND v.group_jid = ?"
                params.append(group_jid)
            sql += " ORDER BY rank LIMIT ?"
            params.append(k)
            cur = conn.execute(sql, params)
            return [(row[0], row[1]) for row in cur.fetchall()]
    except Exception:
        pass

    # Fallback: simple LIKE search
    with transaction() as conn:
        sql = "SELECT text FROM vectors WHERE text LIKE ?"
        params = [f"%{query}%"]
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        if group_jid:
            sql += " AND group_jid = ?"
            params.append(group_jid)
        sql += " LIMIT ?"
        params.append(k)
        cur = conn.execute(sql, params)
        return [(row[0], 1.0) for row in cur.fetchall()]


def vector_get_all(owner: str = "", group_jid: str = "") -> list[dict]:
    """Get all vectors (for rebuilding index)."""
    init_db()
    with transaction() as conn:
        sql = "SELECT text, owner, group_jid, source FROM vectors WHERE 1=1"
        params = []
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        if group_jid:
            sql += " AND group_jid = ?"
            params.append(group_jid)
        cur = conn.execute(sql, params)
        return [
            {"text": row[0], "owner": row[1], "group": row[2], "source": row[3]}
            for row in cur.fetchall()
        ]


def vector_rebuild_fts() -> None:
    """Rebuild FTS5 index from vectors table."""
    init_db()
    try:
        with transaction() as conn:
            conn.execute("INSERT INTO vectors_fts(vectors_fts) VALUES('rebuild')")
            log.info("[Storage] FTS5 index rebuilt")
    except Exception as e:
        log.warning("[Storage] FTS5 rebuild failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

def cache_get(key: str) -> Optional[str]:
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT value FROM cache WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)",
                           (key, time.time()))
        row = cur.fetchone()
        return row[0] if row else None


def cache_set(key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
    init_db()
    expires = time.time() + ttl_seconds if ttl_seconds else None
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, value, expires),
        )


def cache_delete(key: str) -> None:
    init_db()
    with transaction() as conn:
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))


def cache_clear_expired() -> int:
    init_db()
    with transaction() as conn:
        cur = conn.execute("DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at <= ?", (time.time(),))
        return cur.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# SENT MESSAGES (for self-correction)
# ─────────────────────────────────────────────────────────────────────────────

def sent_message_set(sender: str, message_id: str, sent_text: str) -> None:
    init_db()
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sent_messages (sender, message_id, sent_text, sent_at) VALUES (?, ?, ?, ?)",
            (sender, message_id, sent_text, time.time()),
        )


def sent_message_get(sender: str) -> Optional[dict]:
    init_db()
    with transaction() as conn:
        cur = conn.execute("SELECT message_id, sent_text, sent_at FROM sent_messages WHERE sender = ?", (sender,))
        row = cur.fetchone()
        if row:
            return {"message_id": row[0], "sent_text": row[1], "sent_at": row[2]}
        return None


def sent_message_delete(sender: str) -> None:
    init_db()
    with transaction() as conn:
        conn.execute("DELETE FROM sent_messages WHERE sender = ?", (sender,))


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def migrate_from_json() -> dict:
    """One-time migration from JSON files to SQLite. Returns stats."""
    from core.config import (
        SESSIONS_FILE, VAULTS_DIR, CACHE_FILE, VECTORS_FILE,
        load_json, BASE_DIR
    )
    import glob

    init_db()
    stats = {"sessions": 0, "profiles": 0, "vaults": 0, "vectors": 0, "cache": 0}

    # Sessions
    sessions_data = load_json(SESSIONS_FILE, {})
    with transaction() as conn:
        for sender, data in sessions_data.items():
            turns = data.get("turns", [])
            last_active = data.get("last_active", time.time())
            conn.execute(
                "INSERT OR REPLACE INTO sessions (sender, turns, last_active, updated_at) VALUES (?, ?, ?, ?)",
                (sender, json.dumps(turns, ensure_ascii=False), last_active, time.time()),
            )
            stats["sessions"] += 1

    # Profiles (from profiles.py default location)
    profiles_file = os.path.join(BASE_DIR, "user_profiles.json")
    profiles_data = load_json(profiles_file, {})
    with transaction() as conn:
        for user_id, p in profiles_data.items():
            if not isinstance(p, dict):
                continue
            _profile_insert(conn, {
                "user_id": user_id,
                "name": p.get("name"),
                "nicknames": p.get("nicknames", []),
                "facts": p.get("facts", []),
                "interests": p.get("interests", []),
                "relationship": p.get("relationship"),
                "preferences": p.get("preferences", {}),
                "interaction_count": p.get("interaction_count", 0),
                "last_seen": p.get("last_seen", datetime.now().isoformat()),
                "first_seen": p.get("first_seen", datetime.now().isoformat()),
                "is_creator": int(p.get("is_creator", 0)),
                "ignore_status": int(p.get("ignore_status", 0)),
            })
            stats["profiles"] += 1

    # Vaults
    vault_files = glob.glob(os.path.join(VAULTS_DIR, "vault_*.txt"))
    vault_files.append(os.path.join(VAULTS_DIR, "global_vault.txt"))
    with transaction() as conn:
        for vf in vault_files:
            if not os.path.exists(vf):
                continue
            try:
                with open(vf, "r", encoding="utf-8") as f:
                    content = f.read()
                if not content.strip():
                    continue
                fname = os.path.basename(vf)
                if fname == "global_vault.txt":
                    key = "global"
                else:
                    key = f"user:{fname.replace('vault_', '').replace('.txt', '')}"
                conn.execute(
                    "INSERT OR REPLACE INTO vaults (vault_key, content, updated_at) VALUES (?, ?, ?)",
                    (key, content, time.time()),
                )
                stats["vaults"] += 1
            except Exception as e:
                log.warning("[Storage] Failed to migrate vault %s: %s", vf, e)

    # Vectors
    vectors_data = load_json(VECTORS_FILE, {"chunks": []})
    chunks = vectors_data.get("chunks", [])
    with transaction() as conn:
        for chunk in chunks:
            if isinstance(chunk, str):
                text, owner, group = chunk, "", ""
            elif isinstance(chunk, dict) and chunk.get("text"):
                text = chunk.get("text")
                owner = chunk.get("owner", "")
                group = chunk.get("group", "")
            else:
                continue
            conn.execute(
                "INSERT INTO vectors (text, owner, group_jid, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (text, owner, group, "migrated", time.time()),
            )
            stats["vectors"] += 1

    # Cache
    cache_data = load_json(CACHE_FILE, {})
    with transaction() as conn:
        for key, value in cache_data.items():
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, value, None),
            )
            stats["cache"] += 1

    # Rebuild FTS
    try:
        vector_rebuild_fts()
    except Exception:
        pass

    log.info("[Storage] Migration complete: %s", stats)
    return stats


# Initialize on import
init_db()