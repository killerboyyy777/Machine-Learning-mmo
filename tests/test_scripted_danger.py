"""Scripted agents must not plan through rooms that kill them on arrival (#590).

Run from the repo root:  python tests/test_scripted_danger.py

MOB_EXTRA_SPAWNS stacks several copies of one template in every wild room, so
a room's real incoming damage is the sum over live instances. Scored against
static world.json rows, a level-1 agent walked into four Glacier Wyrms (52
damage a tick against 20 hp) and died on arrival, which is what fed the gate's
death spiral.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv
from ml.ml_env import ACTIONS, TextMMOEnv
from ml.plugins.scripted import HunterPlugin, ScriptedPolicy

ROOMS = {
    "town": {
        "name": "Town",
        "wild": False,
        "exits": {"north": "weak", "south": "death"},
    },
    "weak": {"name": "Weak Wild", "wild": True, "exits": {"south": "town"}},
    "death": {"name": "Death Wild", "wild": True, "exits": {"north": "town"}},
}


def _npc(nid, room, attack):
    return {
        "id": nid,
        "name": "Glacier Wyrm" if attack == 13 else "Grey Wolf",
        "room": room,
        "attack": attack,
        "hostile": True,
        "hp": 10,
        "max_hp": 10,
        "alive": True,
    }


saved_rooms, saved_npcs = srv.ROOMS, srv.npcs
try:
    srv.ROOMS = ROOMS
    srv.npcs = {
        "wolf": _npc("wolf", "weak", 2),
        "wyrm__x1": _npc("wyrm__x1", "death", 13),
        "wyrm__x2": _npc("wyrm__x2", "death", 13),
        "wyrm__x3": _npc("wyrm__x3", "death", 13),
        "wyrm__x4": _npc("wyrm__x4", "death", 13),
    }

    env = TextMMOEnv("DangerTest")
    env._state["hp"] = 20
    env._state["max_hp"] = 20
    env._state["room_id"] = "town"
    policy = ScriptedPolicy()

    # Live instances, not the single static row world.json describes.
    assert policy._room_incoming("death") == 52, policy._room_incoming("death")
    assert policy._room_incoming("weak") == 2, policy._room_incoming("weak")
    assert policy._path_attack_cost("town", "death") == 52
    print("LIVE_INCOMING_OK")

    # 52 damage against 20 hp is death whatever the fight maths says.
    assert not policy._survivable(env, "death")
    assert policy._survivable(env, "weak")
    print("SURVIVABLE_OK")

    # Crossing costs one tick, so a 16-damage stack is passable at 20 hp and
    # a 32-damage one is not.
    srv.npcs["treant__x1"] = _npc("treant__x1", "weak", 8)
    srv.npcs["treant__x2"] = _npc("treant__x2", "weak", 8)
    assert policy._survivable(env, "weak"), policy._room_incoming("weak")
    srv.npcs["treant__x3"] = _npc("treant__x3", "weak", 8)
    srv.npcs["treant__x4"] = _npc("treant__x4", "weak", 8)
    assert not policy._survivable(env, "weak"), policy._room_incoming("weak")
    for nid in ("treant__x1", "treant__x2", "treant__x3", "treant__x4"):
        del srv.npcs[nid]
    print("TRANSIT_TICK_OK")

    assert policy._reachable_safely(env, "town", "weak") == ["north"]
    assert policy._reachable_safely(env, "town", "death") is None
    print("SAFE_ROUTE_OK")

    # The hunter only picks a wild room it can actually walk into.
    assert HunterPlugin()._wild_room(env) == "weak", HunterPlugin()._wild_room(env)
    print("HUNTER_TARGET_OK")

    # Moving blind between two exits must not choose the lethal one.
    mask = [False] * len(ACTIONS)
    for direction in ("north", "south"):
        mask[ACTIONS.index(f"move_{direction}")] = True
    for _ in range(20):
        assert ACTIONS[policy._random_move(env, mask)] == "move_north"
    print("WANDER_AVOIDS_LETHAL_OK")
finally:
    srv.ROOMS, srv.npcs = saved_rooms, saved_npcs

print("SCRIPTED_DANGER_OK")
