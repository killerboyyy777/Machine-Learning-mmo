"""NPC leash path (#536 PR 4).

Run from the repo root:  python tests/test_npc_leash.py

The four stalker templates chase on aggro_radius and walk home one hop per tick
once they reach leash_radius hops from spawn_room. LEASHING never attacks, never
enters shelter (except its own home), and heals to full on arrival. Guards,
the Warden and GM bosses keep leash_radius 0 and never take a leash step.
"""

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv

# Leash is confined to the wild crag stalker; civ-boundary wolves stay
# aggressive (the #536 design pins that a civil death floor needs steady
# pressure, and leashing gutted it on gate seed 702).
STALKERS = {"crag_stalker"}


def _npc(**fields):
    npc = {
        "id": "leash_wolf",
        "name": "Leash Wolf",
        "room": "forest_edge",
        "archetype": "stalker",
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
        name=f"LeashVictim{player_id}",
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
        "npcs": srv.npcs,
        "tax_treasury": srv.tax_treasury,
        "scores_dirty": srv._scores_dirty,
    }


def _restore(saved):
    srv.players.clear()
    srv.players.update(saved["players"])
    srv.room_members.clear()
    srv.room_members.update(saved["room_members"])
    srv.players_by_name.clear()
    srv.players_by_name.update(saved["players_by_name"])
    srv.npcs = saved["npcs"]
    srv.tax_treasury = saved["tax_treasury"]
    srv._scores_dirty = saved["scores_dirty"]


def _register(*victims):
    for victim in victims:
        srv.players[victim.id] = victim
        srv.add_member(victim)


# --- exactly four templates resolve to the stalker preset ---
saved = _save()
try:
    assert srv.WORLD["npc_archetypes"]["stalker"] == {
        "aggro_radius": 1,
        "leash_radius": 3,
    }
    assigned = {nid for nid, tmpl in srv.WORLD["npcs"].items() if tmpl.get("archetype")}
    assert assigned == STALKERS, assigned
    for nid in STALKERS:
        cfg = srv._npc_archetype(srv.WORLD["npcs"][nid])
        assert cfg["archetype"] == "stalker", nid
        assert cfg["aggro_radius"] == 1, (nid, cfg)
        assert cfg["leash_radius"] == 3, (nid, cfg)
        assert srv._npc_state_machine_active(cfg), nid
    for nid, tmpl in srv.WORLD["npcs"].items():
        if nid in STALKERS:
            continue
        assert not tmpl.get("archetype"), nid
        assert not srv._npc_state_machine_active(srv._npc_archetype(tmpl)), nid
finally:
    _restore(saved)
print("STALKER_ASSIGNMENT_OK")


# --- acquisition is aggro_radius, not leash_radius ---
saved = _save()
try:
    npc = _npc()
    near = _victim(9201, "deep_forest")
    _register(near)
    ctx = srv._npc_tick_context(npc)
    assert near.id in [p.id for p in ctx["vicinity"]], ctx
    intent = srv.npc_think(npc, ctx, 10.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "deep_forest", intent
    assert intent["state"] == "CHASING", intent
    assert intent["event"] == "aggro_acquired", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 10.0))
    assert npc["room"] == "deep_forest" and npc["state"] == "CHASING", npc

    # Two hops out is outside aggro_radius 1, even though it is inside leash.
    near.logged_in = False
    far = _victim(9202, "pinewood_thicket")
    _register(far)
    npc["room"] = "forest_edge"
    npc["state"] = "IDLE"
    npc["target"] = None
    ctx = srv._npc_tick_context(npc)
    assert far.id not in [p.id for p in ctx.get("vicinity", [])], ctx
    assert srv.npc_think(npc, ctx, 13.0) is None

    npc["state"] = "AGGRO"
    npc["target"] = far.id
    intent = srv.npc_think(npc, ctx, 16.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "IDLE", intent
    assert intent["event"] == "target_cleared", intent
finally:
    _restore(saved)
print("CHASE_ON_AGGRO_RADIUS_OK")


# --- leash fires at leash_radius hops from HOME, wherever the target is ---
saved = _save()
try:
    assert srv._room_hops("forest_edge", "pinewood_thicket") == 2
    assert srv._room_hops("forest_edge", "frozen_pass") == 3

    two_out = _npc(room="pinewood_thicket", spawn_room="forest_edge")
    target = _victim(9203, "pinewood_thicket")
    _register(target)
    two_out["state"] = "AGGRO"
    two_out["target"] = target.id
    intent = srv.npc_think(two_out, _ctx(target), 1.0)
    assert intent["kind"] == "attack", intent
    assert intent["state"] == "AGGRO", intent

    # Three hops out with the target in the same room: home distance wins.
    three_out = _npc(room="frozen_pass", spawn_room="forest_edge")
    three_out["state"] = "AGGRO"
    three_out["target"] = target.id
    target.room = "frozen_pass"
    intent = srv.npc_think(three_out, _ctx(target), 2.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "LEASHING", intent
    assert intent["event"] == "leash_broken", intent
    assert intent["target"] is None, intent

    # A target lost away from home also heads back instead of clearing to IDLE.
    lost = _npc(room="deep_forest", spawn_room="forest_edge", state="AGGRO")
    gone = _victim(9205, "deep_forest", logged_in=False)
    lost["target"] = gone.id
    intent = srv.npc_think(lost, _ctx(), 3.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "LEASHING", intent
    assert intent["event"] == "leash_broken", intent
finally:
    _restore(saved)
print("LEASH_STARTS_AT_HOME_RADIUS_OK")


# --- one hop per tick along the shortest shelter-free path home ---
saved = _save()
try:
    npc = _npc(room="frozen_pass", spawn_room="forest_edge", state="LEASHING")
    blocked = srv._npc_blocked_rooms(npc)
    expected = srv._room_path("frozen_pass", "forest_edge", blocked)
    assert expected == ["pinewood_thicket", "deep_forest", "forest_edge"], expected
    seen = []
    for tick in range(len(expected)):
        here = npc["room"]
        intent = srv.npc_think(npc, _ctx(), float(tick))
        assert intent["kind"] == "move", intent
        assert intent["state"] == "LEASHING", intent
        assert intent["target"] is None, intent
        assert srv._room_hops(here, intent["to"]) == 1, (here, intent)
        seen.append(intent["to"])
        asyncio.run(srv.npc_apply(npc, intent, _ctx(), float(tick)))
        assert npc["room"] == seen[-1], (npc["room"], seen[-1])
        assert npc["state"] == "LEASHING", npc["state"]
    assert seen == expected, seen
    intent = srv.npc_think(npc, _ctx(), 99.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "IDLE", intent
    assert intent["event"] == "leash_returned", intent
finally:
    _restore(saved)
print("WALK_HOME_ONE_HOP_OK")


# --- arrival heals to full and clears contributors ---
saved = _save()
try:
    npc = _npc(
        room="frozen_pass",
        spawn_room="forest_edge",
        state="LEASHING",
        hp=7,
        max_hp=40,
        contributors={"Someone": 3},
    )
    for tick in range(4):
        intent = srv.npc_think(npc, _ctx(), float(tick))
        asyncio.run(srv.npc_apply(npc, intent, _ctx(), float(tick)))
    assert npc["room"] == "forest_edge", npc["room"]
    assert npc["hp"] == 40, npc["hp"]
    assert npc["contributors"] == {}, npc["contributors"]
    assert npc["state"] == "IDLE", npc["state"]
    assert npc["target"] is None, npc["target"]
    assert npc.get("engaged_since") is None, npc.get("engaged_since")
finally:
    _restore(saved)
print("ARRIVAL_HEAL_OK")


# --- a leashing mob passes players without attacking or re-aggroing ---
saved = _save()
try:
    npc = _npc(room="pinewood_thicket", spawn_room="forest_edge", state="LEASHING")
    walker = _victim(9204, "pinewood_thicket")
    _register(walker)
    ctx = _ctx(walker)
    intent = srv.npc_think(npc, ctx, 1.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "deep_forest", intent
    asyncio.run(srv.npc_apply(npc, intent, ctx, 1.0))
    assert walker.hp == 100, walker.hp
    assert npc["state"] == "LEASHING" and npc["target"] is None, npc

    walker.room = "deep_forest"
    intent = srv.npc_think(npc, _ctx(walker), 2.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "forest_edge", intent
    assert npc["target"] is None, npc["target"]
    asyncio.run(srv.npc_apply(npc, intent, _ctx(walker), 2.0))
    assert walker.hp == 100, walker.hp

    # Home again, with a player waiting: completion, never a swing.
    walker.room = "forest_edge"
    intent = srv.npc_think(npc, _ctx(walker), 3.0)
    assert intent["kind"] == "state", intent
    assert intent["state"] == "IDLE", intent
    assert walker.hp == 100, walker.hp
finally:
    _restore(saved)
print("LEASH_NO_ATTACK_NO_REAGGRO_OK")


# --- guards, the Warden and GM bosses never take a leash step ---
saved = _save()
try:
    dungeon = srv.Dungeon(party_id=1)
    guards = []
    for floor_no in (1, srv.DUNGEON_MAX_FLOOR):
        guards.extend(dungeon.floor(floor_no).guards)
    assert any(g["name"] == "The Warden of the Deep" for g in guards)
    for guard in guards:
        cfg = srv._npc_archetype(guard)
        assert cfg["leash_radius"] == 0 and cfg["aggro_radius"] == 0, cfg
        assert guard["spawn_room"] == guard["room"], guard["id"]
        victim = _victim(9300 + len(guard["id"]), guard["room"], hp=1000)
        _register(victim)
        for tick in range(12):
            victim.hp = 1000
            intent = srv.npc_think(guard, _ctx(victim), float(tick))
            assert intent is None or intent["kind"] != "move", intent
            asyncio.run(srv.npc_apply(guard, intent, _ctx(victim), float(tick)))
            assert guard["room"] == guard["spawn_room"], guard["id"]
        guard["state"] = "LEASHING"
        intent = srv.npc_think(guard, _ctx(victim), 20.0)
        assert intent is None or intent["kind"] != "move", intent
        guard["state"] = "IDLE"

    srv.npcs = {}
    srv.tax_treasury = 1000.0
    gm = srv.Player(ws=None, id=9310, name="LeashGM", logged_in=True)
    asyncio.run(srv.cmd_gm_boss(gm, {"room": "graveyard", "strength": 1}))
    boss = next(n for n in srv.npcs.values() if str(n["id"]).startswith("boss_"))
    assert srv._npc_archetype(boss)["leash_radius"] == 0, boss
    assert boss["spawn_room"] == boss["room"] == "graveyard", boss
    boss_victim = _victim(9311, "graveyard", hp=1000)
    _register(boss_victim)
    for tick in range(12):
        boss_victim.hp = 1000
        intent = srv.npc_think(boss, _ctx(boss_victim), float(tick))
        assert intent is None or intent["kind"] != "move", intent
        asyncio.run(srv.npc_apply(boss, intent, _ctx(boss_victim), float(tick)))
        assert boss["room"] == "graveyard", boss["room"]
    boss["state"] = "LEASHING"
    intent = srv.npc_think(boss, _ctx(boss_victim), 20.0)
    assert intent is None or intent["kind"] != "move", intent
finally:
    _restore(saved)
print("GUARDS_AND_BOSSES_NO_MOVE_OK")


# --- the walk home never enters shelter or START_ROOM, except its own spawn ---
saved = _save()
try:
    ordinary = _npc(room="market", spawn_room="forest_edge", state="LEASHING")
    assert srv.START_ROOM in srv.NPC_SHELTER_ROOMS
    assert (
        srv._room_path("market", "forest_edge", srv._npc_blocked_rooms(ordinary))
        is None
    )
    assert srv.npc_think(ordinary, _ctx(), 1.0) is None
    assert ordinary["room"] == "market", ordinary["room"]

    resident = _npc(room="old_shop", spawn_room="forest_edge", state="LEASHING")
    assert "old_shop" in srv.NPC_SHELTER_ROOMS
    assert srv.npc_think(resident, _ctx(), 2.0) is None
    assert resident["room"] == "old_shop", resident["room"]

    homed = _npc(room="town_square", spawn_room="old_shop", state="LEASHING")
    assert srv._npc_can_enter(homed, "old_shop")
    intent = srv.npc_think(homed, _ctx(), 3.0)
    assert intent["kind"] == "move", intent
    assert intent["to"] == "old_shop", intent
    asyncio.run(srv.npc_apply(homed, intent, _ctx(), 3.0))
    assert homed["room"] == "old_shop", homed["room"]

    stranger = _npc(room="town_square", spawn_room="forest_edge", state="LEASHING")
    assert not srv._npc_can_enter(stranger, "old_shop")
finally:
    _restore(saved)
print("LEASH_SHELTER_INVARIANT_OK")


# --- respawn timing still reads spawn_room, not the room a leash ends in ---
saved = _save()
try:
    civ = next(r for r, v in srv.ROOMS.items() if not v.get("wild"))
    wild = next(r for r, v in srv.ROOMS.items() if v.get("wild"))
    wild_home = (
        srv._npc_respawn_at({"room": civ, "spawn_room": wild, "respawn_seconds": 30})
        - time.time()
    )
    assert 29.0 <= wild_home <= 31.0, wild_home
    civ_home = (
        srv._npc_respawn_at({"room": wild, "spawn_room": civ, "respawn_seconds": 30})
        - time.time()
    )
    assert abs(civ_home - 30.0 * srv.CIV_NPC_RESPAWN_MULT) < 1.5, civ_home
finally:
    _restore(saved)
print("RESPAWN_USES_SPAWN_ROOM_OK")


# --- every stalker can walk home from every shelter-free room it can reach ---
saved = _save()
try:
    for nid in STALKERS:
        home = srv.WORLD["npcs"][nid]["room"]
        cfg = srv._npc_archetype(srv.WORLD["npcs"][nid])
        blocked = srv.NPC_SHELTER_ROOMS - {home}
        assert srv._room_path(home, home, blocked) == []
        start = None
        for rid in srv._rooms_within_hops(home, cfg["leash_radius"]):
            if rid == home or rid in srv.NPC_SHELTER_ROOMS:
                continue
            if srv._room_path(home, rid, blocked) is None:
                continue
            back = srv._room_path(rid, home, blocked)
            assert back is not None and back[-1] == home, (nid, rid)
            if start is None or len(back) > len(srv._room_path(start, home, blocked)):
                start = rid
        assert start is not None, nid
        npc = _npc(id=f"{nid}_walker", room=start, spawn_room=home, state="LEASHING")
        for tick in range(12):
            if npc["room"] == home:
                break
            intent = srv.npc_think(npc, _ctx(), float(tick))
            assert intent["kind"] == "move", (nid, npc["room"], intent)
            dest = intent["to"]
            assert dest not in srv.NPC_SHELTER_ROOMS or dest == home, (nid, dest)
            asyncio.run(srv.npc_apply(npc, intent, _ctx(), float(tick)))
        assert npc["room"] == home, (nid, npc["room"], home)
finally:
    _restore(saved)
print("STALKERS_NEVER_STRANDED_OK")
