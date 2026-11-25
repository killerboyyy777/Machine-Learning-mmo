"""Structured event log tests (#316): opt-in JSONL beside the stdout logs.

Run from the repo root:  python tests/test_structured_log.py
No server, no sockets, no torch; only temporary directories.
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml.conductor.conductor import Conductor  # noqa: E402
from ml.conductor.soak import _truthy_env  # noqa: E402
from ml.conductor.supervisor import Supervisor  # noqa: E402
from ml.structured_log import StructuredLog, build_row  # noqa: E402

SOAK_SRC = os.path.join(ROOT, "ml", "conductor", "soak.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "tests.yml")


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_default_off():
    assert (
        inspect.signature(Conductor.__init__).parameters["structured_log"].default
        is None
    )
    assert (
        inspect.signature(Supervisor.__init__).parameters["structured_log"].default
        is None
    )
    with tempfile.TemporaryDirectory() as d:
        cond = Conductor(os.path.join(d, "run"))
        assert cond.structured_log is None
        assert cond.supervisor._structured_log is None
        assert not os.path.exists(os.path.join(d, "run", "structured.jsonl"))
    os.environ.pop("TEXTMMO_STRUCTURED_LOG", None)
    assert _truthy_env("TEXTMMO_STRUCTURED_LOG") is False
    for value in ("1", "true", "yes", "on"):
        os.environ["TEXTMMO_STRUCTURED_LOG"] = value
        assert _truthy_env("TEXTMMO_STRUCTURED_LOG") is True
    os.environ.pop("TEXTMMO_STRUCTURED_LOG", None)
    print("DEFAULT_OFF_OK")


def test_row_and_jsonl():
    row = build_row("episode", ids=["a"], error="boom", episode=3)
    assert row["schema_version"] == 1
    assert row["action"] == "episode"
    assert row["ids"] == ["a"]
    assert row["error"] == "boom"
    assert row["episode"] == 3
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "nested", "structured.jsonl")
        log = StructuredLog(path)
        log.log("wave", wave=2, total_alive=7)
        log.log("checkpoint_reload", ids=["a", "b"], sha256="0" * 64)
        log.log("plugin_disabled", error="x" * 800)
        rows = _rows(path)
        assert len(rows) == 3, rows
        assert rows[0]["schema_version"] == 1
        assert rows[0]["action"] == "wave"
        assert rows[0]["wave"] == 2
        assert isinstance(rows[0]["_ts"], float)
        assert round(rows[0]["_ts"], 3) == rows[0]["_ts"]
        assert "ids" not in rows[0] and "error" not in rows[0]
        assert rows[1]["ids"] == ["a", "b"]
        assert len(rows[2]["error"]) == 500
        assert rows[2]["error"] == "x" * 500
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        assert lines == [json.dumps(r, sort_keys=True, default=str) for r in rows]
        marker = object()
        log.log("agent_spawn", raw=marker)
        assert _rows(path)[-1]["raw"] == str(marker)
        log.log("promotion", ids=(i for i in ("c", "d")))
        assert _rows(path)[-1]["ids"] == ["c", "d"]
    print("ROW_JSONL_OK")


def test_log_never_raises():
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        log = StructuredLog(os.path.join(blocker, "structured.jsonl"))

        class BadIds:
            def __iter__(self):
                raise RuntimeError("no ids")

        log.log("startup")
        log.log("startup", ids=BadIds(), error=ValueError("bad"))
        assert not os.path.exists(blocker + os.sep + "structured.jsonl")
    print("SWALLOW_OK")


def test_conductor_wiring():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "structured.jsonl")
        log = StructuredLog(path)
        cond = Conductor(os.path.join(d, "run"), structured_log=log)
        assert cond.supervisor._structured_log is log

        class Watcher:
            def poll(self):
                return [("ckpt.pt", "a" * 64)]

        class Sup:
            async def restart_agent(self, agent_id, hparams=None):
                return True

        cond._watcher = Watcher()
        cond.supervisor = Sup()
        cond.registry.register("hot", "linear", checkpoint="ckpt.pt")
        asyncio.run(cond._reload_checkpoints())
        rows = _rows(path)
        assert len(rows) == 1, rows
        assert rows[0]["action"] == "checkpoint_reload"
        assert rows[0]["ids"] == ["hot"]
        assert rows[0]["sha256"] == "a" * 64
    print("CONDUCTOR_WIRING_OK")


def test_supervisor_wiring():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "structured.jsonl")
        log = StructuredLog(path)
        sup = Supervisor(None, structured_log=log)

        def policy():
            return None

        for _ in range(3):
            sup._record_policy_fault(policy, "broken")
        rows = _rows(path)
        assert len(rows) == 1, rows
        assert rows[0]["action"] == "plugin_disabled"
        assert rows[0]["faults"] == 3
        assert rows[0]["error"] == "broken"
    print("SUPERVISOR_WIRING_OK")


def test_source_guard():
    with open(SOAK_SRC, encoding="utf-8") as fh:
        src = fh.read()
    assert "TEXTMMO_STRUCTURED_LOG" in src
    assert "--structured-log-dir" in src
    with open(WORKFLOW, encoding="utf-8") as fh:
        workflow = fh.read()
    assert workflow.index("test_structured_log.py") > workflow.index(
        "test_source_policy.py"
    )
    print("SOURCE_GUARD_OK")


if __name__ == "__main__":
    test_default_off()
    test_row_and_jsonl()
    test_log_never_raises()
    test_conductor_wiring()
    test_supervisor_wiring()
    test_source_guard()
    print("ALL_STRUCTURED_LOG_OK")
