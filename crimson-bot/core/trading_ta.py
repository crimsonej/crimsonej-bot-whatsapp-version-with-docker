"""
core/trading_ta.py
==================
Pure technical analysis utilities for the trading coach.

All functions take lists of OHLCV candles and return structured reads.
No external TA library required — just math.
"""

from __future__ import annotations

import math
import statistics
from typing import Any


# ── Moving averages ───────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[float | None]:
    """Simple Moving Average. Returns list with None for warmup."""
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential Moving Average. Returns list with None for warmup."""
    out: list[float | None] = []
    if not values:
        return out
    k = 2 / (period + 1)
    # Seed with SMA of first `period` values
    if len(values) < period:
        return [None] * len(values)
    seed = sum(values[:period]) / period
    out.extend([None] * (period - 1))
    out.append(seed)
    for i in range(period, len(values)):
        prev = out[-1]
        new = values[i] * k + prev * (1 - k)
        out.append(new)
    return out


# ── Momentum indicators ───────────────────────────────────────────────────────

def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    out: list[float | None] = [None] * (period)
    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    # First avg
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    out.append(100 - 100 / (1 + rs))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        out.append(100 - 100 / (1 + rs))

    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, histogram."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line: list[float | None] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    # signal = ema of macd_line over `signal` periods
    valid_macd = [v for v in macd_line if v is not None]
    sig_full = ema(valid_macd, signal) if valid_macd else []
    # Pad sig_full to length of macd_line
    offset = len(macd_line) - len(sig_full)
    sig_line: list[float | None] = [None] * offset + sig_full
    hist: list[float | None] = []
    for m, s in zip(macd_line, sig_line):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)
    return {"macd": macd_line, "signal": sig_line, "hist": hist}


def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    """Average True Range."""
    if not candles:
        return []
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            prev_close = candles[i - 1]["c"]
            tr = max(c["h"] - c["l"], abs(c["h"] - prev_close), abs(c["l"] - prev_close))
            trs.append(tr)
    return sma(trs, period)


def bollinger(closes: list[float], period: int = 20, stddev: float = 2.0) -> dict:
    """Bollinger Bands."""
    mid = sma(closes, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i in range(len(closes)):
        if i + 1 < period or mid[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i + 1 - period:i + 1]
            mean = mid[i]
            sd = statistics.stdev(window) if len(window) > 1 else 0
            upper.append(mean + stddev * sd)
            lower.append(mean - stddev * sd)
    return {"middle": mid, "upper": upper, "lower": lower}


# ── Structure reads ───────────────────────────────────────────────────────────

def detect_structure(candles: list[dict], lookback: int = 50) -> dict:
    """Identify higher-highs/higher-lows vs lower-highs/lower-lows structure.

    Returns:
      trend: 'uptrend' | 'downtrend' | 'range' | 'unknown'
      swings: list of swing highs/lows found
      last_hh, last_hl, last_lh, last_ll: latest significant pivots
    """
    if len(candles) < 10:
        return {"trend": "unknown", "swings": []}

    recent = candles[-lookback:]
    swings = _find_swings(recent, left=3, right=3)

    if len(swings) < 4:
        return {"trend": "range", "swings": swings}

    # Take last 6 swings to determine trend
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    trend = "range"
    last_hh = last_hl = last_lh = last_ll = None

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1]["price"] > highs[-2]["price"]
        hl = lows[-1]["price"] > lows[-2]["price"]
        lh = highs[-1]["price"] < highs[-2]["price"]
        ll = lows[-1]["price"] < lows[-2]["price"]

        if hh and hl:
            trend = "uptrend"
            last_hh = highs[-1]
            last_hl = lows[-1]
        elif lh and ll:
            trend = "downtrend"
            last_lh = highs[-1]
            last_ll = lows[-1]

    return {
        "trend": trend,
        "swings": swings,
        "last_hh": last_hh,
        "last_hl": last_hl,
        "last_lh": last_lh,
        "last_ll": last_ll,
    }


def _find_swings(candles: list[dict], left: int = 3, right: int = 3) -> list[dict]:
    """Find swing highs and lows using pivot detection."""
    swings: list[dict] = []
    for i in range(left, len(candles) - right):
        c = candles[i]
        is_high = all(c["h"] > candles[j]["h"] for j in range(i - left, i + right + 1) if j != i)
        is_low = all(c["l"] < candles[j]["l"] for j in range(i - left, i + right + 1) if j != i)
        if is_high:
            swings.append({"type": "high", "index": i, "price": c["h"], "t": c["t"]})
        elif is_low:
            swings.append({"type": "low", "index": i, "price": c["l"], "t": c["t"]})
    return swings


def key_levels(candles: list[dict], lookback: int = 100, max_levels: int = 5) -> dict:
    """Identify key S/R levels from recent swing points and price clusters.

    Returns:
      supports: list of price levels (sorted)
      resistances: list of price levels (sorted)
    """
    if len(candles) < 10:
        return {"supports": [], "resistances": []}

    recent = candles[-lookback:]
    swings = _find_swings(recent, left=2, right=2)

    supports = sorted({round(s["price"], 6) for s in swings if s["type"] == "low"})
    resistances = sorted({round(s["price"], 6) for s in swings if s["type"] == "high"}, reverse=True)

    last_close = recent[-1]["c"]

    # Filter to levels near current price (within 8% range)
    supports = [s for s in supports if abs(s - last_close) / last_close < 0.08][-max_levels:]
    resistances = [r for r in resistances if abs(r - last_close) / last_close < 0.08][:max_levels]

    return {"supports": supports, "resistances": resistances}


def volume_profile(candles: list[dict], bins: int = 20) -> dict:
    """Simple volume profile — splits price range into bins and shows volume per bin.

    Returns:
      poc: point of control (price with most volume)
      levels: list of {price_low, price_high, volume}
    """
    if not candles:
        return {"poc": None, "levels": []}

    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    pmax = max(highs)
    pmin = min(lows)
    if pmax == pmin:
        return {"poc": pmax, "levels": []}

    bin_size = (pmax - pmin) / bins
    levels = [{"price_low": pmin + i * bin_size, "price_high": pmin + (i + 1) * bin_size, "volume": 0.0} for i in range(bins)]

    for c in candles:
        # Approximate: distribute volume across price range of candle
        cmin = c["l"]
        cmax = c["h"]
        for lvl in levels:
            overlap_low = max(cmin, lvl["price_low"])
            overlap_high = min(cmax, lvl["price_high"])
            if overlap_high > overlap_low:
                frac = (overlap_high - overlap_low) / (cmax - cmin) if cmax > cmin else 1.0
                lvl["volume"] += c["v"] * frac

    poc_level = max(levels, key=lambda x: x["volume"]) if levels else None
    poc = (poc_level["price_low"] + poc_level["price_high"]) / 2 if poc_level else None

    return {"poc": poc, "levels": levels}


# ── Candle reads ──────────────────────────────────────────────────────────────

def last_candle_summary(c: dict) -> dict:
    """Summarize a single candle's character."""
    body = c["c"] - c["o"]
    total_range = c["h"] - c["l"]
    body_pct = (body / total_range * 100) if total_range > 0 else 0
    upper_wick = c["h"] - max(c["o"], c["c"])
    lower_wick = min(c["o"], c["c"]) - c["l"]

    return {
        "body_pct": body_pct,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "direction": "bullish" if body > 0 else ("bearish" if body < 0 else "doji"),
        "is_pinocchio_up": upper_wick > 2 * abs(body) and body > 0,
        "is_pinocchio_down": lower_wick > 2 * abs(body) and body < 0,
        "is_engulfing_potential": abs(body) > 0.7 * total_range,
    }


def momentum_summary(candles: list[dict]) -> dict:
    """Overall momentum read across multiple indicators."""
    closes = [c["c"] for c in candles]
    if len(closes) < 30:
        return {"verdict": "insufficient_data"}

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    rsi_vals = rsi(closes, 14)
    macd_vals = macd(closes)
    atr_vals = atr(candles, 14)

    last = closes[-1]
    e20 = ema20[-1]
    e50 = ema50[-1]
    e200 = ema200[-1]
    r = rsi_vals[-1]
    m = macd_vals["macd"][-1]
    s = macd_vals["signal"][-1]
    h = macd_vals["hist"][-1]
    a = atr_vals[-1] if atr_vals and atr_vals[-1] is not None else 0

    # Score: +1 bullish, -1 bearish
    score = 0
    signals: list[str] = []

    if e20 is not None:
        if last > e20:
            score += 1; signals.append("price > EMA20")
        else:
            score -= 1; signals.append("price < EMA20")
    if e20 is not None and e50 is not None:
        if e20 > e50:
            score += 1; signals.append("EMA20 > EMA50 (momentum up)")
        else:
            score -= 1; signals.append("EMA20 < EMA50 (momentum down)")
    if e200 is not None:
        if last > e200:
            score += 1; signals.append("price > EMA200 (HTF bullish)")
        else:
            score -= 1; signals.append("price < EMA200 (HTF bearish)")
    if r is not None:
        if r > 70:
            score -= 1; signals.append(f"RSI {r:.0f} (overbought)")
        elif r < 30:
            score += 1; signals.append(f"RSI {r:.0f} (oversold)")
        else:
            signals.append(f"RSI {r:.0f} (neutral)")
    if h is not None:
        if h > 0:
            score += 1; signals.append("MACD histogram positive")
        else:
            score -= 1; signals.append("MACD histogram negative")

    if score >= 3:
        verdict = "bullish"
    elif score <= -3:
        verdict = "bearish"
    elif score >= 1:
        verdict = "lean_bullish"
    elif score <= -1:
        verdict = "lean_bearish"
    else:
        verdict = "neutral"

    return {
        "verdict": verdict,
        "score": score,
        "signals": signals,
        "price": last,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": r,
        "macd_hist": h,
        "atr": a,
    }


# ── Bias call ─────────────────────────────────────────────────────────────────

def compute_bias(candles: list[dict]) -> dict:
    """Compute overall bias with confidence.

    Returns:
      bias: 'bullish' | 'bearish' | 'wait'
      confidence: 0-100
      reasons: list of strings explaining the call
    """
    if len(candles) < 30:
        return {"bias": "wait", "confidence": 0, "reasons": ["not enough data"]}

    mom = momentum_summary(candles)
    struct = detect_structure(candles)
    levels = key_levels(candles)

    reasons: list[str] = []
    score = 0
    max_score = 0

    # Momentum weight
    max_score += 5
    v = mom.get("verdict", "neutral")
    if v == "bullish":
        score += 5; reasons.append("momentum reads bullish")
    elif v == "lean_bullish":
        score += 2; reasons.append("momentum leans bullish")
    elif v == "bearish":
        score -= 5; reasons.append("momentum reads bearish")
    elif v == "lean_bearish":
        score -= 2; reasons.append("momentum leans bearish")
    else:
        reasons.append("momentum is neutral")

    # Structure weight
    max_score += 4
    t = struct.get("trend", "range")
    if t == "uptrend":
        score += 4; reasons.append("structure is uptrend (HH/HL)")
    elif t == "downtrend":
        score -= 4; reasons.append("structure is downtrend (LH/LL)")
    else:
        reasons.append("structure is choppy/range")

    # Candle position weight (closer to support = bullish bias, resistance = bearish)
    max_score += 2
    last_close = candles[-1]["c"]
    if levels["supports"] and levels["resistances"]:
        nearest_sup = max(levels["supports"])
        nearest_res = min(levels["resistances"])
        range_size = nearest_res - nearest_sup
        if range_size > 0:
            position = (last_close - nearest_sup) / range_size
            if position < 0.3:
                score += 2; reasons.append("price near support (potential bounce zone)")
            elif position > 0.7:
                score -= 2; reasons.append("price near resistance (potential rejection zone)")
            else:
                reasons.append("price mid-range")

    confidence = min(100, int(abs(score) / max_score * 100))

    if score >= 4:
        bias = "bullish"
    elif score <= -4:
        bias = "bearish"
    else:
        bias = "wait"

    return {
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "momentum": mom,
        "structure": struct,
        "levels": levels,
    }


# ── Pattern Recognition ────────────────────────────────────────────────────────

def detect_patterns(candles: list[dict]) -> list[dict]:
    """Detect common chart patterns.

    Returns list of detected patterns with type, confidence, and description.
    """
    if len(candles) < 20:
        return []

    patterns = []

    # Double Top / Double Bottom
    dt_db = _detect_double_top_bottom(candles)
    if dt_db:
        patterns.append(dt_db)

    # Head and Shoulders / Inverse H&S
    hs = _detect_head_and_shoulders(candles)
    if hs:
        patterns.append(hs)

    # Bull Flag / Bear Flag
    flag = _detect_flag(candles)
    if flag:
        patterns.append(flag)

    # Ascending / Descending Triangle
    triangle = _detect_triangle(candles)
    if triangle:
        patterns.append(triangle)

    # Wedge (Rising/Falling)
    wedge = _detect_wedge(candles)
    if wedge:
        patterns.append(wedge)

    return patterns


def _detect_double_top_bottom(candles: list[dict]) -> dict | None:
    """Detect double top (M) or double bottom (W) patterns."""
    if len(candles) < 30:
        return None

    # Look at last 30 candles
    recent = candles[-30:]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]

    # Find peaks and troughs
    peaks = []
    troughs = []
    for i in range(2, len(recent) - 2):
        if recent[i]["h"] > recent[i-1]["h"] and recent[i]["h"] > recent[i-2]["h"] and \
           recent[i]["h"] > recent[i+1]["h"] and recent[i]["h"] > recent[i+2]["h"]:
            peaks.append({"index": i, "price": recent[i]["h"]})
        if recent[i]["l"] < recent[i-1]["l"] and recent[i]["l"] < recent[i-2]["l"] and \
           recent[i]["l"] < recent[i+1]["l"] and recent[i]["l"] < recent[i+2]["l"]:
            troughs.append({"index": i, "price": recent[i]["l"]})

    # Double Top: two similar peaks separated by a trough
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        price_diff = abs(p1["price"] - p2["price"]) / p1["price"]
        time_diff = p2["index"] - p1["index"]
        if price_diff < 0.02 and 5 <= time_diff <= 20:  # within 2%, 5-20 candles apart
            neckline = min(c["l"] for c in recent[p1["index"]:p2["index"]])
            return {
                "type": "double_top",
                "direction": "bearish",
                "confidence": min(90, int((1 - price_diff) * 100)),
                "peak1": p1["price"],
                "peak2": p2["price"],
                "neckline": neckline,
                "description": f"Double top at ~{p1['price']:,.2f}. Neckline at {neckline:,.2f}. Break below = bearish."
            }

    # Double Bottom: two similar troughs separated by a peak
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        price_diff = abs(t1["price"] - t2["price"]) / t1["price"]
        time_diff = t2["index"] - t1["index"]
        if price_diff < 0.02 and 5 <= time_diff <= 20:
            neckline = max(c["h"] for c in recent[t1["index"]:t2["index"]])
            return {
                "type": "double_bottom",
                "direction": "bullish",
                "confidence": min(90, int((1 - price_diff) * 100)),
                "trough1": t1["price"],
                "trough2": t2["price"],
                "neckline": neckline,
                "description": f"Double bottom at ~{t1['price']:,.2f}. Neckline at {neckline:,.2f}. Break above = bullish."
            }

    return None


def _detect_head_and_shoulders(candles: list[dict]) -> dict | None:
    """Detect Head and Shoulders (bearish) or Inverse H&S (bullish)."""
    if len(candles) < 40:
        return None

    recent = candles[-40:]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]

    # Find peaks
    peaks = []
    for i in range(2, len(recent) - 2):
        if recent[i]["h"] > recent[i-1]["h"] and recent[i]["h"] > recent[i-2]["h"] and \
           recent[i]["h"] > recent[i+1]["h"] and recent[i]["h"] > recent[i+2]["h"]:
            peaks.append({"index": i, "price": recent[i]["h"]})

    if len(peaks) >= 3:
        # Check last 3 peaks for H&S pattern
        left, head, right = peaks[-3], peaks[-2], peaks[-1]
        # Head higher than shoulders
        if head["price"] > left["price"] and head["price"] > right["price"]:
            # Shoulders roughly equal
            shoulder_diff = abs(left["price"] - right["price"]) / left["price"]
            if shoulder_diff < 0.03:
                # Neckline = low between left-head and head-right
                neckline1 = min(c["l"] for c in recent[left["index"]:head["index"]])
                neckline2 = min(c["l"] for c in recent[head["index"]:right["index"]])
                neckline = (neckline1 + neckline2) / 2
                return {
                    "type": "head_and_shoulders",
                    "direction": "bearish",
                    "confidence": 75,
                    "left_shoulder": left["price"],
                    "head": head["price"],
                    "right_shoulder": right["price"],
                    "neckline": neckline,
                    "description": f"H&S: Left {left['price']:,.2f}, Head {head['price']:,.2f}, Right {right['price']:,.2f}. Neckline {neckline:,.2f}. Break = bearish."
                }

    # Inverse H&S (check troughs)
    troughs = []
    for i in range(2, len(recent) - 2):
        if recent[i]["l"] < recent[i-1]["l"] and recent[i]["l"] < recent[i-2]["l"] and \
           recent[i]["l"] < recent[i+1]["l"] and recent[i]["l"] < recent[i+2]["l"]:
            troughs.append({"index": i, "price": recent[i]["l"]})

    if len(troughs) >= 3:
        left, head, right = troughs[-3], troughs[-2], troughs[-1]
        if head["price"] < left["price"] and head["price"] < right["price"]:
            shoulder_diff = abs(left["price"] - right["price"]) / left["price"]
            if shoulder_diff < 0.03:
                neckline1 = max(c["h"] for c in recent[left["index"]:head["index"]])
                neckline2 = max(c["h"] for c in recent[head["index"]:right["index"]])
                neckline = (neckline1 + neckline2) / 2
                return {
                    "type": "inverse_head_and_shoulders",
                    "direction": "bullish",
                    "confidence": 75,
                    "left_shoulder": left["price"],
                    "head": head["price"],
                    "right_shoulder": right["price"],
                    "neckline": neckline,
                    "description": f"Inverse H&S: Left {left['price']:,.2f}, Head {head['price']:,.2f}, Right {right['price']:,.2f}. Neckline {neckline:,.2f}. Break = bullish."
                }

    return None


def _detect_flag(candles: list[dict]) -> dict | None:
    """Detect bull flag or bear flag (continuation patterns)."""
    if len(candles) < 20:
        return None

    recent = candles[-20:]
    closes = [c["c"] for c in recent]

    # Strong move (flagpole) followed by consolidation (flag)
    # Check first 5-8 candles for strong move, next 10-15 for consolidation
    pole_length = 8
    flag_start = pole_length

    if len(closes) < pole_length + 5:
        return None

    pole_move = (closes[pole_length - 1] - closes[0]) / closes[0]
    flag_range = max(closes[flag_start:]) - min(closes[flag_start:])
    flag_range_pct = flag_range / closes[flag_start]

    # Bull flag: strong up move (>3%), then tight consolidation (<2% range)
    if pole_move > 0.03 and flag_range_pct < 0.02:
        return {
            "type": "bull_flag",
            "direction": "bullish",
            "confidence": 70,
            "pole_move_pct": pole_move * 100,
            "flag_range_pct": flag_range_pct * 100,
            "description": f"Bull flag: {pole_move*100:.1f}% pole, {flag_range_pct*100:.1f}% flag. Break up = continuation."
        }

    # Bear flag: strong down move (<-3%), then tight consolidation
    if pole_move < -0.03 and flag_range_pct < 0.02:
        return {
            "type": "bear_flag",
            "direction": "bearish",
            "confidence": 70,
            "pole_move_pct": pole_move * 100,
            "flag_range_pct": flag_range_pct * 100,
            "description": f"Bear flag: {pole_move*100:.1f}% pole, {flag_range_pct*100:.1f}% flag. Break down = continuation."
        }

    return None


def _detect_triangle(candles: list[dict]) -> dict | None:
    """Detect ascending, descending, or symmetrical triangle."""
    if len(candles) < 20:
        return None

    recent = candles[-20:]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]

    # Linear regression on highs and lows
    def slope(values):
        n = len(values)
        x = list(range(n))
        x_mean = sum(x) / n
        y_mean = sum(values) / n
        num = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        den = sum((x[i] - x_mean) ** 2 for i in range(n))
        return num / den if den else 0

    high_slope = slope(highs)
    low_slope = slope(lows)

    # Ascending triangle: flat highs, rising lows
    if abs(high_slope) < 0.001 and low_slope > 0.001:
        return {
            "type": "ascending_triangle",
            "direction": "bullish",
            "confidence": 65,
            "description": "Ascending triangle: flat resistance, rising support. Break up = bullish."
        }

    # Descending triangle: falling highs, flat lows
    if high_slope < -0.001 and abs(low_slope) < 0.001:
        return {
            "type": "descending_triangle",
            "direction": "bearish",
            "confidence": 65,
            "description": "Descending triangle: falling resistance, flat support. Break down = bearish."
        }

    # Symmetrical triangle: converging highs and lows
    if high_slope < -0.001 and low_slope > 0.001:
        return {
            "type": "symmetrical_triangle",
            "direction": "neutral",
            "confidence": 60,
            "description": "Symmetrical triangle: converging trendlines. Break either way = continuation."
        }

    return None


def _detect_wedge(candles: list[dict]) -> dict | None:
    """Detect rising wedge (bearish) or falling wedge (bullish)."""
    if len(candles) < 20:
        return None

    recent = candles[-20:]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]

    def slope(values):
        n = len(values)
        x = list(range(n))
        x_mean = sum(x) / n
        y_mean = sum(values) / n
        num = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        den = sum((x[i] - x_mean) ** 2 for i in range(n))
        return num / den if den else 0

    high_slope = slope(highs)
    low_slope = slope(lows)

    # Rising wedge: both rising, but lows rising faster (converging up) -> bearish
    if high_slope > 0.001 and low_slope > high_slope:
        return {
            "type": "rising_wedge",
            "direction": "bearish",
            "confidence": 65,
            "description": "Rising wedge: both trendlines up, support steeper. Break down = bearish reversal."
        }

    # Falling wedge: both falling, but highs falling faster (converging down) -> bullish
    if high_slope < -0.001 and low_slope < high_slope:
        return {
            "type": "falling_wedge",
            "direction": "bullish",
            "confidence": 65,
            "description": "Falling wedge: both trendlines down, resistance steeper. Break up = bullish reversal."
        }

    return None


# ── Multi-Timeframe Analysis ──────────────────────────────────────────────────

def multi_timeframe_analysis(symbol: str) -> dict:
    """Analyze symbol across multiple timeframes.

    Returns HTF bias, MTF structure, LTF trigger status.
    """
    from core.market_data import get_klines
    from core.trading_ta import compute_bias, detect_structure, momentum_summary

    timeframes = {
        "HTF": ("1d", 100),   # Daily - bias
        "MTF": ("4h", 100),   # 4H - structure
        "LTF": ("1h", 100),   # 1H - trigger
    }

    results = {}
    for label, (interval, limit) in timeframes.items():
        candles = get_klines(symbol, interval, limit)
        if candles and len(candles) >= 30:
            bias = compute_bias(candles)
            struct = detect_structure(candles)
            mom = momentum_summary(candles)
            results[label] = {
                "interval": interval,
                "bias": bias["bias"],
                "confidence": bias["confidence"],
                "structure": struct["trend"],
                "momentum": mom.get("verdict", "neutral"),
                "price": candles[-1]["c"],
            }

    # Synthesize
    htf_bias = results.get("HTF", {}).get("bias", "unknown")
    mtf_struct = results.get("MTF", {}).get("structure", "unknown")
    ltf_momentum = results.get("LTF", {}).get("momentum", "unknown")

    # Overall verdict
    if htf_bias == "bullish" and mtf_struct == "uptrend" and ltf_momentum in ("bullish", "lean_bullish"):
        overall = "bullish_aligned"
    elif htf_bias == "bearish" and mtf_struct == "downtrend" and ltf_momentum in ("bearish", "lean_bearish"):
        overall = "bearish_aligned"
    elif htf_bias != mtf_struct:
        overall = "conflict"
    else:
        overall = "mixed"

    return {
        "symbol": symbol,
        "timeframes": results,
        "overall": overall,
        "summary": f"HTF: {htf_bias}, MTF: {mtf_struct}, LTF: {ltf_momentum} → {overall.replace('_', ' ')}"
    }
