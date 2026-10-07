"""Eval honors a configured custom reward formula (no live server needed).

Run from the repo root:  python tests/test_eval_reward_formula.py
Covers the eval.py gap where eval_seed built TextMMOEnv on reward_mode
only, so a configured formula never reached the env and eval kept
paying the mode reward.
"""

import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, ROOT)

try:
    import torch  # noqa: F401
except ImportError:
    _stub = types.ModuleType("torch")
    _stub.manual_seed = lambda seed: None  # type: ignore[attr-defined]
    sys.modules["torch"] = _stub

import eval as eval_mod  # noqa: E402

seen = []


class _FakeEnv:
    def __init__(
        self, name, url=None, max_steps=None, reward_mode="score", reward_formula=None
    ):
        seen.append({"reward_mode": reward_mode, "reward_formula": reward_formula})
        self._steps = 0
        self._max = 2

    async def reset(self):
        return {"score_raw": 7.0}

    def valid_action_mask(self):
        return [1]

    async def step(self, action):
        self._steps += 1
        done = self._steps >= self._max
        return {"score_raw": 7.0}, 1.0, done, {}

    async def close(self):
        return None


eval_mod.TextMMOEnv = _FakeEnv
eval_mod.flatten_obs = lambda obs: [0.0]
eval_mod._load_agent = lambda policy, checkpoint: (lambda f, m: 0)


async def _run_seed(**kw):
    return await eval_mod.eval_seed(lambda f, m: 0, "ws://x", 0, 2, "xp", **kw)


score = asyncio.run(_run_seed(reward_formula="score_delta"))
assert score == 7.0, score
assert seen[-1]["reward_formula"] == "score_delta", seen[-1]
assert seen[-1]["reward_mode"] == "xp", seen[-1]
print("EVAL_SEED_FORWARDS_FORMULA_OK")

n = len(seen)
scores = asyncio.run(
    eval_mod.evaluate(
        "ckpt.pt",
        "linear",
        "ws://x",
        [0, 1],
        2,
        "score",
        tag="t",
        reward_formula="0.5*xp_delta",
    )
)
assert scores == [7.0, 7.0], scores
assert len(seen) == n + 2, seen
assert all(s["reward_formula"] == "0.5*xp_delta" for s in seen[n:]), seen[n:]
print("EVALUATE_FORWARDS_FORMULA_OK")

n = len(seen)
scores = asyncio.run(
    eval_mod.evaluate("ckpt.pt", "linear", "ws://x", [0], 2, "score", tag="t")
)
assert scores == [7.0], scores
assert len(seen) == n + 1, seen
assert seen[-1]["reward_formula"] is None, seen[-1]
print("EVAL_DEFAULT_FORMULA_NONE_OK")

from ml_env import TextMMOEnv as RealEnv  # noqa: E402

real = RealEnv("EvalFormulaReal", reward_formula="score_delta", reward_mode="xp")
assert real._has_custom_reward is True, real._has_custom_reward
assert real._formula is not None
plain = RealEnv("EvalFormulaPlain", reward_mode="xp")
assert plain._has_custom_reward is False, plain._has_custom_reward
assert plain._formula is None
print("REAL_ENV_FORMULA_GATE_OK")

print("ALL_EVAL_REWARD_FORMULA_OK")
