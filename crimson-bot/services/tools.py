"""
services/tools.py
=================
Unified Tool Registry & Execution Dispatcher.
Fixes tool schema mismatches and provides robust handlers.
"""

from __future__ import annotations

import json
import os
import re

from core.config import log
from core.eventlog import event_log
from services.tasks import task_store
from realtime_search import search_web, needs_realtime_heuristic

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the live web for real-time information, news, and facts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The specific search query to look up."
                }
            },
            "required": ["query"]
        }
    }
}

ANALYZE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": "Analyze an image using AI vision to describe or answer questions about it.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_base64": {
                    "type": "string",
                    "description": "The base64 encoded image data."
                },
                "prompt": {
                    "type": "string",
                    "description": "The question or instruction for analyzing the image.",
                    "default": "Describe this image in detail."
                }
            },
            "required": ["image_base64"]
        }
    }
}

GENERATE_STICKER_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_sticker",
        "description": "Generate a custom sticker based on a text description.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The description of the sticker to generate."
                }
            },
            "required": ["prompt"]
        }
    }
}

SMART_STICKER_RESPONSE_TOOL = {
    "type": "function",
    "function": {
        "name": "smart_sticker_response",
        "description": "Analyze an incoming sticker/image using vision and generate a contextually appropriate response sticker. Analyzes the incoming sticker's content, mood, and context, then generates an appropriate response sticker.",
        "parameters": {
            "type": "object",
            "properties": {
                "sticker_base64": {
                    "type": "string",
                    "description": "The base64 encoded sticker/image data to analyze and respond to."
                },
                "context": {
                    "type": "string",
                    "description": "Optional context about the conversation or situation.",
                    "default": ""
                }
            },
            "required": ["sticker_base64"]
        }
    }
}

GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate a high-quality image based on a text description.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The detailed description of the image to generate."
                }
            },
            "required": ["prompt"]
        }
    }
}

DOWNLOAD_AUDIO_TOOL = {
    "type": "function",
    "function": {
        "name": "download_audio",
        "description": (
            "Download audio from YouTube or web link. Provide a song name or direct URL. "
            "If the user's request implies they want to see options first (e.g. 'show me versions', "
            "'which versions', 'let me pick', 'what's available'), pass the FULL request string as the "
            "query — the tool will return a numbered menu and wait for the user to pick."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The song name, the user's full request if they want to see options, or a YouTube/web URL."
                }
            },
            "required": ["query"]
        }
    }
}

DOWNLOAD_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "download_video",
        "description": (
            "Download video from YouTube or social media. Provide a video name or direct URL. "
            "If the user's request implies they want to see options first (e.g. 'show me versions', "
            "'which versions', 'let me pick', 'what's available'), pass the FULL request string as the "
            "query — the tool will return a numbered menu and wait for the user to pick."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The video name, the user's full request if they want to see options, or a YouTube/web URL."
                }
            },
            "required": ["query"]
        }
    }
}

ANALYZE_VIDEO_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_video",
        "description": (
            "Analyze a video using AI to understand its content, describe scenes, "
            "answer questions about it, or extract information. Provide a base64 encoded video "
            "or a URL. Supports mp4, webm, mov formats. The model can reason about video content, "
            "describe scenes, track objects, transcribe speech, and answer questions about the video."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "video_base64": {
                    "type": "string",
                    "description": "The base64 encoded video data."
                },
                "video_url": {
                    "type": "string",
                    "description": "A URL to the video (alternative to base64)."
                },
                "prompt": {
                    "type": "string",
                    "description": "The question or instruction for analyzing the video.",
                    "default": "Describe this video in detail. What happens, who is in it, what is being said?"
                },
                "max_duration_seconds": {
                    "type": "integer",
                    "description": "Maximum duration of video to analyze (default 60 seconds).",
                    "default": 60
                }
            },
            "required": ["prompt"]
        }
    }
}

PARSE_DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "parse_document",
        "description": (
            "Parse and extract content from documents (PDF, DOCX, PPTX, XLSX, images, etc.). "
            "Extracts text, tables, images, and structure. Can also answer questions about the document. "
            "Provide a base64 encoded document or a URL. Supports PDF, DOCX, PPTX, XLSX, TXT, images, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_base64": {
                    "type": "string",
                    "description": "The base64 encoded document data."
                },
                "document_url": {
                    "type": "string",
                    "description": "A URL to the document (alternative to base64)."
                },
                "filename": {
                    "type": "string",
                    "description": "The filename with extension (e.g., 'report.pdf', 'data.xlsx')."
                },
                "prompt": {
                    "type": "string",
                    "description": "The question or instruction for analyzing the document.",
                    "default": "Extract all text, tables, and key information from this document."
                },
                "extract_images": {
                    "type": "boolean",
                    "description": "Whether to extract images from the document (default false).",
                    "default": False
                },
                "extract_tables": {
                    "type": "boolean",
                    "description": "Whether to extract tables as structured data (default true).",
                    "default": True
                }
            },
            "required": ["prompt"]
        }
    }
}

POST_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "post_status",
        "description": "Post a text or media status update (WhatsApp story) to my status broadcast.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text content or caption of the status update."
                },
                "media_prompt": {
                    "type": "string",
                    "description": "Optional: description of an image to generate and post as the status background."
                }
            },
            "required": ["text"]
        }
    }
}

UPDATE_PREFERENCES_TOOL = {
    "type": "function",
    "function": {
        "name": "update_preferences",
        "description": "Update structured user preferences (key/value map)",
        "parameters": {
            "type": "object",
            "properties": {
                "preferences": {"type": "object", "description": "Mapping of preference keys to values"}
            },
            "required": ["preferences"]
        }
    }
}

UPDATE_PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_user_profile",
        "description": "Update the stored profile of the current user when they state their name, nickname, personal facts, or interests.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The user's self-stated real name or preferred name."
                },
                "add_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of new facts learned about the user (e.g. ['is a software engineer', 'lives in Kampala'])."
                },
                "add_interests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of interests or hobbies mentioned by the user."
                }
            }
        }
    }
}

# ── Self-awareness tools ──────────────────────────────────────────────────────
SELF_AWARE_TOOL = {
    "type": "function",
    "function": {
        "name": "self_aware",
        "description": (
            "Inspect your own state — recent events, open tasks, bridge health, "
            "and the dispatcher. Call this when you need to know 'what's happening "
            "right now' (e.g. 'did you send that song yet?', 'why isn't my reminder firing?', "
            "'what's the bridge doing?')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "look_back": {
                    "type": "integer",
                    "default": 20,
                    "description": "How many recent events to include."
                }
            }
        }
    }
}

RUN_SELF_HEAL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_self_heal",
        "description": (
            "Run a conservative self-healing pass for the bot: restart a dead dispatcher, "
            "clear stale progress messages, and requeue stale running tasks. Safe and reversible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you're triggering the self-heal run."
                }
            }
        }
    }
}

SCHEDULE_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "schedule_task",
        "description": (
            "Schedule a deferred or recurring task: a reminder, a status-post, a recurring "
            "news alert, etc. Pass natural language for when (e.g. 'every 30 minutes', "
            "'every weekday at 6pm', 'in 10 minutes', 'at 14:00'). The bot will "
            "remember and notify the user when it fires."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "When to run, in natural language. e.g. 'every 30 minutes', 'every weekday at 18:30', 'in 10 minutes', 'at 2026-08-15 09:00'."
                },
                "name": {
                    "type": "string",
                    "description": "Short label for the task (shown in notifications)."
                },
                "what": {
                    "type": "string",
                    "description": "What the task should do when it fires. Free-form description; the bot will use it to compose the action (e.g. 'ping me about forex news', 'remind me to drink water')."
                },
                "recurring": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to repeat. If false, fires once."
                }
            },
            "required": ["when", "name", "what"]
        }
    }
}

LIST_TASKS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "List the user's pending and recently completed tasks. Use to confirm scheduling worked.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_done": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include completed/failed/cancelled tasks in the result."
                }
            }
        }
    }
}

CANCEL_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_task",
        "description": "Cancel a pending task by id. Returns whether it was cancelled.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to cancel."}
            },
            "required": ["task_id"]
        }
    }
}

RUN_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "run_task",
        "description": "Trigger a task to fire immediately (before its schedule).",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task id to run now."}
            },
            "required": ["task_id"]
        }
    }
}

# ── Trading coach tools ────────────────────────────────────────────────────────
ANALYZE_MARKET_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_market",
        "description": (
            "Analyze a market (crypto, stock, forex, commodity, index) with full technical analysis. "
            "Returns structure, momentum, key levels, bias (bullish/bearish/wait) with confidence, "
            "and a rendered chart. Use when user asks about a pair, wants a read, or says 'what's X doing?'. "
            "Interval options: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w. Default 1h."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The symbol to analyze (e.g. 'BTC', 'ETH', 'EURUSD', 'GOLD', 'SPX', 'AAPL')."
                },
                "interval": {
                    "type": "string",
                    "description": "Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w. Default 1h.",
                    "default": "1h"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of candles to fetch (default 200).",
                    "default": 200
                }
            },
            "required": ["symbol"]
        }
    }
}

TEACH_CONCEPT_TOOL = {
    "type": "function",
    "function": {
        "name": "teach_concept",
        "description": (
            "Teach a trading concept from the lesson library. Topics: candlesticks, structure, "
            "support_resistance, risk_management, rsi, macd, moving_averages, volume, "
            "multi_timeframe, liquidity, journaling, psychology, market_sessions. "
            "Use when user asks 'teach me X', 'what is X', 'explain X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The lesson topic to teach."
                }
            },
            "required": ["topic"]
        }
    }
}

LIST_LESSONS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_lessons",
        "description": "List all available trading lesson topics with titles and difficulty levels.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}

MANAGE_WATCHLIST_TOOL = {
    "type": "function",
    "function": {
        "name": "manage_watchlist",
        "description": "Add, remove, or list symbols in the user's personal watchlist.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "Action to perform."
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol to add/remove (e.g. 'BTC', 'EURUSD'). Not needed for 'list'."
                }
            },
            "required": ["action"]
        }
    }
}

DAILY_BRIEFING_TOOL = {
    "type": "function",
    "function": {
        "name": "daily_briefing",
        "description": "Generate the daily trading briefing (pre-London or end-of-day). Educational market recap with bias on major pairs.",
        "parameters": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "enum": ["pre_london", "eod"],
                    "description": "Which briefing: 'pre_london' (07:30 UTC) or 'eod' (21:30 UTC).",
                    "default": "pre_london"
                }
            }
        }
    }
}

QUICK_PRICE_TOOL = {
    "type": "function",
    "function": {
        "name": "quick_price",
        "description": "Get current prices for multiple symbols at once. Fast, lightweight.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of symbols (e.g. ['BTC', 'ETH', 'EURUSD', 'GOLD'])."
                }
            },
            "required": ["symbols"]
        }
    }
}

# ── Trading Coach: Quiz & Walkthrough ──────────────────────────────────────────
QUIZ_TOOL = {
    "type": "function",
    "function": {
        "name": "trading_quiz",
        "description": "Get a trading quiz question to test your knowledge. Optional topic filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Optional topic filter (e.g. 'candlesticks', 'risk_management', 'rsi')."
                }
            }
        }
    }
}

QUIZ_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "quiz_answer",
        "description": "Submit your answer to a trading quiz question.",
        "parameters": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "The question identifier (topic)."
                },
                "answer": {
                    "type": "integer",
                    "description": "Your answer index (0, 1, 2, or 3)."
                }
            },
            "required": ["question_id", "answer"]
        }
    }
}

WALKTHROUGH_TOOL = {
    "type": "function",
    "function": {
        "name": "live_walkthrough",
        "description": "Get a step-by-step educational walkthrough of a live chart. Breaks down HTF context, levels, momentum, candle, and action plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The symbol to walk through (e.g. 'BTC', 'ETH', 'EURUSD')."
                },
                "interval": {
                    "type": "string",
                    "description": "Timeframe: 1h, 4h, 1d. Default 4h.",
                    "default": "4h"
                }
            },
            "required": ["symbol"]
        }
    }
}



MULTI_TF_TOOL = {
    "type": "function",
    "function": {
        "name": "multi_timeframe_analysis",
        "description": "Analyze a symbol across multiple timeframes (Daily, 4H, 1H) for HTF bias, MTF structure, LTF trigger.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The symbol to analyze (e.g. 'BTC', 'ETH', 'EURUSD')."
                }
            },
            "required": ["symbol"]
        }
    }
}

PATTERNS_TOOL = {
    "type": "function",
    "function": {
        "name": "detect_patterns",
        "description": "Detect chart patterns (double top/bottom, H&S, flags, triangles, wedges) on a symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The symbol to analyze (e.g. 'BTC', 'ETH', 'EURUSD')."
                },
                "interval": {
                    "type": "string",
                    "description": "Timeframe: 1h, 4h, 1d. Default 4h.",
                    "default": "4h"
                }
            },
            "required": ["symbol"]
        }
    }
}

JOURNAL_TRADE_TOOL = {
    "type": "function",
    "function": {
        "name": "journal_trade",
        "description": "Log a trade to your journal. Include symbol, side, entry, SL, TP, size, result, PnL, R-multiple, setup, notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol traded (e.g. BTC)"},
                "side": {"type": "string", "enum": ["long", "short"], "description": "Trade direction"},
                "entry": {"type": "number", "description": "Entry price"},
                "sl": {"type": "number", "description": "Stop loss price"},
                "tp": {"type": "number", "description": "Take profit price"},
                "size": {"type": "number", "description": "Position size"},
                "result": {"type": "string", "enum": ["win", "loss", "open"], "description": "Trade result"},
                "pnl": {"type": "number", "description": "Profit/loss in quote currency"},
                "r_multiple": {"type": "number", "description": "R-multiple (PnL / risk)"},
                "setup": {"type": "string", "description": "Setup name (e.g. 'bull_flag', 'h&s_break')"},
                "notes": {"type": "string", "description": "Any notes"}
            },
            "required": ["symbol", "side", "entry", "sl", "tp", "size", "result"]
        }
    }
}

JOURNAL_STATS_TOOL = {
    "type": "function",
    "function": {
        "name": "journal_stats",
        "description": "Get your trading statistics: win rate, expectancy, avg R, setup breakdown.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}

# ── Briefing Subscription Tools ────────────────────────────────────────────────
SUBSCRIBE_BRIEFING_TOOL = {
    "type": "function",
    "function": {
        "name": "subscribe_briefing",
        "description": "Subscribe the current group to daily trading briefings. Use when user says 'post daily news in this group', 'add daily briefing here', 'subscribe to briefings'. Requires group context.",
        "parameters": {
            "type": "object",
            "properties": {
                "sessions": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["pre_london", "eod"]},
                    "description": "Which briefings: pre_london (07:30 EAT), eod (21:30 EAT). Default both.",
                    "default": ["pre_london", "eod"]
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pairs to include (e.g. ['BTC', 'ETH', 'EURUSD', 'GOLD']). Max 15. Empty = all major pairs."
                }
            }
        }
    }
}

UNSUBSCRIBE_BRIEFING_TOOL = {
    "type": "function",
    "function": {
        "name": "unsubscribe_briefing",
        "description": "Unsubscribe the current group from daily trading briefings. Use when user says 'stop daily briefings', 'unsubscribe from news', 'stop posting here'. Requires group context.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}

LIST_BRIEFINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_briefings",
        "description": "List all active group briefing subscriptions. Use when user asks 'what briefings are running', 'show subscriptions'.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}

CREATE_DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "create_document",
        "description": (
            "Create, edit, modify, or convert a downloadable document file (Word .docx, PDF .pdf, "
            "PowerPoint .pptx, or Excel .xlsx). Use when the user asks you to "
            "'create a document', 'make a report', 'write a Word file', 'generate a "
            "PDF', 'make a PowerPoint', 'create an Excel sheet', or when asked to EDIT, MODIFY, "
            "UPDATE, REWRITE, or CONVERT an attached or quoted document. "
            "When editing an existing document, combine the extracted content with the requested "
            "modifications and output the complete updated document. "
            "Structure the content clearly with headings, bullet points, tables, and "
            "paragraphs. The file will be sent directly as a WhatsApp attachment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["docx", "pdf", "pptx", "xlsx"],
                    "description": "File format to create: 'docx' (Word), 'pdf', 'pptx' (PowerPoint), 'xlsx' (Excel)."
                },
                "title": {
                    "type": "string",
                    "description": "The document title or heading."
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The full document content in plain text. Use markdown-style formatting: "
                        "# for headings, - for bullets, | for tables (pipe-separated). "
                        "Write complete, detailed content as you'd want it to appear in the final document."
                    )
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename (without extension, e.g. 'market_report'). Defaults to a sanitized version of the title."
                }
            },
            "required": ["format", "title", "content"]
        }
    }
}

FETCH_URL_CONTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url_content",
        "description": "Fetch, extract, and read main article text and headers from any web link or URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The web URL to fetch and read (e.g. 'https://techcrunch.com/...')"
                }
            },
            "required": ["url"]
        }
    }
}

GET_YOUTUBE_TRANSCRIPT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_youtube_transcript",
        "description": "Fetch full text captions/transcript for a YouTube video URL or ID for instant summarization and QA.",
        "parameters": {
            "type": "object",
            "properties": {
                "url_or_id": {
                    "type": "string",
                    "description": "The YouTube video URL or 11-character video ID."
                }
            },
            "required": ["url_or_id"]
        }
    }
}

SEARCH_REDDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_reddit",
        "description": "Search Reddit posts, discussions, and community consensus for product reviews, human opinions, or topic discussions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search term or question to look up on Reddit."
                },
                "subreddit": {
                    "type": "string",
                    "description": "Optional specific subreddit name (e.g. 'technology', 'askreddit')."
                }
            },
            "required": ["query"]
        }
    }
}

SEARCH_GITHUB_TOOL = {
    "type": "function",
    "function": {
        "name": "search_github",
        "description": "Search GitHub repositories, projects, README summaries, and code solutions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The repository, tool, or code search query."
                }
            },
            "required": ["query"]
        }
    }
}

SUBSCRIBE_NEWS_FEED_TOOL = {
    "type": "function",
    "function": {
        "name": "subscribe_news_feed",
        "description": "Fetch latest news headlines from an RSS feed URL or topic ('tech', 'ai', 'crypto', 'news', 'finance').",
        "parameters": {
            "type": "object",
            "properties": {
                "feed_url_or_topic": {
                    "type": "string",
                    "description": "RSS feed URL or preset topic ('tech', 'ai', 'crypto', 'news', 'finance')."
                }
            },
            "required": ["feed_url_or_topic"]
        }
    }
}

DEEP_RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_research",
        "description": (
            "Run an autonomous deep research agent on a complex topic across multiple internet sources "
            "(Web search, Reddit, articles). Generates a detailed multi-section research report. "
            "Set export_doc=true to automatically send the research as a downloadable PDF or Word attachment!"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The complex research topic or prompt."
                },
                "export_doc": {
                    "type": "boolean",
                    "description": "Whether to export and send the research report as a document file attachment (default false)."
                },
                "format": {
                    "type": "string",
                    "enum": ["pdf", "docx"],
                    "description": "Document format if export_doc is true ('pdf' or 'docx'). Default 'pdf'."
                }
            },
            "required": ["topic"]
        }
    }
}

ALL_TOOLS = [
    WEB_SEARCH_TOOL,
    ANALYZE_IMAGE_TOOL,
    ANALYZE_VIDEO_TOOL,
    PARSE_DOCUMENT_TOOL,
    GENERATE_IMAGE_TOOL,
    GENERATE_STICKER_TOOL,
    SMART_STICKER_RESPONSE_TOOL,
    DOWNLOAD_AUDIO_TOOL,
    DOWNLOAD_VIDEO_TOOL,
    POST_STATUS_TOOL,
    UPDATE_PROFILE_TOOL,
    UPDATE_PREFERENCES_TOOL,
    SELF_AWARE_TOOL,
    RUN_SELF_HEAL_TOOL,
    SCHEDULE_TASK_TOOL,
    LIST_TASKS_TOOL,
    CANCEL_TASK_TOOL,
    RUN_TASK_TOOL,
    ANALYZE_MARKET_TOOL,
    TEACH_CONCEPT_TOOL,
    LIST_LESSONS_TOOL,
    MANAGE_WATCHLIST_TOOL,
    DAILY_BRIEFING_TOOL,
    QUICK_PRICE_TOOL,
    QUIZ_TOOL,
    QUIZ_ANSWER_TOOL,
    WALKTHROUGH_TOOL,
    MULTI_TF_TOOL,
    PATTERNS_TOOL,
    JOURNAL_TRADE_TOOL,
    JOURNAL_STATS_TOOL,
    SUBSCRIBE_BRIEFING_TOOL,
    UNSUBSCRIBE_BRIEFING_TOOL,
    LIST_BRIEFINGS_TOOL,
    CREATE_DOCUMENT_TOOL,
    FETCH_URL_CONTENT_TOOL,
    GET_YOUTUBE_TRANSCRIPT_TOOL,
    SEARCH_REDDIT_TOOL,
    SEARCH_GITHUB_TOOL,
    SUBSCRIBE_NEWS_FEED_TOOL,
    DEEP_RESEARCH_TOOL,
]

def execute_tool_calls(tool_calls, messages, user_id, sender_jid=None, media_service=None, vision_service=None) -> dict:
    """Execute tool calls and collect media results into structured dict."""
    import base64
    import requests
    tool_results = {"audio_list": [], "video_list": [], "sticker_list": [], "image_list": [], "filenames": []}

    for tool_call in tool_calls:
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except Exception:
            args = {}

        log.info("[Tool Call] Executing %s with args %s", name, args)

        if name == "web_search":
            query = args.get("query", "")
            tried = []
            search_result = None
            try:
                variants = _expand_music_queries(query) if _looks_like_media_search(query) else [query]
                for q in variants:
                    tried.append(q)
                    search_result = _search_with_retries(q)
                    if isinstance(search_result, dict) and search_result.get("results"):
                        log.info("[Search] variant matched: %r", q)
                        break
                if not isinstance(search_result, dict):
                    search_result = {}
            except Exception as exc:
                log.warning("[Search] failed for %r: %s", query, exc)
                search_result = {"ok": False, "error": str(exc), "results": []}

            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(search_result)})

            if _looks_like_media_search(query):
                search_reply = _format_search_suggestions(query, search_result, tried)
                return {**tool_results, "reply": search_reply}
            continue

        elif name == "analyze_image":
            image_base64 = args.get("image_base64", "")
            prompt = args.get("prompt", "Describe this image.")
            if vision_service:
                description = vision_service.analyze_image_with_nvidia(image_base64, prompt)
            else:
                description = "Vision service unavailable."
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": description or "Failed."})

        elif name == "generate_image":
            prompt = args.get("prompt", "")
            enriched_prompt = _enrich_image_prompt(prompt)
            if vision_service:
                img_path = vision_service.generate_image_auto(enriched_prompt)
                if img_path:
                    tool_results["image_list"].append(img_path)
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success"})
                    continue
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Failed"})

        elif name == "generate_sticker":
            prompt = args.get("prompt", "")
            enriched_prompt = _enrich_image_prompt(prompt)
            if vision_service:
                sticker_b64 = vision_service.generate_sticker_auto(enriched_prompt)
                if sticker_b64:
                    tool_results["sticker_list"].append(sticker_b64)
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success"})
                    continue
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Failed"})

        elif name == "smart_sticker_response":
            sticker_base64 = args.get("sticker_base64", "")
            context = args.get("context", "")
            if vision_service and sticker_base64:
                # First analyze the incoming sticker
                analysis = vision_service.analyze_image_with_nvidia(
                    sticker_base64,
                    "Analyze this sticker: describe the character, emotion, action, style, and mood. What would be an appropriate, contextually relevant response sticker?"
                )
                # Generate appropriate response sticker based on analysis
                response_prompt = (
                    f"Context: {context}\n"
                    f"Incoming sticker analysis: {analysis}\n"
                    f"Generate an appropriate, contextually relevant response sticker description. "
                    f"Match the style, energy, and emotional tone. Be creative but appropriate."
                )
                enriched_prompt = _enrich_image_prompt(response_prompt)
                sticker_b64 = vision_service.generate_sticker_auto(enriched_prompt)
                if sticker_b64:
                    tool_results["sticker_list"].append(sticker_b64)
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success"})
                    continue
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Failed"})

        elif name in ("download_audio", "download_youtube"):
            query = args.get("query") or args.get("url") or ""
            menu_reply = _enqueue_download_task(
                name, query, "audio", user_id, sender_jid,
                media_service, messages, tool_call
            )
            if menu_reply is not None:
                if menu_reply.startswith("{"):
                    try:
                        payload = json.loads(menu_reply)
                        if isinstance(payload, dict) and payload.get("reply"):
                            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                             "content": menu_reply})
                            return {**tool_results, "reply": str(payload["reply"])}
                        messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                         "content": menu_reply})
                    except Exception:
                        pass
                else:
                    return {**tool_results, "reply": str(menu_reply)}
                continue

        elif name in ("download_video", "download_tiktok"):
            query = args.get("query") or args.get("url") or ""
            menu_reply = _enqueue_download_task(
                name, query, "video", user_id, sender_jid,
                media_service, messages, tool_call
            )
            if menu_reply is not None:
                if menu_reply.startswith("{"):
                    try:
                        payload = json.loads(menu_reply)
                        if isinstance(payload, dict) and payload.get("reply"):
                            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                             "content": menu_reply})
                            return {**tool_results, "reply": str(payload["reply"])}
                        messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                         "content": menu_reply})
                    except Exception:
                        pass
                else:
                    return {**tool_results, "reply": str(menu_reply)}
                continue

        elif name == "post_status":
            from core.config import cfg
            if cfg("allow_status_posting") == False:
                log.warning("[Tool] post_status skipped: Status posting is disabled by Master Control.")
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Failed: Status posting is currently disabled by Master Control."})
                continue

            text = args.get("text", "")
            media_prompt = args.get("media_prompt", "")
            media_base64 = None
            mimetype = None

            if media_prompt and vision_service:
                img_path = vision_service.generate_image_auto(media_prompt)
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, "rb") as f:
                            media_base64 = base64.b64encode(f.read()).decode("utf-8")
                        mimetype = "image/png"
                        os.remove(img_path)
                    except Exception as e:
                        log.error("[Tool] Failed to read/remove generated image status: %s", e)

            try:
                requests.post("http://127.0.0.1:7860/post_status", json={
                    "text": text,
                    "media_base64": media_base64,
                    "mimetype": mimetype
                }, timeout=10)
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success"})
            except Exception as e:
                log.error("[Tool] post_status failed: %s", e)
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": f"Failed: {e}"})

        elif name == "update_user_profile":
            from services.memory import profile_mgr
            name_val = args.get("name")
            add_facts = args.get("add_facts", [])
            add_interests = args.get("add_interests", [])

            if name_val and str(name_val).strip():
                profile_mgr.set_name(user_id, str(name_val).strip())
            for fact in add_facts:
                if str(fact).strip():
                    profile_mgr.add_fact(user_id, str(fact).strip())
            for interest in add_interests:
                if str(interest).strip():
                    profile_mgr.add_interest(user_id, str(interest).strip())

            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success: Profile updated."})

        elif name == "update_preferences":
            from services.memory import profile_mgr
            prefs = args.get("preferences") or {}
            if prefs and isinstance(prefs, dict):
                profile_mgr.merge_preferences(user_id or "", prefs)
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps({"ok": True, "reply": "preferences updated"})})
            else:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps({"ok": False, "error": "invalid preferences"})})

        elif name == "self_aware":
            # Return a richer, structured health snapshot via services.health
            try:
                from services.health import get_status
                status = get_status()
                # Add recommended safe actions based on simple heuristics
                recs = []
                if not status.get("dispatcher_alive"):
                    recs.append({"action": "restart_dispatcher", "reason": "dispatcher thread not alive"})
                if status.get("running_tasks") and status.get("task_stats", {}).get("failed", 0) > 3:
                    recs.append({"action": "inspect_tasks", "reason": "multiple recent failures"})
                status["recommendations"] = recs
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps(status)})
            except Exception as e:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": str(e)})})

        elif name == "run_self_heal":
            try:
                from services.autofix import safe_auto_heal, inspect_task_health
                reason = str(args.get("reason") or "health_check")
                health = inspect_task_health()
                result = safe_auto_heal(reason)
                result["task_health"] = health
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps(result)})
            except Exception as e:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": str(e)})})

        elif name == "schedule_task":
            from services.natural_cron import parse as parse_cron
            from datetime import datetime as _dt
            from core.config import TZ as _TZ
            when = args.get("when") or ""
            name_in = args.get("name") or "reminder"
            what = args.get("what") or ""
            recurring = bool(args.get("recurring"))

            parsed = parse_cron(when)
            if not parsed:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False,
                                                        "error": f"couldn't parse 'when' = {when!r}. Try 'every 30 minutes', 'every day at 8am', 'in 10 minutes', 'at 14:00', 'every weekday at 18:30'."})})
                continue
            sched, _next_dt = parsed
            sched["user_prompt"] = what
            action_payload = {"module": "services.tasks", "fn": "run_user_reminder",
                              "kwargs": {"owner_jid": sender_jid or "",
                                          "owner_user_id": user_id or "",
                                          "what": what, "task_name": name_in}}
            t = task_store.create(
                kind="recurring" if recurring else "one_shot",
                name=name_in[:50],
                action=action_payload,
                schedule=sched,
                owner_user_id=user_id or "",
                owner_jid=sender_jid or "",
                notify_on="done",
                metadata={"what": what},
            )
            when_human = _dt.fromtimestamp(sched["next_run_at"], _TZ).strftime("%a %d %b %H:%M")
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "task_id": t["id"],
                                                    "next_run_human": when_human,
                                                    "reply": f"locked in 🔔 task #{t['id']} — next fire {when_human}"}),
                             })

        elif name == "list_tasks":
            include_done = bool(args.get("include_done"))
            user_tasks = task_store.list(owner_user_id=user_id, limit=50) if user_id else []
            if not include_done:
                user_tasks = [t for t in user_tasks if t.get("status") not in ("done", "failed", "cancelled")]
            lines = []
            for t in user_tasks[:20]:
                from datetime import datetime as _dt2
                from core.config import TZ as _TZ2
                nxt = (t.get("schedule") or {}).get("next_run_at")
                when = ""
                if nxt:
                    when = _dt2.fromtimestamp(nxt, _TZ2).strftime("%a %d %b %H:%M")
                lines.append(f"#{t['id']} {t.get('status','?')} {t.get('name','?')} — next: {when or '—'}")
            msg = "\n".join(lines) if lines else "no open tasks for you, all clean 🫡"
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"tasks": lines, "reply": msg})})

        elif name == "cancel_task":
            task_id = args.get("task_id") or ""
            t = task_store.get(task_id)
            if not t:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "no such task"})})
                continue
            # Only the owner or creator (best-effort) can cancel.
            if t.get("owner_user_id") and user_id and t["owner_user_id"] != user_id:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "not your task"})})
                continue
            cancelled = task_store.cancel(task_id)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": bool(cancelled),
                                                    "reply": f"cancelled task #{task_id}" if cancelled else "couldn't cancel (already running?)"})})

        elif name == "run_task":
            task_id = args.get("task_id") or ""
            t = task_store.get(task_id)
            if not t:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "no such task"})})
                continue
            # Force next_run_at to now so the next tick picks it up.
            task_store.update(task_id, schedule={"next_run_at": _now()})
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True,
                                                     "reply": f"firing task #{task_id} now 🔫"})})

        elif name == "analyze_market":
            from services.trading import analyze_symbol
            symbol = args.get("symbol", "")
            interval = args.get("interval", "1h")
            limit = int(args.get("limit", 200))
            if not symbol:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "symbol required"})})
                continue
            result = analyze_symbol(symbol, interval, limit)
            if "error" in result:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": result["error"]})})
            else:
                reply = (
                    f"{result['symbol']} {interval} — {result['bias'].upper()} ({result['confidence']}% conf)\n"
                    f"Price: {result['price']:,.4f} | Trend: {result['structure']}\n"
                    f"Reasons: {'; '.join(result['reasons'])}\n"
                    f"Support: {result['levels']['supports'] or '—'} | Resistance: {result['levels']['resistances'] or '—'}"
                )
                if result.get("chart_path"):
                    tool_results["image_list"].append(result["chart_path"])
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply, "data": result})})
                return {**tool_results, "reply": reply}

        elif name == "teach_concept":
            from services.trading import get_lesson
            topic = args.get("topic", "")
            if not topic:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "topic required"})})
                continue
            lesson = get_lesson(topic)
            if not lesson:
                available = ", ".join([l["topic"] for l in list_lessons()])
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": f"unknown topic. available: {available}"})})
            else:
                reply = f"**{lesson['title']}** ({lesson['level']})\n\n{lesson['content']}"
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply})})
                return {**tool_results, "reply": reply}

        elif name == "list_lessons":
            from services.trading import list_lessons as list_lessons_fn
            lessons = list_lessons_fn()
            lines = [f"• **{l['topic']}** — {l['title']} ({l['level']})" for l in lessons]
            reply = "Available lessons:\n" + "\n".join(lines)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply})})
            return {**tool_results, "reply": reply}

        elif name == "manage_watchlist":
            from services.trading import add_to_watchlist, remove_from_watchlist, get_watchlist
            action = args.get("action", "")
            symbol = args.get("symbol", "")
            if action == "list":
                wl = get_watchlist(user_id)
                reply = "Your watchlist: " + (", ".join(wl) if wl else "empty")
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply})})
                return {**tool_results, "reply": reply}
            elif action == "add":
                ok = add_to_watchlist(user_id, symbol)
                reply = f"Added {symbol} to watchlist" if ok else f"Couldn't add {symbol} (unknown symbol)"
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": ok, "reply": reply})})
                return {**tool_results, "reply": reply}
            elif action == "remove":
                ok = remove_from_watchlist(user_id, symbol)
                reply = f"Removed {symbol} from watchlist" if ok else f"{symbol} not in watchlist"
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": ok, "reply": reply})})
                return {**tool_results, "reply": reply}
            else:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "action must be add/remove/list"})})

        elif name == "daily_briefing":
            from services.trading import generate_daily_briefing
            session = args.get("session", "pre_london")
            brief = generate_daily_briefing(session)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": brief["text"]})})
            return {**tool_results, "reply": brief["text"]}

        elif name == "quick_price":
            from services.trading import quick_price_check
            symbols = args.get("symbols", [])
            if not symbols or not isinstance(symbols, list):
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "symbols array required"})})
                continue
            results = quick_price_check(symbols)
            lines = []
            for r in results:
                if r.get("price"):
                    chg = r.get("change_pct_24h", 0)
                    emoji = "🟢" if chg > 0 else ("🔴" if chg < 0 else "⚪")
                    lines.append(f"{emoji} {r['symbol']}: {r['price']:,.4f} ({chg:+.2f}%)")
                else:
                    lines.append(f"⚪ {r['symbol']}: no data")
            reply = "\n".join(lines)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply})})
            return {**tool_results, "reply": reply}

        elif name == "trading_quiz":
            from services.trading import get_quiz_question
            topic = args.get("topic")
            question = get_quiz_question(topic)
            if "error" in question:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": question["error"]})})
            else:
                reply = (
                    f"🧠 **Quiz: {question['topic'].replace('_', ' ').title()}**\n\n"
                    f"{question['question']}\n\n"
                    f"Options:\n" +
                    "\n".join(f"  {i}. {opt}" for i, opt in enumerate(question["options"])) +
                    f"\n\nReply with your answer (0-3) or use `/quiz_answer <topic> <number>`"
                )
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply, "question": question})})
                return {**tool_results, "reply": reply}

        elif name == "quiz_answer":
            from services.trading import check_quiz_answer
            question_id = args.get("question_id", "")
            user_answer = args.get("answer", -1)
            # We need the original question - for simplicity, find by topic
            from services.trading import QUIZ_QUESTIONS
            question = next((q for q in QUIZ_QUESTIONS if q["topic"] == question_id), None)
            if not question:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "invalid question_id"})})
            else:
                result = check_quiz_answer(question, user_answer)
                reply = result["message"] + "\n\n" + result["explanation"]
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply, "correct": result["correct"]})})
                return {**tool_results, "reply": reply}

        elif name == "live_walkthrough":
            from services.trading import live_walkthrough
            symbol = args.get("symbol", "")
            interval = args.get("interval", "4h")
            if not symbol:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "symbol required"})})
            else:
                result = live_walkthrough(symbol, interval)
                if "error" in result:
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                     "content": json.dumps({"ok": False, "error": result["error"]})})
                else:
                    reply = result["walkthrough"]
                    if result.get("chart_path"):
                        tool_results["image_list"].append(result["chart_path"])
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                     "content": json.dumps({"ok": True, "reply": reply, "data": result})})
                    return {**tool_results, "reply": reply}

        elif name == "multi_timeframe_analysis":
            from core.trading_ta import multi_timeframe_analysis
            symbol = args.get("symbol", "")
            if not symbol:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "symbol required"})})
            else:
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
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "reply": reply, "data": result})})
                return {**tool_results, "reply": reply}

        elif name == "detect_patterns":
            from core.trading_ta import detect_patterns
            from core.market_data import get_klines
            symbol = args.get("symbol", "")
            interval = args.get("interval", "4h")
            if not symbol:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "symbol required"})})
            else:
                candles = get_klines(symbol, interval, 100)
                if not candles or len(candles) < 20:
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                     "content": json.dumps({"ok": False, "error": "not enough data"})})
                else:
                    patterns = detect_patterns(candles)
                    if not patterns:
                        reply = f"No clear patterns detected on {symbol} {interval}."
                    else:
                        lines = [f"🔍 **Patterns on {symbol} {interval}:**"]
                        for p in patterns:
                            direction_emoji = "🟢" if p["direction"] == "bullish" else ("🔴" if p["direction"] == "bearish" else "⚪")
                            lines.append(f"{direction_emoji} **{p['type'].replace('_', ' ').title()}** ({p['confidence']}%)")
                            lines.append(f"   {p['description']}")
                        reply = "\n".join(lines)
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                     "content": json.dumps({"ok": True, "reply": reply, "patterns": patterns})})
                    return {**tool_results, "reply": reply}

        elif name == "journal_trade":
            from services.trading import add_trade_journal
            trade = {k: v for k, v in args.items() if v is not None}
            result = add_trade_journal(user_id, trade)
            reply = f"Trade logged: {trade['symbol']} {trade['side'].upper()} @ {trade['entry']} — {trade['result'].upper()}"
            if trade.get("r_multiple") is not None:
                reply += f" ({trade['r_multiple']}R)"
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply, "trade": result["trade"]})})
            return {**tool_results, "reply": reply}

        elif name == "journal_stats":
            from services.trading import get_trade_stats
            stats = get_trade_stats(user_id)
            if stats.get("total", 0) == 0:
                reply = "No trades recorded yet. Use `/journal_trade` to log your first trade."
            elif stats.get("closed_trades", 0) == 0:
                reply = f"{stats['total']} trades logged, but none closed yet."
            else:
                reply = (
                    f"📈 **Your Trading Stats**\n\n"
                    f"Total trades: {stats['total_trades']} | Closed: {stats['closed_trades']}\n"
                    f"Win rate: {stats['win_rate']}%\n"
                    f"Avg win: {stats['avg_win_r']}R | Avg loss: {stats['avg_loss_r']}R\n"
                    f"Expectancy: {stats['expectancy_r']}R per trade\n"
                    f"Total PnL: {stats['total_pnl']:.2f} | Total R: {stats['total_r']:.2f}\n"
                    f"Profitable system: {'YES ✅' if stats['profitable'] else 'NO ❌'}\n\n"
                    f"**By Setup:**"
                )
                for setup, data in stats.get("setup_breakdown", {}).items():
                    total = data["wins"] + data["losses"]
                    wr = data["wins"] / total * 100 if total else 0
                    reply += f"\n  {setup}: {data['wins']}W/{data['losses']}L ({wr:.0f}% WR) | {data['total_r']:.2f}R"
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply, "stats": stats})})
            return {**tool_results, "reply": reply}

        elif name == "subscribe_briefing":
            from services.trading import subscribe_group
            sessions = args.get("sessions", ["pre_london", "eod"])
            topics = args.get("topics", [])
            # sender_jid is the group JID in group chats
            if not sender_jid or not sender_jid.endswith("@g.us"):
                reply = "This only works in groups. Add me to a group first."
            else:
                res = subscribe_group(sender_jid, user_id, sessions, topics)
                if res["ok"]:
                    sub = res["subscription"]
                    sess_str = ", ".join(sub["sessions"])
                    topic_str = ", ".join(sub["topics"]) if sub["topics"] else "all major pairs"
                    reply = f"✅ Subscribed this group to {sess_str} briefing with: {topic_str}"
                else:
                    reply = f"Failed: {res.get('message', 'unknown error')}"
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply})})
            return {**tool_results, "reply": reply}

        elif name == "unsubscribe_briefing":
            from services.trading import unsubscribe_group
            if not sender_jid or not sender_jid.endswith("@g.us"):
                reply = "Run this in the group you want to unsubscribe."
            else:
                res = unsubscribe_group(sender_jid)
                reply = res["message"]
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply})})
            return {**tool_results, "reply": reply}

        elif name == "list_briefings":
            from services.trading import list_subscriptions
            subs = list_subscriptions()
            if not subs:
                reply = "No active group subscriptions."
            else:
                lines = ["📋 **Active Briefing Subscriptions:**"]
                for s in subs:
                    topics = ", ".join(s["topics"]) if s["topics"] else "all major"
                    lines.append(f"  {s['group_jid'].split('@')[0]} — {', '.join(s['sessions'])} — {topics}")
                reply = "\n".join(lines)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps({"ok": True, "reply": reply})})
            return {**tool_results, "reply": reply}

        elif name == "analyze_video":
            # Accept both tool schema names and bridge's field names
            video_base64 = args.get("video_base64", "") or args.get("video_data", "") or args.get("video", "")
            video_url = args.get("video_url", "")
            prompt = args.get("prompt", "Describe this video in detail. What happens, who is in it, what is being said?")
            max_duration = int(args.get("max_duration_seconds", 60))
            
            if not video_base64 and not video_url:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "video_base64 or video_url required"})})
            else:
                from services.vision import analyze_video_with_nvidia
                description = analyze_video_with_nvidia(
                    video_base64=video_base64,
                    video_url=video_url,
                    prompt=prompt,
                    max_duration_seconds=max_duration
                )
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": description or "Failed."})

        elif name == "create_document":
            from services.doc_writer import create_document_file
            doc_format  = args.get("format", "docx")
            doc_title   = args.get("title", "Document")
            doc_content = args.get("content", "")
            doc_fname   = args.get("filename") or None

            result = create_document_file(doc_format, doc_title, doc_content, doc_fname)
            if result:
                file_path, out_filename = result
                tool_results.setdefault("document_list", []).append({
                    "path": file_path,
                    "filename": out_filename,
                    "format": doc_format,
                })
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": True, "filename": out_filename,
                                                        "reply": f"here you go 📎 *{out_filename}* — sending it now"})})
                return {**tool_results,
                        "reply": f"here you go 📎 *{out_filename}* — sending it now"}
            else:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "document generation failed"})})

        elif name == "fetch_url_content":
            from services.web_reader import fetch_url_content as read_url
            url = args.get("url", "")
            res = read_url(url)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(res)})
            if res.get("ok") and res.get("text"):
                return {**tool_results, "reply": f"📰 *{res.get('title','Article')}* ({res.get('domain','')})\n\n{res['text'][:3000]}..."}

        elif name == "get_youtube_transcript":
            from services.yt_transcript import get_youtube_transcript as get_yt_trans
            url_or_id = args.get("url_or_id", "")
            res = get_yt_trans(url_or_id)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(res)})
            if res.get("ok") and res.get("transcript"):
                return {**tool_results, "reply": f"🎬 *YouTube Transcript ({res.get('duration_mins',0)} mins)*:\n\n{res['transcript'][:3000]}..."}

        elif name == "search_reddit":
            from services.reddit_scraper import search_reddit as reddit_srch
            query = args.get("query", "")
            sub = args.get("subreddit", "")
            res = reddit_srch(query, sub)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(res)})

        elif name == "search_github":
            from services.github_search import search_github as gh_srch
            query = args.get("query", "")
            res = gh_srch(query)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(res)})

        elif name == "subscribe_news_feed":
            from services.rss_watchdog import fetch_rss_feed
            feed = args.get("feed_url_or_topic", "")
            res = fetch_rss_feed(feed)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(res)})

        elif name == "deep_research":
            from services.deep_research import run_deep_research_task
            topic = args.get("topic", "")
            export_doc = bool(args.get("export_doc", False))
            fmt = args.get("format", "pdf")
            t = task_store.create(
                kind="background",
                name=f"deep_research: {topic[:40]}",
                action={"module": "services.deep_research", "fn": "run_deep_research_task",
                        "kwargs": {"topic": topic, "export_doc": export_doc, "format": fmt}},
                owner_user_id=user_id or "",
                owner_jid=sender_jid or "",
                notify_on="done",
                metadata={"topic": topic, "export_doc": export_doc, "format": fmt},
            )
            result = {"ok": True, "task_id": t["id"],
                      "reply": f"I’m on it. I’ll send the research results here when task #{t['id']} finishes."}
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps(result)})
            return {**tool_results, "reply": result["reply"]}

        elif name == "parse_document":
            # Accept both the tool schema names and the bridge's field names
            document_base64 = args.get("document_base64", "") or args.get("document_data", "") or args.get("document", "")
            document_url = args.get("document_url", "")
            filename = args.get("filename", "") or args.get("document_name", "")
            prompt = args.get("prompt", "Extract all text, tables, and key information from this document.")
            extract_images = args.get("extract_images", False)
            extract_tables = args.get("extract_tables", True)
            
            # If no base64 provided, try to get the latest document from bot's doc_session
            if not document_base64 and not document_url:
                try:
                    from bot import doc_session
                    # Get the most recent document from doc_session
                    if doc_session:
                        # Get the most recently added document
                        latest_doc = None
                        for key, doc in doc_session.items():
                            if doc.get("base64"):
                                latest_doc = doc
                                break
                        if latest_doc:
                            document_base64 = latest_doc.get("base64", "")
                            if not filename:
                                filename = latest_doc.get("name", "document")
                            log.info(f"[Tool] Using document from session: {filename}")
                except ImportError:
                    pass
                except Exception as e:
                    log.warning(f"[Tool] Could not get document from session: {e}")
            
            if not document_base64 and not document_url:
                messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                 "content": json.dumps({"ok": False, "error": "document_base64 or document_url required"})})
            else:
                from services.vision import parse_document_with_nvidia
                result = parse_document_with_nvidia(
                    document_base64=document_base64,
                    document_url=document_url,
                    filename=filename,
                    prompt=prompt,
                    extract_images=extract_images,
                    extract_tables=extract_tables
                )
                if isinstance(result, dict) and result.get("text"):
                    reply = result["text"]
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(result)})
                    return {**tool_results, "reply": reply}
                else:
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": result or "Failed."})

    return tool_results


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now() -> float:
    import time
    return time.time()


def _enqueue_download_task(tool_name: str, query: str, media_type: str,
                           user_id: str, sender_jid: str | None,
                           media_service, messages, tool_call) -> str | None:
    """Shared logic for download_audio / download_video.

    Returns:
      - the menu string (when user wants to see options first)
      - a JSON-stringified tool result (when enqueued as a background task)
      - None when nothing matched (caller will fall through to existing path)
    """
    from services.tasks import task_store
    from core.eventlog import event_log
    if not query:
        return json.dumps({"ok": False, "error": "empty query"})

    if not media_service:
        return json.dumps({"ok": False, "error": "media service unavailable"})

    # Direct URL → enqueue background download
    if re.match(r'^https?://', query):
        url_label = query.split("/")[-1][:30] or "from link"
        t = task_store.create(
            kind="background",
            name=f"download_{media_type}",
            action={"module": "services.media", "fn": "download_youtube_task",
                    "kwargs": {"url": query, "media_type": media_type,
                               "owner_jid": sender_jid or "",
                               "owner_user_id": user_id or "",
                               "task_id": "TBD"},
                    "progress_label": (f"🎬 downloading {url_label}" if media_type == "video"
                                        else f"🎵 downloading {url_label}")},
            owner_user_id=user_id or "",
            owner_jid=sender_jid or "",
            notify_on="done",
            metadata={"url": query, "media_type": media_type},
        )
        event_log.append("tool", "task_enqueued",
                         summary=f"{media_type} download task #{t['id']} queued",
                         user_id=user_id or None, jid=sender_jid or None,
                         payload={"task_id": t["id"], "kind": f"download_{media_type}"})
        return json.dumps({"task_id": t["id"],
                            "reply": f"on it 🎬 (task #{t['id']})" if media_type == "video"
                                     else f"on it 🎵 (task #{t['id']})"})

    # Ambiguous query → show the version list and let the user pick (sync menu).
    if media_service.user_wants_versions(query):
        results = media_service.search_youtube_with_versions(query, media_type=media_type, limit=5)
        if results:
            from bot import pending_song_searches  # avoid cycles
            pending_song_searches[user_id] = {"type": media_type, "results": results}
            menu = media_service.format_version_list(results, media_type)
            return menu
        return ("couldn't find anything on YouTube for that 😭 try a different name?")

    # Specific query → background download top result
    results = media_service.search_youtube(query, limit=5, media_type=media_type)
    if not results:
        return "couldn't find anything on YouTube for that 😭 try a different name?"

    chosen = results[0]
    title_short = (chosen.get("title") or "")[:30]
    t = task_store.create(
        kind="background",
        name=f"download_{media_type}",
        action={"module": "services.media", "fn": "download_youtube_task",
                "kwargs": {"url": chosen["url"], "media_type": media_type,
                           "owner_jid": sender_jid or "",
                           "owner_user_id": user_id or "",
                           "task_id": "TBD"},
                "progress_label": (f"🎬 downloading {title_short}" if media_type == "video"
                                    else f"🎵 downloading {title_short}")},
        owner_user_id=user_id or "",
        owner_jid=sender_jid or "",
        notify_on="done",
        metadata={"query": query, "title": chosen.get("title"), "media_type": media_type},
    )
    event_log.append("tool", "task_enqueued",
                     summary=f"{media_type} download task #{t['id']} queued for '{chosen.get('title')}'",
                     user_id=user_id or None, jid=sender_jid or None,
                     payload={"task_id": t["id"], "title": chosen.get("title"),
                              "kind": f"download_{media_type}"})
    return json.dumps({
        "task_id": t["id"],
        "title": chosen.get("title"),
        "reply": f"on it {'🎬' if media_type == 'video' else '🎵'} (task #{t['id']})",
    })


def _format_self_aware(open_tasks: list, recent_done: list, events: list) -> str:
    lines = []
    if open_tasks:
        lines.append(f"📋 {len(open_tasks)} open task(s):")
        for t in open_tasks[:5]:
            lines.append(f"  - #{t['id']} {t.get('name','?')} ({t.get('kind','?')})")
    else:
        lines.append("📋 no open tasks")
    if recent_done:
        lines.append(f"✅ recently done ({len(recent_done)}):")
        for t in recent_done[:3]:
            lines.append(f"  - #{t['id']} {t.get('name','?')}")
    if events:
        lines.append("📰 last few events:")
        for e in events[-5:]:
            lines.append(f"  - [{e.get('kind')}] {e.get('summary','')[:100]}")
    return "\n".join(lines)

def _format_search_suggestions(query: str, search_result: dict, tried: list | None = None) -> str:
    """Turn a search result into a confirmation prompt the user can answer.

    If `tried` is provided, include which search variants were attempted when nothing matched.
    """
    # Backwards-compatible: if caller passed results list, normalize
    if isinstance(search_result, list):
        search_result = {"results": search_result}

    # If the search produced an error or empty response, provide a helpful fallback
    if not isinstance(search_result, dict):
        return "I found a few likely matches, but I need one more clue to be sure."

    if search_result.get("error"):
        return "I tried searching but hit a problem (network or timeout). Give me the artist, exact title, or a lyric and I’ll try again."

    results = search_result.get("results") or []
    if not results:
        tried_info = f" Tried variants: {', '.join(tried[:5])}." if tried else ""
        return ("I checked around and nothing clear popped up." + tried_info + " Give me the artist, exact title, or a lyric to narrow it down.")

    top = results[:3]
    lines = [f"{i+1}. {r.get('title', 'Unknown').strip()[:90]}" for i, r in enumerate(top, 1)]
    label = query.strip() or "that"
    return (
        f"I found a few likely matches for '{label}':\n"
        + "\n".join(lines)
        + "\n\nDid any of these sound like the one you meant? If not, give me the artist, exact title, or a lyric and I’ll narrow it down."
    )


def _expand_music_queries(query: str) -> list:
    """Generate reasonable query variants for short music queries.

    Strategy:
    - include the raw query
    - add suffixes: 'song', 'lyrics', 'original song', 'official'
    - try simple spelling/vowel variants for short single-word titles
    - if query includes words like 'female' or 'male', include 'female singer' variants
    """
    if not query or not isinstance(query, str):
        return [query]

    q = query.strip()
    q_lower = q.lower()
    variants = [q]

    # common suffixes
    suffixes = ["song", "lyrics", "original song", "official", "official audio"]
    for s in suffixes:
        variants.append(f"{q} {s}")

    # handle short single-word titles by generating vowel variants
    tokens = re.findall(r"[a-z0-9]+", q_lower)
    if len(tokens) == 1 and len(tokens[0]) <= 8:
        w = tokens[0]
        vowels = "aeiou"
        for i, ch in enumerate(w):
            if ch in vowels:
                for v in vowels:
                    if v != ch:
                        alt = w[:i] + v + w[i+1:]
                        variants.append(alt)
                        variants.append(f"{alt} song")

    # respect user hints like 'female' or 'male'
    if re.search(r"\bfemale\b|\bshe\b|\bhers\b", q_lower):
        core = re.sub(r"\bfemale\b|\bshe\b|\bhers\b", "", q_lower).strip()
        if core:
            variants.insert(0, f"{core} female singer")

    # keep unique and reasonable length
    seen = set()
    out = []
    for v in variants:
        v = v.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= 8:
            break
    return out


def _looks_like_media_search(query: str) -> bool:
    q = (query or "").lower()
    return bool(re.search(r"\b(song|track|audio|music|lyrics|artist|youtube|video|download|mp3|mp4)\b", q))


def _is_ambiguous_generation_prompt(prompt: str) -> bool:
    """Ambient guard for image/sticker generation prompts that are too vague to act on confidently."""
    if not prompt or not isinstance(prompt, str):
        return True
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    return len(words) <= 3


def _enrich_image_prompt(prompt: str) -> str:
    """Enrich simple or vague prompts into high-quality, vivid visual descriptions for image generation."""
    prompt = (prompt or "").strip()
    if not prompt:
        return "A beautiful high quality digital artwork, cinematic lighting, photorealistic, 8k resolution"
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    if len(words) <= 4:
        return f"A highly detailed, vibrant digital artwork of {prompt}, dramatic cinematic lighting, rich textures, photorealistic, 8k resolution"
    return prompt


def _is_ambiguous_media_query(query: str) -> bool:
    """Reject vague media/tool requests before any search/download task is launched."""
    if not query or not isinstance(query, str):
        return True

    q = re.sub(r"[^a-z0-9\s]", " ", query.lower()).strip()
    if not q:
        return True

    media_terms = ["song", "track", "music", "audio", "video", "download", "find me", "help me get", "search"]
    if not any(term in q for term in media_terms):
        return False

    specific_terms = ["artist", "lyrics", "album", "year", "link", "http", "youtube", "spotify", "feat", "ft", "by "]
    if any(term in q for term in specific_terms):
        return False

    if re.search(r"\b(called|named|its called|it's called|it's named)\b", q):
        return True

    tokens = re.findall(r"[a-z0-9]+", q)
    return len(tokens) <= 2


def _search_with_retries(query: str, max_retries: int = 3) -> dict:
    """Perform a web search with retry and backoff on failure."""
    from time import sleep
    from random import random

    last_exception = None
    for attempt in range(max_retries):
        try:
            result = search_web(query)
            if isinstance(result, dict) and result.get("results"):
                return result
            log.warning("[Search] attempt %d: no results for query: %r", attempt + 1, query)
        except Exception as e:
            last_exception = e
            log.warning("[Search] attempt %d: exception: %s", attempt + 1, e)

        # Exponential backoff: 100ms, 400ms, 900ms, then give up
        sleep_time = (2 ** attempt + random()) / 1000.0
        sleep(sleep_time)

    log.error("[Search] all attempts failed for query: %r", query)
    if last_exception:
        raise last_exception
    return {}
