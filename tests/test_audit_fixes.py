"""Regressions for audit issues #1015-#1019.

Run from the repo root:  python tests/test_audit_fixes.py
No server and no torch: the farm checkpoint test stubs its DQN backend.
Each fix has a positive case and a control that would pass with the bug.
"""

import argparse
import asyncio
import inspect
import json
import os
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml.conductor.conductor import Conductor  # noqa: E402
from ml.conductor.mixer import Mixer  # noqa: E402
from ml.conductor.registry import Registry  # noqa: E402
from ml.conductor.supervisor import AgentTask, Supervisor, _env_floor  # noqa: E402


def _tmp(name):
    path = os.path.join(tempfile.mkdtemp(), name)
    os.makedirs(path, exist_ok=True)
    return path


class _StepEnv:
    def __init__(self, state=None):
        self._state = state or {
            "room_id": "town_square",
            "is_dungeon": False,
            "dungeon_floor": 0,
        }

    async def reset(self):
        await asyncio.sleep(0)
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(0)
        return ({"obs": 1}, 1.0, True, {})

    def valid_action_mask(self):
        return [1, 1, 0]


def test_floor_keys_match_supervisor():
    cond = Conductor(_tmp("floors"), max_agents=3, resume=False)
    dungeon = _StepEnv({"room_id": "d_10_f1", "is_dungeon": True, "dungeon_floor": 1})
    assert "d1" in cond.mixer.floor_ids, cond.mixer.floor_ids
    assert _env_floor(dungeon) == "d1"
    cond.mixer.record_reward(_env_floor(dungeon), "a", 123.0)
    assert cond.mixer.snapshot()["d1"]["mean_reward"] == 123.0
    # Control: the old key is still silently ignored, and a surface id works.
    cond.mixer.record_reward("d_10_f1", "a", 999.0)
    assert "d_10_f1" not in cond.mixer.snapshot()
    cond.mixer.record_reward("town_square", "a", 7.0)
    assert cond.mixer.snapshot()["town_square"]["mean_reward"] == 7.0

    async def episode():
        env = _StepEnv({"room_id": "d_10_f1", "is_dungeon": True, "dungeon_floor": 1})
        await cond.supervisor.start_agent("d1a", lambda aid: env, lambda o, a: 0)
        await asyncio.sleep(0.1)
        await cond.supervisor.stop_all()

    asyncio.run(episode())
    assert cond.mixer.snapshot()["d1"]["mean_reward"] == 1.0
    print("FLOOR_KEYS_OK")


def _rebalance(means, agents=8, tag="mix"):
    mx = Mixer(Registry(_tmp("rebal" + tag), max_agents=20), ["good", "bad"])
    mx.assign_initial([f"a{i}" for i in range(agents)])
    for floor, rewards in means.items():
        for i, reward in enumerate(rewards):
            mx.record_reward(floor, f"{floor}{i}", reward)
    return mx.rebalance(), mx.snapshot()


def test_rebalance_sign_and_mixed_control():
    moves, snap = _rebalance(
        {"good": [-1.0, -1.0], "bad": [-10.0, -10.0]}, tag="allnegative"
    )
    assert moves and all(m[1] == "bad" and m[2] == "good" for m in moves), moves
    assert snap["good"]["agents"] == 7 and snap["bad"]["agents"] == 1, snap
    # Mixed signs with a negative sum inverted the old formula as well.
    moves, snap = _rebalance({"good": [1.0, 1.0], "bad": [-10.0, -10.0]}, tag="mixed")
    assert moves and all(m[1] == "bad" and m[2] == "good" for m in moves), moves
    assert snap["good"]["agents"] > snap["bad"]["agents"], snap
    # Control: a positive-only allocation must keep its old proportional split.
    moves, snap = _rebalance({"good": [10.0, 10.0], "bad": [0.0, 0.0]}, tag="positive")
    assert moves and all(m[1] == "bad" and m[2] == "good" for m in moves), moves
    print("REBALANCE_SIGN_OK")


def test_checkpoint_sidecar_step():
    fake_module = ModuleType("dqn_agent")
    fake_module.TorchDQNAgent = object

    class _Recorder:
        def save_weights(self, path):
            self.saved_at = farm.steps

    sys.path.insert(0, os.path.join(ROOT, "torch_agents"))
    sys.path.insert(0, os.path.join(ROOT, "ml"))
    with patch.dict(sys.modules, {"dqn_agent": fake_module}):
        import torch_farm

        class _Farm(torch_farm.TorchFarm):
            def __init__(self, path):
                self.args = argparse.Namespace(save_every=100, weights=path)
                self.steps = 100
                self.last_save_step = -1
                self.agent = _Recorder()
                self._farm_state_path = path + ".farm.json"
                self.run = None
                self.base_steps = 0
                self.sampled = None

            def _sample_run(self, steps=None):
                self.sampled = steps

        path = os.path.join(_tmp("farm"), "weights.pt")
        farm = _Farm(path)
        gate = asyncio.Event()
        original_to_thread = asyncio.to_thread

        async def gated(fn, *args, **kwargs):
            await gate.wait()
            return await original_to_thread(fn, *args, **kwargs)

        async def scenario():
            # Force the sidecar thread to run after another runner advanced
            # the shared counter, which is the race in #1017.
            with patch.object(asyncio, "to_thread", gated):
                task = asyncio.create_task(farm.checkpoint(force=True))
                await asyncio.sleep(0)
                farm.steps = 200
                gate.set()
                await task

        asyncio.run(scenario())
        with open(farm._farm_state_path) as handle:
            state = json.load(handle)
        assert farm.agent.saved_at == 100
        assert state == {"steps": 100, "last_save_step": 100}, state
        assert farm.sampled == 100, farm.sampled
        assert farm.steps == 200
        print("CHECKPOINT_SIDECAR_OK")


def test_learn_hooks_receive_next_mask():
    six_arg = []
    five_arg = []

    async def scenario():
        sup = Supervisor(Registry(_tmp("learnhooks"), max_agents=5))
        await sup.start_agent(
            "six",
            lambda aid: _StepEnv(),
            lambda o, a: 0,
            learn_hook=lambda p, a, r, n, d, m: six_arg.append(m),
        )
        await sup.start_agent(
            "five",
            lambda aid: _StepEnv(),
            lambda o, a: 0,
            learn_hook=lambda p, a, r, n, d: five_arg.append((p, n)),
        )
        await asyncio.sleep(0.1)
        await sup.stop_all()

    asyncio.run(scenario())
    assert six_arg and all(m == [1, 1, 0] for m in six_arg), six_arg
    assert five_arg and all(n == {"obs": 1} for _p, n in five_arg), five_arg
    task = AgentTask("x", _StepEnv(), lambda o, a: 0, learn_hook=lambda *a: None)
    assert task._learn_takes_mask is False

    from ml.plugins import linear as linear_mod
    from ml.plugins import torch_plugin

    class _FakeDQN:
        def __init__(self):
            self.stored = []
            self.learns = 0

        def store(self, transition):
            self.stored.append(transition)

        def learn(self):
            self.learns += 1

    fake_module = ModuleType("torch_agents.dqn_agent")
    fake_module.TorchDQNAgent = _FakeDQN
    with (
        patch.dict(sys.modules, {"torch_agents.dqn_agent": fake_module}),
        patch.object(linear_mod, "flatten_obs", side_effect=lambda o: o),
        patch.object(torch_plugin, "flatten_obs", side_effect=lambda o: o),
    ):
        linear = linear_mod.LinearPlugin()
        n_actions = linear_mod.N_ACTIONS
        features = [1.0] * linear_mod.OBS_SIZE
        weights = [[0.0] * linear_mod.OBS_SIZE for _ in range(n_actions)]
        weights[-1] = [50.0] * linear_mod.OBS_SIZE
        linear._agent.weights = weights
        mask = [1] * n_actions
        mask[-1] = 0
        masked_td = linear.learn(features, 0, 0.0, features, False, mask)
        unmasked_td = linear.learn(features, 0, 0.0, features, False)
        assert masked_td == 0.0, masked_td
        assert unmasked_td == 0.9 * 50 * linear_mod.OBS_SIZE, unmasked_td

        torch_plugin.TorchPlugin._shared_learn_steps = 0
        plugin = torch_plugin.TorchPlugin(learn_every=100)
        plugin.learn(features, 0, 0.0, features, False, mask)
        plugin.learn(features, 0, 0.0, features, False)
        assert plugin._agent.stored[0]["next_mask"] == mask
        assert plugin._agent.stored[1]["next_mask"] is None
    assert "next_mask" in inspect.signature(linear_mod.LinearPlugin.learn).parameters
    assert "next_mask" in inspect.signature(torch_plugin.TorchPlugin.learn).parameters
    print("LEARN_MASKS_OK")


def test_rebalance_moves_are_applied():
    envs = []

    def make_env(aid):
        env = _StepEnv()
        envs.append(env)
        return env

    cond = Conductor(
        _tmp("apply"),
        max_agents=4,
        resume=False,
        floors=["f1", "f2"],
        runners=[{"env_factory": make_env, "policy_fn": lambda o, a: 0}],
    )

    async def scenario():
        for aid in ("m1", "m2", "m3", "m4"):
            cond.registry.register(aid, "linear")
            assert await cond._maybe_start(aid)
        cond.mixer._floor_agents["f2"] = ["m1", "m2"]
        cond.mixer._floor_agents["f1"] = ["m3", "m4"]
        cond.mixer.record_reward("f1", "x", 100.0)
        cond.mixer.record_reward("f1", "y", 100.0)
        cond.mixer.record_reward("f2", "z", 0.0)
        cond.mixer.record_reward("f2", "w", 0.0)
        moves = cond.mixer.rebalance()
        assert len(moves) == 1 and moves[0][2] == "f1", moves
        apply_moves = getattr(cond, "_apply_moves", None)
        applied = await apply_moves(moves) if apply_moves else []
        assert applied == moves, (applied, moves)
        assert cond.mixer.snapshot()["f1"]["agents"] == 3
        assert cond.mixer.snapshot()["f2"]["agents"] == 1
        assert envs[-1].floor_target == "f1"
        assert envs[-1].spawn_room == "f1"
        before = len(envs)
        assert await cond._apply_moves([]) == []
        assert len(envs) == before
        await cond.supervisor.stop_all()

    asyncio.run(scenario())
    print("REBALANCE_APPLIED_OK")


if __name__ == "__main__":
    test_floor_keys_match_supervisor()
    test_rebalance_sign_and_mixed_control()
    test_checkpoint_sidecar_step()
    test_learn_hooks_receive_next_mask()
    test_rebalance_moves_are_applied()
    print("ALL_AUDIT_FIXES_OK")
