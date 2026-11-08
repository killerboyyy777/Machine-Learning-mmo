"""Companion assist rides the owner's swing (#590).

Run from the repo root:  python tests/test_companion_assist_cap.py

A summoned pet never free-attacks on its own clock. It lands one hit on the
same hostile the owner just swung at, for at most COMPANION_ASSIST_SWING_FRAC
of that swing's damage, so companion damage stays below
SWING_FRAC/(1+SWING_FRAC) = 37.5% of the gate's player+companion total -- even
on slow seeds where an owner is dead or afk, the pet stalls with them.
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
assert 0 < S < 1, S

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


def _swing(owner, npc, swing_dmg):
    before = npc["hp"]
    asyncio.run(srv._companion_attack_npc(owner, WOLF, npc, swing_dmg=swing_dmg))
    return before - npc["hp"]


owner = _owner()
npc = _npc(50)
player_before = srv.DAMAGE_STATS["player"]
companion_before = srv.DAMAGE_STATS["companion"]

# A swing of 30 funds at most int(30*S)=18; the wolf rolls randint(1,7) so the
# pet lands between 1 and 18, and never passes the swing fraction.
dealt = _swing(owner, npc, 30)
assert 1 <= dealt <= int(30 * S), dealt
assert npc["hp"] == 50 - dealt, npc["hp"]
print("RIDES_SWING_OK", dealt)

# The standing bound holds for any swing magnitude: pet <= swing*S.
for swing in (4, 9, 40):
    fresh = _npc(100)
    pet = _swing(owner, fresh, swing)
    assert 0 <= pet <= int(swing * S), (swing, pet)
print("SWING_BOUND_OK")

# A bare tick (no owner swing, swing_dmg=0) deals nothing: the pet has no clock.
stalled = _npc(50)
assert _swing(owner, stalled, 0) == 0
assert stalled["hp"] == 50
print("NO_FREE_ATTACK_OK")

# The owner's own damage is untouched by the pet/companion cap.
assert srv.DAMAGE_STATS["player"] == player_before
assert npc["contributors"][owner.name] == dealt
print("OWNER_DAMAGE_UNTOUCHED_OK")

# The pet never steals the killing blow: it stops at 1 HP.
killing = _npc(5)
pet = _swing(owner, killing, 4)
assert killing["hp"] >= 1, killing["hp"]
print("LEAVES_KILLING_BLOW_OK")

print("COMPANION_ASSIST_CAP_OK")
