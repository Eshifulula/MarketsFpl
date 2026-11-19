import json

import pytest
from conftest import PIN, make_proposal

from fpl_server.approvals import (
    APPROVED,
    EXECUTED,
    EXECUTING,
    EXPIRED,
    PENDING,
    REJECTED,
    ApprovalError,
    squad_fingerprint,
)


def test_cannot_execute_without_approval(proposals, settings):
    p = make_proposal(proposals, settings)
    with pytest.raises(ApprovalError, match="Not approved"):
        proposals.begin_execute(p.id, settings)


def test_wrong_pin_never_approves_and_is_audited_without_the_pin(proposals, settings, tmp_path):
    p = make_proposal(proposals, settings)
    with pytest.raises(ApprovalError, match="Incorrect PIN"):
        proposals.approve(p.id, "guess-guess-guess", p.nonce, settings)
    assert proposals.get(p.id).status == PENDING
    log = "".join(f.read_text() for f in (tmp_path / "audit").glob("*.jsonl"))
    assert "approval_bad_pin" in log and "guess-guess-guess" not in log and PIN not in log


def test_lockout_after_repeated_failures_then_recovers(proposals, monkeypatch, clock):
    from fpl_server.config import get_settings

    monkeypatch.setenv("FPL_PROPOSAL_TTL_SECONDS", "7200")  # outlive the 900s lockout
    settings = get_settings()
    p = make_proposal(proposals, settings)
    for _ in range(settings.pin_max_failures):
        with pytest.raises(ApprovalError):
            proposals.approve(p.id, "nope-nope-nope", p.nonce, settings)
    with pytest.raises(ApprovalError, match="locked"):
        proposals.approve(p.id, PIN, p.nonce, settings)  # even the right PIN is refused
    clock.advance(settings.pin_lockout_seconds + 1)
    assert proposals.approve(p.id, PIN, p.nonce, settings).status == APPROVED


def test_bad_nonce_rejected(proposals, settings):
    p = make_proposal(proposals, settings)
    with pytest.raises(ApprovalError, match="form token"):
        proposals.approve(p.id, PIN, "forged", settings)


def test_approvals_disabled_without_pin(proposals, settings, monkeypatch):
    monkeypatch.delenv("FPL_APPROVAL_PIN")
    from fpl_server.config import get_settings

    p = make_proposal(proposals, settings)
    with pytest.raises(ApprovalError, match="disabled"):
        proposals.approve(p.id, PIN, p.nonce, get_settings())


def test_full_happy_path_and_single_use(proposals, settings):
    p = make_proposal(proposals, settings)
    proposals.approve(p.id, PIN, p.nonce, settings)
    claimed = proposals.begin_execute(p.id, settings)
    assert claimed.status == EXECUTING
    with pytest.raises(ApprovalError, match="executing"):
        proposals.begin_execute(p.id, settings)  # no double submit
    proposals.finish(p.id, EXECUTED, "ok")
    with pytest.raises(ApprovalError, match="executed"):
        proposals.begin_execute(p.id, settings)


def test_proposal_expires_before_approval(proposals, settings, clock):
    p = make_proposal(proposals, settings)
    clock.advance(settings.proposal_ttl_seconds + 1)
    with pytest.raises(ApprovalError, match="expired"):
        proposals.approve(p.id, PIN, p.nonce, settings)
    assert proposals.get(p.id).status == EXPIRED


def test_approval_expires_before_execution(proposals, settings, clock):
    p = make_proposal(proposals, settings)
    proposals.approve(p.id, PIN, p.nonce, settings)
    clock.advance(settings.execute_window_seconds + 1)
    with pytest.raises(ApprovalError, match="expired"):
        proposals.begin_execute(p.id, settings)


def test_reject_and_supersede(proposals, settings):
    a = make_proposal(proposals, settings)
    proposals.reject(a.id, a.nonce)
    assert proposals.get(a.id).status == REJECTED
    b = make_proposal(proposals, settings)
    c = make_proposal(proposals, settings)
    assert proposals.get(b.id).status == REJECTED  # only one live proposal at a time
    assert proposals.get(c.id).status == PENDING


def test_public_view_hides_nonce(proposals, settings):
    p = make_proposal(proposals, settings)
    assert p.nonce not in json.dumps(p.public())


def test_fingerprint_changes_when_squad_changes(world):
    base = squad_fingerprint(world["picks"], 10, 12)
    assert base == squad_fingerprint(list(reversed(world["picks"])), 10, 12)
    assert base != squad_fingerprint(world["picks"], 11, 12)
    assert base != squad_fingerprint(world["picks"], 10, 13)
    changed = [dict(p) for p in world["picks"]]
    changed[0]["element"] = 999
    assert base != squad_fingerprint(changed, 10, 12)
