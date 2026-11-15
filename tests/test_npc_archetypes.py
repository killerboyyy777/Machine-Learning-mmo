"""NPC archetype data and normalised NPC constructors (#536 PR 1).

Run from the repo root:  python tests/test_npc_archetypes.py

PR 1 is data only: every hostile template still resolves to baseline (an absent
`archetype` key), all four NPC constructors carry `spawn_room`/`state`/`target`,
and the hostile tick's output for a fixed world and seed matches the fixture
captured from master@e9c2e05 before this change.
"""

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import npc_tick_fixture
from npc_tick_fixture import GOLDEN

import server as srv

with open(os.path.join(ROOT, "world.json"), encoding="utf-8") as fh:
    WORLD = json.load(fh)

# --- archetype data: the wild crag stalker and the three civil-boundary
# hunters all leash. The hunter leash is one hop, so a wolf cannot follow a
# role across the map into the graveyard and farm it there. ---
assert srv.NPC_ARCHETYPE_DEFAULTS == {
    "archetype": None,
    "aggro_radius": 0,
    "leash_radius": 0,
    "leash_heal": True,
    "flee_below_pct": 0,
    "flee_to": None,
    "patrol": [],
    "pack": 0,
    "retaliate_only": False,
    "prefer": "random",
}
assert WORLD["npc_archetypes"] == {
    "stalker": {"aggro_radius": 1, "leash_radius": 3},
    "hunter": {"aggro_radius": 1, "leash_radius": 1},
    "skirmisher": {"flee_below_pct": 25},
}

STALKERS = {"crag_stalker"}
HUNTERS = {"wolf", "timber_wolf", "mountain_lion"}
assigned = {
    nid: t.get("archetype") for nid, t in WORLD["npcs"].items() if t.get("archetype")
}
assert assigned == {
    **{nid: "stalker" for nid in STALKERS},
    **{nid: "hunter" for nid in HUNTERS},
}, assigned
for nid, tmpl in WORLD["npcs"].items():
    resolved = srv._npc_archetype(tmpl)
    if nid in STALKERS:
        assert resolved["archetype"] == "stalker", nid
        assert resolved["aggro_radius"] == 1, nid
        assert resolved["leash_radius"] == 3, nid
        assert srv._npc_state_machine_active(resolved), nid
    elif nid in HUNTERS:
        assert resolved["archetype"] == "hunter", nid
        assert resolved["aggro_radius"] == 1, nid
        assert resolved["leash_radius"] == 1, nid
        assert srv._npc_state_machine_active(resolved), nid
    else:
        assert resolved == srv.NPC_ARCHETYPE_DEFAULTS, nid
        assert not srv._npc_state_machine_active(resolved), nid

saved_npcs = srv.npcs
saved_extra = srv.MOB_EXTRA_SPAWNS
try:
    srv.npcs = {}
    srv.MOB_EXTRA_SPAWNS = 1
    srv._sync_extra_spawns()
    extras = [npc for nid, npc in srv.npcs.items() if "__x" in nid]
    assert extras, "no extra spawns built"
    for npc in extras:
        base = npc["id"].split("__x")[0]
        resolved = srv._npc_archetype(npc)
        expected = (
            "stalker" if base in STALKERS else "hunter" if base in HUNTERS else None
        )
        assert resolved["archetype"] == expected, npc["id"]
        assert srv._npc_state_machine_active(resolved) == (
            base in STALKERS or base in HUNTERS
        ), npc["id"]
finally:
    srv.npcs = saved_npcs
    srv.MOB_EXTRA_SPAWNS = saved_extra

dungeon = srv.Dungeon(party_id=1)
guards = []
for floor_no in (1, srv.DUNGEON_MAX_FLOOR):
    guards.extend(dungeon.floor(floor_no).guards)
assert any(g["name"] == "The Warden of the Deep" for g in guards)
for guard in guards:
    assert srv._npc_archetype(guard) == srv.NPC_ARCHETYPE_DEFAULTS, guard["id"]
    assert not srv._npc_state_machine_active(srv._npc_archetype(guard)), guard["id"]

saved_npcs = srv.npcs
saved_treasury = srv.tax_treasury
saved_dirty = srv._scores_dirty
try:
    srv.npcs = {}
    srv.tax_treasury = 1000.0
    gm = srv.Player(ws=None, id=9050, name="ArchetypeDataGM", logged_in=True)
    asyncio.run(srv.cmd_gm_boss(gm, {"room": "graveyard", "strength": 1}))
    bosses = [n for n in srv.npcs.values() if str(n["id"]).startswith("boss_")]
    assert bosses, "no GM boss built"
    for boss in bosses:
        assert srv._npc_archetype(boss) == srv.NPC_ARCHETYPE_DEFAULTS, boss["id"]
        assert not srv._npc_state_machine_active(srv._npc_archetype(boss)), boss["id"]
finally:
    srv.npcs = saved_npcs
    srv.tax_treasury = saved_treasury
    srv._scores_dirty = saved_dirty
print("ARCHETYPE_DATA_OK")

# --- construction path 1: static npcs loader ---
for nid, npc in srv.npcs.items():
    assert npc["spawn_room"] == npc["room"], nid
    assert npc["state"] == "IDLE", nid
    assert npc["target"] is None, nid
print("STATIC_NPC_NORMALISED_OK")

# --- construction path 2: MOB_EXTRA_SPAWNS instances ---
saved_npcs = srv.npcs
saved_extra = srv.MOB_EXTRA_SPAWNS
try:
    srv.npcs = {}
    srv.MOB_EXTRA_SPAWNS = 1
    srv._sync_extra_spawns()
    extras = [npc for nid, npc in srv.npcs.items() if "__x" in nid]
    assert extras, "no extra spawns built"
    for npc in extras:
        assert npc["spawn_room"] == npc["room"]
        assert npc["state"] == "IDLE"
        assert npc["target"] is None
finally:
    srv.npcs = saved_npcs
    srv.MOB_EXTRA_SPAWNS = saved_extra
print("EXTRA_SPAWN_NORMALISED_OK")

# --- construction path 3: dungeon guards and the Warden ---
dungeon = srv.Dungeon(party_id=1)
for floor_no in (1, srv.DUNGEON_MAX_FLOOR):
    floor = dungeon.floor(floor_no)
    assert floor.guards, floor_no
    for guard in floor.guards:
        assert guard["spawn_room"] == dungeon.room_id(floor_no)
        assert guard["state"] == "IDLE"
        assert guard["target"] is None
print("DUNGEON_GUARD_NORMALISED_OK")

# --- construction path 4: GM boss ---
saved_npcs = srv.npcs
saved_treasury = srv.tax_treasury
saved_dirty = srv._scores_dirty
try:
    srv.npcs = {}
    srv.tax_treasury = 1000.0
    gm = srv.Player(ws=None, id=9002, name="ArchetypeGM", logged_in=True)
    asyncio.run(srv.cmd_gm_boss(gm, {"room": "graveyard", "strength": 1}))
    bosses = [n for n in srv.npcs.values() if str(n["id"]).startswith("boss_")]
    assert len(bosses) == 1, bosses
    for boss in bosses:
        assert boss["spawn_room"] == "graveyard"
        assert boss["state"] == "IDLE"
        assert boss["target"] is None
finally:
    srv.npcs = saved_npcs
    srv.tax_treasury = saved_treasury
    srv._scores_dirty = saved_dirty
print("GM_BOSS_NORMALISED_OK")

# --- respawn timing reads spawn_room, not the current room ---
civ_room = next(r for r, v in srv.ROOMS.items() if not v.get("wild"))
wild_room = next(r for r, v in srv.ROOMS.items() if v.get("wild"))
assert srv.CIV_NPC_RESPAWN_MULT > 0 and srv.WILD_NPC_RESPAWN_MULT > 0
wild_home = srv._npc_respawn_at(
    {"room": civ_room, "spawn_room": wild_room, "respawn_seconds": 30}
) - (time.time())
assert abs(wild_home - 30.0 * srv.WILD_NPC_RESPAWN_MULT) < 1.5, wild_home
civ_home = srv._npc_respawn_at(
    {"room": wild_room, "spawn_room": civ_room, "respawn_seconds": 30}
) - (time.time())
assert abs(civ_home - 30.0 * srv.CIV_NPC_RESPAWN_MULT) < 1.5, civ_home
print("RESPAWN_USES_SPAWN_ROOM_OK")

# --- hostile tick guard: the seam reproduces the master@e9c2e05 capture ---
replayed = npc_tick_fixture.replay(GOLDEN["ticks"], GOLDEN["seed"])
assert replayed == GOLDEN, replayed
print("HOSTILE_TICK_GOLDEN_OK")
