"""Telemetry export contract tests (#73): offline, no sockets.

Run from the repo root:  python tests/test_telemetry.py
Covers flag-off defaults, JSONL/aggregate shapes, the WebSocket RTT helper,
and the guarded timing pattern in the supervisor task loop.
"""

import ast
import asyncio
import inspect
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml.conductor import AgentTask, Conductor  # noqa: E402
from ml.conductor.soak import _truthy_env  # noqa: E402
from ml.ml_env import TextMMOEnv  # noqa: E402
from ml.telemetry import (  # noqa: E402
    METRIC_NAMES,
    Telemetry,
    main,
    record_ws_rtt,
    rtt_ms,
    summarize,
)

SUPERVISOR_SRC = os.path.join(ROOT, "ml", "conductor", "supervisor.py")
TELEMETRY_SRC = os.path.join(ROOT, "ml", "telemetry.py")
ENV_SRC = os.path.join(ROOT, "ml", "ml_env.py")
SOAK_SRC = os.path.join(ROOT, "ml", "conductor", "soak.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "tests.yml")


def test_default_off():
    saved = os.environ.pop("TEXTMMO_TELEMETRY", None)
    try:
        assert _truthy_env("TEXTMMO_TELEMETRY") is False
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "telemetry.jsonl")
            collector = Telemetry(_truthy_env("TEXTMMO_TELEMETRY"), path)
            assert collector.enabled is False
            collector.record("agent_step_ms", 12.0, agent_id="a")
            assert collector.samples == []
            assert os.listdir(d) == []
    finally:
        if saved is not None:
            os.environ["TEXTMMO_TELEMETRY"] = saved
    assert inspect.signature(Conductor.__init__).parameters["telemetry"].default is None
    assert (
        inspect.signature(TextMMOEnv.__init__).parameters["telemetry"].default is None
    )
    assert inspect.signature(AgentTask.__init__).parameters["telemetry"].default is None
    print("DEFAULT_OFF_OK")


def test_on_records_json_safe():
    with tempfile.TemporaryDirectory() as d:
        raw = os.path.join(d, "telemetry.jsonl")
        out = os.path.join(d, "nested", "telemetry.json")
        collector = Telemetry(True, raw)
        for value in (10.0, 20.0, 30.0):
            collector.record(
                "agent_step_ms", value, agent_id="agent-1", episode=2, step=3
            )
        collector.record("env_reset_ms", 5.0, agent_id="agent-1")
        collector.record("ws_rtt_ms", 0.012, agent_id="agent-1")
        collector.flush()
        with open(raw, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        assert len(rows) == 5
        assert all(row["schema_version"] == 1 for row in rows)
        assert all("agent_id" in row for row in rows)
        assert rows[0]["episode"] == 2 and rows[0]["step"] == 3

        report = summarize(collector.samples)
        json.dumps(report, sort_keys=True)
        assert report["schema_version"] == 1
        assert report["enabled"] is True
        assert set(report["metrics"]) == set(METRIC_NAMES)
        step = report["metrics"]["agent_step_ms"]
        assert step["count"] == 3
        assert step["mean_ms"] == 20.0
        assert step["p50_ms"] == 20.0
        assert step["p95_ms"] == 30.0
        assert step["max_ms"] == 30.0

        path = collector.write_report(out)
        assert path == os.path.abspath(out)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        assert text.endswith("\n")
        saved = json.loads(text)
        assert saved["schema_version"] == 1
        assert saved["enabled"] is True
        assert set(saved["metrics"]) == set(METRIC_NAMES)
        blob = json.dumps(saved, sort_keys=True)
        assert "agent_id" not in blob
        assert "agent-1" not in blob
        assert "prev_obs" not in blob and "action" not in blob

        offline = os.path.join(d, "offline.json")
        assert main(["--input", raw, "--out", offline]) == 0
        with open(offline, encoding="utf-8") as fh:
            assert json.load(fh)["metrics"]["ws_rtt_ms"]["count"] == 1
    print("ON_RECORD_OK")


def test_incomplete_rows_skipped():
    good = {"event": "agent_step_ms", "schema_version": 1, "value_ms": 12.0}
    bad_rows = [
        {"event": "agent_step_ms", "schema_version": 1},
        "not-a-dict",
        {"event": "agent_step_ms", "schema_version": 1, "value_ms": "fast"},
        {"event": "agent_step_ms", "schema_version": 1, "value_ms": True},
    ]
    report = summarize([good] + bad_rows)
    step = report["metrics"]["agent_step_ms"]
    assert step["count"] == 1
    assert step["mean_ms"] == 12.0
    assert step["p50_ms"] == 12.0
    assert step["p95_ms"] == 12.0
    assert step["max_ms"] == 12.0

    with tempfile.TemporaryDirectory() as d:
        raw = os.path.join(d, "telemetry.jsonl")
        out = os.path.join(d, "report.json")
        with open(raw, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(good) + "\n")
            for row in bad_rows:
                fh.write(json.dumps(row) + "\n")
        assert main(["--input", raw, "--out", out]) == 0
        with open(out, encoding="utf-8") as fh:
            saved = json.load(fh)
        step = saved["metrics"]["agent_step_ms"]
        assert step["count"] == 1
        assert step["mean_ms"] == 12.0
        assert step["max_ms"] == 12.0
    print("INCOMPLETE_ROWS_OK")


class _StubWS:
    def __init__(self, seconds=0.012):
        self.seconds = seconds
        self.calls = 0

    async def ping(self):
        self.calls += 1

        async def pong():
            return self.seconds

        return pong()


class _FailingWS:
    async def ping(self):
        raise ConnectionError("gone")


def test_nonfinite_samples_skipped():
    collector = Telemetry(True)
    for value in (float("nan"), float("inf"), float("-inf")):
        collector.record("agent_step_ms", value)
    assert collector.samples == []

    good = {"event": "agent_step_ms", "value_ms": 12.0}
    bad = [
        {"event": "agent_step_ms", "value_ms": float("nan")},
        {"event": "agent_step_ms", "value_ms": float("inf")},
        {"event": "agent_step_ms", "value_ms": float("-inf")},
    ]
    report = summarize([good] + bad)
    step = report["metrics"]["agent_step_ms"]
    assert step["count"] == 1
    assert step["mean_ms"] == step["p50_ms"] == step["p95_ms"] == step["max_ms"] == 12.0
    json.dumps(report, allow_nan=False)

    with tempfile.TemporaryDirectory() as d:
        raw = os.path.join(d, "telemetry.jsonl")
        out = os.path.join(d, "report.json")
        with open(raw, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(good) + "\\n")
            for row in bad:
                fh.write(json.dumps(row) + "\\n")
        assert main(["--input", raw, "--out", out]) == 0
        with open(out, encoding="utf-8") as fh:
            saved_text = fh.read()
        assert "NaN" not in saved_text and "Infinity" not in saved_text
        saved = json.loads(saved_text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        assert saved["metrics"]["agent_step_ms"]["count"] == 1
    print("NONFINITE_SAMPLES_OK")


def test_rtt_stub():
    assert rtt_ms(0.012) == 12.0
    with tempfile.TemporaryDirectory() as d:
        collector = Telemetry(True, os.path.join(d, "telemetry.jsonl"))
        ws = _StubWS()
        asyncio.run(record_ws_rtt(collector, ws, agent_id="a"))
        assert ws.calls == 1
        assert len(collector.samples) == 1
        assert collector.samples[0]["event"] == "ws_rtt_ms"
        assert collector.samples[0]["value_ms"] == 12.0

        off_ws = _StubWS()
        asyncio.run(record_ws_rtt(None, off_ws, agent_id="a"))
        assert off_ws.calls == 0

        asyncio.run(record_ws_rtt(collector, _FailingWS(), agent_id="a"))
        assert len(collector.samples) == 1
    print("RTT_STUB_OK")


def test_source_guard():
    with open(SUPERVISOR_SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    task = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentTask"
    )
    run = next(
        n for n in task.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
    )
    calls = []

    def walk(node, guarded):
        if isinstance(node, ast.If) and ast.unparse(node.test) == (
            "self.telemetry is not None"
        ):
            guarded = True
        if isinstance(node, ast.Call) and ast.unparse(node.func) == (
            "time.perf_counter_ns"
        ):
            calls.append((node.lineno, guarded))
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    walk(run, False)
    assert len(calls) == 4, calls
    assert all(guarded for _, guarded in calls), calls

    with open(SUPERVISOR_SRC, encoding="utf-8") as fh:
        supervisor_src = fh.read()
    assert "self.telemetry.record(" in supervisor_src
    assert '"env_reset_ms"' in supervisor_src
    assert '"agent_step_ms"' in supervisor_src
    with open(TELEMETRY_SRC, encoding="utf-8") as fh:
        telemetry_src = fh.read()
    assert "await (await ws.ping())" in telemetry_src
    with open(ENV_SRC, encoding="utf-8") as fh:
        env_src = fh.read()
    assert "await record_ws_rtt(self.telemetry, self.ws" in env_src
    with open(SOAK_SRC, encoding="utf-8") as fh:
        soak_src = fh.read()
    assert "TEXTMMO_TELEMETRY" in soak_src
    assert "--telemetry-dir" in soak_src
    with open(WORKFLOW, encoding="utf-8") as fh:
        workflow = fh.read()
    assert (
        workflow.index("test_metrics_route.py")
        < workflow.index("test_telemetry.py")
        < workflow.index("test_source_policy.py")
    )
    print("SOURCE_GUARD_OK")


if __name__ == "__main__":
    test_default_off()
    test_on_records_json_safe()
    test_incomplete_rows_skipped()
    test_nonfinite_samples_skipped()
    test_rtt_stub()
    test_source_guard()
    print("ALL_TELEMETRY_OK")
