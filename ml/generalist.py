"""Same-frame generalist vs purist doctrine pilot (#419).

A generalist is a policy that earns one reward summed across several
archetypes instead of specialising in one. This module supplies the reward
shapes, the AgentPlugin classes that expose them, and a same-frame comparison
harness that measures the generalist's cumulative lead over the best purist
across a multi-hour horizon.

It is additive and off by default. Registering the plugins is an explicit call
(``register_plugins()``), so importing this module does not change the plugin
registry, any default reward, the observation or action space, or the gate.
The harness reads signal frames supplied by the caller; it has no runtime hook
and imports no game code.
"""

import json
import math
import os

SCHEMA_VERSION = 1

# Each doctrine is the reward signal set a specialist is trained on. These are
# real ml.reward_dsl signals, so a purist baseline is a genuine single-axis
# specialist rather than an invented toy reward.
ARCHETYPES = {
    "fighter": ("score_delta", "deaths"),
    "trader": ("profit",),
    "explorer": ("xp_delta", "level_delta"),
    "social": ("social", "formation"),
}
DEFAULT_WEIGHTS = {name: 1.0 for name in ARCHETYPES}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def archetype_reward(signals, archetype):
    """Reward for one doctrine: sum of that archetype's signals."""
    keys = ARCHETYPES.get(archetype)
    if keys is None:
        raise KeyError(f"unknown archetype {archetype!r}")
    return sum(_num(signals.get(key)) for key in keys)


def cross_archetype_reward(signals, weights=None):
    """Generalist reward: weighted sum of all doctrine rewards.

    Death-derived signals (the fighter's ``deaths``) are negative, so the
    generalist is penalised for dying just as a fighter would; it simply does
    not optimise any single axis to the exclusion of the others.
    """
    active = DEFAULT_WEIGHTS if weights is None else weights
    total = 0.0
    for name, weight in active.items():
        total += _num(weight) * archetype_reward(signals, name)
    return total


def _plugin_base():
    """Imported lazily so module import never touches the plugin registry."""
    from ml.plugins import AgentPlugin

    return AgentPlugin


class GeneralistPlugin(_plugin_base()):
    """Policy whose reward spans every archetype."""

    name = "generalist"
    agent_type = "custom"

    @classmethod
    def config_schema(cls):
        return {"scale": {"type": float, "default": 1.0, "help": "reward scale"}}

    def reward(self, signals):
        return self.scale * cross_archetype_reward(signals)


class PuristPlugin(_plugin_base()):
    """Specialist baseline trained on a single doctrine."""

    name = "purist"
    agent_type = "custom"

    @classmethod
    def config_schema(cls):
        return {
            "archetype": {
                "type": str,
                "default": "fighter",
                "help": f"one of {', '.join(sorted(ARCHETYPES))}",
            },
            "scale": {"type": float, "default": 1.0, "help": "reward scale"},
        }

    def reward(self, signals):
        return self.scale * archetype_reward(signals, self.archetype)


def register_plugins():
    """Explicitly add the pilot plugins to the registry; returns their names.

    Callers that want ``--slot generalist`` or ``--slot purist:archetype=trader``
    opt in by calling this once at setup. Importing the module alone registers
    nothing.
    """
    from ml.plugins import REGISTRY, register

    names = []
    for plugin in (GeneralistPlugin, PuristPlugin):
        if plugin.name not in REGISTRY:
            register(plugin)
        names.append(plugin.name)
    return names


def same_frame_curve(frames, weights=None, bin_steps=3600):
    """Cumulative reward curves for equal frames, binned over a horizon.

    ``frames`` is a sequence of per-step signal dicts. Every policy sees the
    same frames, which is what makes the comparison same-frame. Returns a
    dict of doctrine name -> list of cumulative reward per bin.
    """
    width = max(1, int(bin_steps))
    curves = {name: [] for name in ARCHETYPES}
    curves["generalist"] = []
    running = {name: 0.0 for name in curves}
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        running["generalist"] += cross_archetype_reward(frame, weights)
        for name in ARCHETYPES:
            running[name] += archetype_reward(frame, name)
        if (index + 1) % width == 0:
            for name in curves:
                curves[name].append(round(running[name], 6))
    if frames and len(frames) % width:
        for name in curves:
            curves[name].append(round(running[name], 6))
    return curves


def pilot_report(curves, min_bins=2, lead_tolerance=0.0):
    """Head-to-head result: generalist cumulative lead over the best purist.

    Verdict is ``promising`` only when the generalist leads at the final bin
    and its lead is non-decreasing over at least ``min_bins`` bins. Anything
    else is ``inconclusive`` -- the number is informational, never a gate.
    """
    gen = curves.get("generalist", [])
    purists = [values for name, values in curves.items() if name in ARCHETYPES]
    if not gen or not purists:
        return {"verdict": "insufficient_data", "final_lead": 0.0, "bins": 0}
    length = min([len(gen)] + [len(values) for values in purists])
    if length < max(1, min_bins):
        return {"verdict": "insufficient_data", "final_lead": 0.0, "bins": length}
    leads = []
    for i in range(length):
        best_purist = max(values[i] for values in purists)
        leads.append(round(gen[i] - best_purist, 6))
    final_lead = leads[-1]
    nondecreasing = all(
        leads[i] + lead_tolerance >= leads[i - 1] for i in range(1, length)
    )
    verdict = (
        "promising"
        if final_lead > 0 and nondecreasing and length >= min_bins
        else "inconclusive"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "final_lead": final_lead,
        "bins": length,
        "lead_curve": leads,
    }


def _write_json(path, obj):
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = os.path.abspath(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, sort_keys=True, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return os.path.abspath(path)


def write_report(report, path):
    """Persist a pilot report next to (never on top of) the source frames."""
    return _write_json(path, report)


def read_report(path):
    """Read a report written by write_report; {} when absent or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
