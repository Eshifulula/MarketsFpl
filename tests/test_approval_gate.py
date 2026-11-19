"""One-time, action-bound approval tokens.

Spec supplied by the project owner. Adaptations: import path is fpl_server.safety, and the team ids
are placeholders because this repository is public. The final section adds tamper / restart /
concurrency cases beyond the supplied spec.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from fpl_server.safety import ApprovalGate

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
SECRET = b"test-only-signing-secret"  # test value, not a real secret


@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(
        secret=SECRET,
        database_path=tmp_path / "approvals.db",
    )


@pytest.fixture
def transfer():
    return {
        "team_id": 1000001,
        "gameweek": 12,
        "action": "transfer",
        "player_out": 101,
        "player_in": 202,
        "points_hit": 0,
    }


def test_transfer_requires_approval(gate, transfer):
    decision = gate.authorize(
        action=transfer,
        approval_token=None,
        now=NOW,
    )

    assert not decision.allowed


def test_valid_approval_authorizes_action(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    decision = gate.authorize(
        action=transfer,
        approval_token=token,
        now=NOW + timedelta(seconds=30),
    )

    assert decision.allowed


def test_expired_approval_rejected(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    decision = gate.authorize(
        action=transfer,
        approval_token=token,
        now=NOW + timedelta(seconds=121),
    )

    assert not decision.allowed


def test_approval_cannot_be_reused(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    first = gate.authorize(
        action=transfer,
        approval_token=token,
        now=NOW + timedelta(seconds=10),
    )

    second = gate.authorize(
        action=transfer,
        approval_token=token,
        now=NOW + timedelta(seconds=11),
    )

    assert first.allowed
    assert not second.allowed


def test_approval_bound_to_specific_team(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    other_team = {
        **transfer,
        "team_id": 1234567,
    }

    decision = gate.authorize(
        action=other_team,
        approval_token=token,
        now=NOW,
    )

    assert not decision.allowed


def test_approval_bound_to_specific_gameweek(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    different_gameweek = {
        **transfer,
        "gameweek": 13,
    }

    decision = gate.authorize(
        action=different_gameweek,
        approval_token=token,
        now=NOW,
    )

    assert not decision.allowed


def test_approval_cannot_authorize_different_transfer(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    modified_transfer = {
        **transfer,
        "player_in": 303,
    }

    decision = gate.authorize(
        action=modified_transfer,
        approval_token=token,
        now=NOW,
    )

    assert not decision.allowed


def test_points_hit_cannot_be_added_after_approval(gate, transfer):
    token = gate.issue(
        action=transfer,
        now=NOW,
        ttl_seconds=120,
    )

    modified_transfer = {
        **transfer,
        "points_hit": 4,
    }

    decision = gate.authorize(
        action=modified_transfer,
        approval_token=token,
        now=NOW,
    )

    assert not decision.allowed


# ---- additions beyond the supplied spec -------------------------------------------------------
def test_forged_or_garbled_tokens_rejected(gate, transfer):
    good = gate.issue(action=transfer, now=NOW, ttl_seconds=120)
    body, _, _sig = good.partition(".")
    for bad in ("", "garbage", "a.b", f"{body}.AAAA", f"{good}x", good.replace(".", "")):
        assert not gate.authorize(action=transfer, approval_token=bad, now=NOW).allowed


def test_token_from_another_secret_rejected(tmp_path, gate, transfer):
    other = ApprovalGate(secret=b"a-completely-different-secret", database_path=tmp_path / "o.db")
    token = other.issue(action=transfer, now=NOW, ttl_seconds=120)

    assert not gate.authorize(action=transfer, approval_token=token, now=NOW).allowed


def test_spent_approval_stays_spent_after_restart(tmp_path, transfer):
    db = tmp_path / "persist.db"
    first = ApprovalGate(secret=SECRET, database_path=db)
    token = first.issue(action=transfer, now=NOW, ttl_seconds=120)
    assert first.authorize(action=transfer, approval_token=token, now=NOW).allowed

    restarted = ApprovalGate(secret=SECRET, database_path=db)

    assert not restarted.authorize(action=transfer, approval_token=token, now=NOW).allowed


def test_mismatched_attempt_does_not_burn_the_real_approval(gate, transfer):
    token = gate.issue(action=transfer, now=NOW, ttl_seconds=120)
    tampered = {**transfer, "player_in": 303}

    assert not gate.authorize(action=tampered, approval_token=token, now=NOW).allowed
    assert gate.authorize(action=transfer, approval_token=token, now=NOW).allowed


def test_simultaneous_attempts_only_one_executes(gate, transfer):
    token = gate.issue(action=transfer, now=NOW, ttl_seconds=120)
    results = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        results.append(gate.authorize(action=transfer, approval_token=token, now=NOW).allowed)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1 and results.count(False) == 7


def test_weak_secret_and_naive_time_refused(tmp_path, transfer):
    with pytest.raises(ValueError):
        ApprovalGate(secret=b"short", database_path=tmp_path / "x.db")
    gate = ApprovalGate(secret=SECRET, database_path=tmp_path / "y.db")
    with pytest.raises(ValueError):
        gate.issue(action=transfer, now=datetime(2026, 11, 21, 10, 0), ttl_seconds=60)
    with pytest.raises(ValueError):
        gate.issue(action=transfer, now=NOW, ttl_seconds=0)
