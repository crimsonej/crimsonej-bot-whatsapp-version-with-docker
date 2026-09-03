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
import os
from functools import lru_cache

from openai import OpenAI as NvidiaOpenAI
import tiktoken

from core.config import cfg, get_nvidia_key, log

# Model constants — verified active on NVIDIA NIM API 2026-08-31
NVIDIA_BRAIN = "meta/llama-3.2-90b-vision-instruct"      # Primary 90B Llama 3.2 Flagship
NVIDIA_SCOUT = "meta/llama-3.2-11b-vision-instruct"      # Fast 11B Vision Scout (free-tier compatible)

nvidia_client: NvidiaOpenAI | None = None
_client_cache: dict[str, NvidiaOpenAI] = {}

PROVIDER_BASE_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "huggingface": "https://router.huggingface.co/v1",
}

PROVIDER_ENV_KEYS = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "huggingface": "HF_API_KEY",
}

def init_clients() -> None:
    global nvidia_client
    nvidia_client = _client_for_provider("nvidia")

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

def _provider_key(provider: str) -> str:
    provider = (provider or "").lower()
    configured = (cfg("providers") or {}).get(provider) or ""
    return configured or os.getenv(PROVIDER_ENV_KEYS.get(provider, ""), "")


def _provider_base_url(provider: str) -> str:
    provider = (provider or "").lower()
    configured = (cfg("provider_base_urls") or {}).get(provider) or ""
    return configured or PROVIDER_BASE_URLS.get(provider, "")


def _client_for_provider(provider: str) -> NvidiaOpenAI | None:
    provider = (provider or "").lower()
    key = _provider_key(provider)
    base_url = _provider_base_url(provider)
    if not key or not base_url:
        return None
    cache_key = f"{provider}:{base_url}:{key[:8]}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]
    try:
        client = NvidiaOpenAI(base_url=base_url, api_key=key, max_retries=1)
        _client_cache[cache_key] = client
        return client
    except Exception as e:
        log.warning("Could not initialize %s client: %s", provider, e)
        return None


def _infer_provider(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if "deepseek" in m:
        return "deepseek"
    if "grok" in m:
        return "xai"
    if "mistral" in m or "mixtral" in m:
        return "mistral"
    if "llama-3.3" in m or "llama3-" in m:
        return "groq"
    return "nvidia"


def _model_candidates(primary_model: str) -> list[dict[str, str]]:
    configured = cfg("models") or []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(model: str, provider: str | None = None) -> None:
        model = (model or "").strip()
        provider = (provider or _infer_provider(model)).strip().lower()
        if not model or not provider:
            return
        key = (provider, model)
        if key not in seen:
            seen.add(key)
            out.append({"provider": provider, "model": model})

    if primary_model:
        provider = None
        for item in configured:
            if isinstance(item, dict) and item.get("id") == primary_model:
                provider = item.get("provider")
                break
        add(primary_model, provider)

    for tier in ("primary", "fallback", "scout"):
        for item in configured:
            if isinstance(item, dict) and str(item.get("tier", "")).lower() == tier:
                add(str(item.get("id") or ""), str(item.get("provider") or ""))

    add(NVIDIA_BRAIN, "nvidia")
    add(NVIDIA_SCOUT, "nvidia")
    return out


def _call_provider(provider: str, messages: list, tools: list | None = None,
                   model: str = NVIDIA_BRAIN, max_tokens: int = 512,
                   timeout: float = 35.0) -> object:
    provider = (provider or "nvidia").lower()
    client = nvidia_client if provider == "nvidia" else None
    if client is None:
        client = _client_for_provider(provider)
    if not client:
        raise RuntimeError(f"{provider} client is not initialized.")
    # Sanitize model string: map deprecated or enterprise-only function IDs to working endpoints
    if provider == "nvidia":
        m_lower = (model or "").lower()
        if any(s in m_lower for s in ["scout", "11b", "8b", "nemo", "mistral-7b"]):
            model = NVIDIA_SCOUT
        elif not model or any(d in m_lower for d in ["llama-3.3", "llama-3.1", "nemotron", "versatile"]):
            model = NVIDIA_BRAIN

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    return client.chat.completions.create(**payload)


def _call_nvidia(messages: list, tools: list | None = None, model: str = NVIDIA_BRAIN, max_tokens: int = 512, timeout: float = 35.0) -> object:
    return _call_provider("nvidia", messages, tools=tools, model=model, max_tokens=max_tokens, timeout=timeout)

def call_llm(messages: list[dict[str, str]], tools: list | None = None, tool_executor_fn=None,
             user_id: str | None = None, sender_jid: str | None = None,
             max_tokens: int = 1024, timeout: float = 18.0) -> dict:
    """
    Unified multi-provider LLM calling function.
    Pipeline: NVIDIA 70B (primary) -> NVIDIA 8B (scout).
    Executes tool calls automatically using tool_executor_fn if provided.
    """
    messages = truncate_messages(messages)
    primary_model = cfg("active_model") or cfg("model") or NVIDIA_BRAIN

    for candidate in _model_candidates(primary_model):
        provider = candidate["provider"]
        model = candidate["model"]
        if not _client_for_provider(provider):
            continue
        try:
            log.info("[Brain] %s (%s)", provider, model)
            brain_response = _call_provider(provider, messages, tools=tools, model=model, max_tokens=max_tokens, timeout=timeout)
            brain_msg = brain_response.choices[0].message
            tool_calls = brain_msg.tool_calls

            if tool_calls and tool_executor_fn:
                log.info("[Brain] Tools via %s: %s", provider, [t.function.name for t in tool_calls])
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
                final = _call_provider(provider, messages, tools=None, model=model, max_tokens=min(max_tokens, 768), timeout=min(timeout, 15.0))
                content = final.choices[0].message.content or ""
                return {"reply": _strip_think(_sanitize_tool_reply(content)), **tool_results}

            content = brain_msg.content or ""
            cleaned = _sanitize_tool_reply(content)
            return {"reply": _strip_think(cleaned)}
        except Exception as exc:
            log.warning("[Brain] %s/%s failed: %s", provider, model, exc)

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
