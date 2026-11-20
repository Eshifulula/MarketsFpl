"""The execution guard for human-approved changes: rollover, deadline, races, ambiguity."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import PIN
from test_write_tools import FakeFPL, _approve, fpl  # noqa: F401  (fpl is a fixture)

from fpl_server import mcp_write_tools as wt
from fpl_server.approvals import squad_fingerprint
from fpl_server.client import FPLAPIError
from fpl_server.config import get_settings
from fpl_server.state import store

AFTER_DEADLINE = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _live(monkeypatch):
    monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    return get_settings()


def _events(*events):
    return SimpleNamespace(events=list(events))


async def test_approval_does_not_survive_a_gameweek_rollover(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    r = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, r["proposal_id"])

    async def rollover(_client):  # the deadline passed while the approval was waiting
        store.bootstrap_data = _events(
            SimpleNamespace(id=12, is_next=False), SimpleNamespace(id=13, is_next=True)
        )

    monkeypatch.setattr(store, "refresh_bootstrap_data", rollover)
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "rolled over" in out["reason"]
    assert fpl.posts == [] and proposals.get(r["proposal_id"]).status == "failed"


async def test_execution_after_the_deadline_is_refused(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    monkeypatch.setattr(
        store,
        "bootstrap_data",
        _events(SimpleNamespace(id=12, is_next=True, deadline_time="2026-11-21T11:00:00Z")),
        raising=False,
    )
    r = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, r["proposal_id"])
    monkeypatch.setattr(wt, "_now", lambda: AFTER_DEADLINE)
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "deadline" in out["reason"]
    assert fpl.posts == []


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345])
async def test_missing_or_unparseable_deadline_blocks_execution(fpl, proposals, monkeypatch, bad):
    settings = _live(monkeypatch)
    r = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, r["proposal_id"])
    monkeypatch.setattr(
        store,
        "bootstrap_data",
        _events(SimpleNamespace(id=12, is_next=True, deadline_time=bad)),
        raising=False,
    )
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "deadline" in out["reason"]
    assert fpl.posts == [] and proposals.get(r["proposal_id"]).status == "failed"


async def test_deadline_passing_during_the_squad_read_blocks_the_post(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    monkeypatch.setattr(
        store,
        "bootstrap_data",
        _events(SimpleNamespace(id=12, is_next=True, deadline_time="2026-11-21T11:00:00Z")),
        raising=False,
    )
    r = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, r["proposal_id"])

    clock = {"now": datetime(2026, 11, 21, 10, 59, 59, tzinfo=UTC)}  # 1s before the deadline
    monkeypatch.setattr(wt, "_now", lambda: clock["now"])
    real_get_my_team = fpl.get_my_team

    async def slow_get_my_team(entry):
        result = await real_get_my_team(entry)
        clock["now"] = AFTER_DEADLINE  # the read started in time but finished after the deadline
        return result

    monkeypatch.setattr(fpl, "get_my_team", slow_get_my_team)
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "deadline" in out["reason"]
    assert fpl.posts == [] and proposals.get(r["proposal_id"]).status == "failed"


async def test_a_new_proposal_cancels_an_approved_but_unexecuted_one(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    first = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, first["proposal_id"])
    second = await wt.propose_transfers(["MID8"], ["MID104"])
    assert proposals.get(first["proposal_id"]).status == "rejected"
    out = await wt.execute_approved_proposal(first["proposal_id"])
    assert out["status"] == "refused" and fpl.posts == []
    assert proposals.get(second["proposal_id"]).status == "pending"


async def test_lineup_order_change_after_approval_blocks_execution(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    r = await wt.propose_captain("MID10", "MID9")
    _approve(proposals, settings, r["proposal_id"])
    rows = copy.deepcopy(fpl.picks)  # someone reorders the XI on the FPL website
    rows[1]["position"], rows[2]["position"] = rows[2]["position"], rows[1]["position"]
    fpl.picks = rows
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "changed" in out["reason"] and fpl.posts == []


async def test_captaincy_change_after_approval_blocks_execution(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    rows = copy.deepcopy(fpl.picks)
    for row in rows:
        row["is_captain"] = row["element"] == 11  # captain moved on the website
    fpl.picks = rows
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and fpl.posts == []


@pytest.mark.parametrize("error", [FPLAPIError(503, "unavailable"), RuntimeError("boom"), TimeoutError()])
async def test_ambiguous_result_after_sending_is_unknown_and_final(fpl, proposals, monkeypatch, error):
    settings = _live(monkeypatch)
    fpl.fail_with = error
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "outcome_unknown" and "FPL website" in out["reason"]
    assert len(fpl.posts) == 1
    assert proposals.get(r["proposal_id"]).status == "outcome_unknown"
    again = await wt.execute_approved_proposal(r["proposal_id"])
    assert again["status"] == "refused" and len(fpl.posts) == 1  # never re-sent


async def test_failure_before_anything_was_sent_is_failed_not_unknown(fpl, proposals, monkeypatch):
    settings = _live(monkeypatch)
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])

    async def broken(_entry):
        raise RuntimeError("network down")

    monkeypatch.setattr(fpl, "get_my_team", broken)
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "Nothing changed" in out["reason"]
    assert fpl.posts == [] and proposals.get(r["proposal_id"]).status == "failed"


def test_fingerprint_covers_order_captaincy_and_prices(world):
    picks = world["picks"]
    base = squad_fingerprint(picks, 10, 12)

    reordered = copy.deepcopy(picks)
    reordered[0]["position"], reordered[11]["position"] = reordered[11]["position"], reordered[0]["position"]
    assert squad_fingerprint(reordered, 10, 12) != base

    new_captain = copy.deepcopy(picks)
    for row in new_captain:
        row["is_captain"] = row["element"] == 11
    assert squad_fingerprint(new_captain, 10, 12) != base

    new_vice = copy.deepcopy(picks)
    for row in new_vice:
        row["is_vice_captain"] = row["element"] == 11
    assert squad_fingerprint(new_vice, 10, 12) != base

    repriced = copy.deepcopy(picks)
    repriced[3]["selling_price"] += 1
    assert squad_fingerprint(repriced, 10, 12) != base


def test_the_fake_fpl_helper_is_the_one_under_test():
    assert FakeFPL.user_info["player"]["entry"] == 42 and PIN
