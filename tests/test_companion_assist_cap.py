"""Companion assist damage cap (#590).

Run from the repo root:  python tests/test_companion_assist_cap.py

A pet is equipment the owner brings, not independent DPS: total companion
assist is bounded per owner at COMPANION_ASSIST_OWNER_SHARE of that owner's
own cmd_attack damage, plus a per-mob ceiling (COMPANION_ASSIST_DAMAGE_CAP_FRAC
of one NPC's max HP) so it cannot chew a single bar or take the killing blow.
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
SHARE = srv.COMPANION_ASSIST_OWNER_SHARE
assert 0 < FRAC < 1, FRAC
assert 0 < SHARE <= 1, SHARE

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


def _reset(owner):
    key = owner.name.lower()
    srv.COMPANION_OWNER_ASSIST.pop(key, None)
    srv.COMPANION_OWNER_PLAYER_DMG.pop(key, None)
    srv.COMPANION_TARGETS.pop(key, None)


def _pet_damage(owner, npc, hits=40):
    dealt = 0
    for _ in range(hits):
        before = npc["hp"]
        srv.COMPANION_LAST_ATTACK.pop(owner.name.lower(), None)
        asyncio.run(srv._companion_attack_npc(owner, WOLF, npc))
        dealt += before - npc["hp"]
    return dealt


def _owner_attack(owner, total_dmg):
    key = owner.name.lower()
    srv.COMPANION_OWNER_PLAYER_DMG[key] = (
        srv.COMPANION_OWNER_PLAYER_DMG.get(key, 0) + total_dmg
    )


owner = _owner()
npc = _npc(40)

# Owner has landed plenty of damage, so the per-mob ceiling is the binding cap.
_reset(owner)
_owner_attack(owner, 1000)
dealt = _pet_damage(owner, npc)
assert dealt == int(40 * FRAC), (dealt, int(40 * FRAC))
assert npc["hp"] == 40 - dealt, npc["hp"]
assert npc["hp"] > 1, npc["hp"]
print("PET_STOPS_AT_PER_MOB_CAP_OK", dealt)

# A further burst lands nothing: the pet is capped out on this mob.
assert _pet_damage(owner, npc, hits=10) == 0
assert owner.name.lower() not in srv.COMPANION_TARGETS
print("CAPPED_PET_DISARMS_OK")

# The owner's own damage budget is untouched by the pet's spend.
assert int(1000 * SHARE) - srv.COMPANION_OWNER_ASSIST[owner.name.lower()] > 0
assert npc["contributors"][owner.name] == dealt
print("OWNER_DAMAGE_UNTOUCHED_OK")

# Owner-share budget binds before the per-mob cap when the owner barely fights.
_reset(owner)
_owner_attack(owner, 10)  # tiny budget: floor(int(10*SHARE))
fresh = _npc(40)
small_dmg = _pet_damage(owner, fresh, hits=80)
assert small_dmg == int(10 * SHARE), small_dmg
assert fresh["hp"] == 40 - small_dmg, fresh["hp"]
assert small_dmg < int(40 * FRAC)
print("OWNER_SHARE_BINDS_OK", small_dmg)

# A further burst adds nothing once the owner budget is spent.
assert _pet_damage(owner, fresh, hits=80) == 0
assert owner.name.lower() not in srv.COMPANION_TARGETS
print("OWNER_BUDGET_DISARMS_OK")

# The per-owner assist tally accumulates across mobs (it is lifetime, unlike
# the per-mob ceiling), so a resummoned or respawned mob does not reset it.
owned_total = srv.COMPANION_OWNER_ASSIST.get(owner.name.lower(), 0)
assert owned_total == int(10 * SHARE)
print("OWNER_ASSIST_ACCUMULATES_OK", owned_total)

# Small mobs still take pet damage: the per-mob cap floors to at least zero.
_reset(owner)
_owner_attack(owner, 1000)
small = _npc(3)
small_dealt = _pet_damage(owner, small, hits=10)
assert small_dealt == int(3 * FRAC), small_dealt
assert small["hp"] >= 2, small["hp"]
print("SMALL_MOB_CAP_OK", small_dealt)

# Respawn and the leash heal both clear the per-mob tally, so a fresh bar is
# fresh for the per-mob ceiling (the owner budget persists independently).
_reset(owner)
_owner_attack(owner, 1000)
respawn_mob = _npc(40)
_spent = _pet_damage(owner, respawn_mob, hits=40)
assert _spent == int(40 * FRAC)
srv.respawn_npc(respawn_mob)
assert respawn_mob["companion_damage"] == {}, respawn_mob.get("companion_damage")
assert respawn_mob["hp"] == respawn_mob["max_hp"]
assert _pet_damage(owner, respawn_mob, hits=40) == int(40 * FRAC)
print("RESPAWN_RESETS_PER_MOB_OK")

_reset(owner)
_owner_attack(owner, 1000)
heal_mob = _npc(40)
_pet_damage(owner, heal_mob, hits=40)
assert heal_mob["companion_damage"] != {}
srv._npc_apply_state(heal_mob, {"state": "IDLE", "heal": True}, 0.0)
assert heal_mob["companion_damage"] == {}, heal_mob.get("companion_damage")
print("HEAL_RESETS_PER_MOB_OK")

print("COMPANION_ASSIST_CAP_OK")
