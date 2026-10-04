"""Farm checkpoint resume (#231): no server needed.

Run from the repo root:  python tests/test_farm_resume.py
Covers: Farm.save_weights round-trips training_steps (and dims/version)
so a restarted farm resumes the epsilon schedule instead of jumping
back to epsilon_start. Also covers TorchFarm checkpoint/resume: farm
steps and best_score survive restart (torch-gated, skips without torch).
"""
import argparse
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))

import ml_botfarm
from ml_botfarm import Farm


def _args(weights):
    return argparse.Namespace(
        weights=weights, checkpoint_every=200,
        epsilon_start=1.0, epsilon_end=0.05, epsilon_decay_steps=5000,
        bots=1,
    )


def test_save_resumes_epsilon():
    with tempfile.TemporaryDirectory() as tmp:
        w = os.path.join(tmp, "ml_weights.json")
        ml_botfarm.WEIGHTS_FILE = w
        ml_botfarm.BEST_FILE = os.path.join(tmp, "ml_best.json")
        farm = Farm(_args(w))
        farm.agent.training_steps = 2500  # halfway through decay
        eps_before = farm.epsilon_now()
        assert 0.05 < eps_before < 1.0, eps_before
        farm.save_weights()
        # Fresh farm on the same file resumes, not restarts.
        farm2 = Farm(_args(w))
        assert farm2.agent.training_steps == 2500, farm2.agent.training_steps
        assert abs(farm2.epsilon_now() - eps_before) < 1e-9
        print(f"FARM_RESUME_OK (steps=2500 eps={eps_before:.3f})")


test_save_resumes_epsilon()

# --- TorchFarm save/resume: steps + best_score survive restart ---
# Torch-gated (HAVE_TORCH pattern like tests/test_training_loops.py):
# importing torch_farm pulls in torch via dqn_agent, so skip cleanly
# where torch is not installed.
try:
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "torch_agents",
        ),
    )
    from torch_farm import TorchFarm

    HAVE_TORCH = True
except ImportError as e:
    print(f"TORCH_FARM_RESUME_SKIP (no torch: {e})")
    HAVE_TORCH = False

if HAVE_TORCH:

    def _torch_args(weights, best):
        return argparse.Namespace(
            name_prefix="T",
            url="ws://127.0.0.1:9",
            weights=weights,
            best_weights=best,
            steps=0,
            save_every=10,
            rnd_lambda=1.0,
            rnd_lambda_min=0.1,
            rnd_decay_steps=100000,
            rnd_lr=1e-3,
        )

    def test_torchfarm_resume():
        with tempfile.TemporaryDirectory() as tmp:
            w = os.path.join(tmp, "farm.pt")
            b = os.path.join(tmp, "farm_best.pt")
            farm = TorchFarm(_torch_args(w, b))
            farm.steps = 42
            farm.agent.t_step = 2500
            farm.agent.best_score = 12.5
            farm.last_best_score = 12.5
            asyncio.run(farm.checkpoint(force=True))
            resumed = TorchFarm(_torch_args(w, b))
            assert resumed.steps == 42, resumed.steps
            assert resumed.last_save_step == 42, resumed.last_save_step
            assert resumed.agent.t_step == 2500, resumed.agent.t_step
            assert resumed.agent.best_score == 12.5, resumed.agent.best_score
            assert resumed.last_best_score == 12.5, resumed.last_best_score
            print("TORCHFARM_RESUME_OK (steps=42 t_step=2500 best=12.5)")

    test_torchfarm_resume()

print("ALL_FARM_RESUME_OK")
