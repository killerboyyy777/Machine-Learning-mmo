"""Conductor unit tests (no live server needed).

Run from the repo root:  python tests/test_conductor.py
Covers: registry CRUD, churn spawn/death, mixer rebalance, metrics writes.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ml.conductor as cond
from ml.conductor import (
    AgentTask,
    ChurnManager,
    MetricsLogger,
    Mixer,
    Registry,
    Supervisor,
)
from ml.conductor.churn import geometric_lifetime, poisson_interval, wave_startup

# #47: public classes are importable from the package root
assert {"Conductor", "Registry", "Supervisor", "ChurnManager", "Mixer",
        "MetricsLogger", "PBTManager", "AgentEntry", "AgentTask"} <= set(cond.__all__)
print("EXPORTS_OK")

tmpdir = tempfile.mkdtemp()
reg_path = os.path.join(tmpdir, "reg")

# --- Registry ---
reg = Registry(reg_path, max_agents=5)
reg.register("a1", "linear")
reg.register("a2", "torch")
reg.register("a3", "linear", branch="stable")
assert len(reg.alive_agents()) == 3
assert len(reg.alive_agents(agent_type="linear")) == 2
assert len(reg.alive_agents(branch="stable")) == 1
reg.record_episode("a1", 10.0)
reg.record_episode("a1", 20.0)
assert reg.get("a1").episodes == 2
assert reg.get("a1").mean_reward == 15.0
reg.promote("a2")
assert reg.get("a2").branch == "stable"
reg.demote("a3")
assert reg.get("a3").branch == "experimental"
reg.remove("a3")
assert reg.get("a3") is None
snap = reg.snapshot()
assert snap["alive"] == 2
reg.save()
reg2 = Registry(reg_path, max_agents=5)
reg2.load()
assert reg2.get("a1").episodes == 2
# #46: full-precision total survives the round-trip (not mean*episodes)
assert reg2.get("a1").total_reward == 30.0
assert reg2.get("a1").mean_reward == 15.0
print("REGISTRY_OK")

# --- Churn helpers ---
intervals = [poisson_interval(60.0) for _ in range(100)]
assert all(i >= 0 for i in intervals)
assert 0.3 < sum(intervals) / len(intervals) < 2.0
lifetimes = [geometric_lifetime(50) for _ in range(200)]
assert all(l >= 1 for l in lifetimes)
assert 30 < sum(lifetimes) / len(lifetimes) < 80
delays = list(wave_startup(25, 10, 5.0))
assert len(delays) == 25
assert delays[10] > delays[9]
print("CHURN_HELPERS_OK")

# --- ChurnManager tick ---
reg3 = Registry(os.path.join(tmpdir, "reg3"), max_agents=5)
cm = ChurnManager(reg3, arrivals_per_minute=1000, mean_lifetime_episodes=2)
cm._next_arrival = 0
arrived = cm.tick(1.0)
assert len(arrived) == 1
aid = arrived[0]
assert reg3.get(aid) is not None
cm._lifetimes[aid] = 1
cm._next_arrival = float("inf")
cm.tick(1.0)
assert not reg3.get(aid).alive
print("CHURN_TICK_OK")

# --- Mixer ---
reg4 = Registry(os.path.join(tmpdir, "reg4"), max_agents=10)
mx = Mixer.__new__(Mixer)
mx.registry = reg4
mx.floor_ids = ["f1", "f2"]
mx.min_per_floor = 1
mx.max_per_floor = 5
mx._floor_rewards = {"f1": [10, 10, 10], "f2": [2, 2, 2]}
mx._floor_agents = {"f1": ["a", "b", "c"], "f2": ["d", "e"]}
moves = mx.rebalance()
assert len(moves) > 0
snap = mx.snapshot()
assert snap["f1"]["mean_reward"] > snap["f2"]["mean_reward"]
print("MIXER_OK")

# --- Metrics ---
mlog = MetricsLogger(os.path.join(tmpdir, "test.jsonl"))
mlog.log_agent_spawn("a1", "linear", "experimental")
mlog.log_episode("a1", 1, 5.0, 100)
mlog.flush()
with open(os.path.join(tmpdir, "test.jsonl")) as f:
    lines = f.readlines()
assert len(lines) == 2
ev1 = json.loads(lines[0])
assert ev1["event"] == "agent_spawn" and ev1["agent_id"] == "a1"
ev2 = json.loads(lines[1])
assert ev2["event"] == "episode" and ev2["reward"] == 5.0
print("METRICS_OK")

# --- #45 supervisor steps counter increments per env step ---
class _FakeEnv:
    async def reset(self):
        await asyncio.sleep(0)  # yield like a real (network) env does
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(0)
        return ({"obs": 1}, 1.0, True, {})


async def _collect(task, n=2):
    # run() is infinite by design (stops only on cancel) -- take n
    # episodes (1 step each: the fake env is always done) then stop it.
    out = []
    gen = task.run()
    async for ev in gen:
        out.append(ev)
        if len(out) >= n:
            task.cancel()
            await gen.aclose()
            break
    return out


at = AgentTask("t1", _FakeEnv(), lambda obs, aid: 0, max_steps=5)
evs = asyncio.run(_collect(at))
assert at.steps == 2 and at.episodes == 2, (at.steps, at.episodes)
assert [e["steps"] for e in evs] == [1, 2]
print("SUPERVISOR_STEPS_OK")

# --- churn arrivals skip a full registry instead of raising ---
r_full = Registry(os.path.join(tmpdir, "full"), max_agents=1)
cm_full = ChurnManager(r_full, arrivals_per_minute=1000000)
cm_full._next_arrival = 0
assert len(cm_full.tick(1.0)) == 1  # fills the single slot
cm_full._next_arrival = 0
assert cm_full.tick(1.0) == []  # full: skipped, no RuntimeError
assert len(r_full.alive_agents()) == 1
# corpses don't wedge long runs: a dead slot is reusable for arrivals
dead_id = r_full.alive_agents()[0].agent_id
r_full.get(dead_id).alive = False
cm_full._next_arrival = 0
assert len(cm_full.tick(1.0)) == 1
assert len(r_full.alive_agents()) == 1 and r_full.snapshot()["total"] == 2
print("CHURN_FULL_OK")

# --- assign_one spreads single arrivals across floors ---
_mx2 = Mixer(Registry(os.path.join(tmpdir, "mx2"), max_agents=10),
             ["f1", "f2", "f3"])
for _aid in ["x", "y", "z", "w"]:
    _mx2.assign_one(_aid)
assert [_mx2._floor_agents[f] for f in ["f1", "f2", "f3"]] == [["x", "w"], ["y"], ["z"]]
print("MIXER_ASSIGN_ONE_OK")

# --- #48 wave_fill is awaitable and fills without blocking ---
async def _fill():
    r = Registry(os.path.join(tmpdir, "wf"), max_agents=5)
    cm = ChurnManager(r)
    n = await cm.wave_fill(3, wave_size=10, wave_delay=0.01)
    assert n == 3 and len(r.alive_agents()) == 3

asyncio.run(_fill())
print("WAVE_FILL_ASYNC_OK")

# --- #50 watchdog: hung env.step ends the episode instead of wedging ---
import time as _time


class _SlowEnv:
    async def reset(self):
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(5)
        return ({}, 0.0, True, {})


at_slow = AgentTask("t-slow", _SlowEnv(), lambda obs, aid: 0,
                    max_steps=5, step_timeout=0.05)
_t0 = _time.time()
_evs_slow = asyncio.run(_collect(at_slow, n=1))
_dt = _time.time() - _t0
assert _dt < 2.0, _dt
assert "timeout" in (at_slow.last_error or "").lower(), at_slow.last_error
print("WATCHDOG_OK")

# --- #51 mixer auto-integration: supervisor feeds episode rewards ---
async def _sup_mixer():
    r = Registry(os.path.join(tmpdir, "sup"), max_agents=5)
    mx = Mixer(r, ["town_square"])
    sup = Supervisor(r, mixer=mx)
    env = _FakeEnv()
    env._state = {"room_id": "town_square", "is_dungeon": False,
                  "dungeon_floor": 0}
    await sup.start_agent("s1", lambda aid: env, lambda obs, aid: 0)
    await asyncio.sleep(0.3)
    assert len(mx._floor_rewards["town_square"]) > 0
    assert mx.snapshot()["town_square"]["mean_reward"] == 1.0
    await sup.stop_all()

asyncio.run(_sup_mixer())
print("MIXER_AUTO_OK")

print("ALL_CONDUCTOR_OK")
