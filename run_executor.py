"""Spawns Claude Code runs and records everything they do.

Invariant: every state transition is written to the store *before* it is
published to subscribers. The WebSocket is a cache-invalidation hint, never
a source of truth.

Second invariant: a run ALWAYS reaches a terminal state. Every exit path out
of `_drive` — success, non-zero exit, timeout, spawn failure, cancellation,
or an unexpected exception anywhere in the pipeline — writes a terminal
status. A run stuck in `running` or `queued` is the failure this module
exists to eliminate.
"""

import asyncio
import logging
import math
import os
import shutil
import time
from typing import Callable

import claude_env
import stream_parser

log = logging.getLogger("jarvis.run_executor")

# Same default and env var as work_mode.py and server.py.
_SKIP_PERMISSIONS = os.getenv("JARVIS_SKIP_PERMISSIONS", "true").lower() \
    not in ("0", "false", "no")

# Same precedent as brain.py's BrainConfig.from_env: never rely on the CLI's
# own default, always pass --model explicitly.
_DEFAULT_MODEL = "sonnet"

# Events are written in batches so a chatty build does not put thousands of
# synchronous SQLite writes on the event loop.
_EVENT_BATCH = 25
_EVENT_FLUSH_SEC = 0.5

# stderr is drained concurrently with stdout — a child that fills the stderr
# pipe buffer blocks forever otherwise — keeping only the tail.
_STDERR_CHARS = 2000
_STDERR_BYTES = _STDERR_CHARS * 4
_STDERR_DRAIN_SEC = 5.0

# How long the child may take to EXIT after it has closed stdout.
#
# EOF on stdout means the CLI has nothing left to say; all that remains is
# process teardown — flushing its session transcript, reaping its own MCP
# children. Thirty seconds is far more than any of that has ever needed and
# still bounds the damage: a child that closes stdout and then parks frees
# its concurrency permit within half a minute instead of holding it for the
# life of the server.
#
# This bound only ever applies to a child that reaches EOF, which is rarer
# than it sounds. See _IDLE_OUTPUT_SEC.
_EOF_EXIT_GRACE_SEC = 30.0

# How long the child may say NOTHING while still holding stdout open.
#
# The EOF grace above is the wrong bound for almost every way a run goes
# quiet, because almost none of them produce EOF. Measured at the asyncio
# level, one child shape per row:
#
#   os.close(1)                      -> EOF        the grace above fires
#   sys.stdout.close()               -> no EOF     nothing fires
#   stops writing, fd 1 still open   -> no EOF     nothing fires
#   exits, grandchild holds fd 1     -> no EOF     nothing fires
#   SIGSTOPs itself                  -> no EOF     nothing fires
#
# `sys.stdout.close()` is the surprising one and it is worth spelling out:
# CPython builds the raw FileIO behind `sys.stdout` with `closefd=False`
# (pylifecycle's create_stdio), so that closing the Python object cannot
# take fd 1 away from the C runtime. Closing it therefore tears down the
# buffered wrapper and leaves the pipe's write end wide open. It is not the
# `os.close(1)` case at all — it is the "stopped talking" case, and so are
# the other three. EOF never arrives, `_consume` never returns, and `_drive`
# parks in `wait_for(reader, timeout=timeout_sec)` with only the six-hour
# wall clock left. The run stays `running` and holds one of three permits.
#
# Thirty minutes, and it is chosen against the longest silence a *working*
# run can legitimately produce, not against how long a wedged one is
# tolerable. The CLI emits a stream-json event per turn and per tool result,
# so the gaps are: one Bash tool call, which the CLI allows up to ten
# minutes; one thinking turn; and the CLI's own retry backoff on a 429 or a
# 529. Ten minutes is the biggest of those by a wide margin, so thirty gives
# roughly threefold headroom over the worst legitimate case — while still
# being a twelfth of the wall-clock bound, so a run that keeps talking is
# never cut short by it and a wedged one gives its permit back in half an
# hour rather than six.
#
# Override with JARVIS_RUN_IDLE_SEC. As with the wall clock there is
# deliberately no way to ask for "no bound".
_IDLE_OUTPUT_SEC = 30 * 60

# How often the read loop wakes up to ask "has anything happened".
#
# Only reached when the child is silent — a line that is ready is returned
# at once — so this is the resolution of the idle bound and of the
# `returncode` check, not a poll of the pipe. One second costs nothing
# against a thirty-minute bound and reaps an exited-but-pipe-held child in
# about two.
_READ_POLL_SEC = 1.0

# The wall-clock bound every run gets when its caller names none.
#
# No production caller passes a timeout: `spawn_run` and `start_build` both
# omit it and `RunRequest.timeout_sec` defaults to 0, which used to mean
# "unbounded". For an unattended process holding one of three permits that is
# not a policy, it is the absence of one. Six hours is chosen to be longer
# than any real `start_build` (which is explicitly meant to run for hours and
# is the longest thing this pipeline starts) and shorter than a working day,
# so a wedged run is reclaimed the same day it is started rather than
# surviving until the next server restart. Override with
# JARVIS_RUN_TIMEOUT_SEC; a caller that names its own `timeout_sec` still
# wins. There is deliberately no way to ask for "no timeout".
_DEFAULT_TIMEOUT_SEC = 6 * 3600

# The ceiling on every bound below, whoever asks and however they ask.
#
# `> 0` is not "is a wall clock". `float("inf") > 0` is True, and so is
# `1e30 > 0` — so `JARVIS_RUN_TIMEOUT_SEC=inf` was accepted and the wall
# clock these functions exist to keep was simply gone, in direct
# contradiction of the paragraph above that says there is deliberately no way
# to ask for one. A day is the ceiling for the same reason six hours is the
# default: longer than any real `start_build`, and short enough that a wedged
# run is reclaimed before the next one starts.
MAX_BOUND_SEC = 24 * 3600


def _wall_clock(value: float) -> float | None:
    """`value` as a bound that a run can actually hit, or None if it is not
    one — zero, negative, NaN, or a number that is a wall clock in name
    only. Capped rather than refused at the top end: an operator who asked
    for a week meant "a long time", and a day is a long time.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return min(value, float(MAX_BOUND_SEC))


def _resolve_bound(explicit: float | None, env_var: str, default: float,
                   what: str) -> float:
    """Explicit argument, else the environment, else the module default —
    and never anything that is not a wall clock. See `_wall_clock`.

    One function rather than two so that a third bound added later cannot
    quietly get the old `> 0` test. tests/test_bounds.py finds every
    `_resolve_*` in this module from the AST and holds it to this.
    """
    bounded = _wall_clock(explicit) if explicit is not None else None
    if bounded is not None:
        return bounded
    raw = os.getenv(env_var)
    if raw:
        bounded = _wall_clock(raw)
        if bounded is not None:
            return bounded
        log.warning("%s=%r is not a length of time %s can be bounded by; "
                    "using %ss", env_var, raw, what, default)
    return float(default)


def _resolve_default_timeout(explicit: float | None) -> float:
    """Explicit argument, else JARVIS_RUN_TIMEOUT_SEC, else six hours.

    Never returns 0, a negative, or anything that is not finite and inside
    MAX_BOUND_SEC: an unbounded unattended process is the defect this exists
    to close, so a nonsensical override falls back to the module default
    rather than disabling the bound.
    """
    return _resolve_bound(explicit, "JARVIS_RUN_TIMEOUT_SEC",
                          _DEFAULT_TIMEOUT_SEC, "a run")


def _resolve_idle(explicit: float | None) -> float:
    """Explicit argument, else JARVIS_RUN_IDLE_SEC, else thirty minutes.

    Same rule as _resolve_default_timeout, for the same reason: a bound that
    can be set to zero — or to infinity — is a bound an operator can
    accidentally remove, and the thing it is protecting is a permit the whole
    pipeline shares.
    """
    return _resolve_bound(explicit, "JARVIS_RUN_IDLE_SEC",
                          _IDLE_OUTPUT_SEC, "a silent run")


class _IdleTimeout(Exception):
    """The child held stdout open and said nothing for too long.

    Raised out of `_consume` rather than returned, so that it cannot be
    mistaken for the ordinary EOF return by any of the code between here and
    `_drive`'s handler.
    """


class RunExecutor:
    def __init__(self, store, claude_path: str | None = None,
                 max_concurrent: int = 3, grace_sec: float = 10.0,
                 eof_grace_sec: float = _EOF_EXIT_GRACE_SEC,
                 default_timeout_sec: float | None = None,
                 idle_sec: float | None = None,
                 poll_sec: float = _READ_POLL_SEC):
        self._store = store
        self._claude_path = claude_path or shutil.which("claude") or "claude"
        self._max_concurrent = max_concurrent
        self._grace_sec = grace_sec
        self._eof_grace_sec = eof_grace_sec
        self._idle_sec = _resolve_idle(idle_sec)
        self._poll_sec = poll_sec if poll_sec and poll_sec > 0 \
            else _READ_POLL_SEC
        self._default_timeout = _resolve_default_timeout(default_timeout_sec)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        # Concurrency gate. A counting semaphore, not a poll over
        # active_count(): a burst of tasks that all reached the gate in the
        # same loop turn each counted the others as "active" and slept forever.
        self._slots = asyncio.Semaphore(max_concurrent)
        # Run ids whose cancellation has been *requested*. Recorded before the
        # signal is sent, so whichever coroutine reaches the terminal write
        # first still records CANCELLED and not "failed, exit -15".
        self._cancelling: set[str] = set()

    # -- pub/sub ----------------------------------------------------------

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        if cb not in self._subscribers:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        if cb in self._subscribers:
            self._subscribers.remove(cb)

    def _publish(self, message: dict) -> None:
        """Never let a bad subscriber take down a run."""
        for cb in list(self._subscribers):
            try:
                cb(message)
            except Exception:
                log.warning("run subscriber raised; continuing", exc_info=True)

    # -- lifecycle --------------------------------------------------------

    def active_count(self) -> int:
        """How many driver tasks are still in flight.

        Informational only — concurrency is gated by `self._slots`. This
        counts queued tasks too, which is exactly why it must not gate.
        """
        return len([t for t in self._tasks.values() if not t.done()])

    def _resolve_model(self, model: str | None) -> str:
        """Explicit argument, else JARVIS_RUN_MODEL, else 'sonnet'.

        Never returns falsy: the CLI's own default must never be relied on,
        so a --model flag is always emitted (brain.py sets this precedent
        for the voice path already).
        """
        return model or os.getenv("JARVIS_RUN_MODEL") or _DEFAULT_MODEL

    async def start_existing(self, run_id: str, prompt: str,
                             project_path: str,
                             resume_from: str | None = None,
                             timeout_sec: float = 0,
                             model: str | None = None) -> str:
        """Drive a run row that was created by the caller.

        This is the body previously inlined in spawn(); the done-callback
        cleanup moves here unchanged.
        """
        # Everything between the row existing and its driver owning it is
        # guarded. `update_run` is a synchronous SQLite write and SQLite
        # writes raise — "database is locked", a full disk — and when this
        # one did, the row sat QUEUED with no driver behind it and no path to
        # a terminal state. That is the exact case CLAUDE.md warns callers
        # about, with this module as the offender.
        coro = None
        try:
            resolved_model = self._resolve_model(model)
            # A caller that names no bound (every production caller does not)
            # gets the default one. See _resolve_default_timeout.
            bound = timeout_sec if timeout_sec and timeout_sec > 0 \
                else self._default_timeout
            # Persisted immediately — before the process even spawns — so a
            # still-queued run already shows what it will run on.
            await asyncio.to_thread(self._store.update_run, run_id,
                                    requested_model=resolved_model)
            coro = self._drive(run_id, prompt, project_path, resume_from,
                               bound, resolved_model)
            task = asyncio.create_task(coro)
            coro = None                     # the task owns it now
        except BaseException as e:
            if coro is not None:
                coro.close()                # never leave it un-awaited
            self._fail_undriven(run_id, e)
            raise
        self._tasks[run_id] = task
        # Drop the reference once finished: this dict would otherwise grow for
        # the life of the server.
        # wait_for() falls back to the store when the task is already gone.
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))
        return run_id

    def _fail_undriven(self, run_id: str, exc: BaseException) -> None:
        """Mark a run terminal when nothing will ever drive it.

        Best effort by necessity: the store is usually the very thing that
        just failed. It must never raise over the original exception — the
        caller has to see what actually went wrong.
        """
        log.exception("run %s could not be handed to a driver", run_id)
        try:
            self._finish_blocking(run_id, self._store.RunStatus.FAILED,
                                  error=repr(exc)[:_STDERR_CHARS])
        except Exception:
            log.exception("run %s could not be marked terminal either", run_id)

    async def spawn(self, prompt: str, project_name: str, project_path: str,
                    origin: str, resume_from: str | None = None,
                    timeout_sec: float = 0, model: str | None = None) -> str:
        run_id = await asyncio.to_thread(
            self._store.create_run, prompt, project_name, project_path,
            origin, resume_from)
        return await self.start_existing(run_id, prompt, project_path,
                                         resume_from, timeout_sec, model)

    async def cancel(self, run_id: str) -> bool:
        """Cancel a run, queued or running.

        False only for an unknown run id or one already in a terminal state.

        True is a promise, not a status write: it means the child was either
        signalled and confirmed dead, or confirmed never to have been started.
        It is never returned for a process that is still alive — that was the
        bug in the post-EOF window, where `_procs` had already been emptied
        and this method cheerfully reported a kill it had not performed.

        The one exception is a child that SIGKILL did not visibly reap
        within `_terminate`'s second grace period. That is logged at ERROR
        by `_terminate` and reported as cancelled anyway; see its docstring
        for why waiting for ever is the worse of the two.
        """
        run = await asyncio.to_thread(self._store.get_run, run_id)
        if run is None or run["status"] in self._store.RunStatus.TERMINAL:
            return False

        # Record the intent BEFORE signalling. `_drive` resumes on stdout EOF
        # and can reach `_finish` before this coroutine does; it consults this
        # set, so the first writer still writes CANCELLED.
        self._cancelling.add(run_id)

        proc = self._procs.get(run_id)
        if proc is not None:
            if proc.returncode is None:
                await self._terminate(proc)
            # Either way the child is now reaped: `_terminate` does not return
            # until `proc.wait()` has (or has demonstrably stopped being able
            # to), and a non-None returncode already means it is gone.
            await self._finish(run_id, self._store.RunStatus.CANCELLED,
                               exit_code=proc.returncode)
            return True

        # No process at all: still queued behind the concurrency gate, or the
        # driver has already reaped and unregistered it. Mark it terminal now
        # and make sure a pending task never spawns anything.
        try:
            await self._finish(run_id, self._store.RunStatus.CANCELLED)
            task = self._tasks.get(run_id)
            if (task is not None and not task.done()
                    and run["status"] == self._store.RunStatus.QUEUED):
                task.cancel()
        finally:
            if self._tasks.get(run_id) is None:
                # No driver left to consult the intent.
                self._cancelling.discard(run_id)
        return True

    async def _terminate(self, proc) -> None:
        """SIGTERM, then SIGKILL after the grace period.

        Returns once the child has been reaped, or once a second grace period
        has passed without it being reaped — whichever comes first.

        That second bound is new and it is a real trade. The wait after
        SIGKILL used to have no ceiling at all, which is fine as long as
        `proc.wait()` is guaranteed to resolve, and it is not: it resolves
        when asyncio's child watcher sees the process, so anything that
        reaps the pid first (a stray `os.waitpid`, a watcher torn down
        mid-flight) leaves that future pending for good. Unbounded, that
        parks `cancel()` and `_drive` forever and the permit never comes
        back — the exact failure this module exists to prevent, reached by a
        different road. Bounded, the caller may in principle be told a child
        is gone that is not; that is logged at ERROR, and it is strictly
        better than wedging the pipeline over it.
        """
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()),
                                   timeout=self._grace_sec)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()),
                                   timeout=self._grace_sec)
        except asyncio.TimeoutError:
            log.error("run child %s was not reaped within %ss of SIGKILL; "
                      "giving up the wait rather than holding its permit",
                      getattr(proc, "pid", "?"), self._grace_sec)

    async def wait_for(self, run_id: str, timeout: float = 30) -> dict:
        task = self._tasks.get(run_id)
        if task is not None:
            # shield(): a caller's timeout must never cancel the run itself.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError:
                # The *run* was cancelled (queued-cancel path) — report its
                # recorded terminal state. If instead *we* were cancelled,
                # propagate.
                if not task.cancelled():
                    raise
        return await asyncio.to_thread(self._store.get_run, run_id)

    # -- internals --------------------------------------------------------

    def _command(self, run_id: str, resume_from: str | None,
                model: str | None = None) -> list[str]:
        base = claude_env.split_command(self._claude_path)
        cmd = base + ["-p", "--output-format", "stream-json", "--verbose",
                      "--session-id", run_id]
        if resume_from:
            # Verified against CLI 2.1.251: --session-id is rejected alongside
            # --resume unless --fork-session is also passed. Forking is also the
            # semantics we want — the retry inherits context but owns its own id.
            cmd += ["--resume", resume_from, "--fork-session"]
        # Always explicit, never the CLI's own default — same reasoning as
        # brain.py always passing --model.
        cmd += ["--model", self._resolve_model(model)]
        if _SKIP_PERMISSIONS:
            # Matches the five existing call sites. Without this a run blocks on
            # a permission prompt it has no TTY to answer, and hangs forever.
            cmd.append("--dangerously-skip-permissions")
        return cmd

    async def _publish_run_updated(self, run_id: str) -> None:
        """Announce a field change on a still-running run.

        Only for changes that alter the row a client renders — the model from
        `system/init`, the accumulated token usage from each `assistant`
        turn, and cost/usage from `result`. NOT per streamed event: that
        would be one message per line of output (three quarters of them hook
        plumbing), and `run_event` already covers the stream. Like every
        other transition, the store write has already happened; this is the
        cache-invalidation hint after it, and carries the full row so the
        client never has to merge a delta.
        """
        run = await asyncio.to_thread(self._store.get_run, run_id)
        if run is not None:
            self._publish({"type": "run_updated", "run": run})

    def _start_write(self, run_id: str, pid: int) -> dict | None:
        """The RUNNING transition, as one unit on a worker thread."""
        self._store.update_run(run_id,
                               status=self._store.RunStatus.RUNNING,
                               pid=pid, started_at=time.time())
        return self._store.get_run(run_id)

    def _finish_write(self, run_id: str, status: str, **fields) -> dict | None:
        """The three store calls behind a terminal transition, as one unit.

        Runs on a worker thread. Returns the finished row, or None when the
        run was already terminal and nothing was written — the first writer
        wins, which is what keeps a cancel racing `_drive` to a single status.
        """
        run = self._store.get_run(run_id)
        if run and run["status"] in self._store.RunStatus.TERMINAL:
            return None
        self._store.update_run(run_id, status=status, ended_at=time.time(),
                               **fields)
        return self._store.get_run(run_id)

    async def _finish(self, run_id: str, status: str, **fields) -> None:
        """Terminal transition: written off the loop, published on it.

        These were three synchronous SQLite calls on the loop thread while
        `_consume` wrote the same database from a worker — and with sqlite's
        5s busy timeout that could stall the voice path, which shares this
        thread. Every other store call in this module was already
        `to_thread`'d; this is the last one. The publish stays on the loop:
        `_publish` fans out to WebSocket subscribers.
        """
        row = await asyncio.to_thread(self._finish_write, run_id, status,
                                      **fields)
        if row is not None:
            self._publish({"type": "run_finished", "run": row})

    def _finish_blocking(self, run_id: str, status: str, **fields) -> None:
        """`_finish` for a path that may not be able to await.

        The error handlers below run inside `except BaseException`, where the
        task may already be cancelled — an `await` there raises
        CancelledError immediately and the terminal write would never happen,
        which is the exact failure this module exists to prevent. Blocking
        the loop for one write on a failure path is the lesser evil, and only
        error paths use it.
        """
        row = self._finish_write(run_id, status, **fields)
        if row is not None:
            self._publish({"type": "run_finished", "run": row})

    async def _drain_stderr(self, proc, sink: bytearray) -> None:
        """Keep the stderr pipe empty so the child never blocks writing to it.

        Only the tail is kept: a run that dumps megabytes to stderr must not
        be able to grow this buffer without bound.
        """
        stream = proc.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            sink.extend(chunk)
            if len(sink) > _STDERR_BYTES:
                del sink[:-_STDERR_BYTES]

    async def _collect_stderr(self, task, sink: bytearray) -> str:
        """Await the drain task (bounded) and decode whatever it captured."""
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task),
                                       timeout=_STDERR_DRAIN_SEC)
            except asyncio.TimeoutError:
                # Something inherited the pipe and is holding it open; take
                # what we have rather than hanging the run.
                task.cancel()
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception:
                log.debug("stderr drain failed", exc_info=True)
        return bytes(sink).decode(errors="replace")[-_STDERR_CHARS:]

    async def _drive(self, run_id: str, prompt: str, project_path: str,
                     resume_from: str | None, timeout_sec: float,
                     model: str | None = None) -> None:
        proc = None
        slot_held = False
        stderr_task = None
        stderr_sink = bytearray()
        try:
            await self._slots.acquire()
            slot_held = True

            if run_id in self._cancelling:
                # Cancelled while queued: never spawn anything.
                await self._finish(run_id, self._store.RunStatus.CANCELLED)
                return

            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._command(run_id, resume_from, model),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=project_path or None,
                    # NOT the inherited environment. server.py loads .env into
                    # os.environ at import, and a developer's .env holds
                    # ANTHROPIC_API_KEY for the older lookup paths — which the
                    # CLI silently prefers over the subscription login, moving
                    # every run onto paid API billing without a word. Same
                    # scrub the brain has had since milestone 1; see
                    # claude_env.py.
                    env=claude_env.child_env(),
                    # asyncio's default 64 KiB StreamReader line limit is far
                    # smaller than a stream-json line can legitimately be
                    # (a big tool result, a long assistant message, a large
                    # diff). See claude_env.STREAM_LINE_LIMIT.
                    limit=claude_env.STREAM_LINE_LIMIT,
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
                await self._finish(run_id, self._store.RunStatus.FAILED,
                                   error=f"could not start claude: {e}")
                return

            self._procs[run_id] = proc
            # Drain stderr from the first byte: nothing else reads it until
            # the process has exited, and a chatty run fills the pipe buffer
            # and deadlocks long before that.
            stderr_task = asyncio.create_task(
                self._drain_stderr(proc, stderr_sink))

            # Off the loop like every other store call here: this is two
            # synchronous SQLite calls, and `_consume` is already writing the
            # same database from a worker thread.
            started = await asyncio.to_thread(
                self._start_write, run_id, proc.pid)
            self._publish({"type": "run_started", "run": started})

            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

            reader = self._consume(run_id, proc)
            try:
                await asyncio.wait_for(reader, timeout=timeout_sec)
            except asyncio.TimeoutError:
                await self._terminate(proc)
                # Keep the stderr we just collected: a run that had to be
                # killed is the one most in need of the context, and this was
                # previously computed and discarded.
                stderr = await self._collect_stderr(stderr_task, stderr_sink)
                error = f"exceeded timeout of {timeout_sec}s"
                if stderr:
                    error = f"{error}\n{stderr}"
                await self._finish(run_id, self._store.RunStatus.TIMED_OUT,
                                   error=error)
                return
            except _IdleTimeout as e:
                # Alive, holding stdout, and silent past the idle bound. EOF
                # is never coming (see _IDLE_OUTPUT_SEC), so the EOF grace
                # below would never have run.
                log.warning("run %s: child %s %s; killing it",
                            run_id, proc.pid, e)
                await self._terminate(proc)
                stderr = await self._collect_stderr(stderr_task, stderr_sink)
                # TIMED_OUT rather than FAILED: it used up a time budget,
                # which is what a timeout is. The budget it used up is named
                # in the error so the two are never confused in the UI.
                error = (f"claude produced no output for {self._idle_sec}s "
                         f"and was still running; killed")
                if stderr:
                    error = f"{error}\n{stderr}"
                await self._finish(run_id, self._store.RunStatus.TIMED_OUT,
                                   error=error)
                return
            # NOT `self._procs.pop(run_id)` here. `_procs` is what `cancel()`
            # signals through, so the process must stay in it until it is
            # actually gone — otherwise cancel() takes the "already dead"
            # branch, writes CANCELLED, returns True, and never touches the
            # live child. The outer `finally` does the pop.

            # EOF on stdout is not the same as the process being over. A
            # child that closes stdout and keeps running (or one whose
            # descendants inherited the pipe) used to park this coroutine on
            # an unbounded `proc.wait()`: the run stayed `running` forever
            # and its concurrency permit was gone for good.
            eof_hang = False
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()),
                                       timeout=self._eof_grace_sec)
            except asyncio.TimeoutError:
                eof_hang = True
                log.warning("run %s: child %s closed stdout but did not exit "
                            "within %ss; killing it", run_id, proc.pid,
                            self._eof_grace_sec)
                await self._terminate(proc)
            stderr = await self._collect_stderr(stderr_task, stderr_sink)

            run = await asyncio.to_thread(self._store.get_run, run_id)
            if run and run["status"] in self._store.RunStatus.TERMINAL:
                return

            if run_id in self._cancelling:
                # We won the race with cancel(). The exit code is whatever the
                # signal produced (-15); the *intent* was cancellation.
                await self._finish(run_id, self._store.RunStatus.CANCELLED,
                                   exit_code=proc.returncode)
            elif eof_hang:
                # Whatever it did before EOF, the process itself misbehaved
                # and we killed it, so its exit status says nothing. FAILED
                # rather than TIMED_OUT: it did not use up a time budget, it
                # refused to exit.
                error = (f"claude closed stdout but did not exit within "
                         f"{self._eof_grace_sec}s; killed")
                if stderr:
                    error = f"{error}\n{stderr}"
                await self._finish(run_id, self._store.RunStatus.FAILED,
                                   exit_code=proc.returncode, error=error)
            elif proc.returncode == 0 and run and run["is_error"]:
                # Exit 0 is not a verdict. The CLI reports an auth failure as
                # `subtype: "success"` with `is_error: true` and STILL exits
                # 0 — brain.py has treated the identical field as fatal since
                # milestone 1 — so a run that never did anything was recorded
                # `succeeded`. `assess_outcome` cannot catch this either: a
                # run that wrote files and then errored looks like work done.
                # A run whose own final event says it errored is FAILED.
                error = (run["result_text"] or stderr
                         or "claude reported is_error on its result event")
                await self._finish(run_id, self._store.RunStatus.FAILED,
                                   exit_code=0, error=error[:_STDERR_CHARS])
            elif proc.returncode == 0:
                await self._finish(run_id, self._store.RunStatus.SUCCEEDED,
                                   exit_code=0)
            else:
                await self._finish(run_id, self._store.RunStatus.FAILED,
                                   exit_code=proc.returncode,
                                   error=stderr or f"exit code {proc.returncode}")
        except BaseException as e:
            # Anything unexpected — a store write raising "database is
            # locked", a decode error, CancelledError on shutdown — must
            # still leave the run terminal and the child reaped, and must be
            # logged rather than swallowed as "never retrieved".
            cancelled = run_id in self._cancelling
            if cancelled and isinstance(e, asyncio.CancelledError):
                # cancel() cancels the queued driver task on purpose. That
                # arrives here as CancelledError; it is a normal user action,
                # not a driver failure, and must not be logged at ERROR.
                log.info("run %s cancelled while queued", run_id)
            else:
                log.exception("run %s driver failed", run_id)
            status = (self._store.RunStatus.CANCELLED
                      if cancelled
                      else self._store.RunStatus.FAILED)
            try:
                self._finish_blocking(run_id, status,
                                      error=repr(e)[:_STDERR_CHARS])
            except Exception:
                log.exception("run %s could not be marked terminal", run_id)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()),
                                           timeout=self._grace_sec)
                except BaseException:
                    log.debug("run %s could not be reaped", run_id,
                              exc_info=True)
            if isinstance(e, (asyncio.CancelledError, SystemExit,
                              KeyboardInterrupt)):
                # The run is terminal and the child reaped; interpreter
                # shutdown and cancellation must still propagate.
                raise
        finally:
            self._procs.pop(run_id, None)
            self._cancelling.discard(run_id)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if slot_held:
                # Released only after the child has been reaped, and on every
                # exit path: a leaked permit permanently shrinks capacity.
                self._slots.release()

    async def _consume(self, run_id: str, proc) -> None:
        """Read the JSONL stream, persisting in batches off the event loop.

        SQLite writes are synchronous. Doing one per event on the loop thread
        would stutter the voice path on a chatty build, so events are buffered
        and flushed via a worker thread.
        """
        seq = await asyncio.to_thread(self._store.next_seq, run_id)
        pending: list[tuple[int, str, str]] = []
        last_flush = time.monotonic()
        # Tokens accumulated from the per-turn usage on `assistant` events, so
        # the dashboard can watch them climb. Dollars are NOT derived from
        # these: cost is recorded from `result`, never estimated.
        totals = {"input_tokens": 0, "output_tokens": 0,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0}
        # Sticky, because `is_error` was written unconditionally on every
        # `result` event and `_drive` reads whichever one landed last. A
        # stream carrying `{"is_error": true}` and then `{"is_error": false}`
        # therefore came back `succeeded` — the CLI's own report of a failure
        # erased by its own next line. The stream is written by the CLI and
        # not by the model, so this is robustness rather than a live attack;
        # it is still the wrong direction to be wrong in. Once true, true.
        saw_error = False

        async def flush():
            nonlocal pending, last_flush
            if pending:
                batch, pending = pending, []
                await asyncio.to_thread(self._store.append_events, run_id, batch)
            last_flush = time.monotonic()

        # The reader is never simply awaited. EOF is only one of the ways a
        # child stops producing output and it is the rarest of them (see
        # _IDLE_OUTPUT_SEC), so every wait for a line is also a chance to ask
        # the two questions EOF would otherwise have answered: has the
        # process exited, and has it been quiet for too long.
        line_task = None
        last_line = time.monotonic()
        # Consecutive quiet ticks during which the child has already exited.
        # Two rather than one so that anything it wrote just before exiting
        # has a full poll interval to arrive.
        exited_ticks = 0
        try:
            while True:
                if line_task is None:
                    line_task = asyncio.ensure_future(proc.stdout.readline())
                # `wait` rather than `wait_for`: a timeout must not cancel
                # the read. Cancelling a readline that is completing in the
                # same instant would discard the line it had just taken out
                # of the buffer, and one of those lines is the `result`
                # event the run's whole verdict is read from.
                done, _pending = await asyncio.wait({line_task},
                                                    timeout=self._poll_sec)
                if not done:
                    if proc.returncode is not None:
                        # The child is GONE — its exit status is known — and
                        # the pipe is still open, so a descendant inherited
                        # it. The real CLI spawns MCP children; any one of
                        # them outliving it produces exactly this, and it
                        # never reaches EOF.
                        exited_ticks += 1
                        if exited_ticks >= 2:
                            log.warning(
                                "run %s: child %s exited (%s) but something "
                                "still holds its stdout; not waiting for an "
                                "EOF that cannot come", run_id, proc.pid,
                                proc.returncode)
                            return
                        continue
                    idle = time.monotonic() - last_line
                    if idle >= self._idle_sec:
                        raise _IdleTimeout(
                            f"no output for {idle:.0f}s while still running")
                    continue

                exited_ticks = 0
                finished, line_task = line_task, None
                # An oversized line is the child talking, so it counts as
                # liveness even though it is dropped.
                last_line = time.monotonic()
                # A plain `async for raw in proc.stdout:` calls readline()
                # under the hood, and readline() raises ValueError when a
                # single line exceeds the StreamReader's limit — even the
                # raised claude_env.STREAM_LINE_LIMIT is not a guarantee,
                # just a much higher ceiling. That must not end the run: the
                # oversized line's bytes have already been discarded by
                # readline() itself (that's what keeps the stream aligned on
                # the next '\n'), so the fix is to log and keep reading.
                try:
                    raw = finished.result()
                except ValueError as e:
                    log.warning("run %s: skipping oversized stdout line "
                               "(%s)", run_id, e)
                    continue
                if not raw:
                    break
                line = raw.decode(errors="replace")
                event = stream_parser.parse_line(line)
                if event is None:
                    continue

                kind = stream_parser.event_kind(event)
                # Capped, not stored verbatim: STREAM_LINE_LIMIT is 64 MiB
                # and jarvis.db is permanent. See stream_parser.cap_payload —
                # it shrinks the long strings inside the event rather than
                # the line, so the payload still parses and still carries the
                # tool names `assess_outcome` reads.
                pending.append((seq, kind,
                                stream_parser.cap_payload(line.strip(), event)))

                if kind == "system" and event.get("subtype") == "init":
                    meta = stream_parser.extract_init_metadata(event)
                    if meta["model"]:
                        await flush()
                        await asyncio.to_thread(self._store.update_run, run_id,
                                                model=meta["model"])
                        await self._publish_run_updated(run_id)
                elif kind == "assistant":
                    # The CLI reports token usage on EVERY assistant turn but
                    # the dollar cost only once, in the terminal `result`. So
                    # these accumulate into the same columns `detail.ts`
                    # already renders — the numbers climb live with no
                    # frontend change — and the `result` below then
                    # overwrites the running estimate with the CLI's own
                    # authoritative totals. Dollars are never derived from
                    # these; cost is recorded from `result`, never estimated.
                    usage = stream_parser.extract_assistant_usage(event)
                    if any(usage.values()):
                        for key, value in usage.items():
                            totals[key] += value
                        await flush()
                        await asyncio.to_thread(self._store.update_run,
                                                run_id, **totals)
                        await self._publish_run_updated(run_id)
                elif kind == "result":
                    metrics = stream_parser.extract_result_metrics(event)
                    saw_error = saw_error or bool(metrics["is_error"])
                    await flush()
                    await asyncio.to_thread(
                        self._store.update_run, run_id,
                        cost_usd=metrics["cost_usd"],
                        input_tokens=metrics["input_tokens"],
                        output_tokens=metrics["output_tokens"],
                        cache_read_tokens=metrics["cache_read_tokens"],
                        cache_creation_tokens=metrics["cache_creation_tokens"],
                        num_turns=metrics["num_turns"],
                        result_text=metrics["result_text"][:20000],
                        # The CLI's own verdict on the turn. It used to be
                        # extracted here and dropped on the floor, which is
                        # how an auth failure — reported as `subtype:
                        # "success"` with `is_error: true`, exit code 0 —
                        # came back as a succeeded run. `saw_error`, not
                        # `metrics["is_error"]`: a later clean result must
                        # not un-say an earlier failure.
                        is_error=int(saw_error),
                    )
                    await self._publish_run_updated(run_id)

                if (len(pending) >= _EVENT_BATCH
                        or time.monotonic() - last_flush >= _EVENT_FLUSH_SEC):
                    await flush()

                self._publish({"type": "run_event", "run_id": run_id, "seq": seq,
                               "kind": kind, "payload": event})
                seq += 1
        finally:
            if line_task is not None:
                # Only ever pending on a path that is ending the run anyway
                # (idle, exited-but-held, cancellation): there is nothing
                # left for a line to change.
                line_task.cancel()
            # Never lose buffered events, even on cancellation or a read error.
            await flush()
