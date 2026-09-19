"""Farm checkpoint resume (#231): no server needed.

Run from the repo root:  python tests/test_farm_resume.py
Covers: Farm.save_weights round-trips training_steps (and dims/version)
so a restarted farm resumes the epsilon schedule instead of jumping
back to epsilon_start.
"""
import argparse
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
print("ALL_FARM_RESUME_OK")
