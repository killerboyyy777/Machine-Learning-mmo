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
import random
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv

with open(os.path.join(ROOT, "world.json"), encoding="utf-8") as fh:
    WORLD = json.load(fh)

# --- archetype data: defaults, presets, every hostile still baseline ---
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
    "skirmisher": {"flee_below_pct": 25},
}

hostiles = {nid: t for nid, t in WORLD["npcs"].items() if t.get("hostile")}
assert hostiles, "world has no hostile templates"
for nid, tmpl in hostiles.items():
    assert "archetype" not in tmpl, nid
    resolved = dict(srv.NPC_ARCHETYPE_DEFAULTS)
    resolved.update(WORLD["npc_archetypes"].get(tmpl.get("archetype", ""), {}))
    resolved.update(
        {key: tmpl[key] for key in srv.NPC_ARCHETYPE_DEFAULTS if key in tmpl}
    )
    assert resolved == srv.NPC_ARCHETYPE_DEFAULTS, nid
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
assert srv.CIV_NPC_RESPAWN_MULT > 1.0
wild_home = srv._npc_respawn_at(
    {"room": civ_room, "spawn_room": wild_room, "respawn_seconds": 30}
) - (time.time())
assert 29.0 <= wild_home <= 31.0, wild_home
civ_home = srv._npc_respawn_at(
    {"room": wild_room, "spawn_room": civ_room, "respawn_seconds": 30}
) - (time.time())
assert abs(civ_home - 30.0 * srv.CIV_NPC_RESPAWN_MULT) < 1.5, civ_home
print("RESPAWN_USES_SPAWN_ROOM_OK")

# --- golden guard: captured from master@e9c2e05, 30 hostile ticks, seed 700 ---
GOLDEN = {
    "seed": 700,
    "ticks": 30,
    "damages": [
        2,
        6,
        5,
        1,
        5,
        2,
        6,
        2,
        3,
        6,
        2,
        5,
        1,
        5,
        6,
        4,
        4,
        2,
        1,
        1,
        4,
        3,
        3,
        6,
        6,
        4,
        2,
        2,
        4,
        1,
    ],
    "victim_hp": 398,
    "npc_hp": 40,
    "npc_room": "forest_edge",
}


class _StopTicks(Exception):
    pass


def _hostile_tick_replay(ticks, seed):
    saved = {
        "npcs": srv.npcs,
        "players": srv.players,
        "room_members": srv.room_members,
        "players_by_name": srv.players_by_name,
        "dungeons": srv.dungeons,
        "asyncio": srv.asyncio,
        "_scores_dirty": srv._scores_dirty,
    }
    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > ticks:
            raise _StopTicks

    async def drive():
        try:
            await srv.npc_ai_loop()
        except _StopTicks:
            pass

    prefix = "Golden Wolf attacks you for "
    try:
        srv.npcs = {
            "golden_wolf": {
                "id": "golden_wolf",
                "name": "Golden Wolf",
                "room": "forest_edge",
                "hp": 40,
                "max_hp": 40,
                "attack": 6,
                "hostile": True,
                "behavior": "idle",
                "alive": True,
                "respawn_at": None,
                "contributors": {},
                "state": "IDLE",
                "spawn_room": "forest_edge",
                "target": None,
            }
        }
        srv.players = {}
        srv.room_members = {}
        srv.players_by_name = {}
        srv.dungeons = {}
        victim = srv.Player(
            ws=None,
            id=9001,
            name="GoldenVictim",
            logged_in=True,
            hp=500,
            max_hp=500,
            room="forest_edge",
        )
        srv.players[victim.id] = victim
        srv.add_member(victim)
        srv.asyncio = types.SimpleNamespace(sleep=fake_sleep)
        random.seed(seed)
        asyncio.run(drive())
        damages = []
        for raw in victim.outbound:
            msg = json.loads(raw)
            text = msg.get("text", "")
            if msg.get("type") == "combat" and text.startswith(prefix):
                damages.append(int(text[len(prefix) :].rstrip(".")))
        return {
            "seed": seed,
            "ticks": ticks,
            "damages": damages,
            "victim_hp": victim.hp,
            "npc_hp": srv.npcs["golden_wolf"]["hp"],
            "npc_room": srv.npcs["golden_wolf"]["room"],
        }
    finally:
        srv.npcs = saved["npcs"]
        srv.players = saved["players"]
        srv.room_members = saved["room_members"]
        srv.players_by_name = saved["players_by_name"]
        srv.dungeons = saved["dungeons"]
        srv.asyncio = saved["asyncio"]
        srv._scores_dirty = saved["_scores_dirty"]


replay = _hostile_tick_replay(GOLDEN["ticks"], GOLDEN["seed"])
assert replay == GOLDEN, replay
print("HOSTILE_TICK_GOLDEN_OK")
