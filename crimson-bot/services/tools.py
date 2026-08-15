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

ALL_TOOLS = [
    WEB_SEARCH_TOOL,
    ANALYZE_IMAGE_TOOL,
    GENERATE_IMAGE_TOOL,
    GENERATE_STICKER_TOOL,
    DOWNLOAD_AUDIO_TOOL,
    DOWNLOAD_VIDEO_TOOL,
    POST_STATUS_TOOL,
    UPDATE_PROFILE_TOOL,
    SELF_AWARE_TOOL,
    SCHEDULE_TASK_TOOL,
    LIST_TASKS_TOOL,
    CANCEL_TASK_TOOL,
    RUN_TASK_TOOL,
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
            search_result = search_web(query)
            search_reply = _format_search_suggestions(query, search_result)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": json.dumps(search_result)})
            return {**tool_results, "reply": search_reply}

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
            if _is_ambiguous_generation_prompt(prompt):
                return {**tool_results, "reply": "I can make it, but give me a little more detail — subject, style, mood, colors, and vibe — so I don’t guess the wrong image."}
            if vision_service:
                img_path = vision_service.generate_image_auto(prompt)
                if img_path:
                    tool_results["image_list"].append(img_path)
                    messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Success"})
                    continue
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name, "content": "Failed"})

        elif name == "generate_sticker":
            prompt = args.get("prompt", "")
            if _is_ambiguous_generation_prompt(prompt):
                return {**tool_results, "reply": "I can do that sticker, but give me a clearer prompt — subject, pose, mood, and style — so I can get the vibe right."}
            if vision_service:
                sticker_b64 = vision_service.generate_sticker_auto(prompt)
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
                        messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                         "content": menu_reply})
                    except Exception:
                        pass
                else:
                    return {**tool_results, "reply": menu_reply}
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
                        messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                                         "content": menu_reply})
                    except Exception:
                        pass
                else:
                    return {**tool_results, "reply": menu_reply}
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

        elif name == "self_aware":
            from core.eventlog import event_log
            from services.dispatcher import get_dispatcher
            look_back = int(args.get("look_back") or 20)
            events = event_log.recent(look_back)
            open_tasks = task_store.list(owner_user_id=user_id,
                                         status="pending", limit=10) if user_id else []
            recent_done = task_store.list(owner_user_id=user_id,
                                          status="done", limit=5) if user_id else []
            disp = get_dispatcher()

            payload = {
                "open_tasks": [{"id": t["id"], "name": t.get("name"),
                                "next_run_at": (t.get("schedule") or {}).get("next_run_at"),
                                "kind": t.get("kind")} for t in open_tasks],
                "recent_completed": [{"id": t["id"], "name": t.get("name"),
                                      "finished_at": t.get("finished_at")} for t in recent_done],
                "recent_events": [{"seq": e.get("seq"), "ts": e.get("iso"),
                                   "kind": e.get("kind"), "summary": e.get("summary")}
                                  for e in events],
                "dispatcher_running": bool(disp and getattr(disp, '_dispatcher_thread', None)
                                            and disp._dispatcher_thread.is_alive()),
                "task_stats": task_store.stats(),
                "reply": _format_self_aware(open_tasks, recent_done, events),
            }
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": name,
                             "content": json.dumps(payload)})

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

def _format_search_suggestions(query: str, search_result: dict) -> str:
    """Turn a search result into a confirmation prompt the user can answer."""
    if not isinstance(search_result, dict):
        return "I found a few likely matches, but I need one more clue to be sure."

    results = search_result.get("results") or []
    if not results:
        return "I checked around and nothing clear popped up. Give me the artist, exact title, or a lyric to narrow it down."

    top = results[:3]
    lines = [f"{i+1}. {r.get('title', 'Unknown').strip()[:90]}" for i, r in enumerate(top, 1)]
    label = query.strip() or "that"
    return (
        f"I found a few likely matches for '{label}':\n"
        + "\n".join(lines)
        + "\n\nDid any of these sound like the one you meant? If not, give me the artist, exact title, or a lyric and I’ll narrow it down."
    )


def _is_ambiguous_generation_prompt(prompt: str) -> bool:
    """Ambient guard for image/sticker generation prompts that are too vague to act on confidently."""
    if not prompt or not isinstance(prompt, str):
        return True
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    return len(words) <= 3


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
