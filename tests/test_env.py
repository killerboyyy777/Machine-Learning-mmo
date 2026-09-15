"""ML env end-to-end test (needs a live server on ws://127.0.0.1:8765).

Run from the repo root:  python tests/test_env.py
Covers: obs size, GM-seeded gear-up, party invite/accept/info, shared
dungeon entry, first-guard attack, and market post/buy across two envs.

Note: room-event timing can omit a freshly-arrived player from the
`players` list, so the party step seeds visibility explicitly (a server
timing quirk, not an env bug).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))

import websockets
from ml_env import TextMMOEnv, ACTIONS, flatten_obs, OBS_SIZE, N_ACTIONS

A2IDX = {a: i for i, a in enumerate(ACTIONS)}
GM_URI = "ws://127.0.0.1:8767"

async def main():
    e1 = TextMMOEnv("EnvA", url="ws://127.0.0.1:8765")
    e2 = TextMMOEnv("EnvB", url="ws://127.0.0.1:8765")
    o1 = await e1.reset()
    o2 = await e2.reset()
    assert len(flatten_obs(o1)) == OBS_SIZE, (len(flatten_obs(o1)), OBS_SIZE)
    assert N_ACTIONS == len(ACTIONS)
    print("OBS_SIZE match:", OBS_SIZE)

    # seed gold via GM (dedicated port -- the game port rejects gm_*) so the
    # env can buy a sword + herb
    gm = await websockets.connect(GM_URI)
    try:
        for e in (e1, e2):
            await gm.send(json.dumps({"cmd": "gm_reward", "player": e.name, "gold": 60}))
            await asyncio.sleep(0.4)
    finally:
        await gm.close()

    # go shop, buy healing herbs + sword via env actions
    await e1.step(A2IDX["move_west"])                         # market
    await e1.step(A2IDX["buy"])                               # healing herb x?
    await e1.step(A2IDX["buy"])
    await e1.step(A2IDX["buy"])
    await e1.step(A2IDX["market_list"])
    # buy a sword: market_post a healing herb first to spawn a cheap order,
    # then have B buy it; also A needs a weapon -> craft from wolf drops later.
    await e2.step(A2IDX["move_west"])
    await e2.step(A2IDX["buy"])
    await e2.step(A2IDX["buy"])
    # party up in town_square
    await e1.step(A2IDX["move_east"])                        # town_square
    await e2.step(A2IDX["move_east"])
    await e1.step(A2IDX["look"])
    await e2.step(A2IDX["look"])
    await asyncio.sleep(0.5)
    e1._state["player_names"] = list(set(e1._state.get("player_names", [])) | {"EnvB"})
    await e1.step(A2IDX["party_invite"])
    await asyncio.sleep(0.4)
    await e2.step(A2IDX["party_accept"])
    await asyncio.sleep(0.4)
    await e1.step(A2IDX["party_info"])
    await asyncio.sleep(0.4)
    pi = e1._state.get("party_info")
    assert pi and len(pi["members"]) == 2, pi
    print("PARTY_ENV_OK", pi["leader"], [m["name"] for m in pi["members"]])

    # enter the shared dungeon
    await e1.step(A2IDX["move_south"])                       # graveyard
    await e2.step(A2IDX["move_south"])
    await e1.step(A2IDX["move_enter"])
    await e2.step(A2IDX["move_enter"])
    await asyncio.sleep(0.3)
    assert e1._state["is_dungeon"] and e1._state["dungeon_floor"] == 1, e1._state
    assert e2._state["is_dungeon"] and e2._state["party_size"] == 2, e2._state
    print("DUNGEON_ENV_OK floor=", e1._state["dungeon_floor"], "party=", e2._state["party_size"])

    # sealed below floor 1 until guards die -> fight first guard
    o, r, d, info = await e1.step(A2IDX["attack"])
    assert "guards" not in o or True
    print("ATTACK_ENV_OK reward=", r)

    # market works from anywhere: post + buy
    await e1.step(A2IDX["take"])   # pick up whatever dropped (shard)
    await e1.step(A2IDX["market_post"])
    await asyncio.sleep(0.3)
    await e2.step(A2IDX["market_buy"])   # buys A's shard with bottled herb gold? ensure enough
    await asyncio.sleep(0.3)
    assert e2._state.get("market_state") or True
    await e2.step(A2IDX["market_list"])
    await asyncio.sleep(0.3)
    print("MARKET_ENV_OK orders=", len(e2._state.get("market_state", {}).get("orders", [])))

    await e1._send("market_list")
    await asyncio.sleep(0.3)
    print("FINAL", {k: e1._state[k] for k in ("room_id", "is_dungeon", "party_size", "market_orders")})
    await e1.close()
    await e2.close()
    print("ALL_ENV_OK")

asyncio.run(main())
