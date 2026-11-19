"""The ONE tool allowed to change the team without a human approval click.

Scope is deliberately tiny: swap a verified suspended / ruled-out STARTER for an available BENCH
player, in the last 15 minutes before a verified deadline, legal formation, no points hit, no chip,
no captaincy change. `safety.SafetyPolicy` makes the decision; this module only gathers the facts.

The model passes two player names. It cannot pass - or influence - any "verified" flag: deadline,
player status, replacement status, chip state and clock agreement are all computed server-side from
fresh official FPL data, and any of them being missing or stale means refusal. Disabled unless
FPL_ALLOW_EMERGENCY=true; still a dry run unless FPL_ALLOW_WRITES=true.

Safety properties of this path:
  * one lock shared with execute_approved_proposal (no two writers on one snapshot);
  * the squad is re-read under that lock right before the POST and must be unchanged;
  * the audit log must be writable, or the swap is refused before anything is sent;
  * at least 60 s must remain before the deadline;
  * a per-gameweek cap that lives on disk and is consumed before sending (never on dry runs);
  * the POST is never retried, and an ambiguous result is reported as UNKNOWN, not "failed".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .approvals import squad_fingerprint
from .client import FPLAPIError, FPLAuthExpired
from .config import get_settings
from .emergency import (
    MIN_SECONDS_BEFORE_DEADLINE,
    chips_known_inactive,
    claim_emergency_slot,
    classify_replacement,
    classify_unavailable,
    count_emergency_slots,
    data_is_fresh,
    parse_deadline,
    plan_swap,
)
from .mcp_tools import mcp
from .mcp_write_tools import UNKNOWN_OUTCOME_MESSAGE, _load_context, _refuse, _safe_error
from .rules import RuleViolation, describe, resolve_player, validate_lineup
from .runtime import execution_lock, get_proposals
from .safety import SafetyPolicy
from .state import store

logger = logging.getLogger("fpl_emergency")

NEXT_STEP = "Use propose_lineup so the user can approve it normally."


def _now() -> datetime:
    """Server clock. Indirected so tests can pin time."""
    return datetime.now(UTC)


def _unknown(audit, gameweek: int, why: str) -> dict:
    audit.record("emergency_outcome_unknown", gameweek=gameweek, why=why[:200])
    return {"status": "outcome_unknown", "reason": UNKNOWN_OUTCOME_MESSAGE}


@mcp.tool()
async def emergency_lineup_swap(player_out: str, player_in: str) -> dict:
    """EMERGENCY lineup fix without approval - only valid in the last 15 minutes before a deadline.

    Swaps ONE starting player who is confirmed suspended or ruled out (per official FPL data) with
    ONE available bench player. Never changes transfers, chips, points or captaincy. The server
    verifies the deadline, availability, chip state and clock itself; you cannot assert any of it.
    If anything is unverified or outside these limits it is refused - use propose_lineup (normal
    approval) then.
    """
    settings = get_settings()
    audit = get_proposals().audit
    async with execution_lock():
        try:
            ctx = await _load_context()  # fresh data, read under the shared lock
            now = _now()
            owned = {int(p["element"]) for p in ctx.picks}
            out_id = resolve_player(player_out, ctx.players, only_ids=owned)
            in_id = resolve_player(player_in, ctx.players, only_ids=owned)
            plan = plan_swap(picks=ctx.picks, players=ctx.players, out_id=out_id, in_id=in_id)
        except RuleViolation as exc:
            return _refuse(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _safe_error(exc)

        # ---- facts, all derived server-side ------------------------------------------------
        event = next((e for e in store.bootstrap_data.events if e.id == ctx.event), None)
        deadline = parse_deadline(getattr(event, "deadline_time", None)) if event else None
        fresh, fresh_note = data_is_fresh(getattr(ctx.client, "bootstrap_clock", None), now)
        deadline_ok = bool(fresh and deadline is not None and not getattr(event, "finished", True))

        try:
            validate_lineup(
                current_ids=owned,
                ordered_ids=plan.ordered_ids,
                captain_id=plan.captain_id,
                vice_id=plan.vice_id,
                players=ctx.players,
                keep_order=True,
            )
            legal, legal_note = True, ""
        except RuleViolation as exc:
            legal, legal_note = False, str(exc)

        request = {
            "team_id": ctx.entry_id,
            "gameweek": ctx.event,
            "action": "lineup_change",
            "mode": "emergency",
            "now": now,
            "deadline": deadline if deadline is not None else now,
            "deadline_verified": deadline_ok,
            "player_status": classify_unavailable(ctx.players[out_id]),
            "player_status_verified": fresh,
            "replacement_status": classify_replacement(ctx.players[in_id]),
            "replacement_verified": fresh,
            "legal_formation": legal,
            "points_hit": 0,
            # Missing / empty / malformed chip data is UNVERIFIED => treated as "chip involved".
            "uses_chip": not chips_known_inactive(ctx.my_team.get("chips")),
        }
        decision = SafetyPolicy().evaluate(request)
        remaining = (deadline - now).total_seconds() if deadline is not None else 0.0
        too_close = decision.allowed and remaining < MIN_SECONDS_BEFORE_DEADLINE
        reason = (
            f"less than {MIN_SECONDS_BEFORE_DEADLINE}s before the deadline: not enough margin to send safely"
            if too_close
            else decision.reason
        )
        summary = [
            f"OUT (starter): {describe(ctx.players[out_id])}",
            f"IN (from bench): {describe(ctx.players[in_id])}",
            "Captain, vice-captain, transfers and chips are unchanged.",
        ]
        written = audit.record(
            "emergency_evaluated",
            gameweek=ctx.event,
            out=ctx.players[out_id].name,
            into=ctx.players[in_id].name,
            allowed=decision.allowed and not too_close,
            reason=reason,
            freshness=fresh_note,
            formation_note=legal_note,
        )
        if not decision.allowed or too_close:
            return {
                "status": "refused",
                "reason": reason + (f" ({legal_note})" if legal_note else ""),
                "next_step": NEXT_STEP,
            }
        if not settings.allow_emergency:
            return {
                "status": "refused",
                "reason": "The emergency exception is switched off (FPL_ALLOW_EMERGENCY).",
                "policy_would_allow": True,
                "next_step": NEXT_STEP,
            }
        if not written:  # an unaudited unapproved change is not acceptable
            return {
                "status": "refused",
                "reason": "The audit log is not writable; refusing to act without a record.",
                "next_step": NEXT_STEP,
            }

        limit = settings.emergency_max_per_gameweek
        picks = [
            {
                "element": pid,
                "position": position,
                "is_captain": pid == plan.captain_id,
                "is_vice_captain": pid == plan.vice_id,
            }
            for position, pid in enumerate(plan.ordered_ids, start=1)
        ]
        payload = {"chip": None, "picks": picks}

        if not settings.allow_writes:
            # Dry run: report the cap but never consume it, so rehearsing cannot lock out the real thing.
            if count_emergency_slots(settings.audit_dir, ctx.entry_id, ctx.event) >= limit:
                return _refuse(f"Emergency limit of {limit} per gameweek reached.")
            audit.record("emergency_dry_run", gameweek=ctx.event, summary=summary)
            return {"status": "dry_run", "summary": summary, "would_send": payload}

        # ---- live: re-verify immediately before sending -----------------------------------
        try:
            latest = await ctx.client.get_my_team(ctx.entry_id)
        except Exception as exc:  # noqa: BLE001
            return _safe_error(exc)
        latest_bank = (latest.get("transfers") or {}).get("bank")
        if squad_fingerprint(latest.get("picks") or [], latest_bank, ctx.event) != ctx.fingerprint:
            audit.record("emergency_aborted_drift", gameweek=ctx.event)
            return _refuse("Your squad changed while this was being prepared. Nothing was sent.")
        if (deadline - _now()).total_seconds() < MIN_SECONDS_BEFORE_DEADLINE:
            audit.record("emergency_aborted_late", gameweek=ctx.event)
            return _refuse("Too close to the deadline to send safely. Nothing was sent.")
        if claim_emergency_slot(settings.audit_dir, ctx.entry_id, ctx.event, limit) is None:
            audit.record("emergency_cap_reached", gameweek=ctx.event)
            return _refuse(f"Emergency limit of {limit} per gameweek reached.")

        try:
            response = await ctx.client.post_my_team(ctx.entry_id, payload)  # never retried
        except FPLAuthExpired as exc:
            audit.record("emergency_failed", gameweek=ctx.event, error=str(exc)[:300])
            return _safe_error(exc)
        except FPLAPIError as exc:
            if exc.status >= 500:  # sent, but FPL errored: it may still have been applied
                return _unknown(audit, ctx.event, f"FPL answered {exc.status}")
            audit.record("emergency_failed", gameweek=ctx.event, error=str(exc)[:300])
            return _safe_error(exc)
        except asyncio.CancelledError:
            _unknown(audit, ctx.event, "call cancelled mid-flight")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("emergency swap: unexpected error after sending")
            return _unknown(audit, ctx.event, "unexpected error after sending")
        audit.record("emergency_executed", gameweek=ctx.event, summary=summary)
        return {"status": "executed", "summary": summary, "fpl_response": response}
