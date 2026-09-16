"""Scores backup + server flags unit tests (no live server needed).

Run from the repo root:  python tests/test_scores_backup.py
Covers #166 (rotated backups, corrupt-primary recovery) and #173
(--host/--port/--http-port/--gm-port parsing).
"""

import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


def _read(path):
    with open(path) as f:
        return json.load(f)


with tempfile.TemporaryDirectory() as tmp:
    db = os.path.join(tmp, "scores.json")
    old_file = srv.SCORES_FILE
    saved_scores = copy.deepcopy(srv.SCORES)
    # Fresh dict, not the live one: mutating module state would leak test
    # keys into the real table under any in-process runner.
    srv.SCORES_FILE = db
    srv.SCORES = {}
    try:
        # fresh start: nothing on disk -> {}
        assert srv.load_scores() == {}
        print("LOAD_EMPTY_OK")

        # two saves -> primary + one rotated generation with older content
        srv.SCORES["a"] = 1
        srv.save_scores()
        assert _read(db) == {"a": 1}
        assert not os.path.exists(db + ".1")
        srv.SCORES["b"] = 2
        srv.save_scores()
        assert _read(db) == {"a": 1, "b": 2}
        assert _read(db + ".1") == {"a": 1}
        # third save rolls generations: .2 appears, .1 holds latest-old
        srv.SCORES["c"] = 3
        srv.save_scores()
        assert _read(db + ".1") == {"a": 1, "b": 2}
        assert _read(db + ".2") == {"a": 1}
        print("ROTATE_OK")

        # corrupt primary -> recovers from backup
        with open(db, "w") as f:
            f.write("{not json")
        assert srv.load_scores() == {"a": 1, "b": 2}
        print("RECOVER_OK")
    finally:
        srv.SCORES_FILE = old_file
        srv.SCORES.clear()
        srv.SCORES.update(saved_scores)

# --- #173 flag parsing (defaults preserve current behavior) ---
d = srv.parse_args([])
assert (d.host, d.port, d.http_port, d.gm_port) == ("0.0.0.0", 8765, 8766, 8767), d
c = srv.parse_args(
    [
        "--host",
        "127.0.0.1",
        "--port",
        "8775",
        "--http-port",
        "8776",
        "--gm-port",
        "8777",
    ]
)
assert (c.host, c.port, c.http_port, c.gm_port) == ("127.0.0.1", 8775, 8776, 8777), c
print("FLAGS_OK")

print("ALL_SCORES_BACKUP_OK")
