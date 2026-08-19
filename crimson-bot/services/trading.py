"""
services/trading.py
===================
Trading coach orchestrator. Handles market analysis, teaching, watchlists,
and daily briefings. Zero dependencies on existing bot code — purely additive.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

from core.config import BASE_DIR, cfg, log, TZ
from core.market_data import get_price, get_klines, normalize_symbol
from core.trading_ta import compute_bias, momentum_summary, last_candle_summary, key_levels
from core.chart_render import render_chart, cleanup_old_charts

_DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(_DATA_DIR, exist_ok=True)

_LESSONS_FILE = os.path.join(_DATA_DIR, "trading_lessons.json")
_WATCHLISTS_FILE = os.path.join(_DATA_DIR, "user_watchlists.json")

_lessons_cache: dict[str, Any] = {}
_watchlists_cache: dict[str, list[str]] = {}
_locks = {"lessons": threading.Lock(), "watchlists": threading.Lock()}


# ── Lessons ───────────────────────────────────────────────────────────────────

DEFAULT_LESSONS = {
    "candlesticks": {
        "title": "Reading Candlesticks",
        "level": "beginner",
        "tags": ["price action", "basics"],
        "content": (
            "A candle shows 4 prices: open, high, low, close.\n\n"
            "**Body** = open to close. Green = close > open (bullish). Red = close < open (bearish).\n"
            "**Wicks** = rejection. Upper wick = sellers pushed price down. Lower wick = buyers pushed price up.\n\n"
            "**Key patterns:**\n"
            "• Doji (tiny body) = indecision\n"
            "• Hammer (long lower wick, small body at top) = bullish rejection at support\n"
            "• Shooting star (long upper wick, small body at bottom) = bearish rejection at resistance\n"
            "• Engulfing = second candle body fully covers first = strong shift\n\n"
            "**Rule:** Context > pattern. A hammer at resistance means nothing. A hammer at support after a downtrend = potential reversal."
        ),
    },
    "structure": {
        "title": "Market Structure (Trends & Ranges)",
        "level": "beginner",
        "tags": ["price action", "trends"],
        "content": (
            "Price only does 3 things: trend up, trend down, or range.\n\n"
            "**Uptrend** = Higher Highs (HH) + Higher Lows (HL).\n"
            "**Downtrend** = Lower Highs (LH) + Lower Lows (LL).\n"
            "**Range** = Equal highs/lows. Price ping-pongs between support & resistance.\n\n"
            "**Trend confirmation:** Need 2+ HH/HL or LH/LL. One HH means nothing.\n"
            "**Trend break:** Uptrend breaks when price makes a LL. Downtrend breaks on HH.\n"
            "**Retest:** After break, price often retests the broken level. That's your entry zone.\n\n"
            "**Rule:** Trade WITH the trend on your timeframe. Counter-trend = lower probability."
        ),
    },
    "support_resistance": {
        "title": "Support & Resistance",
        "level": "beginner",
        "tags": ["price action", "levels"],
        "content": (
            "S/R are price levels where buyers/sellers repeatedly step in.\n\n"
            "**Support** = floor where buyers absorb selling. Old resistance becomes new support.\n"
            "**Resistance** = ceiling where sellers absorb buying. Old support becomes new resistance.\n\n"
            "**Finding them:**\n"
            "• Swing highs/lows (pivots)\n"
            "• Psychological levels (round numbers: 50k, 100k)\n"
            "• Moving averages (EMA20, EMA50, EMA200)\n"
            "• Volume profile POC (point of control)\n\n"
            "**Strength test:** More touches = stronger level (but also more depleted).\n"
            "**Break vs fakeout:** Wait for candle CLOSE beyond level + volume. Wick through = fakeout."
        ),
    },
    "risk_management": {
        "title": "Risk Management & Position Sizing",
        "level": "essential",
        "tags": ["risk", "psychology"],
        "content": (
            "This is the only lesson that keeps you alive.\n\n"
            "**1. Risk per trade:** 1-2% of account MAX. Not 5%, not 10%. 1-2%.\n"
            "**2. Position size:** (Account × Risk%) / (Entry - Stop Loss).\n"
            "   Example: $10k account, 1% risk ($100), entry $50k, SL $49k → size = $100 / $1k = 0.1 BTC.\n"
            "**3. R:R (Reward:Risk):** Minimum 2:1. Ideally 3:1+. If risk $100, target ≥$200.\n"
            "**4. Max drawdown rule:** If down 10% from peak → cut size in half. Down 20% → stop, review.\n"
            "**5. Never add to losers.** Only add to winners (pyramiding) with trailing stops.\n\n"
            "**Math:** 50% win rate + 2:1 R:R = profitable. 40% win rate + 3:1 R:R = profitable. 30% win rate + 1:1 = broke."
        ),
    },
    "rsi": {
        "title": "RSI — What It Actually Tells You",
        "level": "intermediate",
        "tags": ["indicators", "momentum"],
        "content": (
            "RSI measures speed of price change. 0-100 scale.\n\n"
            "**>70** = overbought (not 'sell now'). In strong uptrends, RSI stays >70 for weeks.\n"
            "**<30** = oversold (not 'buy now'). In crashes, RSI stays <30.\n"
            "**50** = neutral/momentum shift line.\n\n"
            "**Real uses:**\n"
            "• **Divergence:** Price makes LL, RSI makes HL = momentum fading = potential reversal.\n"
            "• **Range:** In ranges, buy <30, sell >70 works.\n"
            "• **Trend filter:** In uptrend, only buy dips to 40-50. Ignore >70.\n\n"
            "**Rule:** RSI is a momentum gauge, not a reversal signal. Context is everything."
        ),
    },
    "macd": {
        "title": "MACD — Trend & Momentum",
        "level": "intermediate",
        "tags": ["indicators", "momentum"],
        "content": (
            "MACD = EMA12 - EMA26. Signal = EMA9 of MACD. Histogram = MACD - Signal.\n\n"
            "**Crossovers:** MACD crosses above signal = bullish momentum. Below = bearish.\n"
            "**Zero line:** MACD > 0 = short-term > long-term (uptrend). < 0 = downtrend.\n"
            "**Histogram:** Expanding = momentum accelerating. Contracting = fading.\n"
            "**Divergence:** Same as RSI — price LL, MACD HL = fading downtrend.\n\n"
            "**Best use:** MACD on higher timeframe (4h, 1d) for trend bias. Lower TF for entries."
        ),
    },
    "moving_averages": {
        "title": "Moving Averages (EMA20, 50, 200)",
        "level": "intermediate",
        "tags": ["indicators", "trend"],
        "content": (
            "EMAs weight recent price more. SMAs weight equally.\n\n"
            "**EMA20** = short-term trend (2-4 weeks). Price > EMA20 = bullish bias.\n"
            "**EMA50** = medium-term (2-3 months). Golden cross (20>50) = bullish shift.\n"
            "**EMA200** = long-term (8-10 months). The 'institutional line'. Price > 200 = bull market.\n\n"
            "**Dynamic S/R:** In trends, EMAs act as support/resistance.\n"
            "**Stacking:** EMA20 > 50 > 200 = strong uptrend. 200 > 50 > 20 = strong downtrend.\n"
            "**Slope matters:** Flat EMAs = range. Steep EMAs = strong trend."
        ),
    },
    "volume": {
        "title": "Reading Volume",
        "level": "intermediate",
        "tags": ["volume", "confirmation"],
        "content": (
            "Volume = fuel. Price without volume = hollow move.\n\n"
            "**Breakout + high volume** = conviction. Real.\n"
            "**Breakout + low volume** = trap. Fakeout likely.\n"
            "**Volume climax** = huge volume at extreme = exhaustion = potential reversal.\n"
            "**Volume dry-up** = declining volume in trend = momentum fading.\n"
            "**Volume profile POC** = price where most volume traded = fair value magnet.\n\n"
            "**Rule:** Volume confirms. No volume = no trust."
        ),
    },
    "multi_timeframe": {
        "title": "Multi-Timeframe Analysis",
        "level": "advanced",
        "tags": ["process", "analysis"],
        "content": (
            "Never trade on one timeframe. Always check 3:\n\n"
            "**HTF (1D/1W)** = bias, major S/R, trend direction. This is your map.\n"
            "**MTF (4H)** = structure, swing points, entry zones. This is your plan.\n"
            "**LTF (15m/1H)** = trigger, precise entry, SL placement. This is your execution.\n\n"
            "**Workflow:**\n"
            "1. HTF: 'Daily is uptrend, price at support, bias bullish.'\n"
            "2. MTF: '4H shows HH/HL holding, pullback to EMA20.'\n"
            "3. LTF: '1H prints bullish engulfing at EMA20. Enter.'\n\n"
            "**Rule:** HTF bias dictates direction. LTF only times the entry."
        ),
    },
    "liquidity": {
        "title": "Liquidity & Stop Hunts",
        "level": "advanced",
        "tags": ["price action", "smart money"],
        "content": (
            "Liquidity = where stops cluster. Smart money hunts it.\n\n"
            "**Buy-side liquidity** = stops above swing highs (shorts' SLs).\n"
            "**Sell-side liquidity** = stops below swing lows (longs' SLs).\n\n"
            "**Common patterns:**\n"
            "• **Liquidity sweep:** Price spikes above high, closes back inside = sweep + reversal.\n"
            "• **Stop run:** Quick wick through level, immediate reclaim = engineered.\n"
            "• **Inducement:** Obvious level (e.g. double top) = trap. Real break = clean close.\n\n"
            "**Your edge:** Don't put stops at obvious levels. Put them where the thesis breaks."
        ),
    },
    "journaling": {
        "title": "Trade Journaling — The Fastest Way to Improve",
        "level": "essential",
        "tags": ["psychology", "process"],
        "content": (
            "Journal every trade. Not P&L. The DECISION.\n\n"
            "**Log this for every trade:**\n"
            "1. Setup name (e.g. '4H pullback to EMA20 in uptrend')\n"
            "2. Entry / SL / Target (planned)\n"
            "3. Actual entry / exit\n"
            "4. What you saw (chart snapshot link)\n"
            "5. How you FELT before/during/after\n"
            "6. What you'd do differently\n\n"
            "**Weekly review:** Filter by setup. Find your edge. Kill what loses.\n"
            "**Monthly:** Calculate expectancy = (Win% × Avg Win) - (Loss% × Avg Loss).\n"
            "**If expectancy ≤ 0 → stop trading live. Paper trade until positive.**"
        ),
    },
    "psychology": {
        "title": "Trading Psychology — The Real Game",
        "level": "essential",
        "tags": ["psychology", "mindset"],
        "content": (
            "Your brain is wired to lose at trading.\n\n"
            "**FOMO** = chasing entries. Fix: wait for your setup. Missed trade > bad trade.\n"
            "**Revenge trading** = sizing up after loss to 'make it back'. Fix: hard daily loss limit.\n"
            "**Overconfidence** = winning streak → size up → give it all back. Fix: fixed % risk.\n"
            "**Analysis paralysis** = too many indicators, no action. Fix: 3-factor checklist.\n"
            "**Fear of pulling trigger** = missed winners. Fix: pre-define entry, size, SL. Execute robotically.\n\n"
            "**Best traders:** Boring. Same process. Same risk. Same review. No ego."
        ),
    },
    "market_sessions": {
        "title": "Forex/Crypto Sessions & Volatility",
        "level": "intermediate",
        "tags": ["sessions", "volatility"],
        "content": (
            "Markets have rhythm. Know when YOUR pair moves.\n\n"
            "**Forex (UTC):**\n"
            "• **Sydney** 22:00-07:00 — thin, ranges\n"
            "• **Tokyo** 00:00-09:00 — JPY/AUD/NZD active\n"
            "• **London** 08:00-17:00 — BIG volume, trends start\n"
            "• **New York** 13:00-22:00 — USD pairs, overlap with London = peak vol\n"
            "• **Overlap 13:00-17:00** = highest liquidity, best trends\n\n"
            "**Crypto:** 24/7 but volume follows:\n"
            "• Asia hours (00-08 UTC) = often range\n"
            "• London open (08:00) = directional moves\n"
            "• NY open (13:00) = continuation/reversal\n"
            "• Weekend = low vol, chop\n\n"
            "**Rule:** Trade YOUR session. Don't force moves in dead hours."
        ),
    },
}


def load_lessons() -> dict:
    global _lessons_cache
    with _locks["lessons"]:
        if _lessons_cache:
            return _lessons_cache
        if os.path.exists(_LESSONS_FILE):
            try:
                with open(_LESSONS_FILE, "r", encoding="utf-8") as f:
                    _lessons_cache = json.load(f)
            except Exception:
                _lessons_cache = DEFAULT_LESSONS
        else:
            _lessons_cache = DEFAULT_LESSONS
        return _lessons_cache


def save_lessons() -> None:
    with _locks["lessons"]:
        with open(_LESSONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_lessons_cache, f, indent=2)


def get_lesson(topic: str) -> dict | None:
    lessons = load_lessons()
    return lessons.get(topic.lower())


def list_lessons() -> list[dict]:
    lessons = load_lessons()
    return [{"topic": k, "title": v["title"], "level": v["level"], "tags": v.get("tags", [])} for k, v in lessons.items()]


# ── Watchlists ────────────────────────────────────────────────────────────────

def load_watchlists() -> dict:
    global _watchlists_cache
    with _locks["watchlists"]:
        if _watchlists_cache:
            return _watchlists_cache
        if os.path.exists(_WATCHLISTS_FILE):
            try:
                with open(_WATCHLISTS_FILE, "r", encoding="utf-8") as f:
                    _watchlists_cache = json.load(f)
            except Exception:
                _watchlists_cache = {}
        return _watchlists_cache


def save_watchlists() -> None:
    with _locks["watchlists"]:
        with open(_WATCHLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_watchlists_cache, f, indent=2)


def get_watchlist(user_id: str) -> list[str]:
    wl = load_watchlists()
    return wl.get(user_id, [])


def add_to_watchlist(user_id: str, symbol: str) -> bool:
    norm = normalize_symbol(symbol)
    if norm["kind"] == "unknown":
        return False
    canonical = norm["canonical"]
    wl = load_watchlists()
    user_wl = wl.setdefault(user_id, [])
    if canonical not in user_wl:
        user_wl.append(canonical)
        save_watchlists()
    return True


def remove_from_watchlist(user_id: str, symbol: str) -> bool:
    norm = normalize_symbol(symbol)
    canonical = norm["canonical"]
    wl = load_watchlists()
    if user_id in wl and canonical in wl[user_id]:
        wl[user_id].remove(canonical)
        save_watchlists()
        return True
    return False


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_symbol(symbol: str, interval: str = "1h", limit: int = 200) -> dict:
    """Full analysis: fetch data, compute TA, render chart, return bias + chart."""
    norm = normalize_symbol(symbol)
    if norm["kind"] == "unknown":
        return {"error": f"Unknown symbol: {symbol}"}

    display = norm["display"]
    candles = get_klines(symbol, interval, limit)
    if not candles or len(candles) < 30:
        return {"error": f"Not enough data for {display} on {interval}"}

    bias_result = compute_bias(candles)
    mom = momentum_summary(candles)
    levels = key_levels(candles)
    last_candle = last_candle_summary(candles[-1])

    # Render chart
    indicators = ["ema", "volume", "rsi", "macd", "bb"]
    chart_path = render_chart(
        display, candles, interval,
        indicators=indicators,
        levels=levels,
        bias=bias_result["bias"],
        bias_confidence=bias_result["confidence"],
        title_suffix=f"analyzed {datetime.now(TZ).strftime('%H:%M %Z')}",
    )

    return {
        "symbol": display,
        "kind": norm["kind"],
        "interval": interval,
        "price": candles[-1]["c"],
        "bias": bias_result["bias"],
        "confidence": bias_result["confidence"],
        "reasons": bias_result["reasons"],
        "structure": bias_result["structure"].get("trend", "unknown"),
        "levels": levels,
        "momentum": mom,
        "last_candle": last_candle,
        "chart_path": chart_path,
        "candles_used": len(candles),
    }


def quick_price_check(symbols: list[str]) -> list[dict]:
    """Fast price check for multiple symbols."""
    results = []
    for sym in symbols:
        price_data = get_price(sym)
        results.append(price_data)
    return results


# ── Daily Briefing ────────────────────────────────────────────────────────────

def generate_daily_briefing(session: str = "pre_london") -> dict:
    """Generate educational daily briefing.

    session: 'pre_london' (07:30 UTC) or 'eod' (21:30 UTC)
    """
    now = datetime.now(TZ)
    major_pairs = ["BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "GOLD", "SPX", "DXY", "OIL"]
    analyses = []

    for sym in major_pairs:
        res = analyze_symbol(sym, "4h", 100)
        if "error" not in res:
            analyses.append({
                "symbol": res["symbol"],
                "bias": res["bias"],
                "confidence": res["confidence"],
                "price": res["price"],
                "trend": res["structure"],
            })

    if session == "pre_london":
        header = "☀️ **Pre-London Brief**"
        focus = "What Asia did, London levels to watch, today's calendar"
    else:
        header = "🌙 **End-of-Day Wrap**"
        focus = "What moved today, why, tomorrow's setup"

    lines = [header, f"*{now.strftime('%a %d %b %Y %H:%M %Z')}* — {focus}", ""]
    for a in analyses:
        icon = "🟢" if a["bias"] == "bullish" else ("🔴" if a["bias"] == "bearish" else "⏸️")
        lines.append(f"{icon} **{a['symbol']}** {a['price']:,.2f} — {a['bias'].upper()} ({a['confidence']}%) | {a['trend']}")

    lines.append("")
    lines.append("**Key themes:**")
    if session == "pre_london":
        lines.append("• Asia session range = London breakout zones")
        lines.append("• Watch for sweep of Asia highs/lows at 08:00 UTC")
        lines.append("• DXY direction sets risk tone for the day")
    else:
        lines.append("• NY close often reverses London trend")
        lines.append("• Note which pairs respected/ignored key levels")
        lines.append("• Tomorrow's calendar: check for CPI/FOMC/earnings")

    lines.append("")
    lines.append("_Bias = educational lean, not a signal. You decide._")

    return {
        "session": session,
        "text": "\n".join(lines),
        "analyses": analyses,
        "timestamp": now.isoformat(),
    }


def post_briefing_to_owner(bridge_api, session: str = "pre_london") -> bool:
    """Post daily briefing to owner's JID via bridge."""
    try:
        from core.config import load_json, CFG_FILE
        cfg_data = load_json(CFG_FILE, {})
        owner_jid = cfg_data.get("owner_jid", "")
        if not owner_jid:
            log.warning("[Trading] No owner_jid configured for briefing")
            return False

        brief = generate_daily_briefing(session)
        bridge_api.bridge_send(owner_jid, brief["text"])
        log.info("[Trading] Posted %s briefing to owner", session)
        return True
    except Exception as exc:
        log.error("[Trading] Failed to post briefing: %s", exc)
        return False


# ── Group Subscriptions ────────────────────────────────────────────────────────
# Max topics per subscription to keep briefings concise
MAX_BRIEFING_TOPICS = 15

_SUBSCRIPTIONS_FILE = os.path.join(_DATA_DIR, "briefing_subscriptions.json")
_subscriptions_lock = threading.Lock()
_subscriptions_cache: dict[str, dict] = {}  # group_jid -> {sessions, topics, enabled, added_by, added_at}


def _load_subscriptions() -> dict:
    global _subscriptions_cache
    with _subscriptions_lock:
        if _subscriptions_cache:
            return _subscriptions_cache
        if os.path.exists(_SUBSCRIPTIONS_FILE):
            try:
                with open(_SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                    _subscriptions_cache = json.load(f)
            except Exception:
                _subscriptions_cache = {}
        return _subscriptions_cache


def _save_subscriptions() -> None:
    with _subscriptions_lock:
        with open(_SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_subscriptions_cache, f, indent=2)


def get_subscription(group_jid: str) -> dict | None:
    """Get subscription config for a group."""
    subs = _load_subscriptions()
    return subs.get(group_jid)


def subscribe_group(group_jid: str, user_id: str, sessions: list[str] = None, topics: list[str] = None) -> dict:
    """Subscribe a group to daily briefings.

    sessions: ['pre_london', 'eod'] (default both)
    topics: list of symbols (e.g. ['BTC', 'ETH', 'EURUSD', 'GOLD']) - max MAX_BRIEFING_TOPICS
    """
    subs = _load_subscriptions()

    # Validate sessions
    valid_sessions = {"pre_london", "eod"}
    sessions = sessions or ["pre_london", "eod"]
    sessions = [s for s in sessions if s in valid_sessions]
    if not sessions:
        sessions = ["pre_london", "eod"]

    # Validate topics - just format check, no network calls
    if topics:
        topics = [t.upper().strip() for t in topics]
        topics = topics[:MAX_BRIEFING_TOPICS]
    else:
        topics = []

    sub = {
        "sessions": sessions,
        "topics": topics,
        "enabled": True,
        "added_by": user_id,
        "added_at": datetime.now(TZ).isoformat(),
    }
    subs[group_jid] = sub
    _save_subscriptions()
    return {"ok": True, "subscription": sub}


def unsubscribe_group(group_jid: str) -> dict:
    """Unsubscribe a group from briefings."""
    subs = _load_subscriptions()
    if group_jid in subs:
        del subs[group_jid]
        _save_subscriptions()
        return {"ok": True, "message": "Unsubscribed"}
    return {"ok": False, "message": "Not subscribed"}


def list_subscriptions() -> list[dict]:
    """List all active subscriptions."""
    subs = _load_subscriptions()
    return [{"group_jid": k, **v} for k, v in subs.items() if v.get("enabled")]


def generate_custom_briefing(session: str, topics: list[str]) -> dict:
    """Generate briefing for specific topics only."""
    if not topics:
        return generate_daily_briefing(session)

    now = datetime.now(TZ)
    analyses = []

    for sym in topics:
        res = analyze_symbol(sym, "4h", 100)
        if "error" not in res:
            analyses.append({
                "symbol": res["symbol"],
                "bias": res["bias"],
                "confidence": res["confidence"],
                "price": res["price"],
                "trend": res["structure"],
            })

    if session == "pre_london":
        header = "☀️ **Pre-London Brief (Custom)**"
        focus = "Your selected pairs — Asia recap, London levels"
    else:
        header = "🌙 **End-of-Day Wrap (Custom)**"
        focus = "Your selected pairs — today's moves, tomorrow's setup"

    lines = [header, f"*{now.strftime('%a %d %b %Y %H:%M %Z')}* — {focus}", ""]
    for a in analyses:
        icon = "🟢" if a["bias"] == "bullish" else ("🔴" if a["bias"] == "bearish" else "⏸️")
        lines.append(f"{icon} **{a['symbol']}** {a['price']:,.2f} — {a['bias'].upper()} ({a['confidence']}%) | {a['trend']}")

    lines.append("")
    lines.append("_Custom briefing for your selected pairs. Bias = educational lean, not a signal._")

    return {
        "session": session,
        "text": "\n".join(lines),
        "analyses": analyses,
        "timestamp": now.isoformat(),
    }


def post_briefing_to_groups(bridge_api, session: str = "pre_london") -> int:
    """Post briefings to all subscribed groups for the session."""
    subs = _load_subscriptions()
    posted = 0

    for group_jid, sub in subs.items():
        if not sub.get("enabled"):
            continue
        if session not in sub.get("sessions", ["pre_london", "eod"]):
            continue

        try:
            topics = sub.get("topics", [])
            brief = generate_custom_briefing(session, topics)
            bridge_api.bridge_send(group_jid, brief["text"])
            posted += 1
            log.info("[Trading] Posted %s briefing to group %s", session, group_jid.split("@")[0])
        except Exception as exc:
            log.error("[Trading] Failed to post to group %s: %s", group_jid, exc)

    return posted


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup() -> None:
    cleanup_old_charts(24)


# ── Quiz Mode ──────────────────────────────────────────────────────────────────

QUIZ_QUESTIONS = [
    {
        "topic": "candlesticks",
        "question": "You see a candle with a long lower wick, small body at the top, and little upper wick at support after a downtrend. What is this?",
        "options": ["Shooting star", "Hammer", "Doji", "Engulfing"],
        "answer": 1,
        "explanation": "Hammer = bullish rejection at support. Long lower wick = buyers pushed price up from lows. Small body at top = close near open. At support after downtrend = potential reversal signal."
    },
    {
        "topic": "structure",
        "question": "Price makes Higher High, then Higher Low, then Lower High. What's the structure now?",
        "options": ["Uptrend intact", "Downtrend started", "Range/Chop", "Break of structure"],
        "answer": 3,
        "explanation": "HH + HL = uptrend. But then LH breaks the HH sequence. This is a break of structure — trend is questionable, likely ranging or reversing."
    },
    {
        "topic": "risk_management",
        "question": "Account: $10,000. Risk per trade: 1%. Entry: $50,000. Stop Loss: $49,000. What's your position size?",
        "options": ["0.1 BTC", "0.2 BTC", "0.05 BTC", "0.01 BTC"],
        "answer": 0,
        "explanation": "Risk $ = $10,000 x 1% = $100. Risk per unit = $50,000 - $49,000 = $1,000. Size = $100 / $1,000 = 0.1 BTC."
    },
    {
        "topic": "rsi",
        "question": "In a strong uptrend, RSI stays above 70 for weeks. What does this mean?",
        "options": ["Overbought - sell immediately", "Strong momentum - trend continuing", "Divergence forming", "Range bound"],
        "answer": 1,
        "explanation": "RSI > 70 in strong trend = momentum, not overbought. RSI only means 'overbought' in ranges. In trends, it confirms strength."
    },
    {
        "topic": "multi_timeframe",
        "question": "Daily: uptrend, price at support. 4H: pullback to EMA20. 1H: bullish engulfing at EMA20. What's the play?",
        "options": ["Short the engulfing", "Long with daily bias", "Wait for daily close", "No trade - conflicting"],
        "answer": 1,
        "explanation": "HTF bias (daily uptrend) + MTF pullback zone (4H EMA20) + LTF trigger (1H engulfing) = high-probability long. This is the MTF workflow."
    },
    {
        "topic": "support_resistance",
        "question": "Price breaks resistance on high volume, closes above, then pulls back to test the broken level as support and holds. What happened?",
        "options": ["Fakeout", "Break and retest", "Double top", "Bull trap"],
        "answer": 1,
        "explanation": "Break + close above + retest as support = classic break and retest. Old resistance becomes new support. High volume on break = conviction."
    },
    {
        "topic": "liquidity",
        "question": "Price spikes above a clear swing high (where shorts have stops), then immediately reverses and closes back below. What is this?",
        "options": ["Breakout", "Liquidity sweep", "Bull flag", "Volume climax"],
        "answer": 1,
        "explanation": "Liquidity sweep = engineered stop hunt. Price targets the obvious level (shorts' stops), grabs liquidity, then reverses. Wick through + reclaim = sweep."
    },
    {
        "topic": "journaling",
        "question": "You review your journal: 50% win rate, avg win $300, avg loss $200. What's expectancy per trade?",
        "options": ["+$50", "+$100", "+$150", "-$50"],
        "answer": 0,
        "explanation": "Expectancy = (0.5 x 300) - (0.5 x 200) = 150 - 100 = +$50. Positive expectancy = profitable system."
    },
]




def get_quiz_question(topic: str | None = None) -> dict:
    """Get a random quiz question, optionally filtered by topic."""
    import random
    pool = [q for q in QUIZ_QUESTIONS if topic is None or q["topic"] == topic]
    if not pool:
        return {"error": "No questions for that topic"}
    return random.choice(pool)


def check_quiz_answer(question: dict, user_answer: int) -> dict:
    """Check user's quiz answer."""
    correct = user_answer == question["answer"]
    return {
        "correct": correct,
        "answer": question["answer"],
        "explanation": question["explanation"],
        "message": "Correct! 🎯" if correct else f"Wrong. The answer was {question['options'][question['answer']]}."
    }


# ── Live Walkthrough ───────────────────────────────────────────────────────────

def live_walkthrough(symbol: str, interval: str = "4h") -> dict:
    """Generate a step-by-step educational walkthrough of a live chart."""
    analysis = analyze_symbol(symbol, interval)
    if "error" in analysis:
        return {"error": analysis["error"]}

    bias = analysis["bias"]
    struct = analysis["structure"]
    levels = analysis["levels"]
    mom = analysis["momentum"]
    reasons = analysis["reasons"]
    last_candle = analysis["last_candle"]

    walkthrough = []

    # Step 1: HTF Context
    walkthrough.append("📊 **STEP 1: HTF CONTEXT (Daily/Weekly)**")
    walkthrough.append(f"   {symbol} on {interval} — Bias: **{bias.upper()}** ({analysis['confidence']}%)")
    walkthrough.append(f"   Structure: {struct}. {'Uptrend = look for longs.' if struct == 'uptrend' else 'Downtrend = look for shorts.' if struct == 'downtrend' else 'Range = wait for break.'}")

    # Step 2: Key Levels
    walkthrough.append("\n🎯 **STEP 2: KEY LEVELS**")
    if levels["supports"]:
        walkthrough.append(f"   Support: {', '.join(f'{s:,.2f}' for s in levels['supports'][:3])}")
    if levels["resistances"]:
        walkthrough.append(f"   Resistance: {', '.join(f'{r:,.2f}' for r in levels['resistances'][:3])}")

    # Step 3: Momentum
    walkthrough.append("\n⚡ **STEP 3: MOMENTUM READ**")
    walkthrough.append(f"   RSI: {mom.get('rsi', 'N/A'):.0f} — {'Overbought' if mom.get('rsi', 50) > 70 else 'Oversold' if mom.get('rsi', 50) < 30 else 'Neutral'}")
    walkthrough.append(f"   MACD Hist: {mom.get('macd_hist', 'N/A'):.4f} — {'Bullish' if mom.get('macd_hist', 0) > 0 else 'Bearish'}")
    walkthrough.append(f"   Price vs EMA20: {'Above' if mom.get('price', 0) > (mom.get('ema20', 0) or 0) else 'Below'}")

    # Step 4: Last Candle
    walkthrough.append("\n🕯️ **STEP 4: LAST CANDLE**")
    direction = last_candle.get("direction", "unknown")
    walkthrough.append(f"   {direction.capitalize()} candle. Body: {last_candle.get('body_pct', 0):.0f}% of range.")
    if last_candle.get("is_pinocchio_up"):
        walkthrough.append("   📌 Pinocchio up (long upper wick) = rejection at highs")
    if last_candle.get("is_pinocchio_down"):
        walkthrough.append("   📌 Pinocchio down (long lower wick) = rejection at lows")
    if last_candle.get("is_engulfing_potential"):
        walkthrough.append("   📌 Strong body (>70% range) = conviction")

    # Step 5: Bias Reasoning
    walkthrough.append("\n🧠 **STEP 5: WHY THIS BIAS**")
    for i, r in enumerate(reasons, 1):
        walkthrough.append(f"   {i}. {r}")

    # Step 6: Action Plan
    walkthrough.append("\n🎯 **STEP 6: YOUR ACTION PLAN**")
    if bias == "bullish":
        walkthrough.append("   Bias: LONG. Wait for pullback to support/EMA20.")
        walkthrough.append("   Entry zone: Near support or EMA20 retest.")
        walkthrough.append("   Invalidated if: Price breaks support with volume.")
    elif bias == "bearish":
        walkthrough.append("   Bias: SHORT. Wait for rally to resistance/EMA20.")
        walkthrough.append("   Entry zone: Near resistance or EMA20 retest.")
        walkthrough.append("   Invalidated if: Price breaks resistance with volume.")
    else:
        walkthrough.append("   Bias: WAIT. No clear edge. Range = chop.")
        walkthrough.append("   Wait for: Clean break of range high/low with volume.")

    walkthrough.append("\n_Remember: Bias = educational lean. You choose entry, SL, size._")

    return {
        "symbol": analysis["symbol"],
        "interval": interval,
        "price": analysis["price"],
        "bias": bias,
        "confidence": analysis["confidence"],
        "walkthrough": "\n".join(walkthrough),
        "chart_path": analysis.get("chart_path"),
    }


# ── Trade Journal ──────────────────────────────────────────────────────────────

_JOURNAL_FILE = os.path.join(_DATA_DIR, "trade_journal.json")
_journal_lock = threading.Lock()
_journal_cache: dict[str, list[dict]] = {}


def _load_journal() -> dict:
    global _journal_cache
    with _journal_lock:
        if _journal_cache:
            return _journal_cache
        if os.path.exists(_JOURNAL_FILE):
            try:
                with open(_JOURNAL_FILE, "r", encoding="utf-8") as f:
                    _journal_cache = json.load(f)
            except Exception:
                _journal_cache = {}
        return _journal_cache


def _save_journal() -> None:
    with _journal_lock:
        with open(_JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(_journal_cache, f, indent=2)


def add_trade_journal(user_id: str, trade: dict) -> dict:
    """Add a trade to user's journal.

    trade dict should contain:
      - symbol, side (long/short), entry, sl, tp, size, result (win/loss/open), pnl, notes, setup
    """
    journal = _load_journal()
    user_journal = journal.setdefault(user_id, [])

    trade_entry = {
        "id": len(user_journal) + 1,
        "timestamp": datetime.now(TZ).isoformat(),
        "symbol": trade.get("symbol", ""),
        "side": trade.get("side", ""),
        "entry": trade.get("entry"),
        "sl": trade.get("sl"),
        "tp": trade.get("tp"),
        "size": trade.get("size"),
        "result": trade.get("result", "open"),
        "pnl": trade.get("pnl"),
        "r_multiple": trade.get("r_multiple"),
        "notes": trade.get("notes", ""),
        "setup": trade.get("setup", ""),
    }
    user_journal.append(trade_entry)
    _save_journal()
    return {"ok": True, "trade": trade_entry}


def get_trade_journal(user_id: str, limit: int = 50) -> list[dict]:
    """Get user's trade journal."""
    journal = _load_journal()
    return journal.get(user_id, [])[-limit:]


def get_trade_stats(user_id: str) -> dict:
    """Calculate trading statistics from journal."""
    journal = _load_journal()
    trades = journal.get(user_id, [])

    if not trades:
        return {"total": 0, "message": "No trades recorded yet"}

    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    if not closed:
        return {"total": len(trades), "closed": 0, "message": "No closed trades yet"}

    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]

    win_rate = len(wins) / len(closed) * 100
    avg_win = sum(t.get("r_multiple", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t.get("r_multiple", 0) for t in losses) / len(losses)) if losses else 0
    expectancy = (win_rate / 100 * avg_win) - ((100 - win_rate) / 100 * avg_loss)

    total_pnl = sum(t.get("pnl", 0) for t in closed)
    total_r = sum(t.get("r_multiple", 0) for t in closed)

    # By setup
    setup_stats = {}
    for t in closed:
        setup = t.get("setup", "unknown")
        if setup not in setup_stats:
            setup_stats[setup] = {"wins": 0, "losses": 0, "total_r": 0}
        if t.get("result") == "win":
            setup_stats[setup]["wins"] += 1
        else:
            setup_stats[setup]["losses"] += 1
        setup_stats[setup]["total_r"] += t.get("r_multiple", 0)

    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "win_rate": round(win_rate, 1),
        "avg_win_r": round(avg_win, 2),
        "avg_loss_r": round(avg_loss, 2),
        "expectancy_r": round(expectancy, 2),
        "total_pnl": round(total_pnl, 2),
        "total_r": round(total_r, 2),
        "setup_breakdown": setup_stats,
        "profitable": expectancy > 0,
    }