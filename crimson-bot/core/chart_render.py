"""
core/chart_render.py
====================
Chart rendering for the trading coach. Generates clean candlestick charts
with indicators overlay as PNG files.

Uses matplotlib + mplfinance (lightweight, no heavy deps).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

from core.config import BASE_DIR

_CACHE_DIR = os.path.join(BASE_DIR, "trading", "cache", "charts")
os.makedirs(_CACHE_DIR, exist_ok=True)


def render_chart(
    symbol: str,
    candles: list[dict],
    interval: str = "1h",
    indicators: list[str] | None = None,
    levels: dict | None = None,
    bias: str | None = None,
    bias_confidence: int = 0,
    title_suffix: str = "",
) -> str | None:
    """Render a candlestick chart to PNG.

    Returns path to saved file, or None on failure.
    """
    if not candles or len(candles) < 2:
        return None

    indicators = indicators or []

    # Prepare data
    dates = [datetime.fromtimestamp(c["t"]) for c in candles]
    opens = [c["o"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]

    # Compute indicator data if requested
    ema20 = ema20_vals = ema50_vals = ema200_vals = None
    if "ema" in indicators:
        from core.trading_ta import ema
        ema20_vals = ema(closes, 20)
        ema50_vals = ema(closes, 50)
        if len(closes) >= 200:
            ema200_vals = ema(closes, 200)

    rsi_vals = None
    if "rsi" in indicators:
        from core.trading_ta import rsi
        rsi_vals = rsi(closes, 14)

    macd_vals = None
    if "macd" in indicators:
        from core.trading_ta import macd
        macd_vals = macd(closes)

    boll_vals = None
    if "bb" in indicators:
        from core.trading_ta import bollinger
        boll_vals = bollinger(closes)

    # Create figure with subplots
    n_rows = 1
    height_ratios = [3]
    if "volume" in indicators or True:
        n_rows += 1
        height_ratios.append(1)
    if "rsi" in indicators:
        n_rows += 1
        height_ratios.append(1)
    if "macd" in indicators:
        n_rows += 1
        height_ratios.append(1)

    fig, axes = plt.subplots(n_rows, 1, figsize=(12, 3 + 2 * n_rows), gridspec_kw={"height_ratios": height_ratios})
    if n_rows == 1:
        axes = [axes]

    ax_main = axes[0]
    row = 1

    # ── Main chart: candlesticks ────────────────────────────────────────────
    _plot_candles(ax_main, dates, opens, highs, lows, closes)

    # EMAs
    if ema20_vals:
        _plot_line(ax_main, dates, ema20_vals, "EMA20", "#ff6b6b", 1.0)
    if ema50_vals:
        _plot_line(ax_main, dates, ema50_vals, "EMA50", "#4ecdc4", 1.0)
    if ema200_vals:
        _plot_line(ax_main, dates, ema200_vals, "EMA200", "#ffe66d", 1.5)

    # Bollinger Bands
    if boll_vals:
        _plot_bollinger(ax_main, dates, boll_vals)

    # Key levels
    if levels:
        _plot_levels(ax_main, dates, levels, closes[-1])

    # Bias label
    if bias:
        color = "#00ff88" if bias == "bullish" else ("#ff4444" if bias == "bearish" else "#ffaa00")
        label = f"Bias: {bias.upper()} ({bias_confidence}% conf)"
        ax_main.text(0.02, 0.96, label, transform=ax_main.transAxes, fontsize=11,
                     fontweight="bold", color=color, verticalalignment="top",
                     bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e", edgecolor=color, alpha=0.9))

    # Title
    suffix = f" | {title_suffix}" if title_suffix else ""
    bias_label = f" — {bias.upper()}" if bias else ""
    ax_main.set_title(f"{symbol} {interval}{suffix}{bias_label}", fontsize=14, fontweight="bold", color="#e0e0e0", pad=10)
    ax_main.set_ylabel("Price", color="#aaa")
    ax_main.legend(loc="upper left", framealpha=0.7, fontsize=9)
    ax_main.grid(True, alpha=0.2, linestyle="--")
    _style_axis(ax_main)

    # ── Volume ──────────────────────────────────────────────────────────────
    if "volume" in indicators or True:
        ax_vol = axes[row]
        _plot_volume(ax_vol, dates, opens, closes, volumes)
        ax_vol.set_ylabel("Vol", color="#aaa")
        _style_axis(ax_vol)
        row += 1

    # ── RSI ─────────────────────────────────────────────────────────────────
    if "rsi" in indicators and rsi_vals:
        ax_rsi = axes[row]
        _plot_rsi(ax_rsi, dates, rsi_vals)
        ax_rsi.set_ylabel("RSI", color="#aaa")
        _style_axis(ax_rsi)
        row += 1

    # ── MACD ────────────────────────────────────────────────────────────────
    if "macd" in indicators and macd_vals:
        ax_macd = axes[row]
        _plot_macd(ax_macd, dates, macd_vals)
        ax_macd.set_ylabel("MACD", color="#aaa")
        _style_axis(ax_macd)
        row += 1

    # X-axis formatting (only on bottom)
    _format_xaxis(axes[-1], dates)

    plt.tight_layout()

    # Save
    safe_symbol = symbol.replace("/", "_").replace(":", "_").replace(" ", "_")
    fname = f"{safe_symbol}_{interval}_{int(dates[-1].timestamp())}.png"
    fpath = os.path.join(_CACHE_DIR, fname)
    try:
        fig.savefig(fpath, dpi=150, facecolor="#0d0d14", edgecolor="none", bbox_inches="tight")
        plt.close(fig)
        return fpath
    except Exception as exc:
        plt.close(fig)
        from core.config import log
        log.warning("[Chart] save failed: %s", exc)
        return None


# ── Plotting helpers ──────────────────────────────────────────────────────────

def _plot_candles(ax, dates, opens, highs, lows, closes):
    """Plot candlesticks."""
    width = (dates[1] - dates[0]).total_seconds() / 86400 * 0.8 if len(dates) > 1 else 0.8
    width_days = width

    for i in range(len(dates)):
        color = "#00ff88" if closes[i] >= opens[i] else "#ff4444"
        # Wick
        ax.plot([dates[i], dates[i]], [lows[i], highs[i]], color=color, linewidth=0.8)
        # Body
        body_height = abs(closes[i] - opens[i])
        body_bottom = min(opens[i], closes[i])
        if body_height == 0:
            body_height = (highs[i] - lows[i]) * 0.001 or 0.01
        rect = Rectangle((mdates.date2num(dates[i]) - width_days / 2, body_bottom),
                         width_days, body_height, facecolor=color, edgecolor=color, linewidth=0)
        ax.add_patch(rect)


def _plot_line(ax, dates, values, label, color, width):
    valid = [(d, v) for d, v in zip(dates, values) if v is not None]
    if valid:
        d, v = zip(*valid)
        ax.plot(d, v, label=label, color=color, linewidth=width, alpha=0.9)


def _plot_bollinger(ax, dates, boll):
    mid = boll.get("middle", [])
    upper = boll.get("upper", [])
    lower = boll.get("lower", [])
    valid = [(d, u, m, l) for d, u, m, l in zip(dates, upper, mid, lower) if u is not None]
    if valid:
        d, u, m, l = zip(*valid)
        ax.fill_between(d, u, l, color="#4ecdc4", alpha=0.1, label="BB 20,2")
        ax.plot(d, m, color="#4ecdc4", linewidth=0.8, alpha=0.6)


def _plot_levels(ax, dates, levels, last_close):
    for s in levels.get("supports", []):
        ax.axhline(s, color="#00ff88", linestyle="--", linewidth=0.8, alpha=0.6)
    for r in levels.get("resistances", []):
        ax.axhline(r, color="#ff4444", linestyle="--", linewidth=0.8, alpha=0.6)


def _plot_volume(ax, dates, opens, closes, volumes):
    colors = ["#00ff88" if c >= o else "#ff4444" for o, c in zip(opens, closes)]
    width = (dates[1] - dates[0]).total_seconds() / 86400 * 0.8 if len(dates) > 1 else 0.8
    ax.bar(dates, volumes, width=width, color=colors, alpha=0.6, edgecolor="none")


def _plot_rsi(ax, dates, rsi_vals):
    valid = [(d, v) for d, v in zip(dates, rsi_vals) if v is not None]
    if not valid:
        return
    d, v = zip(*valid)
    ax.plot(d, v, color="#ffaa00", linewidth=1)
    ax.axhline(70, color="#ff4444", linestyle="--", linewidth=0.6, alpha=0.5)
    ax.axhline(50, color="#888888", linestyle="--", linewidth=0.5, alpha=0.5)
    ax.axhline(30, color="#00ff88", linestyle="--", linewidth=0.6, alpha=0.5)
    ax.fill_between(d, 70, v, where=[x > 70 for x in v], color="#ff4444", alpha=0.2)
    ax.fill_between(d, 30, v, where=[x < 30 for x in v], color="#00ff88", alpha=0.2)
    ax.set_ylim(0, 100)


def _plot_macd(ax, dates, macd_vals):
    macd_line = macd_vals.get("macd", [])
    signal = macd_vals.get("signal", [])
    hist = macd_vals.get("hist", [])

    valid = [(d, m, s, h) for d, m, s, h in zip(dates, macd_line, signal, hist) if m is not None and h is not None]
    if not valid:
        return
    d, m, s, h = zip(*valid)
    ax.plot(d, m, color="#4ecdc4", linewidth=1, label="MACD")
    ax.plot(d, s, color="#ff6b6b", linewidth=1, label="Signal")

    # Histogram
    colors = ["#00ff88" if x >= 0 else "#ff4444" for x in h]
    width = (d[1] - d[0]).total_seconds() / 86400 * 0.6 if len(d) > 1 else 0.5
    ax.bar(d, h, width=width, color=colors, alpha=0.5, edgecolor="none")
    ax.axhline(0, color="#888888", linewidth=0.5)


def _format_xaxis(ax, dates):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center", color="#888")


def _style_axis(ax):
    ax.set_facecolor("#0d0d14")
    for spine in ax.spines.values():
        spine.set_color("#333")
        spine.set_linewidth(0.5)
    ax.tick_params(colors="#888", labelsize=9)
    ax.yaxis.set_tick_params(colors="#888")


def cleanup_old_charts(max_age_hours: int = 24) -> None:
    """Delete chart PNGs older than max_age_hours."""
    import time
    now = time.time()
    for fname in os.listdir(_CACHE_DIR):
        if fname.endswith(".png"):
            fpath = os.path.join(_CACHE_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > max_age_hours * 3600:
                    os.remove(fpath)
            except Exception:
                pass