# Crimsonej — AI Trading Coach + WhatsApp Bot

A pro-level trading coach that lives in your WhatsApp. Analyzes real markets, teaches concepts, tracks your journal, and briefs you daily — all from natural conversation.

## Core Identity

**Crimsonej** — your trading partner built by Crimson (Elijah). Manchester City fan. Hates Liverpool. Speaks human.

## Trading Coach Features

| Feature | Commands / Natural Language |
|---------|----------------------------|
| **Market Analysis** | `/analyze BTC 4h` — full TA with chart, bias (🟢/🔴/⏸️), key levels, patterns |
| **Multi-Timeframe** | `/mtf BTC` — HTF (Daily) / MTF (4H) / LTF (1H) alignment |
| **Pattern Detection** | `/patterns BTC 4h` — double top/bottom, H&S, flags, triangles, wedges |
| **Live Walkthrough** | `/walkthrough BTC 4h` — step-by-step HTF→LTF breakdown with action plan |
| **Lessons (13)** | `/teach risk_management` — candlesticks, structure, S/R, risk, RSI, MACD, MTF, liquidity, journaling, psychology, sessions |
| **Interactive Quiz** | `/quiz candlesticks` → `/quiz_answer candlesticks 1` — learn by doing |
| **Trade Journal** | `/journal log BTC long 50000 49000 52000 0.1 win 300 3 bull_flag` — track trades |
| **Stats Dashboard** | `/journal stats` — win rate, expectancy, avg R, setup breakdown |
| **Daily Briefings** | `/brief pre_london` (07:30 EAT) / `/brief eod` (21:30 EAT) — 9 pairs with bias |
| **Group Subscriptions** | `/briefing_subscribe both BTC ETH EURUSD` — auto-post to groups at 07:30/21:30 EAT |
| **Quick Prices** | `/price BTC ETH EURUSD GOLD` — multi-symbol check |

## Natural Language (Just Talk)

> "what's BTC doing on the 4h?"
> "teach me risk management"
> "post daily news in this group for BTC and ETH"
> "show my trading stats"
> "quiz me on candlesticks"

The LLM detects intent and calls the right tools automatically.

## Quick Start

```bash
git clone <repo>
cd crimsonej
python3 install.py        # sets up venv, deps, bridge
crimsonej start           # starts bot + bridge
```

## Commands

| CLI | Action |
|-----|--------|
| `crimsonej start` | Start bot + WhatsApp bridge |
| `crimsonej stop` | Stop everything |
| `crimsonej status` | Show PIDs, health, docs count |
| `crimsonej logs [bot\|bridge]` | Tail logs |
| `crimsonej reindex` | Rebuild RAG index from `docs/` |

## Architecture

```
WhatsApp → Bridge (Node.js) → Flask API (bot.py) → LLM + Tools
                                              ├── Market Data (Binance, CoinGecko, yfinance)
                                              ├── Technical Analysis (pure Python)
                                              ├── Chart Rendering (matplotlib)
                                              ├── Lessons / Quiz / Journal
                                              └── Scheduler (07:30/21:30 EAT)
```

## Data Sources (Free, No Keys)

- **Binance Public REST** — Crypto OHLCV, 24h ticker, order book
- **CoinGecko** — Crypto prices, market cap
- **yfinance (Yahoo Finance)** — Stocks, forex, indices, commodities, bonds

## Requirements

- Python 3.10+
- Node.js 18+ (for WhatsApp bridge)
- Groq API Key (for LLM)
- Optional: NVIDIA API Key (vision), HF API Key (images)

## Project Structure

```
crimsonej/
├── crimson-bot/
│   ├── bot.py                    # Flask app, slash commands, LLM tools
│   ├── core/
│   │   ├── market_data.py        # Binance / CoinGecko / yfinance clients
│   │   ├── trading_ta.py         # RSI, MACD, EMA, structure, patterns, MTF
│   │   └── chart_render.py       # matplotlib candlestick charts
│   ├── services/
│   │   ├── trading.py            # Analysis, lessons, journal, briefings, subs
│   │   ├── trading_scheduler.py  # Daily briefings at 07:30/21:30 EAT
│   │   └── tools.py              # 31 LLM tools (15 trading)
│   └── data/                     # lessons, watchlists, journal, subscriptions
├── whatsapp-bridge/              # Baileys Node.js bridge
└── orchestrator.py               # Process manager (crimsonej CLI)
```

## Credits

Built by **Crimson (Elijah)**. Your trading partner in the group chat.