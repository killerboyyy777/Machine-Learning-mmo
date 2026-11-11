"""Companion assist rides the owner's swing (#590).

Run from the repo root:  python tests/test_companion_assist_cap.py

A summoned pet never free-attacks on its own clock. It lands one hit on the
same hostile the owner just swung at, for at most COMPANION_ASSIST_SWING_FRAC
of that swing's damage -- even on slow seeds where an owner is dead or afk,
the pet stalls with them.

The gate divides companion damage by the WHOLE server's player damage while
only some agents own a pet, so the share is S*k/(1+S*k) for k = the pet
owners' slice of player damage, not S/(1+S). S is set above 1 for that reason.

A level-1 owner swings for 1-3, and int(1 * S) floors away most of that, so
the fraction is banked across swings rather than floored per hit; over many
small swings the pet still lands SWING_FRAC of the owner's damage.
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

S = srv.COMPANION_ASSIST_SWING_FRAC
assert S > 0, S


def _wolf():
    return {"name": "Wolf", "species": "wolf", "level": 1, "hp": 26, "max_hp": 26}


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


def _swing(owner, rec, npc, swing_dmg):
    before = npc["hp"]
    asyncio.run(srv._companion_attack_npc(owner, rec, npc, swing_dmg=swing_dmg))
    return before - npc["hp"]


owner = _owner()

# A swing of 30 funds int(30*S)=18, capped by the credit burst, and the wolf
# rolls randint(1,5), so the pet lands at least 1 and never more than the
# swing fraction.
rec = _wolf()
npc = _npc(50)
player_before = srv.DAMAGE_STATS["player"]
dealt = _swing(owner, rec, npc, 30)
assert 1 <= dealt <= int(30 * S), dealt
assert npc["hp"] == 50 - dealt, npc["hp"]
assert npc["contributors"][owner.name] == dealt
print("RIDES_SWING_OK", dealt)

# The bound holds for any single swing, whatever its magnitude.
for swing in (4, 9, 40):
    fresh = _npc(100)
    pet = _swing(owner, _wolf(), fresh, swing)
    assert 1 <= pet <= int(swing * S), (swing, pet)
print("SWING_BOUND_OK")

# No owner swing, no damage -- the pet has no clock of its own.
stalled = _npc(50)
assert _swing(owner, _wolf(), stalled, 0) == 0
assert stalled["hp"] == 50
print("NO_FREE_ATTACK_OK")

# The owner's own damage is untouched by the pet.
assert srv.DAMAGE_STATS["player"] == player_before
print("OWNER_DAMAGE_UNTOUCHED_OK")

# The pet never steals the killing blow: it stops at 1 HP.
killing = _npc(5)
_swing(owner, _wolf(), killing, 4)
assert killing["hp"] >= 1, killing["hp"]
print("LEAVES_KILLING_BLOW_OK")

# Small swings are banked, not floored away: ten 1-damage swings must land
# about S times the owner's 10 damage, not zero.
banked = _npc(500)
rec = _wolf()
total = sum(_swing(owner, rec, banked, 1) for _ in range(10))
assert abs(total - 10 * S) <= 3, total
print("SMALL_SWINGS_ACCUMULATE_OK", total)

print("COMPANION_ASSIST_CAP_OK")
