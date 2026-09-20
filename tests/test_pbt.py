"""PBT unit tests (no live server needed).

Run from the repo root:  python tests/test_pbt.py
Covers #38: exploit copies winner weights + mutated hparams to the loser,
no-op on tied fitness, min_episodes gating, Conductor wiring.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor.conductor import Conductor
from ml.conductor.metrics import MetricsLogger
from ml.conductor.pbt import PBTManager
from ml.conductor.registry import Registry

tmpdir = tempfile.mkdtemp()
reg = Registry(os.path.join(tmpdir, "reg"), max_agents=10)
mlog = MetricsLogger(os.path.join(tmpdir, "m.jsonl"))

ids = []
for i in range(4):
    aid = f"p{i}"
    reg.register(aid, "torch")
    ids.append(aid)

pbt = PBTManager(reg, mlog, min_episodes=5, min_delta=0.01)
for aid in ids:
    pbt.register(aid, {"lr": 0.001, "batch": 32, "tag": "fixed"})

# --- min_episodes gating: nobody trained yet -> no ops ---
assert pbt.step() == []
print("PBT_GATED_OK")

# --- exploit: bottom copies top (weights + mutated hparams) ---
winner, mid1, mid2, loser = ids[0], ids[1], ids[2], ids[3]
wentry = reg.get(winner)
os.makedirs(os.path.dirname(wentry.checkpoint_path) or ".", exist_ok=True)
with open(wentry.checkpoint_path, "w") as f:
    f.write("winner-weights")
pbt.report(winner, 10.0, episodes=6)
pbt.report(mid1, 5.0, episodes=6)
pbt.report(mid2, 4.0, episodes=6)
pbt.report(loser, 1.0, episodes=6)
ops = pbt.step()
assert len(ops) == 1, ops
op = ops[0]
assert op["winner"] == winner and op["loser"] == loser, op
assert op["weights_copied"] is True
lentry = reg.get(loser)
with open(lentry.checkpoint_path) as f:
    assert f.read() == "winner-weights"
lh = pbt._members[loser]["hparams"]
assert lh["tag"] == "fixed"  # non-numeric preserved
assert 0.0008 <= lh["lr"] <= 0.0012  # mutated around winner lr
assert 25 <= lh["batch"] <= 39  # int hparam mutated, stays int
assert pbt._members[loser]["episodes"] == 0  # must re-prove
mlog.flush()
with open(os.path.join(tmpdir, "m.jsonl")) as f:
    evs = [json.loads(line) for line in f]
assert any(e["event"] == "pbt_exploit" for e in evs)
print("PBT_EXPLOIT_OK")

# --- no-op when the gap is within noise (tied pair skips exploit) ---
pbt2 = PBTManager(reg, None, min_episodes=1, min_delta=0.5)
pbt2.register("a", {"lr": 0.01})
pbt2.register("b", {"lr": 0.01})
pbt2.report("a", 5.0, episodes=2)
pbt2.report("b", 5.2, episodes=2)  # gap 0.2 < 0.5
assert pbt2.step() == []
print("PBT_MIN_DELTA_OK")

# --- Conductor wiring: opt-in PBT, enroll + report + status ---
c = Conductor(os.path.join(tmpdir, "cond"), max_agents=4, pbt={})
assert c.pbt is not None
c2 = Conductor(os.path.join(tmpdir, "cond2"), max_agents=4)
assert c2.pbt is None
c2.report_fitness("x", 1.0)  # no-op, no crash
e = c.registry.register("c0", "linear")
c.enroll_pbt("c0", {"lr": 0.002})
c.report_fitness("c0", 3.0, episodes=11)
assert c.status()["pbt"]["c0"]["fitness"] == 3.0
# vector passthrough: goal-weighted fitness surfaces through Conductor
# (partial goals compose: missing axes read 0).
c.registry.register("c1", "linear", goal={"w_gold": 1.0})
c.enroll_pbt("c1", {"lr": 0.002})
c.report_fitness("c1", -1.0, episodes=11, vector={"gold": 4.0})
assert c.status()["pbt"]["c1"]["fitness"] == 4.0
print("PBT_CONDUCTOR_OK")

# --- goal-weighted fitness: weights influence selection (#288) ---
from ml.ml_env import GOAL_AXES, reward_vector
reg3 = Registry(os.path.join(tmpdir, "reg3"), max_agents=10)
_gold_goal = {"w_" + k: (1.0 if k == "gold" else 0.0) for k in GOAL_AXES}
_xp_goal = {"w_" + k: (1.0 if k == "xp" else 0.0) for k in GOAL_AXES}
reg3.register("gg", "linear", goal=_gold_goal)
reg3.register("gx", "linear", goal=_xp_goal)
pbt3 = PBTManager(reg3, None, min_episodes=1, min_delta=0.01)
pbt3.register("gg", {"lr": 0.01})
pbt3.register("gx", {"lr": 0.01})
v = reward_vector(score_gain=1.0, xp_gain=0.0, gold_delta=5.0,
                  inv_delta=0.0, social=0.0, quest=2.0)
# vector correctness: fitness equals the exact goal dot product, and the
# raw scalar is ignored once a vector is supplied.
pbt3.report("gg", -999.0, episodes=2, vector=v)
assert pbt3._members["gg"]["fitness"] == 5.0
pbt3.report("gx", 999.0, episodes=2, vector=v)
assert pbt3._members["gx"]["fitness"] == 0.0
# selection follows the weights: exploit copies the goal-aligned winner.
ops = pbt3.step()
assert len(ops) == 1 and ops[0]["winner"] == "gg" \
    and ops[0]["loser"] == "gx", ops
print("PBT_GOAL_FITNESS_OK")
# no vector (or no entry goal): raw scalar path unchanged.
pbt3.report("gx", 7.5, episodes=1)
assert pbt3._members["gx"]["fitness"] == 7.5
pbt3.report("ghost", 9.0, episodes=1, vector=v)  # unknown id: safe no-op
print("PBT_RAW_FITNESS_OK")

print("ALL_PBT_OK")
