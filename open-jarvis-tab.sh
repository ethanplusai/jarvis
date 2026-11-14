#!/bin/bash
# Focus the existing JARVIS tab in Chrome (reloading it), or open one if none exists.
osascript <<'OSA'
set target to "localhost:5173"
tell application "Google Chrome"
  set found to false
  repeat with w in windows
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      if URL of t contains target then
        if found then
          close t
        else
          set found to true
          set active tab index of w to i
          set index of w to 1
          tell t to reload
        end if
      end if
    end repeat
  end repeat
  if not found then
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to "http://localhost:5173"
    if URL of active tab of front window does not contain target then tell front window to make new tab with properties {URL:"http://localhost:5173"}
  end if
  activate
end tell
OSA
