"""A child's transport is closed however its reap ends.

The transport owns the child's pipes. Closed only once the exit had been
seen, a reap that was cancelled or timed out left it for the garbage
collector, which closes those pipes against a loop that is by then shut:
"unclosed transport ... I/O operation on closed pipe" and "Event loop is
closed", raised in `__del__` and reported against whichever test happened
to be running. preflight's `_run_subprocess` was the first place this was
traced; `stop` and `spawn`'s own clean-up are the same shape.

The children here are fakes at the OS boundary, so every path is
deterministic: nothing is started and nothing ever exits unless told to.
"""

import asyncio

import pytest

import process_tree


class _Child:
    """A child nobody sees exit until `exits()` is called."""

    pid = 4242

    def __init__(self):
        self.returncode = None
        self.waiting = 0
        self.transport_closed = False
        self._exited = asyncio.Event()
        child = self

        class _Transport:
            def close(self):
                child.transport_closed = True

        self._transport = _Transport()

    def exits(self, code=-9):
        self.returncode = code
        self._exited.set()

    async def wait(self):
        self.waiting += 1
        await self._exited.wait()
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


async def _until(condition):
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never got there")


@pytest.mark.asyncio
async def test_stop_closes_the_transport_of_a_child_that_exits():
    child = _Child()
    task = asyncio.create_task(process_tree.stop(child, grace=60))
    await _until(lambda: child.waiting)
    child.exits()
    await task
    assert child.transport_closed


@pytest.mark.asyncio
async def test_stop_closes_the_transport_when_its_wait_is_cancelled():
    """The brain's `stop` awaits this; cancel that, and the child was left
    with its pipes open."""
    child = _Child()
    task = asyncio.create_task(process_tree.stop(child, grace=60))
    await _until(lambda: child.waiting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.transport_closed, "the transport was left for the garbage collector"


@pytest.mark.asyncio
async def test_stop_closes_the_transport_of_a_child_never_seen_to_die():
    """Asked, then told, and still not seen to exit within either grace:
    `stop` says so, and still lets go of the pipes."""
    child = _Child()
    with pytest.raises(asyncio.TimeoutError):
        await process_tree.stop(child, grace=0.01)
    assert child.waiting == 2
    assert child.transport_closed, "the transport was left for the garbage collector"


@pytest.mark.asyncio
async def test_a_cancelled_spawn_closes_the_transport_of_the_child_it_reaps(monkeypatch):
    """Cancelled while the child was still being created: `spawn` waits for
    it, kills it and reaps it, and must then close what it reaped rather
    than hand it to the garbage collector."""
    child = _Child()
    created = asyncio.Event()

    async def create_subprocess_exec(*args, **kwargs):
        await created.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    task = asyncio.create_task(process_tree.spawn("claude", "--version"))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    created.set()
    await _until(lambda: child.waiting)
    child.exits()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.transport_closed, "the reaped child's transport was never closed"


@pytest.mark.asyncio
async def test_a_cancelled_spawn_closes_the_transport_even_when_the_reap_is_cut_short(monkeypatch):
    child = _Child()

    async def create_subprocess_exec(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    task = asyncio.create_task(process_tree.spawn("claude", "--version"))
    await asyncio.sleep(0)
    task.cancel()
    await _until(lambda: child.waiting)  # killed, now reaping
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.transport_closed, "the transport was left for the garbage collector"


class _Posix:
    """The POSIX boundary, on any host: `killpg` answers as macOS does for a
    group whose members have all died but are not yet reaped."""

    name = "posix"

    def __init__(self):
        self.signalled = []

    def killpg(self, pgid, sig):
        self.signalled.append((pgid, sig))
        raise PermissionError(1, "Operation not permitted")


def test_a_group_left_only_with_zombies_is_released_not_raised(monkeypatch):
    """Seen on the macOS runner: the brain's stuck-turn restart killed the
    group, the leader died, and the second `killpg` in `release` — before
    asyncio had reaped it — got EPERM, not ESRCH. That PermissionError
    escaped the restart. A group JARVIS made in its own session has nobody
    in it it lacks the right to signal, so EPERM there means "nothing left"."""
    fake = _Posix()
    monkeypatch.setattr(process_tree, "os", fake)
    monkeypatch.setattr(process_tree, "signal", type("S", (), {"SIGKILL": 9, "SIGTERM": 15}))
    child = _Child()
    process_tree._owners[child] = child.pid
    process_tree.kill(child)
    process_tree.kill(child, force=False)
    process_tree.release(child)
    assert fake.signalled == [(4242, 9), (4242, 15), (4242, 9)]
    assert child not in process_tree._owners
