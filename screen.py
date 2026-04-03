"""
JARVIS Screen Awareness — see what's on the user's screen.

Two capabilities:
1. Window/app list via AppleScript (fast, text-based)
2. Screenshot via screencapture → Claude vision API (sees everything)
"""

import asyncio
import base64
import logging
import tempfile
from pathlib import Path

log = logging.getLogger("jarvis.screen")

VISION_MAX_IMAGE_BYTES = 5 * 1024 * 1024
VISION_TARGET_IMAGE_BYTES = int(VISION_MAX_IMAGE_BYTES * 0.9)
VISION_REDUCTION_STEPS = [
    (None, 99),
    (None, 95),
    (None, 90),
    (3200, 90),
    (2800, 90),
    (2400, 85),
    (2200, 75),
]


async def get_active_windows() -> list[dict]:
    """Get list of visible windows with app name, window title, and position.

    Uses AppleScript + System Events to enumerate windows.
    Returns list of {"app": str, "title": str, "frontmost": bool}.
    """
    # Use a simpler approach that's more permission-friendly
    script = """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

        if proc.returncode != 0:
            log.warning(f"get_active_windows failed: {stderr.decode()[:200]}")
            return []

        windows = []
        for line in stdout.decode().strip().split("\n"):
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                windows.append({
                    "app": parts[0].strip(),
                    "title": parts[1].strip(),
                    "frontmost": parts[2].strip().lower() == "true",
                })
        return windows

    except asyncio.TimeoutError:
        log.warning("get_active_windows timed out")
        return []
    except Exception as e:
        log.warning(f"get_active_windows error: {e}")
        return []


async def get_running_apps() -> list[str]:
    """Get list of running application names (visible only)."""
    script = """
tell application "System Events"
    set appNames to name of every application process whose visible is true
    set output to ""
    repeat with a in appNames
        set output to output & a & linefeed
    end repeat
    return output
end tell
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return [a.strip() for a in stdout.decode().strip().split("\n") if a.strip()]
        return []
    except Exception as e:
        log.warning(f"get_running_apps error: {e}")
        return []


def _media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


async def _run_image_transform(input_path: Path, output_path: Path, max_pixels: int | None, quality: int) -> bool:
    cmd = [
        "sips",
        "-s", "format", "jpeg",
        "-s", "formatOptions", str(quality),
    ]
    if max_pixels is not None:
        cmd.extend(["-Z", str(max_pixels)])
    cmd.extend([
        str(input_path),
        "--out", str(output_path),
    ])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0 or not output_path.exists():
        log.warning(
            "Image compression failed: quality=%s max_pixels=%s stdout=%s stderr=%s",
            quality,
            max_pixels if max_pixels is not None else "full",
            stdout.decode(errors="replace")[:200],
            stderr.decode(errors="replace")[:200],
        )
        return False
    return True


async def _prepare_image_for_vision(image_path: Path) -> Path | None:
    """Shrink large screenshots until they fit within the Anthropic image limit."""
    if not image_path.exists():
        return None

    try:
        original_size = image_path.stat().st_size
    except Exception:
        return image_path

    if original_size <= VISION_TARGET_IMAGE_BYTES:
        return image_path

    candidate_path = image_path.with_name(f"{image_path.stem}.vision.jpg")
    smallest_path = image_path
    smallest_size = original_size

    for max_pixels, quality in VISION_REDUCTION_STEPS:
        if not await _run_image_transform(image_path, candidate_path, max_pixels, quality):
            continue

        try:
            candidate_size = candidate_path.stat().st_size
        except Exception:
            continue

        if candidate_size < smallest_size:
            smallest_path = candidate_path
            smallest_size = candidate_size

        if candidate_size <= VISION_TARGET_IMAGE_BYTES:
            log.info(
                "Compressed screenshot for vision: %s -> %s bytes (%s @ q%s)",
                original_size,
                candidate_size,
                f"{max_pixels}px" if max_pixels is not None else "full-res",
                quality,
            )
            return candidate_path

    if smallest_size < original_size:
        log.warning(
            "Screenshot still large after compression: %s -> %s bytes; sending smallest version",
            original_size,
            smallest_size,
        )
        return smallest_path

    return image_path


async def take_screenshot(display_only: bool = True) -> tuple[str, str] | None:
    """Take a screenshot and return base64 image data plus media type.

    Args:
        display_only: If True, capture main display only. If False, all displays.

    Returns:
        Tuple of (base64 image data, media type), or None on failure.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    tmp_file = Path(tmp_path)
    final_path = tmp_file

    try:
        cmd = ["screencapture", "-x"]  # -x = no sound
        if display_only:
            cmd.append("-m")  # main display only
        cmd.append(tmp_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

        if proc.returncode != 0 or not final_path.exists():
            log.warning(
                "Screenshot capture failed: returncode=%s stderr=%s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            return None

        optimized_path = await _prepare_image_for_vision(final_path)
        if optimized_path:
            final_path = optimized_path

        data = final_path.read_bytes()
        if len(data) > VISION_MAX_IMAGE_BYTES:
            log.warning(
                "Prepared screenshot still exceeds vision limit: %s bytes (%s)",
                len(data),
                final_path.name,
            )
            return None

        log.info(
            "Screenshot captured for vision: %s bytes (%s)",
            len(data),
            final_path.suffix.lower().lstrip(".") or "bin",
        )
        return base64.b64encode(data).decode(), _media_type_for_path(final_path)

    except asyncio.TimeoutError:
        log.warning("Screenshot timed out")
        return None
    except Exception as e:
        log.warning(f"Screenshot error: {e}")
        return None
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
            if final_path != tmp_file:
                final_path.unlink(missing_ok=True)
        except Exception:
            pass


async def describe_screen(anthropic_client) -> str:
    """Describe what's on the user's screen.

    Tries screenshot + vision first. Falls back to window list + LLM summary.
    """
    # Try screenshot + vision
    screenshot = await take_screenshot()
    if screenshot and anthropic_client:
        screenshot_b64, media_type = screenshot
        try:
            response = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=(
                    "You are JARVIS analyzing a screenshot of the user's desktop. "
                    "Describe what you see concisely: what the user appears to be working on, "
                    "which window is most important, and any notable visible content. "
                    "Focus on readable on-screen text, terminal output, editor text, document titles, "
                    "URLs, filenames, code, or spreadsheet content when legible. "
                    "Do not just list apps unless the text is unreadable. "
                    "2-4 sentences max. No markdown."
                ),
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": screenshot_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "What's on my screen right now?",
                        },
                    ],
                }],
            )
            return response.content[0].text
        except Exception as e:
            log.warning(f"Vision call failed, falling back to window list: {e}")

    # Fallback: get window list and have LLM summarize
    windows = await get_active_windows()
    apps = await get_running_apps()

    if not windows and not apps:
        return "I wasn't able to see your screen, sir. Screen recording permission may be needed."

    # Build a text description for LLM to summarize
    context_parts = []
    if windows:
        for w in windows:
            marker = " (ACTIVE)" if w["frontmost"] else ""
            context_parts.append(f"{w['app']}: {w['title']}{marker}")

    if apps:
        window_apps = set(w["app"] for w in windows) if windows else set()
        bg_apps = [a for a in apps if a not in window_apps]
        if bg_apps:
            context_parts.append(f"Background apps: {', '.join(bg_apps)}")

    if anthropic_client and context_parts:
        try:
            response = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system=(
                    "You are JARVIS. Given the user's open windows and apps, summarize "
                    "what they appear to be working on in 1-2 sentences. Natural voice, no markdown."
                ),
                messages=[{"role": "user", "content": "Open windows:\n" + "\n".join(context_parts)}],
            )
            return response.content[0].text
        except Exception:
            pass

    # Raw fallback
    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {len(windows)} windows open across {len(set(w['app'] for w in windows))} apps."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result

    return f"Running apps: {', '.join(apps)}. Couldn't read window titles, sir."


def format_windows_for_context(windows: list[dict]) -> str:
    """Format window list as context string for the LLM."""
    if not windows:
        return ""
    lines = ["Currently open on your desktop:"]
    for w in windows:
        marker = " (active)" if w["frontmost"] else ""
        lines.append(f"  - {w['app']}: {w['title']}{marker}")
    return "\n".join(lines)
