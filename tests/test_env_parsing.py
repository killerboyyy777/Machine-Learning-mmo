"""Offline env fidelity tests (#191/#194/#183): no server needed.

Run from the repo root:  python tests/test_env_parsing.py
Covers: commission_list parsing (plain + discounted-rate lines); pack
masking from the server's authoritative pack count; priced market posts;
parameterized bounties; merchant/affordability/party gates (#194);
remedy/tonic quest mirror, stages, transitions, events, obs (#183); and
dynamic-shard valuation through live ITEM_DEFS (#194).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))

import ml_env
from ml_env import (ITEM_ID_TO_NAME, ITEM_LIST, MERCHANT_NAMES, OBS_SIZE,
                    QUESTS, TextMMOEnv, _parse_commissions, best_market_ask,
                    flatten_obs, flip_margin, inventory_value,
                    merchant_value, pack_full, pack_units, quest_stage,
                    quest_transitions)
import server as srv  # noqa: E402  (ml_env extends sys.path on import)


def test_discounted_commission_parses():
    plain = "#12: slay 5x Giant Rat -- reward 25g + 50xp (posted by Alice)"
    disc = "#13: slay 5x Giant Rat \u2014 reward 25g + 50xp (your rate: x0.5) (posted by Alice)"
    hyphen = "#14: slay 1x Wolf - reward 10g + 5xp (your rate: x0.1) (posted by Bob)"
    rows = _parse_commissions("\n".join([plain, disc, hyphen]))
    assert len(rows) == 3, rows
    assert rows[0]["id"] == 12 and rows[0]["rate"] is None and rows[0]["poster"] == "Alice"
    assert rows[1]["id"] == 13 and rows[1]["rate"] == 0.5 and rows[1]["poster"] == "Alice"
    assert rows[1]["gold"] == 25 and rows[1]["xp"] == 50
    assert rows[2]["id"] == 14 and rows[2]["rate"] == 0.1 and rows[2]["target"] == "Wolf"
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
    assert e._action_to_cmd("buy_arrows") == {"cmd": "buy", "item": "arrow"}
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
    e = _econ_env(inv_names=["Healing Herb", "Healing Herb"],
                  market_state={"orders": [{"item": "Healing Herb", "price": 20,
                                             "seller": "Other"}]})
    cmd = e._action_to_cmd("market_post")
    assert cmd == {"cmd": "market_post", "item": "Healing Herb", "price": 19}, cmd
    # Unlisted: merchant value + 1 (value defaults to 1 without a value key).
    e = _econ_env(inv_names=["Healing Herb", "Healing Herb"],
                  market_state={"orders": []})
    cmd = e._action_to_cmd("market_post")
    assert cmd == {"cmd": "market_post", "item": "Healing Herb", "price": 2}, cmd
    print("PRICED_POST_OK")


def test_parameterized_bounty():
    e = _econ_env(npc_names=["Giant Rat"], gold=50)
    cmd = e._action_to_cmd("commission_post")
    assert cmd == {"cmd": "commission_post", "target": "Giant Rat",
                   "required_kills": 1, "reward_gold": 10,
                   "reward_xp": 0}, cmd
    # Broke: still a valid 0g listing (affordability priced in, not gated).
    e = _econ_env(npc_names=["Giant Rat"], gold=0)
    assert e._action_to_cmd("commission_post")["reward_gold"] == 0
    # Blind: server's own "rat" default (never masked, like the old post).
    cmd = _econ_env(npc_names=[], gold=0)._action_to_cmd("commission_post")
    assert cmd["target"] == "rat" and cmd["reward_gold"] == 0, cmd
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
        ("market_buy", {"market_state": {"orders": [{"item": "X", "price": 50, "seller": "O"}]},
                        "gold": 10}, True),
        ("market_buy", {"market_state": {"orders": [{"item": "X", "price": 50, "seller": "O"}]},
                        "gold": 100}, False),
        # party_leave/info are deliberately UNGATED (membership isn't
        # client-verifiable: room-event party_size lags joins, so gating
        # blocks the valid info right after accept -- proven by live test).
    ]
    for action, over, expect_none in rows:
        got = _econ_env(**over)._action_to_cmd(action)
        assert (got is None) == expect_none, (action, over, got)
    assert _econ_env(party_size=1)._action_to_cmd("party_leave") == {"cmd": "party_leave"}
    assert _econ_env(party_size=1)._action_to_cmd("party_info") == {"cmd": "party_info"}
    # Mappable states produce real commands, not just non-None.
    assert _econ_env(npc_names=["Wandering Merchant"],
                     inv_names=herbs)._action_to_cmd("sell") == {"cmd": "sell", "item": "Healing Herb"}
    assert _econ_env(market_state={"orders": [{"item": "X", "price": 50, "seller": "O"}]},
                     gold=100)._action_to_cmd("market_buy") == {"cmd": "market_buy"}
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
        assert quest_stage({pre + "_active": True, pre + "_ready": True}, qid) == "ready_turn_in"
        assert quest_stage({pre + "_active": True}, qid) == "collect"
        assert quest_stage({}, qid) == "no_quest"
    print("MAREN_MIRROR_OK")


def test_quest_transitions_all_four():
    specs = (("guard_charm", "quest_guard_active", "guard_charm_crafted"),
             ("delver", "quest_delver_active", "quest_delver_ready"),
             ("remedy", "quest_remedy_active", "quest_remedy_ready"),
             ("tonic", "quest_tonic_active", "quest_tonic_ready"))
    for qid, akey, rkey in specs:
        t = quest_transitions({akey: False}, {akey: True})
        assert t[qid] == {"accepted": True, "turned_in": False, "became_ready": False}, (qid, t)
        t = quest_transitions({akey: True}, {akey: False})
        assert t[qid] == {"accepted": False, "turned_in": True, "became_ready": False}, (qid, t)
        t = quest_transitions({rkey: False}, {rkey: True})
        assert t[qid] == {"accepted": False, "turned_in": False, "became_ready": True}, (qid, t)
        t = quest_transitions({}, {})
        assert t[qid] == {"accepted": False, "turned_in": False, "became_ready": False}, (qid, t)
    print("QUEST_TRANSITIONS_OK")


def test_event_parsing_maren_combat_inventory():
    e = TextMMOEnv("ParseEv")
    e._apply_event({"type": "stats", "hp": 20, "max_hp": 20, "gold": 5, "score": 1.0,
                    "quest_remedy_active": True, "quest_remedy_ready": False,
                    "quest_tonic_active": True, "quest_tonic_ready": True})
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


test_discounted_commission_parses()
test_pack_masks_from_server_count()
test_priced_market_post()
test_parameterized_bounty()
test_gate_matrix()
test_maren_mirror_and_stages()
test_quest_transitions_all_four()
test_event_parsing_maren_combat_inventory()
test_maren_obs_features()
test_item_presence_vectors()
test_dynamic_shard_valuation()
print("ALL_ENV_PARSE_OK")
