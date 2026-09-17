"""Training-loop smokes with synthetic transitions (no live server needed).

Run from the repo root:  python tests/test_training_loops.py
Covers the core learning loops, which had zero repo tests: the linear
online update (ml_client), the shared-farm policy math (ml_botfarm), and
-- where torch is installed -- the DQN store/learn/save cycle
(torch_agents). Torch sections skip cleanly without torch so this file
runs in the offline CI job too.
"""
import os
import random
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_client import LinearQAgent  # noqa: E402
from ml_env import N_ACTIONS, OBS_SIZE  # noqa: E402

random.seed(7)

# --- linear online update: TD error contracts on a repeated transition ---
lin = LinearQAgent(6, 3)
feats = [0.1] * 6
assert lin.act(feats, 0.0, None) == 0  # zeroed weights: greedy takes first valid
assert lin.act(feats, 0.0, [0, 1, 1]) == 1  # mask respected
d1 = lin.update(feats, 0, 2.5, feats, True)
d2 = lin.update(feats, 0, 2.5, feats, True)
assert isinstance(d1, float) and abs(d2) < abs(d1), (d1, d2)
print("LINEAR_UPDATE_OK")

# --- linear mini training loop: act/update/save/load round-trip ---
tmpdir = tempfile.mkdtemp()
agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
for step in range(20):
    f = [random.uniform(-1.0, 1.0) for _ in range(OBS_SIZE)]
    nf = [random.uniform(-1.0, 1.0) for _ in range(OBS_SIZE)]
    a = agent.act(f, 0.0, None)
    assert 0 <= a < N_ACTIONS
    agent.update(f, a, random.uniform(-1.0, 1.0), nf, False)
    agent.training_steps += 1
assert agent.training_steps == 20
path = os.path.join(tmpdir, "lin.json")
agent.save(path)
loaded = LinearQAgent(OBS_SIZE, N_ACTIONS)
assert loaded.load(path)
probe = [0.25] * OBS_SIZE
assert loaded.q_values(probe) == agent.q_values(probe)
assert loaded.training_steps == 20
print("LINEAR_LOOP_OK")

# --- shared-farm policy math (no server): epsilon schedule, fitness gate ---
import ml_botfarm as farm_mod  # noqa: E402

farm_mod.BEST_FILE = os.path.join(tmpdir, "best.json")
farm_mod.WEIGHTS_FILE = os.path.join(tmpdir, "farm_weights.json")
args = types.SimpleNamespace(
    name_prefix="T", url="ws://x:1", scripted="none", reward_window=200,
    epsilon_start=1.0, epsilon_end=0.05, epsilon_decay_steps=100,
    weights=os.path.join(tmpdir, "missing.json"), checkpoint_every=50,
    eval_every=9999, steps=0, bots=2,
)
farm = farm_mod.Farm(args)
assert farm.best_fitness == float("-inf")  # fresh start, no checkpoints
assert farm.epsilon_now() == 1.0
farm.agent.training_steps = 100
assert abs(farm.epsilon_now() - 0.05) < 1e-9  # fully decayed
bot = farm_mod.BotRunner(0, farm)
assert bot.policy is None  # scripted=none -> training path
assert bot.fitness() is None  # not enough recent data yet
bot.recent_rewards = [0.5] * 200
assert bot.fitness() == 0.5
farm.promote(0.5, 10.0)
assert farm.best_fitness == 0.5
import json as _json
assert _json.load(open(farm_mod.BEST_FILE))["fitness"] == 0.5
farm.save_weights()
assert os.path.isfile(farm_mod.WEIGHTS_FILE)
print("FARM_MATH_OK")

# --- torch DQN store/learn/save (skips cleanly without torch) ---
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "torch_agents"))
    from dqn_agent import TorchDQNAgent  # noqa: E402
    HAVE_TORCH = True
except ImportError as e:
    print(f"TORCH_LOOP_SKIP (no torch: {e})")
    HAVE_TORCH = False

if HAVE_TORCH:
    tagent = TorchDQNAgent(replay_size=64, batch_size=8)
    assert 0 <= tagent.act([0.0] * OBS_SIZE, 0.0, None) < N_ACTIONS
    assert tagent.rnd_bonus([0.0] * OBS_SIZE) >= 0.0
    for _ in range(64):
        tagent.store({
            "state": [random.uniform(-1.0, 1.0) for _ in range(OBS_SIZE)],
            "action": random.randrange(N_ACTIONS),
            "reward": random.uniform(-1.0, 1.0),
            "next_state": [random.uniform(-1.0, 1.0) for _ in range(OBS_SIZE)],
            "done": False,
            "gold_delta": 0.0, "loot_delta": 0.0, "market_pnl": 0.0,
            "quest_reward": 0.0, "quest_intrinsic": 0.0, "rnd_bonus": 0.0,
        })
    tagent.t_step = 64
    losses = tagent.learn()
    assert set(losses) == {"td", "gold", "loot", "market", "quest", "rnd"}, losses
    assert all(isinstance(v, float) for v in losses.values()), losses
    tpath = os.path.join(tmpdir, "torch.pt")
    tagent.save_weights(tpath)
    tloaded = TorchDQNAgent(replay_size=64, batch_size=8)
    assert tloaded.load_weights(tpath)
    assert (tloaded.q.q_head.bias.detach().tolist() ==
            tagent.q.q_head.bias.detach().tolist())
    print("TORCH_LOOP_OK")

print("ALL_TRAINING_LOOPS_OK")
