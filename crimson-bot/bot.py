"""
bot.py · Crimsonej AI Engine Entry Point
========================================
Flask application handling incoming WhatsApp bridge requests, RAG search,
slash commands, and autonomous NVIDIA AI tool execution.
"""

from __future__ import annotations

import base64
import hashlib
import json
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import random
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify
from PIL import Image

from core.config import (
    BASE_DIR, DOCS_DIR, VECTORS_FILE, CACHE_FILE, DOC_SESSIONS_FILE,
    CFG_FILE, TZ, cfg, get_groq_key, load_json, save_json, log
)
from core.eventlog import event_log
from core.llm import call_llm, _call_nvidia, NVIDIA_SCOUT, MAX_CONTEXT_TOKENS, MAX_SYSTEM_TOKENS, MAX_USER_MSG_TOKENS, MAX_HISTORY_MSG_TOKENS, truncate_to_tokens, scout_quick_call
import threading
from services.dispatcher import get_dispatcher, start_dispatcher, stop_dispatcher, dispatcher_is_alive
from services.memory import profile_mgr, sessions, get_vault_context, learn_task_background
from services.reporter import start_reporter, stop_reporter
from services.tasks import task_store
from services.tools import ALL_TOOLS, execute_tool_calls
from services.self_correct import verify_and_correct
import services.vision as vision_svc
import services.media as media_svc
import services.bridge_api as bridge_api
from services.scheduler import start_scheduler, stop_scheduler, restart_scheduler, trigger_now

# ── Flask App Setup ──────────────────────────────────────────────────────────
app = Flask(__name__)
_cache: dict[str, str] = load_json(CACHE_FILE, {})
_BOOT_TIME: float = 0.0
doc_session: dict[str, Any] = {}  # docs are transient — never restored from disk

def save_doc_sessions():
    save_json(DOC_SESSIONS_FILE, doc_session)

user_last_search: dict[str, float] = {}
user_last_msg: dict[str, float] = {}
image_memory: dict[str, dict] = {}
pending_song_searches: dict[str, dict] = {}
# sender -> {message_id, sent_text, sent_at} of the bot's most recent
# conversational text reply. Populated by the bridge via POST /sent_ids and
# consulted by services/self_correct.py to decide whether to edit/delete.
_last_sent: dict[str, dict] = {}
MSG_COOLDOWN_SECS = 0.5

# Thread-safe access to the dicts above. Now that Flask runs threaded=True,
# multiple workers can mutate these concurrently.
_state_lock = threading.Lock()

# simple in-memory dedupe for raw error notifications: fingerprint -> last_sent_ts
_error_notify_cache: dict[str, float] = {}
_ERROR_NOTIFY_DEDUPE_SEC = 300

ROAST_PROMPT = """You are only in roast mode when the user is rude, insulting, provoking, or explicitly asks for a roast. Otherwise stay warm, chill, and casual. Keep roasts short, sharp, and only when warranted. Never roast a normal greeting like 'yo', 'hi', or 'sup'."""

TALK_REQUEST_RE = re.compile(
    r'talk to him|respond to that|reply to him|roast him|roast that|talk to this|roast her|clown him|clown her|destroy him|cook him|end him|burn him',
    re.IGNORECASE
)

IDENTITY_PHRASES = ["who are you", "what are you", "who is this", "who are u", "what is your name", "who made you"]
IDENTITY_REPLY = "I'm Crimsonej – your guy built by Crimson. What can I help with? 😎"

# ── RAG Index ────────────────────────────────────────────────────────────────
def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())

def _build_tfidf(corpus: list[str]) -> tuple[list[dict[str, float]], dict[str, float]]:
    N = len(corpus)
    df: dict[str, int] = {}
    tfs: list[dict[str, float]] = []

    for doc in corpus:
        tokens = _tokenize(doc)
        tf: dict[str, int] = {}
        for t in tokens: tf[t] = tf.get(t, 0) + 1
        total = len(tokens) or 1
        tfs.append({t: c / total for t, c in tf.items()})
        for t in tf: df[t] = df.get(t, 0) + 1

    idf = {t: math.log((N + 1) / (cnt + 1)) + 1 for t, cnt in df.items()}
    vecs: list[dict[str, float]] = []
    for tf_doc in tfs:
        v = {t: tf_doc[t] * idf.get(t, 1.0) for t in tf_doc}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})

    return vecs, idf


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    return sum(a[t] * b[t] for t in set(a) & set(b))


def _query_vec(query: str, idf: dict[str, float]) -> dict[str, float]:
    tokens = _tokenize(query)
    tf: dict[str, int] = {}
    for t in tokens: tf[t] = tf.get(t, 0) + 1
    total = len(tokens) or 1
    v = {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / norm for t, x in v.items()}

class Index:
    def __init__(self) -> None:
        # chunks stored as list of dicts: {"text": str, "owner": str|None, "group": str|None}
        self.chunks: list[dict] = []
        self.vecs: list[dict[str, float]] = []
        self.idf: dict[str, float] = {}

    def load(self) -> None:
        data = load_json(VECTORS_FILE, {"chunks": []})
        raw = data.get("chunks", [])
        normalized = []
        for c in raw:
            if isinstance(c, str):
                normalized.append({"text": c, "owner": "", "group": ""})
            elif isinstance(c, dict) and c.get("text"):
                normalized.append({"text": c.get("text"), "owner": c.get("owner", ""), "group": c.get("group", "")})
        self.chunks = normalized
        if self.chunks:
            corpus = [c["text"] for c in self.chunks]
            self.vecs, self.idf = _build_tfidf(corpus)
        log.info("Index loaded: %d chunks", len(self.chunks))

    def save(self) -> None:
        save_json(VECTORS_FILE, {"chunks": self.chunks})

    def build(self, force: bool = False) -> None:
        if self.chunks and not force:
            return
        # Use the RAG reindexer to (re)build vectors.json when requested
        try:
            from services.rag import build_index_from_docs
            build_index_from_docs(force=force)
        except Exception:
            # fall back to naive doc scanning if rag module not available
            if not os.path.isdir(DOCS_DIR) or not os.listdir(DOCS_DIR):
                return
            self.chunks = []
            for fname in sorted(os.listdir(DOCS_DIR)):
                fpath = os.path.join(DOCS_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        raw = fh.read()
                    words = raw.split()
                    size, overlap = cfg("chunk_words"), cfg("chunk_overlap")
                    i = 0
                    while i < len(words):
                        self.chunks.append({"text": " ".join(words[i: i + size]), "owner": "", "group": ""})
                        i += size - overlap
                except Exception as exc:
                    log.warning("Skipping %s: %s", fname, exc)
        data = load_json(VECTORS_FILE, {"chunks": []})
        raw = data.get("chunks", [])
        normalized = []
        for c in raw:
            if isinstance(c, str):
                normalized.append({"text": c, "owner": "", "group": ""})
            elif isinstance(c, dict) and c.get("text"):
                normalized.append({"text": c.get("text"), "owner": c.get("owner", ""), "group": c.get("group", "")})
        self.chunks = normalized
        if self.chunks:
            corpus = [c["text"] for c in self.chunks]
            self.vecs, self.idf = _build_tfidf(corpus)
        self.save()

    def search(self, query: str, k: int | None = None, user_id: str | None = None, group_id: str | None = None) -> tuple[list[str], float]:
        """Search the index. If `user_id` or `group_id` provided, prefer matching chunks.

        Returns (chunks_texts, best_score).
        """
        if not self.chunks: return [], 0.0
        k = k or cfg("top_k")
        # Build a candidate corpus and mapping back to indices
        corpus = [c["text"] if isinstance(c, dict) else str(c) for c in self.chunks]
        qv = _query_vec(query, self.idf)
        scores = [( _cosine(qv, v), i) for i, v in enumerate(self.vecs)]
        # filter out zero scores and sort
        scores = sorted(((s, i) for s, i in scores if s > 0), reverse=True)
        # If user or group provided, prefer chunks owned by them or global
        filtered = []
        for s, i in scores:
            meta = self.chunks[i] if i < len(self.chunks) else {}
            owner = (meta.get("owner") if isinstance(meta, dict) else "") or ""
            group = (meta.get("group") if isinstance(meta, dict) else "") or ""
            if user_id and owner and owner != user_id:
                # skip chunks owned by other users
                continue
            if group_id and group and group != group_id:
                continue
            filtered.append((s, i))

        best = filtered[0][0] if filtered else 0.0
        selected = [ (self.chunks[i]["text"] if isinstance(self.chunks[i], dict) else str(self.chunks[i])) for _, i in filtered[:k] ]
        return selected, best

index = Index()

# ── Helper Functions ─────────────────────────────────────────────────────────
def is_roast_request(message: str, quoted: str = "") -> bool:
    msg_lower = (message + " " + quoted).lower()
    keywords = ['stupid', 'idiot', 'dumb', 'fool', 'loser', 'roast', 'clown', 'burn', 'cook']
    return any(k in msg_lower for k in keywords)

def is_talk_request(message: str) -> bool:
    return bool(TALK_REQUEST_RE.search(message))


def _needs_clarification_for_media(question: str) -> str | None:
    """Return a short follow-up for vague music/search requests that aren't specific enough to act."""
    q = re.sub(r"[^a-z0-9\s]", " ", question.lower()).strip()
    if not q:
        return None

    media_markers = [
        "song", "track", "music", "audio", "video", "download", "find me", "search",
        "look for", "play", "listen to", "help me get", "called", "named"
    ]
    if not any(marker in q for marker in media_markers):
        if not re.search(r"\b(i think|maybe|probably)\b.*\b(called|named)\b", q):
            return None

    specific_markers = [
        "artist", "lyrics", "album", "year", "link", "http", "youtube", "spotify",
        "feat", "ft", "by ", "official", "full song"
    ]
    if any(marker in q for marker in specific_markers):
        return None

    if re.search(r"\b(i think|i guess|maybe|probably)\b.*\b(called|named)\b", q):
        return "I need one more clue before I search — artist, exact title, lyric snippet, or a link."

    low_info_patterns = [
        r"\b(called|named|its called|it's called|it's named|named)\b",
        r"\b(song|track|music|video|download)\b(\s+\w+){0,2}$",
    ]
    if any(re.search(p, q) for p in low_info_patterns):
        return "What’s the exact title, artist, lyrics snippet, or a direct link? I need one solid clue before I search."

    tokens = re.findall(r"[a-z0-9]+", q)
    if len(tokens) <= 2:
        return "Give me one more detail — the artist, exact title, lyrics, or a link — and I’ll find the right one."

    return None

def extract_text_from_doc_payload(sd: str, fname: str, fmime: str) -> str:
    try:
        import PyPDF2, docx as docx_lib
        if ',' in sd: sd = sd.split(',', 1)[1]
        sd += '=' * (-len(sd) % 4)
        doc_bytes = base64.b64decode(sd)
        text = ''
        if fname.lower().endswith('.pdf') or fmime == 'application/pdf':
            reader = PyPDF2.PdfReader(io.BytesIO(doc_bytes))
            for page in reader.pages:
                text += (page.extract_text() or '') + '\n'
        elif fname.lower().endswith('.docx') or 'officedocument' in fmime:
            doc_file = docx_lib.Document(io.BytesIO(doc_bytes))
            for para in doc_file.paragraphs:
                text += para.text + '\n'
        else:
            text = doc_bytes.decode('utf-8', errors='ignore')
        return text.strip()
    except Exception as e:
        log.error("[Doc Extract] Error: %s", e)
        return ""

def _clean_base64(data: str | None) -> str:
    if not data:
        return ""
    data = str(data).strip()
    if ',' in data:
        data = data.split(',', 1)[1]
    return data.strip()

def _visual_payload_base64(body: dict) -> str:
    return _clean_base64(
        body.get("image_base64")
        or body.get("image_data")
        or body.get("sticker_data")
        or body.get("media_base64")
        or body.get("Yimage_base64")
    )

def _vision_failed(text: str | None) -> bool:
    if not text:
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "not configured",
            "could not analyze",
            "no description returned",
            "vision service unavailable",
        )
    )


def _is_emoji_char(ch: str) -> bool:
    """Rudimentary check for emoji characters (keeps patterns simple)."""
    if not ch:
        return False
    # Broad ranges that cover most common emojis
    return bool(
        re.match(r"[\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\u2600-\u26FF\u2700-\u27BF]", ch)
    )


def _limit_emojis(text: str, max_keep: int) -> str:
    """Limit emojis in `text` to at most `max_keep`. If there are more, reduce to
    the first `max_keep` and remove others to keep replies less emoji-heavy."""
    if not text or max_keep is None or max_keep < 0:
        return text
    emojis = []
    for ch in text:
        if _is_emoji_char(ch):
            emojis.append(ch)

    if len(emojis) <= max_keep:
        return text

    # Remove all emoji chars and append up to max_keep of the original emojis at the end
    stripped = ''.join(ch for ch in text if not _is_emoji_char(ch))
    keep = ''.join(emojis[:max_keep])
    # Preserve a single trailing emoji separated by a space if text ends with punctuation/space
    sep = '' if stripped.endswith(' ') or stripped == '' else ' '
    return (stripped + sep + keep).strip()

def _sticker_reply_from_visual(image_b64: str, user_phone: str, sender: str) -> dict:
    desc = vision_svc.analyze_image_with_nvidia(
        image_b64,
        (
            "Analyze this WhatsApp sticker like a chat reaction. Identify the subject, "
            "facial expression, gesture/pose, visible text, emotion, joke, and likely intent. "
            "Be specific and avoid generic wording. Keep it to 2 compact sentences."
        ),
    )
    if _vision_failed(desc):
        log.warning("[Sticker] Vision failed or unavailable: %s", desc)
        desc = "a funny WhatsApp sticker with expressive meme energy"

    profile_context = profile_mgr.get_context_string(user_phone)
    decision_messages = [
        {
            "role": "system",
            "content": (
                cfg("system_prompt")
                + "\n\nYou are deciding how to respond to a sticker. "
                "Return ONLY valid JSON with keys reply and sticker_prompt. "
                "The reply must be empty or under 8 words. "
                "The sticker_prompt must describe one expressive, funny sticker image to generate. "
                "No markdown, no extra text."
                + (profile_context or "")
            ),
        },
        {
            "role": "user",
            "content": (
                f"Incoming sticker analysis: {desc}\n"
                "Create a natural sticker reply that matches or playfully escalates that exact mood like a real WhatsApp chat."
            ),
        },
    ]

    sticker_prompt = (
        f"Respond to this sticker's exact vibe: {desc}. Create a bold, funny, high-contrast WhatsApp sticker response, "
        "single subject, expressive face, clear emotion, clean composition, transparent background, no tiny text."
    )
    reply_text = ""
    try:
        decision = call_llm(decision_messages)
        decision_text = decision.get("reply", "") if isinstance(decision, dict) else str(decision)
        decision_text = re.sub(r"^```(?:json)?|```$", "", decision_text.strip(), flags=re.IGNORECASE).strip()
        parsed = json.loads(decision_text)
        if isinstance(parsed, dict):
            reply_text = str(parsed.get("reply") or "").strip()
            candidate_prompt = str(parsed.get("sticker_prompt") or "").strip()
            if candidate_prompt:
                sticker_prompt = candidate_prompt
    except Exception as exc:
        log.warning("[Sticker] Brain decision fallback: %s", exc)

    sticker_prompt = (
        f"{sticker_prompt[:450]}. Sticker art, 512x512, transparent background, "
        "bold silhouette, readable expression, no watermark."
    )
    stk_b64 = vision_svc.generate_sticker_auto(sticker_prompt)
    if stk_b64:
        res = {"sticker": stk_b64}
        if reply_text:
            res["reply"] = reply_text
        try:
            sessions.get(sender).add("user", f"[Sticker received: {desc}]")
            sessions.get(sender).add("assistant", f"[Sticker reply generated: {sticker_prompt}]")
        except Exception:
            pass
        return res

    return {"reply": f"I saw it: {desc}\nCouldn't generate the sticker reply rn."}

def handle_commands(raw_question: str, user_phone: str, session_id: str, quoted: str = "") -> dict | None:
    lower = raw_question.lower()
    if lower in ("/help", "help") or lower.startswith("/help "):
        help_text = (
            "🤖 *Crimsonej Full Command List* 🤖\n\n"
            "💬 *User Commands:*\n"
            "📄 */read [prompt]* - Summarize or query an attached/quoted document (.pdf, .docx, .txt)\n"
            "🧠 */learn [text/doc]* - Store document or text in permanent long-term memory\n"
            
            "🎨 */imagine <prompt>* - Generate AI image (NVIDIA Flux 2 / HF Schnell)\n"
            # image-edit removed
            "✨ */sticker [prompt]* - Generate AI sticker or convert media to WebP sticker\n"
            "📸 */reg-img [prompt]* - Analyze image using NVIDIA VLM vision intelligence\n"
            "🎵 */song-audio <name/link>* - Search and download audio track\n"
            "🎬 */song-video <name/link>* - Search and download video track\n"
            "🗣️ */respond <prompt>* - Direct reply to a quoted message\n\n"
            "📈 *Trading Coach Commands:*\n"
            "🔍 */analyze <symbol> [interval]* - Full TA on any pair (BTC, ETH, EURUSD, GOLD, SPX, AAPL...)\n"
            "📚 */teach <topic>* - Learn a concept (candlesticks, structure, risk_management, rsi, macd, etc.)\n"
            "📋 */lessons* - List all available lessons\n"
            "👁️ */watchlist add/remove/list <symbol>* - Manage your watchlist\n"
            "💰 */price <symbols...>* - Quick price check (BTC ETH EURUSD GOLD)\n"
            "📊 */brief [pre_london|eod]* - Daily trading briefing\n"
            "🧠 */quiz [topic]* - Take a trading quiz (candlesticks, risk_management, rsi, etc.)\n"
            "✅ */quiz_answer <topic> <0-3>* - Submit quiz answer\n"
            "📖 */walkthrough <symbol> [interval]* - Step-by-step chart walkthrough\n"
            "📊 */mtf <symbol>* - Multi-timeframe analysis (Daily, 4H, 1H)\n"
            "🔍 */patterns <symbol> [interval]* - Detect chart patterns\n"
            "📓 */journal log <trade> | stats* - Log trades & view stats\n\n"
            "👑 *Master Control (Creator Commands):*\n"
            "• `master control chela` - Authenticate as Creator & grant full access\n"
            "• `master control status_posting [on/off]` - Toggle status posting\n"
            "• `master control status_reply [on/off]` - Toggle auto-replying to statuses\n"
            "• `master control scheduler [on/off]` - Toggle background status scheduler\n"
            "• `master control interval [hours]` - Set posting interval in hours\n"
            "• `master control topic add/remove/clear/list` - Manage status topic list\n"
            "• `master control status_now` - Trigger immediate status post\n"
            "• `master control config` - View active Master Control configuration\n\n"
            "🖥️ *Terminal CLI Commands:*\n"
            "• `crimsonej start | stop | status | logs | setup | reindex`\n\n"
            "👤 *Creator:* Crimson (Elijah)"
        )
        return {"reply": help_text}

    if lower.startswith("/song-audio") or lower.startswith("/song-video"):
        media_type = "audio" if "audio" in lower else "video"
        query = raw_question[11:].strip()
        if not query:
            return {"reply": f"Please provide a query: `/song-{media_type} Shape of You`"}

        if re.match(r'^https?://', query):
            res = media_svc.download_youtube(query, media_type)
            if res:
                path, filename = res
                if path.startswith("https://"):
                    return {"reply": f"here's the link fam: {path}"}
                return {media_type: path, "filename": filename, "reply": media_svc.format_download_confirmation(filename, media_type)}
            return {"reply": "download flopped on me 😭 try a different link?"}

        results = media_svc.search_youtube(query, limit=10, media_type=media_type)
        if not results:
            return {"reply": "couldn't find anything on YouTube for that 😭 try a different name?"}
        with _state_lock:
            pending_song_searches[user_phone] = {"type": media_type, "results": results}
        lines = [f"{i+1}. {v['title']} ({media_svc.format_duration(v.get('duration'))})" for i, v in enumerate(results[:10])]
        emoji = "🎬" if media_type == "video" else "🎵"
        return {"reply": f"{emoji} Select a number (1-{len(results)}):\n" + "\n".join(lines)}

    if lower.startswith("/imagine"):
        prompt = raw_question[8:].strip()
        if not prompt: return {"reply": "Usage: `/imagine a lion in space`"}
        img_path = vision_svc.generate_image_auto(prompt)
        return {"image": img_path, "reply": "🎨 Here's your image!"} if img_path else {"reply": "Image generation failed."}

    if lower.startswith("/sticker"):
        prompt = raw_question[9:].strip()
        if not prompt: return {"reply": "Usage: `/sticker happy cat`"}
        stk = vision_svc.generate_sticker_auto(prompt)
        return {"sticker": stk, "reply": "✨ Here's your sticker!"} if stk else {"reply": "Sticker generation failed."}

    # voice feature removed

    # ── Trading Coach Commands ──────────────────────────────────────────────────
    if lower.startswith("/analyze"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/analyze BTC [1h]` — interval optional (1m,5m,15m,30m,1h,4h,1d,1w)"}
        symbol = parts[1].upper()
        interval = parts[2] if len(parts) >= 3 else "1h"
        from services.trading import analyze_symbol
        result = analyze_symbol(symbol, interval)
        if "error" in result:
            return {"reply": f"Couldn't analyze {symbol}: {result['error']}"}
        bias_emoji = "🟢" if result["bias"] == "bullish" else ("🔴" if result["bias"] == "bearish" else "⏸️")
        reply = (
            f"{bias_emoji} *{result['symbol']} {interval}* — **{result['bias'].upper()}** ({result['confidence']}% conf)\n"
            f"Price: {result['price']:,.4f} | Trend: {result['structure']}\n"
            f"Reasons: {'; '.join(result['reasons'])}\n"
            f"Support: {result['levels']['supports'] or '—'} | Resistance: {result['levels']['resistances'] or '—'}"
        )
        res = {"reply": reply}
        if result.get("chart_path"):
            res["image"] = result["chart_path"]
        return res

    if lower.startswith("/teach"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/teach candlesticks` — topics: candlesticks, structure, support_resistance, risk_management, rsi, macd, moving_averages, volume, multi_timeframe, liquidity, journaling, psychology, market_sessions"}
        topic = parts[1].lower()
        from services.trading import get_lesson
        lesson = get_lesson(topic)
        if not lesson:
            return {"reply": f"Unknown topic. Use `/lessons` to see all."}
        return {"reply": f"*{lesson['title']}* ({lesson['level']})\n\n{lesson['content']}"}

    if lower.startswith("/lessons"):
        from services.trading import list_lessons
        lessons = list_lessons()
        lines = [f"• *{l['topic']}* — {l['title']} ({l['level']})" for l in lessons]
        return {"reply": "Available lessons:\n" + "\n".join(lines)}

    if lower.startswith("/watchlist"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/watchlist add BTC` | `/watchlist remove ETH` | `/watchlist list`"}
        action = parts[1].lower()
        from services.trading import add_to_watchlist, remove_from_watchlist, get_watchlist
        if action == "list":
            wl = get_watchlist(user_phone)
            return {"reply": "Your watchlist: " + (", ".join(wl) if wl else "empty")}
        if len(parts) < 3:
            return {"reply": "Usage: `/watchlist add BTC` | `/watchlist remove ETH`"}
        symbol = parts[2].upper()
        if action == "add":
            ok = add_to_watchlist(user_phone, symbol)
            return {"reply": f"Added {symbol} to watchlist" if ok else f"Couldn't add {symbol} (unknown symbol)"}
        elif action == "remove":
            ok = remove_from_watchlist(user_phone, symbol)
            return {"reply": f"Removed {symbol} from watchlist" if ok else f"{symbol} not in watchlist"}
        return {"reply": "Action must be add/remove/list"}

    if lower.startswith("/price"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/price BTC ETH EURUSD GOLD`"}
        symbols = [s.upper() for s in parts[1:]]
        from services.trading import quick_price_check
        results = quick_price_check(symbols)
        lines = []
        for r in results:
            if r.get("price"):
                chg = r.get("change_pct_24h", 0)
                emoji = "🟢" if chg > 0 else ("🔴" if chg < 0 else "⚪")
                lines.append(f"{emoji} {r['symbol']}: {r['price']:,.4f} ({chg:+.2f}%)")
            else:
                lines.append(f"⚪ {r['symbol']}: no data")
        return {"reply": "\n".join(lines)}

    if lower.startswith("/brief"):
        parts = raw_question.split()
        session = parts[1].lower() if len(parts) >= 2 else "pre_london"
        from services.trading import generate_daily_briefing
        brief = generate_daily_briefing(session)
        return {"reply": brief["text"]}

    if lower.startswith("/quiz_answer"):
        parts = raw_question.split()
        if len(parts) < 3:
            return {"reply": "Usage: `/quiz_answer <topic> <0-3>` — e.g. `/quiz_answer candlesticks 1`"}
        topic = parts[1].lower()
        try:
            answer = int(parts[2])
        except ValueError:
            return {"reply": "Answer must be 0, 1, 2, or 3"}
        from services.trading import QUIZ_QUESTIONS, check_quiz_answer
        question = next((q for q in QUIZ_QUESTIONS if q["topic"] == topic), None)
        if not question:
            return {"reply": f"No questions for '{topic}'"}
        result = check_quiz_answer(question, answer)
        return {"reply": result["message"] + "\n\n" + result["explanation"]}

    if lower.startswith("/quiz"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/quiz [topic]` — topics: candlesticks, structure, risk_management, rsi, multi_timeframe, support_resistance, liquidity, journaling"}
        topic = parts[1].lower()
        from services.trading import get_quiz_question
        question = get_quiz_question(topic)
        if "error" in question:
            return {"reply": f"No questions for '{topic}'. Try: candlesticks, structure, risk_management, rsi, multi_timeframe, support_resistance, liquidity, journaling"}
        return {"reply": (
            f"🧠 **Quiz: {question['topic'].replace('_', ' ').title()}**\n\n"
            f"{question['question']}\n\n"
            f"Options:\n" +
            "\n".join(f"  {i}. {opt}" for i, opt in enumerate(question["options"])) +
            f"\n\nReply with: `/quiz_answer {topic} <0-3>`"
        )}

    if lower.startswith("/walkthrough"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/walkthrough BTC [4h]` — interval optional (1h, 4h, 1d)"}
        symbol = parts[1].upper()
        interval = parts[2] if len(parts) >= 3 else "4h"
        from services.trading import live_walkthrough
        result = live_walkthrough(symbol, interval)
        if "error" in result:
            return {"reply": f"Couldn't walk through {symbol}: {result['error']}"}
        res = {"reply": result["walkthrough"]}
        if result.get("chart_path"):
            res["image"] = result["chart_path"]
        return res

    if lower.startswith("/mtf") or lower.startswith("/multitf"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/mtf BTC` — multi-timeframe analysis (Daily, 4H, 1H)"}
        symbol = parts[1].upper()
        from core.trading_ta import multi_timeframe_analysis
        result = multi_timeframe_analysis(symbol)
        reply = (
            f"📊 **Multi-TF Analysis: {result['symbol']}**\n\n"
            f"{result['summary']}\n\n"
            f"**HTF (Daily):** {result['timeframes'].get('HTF', {}).get('bias', 'N/A').upper()} "
            f"({result['timeframes'].get('HTF', {}).get('confidence', 0)}%) | "
            f"Structure: {result['timeframes'].get('HTF', {}).get('structure', 'N/A')}\n"
            f"**MTF (4H):** Bias: {result['timeframes'].get('MTF', {}).get('bias', 'N/A').upper()} | "
            f"Structure: {result['timeframes'].get('MTF', {}).get('structure', 'N/A')} | "
            f"Momentum: {result['timeframes'].get('MTF', {}).get('momentum', 'N/A')}\n"
            f"**LTF (1H):** Momentum: {result['timeframes'].get('LTF', {}).get('momentum', 'N/A').upper()} | "
            f"Price: {result['timeframes'].get('LTF', {}).get('price', 'N/A'):,.4f}"
        )
        return {"reply": reply}

    if lower.startswith("/patterns"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/patterns BTC [4h]` — detect chart patterns"}
        symbol = parts[1].upper()
        interval = parts[2] if len(parts) >= 3 else "4h"
        from core.trading_ta import detect_patterns
        from core.market_data import get_klines
        candles = get_klines(symbol, interval, 100)
        if not candles or len(candles) < 20:
            return {"reply": f"Not enough data for {symbol} {interval}"}
        patterns = detect_patterns(candles)
        if not patterns:
            return {"reply": f"No clear patterns detected on {symbol} {interval}."}
        lines = [f"🔍 **Patterns on {symbol} {interval}:**"]
        for p in patterns:
            direction_emoji = "🟢" if p["direction"] == "bullish" else ("🔴" if p["direction"] == "bearish" else "⚪")
            lines.append(f"{direction_emoji} **{p['type'].replace('_', ' ').title()}** ({p['confidence']}%)")
            lines.append(f"   {p['description']}")
        return {"reply": "\n".join(lines)}

    if lower.startswith("/journal"):
        parts = raw_question.split()
        if len(parts) < 2:
            return {"reply": "Usage: `/journal log BTC long 50000 49000 52000 0.1 win 1000 2 bull_flag 'notes'` | `/journal stats`"}
        sub = parts[1].lower()
        if sub == "stats":
            from services.trading import get_trade_stats
            stats = get_trade_stats(user_phone)
            if stats.get("total", 0) == 0:
                return {"reply": "No trades recorded yet."}
            if stats.get("closed_trades", 0) == 0:
                return {"reply": f"{stats['total']} trades logged, none closed yet."}
            reply = (
                f"📈 **Your Trading Stats**\n\n"
                f"Total: {stats['total_trades']} | Closed: {stats['closed_trades']}\n"
                f"Win rate: {stats['win_rate']}%\n"
                f"Avg win: {stats['avg_win_r']}R | Avg loss: {stats['avg_loss_r']}R\n"
                f"Expectancy: {stats['expectancy_r']}R | Total R: {stats['total_r']:.2f}\n"
                f"Profitable: {'YES ✅' if stats['profitable'] else 'NO ❌'}\n\n"
                f"**By Setup:**"
            )
            for setup, data in stats.get("setup_breakdown", {}).items():
                total = data["wins"] + data["losses"]
                wr = data["wins"] / total * 100 if total else 0
                reply += f"\n  {setup}: {data['wins']}W/{data['losses']}L ({wr:.0f}% WR) | {data['total_r']:.2f}R"
            return {"reply": reply}
        elif sub == "log":
            # /journal log symbol side entry sl tp size result [pnl] [r_multiple] [setup] [notes]
            if len(parts) < 9:
                return {"reply": "Usage: `/journal log BTC long 50000 49000 52000 0.1 win 1000 2 bull_flag 'great setup'`"}
            try:
                symbol = parts[2].upper()
                side = parts[3].lower()
                entry = float(parts[4])
                sl = float(parts[5])
                tp = float(parts[6])
                size = float(parts[7])
                result_trade = parts[8].lower()
                pnl = float(parts[9]) if len(parts) > 9 else 0
                r_multiple = float(parts[10]) if len(parts) > 10 else 0
                setup_name = parts[11] if len(parts) > 11 else ""
                notes = " ".join(parts[12:]) if len(parts) > 12 else ""
            except (ValueError, IndexError) as e:
                return {"reply": f"Invalid format: {e}"}
            from services.trading import add_trade_journal
            trade = {
                "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp,
                "size": size, "result": result_trade, "pnl": pnl, "r_multiple": r_multiple,
                "setup": setup_name, "notes": notes
            }
            res = add_trade_journal(user_phone, trade)
            return {"reply": f"Trade logged: {symbol} {side.upper()} @ {entry} — {result_trade.upper()} ({r_multiple}R)"}
        return {"reply": "Subcommand must be 'log' or 'stats'"}

    return None

def answer(question: str, sender: str = "cli", user_phone: str | None = None,
           is_roast: bool = False, bot_ids: list[str] = None,
           is_group: bool = False, session_key: str | None = None, visual_b64: str | None = None,
           *, message_id: str | None = None) -> dict | str:
    if any(phrase == question.lower().strip() for phrase in IDENTITY_PHRASES):
        return IDENTITY_REPLY

    name_clean = re.sub(r'[^aA-zZ0-9]', '', question.lower())
    if name_clean in {"crimsonej", "crimson"}:
        return random.choice(["Yeah? What's up? 😎", "I'm here, what do you need?", "Yo!", "Sup?"])

    user_id = user_phone or sender
    profile = profile_mgr.get_profile(user_id)
    is_creator = profile.get("is_creator", False) if profile else False

    chunks, best_score = index.search(question, user_id=user_id, group_id=(sender if is_group else None))
    threshold = cfg("relevance_threshold")
    # Creator bypasses RAG relevance threshold
    if is_creator or best_score >= threshold:
        context = truncate_to_tokens("\n\n".join(chunks), MAX_CONTEXT_TOKENS)
    else:
        context = ""

    # Session key may be a group id or the user phone; prefer explicit session_key
    s_key = session_key or sender
    session = sessions.get(s_key)

    system_prompt = (
        " [SITUATIONAL AWARENESS: You are Crimsonej. Respond naturally and helpfully.]\n\n"
    )
    system_prompt += ROAST_PROMPT if is_roast else cfg("system_prompt")

    # ── Per-user profile context (name, facts, interests, familiarity) ────────
    user_context = profile_mgr.get_context_string(user_id)
    if user_context:
        system_prompt += user_context

    if is_creator:
        system_prompt += "\n\n[CREATOR ACCESS: You are talking to your creator and father, Elijah. You must be respectful, friendly, and helpful. You can refer to him as 'Dad' or 'Elijah'.]\n"

    # ── Group awareness: tell the bot who is talking vs the group ─────────────
    if is_group:
        speaker_name = profile_mgr.get_profile(user_id).get('name') or user_id
        system_prompt += (
            f"\n[GROUP CHAT: You are in a group conversation. "
            f"The person messaging you right now is {speaker_name}. "
            f"You know their name but do NOT use it in every reply — that's weird and robotic. "
            f"Use names only when it's natural, like greeting someone new or calling someone out. "
            f"Talk like a real person in a group chat. "
            f"To tag/mention someone, use @phone_number format.]\n"
        )

    system_prompt += get_vault_context(user_id)
    system_msg = truncate_to_tokens(f"{system_prompt}\nCurrent time: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}", MAX_SYSTEM_TOKENS)

    realtime_context = ""
    doc = doc_session.pop(sender, None) or doc_session.pop(session_key, None)
    if doc:
        realtime_context += f"\n[CURRENT DOCUMENT: {doc['name']}]\n{doc['text'][:5000]}...\n"

    # If there is image/sticker data, ask the vision service to analyze and
    # append a short description to the user content so the LLM sees visual info.
    visual_context = ""
    if visual_b64:
        try:
            desc = vision_svc.analyze_image_with_nvidia(visual_b64, "Describe this image briefly.")
            if not _vision_failed(desc):
                visual_context = f"\n[IMAGE DESCRIPTION]: {desc}\n"
        except Exception:
            visual_context = ""

    user_content = truncate_to_tokens(f"Context:\n{context}{realtime_context}{visual_context}\n\nQuestion: {question}", MAX_USER_MSG_TOKENS)
    history = [{**msg, "content": truncate_to_tokens(msg["content"], MAX_HISTORY_MSG_TOKENS)} for msg in session.messages()]

    messages = [{"role": "system", "content": system_msg}, *history, {"role": "user", "content": user_content}]

    def tool_exec_wrapper(tool_calls, msgs, uid, sjid):
        return execute_tool_calls(tool_calls, msgs, uid, sjid, media_service=media_svc, vision_service=vision_svc)

    # Let the LLM think and adapt naturally. It has full conversation context
    # and can decide when to search, when to ask for clarification, or when to
    # refine a search based on the user's feedback. No hardcoded gates.

    reply = call_llm(messages, tools=ALL_TOOLS, tool_executor_fn=tool_exec_wrapper, user_id=user_id, sender_jid=sender)
    reply_text = reply.get("reply", "") if isinstance(reply, dict) else str(reply)

    # Strip any <think>...</think> or unclosed <think>... blocks that leaked through
    if "<think>" in reply_text:
        reply_text = re.sub(r"<think>.*?</think>", "", reply_text, flags=re.DOTALL)
        reply_text = re.sub(r"<think>.*", "", reply_text, flags=re.DOTALL)
        reply_text = reply_text.strip()
    # Ensure no legacy voice markup remains
    reply_text = re.sub(r'<VOICE>.*?</VOICE>', "", reply_text, flags=re.DOTALL | re.IGNORECASE).strip()
    reply_text = _sanitize_assistant_reply(reply_text)

    # ── Self-correction ──────────────────────────────────────────────────────────
    # Check if the bot's reply contradicts what actually happened (tool failure,
    # phantom task enqueue, etc.). If so, edit or delete the just-sent message.
    correction = verify_and_correct(reply, messages, user_id)
    if correction:
        with _state_lock:
            last = _last_sent.get(s_key)
        if last and last.get("message_id"):
            mid = last["message_id"]
            if correction["action"] == "delete":
                bridge_api.bridge_delete(s_key, mid)
                log.info("[Self-correct] deleted mid=%s jid=%s", mid, s_key.split("@")[0])
            elif correction["action"] == "edit":
                new_text = correction["new_text"]
                bridge_api.bridge_edit(s_key, mid, new_text)
                with _state_lock:
                    if s_key in _last_sent:
                        _last_sent[s_key]["sent_text"] = new_text
                log.info("[Self-correct] edited mid=%s jid=%s", mid, s_key.split("@")[0])
        reply_text = _sanitize_assistant_reply(correction.get("new_text", reply_text))
        if isinstance(reply, dict):
            reply["reply"] = reply_text

    # ── Emoji limiting based on config ────────────────────────────────────────
    try:
        emoji_enabled = bool(cfg("emoji_enabled"))
        if emoji_enabled:
            max_per = int(cfg("emoji_max_per_reply") or 1)
            allow_roast = int(cfg("emoji_allow_in_roast") or 2)
            cap = allow_roast if is_roast else max_per
            reply_text = _limit_emojis(reply_text, cap)
            if isinstance(reply, dict):
                reply["reply"] = reply_text
    except Exception:
        pass

    session.add("user", question, message_id=message_id, ts=time.time())
    session.add("assistant", reply_text)
    # Fire-and-forget: try to extract preferences from this exchange
    try:
        import threading
        from core.llm import scout_quick_call
        from services.memory import extract_preferences_background

        def _pref_task(uid, sample):
            try:
                extract_preferences_background(uid, sample, nvidia_scout_fn=scout_quick_call)
            except Exception:
                pass

        t = threading.Thread(target=_pref_task, args=(user_id or "", question), daemon=True)
        t.start()
    except Exception:
        pass

    return reply

# ── Flask API Routes ─────────────────────────────────────────────────────────
@app.route("/reply", methods=["GET", "POST"])
def route_reply():
    body = request.get_json(silent=True, force=True) or request.form.to_dict() or request.args.to_dict() or {}
    if not body and (raw := request.get_data(as_text=True).strip()):
        body = {"message": raw}

    raw_question = (body.get("message") or body.get("text") or body.get("msg") or body.get("content") or "").strip()
    quoted = (body.get("quoted_message") or body.get("quoted") or "").strip()
    quoted_author = (body.get("quoted_author") or "").strip()
    # JID of the user who sent the message (used for session and message-id mapping)
    sender_jid = (body.get('sender') or 'unknown').strip()
    # Phone number digits for profile lookup etc.
    user_phone = (body.get('user_phone') or body.get('phone') or '').strip()
    if not user_phone and sender_jid != 'unknown':
        # Extract digits from JID like '250203957407887@lid' or '@s.whatsapp.net'
        digits = ''.join(ch for ch in sender_jid if ch.isdigit())
        if digits:
            user_phone = digits
        else:
            user_phone = sender_jid  # fallback
    sender = sender_jid  # use JID as the key for session and _last_sent
    push_name = (body.get('push_name') or "").strip()
    session_id = body.get("group_name") or sender_jid
    is_group = bool(body.get("group_name"))

    log.info("← sender=%s name=%s | msg=%r", sender, push_name or '?', raw_question[:80])

# ── /read & /learn document handling ─────────────────────────────────────
    doc_b64 = body.get("document_data") or ""
    if body.get("document") and doc_b64:
        try:
            decoded = base64.b64decode(doc_b64).decode("utf-8", errors="replace")[:50000]
            doc_session[sender] = {"name": body.get("document_name") or "document", "text": decoded}
            save_doc_sessions()
            log.info("[Read] stored document %s (%d chars) for %s", body.get("document_name") or "document", len(decoded), user_phone)
        except Exception as exc:
            log.warning("[Read] failed to decode document: %s", exc)
            if body.get("read_command"):
                return jsonify({"reply": "Couldn't read that document — is it a text-based file?"}), 200

    if body.get("learn_command"):
        learn_text = ""
        if doc_b64:
            try:
                learn_text = base64.b64decode(doc_b64).decode("utf-8", errors="replace")[:50000]
            except Exception as exc:
                log.warning("[Learn] failed to decode document: %s", exc)
        if not learn_text:
            learn_text = raw_question
        if learn_text.strip():
            from services.memory import learn_task_background
            from functools import partial
            threading.Thread(
                target=learn_task_background,
                args=(user_phone, learn_text, (body.get("document_name") or None) if doc_b64 else None,
                      partial(_call_nvidia, model=NVIDIA_SCOUT)),
                daemon=True,
            ).start()
            log.info("[Learn] queued learning task for %s (%d chars)", user_phone, len(learn_text))
            return jsonify({"reply": "🧠 Got it — I'm adding that to my permanent memory."}), 200
        return jsonify({"reply": ""}), 200

# ── Inbound-edit handling ─────────────────────────────────────────────────
    # When the user edits a message after the bot has replied, the bridge
    # forwards a `messages.update` event here as a POST with edited=true. We
    # patch the most recent user turn in place (keeping the message_id) so the
    # downstream LLM call sees the new text, then re-run the normal reply
    # path. The previous assistant turn stays in the session marked with a
    # `[stale]` prefix so future context is honest.
    edit_replace_message_id = None
    if body.get("edited"):
        try:
            sess = sessions.get(session_id)
            replaced = sess.update_last_user(raw_question)
            if replaced:
                for t in reversed(sess.turns):
                    if t.get("role") == "assistant":
                        c = t.get("content") or ""
                        if not c.startswith("[stale] "):
                            t["content"] = "[stale] " + c
                        break
                log.info("[Edit] patched last user turn in session=%s new_text=%r",
                         session_id, raw_question[:60])
            last = _last_sent.get(session_id)
            if last and last.get("message_id"):
                edit_replace_message_id = str(last["message_id"])
                bridge_api.bridge_edit(session_id, edit_replace_message_id, "...")
                log.info("[Edit] placeholder update mid=%s jid=%s", edit_replace_message_id, session_id.split("@")[0])
        except Exception as exc:
            log.warning("[Edit] session patch failed: %s", exc)

    # ── Inbound-delete handling ───────────────────────────────────────────────
    if body.get("deleted"):
        try:
            sender_msg_id = str(body.get("message_id") or body.get("mid") or "")
            sess = sessions.get(session_id)
            if sender_msg_id:
                for idx in range(len(sess.turns) - 1, -1, -1):
                    turn = sess.turns[idx]
                    if turn.get("role") == "user" and str(turn.get("id") or "") == sender_msg_id:
                        del sess.turns[idx]
                        break
            last = _last_sent.get(session_id)
            if last and last.get("message_id"):
                bridge_api.bridge_delete(session_id, str(last["message_id"]))
                _last_sent.pop(session_id, None)
                pending_song_searches.pop(user_phone, None)
                log.info("[Delete] removed bot response for session=%s mid=%s", session_id.split("@")[0], last.get("message_id"))
            return jsonify({"reply": ""}), 200
        except Exception as exc:
            log.warning("[Delete] cleanup failed: %s", exc)
            return jsonify({"reply": ""}), 200

    # ── Auto-learn contact name & bump interaction count ──────────────────────
    profile_mgr.touch(user_phone, push_name=push_name or None)
    visual_b64 = _visual_payload_base64(body)

    # ── WhatsApp Status (Story) Interception ──────────────────────────────────
    if body.get('is_status'):
        # Check if status replying is enabled by creator config
        if cfg("allow_status_reply") == False:
            return jsonify({"reply": ""}), 200

        # Use the contact's name if we know it
        contact_name = push_name or profile_mgr.get_profile(user_phone).get("name") or user_phone

        status_context = raw_question
        if visual_b64:
            desc = vision_svc.analyze_image_with_nvidia(visual_b64, "Describe this WhatsApp status image briefly.")
            if not _vision_failed(desc):
                status_context = f"{raw_question or '[image status]'}\nVisual context: {desc}"

        prompt = (
            f"You noticed a status update (story) from your contact {contact_name} ({user_phone}): \"{status_context}\".\n"
            "If you want to comment on it, write a short, witty, and personalized comment directly to them. "
            "If you do not want to reply, output exactly 'NONE'. Output ONLY the comment or 'NONE'."
        )
        messages = [
            {"role": "system", "content": "You are Crimsonej. Be natural, witty, and savage if fitting. Do not introduce yourself."},
            {"role": "user", "content": prompt}
        ]
        reply = call_llm(messages)
        reply_text = reply.get("reply", "") if isinstance(reply, dict) else str(reply)
        if reply_text.strip().upper() == "NONE" or not reply_text.strip():
            return jsonify({"reply": ""}), 200
        return jsonify({"reply": reply_text}), 200

    # ── Master Control Overrides ──────────────────────────────────────────────
    if raw_question and raw_question.lower().startswith("master control"):
        profile = profile_mgr.get_profile(user_phone)
        is_creator = profile.get("is_creator", False)

        # ─ Authenticate ───────────────────────────────────────────────────────
        if "master control chela" in raw_question.lower():
            profile["is_creator"] = True
            profile_mgr.save()
            # Also record owner_jid for system-task alerts (bridge-down, etc.)
            try:
                cfg_data = load_json(CFG_FILE, {})
                digits = "".join(c for c in user_phone if c.isdigit())
                if digits:
                    cfg_data["owner_jid"] = f"{digits}@s.whatsapp.net"
                    save_json(CFG_FILE, cfg_data)
                    load_config()
            except Exception as e:
                log.warning("[Auth] could not set owner_jid: %s", e)
            return jsonify({"reply": (
                "Acknowledged, Master Control Chela. 👑\n"
                "Creator override active. Full access granted.\n\n"
                "Commands:\n"
                "• master control status_posting [on/off]\n"
                "• master control status_reply [on/off]\n"
                "• master control scheduler [on/off]\n"
                "• master control interval [hours]\n"
                "• master control status topics\n"
                "• master control topic add [name]\n"
                "• master control topic remove [name]\n"
                "• master control topic clear / list\n"
                "• master control status_now\n"
                "• master control wipe cache\n"
                "• master control wipe memory\n"
                "• master control config"
            )}), 200

        if not is_creator:
            return jsonify({"reply": "🔒 Access denied. Authentication required."}), 200

        from core.config import load_config
        cfg_data = load_json(CFG_FILE, {})
        parts = raw_question.strip().split()
        subcommand = parts[2].lower() if len(parts) >= 3 else ""
        arg1 = parts[3].lower() if len(parts) >= 4 else ""

        if subcommand in ("status_reply", "status_replying", "replies"):
            allowed = arg1 in ("true", "yes", "on", "1")
            cfg_data["allow_status_reply"] = allowed
            save_json(CFG_FILE, cfg_data); load_config()
            return jsonify({"reply": f"✅ Status reply {'enabled' if allowed else 'disabled'}."}), 200

        elif subcommand in ("status_posting", "status_post", "posting"):
            allowed = arg1 in ("true", "yes", "on", "1")
            cfg_data["allow_status_posting"] = allowed
            save_json(CFG_FILE, cfg_data); load_config()
            return jsonify({"reply": f"✅ Status posting {'enabled' if allowed else 'disabled'}."}), 200

        elif subcommand == "scheduler":
            enabled = arg1 in ("true", "yes", "on", "1")
            cfg_data["status_scheduler_enabled"] = enabled
            save_json(CFG_FILE, cfg_data); load_config()
            restart_scheduler()
            return jsonify({"reply": f"✅ Scheduled auto-posting {'started 🟢' if enabled else 'stopped 🔴'}."}), 200

        elif subcommand == "interval":
            try:
                hours = float(arg1)
                if hours < 0.25:
                    return jsonify({"reply": "⚠️ Minimum interval is 0.25h (15 min)."}), 200
                cfg_data["status_scheduler_interval_hours"] = hours
                save_json(CFG_FILE, cfg_data); load_config()
                return jsonify({"reply": f"✅ Posting interval set to {hours}h."}), 200
            except Exception:
                return jsonify({"reply": "⚠️ Usage: master control interval [hours]"}), 200

        elif subcommand in ("topic", "topics"):
            if arg1 in ("add", "remove", "clear", "list"):
                if arg1 == "list":
                    return jsonify({"reply": "Topics: " + (", ".join(cfg_data.get("status_scheduler_topics", [])) if cfg_data.get("status_scheduler_topics") else "none")}), 200
                if arg1 == "clear":
                    cfg_data["status_scheduler_topics"] = []
                    save_json(CFG_FILE, cfg_data); load_config()
                    return jsonify({"reply": "✅ Topic list cleared."}), 200
                if len(parts) >= 5:
                    topic = " ".join(parts[4:]).strip()
                    if arg1 == "add":
                        topics = cfg_data.setdefault("status_scheduler_topics", [])
                        if topic not in topics:
                            topics.append(topic)
                        save_json(CFG_FILE, cfg_data); load_config()
                        return jsonify({"reply": f"✅ Topic added: {topic}"}), 200
                    if arg1 == "remove":
                        topics = cfg_data.get("status_scheduler_topics", [])
                        cfg_data["status_scheduler_topics"] = [t for t in topics if t != topic]
                        save_json(CFG_FILE, cfg_data); load_config()
                        return jsonify({"reply": f"✅ Topic removed: {topic}"}), 200
            return jsonify({"reply": "⚠️ Usage: master control topic add/remove/clear/list [topic]"}), 200

        elif subcommand == "status_now":
            trigger_now()
            return jsonify({"reply": "✅ Status trigger sent."}), 200

        elif subcommand == "config":
            return jsonify({"reply": (
                f"allow_status_reply={cfg_data.get('allow_status_reply', True)}\n"
                f"allow_status_posting={cfg_data.get('allow_status_posting', True)}\n"
                f"status_scheduler_enabled={cfg_data.get('status_scheduler_enabled', False)}\n"
                f"interval_hours={cfg_data.get('status_scheduler_interval_hours', 4)}\n"
                f"topics={cfg_data.get('status_scheduler_topics', [])}"
            )}), 200

        elif subcommand == "wipe" or "master control wipe" in raw_question.lower():
            target_type = arg1 if arg1 in ("cache", "memory") else ("cache" if "cache" in raw_question.lower() else "memory" if "memory" in raw_question.lower() else "")

            if target_type == "cache":
                freed_bytes = 0
                file_count = 0
                import glob
                temp_patterns = [
                    "/dev/shm/song_*", "/dev/shm/stk_*", "/dev/shm/nv_*", "/dev/shm/hf_*", "/dev/shm/orig_*", "/dev/shm/*_whatsapp.*", "/dev/shm/*_aac.*",
                    "/tmp/song_*", "/tmp/stk_*", "/tmp/nv_*", "/tmp/hf_*", "/tmp/orig_*", "/tmp/*_whatsapp.*", "/tmp/*_aac.*"
                ]
                for pat in temp_patterns:
                    for p in glob.glob(pat):
                        try:
                            size = os.path.getsize(p)
                            os.remove(p)
                            freed_bytes += size
                            file_count += 1
                        except Exception:
                            pass

                from services.tasks import task_store
                cleared_tasks = 0
                with task_store._lock:
                    retained = {}
                    for tid, t in task_store._tasks.items():
                        if t.get("status") in ("pending", "running"):
                            retained[tid] = t
                        else:
                            cleared_tasks += 1
                    task_store._tasks = retained
                    task_store._save_locked()

                if os.path.exists(CACHE_FILE):
                    try: save_json(CACHE_FILE, {})
                    except Exception: pass

                with _state_lock:
                    pending_song_searches.clear()
                    doc_session.clear()
                    _last_sent.clear()

                freed_mb = round(freed_bytes / (1024 * 1024), 2)
                return jsonify({"reply": (
                    f"🧹 Master Control Cache Wipe Completed! ✨\n"
                    f"• Cleaned {file_count} temporary media files ({freed_mb} MB reclaimed)\n"
                    f"• Cleared {cleared_tasks} completed/failed tasks\n"
                    f"• Flushed transient search & document sessions"
                )}), 200

            elif target_type == "memory":
                profile_mgr.profiles = {}
                profile_mgr.save()

                sessions._store = {}
                sessions.save()

                with _state_lock:
                    doc_session.clear()
                save_json(DOC_SESSIONS_FILE, {})

                from services.tasks import task_store
                with task_store._lock:
                    task_store._tasks = {}
                    task_store._save_locked()

                for path in (EVENTS_FILE, VECTORS_FILE, CACHE_FILE):
                    if os.path.exists(path):
                        try: os.remove(path)
                        except Exception: pass

                if os.path.exists(VAULTS_DIR):
                    import glob
                    for f in glob.glob(os.path.join(VAULTS_DIR, "*")):
                        try: os.remove(f)
                        except Exception: pass

                try:
                    from services.rag import _load_vectors
                    _load_vectors()
                except Exception:
                    pass

                p = profile_mgr.get_profile(user_phone)
                p["is_creator"] = True
                profile_mgr.save()

                return jsonify({"reply": (
                    "💥 MASTER CONTROL FULL MEMORY WIPE COMPLETE! 👑\n\n"
                    "Crimsonej has been reset to original factory state:\n"
                    "• All user profiles erased\n"
                    "• All conversation histories erased\n"
                    "• All permanent memory vaults erased\n"
                    "• All vector RAG knowledge erased\n"
                    "• All background task logs erased\n\n"
                    "Creator authentication preserved."
                )}), 200

            return jsonify({"reply": "⚠️ Usage: master control wipe cache  OR  master control wipe memory"}), 200

        return jsonify({"reply": "⚠️ Unknown master control command."}), 200

    # Handle pending numbered song picks safely & asynchronously.
    pending = None
    with _state_lock:
        if user_phone in pending_song_searches and raw_question.strip().isdigit():
            pending = pending_song_searches.pop(user_phone, None)

    if pending and raw_question.strip().isdigit():
        idx = int(raw_question.strip()) - 1
        results = pending.get("results", [])
        if 0 <= idx < len(results):
            choice = results[idx]
            url = choice.get("url") or ""
            if url:
                mtype = pending.get("type", "audio")
                title_short = (choice.get("title") or "")[:30]
                from services.tasks import task_store
                from core.eventlog import event_log
                t = task_store.create(
                    kind="background",
                    name=f"download_{mtype}",
                    action={
                        "module": "services.media",
                        "fn": "download_youtube_task",
                        "kwargs": {
                            "url": url,
                            "media_type": mtype,
                            "owner_jid": sender,
                            "owner_user_id": user_phone,
                            "task_id": "TBD",
                        },
                        "progress_label": f"🎬 downloading {title_short}" if mtype == "video" else f"🎵 downloading {title_short}",
                    },
                    owner_user_id=user_phone or "",
                    owner_jid=sender or "",
                    notify_on="done",
                    metadata={"url": url, "title": choice.get("title"), "media_type": mtype},
                )
                event_log.append("tool", "task_enqueued",
                                 summary=f"{mtype} download task #{t['id']} queued for '{choice.get('title')}'",
                                 user_id=user_phone or None, jid=sender or None,
                                 payload={"task_id": t["id"], "title": choice.get("title"), "kind": f"download_{mtype}"})
                return jsonify({"reply": f"on it 🎬 (task #{t['id']})" if mtype == "video" else f"on it 🎵 (task #{t['id']})"}), 200
        return jsonify({"reply": "That option isn’t valid. Try a number from the list."}), 200

    # Regular slash commands.
    command_reply = handle_commands(raw_question, user_phone, session_id, quoted)
    if command_reply:
        return jsonify(command_reply), 200

    # General bot reply.
    if raw_question:
        try:
            model_reply = answer(
                raw_question,
                sender=sender,
                user_phone=user_phone,
                is_roast=is_roast_request(raw_question, quoted),
                bot_ids=[],
                is_group=is_group,
                session_key=session_id,
                visual_b64=visual_b64,
                message_id=body.get("message_id") or body.get("mid"),
            )
            if isinstance(model_reply, dict):
                reply_payload = {"reply": model_reply.get("reply", "")}
                if model_reply.get("image"):
                    reply_payload["image"] = model_reply["image"]
                if model_reply.get("sticker"):
                    reply_payload["sticker"] = model_reply["sticker"]
                if model_reply.get("audio"):
                    reply_payload["audio"] = model_reply["audio"]
                if model_reply.get("video"):
                    reply_payload["video"] = model_reply["video"]
                if body.get("edited") and edit_replace_message_id:
                    reply_payload["edit_mode"] = True
                    reply_payload["replace_message_id"] = edit_replace_message_id
                return jsonify(reply_payload), 200
            if body.get("edited") and edit_replace_message_id:
                return jsonify({"reply": _sanitize_assistant_reply(str(model_reply)), "edit_mode": True, "replace_message_id": edit_replace_message_id}), 200
            return jsonify({"reply": _sanitize_assistant_reply(str(model_reply))}), 200
        except Exception as exc:
            _notify_raw_error(exc, context=f"route_reply user={user_phone} sender={sender}", user_phone=user_phone)
            return jsonify({"reply": "I hit a snag on my end — give me a sec and I’ll sort it."}), 200

    return jsonify({"reply": ""}), 200


@app.route("/sent_ids", methods=["POST"])
def route_sent_ids():
    body = request.get_json(silent=True, force=True) or request.form.to_dict() or {}
    sender = (body.get("jid") or body.get("sender") or body.get("phone") or "unknown").strip()
    message_ids = body.get("message_ids") or []
    message_id = body.get("message_id") or body.get("mid")
    if isinstance(message_ids, list) and message_ids:
        message_id = message_ids[-1]
    sent_text = body.get("sent_text") or body.get("text") or body.get("message") or ""
    if sender and message_id:
        with _state_lock:
            _last_sent[sender] = {"message_id": str(message_id), "sent_text": str(sent_text), "sent_at": time.time()}
    return jsonify({"ok": True}), 200


def _sanitize_assistant_reply(reply_text: str | None) -> str:
    """Strip think tags, reject fake tool payloads and placeholder links before sending."""
    if not isinstance(reply_text, str):
        reply_text = str(reply_text or "")
    text = reply_text.strip()
    if not text:
        return ""

    # Strip any <think>...</think> or unclosed <think>... blocks
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        text = text.strip()

    lower = text.lower()
    if re.match(r'^\s*\{.*?"name"\s*:\s*".*?".*?"parameters"\s*:\s*\{', text, re.DOTALL):
        return "I'm not meant to send raw tool data. Tell me the exact track/version and I'll sort it cleanly."
    if "example.com" in lower or "audio-download-link" in lower or "video-download-link" in lower:
        return "I sent the wrong thing there. Tell me the exact track/version and I'll do it properly."
    if "download_video function" in lower or "download_audio function" in lower:
        return "I'm not supposed to expose the tool call. Tell me the exact track/version and I'll sort it cleanly."
    return text


import traceback


def _master_control_jids(user_phone: str | None = None) -> list[str]:
    """Return owner/master-control JIDs that should receive raw technical errors."""
    out: set[str] = set()
    owner_jid = (cfg("owner_jid") or "").strip()
    if owner_jid:
        out.add(owner_jid)

    for phone, profile in profile_mgr.profiles.items():
        if profile.get("is_creator"):
            digits = "".join(ch for ch in str(phone) if ch.isdigit())
            if digits:
                out.add(f"{digits}@s.whatsapp.net")

    if user_phone:
        digits = "".join(ch for ch in str(user_phone) if ch.isdigit())
        if digits:
            out.add(f"{digits}@s.whatsapp.net")

    return sorted(out)


def _notify_raw_error(error: Exception, *, context: str = "", user_phone: str | None = None) -> None:
    """Send the raw traceback to owner/master-control recipients without leaking it to the active chat."""
    try:
        tb = traceback.format_exc()
        payload = (
            f"[CRIMSONEJ_ERROR]\n"
            f"context={context}\n"
            f"user={user_phone or 'unknown'}\n"
            f"error={type(error).__name__}: {error}\n\n"
            f"{tb[:4000]}"
        )
        # dedupe by fingerprint of the traceback to avoid spamming owner
        try:
            import hashlib as _hashlib
            fp = _hashlib.sha256(tb.encode('utf-8', errors='ignore')).hexdigest()
            now = time.time()
            last = _error_notify_cache.get(fp)
            if last and (now - last) < _ERROR_NOTIFY_DEDUPE_SEC:
                return
            _error_notify_cache[fp] = now
        except Exception:
            pass

        for jid in _master_control_jids(user_phone):
            try:
                bridge_api.bridge_send(jid, payload)
            except Exception as send_exc:
                log.warning("[ErrorNotify] failed for %s: %s", jid, send_exc)
    except Exception as exc:
        log.warning("[ErrorNotify] failed to deliver raw error: %s", exc)


@app.errorhandler(Exception)
def _global_error_handler(exc: Exception):
    """Keep the active chat in-character while forwarding raw technical details to owners."""
    try:
        user_phone = None
        try:
            payload = request.get_json(silent=True, force=True) or {}
        except Exception:
            payload = {}
        user_phone = payload.get("user_phone") or payload.get("phone") or payload.get("sender") or None
        _notify_raw_error(exc, context=f"route={request.path} method={request.method}", user_phone=user_phone)
    except Exception:
        pass

    if request.path.endswith("/reply") or request.path.endswith("reply") or "/reply" in request.path:
        return jsonify({"reply": "I hit a snag on my end — give me a sec and I’ll sort it."}), 200
    return jsonify({"error": "internal_error"}), 500


@app.route('/health', methods=['GET'])
def route_health():
    try:
        from services.health import last_status, get_status
        s = last_status() or get_status()
        return jsonify(s), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # Start auxiliary background services
    try:
        start_dispatcher()
    except Exception as e:
        log.warning("[Boot] Failed to start dispatcher: %s", e)
    try:
        from services.progress_sweeper import start_sweeper
        start_sweeper()
    except Exception:
        pass
    try:
        from services.health import start_heartbeat
        start_heartbeat()
    except Exception:
        pass
    try:
        start_reporter()
    except Exception:
        pass
    try:
        from services.trading_scheduler import start_briefing_scheduler
        start_briefing_scheduler()
    except Exception as e:
        log.warning("[Boot] Failed to start trading briefing scheduler: %s", e)
    app.run(host="0.0.0.0", port=cfg("port"), threaded=True)
