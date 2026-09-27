"""
services/plugin_sdk.py
======================
Plugin SDK for external developers to extend Crimsonej capabilities.
Allows third-party developers to create and distribute plugins.
"""

from __future__ import annotations

import json
import os
import importlib.util
import sys
import tempfile
import shutil
import hashlib
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field, asdict

from core.config import cfg, log


# ─────────────────────────────────────────────────────────────────────────────
# PLUGIN DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PluginManifest:
    """Plugin manifest (plugin.json)"""
    name: str
    version: str
    description: str
    author: str
    license: str = "MIT"
    min_bot_version: str = "1.0.0"
    entry_point: str = "main.py"
    permissions: List[str] = field(default_factory=list)  # ["send_message", "read_messages", "storage", "network"]
    config_schema: Dict = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    repository: str = ""
    homepage: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "PluginManifest":
        return cls(**data)
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class Plugin:
    """Installed plugin instance"""
    manifest: PluginManifest
    path: Path
    module: Any = None
    enabled: bool = True
    config: Dict = field(default_factory=dict)
    loaded_at: float = 0
    error: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# PLUGIN REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class PluginRegistry:
    """Manages plugin discovery, loading, and lifecycle."""
    
    def __init__(self, plugins_dir: str = None):
        self.plugins_dir = Path(plugins_dir or cfg("plugins_dir") or "./plugins")
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        
        self.plugins: Dict[str, Plugin] = {}
        self.commands: Dict[str, Dict] = {}  # command -> {plugin_name, func}
        self.event_handlers: Dict[str, List[Callable]] = {}  # event -> [handlers]
        self.tools: Dict[str, Dict] = {}  # tool_name -> {plugin_name, schema, func}
        
        # Load existing plugins
        self._load_all()
    
    def _load_all(self):
        """Load all plugins from plugins directory."""
        for plugin_dir in self.plugins_dir.iterdir():
            if plugin_dir.is_dir() and (plugin_dir / "plugin.json").exists():
                self.load_plugin(plugin_dir)
    
    def load_plugin(self, plugin_dir: Path) -> Optional[Plugin]:
        """Load a single plugin from directory."""
        manifest_path = plugin_dir / "plugin.json"
        if not manifest_path.exists():
            return None
        
        try:
            with open(manifest_path) as f:
                manifest_data = json.load(f)
            manifest = PluginManifest.from_dict(manifest_data)
        except Exception as e:
            log.error("[PluginSDK] Failed to parse manifest for %s: %s", plugin_dir, e)
            return None
        
        # Check version compatibility
        if not self._check_version(manifest.min_bot_version):
            log.warning("[PluginSDK] Plugin %s requires bot version %s", manifest.name, manifest.min_bot_version)
            return None
        
        # Load the plugin module
        entry_point = plugin_dir / manifest.entry_point
        if not entry_point.exists():
            log.error("[PluginSDK] Entry point not found: %s", entry_point)
            return None
        
        spec = importlib.util.spec_from_file_location(manifest.name, entry_point)
        if not spec or not spec.loader:
            return None
        
        module = importlib.util.module_from_spec(spec)
        sys.modules[manifest.name] = module
        
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            log.error("[PluginSDK] Failed to load module %s: %s", manifest.name, e)
            return None
        
        # Create plugin instance
        plugin = Plugin(
            manifest=manifest,
            path=plugin_dir,
            module=module,
            enabled=True,
            loaded_at=time.time(),
        )
        
        # Initialize plugin if it has init function
        if hasattr(module, "on_load"):
            try:
                module.on_load(self._create_plugin_context(manifest.name))
            except Exception as e:
                log.error("[PluginSDK] Plugin %s init failed: %s", manifest.name, e)
                plugin.error = str(e)
                plugin.enabled = False
        
        self.plugins[manifest.name] = plugin
        log.info("[PluginSDK] Loaded plugin: %s v%s", manifest.name, manifest.version)
        return plugin
    
    def _check_version(self, min_version: str) -> bool:
        """Check if bot version meets minimum requirement."""
        from core.config import cfg
        bot_version = cfg("bot_version") or "1.0.0"
        # Simple version comparison
        return self._version_tuple(bot_version) >= self._version_tuple(min_version)
    
    def _version_tuple(self, version: str) -> tuple:
        return tuple(map(int, version.split(".")[:3]))
    
    def _create_plugin_context(self, plugin_name: str) -> "PluginContext":
        return PluginContext(self, plugin_name)
    
    def unload_plugin(self, name: str) -> bool:
        """Unload a plugin."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        
        # Call unload handler
        if plugin.module and hasattr(plugin.module, "on_unload"):
            try:
                plugin.module.on_unload()
            except Exception as e:
                log.error("[PluginSDK] Plugin %s unload error: %s", name, e)
        
        # Remove commands, handlers, tools
        self._unregister_plugin_hooks(name)
        
        del self.plugins[name]
        log.info("[PluginSDK] Unloaded plugin: %s", name)
        return True
    
    def _unregister_plugin_hooks(self, plugin_name: str):
        # Remove commands
        to_remove = [cmd for cmd, info in self.commands.items() if info.get("plugin") == plugin_name]
        for cmd in to_remove:
            del self.commands[cmd]
        
        # Remove event handlers
        for event, handlers in self.event_handlers.items():
            self.event_handlers[event] = [h for h in handlers if getattr(h, "_plugin_name", None) != plugin_name]
        
        # Remove tools
        to_remove = [tool for tool, info in self.tools.items() if info.get("plugin") == plugin_name]
        for tool in to_remove:
            del self.tools[tool]
    
    def enable_plugin(self, name: str) -> bool:
        if name in self.plugins:
            self.plugins[name].enabled = True
            return True
        return False
    
    def disable_plugin(self, name: str) -> bool:
        if name in self.plugins:
            self.plugins[name].enabled = False
            return True
        return False
    
    def get_plugin(self, name: str) -> Optional[Plugin]:
        return self.plugins.get(name)
    
    def list_plugins(self) -> List[Dict]:
        return [
            {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "author": p.manifest.author,
                "enabled": p.enabled,
                "error": p.error,
                "loaded_at": p.loaded_at,
            }
            for p in self.plugins.values()
        ]


# ─────────────────────────────────────────────────────────────────────────────
# PLUGIN CONTEXT (API for plugins)
# ─────────────────────────────────────────────────────────────────────────────

class PluginContext:
    """Context provided to plugins for interacting with the bot."""
    
    def __init__(self, registry: PluginRegistry, plugin_name: str):
        self.registry = registry
        self.plugin_name = plugin_name
        self.config = {}
        self._load_config()
    
    def _load_config(self):
        plugin = self.registry.plugins.get(self.plugin_name)
        if plugin:
            self.config = plugin.config
    
    def save_config(self):
        """Save plugin configuration."""
        plugin = self.registry.plugins.get(self.plugin_name)
        if plugin:
            plugin.config = self.config
            # Persist to storage
            from services.storage import profile_update
            profile_update(f"plugin:{self.plugin_name}", config=self.config)
    
    # ─── Command Registration ───────────────────────────────────────────────
    
    def register_command(self, name: str, func: Callable, description: str = "", 
                         usage: str = "", admin_only: bool = False, group_only: bool = False):
        """Register a slash command."""
        if name in self.registry.commands:
            raise ValueError(f"Command {name} already registered")
        
        self.registry.commands[name] = {
            "plugin": self.plugin_name,
            "func": func,
            "description": description,
            "usage": usage,
            "admin_only": admin_only,
            "group_only": group_only,
        }
        log.info("[PluginSDK] Plugin %s registered command: %s", self.plugin_name, name)
    
    def unregister_command(self, name: str):
        """Unregister a command."""
        if name in self.registry.commands and self.registry.commands[name]["plugin"] == self.plugin_name:
            del self.registry.commands[name]
    
    # ─── Event Handlers ─────────────────────────────────────────────────────
    
    def on_message(self, func: Callable):
        """Register message handler."""
        self._register_event("message", func)
    
    def on_command(self, func: Callable):
        """Register command handler."""
        self._register_event("command", func)
    
    def on_reaction(self, func: Callable):
        """Register reaction handler."""
        self._register_event("reaction", func)
    
    def on_edit(self, func: Callable):
        """Register message edit handler."""
        self._register_event("edit", func)
    
    def on_delete(self, func: Callable):
        """Register message delete handler."""
        self._register_event("delete", func)
    
    def on_join(self, func: Callable):
        """Register group join handler."""
        self._register_event("group_join", func)
    
    def on_leave(self, func: Callable):
        """Register group leave handler."""
        self._register_event("group_leave", func)
    
    def _register_event(self, event: str, func: Callable):
        func._plugin_name = self.plugin_name
        if event not in self.registry.event_handlers:
            self.registry.event_handlers[event] = []
        self.registry.event_handlers[event].append(func)
    
    def unregister_event(self, event: str, func: Callable):
        if event in self.registry.event_handlers:
            self.registry.event_handlers[event] = [
                h for h in self.registry.event_handlers[event] 
                if getattr(h, "_plugin_name", None) != self.plugin_name
            ]
    
    # ─── Tool Registration ──────────────────────────────────────────────────
    
    def register_tool(self, schema: Dict, func: Callable):
        """Register an LLM tool."""
        tool_name = schema["function"]["name"]
        if tool_name in self.registry.tools:
            raise ValueError(f"Tool {tool_name} already registered")
        
        self.registry.tools[tool_name] = {
            "plugin": self.plugin_name,
            "schema": schema,
            "func": func,
        }
        log.info("[PluginSDK] Plugin %s registered tool: %s", self.plugin_name, tool_name)
    
    # ─── Storage ────────────────────────────────────────────────────────────
    
    def set_data(self, key: str, value: Any):
        """Store plugin data."""
        plugin = self.registry.plugins.get(self.plugin_name)
        if plugin:
            plugin.config[key] = value
            self.save_config()
    
    def get_data(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)
    
    def delete_data(self, key: str):
        if key in self.config:
            del self.config[key]
            self.save_config()
    
    # ─── Messaging ──────────────────────────────────────────────────────────
    
    def send_message(self, jid: str, text: str, **kwargs) -> Dict:
        """Send a message via bridge."""
        from services.bridge_api import bridge_send
        return bridge_send(jid, text, **kwargs)
    
    def send_media(self, jid: str, media_path: str, media_type: str = "image",
                   caption: str = "", **kwargs) -> Dict:
        from services.bridge_api import bridge_send
        return bridge_send(jid, caption, media_path=media_path, media_type=media_type)
    
    def edit_message(self, jid: str, message_id: str, new_text: str) -> Dict:
        from services.bridge_api import bridge_edit
        return bridge_edit(jid, message_id, new_text)
    
    def delete_message(self, jid: str, message_id: str) -> Dict:
        from services.bridge_api import bridge_delete
        return bridge_delete(jid, message_id)
    
    def send_reaction(self, jid: str, message_id: str, emoji: str) -> Dict:
        from services.bridge_api import bridge_send_reaction
        return bridge_send_reaction(jid, message_id, emoji)
    
    # ─── Storage Helpers ────────────────────────────────────────────────────
    
    def get_user_profile(self, user_id: str) -> Dict:
        from services.storage import profile_get
        return profile_get(user_id)
    
    def update_user_profile(self, user_id: str, **kwargs):
        from services.storage import profile_update
        profile_update(user_id, **kwargs)
    
    def get_group_context(self, group_jid: str) -> Dict:
        from services.group_intel import get_group_context
        return get_group_context(group_jid)
    
    # ─── Web/HTTP ───────────────────────────────────────────────────────────
    
    def http_get(self, url: str, **kwargs) -> requests.Response:
        import requests
        return requests.get(url, timeout=30, **kwargs)
    
    def http_post(self, url: str, json_data: Dict = None, **kwargs) -> requests.Response:
        import requests
        return requests.post(url, json=json_data, timeout=30, **kwargs)
    
    # ─── Logging ────────────────────────────────────────────────────────────
    
    def log(self, level: str, message: str, **extra):
        getattr(log, level.lower())("[Plugin:%s] %s", self.plugin_name, message, **extra)
    
    # ─── Plugin Management ──────────────────────────────────────────────────
    
    def get_plugin_info(self, name: str) -> Optional[Dict]:
        plugin = self.registry.plugins.get(name)
        if plugin:
            return {
                "name": plugin.manifest.name,
                "version": plugin.manifest.version,
                "description": plugin.manifest.description,
                "enabled": plugin.enabled,
            }
        return None
    
    def list_plugins(self) -> List[Dict]:
        return self.registry.list_plugins()


# ─────────────────────────────────────────────────────────────────────────────
# PLUGIN INSTALLATION / DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

class PluginManager:
    """High-level plugin management: install, update, remove, marketplace."""
    
    def __init__(self, registry: PluginRegistry):
        self.registry = registry
        self.plugins_dir = registry.plugins_dir
        self.repo_url = cfg("plugin_marketplace_url") or "https://api.github.com/repos/crimsonej/plugins"
    
    def install_from_github(self, repo: str, branch: str = "main") -> bool:
        """Install plugin from GitHub repository."""
        import subprocess
        import tempfile
        
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_dir = Path(tmpdir) / "repo"
                
                # Clone repo
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", branch, 
                     f"https://github.com/{repo}.git", str(repo_dir)],
                    capture_output=True, text=True, timeout=60
                )
                
                if result.returncode != 0:
                    log.error("[PluginManager] Git clone failed: %s", result.stderr)
                    return False
                
                # Find plugin.json
                plugin_json = None
                for root, dirs, files in os.walk(repo_dir):
                    if "plugin.json" in files:
                        plugin_json = Path(root) / "plugin.json"
                        break
                
                if not plugin_json:
                    log.error("[PluginManager] No plugin.json found in repo")
                    return False
                
                # Copy to plugins directory
                with open(plugin_json) as f:
                    manifest_data = json.load(f)
                
                plugin_name = manifest_data["name"]
                target_dir = self.registry.plugins_dir / plugin_name
                
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                
                shutil.copytree(plugin_json.parent, target_dir)
                
                # Load the plugin
                self.registry.load_plugin(target_dir)
                
                log.info("[PluginManager] Installed plugin: %s", plugin_name)
                return True
                
        except Exception as e:
            log.error("[PluginManager] Install failed: %s", e)
            return False
    
    def install_from_zip(self, zip_path: str) -> bool:
        """Install plugin from zip file."""
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                shutil.unpack_archive(zip_path, tmpdir)
                
                # Find plugin.json
                plugin_json = None
                for root, dirs, files in os.walk(tmpdir):
                    if "plugin.json" in files:
                        plugin_json = Path(root) / "plugin.json"
                        break
                
                if not plugin_json:
                    return False
                
                with open(plugin_json) as f:
                    manifest_data = json.load(f)
                
                plugin_name = manifest_data["name"]
                target_dir = self.registry.plugins_dir / plugin_name
                
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                
                shutil.copytree(plugin_json.parent, target_dir)
                self.registry.load_plugin(target_dir)
                
                return True
        except Exception as e:
            log.error("[PluginManager] Zip install failed: %s", e)
            return False
    
    def uninstall(self, name: str) -> bool:
        """Uninstall a plugin."""
        plugin_dir = self.plugins_dir / name
        if not plugin_dir.exists():
            return False
        
        # Unload first
        self.registry.unload_plugin(name)
        
        # Remove files
        shutil.rmtree(plugin_dir)
        
        log.info("[PluginManager] Uninstalled plugin: %s", name)
        return True
    
    def update(self, name: str) -> bool:
        """Update a plugin from its source."""
        plugin_dir = self.plugins_dir / name
        if not plugin_dir.exists():
            return False
        
        # Try to find git repo
        git_dir = plugin_dir / ".git"
        if git_dir.exists():
            try:
                subprocess.run(["git", "pull"], cwd=plugin_dir, check=True, capture_output=True)
                self.registry.unload_plugin(name)
                self.registry.load_plugin(plugin_dir)
                return True
            except Exception as e:
                log.error("[PluginManager] Update failed: %s", e)
                return False
        
        return False
    
    def list_available(self) -> List[Dict]:
        """List available plugins from marketplace."""
        try:
            response = requests.get(f"{self.repo_url}/contents", timeout=10)
            if response.ok:
                return response.json()
        except Exception:
            pass
        return []


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL INSTANCES
# ─────────────────────────────────────────────────────────────────────────────

_plugin_registry: Optional[PluginRegistry] = None
_plugin_manager: Optional[PluginManager] = None


def get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None:
        _plugin_registry = PluginRegistry()
    return _plugin_registry


def get_plugin_manager() -> PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager(get_plugin_registry())
    return _plugin_manager


def load_all_plugins() -> int:
    """Load all plugins from directory."""
    registry = get_plugin_registry()
    return len(registry.plugins)


def get_plugin_context(plugin_name: str) -> Optional[PluginContext]:
    registry = get_plugin_registry()
    plugin = registry.plugins.get(plugin_name)
    if plugin:
        return registry._create_plugin_context(plugin_name)
    return None