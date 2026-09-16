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
from ml.ml_env import TextMMOEnv, ACTIONS
from ml.ml_botfarm import (
    SCRIPTED_POLICIES, GatherSellPolicy, DungeonClearerPolicy,
    MarketFlipperPolicy, BotRunner,
)


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

# --- hurt bot heals first regardless of role ---
for cls in (GatherSellPolicy, DungeonClearerPolicy, MarketFlipperPolicy):
    env = fresh_env()
    env._state["hp"] = 2
    env._state["inv_names"] = ["Healing Herb"]
    got = selected(cls(), env)
    assert got == "use", (cls.__name__, got)
print("HEAL_FIRST_OK")

# --- mixed assignment round-robins roles ---
farm = types.SimpleNamespace(
    args=types.SimpleNamespace(name_prefix="T", url="ws://x", scripted="mixed"))
roles = [BotRunner(i, farm).policy.name for i in range(4)]
assert roles == ["gather", "dungeon", "market", "gather"], roles
farm.args.scripted = "dungeon"
assert BotRunner(0, farm).policy.name == "dungeon"
farm.args.scripted = "none"
assert BotRunner(0, farm).policy is None
print("MIXED_ASSIGN_OK")

assert set(SCRIPTED_POLICIES) == {"gather", "dungeon", "market"}
print("ALL_SCRIPTED_OK")
