"""Ground piles stay bounded and keep their value (#975).

Run from the repo root:  python tests/test_ground_bounds.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402

ROOM = "market"
ITEM = next(iid for iid, spec in srv.ITEM_DEFS.items() if spec.get("value", 0) > 0)
VALUE = int(srv.ITEM_DEFS[ITEM]["value"])

saved = (
    list(srv.room_items[ROOM]),
    dict(srv.ground_added_at[ROOM]),
    srv.room_gold.get(ROOM, 0),
    srv.GROUND_ITEM_CAP,
    srv.GROUND_ITEM_TTL_SECONDS,
    srv.GROUND_GOLD_CAP,
    srv.tax_treasury,
)
try:
    srv.room_items[ROOM] = []
    srv.ground_added_at[ROOM] = {}
    srv.room_gold[ROOM] = 0
    srv.GROUND_ITEM_CAP = 3
    srv.GROUND_GOLD_CAP = 4 * VALUE
    treasury_before = srv.tax_treasury

    # More drops than the cap: the pile, its age index and the serialized
    # room all stay at the cap, and evicted items leave their value as gold.
    for _ in range(8):
        srv._add_ground(ROOM, ITEM)
    assert len(srv.room_items[ROOM]) == 3, srv.room_items[ROOM]
    assert len(srv.ground_added_at[ROOM]) == 1
    assert srv.room_gold[ROOM] == 4 * VALUE, (srv.room_gold[ROOM], VALUE)
    assert srv.tax_treasury == treasury_before + VALUE, "overflow was destroyed"
    room = next(r for r in srv.world_snapshot()["rooms"] if r["id"] == ROOM)
    assert len(room["items"]) == 3 and room["gold"] == 4 * VALUE, room
    bounded = len(json.dumps(room))
    for _ in range(50):
        srv._add_ground(ROOM, ITEM)
    room = next(r for r in srv.world_snapshot()["rooms"] if r["id"] == ROOM)
    assert len(srv.room_items[ROOM]) == 3, srv.room_items[ROOM]
    assert len(json.dumps(room)) == bounded, "serialized room grew past cap"
    print("GROUND_CAP_OK")

    # TTL: a stale tracked drop expires on a running server and banks its
    # value, while a fresh drop in the same room is untouched.
    now = srv.time.time()
    stale, fresh = (
        ITEM,
        next(
            i
            for i in srv.ITEM_DEFS
            if i != ITEM and srv.ITEM_DEFS[i].get("value", 0) > 0
        ),
    )
    srv.room_items[ROOM] = []
    srv.ground_added_at[ROOM] = {}
    srv.room_gold[ROOM] = 0
    for iid in (stale, fresh):
        srv._add_ground(ROOM, iid)
    srv.ground_added_at[ROOM][stale] = now - srv.GROUND_ITEM_TTL_SECONDS - 1
    gold_before = srv.room_gold[ROOM]
    assert srv.prune_ground_items(now) == 1
    assert stale not in srv.room_items[ROOM] and fresh in srv.room_items[ROOM]
    assert srv.room_gold[ROOM] == gold_before + VALUE, (
        gold_before,
        srv.room_gold[ROOM],
    )
    assert stale not in srv.ground_added_at[ROOM]
    assert fresh in srv.ground_added_at[ROOM]
    print("GROUND_TTL_OK")

    # A pickup removes the age index: a later sweep must not credit value
    # for an item that is already in a player's pack.
    srv._remove_ground(ROOM, fresh)
    gold_before = srv.room_gold[ROOM]
    assert not srv.ground_added_at[ROOM], srv.ground_added_at[ROOM]
    assert srv.prune_ground_items(now + 10 * srv.GROUND_ITEM_TTL_SECONDS) == 0
    assert srv.room_gold[ROOM] == gold_before
    print("GROUND_PICKUP_NO_DOUBLE_CREDIT_OK")
finally:
    srv.room_items[ROOM] = saved[0]
    srv.ground_added_at[ROOM] = saved[1]
    srv.room_gold[ROOM] = saved[2]
    srv.GROUND_ITEM_CAP = saved[3]
    srv.GROUND_ITEM_TTL_SECONDS = saved[4]
    srv.GROUND_GOLD_CAP = saved[5]
    srv.tax_treasury = saved[6]

print("ALL_GROUND_BOUNDS_OK")
