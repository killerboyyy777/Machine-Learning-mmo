"""Shared-reward open mode and the specialisation reporter (#626).

The scripted role split the conductor runs today (linear, hunter, gather,
dungeon, market, commissioner, crafter, quester, party_leader) is a hypothesis
about what is good that has never been tested against an alternative. This
module supplies the two pieces needed to test it: a population-level shared
reward, and a reporter that reads the action ids the v0.17 transcripts already
pin (#624) to ask whether specialisation emerges without losing throughput.

Everything here is off by default. Nothing imports game state, nothing runs
during a normal gate, and the server, observation vector and default reward are
untouched. A researcher opts in by constructing a SharedReward and/or calling
the reporter on transcripts a run already wrote.

The metric is always a pair: falling action entropy is only interesting if
throughput holds. :func:`emergence_verdict` refuses to call specialisation on
entropy alone.
"""

import json
import math
import os

from ml.diversity import normalized_entropy

SCHEMA_VERSION = 1
DEFAULT_WINDOW_S = 1800
DEFAULT_WEIGHTS = {
    "score_gain": 1.0,
    "gold_delta": 1.0,
    "xp_gain": 0.0,
    "kills": 1.0,
    "quests_completed": 1.0,
}
THROUGHPUT_KEYS = ("kills", "gold_moved", "quests")


def _num(value):
    """Finite float for a telemetry field, else 0.0 (bad samples never skew)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


class SharedReward:
    """Collect one population frame and compute its common reward.

    Call begin_frame(), submit each agent's signals through observe() or an
    observer callback, then read shared_value() once collection is complete.
    The caller assigns that one value to every agent for the frame. A
    single-agent callback cannot know contributions that have not arrived yet.
    """

    def __init__(self, weights=None, per_capita=True):
        self.weights = dict(weights if weights is not None else DEFAULT_WEIGHTS)
        self.per_capita = bool(per_capita)
        self._frame = {}

    def begin_frame(self):
        """Discard the prior frame before collecting the next population step."""
        self._frame = {}

    def observe(self, agent_id, signals):
        """Record one agent's contribution for the current frame."""
        if not isinstance(signals, dict):
            return
        self._frame[agent_id] = {key: _num(signals.get(key)) for key in self.weights}

    def _totals(self):
        return {
            key: sum(row[key] for row in self._frame.values())
            for key in self.weights
        }

    def shared_value(self):
        """Return the common reward after all agents have been observed."""
        if not self._frame:
            return 0.0
        totals = self._totals()
        total = sum(weight * totals[key] for key, weight in self.weights.items())
        if self.per_capita:
            total /= len(self._frame)
        return round(total, 6)

    def observer(self, agent_id):
        """Return a signal-collection callback, not an immediate reward hook."""

        def _observe(signals):
            self.observe(agent_id, signals)

        return _observe

    def snapshot(self):
        """JSON-safe view of the currently collected population frame."""
        return {
            "weights": dict(self.weights),
            "per_capita": self.per_capita,
            "n_agents": len(self._frame),
            "totals": self._totals(),
            "shared_value": self.shared_value(),
        }


def _bin_index(elapsed_s, window_s):
    return max(0, int(_num(elapsed_s) // max(1, int(window_s))))


def specialisation_report(rows, throughput=None, window_s=DEFAULT_WINDOW_S):
    """Bin transcript rows by time and measure per-agent action entropy.

    ``rows`` are ``ml.transcript.read_transcript`` output: each row carries
    ``agent_id``, ``action`` and ``elapsed_s``. ``throughput`` is an optional
    list of ``{elapsed_s, kills, gold_moved, quests}`` telemetry samples. Each
    bin reports the mean normalized action entropy across agents (1 = every
    action used evenly, 0 = one action only) joined with summed throughput.
    """
    window_s = max(1, int(window_s))
    bins = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("action") is None:
            continue
        idx = _bin_index(row.get("elapsed_s", 0.0), window_s)
        entry = bins.setdefault(idx, {"rows": [], "agents": set()})
        entry["rows"].append(row)
        if row.get("agent_id") is not None:
            entry["agents"].add(row["agent_id"])

    samples = {}
    for sample in throughput or []:
        if not isinstance(sample, dict):
            continue
        idx = _bin_index(sample.get("elapsed_s", 0.0), window_s)
        samples.setdefault(idx, []).append(sample)

    result = []
    for idx in sorted(bins):
        entry = bins[idx]
        per_agent = {}
        for agent_id in sorted(entry["agents"]):
            agent_rows = [r for r in entry["rows"] if r.get("agent_id") == agent_id]
            per_agent[agent_id] = round(normalized_entropy(agent_rows), 6)
        values = list(per_agent.values())
        mean_entropy = round(sum(values) / len(values), 6) if values else 0.0
        totals = {key: 0.0 for key in THROUGHPUT_KEYS}
        for sample in samples.get(idx, []):
            for key in THROUGHPUT_KEYS:
                if key in sample:
                    totals[key] += _num(sample[key])
        result.append(
            {
                "bin": idx,
                "bin_start_s": idx * window_s,
                "n_steps": len(entry["rows"]),
                "n_agents": len(per_agent),
                "mean_action_entropy": mean_entropy,
                "per_agent_entropy": per_agent,
                "throughput": totals,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "window_s": window_s,
        "bins": result,
    }


def emergence_verdict(report, throughput_floor=0.8, min_bins=2):
    """Decide whether entropy fell while throughput held.

    This is the issue's "metric as a pair, never entropy alone" rule made
    explicit: a population only counts as a specialisation candidate when its
    mean action entropy drops and its throughput stays near its starting
    level. Otherwise the verdict says so rather than over-claiming.
    """
    bins = report.get("bins", [])
    if len(bins) < min_bins:
        return {"status": "insufficient_data", "bins": len(bins)}
    first, last = bins[0], bins[-1]
    start_entropy = first["mean_action_entropy"]
    end_entropy = last["mean_action_entropy"]
    throughput_by_bin = [
        sum(_num(value) for value in row.get("throughput", {}).values())
        for row in bins
    ]
    start_throughput = throughput_by_bin[0]
    end_throughput = throughput_by_bin[-1]
    entropy_fell = end_entropy < start_entropy - 0.01
    throughput_held = start_throughput > 0 and all(
        value >= start_throughput * throughput_floor
        for value in throughput_by_bin[1:]
    )
    return {
        "status": (
            "specialisation_candidate"
            if entropy_fell and throughput_held
            else "no_emergence"
        ),
        "start_entropy": start_entropy,
        "end_entropy": end_entropy,
        "start_throughput": round(start_throughput, 6),
        "end_throughput": round(end_throughput, 6),
        "entropy_fell": entropy_fell,
        "throughput_held": throughput_held,
        "throughput_floor": throughput_floor,
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
    """Persist a report next to (never on top of) the source transcript."""
    return _write_json(path, report)


def read_report(path):
    """Read a report written by write_report; {} when absent/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
