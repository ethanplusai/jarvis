"""An approval is for a moment, not for ever.

Three holes, all confirmed by probing the real route:

1. SPENDABLE FOREVER. The hold is 120s but the card is offered for 24h, and
   `transition`'s expiry guard is `AND (expires>? OR state!='pending')` — it
   ignores expiry for anything that is not pending. So an approved row sits
   armed indefinitely, and `find_by_digest(digest, "approved")` is consulted
   before any other check. A click the user believed was consumed at ten
   o'clock silently authorises the post whenever those bytes are next
   emitted — days later, with nobody in the room.

2. SPENDABLE BY NOBODY. `/internal/tool` refuses an acting tool when
   `current_origin != "user"`. `/internal/pretool` had no such check, and
   the journal turn runs with origin="system". A human approved it; a human
   should be there when it goes.

3. A NO MEANS NOTHING. The gate reads 'approved' and 'pending' and never
   'rejected', so a refused call is re-staged verbatim on the next retry and
   the user is asked the same question again, indefinitely.

And one the fix for the first made:

4. A LAPSE IS FOR EVER. The expired approval was refused, and stayed the
   newest approved row for those bytes, so every identical call found it
   again and never reached `propose`. It could not be asked again.

5. SO IS A CARD NOBODY ANSWERED. A pending card past `expires` was still
   found and waited on, but `transition` will not move it, so the desk
   could neither approve nor reject it and every identical call was held
   on it and then denied.
"""

import importlib
import time
from contextlib import closing

import pytest


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import business_store
    importlib.reload(business_store)
    import tool_log
    importlib.reload(tool_log)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    business_store.init_db()
    tool_log.init_db()
    monkeypatch.setattr(server_module, "GATE_APPROVAL_WAIT_SEC", 0.05)
    return server_module


POST = {"text": "the launch post"}


class _Brain:
    """Just enough of one to answer `current_origin` and be shut down."""

    def __init__(self, origin):
        self.current_origin = origin

    async def stop(self):
        pass


def _ask(server, origin="user"):
    from fastapi.testclient import TestClient
    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        server.brain_instance = _Brain(origin)
        return client.post("/internal/pretool",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"tool_name": "mcp__linkedin__create_post",
                                 "tool_input": POST, "tool_use_id": "t"}).json()


def _decision(body):
    return body["hookSpecificOutput"]["permissionDecision"]


def _approve_the_staged_one(server):
    import business_store
    row = [a for a in business_store.list_actions()
           if a["operation"] == "mcp__linkedin__create_post" and a["state"] == "pending"][0]
    business_store.transition(row["id"], row["digest"], "pending", "approved")
    return row


def _age_it(server, action_id, seconds):
    import business_store
    with closing(business_store.connect()) as conn, conn:
        conn.execute("UPDATE business_actions SET expires=? WHERE id=?",
                     (time.time() - seconds, action_id))


def test_a_fresh_approval_is_spent_as_before(server):
    _ask(server)
    _approve_the_staged_one(server)
    assert _decision(_ask(server)) == "allow"


def test_an_expired_approval_is_not_spent(server):
    """The UI offers the card for 24h and says so. Past that the answer is
    no, not 'whenever those bytes next appear'."""
    _ask(server)
    row = _approve_the_staged_one(server)
    _age_it(server, row["id"], 60)
    body = _ask(server)
    assert _decision(body) == "deny"
    assert "expired" in body["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_an_approval_is_not_spent_on_a_turn_nobody_drove(server):
    """A human approved it; a human should be there when it goes. The
    journal turn runs with origin='system' and nobody is in the room."""
    _ask(server)
    _approve_the_staged_one(server)
    body = _ask(server, origin="system")
    assert _decision(body) == "deny"
    reason = body["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert "ask" in reason or "you" in reason, reason


def test_the_approval_survives_for_the_user_to_spend_later(server):
    """Refusing the system turn must not BURN it."""
    _ask(server)
    _approve_the_staged_one(server)
    _ask(server, origin="system")
    assert _decision(_ask(server, origin="user")) == "allow", \
        "the unattended attempt consumed an approval it was not allowed to use"


def test_a_rejected_call_is_not_asked_again(server):
    """The gate read 'approved' and 'pending' and never 'rejected', so a no
    was re-staged verbatim on the next retry and the user was asked the same
    question for ever."""
    import business_store
    _ask(server)
    row = [a for a in business_store.list_actions() if a["state"] == "pending"][0]
    business_store.transition(row["id"], row["digest"], "pending", "rejected")

    body = _ask(server)
    assert _decision(body) == "deny"
    assert "declined" in body["hookSpecificOutput"]["permissionDecisionReason"].lower()
    pending = [a for a in business_store.list_actions() if a["state"] == "pending"]
    assert not pending, f"the refused call was queued again: {pending}"


def test_a_rejection_stops_applying_once_it_is_stale(server):
    """A no is about that moment. The user must be able to change his mind
    tomorrow without the gate remembering yesterday's refusal for ever."""
    import business_store
    _ask(server)
    row = [a for a in business_store.list_actions() if a["state"] == "pending"][0]
    business_store.transition(row["id"], row["digest"], "pending", "rejected")
    _age_it(server, row["id"], 60)

    _ask(server)
    assert [a for a in business_store.list_actions() if a["state"] == "pending"], \
        "a stale no still suppresses the question"


def _reason(body):
    return body["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_an_expired_approval_is_put_to_the_user_afresh(server):
    """Not spent, and not a dead end either. Seen live: a Paperclip comment
    approved one evening lapsed unspent the next, and every identical call
    after that was refused on the lapsed row. The only ways out were to
    change the wording or delete the card from the desk by hand."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)

    assert _decision(_ask(server)) == "deny"
    fresh = [a for a in business_store.list_actions() if a["state"] == "pending"]
    assert len(fresh) == 1 and fresh[0]["id"] != old["id"], \
        f"the identical call was not put to the user again: {fresh}"
    assert business_store.get_action(old["id"])["state"] == "approved", \
        "the lapsed approval was spent"

    _approve_the_staged_one(server)
    assert _decision(_ask(server)) == "allow", "the fresh approval could not be spent"


def test_the_lapse_is_mentioned_once(server):
    """When the call is put to the user afresh, and not again: not on a retry
    while the new card waits, and not once that card is spent. The lapsed
    row stays the newest APPROVED one for those bytes, so without care it
    would be announced every time they came round."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)

    assert "afresh" in _reason(_ask(server))
    assert "afresh" not in _reason(_ask(server)), "told again while the new card waits"
    assert len([a for a in business_store.list_actions() if a["state"] == "pending"]) == 1

    _approve_the_staged_one(server)
    assert _decision(_ask(server)) == "allow"
    assert "afresh" not in _reason(_ask(server)), "told again after the new card was spent"


def _pending(business_store, but_not=()):
    return [a for a in business_store.list_actions()
            if a["state"] == "pending" and a["id"] not in but_not]


def test_a_card_nobody_answered_is_put_to_the_user_afresh(server):
    """A card left for a day is past its window: the desk can neither
    approve nor reject it, because `transition` will not move a pending row
    past `expires`. The gate still found it, held every identical call on it
    for the whole wait, and denied. Only deleting it by hand got the call
    asked again."""
    import business_store
    _ask(server)
    old = _pending(business_store)[0]
    _age_it(server, old["id"], 60)
    with pytest.raises(ValueError):
        business_store.transition(old["id"], old["digest"], "pending", "approved")

    _ask(server)
    _ask(server)
    fresh = _pending(business_store, but_not={old["id"]})
    assert len(fresh) == 1, f"held on a card nobody can answer, or piled up: {fresh}"

    business_store.transition(fresh[0]["id"], fresh[0]["digest"], "pending", "approved")
    assert _decision(_ask(server)) == "allow", "the fresh card could not be spent"


def _decide(server, action, approve=True):
    """Through the desk's own route, the way a click does."""
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        return client.post(f"/api/business/actions/{action['id']}/decision",
                           headers={"Origin": "http://localhost:5173"},
                           json={"digest": action["digest"], "approve": approve})


def test_the_desk_can_answer_the_card_that_replaces_an_expired_one(server):
    """The same dead end, seen from the desk: both buttons on the expired
    card answered 'Action changed, expired, or was already submitted'. The
    card that replaces it must be one a click can approve, and the next
    call must spend that approval."""
    import business_store
    _ask(server)
    old = _pending(business_store)[0]
    _age_it(server, old["id"], 60)
    assert _decide(server, old).status_code == 409, "the desk approved an expired card"
    assert _decide(server, old, approve=False).status_code == 409, \
        "the desk rejected an expired card"

    assert _decision(_ask(server)) == "deny"
    fresh = _pending(business_store, but_not={old["id"]})
    assert len(fresh) == 1, f"the identical call was not put to the user again: {fresh}"

    assert _decide(server, fresh[0]).status_code == 200, "the desk could not approve the new card"
    assert _decision(_ask(server)) == "allow", "the new approval could not be spent"
    assert business_store.get_action(fresh[0]["id"])["state"] == "submitted"


def test_the_lapse_is_not_mentioned_again_when_its_new_card_lapses_too(server):
    """The approval lapsed, it was put to the user afresh and said so, and
    that card was left unanswered for a day as well. Asking a third time is
    right; repeating the first lapse is not, it was already told."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)
    assert "afresh" in _reason(_ask(server))
    requeued = _pending(business_store)[0]
    _age_it(server, requeued["id"], 60)

    assert "afresh" not in _reason(_ask(server)), "the first lapse was announced again"
    assert _pending(business_store, but_not={requeued["id"]}), "it was not asked again"


def test_a_lapse_taken_off_the_desk_is_still_mentioned(server):
    """'Clear completed' sweeps a lapsed approval off the desk with the rest,
    and the card only ever read 'approved'. Clearing it is not being told."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)
    business_store.clear_completed()

    assert "afresh" in _reason(_ask(server))


def test_a_card_taken_off_the_desk_does_not_bring_the_lapse_back(server):
    """The re-queued card lapsed too and was deleted. The user was told about
    the first lapse when that card went up; deleting it does not un-tell."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)
    assert "afresh" in _reason(_ask(server))
    requeued = _pending(business_store)[0]
    _age_it(server, requeued["id"], 60)
    business_store.delete_action(requeued["id"])

    assert "afresh" not in _reason(_ask(server)), "the first lapse was announced again"


def test_a_turn_nobody_drove_leaves_the_lapse_for_the_user(server):
    """The journal turn runs with origin='system' and nobody hears its
    reason. Re-queueing there would spend the one mention on an empty room
    and hold a handover with a 15s budget for two minutes. It is refused at
    once, as before, and the user's own ask puts it afresh and says so."""
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)

    started = time.monotonic()
    assert _decision(_ask(server, origin="system")) == "deny"
    assert time.monotonic() - started < 2.0
    assert not _pending(business_store), "a turn nobody drove re-queued it"

    assert "afresh" in _reason(_ask(server, origin="user"))
    assert len(_pending(business_store)) == 1


def test_the_lapse_is_mentioned_however_the_hold_ends(server, monkeypatch):
    """The mention rides on the call's final reason: a no during the hold
    carries it as well as a timeout does."""
    import threading
    import business_store
    _ask(server)
    old = _approve_the_staged_one(server)
    _age_it(server, old["id"], 60)
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 8.0)

    def reject_soon():
        for _ in range(100):
            rows = _pending(business_store)
            if rows:
                business_store.transition(rows[0]["id"], rows[0]["digest"],
                                          "pending", "rejected")
                return
            time.sleep(0.05)
    threading.Thread(target=reject_soon, daemon=True).start()

    reason = _reason(_ask(server))
    assert "declined" in reason and "afresh" in reason, reason
