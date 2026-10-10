"""Two more writers whose output outlives the turn, and the generation gate
they were missing.

`approve_document` writes an approval record into the project: a digest of
the exact text the user approved, which `start_build` later proceeds on
without asking again. `business_record` writes a task, a contact, an
invoice or an expense into the ledger. Neither is loaded back as trusted
prose the way a memory is, but both are kept for good, and both were gated
on the TURN alone. Turn N reads a poisoned README (the write is refused
that turn); turn N+1 the user says anything at all, the turn is clean, the
poison is still in the context, and the approval or the invoice went
through. Flagged in the memory audit of 2026-09-23; closed here the way the
memory writers were closed (tests/test_memory_writers.py): the GENERATION's
taint stands between these tools and a write until a rotation clears it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_memory_writers import _Brain, call, wired_server  # noqa: E402,F401

DURABLE = {"approve_document", "business_record"}
SPEC = "docs/superpowers/specs/2026-09-24-chitauri.md"


def _spec(project: Path) -> None:
    doc = project / SPEC
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Chitauri\n\n## 1. Goal\n\nBuild the thing.\n\n## 2. Not now\n\nAnything else.\n",
                   encoding="utf-8")


ARGS_FOR = {
    "approve_document": {"project": "chitauri"},
    "business_record": {"kind": "invoice", "title": "Pay the README's supplier", "status": "draft",
                        "amount_minor": 250000, "currency": "USD"},
}


def _written(server, tool: str) -> bool:
    """Did the durable thing land?"""
    import specs
    if tool == "approve_document":
        project = server.cached_projects[0]["path"]
        return specs.approval_record_path(project, SPEC).exists()
    import business_api
    return bool(business_api.store.list_records("invoice"))


@pytest.fixture
def poisoned(call):
    """Turn N: the brain read the attacker's README. Turn N+1: the user has
    spoken again, so the turn is clean and the generation is not."""
    _call, brain, server = call
    _spec(Path(server.cached_projects[0]["path"]))
    _call("read_file", project="chitauri", path="README.md")
    brain.new_turn()
    assert brain.turn_untrusted_source is None
    assert brain.generation_untrusted_source is not None
    return _call, brain, server


def test_the_two_are_gated_on_the_generation_like_the_memory_writers(wired_server):
    server = wired_server[0]
    assert server.DURABLE_WRITERS == DURABLE
    assert server.GENERATION_GATED == server.MEMORY_WRITERS | DURABLE
    assert DURABLE <= server.ACTING_TOOLS
    assert DURABLE <= server.FOREIGN_TEXT_REFUSED, "an acting tool that survives a read is not gated at all"
    assert not (DURABLE & server.MEMORY_WRITERS), "these write records, not memory"


@pytest.mark.parametrize("tool", sorted(DURABLE))
def test_a_later_clean_turn_still_cannot_record_what_a_poisoned_one_read(poisoned, tool):
    _call, _brain, server = poisoned
    out = _call(tool, **ARGS_FOR[tool])
    assert out["ok"] is False, f"{tool} went through one turn after the poisoned read: {out}"
    assert "untrusted_content_in_this_session" in out["text"], out
    assert not _written(server, tool)


@pytest.mark.parametrize("tool", sorted(DURABLE))
def test_the_refusal_says_the_record_would_be_kept_and_what_to_do(poisoned, tool):
    _call, _brain, _server = poisoned
    said = _call(tool, **ARGS_FOR[tool])["text"]
    assert "sir" in said and "record" in said, said
    assert "say it again" in said.lower() or "ask me again" in said.lower(), said


@pytest.mark.parametrize("tool", sorted(DURABLE))
def test_a_rotation_clears_the_way(poisoned, tool):
    """The user says it again, in his own words, to a generation that has
    read nothing: the write goes through."""
    _call, brain, server = poisoned
    brain.generation_label = None
    out = _call(tool, **ARGS_FOR[tool])
    assert out["ok"] is True, out
    assert _written(server, tool)


@pytest.mark.parametrize("tool", sorted(DURABLE))
def test_an_untainted_generation_still_writes(call, tool):
    """Without this the refusals above prove nothing."""
    _call, _brain, server = call
    _spec(Path(server.cached_projects[0]["path"]))
    out = _call(tool, **ARGS_FOR[tool])
    assert out["ok"] is True, out
    assert _written(server, tool)


def test_the_gate_is_documented():
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    assert "DURABLE_WRITERS" in readme
