"""
core/llm.py
===========
LLM client initialization, fallback pipeline, and token truncation.
Primary engine: NVIDIA NIM (nvidia/llama-3.1-nemotron-70b-instruct)
Fallback: NVIDIA NIM (nvidia/llama-3.1-nemotron-51b-instruct)
"""

from __future__ import annotations

import random
import time
import re
from functools import lru_cache

from openai import OpenAI as NvidiaOpenAI
import tiktoken

from core.config import cfg, get_nvidia_key, log

# Model constants — verified active on NVIDIA NIM API 2026-08-31
NVIDIA_BRAIN = "meta/llama-3.2-90b-vision-instruct"      # Primary 90B Llama 3.2 Flagship
NVIDIA_SCOUT = "nv-mistralai/mistral-nemo-12b-instruct"  # Fast 12B Scout

nvidia_client: NvidiaOpenAI | None = None

def init_clients() -> None:
    global nvidia_client
    n_key = get_nvidia_key()

    if n_key:
        try:
            nvidia_client = NvidiaOpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=n_key
            )
        except Exception as e:
            log.warning("Could not initialize NVIDIA client: %s", e)

init_clients()

# ── Token-based Truncation ───────────────────────────────────────────────────

MAX_SYSTEM_TOKENS = 8000
MAX_HISTORY_MSG_TOKENS = 800
MAX_USER_MSG_TOKENS = 2000
MAX_CONTEXT_TOKENS = 1500
MAX_SEARCH_TOKENS = 800

@lru_cache(maxsize=4)
def _get_encoder(model_name: str):
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")

def truncate_to_tokens(text: str, max_tokens: int = 2000, model_name: str | None = None) -> str:
    if not text:
        return text
    enc = _get_encoder(model_name or cfg("model"))
    tokens = enc.encode(text)
    if len(tokens) > max_tokens:
        tokens = tokens[:max_tokens]
        return enc.decode(tokens) + " ... [truncated]"
    return text

def truncate_messages(messages: list[dict], max_tokens: int = MAX_HISTORY_MSG_TOKENS) -> list[dict]:
    model = cfg("model")
    truncated = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            if msg.get("role") == "system":
                truncated.append(msg)
                continue
            truncated.append({**msg, "content": truncate_to_tokens(msg["content"], max_tokens, model)})
        else:
            truncated.append(msg)
    return truncated

def _strip_think(text: str) -> str:
    """Strip <think>...</think> blocks from LLM output.
    
    Handles:
    - Complete blocks:  <think>...</think>
    - Unclosed blocks:  <think>...EOF  (model timed out mid-generation)
    - Nested/multiple: multiple complete or partial blocks
    """
    if "<think>" not in text:
        return text
    # First pass: strip complete blocks (non-greedy to handle multiple)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Second pass: strip any leftover unclosed opening tag and everything after it
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return text.strip()

def _call_nvidia(messages: list, tools: list | None = None, model: str = NVIDIA_BRAIN, max_tokens: int = 1024, timeout: float = 20.0) -> object:
    if not nvidia_client:
        raise RuntimeError("NVIDIA client is not initialized.")
    if not model or any(d in model for d in ["llama-3.3", "llama-3.1-8b", "versatile"]):
        model = NVIDIA_BRAIN
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    return nvidia_client.chat.completions.create(**payload)

def call_llm(messages: list[dict[str, str]], tools: list | None = None, tool_executor_fn=None, user_id: str | None = None, sender_jid: str | None = None) -> dict:
    """
    Unified multi-provider LLM calling function.
    Pipeline: NVIDIA 70B (primary) -> NVIDIA 8B (scout).
    Executes tool calls automatically using tool_executor_fn if provided.
    """
    messages = truncate_messages(messages)
    primary_model = cfg("active_model") or cfg("model") or NVIDIA_BRAIN

    # ── Primary: NVIDIA 70B ──────────────────────────────────────────────────
    if nvidia_client:
        try:
            log.info("[Brain] NVIDIA (%s)", primary_model)
            brain_response = _call_nvidia(messages, tools=tools, model=primary_model, max_tokens=1024, timeout=60.0)
            brain_msg = brain_response.choices[0].message
            tool_calls = brain_msg.tool_calls

            if tool_calls and tool_executor_fn:
                log.info("[Brain] Tools: %s", [t.function.name for t in tool_calls])
                messages.append(brain_msg)
                tool_results = tool_executor_fn(tool_calls, messages, user_id, sender_jid)
                if isinstance(tool_results, dict):
                    reply_value = tool_results.get("reply")
                    if isinstance(reply_value, str):
                        tool_results["reply"] = _sanitize_tool_reply(reply_value)
                # If tool executor already prepared a complete reply (menu, search results, etc.),
                # use it directly without calling scout for synthesis.
                if tool_results.get("reply"):
                    return tool_results
                # Otherwise, do final synthesis using the primary 70B model to preserve high quality and tone consistency
                final = _call_nvidia(messages, tools=None, model=primary_model, max_tokens=768, timeout=20.0)
                content = final.choices[0].message.content or ""
                return {"reply": _strip_think(_sanitize_tool_reply(content)), **tool_results}

            content = brain_msg.content or ""
            cleaned = _sanitize_tool_reply(content)
            return {"reply": _strip_think(cleaned)}
        except Exception as exc:
            log.warning("[Brain] NVIDIA 70B failed: %s – trying 8B...", exc)

        # ── Fallback: NVIDIA 8B Scout ────────────────────────────────────────
        try:
            log.info("[Fallback] NVIDIA 8B")
            scout_response = _call_nvidia(messages, tools=tools, model=NVIDIA_SCOUT, max_tokens=512, timeout=10.0)
            scout_msg = scout_response.choices[0].message
            tool_calls = scout_msg.tool_calls

            if tool_calls and tool_executor_fn:
                log.info("[Fallback] Tools: %s", [t.function.name for t in tool_calls])
                messages.append(scout_msg)
                tool_results = tool_executor_fn(tool_calls, messages, user_id, sender_jid)
                if isinstance(tool_results, dict):
                    reply_value = tool_results.get("reply")
                    if isinstance(reply_value, str):
                        tool_results["reply"] = _sanitize_tool_reply(reply_value)
                # If tool executor prepared a complete reply, return it immediately
                if tool_results.get("reply"):
                    return tool_results
                return {"reply": "", **tool_results}

            content = scout_msg.content or ""
            cleaned = _sanitize_tool_reply(content)
            return {"reply": _strip_think(cleaned)}
        except Exception as exc:
            log.warning("[Fallback] NVIDIA 8B failed: %s", exc)

    return {"reply": "aye my connection's trippin rn, give me a sec 📡"}


def scout_quick_call(messages: list[dict[str, str]], tools: list | None = None, max_tokens: int = 256, timeout: float = 3.0) -> dict | None:
    """Fast direct call to the NVIDIA 8B scout for low-latency replies.
    Returns a dict like {"reply": str} on success, or None on failure/timeout.
    """
    if not nvidia_client:
        return None
    try:
        resp = _call_nvidia(messages, tools=tools, model=NVIDIA_SCOUT, max_tokens=max_tokens, timeout=timeout)
        msg = resp.choices[0].message
        content = getattr(msg, "content", "") or ""
        return {"reply": _strip_think(content), "tool_calls": getattr(msg, "tool_calls", None)}
    except Exception as e:
        log.info("[Scout Quick] quick scout call failed: %s", e)
        return None

def _sanitize_tool_reply(reply: object) -> str:
    """Reject raw tool payloads and fake placeholder links before they reach the user."""
    if reply is None:
        return ""
    text = str(reply).strip()
    if not text:
        return ""
    lower = text.lower()
    if any(phrase in lower for phrase in [
        "don't have a specific function",
        "no specific function to call",
        "don't have a tool to",
        "no tool available to",
        "cannot invoke a function",
        "no function to call",
        "what you are trying to accomplish"
    ]):
        return "Yo! What’s good? What are we getting into today? 😎"
    return text

