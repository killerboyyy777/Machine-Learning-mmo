"""Harvest credit survives escrow returns (#1045).

Run from the repo root:  python tests/test_market_credit_restore.py

A harvested unit that leaves the pack through a market ask or a freight
contract keeps its buyback credit when escrow returns the unit. Without the
credit, posting and cancelling turns a free gather into a full price sale.
"""

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402


async def _noop(*args, **kwargs):
    return None


srv.send = _noop
srv.sync_room = _noop
srv.broadcast_room = _noop

IID = "iron_ore"
DISCOUNT = srv.merchant_sell_value(IID, gathered=True)
RETAIL = srv.merchant_sell_value(IID)
NODE = srv.gather_nodes["iron_ore_node"]
NODE_SAVED = (
    NODE["min_yield"],
    NODE["max_yield"],
    NODE["available"],
    NODE["respawn_at"],
)
ORDERS_SAVED = list(srv.market_orders)
BIDS_SAVED = list(srv.market_bids)
FREIGHT_SAVED = dict(srv.FREIGHT)
PRUNE_SAVED = srv._last_market_prune


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


def mkplayer(name, pid, room="smithy", gold=0):
    p = srv.Player(
        ws=FakeWS(),
        id=pid,
        name=name,
        logged_in=True,
        room=room,
        gold=gold,
    )
    srv.players[pid] = p
    srv.add_member(p)
    return p


def unplayer(p):
    srv.remove_member(p)
    srv.players.pop(p.id, None)
    srv.SCORES.pop(p.name.lower(), None)
    srv.name_owners.pop(p.name.lower(), None)


def credits(p):
    entry = srv.get_score_entry(p.name)
    return int(entry.get("gathered_units", {}).get(IID, 0) or 0)


def harvest(p):
    NODE["min_yield"] = NODE["max_yield"] = 1
    NODE["available"] = True
    NODE["respawn_at"] = None
    asyncio.run(srv.cmd_gather(p, {"node": IID}))
    assert p.inventory.count(IID) == 1, p.inventory
    assert credits(p) == 1, credits(p)


def sell_at_discount(p, gold_before=0):
    p.room = "market"
    asyncio.run(srv.cmd_sell(p, {"item": IID}))
    assert p.gold == gold_before + DISCOUNT, (p.gold, gold_before, DISCOUNT)
    assert credits(p) == 0, credits(p)


def market_cancel_restores_credit():
    p = mkplayer("MarketCreditCancel", 88001)
    try:
        harvest(p)
        srv.market_orders.clear()
        srv.market_bids.clear()
        asyncio.run(srv.cmd_market_post(p, {"item": IID, "price": 99}))
        assert p.inventory == [], p.inventory
        assert credits(p) == 0, credits(p)
        order = srv.market_orders[-1]
        assert order["seller"].lower() == p.name.lower(), order
        asyncio.run(srv.cmd_market_cancel(p, {"id": order["id"]}))
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p)
        assert order.get("gathered") is True, order
    finally:
        unplayer(p)
    print("MARKET_CANCEL_CREDIT_OK")


def market_expiry_restores_credit():
    p = mkplayer("MarketCreditExpiry", 88002)
    try:
        harvest(p)
        srv.market_orders.clear()
        srv.market_bids.clear()
        asyncio.run(srv.cmd_market_post(p, {"item": IID, "price": 99}))
        order = srv.market_orders[-1]
        order["ts"] = time.time() - srv.MARKET_ORDER_TTL_SECONDS - 10
        srv._last_market_prune = 0.0
        asyncio.run(srv.prune_market_orders(now=time.time()))
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p)
        assert order.get("gathered") is True, order
    finally:
        unplayer(p)
    print("MARKET_EXPIRY_CREDIT_OK")


def freight_cancel_restores_credit():
    p = mkplayer("FreightCreditCancel", 88003, gold=10)
    try:
        harvest(p)
        p.room = "market"
        srv.FREIGHT.clear()
        asyncio.run(
            srv.cmd_freight(
                p,
                {
                    "action": "post",
                    "destination": "smithy",
                    "fee": 10,
                    "items": [IID],
                },
            )
        )
        rec = next(iter(srv.FREIGHT.values()))
        assert rec["items"] == [IID], rec["items"]
        assert credits(p) == 0, credits(p)
        asyncio.run(srv.cmd_freight(p, {"action": "cancel", "id": rec["id"]}))
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p, gold_before=10)
        assert rec.get("gathered_items") == [IID], rec
    finally:
        unplayer(p)
    print("FREIGHT_CANCEL_CREDIT_OK")


def main():
    try:
        assert DISCOUNT == 2 and RETAIL == 8, (DISCOUNT, RETAIL)
        market_cancel_restores_credit()
        market_expiry_restores_credit()
        freight_cancel_restores_credit()
        print("MARKET_CREDIT_RESTORE_OK")
    finally:
        srv.market_orders[:] = ORDERS_SAVED
        srv.market_bids[:] = BIDS_SAVED
        srv.FREIGHT.clear()
        srv.FREIGHT.update(FREIGHT_SAVED)
        srv._last_market_prune = PRUNE_SAVED
        (
            NODE["min_yield"],
            NODE["max_yield"],
            NODE["available"],
            NODE["respawn_at"],
        ) = NODE_SAVED


main()
