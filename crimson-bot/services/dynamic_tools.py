"""
services/dynamic_tools.py
=========================
Dynamic Tool System.
Allows the bot to create, register, and persist its own tools at runtime.
Tools are stored as Python files in GitHub backup repo and SQLite.
"""

from __future__ import annotations

import json
import textwrap
import time
from typing import Any, Callable, Dict, List, Optional, Set

from core.config import cfg, log
from services.github_search import github_upsert_file, github_get_file, github_list_files
from services.storage import profile_get, profile_update
from services.sandbox import compile_tool_safe, get_safe_builtins


def _compile_tool(name: str, python_code: str) -> Callable:
    """Compile Python code into a callable function with sandbox."""
    # Use sandbox-safe builtins
    from services.sandbox import get_safe_builtins, execute_in_sandbox
    
    namespace = {
        "__builtins__": get_safe_builtins(),
    }
    
    if "def %s" % name not in python_code:
        python_code = "def %s(args, **context):\n" % name + textwrap.indent(python_code, "    ")
    
    exec(python_code, namespace)
    
    func = namespace.get(name)
    if not callable(func):
        raise ValueError("Code did not define callable %s" % name)
    
    # Wrap in sandbox executor
    def sandboxed_func(args, **context):
        from services.sandbox import execute_in_sandbox
        return execute_in_sandbox(lambda: func(args, **context))
    
    return sandboxed_func


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC TOOL REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class DynamicToolRegistry:
    """
    Manages dynamically created tools.
    Tools are Python functions with JSON schemas.
    """

    def __init__(self):
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.loaded_names: Set[str] = set()
        # Auto-load from SQLite on initialization
        self._load_from_sqlite()

    def _persist_tool(self, name: str, schema: Dict, source: str, metadata: Dict) -> None:
        """Persist tool to SQLite via storage service."""
        try:
            from services.storage import get_conn
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO dynamic_tools 
                       (name, schema, source, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, json.dumps(schema), source, json.dumps(metadata or {}), time.time())
                )
        except Exception as e:
            log.warning("[DynamicTools] Failed to persist %s to SQLite: %s", name, e)

    def _delete_tool(self, name: str) -> None:
        """Delete tool from SQLite."""
        try:
            from services.storage import get_conn
            with get_conn() as conn:
                conn.execute("DELETE FROM dynamic_tools WHERE name = ?", (name,))
        except Exception as e:
            log.warning("[DynamicTools] Failed to delete %s from SQLite: %s", name, e)

    def _load_from_sqlite(self) -> int:
        """Load all tools from SQLite."""
        try:
            from services.storage import get_conn
            with get_conn() as conn:
                cur = conn.execute("SELECT name, schema, source, metadata, created_at FROM dynamic_tools")
                loaded = 0
                for row in cur.fetchall():
                    name, schema_json, source, metadata_json, created_at = row
                    try:
                        schema = json.loads(schema_json)
                        metadata = json.loads(metadata_json or "{}")
                        func = _compile_tool(name, source)
                        self.tools[name] = {
                            "schema": schema,
                            "func": func,
                            "source": source,
                            "metadata": metadata,
                            "created_at": created_at,
                            "dynamic": True
                        }
                        self.loaded_names.add(name)
                        loaded += 1
                    except Exception as e:
                        log.warning("[DynamicTools] Failed to load %s from SQLite: %s", name, e)
                return loaded
        except Exception as e:
            log.warning("[DynamicTools] Failed to load from SQLite: %s", e)
            return 0

    def register(self, name: str, schema: Dict, func: Callable, source: str, metadata: Dict = None) -> bool:
        """Register a dynamic tool."""
        if name in self.loaded_names:
            log.warning("[DynamicTools] Tool %s already exists, overwriting", name)

        if not self._validate_schema(schema):
            log.error("[DynamicTools] Invalid schema for %s", name)
            return False

        self.tools[name] = {
            "schema": schema,
            "func": func,
            "source": source,
            "metadata": metadata or {},
            "created_at": time.time(),
            "dynamic": True
        }
        self.loaded_names.add(name)
        self._persist_tool(name, schema, source, metadata or {})
        log.info("[DynamicTools] Registered dynamic tool: %s", name)
        # Record metrics
        try:
            from services.metrics import record_dynamic_tool_created
            record_dynamic_tool_created()
        except Exception:
            pass
        return True

    def _validate_schema(self, schema: Dict) -> bool:
        """Basic JSON schema validation."""
        required = ["type", "function"]
        if not all(k in schema for k in required):
            return False
        func = schema.get("function", {})
        return all(k in func for k in ["name", "description", "parameters"])

    def unregister(self, name: str) -> bool:
        """Remove a dynamic tool."""
        if name in self.tools and self.tools[name].get("dynamic"):
            del self.tools[name]
            self.loaded_names.discard(name)
            self._delete_tool(name)
            log.info("[DynamicTools] Unregistered: %s", name)
            return True
        return False

    def get_schema(self, name: str) -> Optional[Dict]:
        """Get tool schema for LLM."""
        if name in self.tools:
            return self.tools[name]["schema"]
        return None

    def get_all_schemas(self) -> List[Dict]:
        """Get all dynamic tool schemas for LLM."""
        return [t["schema"] for t in self.tools.values() if t.get("dynamic")]

    def execute(self, name: str, args: Dict, **context) -> Any:
        """Execute a dynamic tool."""
        if name not in self.tools:
            raise ValueError("Tool %s not found" % name)
        
        start = time.time()
        func = self.tools[name]["func"]
        try:
            result = func(args, **context)
            # Record success metrics
            try:
                from services.metrics import record_dynamic_tool_executed
                record_dynamic_tool_executed(name, "success")
            except Exception:
                pass
            return result
        except Exception as e:
            # Record error metrics
            try:
                from services.metrics import record_dynamic_tool_executed
                record_dynamic_tool_executed(name, "error")
            except Exception:
                pass
            raise

    def list_dynamic(self) -> List[Dict]:
        """List all dynamic tools."""
        return [
            {"name": name, **t["metadata"], "created_at": t["created_at"]}
            for name, t in self.tools.items() if t.get("dynamic")
        ]


# Global registry
_dynamic_registry = DynamicToolRegistry()


def get_dynamic_registry() -> DynamicToolRegistry:
    return _dynamic_registry


# ─────────────────────────────────────────────────────────────────────────────
# TOOL CREATION FROM LLM
# ─────────────────────────────────────────────────────────────────────────────

CREATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_dynamic_tool",
        "description": "Create a new dynamic tool at runtime. Write Python code, provide JSON schema, and the tool becomes immediately available. Persists to GitHub and survives restarts.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Tool name (snake_case, unique). Will be prefixed with 'dyn_'."
                },
                "description": {
                    "type": "string",
                    "description": "What the tool does - shown to LLM when deciding to use it."
                },
                "parameters_schema": {
                    "type": "object",
                    "description": "JSON schema for tool parameters (OpenAPI format).",
                    "properties": {
                        "type": {"type": "string", "const": "object"},
                        "properties": {"type": "object"},
                        "required": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["type", "properties", "required"]
                },
                "python_code": {
                    "type": "string",
                    "description": "Python function source code. Must define a function matching the name that takes (args: dict, **context) and returns any."
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata: category, version, author, etc.",
                    "properties": {
                        "category": {"type": "string"},
                        "version": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}}
                    }
                }
            },
            "required": ["name", "description", "parameters_schema", "python_code"]
        }
    }
}



def create_dynamic_tool(
    name: str,
    description: str,
    parameters_schema: Dict,
    python_code: str,
    metadata: Dict = None
) -> Dict[str, Any]:
    """Create and register a new dynamic tool."""

    if not name.startswith("dyn_"):
        name = "dyn_" + name

    if not name.isidentifier():
        return {"ok": False, "error": "Invalid tool name: %s" % name}

    try:
        func = _compile_tool(name, python_code)
    except Exception as e:
        return {"ok": False, "error": "Compilation failed: %s" % e}

    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters_schema
        }
    }

    registry = get_dynamic_registry()
    if not registry.register(name, schema, func, python_code, metadata):
        return {"ok": False, "error": "Registration failed"}

    _persist_tool_async(name, description, parameters_schema, python_code, metadata)

    return {
        "ok": True,
        "name": name,
        "message": "Tool %s created and ready to use" % name
    }


def _persist_tool_async(name: str, description: str, schema: Dict, code: str, metadata: Dict):
    """Persist tool to GitHub backup repo."""
    try:
        repo = cfg("github_backup_repo") or ""
        if not repo:
            return

        tool_data = {
            "name": name,
            "description": description,
            "parameters_schema": schema,
            "python_code": code,
            "metadata": metadata or {},
            "created_at": time.time(),
        }

        path = "dynamic_tools/%s.json" % name
        content = json.dumps(tool_data, indent=2)

        github_upsert_file(repo, path, content, "Add/update dynamic tool: %s" % name)
        log.info("[DynamicTools] Persisted %s to GitHub", name)
    except Exception as e:
        log.warning("[DynamicTools] Failed to persist %s: %s", name, e)


def load_dynamic_tools_from_github() -> int:
    """Load all dynamic tools from GitHub on startup."""
    repo = cfg("github_backup_repo") or ""
    if not repo:
        return 0

    try:
        result = github_list_files(repo, "dynamic_tools")
        if not result.get("ok"):
            return 0

        loaded = 0
        for file_info in result.get("files", []):
            if not file_info["name"].endswith(".json"):
                continue

            file_result = github_get_file(repo, "dynamic_tools/%s" % file_info["name"])
            if not file_result.get("ok"):
                continue

            try:
                tool_data = json.loads(file_result["content"])
                name = tool_data["name"]

                func = _compile_tool(name, tool_data["python_code"])
                schema = {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool_data["description"],
                        "parameters": tool_data["parameters_schema"]
                    }
                }

                registry = get_dynamic_registry()
                registry.tools[name] = {
                    "schema": schema,
                    "func": func,
                    "source": tool_data["python_code"],
                    "metadata": tool_data.get("metadata", {}),
                    "created_at": tool_data.get("created_at", time.time()),
                    "dynamic": True
                }
                registry.loaded_names.add(name)
                loaded += 1

            except Exception as e:
                log.warning("[DynamicTools] Failed to load %s: %s", file_info["name"], e)

        log.info("[DynamicTools] Loaded %d dynamic tools from GitHub", loaded)
        return loaded

    except Exception as e:
        log.error("[DynamicTools] Failed to load from GitHub: %s", e)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELF-REPAIR / TROUBLESHOOTING TOOLS
# ─────────────────────────────────────────────────────────────────────────────

SELF_REPAIR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "self_repair",
        "description": "Analyze and fix issues in the bot's own code or configuration. Can create diagnostic tools, patch bugs, or optimize performance.",
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "What's broken or needs fixing"
                },
                "target": {
                    "type": "string",
                    "enum": ["tool", "config", "memory", "performance", "unknown"],
                    "description": "What area to investigate"
                },
                "create_fix_tool": {
                    "type": "boolean",
                    "description": "Whether to create a new dynamic tool to fix the issue"
                }
            },
            "required": ["issue_description"]
        }
    }
}


def execute_self_repair(
    issue_description: str,
    target: str = "unknown",
    create_fix_tool: bool = False
) -> Dict[str, Any]:
    """Self-repair: analyze issue, optionally create a fix tool."""
    from services.escalation import silent_creator_notify

    result = silent_creator_notify(
        user_id="system",
        user_jid="system@self",
        wrong_name="self_repair",
        user_message="Self-repair requested: %s" % issue_description,
        bot_response="Target: %s, Create fix tool: %s" % (target, create_fix_tool)
    )

    return {
        "ok": True,
        "message": "Self-repair request sent to creator. They'll guide the fix.",
        "escalation_id": result.get("escalation_id")
    }


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC TOOLS LIST TOOL
# ─────────────────────────────────────────────────────────────────────────────

LIST_DYNAMIC_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_dynamic_tools",
        "description": "List all dynamically created tools with their metadata.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}


def execute_list_dynamic_tools() -> Dict[str, Any]:
    registry = get_dynamic_registry()
    tools = registry.list_dynamic()
    return {
        "ok": True,
        "tools": tools,
        "count": len(tools)
    }


# ─────────────────────────────────────────────────────────────────────────────
# REMOVE DYNAMIC TOOL
# ─────────────────────────────────────────────────────────────────────────────

REMOVE_DYNAMIC_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remove_dynamic_tool",
        "description": "Remove a dynamically created tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the dynamic tool to remove (with dyn_ prefix)"
                }
            },
            "required": ["name"]
        }
    }
}


def execute_remove_dynamic_tool(name: str) -> Dict[str, Any]:
    if not name.startswith("dyn_"):
        name = "dyn_" + name

    registry = get_dynamic_registry()
    if registry.unregister(name):
        return {"ok": True, "message": "Removed %s" % name}
    return {"ok": False, "error": "Tool %s not found or not dynamic" % name}


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_dynamic_tools() -> int:
    """Load dynamic tools on startup. Call during bot initialization."""
    registry = get_dynamic_registry()
    # Load from SQLite first (primary persistence)
    sqlite_count = registry._load_from_sqlite()
    # Then try GitHub (backup)
    github_count = load_dynamic_tools_from_github()
    return sqlite_count + github_count