"""Minimal MCP stdio server: JSON-RPC 2.0, newline-delimited."""

import asyncio
import json
import sys
import traceback

from harness import __version__
from harness.bridge import BridgeError

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
MINIMAL = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = [
    {
        "name": "game_look",
        "title": "Look at the current room",
        "description": (
            "Read the character's current room: description, exits, NPCs, "
            "ground items, loose gold, players and gatherables. Also refreshes "
            "the game://room resource. Read-only."
        ),
        "inputSchema": MINIMAL,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "game_move",
        "title": "Move through an exit",
        "description": (
            "Move the character through one exit of the current room. Use the "
            "exit names from game://room (for example west, up, down, enter). "
            "Returns the new room and fresh state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dir": {
                    "type": "string",
                    "description": "Exit name from game://room.",
                }
            },
            "required": ["dir"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        },
    },
]

RESOURCES = [
    {
        "uri": "game://room",
        "name": "Current room",
        "description": "Last room view seen by the bridge, cached and read-only.",
        "mimeType": "application/json",
    }
]


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class McpServer:
    def __init__(self, bridge):
        self.bridge = bridge

    async def serve(self):
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":
                return
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except (TypeError, ValueError):
                self._write(_error(None, -32700, "parse error"))
                continue
            response = await self._handle(request)
            if response is not None:
                self._write(response)

    async def _handle(self, request):
        if not isinstance(request, dict):
            return _error(None, -32600, "invalid request")
        request_id = request.get("id")
        has_id = "id" in request
        method = request.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "invalid request") if has_id else None
        if not has_id:
            return None

        try:
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = await self._tools_call(params)
            elif method == "resources/list":
                result = {"resources": RESOURCES}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                result = self._resources_read(params)
            else:
                raise RpcError(-32601, f"unknown method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _error(request_id, -32603, f"internal error: {exc}")

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": "textmmo-harness", "version": __version__},
            "instructions": (
                "Text MMO harness for one character. Call game_look before "
                "acting and use game://room for exits. One action per call; "
                "calls are serialized because the game protocol has no request ids."
            ),
        }

    async def _tools_call(self, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments must be an object")
        if name == "game_look":
            if arguments:
                raise RpcError(-32602, "game_look takes no arguments")
            envelope = await self._call("look", expect=("room",))
        elif name == "game_move":
            extra = set(arguments) - {"dir"}
            direction = arguments.get("dir")
            if extra or not isinstance(direction, str) or not direction.strip():
                raise RpcError(-32602, "game_move takes one non-empty 'dir'")
            envelope = await self._call(
                "move", {"dir": direction}, expect=("room", "stats", "death")
            )
        else:
            raise RpcError(-32602, f"unknown tool: {name}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(envelope, ensure_ascii=True),
                }
            ],
            "isError": not envelope["ok"],
        }

    async def _call(self, cmd, params=None, *, expect):
        try:
            return await self.bridge.command(cmd, params, expect=expect)
        except BridgeError as exc:
            return {
                "ok": False,
                "command": cmd,
                "events": [],
                "state": self.bridge.summary(),
                "error": str(exc),
                "truncated": False,
            }

    def _resources_read(self, params):
        uri = params.get("uri")
        if uri != "game://room":
            raise RpcError(-32602, f"unknown resource: {uri}")
        payload = self.bridge.resource_room()
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=True),
                }
            ]
        }

    def _write(self, response):
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def _error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
