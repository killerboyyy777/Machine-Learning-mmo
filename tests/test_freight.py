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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402

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
assert rec["hauler"] == "hauler"
print("ACCEPT_TAKES_DEPOSIT_OK", deposit)

# A second hauler cannot take a contract that is already carried.
thief = _player("Thief", ORIGIN, 50, [], pid=903)
_run(thief, action="accept", id=rec["id"])
assert rec["hauler"] == "hauler", rec["hauler"]
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
srv.tick_station_freight(time.time())
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

# --- the rush bonus returns on every terminal path where it was not earned --
# #713: fee+bonus are escrowed up front, but open expiry, carried expiry and
# hauler death all used to refund only the fee (and deposit), silently keeping
# the bonus. Each case posts WITH a bonus and checks the poster's own gold.

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
_run(
    sender,
    action="post",
    destination=DEST,
    fee=20,
    bonus=5,
    bonus_seconds=1,
    items=[ITEM],
)
rec = _only_contract()
assert rec["bonus"] == 5, rec
assert sender.gold == 100 - 20 - 5, sender.gold
now = rec["expires_at"] + 1
srv._freight_station_at = now  # keep station consignments out of this probe
srv.prune_freight(now)
assert rec["state"] == "expired", rec["state"]
assert rec["refund"] == 20 + 5, rec["refund"]
gold_before = sender.gold
_run(sender, action="claim", id=rec["id"])
assert sender.gold == gold_before + 25, sender.gold
print("OPEN_EXPIRY_RETURNS_BONUS_OK")

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)
srv.players["Hauler"] = hauler
_run(
    sender,
    action="post",
    destination=DEST,
    fee=20,
    bonus=5,
    bonus_seconds=1,
    items=[ITEM],
)
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
deposit = rec["deposit"]
now = rec["expires_at"] + 1
srv._freight_station_at = now
srv.prune_freight(now)
assert rec["state"] == "lost", rec["state"]
assert rec["refund"] == 20 + deposit + 5, rec["refund"]
gold_before = sender.gold
_run(sender, action="claim", id=rec["id"])
assert sender.gold == gold_before + 25 + deposit, sender.gold
for iid in list(srv._ground_items(hauler.room)):
    srv._remove_ground(hauler.room, iid)
srv.players.pop("Hauler", None)
print("CARRIED_EXPIRY_RETURNS_BONUS_OK")

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)
srv.players["Hauler"] = hauler
_run(
    sender,
    action="post",
    destination=DEST,
    fee=20,
    bonus=5,
    bonus_seconds=1,
    items=[ITEM],
)
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
deposit = rec["deposit"]
srv._freight_release_on_death(hauler, "deep_forest")
assert rec["state"] == "lost", rec["state"]
assert rec["refund"] == 20 + deposit + 5, rec["refund"]
gold_before = sender.gold
_run(sender, action="claim", id=rec["id"])
assert sender.gold == gold_before + 25 + deposit, sender.gold
srv._remove_ground("deep_forest", ITEM)
srv.players.pop("Hauler", None)
print("DEATH_RETURNS_BONUS_OK")

# --- #905: a late bonus refund on a delivered row is flushed exactly once -----

_reset()
sender = _player("LateSender", ORIGIN, 100, [ITEM], pid=906)
hauler = _player("LateHauler", ORIGIN, 50, [], pid=907)
_run(
    sender,
    action="post",
    destination=DEST,
    fee=20,
    bonus=5,
    bonus_seconds=1,
    items=[ITEM],
)
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
rec["bonus_until"] = time.time() - 1  # delivered after the rush window closed
hauler.room = DEST
_run(hauler, action="deliver", id=rec["id"])
assert rec["state"] == "delivered", rec["state"]
assert rec["refund"] == 5, rec["refund"]
assert sender.gold == 100 - 20 - 5, sender.gold
gold_before = sender.gold
assert srv._freight_flush_refunds(sender) == 5
assert sender.gold == gold_before + 5, sender.gold
assert rec["refund"] == 0, rec["refund"]
assert srv._freight_flush_refunds(sender) == 0
sender.room = DEST
_run(sender, action="claim", id=rec["id"])
assert sender.gold == gold_before + 5, sender.gold  # claim pays nothing twice
print("LATE_DELIVERY_REFUND_FLUSH_OK")

# --- #905: retention pays an unclaimed delivered refund before deleting -------

_reset()
sender = _player("RetentionSender", ORIGIN, 100, [ITEM], pid=908)
hauler = _player("RetentionHauler", ORIGIN, 50, [], pid=909)
srv.add_member(sender)  # an online poster is paid directly, not banked
_run(
    sender,
    action="post",
    destination=DEST,
    fee=20,
    bonus=5,
    bonus_seconds=1,
    items=[ITEM],
)
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
rec["bonus_until"] = time.time() - 1
hauler.room = DEST
_run(hauler, action="deliver", id=rec["id"])
assert rec["refund"] == 5, rec["refund"]
now = rec["delivered_at"] + srv.FREIGHT_DELIVERED_RETENTION_SECONDS + 1
srv._freight_station_at = now  # keep station consignments out of this probe
gold_before = sender.gold
srv.prune_freight(now)
assert rec["id"] not in srv.FREIGHT, srv.FREIGHT
assert sender.gold == gold_before + 5, sender.gold
assert srv._freight_flush_refunds(sender) == 0  # already settled, never paid twice
for iid in list(srv._ground_items(DEST)):
    srv._remove_ground(DEST, iid)
srv.remove_member(sender)
print("PRUNE_PAYS_DELIVERED_REFUND_OK")

# --- #907: station freight never leaves a collectable death refund -----------

_reset()
srv._add_ground(ORIGIN, ITEM)
srv._add_ground(ORIGIN, OTHER)
srv.tick_station_freight(10000.0)
station = _only_contract()
assert station["poster_kind"] == "station", station
# The fake 10000.0 station clock is historical; give this contract a live
# deadline so the expiry guard from #906 does not reject the hauler.
station["expires_at"] = time.time() + 60
hauler = _player("StationHauler", station["origin"], 100, [], pid=910)
srv.players["StationHauler"] = hauler
_run(hauler, action="accept", id=station["id"])
srv._freight_release_on_death(hauler, "deep_forest")
assert station["state"] == "lost", station["state"]
assert station["refund"] == 0, station["refund"]
station["refund"] = 50  # even a stray station refund is not a player's to take
namesake = _player(station["poster"], ORIGIN, 500, [], pid=911)
gold_before = namesake.gold
_run(namesake, action="list")
assert namesake.gold == gold_before, namesake.gold
assert station["refund"] == 50, station["refund"]
for iid in list(srv._ground_items("deep_forest")):
    srv._remove_ground("deep_forest", iid)
srv.players.pop("StationHauler", None)
print("STATION_DEATH_NO_REFUND_OK")

# --- #1024: a station's namesake cannot cancel or claim its freight -----------

_reset()
for rid in srv.FREIGHT_STATION_ROOMS:
    for iid in list(srv._ground_items(rid)):
        srv._remove_ground(rid, iid)
srv._add_ground(ORIGIN, ITEM)
srv._add_ground(ORIGIN, OTHER)
srv.tick_station_freight(time.time())
station = _only_contract()
assert station["poster_kind"] == "station", station
namesake = _player(station["poster"], ORIGIN, 500, [], pid=912)
goods = list(station["items"])
gold0 = namesake.gold
SENT.clear()
_run(namesake, action="cancel", id=station["id"])
assert station["id"] in srv.FREIGHT, srv.FREIGHT
assert station["state"] == "open", station["state"]
assert station["items"] == goods, station["items"]
assert namesake.gold == gold0 and not namesake.inventory, namesake.gold
assert any(
    m.get("type") == "error" and "station freight" in m.get("text", "").lower()
    for m in SENT
), SENT
print("STATION_NAMESAKE_CANCEL_REFUSED_OK")

station["state"] = "expired"  # without the kind guard, claim would pay out here
SENT.clear()
_run(namesake, action="claim", id=station["id"])
assert station["id"] in srv.FREIGHT, srv.FREIGHT
assert namesake.gold == gold0 and not namesake.inventory, namesake.gold
assert any(
    m.get("type") == "error" and "station freight" in m.get("text", "").lower()
    for m in SENT
), SENT
print("STATION_NAMESAKE_CLAIM_REFUSED_OK")

# Positive control: the same name on a player contract can still be cancelled.
_reset()
real = _player(station["poster"], ORIGIN, 100, [ITEM], pid=913)
_run(real, action="post", destination=DEST, fee=15, items=[ITEM])
rec = _only_contract()
assert rec["poster_kind"] == "player", rec
_run(real, action="cancel", id=rec["id"])
assert rec["id"] not in srv.FREIGHT, srv.FREIGHT
assert ITEM in real.inventory, real.inventory
assert real.gold == 100, real.gold
print("PLAYER_NAMESAKE_CANCEL_OK")

# --- expired contracts are refused until the prune settles them ---------------
# #906: accept and deliver checked only state, so a contract past its expiry
# could still be acted on during the prune window.

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)
_run(sender, action="post", destination=DEST, fee=20, items=[ITEM])
rec = _only_contract()
rec["expires_at"] = time.time() - 1
_run(hauler, action="accept", id=rec["id"])
assert rec["state"] == "open", rec["state"]
assert rec["hauler"] is None, rec["hauler"]
assert hauler.gold == 50, hauler.gold
print("EXPIRED_ACCEPT_REFUSED_OK")

srv._freight_station_at = time.time()
srv.prune_freight(time.time())
assert rec["state"] == "expired", rec["state"]
assert rec["refund"] == 20, rec["refund"]
print("EXPIRED_ACCEPT_PRUNE_SETTLES_OK")

_reset()
sender = _player("Sender", ORIGIN, 100, [ITEM], pid=901)
hauler = _player("Hauler", ORIGIN, 50, [], pid=902)
srv.players["Hauler"] = hauler
_run(sender, action="post", destination=DEST, fee=20, items=[ITEM])
rec = _only_contract()
_run(hauler, action="accept", id=rec["id"])
deposit = rec["deposit"]
assert hauler.gold == 50 - deposit, hauler.gold
hauler.room = DEST
rec["expires_at"] = time.time() - 1
_run(hauler, action="deliver", id=rec["id"])
assert rec["state"] == "carried", rec["state"]
assert hauler.gold == 50 - deposit, hauler.gold
print("EXPIRED_DELIVER_REFUSED_OK")

srv._freight_station_at = time.time()
srv.prune_freight(time.time())
assert rec["state"] == "lost", rec["state"]
assert rec["refund"] == 20 + deposit, rec["refund"]
for iid in list(srv._ground_items(hauler.room)):
    srv._remove_ground(hauler.room, iid)
srv.players.pop("Hauler", None)
print("EXPIRED_DELIVER_PRUNE_SETTLES_OK")

print("FREIGHT_CONSERVATION_OK")
