"""The dashboard's window onto the brain's memory folder. Carved out of
server.py: the snapshot, one document, and the one write (reindex)."""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import data_paths
import jarvis_memory

router = APIRouter()


# ---------------------------------------------------------------------------
# Memory — the plain-Markdown folder, read-only over HTTP
#
# The dashboard's Memory view is a window onto a folder the user edits by hand;
# nothing here writes, and nothing here creates the folder. A GET that brought
# `jarvis/` into being would be a side effect the caller never asked for, so a
# brain that has never remembered anything reports empty lists instead.
# ---------------------------------------------------------------------------

MEMORY_DOC_KINDS = ("memory", "project", "journal")


@router.get("/api/memory")
async def api_memory():
    """Everything the Memory view lists, in one call.

    Always 200. An absent folder is an empty memory, not a missing route —
    404 here would be indistinguishable from "this endpoint isn't wired",
    which is exactly what the dashboard shows when it sees one.
    """
    return {
        "path": str(data_paths.brain_home()),
        "index": jarvis_memory.index_entries(),
        "memories": jarvis_memory.memory_entries(),
        "projects": jarvis_memory.project_entries(),
        "journal": jarvis_memory.journal_entries_meta(),
        "latest_journal_slug": jarvis_memory.latest_journal_slug(),
        # Notes the index does not name. The brain cannot see these at boot;
        # the page says so and offers the one repair (below).
        "unindexed": jarvis_memory.unindexed_memories(),
    }


@router.post("/api/memory/reindex")
async def api_memory_reindex():
    """Give every note the index does not name a line in MEMORY.md.

    The one write on this surface, and it is the user's to ask for: a note
    without a line may be one he deliberately let go of while keeping the
    file, so nothing does this on its own. In-process rather than
    `maintenance.py reindex` so it needs no shutdown — `add_to_index` is the
    same read-modify-write `remember` does, on the same thread.
    """
    return await asyncio.to_thread(jarvis_memory.reindex)


@router.get("/api/memory/{kind}/{slug}")
async def api_memory_doc(kind: str, slug: str):
    """One file, raw. `slug` comes from a URL and is never trusted: every
    containment decision is made by `jarvis_memory.doc_path`, which resolves
    both the folder and the candidate and refuses anything that lands
    outside. A rejected slug is reported as 404 like any other miss — telling
    an attacker which of their probes were traversal attempts buys them
    information and buys us nothing."""
    path = jarvis_memory.doc_path(kind, slug) if kind in MEMORY_DOC_KINDS else None
    if path is None:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"slug": slug, "text": text}
