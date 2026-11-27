"""Ground piles stay bounded and keep their value (#975).

Run from the repo root:  python tests/test_ground_bounds.py
"""

import asyncio
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
    stamps = sum(len(times) for times in srv.ground_added_at[ROOM].values())
    assert stamps == len(srv.room_items[ROOM]), srv.ground_added_at[ROOM]
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
    srv.ground_added_at[ROOM][stale] = [now - srv.GROUND_ITEM_TTL_SECONDS - 1]
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

    # Duplicate ids carry one stamp per copy (#1058). Three drops at one
    # instant and a fourth later must all expire with value credited once.
    srv.GROUND_ITEM_CAP = 8
    srv.room_items[ROOM] = []
    srv.ground_added_at[ROOM] = {}
    srv.room_gold[ROOM] = 0
    real_time = srv.time.time
    base = real_time()
    try:
        srv.time.time = lambda: base
        for _ in range(3):
            srv._add_ground(ROOM, ITEM)
        srv.time.time = lambda: base + 10
        srv._add_ground(ROOM, ITEM)
        assert len(srv.room_items[ROOM]) == 4, srv.room_items[ROOM]
        srv.time.time = lambda: base + 10 + srv.GROUND_ITEM_TTL_SECONDS
        assert srv.prune_ground_items() == 4
        assert not srv.room_items[ROOM], srv.room_items[ROOM]
        assert not srv.ground_added_at[ROOM], srv.ground_added_at[ROOM]
        assert srv.room_gold[ROOM] == 4 * VALUE, srv.room_gold[ROOM]
    finally:
        srv.time.time = real_time
    print("GROUND_DUPLICATE_TTL_OK")

    # Station freight lifts units without _remove_ground (#1061). Its stamp
    # must leave with the unit or the fresh copy expires on the old stamp.
    saved_freight = (
        srv.FREIGHT_STATION_ROOMS,
        dict(srv.FREIGHT),
        srv._freight_station_at,
    )
    saved_smithy = (
        list(srv.room_items["smithy"]),
        dict(srv.ground_added_at["smithy"]),
    )
    srv.FREIGHT_STATION_ROOMS = (ROOM, "smithy")
    srv.FREIGHT.clear()
    srv._freight_station_at = 0.0
    srv.room_items["smithy"] = []
    srv.ground_added_at["smithy"] = {}
    srv.room_items[ROOM] = []
    srv.ground_added_at[ROOM] = {}
    srv.room_gold[ROOM] = 0
    base = real_time()
    try:
        srv.time.time = lambda: base
        srv._add_ground(ROOM, ITEM)
        srv.tick_station_freight(base + 1)
        assert ITEM not in srv.room_items[ROOM], srv.room_items[ROOM]
        assert not srv.ground_added_at[ROOM], srv.ground_added_at[ROOM]
        srv.time.time = lambda: base + 10
        srv._add_ground(ROOM, ITEM)
        srv.time.time = lambda: base + srv.GROUND_ITEM_TTL_SECONDS + 1
        assert srv.prune_ground_items() == 0
        assert ITEM in srv.room_items[ROOM], srv.room_items[ROOM]
        srv.time.time = lambda: base + 10 + srv.GROUND_ITEM_TTL_SECONDS
        assert srv.prune_ground_items() == 1
        assert not srv.room_items[ROOM], srv.room_items[ROOM]
        assert srv.room_gold[ROOM] == VALUE, srv.room_gold[ROOM]
    finally:
        srv.time.time = real_time
        srv.FREIGHT_STATION_ROOMS = saved_freight[0]
        srv.FREIGHT.clear()
        srv.FREIGHT.update(saved_freight[1])
        srv._freight_station_at = saved_freight[2]
        srv.room_items["smithy"] = saved_smithy[0]
        srv.ground_added_at["smithy"] = saved_smithy[1]
    print("GROUND_FREIGHT_STAMP_OK")

    # WORLD seeds have no stamp. Freight lifts the seed at index 0 and must
    # leave the stamp of a fresh copy of the same id in place.
    seed_room, seed_item = "smithy", "iron_ore"
    assert srv.room_items[seed_room].count(seed_item) == 1
    assert not srv.ground_added_at[seed_room]
    saved_seed = (
        srv.FREIGHT_STATION_ROOMS,
        srv.FREIGHT_STATION_MAX_UNITS,
        dict(srv.FREIGHT),
        srv._freight_station_at,
        list(srv.room_items[seed_room]),
        dict(srv.ground_added_at[seed_room]),
        srv.room_gold.get(seed_room, 0),
    )
    srv.FREIGHT_STATION_ROOMS = (seed_room, ROOM)
    srv.FREIGHT_STATION_MAX_UNITS = 1
    srv.FREIGHT.clear()
    srv._freight_station_at = 0.0
    srv.room_items[ROOM] = []
    srv.ground_added_at[ROOM] = {}
    srv.room_gold[seed_room] = 0
    base = real_time()
    try:
        srv.time.time = lambda: base
        srv._add_ground(seed_room, seed_item)
        assert srv.room_items[seed_room].count(seed_item) == 2
        srv.tick_station_freight(base + 1)
        assert srv.room_items[seed_room].count(seed_item) == 1
        assert len(srv.ground_added_at[seed_room][seed_item]) == 1
        srv.time.time = lambda: base + srv.GROUND_ITEM_TTL_SECONDS
        assert srv.prune_ground_items() == 1
        assert not srv.room_items[seed_room], srv.room_items[seed_room]
        assert not srv.ground_added_at[seed_room], srv.ground_added_at[seed_room]
        assert srv.room_gold[seed_room] == srv.ITEM_DEFS[seed_item]["value"]
    finally:
        srv.time.time = real_time
        srv.FREIGHT_STATION_ROOMS = saved_seed[0]
        srv.FREIGHT_STATION_MAX_UNITS = saved_seed[1]
        srv.FREIGHT.clear()
        srv.FREIGHT.update(saved_seed[2])
        srv._freight_station_at = saved_seed[3]
        srv.room_items[seed_room] = saved_seed[4]
        srv.ground_added_at[seed_room] = saved_seed[5]
        srv.room_gold[seed_room] = saved_seed[6]
    print("GROUND_SEED_FREIGHT_STAMP_OK")

    # #1065: one marked and one unmarked copy of the same id share a floor.
    # Losing the unmarked copy by cap or TTL must not clear the survivor's mark.
    async def _noop(*args, **kwargs):
        return None

    send_saved, sync_saved, broadcast_saved = (
        srv.send,
        srv.sync_room,
        srv.broadcast_room,
    )
    srv.send = srv.sync_room = srv.broadcast_room = _noop
    MARK_LIST = isinstance(srv.ground_gathered["market"], list)
    CHECK_ITEM = "iron_ore"
    DISCOUNT = srv.merchant_sell_value(CHECK_ITEM, gathered=True)
    RETAIL = srv.merchant_sell_value(CHECK_ITEM)
    assert DISCOUNT < RETAIL, (DISCOUNT, RETAIL)

    def _floor_mark_survivor(room, lose, label):
        d = srv.dungeon_for_room(room)
        floor = d.floors.get(srv.floor_from_room(room)) if d else None
        ground_saved = list(srv._ground_items(room))
        stamps_saved = dict(srv.ground_added_at.get(room, {}))
        marks_saved = srv.ground_gathered.get(room)
        marks_copy = None
        if marks_saved is not None:
            marks_copy = list(marks_saved) if MARK_LIST else dict(marks_saved)
        cap_saved = srv.GROUND_ITEM_CAP
        try:
            for iid in list(srv._ground_items(room)):
                srv._remove_ground(room, iid)
            srv.ground_added_at[room] = {}
            srv.ground_gathered[room] = [] if MARK_LIST else {}
            srv._add_ground(room, CHECK_ITEM)
            srv._add_ground(room, CHECK_ITEM)
            srv._mark_ground_gathered(room, CHECK_ITEM)
            assert srv._ground_items(room).count(CHECK_ITEM) == 2
            if lose == "evict":
                srv.GROUND_ITEM_CAP = 2
                srv._add_ground(room, CHECK_ITEM)
            else:
                srv.ground_added_at[room][CHECK_ITEM][0] = (
                    srv.time.time() - srv.GROUND_ITEM_TTL_SECONDS - 1
                )
                assert srv.prune_ground_items() == 1
            picker = srv.Player(
                id=9301,
                name="FloorPicker",
                logged_in=True,
                room=room,
                gold=0,
            )
            srv.players[picker.id] = picker
            srv.add_member(picker)
            try:
                asyncio.run(srv.cmd_take(picker, {"item": CHECK_ITEM}))
                assert picker.inventory == [CHECK_ITEM], picker.inventory
                picker.room = "market"
                asyncio.run(srv.cmd_sell(picker, {"item": CHECK_ITEM}))
                assert picker.gold == DISCOUNT, (picker.gold, DISCOUNT, RETAIL)
                print(f"FLOOR_MARK_SURVIVES_{label.upper()}_OK")
            finally:
                srv.remove_member(picker)
                srv.players.pop(picker.id, None)
                srv.SCORES.pop(picker.name.lower(), None)
                srv.name_owners.pop(picker.name.lower(), None)
        finally:
            srv.GROUND_ITEM_CAP = cap_saved
            if floor is not None:
                floor.items[:] = ground_saved
            else:
                srv.room_items[room] = ground_saved
            srv.ground_added_at[room] = stamps_saved
            if marks_saved is None:
                srv.ground_gathered.pop(room, None)
            else:
                srv.ground_gathered[room] = marks_copy

    dungeon = srv.Dungeon(party_id=9901065)
    srv.dungeons[dungeon.id] = dungeon
    try:
        _floor_mark_survivor("market", "evict", "evict")
        _floor_mark_survivor("market", "expire", "expire")
        _floor_mark_survivor(dungeon.room_id(1), "evict", "dungeon_evict")
    finally:
        srv.dungeons.pop(dungeon.id, None)
        srv.send, srv.sync_room, srv.broadcast_room = (
            send_saved,
            sync_saved,
            broadcast_saved,
        )
finally:
    srv.room_items[ROOM] = saved[0]
    srv.ground_added_at[ROOM] = saved[1]
    srv.room_gold[ROOM] = saved[2]
    srv.GROUND_ITEM_CAP = saved[3]
    srv.GROUND_ITEM_TTL_SECONDS = saved[4]
    srv.GROUND_GOLD_CAP = saved[5]
    srv.tax_treasury = saved[6]

print("ALL_GROUND_BOUNDS_OK")
