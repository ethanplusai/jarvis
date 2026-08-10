"""
JARVIS Obsidian Access — READ-ONLY access to an Obsidian vault.

A vault is a folder of Markdown files, so this reads the filesystem directly:
no AppleScript, no TCC prompts, no plugin or REST bridge to keep running.

IMPORTANT: This module is intentionally READ-ONLY.
No create, edit, move, or delete functions exist by design — the vault is the
user's own knowledge base, and a voice assistant mishearing a command should
never be able to alter it.

Set OBSIDIAN_VAULT to the vault's path to enable it. Unset, every function
returns empty and JARVIS simply has no vault.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("jarvis.obsidian")

# Folders that hold no prose worth searching: Obsidian's own config, plus the
# usual attachment and template directories.
_SKIP_DIRS = {".obsidian", ".git", ".trash", "_attachments", "node_modules"}

# A vault is small (hundreds of notes, well under a megabyte of text), so
# search walks it rather than maintaining an index that could go stale.
_MAX_NOTE_BYTES = 400_000


def _vault_root() -> Path | None:
    """The configured vault, or None when the feature is switched off."""
    raw = os.getenv("OBSIDIAN_VAULT", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_dir():
        log.warning(f"OBSIDIAN_VAULT is not a directory: {root}")
        return None
    return root.resolve()


def _iter_notes(root: Path):
    """Yield every Markdown file in the vault, skipping machinery folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_NOTE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning(f"Could not read {path.name}: {e}")
        return ""


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]


def _score(title: str, body: str, terms: list[str]) -> int:
    """Rank a note against the query terms.

    A term in the title counts for far more than one in the body: notes are
    named deliberately, so "memoria" in a filename is a stronger signal than
    the same word buried in a paragraph.
    """
    title_l, body_l = title.lower(), body.lower()
    score = 0
    for t in terms:
        score += 10 * title_l.count(t)
        score += min(body_l.count(t), 5)
    return score


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    """A readable line around the first hit — this gets spoken aloud."""
    for line in body.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if not stripped:
            continue
        low = stripped.lower()
        if any(t in low for t in terms):
            return stripped[:width]
    for line in body.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:width]
    return ""


def _search_sync(query: str, limit: int) -> list[dict]:
    root = _vault_root()
    if not root:
        return []
    terms = _terms(query)
    if not terms:
        return []

    hits = []
    for path in _iter_notes(root):
        body = _read(path)
        if not body:
            continue
        title = path.stem
        score = _score(title, body, terms)
        if score:
            hits.append({
                "title": title,
                "path": str(path.relative_to(root)),
                "score": score,
                "snippet": _snippet(body, terms),
            })
    hits.sort(key=lambda h: (-h["score"], h["title"].lower()))
    return hits[:limit]


async def search_vault(query: str, limit: int = 5) -> list[dict]:
    """Find vault notes matching a query, best first."""
    return await asyncio.to_thread(_search_sync, query, limit)


def _read_note_sync(query: str, max_chars: int) -> dict | None:
    root = _vault_root()
    if not root:
        return None
    hits = _search_sync(query, limit=1)
    if not hits:
        return None

    # Re-derive the path from the vault root and confirm it stayed inside.
    # The query reaches here from a speech transcript by way of the model, so
    # it is untrusted input, and a note name is not a licence to read the disk.
    target = (root / hits[0]["path"]).resolve()
    if not target.is_relative_to(root):
        log.warning(f"Refusing to read outside the vault: {target}")
        return None

    body = _read(target)
    return {
        "title": hits[0]["title"],
        "path": hits[0]["path"],
        "body": body[:max_chars],
        "truncated": len(body) > max_chars,
    }


async def read_vault_note(query: str, max_chars: int = 3000) -> dict | None:
    """Read the vault note that best matches a title or topic."""
    return await asyncio.to_thread(_read_note_sync, query, max_chars)


def _recent_sync(count: int) -> list[dict]:
    root = _vault_root()
    if not root:
        return []
    notes = []
    for path in _iter_notes(root):
        try:
            notes.append((path.stat().st_mtime, path))
        except OSError:
            continue
    notes.sort(reverse=True)
    return [
        {"title": p.stem, "path": str(p.relative_to(root))}
        for _, p in notes[:count]
    ]


async def recent_vault_notes(count: int = 5) -> list[dict]:
    """List the most recently edited notes."""
    return await asyncio.to_thread(_recent_sync, count)


def _stats_sync() -> dict:
    root = _vault_root()
    if not root:
        return {"configured": False, "notes": 0, "path": ""}
    return {
        "configured": True,
        "notes": sum(1 for _ in _iter_notes(root)),
        "path": str(root),
    }


async def vault_stats() -> dict:
    """Vault reachability, for the settings panel."""
    return await asyncio.to_thread(_stats_sync)


def format_search_for_voice(hits: list[dict], query: str) -> str:
    """Phrase search results as JARVIS would say them."""
    if not hits:
        return f"Nothing in your vault about {query}, sir."
    top = hits[0]
    line = f"Your note '{top['title']}' says: {top['snippet']}"
    if len(hits) > 1:
        others = ", ".join(h["title"] for h in hits[1:3])
        line += f" There's also {others}."
    return line
