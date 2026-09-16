"""Curriculum unit tests (no live server needed).

Run from the repo root:  python tests/test_curriculum.py
Covers #59: stage validation, per-stage action gating, mask integration,
and score-threshold auto-advance.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.ml_env import (
    ACTIONS,
    CURRICULUM_STAGES,
    CURRICULUM_THRESHOLDS,
    TextMMOEnv,
)

assert CURRICULUM_STAGES == ("rats", "dungeons", "crafting", "full")
assert len(CURRICULUM_THRESHOLDS) == 4

# --- validation + defaults ---
env3 = TextMMOEnv("Curr3")
assert env3.curriculum_stage == 3 and env3.curriculum_auto is False
try:
    TextMMOEnv("CurrBad", curriculum_stage=4)
    raise SystemExit("FAIL: bad curriculum_stage accepted")
except ValueError:
    pass
print("CURR_VALIDATION_OK")

# --- stage 3 (default): nothing gated ---
assert not any(env3._curriculum_gated(a) for a in ACTIONS)
print("CURR_FULL_OK")

# --- stage 0: dungeons, crafting, economy locked; basics open ---
env0 = TextMMOEnv("Curr0", curriculum_stage=0)
for a in ("move_enter", "move_down", "quest2_accept", "craft", "craft_charm",
          "gather", "quest_accept", "market_post", "market_buy",
          "commission_post", "commission_fill"):
    assert env0._curriculum_gated(a), a
for a in ("attack", "take", "rest", "look", "buy", "sell", "equip", "use",
          "heal", "quest_list", "market_list", "party_invite"):
    assert not env0._curriculum_gated(a), a
print("CURR_STAGE0_OK")

# --- stage 1: dungeons open, crafting/economy still locked ---
env1 = TextMMOEnv("Curr1", curriculum_stage=1)
assert not env1._curriculum_gated("move_enter")
assert not env1._curriculum_gated("quest2_accept")
assert env1._curriculum_gated("craft")
assert env1._curriculum_gated("market_post")
print("CURR_STAGE1_OK")

# --- stage 2: crafting open, economy still locked ---
env2 = TextMMOEnv("Curr2", curriculum_stage=2)
assert not env2._curriculum_gated("craft")
assert not env2._curriculum_gated("gather")
assert not env2._curriculum_gated("quest_accept")
assert env2._curriculum_gated("market_post")
assert env2._curriculum_gated("commission_fill")
print("CURR_STAGE2_OK")

# --- mask integration: dungeon door visible but locked at stage 0 ---
env0._state["exits"] = ["enter", "north"]
mask0 = env0.valid_action_mask()
assert mask0[ACTIONS.index("move_enter")] == 0
env1._state["exits"] = ["enter", "north"]
mask1 = env1.valid_action_mask()
assert mask1[ACTIONS.index("move_enter")] == 1
print("CURR_MASK_OK")

# --- auto-advance on score thresholds ---
auto = TextMMOEnv("CurrAuto", curriculum_stage=0, curriculum_auto=True)
auto._state["score"] = 5.0
auto._maybe_advance_curriculum()
assert auto.curriculum_stage == 0
auto._state["score"] = 15.0
auto._maybe_advance_curriculum()
assert auto.curriculum_stage == 1
auto._state["score"] = 100.0
auto._maybe_advance_curriculum()
assert auto.curriculum_stage == 3
manual = TextMMOEnv("CurrManual", curriculum_stage=0)
manual._state["score"] = 100.0
manual._maybe_advance_curriculum()
assert manual.curriculum_stage == 0
print("CURR_AUTO_OK")

# --- misconfigured thresholds never crash auto-advance ---
import ml.ml_env as _envmod

_saved = _envmod.CURRICULUM_THRESHOLDS
_envmod.CURRICULUM_THRESHOLDS = (0, 10)
try:
    short = TextMMOEnv("CurrShort", curriculum_stage=0, curriculum_auto=True)
    short._state["score"] = 100.0
    short._maybe_advance_curriculum()
    assert short.curriculum_stage == 0
finally:
    _envmod.CURRICULUM_THRESHOLDS = _saved
print("CURR_SHORT_THRESHOLDS_OK")

print("ALL_CURRICULUM_OK")
