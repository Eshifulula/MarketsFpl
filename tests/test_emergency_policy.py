"""Emergency-correction policy: the only change allowed to skip the normal approval step.

Spec supplied by the project owner. Adaptations: import path is fpl_server.safety, and TEAM_ID is a
placeholder because this repository is public.
"""

from datetime import datetime, timedelta, timezone

import pytest

from fpl_server.safety import SafetyPolicy

NOW = datetime(2026, 11, 21, 10, 45, tzinfo=timezone.utc)
TEAM_ID = 1000001  # placeholder, not a real manager id


@pytest.fixture
def policy():
    return SafetyPolicy()


@pytest.fixture
def valid_emergency():
    return {
        "team_id": TEAM_ID,
        "gameweek": 12,
        "action": "lineup_change",
        "mode": "emergency",
        "now": NOW,
        "deadline": NOW + timedelta(minutes=15),
        "deadline_verified": True,
        "player_status": "suspended",
        "player_status_verified": True,
        "replacement_status": "available",
        "replacement_verified": True,
        "legal_formation": True,
        "points_hit": 0,
        "uses_chip": False,
    }


def test_emergency_allowed_at_15_minutes(policy, valid_emergency):
    decision = policy.evaluate(valid_emergency)
    assert decision.allowed is True


def test_emergency_denied_before_15_minute_window(policy, valid_emergency):
    valid_emergency["deadline"] = NOW + timedelta(minutes=15, seconds=1)

    assert not policy.evaluate(valid_emergency).allowed


def test_emergency_denied_after_deadline(policy, valid_emergency):
    valid_emergency["deadline"] = NOW

    assert not policy.evaluate(valid_emergency).allowed


def test_unverified_deadline_denied(policy, valid_emergency):
    valid_emergency["deadline_verified"] = False

    assert not policy.evaluate(valid_emergency).allowed


@pytest.mark.parametrize("status", ["suspended", "ruled_out"])
def test_confirmed_unavailable_players(policy, valid_emergency, status):
    valid_emergency["player_status"] = status

    assert policy.evaluate(valid_emergency).allowed


@pytest.mark.parametrize("status", ["available", "doubtful", "unknown"])
def test_unconfirmed_unavailability_denied(policy, valid_emergency, status):
    valid_emergency["player_status"] = status

    assert not policy.evaluate(valid_emergency).allowed


def test_unverified_injury_information_denied(policy, valid_emergency):
    valid_emergency["player_status_verified"] = False

    assert not policy.evaluate(valid_emergency).allowed


def test_unavailable_replacement_denied(policy, valid_emergency):
    valid_emergency["replacement_status"] = "ruled_out"

    assert not policy.evaluate(valid_emergency).allowed


def test_unverified_replacement_denied(policy, valid_emergency):
    valid_emergency["replacement_verified"] = False

    assert not policy.evaluate(valid_emergency).allowed


def test_illegal_formation_denied(policy, valid_emergency):
    valid_emergency["legal_formation"] = False

    assert not policy.evaluate(valid_emergency).allowed


@pytest.mark.parametrize("action", ["transfer", "activate_chip", "wildcard"])
def test_emergency_cannot_perform_restricted_actions(policy, valid_emergency, action):
    valid_emergency["action"] = action

    assert not policy.evaluate(valid_emergency).allowed


def test_emergency_cannot_take_points_hit(policy, valid_emergency):
    valid_emergency["points_hit"] = 4

    assert not policy.evaluate(valid_emergency).allowed


def test_emergency_cannot_activate_chip(policy, valid_emergency):
    valid_emergency["uses_chip"] = True

    assert not policy.evaluate(valid_emergency).allowed


def test_missing_critical_information_fails_closed(policy, valid_emergency):
    del valid_emergency["deadline_verified"]

    assert not policy.evaluate(valid_emergency).allowed


# ---- additions beyond the supplied spec (fail-closed edge cases) -----------------------------
def test_non_emergency_mode_is_not_covered_by_the_exception(policy, valid_emergency):
    valid_emergency["mode"] = "normal"

    assert not policy.evaluate(valid_emergency).allowed


def test_truthy_but_not_true_flags_are_denied(policy, valid_emergency):
    for key in ("deadline_verified", "player_status_verified", "replacement_verified", "legal_formation"):
        request = {**valid_emergency, key: "yes"}
        assert not policy.evaluate(request).allowed, key
    assert not policy.evaluate({**valid_emergency, "uses_chip": 0}).allowed


def test_naive_timestamps_are_denied(policy, valid_emergency):
    valid_emergency["now"] = valid_emergency["now"].replace(tzinfo=None)

    assert not policy.evaluate(valid_emergency).allowed
