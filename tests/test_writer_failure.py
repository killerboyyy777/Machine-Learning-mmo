"""A dead outbound writer must release the session (#979).

Run from the repo root:  python tests/test_writer_failure.py
"""

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402


class FailingWS:
    remote_address = ("127.0.0.1", 4421)

    def __init__(self):
        self.closed = asyncio.Event()
        self.sends = 0

    def __aiter__(self):
        async def frames():
            yield json.dumps({"cmd": "login", "name": "WriterFail"})
            # A real socket's receive loop ends when close() lands.
            await self.closed.wait()

        return frames()

    async def send(self, payload):
        self.sends += 1
        raise OSError("simulated write failure")

    async def close(self):
        self.closed.set()


async def _check():
    before_players = set(srv.players)
    before_room = list(srv.players_in_room(srv.START_ROOM))
    ws = FailingWS()
    await asyncio.wait_for(srv.handle_connection(ws), timeout=5)
    assert ws.sends >= 1, "writer never attempted a send"
    assert ws.closed.is_set(), "writer did not close the dead socket"
    new_players = set(srv.players) - before_players
    assert not new_players, f"zombie session still registered: {new_players}"
    assert list(srv.players_in_room(srv.START_ROOM)) == before_room, "room slot leaked"
    assert "writerfail" not in srv.name_owners, "name lock leaked"
    print("WRITER_FAILURE_TEARDOWN_OK")

    # Positive control: a working writer still delivers replies normally.
    class HealthyWS(FailingWS):
        def __aiter__(self):
            async def frames():
                yield json.dumps({"cmd": "login", "name": "WriterOk"})
                # Give the outbound writer a turn to drain the queue.
                await asyncio.sleep(0.05)

            return frames()

        async def send(self, payload):
            self.sends += 1

    healthy = HealthyWS()
    await asyncio.wait_for(srv.handle_connection(healthy), timeout=5)
    assert healthy.sends >= 1, "healthy writer sent nothing"
    assert "writerfail" not in srv.name_owners
    print("HEALTHY_WRITER_OK")


try:
    srv.SCORES.pop("writerfail", None)
    srv.SCORES.pop("writerok", None)
    asyncio.run(_check())
finally:
    srv.SCORES.pop("writerfail", None)
    srv.SCORES.pop("writerok", None)

print("ALL_WRITER_FAILURE_OK")
