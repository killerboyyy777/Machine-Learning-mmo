"""Offline MCP harness test: fake game server + real stdio subprocess."""

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parent.parent


def room_view(room_id):
    name = "Town Square" if room_id == "town_square" else "Old Shop"
    exits = (
        {"west": "old_shop"} if room_id == "town_square" else {"east": "town_square"}
    )
    return {
        "type": "room",
        "id": room_id,
        "name": name,
        "description": "A test room.",
        "exits": exits,
        "npcs": [],
        "items": [],
        "gold": 0,
        "players": [],
        "is_dungeon": False,
        "dungeon_floor": 0,
        "party_size": 1,
        "shelter": True,
        "wild": False,
    }


def stats_view():
    return {
        "type": "stats",
        "hp": 20,
        "max_hp": 20,
        "gold": 10,
        "level": 1,
        "pack": 0,
        "pack_max": 20,
        "party_size": 1,
    }


async def main():
    seen = []
    current = {"room": "town_square"}

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg)
            cmd = msg.get("cmd")
            if cmd == "login":
                await ws.send(
                    json.dumps(
                        {
                            "type": "welcome",
                            "protocol_version": 2,
                            "client_version": 2,
                            "text": "Welcome, HarnessTest (level 1).",
                        }
                    )
                )
                await ws.send(json.dumps(room_view(current["room"])))
                await ws.send(json.dumps(stats_view()))
            elif cmd == "look":
                await ws.send(json.dumps(room_view(current["room"])))
            elif cmd == "move":
                direction = msg.get("dir")
                if direction == "bad":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "error",
                                "text": "You can't go 'bad' from here.",
                            }
                        )
                    )
                    continue
                current["room"] = "old_shop" if direction == "west" else "town_square"
                await ws.send(json.dumps(room_view(current["room"])))
                await ws.send(json.dumps(stats_view()))
            else:
                await ws.send(json.dumps({"type": "error", "text": "unknown command"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proc = None
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "harness",
            "--url",
            f"ws://127.0.0.1:{port}",
            "--name",
            "HarnessTest",
            cwd=str(REPO),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        async def rpc(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()
            if "id" not in message:
                return None
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 10)
                if not line:
                    raise AssertionError("harness closed stdout before replying")
                reply = json.loads(line)
                if reply.get("id") == message["id"]:
                    return reply

        init = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "harness-test", "version": "0"},
                },
            }
        )
        assert init["result"]["serverInfo"]["name"] == "textmmo-harness", init
        assert init["result"]["protocolVersion"] == "2025-06-18", init
        print("HARNESS_INIT_OK")

        await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

        listed = await rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {"game_look", "game_move"}, names
        print("HARNESS_TOOLS_OK")

        looked = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "game_look", "arguments": {}},
            }
        )
        look_payload = json.loads(looked["result"]["content"][0]["text"])
        assert looked["result"]["isError"] is False, looked
        assert look_payload["ok"] is True, look_payload
        assert look_payload["state"]["room"] == "town_square", look_payload
        assert look_payload["events"][-1]["type"] == "room", look_payload
        assert {"cmd": "look"} in seen, seen
        print("HARNESS_LOOK_OK")

        resource = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "game://room"},
            }
        )
        room_payload = json.loads(resource["result"]["contents"][0]["text"])
        assert room_payload["available"] is True, room_payload
        assert room_payload["room"]["id"] == "town_square", room_payload
        print("HARNESS_RESOURCE_OK")

        moved = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "game_move", "arguments": {"dir": "west"}},
            }
        )
        move_payload = json.loads(moved["result"]["content"][0]["text"])
        assert moved["result"]["isError"] is False, moved
        assert move_payload["state"]["room"] == "old_shop", move_payload
        assert {"cmd": "move", "dir": "west"} in seen, seen
        print("HARNESS_MOVE_OK")

        refused = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "game_move", "arguments": {"dir": "bad"}},
            }
        )
        refused_payload = json.loads(refused["result"]["content"][0]["text"])
        assert refused["result"]["isError"] is True, refused
        assert "can't go" in refused_payload["error"], refused_payload
        print("HARNESS_ERROR_OK")

        unknown = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "game_nope", "arguments": {}},
            }
        )
        assert unknown["error"]["code"] == -32602, unknown
        print("HARNESS_UNKNOWN_OK")

        proc.stdin.close()
        await proc.stdin.wait_closed()
        code = await asyncio.wait_for(proc.wait(), 10)
        assert code == 0, code
        print("HARNESS_EOF_OK")

        async def cli_exit(args, child_env=None):
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "harness",
                *args,
                cwd=str(REPO),
                env=child_env or env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await asyncio.wait_for(child.wait(), 10)

        code = await cli_exit(
            ["--url", "ws://203.0.113.1:8765", "--name", "HarnessTest"]
        )
        assert code == 2, code
        print("HARNESS_REMOTE_GATE_OK")

        missing = env.copy()
        missing.pop("HARNESS_MISSING_ENV", None)
        code = await cli_exit(
            [
                "--url",
                "ws://127.0.0.1:1",
                "--name",
                "HarnessTest",
                "--token-env",
                "HARNESS_MISSING_ENV",
            ],
            child_env=missing,
        )
        assert code == 2, code
        print("HARNESS_TOKEN_ENV_GATE_OK")
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.close()
        await server.wait_closed()

    print("HARNESS_MCP_OK")


if __name__ == "__main__":
    asyncio.run(main())
