"""Conductor unit tests (no live server needed).

Run from the repo root:  python tests/test_conductor.py
Covers: registry CRUD, churn spawn/death, mixer rebalance, metrics writes.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ml.conductor.registry import Registry
from ml.conductor.churn import ChurnManager, poisson_interval, geometric_lifetime, wave_startup
from ml.conductor.mixer import Mixer
from ml.conductor.metrics import MetricsLogger

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

print("ALL_CONDUCTOR_OK")
