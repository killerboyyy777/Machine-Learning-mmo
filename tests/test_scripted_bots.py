"""Scripted-bot baseline unit tests (no live server needed).

Run from the repo root:  python tests/test_scripted_bots.py
Covers #34: role policies pick the expected action from crafted states,
and --scripted=mixed assigns roles round-robin.
"""

import os
import sys
import types
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv
from ml.ml_botfarm import (
    SCRIPTED_POLICIES,
    BotRunner,
    CommissionerPolicy,
    DungeonClearerPolicy,
    GatherSellPolicy,
    MarketFlipperPolicy,
    MarketMakerPolicy,
    build_role_list,
    parse_roles,
)
from ml.ml_env import ACTIONS, QUEST_GIVER_NAME, TextMMOEnv
from ml.plugins.scripted import CrafterPolicy, PartyLeaderPolicy, QuesterPolicy


def fresh_env():
    env = TextMMOEnv("ScriptTest")
    env._state["hp"] = 20
    env._state["max_hp"] = 20
    return env


def selected(policy, env):
    return ACTIONS[policy.select(env)]


# --- gather role: nodes present -> gather ---
env = fresh_env()
env._state["gatherables"] = ["Iron Vein"]
assert selected(GatherSellPolicy(), env) == "gather", selected(GatherSellPolicy(), env)
print("GATHER_OK")

# --- gather role: loose gold -> take first ---
env = fresh_env()
env._state["room_gold"] = 5
assert selected(GatherSellPolicy(), env) == "take"
print("TAKE_OK")

# --- dungeon role: hostile present -> attack ---
hostile = next(n["name"] for n in srv.WORLD["npcs"].values() if n.get("hostile"))
env = fresh_env()
env._state["npc_names"] = [hostile]
assert selected(DungeonClearerPolicy(), env) == "attack"
print(f"ATTACK_OK ({hostile})")

# --- dungeon role: no hostiles, dungeon door open -> enter ---
env = fresh_env()
env._state["exits"] = ["enter", "north"]
assert selected(DungeonClearerPolicy(), env) == "move_enter"
print("DUNGEON_ENTER_OK")

# --- dungeon role: giver present, no quest -> accept (not turn_in) (#235) ---
env = fresh_env()
env._state["npc_names"] = [QUEST_GIVER_NAME]
env._state["quest_delver_active"] = False
assert selected(DungeonClearerPolicy(), env) == "quest2_accept"
# --- dungeon role: active + ready -> turn_in; active-but-not-ready must
# fall through to the descent instead of repeating turn-in (#357) ---
env._state["quest_delver_active"] = True
env._state["quest_delver_ready"] = True
assert selected(DungeonClearerPolicy(), env) == "quest2_turn_in"
# Active-but-not-ready falls through to the descent on a fresh observation
# (masks cache per-observation, so set exits before the first select).
env = fresh_env()
env._state["npc_names"] = [QUEST_GIVER_NAME]
env._state["quest_delver_active"] = True
env._state["quest_delver_ready"] = False
env._state["exits"] = ["enter", "north"]
assert selected(DungeonClearerPolicy(), env) == "move_enter"
print("DELVER_ACCEPT_OK")

# --- market role: no snapshot yet -> list ---
env = fresh_env()
env._state["market_state"] = None
assert selected(MarketFlipperPolicy(), env) == "market_list"
print("MARKET_LIST_OK")

# --- market role: empty book -> buy masked, re-lists instead ---
env = fresh_env()
env._state["market_state"] = {"orders": []}
assert selected(MarketFlipperPolicy(), env) == "market_list"
print("MARKET_BUY_MASKED_OK")

# --- market role: affordable order on the book -> buy ---
env = fresh_env()
env._state["gold"] = 100
env._state["market_state"] = {"orders": [{"item": "Healing Herb", "price": 10,
                                           "seller": "Other"}]}
assert selected(MarketFlipperPolicy(), env) == "market_buy"
print("MARKET_BUY_OK")

# --- maker role: fresh snapshot, nothing to post -> buys (bid side) ---
env = fresh_env()
env._state["gold"] = 100
env._state["market_state"] = {"orders": [{"item": "Healing Herb", "price": 10,
                                           "seller": "Other"}]}
assert selected(MarketMakerPolicy(), env) == "market_buy"
print("MAKER_BUY_OK")

# --- maker role: stale snapshot -> list first ---
env = fresh_env()
env._state["gold"] = 10
env._state["market_state"] = None
assert selected(MarketMakerPolicy(), env) == "market_list"
print("MAKER_LIST_OK")

# --- maker role: broke (no gold, nothing sellable) -> hold, never wander
# into hostile rooms (#357) ---
env = fresh_env()
env._state["gold"] = 0
env._state["inv_names"] = []
assert selected(MarketMakerPolicy(), env) == "look"
print("MAKER_BROKE_OK")

# --- circuit breaker: same action + frozen state 15x -> escalate (#357) ---
env = fresh_env()
env._state["npc_names"] = [QUEST_GIVER_NAME]
env._state["quest_delver_active"] = True
env._state["quest_delver_ready"] = False
env._state["exits"] = ["enter", "north"]
env._step_count = 0
pol = DungeonClearerPolicy()
for _ in range(14):
    env._step_count += 1
    assert selected(pol, env) == "move_enter"
env._step_count += 1
assert selected(pol, env) != "move_enter"
print("STUCK_BREAK_OK")

# --- commissioner role: empty board, even step -> list ---
env = fresh_env()
env._state["open_commissions"] = []
env._step_count = 0
assert selected(CommissionerPolicy(), env) == "commission_list"
print("COMM_LIST_OK")

# --- commissioner role: empty board, odd step -> (re)stock via post ---
env = fresh_env()
env._state["open_commissions"] = []
env._step_count = 1
assert selected(CommissionerPolicy(), env) == "commission_post"
print("COMM_POST_OK")

# --- commissioner role: hostile present -> attack first ---
env = fresh_env()
env._state["npc_names"] = [hostile]
env._state["open_commissions"] = []
assert selected(CommissionerPolicy(), env) == "attack"
print("COMM_ATTACK_OK")

# --- commissioner role: other's bounty present -> fill richest ---
env = fresh_env()
env._state["open_commissions"] = [
    {"id": 7, "kills": 1, "target": "rat", "gold": 10, "xp": 0,
     "rate": 1.0, "poster": "Other"},
]
assert selected(CommissionerPolicy(), env) == "commission_fill"
print("COMM_FILL_OK")

# --- commissioner role: only own bounty -> cancel oldest ---
env = fresh_env()
env._state["open_commissions"] = [
    {"id": 9, "kills": 1, "target": "rat", "gold": 0, "xp": 0,
     "rate": None, "poster": "ScriptTest"},
]
assert selected(CommissionerPolicy(), env) == "commission_cancel"
print("COMM_CANCEL_OK")

# --- hurt bot heals first regardless of role ---
for cls in (
    GatherSellPolicy,
    DungeonClearerPolicy,
    MarketFlipperPolicy,
    MarketMakerPolicy,
    CommissionerPolicy,
    QuesterPolicy,
    CrafterPolicy,
    PartyLeaderPolicy,
):
    env = fresh_env()
    env._state["hp"] = 2
    env._state["inv_names"] = ["Healing Herb"]
    got = selected(cls(), env)
    assert got == "use", (cls.__name__, got)
print("HEAL_FIRST_OK")

# --- quester role: giver present, guard quest inactive -> accept ---
env = fresh_env()
env._state["npc_names"] = [QUEST_GIVER_NAME]
env._state["quest_guard_active"] = False
env._state["guard_charm_crafted"] = False
assert selected(QuesterPolicy(), env) == "quest_accept", selected(QuesterPolicy(), env)
env._state["quest_guard_active"] = True
env._state["guard_charm_crafted"] = True
assert selected(QuesterPolicy(), env) == "quest_turn_in"
print("QUESTER_ACCEPT_OK")

# --- quester role: no giver, charm mats held -> craft_charm ---
env = fresh_env()
env._state["npc_names"] = []
env._state["quest_guard_active"] = True
env._state["inv_names"] = ["Treant Bark", "Troll Hide", "Ectoplasm"]
assert selected(QuesterPolicy(), env) == "craft_charm"
print("QUESTER_CRAFT_OK")

# --- crafter role: iron ore held -> craft_arrows ---
env = fresh_env()
env._state["npc_names"] = []
env._state["inv_names"] = ["Iron Ore"]
assert selected(CrafterPolicy(), env) == "craft_arrows"
print("CRAFTER_CRAFT_OK")

# --- party-leader role: another player present, even step -> invite ---
env = fresh_env()
env._state["player_names"] = ["PartyTest", "Other"]
env._state["party_size"] = 1
env._state["npc_names"] = []
env._step_count = 0
assert selected(PartyLeaderPolicy(), env) == "party_invite"
env._step_count = 1
assert selected(PartyLeaderPolicy(), env) == "party_accept"
print("PARTY_LEADER_INVITE_OK")

# --- party-leader role: already in a party -> descend ---
env = fresh_env()
env._state["player_names"] = ["PartyTest"]
env._state["party_size"] = 2
env._state["npc_names"] = []
env._state["exits"] = ["enter", "north"]
assert selected(PartyLeaderPolicy(), env) == "move_enter"
print("PARTY_LEADER_DESCEND_OK")

# --- mixed assignment round-robins roles ---
farm = types.SimpleNamespace(
    args=types.SimpleNamespace(name_prefix="T", url="ws://x", scripted="mixed",
                               bots=8, roles=None),
    role_list=build_role_list(
        types.SimpleNamespace(scripted="mixed", bots=8, roles=None)
    ),
)
roles = [BotRunner(i, farm).policy.name for i in range(8)]
assert roles == ["gather", "dungeon", "market", "maker", "commissioner",
                 "quester", "crafter", "party_leader"], roles
farm.args.scripted = "dungeon"
farm.role_list = build_role_list(farm.args)
assert BotRunner(0, farm).policy.name == "dungeon"
farm.args.scripted = "commissioner"
farm.role_list = build_role_list(farm.args)
assert BotRunner(0, farm).policy.name == "commissioner"
farm.args.scripted = "quester"
farm.role_list = build_role_list(farm.args)
assert BotRunner(0, farm).policy.name == "quester"
farm.args.scripted = "none"
farm.role_list = build_role_list(farm.args)
assert BotRunner(0, farm).policy is None
print("MIXED_ASSIGN_OK")

# --- --roles explicit split ---
farm.args.roles = "gather:8,dungeon:4,market:3,maker:4,commissioner:2,flex:gather"
farm.args.bots = 21
farm.role_list = build_role_list(farm.args)
got = [BotRunner(i, farm).policy.name for i in range(21)]
assert Counter(got) == {"gather": 8, "dungeon": 4, "market": 3, "maker": 4,
                        "commissioner": 2}, Counter(got)
assert parse_roles(farm.args.roles, farm.args.bots) == farm.role_list
print("ROLES_SPLIT_OK")

assert set(SCRIPTED_POLICIES) == {"gather", "dungeon", "market", "maker",
                                  "commissioner", "quester", "crafter",
                                  "party_leader"}
print("ALL_SCRIPTED_OK")
