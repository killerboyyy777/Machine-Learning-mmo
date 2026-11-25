"""Checkpoint hot-reload tests (#75): stdlib watcher plus conductor seam.

Run from the repo root:  python tests/test_checkpoint_reload.py
No server, no sockets, no torch: the watcher reads linear checkpoints and the
conductor seam uses a fake supervisor.
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml.conductor.conductor import Conductor  # noqa: E402
from ml.conductor.reload import CheckpointWatcher  # noqa: E402
from ml.ml_client import LinearQAgent  # noqa: E402
from ml.ml_env import N_ACTIONS, OBS_SIZE  # noqa: E402

SOAK_SRC = os.path.join(ROOT, "ml", "conductor", "soak.py")
_HEAVY_MARKER = 123456789.0


def _write_checkpoint(path, marker):
    agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
    agent.weights[0][0] = marker
    agent.save(path)


def _reload_events(cond):
    cond.metrics.flush()
    if not os.path.isfile(cond.metrics.path):
        return []
    with open(cond.metrics.path) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return [r for r in rows if r.get("event") == "checkpoint_reload"]


def test_off_by_default():
    assert inspect.signature(Conductor.__init__).parameters["reload"].default is None
    with tempfile.TemporaryDirectory() as d:
        cond = Conductor(os.path.join(d, "run"), reload=None)
        assert cond._watcher is None
        assert _reload_events(cond) == []
    from ml.conductor.soak import _truthy_env

    os.environ.pop("TEXTMMO_RELOAD", None)
    assert _truthy_env("TEXTMMO_RELOAD") is False
    for value in ("1", "true", "yes", "on"):
        os.environ["TEXTMMO_RELOAD"] = value
        assert _truthy_env("TEXTMMO_RELOAD") is True
    os.environ.pop("TEXTMMO_RELOAD", None)
    print("OFF_OK")


def test_watcher_baseline_and_change():
    with tempfile.TemporaryDirectory() as d:
        ckpt = os.path.join(d, "weights.pt")
        _write_checkpoint(ckpt, 0.0)
        watcher = CheckpointWatcher(d, interval=0.0)
        assert watcher.poll() == []
        _write_checkpoint(ckpt, _HEAVY_MARKER)
        changed = watcher.poll()
        assert len(changed) == 1, changed
        path, digest = changed[0]
        assert os.path.normpath(path) == os.path.normpath(ckpt)
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        assert watcher.poll() == []
        with open(os.path.join(d, "scratch.tmp"), "w") as fh:
            fh.write("x")
        with open(os.path.join(d, ".hidden.json"), "w") as fh:
            fh.write("{}")
        assert watcher.poll() == []
        assert len(watcher._baseline) == 1
    print("WATCHER_OK")


def test_watcher_interval_gate():
    with tempfile.TemporaryDirectory() as d:
        ckpt = os.path.join(d, "gated.pt")
        _write_checkpoint(ckpt, 0.0)
        watcher = CheckpointWatcher(d, interval=0.1)
        assert watcher.poll() == []
        _write_checkpoint(ckpt, _HEAVY_MARKER)
        watcher._last_scan = time.monotonic()
        assert watcher.poll() == []
        time.sleep(0.3)
        assert len(watcher.poll()) == 1
    print("GATE_OK")


def test_conductor_restarts_only_matching_agents():
    with tempfile.TemporaryDirectory() as d:
        watch_dir = os.path.join(d, "watch")
        os.makedirs(watch_dir)
        hot_ckpt = os.path.join(watch_dir, "hot.pt")
        cold_ckpt = os.path.join(watch_dir, "cold.pt")
        _write_checkpoint(hot_ckpt, 0.0)
        _write_checkpoint(cold_ckpt, 0.0)

        cond = Conductor(os.path.join(d, "run"), max_agents=3, reload=watch_dir)
        cond.registry.register("hot", "linear", checkpoint=hot_ckpt)
        cond.registry.register("cold", "linear", checkpoint=cold_ckpt)
        cond._watcher.interval = 0.0
        assert cond._watcher.poll() == []

        calls = []

        class FakeSupervisor:
            async def restart_agent(self, agent_id, hparams=None):
                calls.append((agent_id, hparams))
                return True

        cond.supervisor = FakeSupervisor()
        _write_checkpoint(hot_ckpt, _HEAVY_MARKER)
        asyncio.run(cond._reload_checkpoints())
        assert calls == [("hot", None)], calls
        events = _reload_events(cond)
        assert len(events) == 1, events
        assert events[0]["agent_id"] == "hot"
        assert os.path.normpath(events[0]["path"]) == os.path.normpath(hot_ckpt)
        assert len(events[0]["sha256"]) == 64
        asyncio.run(cond._reload_checkpoints())
        assert calls == [("hot", None)], calls
        assert len(_reload_events(cond)) == 1
    print("SEAM_OK")


def test_source_guard():
    with open(SOAK_SRC, encoding="utf-8") as fh:
        src = fh.read()
    assert "TEXTMMO_RELOAD" in src
    assert "--reload-dir" in src
    print("SOURCE_GUARD_OK")


if __name__ == "__main__":
    test_off_by_default()
    test_watcher_baseline_and_change()
    test_watcher_interval_gate()
    test_conductor_restarts_only_matching_agents()
    test_source_guard()
    print("ALL_CHECKPOINT_RELOAD_OK")
