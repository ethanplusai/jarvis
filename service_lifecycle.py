"""Ordered service shutdown with injected owners; no server globals or REST."""
import asyncio


async def shutdown(executor, stop_watcher, background, stop_voice, release_runtime):
    """Close work admission and reap runs before releasing runtime ownership.

If an owner cannot shut down, propagate that failure and retain the runtime
lock. It must never look safe to restore data while owned work is still live.
The server serializes calls and records completion only after this returns.
"""
    await executor.shutdown()
    await stop_watcher()
    pending = [task for task in background
               if task is not asyncio.current_task() and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await stop_voice()
    release_runtime()
