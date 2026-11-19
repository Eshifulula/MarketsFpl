"""Human-in-the-loop approval for every change to the FPL team.

The model can only *propose*. A proposal is executed only if a human has approved it on the local
approval page by entering the operator PIN. The PIN is never an MCP tool parameter, so a model
(or a prompt injection) cannot approve its own proposal.

Guarantees enforced here:
  * what the human approves is rendered from the stored payload, not from model-written text;
  * proposals expire; approvals expire; each proposal executes at most once;
  * PIN checks are constant-time with a global lockout after repeated failures;
  * the squad is re-checked at execution time (drift guard);
  * every step is appended to an audit log (never containing the PIN or any token).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
EXECUTING = "executing"
EXECUTED = "executed"
DRY_RUN = "dry_run"
FAILED = "failed"
UNKNOWN = "outcome_unknown"  # request was sent; FPL may or may not have applied it

_TERMINAL = {REJECTED, EXPIRED, EXECUTED, DRY_RUN, FAILED, UNKNOWN}


class ApprovalError(Exception):
    """Raised for any refused approval / execution step. Messages are safe to show."""


def squad_fingerprint(picks: list[dict], bank: int | None, event: int) -> str:
    """Stable hash of the parts of the squad a proposal depends on."""
    rows = sorted(
        (
            int(p["element"]),
            int(p["position"]),
            bool(p.get("is_captain")),
            bool(p.get("is_vice_captain")),
            p.get("selling_price"),
        )
        for p in picks
    )
    blob = json.dumps({"picks": rows, "bank": bank, "event": event}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class AuditLog:
    """Append-only JSONL audit trail. Contains decisions and summaries, never secrets."""

    def __init__(self, directory: Path):
        self._dir = directory
        self._lock = threading.Lock()

    def record(self, name: str, /, **fields: Any) -> bool:
        """Append one entry. Returns False if it could not be written (callers on the emergency
        path treat that as a refusal)."""
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": name, **fields}
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self._dir, 0o700)
                except OSError:  # pragma: no cover - best effort (Windows)
                    pass
                path = self._dir / f"audit-{datetime.now(UTC):%Y-%m}.audit.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                return True
            except OSError:  # pragma: no cover - callers on the emergency path refuse on False
                return False


@dataclass
class Proposal:
    id: str
    kind: str  # transfers | lineup | captain | chip
    entry_id: int
    event: int
    endpoint: str  # FPL API path the payload will be POSTed to
    payload: dict
    summary: list[str]
    warnings: list[str]
    fingerprint: str
    created_at: float
    expires_at: float
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    status: str = PENDING
    approved_at: float | None = None
    result: str | None = None

    def public(self) -> dict:
        """What is safe to hand back to the model (no nonce)."""
        return {
            "proposal_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "gameweek": self.event,
            "summary": self.summary,
            "warnings": self.warnings,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
            "result": self.result,
        }


class ProposalStore:
    def __init__(
        self,
        audit: AuditLog,
        clock: Callable[[], float] = time.time,
    ):
        self._audit = audit
        self._clock = clock
        self._lock = threading.RLock()
        self._proposals: dict[str, Proposal] = {}
        self._failures = 0
        self._locked_until = 0.0

    @property
    def audit(self) -> AuditLog:
        return self._audit

    # ----- creation / lookup ---------------------------------------------------------
    def create(
        self,
        *,
        settings: Settings,
        kind: str,
        entry_id: int,
        event: int,
        endpoint: str,
        payload: dict,
        summary: list[str],
        warnings: list[str],
        fingerprint: str,
    ) -> Proposal:
        now = self._clock()
        with self._lock:
            self._purge(now)
            for old in self._proposals.values():
                # One live proposal at a time: a newer one cancels both a waiting AND an
                # already-approved-but-unexecuted older one, so two approvals can never race.
                if old.status in {PENDING, APPROVED}:
                    self._set(old, REJECTED, "superseded by a newer proposal")
            proposal = Proposal(
                id=secrets.token_urlsafe(12),
                kind=kind,
                entry_id=entry_id,
                event=event,
                endpoint=endpoint,
                payload=payload,
                summary=summary,
                warnings=warnings,
                fingerprint=fingerprint,
                created_at=now,
                expires_at=now + settings.proposal_ttl_seconds,
            )
            self._proposals[proposal.id] = proposal
        self._audit.record(
            "proposed",
            proposal=proposal.id,
            kind=kind,
            gameweek=event,
            summary=summary,
            warnings=warnings,
        )
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is not None:
                self._expire_if_due(proposal, self._clock())
            return proposal

    def list(self) -> list[Proposal]:
        with self._lock:
            now = self._clock()
            for proposal in self._proposals.values():
                self._expire_if_due(proposal, now)
            return sorted(self._proposals.values(), key=lambda p: p.created_at, reverse=True)

    # ----- human decision ------------------------------------------------------------
    def approve(self, proposal_id: str, pin: str, nonce: str, settings: Settings) -> Proposal:
        now = self._clock()
        with self._lock:
            if settings.approval_pin is None:
                raise ApprovalError("Approvals are disabled: FPL_APPROVAL_PIN is not configured.")
            if now < self._locked_until:
                self._audit.record("approval_blocked_lockout", proposal=proposal_id)
                raise ApprovalError("Too many wrong PIN attempts. Approvals are locked for now.")

            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise ApprovalError("Unknown proposal.")
            self._expire_if_due(proposal, now)
            if proposal.status != PENDING:
                raise ApprovalError(f"Proposal is {proposal.status}; it can no longer be approved.")
            if not hmac.compare_digest(nonce.encode(), proposal.nonce.encode()):
                self._audit.record("approval_bad_nonce", proposal=proposal_id)
                raise ApprovalError("Invalid form token. Reload the approval page.")

            if not hmac.compare_digest(pin.encode(), settings.approval_pin.encode()):
                self._failures += 1
                self._audit.record(
                    "approval_bad_pin", proposal=proposal_id, failures=self._failures
                )
                if self._failures >= settings.pin_max_failures:
                    self._locked_until = now + settings.pin_lockout_seconds
                    self._failures = 0
                    self._audit.record("approval_lockout", seconds=settings.pin_lockout_seconds)
                raise ApprovalError("Incorrect PIN.")

            self._failures = 0
            proposal.status = APPROVED
            proposal.approved_at = now
        self._audit.record("approved", proposal=proposal_id, kind=proposal.kind)
        return proposal

    def reject(self, proposal_id: str, nonce: str | None = None) -> Proposal:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise ApprovalError("Unknown proposal.")
            if nonce is not None and not hmac.compare_digest(
                nonce.encode(), proposal.nonce.encode()
            ):
                raise ApprovalError("Invalid form token. Reload the approval page.")
            if proposal.status in _TERMINAL or proposal.status == EXECUTING:
                raise ApprovalError(f"Proposal is already {proposal.status}.")
            self._set(proposal, REJECTED, "rejected")
        return proposal

    # ----- execution -----------------------------------------------------------------
    def begin_execute(self, proposal_id: str, settings: Settings) -> Proposal:
        """Atomically claim an approved proposal for execution (single use)."""
        now = self._clock()
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise ApprovalError("Unknown proposal.")
            self._expire_if_due(proposal, now)
            if proposal.status == PENDING:
                raise ApprovalError("Not approved yet. The user must approve it on the approval page.")
            if proposal.status != APPROVED:
                raise ApprovalError(f"Proposal is {proposal.status}; it cannot be executed.")
            if proposal.approved_at is None or now > proposal.approved_at + settings.execute_window_seconds:
                self._set(proposal, EXPIRED, "approval window elapsed before execution")
                raise ApprovalError("Approval expired before execution. Create a new proposal.")
            proposal.status = EXECUTING
        self._audit.record("execute_started", proposal=proposal_id, kind=proposal.kind)
        return proposal

    def finish(self, proposal_id: str, status: str, result: str) -> None:
        with self._lock:
            proposal = self._proposals[proposal_id]
            proposal.status = status
            proposal.result = result
        self._audit.record(status, proposal=proposal_id, result=result[:500])

    # ----- internals -----------------------------------------------------------------
    def _set(self, proposal: Proposal, status: str, note: str) -> None:
        proposal.status = status
        proposal.result = note
        self._audit.record(status, proposal=proposal.id, note=note)

    def _expire_if_due(self, proposal: Proposal, now: float) -> None:
        if proposal.status == PENDING and now > proposal.expires_at:
            self._set(proposal, EXPIRED, "not approved in time")

    def _purge(self, now: float) -> None:
        stale = [
            pid
            for pid, p in self._proposals.items()
            if p.status in _TERMINAL and now - p.created_at > 86400
        ]
        for pid in stale:
            del self._proposals[pid]
