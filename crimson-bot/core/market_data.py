"""
core/market_data.py
===================
Free, no-API-key market data clients for the trading coach.

Sources:
  - CoinGecko (public REST): crypto prices, market cap, basic info
  - Binance (public REST): real-time crypto candles, order book
  - yfinance: stocks, forex, indices, commodities, bonds

All clients are read-only, cache aggressively, and fail gracefully.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from core.config import log

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading", "cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
_CACHE_FILE = os.path.join(_CACHE_DIR, "market_data_cache.json")

_TTL_PRICE = 30          # 30s for prices
_TTL_KLINES = 300        # 5min for candles
_TTL_META = 86400        # 24h for symbol metadata

_cache: dict[str, tuple[float, Any]] = {}


def _load_cache() -> None:
    global _cache
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            _cache = {k: (ts, v) for k, (ts, v) in raw.items() if now - ts < _TTL_META}
        except Exception as exc:
            log.warning("[MarketData] cache load failed: %s", exc)
            _cache = {}


def _save_cache() -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: [ts, v] for k, (ts, v) in _cache.items()}, f)
    except Exception as exc:
        log.warning("[MarketData] cache save failed: %s", exc)


_load_cache()


def _cached(key: str, ttl: int) -> Any | None:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    return None


def _put(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)
    if len(_cache) % 25 == 0:
        _save_cache()


# ── Symbol normalization ──────────────────────────────────────────────────────

_CRYPTO_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
    "xrp": "ripple", "ada": "cardano", "doge": "dogecoin", "avax": "avalanche-2",
    "matic": "matic-network", "dot": "polkadot", "link": "chainlink",
    "ton": "the-open-network", "shib": "shiba-inu", "trx": "tron",
    "uni": "uniswap", "ltc": "litecoin", "bch": "bitcoin-cash", "near": "near",
    "atom": "cosmos", "xlm": "stellar", "xmr": "monero", "etc": "ethereum-classic",
    "fil": "filecoin", "apt": "aptos", "arb": "arbitrum", "op": "optimism",
    "sui": "sui", "pepe": "pepe", "wif": "dogwifcoin", "bonk": "bonk",
    "ftm": "fantom", "aave": "aave", "algo": "algorand", "sand": "the-sandbox",
    "mana": "decentraland", "axs": "axie-infinity", "crv": "curve-dao-token",
    "mkr": "maker", "ldo": "lido-dao", "rndr": "render-token",
}

_BINANCE_QUOTE = "USDT"


def normalize_symbol(raw: str) -> dict:
    """Turn a free-text symbol into a normalized record.

    Returns dict with keys:
      kind: 'crypto' | 'stock' | 'forex' | 'index' | 'commodity' | 'bond' | 'unknown'
      canonical: canonical symbol
      display: human label
      source_pref: ordered list of preferred sources
    """
    if not raw:
        return {"kind": "unknown", "canonical": "", "display": "", "source_pref": []}

    s = raw.strip().upper().replace("/", "").replace("-", "").replace(" ", "")

    # Forex patterns like EURUSD, GBPJPY
    forex_pairs = {
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
        "EURJPY", "GBPJPY", "EURGBP", "EURCHF", "AUDJPY", "EURAUD", "GBPAUD",
        "USDCNH", "USDTRY", "USDZAR", "USDMXN", "USDBRL", "USDSGD", "USDHKD",
    }
    if s in forex_pairs:
        return {
            "kind": "forex",
            "canonical": f"{s[:3]}{s[3:]}=X",
            "display": f"{s[:3]}/{s[3:]}",
            "source_pref": ["yfinance"],
        }

    # Indices
    indices = {
        "SPX": "^GSPC", "SP500": "^GSPC", "NDX": "^IXIC", "NASDAQ": "^IXIC",
        "DJI": "^DJI", "DOW": "^DJI", "DXY": "DX-Y.NYB", "VIX": "^VIX",
        "FTSE": "^FTSE", "DAX": "^GDAXI", "NIKKEI": "^N225", "N225": "^N225",
        "HSI": "^HSI", "CAC": "^FCHI",
    }
    if s in indices:
        canon = indices[s]
        return {
            "kind": "index",
            "canonical": canon,
            "display": s,
            "source_pref": ["yfinance"],
        }

    # Commodities
    commodities = {
        "GOLD": "GC=F", "XAU": "GC=F", "XAUUSD": "GC=F",
        "SILVER": "SI=F", "XAG": "SI=F", "XAGUSD": "SI=F",
        "OIL": "CL=F", "WTI": "CL=F", "CRUDE": "CL=F", "USOIL": "CL=F",
        "BRENT": "BZ=F", "UKOIL": "BZ=F",
        "NATGAS": "NG=F", "COPPER": "HG=F",
    }
    if s in commodities:
        canon = commodities[s]
        return {
            "kind": "commodity",
            "canonical": canon,
            "display": s,
            "source_pref": ["yfinance"],
        }

    # Bonds / yields
    bonds = {
        "US10Y": "^TNX", "US02Y": "^IRX", "US30Y": "^TYX", "TNX": "^TNX",
        "TYX": "^TYX", "IRX": "^IRX", "FVX": "^FVX",
    }
    if s in bonds:
        canon = bonds[s]
        return {
            "kind": "bond",
            "canonical": canon,
            "display": s,
            "source_pref": ["yfinance"],
        }

    # Crypto: try 3-5 letter tickers
    base = s.replace("USDT", "").replace("USD", "").replace("USDC", "").replace("BUSD", "")
    if base and base.isalpha() and 2 <= len(base) <= 6:
        cg_id = _CRYPTO_MAP.get(base.lower())
        return {
            "kind": "crypto",
            "canonical": base,
            "display": f"{base}/USDT",
            "source_pref": ["binance", "coingecko"],
            "coingecko_id": cg_id,
            "binance_symbol": f"{base}{_BINANCE_QUOTE}",
        }

    # Stocks: any other ticker (assume US by default, yfinance will resolve)
    if s.isalpha() and 1 <= len(s) <= 5:
        return {
            "kind": "stock",
            "canonical": s,
            "display": s,
            "source_pref": ["yfinance"],
        }

    return {"kind": "unknown", "canonical": s, "display": raw, "source_pref": []}


# ── CoinGecko ─────────────────────────────────────────────────────────────────

def coingecko_simple_price(symbols: list[str], vs: str = "usd") -> dict[str, dict]:
    """Fetch simple prices from CoinGecko (free, no key)."""
    if not symbols:
        return {}
    cache_key = f"cg:price:{','.join(sorted(symbols))}:{vs}"
    cached = _cached(cache_key, _TTL_PRICE)
    if cached is not None:
        return cached

    ids = []
    for sym in symbols:
        norm = normalize_symbol(sym)
        if norm.get("coingecko_id"):
            ids.append(norm["coingecko_id"])
    if not ids:
        return {}

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": vs,
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
                "include_market_cap": "true",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        result = {sym: data.get(norm["coingecko_id"], {}) for sym in symbols if norm.get("coingecko_id")}
        _put(cache_key, result)
        return result
    except Exception as exc:
        log.warning("[CoinGecko] price fetch failed: %s", exc)
        return {}


# ── Binance public REST ───────────────────────────────────────────────────────

_BINANCE_BASE = "https://api.binance.com"
_BINANCE_INTERVALS = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
}


def binance_klines(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Fetch OHLCV candles from Binance (free, no key, public).

    Returns list of dicts: {t, o, h, l, c, v}
    """
    cache_key = f"bn:klines:{symbol}:{interval}:{limit}"
    cached = _cached(cache_key, _TTL_KLINES)
    if cached is not None:
        return cached

    if interval not in _BINANCE_INTERVALS:
        interval = "1h"

    try:
        r = requests.get(
            f"{_BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol.upper(), "interval": _BINANCE_INTERVALS[interval], "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        result = []
        for row in raw:
            result.append({
                "t": int(row[0]) // 1000,  # ms → s
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        _put(cache_key, result)
        return result
    except Exception as exc:
        log.warning("[Binance] klines fetch failed for %s %s: %s", symbol, interval, exc)
        return []


def binance_ticker(symbol: str) -> dict:
    """Get 24h ticker from Binance (price, change, volume, high, low)."""
    cache_key = f"bn:ticker:{symbol}"
    cached = _cached(cache_key, _TTL_PRICE)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{_BINANCE_BASE}/api/v3/ticker/24hr",
            params={"symbol": symbol.upper()},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        result = {
            "price": float(data.get("lastPrice", 0)),
            "change_pct": float(data.get("priceChangePercent", 0)),
            "volume": float(data.get("volume", 0)),
            "quote_volume": float(data.get("quoteVolume", 0)),
            "high": float(data.get("highPrice", 0)),
            "low": float(data.get("lowPrice", 0)),
            "open": float(data.get("openPrice", 0)),
        }
        _put(cache_key, result)
        return result
    except Exception as exc:
        log.warning("[Binance] ticker fetch failed for %s: %s", symbol, exc)
        return {}


def binance_orderbook(symbol: str, limit: int = 20) -> dict:
    """Get order book depth (free, no key)."""
    cache_key = f"bn:ob:{symbol}:{limit}"
    cached = _cached(cache_key, 15)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{_BINANCE_BASE}/api/v3/depth",
            params={"symbol": symbol.upper(), "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        result = {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])[:limit]],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])[:limit]],
        }
        _put(cache_key, result)
        return result
    except Exception as exc:
        log.warning("[Binance] orderbook fetch failed for %s: %s", symbol, exc)
        return {"bids": [], "asks": []}


# ── Unified fetchers ──────────────────────────────────────────────────────────

def get_price(symbol_raw: str) -> dict:
    """Get current price for any symbol. Returns dict with price, change, volume, source."""
    norm = normalize_symbol(symbol_raw)
    kind = norm["kind"]
    display = norm["display"]

    if kind == "crypto":
        sym = norm.get("binance_symbol", "")
        if sym:
            ticker = binance_ticker(sym)
            if ticker.get("price"):
                return {
                    "symbol": display,
                    "kind": kind,
                    "price": ticker["price"],
                    "change_pct_24h": ticker["change_pct"],
                    "high_24h": ticker["high"],
                    "low_24h": ticker["low"],
                    "volume_24h": ticker["quote_volume"],
                    "source": "binance",
                }
        # Fallback to CoinGecko
        prices = coingecko_simple_price([symbol_raw])
        if prices:
            for sym, data in prices.items():
                if data.get("usd"):
                    return {
                        "symbol": display,
                        "kind": kind,
                        "price": data["usd"],
                        "change_pct_24h": data.get("usd_24h_change", 0),
                        "volume_24h": data.get("usd_24h_vol", 0),
                        "market_cap": data.get("usd_market_cap", 0),
                        "source": "coingecko",
                    }

    elif kind in ("stock", "forex", "index", "commodity", "bond"):
        try:
            import yfinance as yf
            ticker = yf.Ticker(norm["canonical"])
            hist = ticker.history(period="5d", interval="1d")
            if not hist.empty:
                last = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2] if len(hist) > 1 else last
                change_pct = ((last - prev) / prev * 100) if prev else 0
                return {
                    "symbol": display,
                    "kind": kind,
                    "price": float(last),
                    "change_pct_24h": float(change_pct),
                    "volume_24h": float(hist["Volume"].iloc[-1]) if "Volume" in hist else 0,
                    "source": "yfinance",
                }
        except Exception as exc:
            log.warning("[MarketData] yfinance failed for %s: %s", symbol_raw, exc)

    return {"symbol": display, "kind": kind, "price": None, "source": None, "error": "no data"}


def get_klines(symbol_raw: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Get OHLCV candles. Returns list of {t, o, h, l, c, v}."""
    norm = normalize_symbol(symbol_raw)
    kind = norm["kind"]

    if kind == "crypto":
        sym = norm.get("binance_symbol", "")
        if sym:
            candles = binance_klines(sym, interval, limit)
            if candles:
                return candles

    # Fallback to yfinance for everything else
    try:
        import yfinance as yf
        # yfinance intervals: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo
        yf_interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "60m", "2h": "60m", "4h": "60m", "1d": "1d", "1w": "1wk", "1M": "1mo",
        }
        yf_period_map = {
            "1m": "5d", "5m": "1mo", "15m": "1mo", "30m": "1mo",
            "1h": "3mo", "2h": "3mo", "4h": "6mo", "1d": "2y", "1w": "5y", "1M": "10y",
        }
        yf_int = yf_interval_map.get(interval, "1h")
        yf_per = yf_period_map.get(interval, "3mo")

        ticker = yf.Ticker(norm["canonical"])
        hist = ticker.history(period=yf_per, interval=yf_int)
        if hist.empty:
            return []
        candles = []
        for idx, row in hist.iterrows():
            ts = int(idx.timestamp())
            candles.append({
                "t": ts,
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": float(row.get("Volume", 0)),
            })
        # Resample higher timeframes if requested
        if interval in ("2h", "4h", "1d", "1w", "1M") and interval not in ("1m", "5m", "15m", "30m", "1h"):
            candles = _resample(candles, interval, limit)
        return candles[-limit:]
    except Exception as exc:
        log.warning("[MarketData] yfinance klines failed for %s %s: %s", symbol_raw, interval, exc)
        return []


def _resample(candles: list[dict], interval: str, limit: int) -> list[dict]:
    """Resample candles to higher timeframe (simple OHLCV aggregation)."""
    seconds_map = {
        "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800, "1M": 2592000,
    }
    bucket = seconds_map.get(interval)
    if not bucket:
        return candles

    if not candles:
        return candles

    out = []
    current = None
    for c in candles:
        ts_bucket = (c["t"] // bucket) * bucket
        if current is None or ts_bucket > current["t"]:
            current = {"t": ts_bucket, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
            out.append(current)
        else:
            current["h"] = max(current["h"], c["h"])
            current["l"] = min(current["l"], c["l"])
            current["c"] = c["c"]
            current["v"] += c["v"]
    return out


# ── Cleanup on shutdown ───────────────────────────────────────────────────────

def save_cache() -> None:
    _save_cache()
