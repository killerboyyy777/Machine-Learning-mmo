"""Bounded process exit after a timed-out policy (#992).

Run from the repo root: python tests/test_policy_exit.py
The wedged case runs in a child process with an outer deadline so a
regression cannot stall the main test runner.
"""

import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHILD = """
import asyncio
import sys
import tempfile
import time

sys.path.insert(0, {root!r})
from ml.conductor import Conductor

class Env:
    async def reset(self):
        await asyncio.sleep(0)
        return {{}}
    async def step(self, action):
        await asyncio.sleep(0)
        return {{}}, 1.0, True, {{}}
    async def close(self):
        pass

def policy(obs, agent_id):
{policy}

async def main():
    cond = Conductor(
        tempfile.mkdtemp(),
        max_agents=1,
        runners=[{{
            "env_factory": lambda aid: Env(),
            "policy_fn": policy,
            "policy_timeout": 0.1,
        }}],
        resume=False,
    )
    run = asyncio.ensure_future(
        cond.run(duration_seconds=1, wave_size=1, wave_delay=0)
    )
    await asyncio.sleep(0.4)
    cond.stop()
    await run
    print("CHILD_DONE", flush=True)

asyncio.run(main())
"""


def _run_child(policy_body):
    script = CHILD.format(root=ROOT, policy=policy_body)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=12
    )
    return proc, time.monotonic() - started


def test_wedged_policy_does_not_hold_the_interpreter_open():
    proc, elapsed = _run_child("    while True:\n        time.sleep(0.05)")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "CHILD_DONE" in proc.stdout, proc.stdout
    assert elapsed < 10, elapsed
    print("POLICY_EXIT_WEDGED_OK")


def test_healthy_policy_child_still_completes():
    proc, elapsed = _run_child("    return 0")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "CHILD_DONE" in proc.stdout, proc.stdout
    assert elapsed < 10, elapsed
    print("POLICY_EXIT_HEALTHY_OK")


def test_policy_worker_is_daemon_and_not_atexit_registered():
    import concurrent.futures.thread as cft
    from concurrent.futures import ThreadPoolExecutor

    from ml.conductor.supervisor import Supervisor

    class Registry:
        def get(self, agent_id):
            return None

    sup = Supervisor(Registry())
    policy = lambda obs, agent_id: 0  # noqa: E731
    executor = sup._executor_for(policy)
    try:
        worker = executor.submit(threading.current_thread).result(timeout=2)
        assert worker.daemon is True
        assert worker not in cft._threads_queues
    finally:
        executor.shutdown(wait=False)
    # Control: a stock pool worker is joined by the interpreter-exit hook.
    stock = ThreadPoolExecutor(max_workers=1)
    try:
        other = stock.submit(threading.current_thread).result(timeout=2)
        assert not other.daemon
        assert other in cft._threads_queues
    finally:
        stock.shutdown(wait=False)
    print("POLICY_EXIT_DAEMON_OK")


if __name__ == "__main__":
    test_wedged_policy_does_not_hold_the_interpreter_open()
    test_healthy_policy_child_still_completes()
    test_policy_worker_is_daemon_and_not_atexit_registered()
    print("ALL_POLICY_EXIT_OK")
