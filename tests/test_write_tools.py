"""End-to-end tests of the propose -> human approve -> execute pipeline using a fake FPL."""

from types import SimpleNamespace

import pytest
from conftest import PIN

from fpl_server import mcp_write_tools as wt
from fpl_server.client import FPLAPIError
from fpl_server.state import store


class FakeFPL:
    user_info = {"player": {"entry": 42}}

    def __init__(self, picks):
        self.picks = picks
        self.bank = 10
        self.chips = [
            {"name": "bboost", "status_for_entry": "available", "start_event": 1, "stop_event": 38},
            {"name": "wildcard", "status_for_entry": "available"},
            {"name": "3xc", "status_for_entry": "played"},
        ]
        self.posts = []
        self.fail_with = None

    async def get_my_team(self, entry_id):
        return {
            "picks": self.picks,
            "transfers": {"bank": self.bank, "limit": 1, "made": 0, "value": 1000},
            "chips": self.chips,
        }

    async def post_transfers(self, payload):
        self.posts.append(("transfers/", payload))
        if self.fail_with:
            raise self.fail_with
        return {"ok": True}

    async def post_my_team(self, entry_id, payload):
        self.posts.append((f"my-team/{entry_id}/", payload))
        return {"ok": True}


@pytest.fixture
def fpl(world, proposals, monkeypatch):
    client = FakeFPL(world["picks"])

    async def refresh(_client):
        return None

    monkeypatch.setattr(wt, "_get_client", lambda: client)
    monkeypatch.setattr(wt, "_players_from_bootstrap", lambda: world["players"])
    monkeypatch.setattr(store, "refresh_bootstrap_data", refresh)
    monkeypatch.setattr(
        store, "bootstrap_data", SimpleNamespace(events=[SimpleNamespace(id=12, is_next=True)]), raising=False
    )
    return client


def _approve(proposals, settings, proposal_id):
    p = proposals.get(proposal_id)
    proposals.approve(proposal_id, PIN, p.nonce, settings)


async def test_propose_returns_link_and_hides_nonce(fpl, proposals, settings):
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    assert r["kind"] == "transfers" and r["status"] == "pending"
    assert r["approval_url"].endswith(f"/approve/{r['proposal_id']}")
    assert proposals.get(r["proposal_id"]).nonce not in str(r)
    assert any("Bank: £1.0m -> £0.5m" in line for line in r["summary"])
    assert fpl.posts == []  # proposing never writes


async def test_cannot_execute_unapproved(fpl, proposals, settings):
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "approved" in out["reason"].lower()
    assert fpl.posts == []


async def test_default_is_dry_run_even_when_approved(fpl, proposals, settings):
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "dry_run" and out["would_send"]["entry"] == 42
    assert fpl.posts == []


async def test_live_execution_posts_exact_payload_once(fpl, proposals, monkeypatch):
    monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    from fpl_server.config import get_settings

    settings = get_settings()
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "executed"
    assert fpl.posts == [
        (
            "transfers/",
            {
                "chip": None,
                "entry": 42,
                "event": 12,
                "transfers": [
                    {"element_out": 8, "element_in": 104, "selling_price": 50, "purchase_price": 55}
                ],
                "wildcard": False,
                "freehit": False,
            },
        )
    ]
    again = await wt.execute_approved_proposal(r["proposal_id"])
    assert again["status"] == "refused" and len(fpl.posts) == 1  # single use


async def test_drift_guard_blocks_if_squad_changed(fpl, proposals, monkeypatch):
    monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    from fpl_server.config import get_settings

    settings = get_settings()
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    fpl.bank = 99  # e.g. a price change or a manual edit on the FPL website
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "changed" in out["reason"]
    assert fpl.posts == []


async def test_fpl_rejection_is_reported_and_never_retried(fpl, proposals, monkeypatch):
    monkeypatch.setenv("FPL_ALLOW_WRITES", "true")
    from fpl_server.config import get_settings

    settings = get_settings()
    fpl.fail_with = FPLAPIError(400, "Transfers deadline passed")
    r = await wt.propose_transfers(["MID8"], ["MID104"])
    _approve(proposals, settings, r["proposal_id"])
    out = await wt.execute_approved_proposal(r["proposal_id"])
    assert out["status"] == "refused" and "deadline" in out["reason"]
    assert len(fpl.posts) == 1
    assert proposals.get(r["proposal_id"]).status == "failed"


async def test_bad_names_and_rules_are_refused_not_guessed(fpl):
    assert (await wt.propose_transfers(["Mid"], ["MID104"]))["status"] == "refused"  # typo/partial
    assert (await wt.propose_transfers(["MID8"], ["Expensive"]))["status"] == "refused"  # budget
    assert (await wt.propose_transfers(["MID8"], ["Gone"]))["status"] == "refused"  # unavailable
    assert (await wt.propose_transfers(["MID8"], ["MID104"], chip="bboost"))["status"] == "refused"


async def test_captain_lineup_and_chip_payloads(fpl, proposals, settings):
    cap = await wt.propose_captain("MID10", "MID9")
    payload = proposals.get(cap["proposal_id"]).payload
    assert payload["chip"] is None
    flags = {r["element"]: (r["is_captain"], r["is_vice_captain"]) for r in payload["picks"]}
    assert flags[10] == (True, False) and flags[9] == (False, True)

    chip = await wt.propose_chip("Bench Boost")
    assert proposals.get(chip["proposal_id"]).payload["chip"] == "bboost"
    assert (await wt.propose_chip("wildcard"))["status"] == "refused"  # must go with transfers
    assert (await wt.propose_chip("3xc"))["status"] == "refused"  # already played

    xi = ["GKP1", "DEF3", "DEF4", "DEF5", "DEF6", "MID8", "MID9", "MID10", "MID11", "FWD13", "FWD14"]
    lineup = await wt.propose_lineup(xi, ["GKP2", "DEF7", "MID12", "FWD15"], "MID9", "MID10")
    assert lineup["kind"] == "lineup"
    bad = await wt.propose_lineup(xi, ["DEF7", "GKP2", "MID12", "FWD15"], "MID9", "MID10")
    assert bad["status"] == "refused"


async def test_unauthenticated_is_refused(monkeypatch, proposals):
    monkeypatch.setattr(wt, "_get_client", lambda: None)
    assert (await wt.propose_captain("a", "b"))["status"] == "refused"


async def test_write_policy_reports_dry_run(settings):
    policy = await wt.get_write_policy()
    assert policy["live_writes_enabled"] is False and "DRY_RUN" in policy["mode"]


def test_no_unguarded_write_tool_is_registered():
    import asyncio

    from fpl_server import mcp_tools

    names = {t.name for t in asyncio.run(mcp_tools.mcp.list_tools())}
    assert "make_transfers" not in names
    assert not {n for n in names if "approve" in n and n != "execute_approved_proposal"}
