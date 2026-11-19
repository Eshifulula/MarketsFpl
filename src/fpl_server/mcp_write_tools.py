"""Approval-gated tools that change the FPL team.

Every tool here either only PROPOSES (and returns a link for the human) or EXECUTES a proposal that
a human has already approved on the local approval page. There is deliberately no tool that
approves anything: approval needs the operator PIN, typed into the local web page.

Execution is additionally gated by FPL_ALLOW_WRITES (default off => dry run) and re-checks the squad
immediately before sending (drift guard).
"""

from __future__ import annotations

import logging

from .approvals import (
    DRY_RUN,
    EXECUTED,
    FAILED,
    ApprovalError,
    Proposal,
    squad_fingerprint,
)
from .client import FPLAPIError, FPLAuthExpired
from .config import Settings, get_settings
from .mcp_tools import _get_client, mcp
from .rules import (
    TEAM_CHIPS,
    TRANSFER_CHIPS,
    PlayerInfo,
    RuleViolation,
    describe,
    fmt_money,
    normalize_chip,
    resolve_player,
    validate_chip,
    validate_lineup,
    validate_transfers,
)
from .runtime import get_proposals
from .state import store

logger = logging.getLogger("fpl_write_tools")


# --------------------------------------------------------------------------- context ------
class _Context:
    def __init__(self, client, entry_id: int, my_team: dict, event: int, players: dict[int, PlayerInfo]):
        self.client = client
        self.entry_id = entry_id
        self.my_team = my_team
        self.event = event
        self.players = players

    @property
    def picks(self) -> list[dict]:
        return self.my_team.get("picks") or []

    @property
    def bank(self) -> int | None:
        return (self.my_team.get("transfers") or {}).get("bank")

    @property
    def free_transfers(self) -> int | None:
        transfers = self.my_team.get("transfers") or {}
        limit, made = transfers.get("limit"), transfers.get("made")
        if limit is None:
            return None
        return max(0, int(limit) - int(made or 0))

    @property
    def fingerprint(self) -> str:
        return squad_fingerprint(self.picks, self.bank, self.event)


def _players_from_bootstrap() -> dict[int, PlayerInfo]:
    data = store.bootstrap_data
    teams = {t.id: t.name for t in data.teams}
    out: dict[int, PlayerInfo] = {}
    for e in data.elements:
        aliases = {
            f"{e.first_name} {e.second_name}",
            e.second_name,
            f"{e.first_name} {e.web_name}",
        }
        out[e.id] = PlayerInfo(
            id=e.id,
            name=e.web_name,
            team_id=e.team,
            team_name=teams.get(e.team, "Unknown"),
            position=e.element_type,
            price=e.now_cost,
            status=e.status,
            news=e.news or "",
            aliases=tuple(sorted(aliases)),
        )
    return out


async def _load_context() -> _Context:
    client = _get_client()
    if client is None:
        raise RuleViolation("Not authenticated. Call login_to_fpl first.")
    await store.refresh_bootstrap_data(client)  # fresh prices/availability for anything we write
    entry_id = store.get_user_entry_id(client)
    if not entry_id:
        raise RuleViolation("Could not determine your FPL entry id.")
    event = next((e.id for e in store.bootstrap_data.events if e.is_next), None)
    if event is None:
        raise RuleViolation("No upcoming gameweek found - the season may be over.")
    my_team = await client.get_my_team(entry_id)
    if not (my_team.get("picks")):
        raise RuleViolation("Your squad is unavailable (season not started?).")
    return _Context(client, int(entry_id), my_team, int(event), _players_from_bootstrap())


def _refuse(reason: str) -> dict:
    return {"status": "refused", "reason": reason}


def _proposal_response(proposal: Proposal, settings: Settings) -> dict:
    data = proposal.public()
    data["approval_url"] = f"{settings.public_web_base_url}/approve/{proposal.id}"
    data["live_writes_enabled"] = settings.allow_writes
    data["next_step"] = (
        "Show the summary to the user. They must open approval_url and enter their PIN to "
        "approve (you cannot approve). After they say they approved, call "
        "execute_approved_proposal with this proposal_id."
    )
    return data


def _safe_error(exc: Exception) -> dict:
    if isinstance(exc, FPLAuthExpired):
        return _refuse("Your FPL session expired. Call login_to_fpl and sign in again.")
    if isinstance(exc, FPLAPIError):
        return _refuse(f"FPL rejected the request ({exc.status}): {exc.body}")
    logger.exception("write tool failed")
    return _refuse("Unexpected error while preparing the proposal. See the server log.")


# --------------------------------------------------------------------------- tools --------
@mcp.tool()
async def get_write_policy() -> dict:
    """Show the active safety policy for team changes (limits, dry-run state)."""
    s = get_settings()
    return {
        "live_writes_enabled": s.allow_writes,
        "mode": "LIVE" if s.allow_writes else "DRY_RUN (approved changes are only recorded)",
        "approvals_configured": s.approval_pin is not None,
        "max_transfers_per_proposal": s.max_transfers,
        "max_points_hit": s.max_points_hit,
        "proposal_expires_after_seconds": s.proposal_ttl_seconds,
        "execute_within_seconds_of_approval": s.execute_window_seconds,
        "rule": "Every change needs a human to approve it with a PIN on the local approval page.",
    }


@mcp.tool()
async def propose_transfers(
    players_out: list[str],
    players_in: list[str],
    chip: str | None = None,
) -> dict:
    """Propose transfers. Does NOT change the team - the user must approve it first.

    players_out / players_in: equal-length lists, paired by index and by position. Each entry is a
    player id or an EXACT name (web name, full name or surname). Ambiguous or unknown names are
    refused, never guessed. Optional chip: 'wildcard' or 'freehit' (activated with the transfers).
    """
    settings = get_settings()
    try:
        ctx = await _load_context()
        owned = {int(p["element"]) for p in ctx.picks}
        out_ids = [resolve_player(t, ctx.players, only_ids=owned) for t in players_out]
        in_ids = [resolve_player(t, ctx.players) for t in players_in]
        norm_chip = normalize_chip(chip)
        if norm_chip in TRANSFER_CHIPS:
            validate_chip(chip=norm_chip, chips=ctx.my_team.get("chips"), event=ctx.event)
        plan = validate_transfers(
            picks=ctx.picks,
            players=ctx.players,
            bank=ctx.bank,
            free_transfers=ctx.free_transfers,
            out_ids=out_ids,
            in_ids=in_ids,
            chip=norm_chip,
            max_transfers=settings.max_transfers,
            max_points_hit=settings.max_points_hit,
        )
    except RuleViolation as exc:
        return _refuse(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)

    summary = [
        f"OUT {describe(ctx.players[t['element_out']])} sold at {fmt_money(t['selling_price'])}  ->  "
        f"IN {describe(ctx.players[t['element_in']])}"
        for t in plan.transfers
    ]
    summary.append(f"Bank: {fmt_money(plan.bank_before)} -> {fmt_money(plan.bank_after)}")
    summary.append(
        f"Points hit: -{plan.points_hit}"
        + (f" ({plan.transfers_paid_for} paid transfer(s))" if plan.points_hit else "")
    )
    summary.append(f"Chip: {plan.chip or 'none'}")
    payload = {
        "chip": plan.chip,
        "entry": ctx.entry_id,
        "event": ctx.event,
        "transfers": plan.transfers,
        "wildcard": plan.chip == "wildcard",
        "freehit": plan.chip == "freehit",
    }
    proposal = get_proposals().create(
        settings=settings,
        kind="transfers",
        entry_id=ctx.entry_id,
        event=ctx.event,
        endpoint="transfers/",
        payload=payload,
        summary=summary,
        warnings=plan.warnings,
        fingerprint=ctx.fingerprint,
    )
    return _proposal_response(proposal, settings)


@mcp.tool()
async def propose_captain(captain: str, vice_captain: str) -> dict:
    """Propose a new captain / vice-captain (both must be in the starting XI).
    Does NOT change the team - the user must approve it first."""
    settings = get_settings()
    try:
        ctx = await _load_context()
        ordered = [int(p["element"]) for p in sorted(ctx.picks, key=lambda p: p["position"])]
        ids = set(ordered)
        cap = resolve_player(captain, ctx.players, only_ids=ids)
        vice = resolve_player(vice_captain, ctx.players, only_ids=ids)
        plan = validate_lineup(
            current_ids=ids,
            ordered_ids=ordered,
            captain_id=cap,
            vice_id=vice,
            players=ctx.players,
            keep_order=True,
        )
    except RuleViolation as exc:
        return _refuse(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)

    summary = [
        f"Captain: {describe(ctx.players[cap])}",
        f"Vice-captain: {describe(ctx.players[vice])}",
        "Lineup order is unchanged.",
    ]
    proposal = get_proposals().create(
        settings=settings,
        kind="captain",
        entry_id=ctx.entry_id,
        event=ctx.event,
        endpoint=f"my-team/{ctx.entry_id}/",
        payload={"chip": None, "picks": plan.picks},
        summary=summary,
        warnings=plan.warnings,
        fingerprint=ctx.fingerprint,
    )
    return _proposal_response(proposal, settings)


@mcp.tool()
async def propose_lineup(
    starting_xi: list[str],
    bench: list[str],
    captain: str,
    vice_captain: str,
) -> dict:
    """Propose a starting XI and bench order. Does NOT change the team - the user must approve it.

    starting_xi: exactly 11 players (id or exact name). bench: exactly 4, in substitution order,
    FIRST ONE MUST BE THE SUBSTITUTE GOALKEEPER. Captain/vice must be in the XI.
    """
    settings = get_settings()
    try:
        ctx = await _load_context()
        ids = {int(p["element"]) for p in ctx.picks}
        xi = [resolve_player(t, ctx.players, only_ids=ids) for t in starting_xi]
        bn = [resolve_player(t, ctx.players, only_ids=ids) for t in bench]
        cap = resolve_player(captain, ctx.players, only_ids=ids)
        vice = resolve_player(vice_captain, ctx.players, only_ids=ids)
        if len(xi) != 11 or len(bn) != 4:
            raise RuleViolation("Need exactly 11 starters and 4 bench players.")
        plan = validate_lineup(
            current_ids=ids,
            ordered_ids=xi + bn,
            captain_id=cap,
            vice_id=vice,
            players=ctx.players,
        )
    except RuleViolation as exc:
        return _refuse(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)

    by_pos = {r["position"]: r for r in plan.picks}
    summary = ["Starting XI:"] + [
        f"  {n}. {describe(ctx.players[by_pos[n]['element']])}"
        + (" (C)" if by_pos[n]["is_captain"] else " (VC)" if by_pos[n]["is_vice_captain"] else "")
        for n in range(1, 12)
    ]
    summary.append("Bench:")
    summary += [f"  {n}. {describe(ctx.players[by_pos[n]['element']])}" for n in range(12, 16)]
    proposal = get_proposals().create(
        settings=settings,
        kind="lineup",
        entry_id=ctx.entry_id,
        event=ctx.event,
        endpoint=f"my-team/{ctx.entry_id}/",
        payload={"chip": None, "picks": plan.picks},
        summary=summary,
        warnings=plan.warnings,
        fingerprint=ctx.fingerprint,
    )
    return _proposal_response(proposal, settings)


@mcp.tool()
async def propose_chip(chip: str) -> dict:
    """Propose activating Bench Boost ('bboost') or Triple Captain ('3xc') for the next gameweek.
    For Wildcard / Free Hit use propose_transfers(chip=...). Needs user approval first."""
    settings = get_settings()
    try:
        name = normalize_chip(chip)
        if name in TRANSFER_CHIPS:
            raise RuleViolation(
                f"'{name}' is activated together with transfers: use propose_transfers(chip='{name}')."
            )
        if name not in TEAM_CHIPS:
            raise RuleViolation("Use 'bboost' or '3xc'.")
        ctx = await _load_context()
        validate_chip(chip=name, chips=ctx.my_team.get("chips"), event=ctx.event)
        picks = [
            {
                "element": int(p["element"]),
                "position": int(p["position"]),
                "is_captain": bool(p.get("is_captain")),
                "is_vice_captain": bool(p.get("is_vice_captain")),
            }
            for p in sorted(ctx.picks, key=lambda p: p["position"])
        ]
    except RuleViolation as exc:
        return _refuse(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)

    label = "Bench Boost" if name == "bboost" else "Triple Captain"
    proposal = get_proposals().create(
        settings=settings,
        kind="chip",
        entry_id=ctx.entry_id,
        event=ctx.event,
        endpoint=f"my-team/{ctx.entry_id}/",
        payload={"chip": name, "picks": picks},
        summary=[f"Activate {label} for GW{ctx.event}", "Lineup order is unchanged."],
        warnings=["Chips are single-use per half-season and cannot be undone once played."],
        fingerprint=ctx.fingerprint,
    )
    return _proposal_response(proposal, settings)


@mcp.tool()
async def get_proposal(proposal_id: str) -> dict:
    """Check the status of a proposal (pending / approved / executed / dry_run / ...)."""
    proposal = get_proposals().get(proposal_id)
    return proposal.public() if proposal else _refuse("Unknown proposal.")


@mcp.tool()
async def list_proposals() -> list[dict]:
    """List recent proposals and their statuses."""
    return [p.public() for p in get_proposals().list()[:10]]


@mcp.tool()
async def cancel_proposal(proposal_id: str) -> dict:
    """Cancel a pending proposal."""
    try:
        return get_proposals().reject(proposal_id).public()
    except ApprovalError as exc:
        return _refuse(str(exc))


@mcp.tool()
async def execute_approved_proposal(proposal_id: str) -> dict:
    """Execute a proposal the USER has already approved on the approval page.

    Refuses if it is not approved, expired, already executed, or if the squad changed since the
    proposal was made. With live writes disabled it records a dry run and sends nothing.
    """
    settings = get_settings()
    proposals = get_proposals()
    try:
        proposal = proposals.begin_execute(proposal_id, settings)
    except ApprovalError as exc:
        return _refuse(str(exc))

    try:
        client = _get_client()
        if client is None:
            proposals.finish(proposal.id, FAILED, "not authenticated")
            return _refuse("Not authenticated. Call login_to_fpl first.")

        # Drift guard: the squad / bank / gameweek must be exactly as it was when proposed.
        entry_id = store.get_user_entry_id(client)
        my_team = await client.get_my_team(entry_id)
        event = next((e.id for e in store.bootstrap_data.events if e.is_next), None)
        transfers = my_team.get("transfers") or {}
        now_fp = squad_fingerprint(my_team.get("picks") or [], transfers.get("bank"), event)
        if entry_id != proposal.entry_id or event != proposal.event or now_fp != proposal.fingerprint:
            proposals.finish(proposal.id, FAILED, "squad changed since proposal; blocked")
            return _refuse("Your squad, bank or gameweek changed since this was proposed. Create a new proposal.")

        if not settings.allow_writes:
            note = f"DRY RUN: would POST to {proposal.endpoint}"
            proposals.finish(proposal.id, DRY_RUN, note)
            return {"status": DRY_RUN, "note": note, "would_send": proposal.payload}

        if proposal.endpoint == "transfers/":
            response = await client.post_transfers(proposal.payload)
        else:
            response = await client.post_my_team(proposal.entry_id, proposal.payload)
        proposals.finish(proposal.id, EXECUTED, "accepted by FPL")
        return {"status": EXECUTED, "fpl_response": response}
    except (FPLAuthExpired, FPLAPIError) as exc:
        proposals.finish(proposal.id, FAILED, str(exc)[:300])
        return _safe_error(exc)
    except Exception:  # noqa: BLE001
        logger.exception("execution failed")
        proposals.finish(proposal.id, FAILED, "unexpected error")
        return _refuse("Unexpected error during execution. Check your FPL team before retrying.")
