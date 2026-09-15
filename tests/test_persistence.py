"""Offline save/load checks for both ML agents.

Run from the repo root: python tests/test_persistence.py
No game server is needed.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))

from ml_client import LinearQAgent
from ml_env import N_ACTIONS, OBS_SIZE
from dqn_agent import TorchDQNAgent


def linear_check(folder):
    path = os.path.join(folder, "linear.json")
    agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
    features = [0.25] * OBS_SIZE
    agent.training_steps = 123
    agent.update(features, 3, 2.5, features, False)
    expected = agent.q_values(features)
    agent.save(path)

    loaded = LinearQAgent(OBS_SIZE, N_ACTIONS)
    assert loaded.load(path)
    assert loaded.training_steps == 123
    assert loaded.q_values(features) == expected
    assert loaded.bias == agent.bias
    print("LINEAR_PERSISTENCE_OK")


def torch_check(folder):
    path = os.path.join(folder, "torch.pt")
    agent = TorchDQNAgent()
    agent.q.q_head.bias.data.fill_(1.25)
    agent.target.load_state_dict(agent.q.state_dict())
    agent.t_step = 456
    agent.learn_step = 17
    agent.best_score = 89.5
    agent.save_weights(path)

    loaded = TorchDQNAgent()
    assert loaded.load_weights(path)
    assert loaded.t_step == 456
    assert loaded.learn_step == 17
    assert loaded.best_score == 89.5
    assert loaded.q.q_head.bias.detach().tolist() == agent.q.q_head.bias.detach().tolist()
    assert loaded.target.q_head.bias.detach().tolist() == agent.target.q_head.bias.detach().tolist()
    print("TORCH_PERSISTENCE_OK")


with tempfile.TemporaryDirectory() as folder:
    linear_check(folder)
    torch_check(folder)
print("PERSISTENCE_ALL_OK")
