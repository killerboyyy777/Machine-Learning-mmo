"""Offline env fidelity tests (#191/#194/#183): no server needed.

Run from the repo root:  python tests/test_env_parsing.py
Covers: commission_list parsing (plain + discounted-rate lines); pack
masking from the server's authoritative pack count; priced market posts;
parameterized bounties; merchant/affordability/party gates (#194);
remedy/tonic quest mirror, stages, transitions, events, obs (#183);
dynamic-shard valuation through live ITEM_DEFS (#194); and loaded ammo
effect flags from the server's arrow pick (#986).
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml")
)

import ml_env
from ml_env import (
    GOAL_AXES,
    ITEM_ID_TO_NAME,
    ITEM_LIST,
    MERCHANT_NAMES,
    N_ACTIONS,
    OBS_SIZE,
    QUESTS,
    TextMMOEnv,
    _parse_commissions,
    best_market_ask,
    flatten_obs,
    flip_margin,
    goal_reward,
    inventory_value,
    merchant_value,
    mutate_goal,
    pack_full,
    pack_units,
    quest_stage,
    quest_transitions,
    reward_vector,
    sample_goal,
)

import server as srv


def test_discounted_commission_parses():
    plain = "#12: slay 5x Giant Rat -- reward 25g + 50xp (posted by Alice)"
    disc = "#13: slay 5x Giant Rat \u2014 reward 25g + 50xp (your rate: x0.5) (posted by Alice)"
    hyphen = "#14: slay 1x Wolf - reward 10g + 5xp (your rate: x0.1) (posted by Bob)"
    rows = _parse_commissions("\n".join([plain, disc, hyphen]))
    assert len(rows) == 3, rows
    assert (
        rows[0]["id"] == 12 and rows[0]["rate"] is None and rows[0]["poster"] == "Alice"
    )
    assert (
        rows[1]["id"] == 13 and rows[1]["rate"] == 0.5 and rows[1]["poster"] == "Alice"
    )
    assert rows[1]["gold"] == 25 and rows[1]["xp"] == 50
    assert (
        rows[2]["id"] == 14 and rows[2]["rate"] == 0.1 and rows[2]["target"] == "Wolf"
    )
    assert _parse_commissions("No open commissions right now.") == []
    print("COMMISSION_PARSE_OK")


def _env_with_pack(names, units):
    e = TextMMOEnv("ParseT")
    e._state["inv_names"] = list(names)
    e._state["pack_units"] = units
    e._state["pack_max"] = 24
    return e


def test_pack_masks_from_server_count():
    names20 = [f"Trinket {i}" for i in range(20)]  # truncated list, as stats sends it
    # Full 24-unit pack reading as 20 names: name-counting says valid,
    # server truth says full (this is the desync that wasted agent steps).
    e = _env_with_pack(names20, 24)
    assert pack_units(e._state) == 24
    assert pack_full(e._state)
    e._state["item_names"] = ["Rusty Nail"]
    e._state["gatherables"] = ["Pine Timber Node"]
    assert e._action_to_cmd("take") is None
    assert e._action_to_cmd("gather") is None
    assert e._action_to_cmd("buy_arrows") is None
    # gold piles ignore the cap: still takeable from a full pack
    e._state["room_gold"] = 5
    assert e._action_to_cmd("take") == {"cmd": "take", "item": "gold"}
    e._state["room_gold"] = 0
    # 23 units: still room, everything valid (name-counting agrees here)
    e._state["pack_units"] = 23
    assert not pack_full(e._state)
    # 20 units: everything valid again (merchant present for buy_arrows)
    e._state["pack_units"] = 20
    e._state["npc_names"] = list(MERCHANT_NAMES[:1])
    assert not pack_full(e._state)
    assert e._action_to_cmd("take") == {"cmd": "take", "item": "Rusty Nail"}
    assert e._action_to_cmd("gather") == {"cmd": "gather"}
    assert e._action_to_cmd("buy_arrows") == {"cmd": "buy", "item": "Arrow"}
    # legacy fallback (no stats yet): counts names
    e._state["pack_units"] = None
    assert pack_units(e._state) == 20
    assert not pack_full(e._state)
    print("PACK_MASK_OK")


def _econ_env(**over):
    e = TextMMOEnv("ParseEcon")
    e._state.update(over)
    e._refresh_holdings()
    return e


def test_priced_market_post():
    # Listed ask 20, herb value 1: margin 17 > 0, undercut to 19.
    e = _econ_env(
        inv_names=["Healing Herb", "Healing Herb"],
        market_state={
            "orders": [{"item": "Healing Herb", "price": 20, "seller": "Other"}]
        },
    )
    cmd = e._action_to_cmd("market_post")
    assert cmd == {"cmd": "market_post", "item": "Healing Herb", "price": 19}, cmd
    # Unlisted: merchant value + 1 (value defaults to 1 without a value key).
    e = _econ_env(
        inv_names=["Healing Herb", "Healing Herb"], market_state={"orders": []}
    )
    cmd = e._action_to_cmd("market_post")
    assert cmd == {"cmd": "market_post", "item": "Healing Herb", "price": 2}, cmd
    print("PRICED_POST_OK")


def test_parameterized_bounty():
    e = _econ_env(npc_names=["Giant Rat"], gold=50, room_id="guild_hall")
    cmd = e._action_to_cmd("commission_post")
    assert cmd == {
        "cmd": "commission_post",
        "target": "Giant Rat",
        "required_kills": 1,
        "reward_gold": 10,
        "reward_xp": 0,
    }, cmd
    # Broke: masked (a zero-reward post would be rejected, #337 item 3).
    e = _econ_env(npc_names=["Giant Rat"], gold=0, room_id="guild_hall")
    assert e._action_to_cmd("commission_post") is None
    # Blind but funded: server's own "rat" default.
    cmd = _econ_env(npc_names=[], gold=50, room_id="guild_hall")._action_to_cmd(
        "commission_post"
    )
    assert cmd["target"] == "rat" and cmd["reward_gold"] == 10, cmd
    # Away from the guild hall: post is presence-gated (#337).
    e = _econ_env(npc_names=["Giant Rat"], gold=50, room_id="town_square")
    assert e._action_to_cmd("commission_post") is None
    e = _econ_env(
        npc_names=["Giant Rat"],
        gold=50,
        room_id="town_square",
        open_commissions=[{"id": 1, "poster": "Other", "gold": 5, "xp": 0}],
    )
    assert e._action_to_cmd("commission_fill") is None
    e = _econ_env(
        npc_names=["Giant Rat"],
        gold=50,
        room_id="guild_hall",
        open_commissions=[{"id": 1, "poster": "Other", "gold": 5, "xp": 0}],
    )
    assert e._action_to_cmd("commission_fill") == {
        "cmd": "commission_fill",
        "commission_id": 1,
    }
    print("BOUNTY_PARAMS_OK")


def test_gate_matrix():
    # Each row: masked-out states must map to None exactly when the server
    # would reject; mappable states must produce a command (#194 1:1).
    herbs = ["Healing Herb", "Healing Herb"]
    rows = [
        ("sell", {"npc_names": [], "inv_names": herbs}, True),
        ("sell", {"npc_names": ["Wandering Merchant"], "inv_names": herbs}, False),
        ("buy_arrows", {"npc_names": []}, True),
        ("buy_arrows", {"npc_names": ["Wandering Merchant"]}, False),
        ("market_buy", {"market_state": {"orders": []}, "gold": 100}, True),
        (
            "market_buy",
            {
                "market_state": {"orders": [{"item": "X", "price": 50, "seller": "O"}]},
                "gold": 10,
            },
            True,
        ),
        (
            "market_buy",
            {
                "market_state": {"orders": [{"item": "X", "price": 50, "seller": "O"}]},
                "gold": 100,
            },
            False,
        ),
        # party_leave/info are deliberately UNGATED (membership isn't
        # client-verifiable: room-event party_size lags joins, so gating
        # blocks the valid info right after accept -- proven by live test).
    ]
    for action, over, expect_none in rows:
        got = _econ_env(**over)._action_to_cmd(action)
        assert (got is None) == expect_none, (action, over, got)
    assert _econ_env(party_size=1)._action_to_cmd("party_leave") == {
        "cmd": "party_leave"
    }
    assert _econ_env(party_size=1)._action_to_cmd("party_info") == {"cmd": "party_info"}
    # Mappable states produce real commands, not just non-None.
    assert _econ_env(npc_names=["Wandering Merchant"], inv_names=herbs)._action_to_cmd(
        "sell"
    ) == {"cmd": "sell", "item": "Healing Herb"}
    assert _econ_env(
        market_state={"orders": [{"item": "X", "price": 50, "seller": "O"}]}, gold=100
    )._action_to_cmd("market_buy") == {"cmd": "market_buy"}
    print("GATES_MATRIX_OK")


def test_maren_mirror_and_stages():
    assert QUESTS["remedy"]["reward_xp"] == srv.QUEST_REMEDY_XP == 20
    assert QUESTS["remedy"]["reward_gold"] == srv.QUEST_REMEDY_GOLD == 10
    assert QUESTS["remedy"]["reward_points"] == srv.QUEST_REMEDY_POINTS == 8
    assert QUESTS["remedy"]["giver_name"] == "Sister Maren"
    assert QUESTS["remedy"]["room"] == "healing_spring"
    assert QUESTS["remedy"]["inputs"] == ["healing_herb"]
    assert QUESTS["tonic"]["reward_xp"] == srv.QUEST_TONIC_XP == 40
    assert QUESTS["tonic"]["reward_gold"] == srv.QUEST_TONIC_GOLD == 20
    assert QUESTS["tonic"]["reward_points"] == srv.QUEST_TONIC_POINTS == 12
    assert QUESTS["tonic"]["inputs"] == ["fortitude_tonic"]
    for qid, pre in (("remedy", "quest3"), ("tonic", "quest4")):
        assert (
            quest_stage({pre + "_active": True, pre + "_ready": True}, qid)
            == "ready_turn_in"
        )
        assert quest_stage({pre + "_active": True}, qid) == "collect"
        assert quest_stage({}, qid) == "no_quest"
    print("MAREN_MIRROR_OK")


def test_quest_transitions_all_four():
    specs = (
        ("guard_charm", "quest_guard_active", "guard_charm_crafted"),
        ("delver", "quest_delver_active", "quest_delver_ready"),
        ("remedy", "quest_remedy_active", "quest_remedy_ready"),
        ("tonic", "quest_tonic_active", "quest_tonic_ready"),
    )
    for qid, akey, rkey in specs:
        t = quest_transitions({akey: False}, {akey: True})
        assert t[qid] == {
            "accepted": True,
            "turned_in": False,
            "became_ready": False,
        }, (qid, t)
        t = quest_transitions({akey: True}, {akey: False})
        assert t[qid] == {
            "accepted": False,
            "turned_in": True,
            "became_ready": False,
        }, (qid, t)
        t = quest_transitions({rkey: False}, {rkey: True})
        assert t[qid] == {
            "accepted": False,
            "turned_in": False,
            "became_ready": True,
        }, (qid, t)
        t = quest_transitions({}, {})
        assert t[qid] == {
            "accepted": False,
            "turned_in": False,
            "became_ready": False,
        }, (qid, t)
    print("QUEST_TRANSITIONS_OK")


def test_event_parsing_maren_combat_inventory():
    e = TextMMOEnv("ParseEv")
    e._apply_event(
        {
            "type": "stats",
            "hp": 20,
            "max_hp": 20,
            "gold": 5,
            "score": 1.0,
            "quest_remedy_active": True,
            "quest_remedy_ready": False,
            "quest_tonic_active": True,
            "quest_tonic_ready": True,
        }
    )
    assert e._state["quest_remedy_active"] is True
    assert e._state["quest_remedy_ready"] is False
    assert e._state["quest_tonic_active"] is True
    assert e._state["quest_tonic_ready"] is True
    e._apply_event({"type": "combat", "text": "Giant Rat hits you for 3."})
    assert e._state["last_combat"] == "Giant Rat hits you for 3."
    full = [f"Trinket {i}" for i in range(24)]
    e._apply_event({"type": "inventory", "items": full, "equipped": "Rusty Sword"})
    assert e._state["inv_names"] == full  # untruncated, unlike stats.inv
    assert e._state["equipped"] == "Rusty Sword"
    print("EVENT_PARSE_OK")


def test_death_accounting_reaches_reward_signals():
    # The death payload carries gold_lost/gold_dropped/xp_lost and the env
    # used to read none of them, so a reward formula had no way to express
    # "died expensively" (the death penalty is a negative score_delta, and
    # the death event itself does not move the gold stat -- the server
    # follows it with a separate stats snapshot).
    from ml.reward_dsl import SIGNALS

    e = TextMMOEnv("DeathSig", reward_formula="gold_lost + gold_dropped")
    e._apply_event(
        {
            "type": "death",
            "gold_lost": 12.0,
            "gold_dropped": 8.0,
            "xp_lost": 5.0,
            "level": 3,
            "text": "You die!",
        }
    )
    sig = e._reward_signals(0.0, 0.0, 0, 0.0, 0.0, *_pendings(e))
    assert sig["deaths"] == 1.0, sig
    assert sig["deaths_total"] == 1.0, sig
    assert (sig["gold_lost"], sig["gold_dropped"], sig["xp_lost"]) == (12.0, 8.0, 5.0)
    assert set(sig) == set(SIGNALS), sorted(set(SIGNALS) ^ set(sig))
    # the accounting is per-step, and drains after the step reads it
    assert e._compute_reward(0.0, 0.0, 0, 0.0, 0.0, 1, sig) == 20.0
    e._pending_deaths = e._pending_gold_lost = e._pending_gold_dropped = 0.0
    e._pending_xp_lost = 0
    e._steps_since_death = 5  # arbitrary
    after = e._reward_signals(0.0, 0.0, 0, 0.0, 0.0, *_pendings(e))
    assert after["deaths"] == 0.0 and after["gold_lost"] == 0.0, after
    assert after["deaths_total"] == 1.0, "cumulative count must survive the drain"
    # a death with no accounting fields still counts, and does not crash
    e._apply_event({"type": "death"})
    assert e._pending_deaths == 1.0, e._pending_deaths
    print("DEATH_SIGNALS_OK")


def _pendings(e):
    return (
        e._pending_deaths,
        e._pending_gold_lost,
        e._pending_gold_dropped,
        e._pending_xp_lost,
        e._steps_since_death,
    )


def test_wild_indoor_flags_observed():
    e = TextMMOEnv("WildObs")
    base = {
        "type": "room",
        "exits": {},
        "npcs": [],
        "items": [],
        "gold": 0,
        "players": [],
        "is_dungeon": False,
        "dungeon_floor": 0,
        "party_size": 1,
        "gatherables": [],
    }
    e._apply_event(dict(base, id="town_square", shelter=False, wild=False))
    assert e._state["wild"] is False and e._state["shelter"] is False
    # an indoor wild gate: both axes true at once (orthogonal flags)
    e._apply_event(dict(base, id="summit_gatehouse", shelter=True, wild=True))
    assert e._state["wild"] is True and e._state["shelter"] is True
    obs = e._build_obs()
    assert obs["wild_flag"] == 1.0 and obs["indoor_flag"] == 1.0
    e._apply_event(dict(base, id="howling_col", shelter=False, wild=True))
    obs = e._build_obs()
    assert obs["wild_flag"] == 1.0 and obs["indoor_flag"] == 0.0
    # a server predating the flag still parses (additive wire change)
    e._apply_event(dict(base, id="market"))
    obs = e._build_obs()
    assert obs["wild_flag"] == 0.0 and obs["indoor_flag"] == 0.0
    flat = flatten_obs(obs)
    assert len(flat) == OBS_SIZE, (len(flat), OBS_SIZE)
    assert flat[-2:] == [0.0, 0.0], flat[-2:]
    print("WILD_OBS_OK")


def test_maren_obs_features():
    e = TextMMOEnv("ParseObs")
    e._state["quest_remedy_active"] = True
    e._state["quest_tonic_active"] = True
    e._state["quest_tonic_ready"] = True
    e._state["npc_names"] = ["Sister Maren"]
    obs = e._build_obs()
    assert obs["quest3_active"] == 1.0 and obs["quest3_ready"] == 0.0
    assert obs["quest4_active"] == 1.0 and obs["quest4_ready"] == 1.0
    assert obs["quest3_giver_here"] == 1.0 and obs["quest4_giver_here"] == 1.0
    assert obs["quest3_stage"] == "collect" and obs["quest4_stage"] == "ready_turn_in"
    assert len(flatten_obs(obs)) == OBS_SIZE
    print("MAREN_OBS_OK")


def test_presence_obs_flags():
    # #263: presence flags looked ids up in a name->id map (always None),
    # so both vectors were identically zero. Craft a state holding one
    # known item and assert exactly its slots flag.
    iid = ml_env.ITEM_LIST[0]
    name = ml_env.ITEM_ID_TO_NAME[iid]
    e = TextMMOEnv("ParsePresence")
    e._state["item_names"] = [name]
    e._state["inv_names"] = [name]
    obs = e._build_obs()
    assert obs["item_presence"][0] == 1.0
    assert obs["inv_presence"][0] == 1.0
    assert sum(obs["item_presence"]) == 1.0
    assert sum(obs["inv_presence"]) == 1.0
    print("PRESENCE_OBS_OK")


def test_equip_mask_excludes_worn_weapon():
    # #229: id-vs-display-name compare never excluded the worn weapon.
    e = TextMMOEnv("ParseEquip")
    e._state["inv_names"] = ["Rusty Sword"]
    e._state["equipped"] = "Rusty Sword"
    e._build_obs()  # refreshes the per-observation type cache, as in live flow
    assert e._first_inv_typed("weapon") is None
    e._state["equipped"] = "Oak Longbow"
    e._build_obs()
    assert e._first_inv_typed("weapon") == "Rusty Sword"
    print("EQUIP_MASK_OK")


def test_item_presence_vectors():
    # #221: presence looked up ids in a name->id map, so both vectors were
    # all-zeros forever. Ground/inventory display names must light up.
    e = TextMMOEnv("ParsePresence")
    ground_id, held_id = ITEM_LIST[0], ITEM_LIST[1]
    e._state["item_names"] = [ITEM_ID_TO_NAME[ground_id]]
    e._state["inv_names"] = [ITEM_ID_TO_NAME[held_id]]
    obs = e._build_obs()
    assert obs["item_presence"][ITEM_LIST.index(ground_id)] == 1.0
    assert obs["inv_presence"][ITEM_LIST.index(held_id)] == 1.0
    assert sum(obs["item_presence"]) == 1.0, sum(obs["item_presence"])
    assert sum(obs["inv_presence"]) == 1.0, sum(obs["inv_presence"])
    e._state["item_names"] = []
    e._state["inv_names"] = []
    obs = e._build_obs()
    assert sum(obs["item_presence"]) == 0.0
    assert sum(obs["inv_presence"]) == 0.0
    assert len(flatten_obs(obs)) == OBS_SIZE
    print("ITEM_PRESENCE_OK")


def test_dynamic_shard_valuation():
    # dungeon_shard_{n} ids register at floor build, after import: they must
    # still value through live ITEM_DEFS and flag inv_unknown (#194).
    srv.ITEM_DEFS["dungeon_shard_99"] = {"name": "Dungeon Relic +99", "value": 42}
    try:
        assert merchant_value("dungeon_shard_99") == 42
        assert inventory_value(["Dungeon Relic +99"]) == 42
        assert inventory_value(["No Such Item"]) == 0
        orders = [{"item": "Dungeon Relic +99", "price": 100, "seller": "O"}]
        assert best_market_ask(orders, "dungeon_shard_99") == 100
        assert flip_margin("dungeon_shard_99", orders) == (100 - 10) - 42
        assert "Dungeon Relic +99" not in ml_env.ITEM_ID_TO_NAME.values()
    finally:
        del srv.ITEM_DEFS["dungeon_shard_99"]
    print("SHARD_VALUE_OK")


def test_goal_vector_and_dot():
    v = reward_vector(score_gain=1.0, gold_delta=5.0)
    assert set(v) == set(GOAL_AXES) and v["score"] == 1.0 and v["xp"] == 0.0
    assert goal_reward(v, {"w_score": 1.0, "w_gold": 0.5}) == 3.5
    # Partial goals/vectors compose safely (missing keys read 0).
    assert goal_reward({}, {"w_score": 1.0}) == 0.0
    assert goal_reward(v, {}) == 0.0
    print("GOAL_VECTOR_OK")


def test_goal_sampling():
    import random as _random

    for seed in range(5):
        _random.seed(seed)
        g = sample_goal()
        assert set(g) == {"w_" + a for a in GOAL_AXES}
        assert abs(sum(g.values()) - 1.0) < 1e-9
        assert all(x >= 0 for x in g.values())
    _random.seed(7)
    g = sample_goal()
    m = mutate_goal(g)
    assert abs(sum(m.values()) - 1.0) < 1e-9
    assert all(x >= 0 for x in m.values())
    assert m != g  # scale 0.2 moves a spread Dirichlet draw
    assert mutate_goal({}) == {"w_" + a: 1.0 / len(GOAL_AXES) for a in GOAL_AXES}
    print("GOAL_SAMPLE_OK")


def test_env_goal_carriage():
    import random as _random

    _random.seed(3)
    g = sample_goal()
    e = TextMMOEnv("GoalT", goal=g)
    assert e._state is not None and e.goal == g and e.goal is not g
    assert TextMMOEnv("GoalFree").goal is None
    print("GOAL_CARRIAGE_OK")


def test_arrow_family_uses_server_ids():
    assert ml_env.AMMO_IDS == frozenset(srv.AMMO_BONUS)
    assert ml_env._is_ammo_name("Arrow")
    assert ml_env._is_ammo_name("Iron Arrow")
    assert ml_env._is_ammo_name("Steel Arrow")
    assert not ml_env._is_ammo_name("Rusty Nail")
    # The old substring rule counted this as ammo even though the server
    # does not.
    srv.ITEM_DEFS["arrow_charm"] = {"name": "Arrow Charm"}
    try:
        assert not ml_env._is_ammo_name("Arrow Charm")
        state = {"inv_names": ["Arrow Charm"] * 6, "pack_units": None}
        assert pack_units(state) == 6
    finally:
        srv.ITEM_DEFS.pop("arrow_charm", None)
    print("AMMO_IDS_OK")


def test_ammo_best_norm_requires_ranged_weapon():
    # Issue 1056. The server adds ammo damage only when the worn weapon
    # declares an ammo family, so carrying a Blood Arrow with a sword must
    # read as zero.
    carried = ["Blood Arrow", "Oak Longbow", "Rusty Sword"]
    for weapon, expected in (("Oak Longbow", 1.0), ("Rusty Sword", 0.0)):
        e = TextMMOEnv("AmmoObs")
        e._state["inv_names"] = list(carried)
        e._state["equipped"] = weapon
        obs = e._build_obs()
        assert obs["ammo_best_norm"] == expected, (weapon, obs["ammo_best_norm"])
        assert len(flatten_obs(obs)) == OBS_SIZE
    print("AMMO_BEST_NEEDS_RANGED_OK")


def test_ammo_effect_flags_follow_auto_select():
    # #986: the server fires the best arrow in the family and applies that
    # arrow's ammo_effect. Give Fire and Blood the same bonus so the bonus
    # cannot separate the two loads; only the effect flag can.
    tied = dict(srv.AMMO_BONUS)
    tied["fire_arrow"] = 4
    tied["blood_arrow"] = 4
    original = srv.AMMO_BONUS
    srv.AMMO_BONUS = tied
    try:
        loads = {}
        for arrow in ("Fire Arrow", "Blood Arrow"):
            e = TextMMOEnv("AmmoEffect")
            e._state["inv_names"] = ["Oak Longbow", arrow]
            e._state["equipped"] = "Oak Longbow"
            obs = e._build_obs()
            assert obs["ammo_best_norm"] == 1.0, obs["ammo_best_norm"]
            assert 0.0 <= obs["ammo_best_norm"] <= 1.0
            assert len(flatten_obs(obs)) == OBS_SIZE
            loads[arrow] = obs
        fire, blood = loads["Fire Arrow"], loads["Blood Arrow"]
        assert fire["ammo_effect_flags"] != blood["ammo_effect_flags"]
        assert flatten_obs(fire) != flatten_obs(blood)
        assert fire["ammo_effect_burn"] == 1.0 and fire["ammo_effect_heal"] == 0.0
        assert blood["ammo_effect_burn"] == 0.0 and blood["ammo_effect_heal"] == 1.0
        # On a tie the server picks the first id in AMMO_BONUS, so Fire wins.
        both = TextMMOEnv("AmmoTie")
        both._state["inv_names"] = ["Oak Longbow", "Fire Arrow", "Blood Arrow"]
        both._state["equipped"] = "Oak Longbow"
        tie = both._build_obs()
        assert tie["ammo_effect_flags"] == fire["ammo_effect_flags"]
        print("AMMO_EFFECT_FLAGS_OK")
    finally:
        srv.AMMO_BONUS = original


def test_ammo_flags_use_full_stats_ammo():
    # #1078: stats.inv ends at 20 names, but cmd_attack picks from the whole
    # pack. stats.ammo carries the full counts, so the effect flags and best
    # bonus must come from there, not from the truncated name list.
    async def noop(*args, **kwargs):
        return None

    saved = (srv.send, srv.sync_room, srv.broadcast_room)
    srv.send = srv.sync_room = srv.broadcast_room = noop

    class FakeWS:
        remote_address = ("127.0.0.1", 1234)

    name = "AmmoTruncationProbe"
    srv.SCORES.pop(name.lower(), None)
    srv.name_owners.pop(name.lower(), None)
    p = srv.Player(
        ws=FakeWS(), id=99001, name=name, logged_in=True, room="smithy", gold=0
    )
    srv.players[p.id] = p
    srv.add_member(p)
    p.equipped = "oak_longbow"
    try:
        cases = (
            ("fire_arrow", "burn", 0.75, True),
            ("blood_arrow", "heal", 1.0, True),
            ("fire_arrow", "burn", 0.75, False),
        )
        for iid, kind, best, truncated in cases:
            p.inventory[:] = (
                ["rat_tail"] * 20 + [iid] if truncated else ["oak_longbow", iid]
            )
            stats = srv.stats_view(p)
            if truncated:
                assert len(stats["inv"]) == 20 and iid not in stats["inv"]
            assert next(a for a in stats["ammo"] if a["id"] == iid)["count"] == 1
            env = TextMMOEnv(name)
            env._apply_event(stats)
            obs = env._build_obs()
            assert obs["ammo_effect_" + kind] == 1.0, (iid, obs)
            assert obs["ammo_effect_none"] == 0.0, (iid, obs)
            assert obs["ammo_best_norm"] == best, (iid, obs["ammo_best_norm"])
            assert 0.0 <= obs["ammo_best_norm"] <= 1.0
            assert len(flatten_obs(obs)) == OBS_SIZE

        # Multiple variants behind the cap: duplicates collapse and the
        # highest bonus wins, as in cmd_attack.
        p.inventory[:] = ["rat_tail"] * 20 + [
            "fire_arrow",
            "blood_arrow",
            "fire_arrow",
        ]
        stats = srv.stats_view(p)
        fire_count = next(a for a in stats["ammo"] if a["id"] == "fire_arrow")
        assert fire_count["count"] == 2
        env = TextMMOEnv(name)
        env._apply_event(stats)
        obs = env._build_obs()
        assert obs["ammo_effect_heal"] == 1.0
        assert obs["ammo_effect_burn"] == 0.0
        assert obs["ammo_best_norm"] == 1.0

        # Snapshots without ammo counts keep the old name-list behavior.
        for weapon in ("Oak Longbow", "Rusty Sword", None):
            env = TextMMOEnv(name)
            env._apply_event({"type": "stats", "inv": [], "equipped": weapon})
            obs = env._build_obs()
            assert obs["ammo_effect_none"] == 1.0
            assert obs["ammo_best_norm"] == 0.0
        env = TextMMOEnv(name)
        env._apply_event(
            {
                "type": "stats",
                "inv": ["Oak Longbow"],
                "equipped": "Oak Longbow",
                "ammo": [],
            }
        )
        obs = env._build_obs()
        assert obs["ammo_effect_none"] == 1.0 and obs["ammo_best_norm"] == 0.0
    finally:
        srv.send, srv.sync_room, srv.broadcast_room = saved
        srv.remove_member(p)
        srv.players.pop(p.id, None)
        srv.SCORES.pop(name.lower(), None)
        srv.name_owners.pop(name.lower(), None)
    print("AMMO_FULL_STATS_FLAGS_OK")


def test_ammo_counts_reset_when_stats_omit_ammo():
    # #1081 review: a stats event without ammo must drop earlier counts.
    env = TextMMOEnv("AmmoStaleCounts")
    env._apply_event(
        {
            "type": "stats",
            "inv": ["Oak Longbow"],
            "equipped": "Oak Longbow",
            "ammo": [{"id": "fire_arrow", "count": 1}],
        }
    )
    obs = env._build_obs()
    assert obs["ammo_effect_burn"] == 1.0 and obs["ammo_best_norm"] == 0.75
    env._apply_event({"type": "stats", "inv": [], "equipped": "Oak Longbow"})
    obs = env._build_obs()
    assert obs["ammo_effect_none"] == 1.0
    assert obs["ammo_effect_burn"] == 0.0
    assert obs["ammo_best_norm"] == 0.0
    # The next snapshot has no counts either, so the name list decides.
    env._apply_event(
        {
            "type": "stats",
            "inv": ["Oak Longbow", "Blood Arrow"],
            "equipped": "Oak Longbow",
        }
    )
    obs = env._build_obs()
    assert obs["ammo_effect_heal"] == 1.0
    assert obs["ammo_effect_none"] == 0.0
    assert obs["ammo_best_norm"] == 1.0
    print("AMMO_COUNTS_RESET_OK")


def test_ammo_counts_ignore_malformed_rows():
    # #1081 review: only integer counts may select an arrow.
    bad = (
        [],
        None,
        "fire_arrow",
        [{"id": "fire_arrow", "count": "one"}],
        [{"id": "fire_arrow", "count": True}],
        [{"id": "fire_arrow", "count": 1.5}],
        [{"id": "fire_arrow", "count": 0}],
        [{"id": "fire_arrow", "count": -1}],
        [{"count": 1}],
        ["fire_arrow"],
    )
    for rows in bad:
        env = TextMMOEnv("AmmoBadCounts")
        env._apply_event(
            {
                "type": "stats",
                "inv": ["Oak Longbow", "Fire Arrow"],
                "equipped": "Oak Longbow",
                "ammo": rows,
            }
        )
        obs = env._build_obs()
        assert obs["ammo_effect_none"] == 1.0, rows
        assert obs["ammo_effect_burn"] == 0.0, rows
        assert obs["ammo_best_norm"] == 0.0, rows
    env = TextMMOEnv("AmmoStringCount")
    env._apply_event(
        {
            "type": "stats",
            "inv": ["Oak Longbow"],
            "equipped": "Oak Longbow",
            "ammo": [{"id": "fire_arrow", "count": " 1 "}],
        }
    )
    obs = env._build_obs()
    assert obs["ammo_effect_burn"] == 1.0
    assert obs["ammo_best_norm"] == 0.75
    print("AMMO_MALFORMED_COUNTS_OK")


def test_ammo_effect_contracts():
    assert ml_env.AMMO_EFFECT_KINDS == ("none", "burn", "heal")
    assert OBS_SIZE == 225 + len(ml_env.AMMO_EFFECT_KINDS)
    e = TextMMOEnv("AmmoContract")
    e._state["inv_names"] = ["Oak Longbow", "Fire Arrow"]
    e._state["equipped"] = "Oak Longbow"
    obs = e._build_obs()
    assert len(flatten_obs(obs)) == OBS_SIZE
    assert flatten_obs(obs)[-len(ml_env.AMMO_EFFECT_KINDS) :] == (
        obs["ammo_effect_flags"]
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from ml.plugins.linear import LinearPlugin

    plugin = LinearPlugin(epsilon=0.0)
    assert plugin._agent.obs_size == OBS_SIZE
    assert len(plugin._agent.weights[0]) == OBS_SIZE
    assert 0 <= plugin.act(obs, "AmmoContract", None) < N_ACTIONS
    from ml.ml_client import LinearQAgent

    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old.json")
        with open(old, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "weights": [[0.0] * 225 for _ in range(N_ACTIONS)],
                    "bias": [0.0] * N_ACTIONS,
                    "training_steps": 0,
                    "obs_size": 225,
                    "n_actions": N_ACTIONS,
                },
                fh,
            )
        # A 225-input checkpoint is refused with a warning, not reshaped.
        assert LinearQAgent(OBS_SIZE, N_ACTIONS).load(old) is False
    with open(os.path.join(root, "README.md"), encoding="utf-8") as readme:
        docs = readme.read()
    assert f"({OBS_SIZE} obs dims, {N_ACTIONS} actions)" in docs, docs[:200]
    for token in (
        "ammo_effect_none",
        "ammo_effect_burn",
        "ammo_effect_heal",
        "ammo-select action",
        "cannot load into the 228-input environment",
    ):
        assert token in docs, token
    print("AMMO_EFFECT_CONTRACTS_OK")


def test_readme_ml_dimensions_match_env():
    with open(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
        ),
        encoding="utf-8",
    ) as readme:
        docs = readme.read()
    expected = f"({OBS_SIZE} obs dims, {N_ACTIONS} actions)"
    assert expected in docs, (expected, "README ML dimensions are stale")
    print("README_ML_DIMENSIONS_OK")


def test_reader_survives_recursion_error():
    class FakeWS:
        def __init__(self, frames):
            self.frames = frames

        def __aiter__(self):
            async def frames():
                for raw in self.frames:
                    yield raw

            return frames()

    deep = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    env = TextMMOEnv("ReaderDepth")
    overflow = b'{"type":"stats","buffs":{"attack":{"amount":1e9999,"remaining":1}}}'
    env.ws = FakeWS([deep, overflow, b'{"type":"welcome","protocol_version":2}'])
    asyncio.run(env._reader())
    assert env._state["server_version"] == 2
    print("READER_RECURSION_OK")


test_reader_survives_recursion_error()
test_readme_ml_dimensions_match_env()
test_discounted_commission_parses()
test_pack_masks_from_server_count()
test_arrow_family_uses_server_ids()
test_ammo_best_norm_requires_ranged_weapon()
test_ammo_effect_flags_follow_auto_select()
test_ammo_flags_use_full_stats_ammo()
test_ammo_counts_reset_when_stats_omit_ammo()
test_ammo_counts_ignore_malformed_rows()
test_ammo_effect_contracts()
test_priced_market_post()
test_parameterized_bounty()
test_gate_matrix()
test_maren_mirror_and_stages()
test_quest_transitions_all_four()
test_event_parsing_maren_combat_inventory()
test_death_accounting_reaches_reward_signals()
test_wild_indoor_flags_observed()
test_maren_obs_features()
test_presence_obs_flags()
test_equip_mask_excludes_worn_weapon()
test_item_presence_vectors()
test_dynamic_shard_valuation()
test_goal_vector_and_dot()
test_goal_sampling()
test_env_goal_carriage()
print("ALL_ENV_PARSE_OK")
