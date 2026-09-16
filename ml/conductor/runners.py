"""Conductor agent runners: concrete env_factory + policy_fn (#52).

The supervisor only knows the seams (env_factory(agent_id) -> env,
policy_fn(obs, agent_id[, mask]) -> action_idx). This module fills them
with the real game env and real checkpoint-backed policies, so the
conductor can actually run agents instead of just tracking them:

    from ml.conductor.runners import make_env_factory, make_linear_policy

    sup = Supervisor(registry, mixer=mixer)
    await sup.start_agent("a0", make_env_factory(url, reward_mode="econ"),
                          make_linear_policy("ml/ml_best.json"))

Policies take the env's structured obs dict (what reset()/step() return)
and an optional valid-action mask -- the supervisor passes the mask when
the policy accepts a third argument.
"""

try:
    from ..ml_client import LinearQAgent
    from ..ml_env import N_ACTIONS, OBS_SIZE, TextMMOEnv, flatten_obs
except ImportError:
    # Running from inside ml/ (python ml/conductor/runners.py): flat layout.
    from ml_client import LinearQAgent
    from ml_env import N_ACTIONS, OBS_SIZE, TextMMOEnv, flatten_obs


def make_env_factory(
    url="ws://localhost:8765", reward_mode="score", max_steps=None, step_delay=0.15
):
    """Return env_factory(agent_id) creating a live TextMMOEnv per agent.

    Construction is sync (the WebSocket connects on reset(), inside the
    supervised task), matching the supervisor's sync factory seam."""

    def factory(agent_id):
        return TextMMOEnv(
            agent_id,
            url=url,
            step_delay=step_delay,
            max_steps=max_steps,
            reward_mode=reward_mode,
        )

    return factory


def make_linear_policy(checkpoint=None, epsilon=0.0):
    """Return policy_fn running a linear-Q checkpoint (epsilon-greedy)."""
    agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
    if checkpoint:
        agent.load(checkpoint)

    def policy(obs, agent_id, mask=None):
        return agent.act(flatten_obs(obs), epsilon, mask)

    return policy


def make_torch_policy(checkpoint=None, epsilon=0.0, **agent_kwargs):
    """Return policy_fn running a DQN checkpoint (epsilon-greedy).

    Torch is imported lazily so the conductor package stays importable
    without it."""
    try:
        from torch_agents.dqn_agent import TorchDQNAgent
    except ImportError as e:
        raise ImportError(f"make_torch_policy needs torch + torch_agents: {e}")
    agent = TorchDQNAgent(**agent_kwargs)
    if checkpoint:
        agent.load_weights(checkpoint)

    def policy(obs, agent_id, mask=None):
        return agent.act(flatten_obs(obs), epsilon, mask)

    return policy
