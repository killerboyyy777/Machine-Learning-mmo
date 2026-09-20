"""Linear-Q agent plugin (moved from ml/conductor/runners.py)."""

try:
    from ..ml_client import LinearQAgent
    from ..ml_env import N_ACTIONS, OBS_SIZE, flatten_obs
except ImportError:
    from ml_client import LinearQAgent
    from ml_env import N_ACTIONS, OBS_SIZE, flatten_obs

from . import AgentPlugin, register


@register
class LinearPlugin(AgentPlugin):
    """Epsilon-greedy linear-Q checkpoint."""

    name = "linear"
    agent_type = "linear"

    @classmethod
    def config_schema(cls):
        return {
            "checkpoint": {
                "type": str,
                "default": None,
                "help": "weights file (fresh zeroed policy if unset)",
            },
            "epsilon": {"type": float, "default": 0.0, "help": "exploration rate"},
        }

    def __init__(self, **config):
        super().__init__(**config)
        self._agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
        if self.checkpoint:
            self._agent.load(self.checkpoint)

    def act(self, obs, agent_id, mask=None):
        return self._agent.act(flatten_obs(obs), self.epsilon, mask)

    def learn(self, prev_obs, action, reward, next_obs, done):
        """One Q-learning step on this transition (#292)."""
        return self._agent.update(flatten_obs(prev_obs), action, reward,
                                  flatten_obs(next_obs), done)

    def save(self, path):
        self._agent.save(path)
        return True

    def load(self, path):
        self._agent.load(path)
        return True
