"""Companion assist damage cap (#590).

Run from the repo root:  python tests/test_companion_assist_cap.py

A pet may land at most COMPANION_ASSIST_DAMAGE_CAP_FRAC of one NPC's max HP;
the owner has to finish the bar. The cap is per NPC per pet, so a resummoned or
respawned mob resets it, and player damage is never counted against it.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv


async def _noop(*args, **kwargs):
    return None


srv.send = _noop
srv.broadcast_room = _noop
srv.sync_room = _noop

FRAC = srv.COMPANION_ASSIST_DAMAGE_CAP_FRAC
assert 0 < FRAC < 1, FRAC

WOLF = {"name": "Wolf", "species": "wolf", "level": 1, "hp": 26, "max_hp": 26}


def _npc(max_hp):
    return {
        "id": "cap_wolf",
        "name": "Cap Wolf",
        "room": "forest_edge",
        "alive": True,
        "hp": max_hp,
        "max_hp": max_hp,
        "contributors": {},
        "hostile": True,
        "respawn_seconds": 30,
    }


def _owner():
    return srv.Player(
        ws=None,
        id=991,
        name="CapOwner",
        logged_in=True,
        hp=20,
        max_hp=20,
        room="forest_edge",
    )


def _pet_damage(owner, npc, hits=40):
    dealt = 0
    for _ in range(hits):
        before = npc["hp"]
        srv.COMPANION_LAST_ATTACK.pop(owner.name.lower(), None)
        asyncio.run(srv._companion_attack_npc(owner, WOLF, npc))
        dealt += before - npc["hp"]
    return dealt


owner = _owner()
npc = _npc(40)
player_before = srv.DAMAGE_STATS["player"]
companion_before = srv.DAMAGE_STATS["companion"]

dealt = _pet_damage(owner, npc)
assert dealt == int(40 * FRAC), (dealt, int(40 * FRAC))
assert npc["hp"] == 40 - dealt, npc["hp"]
assert npc["hp"] > 1, npc["hp"]
print("PET_STOPS_AT_CAP_OK", dealt)

# A further burst lands nothing: the pet is capped out on this mob.
assert _pet_damage(owner, npc, hits=10) == 0
assert npc["hp"] == 40 - dealt
assert owner.name.lower() not in srv.COMPANION_TARGETS
print("CAPPED_PET_DISARMS_OK")

# The owner's own damage is untouched by the cap and can still finish the mob.
assert srv.DAMAGE_STATS["player"] == player_before
assert srv.DAMAGE_STATS["companion"] - companion_before == dealt
assert npc["contributors"][owner.name] == dealt
print("OWNER_DAMAGE_UNTOUCHED_OK")

# Small mobs still take pet damage: the cap floors to at least zero, never
# above half, and leaves at least one point for the killing blow.
small = _npc(3)
small_dealt = _pet_damage(owner, small, hits=10)
assert small_dealt == int(3 * FRAC), small_dealt
assert small["hp"] >= 2, small["hp"]
print("SMALL_MOB_CAP_OK", small_dealt)

# Respawn and the leash heal both clear the tally, so a fresh bar is fresh.
srv.respawn_npc(npc)
assert npc["companion_damage"] == {}, npc.get("companion_damage")
assert npc["hp"] == npc["max_hp"]
assert _pet_damage(owner, npc, hits=40) == int(40 * FRAC)
print("RESPAWN_RESETS_CAP_OK")

npc["companion_damage"] = {owner.name.lower(): 99}
srv._npc_apply_state(npc, {"state": "IDLE", "heal": True}, 0.0)
assert npc["companion_damage"] == {}, npc.get("companion_damage")
print("HEAL_RESETS_CAP_OK")

print("COMPANION_ASSIST_CAP_OK")
