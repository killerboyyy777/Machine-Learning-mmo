"""The dungeon role enters the dungeon instead of bouncing at the door (#590).

Run from the repo root:  python tests/test_dungeon_descent.py

The dungeon entrance room is itself hostile (a Restless Ghost lives there), and
`_flee_unwinnable` used to fire before the descent step, so the role stepped in,
got pushed back out, and repeated that until it died. Every gate run so far
recorded zero dungeon clears while the dungeon role produced most of the civil
deaths.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ml"))

from ml_env import ACTIONS, TextMMOEnv
from plugins.scripted import DungeonPlugin, ScriptedPolicy

import server as srv


def _env(room, hp, npcs=(), is_dungeon=False):
    env = TextMMOEnv("DungeonProbe")
    env._state.update(
        {
            "room_id": room,
            "exits": list((srv.ROOMS.get(room) or {}).get("exits") or {}),
            "level": 1,
            "hp": hp,
            "max_hp": 20,
            "is_dungeon": is_dungeon,
            "dungeon_floor": 0,
            "gold": 0,
            "equipped": None,
            "defense": 0,
            "inv_names": [],
            "item_names": [],
            "npc_names": list(npcs),
            "player_names": [],
            "other_players": 0,
            "companion": None,
            "buff_attack_amount": 0,
            "buff_damage_reduction_amount": 0,
        }
    )
    env._mask_cache = None
    return env


def _first_action(plugin, env):
    mask = env.valid_action_mask()
    for index in plugin.plan(env, mask):
        if index is not None:
            return index
    return None


PLUGIN = DungeonPlugin()
ENTRANCE = PLUGIN._dungeon_entrance_room()
assert ENTRANCE and ENTRANCE in srv.ROOMS, ENTRANCE

# Standing on the entrance with the ghost in the room: descend, do not flee.
action = _first_action(PLUGIN, _env(ENTRANCE, 20, npcs=["Restless Ghost"]))
assert action is not None, "dungeon role produced no action at the entrance"
assert ACTIONS[action] in ("move_enter", "move_down"), ACTIONS[action]
print("DUNGEON_DESCENT_OK", ACTIONS[action])

# A wounded agent may still descend: the dungeon is the role's job, and the
# entrance room kills it just as fast as the first floor.
wounded = _first_action(PLUGIN, _env(ENTRANCE, 4, npcs=["Restless Ghost"]))
assert wounded is not None and ACTIONS[wounded] in ("move_enter", "move_down")
print("DUNGEON_DESCENT_WOUNDED_OK")

# Already inside the dungeon: no second descent, keep working the floor.
inside = _env("d_521_f1", 20, npcs=["Cave Bat"], is_dungeon=True)
action = _first_action(PLUGIN, inside)
assert action is None or ACTIONS[action] not in ("move_enter", "move_down"), action
print("NO_DOUBLE_DESCENT_OK")

# The descent rule must not become a general "ignore hostiles" rule: a healthy
# agent parked in a hostile room that is NOT the entrance still retreats.
assert ENTRANCE != "heron_nest"
flee = _env("heron_nest", 20, npcs=["Giant Heron"])
assert ScriptedPolicy()._flee_unwinnable(flee, flee.valid_action_mask()) is not None
print("NON_ENTRANCE_STILL_FLEES_OK")

print("DUNGEON_DESCENT_ORDER_OK")
