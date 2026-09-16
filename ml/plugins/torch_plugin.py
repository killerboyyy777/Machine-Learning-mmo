"""DQN agent plugin (moved from ml/conductor/runners.py).

Torch stays a lazy import so the plugin package (and discovery) works
without it; instantiating this plugin without torch raises a clear error.
"""

try:
    from ..ml_env import flatten_obs
except ImportError:
    from ml_env import flatten_obs

from . import AgentPlugin, register


@register
class TorchPlugin(AgentPlugin):
    """Epsilon-greedy DQN checkpoint."""

    name = "torch"
    agent_type = "torch"

    @classmethod
    def config_schema(cls):
        return {
            "checkpoint": {
                "type": str,
                "default": None,
                "help": "weights file (fresh network if unset)",
            },
            "epsilon": {"type": float, "default": 0.0, "help": "exploration rate"},
        }

    def __init__(self, **config):
        super().__init__(**config)
        try:
            from torch_agents.dqn_agent import TorchDQNAgent
        except ImportError as e:
            raise ImportError(f"torch plugin needs torch + torch_agents: {e}")
        self._agent = TorchDQNAgent()
        if self.checkpoint:
            self._agent.load_weights(self.checkpoint)

    def act(self, obs, agent_id, mask=None):
        return self._agent.act(flatten_obs(obs), self.epsilon, mask)
