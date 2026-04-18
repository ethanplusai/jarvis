"""
Project discovery — scan common directories for git repos so JARVIS
knows which projects the user is working on.
"""

import contextlib
import logging
from pathlib import Path

log = logging.getLogger("jarvis.projects")

_DESKTOP = Path.home() / "Desktop"
_SEARCH_DIRS = [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "IdeaProjects",
    Path.home() / "Projects",
]


async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects: list[dict] = []
    if not _DESKTOP.exists():
        return projects

    try:
        for entry in sorted(_DESKTOP.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                with contextlib.suppress(Exception):
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")

                projects.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "branch": branch,
                    }
                )
    except (PermissionError, FileNotFoundError):
        pass

    return projects


def scan_projects_sync() -> list[dict]:
    """Scan common project directories — runs in executor.

    Returns all subdirectories, not just git repos. Used by the WebSocket
    handler which lists every directory the user might reference.
    """
    projects: list[dict] = []
    for search_dir in _SEARCH_DIRS:
        try:
            for entry in search_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    projects.append({"name": entry.name, "path": str(entry), "branch": ""})
        except PermissionError:
            continue
        except Exception as e:
            log.debug(f"Project scan error in {search_dir}: {e}")
            continue
    return projects
