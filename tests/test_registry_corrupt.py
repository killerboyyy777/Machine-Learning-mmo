"""Corrupt conductor registry regression (#983).

Run from the repo root:  python tests/test_registry_corrupt.py

A torn or hand-edited registry.json must not keep a conductor (or the soak
CLI that builds one) from starting. Valid entries still restore exactly.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor import Conductor, Registry  # noqa: E402


def _write(base, text):
    reg_dir = Path(base) / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / "registry.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_registry_restores_entries_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry(os.path.join(tmp, "reg"), max_agents=5)
        parent = reg.register("a1", "linear", goal={"w_score": 1.0}, character="role_0")
        parent.episodes = 4
        parent.total_reward = 12.5
        parent.alive = False
        reg.register("a2", "torch", parent_id="a1")
        reg.save()

        again = Registry(os.path.join(tmp, "reg"), max_agents=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            again.load()
        assert out.getvalue() == ""
        restored = again.get("a1")
        assert restored.goal == {"w_score": 1.0}
        assert restored.character == "role_0"
        assert restored.episodes == 4
        assert restored.total_reward == 12.5
        assert restored.alive is False
        assert again.get("a2").parent_id == "a1"
        assert again.snapshot()["total"] == 2
    print("REGISTRY_VALID_OK")


def test_missing_registry_is_a_silent_noop():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry(os.path.join(tmp, "no-file"), max_agents=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            reg.load()
        assert out.getvalue() == ""
        assert reg.snapshot()["total"] == 0
    print("REGISTRY_MISSING_OK")


def test_corrupt_json_starts_an_empty_conductor():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "soak")
        _write(base, '{"agents": [')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cond = Conductor(base, max_agents=2, resume=True)
        assert cond.pbt is None
        assert cond._pending_resume == []
        assert cond.registry.snapshot()["total"] == 0
        assert cond.status()["registry"]["agents"] == []
        message = out.getvalue()
        assert "Warning" in message and "registry.json" in message, message
    print("REGISTRY_CORRUPT_CONDUCTOR_OK")


def test_unexpected_shape_and_bad_rows_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "[]")
        reg = Registry(os.path.join(tmp, "shape"), max_agents=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            reg.load(path=path)
        assert "unexpected shape" in out.getvalue()
        assert reg.snapshot()["total"] == 0

        payload = {
            "agents": [
                {
                    "agent_id": "ok",
                    "agent_type": "linear",
                    "goal": {"w_score": 1.0},
                    "episodes": 2,
                    "total_reward": 3.0,
                    "parent_id": "root",
                },
                {"agent_id": "no-type"},
                "junk",
            ]
        }
        path = _write(tmp, json.dumps(payload))
        rows = Registry(os.path.join(tmp, "rows"), max_agents=5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rows.load(path=path)
        message = out.getvalue()
        assert "skipping malformed agent entry" in message, message
        assert rows.snapshot()["total"] == 1
        good = rows.get("ok")
        assert good.goal == {"w_score": 1.0}
        assert good.parent_id == "root"
        assert good.episodes == 2 and good.total_reward == 3.0
        assert rows.get("no-type") is None
    print("REGISTRY_BAD_ROWS_OK")


if __name__ == "__main__":
    test_valid_registry_restores_entries_exactly()
    test_missing_registry_is_a_silent_noop()
    test_corrupt_json_starts_an_empty_conductor()
    test_unexpected_shape_and_bad_rows_are_skipped()
    print("ALL_REGISTRY_CORRUPT_OK")
