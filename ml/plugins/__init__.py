"""Agent plugins: every agent ships as a plugin; built-ins live here.

A plugin is a policy plus metadata. Learned policies implement
``act(obs, agent_id, mask)``; scripted policies implement
``select(env)`` instead. :meth:`AgentPlugin.make_policy` builds the
supervisor closure for either style (3-arg vs 4-arg -- the supervisor
probes arity and passes what the policy accepts).

Built-ins (all usable from ``--slot`` today, no extra installs):

- ``linear`` -- linear-Q checkpoint, epsilon-greedy
- ``torch`` -- DQN checkpoint, epsilon-greedy (needs torch)
- ``gather`` / ``dungeon`` / ``market`` / ``commissioner``
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
import string
import subprocess
import sys

REGISTRY = {}  # plugin name -> AgentPlugin subclass

_BUILTINS = ("linear", "torch_plugin", "scripted")

__all__ = [
    "KNOWN_AGENT_TYPES",
    "REGISTRY",
    "AgentPlugin",
    "discover",
    "get",
    "instantiate",
    "load_plugins",
    "manifest_problems",
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

    def reward(self, signals):
        """Optional per-step reward hook (#68). Called from inside
        ``env.step()`` as ``hook(signals)`` with the per-step signal dict
        (the same names a ``reward_formula`` may use -- see
        ``ml.reward_dsl.reference()``); returns a float reward, or None
        to abstain.

        This is the researcher-injected-Python half of #68: anything the
        expression language cannot express belongs here. Exceptions never
        propagate and a non-finite result is refused -- the step keeps the
        reward it would otherwise have given and the env counts the
        failure (see ``info["custom_reward_errors"]``), so a bad hook
        degrades a reward curve instead of ending an overnight run.

        Base default: abstain, leaving the formula/mode reward in place.
        """
        return

    goal_dim = None  # goal-vector width this policy consumes (None =
    # ignores-goals; slice 6/6 wires goal-conditioned policies in).

    def learn(self, prev_obs, action, reward, next_obs, done):
        """Per-step learning hook (#292, slice 5/6). Called by the
        supervisor after every env step as
        ``hook(prev_obs, action, reward, next_obs, done)``; returns a
        loss-ish float or None. Base default: no learning (scripted
        policies). Exceptions never propagate (supervisor guards)."""
        return None  # noqa: RET501 - documented as "returns None or a float"

    def save(self, path):
        """Persist learned weights. Base default: nothing to save."""
        return False

    def load(self, path):
        """Reload learned weights. Base default: nothing to load."""
        return False

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
            if mod != "torch_plugin":
                raise
            # optional dependency (e.g. torch) missing


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


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Long enough for a heavy import (a plugin may pull numpy or torch), short
# enough that a wedged one is named instead of hanging the run before its
# first agent starts.
IMPORT_PROBE_TIMEOUT_S = 10.0

# Runs in a child interpreter: import one file and exit. Nothing from the
# plugin's module survives into the conductor, which is the point -- a module
# that loops at import time never gets the chance to loop here.
#
# It mirrors the real loader on both points the review caught: the plugin
# directory goes on sys.path (a drop-in may import a sibling helper), and the
# module is registered under its own name before it executes (dataclasses and
# anything else that looks itself up in sys.modules needs it there).
_PROBE_SOURCE = """
import importlib.util
import sys

root, plugin_dir, path, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, root)
sys.path.insert(0, plugin_dir)
spec = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
print("PROBE_OK")
"""


def probe_import(path, timeout=None, plugin_dir=None):
    """Import one plugin file in a child process; None when it imports.

    Returns the reason string when it does not: a crash (an exception on
    import) or a hang (the timeout). The file is imported here only, so a
    plugin that is going to take the run down does it to the probe instead."""
    budget = IMPORT_PROBE_TIMEOUT_S if timeout is None else timeout
    path = os.path.abspath(path)
    if plugin_dir is None:
        plugin_dir = os.path.dirname(path)
    # Same module name the real loader uses, so a file that introspects itself
    # is probed under the name it will actually have.
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SOURCE, _ROOT, plugin_dir, path, name],
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"did not import within {budget:g}s"
    if proc.returncode == 0:
        return None
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"failed to import: {tail[-1] if tail else 'exit ' + str(proc.returncode)}"


# Families a drop-in may declare. Per-type status and PBT read this label, so
# a plugin that invents one lands in a bucket nothing else shares; the manifest
# names the accepted set instead of accepting any string (#176).
KNOWN_AGENT_TYPES = ("linear", "scripted", "torch", "custom")

# Explicit ASCII: str.islower()/str.isdigit() accept 'e-acute' and Arabic-Indic
# digits, which would put a name in the registry that no shell or log can spell
# back (#176 review).
_NAME_CHARS = frozenset(string.ascii_lowercase + string.digits + "-_")


def _name_problem(plugin_cls):
    name = getattr(plugin_cls, "name", "")
    if not isinstance(name, str) or not name:
        return "has no name"
    if name == "base":
        return "uses the reserved name 'base'"
    if not all(c in _NAME_CHARS for c in name):
        return f"name {name!r} must be ASCII lowercase letters, digits, '-' or '_'"
    return None


def manifest_problems(plugin_cls):
    """Reasons a plugin cannot be admitted; an empty list means it can."""
    problems = []
    bad = _name_problem(plugin_cls)
    if bad:
        problems.append(bad)
    kind = getattr(plugin_cls, "agent_type", "")
    if kind not in KNOWN_AGENT_TYPES:
        problems.append(
            f"agent_type {kind!r} is not one of {', '.join(KNOWN_AGENT_TYPES)}"
        )
    schema = None
    try:
        schema = plugin_cls.config_schema()
    except Exception as exc:  # a raising schema is a manifest error, not a crash
        problems.append(f"config_schema() raised {type(exc).__name__}: {exc}")
    if schema is not None:
        if not isinstance(schema, dict):
            problems.append("config_schema() must return a dict")
        else:
            for key, spec in schema.items():
                if not isinstance(spec, dict) or "type" not in spec:
                    problems.append(f"config_schema() entry {key!r} needs a 'type'")
                elif not callable(spec["type"]):
                    # __init__ coerces with isinstance(), which raises TypeError
                    # for a string type -- only on the first config that sets the
                    # value, so an omitted default hides it until an agent starts.
                    problems.append(
                        f"config_schema() entry {key!r} needs a callable 'type' "
                        f"(got {spec['type']!r})"
                    )
    if not problems:
        try:
            plugin_cls()
        except Exception as exc:  # a plugin whose own defaults fail cannot run
            problems.append(f"refuses its own default config: {exc}")
    return problems


def load_plugins(plugin_dir=None):
    """Discover ``plugin_dir`` and admit only plugins with a sound manifest.

    Every file is imported once in a child process first (:func:`probe_import`),
    so a plugin that crashes or hangs at import is rejected with its reason
    instead of taking the run down before the first agent starts. Survivors are
    imported here; an import error that survived the probe still propagates,
    because that is a different file state, not a reason to run blind.

    A file that imports but fails its manifest is dropped from the registry and
    returned in ``rejected`` as ``(name, reason)``, so one bad drop-in does not
    take the others with it.

    A missing directory is not an error: container images copy ``ml/`` and not
    the top-level ``plugins/``, so the default path must stay harmless there.
    """
    # Built-ins first, so ``admitted`` names the drop-ins and a missing folder
    # still leaves the standard slots selectable.
    discover()
    before = set(REGISTRY)
    if plugin_dir and not os.path.isdir(plugin_dir):
        return [], []
    rejected = []
    # Probe in a child process first: importing here would be the very hang
    # the probe exists to catch. Survivors are imported for real below, so a
    # file that imports once but registers nothing is harmless.
    probe_dir = os.path.abspath(plugin_dir) if plugin_dir else None
    if probe_dir:
        sys.path.insert(0, probe_dir)
        for mod in sorted(pkgutil.iter_modules([probe_dir]), key=lambda m: m.name):
            why = probe_import(
                os.path.join(probe_dir, f"{mod.name}.py"), plugin_dir=probe_dir
            )
            if why:
                rejected.append((mod.name, why))
                continue
            importlib.import_module(mod.name)
    admitted = []
    for name in [n for n in REGISTRY if n not in before]:
        problems = manifest_problems(REGISTRY[name])
        if problems:
            rejected.append((name, "; ".join(problems)))
            REGISTRY.pop(name, None)
        else:
            admitted.append(name)
    return admitted, rejected


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


# Slot keys (before the env_ prefix is stripped) whose value is text no
# matter how numeric it looks. Every other key is coerced, so plugin config
# like ``learning_rate=0.001`` still arrives as a float.
_TEXT_VALUES = frozenset(
    (
        "env_url",
        "env_spawn_room",
        "env_reward_mode",
        "env_reward_formula",
    )
)


def _coerce_value(text, key=None):
    text = text.strip()
    if key in _TEXT_VALUES:
        # Env keys whose value is text whatever it looks like. Coercing
        # ``env_reward_formula=42`` to int 42 handed compile_formula a
        # non-string, which raised inside the env factory and killed the
        # agent at startup (#68 review). Degenerate formulas are still the
        # user's own typo to see, not a reason to lose the slot.
        return text
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


def _split_params(text):
    """Split ``a=1,b=2`` on top-level commas only.

    Reward formulas (#68) legitimately contain commas -- ``min(a, b)`` --
    and a blind split would shred them into junk slots, so track
    parenthesis depth. A leading '(' as the whole value is left intact by
    the caller's partition, and unbalanced parens are passed through
    un-split rather than rejected here: the formula parser reports that
    far better than this splitter can (#68).
    """
    out, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def parse_slot(spec):
    """Parse a ``--slot`` spec into a slot dict.

    ``"gather"`` -> ``{"plugin": "gather", "config": {}, "env": {},
    "weight": 1}``; ``"torch:checkpoint=X,epsilon=0.1"`` fills config;
    ``env_*`` keys (``env_reward_mode``, ``env_max_steps``,
    ``env_curriculum_stage``, ``env_step_delay``, ``env_reward_formula``)
    route to the env factory instead of the policy. Commas inside
    parentheses do not split params, so an ``env_reward_formula`` may use
    ``clamp(0, 1)``. Raises ValueError on bad syntax.
    """
    if ":" in spec:
        name, _, rest = spec.partition(":")
    else:
        name, rest = spec, ""
    name = name.strip()
    if not name:
        raise ValueError(f"empty plugin name in slot {spec!r}")
    config, env = {}, {}
    weight = 1
    for chunk in _split_params(rest):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"bad slot param {chunk!r} in {spec!r} " "(want key=value)"
            )
        key, _, value = chunk.partition("=")
        key, value = key.strip(), _coerce_value(value, key)
        if not key:
            raise ValueError(f"empty key in slot {spec!r}")
        if key == "weight":
            try:
                weight = int(value)
            except (TypeError, ValueError):
                weight = 0
            if weight < 1:
                raise ValueError(
                    f"bad slot weight {value!r} in {spec!r} (want a positive int)"
                )
        elif key.startswith("env_"):
            env[key[len("env_") :]] = value
        else:
            config[key] = value
    return {"plugin": name, "config": config, "env": env, "weight": weight}
