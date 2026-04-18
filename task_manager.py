"""
ClaudeTaskManager — manages background claude -p subprocesses.

Dependencies (qa_agent, success_tracker, suggest_followup) are injected
via constructor or set via module-level `configure()` after import.
Keeps server.py free of this class's internals.
"""

import asyncio
import contextlib
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from models import ClaudeTask
from prompts import BUILD_DOCS_REQUIREMENT
from sanitize import DANGEROUS_FLAG, escape_shell_in_applescript

log = logging.getLogger("jarvis.task_manager")


class ClaudeTaskManager:
    """Manages background claude -p subprocesses.

    Optional injected dependencies (only needed if _run_qa is called):
    - qa_agent: QAAgent with .verify() and .auto_retry()
    - success_tracker: SuccessTracker with .log_task() and .log_suggestion()
    - suggest_followup: callable returning a suggestion or None
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        qa_agent: Any | None = None,
        success_tracker: Any | None = None,
        suggest_followup: Any | None = None,
    ):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications
        self._qa_agent = qa_agent
        self._success_tracker = success_tracker
        self._suggest_followup = suggest_followup

    def register_websocket(self, ws: WebSocket) -> None:
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket) -> None:
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict) -> None:
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify(
            {
                "type": "task_spawned",
                "task_id": task_id,
                "prompt": prompt,
            }
        )

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re

        words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask) -> None:
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt + BUILD_DOCS_REQUIREMENT)

        applescript = f"""
        tell application "Terminal"
            activate
            set newTab to do script "cd {escape_shell_in_applescript(work_dir)} && cat .jarvis_prompt.md | claude -p{DANGEROUS_FLAG} | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        """

        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        await self._notify(
            {
                "type": "task_complete",
                "task_id": task.id,
                "status": task.status,
                "summary": task.result[:200] if task.result else task.error,
            }
        )

        with contextlib.suppress(Exception):
            prompt_file.unlink()

        if task.status == "completed" and self._qa_agent is not None:
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1) -> None:
        """Run QA verification on a completed task, auto-retry on failure."""
        if self._qa_agent is None or self._success_tracker is None:
            return  # QA not configured
        try:
            qa_result = await self._qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                self._success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify(
                    {
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": True,
                        "summary": qa_result.summary,
                    }
                )

                if self._suggest_followup is not None:
                    suggestion = self._suggest_followup(
                        task_type="dev",
                        task_description=task.prompt,
                        working_dir=task.working_dir,
                        qa_result=qa_result,
                    )
                    if suggestion:
                        self._success_tracker.log_suggestion(task.id, suggestion.text)
                        await self._notify(
                            {
                                "type": "suggestion",
                                "task_id": task.id,
                                "text": suggestion.text,
                                "action_type": suggestion.action_type,
                                "action_details": suggestion.action_details,
                            }
                        )
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await self._qa_agent.auto_retry(
                        task.prompt,
                        qa_result.issues,
                        task.working_dir,
                        attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        await self._run_qa(task, attempt + 1)
                    else:
                        self._success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify(
                            {
                                "type": "qa_result",
                                "task_id": task.id,
                                "passed": False,
                                "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                            }
                        )
                else:
                    self._success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify(
                        {
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                        }
                    )
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> ClaudeTask | None:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t
            for t in self._tasks.values()
            if t.status == "completed" and t.completed_at and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)
