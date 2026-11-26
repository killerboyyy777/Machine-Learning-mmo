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
            "learn_every": {
                "type": int,
                "default": 4,
                "help": "shared env steps between DQN learn() calls",
            },
        }

    def __init__(self, **config):
        super().__init__(**config)
        # One plugin instance is shared by the agents assigned to a slot.
        # Keep the throttle on that instance so independent slots do not
        # advance each other's cadence or starve one another of learning.
        self._shared_learn_steps = 0
        try:
            from torch_agents.dqn_agent import TorchDQNAgent
        except ImportError as e:
            raise ImportError(f"torch plugin needs torch + torch_agents: {e}")
        self._agent = TorchDQNAgent()
        if self.checkpoint:
            self._agent.load_weights(self.checkpoint)

    def act(self, obs, agent_id, mask=None):
        return self._agent.act(flatten_obs(obs), self.epsilon, mask)

    def learn(self, prev_obs, action, reward, next_obs, done, next_mask=None):
        """Store every transition; learn every K shared slot steps."""
        transition = {
            "state": flatten_obs(prev_obs),
            "action": action,
            "reward": reward,
            "next_state": flatten_obs(next_obs),
            "done": done,
        }
        # Omit, not None: replay defaults a missing mask to all-valid, but a
        # None value cannot become a tensor in the DQN batch conversion.
        if next_mask is not None:
            transition["next_mask"] = next_mask
        self._agent.store(transition)
        self._shared_learn_steps += 1
        if self._shared_learn_steps % max(1, self.learn_every) == 0:
            self._agent.learn()

    def save(self, path):
        self._agent.save_weights(path)
        return True

    def load(self, path):
        self._agent.load_weights(path)
        return True
