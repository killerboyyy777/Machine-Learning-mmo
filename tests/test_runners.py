"""Conductor runner tests (no live server needed).

Run from the repo root:  python tests/test_runners.py
Covers #52: env_factory builds configured envs, linear/torch policies run
inference on offline-built observations, and the supervisor passes the
valid-action mask to 3-arg policies (2-arg policies keep working).
Covers #183: torch quest-reward targets fire on all four chains' turn-ins.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor.runners import make_env_factory, make_linear_policy, make_torch_policy
from ml.conductor.supervisor import AgentTask
from ml.ml_env import N_ACTIONS, QUESTS, TextMMOEnv

# --- env_factory: configured env per agent id, no connection yet ---
factory = make_env_factory(url="ws://x:1", reward_mode="econ", max_steps=7)
env = factory("RunnerA")
assert isinstance(env, TextMMOEnv)
assert env.name == "RunnerA" and env.url == "ws://x:1"
assert env.reward_mode == "econ" and env.max_steps == 7
try:
    make_env_factory(reward_mode="fame")("X")
    raise SystemExit("FAIL: bad reward_mode accepted")
except ValueError:
    pass
print("ENV_FACTORY_OK")

# --- linear policy: inference on an offline-built observation ---
obs_env = TextMMOEnv("RunnerObs")
obs = obs_env._build_obs()
mask = obs_env.valid_action_mask()
lin = make_linear_policy(epsilon=0.0)
a = lin(obs, "RunnerObs", mask)
assert isinstance(a, int) and 0 <= a < N_ACTIONS, a
print("LINEAR_POLICY_OK")

# --- torch policy: same seam (lazy torch import) ---
tch = make_torch_policy(epsilon=0.0)
a = tch(obs, "RunnerObs", mask)
assert isinstance(a, int) and 0 <= a < N_ACTIONS, a
print("TORCH_POLICY_OK")


class _MaskEnv:
    def valid_action_mask(self):
        return [1] * N_ACTIONS

    async def reset(self):
        await asyncio.sleep(0)
        return {"o": 0}

    async def step(self, action):
        await asyncio.sleep(0)
        return ({"o": 1}, 0.5, True, {})


async def _run_once(task):
    gen = task.run()
    async for _ev in gen:
        task.cancel()
        await gen.aclose()
        break
    return task


# --- 3-arg policy receives the mask ---
seen = {}


def _p3(obs, aid, mask):
    seen.update(aid=aid, mask=list(mask))
    return 0


at3 = asyncio.run(_run_once(AgentTask("r3", _MaskEnv(), _p3, max_steps=2)))
assert seen["aid"] == "r3" and len(seen["mask"]) == N_ACTIONS
assert at3.steps == 1
print("MASK_ARITY_OK")

# --- 2-arg policy still works (arity probe falls back) ---
at2 = asyncio.run(_run_once(AgentTask("r2", _MaskEnv(), lambda o, a: 0, max_steps=2)))
assert at2.steps == 1
print("LEGACY_ARITY_OK")

# --- #183: torch quest-reward targets fire on all four chains' turn-ins ---
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "torch_agents"))
from torch_farm import Runner as TorchRunner

_farm = types.SimpleNamespace(args=types.SimpleNamespace(name_prefix="T", url="ws://x"),
                               agent=types.SimpleNamespace(intrinsic_accept=1.0,
                                                           intrinsic_progress=2.0,
                                                           rnd_lambda=0))
_tr = TorchRunner(0, _farm)
_info_all = {"quest": {"by_quest": {
    "guard_charm": {"turned_in": True},
    "delver": {"turned_in": True},
    "remedy": {"turned_in": True},
    "tonic": {"turned_in": True},
}}}
_gold, _loot, _pnl, _qr, _qi, _rnd = _tr.transition_targets({"gold_raw": 0}, [], _info_all, "quest_turn_in")
assert _qr == float(QUESTS["guard_charm"]["reward_points"] + QUESTS["delver"]["reward_points"]
                    + QUESTS["remedy"]["reward_points"] + QUESTS["tonic"]["reward_points"]), _qr
assert _qi == 0.0 and _rnd == 0.0
_gold, _loot, _pnl, _qr0, _qi0, _rnd0 = _tr.transition_targets({"gold_raw": 0}, [], {}, "attack")
assert _qr0 == 0.0
print("TORCH_QUEST_TARGETS_OK")

print("ALL_RUNNERS_OK")
