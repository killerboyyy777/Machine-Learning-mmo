"""Harvest credit survives escrow returns (#1045, #1052, #1053).

Run from the repo root:  python tests/test_market_credit_restore.py

A harvested unit that leaves the pack through a market ask, a craft job or a
freight contract keeps its buyback credit when it comes back. Without the
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


def post_freight(p):
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
    return next(iter(srv.FREIGHT.values()))


def claim_freight(p, rec):
    asyncio.run(srv.cmd_freight(p, {"action": "claim", "id": rec["id"]}))


def deliver_freight(rec, hauler_name, hauler_id):
    hauler = mkplayer(hauler_name, hauler_id, room="market", gold=100)
    try:
        asyncio.run(srv.cmd_freight(hauler, {"action": "accept", "id": rec["id"]}))
        hauler.room = "smithy"
        asyncio.run(srv.cmd_freight(hauler, {"action": "deliver", "id": rec["id"]}))
        assert rec["state"] == "delivered", rec["state"]
    finally:
        unplayer(hauler)


def craft_cancel_restores_credit():
    p = mkplayer("CraftCreditCancel", 88004)
    try:
        harvest(p)
        entry = srv.get_score_entry(p.name)
        entry["craft_jobs"] = []
        asyncio.run(srv.cmd_craft(p, {"action": "start", "recipe": "arrows"}))
        assert p.inventory == [] and credits(p) == 0, (p.inventory, credits(p))
        job_id = entry["craft_jobs"][0]["id"]
        asyncio.run(srv.cmd_craft(p, {"action": "cancel", "id": job_id}))
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p)
        print("CRAFT_CANCEL_CREDIT_OK")
    finally:
        unplayer(p)


def craft_pending_refund_restores_credit():
    p = mkplayer("CraftCreditPending", 88005)
    try:
        harvest(p)
        p.inventory.extend(["rat_tail"] * (srv.INVENTORY_CAP - 1))
        entry = srv.get_score_entry(p.name)
        entry["craft_jobs"] = []
        entry["craft_pending"] = []
        asyncio.run(srv.cmd_craft(p, {"action": "start", "recipe": "arrows"}))
        p.inventory.append("rat_tail")
        assert srv._inventory_units(p) == srv.INVENTORY_CAP
        job_id = entry["craft_jobs"][0]["id"]
        asyncio.run(srv.cmd_craft(p, {"action": "cancel", "id": job_id}))
        assert IID not in p.inventory and credits(p) == 0, (p.inventory, credits(p))
        held = entry["craft_pending"][0]
        assert held["kind"] == "refund" and held["qty"] == 1, held
        assert held.get("gathered") == 1, held
        p.inventory.remove("rat_tail")
        asyncio.run(srv.cmd_craft(p, {"action": "collect"}))
        assert p.inventory.count(IID) == 1, p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p)
        print("CRAFT_PENDING_CREDIT_OK")
    finally:
        unplayer(p)


def craft_cancel_keeps_bought_units_distinct():
    p = mkplayer("CraftCreditMixed", 88006)
    try:
        harvest(p)
        p.inventory.append(IID)
        entry = srv.get_score_entry(p.name)
        entry["craft_jobs"] = []
        asyncio.run(srv.cmd_craft(p, {"action": "start", "recipe": "arrows"}))
        asyncio.run(srv.cmd_craft(p, {"action": "start", "recipe": "arrows"}))
        assert p.inventory == [] and credits(p) == 0, (p.inventory, credits(p))
        for job in list(entry["craft_jobs"]):
            asyncio.run(srv.cmd_craft(p, {"action": "cancel", "id": job["id"]}))
        assert p.inventory.count(IID) == 2, p.inventory
        assert credits(p) == 1, credits(p)
        p.room = "market"
        asyncio.run(srv.cmd_sell(p, {"item": IID}))
        asyncio.run(srv.cmd_sell(p, {"item": IID}))
        assert p.gold == DISCOUNT + RETAIL, p.gold
        print("CRAFT_MIXED_CREDIT_OK")
    finally:
        unplayer(p)


def freight_claim_delivered_restores_credit():
    p = mkplayer("FreightCreditClaimed", 88007, gold=10)
    try:
        harvest(p)
        p.room = "market"
        rec = post_freight(p)
        assert credits(p) == 0, credits(p)
        deliver_freight(rec, "FreightCreditHauler", 88008)
        claim_freight(p, rec)
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p)
        print("FREIGHT_CLAIM_DELIVERED_CREDIT_OK")
    finally:
        unplayer(p)


def freight_claim_expired_restores_credit():
    p = mkplayer("FreightCreditExpired", 88009, gold=10)
    station_at = srv._freight_station_at
    try:
        harvest(p)
        p.room = "market"
        rec = post_freight(p)
        rec["expires_at"] = time.time() - 1
        srv._freight_station_at = time.time()
        srv.prune_freight(now=time.time())
        assert rec["state"] == "expired", rec["state"]
        claim_freight(p, rec)
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p, gold_before=10)
        print("FREIGHT_CLAIM_EXPIRED_CREDIT_OK")
    finally:
        srv._freight_station_at = station_at
        unplayer(p)


def freight_claim_lost_restores_credit():
    p = mkplayer("FreightCreditLost", 88010, gold=10)
    try:
        harvest(p)
        p.room = "market"
        rec = post_freight(p)
        rec["state"] = "lost"
        rec["refund"] = rec["fee"]
        claim_freight(p, rec)
        gold_before = p.gold
        assert p.inventory == [IID], p.inventory
        assert credits(p) == 1, credits(p)
        sell_at_discount(p, gold_before=gold_before)
        print("FREIGHT_CLAIM_LOST_CREDIT_OK")
    finally:
        unplayer(p)


def freight_claim_bank_restores_credit():
    p = mkplayer("FreightCreditBank", 88011, gold=10)
    try:
        harvest(p)
        p.inventory.extend(["rat_tail"] * (srv.INVENTORY_CAP - 1))
        p.room = "market"
        rec = post_freight(p)
        p.inventory.append("rat_tail")
        assert srv._inventory_units(p) == srv.INVENTORY_CAP
        deliver_freight(rec, "FreightCreditBankHauler", 88012)
        claim_freight(p, rec)
        entry = srv.get_score_entry(p.name)
        assert entry["item_bank"] == [IID], entry["item_bank"]
        assert credits(p) == 1, credits(p)
        p.inventory.remove("rat_tail")
        p.inventory.append(entry["item_bank"].pop())
        sell_at_discount(p)
        print("FREIGHT_CLAIM_BANK_CREDIT_OK")
    finally:
        unplayer(p)


def main():
    try:
        assert DISCOUNT == 2 and RETAIL == 8, (DISCOUNT, RETAIL)
        market_cancel_restores_credit()
        market_expiry_restores_credit()
        freight_cancel_restores_credit()
        craft_cancel_restores_credit()
        craft_pending_refund_restores_credit()
        craft_cancel_keeps_bought_units_distinct()
        freight_claim_delivered_restores_credit()
        freight_claim_expired_restores_credit()
        freight_claim_lost_restores_credit()
        freight_claim_bank_restores_credit()
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
