"""Plugin system unit tests (no live server needed).

Run from the repo root:  python tests/test_plugins.py
Covers #62/#152: registry + discovery, config validation, policy arity,
slot specs, weighted conductor slots, 4-arg supervisor delivery.
"""

import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor.conductor import Conductor
from ml.conductor.registry import Registry
from ml.conductor.supervisor import AgentTask
from ml.plugins import (
    REGISTRY,
    discover,
    get,
    instantiate,
    parse_slot,
)

# --- built-ins registered ---
discover()
assert {"linear", "torch", "gather", "dungeon", "market", "maker"} <= set(REGISTRY)
print("BUILTINS_OK")

# --- config validation ---
lin = instantiate("linear")
assert lin.epsilon == 0.0 and lin.checkpoint is None
lin2 = instantiate("linear", epsilon="0.2")  # coerced str -> float
assert lin2.epsilon == 0.2
try:
    instantiate("linear", bogus=1)
    raise SystemExit("FAIL: unknown config accepted")
except ValueError:
    pass
try:
    get("nope")
    raise SystemExit("FAIL: unknown plugin accepted")
except KeyError:
    pass
print("CONFIG_OK")

# --- policy arity: learned 3-arg, scripted 4-arg ---
assert len(inspect.signature(lin.make_policy()).parameters) == 3
gather = instantiate("gather")
assert len(inspect.signature(gather.make_policy()).parameters) == 4
print("ARITY_OK")

# --- external discovery from a plugin dir ---
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "mybots.py"), "w") as f:
        f.write(
            "from ml.plugins import AgentPlugin, register\n"
            "@register\n"
            "class Ext(AgentPlugin):\n"
            "    name = 'ext'\n"
            "    agent_type = 'custom'\n"
            "    def act(self, obs, aid, mask=None):\n"
            "        return 0\n"
        )
    before = set(REGISTRY)
    discover(tmp)
    assert "ext" in REGISTRY and set(REGISTRY) - before == {"ext"}
    assert instantiate("ext").make_policy()(None, "x") == 0
    del REGISTRY["ext"]  # keep the global registry clean for other tests
print("DISCOVERY_OK")

# --- slot spec parsing ---
assert parse_slot("gather") == {
    "plugin": "gather",
    "config": {},
    "env": {},
    "weight": 1,
}
s = parse_slot(
    "torch:checkpoint=X.pt,epsilon=0.1,env_reward_mode=econ,env_max_steps=200"
)
assert s == {
    "plugin": "torch",
    "config": {"checkpoint": "X.pt", "epsilon": 0.1},
    "env": {"reward_mode": "econ", "max_steps": 200},
    "weight": 1,
}, s
for bad in ("", ":epsilon=1", "gather:epsilon", "gather:=1"):
    try:
        parse_slot(bad)
        raise SystemExit(f"FAIL: bad slot accepted: {bad!r}")
    except ValueError:
        pass
print("SLOT_PARSE_OK")

# --- weighted slots cycle deterministically ---
with tempfile.TemporaryDirectory() as tmp:
    cond = Conductor(
        os.path.join(tmp, "c"),
        max_agents=6,
        runners=[
            {"plugin": "gather", "weight": 2},
            {"plugin": "dungeon", "weight": 1},
        ],
    )
    order = [cond._next_slot()["label"] for _ in range(6)]
    assert order == ["gather", "gather", "dungeon"] * 2, order
    # legacy single-runner dict still works
    cond2 = Conductor(
        os.path.join(tmp, "c2"),
        max_agents=2,
        runner={"env_factory": lambda aid: None, "policy_fn": lambda o, a: 0},
    )
    assert cond2._next_slot()["label"] == "custom"
    assert Conductor(os.path.join(tmp, "c3"), max_agents=2)._next_slot() is None
print("SLOTS_OK")

# --- per-type status breakdown ---
with tempfile.TemporaryDirectory() as tmp:
    reg = Registry(os.path.join(tmp, "r"), max_agents=5)
    reg.register("a1", "linear")
    reg.register("a2", "torch")
    reg.record_episode("a1", 10.0)
    reg.record_episode("a1", 20.0)
    reg.record_episode("a2", 4.0)
    cond = Conductor(os.path.join(tmp, "c"), max_agents=5)
    cond.registry = reg  # point at the hand-built registry
    by_type = cond.status()["by_type"]
    assert by_type["linear"] == {
        "alive": 1,
        "episodes": 2,
        "mean_reward": 15.0,
    }, by_type
    assert by_type["torch"] == {"alive": 1, "episodes": 1, "mean_reward": 4.0}, by_type
print("BY_TYPE_OK")


class _Env:
    def __init__(self):
        self._state = {
            "room_id": "town_square",
            "is_dungeon": False,
            "dungeon_floor": 0,
        }

    def valid_action_mask(self):
        from ml.ml_env import N_ACTIONS

        return [1] * N_ACTIONS

    async def reset(self):
        await asyncio.sleep(0)
        return {"o": 0}

    async def step(self, action):
        await asyncio.sleep(0)
        return ({"o": 1}, 0.5, True, {})


# --- supervisor passes env to 4-arg policies ---
seen = {}


async def _run_once(task):
    gen = task.run()
    async for _ev in gen:
        task.cancel()
        await gen.aclose()
        break
    return task


def _p4(obs, aid, mask, env):
    seen.update(aid=aid, mask=list(mask), env=env)
    return 0


async def _go():
    return await _run_once(AgentTask("q4", _Env(), _p4, max_steps=2))


at = asyncio.run(_go())
assert seen["aid"] == "q4" and isinstance(seen["mask"], list)
assert isinstance(seen["env"], _Env) and at.steps == 1
print("ENV_ARITY_OK")

print("ALL_PLUGINS_OK")
