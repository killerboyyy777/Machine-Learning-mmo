"""Scripted-bot baseline unit tests (no live server needed).

Run from the repo root:  python tests/test_scripted_bots.py
Covers #34: role policies pick the expected action from crafted states,
and --scripted=mixed assigns roles round-robin.
"""

import os
import sys
import types

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
)
from ml.ml_env import ACTIONS, TextMMOEnv


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

# --- market role: no snapshot yet -> list ---
env = fresh_env()
env._state["market_state"] = None
assert selected(MarketFlipperPolicy(), env) == "market_list"
print("MARKET_LIST_OK")

# --- market role: snapshot fresh, nothing to post -> buy ---
env = fresh_env()
env._state["market_state"] = {"orders": []}
assert selected(MarketFlipperPolicy(), env) == "market_buy"
print("MARKET_BUY_OK")

# --- maker role: fresh snapshot, nothing to post -> buys (bid side) ---
env = fresh_env()
env._state["market_state"] = {"orders": []}
assert selected(MarketMakerPolicy(), env) == "market_buy"
print("MAKER_BUY_OK")

# --- maker role: stale snapshot -> list first ---
env = fresh_env()
env._state["market_state"] = None
assert selected(MarketMakerPolicy(), env) == "market_list"
print("MAKER_LIST_OK")

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
):
    env = fresh_env()
    env._state["hp"] = 2
    env._state["inv_names"] = ["Healing Herb"]
    got = selected(cls(), env)
    assert got == "use", (cls.__name__, got)
print("HEAL_FIRST_OK")

# --- mixed assignment round-robins roles ---
farm = types.SimpleNamespace(
    args=types.SimpleNamespace(name_prefix="T", url="ws://x", scripted="mixed")
)
roles = [BotRunner(i, farm).policy.name for i in range(6)]
assert roles == ["gather", "dungeon", "market", "maker", "commissioner",
                 "gather"], roles
farm.args.scripted = "dungeon"
assert BotRunner(0, farm).policy.name == "dungeon"
farm.args.scripted = "commissioner"
assert BotRunner(0, farm).policy.name == "commissioner"
farm.args.scripted = "none"
assert BotRunner(0, farm).policy is None
print("MIXED_ASSIGN_OK")

assert set(SCRIPTED_POLICIES) == {"gather", "dungeon", "market", "maker",
                                  "commissioner"}
print("ALL_SCRIPTED_OK")
