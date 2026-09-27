"""
services/metrics.py
===================
Prometheus metrics for monitoring and observability.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, Dict, Optional

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
from core.config import log


# ─────────────────────────────────────────────────────────────────────────────
# METRICS DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

# HTTP metrics
http_requests_total = Counter(
    "crimsonej_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"]
)

http_request_duration = Histogram(
    "crimsonej_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# Message processing metrics
messages_received_total = Counter(
    "crimsonej_messages_received_total",
    "Total messages received from bridge",
    ["type"]  # text, image, document, audio, video, location, contact, status
)

messages_sent_total = Counter(
    "crimsonej_messages_sent_total",
    "Total messages sent to bridge",
    ["type"]  # text, image, audio, video, document, sticker
)

message_processing_duration = Histogram(
    "crimsonej_message_processing_duration_seconds",
    "Message processing latency in seconds",
    ["type"],  # text, command, media
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)

# LLM metrics
llm_requests_total = Counter(
    "crimsonej_llm_requests_total",
    "Total LLM API requests",
    ["provider", "model", "status"]  # success, error, timeout
)

llm_request_duration = Histogram(
    "crimsonej_llm_request_duration_seconds",
    "LLM API request latency in seconds",
    ["provider", "model"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0]
)

llm_tokens_total = Counter(
    "crimsonej_llm_tokens_total",
    "Total tokens used",
    ["provider", "model", "type"]  # prompt, completion, total
)

# Tool metrics
tool_calls_total = Counter(
    "crimsonej_tool_calls_total",
    "Total tool calls",
    ["tool", "status"]  # success, error, timeout
)

tool_duration = Histogram(
    "crimsonej_tool_duration_seconds",
    "Tool execution latency in seconds",
    ["tool"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

dynamic_tools_created = Counter(
    "crimsonej_dynamic_tools_created_total",
    "Total dynamic tools created",
)

dynamic_tools_executed = Counter(
    "crimsonej_dynamic_tools_executed_total",
    "Total dynamic tool executions",
    ["tool", "status"]
)

# Memory/Storage metrics
sessions_active = Gauge(
    "crimsonej_sessions_active",
    "Number of active conversation sessions"
)

profiles_total = Gauge(
    "crimsonej_profiles_total",
    "Total user profiles stored"
)

vaults_total = Gauge(
    "crimsonej_vaults_total",
    "Total vaults (personal + global)"
)

vectors_total = Gauge(
    "crimsonej_vectors_total",
    "Total RAG vector chunks"
)

dynamic_tools_active = Gauge(
    "crimsonej_dynamic_tools_active",
    "Number of registered dynamic tools"
)

# GitHub sync metrics
github_sync_total = Counter(
    "crimsonej_github_sync_total",
    "Total GitHub sync operations",
    ["operation", "status"]  # pull/push, success/error
)

github_sync_duration = Histogram(
    "crimsonej_github_sync_duration_seconds",
    "GitHub sync duration in seconds",
    ["operation"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0]
)

# Bridge health metrics
bridge_connected = Gauge(
    "crimsonej_bridge_connected",
    "Bridge connection status (1=connected, 0=disconnected)"
)

bridge_latency = Histogram(
    "crimsonej_bridge_latency_seconds",
    "Bridge API latency in seconds",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0]
)

# Escalation/Relay metrics
escalations_total = Counter(
    "crimsonej_escalations_total",
    "Total escalations to creator",
    ["type", "status"]  # mixup/self_repair, pending/resolved
)

contact_relays_total = Counter(
    "crimsonej_contact_relays_total",
    "Total contact relay requests",
    ["status"]  # pending/approved/declined/expired
)

# Trading metrics
trading_analyses_total = Counter(
    "crimsonej_trading_analyses_total",
    "Total trading analyses performed",
    ["symbol", "timeframe"]
)

trading_briefings_sent = Counter(
    "crimsonej_trading_briefings_sent_total",
    "Total trading briefings sent",
    ["session", "status"]  # pre_london/eod, success/failed
)

# Error metrics
errors_total = Counter(
    "crimsonej_errors_total",
    "Total errors",
    ["component", "error_type"]
)

# System info
system_info = Info(
    "crimsonej_system_info",
    "System information"
)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def record_http_request(method: str, endpoint: str, status: int, duration: float):
    """Record HTTP request metrics."""
    http_requests_total.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    http_request_duration.labels(method=method, endpoint=endpoint).observe(duration)


def record_message_received(msg_type: str):
    """Record incoming message."""
    messages_received_total.labels(type=msg_type).inc()


def record_message_sent(msg_type: str):
    """Record outgoing message."""
    messages_sent_total.labels(type=msg_type).inc()


def record_message_processing(msg_type: str, duration: float):
    """Record message processing latency."""
    message_processing_duration.labels(type=msg_type).observe(duration)


def record_llm_request(provider: str, model: str, status: str, duration: float, 
                       prompt_tokens: int = 0, completion_tokens: int = 0):
    """Record LLM request metrics."""
    llm_requests_total.labels(provider=provider, model=model, status=status).inc()
    llm_request_duration.labels(provider=provider, model=model).observe(duration)
    if prompt_tokens:
        llm_tokens_total.labels(provider=provider, model=model, type="prompt").inc(prompt_tokens)
    if completion_tokens:
        llm_tokens_total.labels(provider=provider, model=model, type="completion").inc(completion_tokens)
    if prompt_tokens or completion_tokens:
        llm_tokens_total.labels(provider=provider, model=model, type="total").inc(
            prompt_tokens + completion_tokens
        )


def record_tool_call(tool: str, status: str, duration: float):
    """Record tool call metrics."""
    tool_calls_total.labels(tool=tool, status=status).inc()
    tool_duration.labels(tool=tool).observe(duration)


def record_dynamic_tool_created():
    """Record dynamic tool creation."""
    dynamic_tools_created.inc()


def record_dynamic_tool_executed(tool: str, status: str):
    """Record dynamic tool execution."""
    dynamic_tools_executed.labels(tool=tool, status=status).inc()


def record_github_sync(operation: str, status: str, duration: float):
    """Record GitHub sync metrics."""
    github_sync_total.labels(operation=operation, status=status).inc()
    github_sync_duration.labels(operation=operation).observe(duration)


def record_bridge_health(connected: bool, latency: float = None):
    """Record bridge health metrics."""
    bridge_connected.set(1 if connected else 0)
    if latency is not None:
        bridge_latency.observe(latency)


def record_escalation(esc_type: str, status: str):
    """Record escalation metrics."""
    escalations_total.labels(type=esc_type, status=status).inc()


def record_contact_relay(status: str):
    """Record contact relay metrics."""
    contact_relays_total.labels(status=status).inc()


def record_trading_analysis(symbol: str, timeframe: str):
    """Record trading analysis."""
    trading_analyses_total.labels(symbol=symbol, timeframe=timeframe).inc()


def record_trading_briefing(session: str, status: str):
    """Record trading briefing."""
    trading_briefings_sent.labels(session=session, status=status).inc()


def record_error(component: str, error_type: str):
    """Record error."""
    errors_total.labels(component=component, error_type=error_type).inc()


def update_system_gauges(sessions: int, profiles: int, vaults: int, 
                         vectors: int, dynamic_tools: int):
    """Update system gauges."""
    sessions_active.set(sessions)
    profiles_total.set(profiles)
    vaults_total.set(vaults)
    vectors_total.set(vectors)
    dynamic_tools_active.set(dynamic_tools)


def set_system_info(version: str, python_version: str, platform: str):
    """Set system info."""
    info = {
        "version": version,
        "python_version": python_version,
        "platform": platform,
    }
    # Note: prometheus_client.Info doesn't have a set method in some versions
    # This is a placeholder for the pattern


# ─────────────────────────────────────────────────────────────────────────────
# MIDDLEWARE DECORATORS
# ─────────────────────────────────────────────────────────────────────────────

def track_latency(metric: Histogram, labels: Dict[str, str] = None):
    """Decorator to track function latency."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.time() - start
                if labels:
                    metric.labels(**labels).observe(duration)
                else:
                    metric.observe(duration)
        return wrapper
    return decorator


def track_counter(metric: Counter, labels: Dict[str, str] = None):
    """Decorator to increment counter on success."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if labels:
                    metric.labels(**labels).inc()
                else:
                    metric.inc()
                return result
            except Exception as e:
                if labels:
                    error_labels = {**labels, "status": "error"}
                else:
                    error_labels = {"status": "error"}
                metric.labels(**error_labels).inc()
                raise
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def get_metrics() -> bytes:
    """Generate Prometheus metrics output."""
    return generate_latest()

def get_content_type() -> str:
    """Get Prometheus content type."""
    return CONTENT_TYPE_LATEST