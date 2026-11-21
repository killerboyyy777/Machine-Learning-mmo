"""Minimal replayable repros from action-level transcripts (#624).

The transcript writer (ml.transcript) records one JSON line per step during a
run. A finding is only useful if someone else can replay the exact trajectory
that produced it, so this module folds those rows into a small, serializable
repro artifact: the ordered steps that mattered, with the observation, action
and reward for each.

Everything here is pure and stdlib-only. It imports no game code, writes no
orders, and never touches the world: a repro is a report about a run, not a
way to change one. A finding can therefore be shipped and re-read without the
server that produced the transcript.
"""

import hashlib
import json
import os

SCHEMA_VERSION = 1


def _finite(value):
    """Return a finite float, or None for anything else (never raises)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _changed(prev, current):
    """Keys whose value differs between two observation dicts.

    Only keys present in ``current`` are considered: an observation that
    dropped a key is not a state change, it is a schema difference.
    """
    if not isinstance(current, dict):
        return {}
    if not isinstance(prev, dict):
        prev = {}
    return {
        key: value
        for key, value in current.items()
        if key not in prev or prev[key] != value
    }


def _compact_info(info):
    """Keep the reward detail a researcher needs, discard the rest."""
    if not isinstance(info, dict):
        return {}
    keep = {}
    for key in ("reward_vector", "reward_mode", "custom_reward_errors"):
        if key in info:
            keep[key] = info[key]
    return keep


def repro_fingerprint(steps):
    """Stable short digest of the action/reward sequence, for dedup."""
    payload = [
        [step.get("step"), step.get("action"), step.get("reward")] for step in steps
    ]
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def compact_repro(
    rows,
    agent_id=None,
    episode=None,
    incarnation=None,
    max_steps=0,
    defect_class="unclassified",
):
    """Fold transcript rows into one minimal, serializable repro.

    ``rows`` are the dicts returned by :func:`ml.transcript.read_transcript`.
    The optional filters narrow to one agent, episode and/or incarnation;
    survivors are ordered by (incarnation, step) so the result is independent
    of file order. A positive ``max_steps`` keeps only the earliest steps,
    which is what makes a long episode into a minimal reproducer.

    Agents, episodes and incarnations are selected by value, never inferred:
    a caller that passes no filter gets every row, in timeline order. Rows
    missing a field that a filter asks for are skipped rather than guessed.
    """
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if agent_id is not None and row.get("agent_id") != agent_id:
            continue
        if episode is not None and row.get("episode") != episode:
            continue
        if incarnation is not None and row.get("incarnation") != incarnation:
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (
            _finite(row.get("incarnation")) or 0,
            _finite(row.get("step")) or 0,
        )
    )
    if max_steps and max_steps > 0:
        selected = selected[:max_steps]

    steps = []
    for row in selected:
        current = row.get("obs")
        prev = row.get("prev_obs")
        steps.append(
            {
                "step": row.get("step"),
                "incarnation": row.get("incarnation"),
                "elapsed_s": _finite(row.get("elapsed_s")),
                "action": row.get("action"),
                "reward": _finite(row.get("reward")),
                "done": bool(row.get("done")),
                "obs": current if isinstance(current, dict) else {},
                "changed": _changed(prev, current),
                "info": _compact_info(row.get("info")),
            }
        )

    first = selected[0] if selected else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "defect_class": defect_class,
        "agent_id": agent_id if agent_id is not None else first.get("agent_id"),
        "episode": episode if episode is not None else first.get("episode"),
        "incarnation": (
            incarnation if incarnation is not None else first.get("incarnation")
        ),
        "n_steps": len(steps),
        "fingerprint": repro_fingerprint(steps),
        "steps": steps,
    }


def write_repro(repro, path):
    """Atomically write a repro as JSON, creating the parent dir if needed.

    Returns the absolute path written. The destination is always the path the
    caller gave: nothing is derived from a source filename, so a repro can
    never overwrite the transcript or measurement it was built from.
    """
    destination = os.path.abspath(path)
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = destination + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(repro, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    os.replace(temporary, destination)
    return destination


def read_repro(path):
    """Read a repro back from disk (raises if it is missing or malformed)."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
