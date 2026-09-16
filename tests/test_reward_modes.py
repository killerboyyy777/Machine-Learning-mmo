"""Specialist reward-mode unit tests (no live server needed).

Run from the repo root:  python tests/test_reward_modes.py
Covers #36 (econ mode) and #37 (xp mode): mode validation, inventory
pricing, and per-mode reward math via the pure _compute_reward helper.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv
from ml.ml_env import (
    TextMMOEnv, REWARD_MODES, inventory_value, merchant_value,
    XP_LEVEL_BONUS, ECON_INV_LAMBDA,
)

# --- mode validation ---
assert set(REWARD_MODES) == {"score", "xp", "econ"}
env_default = TextMMOEnv("ModeDefault")
assert env_default.reward_mode == "score"
try:
    TextMMOEnv("ModeBad", reward_mode="fame")
    raise SystemExit("FAIL: invalid reward_mode accepted")
except ValueError:
    pass
print("MODE_VALIDATION_OK")

# --- inventory_value pricing ---
assert inventory_value([]) == 0
assert inventory_value(None) == 0
assert inventory_value(["No Such Item XYZ"]) == 0
iid, idef = next(iter(srv.ITEM_DEFS.items()))
name, value = idef["name"], merchant_value(iid)
assert inventory_value([name]) == value, (name, value)
assert inventory_value([name, "No Such Item XYZ"]) == value
print(f"INVENTORY_VALUE_OK ({iid}={value}g)")

# --- xp mode: pure XP, ignores score/gold/inv/social ---
env_xp = TextMMOEnv("ModeXP", reward_mode="xp")
r = env_xp._compute_reward(score_gain=100.0, xp_gain=8.0, levels=1,
                           gold_delta=50.0, inv_delta=30.0, party_before=1)
assert r == 8.0 + XP_LEVEL_BONUS * 1, r
r0 = env_xp._compute_reward(0.0, 0.0, 0, 0.0, 0.0, 1)
assert r0 == 0.0, r0
print("XP_MODE_OK")

# --- econ mode: gold + lambda * inv delta, ignores score/xp ---
env_econ = TextMMOEnv("ModeEcon", reward_mode="econ")
r = env_econ._compute_reward(score_gain=100.0, xp_gain=40.0, levels=2,
                             gold_delta=7.0, inv_delta=-3.0, party_before=1)
assert r == 7.0 + ECON_INV_LAMBDA * -3.0, r
print("ECON_MODE_OK")

# --- score mode unchanged: solo passes score through, social still applies ---
env_score = TextMMOEnv("ModeScore")
r = env_score._compute_reward(2.5, 99.0, 3, 99.0, 99.0, 1)
assert r == 2.5, r
env_score._state["other_players"] = 1
env_score._state["party_size"] = 2
env_score._state["score"] = 0.0
r = env_score._compute_reward(2.5, 0.0, 0, 0.0, 0.0, 2)
assert abs(r - 2.55) < 1e-9, r  # social only (already grouped)
r = env_score._compute_reward(2.5, 0.0, 0, 0.0, 0.0, 1)
assert abs(r - 3.05) < 1e-9, r  # social + formation bonus on 1->2
print("SCORE_MODE_OK")

print("ALL_REWARD_MODES_OK")
