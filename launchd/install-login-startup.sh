#!/usr/bin/env bash
#
# Install the Copilot Agent Monitor as a macOS LaunchAgent so it starts
# automatically at login and runs the web dashboard in the background.
#
# Usage:
#   ./launchd/install-login-startup.sh            # install & start
#   ./launchd/install-login-startup.sh uninstall  # stop & remove
#
set -euo pipefail

LABEL="com.$(id -un).copilot-agent-monitor"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MONITOR_PY="${REPO_DIR}/monitor.py"
PORT="${MONITOR_PORT:-8787}"
PYTHON="$(command -v python3)"

if [ "${1:-install}" = "uninstall" ]; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "Removed $PLIST_DEST"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_DEST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${MONITOR_PY}</string>
        <string>--no-terminal</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/tmp/copilot-monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/copilot-monitor.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MONITOR_PORT</key>
        <string>${PORT}</string>
    </dict>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "Installed and started: $PLIST_DEST"
echo "Dashboard: http://localhost:${PORT}  (logs: /tmp/copilot-monitor.log)"
