"""
orchestrator.py
===============
Process orchestrator for managing Python AI Server and Node.js WhatsApp Bridge.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import requests
from typing import Tuple

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(ROOT_DIR, "crimson-bot")
BRIDGE_DIR = os.path.join(ROOT_DIR, "whatsapp-bridge")

BOT_PID_FILE = os.path.join(ROOT_DIR, ".bot.pid")
BRIDGE_PID_FILE = os.path.join(ROOT_DIR, ".bridge.pid")

BOT_LOG_FILE = os.path.join(ROOT_DIR, "bot.log")
BRIDGE_LOG_FILE = os.path.join(ROOT_DIR, "bridge.log")

def get_pid(file_path: str) -> int | None:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                pid = int(f.read().strip())
            # Check if process is still running
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            if os.path.exists(file_path):
                os.remove(file_path)
    return None

def start_services(foreground: bool = False) -> None:
    bot_pid = get_pid(BOT_PID_FILE)
    bridge_pid = get_pid(BRIDGE_PID_FILE)

    if bot_pid or bridge_pid:
        print(f"\033[1;33m[WARN] Crimsonej services already running (Bot PID: {bot_pid}, Bridge PID: {bridge_pid}).\033[0m")
        return

    print("\033[1;32m🚀 Starting Crimsonej AI Engine & WhatsApp Bridge...\033[0m")

    python_bin = os.path.join(BOT_DIR, "venv", "bin", "python")
    if not os.path.exists(python_bin):
        python_bin = sys.executable

    # 1. Start Python Bot Server
    print("  ► Spawning Python AI Server on port 5000...")
    bot_log = open(BOT_LOG_FILE, "a")
    bot_proc = subprocess.Popen(
        [python_bin, "bot.py", "server"],
        cwd=BOT_DIR,
        stdout=bot_log,
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    with open(BOT_PID_FILE, "w") as f:
        f.write(str(bot_proc.pid))
    print(f"    ✓ AI Server running (PID: {bot_proc.pid})")

    # 2. Start Node.js WhatsApp Bridge
    print("  ► Spawning WhatsApp Bridge on port 7860...")
    bridge_log = open(BRIDGE_LOG_FILE, "a")
    bridge_proc = subprocess.Popen(
        ["node", "bridge.js"],
        cwd=BRIDGE_DIR,
        stdout=bridge_log,
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    with open(BRIDGE_PID_FILE, "w") as f:
        f.write(str(bridge_proc.pid))
    print(f"    ✓ WhatsApp Bridge running (PID: {bridge_proc.pid})")

    print("\n\033[1;32m✨ Crimsonej started successfully! ✨\033[0m")
    print("  • View logs: \033[1mcrimsonej logs\033[0m")
    print("  • Check status: \033[1mcrimsonej status\033[0m")
    print("  • Stop bot: \033[1mcrimsonej stop\033[0m\n")

    if foreground:
        def _handle_signal(sig, frame):
            print(f"\n[Orchestrator] Received signal {sig}, stopping services...")
            stop_services()
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            while True:
                time.sleep(3)
                # Check Python AI Server process
                if not get_pid(BOT_PID_FILE):
                    print("\033[1;31m[Orchestrator] Python AI Server stopped! Respawning...\033[0m")
                    bot_log = open(BOT_LOG_FILE, "a")
                    bot_proc = subprocess.Popen(
                        [python_bin, "bot.py", "server"],
                        cwd=BOT_DIR,
                        stdout=bot_log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True
                    )
                    with open(BOT_PID_FILE, "w") as f:
                        f.write(str(bot_proc.pid))

                # Check Node.js WhatsApp Bridge process
                if not get_pid(BRIDGE_PID_FILE):
                    print("\033[1;31m[Orchestrator] WhatsApp Bridge stopped! Respawning...\033[0m")
                    bridge_log = open(BRIDGE_LOG_FILE, "a")
                    bridge_proc = subprocess.Popen(
                        ["node", "bridge.js"],
                        cwd=BRIDGE_DIR,
                        stdout=bridge_log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True
                    )
                    with open(BRIDGE_PID_FILE, "w") as f:
                        f.write(str(bridge_proc.pid))
        except KeyboardInterrupt:
            stop_services()

def _kill_process(pid: int, name: str, wait_secs: float = 4.0) -> None:
    """Send SIGTERM; escalate to SIGKILL if the process hasn't exited after wait_secs."""
    import time as _time
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # Already dead

    deadline = _time.time() + wait_secs
    while _time.time() < deadline:
        _time.sleep(0.25)
        try:
            os.kill(pid, 0)  # Check still alive
        except OSError:
            return  # Exited cleanly

    # Still alive — escalate
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"  ⚡ Force-killed {name} (PID: {pid})")
    except OSError:
        pass


def _free_port(port: int) -> None:
    """Best-effort: kill any process still listening on a port (Linux only)."""
    try:
        import subprocess as _sub
        result = _sub.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            print(f"  🔓 Freed port {port} via fuser")
    except Exception:
        pass  # fuser may not be installed; that is fine


def stop_services() -> None:
    print("\033[1;33m🛑 Stopping Crimsonej Services...\033[0m")
    for name, pid_file in [("AI Server", BOT_PID_FILE), ("WhatsApp Bridge", BRIDGE_PID_FILE)]:
        pid = get_pid(pid_file)
        if pid:
            try:
                _kill_process(pid, name)
                print(f"  ✓ Stopped {name} (PID: {pid})")
            except OSError as e:
                print(f"  [ERROR] Failed to stop {name}: {e}")
            if os.path.exists(pid_file):
                os.remove(pid_file)
        else:
            print(f"  • {name} was not running.")

    # Final safety net: make sure ports are actually free before returning
    _free_port(5000)
    _free_port(7860)

def restart_services() -> None:
    print("\033[1;36m♻️  Restarting Crimsonej Services...\033[0m")
    stop_services()
    print("  • Waiting 2s for ports to release...")
    time.sleep(2)
    start_services()

def status_services() -> None:
    print("\033[1;36m=== Crimsonej System Status ===\033[0m")

    bot_pid = get_pid(BOT_PID_FILE)
    bridge_pid = get_pid(BRIDGE_PID_FILE)

    bot_status = f"\033[1;32mRUNNING (PID: {bot_pid})\033[0m" if bot_pid else "\033[1;31mSTOPPED\033[0m"
    bridge_status = f"\033[1;32mRUNNING (PID: {bridge_pid})\033[0m" if bridge_pid else "\033[1;31mSTOPPED\033[0m"

    print(f"  • Python AI Engine:  {bot_status}")
    print(f"  • WhatsApp Bridge:   {bridge_status}")

    # Health probe
    if bot_pid:
        try:
            r = requests.get("http://localhost:5000/health", timeout=2)
            if r.status_code == 200:
                print(f"  • AI Server Health:  \033[32mHEALTHY (Chunks: {r.json().get('chunks', 0)})\033[0m")
        except Exception:
            print("  • AI Server Health:  \033[31mUNREACHABLE\033[0m")

    if bridge_pid:
        try:
            r = requests.get("http://localhost:7860/", timeout=2)
            if r.status_code == 200:
                print("  • Bridge API Health: \033[32mALIVE (Port 7860)\033[0m")
        except Exception:
            print("  • Bridge API Health: \033[31mUNREACHABLE\033[0m")

def stream_logs(target: str = "all") -> None:
    print(f"\033[1;36m=== Streaming Crimsonej Logs ({target}) ===\033[0m (Press Ctrl+C to exit)\n")
    log_files = []
    if target in ("all", "bot") and os.path.exists(BOT_LOG_FILE):
        log_files.append(BOT_LOG_FILE)
    if target in ("all", "bridge") and os.path.exists(BRIDGE_LOG_FILE):
        log_files.append(BRIDGE_LOG_FILE)

    if not log_files:
        print("No log files found yet. Start the bot first with 'crimsonej start'.")
        return

    cmd = ["tail", "-f", "-n", "50"] + log_files
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    is_fg = "--foreground" in sys.argv or "-f" in sys.argv
    if cmd == "start":
        start_services(foreground=is_fg)
    elif cmd == "stop":
        stop_services()
    elif cmd == "restart":
        restart_services()
    elif cmd == "status":
        status_services()
    elif cmd == "logs":
        target = sys.argv[2] if len(sys.argv) > 2 else "all"
        stream_logs(target)
