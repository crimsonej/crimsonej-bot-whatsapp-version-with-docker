"""
services/sandbox.py
===================
Sandboxed execution for dynamic tools.
Provides resource limits, timeout, and safe execution environment.
"""

from __future__ import annotations

import json
import resource
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Callable, Dict, Optional

from core.config import cfg, log


# ─────────────────────────────────────────────────────────────────────────────
# RESOURCE LIMITS
# ─────────────────────────────────────────────────────────────────────────────

# Default limits (configurable via environment)
DEFAULT_LIMITS = {
    "cpu_seconds": int(cfg("sandbox_cpu_seconds") or 10),      # CPU time limit
    "wall_seconds": int(cfg("sandbox_wall_seconds") or 30),    # Wall clock limit
    "memory_mb": int(cfg("sandbox_memory_mb") or 128),         # Memory limit
    "max_output_kb": int(cfg("sandbox_max_output_kb") or 512), # Output size limit
}

# Track active executions for monitoring
_active_executions = 0
_MAX_CONCURRENT = int(cfg("sandbox_max_concurrent") or 3)


def _set_resource_limits():
    """Set resource limits for current process."""
    try:
        # CPU time (soft/hard)
        resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_LIMITS["cpu_seconds"], DEFAULT_LIMITS["cpu_seconds"]))
        
        # Memory (virtual memory)
        mem_bytes = DEFAULT_LIMITS["memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        
        # File size
        max_output = DEFAULT_LIMITS["max_output_kb"] * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_output, max_output))
        
        # Number of processes
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        
        # Open files
        resource.setrlimit(resource.RLIMIT_NOFILE, (10, 10))
    except Exception as e:
        log.warning("[Sandbox] Could not set resource limits: %s", e)


class SandboxExecutor:
    """
    Safe execution environment for dynamic tools.
    Uses thread pool with timeout and resource limits.
    """
    
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT)
        self._active = 0
    
    def execute(self, func: Callable, args: tuple, kwargs: dict, 
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """
        Execute function in sandbox.
        
        Returns:
            Dict with keys: ok, result, error, execution_time_ms
        """
        global _active_executions
        
        if _active_executions >= _MAX_CONCURRENT:
            return {"ok": False, "error": "Sandbox at capacity", "result": None}
        
        wall_timeout = timeout or DEFAULT_LIMITS["wall_seconds"]
        
        _active_executions += 1
        start = time.time()
        
        try:
            # Submit to thread pool - use ThreadPoolExecutor's built-in timeout
            future = self._executor.submit(_run_with_limits, func, args, kwargs)
            result = future.result(timeout=wall_timeout)
            
            elapsed = (time.time() - start) * 1000
            return {"ok": True, "result": result, "error": None, "execution_time_ms": elapsed}
            
        except TimeoutError:
            elapsed = (time.time() - start) * 1000
            return {"ok": False, "error": f"Timeout after {wall_timeout}s", "result": None, "execution_time_ms": elapsed}
            
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "result": None, "execution_time_ms": elapsed}
            
        finally:
            _active_executions -= 1


def _run_with_limits(func: Callable, args: tuple, kwargs: dict) -> Any:
    """Run function with resource limits applied."""
    # Set limits in this thread
    _set_resource_limits()
    
    # Execute function directly - ThreadPoolExecutor handles timeout
    return func(*args, **kwargs)


# Global sandbox instance
_sandbox = SandboxExecutor()


def execute_in_sandbox(func: Callable, *args, **kwargs) -> Dict[str, Any]:
    """Execute a function in the sandbox."""
    return _sandbox.execute(func, args, kwargs)


def get_sandbox_stats() -> Dict[str, Any]:
    """Get sandbox statistics."""
    return {
        "active_executions": _active_executions,
        "max_concurrent": _MAX_CONCURRENT,
        "limits": DEFAULT_LIMITS,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SAFE BUILTINS FOR DYNAMIC TOOLS
# ─────────────────────────────────────────────────────────────────────────────

SAFE_BUILTINS = {
    # Basic types
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "frozenset": frozenset, "bytes": bytes, "bytearray": bytearray,
    
    # Math
    "sum": sum, "max": max, "min": min, "abs": abs, "round": round,
    "pow": pow, "divmod": divmod,
    
    # Iteration
    "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
    "zip": zip, "range": range, "map": map, "filter": filter,
    "all": all, "any": any,
    
    # Type checking
    "isinstance": isinstance, "issubclass": issubclass,
    "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
    "type": type, "callable": callable,
    
    # Exceptions
    "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
    "TypeError": TypeError, "AttributeError": AttributeError,
    "IndexError": IndexError, "StopIteration": StopIteration,
    
    # Safe modules
    "json": __import__("json"),
    "re": __import__("re"),
    "time": __import__("time"),
    "datetime": __import__("datetime"),
    "math": __import__("math"),
    "random": __import__("random"),
    "string": __import__("string"),
    "collections": __import__("collections"),
    "itertools": __import__("itertools"),
    "functools": __import__("functools"),
    "hashlib": __import__("hashlib"),
    "base64": __import__("base64"),
    "urllib.parse": __import__("urllib.parse"),
    "html": __import__("html"),
    "textwrap": __import__("textwrap"),
    "uuid": __import__("uuid"),
    "decimal": __import__("decimal"),
    "fractions": __import__("fractions"),
    "statistics": __import__("statistics"),
}


def get_safe_builtins() -> Dict[str, Any]:
    """Get the safe builtins dict for sandbox execution."""
    return SAFE_BUILTINS.copy()


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION WITH DYNAMIC TOOLS
# ─────────────────────────────────────────────────────────────────────────────

def compile_tool_safe(name: str, python_code: str) -> Callable:
    """
    Compile tool code with sandbox-safe builtins.
    This replaces the _compile_tool in dynamic_tools.py
    """
    namespace = {
        "__builtins__": get_safe_builtins(),
    }
    
    if "def %s" % name not in python_code:
        python_code = "def %s(args, **context):\n" % name + textwrap.indent(python_code, "    ")
    
    exec(python_code, namespace)
    
    func = namespace.get(name)
    if not callable(func):
        raise ValueError("Code did not define callable %s" % name)
    
    # Wrap in sandbox
    def sandboxed_func(args, **context):
        from services.sandbox import execute_in_sandbox
        return execute_in_sandbox(lambda: func(args, **context))
    
    return sandboxed_func