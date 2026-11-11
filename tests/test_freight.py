"""Freight keeps gold and goods conserved (#503).

Run from the repo root:  python tests/test_freight.py

Custody transfers at pickup and the package drops where the hauler dies, so
the two things this suite proves are: gold only ever moves between the sender,
the hauler and escrow, and an item in a package is the same item the sender
took out of their pack -- it is never duplicated and never vanishes.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv

SENT = []


async def _send(player, payload):
    SENT.append(payload)


async def _noop(*args, **kwargs):
    return None


srv.send = _send
srv.broadcast_room = _noop
srv.sync_room = _noop

_ITEMS = sorted(srv.ITEM_DEFS)
ITEM, OTHER = _ITEMS[0], _ITEMS[1]
ORIGIN = "market"
DEST = "smithy"


def _player(name, room, gold, inventory=(), pid=900):
    return srv.Player(
        ws=None,
        id=pid,
        name=name,
        logged_in=True,
        hp=20,
        max_hp=20,
        room=room,
        gold=gold,
        inventory=list(inventory),
    )


def _reset():
    srv.FREIGHT.clear()
    srv._freight_station_at = 0.0
    for key in srv.FREIGHT_STATS:
        srv.FREIGHT_STATS[key] = 0
    SENT.clear()


def _run(player, **msg):
    asyncio.run(srv.cmd_freight(player, msg))


def _only_contract():
    assert len(srv.FREIGHT) == 1, srv.FREIGHT
    return next(iter(srv.FREIGHT.values()))


# --- post, accept, deliver: gold is conserved, goods reach the receiver -------

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM, OTHER], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)

_run(sender, action="post", destination=DEST, fee=20, items=[ITEM])
rec = _only_contract()
deposit = rec["deposit"]
assert rec["state"] == "open"
assert sender.gold == 80, sender.gold
assert ITEM not in sender.inventory
assert rec["items"] == [ITEM]
print("POST_TAKES_GOODS_OK")

_run(hauler, action="accept", id=rec["id"], contract_id=rec["id"])
assert rec["state"] == "carried"
assert hauler.gold == 50 - deposit, hauler.gold
assert rec["hauler"] == "Hauler"
print("ACCEPT_TAKES_DEPOSIT_OK", deposit)

# A second hauler cannot take a contract that is already carried.
thief = _player("Thief", ORIGIN, 50, [], pid=903)
_run(thief, action="accept", id=rec["id"])
assert rec["hauler"] == "Hauler", rec["hauler"]
print("NO_DOUBLE_ACCEPT_OK")

hauler.room = DEST
_run(hauler, action="deliver", id=rec["id"])
assert rec["state"] == "delivered", rec["state"]
assert hauler.gold == 50 + 20, hauler.gold
assert sender.gold == 80
assert sender.gold + hauler.gold == 150
print("DELIVER_PAYS_FEE_OK")

# The receiver claims the goods; the sender's gold never came back.
sender.room = DEST
_run(sender, action="claim", id=rec["id"])
assert ITEM in sender.inventory, sender.inventory
assert rec["id"] not in srv.FREIGHT
print("CLAIM_RETURNS_GOODS_OK")

# --- cancel before pickup returns everything ---------------------------------

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
_run(sender, action="post", destination=DEST, fee=15, items=[ITEM])
rec = _only_contract()
_run(sender, action="cancel", id=rec["id"])
assert sender.gold == 100, sender.gold
assert ITEM in sender.inventory
assert rec["id"] not in srv.FREIGHT
print("CANCEL_RETURNS_ALL_OK")

# --- a hauler death drops the package and refunds the poster -----------------

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)
srv.players["Hauler"] = hauler
_run(sender, action="post", destination=DEST, fee=20, items=[ITEM])
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
before = sum(1 for iid in srv._ground_items("deep_forest") if iid == ITEM)

srv._freight_release_on_death(hauler, "deep_forest")
after = sum(1 for iid in srv._ground_items("deep_forest") if iid == ITEM)
assert after == before + 1, (before, after)
assert rec["state"] == "lost"
assert rec["refund"] == 20 + rec["deposit"], rec["refund"]
assert "items" not in rec or not rec["items"]
print("DEATH_DROPS_PACKAGE_OK")

gold_before = sender.gold
_run(sender, action="claim", id=rec["id"])
assert sender.gold == gold_before + 20 + deposit, sender.gold
srv._remove_ground("deep_forest", ITEM)
srv.players.pop("Hauler", None)
print("LOSS_REFUNDS_POSTER_OK")

# --- a station consigns stock it already holds -------------------------------

_reset()
srv._add_ground(ORIGIN, ITEM)
srv._add_ground(ORIGIN, OTHER)
srv.tick_station_freight(10000.0)
station = _only_contract()
assert station["poster_kind"] == "station", station
assert station["origin"] in srv.FREIGHT_STATION_ROOMS, station["origin"]
assert station["destination"] != station["origin"]
assert len(station["items"]) >= 1

hauler = _player("Hauler", station["origin"], 100, [], pid=902)
srv.players["Hauler"] = hauler
_run(hauler, action="accept", id=station["id"])
hauler.room = station["destination"]
gold_before = hauler.gold
units = len(station["items"])
_run(hauler, action="deliver", id=station["id"])
assert station["state"] == "delivered"
assert hauler.gold == 100 + station["fee"], hauler.gold
landed = sum(1 for iid in srv._ground_items(station["destination"]) if iid)
assert landed >= units, (landed, units)
print("STATION_CONSIGNMENT_DELIVERS_OK", station["fee"])

for iid in list(srv._ground_items(station["destination"])):
    srv._remove_ground(station["destination"], iid)
srv.players.pop("Hauler", None)

# --- refusal paths -----------------------------------------------------------

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
_run(sender, action="post", destination=DEST, fee=20, items=[ITEM])
rec = _only_contract()
_run(sender, action="accept", id=rec["id"])
assert rec["state"] == "open"
print("OWN_CONTRACT_REFUSED_OK")

far = _player("Far", "tannery", 100, [], pid=904)
_run(far, action="accept", id=rec["id"])
assert rec["state"] == "open"
print("WRONG_ROOM_REFUSED_OK")

poor = _player("Poor", ORIGIN, 0, [], pid=905)
_run(poor, action="post", destination=DEST, fee=5, items=[OTHER])
assert len(srv.FREIGHT) == 1
print("NO_GOLD_REFUSED_OK")

print("FREIGHT_CONSERVATION_OK")
