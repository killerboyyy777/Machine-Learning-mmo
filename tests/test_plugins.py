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
assert {"linear", "torch", "gather", "dungeon", "market",
        "commissioner"} <= set(REGISTRY)
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


def _bughunt_377():
    import importlib
    import json

    import server as srv
    from ml.conductor.runners import make_env_factory
    from ml.ml_env import MERCHANT_NAMES, TextMMOEnv
    from ml.plugins import _import_builtins
    from ml.plugins.scripted import (
        CommissionerPlugin,
        PartyLeaderPlugin,
        ScriptedPolicy,
    )

    factory = make_env_factory(url="ws://x:1", reward_mode="score", curriculum_stage=1)
    env = factory("BugA")
    assert env.curriculum_stage == 1
    assert env.url == "ws://x:1"
    assert make_env_factory(curriculum_auto=True)("BugB").curriculum_auto is True
    print("ENV_KWARGS_OK")

    slot = parse_slot("gather:weight=3")
    assert slot["weight"] == 3 and slot["config"] == {}, slot
    slot2 = parse_slot("torch:checkpoint=X.pt,weight=2,epsilon=0.1")
    assert slot2["weight"] == 2, slot2
    assert slot2["config"] == {"checkpoint": "X.pt", "epsilon": 0.1}, slot2
    for _bad in ("gather:weight=0", "gather:weight=-2", "gather:weight=many"):
        try:
            parse_slot(_bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"weight accepted: {_bad}")
    print("SLOT_WEIGHT_OK")

    real_import = importlib.import_module

    def _boom(name):
        if name.endswith("torch_plugin"):
            raise ImportError("no torch")
        if name.endswith("scripted"):
            raise ImportError("boom-scripted")
        return real_import(name)

    importlib.import_module = _boom
    try:
        _import_builtins()
        raise SystemExit("FAIL: scripted ImportError swallowed")
    except ImportError as exc:
        assert "boom-scripted" in str(exc)
    finally:
        importlib.import_module = real_import

    def _torch_only(name):
        if name.endswith("torch_plugin"):
            raise ImportError("no torch")
        return real_import(name)

    importlib.import_module = _torch_only
    try:
        _import_builtins()
    finally:
        importlib.import_module = real_import
    print("BUILTIN_IMPORT_OK")

    arrow_env = TextMMOEnv("BugArrow")
    arrow_env._state["npc_names"] = list(MERCHANT_NAMES[:1])
    arrow_env._state["pack_units"] = 0
    arrow_env._state["pack_max"] = 24
    cmd = arrow_env._action_to_cmd("buy_arrows")
    want = srv.ITEM_DEFS["arrow"]["name"]
    assert cmd == {"cmd": "buy", "item": want}, cmd
    print("BUY_ARROWS_OK")

    pol = ScriptedPolicy()
    sig_env = TextMMOEnv("BugSig")
    sig_env._state["open_commissions"] = []
    sig0 = pol._stuck_state_sig(sig_env)
    sig_env._state["open_commissions"] = [
        {"id": 1, "poster": "O", "target": "rat", "kills": 3, "gold": 10}
    ]
    sig1 = pol._stuck_state_sig(sig_env)
    sig_env._state["open_commissions"] = [
        {"id": 1, "poster": "O", "target": "rat", "kills": 2, "gold": 10}
    ]
    sig2 = pol._stuck_state_sig(sig_env)
    assert sig0 != sig1
    assert sig1 != sig2
    print("STUCK_SIG_OK")

    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry(tmp, max_agents=5)
        reg.register("b1", "linear", parent_id="p0")
        reg.save()
        with open(os.path.join(tmp, "registry.json")) as f:
            assert json.load(f)["agents"][0]["parent_id"] == "p0"
        reg.reset_state("lineage")
        assert reg.get("b1").parent_id is None
        with open(os.path.join(tmp, "registry.json")) as f:
            assert json.load(f)["agents"][0]["parent_id"] is None
    print("RESET_LINEAGE_OK")

    class _Duck:
        def __init__(self):
            self._state = {
                "hp": 20,
                "max_hp": 20,
                "open_commissions": [],
                "party_size": 1,
                "npc_names": [],
            }
            self.name = "duck"

        def valid_action_mask(self):
            from ml.ml_env import N_ACTIONS

            return [1] * N_ACTIONS

    duck = _Duck()
    mask = duck.valid_action_mask()
    list(CommissionerPlugin().plan(duck, mask))
    list(PartyLeaderPlugin().plan(duck, mask))
    assert isinstance(CommissionerPlugin().select(duck), int)
    assert isinstance(PartyLeaderPlugin().select(duck), int)
    print("DUCK_STEP_OK")


_bughunt_377()
print("BUGHUNT_377_OK")

print("ALL_PLUGINS_OK")
