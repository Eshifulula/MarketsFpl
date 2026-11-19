"""Shared fixtures: a fake FPL world (no network, no browser, no credentials)."""

from __future__ import annotations

import os

import pytest

from fpl_server import runtime
from fpl_server.approvals import AuditLog, ProposalStore
from fpl_server.config import Settings, get_settings
from fpl_server.rules import PlayerInfo

PIN = "correct-horse-battery"  # test-only value, not a real secret

_POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _p(pid, pos, team, price=50, status="a", news="", name=None):
    return PlayerInfo(
        id=pid,
        name=name or f"{_POS[pos]}{pid}",
        team_id=team,
        team_name=f"Club{team}",
        position=pos,
        price=price,
        status=status,
        news=news,
    )


@pytest.fixture
def world():
    """15-man legal squad (ids 1-15, one player per club) plus market players (101+)."""
    players = {}
    shape = [(1, 2), (2, 5), (3, 5), (4, 3)]
    pid, team = 1, 1
    for pos, count in shape:
        for _ in range(count):
            players[pid] = _p(pid, pos, team)
            pid += 1
            team += 1
    market = [
        _p(101, 1, 1, price=45),
        _p(102, 2, 3, price=50),
        _p(103, 3, 30, price=200, name="Expensive"),
        _p(104, 3, 31, price=55),
        _p(105, 4, 32, status="u", name="Gone"),
        _p(106, 2, 33, status="d", news="Knock", name="Doubtful"),
        _p(107, 3, 34, price=40, name="Cheap"),
    ]
    for m in market:
        players[m.id] = m
    # A legal 1-4-4-2 lineup, bench GK first (positions 1-11 start, 12-15 bench).
    order = [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 2, 7, 12, 15]
    picks = [
        {
            "element": element,
            "position": position,
            "is_captain": element == 9,
            "is_vice_captain": element == 10,
            "selling_price": 50,
            "purchase_price": 50,
        }
        for position, element in enumerate(order, start=1)
    ]
    return {"players": players, "picks": picks, "bank": 10, "free": 1}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("FPL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FPL_APPROVAL_PIN", PIN)
    monkeypatch.setenv("FPL_AUDIT_DIR", str(tmp_path / "audit"))
    return get_settings()


@pytest.fixture
def clock():
    class Clock:
        now = 1_000_000.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()


@pytest.fixture
def proposals(settings, clock):
    store = ProposalStore(AuditLog(settings.audit_dir), clock=clock)
    runtime.set_proposals(store)
    yield store
    runtime.set_proposals(None)


def make_proposal(store: ProposalStore, settings: Settings, **overrides):
    args = {
        "settings": settings,
        "kind": "transfers",
        "entry_id": 42,
        "event": 12,
        "endpoint": "transfers/",
        "payload": {"entry": 42, "event": 12, "transfers": []},
        "summary": ["OUT a -> IN b"],
        "warnings": [],
        "fingerprint": "fp",
    }
    args.update(overrides)
    return store.create(**args)
