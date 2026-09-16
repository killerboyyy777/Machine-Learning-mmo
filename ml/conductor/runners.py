"""Conductor agent runners: concrete env_factory + policy_fn (#52).

Thin compatibility layer over ml.plugins: prefer
``ml.plugins.instantiate`` + ``make_env_factory`` for new code.

    from ml.conductor.runners import make_env_factory, make_linear_policy

    sup = Supervisor(registry, mixer=mixer)
    await sup.start_agent("a0", make_env_factory(url, reward_mode="econ"),
                          make_linear_policy("ml/ml_best.json"))

Policies take the env's structured obs dict (what reset()/step() return)
and an optional valid-action mask -- the supervisor passes the mask when
the policy accepts a third argument (and the env itself for a fourth).
"""

try:
    from ..ml_env import TextMMOEnv
    from ..plugins import instantiate
except ImportError:
    from ml_env import TextMMOEnv
    from plugins import instantiate
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
    plugin = instantiate("linear", checkpoint=checkpoint, epsilon=epsilon)
    return plugin.make_policy()


def make_torch_policy(checkpoint=None, epsilon=0.0, **agent_kwargs):
    """Return policy_fn running a DQN checkpoint (epsilon-greedy).

    Torch is imported lazily so the conductor package stays importable
    without it."""
    if agent_kwargs:
        raise ValueError(
            "make_torch_policy no longer takes agent kwargs; use "
            "ml.plugins.instantiate('torch', ...) directly")
    plugin = instantiate("torch", checkpoint=checkpoint, epsilon=epsilon)
    return plugin.make_policy()
