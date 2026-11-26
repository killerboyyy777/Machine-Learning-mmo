"""CLI range guards, action typing, shared budgets, crash status (#999, #1001, #1002, #1008).

Run from the repo root:  python tests/test_ml_cli_guards.py
Torch sections skip cleanly where torch is not installed.
"""

import argparse
import asyncio
import contextlib
import io
import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, ROOT)

import ml_botfarm  # noqa: E402
import ml_client  # noqa: E402
from ml_env import ACTIONS, N_ACTIONS, OBS_SIZE, TextMMOEnv  # noqa: E402

# --- #1001: linear client CLI rejects settings that divide by zero or no-op ---


def _cli_error(main, argv, option):
    with (
        patch.object(sys, "argv", ["prog"] + argv),
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        try:
            main()
        except SystemExit as exc:
            assert exc.code == 2, (argv, exc.code)
        else:
            raise SystemExit(f"FAIL: accepted invalid CLI args {argv}")
    assert option in err.getvalue(), (argv, err.getvalue())


for _argv, _option in (
    (["--save-every", "0"], "--save-every"),
    (["--epsilon-decay-steps", "0"], "--epsilon-decay-steps"),
    (["--epsilon-decay-steps", "-2"], "--epsilon-decay-steps"),
    (["--steps", "-1"], "--steps"),
    (["--epsilon-start", "-0.1"], "--epsilon-start"),
    (["--epsilon-start", "1.1"], "--epsilon-start"),
    (["--epsilon-end", "-0.1"], "--epsilon-end"),
    (["--epsilon-end", "1.1"], "--epsilon-end"),
):
    _cli_error(ml_client.main, _argv, _option)
print("LINEAR_CLI_GUARDS_OK")


def _linear_accepted(argv):
    seen = {}

    async def _train(*args, **kwargs):
        seen["args"] = args

    with (
        patch.object(sys, "argv", ["ml_client.py"] + argv),
        patch.object(ml_client, "start_run", return_value=None),
        patch.object(ml_client, "train", _train),
    ):
        ml_client.main()
    return seen["args"]


_accepted = _linear_accepted(
    [
        "--steps",
        "0",
        "--save-every",
        "1",
        "--epsilon-decay-steps",
        "1",
        "--epsilon-start",
        "0",
        "--epsilon-end",
        "1",
        "--no-run-record",
    ]
)
assert _accepted[2:] == (0, 1, 0, 1, 1), _accepted
print("LINEAR_CLI_BOUNDARIES_OK")


# --- #1001: bot farm CLI guards ---
# --steps 0 is the documented "run until stopped" mode.


def _botfarm_parse(argv):
    with patch.object(sys, "argv", ["ml_botfarm.py"] + argv):
        return ml_botfarm.parse_args()


for _argv, _option in (
    (["--steps", "-1"], "--steps"),
    (["--reward-window", "0"], "--reward-window"),
    (["--reward-window", "-3"], "--reward-window"),
    (["--eval-every", "0"], "--eval-every"),
    (["--eval-every", "-1"], "--eval-every"),
    (["--epsilon-start", "-0.1"], "--epsilon-start"),
    (["--epsilon-start", "1.2"], "--epsilon-start"),
    (["--epsilon-end", "-0.1"], "--epsilon-end"),
    (["--epsilon-end", "1.2"], "--epsilon-end"),
):
    _cli_error(ml_botfarm.parse_args, _argv, _option)
_bfa = _botfarm_parse(
    [
        "--steps",
        "0",
        "--reward-window",
        "1",
        "--eval-every",
        "0.01",
        "--epsilon-start",
        "0",
        "--epsilon-end",
        "1",
    ]
)
assert (
    _bfa.steps,
    _bfa.reward_window,
    _bfa.eval_every,
    _bfa.epsilon_start,
    _bfa.epsilon_end,
) == (0, 1, 0.01, 0.0, 1.0)
_bot = ml_botfarm.BotRunner(
    0,
    argparse.Namespace(
        role_list=[None],
        args=argparse.Namespace(reward_window=1, name_prefix="B", url="ws://x:1"),
    ),
)
assert _bot.fitness() is None, "an empty window must not divide by zero"
print("BOTFARM_CLI_GUARDS_OK")

# --- #1002: TextMMOEnv.step is an integer-index contract, not a bool one ---


async def _step_type_contract():
    class _Recorder:
        def __init__(self):
            self.cmds = []

        async def send(self, cmd, **kwargs):
            self.cmds.append(cmd)

    for bad in (True, False):
        env = TextMMOEnv("BoolStep")
        env.step_delay = 0
        env._state["room_id"] = "town_square"
        rec = _Recorder()
        env._send = rec.send
        try:
            await env.step(bad)
        except TypeError:
            pass
        else:
            raise SystemExit(f"FAIL: env.step accepted bool {bad!r}")
        assert rec.cmds == [], rec.cmds
        await env.close()

    env = TextMMOEnv("IntStep")
    env.step_delay = 0
    env._state["room_id"] = "town_square"
    rec = _Recorder()
    env._send = rec.send
    await env.step(ACTIONS.index("look"))
    assert "look" in rec.cmds, rec.cmds
    await env.close()


asyncio.run(_step_type_contract())
print("ENV_STEP_TYPE_OK")

# --- #1008: shared step budgets are reserved before the await ---


def _barrier_env(total, stop=None):
    gate = asyncio.Event()
    state = {"entered": 0, "accepted": 0}
    obs = {"score_raw": 0.0, "room_id": "town_square", "inv_names": []}

    class _Env:
        def __init__(self, name, **_kwargs):
            self.name = name

        async def reset(self):
            return dict(obs)

        def valid_action_mask(self):
            return [1] * N_ACTIONS

        async def step(self, _action):
            state["entered"] += 1
            if state["entered"] >= total:
                gate.set()
                if stop is not None:
                    stop.set()
            await gate.wait()
            state["accepted"] += 1
            return dict(obs), 0.0, False, {}

        async def close(self):
            pass

    return _Env, gate, state


async def _release(gate, coros):
    handle = asyncio.get_running_loop().call_later(0.05, gate.set)
    try:
        await asyncio.wait_for(asyncio.gather(*coros), timeout=2)
    finally:
        handle.cancel()


def _botfarm_fleet(budget, fleet, scripted=False):
    class _Policy:
        def select(self, _env):
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        args = argparse.Namespace(
            name_prefix="F",
            url="ws://x:1",
            scripted="gather" if scripted else "none",
            roles=None,
            reward_window=10,
            epsilon_start=0.0,
            epsilon_end=0.0,
            epsilon_decay_steps=1,
            weights=os.path.join(tmp, "missing.json"),
            checkpoint_every=100,
            eval_every=9999,
            steps=budget,
            bots=fleet,
        )
        policies = patch.dict(ml_botfarm.SCRIPTED_POLICIES, {"gather": _Policy})
        with (
            patch.object(ml_botfarm, "BEST_FILE", os.path.join(tmp, "best.json")),
            patch.object(ml_botfarm, "WEIGHTS_FILE", os.path.join(tmp, "weights.json")),
            policies,
        ):
            farm = ml_botfarm.Farm(args)
            env_cls, gate, state = _barrier_env(fleet, farm.stop)
            with (
                patch.object(ml_botfarm, "TextMMOEnv", env_cls),
                patch.object(ml_botfarm, "flatten_obs", lambda _obs: [0.0] * OBS_SIZE),
            ):
                farm.bots = [ml_botfarm.BotRunner(i, farm) for i in range(fleet)]
                asyncio.run(_release(gate, [b.run() for b in farm.bots]))
                return farm.steps, state["accepted"], farm.stop.is_set()


assert _botfarm_fleet(1, 4) == (1, 1, True)
assert _botfarm_fleet(1, 4, scripted=True) == (1, 1, True)
# Negative control: without a budget every bot reaches the barrier and steps.
_unlimited = _botfarm_fleet(0, 4)
assert _unlimited[:2] == (4, 4), _unlimited
print("BOTFARM_STEP_BUDGET_OK")

# --- torch-gated: torch farm budget and crash exit status ---

try:
    sys.path.insert(0, os.path.join(ROOT, "torch_agents"))
    import dqn_agent  # noqa: F401
    import torch_farm

    HAVE_TORCH = True
except ImportError as exc:
    print(f"TORCH_GUARDS_SKIP (no torch: {exc})")
    HAVE_TORCH = False

if HAVE_TORCH:

    class _FakeAgent:
        def __init__(self, **_kwargs):
            self.t_step = 0
            self.best_score = 0.0

        def load_weights(self, _path):
            return False

        def _epsilon(self):
            return 0.0

        def act(self, _features, _epsilon, _mask):
            return 0

        def store(self, _transition):
            pass

        def learn(self):
            pass

        def save_weights(self, _path):
            pass

    def _torch_fleet(budget, fleet):
        async def _reset(env, **_kwargs):
            return await env.reset()

        async def _checkpoint(_farm, force=False):
            pass

        def _targets(_runner, _obs, _features, _info, _action):
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name_prefix="TF",
                url="ws://x:1",
                weights=os.path.join(tmp, "w.pt"),
                best_weights=os.path.join(tmp, "b.pt"),
                steps=budget,
                save_every=1000,
                agents=fleet,
                rnd_lambda=0.0,
                rnd_lambda_min=0.0,
                rnd_decay_steps=1,
                rnd_lr=1e-3,
                spawn_spread=False,
                td_norm=False,
                td_clip=5.0,
                reward_formula=None,
                seed=None,
                run_record=False,
                runs_dir=None,
            )
            with patch.object(torch_farm, "TorchDQNAgent", _FakeAgent):
                farm = torch_farm.TorchFarm(args)
                env_cls, gate, state = _barrier_env(fleet, farm.stop)
                with (
                    patch.object(torch_farm, "TextMMOEnv", env_cls),
                    patch.object(
                        torch_farm, "flatten_obs", lambda _obs: [0.0] * OBS_SIZE
                    ),
                    patch.object(torch_farm, "reset_with_retry", _reset),
                    patch.object(torch_farm.Runner, "transition_targets", _targets),
                    patch.object(torch_farm.TorchFarm, "checkpoint", _checkpoint),
                ):
                    runners = [torch_farm.Runner(i, farm) for i in range(fleet)]
                    asyncio.run(_release(gate, [r.run() for r in runners]))
                    return farm.steps, state["accepted"], farm.stop.is_set()

    assert _torch_fleet(1, 4) == (1, 1, True)
    # Negative control: an unlimited farm still admits the whole fleet.
    assert _torch_fleet(0, 4)[:2] == (4, 4)
    print("TORCHFARM_STEP_BUDGET_OK")

    class _FakeRun:
        run_id = "probe"
        path = "probe"

        def __init__(self):
            self.manifest = {"status": "running"}
            self.status = None
            self.kwargs = None

        def finish(self, status, **kwargs):
            self.status = status
            self.manifest["status"] = status
            self.kwargs = kwargs

    class _FakeFarm:
        def __init__(self, args):
            self.args = args
            self.stop = asyncio.Event()
            self.steps = 0
            self.last_save_step = -1
            self.last_best_score = 0.0
            self.runners = []
            self.run = None
            self.base_steps = 0
            self.spawn_rooms = []

        async def checkpoint(self, force=False):
            pass

        def _sample_run(self):
            pass

    def _fake_runner(mode):
        class _Runner:
            def __init__(self, index, farm):
                self.index = index
                self.farm = farm
                self.name = f"R{index}"
                self.steps = 0
                self.score = 0.0
                self.reset_retries = 0
                self.last_error = None

            async def run(self):
                if mode == "crash" and self.index == 0:
                    raise RuntimeError("boom")
                if mode == "clean":
                    return
                if mode == "steps":
                    self.farm.steps = self.farm.args.steps
                    return
                # Only cancellation ends this one: proves a crash stops the
                # remaining fleet instead of waiting on them forever.
                await asyncio.Event().wait()

        return _Runner

    def _main_args(steps):
        return argparse.Namespace(
            agents=3,
            name_prefix="T",
            url="ws://x:1",
            steps=steps,
            save_every=5,
        )

    async def _run_main(mode, steps):
        record = _FakeRun()
        with (
            patch.object(torch_farm, "TorchFarm", _FakeFarm),
            patch.object(torch_farm, "Runner", _fake_runner(mode)),
            patch.object(torch_farm, "open_run", lambda _args: record),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = await asyncio.wait_for(
                torch_farm.main_async(_main_args(steps)), timeout=2
            )
        return code, record

    for _steps in (0, 10):
        _code, _record = asyncio.run(_run_main("crash", _steps))
        assert _code == 1, (_steps, _code)
        assert _record.status == "failed", _record.status
        assert _record.kwargs["crashed"] == 1, _record.kwargs
    print("TORCHFARM_CRASH_EXIT_OK")

    for _mode, _steps in (("clean", 0), ("clean", 10), ("steps", 1)):
        _code, _record = asyncio.run(_run_main(_mode, _steps))
        assert _code == 0, (_mode, _steps, _code)
        assert _record.status == "finished", _record.status
        assert _record.kwargs["crashed"] == 0, _record.kwargs
    print("TORCHFARM_CLEAN_EXIT_OK")

# --- #1001: standalone DQN trainer CLI guards (torch-gated) ---

if HAVE_TORCH:

    class _FakeDQNAgent:
        seen = None

        def __init__(self, **_kwargs):
            pass

        def load_weights(self):
            return False

        async def train(self, total_steps, save_every):
            type(self).seen = (total_steps, save_every)

    def _dqn_main(argv):
        with (
            patch.object(sys, "argv", ["dqn_agent.py"] + argv),
            patch.object(dqn_agent, "TorchDQNAgent", _FakeDQNAgent),
            patch.object(dqn_agent, "start_run", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            dqn_agent.main()

    for _argv, _option in (
        (["--save-every", "0"], "--save-every"),
        (["--steps", "-1"], "--steps"),
    ):
        _cli_error(dqn_agent.main, _argv, _option)
    _dqn_main(["--steps", "0", "--save-every", "1", "--no-run-record"])
    assert _FakeDQNAgent.seen == (0, 1), _FakeDQNAgent.seen
    print("DQN_CLI_GUARDS_OK")

print("ALL_ML_CLI_GUARDS_OK")
