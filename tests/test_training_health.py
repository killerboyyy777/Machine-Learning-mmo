"""Training-health unit tests: #418 reset-retry + logging, #417 spawn
spread + room-discovery ledger. No live server and no torch needed for the
env-side halves.

Run from the repo root:  python tests/test_training_health.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml")
)

from ml_env import (
    ACTIONS,
    CONNECTION_ERRORS,
    TextMMOEnv,
    reachable_path,
    reset_with_retry,
    safe_spread_rooms,
)

import server as srv

# --- #418: reset() retries are logged, bounded, and stoppable ----------------


class _FlakyEnv:
    """Raises `failures` connection errors, then hands back a room obs."""

    def __init__(self, failures, exc=ConnectionError):
        self.failures = failures
        self.exc = exc
        self.attempts = 0
        self.resets = 0

    async def reset(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc("socket gone")
        self.resets += 1
        return {"room_id": "town_square"}


async def _retry_recovers():
    slept = []

    async def _sleep(delay):
        slept.append(delay)

    seen = []
    env = _FlakyEnv(2)
    obs = await reset_with_retry(
        env,
        name="Torch0",
        backoff_base=1.0,
        on_retry=lambda a, d, e: seen.append((a, d)),
        sleep=_sleep,
    )
    assert obs["room_id"] == "town_square"
    assert env.resets == 1, env.resets
    assert env.attempts == 3, env.attempts
    assert [a for a, _ in seen] == [1, 2], seen
    # Exponential, and it does not grow forever: attempt 3 would be 4.0s.
    assert [d for _, d in seen] == [1.0, 2.0], seen
    assert slept == [1.0, 2.0], slept


asyncio.run(_retry_recovers())
print("RESET_RETRY_OK")


async def _retry_backoff_cap():
    delays = []
    env = _FlakyEnv(6)
    await reset_with_retry(
        env,
        name="Torch1",
        backoff_base=1.0,
        backoff_max=4.0,
        on_retry=lambda a, d, e: delays.append(d),
        sleep=lambda d: asyncio.sleep(0),
    )
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0], delays


asyncio.run(_retry_backoff_cap())
print("RESET_BACKOFF_CAP_OK")


async def _jitter_stays_under_cap():
    # Jitter is a fleet stagger, not an exemption from the cap: 60.5s of
    # "capped" backoff is not capped.
    delays = []
    env = _FlakyEnv(8)
    await reset_with_retry(
        env,
        name="Torch4",
        backoff_base=1.0,
        backoff_max=4.0,
        jitter=0.5,
        on_retry=lambda a, d, e: delays.append(d),
        sleep=lambda d: asyncio.sleep(0),
    )
    assert delays == [1.5, 2.5, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0], delays
    assert max(delays) <= 4.0, delays


asyncio.run(_jitter_stays_under_cap())
print("RESET_JITTER_CAP_OK")


async def _retry_propagates_bugs():
    env = _FlakyEnv(1, exc=RuntimeError)
    try:
        await reset_with_retry(env, name="Torch2", sleep=lambda d: asyncio.sleep(0))
    except RuntimeError:
        pass
    else:
        raise SystemExit("FAIL: non-connection error was swallowed by the retry")


asyncio.run(_retry_propagates_bugs())
print("RESET_BUG_PROPAGATES_OK")


async def _retry_stops_when_asked():
    slept = []

    async def _sleep(delay):
        slept.append(delay)

    env = _FlakyEnv(99)
    try:
        await reset_with_retry(
            env,
            name="Torch3",
            on_retry=lambda a, d, e: None,
            should_stop=lambda: True,
            sleep=_sleep,
        )
    except ConnectionError:
        pass
    else:
        raise SystemExit("FAIL: retry loop ignored should_stop")
    assert env.attempts == 1, env.attempts
    assert slept == [], slept


asyncio.run(_retry_stops_when_asked())
print("RESET_STOPS_OK")


# --- #418: the connection-error tuple covers the paths that actually fire ---

assert ConnectionError in CONNECTION_ERRORS
assert OSError in CONNECTION_ERRORS
assert asyncio.TimeoutError in CONNECTION_ERRORS
try:
    import websockets

    assert websockets.ConnectionClosed in CONNECTION_ERRORS
except ImportError:
    raise SystemExit("FAIL: websockets missing, ConnectionClosed untestable")
print("CONNECTION_ERRORS_OK")


# --- #420: step() absorbs the whole tuple, so a dead socket is done, not a
# crash. Before this, only ConnectionClosed was caught and an OSError out of
# _send propagated into the trainer's step handler instead.


async def _step_survives_dead_socket():
    class _DeadSocket:
        async def send(self, _payload):
            raise OSError("write on closed transport")

    env = TextMMOEnv("StepDead")
    env.step_delay = 0
    env.ws = _DeadSocket()
    env._state = env._build_obs() and env._state
    _, _, done, _info = await env.step(ACTIONS.index("look"))
    assert done is True, "a refused write must end the episode, not raise"

    env.ws = None
    # A send on the None socket a failed reconnect leaves behind raised
    # AttributeError, which read like a bug in the caller and raced past
    # every connection-error handler. _send now raises ConnectionError.
    env._state["room_id"] = "town_square"
    obs, _, done, _ = await env.step(ACTIONS.index("look"))
    assert done is True, "a send with no socket must end the episode"
    assert obs["room_id"]


asyncio.run(_step_survives_dead_socket())
print("STEP_SURVIVES_DEAD_SOCKET_OK")


# --- #417: walk routing honours directed edges -------------------------------

_ROOMS = {
    "a": {"exits": {"north": "b", "south": "c"}},
    "b": {"exits": {"south": "a"}},
    "c": {"exits": {"east": "d"}},
    "d": {"exits": {"up": "c"}},  # one-way back: a naive symmetric BFS lies
}

assert reachable_path(_ROOMS, "a", "a") == []
assert reachable_path(_ROOMS, "a", "b") == ["north"]
assert reachable_path(_ROOMS, "a", "d") == ["south", "east"]
assert reachable_path(_ROOMS, "b", "d") == ["south", "south", "east"]
# d -> a would need d's "up" to be a two-way edge; it is not.
assert reachable_path(_ROOMS, "d", "a") is None
assert reachable_path(_ROOMS, "a", "nowhere") is None
# Real world: the #415 escape shafts walk straight out of the depths and no
# room has the opposite-label edge back -- a symmetric BFS would invent one.
_OPPOSITE = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "up": "down",
    "down": "up",
}


def _shaft(shaft_room, direction):
    dest = srv.ROOMS[shaft_room]["exits"][direction]
    assert reachable_path(srv.ROOMS, shaft_room, dest) == [direction]
    assert srv.ROOMS[dest]["exits"].get(_OPPOSITE[direction]) != shaft_room
    return dest


assert _shaft("burial_chamber", "up") == "graveyard"
assert _shaft("bone_pit", "up") == "graveyard"
assert _shaft("deep_catacombs", "south") == "graveyard"
assert _shaft("forge", "south") == "artisan_row"
print("REACHABLE_PATH_OK")


# --- #417: the spread pool is safe, walkable, and not the start room only ----

pool = safe_spread_rooms()
assert len(pool) >= 8, pool
assert len(set(pool)) == len(pool), pool
assert pool == sorted(pool), pool
assert srv.START_ROOM in pool
assert not any(srv.dungeon_for_room(r) for r in pool), pool
hostile_rooms = {v.get("room") for v in srv.WORLD["npcs"].values() if v.get("hostile")}
assert not (set(pool) & hostile_rooms), set(pool) & hostile_rooms
for room in pool:
    assert room in srv.ROOMS, room
    assert reachable_path(srv.ROOMS, srv.START_ROOM, room) is not None, room
    # Every pooled room must actually be walkable to, or a trainee spends
    # its first steps failing a move it can never make.
    assert srv.ROOMS[room]["exits"], room
print("SAFE_SPREAD_ROOMS_OK")


def test_spawn_room_validation():
    for bad in ("nowhere", "dungeon_entrance", 7):
        try:
            TextMMOEnv("SpawnBad", spawn_room=bad)
        except ValueError:
            pass
        else:
            raise SystemExit(f"FAIL: accepted spawn_room={bad!r}")
    env = TextMMOEnv("SpawnNone")
    assert env.spawn_room is None
    assert env.walked_rooms == []
    env = TextMMOEnv("SpawnOk", spawn_room="market")
    assert env.spawn_room == "market"
    # Default (None) is today's behavior: the server's placement stands.
    assert TextMMOEnv("SpawnDefault").spawn_room is None


test_spawn_room_validation()
print("SPAWN_ROOM_VALIDATION_OK")


# --- #417: _walk_to is best effort and records the rooms it used -------------


async def _walk_to_checks():
    # Toy map: every real room is reachable from town_square, so the
    # unreachable-target branch needs a map of its own.
    real_rooms = srv.ROOMS
    try:
        srv.ROOMS = {
            "town_square": {"exits": {"west": "market"}},
            "market": {"exits": {"west": "town_square"}},
            "island": {"exits": {}},
        }
        env = TextMMOEnv("WalkTest", spawn_room="market")
        env.step_delay = 0
        env._state["room_id"] = "town_square"
        sent = []

        async def _fake_send(cmd, **kwargs):
            sent.append((cmd, kwargs))
            room = env._state["room_id"]
            dest = srv.ROOMS[room]["exits"].get(kwargs.get("dir"))
            env._state["room_id"] = dest or "still_here"

        env._send = _fake_send
        assert await env._walk_to("market") is True
        assert sent == [("move", {"dir": "west"})], sent
        assert env.walked_rooms == ["town_square", "market"], env.walked_rooms

        # Unreachable target: no moves attempted, honest False, and the room
        # it did stand in is still recorded.
        sent.clear()
        env._state["room_id"] = "market"
        assert await env._walk_to("island") is False
        assert sent == [], sent
        assert env.walked_rooms == ["market"], env.walked_rooms

        # Already there: no walk, no rooms claimed beyond the current one.
        env.walked_rooms = []
        env._state["room_id"] = "market"
        assert await env._walk_to("market") is True
        assert sent == [] and env.walked_rooms == ["market"], env.walked_rooms
    finally:
        srv.ROOMS = real_rooms

    # The real map: town_square -> market is a single westward hop.
    env = TextMMOEnv("WalkReal", spawn_room="market")
    env.step_delay = 0
    env._state["room_id"] = "town_square"
    moves = []

    async def _real_send(cmd, **kwargs):
        moves.append(kwargs.get("dir"))
        room = srv.ROOMS[env._state["room_id"]]["exits"]
        env._state["room_id"] = room.get(kwargs.get("dir"), env._state["room_id"])

    env._send = _real_send
    assert await env._walk_to("market") is True
    assert moves == ["west"], moves
    assert env.walked_rooms == ["town_square", "market"], env.walked_rooms


asyncio.run(_walk_to_checks())
print("WALK_TO_OK")


def test_discovery_ledger():
    """The discovery ledger is per character, not per episode: a death back
    into the start room cannot re-earn the bonus."""

    class _Ledger:
        # Same rule as TorchDQNAgent.discovery_bonus, exercised without
        # importing torch.
        def __init__(self, explore_bonus=0.2):
            self.explore_bonus = explore_bonus
            self.seen = set()

        def discovery_bonus(self, room):
            if not room or room in self.seen:
                return 0.0
            self.seen.add(room)
            return float(self.explore_bonus)

    agent = _Ledger()
    assert agent.discovery_bonus("town_square") == 0.2
    assert agent.discovery_bonus("town_square") == 0.0  # respawn pays nothing
    assert agent.discovery_bonus("market") == 0.2
    assert agent.discovery_bonus(None) == 0.0
    assert agent.seen == {"town_square", "market"}

    off = _Ledger(explore_bonus=0.0)
    assert off.discovery_bonus("market") == 0.0
    assert off.seen == {"market"}, "ledger still tracks with the bonus off"


test_discovery_ledger()
print("DISCOVERY_LEDGER_OK")

try:
    import torch
except ImportError:
    print("TORCH_MISSING: skipping torch_farm/dqn_agent assertions")
else:
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "torch_agents"
        ),
    )
    from dqn_agent import TorchDQNAgent
    from torch_farm import Runner, TorchFarm, _is_crash, _is_stop, parse_args

    agent = TorchDQNAgent(name="HealthTest", explore_bonus=0.25)
    assert agent.discovery_bonus("town_square") == 0.25
    assert agent.discovery_bonus("town_square") == 0.0
    assert agent._discovered_rooms == {"town_square"}
    assert agent.discovery_bonus("forest_edge") == 0.25
    # The checkpoint carries the ledger, so a relaunched pilot keeps it.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "w.pt")
        agent.save_weights(path)
        fresh = TorchDQNAgent(name="HealthTest2", explore_bonus=0.25)
        assert fresh.load_weights(path) is True
        assert fresh._discovered_rooms == {"town_square", "forest_edge"}
        assert fresh.discovery_bonus("forest_edge") == 0.0
        # Older checkpoints without the key still load, with an empty ledger.
        legacy = os.path.join(tmp, "legacy.pt")
        blob = torch.load(path, weights_only=True)
        blob.pop("discovered_rooms")
        torch.save(blob, legacy)
        old = TorchDQNAgent(name="HealthTest3")
        assert old.load_weights(legacy) is True
        assert old._discovered_rooms == set()
    print("TORCH_DISCOVERY_LEDGER_OK")

    class _Args:
        agents = 4
        name_prefix = "TorchHealth"
        url = "ws://localhost:1"
        steps = 10
        save_every = 5
        weights = os.path.join(tempfile.gettempdir(), "health_weights.json")
        best_weights = os.path.join(tempfile.gettempdir(), "health_best.json")
        spawn_spread = True
        explore_bonus = 0.2

    farm = TorchFarm(_Args())
    assert farm.spawn_rooms, "spread pool empty with --spawn-spread on"
    assert len(farm.spawn_rooms) == len(safe_spread_rooms())
    targets = [farm.spawn_room_for(i) for i in range(4)]
    assert all(t in farm.spawn_rooms for t in targets), targets
    # Index-based, not random: a relaunched farm spreads the same way.
    assert targets == [farm.spawn_room_for(i) for i in range(4)]
    # Sixteen trainees, 16 names: the pool must actually spread them.
    wide = [farm.spawn_room_for(i) for i in range(16)]
    assert len(set(wide)) == min(16, len(farm.spawn_rooms)), wide

    _Args.spawn_spread = False
    farm_off = TorchFarm(_Args())
    assert farm_off.spawn_rooms == []
    assert farm_off.spawn_room_for(3) is None
    assert Runner(0, farm_off).spawn_room is None
    assert Runner(0, farm).spawn_room in farm.spawn_rooms

    # A clean stop is not a crash. Ctrl-C during a retry, or the
    # task.cancel() in the finally block, both reach the gather as
    # exceptions; counting them deflated runners_alive on every tidy exit.
    assert _is_stop(asyncio.CancelledError()) is True
    assert _is_crash(asyncio.CancelledError()) is False
    assert _is_stop(ConnectionError("stopped")) is True
    assert _is_crash(ConnectionError("stopped")) is False
    assert _is_stop(ConnectionResetError("reset")) is True
    assert _is_stop(RuntimeError("bad state")) is False
    assert _is_crash(RuntimeError("bad state")) is True
    assert _is_crash(ValueError("bad obs")) is True
    assert _is_crash(None) is False
    # A clean finish returns None and is neither crashed nor stopped.
    print("TORCH_SHUTDOWN_CLASSIFY_OK")

    _argv = sys.argv
    try:
        sys.argv = ["torch_farm.py", "--agents", "2", "--explore-bonus", "0"]
        off_args = parse_args()
        assert off_args.spawn_spread is True and off_args.explore_bonus == 0.0
        sys.argv = ["torch_farm.py", "--no-spawn-spread"]
        assert parse_args().spawn_spread is False
        sys.argv = ["torch_farm.py", "--explore-bonus", "-1"]
        try:
            parse_args()
        except SystemExit:
            pass
        else:
            raise SystemExit("FAIL: negative --explore-bonus accepted")
    finally:
        sys.argv = _argv
    print("TORCH_FARM_SPREAD_OK")

print("ALL_TRAINING_HEALTH_OK")
