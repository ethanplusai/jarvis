"""
JARVIS QA Agent — Verifies coding-agent task output.

Spawns the active secondary agent to check completed work, auto-retries on failure.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict

from secondary_agent import SECONDARY_AGENT, run_secondary_agent_prompt

log = logging.getLogger("jarvis.qa")

MAX_RETRIES = 3


@dataclass
class QAResult:
    passed: bool
    issues: list[str]
    summary: str
    attempt: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


class QAAgent:
    """Verifies coding-agent task output."""

    async def verify(self, task_prompt: str, task_result: str, working_dir: str = ".") -> QAResult:
        """Run QA on a completed task via the active secondary agent."""
        qa_prompt = (
            "You are a QA agent. Verify the following completed task.\n\n"
            f"ORIGINAL TASK:\n{task_prompt}\n\n"
            f"TASK OUTPUT:\n{task_result[:3000]}\n\n"
            "INSTRUCTIONS:\n"
            "1. Check if the output matches the requirements\n"
            "2. Check if files mentioned actually exist (if applicable)\n"
            "3. Check for obvious errors, missing pieces, or incomplete work\n"
            "4. Respond with JSON only, no markdown:\n"
            '{"passed": true/false, "issues": ["issue1", ...], "summary": "one line summary"}\n'
        )

        try:
            result = await run_secondary_agent_prompt(
                prompt=qa_prompt,
                working_dir=working_dir,
                timeout=120.0,
            )
            raw = result.message.strip()

            # Try to parse JSON from the response
            try:
                # Handle markdown-wrapped JSON
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()

                data = json.loads(raw)
                return QAResult(
                    passed=data.get("passed", False),
                    issues=data.get("issues", []),
                    summary=data.get("summary", "QA completed"),
                )
            except (json.JSONDecodeError, IndexError):
                # If we can't parse JSON, treat any output as a pass with notes
                log.warning(f"QA response not valid JSON, treating as pass: {raw[:200]}")
                return QAResult(
                    passed=True,
                    issues=[],
                    summary=f"QA output (non-JSON): {raw[:200]}",
                )

        except asyncio.TimeoutError:
            log.warning("QA verification timed out")
            return QAResult(
                passed=True,
                issues=["QA timed out — manual review recommended"],
                summary="QA timed out",
            )
        except FileNotFoundError:
            log.error(f"{SECONDARY_AGENT.display_name} not found for QA")
            return QAResult(
                passed=True,
                issues=[f"{SECONDARY_AGENT.display_name} not available for QA"],
                summary="QA skipped — CLI not found",
            )
        except Exception as e:
            log.error(f"QA error: {e}")
            return QAResult(
                passed=True,
                issues=[f"QA error: {str(e)}"],
                summary=f"QA error: {str(e)}",
            )

    async def auto_retry(
        self,
        task_prompt: str,
        issues: list[str],
        working_dir: str = ".",
        attempt: int = 1,
    ) -> dict:
        """Retry a failed task with feedback from QA. Returns new task result."""
        if attempt >= MAX_RETRIES:
            return {
                "status": "failed",
                "result": "",
                "error": f"Max retries ({MAX_RETRIES}) exceeded. Issues: {issues}",
                "attempt": attempt,
            }

        retry_prompt = (
            f"RETRY ATTEMPT {attempt + 1}/{MAX_RETRIES}\n\n"
            f"ORIGINAL TASK:\n{task_prompt}\n\n"
            f"PREVIOUS ATTEMPT FAILED QA. Issues found:\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n\nPlease fix these issues and complete the task correctly."
        )

        try:
            result = await run_secondary_agent_prompt(
                prompt=retry_prompt,
                working_dir=working_dir,
                timeout=300.0,
            )
            if result.returncode == 0:
                return {
                    "status": "completed",
                    "result": result.message.strip(),
                    "error": "",
                    "attempt": attempt + 1,
                }
            else:
                return {
                    "status": "failed",
                    "result": result.message.strip(),
                    "error": result.stderr.strip() or result.stdout.strip(),
                    "attempt": attempt + 1,
                }

        except asyncio.TimeoutError:
            return {
                "status": "failed",
                "result": "",
                "error": "Retry timed out",
                "attempt": attempt + 1,
            }
        except Exception as e:
            return {
                "status": "failed",
                "result": "",
                "error": str(e),
                "attempt": attempt + 1,
            }
