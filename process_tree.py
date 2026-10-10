"""Owned subprocess trees: POSIX sessions and Windows kill-on-close jobs.

Windows children start suspended, are assigned to a job, then resumed. This
prevents a fast child from spawning outside our job before assignment.
No shell, task-name matching, or global process killing is used.
"""
from __future__ import annotations

import asyncio
import os
import signal
import weakref

_owners = weakref.WeakKeyDictionary()

# What `killpg` raises for a group with nothing left to signal. Linux says
# ESRCH; macOS says EPERM while the members are dead but not yet reaped,
# which is exactly the moment `release` runs after `kill`. The group is one
# this process made in a session of its own; a real EPERM would need every
# live member to be beyond our right to signal, and then there is nothing
# more this call could do anyway (guarded_mcp.py treats it the same way).
_GONE = (ProcessLookupError, PermissionError)

if os.name == "nt":
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)

    class Limits(c.Structure):
        _fields_ = [("per_process", c.c_int64), ("per_job", c.c_int64),
                    ("flags", w.DWORD), ("min_ws", c.c_size_t),
                    ("max_ws", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD),
                    ("scheduling", w.DWORD)]

    class ExtendedLimits(c.Structure):
        _fields_ = [("basic", Limits), ("io", c.c_uint64 * 6),
                    ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                    ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

    class ThreadEntry(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD),
                    ("tid", w.DWORD), ("pid", w.DWORD),
                    ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]

    def _api(name, result, *args):
        fn = getattr(kernel, name)
        fn.restype, fn.argtypes = result, args
        return fn

    _create = _api("CreateJobObjectW", w.HANDLE, c.c_void_p, w.LPCWSTR)
    _set = _api("SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    _assign = _api("AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
    _open = _api("OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    _close = _api("CloseHandle", w.BOOL, w.HANDLE)
    _terminate = _api("TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
    _snapshot = _api("CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
    _first = _api("Thread32First", w.BOOL, w.HANDLE, c.POINTER(ThreadEntry))
    _next = _api("Thread32Next", w.BOOL, w.HANDLE, c.POINTER(ThreadEntry))
    _open_thread = _api("OpenThread", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    _resume = _api("ResumeThread", w.DWORD, w.HANDLE)

    class Job:
        def __init__(self):
            self.handle = _create(None, None)
            if not self.handle:
                raise c.WinError(c.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _set(self.handle, 9, c.byref(limits), c.sizeof(limits)):
                error = c.WinError(c.get_last_error())
                self.close()
                raise error

        def attach_and_resume(self, pid):
            handle = _open(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise c.WinError(c.get_last_error())
            try:
                if not _assign(self.handle, handle):
                    raise c.WinError(c.get_last_error())
            finally:
                _close(handle)
            snapshot = _snapshot(4, 0)  # TH32CS_SNAPTHREAD
            if snapshot == c.c_void_p(-1).value:
                raise c.WinError(c.get_last_error())
            try:
                entry = ThreadEntry()
                entry.size = c.sizeof(entry)
                found = _first(snapshot, c.byref(entry))
                while found:
                    if entry.pid == pid:
                        thread = _open_thread(2, False, entry.tid)
                        if not thread:
                            raise c.WinError(c.get_last_error())
                        try:
                            if _resume(thread) == 0xFFFFFFFF:
                                raise c.WinError(c.get_last_error())
                        finally:
                            _close(thread)
                        return
                    found = _next(snapshot, c.byref(entry))
                raise OSError("Suspended child has no resumable thread")
            finally:
                _close(snapshot)

        def kill(self):
            if self.handle and not _terminate(self.handle, 1):
                raise c.WinError(c.get_last_error())

        def close(self):
            if self.handle:
                _close(self.handle)
                self.handle = None


async def spawn(*args, **kwargs):
    owner = Job() if os.name == "nt" else None
    if owner:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 4  # CREATE_SUSPENDED
    else:
        kwargs["start_new_session"] = True
    task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    proc = None
    try:
        proc = await asyncio.shield(task)
        # Unit tests fake the OS boundary; never operate on their invented pids.
        if isinstance(proc, asyncio.subprocess.Process):
            _owners[proc] = owner if owner else proc.pid
            if owner:
                owner.attach_and_resume(proc.pid)
        elif owner:
            owner.close()
        return proc
    except BaseException:
        if proc is None:
            try:
                proc = await task
            except BaseException:
                pass
        if owner:
            owner.close()
        if proc is not None:
            if proc.returncode is None:
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            finally:
                release(proc)
                _close_transport(proc)
        raise


def kill(proc, *, force=True):
    owner = _owners.get(proc)
    if owner is not None:
        if os.name == "nt":
            owner.kill()
        else:
            try:
                os.killpg(owner, signal.SIGKILL if force else signal.SIGTERM)
            except _GONE:
                pass
    elif proc.returncode is None:
        try:
            proc.kill() if force else proc.terminate()
        except ProcessLookupError:
            pass


def release(proc):
    """End ownership, also cleaning children left after the leader exited."""
    if proc is None:
        return
    owner = _owners.pop(proc, None)
    if owner is not None:
        if os.name == "nt":
            owner.close()
        else:
            try:
                os.killpg(owner, signal.SIGKILL)
            except _GONE:
                pass


async def stop(proc, grace=5):
    if proc is None:
        return
    kill(proc, force=False)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        kill(proc)
        await asyncio.wait_for(proc.wait(), grace)
    finally:
        release(proc)
        _close_transport(proc)


def _close_transport(proc):
    """Let go of a reaped child's pipes, whether or not it was seen to exit.

    Closed only once `returncode` was known, a reap that was cancelled or
    timed out left the transport for the garbage collector, which closes
    its pipes against a loop that has by then shut ("I/O operation on
    closed pipe", "Event loop is closed"). Every caller has already killed
    the child, and closing kills it too if somehow it still runs.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
