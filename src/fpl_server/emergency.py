"""Pure logic for the emergency lineup exception (no network, no MCP).

The model never supplies a "verified" flag. Every fact that `safety.SafetyPolicy` checks is derived
HERE from fresh official FPL data and the server clock. If the data is stale, the clocks disagree,
or anything is missing, the corresponding flag is False and the policy denies.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .client import ServerClock
from .rules import PlayerInfo, RuleViolation

MAX_CLOCK_SKEW_SECONDS = 30  # our clock vs FPL's Date header
MAX_DATA_AGE_SECONDS = 180  # CDN cache age of the bootstrap data
MAX_SNAPSHOT_AGE_SECONDS = 120  # time between receiving the data and acting on it
MIN_SECONDS_BEFORE_DEADLINE = 60  # margin for clock skew + request latency; closer than this = refuse


def chips_known_inactive(chips: object) -> bool:
    """True ONLY if FPL returned usable chip data and no chip is active or pending.

    Missing, empty or malformed chip data is treated as unverified (=> the policy denies).
    """
    if not isinstance(chips, list) or not chips:
        return False
    for chip in chips:
        if not isinstance(chip, dict) or not isinstance(chip.get("status_for_entry"), str):
            return False
        if chip["status_for_entry"] == "active" or chip.get("is_pending"):
            return False
    return True


def data_is_fresh(clock: ServerClock | None, now: datetime) -> tuple[bool, str]:
    """Is the FPL data recent, and does our clock agree with FPL's? Fails closed."""
    if clock is None or clock.server_time is None:
        return False, "FPL did not report a server time"
    if now.tzinfo is None:
        return False, "local clock is not timezone-aware"
    skew = abs((clock.server_time - clock.received_at).total_seconds())
    if skew > MAX_CLOCK_SKEW_SECONDS:
        return False, f"local clock differs from FPL's by {int(skew)}s"
    if clock.age_seconds > MAX_DATA_AGE_SECONDS:
        return False, f"FPL data is {clock.age_seconds}s old (cached)"
    elapsed = (now - clock.received_at).total_seconds()
    if elapsed < -MAX_CLOCK_SKEW_SECONDS or elapsed > MAX_SNAPSHOT_AGE_SECONDS:
        return False, "FPL snapshot is not recent"
    return True, "fresh"


def parse_deadline(text: str | None) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def classify_unavailable(player: PlayerInfo) -> str:
    """Map official FPL flags to the policy's vocabulary. Anything uncertain is NOT 'ruled_out'."""
    status = player.status
    if status == "s":
        return "suspended"
    if status in {"i", "u", "n"}:
        # Ruled out only when FPL explicitly gives a 0% chance of playing.
        return "ruled_out" if player.chance_next == 0 else "doubtful"
    if status == "d":
        return "doubtful"
    if status == "a":
        return "available"
    return "unknown"


def classify_replacement(player: PlayerInfo) -> str:
    if player.status == "a" and player.chance_next in (None, 100):
        return "available"
    return "unavailable"


class SwapPlan:
    def __init__(self, ordered_ids: list[int], captain_id: int, vice_id: int):
        self.ordered_ids = ordered_ids
        self.captain_id = captain_id
        self.vice_id = vice_id


def plan_swap(*, picks: list[dict], players: dict[int, PlayerInfo], out_id: int, in_id: int) -> SwapPlan:
    """Swap one starter with one bench player, nothing else. Captaincy changes are out of scope."""
    ordered = [int(p["element"]) for p in sorted(picks, key=lambda p: p["position"])]
    if len(ordered) != 15 or len(set(ordered)) != 15:
        raise RuleViolation("Could not read a full 15-man squad.")
    starters, bench = ordered[:11], ordered[11:]
    out_name = players[out_id].name if out_id in players else out_id
    in_name = players[in_id].name if in_id in players else in_id
    if out_id not in starters:
        raise RuleViolation(f"{out_name} is not in your starting XI.")
    if in_id not in bench:
        raise RuleViolation(f"{in_name} is not on your bench.")
    captain = next((int(p["element"]) for p in picks if p.get("is_captain")), None)
    vice = next((int(p["element"]) for p in picks if p.get("is_vice_captain")), None)
    if captain is None or vice is None:
        raise RuleViolation("Could not read captain / vice-captain.")
    if out_id in {captain, vice}:
        raise RuleViolation(
            f"{out_name} is your captain or vice-captain. The emergency exception never changes "
            "captaincy; use the normal propose_lineup approval flow."
        )
    i, j = ordered.index(out_id), ordered.index(in_id)
    ordered[i], ordered[j] = ordered[j], ordered[i]
    return SwapPlan(ordered, captain, vice)


_SLOT_PART = re.compile(r"^[0-9]{1,12}$")


def claim_emergency_slot(directory: Path, entry_id: int, event: int, limit: int) -> int | None:
    """Atomically take one of `limit` emergency slots for (team, gameweek).

    Persistent (O_EXCL marker files) so a restart cannot reset the cap, and consumed even if the
    FPL call later fails - a failed emergency attempt must not be hammered.
    """
    if not (_SLOT_PART.match(str(entry_id)) and _SLOT_PART.match(str(event))):
        return None
    folder = directory / "emergency"
    folder.mkdir(parents=True, exist_ok=True)
    for slot in range(1, limit + 1):
        try:
            fd = os.open(folder / f"{entry_id}-gw{event}-{slot}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return slot
    return None


def count_emergency_slots(directory: Path, entry_id: int, event: int) -> int:
    """How many emergency slots are already used for (team, gameweek). Does not consume any."""
    folder = directory / "emergency"
    if not folder.is_dir():
        return 0
    return sum(1 for f in folder.glob(f"{int(entry_id)}-gw{int(event)}-*"))
