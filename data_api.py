"""Bounded local data-management endpoints; restore is explicitly offline."""
import asyncio
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import data_paths
import maintenance

router = APIRouter()
_lock = asyncio.Lock()


class Retention(BaseModel):
    days: int = Field(ge=1, le=36500)
    apply: bool = False


def _directory():
    directory = data_paths.data_dir() / "backups"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Backup directory cannot be a link")
    return directory


@router.post("/api/data/backup")
async def backup():
    async with _lock:
        target = _directory() / f"jarvis-{uuid.uuid4().hex}.zip"
        result = await asyncio.to_thread(maintenance.backup, target)
        return {**result, "download": f"/api/data/files/{target.name}"}


@router.post("/api/data/export")
async def export():
    async with _lock:
        target = _directory() / f"runs-{uuid.uuid4().hex}.jsonl"
        await asyncio.to_thread(maintenance.export_runs, target)
        return {"download": f"/api/data/files/{target.name}"}


@router.get("/api/data/files/{name}")
async def download(name: str):
    if Path(name).name != name or not name.startswith(("jarvis-", "runs-")) or Path(name).suffix not in (".zip", ".jsonl"):
        raise HTTPException(404, "Archive not found")
    path = _directory() / name
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Archive not found")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.post("/api/data/retention")
async def retention(body: Retention):
    async with _lock:
        archive = None
        if body.apply:
            archive = _directory() / f"jarvis-{uuid.uuid4().hex}.zip"
            await asyncio.to_thread(maintenance.backup, archive)
        report = await asyncio.to_thread(maintenance.prune, body.days, body.apply)
        # One policy, several stores (see maintenance.prune): the per-store
        # report, plus the totals the page has always read.
        stores = [v for v in report.values() if isinstance(v, dict)]
        result = {**report,
                  "eligible": sum(s.get("eligible", 0) for s in stores),
                  "deleted": sum(s.get("deleted", 0) for s in stores)}
        if archive:
            result["backup"] = f"/api/data/files/{archive.name}"
        return result
