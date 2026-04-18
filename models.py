"""
Shared data models for JARVIS.

Dataclasses and Pydantic models used across the server. No logic, no deps
beyond stdlib + pydantic.
"""

from dataclasses import asdict, dataclass
from datetime import datetime

from pydantic import BaseModel


@dataclass
class ClaudeTask:
    """A background claude -p subprocess task."""

    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: int | None = None
    result: str = ""
    error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    experiment_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."
