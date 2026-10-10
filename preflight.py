"""
JARVIS preflight -- first-run environment checks.

Every one of these has already bitten this project: `claude` missing or too
old, not logged in (the voice brain runs on the user's *subscription*, never
an API key -- see claude_env.py's SCRUBBED_ENV_PREFIXES), `crossSessionInbound`
not accepting steers into other sessions, `osascript` lacking Accessibility
so `answer_dialog`'s keystroke fails, and no Fish Audio key at all.

This module only *observes*. Nothing here writes a file, changes a setting,
or grants a permission -- see `_check_cross_session_inbound_sync`'s
docstring for why that one in particular must stay read-only. It runs at
server startup, so the contract is the same one `notifier.py` keeps for its
own subprocess boundary: **never raise**. A check that itself errors, hangs,
or can't be parsed becomes a `warn` Check carrying the error text, never an
exception -- this must not be able to prevent the server from booting.

Subprocess handling follows notifier.py's pattern (read it first): spawn off
the event loop, bound by a timeout, kill-and-reap on timeout, decode leniently.
All process boundaries funnel through `_run_subprocess` below so tests can
mock the one seam instead of patching `asyncio.create_subprocess_exec`
per-call (the pattern `dialog.py` uses for `_osascript`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import claude_env
import screen

log = logging.getLogger("jarvis.preflight")

# CLAUDE.md: "2.1.224 or newer" -- cross-session messaging does not exist
# below that. Compared as a tuple of ints, never as a string: "2.1.9" is
# lexicographically GREATER than "2.1.224" (the naive, wrong comparison),
# even though 9 < 224 as a version component.
MIN_CLAUDE_VERSION = (2, 1, 224)
MIN_CLAUDE_VERSION_STR = "2.1.224"

# A hung subprocess (a wedged `claude`, a modal `osascript` is waiting on)
# must not stall server startup. Each check gets its own budget; they run
# concurrently in run_checks() so the wall-clock cost is one timeout, not
# the sum of them.
DEFAULT_CHECK_TIMEOUT = 5.0

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """One preflight result.

    `remedy` is a concrete, user-actionable next step ("run `claude` and
    log in") -- it is None exactly when `status` is "ok", since a passing
    check has nothing to remedy.
    """
    name: str
    status: str  # "ok" | "warn" | "fail"
    message: str
    remedy: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


# ── the one subprocess boundary ─────────────────────────────────────────

async def _run_subprocess(*args: str, timeout: float,
                           env: Optional[dict[str, str]] = None) -> tuple[int, str, str]:
    """Run a subprocess, capturing stdout/stderr, bounded by `timeout`.

    `env=None` (the default) inherits this process's ambient environment,
    same as before. A caller that needs the subprocess to see a SPECIFIC
    environment -- e.g. `claude_login` checking under exactly the
    environment the brain spawns with -- passes one explicitly.

    Never raises: a spawn failure or a timeout comes back as returncode -1
    with the problem described in stderr, exactly like notifier.py's
    `notify()` treats a wedged or missing `osascript`. This is the single
    seam every check in this module spawns a process through, so tests can
    mock it once instead of patching `asyncio.create_subprocess_exec` at
    each call site.
    """
    import process_tree
    try:
        proc = await process_tree.spawn(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return -1, "", f"failed to spawn {args[0] if args else '?'}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        try:
            process_tree.kill(proc)
            await proc.communicate()
        except Exception:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        return -1, "", f"{args[0] if args else '?'} timed out after {timeout}s"
    finally:
        process_tree.release(proc)
        # Whether or not it has been seen to exit. Cancelled again during the
        # reap above, `returncode` is still None, and a transport left open
        # then outlives its loop: the garbage collector closes its pipes
        # against a closed loop. The child is being killed either way.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()

    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """Pull the first X.Y.Z out of e.g. '2.1.258 (Claude Code)'."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


# ── individual checks ────────────────────────────────────────────────────

async def _check_claude_cli(timeout: float = DEFAULT_CHECK_TIMEOUT) -> Check:
    """`claude` is on PATH and is at least MIN_CLAUDE_VERSION."""
    claude = shutil.which("claude")
    if not claude:
        return Check(
            name="claude_cli",
            status=STATUS_FAIL,
            message="`claude` is not on PATH.",
            remedy=(
                "Install Claude Code (npm install -g @anthropic-ai/claude-code, "
                f"{MIN_CLAUDE_VERSION_STR} or newer) and make sure it's on PATH."
            ),
        )

    rc, stdout, stderr = await _run_subprocess(claude, "--version", timeout=timeout)
    if rc != 0:
        return Check(
            name="claude_cli",
            status=STATUS_WARN,
            message=f"`claude --version` failed: {(stderr or stdout).strip() or f'exit {rc}'}",
            remedy="Run `claude --version` yourself to see what's wrong.",
        )

    version = _parse_version(stdout) or _parse_version(stderr)
    if version is None:
        return Check(
            name="claude_cli",
            status=STATUS_WARN,
            message=f"Could not parse a version from `claude --version` output: {stdout.strip()!r}",
            remedy=f"Run `claude --version` yourself and confirm it's {MIN_CLAUDE_VERSION_STR} or newer.",
        )

    version_str = ".".join(str(p) for p in version)
    if version < MIN_CLAUDE_VERSION:
        return Check(
            name="claude_cli",
            status=STATUS_FAIL,
            message=f"claude {version_str} is older than the required {MIN_CLAUDE_VERSION_STR}.",
            remedy="Update Claude Code: npm install -g @anthropic-ai/claude-code@latest",
        )

    return Check(name="claude_cli", status=STATUS_OK, message=f"claude {version_str} on PATH.")


def _config_dir_from_env(env: dict[str, str]) -> Path:
    """Where `claude` reads its config from, under `env` -- honours
    CLAUDE_CONFIG_DIR exactly like the CLI does, falling back to its
    default of ~/.claude. Mirrors `_settings_path()`, but takes an
    explicit env dict rather than reading `os.environ` directly, so a
    caller can point it at the SAME environment a subprocess was run
    under instead of whatever this process's ambient environment is."""
    root = env.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(root).expanduser()


def _keychain_service_name(config_dir: Path) -> str:
    """The macOS Keychain service name `claude` stores OAuth credentials
    under for a given config dir.

    Reverse-engineered empirically against a real install (not documented
    by the CLI): the default config dir (~/.claude) uses the plain service
    name "Claude Code-credentials"; any other CLAUDE_CONFIG_DIR gets an
    8-hex-char suffix that is the start of sha256(str(config_dir)) --
    verified against a live ~/.claude-orcha install. If a future CLI
    version changes this scheme, the keychain lookup below simply finds no
    matching entry, and the caller treats that as "cannot verify" rather
    than reporting a wrong answer -- this function is a best-effort second
    signal, never the sole basis for a result.
    """
    default = Path("~/.claude").expanduser()
    if config_dir == default:
        return "Claude Code-credentials"
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


async def _read_oauth_refresh_expiry(config_dir: Path, timeout: float) -> Optional[float]:
    """Best-effort read of the OAuth refresh token's expiry (unix seconds)
    from the macOS Keychain, for `config_dir` -- WITHOUT ever surfacing the
    access or refresh token values themselves, only the expiry timestamp.

    This is what actually distinguishes "logged in" from "logged in but
    the session cannot be refreshed": `claude auth status` reports only
    presence, and on a live machine was seen reporting a session as logged
    in that a real turn then failed to authenticate with. The CLI decides
    whether a refresh will succeed from this same stored
    `refreshTokenExpiresAt`, so reading it is the most faithful non-invasive
    signal available -- short of spending a real turn, which this module
    must not do.

    Returns None -- "unknown", never "not expired" -- when the probe can't
    be attempted or trusted: not macOS, `security` missing, no matching
    keychain entry, or output that doesn't parse the way expected. Callers
    must not treat None as a clean bill of health.
    """
    if sys.platform != "darwin" or not shutil.which("security"):
        return None
    service = _keychain_service_name(config_dir)
    rc, stdout, _stderr = await _run_subprocess(
        "security", "find-generic-password", "-s", service, "-w", timeout=timeout)
    if rc != 0 or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    expires_at_ms = oauth.get("refreshTokenExpiresAt")
    if not isinstance(expires_at_ms, (int, float)):
        return None
    return expires_at_ms / 1000.0


async def _check_claude_login(timeout: float = DEFAULT_CHECK_TIMEOUT) -> Check:
    """The voice brain runs on the user's subscription -- `claude` must be
    logged in, AND that login must actually be usable.

    Runs `claude auth status` under exactly `claude_env.child_env()` -- the
    same environment brain.py and run_executor.py spawn `claude` with --
    rather than whatever happened to be in this process's ambient
    environment, so a mismatched CLAUDE_CONFIG_DIR can't make this check
    pass while the brain itself fails to authenticate. The config dir in
    play is always named in the result so a mismatch is visible at a
    glance.

    `loggedIn: true` alone is not enough: a real incident on this project
    had `claude auth status` report a session as logged in that then
    failed every turn with "OAuth session expired and could not be
    refreshed". When the account uses OAuth (authMethod "claude.ai"), this
    also reads the stored refresh token's expiry from the Keychain (see
    `_read_oauth_refresh_expiry`) and fails the check if it has already
    passed. When that secondary probe can't be attempted or trusted, the
    check stays OK (matching prior behaviour) but says so honestly rather
    than implying a guarantee it cannot make.
    """
    claude = shutil.which("claude")
    if not claude:
        return Check(
            name="claude_login",
            status=STATUS_WARN,
            message="Can't check login: `claude` is not on PATH.",
            remedy="Install Claude Code and log in with `claude`.",
        )

    env = claude_env.child_env()
    config_dir = _config_dir_from_env(env)
    where = f"config dir: {config_dir}"

    rc, stdout, stderr = await _run_subprocess(claude, "auth", "status", timeout=timeout, env=env)
    if rc != 0:
        return Check(
            name="claude_login",
            status=STATUS_FAIL,
            message=f"`claude auth status` failed ({where}): {(stderr or stdout).strip() or f'exit {rc}'}",
            remedy="Run `claude` and log in.",
        )

    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return Check(
            name="claude_login",
            status=STATUS_WARN,
            message=f"Could not parse `claude auth status` output ({where}): {stdout.strip()!r}",
            remedy="Run `claude auth status` yourself to confirm you're logged in.",
        )

    if not data.get("loggedIn"):
        return Check(
            name="claude_login",
            status=STATUS_FAIL,
            message=f"Claude Code is not logged in ({where}).",
            remedy="Run `claude` and log in -- the voice brain runs on your subscription, not an API key.",
        )

    email = data.get("email")
    message = f"Logged in as {email} ({where})." if email else f"Logged in ({where})."

    if data.get("authMethod") == "claude.ai":
        expires_at = await _read_oauth_refresh_expiry(config_dir, timeout)
        if expires_at is None:
            message += (" Could not independently verify the OAuth session's refresh-token "
                        "expiry from this process -- `claude auth status` alone can report "
                        "\"logged in\" even when a real turn would fail to authenticate.")
        elif expires_at <= time.time():
            when = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
            return Check(
                name="claude_login",
                status=STATUS_FAIL,
                message=(f"Claude Code reports logged in ({where}), but its OAuth session's "
                         f"refresh token expired on {when} and could not be refreshed -- this "
                         "is the exact failure that silences the voice brain."),
                remedy="Run `claude` in a terminal and log in again.",
            )
        else:
            message += " OAuth refresh token is current."

    return Check(name="claude_login", status=STATUS_OK, message=message)


# macOS's own wording (and error code) for "Accessibility not granted" --
# distinctive enough it can't match an ordinary AppleScript error. Mirrors
# dialog.py's _PERMISSION_MARKERS, which sees the sibling "-1743"/"-25211"
# errors for a different System Events call.
_ACCESSIBILITY_MARKERS = (
    "-1728",
    "not allowed assistive access",
)


async def _check_accessibility(timeout: float = DEFAULT_CHECK_TIMEOUT) -> Check:
    """Whether osascript has Accessibility (assistive access), without prompting for it.

    `answer_dialog` sends a keystroke via System Events; without this
    permission the first real keypress fails. Asking System Events for a
    window list is read-only and, verified live on the dev machine, does
    NOT trigger a permission dialog when access is missing -- it simply
    returns AppleScript error -1728. If that ever stops being true on some
    macOS version, this comes back as a WARN (unrecognised error) rather
    than mis-reporting OK, so it fails safe.
    """
    if sys.platform != "darwin":
        # A macOS permission. On any other platform there is nothing to
        # grant and nothing to fix, so this is not a warning — it warned on
        # every Windows start and taught the user to ignore the list.
        return Check(name="accessibility", status=STATUS_OK,
                     message="Not applicable: Accessibility is a macOS permission.")
    if not shutil.which("osascript"):
        return Check(
            name="accessibility",
            status=STATUS_WARN,
            message="Cannot check Accessibility: osascript is missing.",
        )

    rc, stdout, stderr = await _run_subprocess(
        "osascript", "-e",
        'tell application "System Events" to tell process "Finder" to get name of every window',
        timeout=timeout,
    )

    if rc == 0:
        return Check(name="accessibility", status=STATUS_OK, message="osascript has Accessibility access.")

    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _ACCESSIBILITY_MARKERS):
        return Check(
            name="accessibility",
            status=STATUS_FAIL,
            message="osascript is not granted Accessibility (assistive access); answer_dialog's keystroke will fail.",
            remedy=(
                "macOS attributes this to the app that launched JARVIS, not to "
                "python or osascript. Grant that app under System Settings -> "
                "Privacy & Security -> Accessibility, or start the server from a "
                "terminal that already has it. If the app is already ticked and "
                "this still fails, check whether it is running from "
                "/private/var/.../AppTranslocation/ (`ps -o comm= -p <its pid>`): "
                "macOS runs apps opened straight from Downloads at a randomised "
                "path, and a grant does not follow them there. Move the app to "
                "/Applications and relaunch it."
            ),
        )

    return Check(
        name="accessibility",
        status=STATUS_WARN,
        message=f"Could not determine Accessibility status: {(stderr or stdout).strip()}",
    )


def _check_screen_recording_sync() -> Check:
    """Whether JARVIS may see the screen at all -- asked, never demonstrated.

    The same lesson as Accessibility one permission along, and worse in one
    respect: Accessibility fails loudly (AppleScript error -1728), while a
    `screencapture` without Screen Recording exits 0 and hands back a black
    or desktop-only frame. `screen.capture_screen` refuses such a frame at
    the moment of asking; this says it at startup, before the user has spoken.

    It asks CoreGraphics (`CGPreflightScreenCaptureAccess`, the non-prompting
    one) and NEVER captures anything to find out -- a screenshot the user did
    not ask for, at every boot, is precisely what this capability must not do.
    """
    try:
        granted = screen.screen_recording_granted()
    except Exception as e:  # the module must never take startup down
        return Check(name="screen_recording", status=STATUS_WARN,
                     message=f"Could not determine Screen Recording status: {e}")

    if granted is True:
        return Check(name="screen_recording", status=STATUS_OK,
                     message="JARVIS has Screen Recording access.")
    if granted is None:
        if sys.platform != "darwin":
            # Not a permission this platform has; `screen.py` captures
            # without one. Saying "could not determine" every start was noise.
            return Check(name="screen_recording", status=STATUS_OK,
                         message="Not applicable: Screen Recording is a macOS permission.")
        return Check(
            name="screen_recording", status=STATUS_WARN,
            message="Could not determine Screen Recording status: CoreGraphics could not be asked.")
    return Check(
        name="screen_recording",
        status=STATUS_FAIL,
        message="JARVIS has not been granted Screen Recording; look_at_screen will refuse.",
        remedy=(
            "macOS attributes this to the app that launched JARVIS, not to "
            "python or screencapture -- the same rule as Accessibility above. "
            "Grant that app Screen Recording under System Settings -> Privacy "
            "& Security -> Screen & System Audio Recording, then RESTART it: "
            "the grant only reaches a process started after it was given."
        ),
    )


def _check_fish_api_key_sync() -> Check:
    """FISH_API_KEY must be set or JARVIS has no voice."""
    if os.environ.get("FISH_API_KEY"):
        return Check(name="fish_api_key", status=STATUS_OK, message="FISH_API_KEY is set.")
    return Check(
        name="fish_api_key",
        status=STATUS_FAIL,
        message="FISH_API_KEY is not set.",
        remedy="Get a Fish Audio API key from fish.audio and set FISH_API_KEY in .env.",
    )


def _check_whatsapp_sync() -> Check:
    """The WhatsApp line is optional; a HALF-configured one is the failure.

    Nothing set is fine and says so. One of the three names set and the
    others not — or all three set with the owner's number in the wrong shape
    — means the user meant to have it and does not, and the first they would
    hear of it is a card that never reached their phone.
    """
    import whatsapp
    state = whatsapp.status()
    if not state["touched"]:
        return Check(name="whatsapp", status=STATUS_OK,
                     message="WhatsApp is not configured (optional; see docs/whatsapp.md).")
    if state["missing"]:
        return Check(
            name="whatsapp", status=STATUS_WARN,
            message=f"WhatsApp is half set up: {', '.join(state['missing'])} not set.",
            remedy=("Set KAPSO_API_KEY, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_OWNER_NUMBER "
                    "(or JARVIS_OWNER_PHONE) in .env — Settings → WhatsApp does it — or "
                    "unset them all. `python scripts/whatsapp_setup.py numbers` finds the id."))
    if state["issue"]:
        return Check(name="whatsapp", status=STATUS_WARN,
                     message=f"WhatsApp cannot be used: {state['issue']}.",
                     remedy="Correct the value in .env and restart, or fix it under Settings → WhatsApp.")
    extra = ""
    if not state["template"]:
        extra = (" No WHATSAPP_TEMPLATE: outside the 24 hours after your last message he "
                 "cannot reach you (`python scripts/whatsapp_setup.py template`).")
    return Check(name="whatsapp", status=STATUS_OK,
                 message=f"WhatsApp is configured for {state['owner']}.{extra}")


def _check_telegram_sync() -> Check:
    """The Telegram line is optional; a token waiting to be paired is the
    state worth reporting, because until the code is sent from the owner's
    phone he can reach nobody on it."""
    import telegram
    state = telegram.status()
    if not state["touched"]:
        return Check(name="telegram", status=STATUS_OK,
                     message="Telegram is not configured (optional; see docs/telegram.md).")
    if state["issue"]:
        return Check(name="telegram", status=STATUS_WARN,
                     message=f"Telegram cannot be used: {state['issue']}.",
                     remedy="Correct the value in .env, or under Settings → Telegram.")
    # What the poll last ran into, in any state: a token Telegram refuses
    # (401) or a bot someone else is polling (409) looks, from the outside,
    # exactly like "not paired yet" — and pairing cannot fix either. The
    # poll's own error, which the next good poll clears; not `last_error`,
    # which a send that failed once would hold here for good.
    failing = f" The last poll failed: {state['poll_error']}." if state["poll_error"] else ""
    if state["missing"] == ["TELEGRAM_OWNER_ID"]:
        return Check(
            name="telegram", status=STATUS_WARN,
            message="Telegram has a bot token but is not paired with you yet." + failing,
            remedy=("Settings → Telegram → Pair, then send the code to the bot from your "
                    "phone; or `python scripts/telegram_setup.py pair`."))
    if state["missing"]:
        return Check(
            name="telegram", status=STATUS_WARN,
            message=f"Telegram is half set up: {', '.join(state['missing'])} not set.",
            remedy="Set TELEGRAM_BOT_TOKEN (from @BotFather) under Settings → Telegram, then Pair.")
    if failing:
        return Check(name="telegram", status=STATUS_WARN,
                     message=f"Telegram is configured for user id {state['owner_id']}.{failing}",
                     remedy=("A 401 means Telegram refuses the token: paste it again from "
                             "@BotFather. A 409 means something else is polling this bot."))
    return Check(name="telegram", status=STATUS_OK,
                 message=f"Telegram is configured for user id {state['owner_id']}.")


async def _check_chatgpt_fallback(timeout: float = DEFAULT_CHECK_TIMEOUT) -> Check:
    """The ChatGPT fallback is optional, and off unless switched on. On, it
    is only as good as Codex's state under JARVIS's own home — signed in
    with a ChatGPT account, nothing unvetted switched on, a model it can
    gate — and a limit is the worst moment to find that out: in the middle
    of a conversation, with Claude already gone. So it is asked here, of
    Codex itself (`chatgpt_fallback.check_readiness`: local commands only,
    no model is called), and shown with the other start-up checks.

    Several Codex processes, so it gets the whole budget of its own and,
    when Codex is slow to start, goes on in the background: the answer is
    then ready for the first limit instead of lost to a timeout."""
    import chatgpt_fallback
    if not chatgpt_fallback.enabled():
        return Check(name="chatgpt_fallback", status=STATUS_OK,
                     message=("The ChatGPT fallback is off (optional; see "
                              "docs/chatgpt-fallback.md)."))
    # A fact of the configuration, not of Codex: said whatever Codex answers.
    unreachable = chatgpt_fallback.bind_problem() if _declared_connections() else None
    bind = f" Also, {unreachable}." if unreachable else ""
    bind_remedy = (" Bind JARVIS to 127.0.0.1, 0.0.0.0 or :: for your connections to work on "
                   "ChatGPT too." if unreachable else "")
    asking = asyncio.ensure_future(asyncio.to_thread(chatgpt_fallback.readiness, refresh=True))
    try:
        ready = await asyncio.wait_for(asyncio.shield(asking), timeout)
    except asyncio.TimeoutError:
        return Check(name="chatgpt_fallback", status=STATUS_WARN,
                     message=("The ChatGPT fallback's check did not finish in time; it goes on "
                              "in the background, and is asked again when Claude's limit is "
                              "reached." + bind),
                     remedy="Run `python scripts/chatgpt_setup.py status` for its answer."
                            + bind_remedy)
    if not ready.ok:
        remedy = ready.remedy[:1].upper() + ready.remedy[1:] + "." if ready.remedy else ""
        return Check(name="chatgpt_fallback", status=STATUS_WARN,
                     message=f"The ChatGPT fallback is on but can't stand in: {ready.reason}.{bind}",
                     remedy=(remedy + bind_remedy).strip() or None)
    if unreachable:
        return Check(name="chatgpt_fallback", status=STATUS_WARN,
                     message=f"ChatGPT will stand in while Claude is limited, but {unreachable}.",
                     remedy=bind_remedy.strip())
    return Check(name="chatgpt_fallback", status=STATUS_OK,
                 message=(f"ChatGPT will stand in while Claude is limited "
                          f"({ready.version or 'Codex'}, {chatgpt_fallback.model()})."))


def _declared_connections() -> list:
    """The server names in the user's connections file, or []. Never raises."""
    try:
        import data_paths
        body = json.loads(data_paths.connections_path().read_text(encoding="utf-8"))
        block = body.get("mcpServers") if isinstance(body, dict) else None
        return list(block) if isinstance(block, dict) else []
    except (OSError, ValueError):
        return []


def _check_anthropic_key_leftover_sync() -> Check:
    """A leftover ANTHROPIC_* var signals a misconfigured .env.

    `claude_env.child_env()` scrubs every ANTHROPIC_* variable from every
    Claude Code child -- the brain and every run -- so this can no longer
    make JARVIS silently bill an API key. It is still worth reporting:
    its presence means someone put an Anthropic API key in `.env`, which is
    not how this project authenticates (see brain-subscription-only-env-scrub
    history) and is a sign the rest of the setup may be off too.
    """
    leftover = sorted(k for k in os.environ if k.startswith("ANTHROPIC_"))
    if not leftover:
        return Check(
            name="anthropic_key_leftover",
            status=STATUS_OK,
            message="No leftover ANTHROPIC_* variables in the environment.",
        )
    return Check(
        name="anthropic_key_leftover",
        status=STATUS_WARN,
        message=(
            f"{', '.join(leftover)} set in the environment. No Claude Code child "
            "of JARVIS receives them, but this signals a misconfigured .env."
        ),
        remedy=(
            "Remove ANTHROPIC_* variables from .env -- JARVIS's voice brain runs "
            "on your Claude subscription, not an API key."
        ),
    )


_NAMES_SHOWN = 8


def _named(names: list[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    rest = len(names) - _NAMES_SHOWN
    return f"{shown} and {rest} more" if rest > 0 else shown


def _check_claude_session_env_sync() -> Check:
    """Was JARVIS itself started from inside a Claude Code session?

    A session hands every process it starts its own identity, wiring and
    tuning -- CLAUDECODE, its session id, its effort, its MCP start-up
    settings, its API timeout, its trace. Measured 2026-09-26: an agent ran
    `scripts/start-jarvis.ps1` and the backend carried all of it into the
    brain's `claude -p`. `claude_env.child_env()` now keeps it from every
    Claude Code child, and the launcher starts the backend without it; this
    says when the backend was started some other way, because its own
    environment still reaches everything else it starts, and `session_steer` still sends an
    inherited CLAUDE_CODE_MESSAGING_TOKEN -- a credential that authenticates
    only to the inbox of the session that exported it -- to every session it
    steers.

    Names only, never values: several of these are credentials. A user's own
    MCP_TIMEOUT is scrubbed from children too, but it is not a session, so it
    is reported without a warning.
    """
    names = claude_env.inherited_names()
    markers = claude_env.session_markers()
    if not markers:
        if not names:
            return Check(name="claude_session_env", status=STATUS_OK,
                         message="No Claude Code session variables in JARVIS's environment.")
        return Check(name="claude_session_env", status=STATUS_OK,
                     message=(f"Set in JARVIS's environment and kept from its Claude Code "
                              f"children: {_named(names)}."))
    token = ("; steering sends that session's inbox token to every session it steers, "
             "and only that session accepts it"
             if "CLAUDE_CODE_MESSAGING_TOKEN" in names else "")
    remedy = "Restart JARVIS from a terminal outside Claude Code"
    if sys.platform == "win32":
        remedy = ("Restart JARVIS with scripts\\stop-jarvis.ps1 then scripts\\start-jarvis.ps1, "
                  "which start it without them, or from a terminal outside Claude Code")
    return Check(
        name="claude_session_env",
        status=STATUS_WARN,
        message=(f"Started from inside a Claude Code session ({', '.join(markers)}): "
                 f"of the variables such a session hands down, JARVIS's own environment "
                 f"carries {len(names)} -- {_named(names)}. No Claude Code child of JARVIS "
                 f"receives them, but everything else it starts does{token}."),
        remedy=remedy + ".",
    )


def _settings_path() -> Path:
    """Where the CLI's user settings.json lives.

    Honours CLAUDE_CONFIG_DIR (the env var the CLI itself, and session_watch.py,
    respect) so this reports on the file `claude` will actually read for this
    process; falls back to the CLI's default of ~/.claude.
    """
    root = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(root).expanduser() / "settings.json"


def _check_cross_session_inbound_sync() -> Check:
    """Report -- never change -- what settings.json says about crossSessionInbound.

    Steering a session posts to its inbox socket; if the receiving side is
    not configured to accept, steers are held for approval or dropped. The
    fix is `"crossSessionInbound": "accept"` in the user's settings.json,
    but this function must never write it: the design is that JARVIS offers
    and the user agrees, never a silent write. This function only reads.
    """
    path = _settings_path()
    if not path.exists():
        return Check(
            name="cross_session_inbound",
            status=STATUS_WARN,
            message=(
                f"No settings file at {path}; crossSessionInbound is unset, so "
                "steers into other sessions will be held for approval or dropped."
            ),
            remedy=(
                'Offer to add "crossSessionInbound": "accept" to that settings.json '
                "-- only if the user agrees."
            ),
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return Check(
            name="cross_session_inbound",
            status=STATUS_WARN,
            message=f"Could not read/parse {path}: {e}",
            remedy=f"Check that {path} is valid JSON.",
        )

    if not isinstance(data, dict):
        return Check(
            name="cross_session_inbound",
            status=STATUS_WARN,
            message=f"{path} did not contain a JSON object.",
            remedy=f"Check that {path} is valid JSON.",
        )

    value = data.get("crossSessionInbound")
    if value == "accept":
        return Check(
            name="cross_session_inbound",
            status=STATUS_OK,
            message=f'crossSessionInbound is "accept" in {path}.',
        )

    if value is None:
        detail = f"crossSessionInbound is not set in {path}"
    else:
        detail = f'crossSessionInbound is {value!r} in {path}, not "accept"'

    return Check(
        name="cross_session_inbound",
        status=STATUS_WARN,
        message=f"{detail}; steers into other sessions will be held for approval or dropped.",
        remedy=(
            'Offer to set "crossSessionInbound": "accept" in that settings.json '
            "-- only if the user agrees; never write it silently."
        ),
    )


def _read_settings(path: Path) -> dict | None:
    """The settings object, or None if it is missing or not readable JSON.

    None is deliberately NOT the same as `{}`: a caller about to write must
    be able to tell "there is no file" from "there is a file I could not
    parse", because overwriting the second would destroy the user's hooks,
    plugins, marketplaces and status line.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cross_session_inbound_accepted() -> bool:
    """True only when settings.json says `"crossSessionInbound": "accept"`.

    Cheap enough to call on the steer path. Anything else — no file, bad
    JSON, a different value — is False, because in every one of those cases
    the message really will be held for the user to approve.
    """
    path = _settings_path()
    if not path.exists():
        return False
    data = _read_settings(path)
    return bool(data) and data.get("crossSessionInbound") == "accept"


def enable_cross_session_inbound() -> tuple[bool, str]:
    """Write `"crossSessionInbound": "accept"`, preserving everything else.

    Called ONLY after the user has said yes out loud — the design has always
    been that JARVIS offers and the user agrees, and nothing here should ever
    be reached from a preflight check or a background turn.

    Read-modify-write on the parsed object: this user's settings.json holds
    hooks, plugins, marketplaces and a status line, and every one of them
    survives. A file that exists but does not parse is REFUSED rather than
    replaced — a broken JSON file is still the user's configuration, and
    guessing at it would lose the lot.
    """
    path = _settings_path()
    if path.exists():
        data = _read_settings(path)
        if data is None:
            return False, (f"{path} isn't readable JSON; I won't rewrite it.")
    else:
        data = {}
    if data.get("crossSessionInbound") == "accept":
        return True, "already set"

    data["crossSessionInbound"] = "accept"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole, then moved into place, so an interrupted write can
        # never leave a truncated settings.json behind.
        tmp = path.with_name(path.name + ".jarvis-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        return False, f"could not write {path}: {e}"
    return True, str(path)


# ── memory ───────────────────────────────────────────────────────────────
#
# Both of these were live defects for days, and both are one directory
# listing to detect. Observation only, like everything else here: the repair
# for the first is the dashboard's button or `maintenance.py reindex`, and
# the repair for the second is the user's — it is their file.

def _check_memory_index_sync() -> Check:
    """Every note in `memory/` has a line in MEMORY.md.

    The index is `@`-imported into every generation and is the ONLY thing
    that tells the brain a note exists at boot. A note with no line — written
    by hand, by a setup script, by a partial restore — is invisible to him
    until something happens to `recall` it, and nothing said so.
    """
    import jarvis_memory
    orphans = jarvis_memory.unindexed_memories()
    if not orphans:
        return Check(name="memory_index", status=STATUS_OK,
                     message="Every memory file is in MEMORY.md.")
    names = ", ".join(o["title"] for o in orphans[:5])
    more = f" and {len(orphans) - 5} more" if len(orphans) > 5 else ""
    return Check(
        name="memory_index", status=STATUS_WARN,
        message=(f"{len(orphans)} memory file(s) are not in MEMORY.md, so the "
                 f"brain does not know they exist: {names}{more}."),
        remedy=("Open the dashboard's Memory tab and choose 'Add them to the "
                "index', or run `python maintenance.py reindex` with JARVIS "
                "stopped."))


def _check_persona_sync() -> Check:
    """The brain's CLAUDE.md is the one this version ships, or will be.

    An edited persona is never overwritten — that is the promise — but it
    also never receives another upgrade, including the memory instructions
    and the injection-handling rules that file carries. The log said so;
    nobody reads the log. `LOCAL.md` exists so the user's words have
    somewhere to live that does not cost them every future fix.
    """
    import data_paths
    status = data_paths.persona_status()
    if status != "edited":
        return Check(name="persona", status=STATUS_OK,
                     message=f"The persona is {status}.")
    return Check(
        name="persona", status=STATUS_WARN,
        message=(f"{data_paths.persona_path()} has been edited, so the persona "
                 f"shipped with this version is not being applied."),
        remedy=(f"Move your own additions into {data_paths.local_persona_path()} "
                f"(read into every conversation, never overwritten) and delete "
                f"CLAUDE.md; JARVIS writes the current one on the next start."))


# ── private files ────────────────────────────────────────────────────────
#
# The token that admits a caller to the memory writers, the database, the
# memory folder and every backup archive live under the data directory.
# `data_paths.restrict_to_owner` keeps that folder to this account; this
# says so when it has not happened — on Windows by reading the ACL, since a
# POSIX mode means nothing there. Measured live before the fix:
# `Authenticated Users:(M)` on the token and the database.

def _posix_mode(path: Path) -> int:
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


def _windows_private_files(root: Path) -> Check:
    import data_paths
    try:
        strangers = data_paths.foreign_entries(root)
    except Exception as e:
        return Check(name="private_files", status=STATUS_WARN,
                     message=f"Could not read the permissions under {root}: {e}",
                     remedy="Check the folder's Security tab; only your account should have access.")
    if not strangers:
        return Check(name="private_files", status=STATUS_OK,
                     message=f"{root} and everything under it are restricted to this account.")
    sid = data_paths._current_sid() or "%USERNAME%"
    at_root = next((who for path, who in strangers if path == root), None)
    if at_root:
        # The directory grant, and only the directory: what is under it
        # inherits. `/T` would hand the same grant to every file, where
        # icacls drops it and leaves the file admitting nobody.
        return Check(
            name="private_files", status=STATUS_WARN,
            message=(f"{root} is readable by {', '.join(at_root)} — the tool token, the "
                     f"database and every backup inherit that."),
            remedy=(f"Restart JARVIS (it restricts the folder at startup), or run: "
                    f'icacls "{root}" /inheritance:r /grant:r "*{sid}:(OI)(CI)F" '
                    f'/grant:r "*S-1-5-18:(OI)(CI)F" /grant:r "*S-1-5-32-544:(OI)(CI)F"'))
    shown = strangers[:3]
    listed = "; ".join(f"{path} ({', '.join(who)})" for path, who in shown)
    more = f"; and {len(strangers) - len(shown)} more" if len(strangers) > len(shown) else ""
    noun = "item" if len(strangers) == 1 else "items"
    return Check(
        name="private_files", status=STATUS_WARN,
        message=f"{len(strangers)} {noun} under {root} can be read by other accounts: {listed}{more}.",
        remedy=("Restart JARVIS (it restricts them at startup), or make each inherit again: "
                + " ".join(f'icacls "{path}" /reset' for path, _ in shown)))


def _check_private_files_sync() -> Check:
    import data_paths
    root = data_paths.data_dir()
    if sys.platform == "win32":
        return _windows_private_files(root)
    try:
        mode = _posix_mode(root)
    except OSError as e:
        return Check(name="private_files", status=STATUS_WARN,
                     message=f"Could not read the mode of {root}: {e}")
    if mode & 0o077:
        return Check(name="private_files", status=STATUS_WARN,
                     message=f"{root} is mode {mode:o}: readable beyond this account.",
                     remedy=f"chmod 700 {root}")
    return Check(name="private_files", status=STATUS_OK,
                 message=f"{root} is mode {mode:o}.")


# ── MCP servers started by a package runner ─────────────────────────────
#
# The brain records which of the user's MCP servers had joined when the
# CLI started; one still starting is logged "MCP roster incomplete" and its
# tools are absent from the brain's first turns. A server launched through
# uvx, npx, `uv run` or `pipx run` resolves its packages on every launch,
# and whenever a dependency has released since the last start it installs
# first. Measured here twice: 2026-09-23 the linkedin server (uvx, floating
# transitive deps, ~24 s against 1.7 s from a venv), and 2026-09-25
# paperclip-board (uvx reinstalled 72 packages for a new FastMCP; 22 s
# against 1.2 s from a venv). Both left the brain without the server and
# nothing said why until the roster line was added. This says it before
# the fact, from connections.json alone: read-only, no process started.

# Runners that fetch and resolve at launch unless told to stay offline.
_RESOLVING_RUNNERS = frozenset({"uvx", "npx", "pnpx", "bunx"})
# `uv run` / `uv tool run` sync an environment first; these flags stop that
# touching the network or re-resolving.
_UV_RUN_SAFE_FLAGS = frozenset({"--frozen", "--offline", "--no-sync"})
_WRAPPER_SHELLS = frozenset({"cmd", "powershell", "pwsh"})
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1")


def _executable_name(token: str) -> str:
    """`C:/…/uvx.exe` -> `uvx`: what a launcher is, whatever the path says."""
    name = re.split(r"[\\/]", token.strip().strip('"'))[-1].lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _launched_commands(command: str, args: list) -> list[tuple[str, list[str]]]:
    """Every (executable, its args) this entry starts, wrappers unwrapped.

    Two wrapper shapes are followed: the `--` convention (`node mcp-subset
    --allow … -- <real command> …`, how this install filters linkedin's
    tools) and a Windows shell (`cmd /c npx …`).
    """
    tokens = [a for a in args if isinstance(a, str)]
    launched = [(_executable_name(command), tokens)]
    if "--" in tokens:
        rest = tokens[tokens.index("--") + 1:]
        if rest:
            launched += _launched_commands(rest[0], rest[1:])
    if _executable_name(command) in _WRAPPER_SHELLS:
        for i, token in enumerate(tokens):
            if token.lower() in ("/c", "/k", "-c", "-command") and i + 1 < len(tokens):
                inner = tokens[i + 1:]
                if len(inner) == 1 and " " in inner[0]:
                    inner = inner[0].split()          # `cmd /c "npx -y pkg"`
                launched += _launched_commands(inner[0], inner[1:])
                break
    return launched


def _runner_for(executable: str, args: list[str]) -> Optional[str]:
    """The package runner this launch goes through, or None if it needs none."""
    flags = set(args)
    if executable in _RESOLVING_RUNNERS:
        return None if "--offline" in flags else executable
    positional = [a for a in args if not a.startswith("-")]
    if executable == "uv":
        if positional[:1] == ["run"]:
            runner = "uv run"
        elif positional[:2] == ["tool", "run"]:       # the long spelling of uvx
            runner = "uv tool run"
        else:
            return None                                # uv pip, uv venv, …
        return None if flags & _UV_RUN_SAFE_FLAGS else runner
    if executable == "pipx" and positional[:1] == ["run"]:
        return "pipx run"
    return None


def _check_mcp_launchers_sync() -> Check:
    import data_paths
    path = data_paths.connections_path()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Check(name="mcp_launchers", status=STATUS_OK,
                     message="No connections declared, so nothing is launched through a package runner.")
    except (OSError, ValueError) as e:
        # Not this check's finding to report: _write_mcp_config logs it at
        # startup and the `connections` tool says it aloud.
        return Check(name="mcp_launchers", status=STATUS_OK,
                     message=f"{path} could not be read ({e}); the connections report says why.")
    block = body.get("mcpServers") if isinstance(body, dict) else None
    found = []
    for name, entry in (block.items() if isinstance(block, dict) else ()):
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue                                   # URL servers start nothing
        args = entry.get("args") if isinstance(entry.get("args"), list) else []
        for executable, its_args in _launched_commands(entry["command"], args):
            runner = _runner_for(executable, its_args)
            if runner:
                found.append((str(name)[:64], runner))
                break
    if not found:
        return Check(name="mcp_launchers", status=STATUS_OK,
                     message="Every declared MCP server starts from an installed executable.")
    listed = ", ".join(f"{json.dumps(name)} ({runner})" for name, runner in found)
    return Check(
        name="mcp_launchers", status=STATUS_WARN,
        message=(f"Started through a package runner that resolves packages on every launch: "
                 f"{listed}. When a dependency has released since the last start it installs "
                 f"first, the server can miss the start-up window, and the brain then begins "
                 f"without it (logged as \"MCP roster incomplete\")."),
        remedy=(f"Install each one once and point its \"command\" in {path} at the installed "
                f"executable: a venv for a Python server (e.g. `uv venv` + `uv pip install`, then "
                f"<venv>\\Scripts\\<server>.exe), `npm install` into a folder of its own for a Node "
                f"one. Restart JARVIS afterwards."))


# ── running them all ─────────────────────────────────────────────────────

_ASYNC_CHECKS = (_check_claude_cli, _check_claude_login, _check_accessibility,
                 _check_chatgpt_fallback)
_SYNC_CHECKS = (_check_fish_api_key_sync, _check_anthropic_key_leftover_sync,
                _check_whatsapp_sync, _check_telegram_sync, _check_claude_session_env_sync,
                _check_cross_session_inbound_sync, _check_screen_recording_sync,
                _check_memory_index_sync, _check_persona_sync,
                _check_private_files_sync, _check_mcp_launchers_sync)


async def _run_one(fn, *, is_async: bool, timeout: float) -> Check:
    """Run one check, bounded by `timeout`, and never let it raise or hang.

    A check that errors internally, or simply runs long (a wedged
    subprocess, an unexpectedly slow disk), becomes a `warn` Check instead
    of propagating -- this runs at server startup and must not be able to
    block or crash it.
    """
    name = getattr(fn, "__name__", "check")
    try:
        if is_async:
            coro = fn(timeout=timeout)
        else:
            coro = asyncio.to_thread(fn)
        # A little slack over the inner subprocess timeout so a check that
        # honours its own `timeout` argument reports its own message
        # instead of being pre-empted by this outer guard.
        return await asyncio.wait_for(coro, timeout=timeout + 1.0)
    except asyncio.TimeoutError:
        return Check(name=name, status=STATUS_WARN, message=f"Check '{name}' timed out.")
    except Exception as e:  # belt and suspenders: this must never raise into the caller
        log.warning(f"preflight: check '{name}' raised: {e}")
        return Check(name=name, status=STATUS_WARN, message=f"Check '{name}' raised: {e}")


async def run_checks(*, timeout: float = DEFAULT_CHECK_TIMEOUT) -> list[Check]:
    """Run every environment check concurrently, each individually time-boxed.

    Never raises. Safe to call at startup: the worst case is a handful of
    `warn` results after `timeout` seconds, not a hung or crashed server.
    """
    runs = [_run_one(fn, is_async=True, timeout=timeout) for fn in _ASYNC_CHECKS]
    runs += [_run_one(fn, is_async=False, timeout=timeout) for fn in _SYNC_CHECKS]
    # A TaskGroup, not `gather`: cancelled -- the server shutting down while
    # the checks still run -- it waits for EVERY check to stop before passing
    # the cancellation on. `gather` passed it on as soon as the first had,
    # while another was still killing and reaping its `claude` child, so
    # shutdown carried on and the loop closed under the reap.
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(run) for run in runs]
    return [task.result() for task in tasks]


# ── spoken summary ───────────────────────────────────────────────────────

# Short, voice-friendly phrases keyed by check name. Deliberately not the
# full `message` text (which is written for logs/UI, not a sentence spoken
# aloud) -- picked by a substring of the message so the phrase still fits
# the specific failure (e.g. "isn't installed" vs. "needs updating").
def _phrase_for(check: Check) -> str:
    name, msg = check.name, check.message
    if name == "claude_cli":
        if "not on PATH" in msg:
            return "Claude Code isn't installed"
        if "older than" in msg:
            return "Claude Code needs updating"
        return "Claude Code's version couldn't be checked"
    if name == "claude_login":
        if "not logged in" in msg:
            return "Claude Code isn't logged in"
        return "Claude Code's login couldn't be checked"
    if name == "accessibility":
        if "not granted Accessibility" in msg:
            return "I don't have Accessibility permission"
        return "Accessibility couldn't be checked"
    if name == "screen_recording":
        if "not been granted" in msg:
            return "I don't have Screen Recording permission"
        return "Screen Recording couldn't be checked"
    if name == "fish_api_key":
        return "I have no Fish Audio key"
    if name == "whatsapp":
        return "my WhatsApp line is only half set up"
    if name == "telegram":
        if "not paired" in msg:
            return "my Telegram line is waiting to be paired"
        return "my Telegram line is only half set up"
    if name == "chatgpt_fallback":
        if "did not finish" in msg:
            return "the ChatGPT fallback couldn't be checked in time"
        if "but JARVIS listens" in msg:
            return "on ChatGPT, my connected services would be left out"
        return "ChatGPT can't stand in for me when Claude's limit is reached"
    if name == "anthropic_key_leftover":
        return "there's a leftover Anthropic API key in the environment"
    if name == "claude_session_env":
        return "I was started from inside a Claude Code session and still carry its settings"
    if name == "cross_session_inbound":
        return "cross-session steering isn't enabled"
    if name == "memory_index":
        return "some of my memories are missing from my index"
    if name == "persona":
        return "my persona file has been edited, so upgrades to it aren't applied"
    if name == "private_files":
        return "my private files are readable by other accounts on this machine"
    if name == "mcp_launchers":
        # No server name: they come from the user's file, and this sentence
        # is spoken as JARVIS's own words.
        return "a connection is started through a package runner, so it may not be ready when I am"
    return check.message


def spoken_summary(checks: list["Check"]) -> str:
    """One or two spoken sentences naming what's wrong, or "" when all is well.

    Silence is the correct report for a healthy system: this deliberately
    never produces "all checks passed" -- only what needs attention, so the
    voice path has nothing to say when there is nothing to say.
    """
    issues = [c for c in checks if c.status != STATUS_OK]
    if not issues:
        return ""

    phrases = [_phrase_for(c) for c in issues]

    if len(phrases) == 1:
        return f"One thing needs attention, sir: {phrases[0]}."
    if len(phrases) == 2:
        return f"Two things need attention, sir: {phrases[0]}, and {phrases[1]}."
    return (
        f"{len(phrases)} things need attention, sir: "
        + ", ".join(phrases[:-1])
        + f", and {phrases[-1]}."
    )
