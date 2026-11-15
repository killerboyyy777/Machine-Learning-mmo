"""The seed-700 hostile tick replay both NPC tick suites assert.

The golden block was captured from master@e9c2e05, before npc_think/npc_apply
were split out of npc_ai_loop. It replays through the seam rather than through
npc_ai_loop: the loop runs other subsystems first, and tick_station_freight
draws shuffle/choice from the same global RNG, so a loop-driven replay pins
whatever happens to draw above the NPC block instead of the tick's own
behaviour. That is what made this fixture drift when #633 landed.

victim_hp is 500 minus the sum of the damages. The pre-#633 capture recorded
398 because the loop's HP regen ran once between two ticks; the seam has no
regen, so the value is now 396.
"""

import asyncio
import json
import random

import server as srv

TICK_NOW = 1234.0
PREFIX = "Golden Wolf attacks you for "

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
    "victim_hp": 396,
    "npc_hp": 40,
    "npc_room": "forest_edge",
}


def _npc():
    return {
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


async def _drive(npc, victim, ticks):
    ctx = {"targets": [victim], "companions": []}
    for _ in range(ticks):
        intent = srv.npc_think(npc, ctx, TICK_NOW)
        await srv.npc_apply(npc, intent, ctx, TICK_NOW)


def replay(ticks, seed):
    saved = {
        "npcs": srv.npcs,
        "players": srv.players,
        "room_members": srv.room_members,
        "players_by_name": srv.players_by_name,
        "dungeons": srv.dungeons,
        "scores_dirty": srv._scores_dirty,
        "last_hit": dict(srv.LAST_HIT),
        "companions_summoned": srv.COMPANION_SUMMONED,
        "rng_state": random.getstate(),
        "companion_targets": srv.COMPANION_TARGETS,
    }
    try:
        npc = _npc()
        srv.npcs = {"golden_wolf": npc}
        srv.players = {}
        srv.room_members = {}
        srv.players_by_name = {}
        srv.dungeons = {}
        srv.COMPANION_SUMMONED = set()
        srv.COMPANION_TARGETS = {}
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
        random.seed(seed)
        asyncio.run(_drive(npc, victim, ticks))
        damages = []
        for raw in victim.outbound:
            msg = json.loads(raw)
            text = msg.get("text", "")
            if msg.get("type") == "combat" and text.startswith(PREFIX):
                damages.append(int(text[len(PREFIX) :].rstrip(".")))
        return {
            "seed": seed,
            "ticks": ticks,
            "damages": damages,
            "victim_hp": victim.hp,
            "npc_hp": npc["hp"],
            "npc_room": npc["room"],
        }
    finally:
        srv.npcs = saved["npcs"]
        srv.players = saved["players"]
        srv.room_members = saved["room_members"]
        srv.players_by_name = saved["players_by_name"]
        srv.dungeons = saved["dungeons"]
        srv._scores_dirty = saved["scores_dirty"]
        srv.LAST_HIT.clear()
        srv.LAST_HIT.update(saved["last_hit"])
        srv.COMPANION_SUMMONED = saved["companions_summoned"]
        srv.COMPANION_TARGETS = saved["companion_targets"]
        random.setstate(saved["rng_state"])
