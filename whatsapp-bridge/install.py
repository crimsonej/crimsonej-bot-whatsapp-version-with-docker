#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil

def _print_step(msg):
    print(f"\n\033[1m=== {msg} ===\033[0m")

def _print_info(msg):
    print(f"\033[0;32m[INFO]\033[0m {msg}")

def _print_warn(msg):
    print(f"\033[0;33m[WARN]\033[0m {msg}")

def _print_err(msg):
    print(f"\033[0;31m[ERR]\033[0m {msg}")

def check_bot():
    _print_step("Checking for groq-bot")
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "groq-bot")
    if not os.path.isdir(bot_path):
        _print_warn("Could not find '../groq-bot' directory.")
        _print_warn("This bridge requires the groq-bot to function properly.")
        _print_warn("Please make sure to clone/download the groq-bot repository next to this one.")
    else:
        _print_info("Found groq-bot directory.")

def check_node():
    _print_step("Checking Node.js & NPM")
    if not shutil.which("node") or not shutil.which("npm"):
        _print_err("Node.js or npm is not installed. Please install them and try again.")
        sys.exit(1)
    _print_info("Node.js and npm are installed.")

def install_deps():
    _print_step("Installing NPM Packages")
    npm_exe = "npm.cmd" if os.name == "nt" else "npm"
    try:
        subprocess.run([npm_exe, "install"], check=True)
        _print_info("NPM dependencies installed.")
    except subprocess.CalledProcessError:
        _print_err("Failed to run npm install.")
        sys.exit(1)

def setup_env():
    _print_step("Securing Environment")
    if not os.path.isfile(".env"):
        if os.path.isfile(".env.example"):
            shutil.copy(".env.example", ".env")
            _print_info("Copied .env.example to .env.")
        else:
            with open(".env", "w") as f:
                f.write("API_URL=http://localhost:5000/reply\n")
            _print_info("Created default .env file.")
        
        if os.name != "nt":
            os.chmod(".env", 0o600)
    else:
        _print_info(".env file already exists.")

def setup_pm2():
    _print_step("Checking Background Process Setup (PM2)")
    if shutil.which("pm2"):
        _print_info("pm2 found! Generating ecosystem.config.js...")
        ecosystem = """module.exports = {
  apps : [{
    name   : "whatsapp-bridge",
    script : "bridge.js",
    watch  : false
  }]
}
"""
        with open("ecosystem.config.js", "w") as f:
            f.write(ecosystem)
        _print_info("You can start the bridge using: pm2 start ecosystem.config.js")
    else:
        _print_warn("pm2 is not installed globally. To run the bridge in the background across platforms, consider installing it: npm install -g pm2")

def main():
    print("🚀 Starting WhatsApp Bridge Setup...")
    check_bot()
    check_node()
    install_deps()
    setup_env()
    setup_pm2()
    print("\n✨ Installation Complete! ✨")

if __name__ == "__main__":
    main()
