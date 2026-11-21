"""Durable action-level transcripts for agent episodes (#624).

runlog records run identity and scalar metric samples, but every per-step
detail - the observation vector, the action chosen, the reward, the elapsed
time - is collapsed into a single episode event and discarded. That makes a
reported exploit impossible to replay. A transcript is the missing piece:
one JSON line per step, written as it happens, so a finding can be shipped
with the exact trajectory that produced it.

Writing is opt-in and additive: nothing is recorded unless a writer is
handed to the agent, and a transcript failure never disturbs the run.
"""

import json
import os
import time


def _json_line(obj):
    return json.dumps(obj, sort_keys=True, default=str) + "\n"


class TranscriptWriter:
    """Append one JSON line per step to a transcript file.

    Never raises: a full disk or a bad value must not kill an episode that
    was otherwise healthy. The parent directory is created on first write.
    """

    def __init__(self, path, incarnation=0, started=None):
        self.path = os.path.abspath(path)
        # A PBT restart reuses the agent's file. The incarnation marks each
        # generation and the carried start keeps elapsed_s monotonic, so a
        # consumer can key on (incarnation, episode, step) and read one
        # timeline instead of two overlapping ones (#703 review).
        self.incarnation = incarnation
        self.started = time.time() if started is None else started

    def record(self, **fields):
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            row = dict(fields)
            row["incarnation"] = self.incarnation
            row["elapsed_s"] = round(time.time() - self.started, 6)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(_json_line(row))
        except Exception:
            pass


def read_transcript(path):
    """Read a transcript back into a list of step dicts (empty if absent)."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
