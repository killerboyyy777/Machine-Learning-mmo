"""server_config.json obeys config_schema bounds (#981).

Run from the repo root:  python tests/test_config_bounds.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402

KEYS = (
    "CONFIG_FILE",
    "MAX_PLAYERS_PER_ROOM",
    "SCORE_ENTRY_MAX",
    "DEATH_ITEM_DROP_PCT",
    "TAX_RATE",
    "GROUND_ITEM_CAP",
    "AUTH_TOKEN_REQUIRED",
)
_saved = {key: getattr(srv, key) for key in KEYS}
try:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "server_config.json")
        srv.CONFIG_FILE = path

        # Out-of-range values keep the code default and warn.
        with open(path, "w") as f:
            json.dump(
                {
                    "limits": {"MAX_PLAYERS_PER_ROOM": 0},
                    "scoring": {"SCORE_ENTRY_MAX": -5},
                    "economy": {"TAX_RATE": 0.25, "GROUND_ITEM_CAP": 40},
                },
                f,
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            srv._apply_config()
        assert srv.MAX_PLAYERS_PER_ROOM == _saved["MAX_PLAYERS_PER_ROOM"]
        assert srv.SCORE_ENTRY_MAX == _saved["SCORE_ENTRY_MAX"]
        assert srv.TAX_RATE == 0.25 and srv.GROUND_ITEM_CAP == 40
        assert "MAX_PLAYERS_PER_ROOM=0" in out.getvalue(), out.getvalue()
        assert "SCORE_ENTRY_MAX=-5" in out.getvalue(), out.getvalue()
        print("CONFIG_OUT_OF_RANGE_SKIPPED_OK")

        # Valid values still apply, including every schema key's own bounds.
        with open(path, "w") as f:
            json.dump(
                {
                    "limits": {"MAX_PLAYERS_PER_ROOM": 8},
                    "scoring": {"SCORE_ENTRY_MAX": 500, "DEATH_ITEM_DROP_PCT": 25},
                    "economy": {"GROUND_ITEM_CAP": 12},
                },
                f,
            )
        with contextlib.redirect_stdout(io.StringIO()):
            srv._apply_config()
        assert srv.MAX_PLAYERS_PER_ROOM == 8
        assert srv.SCORE_ENTRY_MAX == 500 and srv.GROUND_ITEM_CAP == 12
        assert srv.DEATH_ITEM_DROP_PCT == 25 and isinstance(
            srv.DEATH_ITEM_DROP_PCT, int
        ), "a float-typed percentage must not change an int global's type"
        print("CONFIG_VALID_VALUES_APPLIED_OK")

        # Non-schema globals stay unknown even when they name a real global:
        # a file path or a flag outside the editor must not be redirected.
        with open(path, "w") as f:
            json.dump(
                {
                    "flags": {
                        "CONFIG_FILE": "/tmp/evil.json",
                        "SOME_REAL_GLOBAL": 1,
                    }
                },
                f,
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            srv._apply_config()
        assert srv.CONFIG_FILE == path
        assert "CONFIG_FILE" in out.getvalue() and "unknown key" in out.getvalue()
        print("CONFIG_NON_SCHEMA_BLOCKED_OK")

        # The legacy boolean gate keeps its explicit string coercion.
        for raw, want in (("false", False), ("yes", True), ("0", False)):
            with open(path, "w") as f:
                json.dump({"flags": {"AUTH_TOKEN_REQUIRED": raw}}, f)
            with contextlib.redirect_stdout(io.StringIO()):
                srv._apply_config()
            assert srv.AUTH_TOKEN_REQUIRED is want, (raw, srv.AUTH_TOKEN_REQUIRED)
        print("CONFIG_BOOL_COMPAT_OK")
finally:
    for key, value in _saved.items():
        setattr(srv, key, value)

print("ALL_CONFIG_BOUNDS_OK")
