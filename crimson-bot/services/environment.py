"""
services/environment.py
========================
Environment Detection & Smart Optimization.
Detects container limits and provides optimization strategies for each feature.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Callable

from core.config import cfg, log

_env_info: Optional["EnvironmentInfo"] = None
_env_lock = threading.Lock()


@dataclass
class OptimizationConfig:
    """Configuration for how to optimize a feature under constraints."""
    # Resource thresholds
    min_disk_gb: float = 0.5
    min_memory_mb: int = 256
    min_cpu_cores: float = 0.5
    requires_internet: bool = False
    requires_dns: bool = False

    # Optimization strategies (callable that returns modified params)
    on_low_disk: Optional[Callable[[Dict], Dict]] = None
    on_low_memory: Optional[Callable[[Dict], Dict]] = None
    on_low_cpu: Optional[Callable[[Dict], Dict]] = None
    on_no_internet: Optional[Callable[[Dict], Dict]] = None
    on_no_dns: Optional[Callable[[Dict], Dict]] = None

    # Fallback behavior
    fallback_message: str = "Feature temporarily limited."


@dataclass
class EnvironmentInfo:
    """Collected environment information."""
    # Platform
    platform: str = ""
    python_version: str = ""
    container_runtime: str = ""

    # Resources
    cpu_count: int = 1
    cpu_limit: Optional[float] = None
    memory_total_mb: int = 0
    memory_limit_mb: Optional[int] = None
    disk_total_gb: float = 0.0
    disk_free_gb: float = 0.0

    # Network
    has_internet: bool = True
    dns_works: bool = True

    # Services
    redis_available: bool = False
    database_path: str = ""

    # Optimization configs per feature
    optimizations: Dict[str, OptimizationConfig] = field(default_factory=dict)

    # Render-specific
    is_render: bool = False
    render_service_id: Optional[str] = None

    def get_optimization(self, feature: str) -> OptimizationConfig:
        """Get optimization config for a feature."""
        return self.optimizations.get(feature, OptimizationConfig())

    def is_constrained(self, feature: str) -> bool:
        """Check if a feature is resource-constrained."""
        opt = self.get_optimization(feature)
        
        if opt.requires_internet and not self.has_internet:
            return True
        if opt.requires_dns and not self.dns_works:
            return True
        if opt.min_disk_gb > 0 and self.disk_free_gb < opt.min_disk_gb:
            return True
        if opt.min_memory_mb > 0 and self.memory_limit_mb and self.memory_limit_mb < opt.min_memory_mb:
            return True
        if opt.min_cpu_cores > 0 and self.cpu_limit and self.cpu_limit < opt.min_cpu_cores:
            return True
        return False

    def get_optimized_params(self, feature: str, base_params: Dict) -> Dict:
        """Apply optimization strategies to base parameters."""
        opt = self.get_optimization(feature)
        params = dict(base_params)

        # Apply constraints in priority order
        if opt.requires_internet and not self.has_internet:
            if opt.on_no_internet:
                params = opt.on_no_internet(params)
            return params

        if opt.requires_dns and not self.dns_works:
            if opt.on_no_dns:
                params = opt.on_no_dns(params)
            return params

        if opt.min_disk_gb > 0 and self.disk_free_gb < opt.min_disk_gb:
            if opt.on_low_disk:
                params = opt.on_low_disk(params)

        if opt.min_memory_mb > 0 and self.memory_limit_mb and self.memory_limit_mb < opt.min_memory_mb:
            if opt.on_low_memory:
                params = opt.on_low_memory(params)

        if opt.min_cpu_cores > 0 and self.cpu_limit and self.cpu_limit < opt.min_cpu_cores:
            if opt.on_low_cpu:
                params = opt.on_low_cpu(params)

        return params

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "python_version": self.python_version,
            "container_runtime": self.container_runtime,
            "cpu_count": self.cpu_count,
            "cpu_limit": self.cpu_limit,
            "memory_total_mb": self.memory_total_mb,
            "memory_limit_mb": self.memory_limit_mb,
            "disk_total_gb": round(self.disk_total_gb, 2),
            "disk_free_gb": round(self.disk_free_gb, 2),
            "has_internet": self.has_internet,
            "dns_works": self.dns_works,
            "redis_available": self.redis_available,
            "database_path": self.database_path,
            "constraints": {
                feat: self.is_constrained(feat) for feat in self.optimizations
            },
            "is_render": self.is_render,
            "render_service_id": self.render_service_id,
        }


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMIZATION STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

def _media_low_disk(params: Dict) -> Dict:
    """Optimize media download for low disk space."""
    params["stream_only"] = True           # Don't save to disk, stream directly
    params["max_size_mb"] = min(params.get("max_size_mb", 50), 10)  # Cap at 10MB
    params["cleanup_after_send"] = True    # Delete temp file immediately after sending
    params["prefer_audio_over_video"] = True  # Audio is smaller
    return params


def _media_low_memory(params: Dict) -> Dict:
    """Optimize for low memory."""
    params["max_size_mb"] = min(params.get("max_size_mb", 50), 5)
    params["use_external_api"] = True  # Offload to API if available
    return params


def _media_no_internet(params: Dict) -> Dict:
    """Fallback when no internet."""
    params["offline_mode"] = True
    params["message"] = "Can't download right now — no internet. Try again later."
    return params


def _image_low_memory(params: Dict) -> Dict:
    """Optimize image generation for low memory."""
    params["resolution"] = params.get("resolution", "512x512")
    # Downscale resolution
    if "x" in params["resolution"]:
        w, h = params["resolution"].split("x")
        params["resolution"] = f"{int(w)//2}x{int(h)//2}"
    params["use_external_api"] = True  # Use NVIDIA/HF API instead of local
    params["steps"] = min(params.get("steps", 20), 10)
    return params


def _image_low_disk(params: Dict) -> Dict:
    """Optimize for low disk."""
    params["cleanup_after_send"] = True
    params["format"] = "webp"  # Smaller than PNG
    return params


def _image_no_internet(params: Dict) -> Dict:
    """Fallback when no internet."""
    params["offline_mode"] = True
    params["message"] = "Image gen needs internet. I can describe what it would look like instead."
    return params


def _web_search_no_internet(params: Dict) -> Dict:
    """Fallback for web search without internet."""
    params["use_cache"] = True
    params["use_local_knowledge"] = True
    params["message"] = "Searching my memory... (offline)"
    return params


def _github_low_disk(params: Dict) -> Dict:
    """Optimize GitHub sync for low disk."""
    params["batch_size"] = 10  # Smaller batches
    params["compress"] = True
    params["skip_vectors"] = True  # Vectors take most space
    return params


def _github_no_internet(params: Dict) -> Dict:
    """Queue for later when internet returns."""
    params["queue_for_later"] = True
    params["message"] = "GitHub sync queued — will run when online."
    return params


def _background_low_cpu(params: Dict) -> Dict:
    """Reduce background worker concurrency."""
    params["max_workers"] = max(1, int(params.get("max_workers", 3) * 0.5))
    params["increase_intervals"] = True
    return params


def _vector_low_memory(params: Dict) -> Dict:
    """Optimize vector search for low memory."""
    params["max_chunks"] = min(params.get("max_chunks", 5000), 1000)
    params["use_fts_only"] = True  # Skip embedding computation
    return params


def _doc_low_memory(params: Dict) -> Dict:
    """Optimize document processing for low memory."""
    params["max_pages"] = 10
    params["chunk_size"] = 200
    params["streaming"] = True
    return params


# ─────────────────────────────────────────────────────────────────────────────
# BUILD OPTIMIZATION MAP
# ─────────────────────────────────────────────────────────────────────────────

OPTIMIZATION_MAP = {
    "web_search": OptimizationConfig(
        requires_internet=True,
        requires_dns=True,
        on_no_internet=_web_search_no_internet,
        on_no_dns=_web_search_no_internet,
        fallback_message="Searching offline memory...",
    ),
    "media_download": OptimizationConfig(
        requires_internet=True,
        min_disk_gb=0.5,
        min_memory_mb=256,
        on_low_disk=_media_low_disk,
        on_low_memory=_media_low_memory,
        on_no_internet=_media_no_internet,
        fallback_message="Downloading with optimizations...",
    ),
    "image_generation": OptimizationConfig(
        requires_internet=True,
        min_memory_mb=512,
        min_disk_gb=0.5,
        on_low_memory=_image_low_memory,
        on_low_disk=_image_low_disk,
        on_no_internet=_image_no_internet,
        fallback_message="Generating with reduced quality...",
    ),
    "github_sync": OptimizationConfig(
        requires_internet=True,
        requires_dns=True,
        min_disk_gb=0.5,
        on_low_disk=_github_low_disk,
        on_no_internet=_github_no_internet,
        fallback_message="Sync queued for when online.",
    ),
    "background_workers": OptimizationConfig(
        min_cpu_cores=1.0,
        on_low_cpu=_background_low_cpu,
    ),
    "vector_search": OptimizationConfig(
        min_memory_mb=256,
        on_low_memory=_vector_low_memory,
    ),
    "document_processing": OptimizationConfig(
        min_memory_mb=256,
        on_low_memory=_doc_low_memory,
    ),
    "trading_analysis": OptimizationConfig(
        # Lightweight, always available
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_container_runtime() -> str:
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
            if "docker" in content or "containerd" in content:
                return "docker"
            if "kubepods" in content:
                return "kubernetes"
    except Exception:
        pass
    if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
        return "render"
    return "unknown"


def get_cpu_info() -> tuple[int, Optional[float]]:
    cpu_count = os.cpu_count() or 1
    cpu_limit = None
    try:
        with open("/sys/fs/cgroup/cpu.max", "r") as f:
            content = f.read().strip()
            if content != "max":
                quota, period = map(int, content.split())
                cpu_limit = quota / period
    except Exception:
        pass
    if cpu_limit is None:
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as f:
                quota = int(f.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as f:
                period = int(f.read().strip())
            if quota > 0:
                cpu_limit = quota / period
        except Exception:
            pass
    return cpu_count, cpu_limit


def get_memory_info() -> tuple[int, Optional[int]]:
    memory_total_mb = 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_kb = int(line.split()[1])
                    memory_total_mb = mem_kb // 1024
                    break
    except Exception:
        memory_total_mb = 0

    memory_limit_mb = None
    try:
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            content = f.read().strip()
            if content != "max":
                memory_limit_mb = int(content) // (1024 * 1024)
    except Exception:
        pass
    if memory_limit_mb is None:
        try:
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                limit = int(f.read().strip())
                if limit < 10**12:
                    memory_limit_mb = limit // (1024 * 1024)
        except Exception:
            pass
    return memory_total_mb, memory_limit_mb


def get_disk_info(path: str = None) -> tuple[float, float]:
    paths_to_try = []
    if path:
        paths_to_try.append(path)
    paths_to_try.extend(["/data", "/", os.getcwd()])
    for p in paths_to_try:
        try:
            total, used, free = shutil.disk_usage(p)
            return total / (1024**3), free / (1024**3)
        except Exception:
            continue
    return 0.0, 0.0


def check_internet() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen("http://8.8.8.8", timeout=3)
        return True
    except Exception:
        try:
            urllib.request.urlopen("https://www.google.com", timeout=3)
            return True
        except Exception:
            return False


def check_dns() -> bool:
    try:
        import socket
        socket.gethostbyname("github.com")
        return True
    except Exception:
        return False


def check_redis() -> bool:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return False
    try:
        import redis
        client = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


def get_database_path() -> str:
    from core.config import BASE_DIR
    return os.path.join(BASE_DIR, "crimson.db")


def collect_environment_info() -> EnvironmentInfo:
    info = EnvironmentInfo()

    info.platform = platform.platform()
    info.python_version = platform.python_version()
    info.container_runtime = detect_container_runtime()
    info.is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
    info.render_service_id = os.getenv("RENDER_SERVICE_ID")

    info.cpu_count, info.cpu_limit = get_cpu_info()
    info.memory_total_mb, info.memory_limit_mb = get_memory_info()
    info.disk_total_gb, info.disk_free_gb = get_disk_info()

    info.has_internet = check_internet()
    info.dns_works = check_dns()

    info.redis_available = check_redis()
    info.database_path = get_database_path()

    # Load optimization configs
    info.optimizations = OPTIMIZATION_MAP

    return info


def get_environment_info() -> EnvironmentInfo:
    global _env_info
    with _env_lock:
        if _env_info is None:
            _env_info = collect_environment_info()
            log.info("[Environment] Detected: %s", _env_info.to_dict())
        return _env_info


def get_optimization_config(feature: str) -> OptimizationConfig:
    """Get optimization config for a feature."""
    info = get_environment_info()
    return info.get_optimization(feature)


def is_constrained(feature: str) -> bool:
    """Check if a feature is resource-constrained."""
    info = get_environment_info()
    return info.is_constrained(feature)


def get_optimized_params(feature: str, base_params: Dict) -> Dict:
    """Get optimized parameters for a feature given current environment."""
    info = get_environment_info()
    return info.get_optimized_params(feature, base_params)


def log_environment_summary() -> None:
    info = get_environment_info()
    log.info("=== Environment Summary ===")
    log.info("Platform: %s (%s)", info.platform, info.container_runtime)
    log.info("Python: %s", info.python_version)
    log.info("CPU: %d cores%s", info.cpu_count, f" (limit: {info.cpu_limit:.1f})" if info.cpu_limit else "")
    log.info("Memory: %d MB%s", info.memory_total_mb, f" (limit: {info.memory_limit_mb} MB)" if info.memory_limit_mb else "")
    log.info("Disk: %.1f GB free of %.1f GB", info.disk_free_gb, info.disk_total_gb)
    log.info("Internet: %s | DNS: %s", "✓" if info.has_internet else "✗", "✓" if info.dns_works else "✗")
    log.info("Redis: %s", "✓" if info.redis_available else "✗")
    log.info("Database: %s", info.database_path)
    log.info("Feature Optimizations:")
    for feat in info.optimizations:
        constrained = "⚠ CONSTRAINED" if info.is_constrained(feat) else "✓ OK"
        log.info("  %s %s", constrained, feat)
    if info.is_render:
        log.info("Render Service: %s", info.render_service_id or "unknown")


# Auto-collect on import
collect_environment_info()