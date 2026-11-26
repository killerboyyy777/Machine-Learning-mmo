"""Offline MCP harness test: fake game server + real stdio subprocess."""

import asyncio
import collections
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import websockets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import harness.bridge as bridge_module  # noqa: E402
from harness.bridge import (  # noqa: E402
    DRAIN_FRAME_LIMIT,
    RECONNECT_ATTEMPTS,
    BacklogError,
    BridgeError,
    GameBridge,
    LoginError,
)

DEEP_NESTING = 200000


def deep_json_frame():
    return '{"nested":' + '{"a":' * DEEP_NESTING + "0" + "}" * (DEEP_NESTING + 1)


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


async def test_bridge_recursion_frame():
    class FakeWS:
        def __init__(self):
            self.frames = [
                deep_json_frame().encode(),
                b'{"type": "stats", "hp": 20}',
            ]

        async def recv(self):
            return self.frames.pop(0)

    bridge = GameBridge("ws://127.0.0.1:1", "BadFrame")
    bridge._ws = FakeWS()
    assert await bridge._recv_frame(1) is None
    assert await bridge._recv_frame(1) == {"type": "stats", "hp": 20}
    print("HARNESS_BRIDGE_RECURSION_OK")


class FakeQueueWS:
    """Socket whose recv blocks only while the frame queue is empty."""

    def __init__(self, frames=()):
        self.frames = collections.deque(frames)
        self.sent = []
        self._wake = asyncio.Event()

    def push(self, frame):
        self.frames.append(json.dumps(frame).encode())
        self._wake.set()

    async def recv(self):
        while not self.frames:
            self._wake.clear()
            await self._wake.wait()
        return self.frames.popleft()

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("cmd") == "move":
            self.push({"type": "room", "id": "fresh"})
            self.push(stats_view())


async def test_bridge_backlog():
    stale = json.dumps({"type": "room", "id": "stale"}).encode()
    for count in (1, 100, 150):
        ws = FakeQueueWS([stale] * count)
        bridge = GameBridge("ws://127.0.0.1:1", "BacklogTest", command_timeout=2.0)
        bridge._ws = ws
        result = await asyncio.wait_for(
            bridge.command("move", {"dir": "west"}, expect=("room",)), 5
        )
        assert result["ok"] is True, result
        assert result["state"]["room"] == "fresh", result
        assert ws.sent == [{"cmd": "move", "dir": "west"}], ws.sent

    ws = FakeQueueWS([stale] * (DRAIN_FRAME_LIMIT + 1))
    bridge = GameBridge("ws://127.0.0.1:1", "BacklogTest", command_timeout=2.0)
    bridge._ws = ws
    result = await asyncio.wait_for(
        bridge.command("move", {"dir": "west"}, expect=("room",)), 5
    )
    assert result["ok"] is False, result
    assert "backlog" in result["error"].lower(), result
    assert ws.sent == [], ws.sent

    ws = FakeQueueWS([stale] * (DRAIN_FRAME_LIMIT + 1))
    bridge = GameBridge("ws://127.0.0.1:1", "BacklogTest")
    bridge._ws = ws
    try:
        await asyncio.wait_for(bridge._drain(), 5)
    except BacklogError as exc:
        assert "backlog" in str(exc).lower(), exc
    else:
        raise AssertionError("expected BacklogError from _drain")
    print("HARNESS_BRIDGE_BACKLOG_OK")


async def test_bridge_reconnect():
    generations = []
    logins = []
    commands = []

    async def handler(ws):
        generation = len(generations)
        generations.append(generation)
        async for raw in ws:
            msg = json.loads(raw)
            cmd = msg.get("cmd")
            if cmd == "login":
                logins.append((generation, msg))
                await ws.send(json.dumps({"type": "welcome", "protocol_version": 2}))
                await ws.send(json.dumps(room_view(f"room_{generation}")))
                await ws.send(json.dumps(stats_view()))
            elif cmd == "buy":
                commands.append((generation, cmd))
                await ws.close()
                return
            elif cmd == "move":
                commands.append((generation, cmd))
                await ws.send(json.dumps(room_view(f"moved_{generation}")))
                await ws.send(json.dumps(stats_view()))
            else:
                await ws.send(json.dumps({"type": "error", "text": "unknown command"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    bridge = GameBridge(
        f"ws://127.0.0.1:{port}",
        "HarnessTest",
        token="owner-token",
        connect_timeout=1.0,
        command_timeout=1.0,
        move_timeout=1.0,
    )
    try:
        await asyncio.wait_for(bridge.connect(), 5)
        assert bridge.cache["room"]["id"] == "room_0", bridge.cache

        lost = await asyncio.wait_for(
            bridge.command("buy", {"item": "potion"}, expect=("room",)), 5
        )
        assert lost["ok"] is False, lost
        assert "indeterminate" in lost["error"], lost
        assert bridge._ws is None
        assert commands == [(0, "buy")], commands

        moved = await asyncio.wait_for(
            bridge.command("move", {"dir": "west"}, expect=("room",)), 5
        )
        assert moved["ok"] is True, moved
        assert moved["state"]["room"] == "moved_1", moved
        assert generations == [0, 1], generations
        assert [msg.get("token") for _, msg in logins] == ["owner-token"] * 2, logins
        assert commands == [(0, "buy"), (1, "move")], commands
    finally:
        await bridge.close()
        server.close()
        await server.wait_closed()
    print("HARNESS_BRIDGE_RECONNECT_OK")


async def test_bridge_login_rejected():
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("cmd") == "login":
                await ws.send(json.dumps({"type": "error", "text": "bad token"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    bridge = GameBridge(
        f"ws://127.0.0.1:{port}", "HarnessTest", token="bad", connect_timeout=1.0
    )
    try:
        try:
            await asyncio.wait_for(bridge._reconnect(), 5)
        except LoginError as exc:
            assert "login rejected" in str(exc), exc
            assert "bad token" in str(exc), exc
        else:
            raise AssertionError("expected LoginError")
        assert bridge._ws is None
    finally:
        await bridge.close()
        server.close()
        await server.wait_closed()
    print("HARNESS_BRIDGE_LOGIN_REJECTED_OK")


async def test_bridge_reconnect_bounded():
    attempts = []

    async def fake_connect(*args, **kwargs):
        attempts.append(1)
        raise OSError("server down")

    bridge = GameBridge("ws://127.0.0.1:1", "HarnessTest", connect_timeout=0.01)
    with patch.object(bridge_module.websockets, "connect", fake_connect):
        try:
            await asyncio.wait_for(bridge._reconnect(), 5)
        except BridgeError as exc:
            assert "reconnect failed" in str(exc), exc
        else:
            raise AssertionError("expected reconnect failure")
    assert len(attempts) == RECONNECT_ATTEMPTS, attempts
    print("HARNESS_BRIDGE_RECONNECT_BOUNDED_OK")


async def main():
    await test_bridge_recursion_frame()
    await test_bridge_backlog()
    await test_bridge_reconnect()
    await test_bridge_login_rejected()
    await test_bridge_reconnect_bounded()
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
            elif cmd == "attack":
                await ws.send(
                    json.dumps({"type": "combat", "text": "You hit the target."})
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "take":
                await ws.send(json.dumps({"type": "inventory", "items": []}))
                await ws.send(json.dumps(stats_view()))
            elif cmd in (
                "rest",
                "buy",
                "sell",
                "equip",
                "use",
                "craft",
                "gather",
                "heal",
                "drop",
            ):
                await ws.send(json.dumps(stats_view()))
            elif cmd == "companion":
                action = msg.get("action")
                sub = msg.get("sub")
                if action == "script" and sub == "set":
                    await ws.send(json.dumps({"type": "message", "script_set": True}))
                elif action == "script" and sub == "show":
                    await ws.send(json.dumps({"type": "message", "script": ""}))
                elif action == "script" and sub == "clear":
                    await ws.send(json.dumps({"type": "message", "script_set": False}))
                elif action == "status":
                    await ws.send(
                        json.dumps({"type": "companion", "has_companion": False})
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {"type": "error", "text": "unknown companion action"}
                        )
                    )
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

        deep = deep_json_frame()
        proc.stdin.write((deep + "\n").encode())
        await proc.stdin.drain()
        parse_error = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        assert parse_error["error"]["code"] == -32700, parse_error
        print("HARNESS_DEEP_JSON_PARSE_ERROR_OK")

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
        expected = {
            "game_look",
            "game_move",
            "game_attack",
            "game_take",
            "game_rest",
            "game_buy",
            "game_sell",
            "game_equip",
            "game_use",
            "game_craft",
            "game_gather",
            "game_heal",
            "game_drop",
            "companion_script_set",
            "companion_script_show",
            "companion_script_clear",
            "companion_status",
        }
        assert names == expected, names
        print("HARNESS_TOOLS_OK")

        extra_cases = [
            ("game_look", {"extra": 1}),
            ("game_move", {"dir": "west", "extra": 1}),
            ("game_attack", {"target": "rat", "extra": 1}),
            ("game_take", {"item": "herb", "extra": 1}),
            ("game_buy", {"item": "potion", "extra": 1}),
            ("game_craft", {"recipe": "oil", "extra": 1}),
            ("companion_script_set", {"script": "x", "extra": 1}),
        ]
        for index, (tool_name, args) in enumerate(extra_cases):
            before = len(seen)
            reply = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 100 + index,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": args},
                }
            )
            assert reply["error"]["code"] == -32602, reply
            assert len(seen) == before, reply
        print("HARNESS_TOOL_EXTRA_ARGS_OK")

        for index, value in enumerate(([], "", 0, None, False)):
            reply = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 200 + index,
                    "method": "tools/call",
                    "params": {"name": "game_look", "arguments": value},
                }
            )
            assert reply["error"]["code"] == -32602, reply
        bad_params = await rpc(
            {"jsonrpc": "2.0", "id": 299, "method": "tools/list", "params": []}
        )
        assert bad_params["error"]["code"] == -32602, bad_params
        print("HARNESS_TOOL_ARG_TYPES_OK")

        no_arguments = await rpc(
            {
                "jsonrpc": "2.0",
                "id": 300,
                "method": "tools/call",
                "params": {"name": "game_look"},
            }
        )
        assert no_arguments["result"]["isError"] is False, no_arguments
        assert {"cmd": "look"} in seen, seen
        print("HARNESS_TOOL_MISSING_ARGS_OK")

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
