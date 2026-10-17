"""Engine/mode seam (#474).

The engine (server.py) owns transport, the world sim, and persistence.
A mode owns one ruleset layered on top: lifecycle hooks, spawn placement,
and its own scoring. Modes never mutate engine state directly -- each
mode gets an isolated dict inside ModeContext.mode_state, keyed by mode
name, and a read-only view of the world. Per-mode scores live in that
isolated dict, never in the engine's score entries, so experimental
rulesets cannot pollute the main leaderboard.

Wiring into the tick loop / command handlers is a follow-up; this
module defines the contract and the registry so modes load today and
the engine can call them tomorrow without renegotiating the API.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _freeze(value):
    """Recursive read-only view of engine-owned JSON-ish data."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class ModeContext:
    """What a mode may see and touch during a hook call.

    world is read-only (room ids, npc rosters supplied by the engine);
    mode_state is this mode's private ledger, persisted by the engine
    under the mode's name but never interpreted by it.
    """

    world: Mapping = field(default_factory=dict)
    mode_state: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    def __post_init__(self):
        # Enforced, not conventional: frozen rejects rebinding the
        # attribute and the recursive freeze rejects writes at every
        # depth, because a mode appending to ctx.world["npcs"][room] is
        # an engine-state write wearing a proxy. Detached from the
        # engine's own structures so a later tick cannot move what the
        # mode already saw. Read-only is shallow-by-need, not
        # shallow-by-design.
        object.__setattr__(self, "world", _freeze(self.world))


class BaseMode:
    """One ruleset. Subclass + @register to add a new one.

    Every hook defaults to passthrough: an unimplemented mode changes
    nothing, so the engine can call hooks unconditionally and today's
    behavior is the default mode with zero overrides.
    """

    name = "base"
    version = 1

    @classmethod
    def config_schema(cls):
        """{param: {"type": callable, "default": value, "help": str}}."""
        return {}

    def __init__(self, **config):
        schema = self.config_schema()
        unknown = set(config) - set(schema)
        if unknown:
            raise ValueError(
                f"unknown config for mode {self.name!r}: {sorted(unknown)} "
                f"(schema: {sorted(schema)})"
            )
        for key, spec in schema.items():
            default = spec.get("default")
            value = config.get(key, default)
            coerce = spec.get("type")
            # None skips only when the schema opts into it (default is
            # None). Otherwise it is just another wrong type: passing
            # kill_points=None must fail here, not as a TypeError inside
            # score_kill three events later.
            if (
                coerce is not None
                and not isinstance(value, coerce)
                and not (value is None and default is None)
            ):
                try:
                    value = coerce(value)
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"bad config {key}={value!r} for mode {self.name!r}: {e}"
                    )
            setattr(self, key, value)

    def initial_state(self):
        """Fresh isolated ledger for one character (or party)."""
        return {}

    def spawn_room(self, ctx):
        """Room id for login/respawn placement, or None for engine default."""

    def on_login(self, ctx, character):
        """After login; return spawn-room override or None."""
        return self.spawn_room(ctx)

    def on_respawn(self, ctx, character):
        """After death; return spawn-room override or None."""
        return self.spawn_room(ctx)

    def on_tick(self, ctx, character):
        """Per engine tick; return None. Used for timed mode rules."""

    def score_kill(self, ctx, killer, victim, base_points, base_xp):
        """Per-mode award for a kill. Returns (points, xp) for the LEDGER."""
        return 0.0, 0.0

    def score_death(self, ctx, character, base_penalty):
        """Per-mode death cost. Returns the ledger penalty."""
        return 0.0

    def on_kill(self, ctx, killer, victim):
        """Side effects of a kill beyond scoring (announcements).

        Never touches the ledger: the engine calls score_kill once per
        kill first, then this hook, so scoring here would credit twice.
        """

    def on_death(self, ctx, character):
        """Side effects of a death beyond scoring (streak resets elsewhere).

        Never touches the ledger: the engine calls score_death once per
        death first, then this hook, so scoring here would count twice.
        """


REGISTRY = {}


def register(mode_cls):
    """Class decorator enrolling a mode under its ``name``."""
    if not mode_cls.name or mode_cls.name == "base":
        raise ValueError(f"mode {mode_cls} needs a unique name")
    if mode_cls.name in REGISTRY:
        raise ValueError(f"duplicate mode name: {mode_cls.name!r}")
    REGISTRY[mode_cls.name] = mode_cls
    return mode_cls


def _import_builtins():
    from ml.modes import arena as _arena

    _ = _arena


def available():
    """Sorted mode names (imports built-ins)."""
    if "arena" not in REGISTRY:
        _import_builtins()
    return sorted(REGISTRY)


def get(name):
    """Fetch a registered mode class by name (discovers built-ins)."""
    if name not in REGISTRY:
        _import_builtins()
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown mode {name!r} (available: {sorted(REGISTRY)})")


def instantiate(name, **config):
    """Build a mode instance from name + config kwargs."""
    return get(name)(**config)


def isolate(store, name):
    """Return (creating) the isolated ledger for ``name`` in ``store``.

    The engine keeps one ``store`` dict (per character or party);
    each mode sees only ``store[name]``. Modes must not reach into
    sibling keys -- that is the whole separation guarantee.
    """
    if name in store:
        ledger = store[name]
        if not isinstance(ledger, dict):
            raise ValueError(
                f"corrupt ledger for mode {name!r}: expected dict, got "
                f"{type(ledger).__name__}; refusing to wipe a legacy save"
            )
        return ledger
    ledger = {}
    store[name] = ledger
    return ledger
