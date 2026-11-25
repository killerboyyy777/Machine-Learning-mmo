"""Offline behavioural-diversity and mode-collapse summaries (#313).

runlog records run identity and scalar samples; the per-step transcript from
#624 records the action id of every step. This module turns those transcripts
into diversity measurements without touching the live runtime: it reads rows
that were already written, so it is inert during a run and cannot change the
gate. Nothing in the conductor, supervisor or server imports this module.

The measurements are informational. "Mode collapse" is a signal for a
researcher to investigate, not a failure condition and not a gate threshold.
"""

import json
import math
import os

SCHEMA_VERSION = 1
DEFAULT_COLLAPSE_TOP_SHARE = 0.8


def _action_key(row):
    """Read the action id from a transcript row, tolerating old rows."""
    value = row.get("action")
    if value is None:
        return None
    return value


def action_histogram(rows, agent_id=None, episode=None, incarnation=None):
    """Count action ids across matching transcript rows.

    The optional filters let one multi-agent transcript be summarised per
    agent, per episode or per PBT incarnation without re-reading the file.
    """
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if agent_id is not None and row.get("agent_id") != agent_id:
            continue
        if episode is not None and row.get("episode") != episode:
            continue
        if incarnation is not None and row.get("incarnation") != incarnation:
            continue
        action = _action_key(row)
        if action is None:
            continue
        counts[action] = counts.get(action, 0) + 1
    return counts


def top_action_share(rows, **filters):
    """Fraction of steps taken with the single most common action."""
    counts = action_histogram(rows, **filters)
    total = sum(counts.values())
    if not total:
        return 0.0
    return max(counts.values()) / total


def reuse_ratio(rows, **filters):
    """Fraction of consecutive steps that repeat the previous action.

    High reuse with a short action list is the simplest collapse signal; it
    is order-dependent, so rows must be in step order (read_transcript keeps
    file order).
    """
    previous = None
    repeats = 0
    compared = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            filters.get("agent_id") is not None
            and row.get("agent_id") != filters["agent_id"]
        ):
            continue
        if (
            filters.get("episode") is not None
            and row.get("episode") != filters["episode"]
        ):
            continue
        if (
            filters.get("incarnation") is not None
            and row.get("incarnation") != filters["incarnation"]
        ):
            continue
        action = _action_key(row)
        if action is None:
            continue
        if previous is not None:
            compared += 1
            if action == previous:
                repeats += 1
        previous = action
    return repeats / compared if compared else 0.0


def shannon_entropy(rows, **filters):
    """Shannon entropy (bits) of the action distribution."""
    counts = action_histogram(rows, **filters)
    total = sum(counts.values())
    if not total:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def normalized_entropy(rows, **filters):
    """Entropy scaled to [0, 1] by the maximum for this action count."""
    counts = action_histogram(rows, **filters)
    distinct = len(counts)
    if distinct <= 1:
        return 0.0
    return shannon_entropy(rows, **filters) / math.log2(distinct)


def summarize(rows, collapse_top_share=DEFAULT_COLLAPSE_TOP_SHARE, **filters):
    """Summarise action diversity for one slice of a transcript.

    `mode_collapse` is true when one action accounts for at least
    `collapse_top_share` of the steps. It is a flag for investigation, not a
    pass/fail gate. Returns a JSON-safe dict.
    """
    counts = action_histogram(rows, **filters)
    total = sum(counts.values())
    histogram = [
        {"action": action, "count": count}
        for action, count in sorted(counts.items(), key=lambda pair: str(pair[0]))
    ]
    share = top_action_share(rows, **filters)
    entropy = shannon_entropy(rows, **filters)
    return {
        "schema_version": SCHEMA_VERSION,
        "filters": {key: value for key, value in filters.items() if value is not None},
        "n_steps": total,
        "n_actions": len(counts),
        "histogram": histogram,
        "top_action_share": round(share, 6),
        "reuse_ratio": round(reuse_ratio(rows, **filters), 6),
        "entropy": round(entropy, 6),
        "entropy_norm": round(normalized_entropy(rows, **filters), 6),
        "collapse_top_share": collapse_top_share,
        "mode_collapse": bool(total) and share >= collapse_top_share,
    }


def diversity_timeline(rows, window=100, **filters):
    """Summarise consecutive windows of steps to show collapse over time."""
    collapse_top_share = filters.pop("collapse_top_share", DEFAULT_COLLAPSE_TOP_SHARE)
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            filters.get("agent_id") is not None
            and row.get("agent_id") != filters["agent_id"]
        ):
            continue
        if (
            filters.get("episode") is not None
            and row.get("episode") != filters["episode"]
        ):
            continue
        if (
            filters.get("incarnation") is not None
            and row.get("incarnation") != filters["incarnation"]
        ):
            continue
        if _action_key(row) is not None:
            selected.append(row)
    width = max(1, int(window))
    result = []
    for start in range(0, len(selected), width):
        result.append(
            summarize(
                selected[start : start + width],
                collapse_top_share=collapse_top_share,
                **filters,
            )
        )
    return result


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


def write_summary(summary, path):
    """Persist a summary next to (never on top of) the source transcript."""
    return _write_json(path, summary)


def read_summary(path):
    """Read a summary written by write_summary; {} when absent/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
