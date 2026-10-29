"""Offline MCP harness test: complex game goal using full tool set."""

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parent.parent


def room_view(room_id, npcs=None, items=None, gold=0):
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
        "npcs": npcs or [],
        "items": items or [],
        "gold": gold,
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
    current = {"room": "town_square", "gold": 50, "inventory": [], "quests": []}

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
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
                await ws.send(
                    json.dumps(
                        room_view(
                            current["room"],
                            npcs=[{"id": "rat", "name": "Giant Rat"}],
                            items=[],
                            gold=0,
                        )
                    )
                )
            elif cmd == "move":
                direction = msg.get("dir")
                current["room"] = "old_shop" if direction == "west" else "town_square"
                # In old_shop: rat NPC, no items by default
                if current["room"] == "old_shop":
                    await ws.send(
                        json.dumps(
                            room_view(
                                "old_shop",
                                npcs=[{"id": "rat", "name": "Giant Rat"}],
                                items=[],
                                gold=0,
                            )
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(room_view("town_square", npcs=[], items=[], gold=0))
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "attack":
                target = msg.get("target")
                # Deal damage - reduce rat HP conceptually, just report success
                await ws.send(
                    json.dumps({"type": "combat", "text": f"You hit the {target}."})
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "take":
                item = msg.get("item")
                if item and item in ["herb", "potion"]:
                    current["inventory"].append(item)
                    current["gold"] = current.get("gold", 0) + 1
                await ws.send(json.dumps(stats_view()))
            elif cmd == "rest":
                # Rest costs 5 gold, recovers HP
                gold = current.get("gold", 0)
                if gold >= 5:
                    current["gold"] = gold - 5
                    await ws.send(
                        json.dumps({"type": "effect", "text": "Recovered HP."})
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": "Not enough gold."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "buy":
                item = msg.get("item")
                gold = current.get("gold", 0)
                # Simple economy: potion costs 5 gold, herb costs 3 gold
                prices = {"potion": 5, "herb": 3}
                price = prices.get(item, 0)
                if price > 0 and gold >= price:
                    current["gold"] = gold - price
                    if item not in current.get("inventory", []):
                        current.setdefault("inventory", []).append(item)
                    await ws.send(
                        json.dumps({"type": "effect", "text": f"Bought {item}."})
                    )
                elif price > 0:
                    await ws.send(
                        json.dumps(
                            {"type": "error", "text": f"Not enough gold. Need {price}."}
                        )
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": f"Unknown item {item}."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "sell":
                item = msg.get("item")
                gold = current.get("gold", 0)
                # Sell: get half gold back
                if item in current.get("inventory", []):
                    current["inventory"] = [
                        i for i in current.get("inventory", []) if i != item
                    ]
                    current["gold"] = gold + 2  # get 2 gold back
                    await ws.send(
                        json.dumps(
                            {"type": "effect", "text": f"Sold {item} for 2 gold."}
                        )
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": f"You don't have {item}."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "equip":
                item = msg.get("item")
                inventory = current.get("inventory", [])
                if item in inventory:
                    await ws.send(
                        json.dumps({"type": "effect", "text": f"Equipped {item}."})
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": f"You don't have {item}."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "use":
                item = msg.get("item")
                inventory = current.get("inventory", [])
                if item in inventory:
                    if item == "potion":
                        current["hp"] = min(20, current.get("hp", 20) + 5)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "effect",
                                    "text": "Used potion, recovered 5 HP.",
                                }
                            )
                        )
                    elif item == "herb":
                        current["hp"] = min(20, current.get("hp", 20) + 3)
                        await ws.send(
                            json.dumps(
                                {"type": "effect", "text": "Used herb, recovered 3 HP."}
                            )
                        )
                    else:
                        await ws.send(
                            json.dumps({"type": "effect", "text": f"Used {item}."})
                        )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": f"You don't have {item}."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "craft":
                recipe = msg.get("recipe")
                # Simple crafting: some recipes work, others don't
                valid_recipes = {"sharpening_oil", "fortitude_tonic"}
                if recipe in valid_recipes:
                    await ws.send(
                        json.dumps({"type": "effect", "text": f"Crafted {recipe}."})
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {"type": "error", "text": f"Unknown recipe {recipe}."}
                        )
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "gather":
                # Gather gives a herb
                current.setdefault("inventory", []).append("herb")
                current["gold"] = current.get("gold", 0) + 1
                await ws.send(
                    json.dumps({"type": "effect", "text": "Gathered a herb."})
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "heal":
                gold = current.get("gold", 0)
                if gold >= 10:
                    current["gold"] = gold - 10
                    current["hp"] = 20  # full heal
                    await ws.send(json.dumps({"type": "effect", "text": "Full heal."}))
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": "Not enough gold."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "drop":
                item = msg.get("item")
                inventory = current.get("inventory", [])
                if item in inventory:
                    current["inventory"] = [i for i in inventory if i != item]
                    await ws.send(
                        json.dumps({"type": "effect", "text": f"Dropped {item}."})
                    )
                else:
                    await ws.send(
                        json.dumps({"type": "error", "text": f"You don't have {item}."})
                    )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "companion_script_set":
                script = msg.get("script")
                await ws.send(
                    json.dumps({"type": "companion", "text": f"Script set: {script}."})
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "companion_script_show":
                await ws.send(
                    json.dumps(
                        {"type": "companion", "text": "Current script: (none set)."}
                    )
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "companion_script_clear":
                await ws.send(
                    json.dumps({"type": "companion", "text": "Script cleared."})
                )
                await ws.send(json.dumps(stats_view()))
            elif cmd == "companion_status":
                await ws.send(
                    json.dumps({"type": "companion", "text": "Companion is ready."})
                )
                await ws.send(json.dumps(stats_view()))
            else:
                await ws.send(json.dumps({"type": "error", "text": "unknown command"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harness",
        "--url",
        f"ws://127.0.0.1:{port}",
        "--name",
        "HarnessTest",
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def rpc(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        if "id" not in message:
            return None
        line = await asyncio.wait_for(proc.stdout.readline(), 10)
        return json.loads(line)

    # Init
    await rpc(
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
    await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # 1. Look around the starting room
    looked = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "game_look", "arguments": {}},
        }
    )
    assert looked["result"]["isError"] == False, f"LOOK failed: {looked}"
    print("LOOK: OK - room state received")

    # 2. Move west to Old Shop
    moved = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "game_move", "arguments": {"dir": "west"}},
        }
    )
    assert moved["result"]["isError"] == False, f"MOVE failed: {moved}"
    print("MOVE: OK - moved to Old Shop")

    # 3. Attack the rat NPC
    attacked = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "game_attack", "arguments": {"target": "rat"}},
        }
    )
    assert attacked["result"]["isError"] == False, f"ATTACK failed: {attacked}"
    print("ATTACK: OK - attacked rat")

    # 4. Take a herb from the ground (if available)
    taken = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "game_take", "arguments": {"item": "herb"}},
        }
    )
    assert taken["result"]["isError"] == False, f"TAKE failed: {taken}"
    print("TAKE: OK - took herb")

    # 5. Rest to recover HP
    rested = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "game_rest", "arguments": {}},
        }
    )
    assert rested["result"]["isError"] == False, f"REST failed: {rested}"
    print("REST: OK - rested")

    # 6. Buy a potion
    bought = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "game_buy", "arguments": {"item": "potion"}},
        }
    )
    assert bought["result"]["isError"] == False, f"BUY failed: {bought}"
    print("BUY: OK - bought potion")

    # 7. Use the potion
    used = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "game_use", "arguments": {"item": "potion"}},
        }
    )
    assert used["result"]["isError"] == False, f"USE failed: {used}"
    print("USE: OK - used potion")

    # 8. Sell the potion back
    sold = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "game_sell", "arguments": {"item": "potion"}},
        }
    )
    assert sold["result"]["isError"] == False, f"SELL failed: {sold}"
    print("SELL: OK - sold potion back")

    # 9. Gather resources
    gathered = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "game_gather", "arguments": {}},
        }
    )
    assert gathered["result"]["isError"] == False, f"GATHER failed: {gathered}"
    print("GATHER: OK - gathered resources")

    # 10. Craft sharpening oil
    crafted = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "game_craft", "arguments": {"recipe": "sharpening_oil"}},
        }
    )
    assert crafted["result"]["isError"] == False, f"CRAFT failed: {crafted}"
    print("CRAFT: OK - crafted sharpening_oil")

    # 11. Check companion script set
    script_set = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "companion_script_set",
                "arguments": {"script": "harvest_loop"},
            },
        }
    )
    assert (
        script_set["result"]["isError"] == False
    ), f"COMPANION_SCRIPT_SET failed: {script_set}"
    print("COMPANION_SCRIPT_SET: OK - set script")

    # 12. Show companion script
    script_show = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "companion_script_show", "arguments": {}},
        }
    )
    assert (
        script_show["result"]["isError"] == False
    ), f"COMPANION_SCRIPT_SHOW failed: {script_show}"
    print("COMPANION_SCRIPT_SHOW: OK - showed script")

    # 13. Clear companion script
    script_clear = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "companion_script_clear", "arguments": {}},
        }
    )
    assert (
        script_clear["result"]["isError"] == False
    ), f"COMPANION_SCRIPT_CLEAR failed: {script_clear}"
    print("COMPANION_SCRIPT_CLEAR: OK - cleared script")

    # 14. Check companion status
    status = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": "companion_status", "arguments": {}},
        }
    )
    assert status["result"]["isError"] == False, f"COMPANION_STATUS failed: {status}"
    print("COMPANION_STATUS: OK - checked status")

    # 15. Equip an item (herb, which was gathered)
    equipped = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {"name": "game_equip", "arguments": {"item": "herb"}},
        }
    )
    assert equipped["result"]["isError"] == False, f"EQUIP failed: {equipped}"
    print("EQUIP: OK - equipped herb")

    # 16. Drop an item
    dropped = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
            "params": {"name": "game_drop", "arguments": {"item": "herb"}},
        }
    )
    assert dropped["result"]["isError"] == False, f"DROP failed: {dropped}"
    print("DROP: OK - dropped herb")

    # 17. Heal at Sister Maren (costs 10 gold)
    healed = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {"name": "game_heal", "arguments": {}},
        }
    )
    assert healed["result"]["isError"] == False, f"HEAL failed: {healed}"
    print("HEAL: OK - healed")

    proc.stdin.close()
    await proc.wait()
    server.close()
    await server.wait_closed()

    print("\n=== ALL INTERACTIONS COMPLETED SUCCESSFULLY ===")


if __name__ == "__main__":
    asyncio.run(main())
