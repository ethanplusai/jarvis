#!/usr/bin/env bash
# setup_permissions.sh — JARVIS macOS Permissions Setup Guide
# Run this script to check and configure all required system permissions.

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

print_header() {
  echo ""
  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${CYAN}║        JARVIS — macOS Permissions Setup Guide        ║${RESET}"
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
  echo ""
}

print_section() {
  echo ""
  echo -e "${BOLD}${YELLOW}── $1 ──${RESET}"
}

check_mark() { echo -e "  ${GREEN}✓${RESET} $1"; }
warn_mark()  { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail_mark()  { echo -e "  ${RED}✗${RESET} $1"; }
info_mark()  { echo -e "  ${CYAN}ℹ${RESET}  $1"; }

open_pane() {
  # Open a System Settings pane by URL scheme
  open "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 1. Accessibility
# ---------------------------------------------------------------------------
check_accessibility() {
  print_section "1. Accessibility"
  echo "  WHY: JARVIS uses Accessibility to send keystrokes (copy, paste, undo,"
  echo "       type text) and manipulate windows via System Events. Without this,"
  echo "       keyboard shortcuts and window control will fail."
  echo ""

  # Try a no-op keystroke — if it fails, Accessibility is denied
  local result
  result=$(osascript -e 'tell application "System Events" to keystroke ""' 2>&1) || true

  if echo "$result" | grep -qi "not allowed\|accessibility\|1002\|osascript is not allowed"; then
    fail_mark "Accessibility: NOT granted"
    warn_mark "Opening System Settings → Privacy & Security → Accessibility..."
    open_pane "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    echo ""
    echo "  ACTION REQUIRED:"
    echo "    1. In the list, find 'Terminal' (or 'iTerm2' / your terminal app)"
    echo "    2. Toggle it ON"
    echo "    3. If Python or 'jarvis' appears, toggle that ON too"
    echo "    4. Re-run this script to verify"
  else
    check_mark "Accessibility: granted"
  fi
}

# ---------------------------------------------------------------------------
# 2. Automation
# ---------------------------------------------------------------------------
check_automation() {
  print_section "2. Automation"
  echo "  WHY: JARVIS controls other apps (Chrome, Finder, VS Code, etc.) via"
  echo "       AppleScript 'tell application' commands. Automation permission is"
  echo "       required per-app — Terminal must be allowed to control each one."
  echo ""

  # Try to get the name of frontmost app — requires Automation for System Events
  local result
  result=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>&1) || true

  if echo "$result" | grep -qi "not authorized\|not allowed\|automation"; then
    fail_mark "Automation: NOT granted for System Events"
    warn_mark "Opening System Settings → Privacy & Security → Automation..."
    open_pane "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
    echo ""
    echo "  ACTION REQUIRED:"
    echo "    1. Find 'Terminal' (or your terminal/Python process) in the list"
    echo "    2. Enable 'System Events' under it"
    echo "    3. Enable 'Google Chrome', 'Finder', and any other apps JARVIS should"
    echo "       control (VS Code, Safari, etc.)"
    echo "    4. Re-run this script to verify"
  else
    check_mark "Automation (System Events): granted"
    info_mark "If controlling a specific app (e.g. Chrome) fails, go to"
    info_mark "Privacy & Security → Automation and enable it there."
  fi
}

# ---------------------------------------------------------------------------
# 3. Screen Recording
# ---------------------------------------------------------------------------
check_screen_recording() {
  print_section "3. Screen Recording"
  echo "  WHY: JARVIS uses 'screencapture' to take screenshots when you ask it to"
  echo "       capture your screen. Without this, screenshots will be blank or fail."
  echo ""

  # screencapture -x writes a file; if Screen Recording is denied the file is
  # produced but contains a black/blank frame. We test by checking the exit code
  # of a quick attempt — not perfect but avoids writing a real file.
  local tmp
  tmp=$(mktemp /tmp/jarvis_sc_test_XXXXXX.png)
  local result=0
  screencapture -x "$tmp" 2>/dev/null || result=$?
  rm -f "$tmp"

  if [[ $result -ne 0 ]]; then
    fail_mark "Screen Recording: likely NOT granted (screencapture exited $result)"
    warn_mark "Opening System Settings → Privacy & Security → Screen Recording..."
    open_pane "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    echo ""
    echo "  ACTION REQUIRED:"
    echo "    1. Find 'Terminal' (or your terminal app) in the list"
    echo "    2. Toggle it ON"
    echo "    3. You may need to restart Terminal after granting"
  else
    check_mark "Screen Recording: granted (or not yet denied)"
    info_mark "If screenshots appear black/blank, revoke & re-grant in"
    info_mark "Privacy & Security → Screen Recording."
  fi
}

# ---------------------------------------------------------------------------
# 4. Microphone
# ---------------------------------------------------------------------------
check_microphone() {
  print_section "4. Microphone"
  echo "  WHY: JARVIS listens to your voice via the browser's Web Speech API."
  echo "       The browser (e.g. Chrome) must have Microphone access granted."
  echo "       The Python server itself does NOT need microphone access."
  echo ""

  # We can't reliably check browser microphone permission from a shell script.
  # Instead, remind the user to check in the browser and System Settings.
  info_mark "Browser-level check (cannot be automated from shell):"
  echo ""
  echo "  TO GRANT:"
  echo "    System Settings → Privacy & Security → Microphone"
  echo "      → Ensure your browser (Chrome / Safari / Firefox) is toggled ON"
  echo ""
  echo "    In Chrome: visit chrome://settings/content/microphone"
  echo "      → JARVIS runs on localhost — make sure it is not in the 'Blocked' list"
  echo "      → Or open http://localhost:8000, click the lock icon → allow microphone"
  echo ""
  warn_mark "Opening System Settings → Privacy & Security → Microphone..."
  open_pane "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
}

# ---------------------------------------------------------------------------
# 5. Full Disk Access (optional)
# ---------------------------------------------------------------------------
check_full_disk_access() {
  print_section "5. Full Disk Access (optional)"
  echo "  WHY: Required only if you want JARVIS to open, move, or reveal files"
  echo "       in protected directories (Desktop, Documents, Downloads, iCloud)."
  echo "       Without it, Finder operations on those paths will fail with a"
  echo "       'permission denied' or 'not allowed' error."
  echo ""
  echo "  This is OPTIONAL — JARVIS works without it for most use cases."
  echo ""

  # Test access to ~/Documents (will fail if Full Disk Access is missing for Terminal)
  if ls ~/Documents/ &>/dev/null; then
    check_mark "Full Disk Access: appears granted (~/Documents readable)"
  else
    warn_mark "Full Disk Access: NOT granted (cannot read ~/Documents)"
    warn_mark "Opening System Settings → Privacy & Security → Full Disk Access..."
    open_pane "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    echo ""
    echo "  ACTION REQUIRED (if you want Finder file operations):"
    echo "    1. Find 'Terminal' in the list and toggle it ON"
    echo "    2. Restart Terminal after granting"
  fi
}

# ---------------------------------------------------------------------------
# 6. Rectangle (optional — window snapping)
# ---------------------------------------------------------------------------
check_rectangle() {
  print_section "6. Rectangle App (optional — window snapping)"
  echo "  WHY: JARVIS uses Rectangle keyboard shortcuts (Ctrl+Opt+Arrow) to snap"
  echo "       windows to left/right halves of the screen. Without Rectangle,"
  echo "       JARVIS falls back to AppleScript-based resizing (less precise)."
  echo ""

  if [[ -d "/Applications/Rectangle.app" ]]; then
    check_mark "Rectangle: installed at /Applications/Rectangle.app"
    info_mark "Make sure Rectangle is running and has Accessibility permission."
  else
    warn_mark "Rectangle: NOT installed"
    info_mark "Download free from https://rectangleapp.com"
    info_mark "JARVIS will use AppleScript window resizing as fallback."
  fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
  echo ""
  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${CYAN}║                     Summary                         ║${RESET}"
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
  echo ""
  echo "  REQUIRED (JARVIS won't work without these):"
  echo "    • Accessibility    — keyboard shortcuts, window control"
  echo "    • Automation       — controlling Chrome, Finder, other apps"
  echo "    • Microphone       — voice input in your browser"
  echo ""
  echo "  RECOMMENDED:"
  echo "    • Screen Recording — screenshot capability"
  echo ""
  echo "  OPTIONAL:"
  echo "    • Full Disk Access — Finder ops on protected folders"
  echo "    • Rectangle App    — precise window snapping"
  echo ""
  echo "  After granting any permission, restart JARVIS:"
  echo "    cd $(dirname "$0") && python server.py"
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  print_header
  check_accessibility
  check_automation
  check_screen_recording
  check_microphone
  check_full_disk_access
  check_rectangle
  print_summary
}

main
