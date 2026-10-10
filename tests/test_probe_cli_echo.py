"""scripts/probe_cli_echo.py judges a real CLI; these judge the judge.

The probe needs a real `claude` and is not run here (the suite never
invokes one). Each check reads a stream; given the stream the CLI produced
on 2026-09-30 it passes, and given one that has drifted it says how — a
check that quietly stopped seeing the difference would pass every upgrade
while `Brain._handle` misattributed.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _probe():
    spec = importlib.util.spec_from_file_location("probe_cli_echo",
                                                  ROOT / "scripts" / "probe_cli_echo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("probe_cli_echo", module)
    spec.loader.exec_module(module)
    module.SETTLE_SECONDS = 0          # a scripted stream has nothing still to land
    return module


class Scripted:
    """A CLI that has already said everything: `send` hands out the tags
    the script uses, in order, and `wait` looks at the whole stream."""

    def __init__(self, tags, events):
        self.tags, self.events = list(tags), events

    def send(self, text, *, tag=None, peer=False):
        if peer:
            return "the-other-sessions"
        return tag or self.tags.pop(0)

    def wait(self, pred, count=1, timeout=0):
        return sum(1 for e in self.events if pred(e)) >= count


def echo(tag, peer=False):
    ev = {"type": "user", "isReplay": True, "uuid": tag, "message": {"content": "..."}}
    if peer:
        ev.update(isSynthetic=True, origin={"kind": "peer", "from": "p"})
    return ev


def words():
    return {"type": "stream_event", "event": {"type": "content_block_delta"}}


def tool():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}


def came_back():
    return {"type": "user", "message": {"content": [{"type": "tool_result"}]}}


def result(*named, peer=False):
    ev = {"type": "result", "subtype": "success"}
    if named:
        ev.update(user_message_uuid=named[0], user_message_uuids=list(named))
    if peer:
        ev["origin"] = {"kind": "peer"}
    return ev


INIT = {"type": "system", "subtype": "init"}


def test_the_echo_as_measured_passes():
    p = _probe()
    assert p.check_echo(Scripted(["a"], [INIT, echo("a"), words(), result("a")])) == []


def test_output_before_the_echo_is_seen():
    p = _probe()
    assert p.check_echo(Scripted(["a"], [INIT, words(), echo("a"), result("a")])) == [
        "output came before the echo"]
    assert p.check_echo(Scripted(["a"], [INIT, words(), result("a")])) == ["never echoed"]
    assert p.check_echo(Scripted(["a"], [echo("a"), words(), result()])) == [
        "the result does not name the message"]


def test_a_fold_as_measured_passes():
    p = _probe()
    stream = [echo("a"), tool(), came_back(), echo("b"), words(), result("a", "b")]
    assert p.check_fold(Scripted(["a", "b"], stream)) == []


def test_a_message_that_waits_instead_of_folding_is_seen():
    p = _probe()
    stream = [echo("a"), tool(), came_back(), words(), result("a"), echo("b"), words(), result("b")]
    assert p.check_fold(Scripted(["a", "b"], stream)) == [
        "not echoed mid-turn", "not one result for both"]


def test_queued_behind_a_wake_as_measured_passes():
    p = _probe()
    stream = [echo("w", peer=True), words(), result(peer=True), echo("m"), words(), result("m")]
    assert p.check_queued(Scripted(["m"], stream)) == []


def test_a_wake_that_is_not_told_apart_is_seen():
    p = _probe()
    stream = [echo("w"), words(), result("m"), echo("m"), words(), result("m")]
    assert p.check_queued(Scripted(["m"], stream)) == [
        "the wake's echo does not say peer", "the wake's result names our message"]


def test_folded_into_a_wake_as_measured_passes():
    p = _probe()
    stream = [echo("w", peer=True), tool(), came_back(), echo("m"), words(), result("m", peer=True)]
    assert p.check_into_wake(Scripted(["m"], stream)) == []
    waited = [echo("w", peer=True), tool(), came_back(), words(), result(peer=True),
              echo("m"), words(), result("m")]
    assert p.check_into_wake(Scripted(["m"], waited)) == [
        "not folded into the wake", "the result does not name our message"]


def test_a_peer_folded_into_ours_as_measured_passes():
    p = _probe()
    stream = [echo("m"), tool(), came_back(), echo("x", peer=True), words(), result("m")]
    assert p.check_peer_in(Scripted(["m"], stream)) == []
    unmarked = [echo("m"), tool(), came_back(), echo("x"), words(), result("m")]
    assert p.check_peer_in(Scripted(["m"], unmarked)) == ["its echo does not say peer"]


def test_a_repeated_tag_that_runs_again_is_seen():
    p = _probe()
    assert p.check_once(Scripted(["a"], [echo("a"), words(), result("a"), echo("a")])) == []
    assert p.check_once(Scripted(["a"], [echo("a"), words(), result("a"),
                                         echo("a"), words(), result("a")])) == [
        "a repeated tag was run again"]
