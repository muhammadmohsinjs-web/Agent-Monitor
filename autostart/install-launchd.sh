#!/bin/sh
# Keep the monitor running from login onwards, for every tool at once (macOS).
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.ccviz.server.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ccviz.server</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$DIR/server.py</string><string>--port</string><string>8765</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/cc-viz.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/com.ccviz.server" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "cc-viz now starts at login: http://127.0.0.1:8765"
