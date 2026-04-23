"""
JARVIS Work Mode — persistent claude -p sessions tied to projects.

JARVIS can connect to any project directory and maintain a conversation
with Claude Code. Uses --continue to resume the most recent session
in that directory, so context persists across messages.

The user sees Claude Code working in their Terminal window.
JARVIS reads the responses via subprocess, summarizes, and reports back.
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("jarvis.work_mode")

SESSION_FILE = Path(__file__).parent / "data" / "active_session.json"


class WorkSession:
    """A claude -p session tied to a project directory.

    Each project gets its own session. JARVIS can switch between projects
    and --continue picks up where the last message left off.
    """

    def __init__(self):
        self._active = False
        self._working_dir: str | None = None
        self._project_name: str | None = None
        self._message_count = 0  # Track if this is first message (no --continue)
        self._status = "idle"  # idle, working, done

    @property
    def active(self) -> bool:
        return self._active

    @property
    def project_name(self) -> str | None:
        return self._project_name

    @property
    def status(self) -> str:
        return self._status

    async def start(self, working_dir: str, project_name: str = None):
        """Start or switch to a project session."""
        self._working_dir = working_dir
        self._project_name = project_name or working_dir.split("/")[-1]
        self._active = True
        self._message_count = 0
        self._status = "idle"
        self._save_session()
        log.info(f"Work mode started: {self._project_name} ({working_dir})")

    async def send(self, user_text: str, memory_context: str = "") -> str:
        """Send a message to claude -p and get the full response.

        First message in a session: fresh claude -p
        Subsequent messages: claude -p --continue (resumes last session in dir)

        Args:
            user_text: The prompt/instruction to send.
            memory_context: Optional memory/context preamble injected before the prompt.
        """
        claude_path = shutil.which("claude")
        if not claude_path:
            return "Claude CLI not found on this system."

        cmd = [
            claude_path, "-p",
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]

        # Use --continue for subsequent messages to maintain context
        if self._message_count > 0:
            cmd.append("--continue")

        self._status = "working"

        # Prepend memory context if provided, separated clearly so Claude Code
        # treats it as background knowledge, not an instruction to repeat.
        if memory_context:
            full_prompt = (
                "<!-- JARVIS CONTEXT: background knowledge for this session -->\n"
                f"{memory_context}\n"
                "<!-- END JARVIS CONTEXT -->\n\n"
                f"{user_text}"
            )
        else:
            full_prompt = user_text

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._working_dir,
            )

            # Increased from 300s: complex builds (npm install + webpack + tests)
            # routinely exceed 5 minutes. 15 minutes covers the vast majority.
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=full_prompt.encode("utf-8", errors="replace")),
                timeout=900,
            )

            response = stdout.decode("utf-8", errors="replace").strip()
            self._message_count += 1
            self._save_session()
            self._status = "done"

            if process.returncode != 0:
                error = stderr.decode("utf-8", errors="replace").strip()[:200]
                log.error(f"claude -p error (rc={process.returncode}): {error}")
                self._status = "error"
                return f"Hit a problem, sir: {error}"

            log.info(f"Claude Code response for {self._project_name} ({len(response)} chars)")
            return response

        except asyncio.TimeoutError:
            log.error("claude -p timed out after 900s")
            self._status = "timeout"
            return "That's taking longer than expected, sir. The operation timed out."
        except Exception as e:
            log.error(f"Work mode error: {e}", exc_info=True)
            self._status = "error"
            return f"Something went wrong, sir: {str(e)[:100]}"

    async def stop(self):
        """End the work session."""
        project = self._project_name
        self._active = False
        self._working_dir = None
        self._project_name = None
        self._message_count = 0
        self._status = "idle"
        self._clear_session()
        log.info(f"Work mode ended for {project}")

    def _save_session(self):
        """Persist session state so it survives restarts."""
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps({
                "project_name": self._project_name,
                "working_dir": self._working_dir,
                "message_count": self._message_count,
            }))
        except Exception as e:
            log.debug(f"Failed to save session: {e}")

    def _clear_session(self):
        """Remove persisted session."""
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def restore(self) -> bool:
        """Restore session from disk after restart. Returns True if restored."""
        try:
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text())
                self._working_dir = data["working_dir"]
                self._project_name = data["project_name"]
                self._message_count = data.get("message_count", 1)  # Assume at least 1 so --continue is used
                self._active = True
                self._status = "idle"
                log.info(f"Restored work session: {self._project_name} ({self._working_dir})")
                return True
        except Exception as e:
            log.debug(f"No session to restore: {e}")
        return False


def is_casual_question(text: str) -> bool:
    """Detect if a message is casual chat vs work-related.

    Casual questions go to Haiku (fast). Work goes to claude -p (powerful).
    """
    t = text.lower().strip()

    casual_patterns = [
        "what time", "what's the time", "what day",
        "what's the weather", "weather",
        "how are you", "are you there", "hey jarvis",
        "good morning", "good evening", "good night",
        "thank you", "thanks", "never mind", "nevermind",
        "stop", "cancel", "quit work mode", "exit work mode",
        "go back to chat", "regular mode",
        "how's it going", "what's up",
        "are you still there", "you there", "jarvis",
        "are you doing it", "is it working", "what happened",
        "did you hear me", "hello", "hey",
        "how's that coming", "hows that coming",
        "any update", "status update",
    ]

    # Short greetings/acknowledgments
    if len(t.split()) <= 3 and any(w in t for w in ["ok", "okay", "sure", "yes", "no", "yeah", "nah", "cool"]):
        return True

    return any(p in t for p in casual_patterns)
