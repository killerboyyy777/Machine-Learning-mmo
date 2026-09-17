"""Agent plugins: every agent ships as a plugin; built-ins live here.

A plugin is a policy plus metadata. Learned policies implement
``act(obs, agent_id, mask)``; scripted policies implement
``select(env)`` instead. :meth:`AgentPlugin.make_policy` builds the
supervisor closure for either style (3-arg vs 4-arg -- the supervisor
probes arity and passes what the policy accepts).

Built-ins (all usable from ``--slot`` today, no extra installs):

- ``linear`` -- linear-Q checkpoint, epsilon-greedy
- ``torch`` -- DQN checkpoint, epsilon-greedy (needs torch)
- ``gather`` / ``dungeon`` / ``market`` / ``maker`` / ``commissioner``
  -- scripted behavior-tree baselines (no learning)

External plugins live in ``--plugin-dir``: any ``*.py`` file defining
``AgentPlugin`` subclasses decorated with :func:`register` is picked up
by :func:`discover`::

    from ml.plugins import AgentPlugin, register

    @register
    class MyBot(AgentPlugin):
        name = "mybot"
        agent_type = "custom"

        @classmethod
        def config_schema(cls):
            return {"epsilon": {"type": float, "default": 0.1,
                                "help": "exploration rate"}}

        def act(self, obs, agent_id, mask=None):
            ...
"""

import importlib
import os
import pkgutil
import sys

REGISTRY = {}  # plugin name -> AgentPlugin subclass

_BUILTINS = ("linear", "torch_plugin", "scripted")

__all__ = [
    "REGISTRY",
    "AgentPlugin",
    "discover",
    "get",
    "instantiate",
    "parse_slot",
    "register",
]


class AgentPlugin:
    """One agent kind. Subclass + :func:`register` to add a new one."""

    name = "base"
    agent_type = "base"  # registry family (drives per-type status/PBT)

    @classmethod
    def config_schema(cls):
        """{param: {"type": callable, "default": value, "help": str}}."""
        return {}

    def __init__(self, **config):
        schema = self.config_schema()
        unknown = set(config) - set(schema)
        if unknown:
            raise ValueError(
                f"unknown config for plugin {self.name!r}: {sorted(unknown)} "
                f"(schema: {sorted(schema)})"
            )
        for key, spec in schema.items():
            value = config.get(key, spec.get("default"))
            coerce = spec.get("type")
            if (
                value is not None
                and coerce is not None
                and not isinstance(value, coerce)
            ):
                try:
                    value = coerce(value)
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"bad config {key}={value!r} for plugin " f"{self.name!r}: {e}"
                    )
            setattr(self, key, value)

    def on_episode_end(self, info):
        """Optional hook after each episode (logging, schedules)."""

    def act(self, obs, agent_id, mask=None):
        """Learned-style policy: return an action index."""
        raise NotImplementedError

    def select(self, env):
        """Scripted-style policy: return an action index from the env."""
        raise NotImplementedError

    def make_policy(self):
        """Build the supervisor closure for whichever style is defined."""
        if type(self).select is not AgentPlugin.select:
            return lambda obs, aid, mask, env: self.select(env)
        return lambda obs, aid, mask=None: self.act(obs, aid, mask)


def register(plugin_cls):
    """Class decorator enrolling a plugin under its ``name``."""
    if not plugin_cls.name or plugin_cls.name == "base":
        raise ValueError(f"plugin {plugin_cls} needs a unique name")
    if plugin_cls.name in REGISTRY:
        raise ValueError(f"duplicate plugin name: {plugin_cls.name!r}")
    REGISTRY[plugin_cls.name] = plugin_cls
    return plugin_cls


def _import_builtins():
    for mod in _BUILTINS:
        try:
            importlib.import_module(f"{__name__}.{mod}")
        except ImportError:
            pass  # optional dependency (e.g. torch) missing


def discover(plugin_dir=None):
    """Import built-ins plus every ``*.py`` in ``plugin_dir``.

    Returns the registry dict. Import errors in external files propagate
    (a broken plugin should fail loudly, not vanish silently)."""
    _import_builtins()
    if plugin_dir:
        sys.path.insert(0, os.path.abspath(plugin_dir))
        for mod in pkgutil.iter_modules([os.path.abspath(plugin_dir)]):
            importlib.import_module(mod.name)
    return REGISTRY


def get(name):
    """Fetch a registered plugin class by name (discovers built-ins)."""
    if name not in REGISTRY:
        discover()
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown agent plugin {name!r} " f"(available: {sorted(REGISTRY)})"
        )


def instantiate(name, **config):
    """Build a plugin instance from name + config kwargs."""
    return get(name)(**config)


def _coerce_value(text):
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


def parse_slot(spec):
    """Parse a ``--slot`` spec into a slot dict.

    ``"gather"`` -> ``{"plugin": "gather", "config": {}, "env": {},
    "weight": 1}``; ``"torch:checkpoint=X,epsilon=0.1"`` fills config;
    ``env_*`` keys (``env_reward_mode``, ``env_max_steps``,
    ``env_curriculum_stage``, ``env_step_delay``) route to the env
    factory instead of the policy. Raises ValueError on bad syntax.
    """
    if ":" in spec:
        name, _, rest = spec.partition(":")
    else:
        name, rest = spec, ""
    name = name.strip()
    if not name:
        raise ValueError(f"empty plugin name in slot {spec!r}")
    config, env = {}, {}
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"bad slot param {chunk!r} in {spec!r} " "(want key=value)"
            )
        key, _, value = chunk.partition("=")
        key, value = key.strip(), _coerce_value(value)
        if not key:
            raise ValueError(f"empty key in slot {spec!r}")
        if key.startswith("env_"):
            env[key[len("env_") :]] = value
        else:
            config[key] = value
    return {"plugin": name, "config": config, "env": env, "weight": 1}
