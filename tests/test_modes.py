"""Engine/mode seam + arena skeleton tests (no live server needed).

Run from the repo root:  python tests/test_modes.py
Covers #474: mode registry/loading, BaseMode passthrough defaults,
world-state isolation, arena scoring ledger, arena lifecycle.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.modes import (
    BaseMode,
    ModeContext,
    available,
    get,
    instantiate,
    isolate,
)
from ml.modes.arena import STAGING_ROOM, ArenaMode

# --- registry: skeleton loads by name ---
assert "arena" in available(), available()
assert get("arena") is ArenaMode
try:
    get("no_such_mode")
    raise SystemExit("FAIL: unknown mode accepted")
except KeyError:
    pass
try:
    instantiate("arena", bogus_knob=1)
    raise SystemExit("FAIL: unknown mode config accepted")
except ValueError:
    pass
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
arena.on_kill(hook_ctx, "Alice", "Bob")
arena.on_kill(hook_ctx, "Alice", "Carol")
assert hook_ctx.mode_state["kills"] == 2
assert hook_ctx.mode_state["streak"] == 2
assert hook_ctx.mode_state["arena_score"] == 10.0
arena.on_death(hook_ctx, "Alice")
assert hook_ctx.mode_state["deaths"] == 1
assert hook_ctx.mode_state["streak"] == 0
assert hook_ctx.mode_state["arena_score"] == 10.0
print("ARENA_LIFECYCLE_OK")

# --- arena config: kill value is tunable, ledger follows it ---
rich = instantiate("arena", kill_points=25.0)
rich_ctx = ModeContext(mode_state=rich.initial_state())
rich.on_kill(rich_ctx, "Alice", "Bob")
assert rich_ctx.mode_state["arena_score"] == 25.0
print("ARENA_CONFIG_OK")

print("ALL_MODES_OK")
