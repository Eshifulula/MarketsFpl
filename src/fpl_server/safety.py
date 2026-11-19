"""Standalone safety primitives: the emergency-correction policy and one-time approval tokens.

Both are pure decision logic (no FPL access, no network). They FAIL CLOSED: anything missing,
mistyped or unverified is a denial.

SafetyPolicy is used by the emergency tool (mcp_emergency_tools.py). ApprovalGate is tested but
not used by the live path (the PIN approval page in approvals.py is). See docs/SECURITY_MODEL.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


# --------------------------------------------------------------------------------------------
# Emergency policy: the ONLY change that may skip the normal human approval step.
# --------------------------------------------------------------------------------------------
EMERGENCY_WINDOW = timedelta(minutes=15)
EMERGENCY_ALLOWED_ACTIONS = frozenset({"lineup_change"})  # allow-list: transfers/chips never
CONFIRMED_UNAVAILABLE = frozenset({"suspended", "ruled_out"})

_REQUIRED_KEYS = (
    "team_id",
    "gameweek",
    "action",
    "mode",
    "now",
    "deadline",
    "deadline_verified",
    "player_status",
    "player_status_verified",
    "replacement_status",
    "replacement_verified",
    "legal_formation",
    "points_hit",
    "uses_chip",
)


class SafetyPolicy:
    def evaluate(self, request: dict[str, Any]) -> Decision:
        """Decide whether an *emergency* lineup correction may proceed without prior approval.

        Allowed only when ALL hold: a verified deadline is within 15 minutes (and not passed);
        the player being replaced is verified suspended / ruled out; the replacement is verified
        available; the formation is legal; no points hit; no chip. Everything else is denied and
        must go through the normal approval flow.
        """
        if not isinstance(request, dict):
            return _deny("malformed request")
        missing = [k for k in _REQUIRED_KEYS if k not in request]
        if missing:
            return _deny(f"missing required information: {', '.join(missing)}")

        if request["mode"] != "emergency":
            return _deny("not an emergency request; normal approval is required")
        if request["action"] not in EMERGENCY_ALLOWED_ACTIONS:
            return _deny("this action is never permitted under the emergency exception")
        if isinstance(request["team_id"], bool) or not isinstance(request["team_id"], int):
            return _deny("invalid team id")
        if isinstance(request["gameweek"], bool) or not isinstance(request["gameweek"], int):
            return _deny("invalid gameweek")

        now, deadline = request["now"], request["deadline"]
        if not (isinstance(now, datetime) and isinstance(deadline, datetime)):
            return _deny("now and deadline must be datetimes")
        if now.tzinfo is None or deadline.tzinfo is None:
            return _deny("timestamps must be timezone-aware")
        if request["deadline_verified"] is not True:
            return _deny("deadline is not verified")
        remaining = deadline - now
        if remaining <= timedelta(0):
            return _deny("the deadline has passed")
        if remaining > EMERGENCY_WINDOW:
            return _deny("too early: emergency changes open 15 minutes before the deadline")

        if request["player_status_verified"] is not True:
            return _deny("player availability is not verified")
        if request["player_status"] not in CONFIRMED_UNAVAILABLE:
            return _deny("player is not confirmed suspended or ruled out")
        if request["replacement_verified"] is not True:
            return _deny("replacement availability is not verified")
        if request["replacement_status"] != "available":
            return _deny("replacement is not available")
        if request["legal_formation"] is not True:
            return _deny("resulting formation is not legal")

        points_hit = request["points_hit"]
        if isinstance(points_hit, bool) or not isinstance(points_hit, int) or points_hit != 0:
            return _deny("emergency changes may not cost points")
        if request["uses_chip"] is not False:
            return _deny("emergency changes may not use a chip")

        return Decision(True, "verified emergency lineup correction")


# --------------------------------------------------------------------------------------------
# One-time approval tokens, bound to the exact action.
# --------------------------------------------------------------------------------------------
MIN_SECRET_BYTES = 16
_JTI = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _canonical(action: dict[str, Any]) -> bytes:
    return json.dumps(action, sort_keys=True, separators=(",", ":"), default=str).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ApprovalGate:
    """Issues HMAC-signed, expiring, single-use approvals bound to one exact action.

    Spent token ids persist on disk so a restart cannot resurrect a spent approval. Each spend is an
    atomic exclusive-create (O_EXCL) of a marker file, so simultaneous attempts - threads or whole
    processes - race safely and exactly one wins. `database_path` is the base path; markers live in
    the sibling directory "<database_path>.consumed/".
    """

    def __init__(self, secret: bytes, database_path: str | Path):
        if not isinstance(secret, bytes | bytearray) or len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"secret must be at least {MIN_SECRET_BYTES} bytes")
        self._secret = bytes(secret)
        self._consumed_dir = Path(f"{database_path}.consumed")
        self._consumed_dir.mkdir(parents=True, exist_ok=True)

    def _consume(self, jti: str) -> bool:
        """Atomically mark a token spent. True only for the first caller."""
        if not _JTI.fullmatch(jti):
            return False
        try:
            fd = os.open(self._consumed_dir / jti, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())

    def issue(self, *, action: dict[str, Any], now: datetime, ttl_seconds: int) -> str:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        claims = {
            "jti": secrets.token_urlsafe(16),
            "exp": now.timestamp() + ttl_seconds,
            "act": hashlib.sha256(_canonical(action)).hexdigest(),
        }
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def authorize(
        self,
        *,
        action: dict[str, Any],
        approval_token: str | None,
        now: datetime,
    ) -> Decision:
        if not approval_token or not isinstance(approval_token, str):
            return _deny("approval required")
        body, dot, signature = approval_token.partition(".")
        if not dot or not hmac.compare_digest(signature, self._sign(body)):
            return _deny("invalid approval")
        try:
            claims = json.loads(_unb64(body))
            jti, exp, bound = str(claims["jti"]), float(claims["exp"]), str(claims["act"])
        except (ValueError, KeyError, TypeError):
            return _deny("invalid approval")
        if now.tzinfo is None:
            return _deny("timestamps must be timezone-aware")
        if now.timestamp() > exp:
            return _deny("approval expired")
        if not hmac.compare_digest(bound, hashlib.sha256(_canonical(action)).hexdigest()):
            return _deny("approval does not match this exact action")

        if not self._consume(jti):
            return _deny("approval already used")
        return Decision(True, "approved")
