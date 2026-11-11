"""Attack mask judges the whole room, not the weakest single hostile (#590).

Run from the repo root:  python tests/test_env_attack_mask.py

`MOB_EXTRA_SPAWNS` stacks several copies of one wild template into a room and
the room payload lists one entry per live instance. Rating the fight against
the single best target made a four-wyrm room look winnable at level 15+, so a
respawned agent walked straight back in and died: gate seed 701 recorded 321
deaths in 20 minutes.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ml"))

from ml_env import ACTIONS, TextMMOEnv

import server as srv

ATTACK = ACTIONS.index("attack")


def _attack_allowed(room, npc_names, level, max_hp=None):
    """True when the env mask offers attack for one full-HP player alone."""
    env = TextMMOEnv("MaskProbe")
    max_hp = max_hp or (20 + srv.LEVEL_HP_PER_LEVEL * (level - 1))
    env._state.update(
        {
            "room_id": room,
            "wild": True,
            "shelter": False,
            "level": level,
            "hp": max_hp,
            "max_hp": max_hp,
            "equipped": None,
            "defense": 0,
            "inv_names": [],
            "item_names": [],
            "npc_names": list(npc_names),
            "player_names": [],
            "other_players": 0,
            "buff_attack_amount": 0,
            "buff_damage_reduction_amount": 0,
        }
    )
    # valid_action_mask is cached per state version, and this test edits the
    # private state directly, so drop the cache before asking.
    env._mask_cache = None
    return bool(env.valid_action_mask()[ATTACK])


# A lone Glacier Wyrm (105 hp / 13 atk) is a fair level-20 fight.
assert _attack_allowed("glacier_crown", ["Glacier Wyrm"], 20)
# Four of them swing 52 attack per tick: no level clears that.
for level in (15, 20):
    assert not _attack_allowed("glacier_crown", ["Glacier Wyrm"] * 4, level), level
print("WYRM_STACK_BLOCKS_OK")

# A lone Treant is beatable from level 10; four are not.
assert _attack_allowed("deep_forest", ["Treant"], 10)
assert not _attack_allowed("deep_forest", ["Treant"] * 4, 10)
print("TREANT_STACK_BLOCKS_OK")

# The fix must not turn off combat: weak mobs stay farmable in a stack.
for count in (1, 4):
    assert _attack_allowed("summit_gatehouse", ["Gale Hound"] * count, 20), count
print("WEAK_STACK_STILL_FIGHTABLE_OK")

# Dynamic dungeon guards count per instance too: one is a fair level-15
# fight, four outpace a level-15 player.
assert _attack_allowed("crypt_hall", ["Crypt Guard"], 15)
assert not _attack_allowed("crypt_hall", ["Crypt Guard"] * 4, 15)
print("DUNGEON_STACK_BLOCKS_OK")

# Friendlies never contribute incoming damage, and a room with no hostile
# offers no attack target at all.
assert not _attack_allowed("market", ["Sister Maren"], 20)
print("NON_HOSTILE_IGNORED_OK")

print("ATTACK_MASK_WHOLE_ROOM_OK")
