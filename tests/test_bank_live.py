"""Live bank cycle: deposit, interest tick, withdraw (needs spawned server).

Run from the repo root:  python tests/test_bank_live.py

The test starts its own server on 19865/19866/19867 with a one-second
interest interval, so it does not touch a shared balance seat. It waits for a
real tick, then checks the GM stream and treasury floor.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_PORT = 19865
HTTP_PORT = 19866
GM_PORT = 19867
GAME_URI = f"ws://127.0.0.1:{GAME_PORT}"
GM_URI = f"ws://127.0.0.1:{GM_PORT}"


async def send(ws, obj):
    await ws.send(json.dumps(obj))


async def recv_until(ws, wanted, timeout=15.0):
    """Collect events until one has the wanted type; return (found, all)."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), max(0.05, deadline - time.monotonic())
            )
        except TimeoutError:
            break
        msg = json.loads(raw)
        seen.append(msg)
        if msg.get("type") == wanted:
            return msg, seen
    return None, seen


async def drain(ws, timeout=0.3):
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), max(0.05, deadline - time.monotonic())
            )
        except TimeoutError:
            break
        seen.append(json.loads(raw))
    return seen


def wait_health(timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{HTTP_PORT}/health", timeout=1.0
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not answer /health on the bank test port")


async def run_cycle():
    async with websockets.connect(GAME_URI, max_size=None) as game:
        await send(game, {"cmd": "login", "name": "BankLive"})
        stats, _ = await recv_until(game, "stats")
        assert stats is not None, "no stats after login"
        assert stats.get("bank", 0) == 0, stats

        async with websockets.connect(GM_URI, max_size=None) as gm:
            await send(gm, {"cmd": "gm_reward", "player": "BankLive", "gold": 500})
            ack, _ = await recv_until(gm, "message")
            assert ack is not None and "Treasury now" in ack.get("text", ""), ack
            await drain(game)

            await send(game, {"cmd": "bank", "action": "deposit", "gold": 500})
            deposited, _ = await recv_until(game, "bank")
            assert deposited is not None and deposited["action"] == "deposit", deposited
            assert deposited["bank"] == 500, deposited
            await drain(game)

            interest, seen = await recv_until(gm, "bank_interest", timeout=20.0)
            assert interest is not None, "no bank_interest event on the GM stream"
            assert any(
                p["player"] == "BankLive" for p in interest["payments"]
            ), interest
            assert interest["treasury"] >= 0, interest

            await send(game, {"cmd": "bank", "action": "balance"})
            balance, _ = await recv_until(game, "bank")
            assert balance is not None and balance["bank"] > 500, balance
            amount = balance["bank"]

            await send(game, {"cmd": "bank", "action": "withdraw", "gold": amount})
            withdrawn, _ = await recv_until(game, "bank")
            assert (
                withdrawn is not None and withdrawn["action"] == "withdraw"
            ), withdrawn
            assert withdrawn["bank"] == 0, withdrawn
            assert withdrawn["purse"] >= 510, withdrawn

            await send(gm, {"cmd": "gm_tables"})
            tables, _ = await recv_until(gm, "tables")
            assert tables is not None, "no tables on the GM stream"
            assert tables["treasury"] >= 0, tables
            assert tables["bank"]["funding"] == "treasury", tables["bank"]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "bank_config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"economy": {"BANK_INTEREST_SECONDS": 1}}, fh)
        log = os.path.join(tmp, "server.log")
        env = dict(os.environ)
        env["TEXTMMO_DATA_DIR"] = tmp
        env["TEXTMMO_GM_SEED"] = "700"
        cmd = [
            sys.executable,
            os.path.join(ROOT, "server.py"),
            "--port",
            str(GAME_PORT),
            "--http-port",
            str(HTTP_PORT),
            "--gm-port",
            str(GM_PORT),
            "--config",
            config,
        ]
        with open(log, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=logf, stderr=logf)
            try:
                wait_health()
                asyncio.run(run_cycle())
                print("BANK_LIVE_CYCLE_OK")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                if proc.returncode not in (0, -1, 1):
                    with open(log, encoding="utf-8", errors="replace") as fh:
                        print(fh.read()[-2000:])


if __name__ == "__main__":
    main()
