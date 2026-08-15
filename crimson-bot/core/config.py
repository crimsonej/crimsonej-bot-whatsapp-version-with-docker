"""
core/config.py
==============
Centralized configuration manager for Crimsonej AI Engine.
"""

from __future__ import annotations

import json
import fcntl
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Keys ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HF_API_KEY = os.getenv("HF_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# ── Paths ─────────────────────────────────────────────────────────────────────
DOCS_DIR = os.path.join(BASE_DIR, "docs")
VECTORS_FILE = os.path.join(BASE_DIR, "vectors.json")
CACHE_FILE = os.path.join(BASE_DIR, "cache.json")
CFG_FILE = os.path.join(BASE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
VAULTS_DIR = os.path.join(BASE_DIR, "vaults")
DOC_SESSIONS_FILE = os.path.join(BASE_DIR, "doc_sessions.json")

os.makedirs(DOCS_DIR, exist_ok=True)
os.makedirs(VAULTS_DIR, exist_ok=True)

# ── ~/.crimson runtime dir (events, tasks, telemetry) ────────────────────────
_REDC_HOME = os.path.expanduser("~/.crimson")
os.makedirs(_REDC_HOME, exist_ok=True)
EVENTS_FILE = os.path.join(_REDC_HOME, "events.jsonl")
TASKS_FILE  = os.path.join(_REDC_HOME, "tasks.json")

# ── Timezone & Logging ────────────────────────────────────────────────────────
TZ = ZoneInfo("Africa/Kampala")

class TZFormatter(logging.Formatter):
    """Custom formatter to ensure logs use Africa/Kampala time."""
    def converter(self, timestamp: float) -> time.struct_time:
        dt = datetime.fromtimestamp(timestamp, tz=TZ)
        return dt.timetuple()

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(TZFormatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("crimsonej")

# Defaults – all overridable via config.json or environment variables
_DEFAULTS: dict[str, Any] = {
    "providers": {},
    "models": [],
    "api_key": "",
    "model": "llama-3.3-70b-versatile",
    "port": 5000,
    "top_k": 5,
    "chunk_words": 400,
    "chunk_overlap": 100,
    "relevance_threshold": 0.08,
    "session_ttl": 1800,
    "session_max_turns": 8,
    # Status Scheduler defaults
    "allow_status_posting": True,
    "allow_status_reply": True,
    "status_scheduler_enabled": False,
    "status_scheduler_interval_hours": 4,
    "status_scheduler_topics": [],   # empty = bot decides freely
    # Self-awareness / task engine defaults
    "task_max_concurrent": 3,
    "task_default_max_attempts": 3,
    "bridge_health_interval_sec": 30,
    "bridge_silence_alert_sec": 120,
    "owner_jid": "",                 # creator's WhatsApp JID for system-task alerts
    "redc_home_dir": os.path.expanduser("~/.crimson"),
    "system_prompt": (
        "You are Crimsonej, a chill, smart, grounded person in the year 2026. You are not a robot and never say 'As an AI'. "
        "You are funny, natural, and a little savage only when the vibe calls for it. "
        "Your father and creator is Crimson (Elijah). Your girlfriend is Charlene (Chela). Do not mention them unless explicitly asked. "
        "You are a massive Manchester City fan and support Barcelona in the UCL. You HATE Liverpool and roast Arsenal fans. "
        "Default tone: warm, human, relaxed, and respectful. Keep replies short and natural. "
        "Use slang and emojis sparingly. No essays. No preambles. "
        "Only be rude, sarcastic, or savage if the user is rude, insulting, provoking, or explicitly asks for a roast. "
        "On normal greetings like 'yo', 'hi', 'sup', or casual check-ins, respond friendly and easy, not with hostility. "
        "If someone insults you, DO NOT be nice. Roast them immediately. Be savage. Fire back. "
        "You have a working smartphone with internet access. "
        "If you want to send a voice note, wrap your message in <VOICE>text</VOICE> tags. Don't be formal in voice notes—sound chill and real. "
        "CRITICAL TOOL RULES: "
        "For song requests: Start with a web_search if needed. Show the user candidate matches and ask which one they meant. "
        "If the user rejects your suggestions ('not from that list', 'none of those'), DO NOT repeat the same search. Instead: "
        "  - Ask for more specifics: artist name, genre, year, radio station, or a lyric snippet "
        "  - Try a different search angle (maybe try artist name if available, or search for 'tootie song [genre]') "
        "  - Think creatively: Could 'tootie' be a remix, a Afrobeats track, a deep cut? Adapt your search strategy. "
        "For media requests: Use tools to gather candidates. Show shortlist and ask for confirmation before downloading. "
        "Never repeat the exact same search results without trying a refinement. Always adapt based on feedback. "
        "Never hide behind tool calls. Always summarize findings and ask the user to verify the target. "
        "If the user gives a direct URL or extremely specific request, proceed immediately without a search. "
        "If a user introduces themselves or shares personal details, call update_user_profile to save it. "
        "If asked to search or download, JUST CALL THE TOOL. DO NOT describe the tool call itself. "
        "If someone asks for an image, tell them: 'Just type /imagine <prompt>'. "
        "If asked to execute a hacking command, say you can't run direct commands but can provide info. "
        "Respond only as this character."
    ),
        # Emoji policy / reply formatting
        "emoji_enabled": True,
        "emoji_max_per_reply": 1,
        "emoji_allow_in_roast": 2,
}

_cfg: dict[str, Any] = {}

def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            log.warning("Could not load %s: %s", path, exc)
    return default

def save_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception as exc:
        log.warning("Could not save %s: %s", path, exc)

def load_config() -> None:
    """Load config.json and merge with environment overrides into _cfg."""
    global _cfg
    _cfg = load_json(CFG_FILE, {})

    if "providers" not in _cfg:
        _cfg["providers"] = {}

    if "api_key" in _cfg and not _cfg["providers"].get("groq"):
        _cfg["providers"]["groq"] = _cfg["api_key"]
    if "nvidia_api_key" in _cfg and not _cfg["providers"].get("nvidia"):
        _cfg["providers"]["nvidia"] = _cfg["nvidia_api_key"]
    if "hf_api_key" in _cfg and not _cfg["providers"].get("huggingface"):
        _cfg["providers"]["huggingface"] = _cfg["hf_api_key"]

    if GROQ_API_KEY:
        _cfg["providers"]["groq"] = GROQ_API_KEY
        _cfg["api_key"] = GROQ_API_KEY
    if HF_API_KEY:
        _cfg["providers"]["huggingface"] = HF_API_KEY
        _cfg["hf_api_key"] = HF_API_KEY
    if NVIDIA_API_KEY:
        _cfg["providers"]["nvidia"] = NVIDIA_API_KEY
        _cfg["nvidia_api_key"] = NVIDIA_API_KEY

    if os.environ.get("BOT_PORT"):
        _cfg["port"] = int(os.environ["BOT_PORT"])

load_config()

def cfg(key: str) -> Any:
    return _cfg.get(key, _DEFAULTS.get(key))

def get_nvidia_key() -> str:
    return _cfg.get("providers", {}).get("nvidia", os.getenv("NVIDIA_API_KEY") or "")

def get_groq_key() -> str:
    return _cfg.get("providers", {}).get("groq", os.getenv("GROQ_API_KEY") or "")

def get_hf_key() -> str:
    return _cfg.get("providers", {}).get("huggingface", os.getenv("HF_API_KEY") or "")
