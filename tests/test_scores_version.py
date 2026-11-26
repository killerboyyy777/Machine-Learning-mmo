"""Versioned scores.json and safe companion rows on load (#980).

Run from the repo root:  python tests/test_scores_version.py
"""

import copy
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402


def _write(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f)


_saved_scores = copy.deepcopy(srv.SCORES)
_saved_file = srv.SCORES_FILE
_saved_blocked = srv.SCORES_FILE_BLOCKED
try:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "scores.json")
        srv.SCORES_FILE = path
        srv.SCORES = {}

        # A future version is left on disk and blocks writes, as the other
        # versioned state files do.
        raw = {"version": srv.SCORES_VERSION + 1, "scores": {"future": {}}}
        _write(path, raw)
        srv.SCORES_FILE_BLOCKED = False
        assert srv.load_scores() == {}, "future scores were loaded"
        assert srv.SCORES_FILE_BLOCKED is True
        assert srv.save_scores() is False
        with open(path) as f:
            assert json.load(f) == raw, "future file was overwritten"
        print("SCORES_FUTURE_UNTOUCHED_OK")

        # A bad backup beside a healthy primary cannot veto saves.
        _write(path, {"version": srv.SCORES_VERSION, "scores": {"ok": {}}})
        _write(
            path + ".1",
            {"version": srv.SCORES_VERSION + 1, "scores": {"bad": {}}},
        )
        srv.SCORES_FILE_BLOCKED = False
        assert set(srv.load_scores()) == {"ok"}
        assert srv.SCORES_FILE_BLOCKED is False
        assert srv.save_scores() is True
        print("SCORES_BAD_BACKUP_SAVES_OK")

        # Non-dict entries are skipped; get_score_entry can repair the name.
        _write(
            path,
            {
                "version": srv.SCORES_VERSION,
                "scores": {"good": {"level": 2}, "scalar": 7},
            },
        )
        srv.SCORES_FILE_BLOCKED = False
        loaded = srv.load_scores()
        assert loaded == {"good": {"level": 2}}, loaded
        srv.SCORES = loaded
        fixed = srv.get_score_entry("Scalar")
        assert isinstance(fixed, dict) and fixed["level"] == 1, fixed
        print("SCORES_SCALAR_SKIPPED_OK")

        # Unversioned files remain readable as v1 (fresh "{}" seeds included).
        _write(path, {"legacy": {"level": 3}})
        assert set(srv.load_scores()) == {"legacy"}
        _write(path, {})
        assert srv.load_scores() == {}
        print("SCORES_LEGACY_OK")
finally:
    srv.SCORES_FILE = _saved_file
    srv.SCORES.clear()
    srv.SCORES.update(_saved_scores)
    srv.SCORES_FILE_BLOCKED = _saved_blocked

# --- companion rows are clamped and finite on load -------------------------
base = {"species": "wolf", "name": "Wolf", "hp": 20, "xp": 5, "acquired_at": 1.0}
rec = srv._normalize_companion_record(dict(base, level=100000))
assert rec is not None and rec["level"] == srv.COMPANION_LEVEL_CAP, rec
rec = srv._normalize_companion_record(dict(base, level=0))
assert rec is not None and rec["level"] == 1, rec
rec = srv._normalize_companion_record(dict(base, level=3))
assert rec is not None and rec["level"] == 3, rec
for bad in (
    {"xp": float("inf")},
    {"downed_until": float("nan")},
    {"acquired_at": float("inf")},
    {"hp": float("inf")},
):
    assert srv._normalize_companion_record(dict(base, **bad)) is None, bad
print("COMPANION_CLAMP_OK")

print("ALL_SCORES_VERSION_OK")
