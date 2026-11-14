#!/bin/bash
# Builds ~/Applications/Jarvis.app, the background app JARVIS runs under so
# macOS permissions (Screen Recording, Accessibility, Microphone) apply to it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
JARVIS_DIR="$(dirname "$HERE")"
APP="$HOME/Applications/Jarvis.app"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundleIdentifier</key><string>local.jarvis.assistant</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Jarvis listens for your voice and for a double clap.</string>
  <key>NSAppleEventsUsageDescription</key><string>Jarvis controls apps on your Mac when you ask it to.</string>
</dict>
</plist>
PLIST
clang -O2 -Wall -framework CoreGraphics -framework ApplicationServices -DJARVIS_DIR="\"$JARVIS_DIR\"" -o "$APP/Contents/MacOS/Jarvis" "$HERE/launcher.c"
codesign --force --sign - --identifier local.jarvis.assistant "$APP"
echo "built $APP"
