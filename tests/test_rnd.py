"""RND curiosity unit tests (no live server needed).

Run from the repo root: python tests/test_rnd.py
Covers #35: bonus is non-negative and finite, the predictor learns
(error shrinks with updates), and RND state survives save/load.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))

import torch
from dqn_agent import TorchDQNAgent
from ml_env import OBS_SIZE

agent = TorchDQNAgent(rnd_lambda=0.1)
obs = [0.1 * ((i % 7) + 1) for i in range(OBS_SIZE)]

# --- bonus is non-negative and finite ---
b = agent.rnd_bonus(obs)
assert b >= 0.0 and b != float("inf") and b == b, b  # noqa: PLR0124 (NaN-proof)
print(f"RND_BONUS_OK ({b:.4f})")

# --- predictor learns: error shrinks after updates on the same state ---
x = torch.tensor([obs], dtype=torch.float32)
with torch.no_grad():
    tgt = agent.rnd_target(x)
err0 = float((((agent.rnd_pred(x) - tgt) ** 2).mean()).item())
for _ in range(30):
    agent.update_rnd(x)
err1 = float((((agent.rnd_pred(x) - tgt) ** 2).mean()).item())
assert err1 < err0, (err0, err1)
print(f"RND_LEARNS_OK ({err0:.4f} -> {err1:.4f})")

# --- novel states pay more than the trained one (#245) ---
b_seen = agent.rnd_bonus(obs)
novels = [[(0.9 - v + 0.05 * k) % 1.0 for v in obs] for k in range(3)]
b_novels = [agent.rnd_bonus(n) for n in novels]
for b_novel in b_novels:
    assert b_novel >= 0.0 and b_novel == b_novel  # noqa: PLR0124 (NaN-proof)
# Ordering on raw predictor error: training provably shrank the seen
# error above (RND_LEARNS_OK), so untouched regions must score higher.
with torch.no_grad():
    err_seen = float(agent._rnd_error(x).item())
    err_novels = [float(agent._rnd_error(
        torch.tensor([n], dtype=torch.float32)).item()) for n in novels]
assert min(err_novels) > err_seen, (err_seen, err_novels)
print(f"RND_NOVELTY_OK (seen={b_seen:.4f} novel_mean={sum(b_novels)/len(b_novels):.4f})")

# --- RND state survives save/load ---
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "rnd.pt")
    before = {k: v.clone() for k, v in agent.rnd_pred.state_dict().items()}
    agent.save_weights(path)
    fresh = TorchDQNAgent(rnd_lambda=0.1)
    assert fresh.load_weights(path)
    for k, v in before.items():
        assert torch.equal(fresh.rnd_pred.state_dict()[k], v), k
    assert fresh._rnd_mean == agent._rnd_mean
    assert fresh._rnd_var == agent._rnd_var
print("RND_PERSISTENCE_OK")

# --- rnd_lambda=0 disables curiosity: learn() reports rnd 0.0 and the
# predictor is untouched ---
off = TorchDQNAgent(rnd_lambda=0, replay_size=32, batch_size=8)
flat = [0.0] * OBS_SIZE
for _ in range(32):
    off.store({"state": flat, "action": 0, "reward": 0.0,
               "next_state": flat, "done": False})
off.t_step = 32
pred_before = {k: v.clone() for k, v in off.rnd_pred.state_dict().items()}
losses = off.learn()
assert losses["rnd"] == 0.0, losses
for k, v in pred_before.items():
    assert torch.equal(off.rnd_pred.state_dict()[k], v), k
print("RND_DISABLE_OK")

# --- pre-curiosity checkpoints still load (RND stays fresh) ---
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "old.pt")
    torch.save(
        {
            "q_state_dict": agent.q.state_dict(),
            "target_state_dict": agent.target.state_dict(),
            "optimizer_state_dict": agent.optimizer.state_dict(),
            "training_steps": 0,
            "learn_step": 0,
            "best_score": 0.0,
            "obs_size": OBS_SIZE,
        },
        path,
    )
    old = TorchDQNAgent()
    assert old.load_weights(path)
print("RND_BACKWARD_COMPAT_OK")

print("ALL_RND_OK")
