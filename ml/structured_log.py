"""Structured JSONL event log for conductor and soak runs (#316).

Opt-in, standard library only and file-backed: stdout belongs to the human
readable soak log (server.py redirects it), so structured lines never go
there. Action names are those already used by metrics.py, reload.py and
telemetry.py; this module adds no parallel vocabulary for the same events.
"""

import json
import os
import time

SCHEMA_VERSION = 1
MAX_ERROR_CHARS = 500


def build_row(action, ids=None, error=None, **fields):
    """Build one flat event row with a rounded clock and bounded error."""
    row = {
        "schema_version": SCHEMA_VERSION,
        "_ts": round(time.time(), 3),
        "action": str(action),
    }
    if ids is not None:
        row["ids"] = list(ids)
    if error is not None:
        row["error"] = str(error)[:MAX_ERROR_CHARS]
    row.update(fields)
    return row


def encode(row):
    """Serialize a row deterministically; unknown values become strings."""
    return json.dumps(row, sort_keys=True, default=str)


class StructuredLog:
    """Append one JSON object per call; a write failure never propagates."""

    def __init__(self, path):
        self.path = path

    def log(self, action, ids=None, error=None, **fields):
        try:
            row = build_row(action, ids=ids, error=error, **fields)
            folder = os.path.dirname(os.path.abspath(self.path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(encode(row) + "\n")
        except Exception:
            pass
