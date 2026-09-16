"""Live end-to-end protocol test (needs a live server).

Prerequisites: fresh `scores.json` (`{}`), server started with
`TEXTMMO_GM_SEED=700`, then run from the repo root:
    python tests/test_live.py
Covers: GM-seeded treasury math, gear-up, graveyard + instanced dungeon
entry, sealed-floor rules, floor-1 retreat, market trade + tax, GM spends,
party invite/accept/shared dungeon, and the dashboard snapshot.

Note: the raw protocol requires FULL direction names (north/south/...,
up/down/enter) -- shorthand like `w` is rejected by the server.
"""
import asyncio, json, time
import websockets

URI = "ws://127.0.0.1:8765"
GM_URI = "ws://127.0.0.1:8767"

async def recv(ws, timeout=3.0, want_type=None):
    deadline = time.time() + timeout
    out = []
    while time.time() < deadline:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), max(0.05, deadline - time.time())))
        except asyncio.TimeoutError:
            break
        out.append(msg)
        if want_type and msg.get("type") == want_type:
            return msg
    return out if not want_type else None


async def send(ws, obj):
    await ws.send(json.dumps(obj))


async def drain(ws, timeout=1.0):
    msgs = []
    while True:
        m = await recv(ws, timeout=0.1)
        if not m:
            break
        msgs.extend(m if isinstance(m, list) else [m])
    return msgs


async def wait_room(ws, timeout=5.0):
    r = await recv(ws, want_type="room", timeout=timeout)
    if r is not None:
        return r
    await drain(ws, 1.0)
    return None


async def move_for_room(ws, dir, timeout=5.0, retries=2):
    """Move, then wait for the room event; re-sync with `look` on timeout.

    CI runners can stall a broadcast past a single window, so a missing
    room is re-requested instead of failing outright. Returns None only
    when the server stays silent through every attempt."""
    await send(ws, {"cmd": "move", "dir": dir})
    for _ in range(retries + 1):
        room = await recv(ws, want_type="room", timeout=timeout)
        if room is not None:
            return room
        await send(ws, {"cmd": "look"})
    return None


async def fight_until_room(ws, target_room, dir_to_target, max_rounds=40):
    """Navigate to a room, attacking hostile NPCs (wolf/ghost/rat) that block
    the way. Dies are OK: death respawns you in town_square and this loops."""
    for _ in range(max_rounds):
        await send(ws, {"cmd": "look"})
        room = await wait_room(ws)
        if room is None:
            continue
        print(f"  [{target_room}] at {room['id']} npcs={room.get('npcs')}")
        if room["id"] == target_room:
            return room
        for npc in list(room.get("npcs", [])):
            for _ in range(20):
                await send(ws, {"cmd": "attack", "target": npc})
                await drain(ws, 0.3)
                await send(ws, {"cmd": "look"})
                r2 = await wait_room(ws)
                if r2 is None:
                    break
                room = r2
                if npc not in room.get("npcs", []):
                    break
            if room["id"] == target_room:
                return room
            if any("healing" in i.lower() for i in room.get("items", [])):
                await send(ws, {"cmd": "take", "item": "healing"})
                await drain(ws, 0.5)
        await send(ws, {"cmd": "move", "dir": dir_to_target})
        moved = await drain(ws, 1.0)
        for m in moved[:6]:
            print(f"    move-> {m.get('type')} {str(m.get('text', m.get('id', '')))[:50]}")
        await drain(ws, 0.3)
    raise RuntimeError(f"Could not reach {target_room}")


async def main():
    A = await websockets.connect(URI)
    B = await websockets.connect(URI)
    # GM commands go through the dedicated loopback GM stream, never the game port
    GM = await websockets.connect(GM_URI)

    async def gm_send(cmd, **kwargs):
        await GM.send(json.dumps({"cmd": cmd, **kwargs}))
        return await recv(GM, timeout=5.0)

    # ---- A: login + seed gold via GM (loopback, treasury at TEXTMMO_GM_SEED=700)
    await send(A, {"cmd": "login", "name": "LiveA"})
    ent = {}
    for m in await recv(A, timeout=5.0):
        ent.setdefault(m["type"], m)
    assert ent.get("room", {}).get("id") == "town_square"
    assert ent.get("welcome", {}).get("protocol_version") == 1, ent.get("welcome")
    msgs = await gm_send("gm_reward", player="LiveA", gold=30)
    assert any("Treasury now 670.0" in m.get("text", "") for m in msgs), msgs
    print("LOGIN_GM_SEED_OK")

    # ---- A: buy a sword (attack 7) so fights are quick
    await send(A, {"cmd": "move", "dir": "west"})
    await wait_room(A)
    await send(A, {"cmd": "buy", "item": "rusty"})
    await drain(A, 0.8)
    await send(A, {"cmd": "equip", "item": "rusty"})
    await drain(A, 0.5)
    print("GEAR_UP_OK")

    # ---- A: grab the free shield from the shop for later market sale
    await send(A, {"cmd": "move", "dir": "east"})     # back to town_square
    await wait_room(A)
    await send(A, {"cmd": "move", "dir": "east"})     # old_shop
    await wait_room(A)
    await send(A, {"cmd": "take", "item": "shield"})
    await drain(A, 1.0)
    await send(A, {"cmd": "move", "dir": "west"})     # back to town_square
    await wait_room(A)

    # ---- A: reach graveyard (fight the wandering wolf if it shows again)
    await fight_until_room(A, "graveyard", "south")
    await send(A, {"cmd": "look"})
    room = await recv(A, want_type="room", timeout=5.0)
    assert room is not None and room["id"] == "graveyard"
    assert "enter" in room["exits"]
    print("GRAVEYARD_OK")

    # ---- A: enter dungeon (solo auto-party, floor 1)
    droom = await move_for_room(A, "enter")
    assert droom is not None and droom["is_dungeon"] is True and droom["dungeon_floor"] == 1, droom
    assert set(droom["exits"]) == {"up"}
    assert droom["party_size"] >= 1
    print("DUNGEON_ENTER_OK")

    await send(A, {"cmd": "move", "dir": "down"})
    # First error only (no full-window drain): every idle second in the
    # dungeon is a combat round tanked without fighting back.
    err = await recv(A, want_type="error", timeout=5.0)
    assert err is not None and "sealed" in err.get("text", "").lower(), err
    print("SEALED_DOWN_BLOCKED")

    # Floor 1 retreat is allowed even while sealed. Resilient to dying
    # while idle: a death respawns in town_square, so re-sync with `look`
    # and, if respawned, walk back and re-enter before retrying.
    room = None
    for _ in range(4):
        await send(A, {"cmd": "look"})
        cur = await recv(A, want_type="room", timeout=5.0)
        if cur is None:
            continue
        if cur["id"] == "graveyard":
            room = cur
            break
        if cur.get("is_dungeon"):
            room = await move_for_room(A, "up")
            if room is not None and room["id"] == "graveyard":
                break
            continue  # died mid-retreat; loop re-syncs
        await fight_until_room(A, "graveyard", "south")
        await move_for_room(A, "enter")
    assert room is not None and room["id"] == "graveyard", room
    print("FLOOR1_RETREAT_OK")

    # ---- B: login + seed gold, gear up, meet A at graveyard
    await send(B, {"cmd": "login", "name": "LiveB"})
    await drain(B, 1.5)
    msgs = await gm_send("gm_reward", player="LiveB", gold=250)
    assert any("Treasury now 420.0" in m.get("text", "") for m in msgs), msgs
    await send(B, {"cmd": "move", "dir": "west"})
    await wait_room(B)
    await send(B, {"cmd": "buy", "item": "rusty"})
    await drain(B, 0.8)
    await send(B, {"cmd": "equip", "item": "rusty"})
    await drain(B, 0.5)
    await send(B, {"cmd": "move", "dir": "east"})
    await wait_room(B)
    await fight_until_room(B, "graveyard", "south")
    print("B_GRAVEYARD_OK")

    # ---- Market: A posts the shield from ANYWHERE, B buys (market works anywhere)
    await send(A, {"cmd": "market_post", "item": "shield", "price": 50})
    await drain(A, 1.0)
    await send(B, {"cmd": "market_buy"})
    got = await recv(B, timeout=5.0)
    assert any(m.get("type") == "message" and "You buy" in m.get("text", "") for m in got), got
    await send(A, {"cmd": "market_list"})
    ml = await recv(A, want_type="market", timeout=5.0)
    assert ml is not None
    # 420 (after B's gold) + 5 (tax on 50 sale) = 425
    assert ml["tax_treasury"] == 425.0 and ml["tax_collected_lifetime"] == 5.0, (ml["tax_treasury"], ml)
    assert len(ml["orders"]) == 0
    print("MARKET_TRADE_TAX_OK")

    # ---- GM spends tax treasury
    got = await gm_send("gm_boss", room="deep_forest", strength=1)
    assert any("spawned" in m.get("text", "") for m in got), got      # 425-100 = 325
    got = await gm_send("gm_buff", type="xp", minutes=1)
    assert any("Treasury now 275.0" in m.get("text", "") for m in got), got
    print("GM_SPEND_OK")

    # ---- Party: invite + accept in graveyard
    # Either character may have died (respawning in town_square) while
    # idling in the hostile graveyard during B's setup + market + GM steps,
    # so make sure both are actually standing there before inviting.
    await fight_until_room(A, "graveyard", "south")
    await fight_until_room(B, "graveyard", "south")
    await send(A, {"cmd": "party_invite", "target": "LiveB"})
    await drain(A, 1.0)
    await send(B, {"cmd": "party_accept"})
    await drain(B, 1.0)
    await send(A, {"cmd": "party_info"})
    pi = await recv(A, want_type="party", timeout=5.0)
    assert pi is not None and len(pi["members"]) == 2 and pi["leader"] == "LiveA", pi
    print("PARTY_OK")

    # party dungeon instance is shared
    da = await move_for_room(A, "enter")
    assert da is not None and da["is_dungeon"] and da["dungeon_floor"] == 1
    assert da["party_size"] == 2, da
    print("PARTY_DUNGEON_SHARED")

    # ---- Abrupt disconnect cleans up: the same name can log back in
    # (regression: cleanup skipped on writer CancelledError leaked the
    # name lock, failing relogin with "already in use" forever)
    C = await websockets.connect(URI)
    await send(C, {"cmd": "login", "name": "RelogProbe"})
    got = await recv(C, timeout=5.0)
    assert any(m.get("type") == "welcome" for m in got), got
    await C.close()
    await asyncio.sleep(1.5)  # let the server run disconnect cleanup
    C2 = await websockets.connect(URI)
    await send(C2, {"cmd": "login", "name": "RelogProbe"})
    got2 = await recv(C2, timeout=5.0)
    assert any(m.get("type") == "welcome" for m in got2), got2
    print("RELOGIN_OK")
    await C2.close()

    # ---- Dashboard snapshot has the new sections
    import urllib.request
    state = json.loads(urllib.request.urlopen("http://127.0.0.1:8766/api/state", timeout=3).read())
    assert isinstance(state["dungeons"], list) and len(state["dungeons"]) >= 1
    assert state["market"]["treasury"] == 275.0, state["market"]
    assert state["market"]["collected_lifetime"] == 5.0, state["market"]
    assert "buffs" in state and "bosses" in state
    assert state["server"]["ws_port"] == 8765
    town = next(r for r in state["rooms"] if r["id"] == "town_square")
    assert {e["dir"] for e in town["exits"]} == {"north", "east", "south", "west"}
    assert any(e["to"] == "market" and e["to_name"] == "Market" for e in town["exits"])
    pl = {pp["name"]: pp for pp in state["players"]}
    assert pl["LiveA"]["level"] == 1
    assert any(b["name"].startswith("Elite") for b in state["bosses"]), state["bosses"]
    print("DASHBOARD_SNAPSHOT_OK")

    print("LIVE_ALL_OK")
    await GM.close()
    await A.close()
    await B.close()


asyncio.run(main())
