import pytest

from fpl_server.rules import (
    PlayerInfo,
    RuleViolation,
    normalize_chip,
    resolve_player,
    validate_chip,
    validate_lineup,
    validate_transfers,
)


def _transfer(world, out, inn, **kw):
    args = {
        "picks": world["picks"],
        "players": world["players"],
        "bank": world["bank"],
        "free_transfers": world["free"],
        "out_ids": out,
        "in_ids": inn,
        "chip": None,
        "max_transfers": 3,
        "max_points_hit": 4,
    }
    args.update(kw)
    return validate_transfers(**args)


def test_valid_single_transfer_computes_money_and_no_hit(world):
    plan = _transfer(world, [8], [104])  # MID 50 -> MID 55, bank 10
    assert plan.bank_after == 5 and plan.points_hit == 0 and plan.cost == 55
    assert plan.transfers == [
        {"element_out": 8, "element_in": 104, "selling_price": 50, "purchase_price": 55}
    ]


def test_extra_transfer_costs_four_points_and_respects_cap(world):
    plan = _transfer(world, [8, 9], [104, 107])
    assert plan.points_hit == 4 and plan.transfers_paid_for == 1
    with pytest.raises(RuleViolation, match="limit is 0"):
        _transfer(world, [8, 9], [104, 107], max_points_hit=0)


def test_wildcard_has_no_hit_and_no_transfer_cap(world):
    plan = _transfer(world, [8, 9], [104, 107], chip="wc", max_transfers=1, free_transfers=0)
    assert plan.points_hit == 0 and plan.chip == "wildcard"


def test_duplicates_refused(world):
    with pytest.raises(RuleViolation, match="Duplicate"):
        _transfer(world, [8, 9], [104, 104], chip="wildcard")


def test_too_many_transfers_without_chip(world):
    with pytest.raises(RuleViolation, match="per-proposal limit"):
        _transfer(world, [8, 9], [104, 107], max_transfers=1)


def test_insufficient_funds(world):
    with pytest.raises(RuleViolation, match="Not enough money"):
        _transfer(world, [8], [103])  # 200 > 50 + 10


def test_cannot_sell_unowned_or_buy_owned(world):
    with pytest.raises(RuleViolation, match="do not own"):
        _transfer(world, [104], [107])
    with pytest.raises(RuleViolation, match="already own"):
        _transfer(world, [8], [9])


def test_pair_must_share_position(world):
    with pytest.raises(RuleViolation, match="Pair"):
        _transfer(world, [8], [102])  # MID out, DEF in


def test_unavailable_blocked_doubtful_warned(world):
    with pytest.raises(RuleViolation, match="unavailable"):
        _transfer(world, [13], [105])
    plan = _transfer(world, [3], [106])
    assert any("Doubtful" in w for w in plan.warnings)


def test_club_limit_enforced(world):
    players = dict(world["players"])
    players[103] = PlayerInfo(103, "C3a", 3, "Club3", 3, 50)
    players[104] = PlayerInfo(104, "C3b", 3, "Club3", 3, 50)
    players[105] = PlayerInfo(105, "C3c", 3, "Club3", 3, 50)
    # squad DEF id 3 already plays for club 3, so three more makes four
    with pytest.raises(RuleViolation, match="max is 3"):
        validate_transfers(
            picks=world["picks"],
            players=players,
            bank=100,
            free_transfers=3,
            out_ids=[8, 9, 10],
            in_ids=[103, 104, 105],
            chip=None,
            max_transfers=3,
            max_points_hit=4,
        )


def test_missing_bank_refused(world):
    with pytest.raises(RuleViolation, match="Bank"):
        _transfer(world, [8], [104], bank=None)


def test_team_chips_rejected_with_transfers(world):
    with pytest.raises(RuleViolation, match="propose_chip"):
        _transfer(world, [8], [104], chip="bboost")


def _lineup(world, order, cap=9, vc=10, **kw):
    return validate_lineup(
        current_ids=set(range(1, 16)),
        ordered_ids=order,
        captain_id=cap,
        vice_id=vc,
        players=world["players"],
        **kw,
    )


GOOD = [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 2, 7, 12, 15]  # 1-4-4-2 + bench GK first


def test_valid_lineup_sets_positions_and_flags(world):
    plan = _lineup(world, GOOD)
    assert [r["position"] for r in plan.picks] == list(range(1, 16))
    assert [r["element"] for r in plan.picks if r["is_captain"]] == [9]


def test_lineup_rejects_bad_formation_bench_gk_and_captains(world):
    with pytest.raises(RuleViolation, match="formation"):
        _lineup(world, [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 2, 13, 14, 15])  # 1-5-5-0
    with pytest.raises(RuleViolation, match="substitute goalkeeper"):
        _lineup(world, [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 7, 2, 12, 15])
    with pytest.raises(RuleViolation, match="different"):
        _lineup(world, GOOD, cap=9, vc=9)
    with pytest.raises(RuleViolation, match="starting XI"):
        _lineup(world, GOOD, cap=9, vc=2)  # vice-captain on the bench
    with pytest.raises(RuleViolation, match="exactly the players"):
        _lineup(world, [101, *GOOD[1:]])


def test_keep_order_preserves_positions(world):
    order = [1, 5, 3, 4, 6, 8, 9, 10, 11, 13, 14, 2, 7, 12, 15]
    plan = _lineup(world, order, keep_order=True)
    assert [r["element"] for r in plan.picks] == order


def test_chip_validation():
    chips = [
        {"name": "bboost", "status_for_entry": "available", "start_event": 2, "stop_event": 19},
        {"name": "3xc", "status_for_entry": "played"},
        {"name": "wildcard", "status_for_entry": "available"},
    ]
    assert validate_chip(chip="Bench Boost", chips=chips, event=5) == "bboost"
    with pytest.raises(RuleViolation, match="not available"):
        validate_chip(chip="3xc", chips=chips, event=5)
    with pytest.raises(RuleViolation, match="GW2-GW19"):
        validate_chip(chip="bboost", chips=chips, event=25)
    active = [*chips, {"name": "freehit", "status_for_entry": "active"}]
    with pytest.raises(RuleViolation, match="already active"):
        validate_chip(chip="bboost", chips=active, event=5)
    with pytest.raises(RuleViolation, match="Unknown chip"):
        normalize_chip("superboost")


def test_resolver_is_strict(world):
    players = world["players"]
    assert resolve_player("expensive", players) == 103
    assert resolve_player("104", players) == 104
    with pytest.raises(RuleViolation, match="No exact match"):
        resolve_player("Expensiv", players)  # a typo is NOT silently accepted
    with pytest.raises(RuleViolation, match="not available"):
        resolve_player(104, players, only_ids={1, 2})
    dup = {**players, 900: PlayerInfo(900, "MID8", 40, "C", 3, 50)}
    with pytest.raises(RuleViolation, match="ambiguous"):
        resolve_player("MID8", dup)
