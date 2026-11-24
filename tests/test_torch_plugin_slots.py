"""Regression for independent TorchPlugin slot learning cadences."""

import sys
from types import ModuleType
from unittest.mock import patch

from ml.plugins import torch_plugin


class _FakeDQNAgent:
    def __init__(self):
        self.stored = []
        self.learn_calls = 0

    def store(self, transition):
        self.stored.append(transition)

    def learn(self):
        self.learn_calls += 1


def main():
    # The offline test job does not install torch. Stub only the DQN backend;
    # the plugin's scheduling and transition forwarding remain real.
    fake_module = ModuleType("torch_agents.dqn_agent")
    fake_module.TorchDQNAgent = _FakeDQNAgent

    # Reset the old class counter too, so this regression is deterministic
    # against the buggy implementation and does not depend on test order.
    torch_plugin.TorchPlugin._shared_learn_steps = 0

    with patch.dict(sys.modules, {"torch_agents.dqn_agent": fake_module}):
        with patch.object(torch_plugin, "flatten_obs", side_effect=lambda obs: obs):
            first = torch_plugin.TorchPlugin(learn_every=2)
            second = torch_plugin.TorchPlugin(learn_every=2)

            for _ in range(2):
                first.learn({}, 0, 0.0, {}, False)
                second.learn({}, 0, 0.0, {}, False)

    assert len(first._agent.stored) == 2
    assert len(second._agent.stored) == 2
    assert first._agent.learn_calls == 1, first._agent.learn_calls
    assert second._agent.learn_calls == 1, second._agent.learn_calls
    print("TORCH_PLUGIN_SLOT_CADENCE_OK")


if __name__ == "__main__":
    main()
