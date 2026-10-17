"""Engine/mode seam + arena skeleton tests (no live server needed).

Run from the repo root:  python tests/test_modes.py
Covers #474: mode registry/loading, BaseMode passthrough defaults,
world-state isolation, arena scoring ledger, arena lifecycle.
Rework: single-credit scoring per event, config type enforcement at
construction, frozen world view, corrupt-ledger refusal.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import FrozenInstanceError

from ml.modes import (
    BaseMode,
    ModeContext,
    available,
    get,
    instantiate,
    isolate,
)
from ml.modes.arena import STAGING_ROOM, ArenaMode


def rejects(exc, call, label):
    """Assert call() raises exc. No raise is the failure that matters;
    a wrong-type raise propagates here and fails the run on its own."""
    try:
        call()
    except exc:
        return
    raise SystemExit(f"FAIL: {label}: no raise, want {exc.__name__}")


def setitem(mapping, key, value):
    mapping[key] = value


# --- registry: skeleton loads by name ---
assert "arena" in available(), available()
assert get("arena") is ArenaMode
rejects(KeyError, lambda: get("no_such_mode"), "unknown mode accepted")
rejects(
    ValueError,
    lambda: instantiate("arena", bogus_knob=1),
    "unknown mode config accepted",
)
print("REGISTRY_OK")

# --- base mode: every hook is passthrough, scoring pays nothing ---
base = BaseMode()
ctx = ModeContext(world={"rooms": ["town_square"]}, mode_state={})
assert base.spawn_room(ctx) is None
assert base.on_login(ctx, "Alice") is None
assert base.on_respawn(ctx, "Alice") is None
assert base.on_tick(ctx, "Alice") is None
assert base.score_kill(ctx, "Alice", "rat", 10.0, 5.0) == (0.0, 0.0)
assert base.score_death(ctx, "Alice", 5.0) == 0.0
assert ctx.mode_state == {}, ctx.mode_state
print("BASE_PASSTHROUGH_OK")

# --- isolation: ledgers are per-mode namespaces in one store ---
store: dict = {}
a = isolate(store, "arena")
b = isolate(store, "other")
a["arena_score"] = 5.0
assert b == {}, b
assert isolate(store, "arena") is a
print("ISOLATION_OK")

# --- corrupt ledgers raise instead of being silently wiped ---
for bad in ([1, 2], "ledger", None, 7):
    rejects(
        ValueError,
        lambda bad=bad: isolate({"arena": bad}, "arena"),
        f"corrupt ledger {bad!r} silently replaced",
    )
# ...and the refusal leaves the save intact for migration.
survivor = {"arena": [1, 2]}
try:
    isolate(survivor, "arena")
except ValueError:
    pass
assert survivor == {"arena": [1, 2]}, survivor
print("ISOLATION_CORRUPT_OK")

# --- the world view is frozen: writes raise, reads work ---
engine_world: dict = {
    "rooms": ["town_square"],
    "npcs": {"town_square": ["rat", "thief"]},
}
frozen = ModeContext(world=engine_world)
assert frozen.world["rooms"] == ("town_square",), frozen.world["rooms"]
assert frozen.world["npcs"]["town_square"] == ("rat", "thief")
rejects(
    TypeError,
    lambda: setitem(frozen.world, "pwned", True),
    "mode mutated the world view",
)
# Nested rosters are the actual attack surface: a mode appending to a
# roster is an engine-state write even though the top level is a proxy.
roster = frozen.world["npcs"]["town_square"]
rejects(
    AttributeError,
    lambda: roster.append("ghost"),  # type: ignore[attr-defined]
    "mode mutated a nested world roster",
)
rejects(
    TypeError,
    lambda: setitem(frozen.world["npcs"], "town_square", ()),  # type: ignore[index]
    "mode replaced a nested world roster",
)
# Rebinding the field would swap the proxy out wholesale.
rejects(
    FrozenInstanceError,
    lambda: setattr(frozen, "world", {}),
    "mode rebound ctx.world",
)
# The engine's own structures stay engine-owned: later engine ticks
# cannot move what the mode already saw.
engine_world["rooms"].append("dupe_town")
engine_world["npcs"]["town_square"].append("boss")
assert frozen.world["rooms"] == ("town_square",), frozen.world["rooms"]
assert frozen.world["npcs"]["town_square"] == ("rat", "thief")
print("WORLD_FROZEN_OK")

# --- arena: loads with defaults, stages at the foyer ---
arena = instantiate("arena")
assert arena.kill_points == 5.0
assert arena.spawn_room(ctx) == STAGING_ROOM
assert arena.on_login(ModeContext(), "Alice") == STAGING_ROOM
assert arena.on_respawn(ModeContext(), "Alice") == STAGING_ROOM
state = arena.initial_state()
assert state == {"arena_score": 0.0, "kills": 0, "deaths": 0, "streak": 0}
print("ARENA_LOAD_OK")

# --- arena scoring: ledger only, engine values untouched ---
ledger_ctx = ModeContext(mode_state=arena.initial_state())
engine_score_before = 100.0
pts, xp = arena.score_kill(ledger_ctx, "Alice", "Bob", 10.0, 5.0)
assert (pts, xp) == (5.0, 0.0), (pts, xp)
assert ledger_ctx.mode_state["arena_score"] == 5.0
assert ledger_ctx.mode_state["kills"] == 1
assert ledger_ctx.mode_state["streak"] == 1
assert engine_score_before == 100.0
penalty = arena.score_death(ledger_ctx, "Alice", 5.0)
assert penalty == 0.0
assert ledger_ctx.mode_state["deaths"] == 1
assert ledger_ctx.mode_state["streak"] == 0
assert ledger_ctx.mode_state["arena_score"] == 5.0
print("ARENA_SCORING_OK")

# --- arena hooks drive the same ledger as direct scoring ---
hook_ctx = ModeContext(mode_state=arena.initial_state())
for victim in ("Bob", "Carol"):
    arena.score_kill(hook_ctx, "Alice", victim, 0.0, 0.0)
    arena.on_kill(hook_ctx, "Alice", victim)
assert hook_ctx.mode_state["kills"] == 2, hook_ctx.mode_state
assert hook_ctx.mode_state["streak"] == 2
assert hook_ctx.mode_state["arena_score"] == 10.0
arena.score_death(hook_ctx, "Alice", 0.0)
arena.on_death(hook_ctx, "Alice")
assert hook_ctx.mode_state["deaths"] == 1
assert hook_ctx.mode_state["streak"] == 0
assert hook_ctx.mode_state["arena_score"] == 10.0
print("ARENA_LIFECYCLE_OK")

# --- one event credits once: hooks never score on their own ---
single_ctx = ModeContext(mode_state=arena.initial_state())
arena.on_kill(single_ctx, "Alice", "Bob")
arena.on_death(single_ctx, "Alice")
assert single_ctx.mode_state["kills"] == 0, single_ctx.mode_state
assert single_ctx.mode_state["deaths"] == 0, single_ctx.mode_state
assert single_ctx.mode_state["arena_score"] == 0.0
# Full engine order (score then hook) per kill must move the ledger
# exactly one step, and a death costs no arena points.
order_ctx = ModeContext(mode_state=arena.initial_state())
arena.score_kill(order_ctx, "Alice", "Bob", 7.0, 3.0)
arena.on_kill(order_ctx, "Alice", "Bob")
arena.score_kill(order_ctx, "Alice", "Carol", 7.0, 3.0)
arena.on_kill(order_ctx, "Alice", "Carol")
arena.score_death(order_ctx, "Alice", 7.0)
arena.on_death(order_ctx, "Alice")
assert order_ctx.mode_state == {
    "arena_score": 10.0,
    "kills": 2,
    "deaths": 1,
    "streak": 0,
}, order_ctx.mode_state
print("NO_DOUBLE_COUNT_OK")

# --- bad knob types fail at construction, not mid-run ---
# Coercion is the contract (mirrors ml.plugins), so "12" -> 12.0; what
# must never happen is a value the knob cannot hold surviving to
# score_kill and blowing up there as a TypeError.
for bad_value in ("bad", None, [1], {"k": 1}, object()):
    rejects(
        ValueError,
        lambda bad_value=bad_value: instantiate("arena", kill_points=bad_value),
        f"bad knob type {bad_value!r} accepted at construction",
    )
assert instantiate("arena", kill_points=25).kill_points == 25.0
assert instantiate("arena", kill_points="12").kill_points == 12.0
print("CONFIG_TYPES_OK")

# --- arena config: kill value is tunable, ledger follows it ---
rich = instantiate("arena", kill_points=25.0)
rich_ctx = ModeContext(mode_state=rich.initial_state())
rich.score_kill(rich_ctx, "Alice", "Bob", 0.0, 0.0)
rich.on_kill(rich_ctx, "Alice", "Bob")
assert rich_ctx.mode_state["arena_score"] == 25.0
print("ARENA_CONFIG_OK")

print("ALL_MODES_OK")
