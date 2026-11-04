"""NPC tick seam parity (#536 PR 2).

Run from the repo root:  python tests/test_npc_tick_parity.py

npc_think is pure and npc_apply carries the old hostile tick's I/O. The golden
block replays the same fixture as tests/test_npc_archetypes.py so the extracted
seam cannot drift from the committed damage sequence.
"""

import asyncio
import inspect
import json
import os
import random
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv

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


def _fake_victim(player_id=9001, name="ParityVictim", hp=500):
    return types.SimpleNamespace(
        id=player_id,
        name=name,
        hp=hp,
        max_hp=hp,
        armor=None,
        offhand=None,
        active_buffs={},
    )


def _fake_npc(hostile=True, behavior="idle"):
    return {
        "id": "parity_wolf",
        "name": "Golden Wolf",
        "room": "forest_edge",
        "hp": 40,
        "max_hp": 40,
        "attack": 6,
        "hostile": hostile,
        "behavior": behavior,
        "alive": True,
        "respawn_at": None,
        "contributors": {},
        "state": "IDLE",
        "spawn_room": "forest_edge",
        "target": None,
    }


# --- THINK_IS_PURE_OK: no I/O, no awaits, deterministic under a seed ---
saved_io = {
    name: getattr(srv, name)
    for name in (
        "send",
        "broadcast_room",
        "sync_room",
        "respawn_player",
        "_damage_companion",
        "time",
        "asyncio",
    )
}


def _boom(*_args, **_kwargs):
    raise AssertionError("npc_think touched I/O")


try:
    for name in (
        "send",
        "broadcast_room",
        "sync_room",
        "respawn_player",
        "_damage_companion",
    ):
        setattr(srv, name, _boom)
    srv.time = types.SimpleNamespace(time=_boom)
    srv.asyncio = types.SimpleNamespace(sleep=_boom)

    source = inspect.getsource(srv.npc_think)
    assert "await" not in source
    assert "time.time" not in source
    assert "send(" not in source
    assert "broadcast_room(" not in source
    assert "sync_room(" not in source

    npc = _fake_npc()
    victim = _fake_victim()
    ctx = {"targets": [victim], "companions": []}
    before_npc = dict(npc)
    before_victim = (victim.hp, dict(victim.active_buffs))

    random.seed(700)
    first = srv.npc_think(npc, ctx, 1234.0)
    random.seed(700)
    second = srv.npc_think(npc, ctx, 1234.0)
    assert first == second, (first, second)
    assert first["kind"] == "attack", first
    assert npc == before_npc
    assert (victim.hp, dict(victim.active_buffs)) == before_victim
finally:
    for name, value in saved_io.items():
        setattr(srv, name, value)
print("THINK_IS_PURE_OK")


# --- BASELINE_NO_CHASE_OK: baseline only ever attacks or does nothing ---
victim = _fake_victim()
target_ctx = {"targets": [victim], "companions": []}
owner = _fake_victim(player_id=9002, name="Owner")
companion_ctx = {"targets": [victim], "companions": [(owner, {"hp": 10})]}
for seed in range(50):
    random.seed(seed)
    intent = srv.npc_think(_fake_npc(), target_ctx, 0.0)
    assert intent is None or intent["kind"] == "attack", intent
    random.seed(seed)
    intent = srv.npc_think(_fake_npc(), companion_ctx, 0.0)
    assert intent is None or intent["kind"] == "attack", intent
    random.seed(seed)
    assert srv.npc_think(_fake_npc(), {"targets": [], "companions": []}, 0.0) is None
    random.seed(seed)
    assert (
        srv.npc_think(_fake_npc(hostile=False, behavior="wander"), target_ctx, 0.0)
        is None
    )
assert srv.NPC_ARCHETYPE_DEFAULTS["aggro_radius"] == 0
assert srv.NPC_ARCHETYPE_DEFAULTS["leash_radius"] == 0
assert srv.NPC_ARCHETYPE_DEFAULTS["flee_below_pct"] == 0
print("BASELINE_NO_CHASE_OK")


# --- GOLDEN_REPLAY_OK: the committed fixture through the seam driver ---
class _StopTicks(Exception):
    pass


def _golden_replay(ticks, seed):
    saved = {
        "npcs": srv.npcs,
        "players": srv.players,
        "room_members": srv.room_members,
        "players_by_name": srv.players_by_name,
        "dungeons": srv.dungeons,
        "asyncio": srv.asyncio,
        "scores_dirty": srv._scores_dirty,
        "companions_summoned": srv.COMPANION_SUMMONED,
        "companion_targets": srv.COMPANION_TARGETS,
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

    try:
        npc = _fake_npc()
        srv.npcs = {"parity_wolf": npc}
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
        srv.asyncio = types.SimpleNamespace(sleep=fake_sleep)
        random.seed(seed)
        asyncio.run(drive())
        prefix = "Golden Wolf attacks you for "
        damages = []
        for raw in victim.outbound:
            msg = json.loads(raw)
            text = msg.get("text", "")
            if text.startswith(prefix):
                damages.append(int(text[len(prefix) :].rstrip(".")))
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
        srv.asyncio = saved["asyncio"]
        srv._scores_dirty = saved["scores_dirty"]
        srv.COMPANION_SUMMONED = saved["companions_summoned"]
        srv.COMPANION_TARGETS = saved["companion_targets"]


# The loop is the seam driver, so this replay covers npc_think/npc_apply and
# the pre-tick HP regen that the committed fixture's victim_hp already counts.
replay = _golden_replay(GOLDEN["ticks"], GOLDEN["seed"])
assert replay == GOLDEN, replay
assert replay["victim_hp"] == GOLDEN["victim_hp"]
print("GOLDEN_REPLAY_OK")


# --- DEATH_PATH_OK: a lethal intent still runs the full respawn path ---
saved = {
    "npcs": srv.npcs,
    "players": srv.players,
    "room_members": srv.room_members,
    "players_by_name": srv.players_by_name,
    "dungeons": srv.dungeons,
    "companions_summoned": srv.COMPANION_SUMMONED,
    "companion_targets": srv.COMPANION_TARGETS,
    "room_gold": dict(srv.room_gold),
    "scores_dirty": srv._scores_dirty,
    "scores_entry": srv.SCORES.get("paritydeathvictim"),
    "score_history": srv._score_history.get("paritydeathvictim"),
}
death_calls = []


async def _death_spy(player):
    death_calls.append((player.name, player.hp))
    await saved_respawn(player)


saved_respawn = srv.respawn_player
try:
    npc = _fake_npc()
    srv.npcs = {"parity_wolf": npc}
    srv.players = {}
    srv.room_members = {}
    srv.players_by_name = {}
    srv.dungeons = {}
    srv.COMPANION_SUMMONED = set()
    srv.COMPANION_TARGETS = {}
    victim = srv.Player(
        ws=None,
        id=9003,
        name="ParityDeathVictim",
        logged_in=True,
        hp=1,
        max_hp=100,
        gold=100,
        room="forest_edge",
    )
    srv.players[victim.id] = victim
    srv.add_member(victim)
    srv.respawn_player = _death_spy
    ctx = {"targets": [victim], "companions": []}
    intent = {
        "kind": "attack",
        "target": victim.id,
        "dmg": 25,
        "companion": None,
    }
    asyncio.run(srv.npc_apply(npc, intent, ctx, 1000.0))

    assert death_calls == [(victim.name, -24)], death_calls
    assert victim.room == srv.START_ROOM
    assert victim.hp == victim.max_hp == 20, (victim.hp, victim.max_hp)
    assert victim.gold == 50, victim.gold
    assert srv.room_gold["forest_edge"] == saved["room_gold"]["forest_edge"] + 40
    entry = srv.SCORES["paritydeathvictim"]
    assert entry["deaths"] == 1
    assert any(json.loads(raw).get("type") == "death" for raw in victim.outbound)
finally:
    srv.respawn_player = saved_respawn
    srv.npcs = saved["npcs"]
    srv.players = saved["players"]
    srv.room_members = saved["room_members"]
    srv.players_by_name = saved["players_by_name"]
    srv.dungeons = saved["dungeons"]
    srv.COMPANION_SUMMONED = saved["companions_summoned"]
    srv.COMPANION_TARGETS = saved["companion_targets"]
    srv.room_gold.clear()
    srv.room_gold.update(saved["room_gold"])
    srv._scores_dirty = saved["scores_dirty"]
    if saved["scores_entry"] is None:
        srv.SCORES.pop("paritydeathvictim", None)
    else:
        srv.SCORES["paritydeathvictim"] = saved["scores_entry"]
    if saved["score_history"] is None:
        srv._score_history.pop("paritydeathvictim", None)
    else:
        srv._score_history["paritydeathvictim"] = saved["score_history"]
print("DEATH_PATH_OK")
