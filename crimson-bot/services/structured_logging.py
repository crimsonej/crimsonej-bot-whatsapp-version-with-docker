"""
services/structured_logging.py
==============================
Structured JSON logging for production observability.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import cfg, log


# Context variables for request tracing
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
session_id_var: ContextVar[Optional[str]] = ContextVar("session_id", default=None)


class StructuredFormatter(logging.Formatter):
    """JSON formatter with context enrichment."""
    
    def format(self, record: logging.LogRecord) -> str:
        # Base log structure
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add context variables
        request_id = request_id_var.get()
        user_id = user_id_var.get()
        session_id = session_id_var.get()
        
        if request_id:
            log_entry["request_id"] = request_id
        if user_id:
            log_entry["user_id"] = user_id
        if session_id:
            log_entry["session_id"] = session_id
        
        # Add extra fields from record
        extra_fields = {
            k: v for k, v in record.__dict__.items()
            if k not in [
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "name", "pathname", "process", "processName",
                "relativeCreated", "thread", "threadName", "exc_info",
                "exc_text", "stack_info", "getMessage"
            ]
        }
        if extra_fields:
            log_entry["extra"] = extra_fields
        
        # Add exception info
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info)
            }
        
        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_structured_logging():
    """Configure structured JSON logging."""
    # Get log level from config
    log_level = getattr(logging, cfg("log_level") or "INFO")
    
    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers = [handler]
    
    # Set specific logger levels
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    log.info("[StructuredLogging] Initialized with JSON output")


def set_request_context(request_id: str = None, user_id: str = None, session_id: str = None):
    """Set context variables for current request."""
    if request_id:
        request_id_var.set(request_id)
    if user_id:
        user_id_var.set(user_id)
    if session_id:
        session_id_var.set(session_id)


def clear_request_context():
    """Clear context variables."""
    request_id_var.set(None)
    user_id_var.set(None)
    session_id_var.set(None)


def get_context() -> Dict[str, Optional[str]]:
    """Get current context."""
    return {
        "request_id": request_id_var.get(),
        "user_id": user_id_var.get(),
        "session_id": session_id_var.get(),
    }


class StructuredLogger:
    """Wrapper for structured logging with context."""
    
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
    
    def _log(self, level: int, message: str, **extra):
        extra["context"] = get_context()
        self.logger.log(level, message, extra=extra)
    
    def debug(self, message: str, **extra):
        self._log(logging.DEBUG, message, **extra)
    
    def info(self, message: str, **extra):
        self._log(logging.INFO, message, **extra)
    
    def warning(self, message: str, **extra):
        self._log(logging.WARNING, message, **extra)
    
    def error(self, message: str, **extra):
        self._log(logging.ERROR, message, **extra)
    
    def critical(self, message: str, **extra):
        self._log(logging.CRITICAL, message, **extra)
    
    def exception(self, message: str, **extra):
        extra["exc_info"] = True
        self._log(logging.ERROR, message, **extra)


def get_structured_logger(name: str) -> StructuredLogger:
    """Get a structured logger instance."""
    return StructuredLogger(name)


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MIDDLEWARE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def log_request_response(
    request_id: str,
    user_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    request_size: int = 0,
    response_size: int = 0
):
    """Log request/response as structured event."""
    logger = get_structured_logger("crimsonej.http")
    logger.info(
        "HTTP request completed",
        request_id=request_id,
        user_id=user_id,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        request_size_bytes=request_size,
        response_size_bytes=response_size,
    )