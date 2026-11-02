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
    {
        "name": "game_attack",
        "title": "Attack a target",
        "description": "Attack a target NPC in the current room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name of the NPC to attack.",
                }
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_take",
        "title": "Take an item",
        "description": "Take an item from the ground.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to take.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_rest",
        "title": "Rest",
        "description": "Rest to recover HP (costs gold).",
        "inputSchema": MINIMAL,
    },
    {
        "name": "game_buy",
        "title": "Buy an item",
        "description": "Buy an item from a merchant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to buy.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_sell",
        "title": "Sell an item",
        "description": "Sell an item to a merchant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to sell.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_equip",
        "title": "Equip an item",
        "description": "Equip an item from inventory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to equip.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_use",
        "title": "Use an item",
        "description": "Use an item from inventory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to use.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_craft",
        "title": "Craft an item",
        "description": "Craft an item using a recipe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe": {
                    "type": "string",
                    "description": "Name of the recipe.",
                }
            },
            "required": ["recipe"],
            "additionalProperties": False,
        },
    },
    {
        "name": "game_gather",
        "title": "Gather",
        "description": "Gather resources from the room.",
        "inputSchema": MINIMAL,
    },
    {
        "name": "game_heal",
        "title": "Heal",
        "description": "Full heal at Sister Maren's tile (costs gold).",
        "inputSchema": MINIMAL,
    },
    {
        "name": "game_drop",
        "title": "Drop an item",
        "description": "Drop an item from inventory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Name of the item to drop.",
                }
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    {
        "name": "companion_script_set",
        "title": "Set companion script",
        "description": "Set the companion's DroneLang script.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "DroneLang script to set.",
                }
            },
            "required": ["script"],
            "additionalProperties": False,
        },
    },
    {
        "name": "companion_script_show",
        "title": "Show companion script",
        "description": "Show the companion's current script.",
        "inputSchema": MINIMAL,
    },
    {
        "name": "companion_script_clear",
        "title": "Clear companion script",
        "description": "Clear the companion's script.",
        "inputSchema": MINIMAL,
    },
    {
        "name": "companion_status",
        "title": "Companion status",
        "description": "Get the companion's status.",
        "inputSchema": MINIMAL,
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
        elif name == "game_attack":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                raise RpcError(-32602, "game_attack takes a non-empty 'target'")
            envelope = await self._call(
                "attack", {"target": target}, expect=("room", "stats", "death")
            )
        elif name == "game_take":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_take takes a non-empty 'item'")
            envelope = await self._call(
                "take", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "game_rest":
            envelope = await self._call("rest", expect=("room", "stats", "death"))
        elif name == "game_buy":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_buy takes a non-empty 'item'")
            envelope = await self._call(
                "buy", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "game_sell":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_sell takes a non-empty 'item'")
            envelope = await self._call(
                "sell", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "game_equip":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_equip takes a non-empty 'item'")
            envelope = await self._call(
                "equip", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "game_use":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_use takes a non-empty 'item'")
            envelope = await self._call(
                "use", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "game_craft":
            recipe = arguments.get("recipe")
            if not isinstance(recipe, str) or not recipe.strip():
                raise RpcError(-32602, "game_craft takes a non-empty 'recipe'")
            envelope = await self._call(
                "craft", {"recipe": recipe}, expect=("room", "stats", "death")
            )
        elif name == "game_gather":
            envelope = await self._call("gather", expect=("room", "stats", "death"))
        elif name == "game_heal":
            envelope = await self._call("heal", expect=("room", "stats", "death"))
        elif name == "game_drop":
            item = arguments.get("item")
            if not isinstance(item, str) or not item.strip():
                raise RpcError(-32602, "game_drop takes a non-empty 'item'")
            envelope = await self._call(
                "drop", {"item": item}, expect=("room", "stats", "death")
            )
        elif name == "companion_script_set":
            script = arguments.get("script")
            if not isinstance(script, str) or not script.strip():
                raise RpcError(
                    -32602, "companion_script_set takes a non-empty 'script'"
                )
            envelope = await self._call(
                "companion_script_set", {"script": script}, expect=("companion",)
            )
        elif name == "companion_script_show":
            envelope = await self._call("companion_script_show", expect=("companion",))
        elif name == "companion_script_clear":
            envelope = await self._call("companion_script_clear", expect=("companion",))
        elif name == "companion_status":
            envelope = await self._call("companion_status", expect=("companion",))
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
