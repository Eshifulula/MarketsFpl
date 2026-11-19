"""The emergency exception end-to-end against a fake FPL, plus its pure helpers."""

import asyncio
import copy
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import PIN

from fpl_server import mcp_emergency_tools as et
from fpl_server import mcp_write_tools as wt
from fpl_server.client import FPLAPIError, ServerClock
from fpl_server.config import get_settings
from fpl_server.emergency import (
    chips_known_inactive,
    claim_emergency_slot,
    classify_replacement,
    classify_unavailable,
    count_emergency_slots,
    data_is_fresh,
    parse_deadline,
    plan_swap,
)
from fpl_server.rules import PlayerInfo, RuleViolation
from fpl_server.state import store

NOW = datetime(2026, 11, 21, 10, 45, tzinfo=UTC)


class FakeFPL:
    user_info = {"player": {"entry": 42}}

    def __init__(self, picks):
        self.picks = picks
        self.chips = [{"name": "bboost", "status_for_entry": "available"}]
        self.posts = []
        self.fail_with = None
        self.bootstrap_clock = ServerClock(NOW, NOW, 0)
        self.calls = 0
        self.drift_from_call = None  # from this get_my_team call on, a selling price changes
        self.apply_posts = False  # POSTs mutate the squad, like the real FPL
        self.yield_to_loop = False  # let concurrent tool calls interleave
        self.inflight = 0
        self.max_inflight = 0

    async def get_my_team(self, entry_id):
        self.calls += 1
        if self.yield_to_loop:
            await asyncio.sleep(0)
        picks = copy.deepcopy(self.picks)
        if self.drift_from_call and self.calls >= self.drift_from_call:
            picks[0]["selling_price"] += 1
        return {
            "picks": picks,
            "transfers": {"bank": 10, "limit": 1, "made": 0},
            "chips": self.chips,
        }

    async def post_my_team(self, entry_id, payload):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.posts.append((f"my-team/{entry_id}/", payload))
            if self.yield_to_loop:
                await asyncio.sleep(0)
            if self.fail_with:
                raise self.fail_with
            if self.apply_posts:
                by_el = {p["element"]: p for p in self.picks}
                rows = []
                for r in payload["picks"]:
                    row = dict(by_el[r["element"]])
                    row.update(
                        position=r["position"],
                        is_captain=r["is_captain"],
                        is_vice_captain=r["is_vice_captain"],
                    )
                    rows.append(row)
                self.picks = rows
            return {"ok": True}
        finally:
            self.inflight -= 1


@pytest.fixture
def env(world, proposals, monkeypatch):
    """Emergency ON, writes OFF (dry run), deadline 10 minutes away, MID8 suspended, MID12 fit."""
    monkeypatch.setenv("FPL_ALLOW_EMERGENCY", "true")
    players = dict(world["players"])
    players[8] = replace(players[8], status="s", news="Suspended")
    fpl = FakeFPL(world["picks"])
    deadline = (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = SimpleNamespace(id=12, is_next=True, deadline_time=deadline, finished=False)

    async def refresh(_client):
        return None

    monkeypatch.setattr(et, "_now", lambda: NOW)
    monkeypatch.setattr(wt, "_now", lambda: NOW)
    monkeypatch.setattr(wt, "_get_client", lambda: fpl)
    monkeypatch.setattr(wt, "_players_from_bootstrap", lambda: players)
    monkeypatch.setattr(store, "refresh_bootstrap_data", refresh)
    monkeypatch.setattr(store, "bootstrap_data", SimpleNamespace(events=[event]), raising=False)
    return SimpleNamespace(fpl=fpl, players=players, event=event, monkeypatch=monkeypatch)


def _audit_text(settings):
    return "".join(f.read_text() for f in settings.audit_dir.glob("*.jsonl"))


def _swap_rows(out):
    return {r["element"]: r["position"] for r in out["would_send"]["picks"]}


async def test_disabled_by_default_even_when_policy_would_allow(env):
    env.monkeypatch.delenv("FPL_ALLOW_EMERGENCY")
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and out["policy_would_allow"] is True
    assert env.fpl.posts == []


async def test_dry_run_when_writes_off(env):
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "dry_run" and env.fpl.posts == []
    by_el = _swap_rows(out)
    assert by_el[12] == 6 and by_el[8] == 14  # swapped slots, nothing else moved
    assert "emergency_dry_run" in _audit_text(get_settings())


async def test_live_swap_posts_once_with_unchanged_captaincy(env):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "executed" and len(env.fpl.posts) == 1
    endpoint, payload = env.fpl.posts[0]
    assert endpoint == "my-team/42/" and payload["chip"] is None
    flags = {r["element"]: (r["is_captain"], r["is_vice_captain"]) for r in payload["picks"]}
    assert flags[9] == (True, False) and flags[10] == (False, True)
    assert sum(r["is_captain"] for r in payload["picks"]) == 1
    assert [r["position"] for r in payload["picks"]] == list(range(1, 16))
    assert "emergency_executed" in _audit_text(get_settings())


async def test_model_cannot_assert_verification():
    params = set(inspect.signature(et.emergency_lineup_swap).parameters)
    assert params == {"player_out", "player_in"}  # no flag the model could set


@pytest.mark.parametrize(
    "seconds,ok",
    [(900, True), (901, False), (3600, False), (60, True), (59, False), (1, False), (0, False), (-300, False)],
)
async def test_window_boundaries_including_the_send_margin(env, seconds, ok):
    env.event.deadline_time = (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert (out["status"] == "dry_run") is ok
    assert env.fpl.posts == []


async def test_margin_is_rechecked_just_before_sending(env):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.event.deadline_time = (NOW + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    times = iter([NOW, NOW + timedelta(seconds=45)])  # 45s pass while we prepare
    env.monkeypatch.setattr(et, "_now", lambda: next(times, NOW + timedelta(seconds=45)))
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and "deadline" in out["reason"].lower()
    assert env.fpl.posts == [] and "emergency_aborted_late" in _audit_text(get_settings())


@pytest.mark.parametrize(
    "status,chance,ok",
    [("s", None, True), ("i", 0, True), ("i", None, False), ("i", 25, False), ("d", 50, False), ("a", None, False)],
)
async def test_only_confirmed_unavailability_qualifies(env, status, chance, ok):
    env.players[8] = replace(env.players[8], status=status, chance_next=chance)
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert (out["status"] == "dry_run") is ok


async def test_replacement_must_be_available(env):
    env.players[12] = replace(env.players[12], status="d", chance_next=75)
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    env.players[12] = replace(env.players[12], status="a", chance_next=50)
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"


async def test_stale_or_skewed_data_is_unverified(env):
    env.fpl.bootstrap_clock = ServerClock(NOW + timedelta(seconds=90), NOW, 0)  # server ahead
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    env.fpl.bootstrap_clock = ServerClock(NOW - timedelta(seconds=90), NOW, 0)  # local clock FAST
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    env.fpl.bootstrap_clock = ServerClock(NOW, NOW, 600)  # CDN-cached 10 minutes ago
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    env.fpl.bootstrap_clock = ServerClock(None, NOW, 0)  # no Date header
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    env.fpl.bootstrap_clock = None
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"
    assert env.fpl.posts == []


@pytest.mark.parametrize(
    "chips",
    [
        None,  # key missing / null
        [],  # empty
        "active",  # wrong type
        [{"name": "bboost"}],  # no status
        [{"name": "bboost", "status_for_entry": None}],
        [{"name": "bboost", "status_for_entry": "active"}],
        [{"name": "bboost", "status_for_entry": "available", "is_pending": True}],
        ["bboost"],
    ],
)
async def test_chip_state_must_be_positively_known_and_inactive(env, chips):
    env.fpl.chips = chips
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and env.fpl.posts == []


async def test_captaincy_and_illegal_formations_are_out_of_scope(env):
    env.players[9] = replace(env.players[9], status="s")
    assert (await et.emergency_lineup_swap("MID9", "MID12"))["status"] == "refused"  # captain
    env.players[10] = replace(env.players[10], status="s")
    assert (await et.emergency_lineup_swap("MID10", "MID12"))["status"] == "refused"  # vice
    env.players[1] = replace(env.players[1], status="s")
    assert (await et.emergency_lineup_swap("GKP1", "DEF7"))["status"] == "refused"  # no keeper left
    assert env.fpl.posts == []


async def test_must_be_starter_out_and_bench_in(env):
    assert (await et.emergency_lineup_swap("MID12", "MID8"))["status"] == "refused"  # reversed
    assert (await et.emergency_lineup_swap("MID8", "MID104"))["status"] == "refused"  # not in squad
    assert (await et.emergency_lineup_swap("Nobody", "MID12"))["status"] == "refused"


async def test_audit_log_must_be_writable_or_nothing_is_sent(env, proposals):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.monkeypatch.setattr(proposals.audit, "record", lambda *a, **k: False)
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and "audit" in out["reason"].lower()
    assert env.fpl.posts == [] and count_emergency_slots(get_settings().audit_dir, 42, 12) == 0


async def test_dry_runs_never_consume_the_emergency_cap(env):
    for _ in range(4):  # more than the default cap of 2
        assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "dry_run"
    assert count_emergency_slots(get_settings().audit_dir, 42, 12) == 0
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "executed"  # still available


async def test_per_gameweek_cap_survives_restart(env):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.monkeypatch.setenv("FPL_EMERGENCY_MAX_PER_GAMEWEEK", "1")
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "executed"
    again = await et.emergency_lineup_swap("MID8", "MID12")
    assert again["status"] == "refused" and "limit" in again["reason"]
    assert len(env.fpl.posts) == 1
    # the cap lives on disk, not in memory
    assert claim_emergency_slot(get_settings().audit_dir, 42, 12, 1) is None
    assert claim_emergency_slot(get_settings().audit_dir, 42, 13, 1) == 1  # next gameweek is fresh


async def test_dry_run_reports_a_reached_cap_without_consuming(env):
    env.monkeypatch.setenv("FPL_EMERGENCY_MAX_PER_GAMEWEEK", "1")
    assert claim_emergency_slot(get_settings().audit_dir, 42, 12, 1) == 1
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and "limit" in out["reason"]


async def test_squad_changing_before_the_post_aborts_without_sending(env):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.fpl.drift_from_call = 2  # 1st read = planning, 2nd read = the pre-send re-check
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and "changed" in out["reason"]
    assert env.fpl.posts == [] and count_emergency_slots(get_settings().audit_dir, 42, 12) == 0
    assert "emergency_aborted_drift" in _audit_text(get_settings())


async def test_fpl_rejection_is_failed_slot_burned_never_retried(env):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.monkeypatch.setenv("FPL_EMERGENCY_MAX_PER_GAMEWEEK", "2")
    env.fpl.fail_with = FPLAPIError(400, "Deadline passed")
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "refused" and "Deadline" in out["reason"]
    assert len(env.fpl.posts) == 1
    assert "emergency_failed" in _audit_text(get_settings())
    assert claim_emergency_slot(get_settings().audit_dir, 42, 12, 2) == 2  # slot 1 was burned


@pytest.mark.parametrize("error", [FPLAPIError(502, "bad gateway"), RuntimeError("boom"), TimeoutError()])
async def test_ambiguous_post_outcome_is_reported_unknown_not_failed(env, error):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.fpl.fail_with = error
    out = await et.emergency_lineup_swap("MID8", "MID12")
    assert out["status"] == "outcome_unknown" and "FPL website" in out["reason"]
    assert len(env.fpl.posts) == 1  # never retried
    assert "emergency_outcome_unknown" in _audit_text(get_settings())


async def test_unauthenticated_is_refused(env):
    env.monkeypatch.setattr(wt, "_get_client", lambda: None)
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "refused"


async def test_policy_reports_emergency_state(env):
    policy = await wt.get_write_policy()
    assert policy["emergency_exception_enabled"] is True
    assert "emergency" in policy["rule"]


# ------------------------------------------------- shared lock: no lost updates
async def test_emergency_and_approved_execution_never_overwrite_each_other(env, proposals):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.fpl.apply_posts = True
    env.fpl.yield_to_loop = True
    settings = get_settings()

    proposal = await wt.propose_captain("MID10", "MID9")  # human-approved: captain 9 -> 10
    pid = proposal["proposal_id"]
    proposals.approve(pid, PIN, proposals.get(pid).nonce, settings)

    first, second = await asyncio.gather(
        wt.execute_approved_proposal(pid),
        et.emergency_lineup_swap("MID8", "MID12"),
    )
    final = {r["element"]: r for r in env.fpl.picks}
    assert env.fpl.max_inflight == 1  # the two writers were never in flight together
    if first["status"] == "executed":
        assert final[10]["is_captain"] and not final[9]["is_captain"]  # approved change survives
    if second["status"] == "executed":
        assert final[12]["position"] <= 11 and final[8]["position"] >= 12  # swap is in effect
    assert first["status"] == "executed" and second["status"] == "executed"  # lock => sequential, both apply


async def test_second_writer_acting_on_a_changed_squad_is_refused(env, proposals):
    env.monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    env.fpl.apply_posts = True
    settings = get_settings()
    proposal = await wt.propose_captain("MID10", "MID9")
    pid = proposal["proposal_id"]
    proposals.approve(pid, PIN, proposals.get(pid).nonce, settings)
    assert (await et.emergency_lineup_swap("MID8", "MID12"))["status"] == "executed"  # moves positions
    stale = await wt.execute_approved_proposal(pid)  # approved against the OLD lineup
    assert stale["status"] == "refused" and "changed" in stale["reason"]
    assert len(env.fpl.posts) == 1


# ------------------------------------------------------------------ pure helpers
def _pi(status, chance=None):
    return PlayerInfo(1, "X", 1, "C", 3, 50, status=status, chance_next=chance)


def test_classification_is_conservative():
    assert classify_unavailable(_pi("s")) == "suspended"
    assert classify_unavailable(_pi("i", 0)) == "ruled_out"
    assert classify_unavailable(_pi("i")) == "doubtful"  # no explicit 0% => not "ruled out"
    assert classify_unavailable(_pi("n", 0)) == "ruled_out"
    assert classify_unavailable(_pi("a")) == "available"
    assert classify_unavailable(_pi("?")) == "unknown"
    assert classify_replacement(_pi("a")) == "available"
    assert classify_replacement(_pi("a", 100)) == "available"
    assert classify_replacement(_pi("a", 75)) == "unavailable"
    assert classify_replacement(_pi("d")) == "unavailable"


def test_freshness_rules_including_a_fast_local_clock():
    fresh = ServerClock(NOW, NOW, 10)
    assert data_is_fresh(fresh, NOW)[0]
    assert not data_is_fresh(fresh, NOW + timedelta(seconds=300))[0]  # snapshot too old
    assert not data_is_fresh(fresh, NOW - timedelta(seconds=300))[0]  # local clock jumped back
    assert not data_is_fresh(fresh, datetime(2026, 11, 21, 10, 45))[0]  # naive now
    assert not data_is_fresh(None, NOW)[0]
    # skew is symmetric: a local clock AHEAD of FPL's must be rejected just like one behind it
    assert not data_is_fresh(ServerClock(NOW - timedelta(seconds=90), NOW, 0), NOW)[0]
    assert not data_is_fresh(ServerClock(NOW + timedelta(seconds=90), NOW, 0), NOW)[0]
    assert data_is_fresh(ServerClock(NOW - timedelta(seconds=30), NOW, 0), NOW)[0]  # at the limit


def test_chips_known_inactive_is_strict():
    assert chips_known_inactive([{"name": "a", "status_for_entry": "available"}])
    assert chips_known_inactive([{"name": "a", "status_for_entry": "played"}])
    for bad in (None, [], {}, "x", [{}], [1], [{"status_for_entry": 3}],
                [{"status_for_entry": "active"}], [{"status_for_entry": "available", "is_pending": True}]):
        assert not chips_known_inactive(bad), bad


def test_parse_deadline():
    assert parse_deadline("2026-11-21T11:00:00Z") == datetime(2026, 11, 21, 11, 0, tzinfo=UTC)
    assert parse_deadline("garbage") is None and parse_deadline(None) is None
    assert parse_deadline("2026-11-21T11:00:00").tzinfo is not None


def test_plan_swap_refuses_for_the_right_reason(world):
    args = {"picks": world["picks"], "players": world["players"]}
    ok = plan_swap(**args, out_id=8, in_id=12)
    assert ok.ordered_ids.index(12) == 5 and ok.ordered_ids.index(8) == 13  # slots exchanged
    assert ok.captain_id == 9 and ok.vice_id == 10
    with pytest.raises(RuleViolation, match="not in your starting XI"):
        plan_swap(**args, out_id=12, in_id=7)  # a BENCH player as "out"
    with pytest.raises(RuleViolation, match="not on your bench"):
        plan_swap(**args, out_id=8, in_id=11)  # a STARTER as "in"
    with pytest.raises(RuleViolation, match="not in your starting XI"):
        plan_swap(**args, out_id=12, in_id=8)  # reversed
    with pytest.raises(RuleViolation, match="captain or vice-captain"):
        plan_swap(**args, out_id=9, in_id=12)
    with pytest.raises(RuleViolation, match="captain or vice-captain"):
        plan_swap(**args, out_id=10, in_id=12)
    with pytest.raises(RuleViolation, match="15-man"):
        plan_swap(picks=world["picks"][:14], players=world["players"], out_id=8, in_id=12)
