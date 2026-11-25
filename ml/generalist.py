"""Same-frame generalist vs purist doctrine pilot (#419).

A generalist is a policy whose TRAINING reward is one signal summed across
several archetypes instead of specialising in one. This module supplies those
reward shapes, the AgentPlugin classes that expose them, and an informational
comparison harness.

Training reward is not an evaluation score. ``cross_archetype_reward`` sums
four archetype rewards, so feeding that sum to a head-to-head comparison makes
the generalist look ahead whenever several axes are positive, even though no
generalist policy acted or learned. ``same_frame_curve`` records those summed
training-reward curves from identical signal frames; it is not a measurement
of generalist performance.

A real comparison needs SEPARATE per-policy trajectories scored on one COMMON
objective with matched seeds and frames. ``pilot_report`` takes per-policy
cumulative outcome curves: pass ``objective`` to score them on that common
objective, where a positive lead is a genuine outcome difference. Without
``objective`` the curves are treated as training rewards and the generalist is
reduced to a weighted average across its active archetypes, so summing more
axes cannot manufacture a lead and ``promising`` is reserved for the explicit
objective path. Curve lengths must match; unequal lengths are rejected as
``misaligned_data`` rather than compared over a common prefix.

The module is additive and off by default. Registering the plugins is an
explicit call (``register_plugins()``), so importing this module does not
change the plugin registry, any default reward, the observation or action
space, or the gate. The harness reads curves supplied by the caller; it has no
runtime hook, no gate hook, and imports no game code. Its output is
informational only and never gates a run.
"""

import json
import math
import os

SCHEMA_VERSION = 2

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
    """Cumulative TRAINING reward curves for equal frames, binned by horizon.

    ``frames`` is a sequence of per-step signal dicts. Every policy sees the
    same frames, which is what makes the curves same-frame. The generalist
    entry is the summed reward across archetypes; each purist entry is one
    archetype. These are training-reward shapes, not evaluation outcomes, and
    must not be compared head-to-head as-is (see ``pilot_report``).
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


def pilot_report(curves, min_bins=2, lead_tolerance=0.0, objective=None, weights=None):
    """Informational head-to-head report from per-policy outcome curves.

    ``curves`` maps a policy name to a cumulative outcome curve. The
    ``generalist`` entry is compared with the best archetype entry. All curves
    must have the same length; unequal lengths return ``misaligned_data``
    instead of being truncated to a common prefix.

    With ``objective`` set, values are outcomes on one common objective from
    separate per-policy trajectories on matched seeds and frames, and the lead
    is compared directly. Without it, values are training rewards from
    ``same_frame_curve``; the generalist is divided by the sum of its weights
    to a weighted average across active archetypes, so a larger summed reward
    cannot create a lead and ``promising`` is reserved for the objective path.
    Verdicts are informational and never gate a run.
    """
    gen = curves.get("generalist", [])
    purists = {name: values for name, values in curves.items() if name in ARCHETYPES}
    base = {"schema_version": SCHEMA_VERSION, "objective": objective}
    if not gen or not purists:
        return {
            **base,
            "verdict": "insufficient_data",
            "final_lead": 0.0,
            "bins": 0,
            "lengths": {},
        }
    lengths = {"generalist": len(gen)}
    lengths.update({name: len(values) for name, values in purists.items()})
    if len(set(lengths.values())) != 1:
        return {
            **base,
            "verdict": "misaligned_data",
            "final_lead": 0.0,
            "bins": 0,
            "lengths": lengths,
        }
    length = len(gen)
    if length < max(1, min_bins):
        return {
            **base,
            "verdict": "insufficient_data",
            "final_lead": 0.0,
            "bins": length,
            "lengths": lengths,
        }
    if objective is None:
        active = DEFAULT_WEIGHTS if weights is None else weights
        divisor = sum(_num(weight) for weight in active.values())
        if divisor <= 0:
            divisor = 1.0
        gen_values = [value / divisor for value in gen]
    else:
        gen_values = list(gen)
    leads = []
    for i in range(length):
        best_purist = max(values[i] for values in purists.values())
        leads.append(round(gen_values[i] - best_purist, 6))
    final_lead = leads[-1]
    nondecreasing = all(
        leads[i] + lead_tolerance >= leads[i - 1] for i in range(1, length)
    )
    verdict = (
        "promising"
        if (
            objective is not None
            and final_lead > 0
            and nondecreasing
            and length >= min_bins
        )
        else "inconclusive"
    )
    return {
        **base,
        "verdict": verdict,
        "final_lead": final_lead,
        "bins": length,
        "lead_curve": leads,
        "lengths": lengths,
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
