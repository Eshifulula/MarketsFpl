"""Pure FPL rule validation for proposed team changes.

No I/O, no network, no global state - everything is passed in, so it is trivially testable.
The goal is to refuse an invalid or obviously unsafe change *before* a human is asked to approve
it, and to compute the facts (cost, points hit, warnings) the approval page shows.

Prices are integers in tenths of a million (FPL's native unit): 105 == GBP 10.5m.
"""

from __future__ import annotations

import difflib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}  # position -> required count in the 15-man squad
MAX_PER_CLUB = 3
POINTS_PER_EXTRA_TRANSFER = 4
FORMATION_LIMITS = {1: (1, 1), 2: (3, 5), 3: (2, 5), 4: (1, 3)}  # starting-XI min/max per position

CHIP_ALIASES = {
    "wildcard": "wildcard",
    "wc": "wildcard",
    "freehit": "freehit",
    "free hit": "freehit",
    "free_hit": "freehit",
    "fh": "freehit",
    "bboost": "bboost",
    "bench boost": "bboost",
    "bench_boost": "bboost",
    "benchboost": "bboost",
    "bb": "bboost",
    "3xc": "3xc",
    "triple captain": "3xc",
    "triple_captain": "3xc",
    "triplecaptain": "3xc",
    "tc": "3xc",
}
TRANSFER_CHIPS = {"wildcard", "freehit"}  # activated together with a transfer list
TEAM_CHIPS = {"bboost", "3xc"}  # activated through the team-selection endpoint


class RuleViolation(ValueError):
    """A proposed change breaks an FPL rule or a configured safety limit."""


@dataclass(frozen=True)
class PlayerInfo:
    id: int
    name: str
    team_id: int
    team_name: str
    position: int
    price: int
    status: str = "a"
    news: str = ""
    aliases: tuple[str, ...] = ()
    chance_next: int | None = None  # FPL chance_of_playing_next_round (None = no flag)

    @property
    def pos_name(self) -> str:
        return POSITION_NAMES.get(self.position, "UNK")


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def describe(player: PlayerInfo) -> str:
    return f"{player.name} ({player.pos_name}, {player.team_name}, {fmt_money(player.price)}) [id {player.id}]"


def resolve_player(
    token: str | int,
    players: dict[int, PlayerInfo],
    *,
    only_ids: set[int] | None = None,
) -> int:
    """Strict name/id -> player id. Never guesses: ambiguity and misses raise RuleViolation.

    Accepts a numeric id, or an exact (accent/case-insensitive) web name, full name or surname.
    """
    pool = {i: p for i, p in players.items() if only_ids is None or i in only_ids}
    text = str(token).strip()
    if text.isdigit():
        pid = int(text)
        if pid in pool:
            return pid
        raise RuleViolation(f"Player id {pid} is not available here.")
    key = _norm(text)
    if not key:
        raise RuleViolation("Empty player name.")
    matches = [
        pid
        for pid, p in pool.items()
        if key in {_norm(p.name), *(_norm(a) for a in p.aliases)}
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = "; ".join(describe(pool[m]) for m in matches[:6])
        raise RuleViolation(f"'{text}' is ambiguous - use a player id. Options: {options}")
    names = {_norm(p.name): pid for pid, p in pool.items()}
    close = difflib.get_close_matches(key, list(names), n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(describe(pool[names[c]]) for c in close)}?" if close else ""
    where = "in your squad" if only_ids is not None else "in the player list"
    raise RuleViolation(f"No exact match for '{text}' {where}.{hint}")


def fmt_money(tenths: int | None) -> str:
    return "n/a" if tenths is None else f"£{tenths / 10:.1f}m"


@dataclass
class TransferPlan:
    transfers: list[dict]
    chip: str | None
    bank_before: int
    bank_after: int
    proceeds: int
    cost: int
    points_hit: int
    transfers_paid_for: int
    warnings: list[str] = field(default_factory=list)


def normalize_chip(chip: str | None) -> str | None:
    if chip is None:
        return None
    key = str(chip).strip().lower()
    if key in {"", "none", "null"}:
        return None
    if key not in CHIP_ALIASES:
        raise RuleViolation(f"Unknown chip '{chip}'. Use wildcard, freehit, bboost or 3xc.")
    return CHIP_ALIASES[key]


def _check_squad_shape(squad: list[PlayerInfo]) -> None:
    if len(squad) != 15:
        raise RuleViolation(f"Squad would have {len(squad)} players; it must have 15.")
    by_pos = Counter(p.position for p in squad)
    for pos, needed in SQUAD_SHAPE.items():
        if by_pos.get(pos, 0) != needed:
            raise RuleViolation(
                f"Squad would have {by_pos.get(pos, 0)} {POSITION_NAMES[pos]}; needs {needed}."
            )
    by_club = Counter(p.team_id for p in squad)
    for team_id, count in by_club.items():
        if count > MAX_PER_CLUB:
            name = next(p.team_name for p in squad if p.team_id == team_id)
            raise RuleViolation(f"Squad would have {count} players from {name}; max is 3.")


def validate_transfers(
    *,
    picks: list[dict],
    players: dict[int, PlayerInfo],
    bank: int | None,
    free_transfers: int | None,
    out_ids: list[int],
    in_ids: list[int],
    chip: str | None,
    max_transfers: int,
    max_points_hit: int,
) -> TransferPlan:
    chip = normalize_chip(chip)
    if chip in TEAM_CHIPS:
        raise RuleViolation("bboost / 3xc are not activated with transfers. Use propose_chip.")
    if not out_ids or len(out_ids) != len(in_ids):
        raise RuleViolation("Provide the same (non-zero) number of players out and in.")
    if len(set(out_ids)) != len(out_ids) or len(set(in_ids)) != len(in_ids):
        raise RuleViolation("Duplicate players in the transfer list.")
    if bank is None:
        raise RuleViolation("Bank balance is unavailable (is the season/gameweek live?).")

    unlimited = chip in TRANSFER_CHIPS
    if not unlimited and len(out_ids) > max_transfers:
        raise RuleViolation(
            f"{len(out_ids)} transfers exceeds the per-proposal limit of {max_transfers}."
        )

    owned = {int(p["element"]): p for p in picks}
    if len(owned) != 15:
        raise RuleViolation("Could not read a full 15-man squad; refusing to propose.")

    for pid in out_ids:
        if pid not in owned:
            raise RuleViolation(f"You do not own player {pid}.")
    for pid in in_ids:
        if pid in owned and pid not in out_ids:
            raise RuleViolation(f"You already own {players[pid].name if pid in players else pid}.")
        if pid not in players:
            raise RuleViolation(f"Unknown incoming player id {pid}.")

    transfers: list[dict] = []
    warnings: list[str] = []
    proceeds = 0
    cost = 0
    for out_id, in_id in zip(out_ids, in_ids, strict=True):
        out_pick = owned[out_id]
        sell = out_pick.get("selling_price")
        if sell is None:
            raise RuleViolation("Selling price unavailable for an outgoing player.")
        pin = players[in_id]
        pout = players.get(out_id)
        if pout is not None and pout.position != pin.position:
            raise RuleViolation(
                f"Pair {pout.name} ({pout.pos_name}) with a {pout.pos_name}, "
                f"not {pin.name} ({pin.pos_name})."
            )
        if pin.status == "u":
            raise RuleViolation(f"{pin.name} is unavailable and cannot be bought.")
        if pin.status != "a":
            note = f" - {pin.news}" if pin.news else ""
            warnings.append(f"{pin.name} is flagged '{pin.status}'{note}")
        proceeds += int(sell)
        cost += pin.price
        transfers.append(
            {
                "element_out": out_id,
                "element_in": in_id,
                "selling_price": int(sell),
                "purchase_price": pin.price,
            }
        )

    bank_after = bank + proceeds - cost
    if bank_after < 0:
        raise RuleViolation(
            f"Not enough money: need {fmt_money(cost)}, have {fmt_money(bank + proceeds)}."
        )

    final_squad = [players[i] for i in (set(owned) - set(out_ids)) | set(in_ids) if i in players]
    _check_squad_shape(final_squad)

    if unlimited:
        paid = 0
    elif free_transfers is None:
        paid = 0
        warnings.append("Free-transfer count unavailable; points hit not computed.")
    else:
        paid = max(0, len(out_ids) - max(0, free_transfers))
    hit = paid * POINTS_PER_EXTRA_TRANSFER
    if hit > max_points_hit:
        raise RuleViolation(
            f"This would cost {hit} points; the configured limit is {max_points_hit}."
        )

    return TransferPlan(
        transfers=transfers,
        chip=chip,
        bank_before=bank,
        bank_after=bank_after,
        proceeds=proceeds,
        cost=cost,
        points_hit=hit,
        transfers_paid_for=paid,
        warnings=warnings,
    )


@dataclass
class LineupPlan:
    picks: list[dict]  # exact payload rows: element, position, is_captain, is_vice_captain
    warnings: list[str] = field(default_factory=list)


def validate_lineup(
    *,
    current_ids: set[int],
    ordered_ids: list[int],
    captain_id: int,
    vice_id: int,
    players: dict[int, PlayerInfo],
    keep_order: bool = False,
) -> LineupPlan:
    """`ordered_ids` is the full 15: first 11 = starters, then bench in order (GK first)."""
    if len(ordered_ids) != 15 or len(set(ordered_ids)) != 15:
        raise RuleViolation("A lineup needs exactly 15 distinct players (11 starters + 4 bench).")
    if set(ordered_ids) != current_ids:
        raise RuleViolation("Lineup must use exactly the players currently in your squad.")
    missing = [i for i in ordered_ids if i not in players]
    if missing:
        raise RuleViolation(f"Unknown player ids: {missing}")

    starters, bench = ordered_ids[:11], ordered_ids[11:]
    counts = Counter(players[i].position for i in starters)
    for pos, (low, high) in FORMATION_LIMITS.items():
        if not low <= counts.get(pos, 0) <= high:
            raise RuleViolation(
                f"Invalid formation: {counts.get(pos, 0)} {POSITION_NAMES[pos]} in the XI "
                f"(allowed {low}-{high})."
            )
    if players[bench[0]].position != 1:
        raise RuleViolation("The first bench slot (position 12) must be the substitute goalkeeper.")
    if any(players[i].position == 1 for i in bench[1:]):
        raise RuleViolation("Only one goalkeeper may be on the bench.")

    if captain_id == vice_id:
        raise RuleViolation("Captain and vice-captain must be different players.")
    if captain_id not in starters or vice_id not in starters:
        raise RuleViolation("Captain and vice-captain must both be in the starting XI.")

    warnings = []
    for pid in starters:
        p = players[pid]
        if p.status in {"i", "s", "u"}:
            warnings.append(f"Starter {p.name} is flagged '{p.status}'" + (f" - {p.news}" if p.news else ""))
    for label, pid in (("Captain", captain_id), ("Vice-captain", vice_id)):
        if players[pid].status != "a":
            warnings.append(f"{label} {players[pid].name} is flagged '{players[pid].status}'")

    # Mirror the website: starters ordered GKP, DEF, MID, FWD (stable within position).
    if keep_order:
        final_order = list(ordered_ids)
    else:
        starters_sorted = sorted(starters, key=lambda i: (players[i].position, starters.index(i)))
        final_order = starters_sorted + bench
    picks = [
        {
            "element": pid,
            "position": idx,
            "is_captain": pid == captain_id,
            "is_vice_captain": pid == vice_id,
        }
        for idx, pid in enumerate(final_order, start=1)
    ]
    return LineupPlan(picks=picks, warnings=warnings)


def validate_chip(*, chip: str, chips: list[dict] | None, event: int) -> str:
    """Returns the normalised chip name or raises RuleViolation."""
    name = normalize_chip(chip)
    if name is None:
        raise RuleViolation("No chip specified.")
    if not chips:
        raise RuleViolation("Chip information is unavailable.")
    if any(c.get("status_for_entry") == "active" for c in chips):
        raise RuleViolation("A chip is already active for this gameweek.")
    entry = next((c for c in chips if c.get("name") == name), None)
    if entry is None:
        raise RuleViolation(f"Chip '{name}' does not exist for this account.")
    if entry.get("status_for_entry") != "available":
        raise RuleViolation(f"Chip '{name}' is not available ({entry.get('status_for_entry')}).")
    start, stop = entry.get("start_event"), entry.get("stop_event")
    if isinstance(start, int) and isinstance(stop, int) and not start <= event <= stop:
        raise RuleViolation(f"Chip '{name}' can only be played in GW{start}-GW{stop}.")
    return name
