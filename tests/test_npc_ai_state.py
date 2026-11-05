"""NPC AI state machine (#536 PR 3).

Run from the repo root:  python tests/test_npc_ai_state.py

Transitions here are driven by a synthetic npc; the only live template with a
state machine is the wild crag stalker. The parity and golden suites are the
proof that the rest of the world did not move.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv


def _npc(**fields):
    npc = {
        "id": "state_wolf",
        "name": "State Wolf",
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
    npc.update(fields)
    return npc


def _victim(player_id, room, hp=100, logged_in=True):
    return srv.Player(
        ws=None,
        id=player_id,
        name=f"StateVictim{player_id}",
        logged_in=logged_in,
        hp=hp,
        max_hp=100,
        room=room,
    )


def _ctx(*occupants):
    return {"targets": list(occupants), "companions": []}


def _save():
    return {
        "players": dict(srv.players),
        "room_members": dict(srv.room_members),
        "players_by_name": dict(srv.players_by_name),
        "respawn_player": srv.respawn_player,
    }


def _restore(saved):
    srv.players.clear()
    srv.players.update(saved["players"])
    srv.room_members.clear()
    srv.room_members.update(saved["room_members"])
    srv.players_by_name.clear()
    srv.players_by_name.update(saved["players_by_name"])
    srv.respawn_player = saved["respawn_player"]


def _register(*victims):
    for victim in victims:
        srv.players[victim.id] = victim


# --- every field zero: IDLE forever, attacks only, nothing moves ---
saved = _save()
try:
    npc = _npc()
    victim = _victim(9101, "forest_edge", hp=1000)
    _register(victim)
    assert srv.NPC_STATES == (
        "IDLE",
        "AGGRO",
        "CHASING",
        "LEASHING",
        "FLEEING",
    )
    assert not srv._npc_state_machine_active(srv._npc_archetype(npc))
    ctx = _ctx(victim)
    for tick in range(20):
        victim.hp = 1000
        intent = srv.npc_think(npc, ctx, float(tick))
        assert intent is None or intent["kind"] == "attack", intent
        assert "state" not in (intent or {}), intent
        assert "event" not in (intent or {}), intent
        asyncio.run(srv.npc_apply(npc, intent, ctx, float(tick)))
        assert npc["state"] == "IDLE", npc["state"]
        assert npc["target"] is None, npc["target"]
        assert npc["room"] == "forest_edge", npc["room"]
    assert npc["hp"] == 40, npc["hp"]
    # Leashing is confined to the wild crag stalker; the civ-boundary wolves
    # stay on the baseline aggressive loop so the civil death floor holds.
    stalkers = {"crag_stalker"}
    for nid, live in srv.npcs.items():
        active = srv._npc_state_machine_active(srv._npc_archetype(live))
        assert active == (nid.split("__x")[0] in stalkers), nid
finally:
    _restore(saved)
print("BASELINE_IDLE_OK")


# --- aggro pickup in room, chase one hop, re-acquire in room ---
saved = _save()
try:
    npc = _npc(aggro_radius=1)
    victim = _victim(9102, "forest_edge")
    _register(victim)
    ctx = _ctx(victim)

    intent = srv.npc_think(npc, ctx, 10.0)
    assert intent["kind"] == "attack", intent
    assert intent["state"] == "AGGRO", intent
    assert intent["target"] == victim.id, intent
    assert intent["event"] == "aggro_acquired", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 10.0))
    assert npc["state"] == "AGGRO", npc["state"]
    assert npc["target"] == victim.id, npc["target"]
    assert npc["engaged_since"] == 10.0, npc["engaged_since"]

    victim.room = "deep_forest"
    intent = srv.npc_think(npc, ctx, 13.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "deep_forest", intent
    assert intent["state"] == "CHASING", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 13.0))
    assert npc["room"] == "deep_forest", npc["room"]
    assert npc["state"] == "CHASING", npc["state"]

    victim.room = "deep_forest"
    intent = srv.npc_think(npc, ctx, 16.0)
    assert intent["kind"] == "attack", intent
    assert intent["state"] == "AGGRO", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 16.0))
    assert npc["state"] == "AGGRO", npc["state"]
finally:
    _restore(saved)
print("AGGRO_AND_CHASE_OK")


# --- a lost, logged-out or dead target clears; a same-room replacement switches ---
saved = _save()
try:
    npc = _npc(aggro_radius=1)
    victim = _victim(9103, "forest_edge")
    other = _victim(9104, "forest_edge")
    _register(victim, other)
    ctx = _ctx(victim)

    intent = srv.npc_think(npc, ctx, 1.0)
    asyncio.run(srv.npc_apply(npc, intent, ctx, 1.0))
    assert npc["target"] == victim.id

    victim.logged_in = False
    intent = srv.npc_think(npc, ctx, 4.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "IDLE", intent
    assert intent["target"] is None, intent
    assert intent["event"] == "target_cleared", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 4.0))
    assert npc["state"] == "IDLE" and npc["target"] is None

    victim.logged_in = True
    ctx = _ctx(victim)
    intent = srv.npc_think(npc, ctx, 7.0)
    asyncio.run(srv.npc_apply(npc, intent, ctx, 7.0))
    assert npc["target"] == victim.id

    victim.room = "pinewood_thicket"
    intent = srv.npc_think(npc, ctx, 10.0)
    assert intent["kind"] == "state", intent
    assert intent["event"] == "target_cleared", intent
    assert intent["state"] == "IDLE", intent

    victim.room = "forest_edge"
    ctx = _ctx(victim)
    intent = srv.npc_think(npc, ctx, 13.0)
    asyncio.run(srv.npc_apply(npc, intent, ctx, 13.0))
    victim.hp = 0
    intent = srv.npc_think(npc, ctx, 16.0)
    assert intent["kind"] == "state", intent
    assert intent["event"] == "target_cleared", intent

    victim.hp = 100
    ctx = _ctx(victim)
    intent = srv.npc_think(npc, ctx, 19.0)
    asyncio.run(srv.npc_apply(npc, intent, ctx, 19.0))
    assert npc["target"] == victim.id
    victim.logged_in = False
    intent = srv.npc_think(npc, _ctx(victim, other), 22.0)
    assert intent["kind"] == "attack", intent
    assert intent["event"] == "target_switched", intent
    assert intent["target"] == other.id, intent
    assert intent["state"] == "AGGRO", intent
finally:
    _restore(saved)
print("STALE_TARGET_OK")


# --- hp > 0 is re-checked immediately before damage lands ---
saved = _save()
try:
    npc = _npc(aggro_radius=1)
    victim = _victim(9105, "forest_edge", hp=1)
    _register(victim)
    ctx = _ctx(victim)
    deaths = []

    async def _spy(player):
        deaths.append(player.name)

    srv.respawn_player = _spy
    intent = {"kind": "attack", "target": victim.id, "dmg": 5, "companion": None}

    victim.hp = 0
    asyncio.run(srv.npc_apply(npc, intent, ctx, 1.0))
    assert victim.hp == 0, victim.hp
    assert deaths == [], deaths
    assert not any("attacks you" in raw for raw in victim.outbound), list(
        victim.outbound
    )

    victim.hp = 1
    asyncio.run(srv.npc_apply(npc, intent, ctx, 2.0))
    assert victim.hp == -4, victim.hp
    assert deaths == [victim.name], deaths
finally:
    _restore(saved)
print("PRE_DAMAGE_HP_RECHECK_OK")


# --- a hostile never walks into shelter or the start room, unless it spawned there ---
saved = _save()
try:
    assert srv.START_ROOM in srv.NPC_SHELTER_ROOMS
    assert "old_shop" in srv.NPC_SHELTER_ROOMS
    assert "tideline_lighthouse" in srv.NPC_SHELTER_ROOMS
    assert "forest_edge" not in srv.NPC_SHELTER_ROOMS
    assert len(srv.NPC_SHELTER_ROOMS) == 15, len(srv.NPC_SHELTER_ROOMS)

    npc = _npc(aggro_radius=2)
    victim = _victim(9106, srv.START_ROOM)
    _register(victim)
    assert not srv._npc_can_enter(npc, srv.START_ROOM)
    intent = srv.npc_think(npc, _ctx(victim), 1.0)
    assert intent["kind"] == "state", intent
    assert intent["event"] == "target_cleared", intent
    assert intent["state"] == "IDLE", intent

    shelter_npc = _npc(room="town_square", spawn_room="market", aggro_radius=1)
    shelter_victim = _victim(9107, "old_shop")
    intent = srv.npc_think(shelter_npc, _ctx(shelter_victim), 1.0)
    assert intent["kind"] == "state", intent
    assert intent["event"] == "target_cleared", intent
    assert not srv._npc_can_enter(shelter_npc, "old_shop")

    homed = _npc(room="town_square", spawn_room="old_shop", aggro_radius=1)
    assert srv._npc_can_enter(homed, "old_shop")
    intent = srv.npc_think(homed, _ctx(shelter_victim), 2.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "old_shop", intent
    asyncio.run(srv.npc_apply(homed, intent, _ctx(shelter_victim), 2.0))
    assert homed["room"] == "old_shop", homed["room"]

    resident = _npc(room="old_shop", spawn_room="forest_edge", aggro_radius=1)
    resident_victim = _victim(9108, "old_shop")
    intent = srv.npc_think(resident, _ctx(resident_victim), 3.0)
    assert intent["kind"] == "attack", intent
    asyncio.run(srv.npc_apply(resident, intent, _ctx(resident_victim), 3.0))
    assert resident["room"] == "old_shop", resident["room"]
    idle_resident = _npc(room="old_shop", spawn_room="forest_edge", aggro_radius=1)
    assert srv.npc_think(idle_resident, _ctx(), 4.0) is None
    assert idle_resident["room"] == "old_shop", idle_resident["room"]
finally:
    _restore(saved)
print("SHELTER_INVARIANT_OK")


# --- flee cap, flee-before-swing, and completion heals to full ---
saved = _save()
try:
    assert srv._npc_archetype({"flee_below_pct": 90})["flee_below_pct"] == 35
    assert srv._npc_archetype({"flee_below_pct": 35})["flee_below_pct"] == 35
    assert srv._npc_archetype({"flee_below_pct": -10})["flee_below_pct"] == 0
    assert srv._npc_archetype({"flee_below_pct": "nope"})["flee_below_pct"] == 0
    assert srv.NPC_FLEE_PCT_CAP == 35

    npc = _npc(flee_below_pct=25, hp=30, max_hp=100)
    victim = _victim(9109, "forest_edge")
    _register(victim)
    ctx = _ctx(victim)
    intent = srv.npc_think(npc, ctx, 1.0)
    assert intent["kind"] == "attack", intent

    npc["hp"] = 25
    intent = srv.npc_think(npc, ctx, 2.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "FLEEING", intent
    assert intent["event"] == "flee_started", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 2.0))
    assert npc["state"] == "FLEEING", npc["state"]
    assert npc["target"] is None, npc["target"]

    npc["room"] = "forest_edge"
    npc["hp"] = 5
    npc["contributors"] = {"Someone": 1}
    intent = srv.npc_think(npc, _ctx(), 3.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "IDLE", intent
    assert intent["heal"] is True, intent
    assert intent["event"] == "flee_succeeded", intent
    asyncio.run(srv.npc_apply(npc, intent, _ctx(), 3.0))
    assert npc["hp"] == npc["max_hp"] == 100, (npc["hp"], npc["max_hp"])
    assert npc["contributors"] == {}, npc["contributors"]
    assert npc["state"] == "IDLE", npc["state"]

    fled = _npc(flee_below_pct=25, flee_to="old_shop", state="FLEEING")
    fled["room"] = "old_shop"
    intent = srv.npc_think(fled, _ctx(), 4.0)
    assert intent["event"] == "flee_succeeded", intent

    no_heal = _npc(
        flee_below_pct=25, leash_heal=False, state="FLEEING", hp=5, max_hp=100
    )
    no_heal["room"] = no_heal["spawn_room"]
    intent = srv.npc_think(no_heal, _ctx(), 5.0)
    assert intent["heal"] is False, intent
    asyncio.run(srv.npc_apply(no_heal, intent, _ctx(), 5.0))
    assert no_heal["hp"] == 5, no_heal["hp"]
finally:
    _restore(saved)
print("FLEE_CAP_AND_COMPLETION_OK")


# --- leash is measured in room hops from spawn_room and heals on return ---
saved = _save()
try:
    assert srv._room_hops("forest_edge", "mountain_pass") == 2
    assert srv._room_hops("forest_edge", "forest_edge") == 0

    npc = _npc(room="mountain_pass", aggro_radius=1, leash_radius=1, hp=10)
    victim = _victim(9110, "mountain_pass")
    _register(victim)
    npc["state"] = "AGGRO"
    npc["target"] = victim.id
    intent = srv.npc_think(npc, _ctx(victim), 1.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "LEASHING", intent
    assert intent["event"] == "leash_broken", intent
    assert intent["target"] is None, intent
    asyncio.run(srv.npc_apply(npc, intent, _ctx(victim), 1.0))
    assert npc["state"] == "LEASHING", npc["state"]
    assert npc["target"] is None, npc["target"]

    npc["room"] = "forest_edge"
    npc["contributors"] = {"Someone": 2}
    intent = srv.npc_think(npc, _ctx(), 2.0)
    assert intent["kind"] == "state", intent
    assert intent["event"] == "leash_returned", intent
    asyncio.run(srv.npc_apply(npc, intent, _ctx(), 2.0))
    assert npc["hp"] == npc["max_hp"] == 40, npc["hp"]
    assert npc["contributors"] == {}, npc["contributors"]
    assert npc["state"] == "IDLE", npc["state"]
finally:
    _restore(saved)
print("LEASH_HOPS_AND_HEAL_OK")


# --- retaliate_only never acquires; prefer picks weakest then nearest ---
saved = _save()
try:
    passive = _npc(aggro_radius=1, retaliate_only=True)
    victim = _victim(9111, "forest_edge")
    _register(victim)
    assert srv.npc_think(passive, _ctx(victim), 1.0) is None

    picker = _npc(aggro_radius=1, prefer="weakest")
    strong = _victim(9112, "forest_edge", hp=90)
    weak = _victim(9113, "forest_edge", hp=40)
    _register(strong, weak)
    intent = srv.npc_think(picker, _ctx(strong, weak), 2.0)
    assert intent["target"] == weak.id, intent

    nearest = _npc(aggro_radius=1, prefer="nearest")
    near = _victim(9114, "forest_edge")
    far = _victim(9115, "deep_forest")
    _register(near, far)
    intent = srv.npc_think(nearest, _ctx(far, near), 3.0)
    assert intent["target"] == near.id, intent
finally:
    _restore(saved)
print("RETALIATE_AND_PREFER_OK")


# --- never zero: live hostiles with today's data emit no move; guards and
# GM bosses keep their leash radius at zero and never change room ---
saved_npcs = srv.npcs
saved_treasury = srv.tax_treasury
saved_dirty = srv._scores_dirty
saved = _save()
try:
    guard = srv.Dungeon(party_id=1).floor(1).guards[0]
    assert guard["spawn_room"] == guard["room"], guard["spawn_room"]
    guard_cfg = srv._npc_archetype(guard)
    assert guard_cfg["leash_radius"] == 0 and guard_cfg["aggro_radius"] == 0, guard_cfg
    guard_victim = _victim(9116, guard["room"], hp=1000)
    _register(guard_victim)
    for tick in range(12):
        guard_victim.hp = 1000
        intent = srv.npc_think(guard, _ctx(guard_victim), float(tick))
        assert intent is None or intent["kind"] != "move", intent
        asyncio.run(srv.npc_apply(guard, intent, _ctx(guard_victim), float(tick)))
        assert guard["room"] == guard["spawn_room"], guard["room"]

    srv.npcs = {}
    srv.tax_treasury = 1000.0
    gm = srv.Player(ws=None, id=9117, name="StateGM", logged_in=True)
    asyncio.run(srv.cmd_gm_boss(gm, {"room": "graveyard", "strength": 1}))
    boss = next(n for n in srv.npcs.values() if str(n["id"]).startswith("boss_"))
    boss_cfg = srv._npc_archetype(boss)
    assert boss_cfg["leash_radius"] == 0 and boss_cfg["aggro_radius"] == 0, boss_cfg
    assert boss["spawn_room"] == "graveyard", boss["spawn_room"]
    boss_victim = _victim(9118, "graveyard", hp=1000)
    _register(boss_victim)
    for tick in range(12):
        boss_victim.hp = 1000
        intent = srv.npc_think(boss, _ctx(boss_victim), float(tick))
        assert intent is None or intent["kind"] != "move", intent
        asyncio.run(srv.npc_apply(boss, intent, _ctx(boss_victim), float(tick)))
        assert boss["room"] == "graveyard", boss["room"]
finally:
    srv.npcs = saved_npcs
    srv.tax_treasury = saved_treasury
    srv._scores_dirty = saved_dirty
    _restore(saved)
print("GUARDS_AND_BOSSES_STAY_PUT_OK")
