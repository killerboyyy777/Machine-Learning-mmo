"""Torch batch loop regressions (#989): failed workers, dead workers with no
marker, Ctrl+C with a live DQN child, and worker ownership of that child.

Run from the repo root: python tests/test_torch_batch_loop.py
Failure paths are scripted through fake Popen objects and a fake sleep hook,
so no case depends on wall-clock timing. The process-tree case uses real
short-lived helper processes with file-based readiness.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import start  # noqa: E402


class FakePopen:
    def __init__(self, pid=1000):
        self.pid = pid
        self.returncode = None
        self.wait_error = None
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self.wait_error = None
        return self


def _run_loop(rounds, on_sleep):
    """Drive cmd_torch_batch_loop with scripted workers and sleep."""
    args = types.SimpleNamespace(url="ws://127.0.0.1:1", steps=1, rounds=rounds)
    created = []
    killed = []

    def fake_spawn(name, steps, done, url, log):
        proc = FakePopen(1000 + len(created))
        created.append((name, proc, done, log))
        return proc

    def fake_kill(proc):
        if proc.poll() is None:
            killed.append(proc)

    def fake_sleep(seconds):
        return on_sleep(created, seconds)

    with (
        patch.object(start, "_spawn_torch_batch_worker", fake_spawn),
        patch.object(start, "_kill_process_tree", fake_kill),
        patch.object(start.time, "sleep", fake_sleep),
        contextlib.redirect_stdout(io.StringIO()) as out,
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        code = start.cmd_torch_batch_loop(args)
    return code, created, killed, out.getvalue(), err.getvalue()


def test_nonzero_marker_fails_batch():
    def on_sleep(created, seconds):
        for index, (name, proc, done, log) in enumerate(created):
            with open(done, "w") as marker:
                marker.write("3" if index == 1 else "0")

    code, created, killed, out, err = _run_loop(2, on_sleep)
    assert code == 1, code
    assert len(created) == 4, created  # no second batch after a failed one
    assert len(killed) == 4, killed
    assert "TorchBatch0_1" in err and "exit code 3" in err, err
    assert created[1][3] in err, err  # log path is reported
    assert "Batch 0 finished" not in out, out
    print("MARKED_FAILURE_OK")


def test_all_zero_markers_advance():
    def on_sleep(created, seconds):
        for name, proc, done, log in created:
            with open(done, "w") as marker:
                marker.write("0")
            proc.returncode = 0

    code, created, killed, out, err = _run_loop(2, on_sleep)
    assert code == 0, (code, err)
    assert len(created) == 8, created  # two full batches
    assert "Batch 0 finished" in out and "Batch 1 finished" in out, out
    assert killed == [], killed
    print("MARKED_SUCCESS_OK")


def test_exited_worker_without_marker_fails_batch():
    calls = []

    def on_sleep(created, seconds):
        calls.append(1)
        created[2][1].returncode = 9
        if len(calls) > 3:
            raise AssertionError("loop kept waiting on a dead worker")

    code, created, killed, out, err = _run_loop(1, on_sleep)
    assert code == 1, code
    assert len(calls) == 1, calls  # surfaced on the first poll, not after a wait
    assert len(created) == 4 and len(killed) == 3, (created, killed)
    assert "TorchBatch0_2" in err and "exit code 9" in err, err
    assert "without writing a done marker" in err, err
    assert created[2][3] in err, err
    assert "Batch 0 finished" not in out, out
    print("MISSING_MARKER_OK")


def test_ctrl_c_stops_live_workers():
    def on_sleep(created, seconds):
        raise KeyboardInterrupt()

    code, created, killed, out, err = _run_loop(0, on_sleep)
    assert code == 0, (code, err)
    assert len(created) == 4 and len(killed) == 4, (created, killed)
    assert "Stopping loop and current agents" in out, out
    print("CTRL_C_OK")


def test_worker_owns_dqn_child():
    with tempfile.TemporaryDirectory() as directory:
        done = os.path.join(directory, "worker.done")
        child = FakePopen()
        child.returncode = 7
        args = types.SimpleNamespace(
            name="Worker", steps=3, url="ws://127.0.0.1:1", done_file=done
        )
        with (
            patch.object(start, "_spawn_dqn_child", return_value=child),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = start.cmd_torch_batch_worker(args)
        with open(done) as marker:
            assert marker.read() == "7", "worker must report the DQN exit code"
        assert code == 7, code
        assert child.poll() == 7, child.poll()  # already exited, nothing to reap

        # Interrupted while waiting: still reap the child and fail the marker.
        child2 = FakePopen()
        child2.wait_error = KeyboardInterrupt()
        args2 = types.SimpleNamespace(
            name="Worker2", steps=3, url="ws://127.0.0.1:1", done_file=done
        )
        with (
            patch.object(start, "_spawn_dqn_child", return_value=child2),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code2 = start.cmd_torch_batch_worker(args2)
        with open(done) as marker:
            assert marker.read() == "1", "an interrupted worker must not look done"
        assert code2 == 1, code2
        assert child2.terminate_calls >= 1, child2.terminate_calls
    print("WORKER_CHILD_OK")


def _wait_for_pid(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path) as handle:
                text = handle.read().strip()
            if text.isdigit():
                return int(text)
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"no pid written to {path}")


def _pid_alive(pid):
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_kill_process_tree_reaps_descendant():
    """The loop's stop path must not leave a DQN grandchild running."""
    wrapper = """
import os, subprocess, sys, time
GRAND = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
grand = subprocess.Popen([sys.executable, "-c", GRAND, sys.argv[1]])
open(sys.argv[1] + ".parent", "w").write(str(os.getpid()))
grand.wait()
"""
    with tempfile.TemporaryDirectory() as directory:
        pid_file = os.path.join(directory, "grandchild.pid")
        script = os.path.join(directory, "wrapper.py")
        with open(script, "w") as handle:
            handle.write(wrapper)
        proc = subprocess.Popen(
            [sys.executable, script, pid_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **start._torch_worker_popen_kwargs(),
        )
        try:
            grand_pid = _wait_for_pid(pid_file)
            wrapper_pid = _wait_for_pid(pid_file + ".parent")
            assert _pid_alive(grand_pid) and _pid_alive(wrapper_pid)
            start._kill_process_tree(proc)
            assert _wait_dead(wrapper_pid), "wrapper survived the tree kill"
            assert _wait_dead(grand_pid), "DQN descendant survived the tree kill"
        finally:
            start._kill_process_tree(proc)
    print("PROCESS_TREE_OK")


if __name__ == "__main__":
    test_nonzero_marker_fails_batch()
    test_all_zero_markers_advance()
    test_exited_worker_without_marker_fails_batch()
    test_ctrl_c_stops_live_workers()
    test_worker_owns_dqn_child()
    test_kill_process_tree_reaps_descendant()
    print("ALL_TORCH_BATCH_LOOP_OK")
