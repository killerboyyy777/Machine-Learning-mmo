"""Env reset-timeout unit tests (no live server needed).

Run from the repo root:  python tests/test_env_reset.py
Covers the wedge fix: _close_ws() abandons a hung socket within
close_timeout, and reset() raises ConnectionError (instead of hanging)
when the server is unreachable.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.ml_env import TextMMOEnv


class _HungSocket:
    async def close(self):
        await asyncio.sleep(3600)


async def _close_check():
    env = TextMMOEnv("CloseTest", close_timeout=0.5)
    env._reader_task = None
    env.ws = _HungSocket()
    t0 = time.time()
    await env._close_ws()
    dt = time.time() - t0
    assert dt < 2.0 and env.ws is None, dt


asyncio.run(_close_check())
print("CLOSE_TIMEOUT_OK")


async def _refused_check():
    env = TextMMOEnv("RefusedTest", url="ws://127.0.0.1:1",
                     connect_timeout=0.5, connect_retries=2)
    t0 = time.time()
    try:
        await env.reset()
        raise SystemExit("FAIL: reset to closed port did not raise")
    except ConnectionError:
        pass
    assert time.time() - t0 < 10.0

asyncio.run(_refused_check())
print("CONNECT_RETRIES_OK")

print("ALL_ENV_RESET_OK")
