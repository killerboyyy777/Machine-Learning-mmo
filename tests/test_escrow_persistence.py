"""Escrow persistence for market, commissions and guild transfers (#974).

Run from the repo root:  python tests/test_escrow_persistence.py
No live server or sockets are needed.
"""

import asyncio
import itertools
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _item():
    return next(iter(srv.ITEM_DEFS))


def market_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "market.json")
        saved = (
            srv.MARKET_FILE,
            srv.market_orders,
            srv.market_bids,
            srv._id_counter,
            srv._market_dirty,
        )
        try:
            srv.MARKET_FILE = path
            srv.market_orders = []
            srv.market_bids = []
            srv._id_counter = itertools.count(900)
            srv._market_dirty = False
            item = _item()
            ask = {
                "id": 901,
                "seller": "Seller",
                "item": item,
                "price": 7,
                "ts": 1000.0,
            }
            bid = {
                "id": 902,
                "buyer": "Buyer",
                "item": item,
                "price": 5,
                "ts": 1001.0,
            }
            srv.market_orders.append(ask)
            srv.market_bids.append(bid)
            srv.mark_market_dirty()
            assert srv._market_dirty is True
            assert srv.save_market(path) is True
            blob = _read(path)
            assert blob["version"] == srv.MARKET_VERSION
            assert blob["orders"] == [ask] and blob["bids"] == [bid]

            srv.market_orders, srv.market_bids = [], []
            srv._id_counter = itertools.count(1)
            orders, bids = srv.load_market(path)
            assert orders == [ask], orders
            assert bids == [bid], bids
            assert next(srv._id_counter) > 902
            print("MARKET_ROUND_TRIP_OK")
        finally:
            (
                srv.MARKET_FILE,
                srv.market_orders,
                srv.market_bids,
                srv._id_counter,
                srv._market_dirty,
            ) = saved


def market_invalid_row_refunds():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "market.json")
        saved_scores = dict(srv.SCORES)
        saved_dirty = srv._scores_dirty
        saved_market_dirty = srv._market_dirty
        try:
            item = _item()
            good = {
                "id": 1,
                "seller": "Good",
                "item": item,
                "price": 4,
                "ts": 1000.0,
            }
            bad_ask = {
                "id": 2,
                "seller": "RefundSeller",
                "item": item,
                "price": 0,
                "ts": 1001.0,
            }
            bad_bid = {
                "id": 3,
                "buyer": "RefundBuyer",
                "item": item,
                "price": 9,
                "ts": "not a timestamp",
            }
            _write(
                path,
                {
                    "version": srv.MARKET_VERSION,
                    "orders": [bad_ask, good],
                    "bids": [bad_bid],
                },
            )
            orders, bids = srv.load_market(path)
            assert orders == [good], orders
            assert bids == [], bids
            assert item in srv.get_score_entry("RefundSeller")["item_bank"]
            assert srv.get_score_entry("RefundBuyer")["gold_bank"] >= 9
            assert srv._scores_dirty is True
            assert srv._market_dirty is True
            cleaned = _read(path)
            assert cleaned["orders"] == [good] and cleaned["bids"] == []
            print("MARKET_INVALID_REFUND_OK")
        finally:
            srv.SCORES.clear()
            srv.SCORES.update(saved_scores)
            srv._scores_dirty = saved_dirty
            srv._market_dirty = saved_market_dirty


def market_version_and_backup():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "market.json")
        row = {
            "id": 5,
            "seller": "Later",
            "item": _item(),
            "price": 3,
            "ts": 1000.0,
        }
        _write(path, {"version": 999, "orders": [row], "bids": []})
        assert srv.load_market(path) == ([], [])
        assert srv.save_market(path) is True
        assert _read(path)["version"] == srv.MARKET_VERSION

        _write(
            path + ".1", {"version": srv.MARKET_VERSION, "orders": [row], "bids": []}
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        orders, bids = srv.load_market(path)
        assert orders == [row] and bids == []
        print("MARKET_VERSION_BACKUP_OK")


def commission_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "commissions.json")
        saved = (
            srv.COMMISSIONS_FILE,
            srv._commissions,
            srv._commission_counter,
            srv._commissions_dirty,
        )
        try:
            srv.COMMISSIONS_FILE = path
            srv._commissions = {}
            srv._commissions_dirty = False
            row = {
                "id": 12,
                "poster": "Poster",
                "target": "rat",
                "required_kills": 2,
                "reward_gold": 30,
                "reward_xp": 5,
                "escrow": 30,
                "status": "open",
                "created_ts": 10.0,
            }
            srv._commissions[12] = row
            srv.mark_commissions_dirty()
            assert srv._commissions_dirty is True
            assert srv.save_commissions(path) is True
            assert _read(path)["version"] == srv.COMMISSIONS_VERSION

            srv._commissions = {}
            loaded = srv.load_commissions(path)
            assert loaded == {12: row}, loaded
            assert next(srv._commission_counter) > 12
            print("COMMISSION_ROUND_TRIP_OK")
        finally:
            (
                srv.COMMISSIONS_FILE,
                srv._commissions,
                srv._commission_counter,
                srv._commissions_dirty,
            ) = saved


def commission_invalid_and_version():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "commissions.json")
        saved_scores = dict(srv.SCORES)
        saved_dirty = srv._scores_dirty
        saved_commissions_dirty = srv._commissions_dirty
        saved_commissions = srv._commissions
        saved_counter = srv._commission_counter
        try:
            srv._commissions = {}
            row = {
                "id": 12,
                "poster": "EscrowPoster",
                "target": "rat",
                "required_kills": 2,
                "reward_gold": 30,
                "reward_xp": 5,
                "escrow": 30,
                "status": "open",
                "created_ts": 10.0,
            }
            bad = dict(row, id=13, escrow=20, status="bogus")
            _write(
                path, {"version": srv.COMMISSIONS_VERSION, "commissions": [row, bad]}
            )
            loaded = srv.load_commissions(path)
            assert loaded == {12: row}, loaded
            assert srv.get_score_entry("EscrowPoster")["gold_bank"] >= 20
            assert srv._commissions_dirty is True
            assert _read(path)["commissions"] == [row]

            _write(path, {"version": 999, "commissions": [row]})
            assert srv.load_commissions(path) == {}
            assert srv.save_commissions(path) is True
            assert _read(path)["version"] == srv.COMMISSIONS_VERSION
            print("COMMISSION_INVALID_VERSION_OK")
        finally:
            srv.SCORES.clear()
            srv.SCORES.update(saved_scores)
            srv._scores_dirty = saved_dirty
            srv._commissions_dirty = saved_commissions_dirty
            srv._commissions = saved_commissions
            srv._commission_counter = saved_counter


def guild_transfer_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "guilds.json")
        saved = (
            srv.GUILDS_FILE,
            srv.GUILDS,
            srv.GUILD_TOMBSTONES,
            srv.GUILD_TRANSFERS,
            srv.GUILD_TRANSFER_INCOMING,
            srv._guild_transfer_ids,
            srv.GUILD_TRANSFERS_COMPLETED,
            srv.GUILD_TRANSFERS_EXPIRED,
            srv.GUILD_GOLD_TRANSFERRED,
            srv.GUILDS_FILE_BLOCKED,
        )
        try:
            srv.GUILDS_FILE = path
            srv.GUILDS_FILE_BLOCKED = False
            srv.GUILDS = {}
            srv.GUILD_TOMBSTONES = []
            srv.GUILD_TRANSFERS = {}
            srv.GUILD_TRANSFER_INCOMING = {}
            srv.GUILD_TRANSFERS_COMPLETED = 3
            srv.GUILD_TRANSFERS_EXPIRED = 1
            srv.GUILD_GOLD_TRANSFERRED = 100
            row = {
                "id": 7,
                "from": 1,
                "to": 2,
                "from_key": "alpha",
                "to_key": "beta",
                "amount": 50,
                "by": "Leader",
                "ts": 100.0,
            }
            srv.GUILD_TRANSFERS[7] = row
            srv.GUILD_TRANSFER_INCOMING[2] = [{"ts": 100.0, "from": 1}]
            assert srv.save_guilds(path) is True
            blob = _read(path)
            assert blob["transfers"]["7"] == row
            assert blob["transfer_incoming"]["2"] == [{"ts": 100.0, "from": 1}]
            assert blob["transfer_stats"]["completed"] == 3

            srv.GUILD_TRANSFERS = {}
            srv.GUILD_TRANSFER_INCOMING = {}
            srv.GUILD_TRANSFERS_COMPLETED = 0
            srv.GUILD_TRANSFERS_EXPIRED = 0
            srv.GUILD_GOLD_TRANSFERRED = 0
            srv.load_guilds(path)
            assert srv.GUILD_TRANSFERS == {7: row}, srv.GUILD_TRANSFERS
            assert srv.GUILD_TRANSFER_INCOMING == {2: [{"ts": 100.0, "from": 1}]}
            assert srv.GUILD_TRANSFERS_COMPLETED == 3
            assert srv.GUILD_TRANSFERS_EXPIRED == 1
            assert srv.GUILD_GOLD_TRANSFERRED == 100
            assert next(srv._guild_transfer_ids) > 7
            print("GUILD_TRANSFER_ROUND_TRIP_OK")
        finally:
            (
                srv.GUILDS_FILE,
                srv.GUILDS,
                srv.GUILD_TOMBSTONES,
                srv.GUILD_TRANSFERS,
                srv.GUILD_TRANSFER_INCOMING,
                srv._guild_transfer_ids,
                srv.GUILD_TRANSFERS_COMPLETED,
                srv.GUILD_TRANSFERS_EXPIRED,
                srv.GUILD_GOLD_TRANSFERRED,
                srv.GUILDS_FILE_BLOCKED,
            ) = saved


def no_double_refund_after_crash():
    """A score flush before a crash must not pay invalid-row escrow twice."""
    with tempfile.TemporaryDirectory() as d:
        market_path = os.path.join(d, "market.json")
        commission_path = os.path.join(d, "commissions.json")
        saved_scores = dict(srv.SCORES)
        saved_dirty = srv._scores_dirty
        saved_market_dirty = srv._market_dirty
        saved_commission_dirty = srv._commissions_dirty
        item = _item()
        bad_ask = {
            "id": 2,
            "seller": "CrashSeller",
            "item": item,
            "price": 0,
            "ts": 1.0,
        }
        bad_bid = {
            "id": 3,
            "buyer": "CrashBuyer",
            "item": item,
            "price": 9,
            "ts": "bad timestamp",
        }
        bad_commission = {
            "id": 4,
            "poster": "CrashPoster",
            "target": "rat",
            "required_kills": 1,
            "reward_gold": 11,
            "reward_xp": 0,
            "escrow": 11,
            "status": "bogus",
            "created_ts": 1.0,
        }
        _write(
            market_path,
            {"version": srv.MARKET_VERSION, "orders": [bad_ask], "bids": [bad_bid]},
        )
        _write(
            commission_path,
            {"version": srv.COMMISSIONS_VERSION, "commissions": [bad_commission]},
        )
        try:
            # First start: the source rows are cleaned on disk before the
            # refunds reach SCORES.
            assert srv.load_market(market_path) == ([], [])
            assert srv.load_commissions(commission_path) == {}
            assert srv._scores_dirty is True
            seller = srv.get_score_entry("CrashSeller")["item_bank"]
            buyer = srv.get_score_entry("CrashBuyer")["gold_bank"]
            poster = srv.get_score_entry("CrashPoster")["gold_bank"]
            assert seller.count(item) == 1, seller
            assert buyer == 9, buyer
            assert poster == 11, poster
            assert _read(market_path)["orders"] == []
            assert _read(market_path)["bids"] == []
            assert _read(commission_path)["commissions"] == []

            # Crash after the 5s score flush, before any other save.
            srv._scores_dirty = False

            # Next start: no source rows remain, so no second refund.
            assert srv.load_market(market_path) == ([], [])
            assert srv.load_commissions(commission_path) == {}
            assert srv._scores_dirty is False
            assert srv.get_score_entry("CrashSeller")["item_bank"].count(item) == 1
            assert srv.get_score_entry("CrashBuyer")["gold_bank"] == 9
            assert srv.get_score_entry("CrashPoster")["gold_bank"] == 11
            print("NO_DOUBLE_REFUND_AFTER_CRASH_OK")
        finally:
            srv.SCORES.clear()
            srv.SCORES.update(saved_scores)
            srv._scores_dirty = saved_dirty
            srv._market_dirty = saved_market_dirty
            srv._commissions_dirty = saved_commission_dirty


def save_loops_flush():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            market_path = os.path.join(d, "market.json")
            commission_path = os.path.join(d, "commissions.json")
            saved = (
                srv.MARKET_FILE,
                srv.COMMISSIONS_FILE,
                srv.MARKET_SAVE_SECONDS,
                srv.COMMISSIONS_SAVE_SECONDS,
                srv.market_orders,
                srv._commissions,
                srv._market_dirty,
                srv._commissions_dirty,
            )
            try:
                srv.MARKET_FILE = market_path
                srv.COMMISSIONS_FILE = commission_path
                srv.MARKET_SAVE_SECONDS = 0.01
                srv.COMMISSIONS_SAVE_SECONDS = 0.01
                srv.market_orders = [
                    {
                        "id": 41,
                        "seller": "LoopSeller",
                        "item": _item(),
                        "price": 2,
                        "ts": 1000.0,
                    }
                ]
                srv._commissions = {}
                srv._market_dirty = True
                srv._commissions_dirty = True
                tasks = [
                    asyncio.create_task(srv.market_save_loop()),
                    asyncio.create_task(srv.commissions_save_loop()),
                ]
                await asyncio.sleep(0.05)
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                assert os.path.exists(market_path)
                assert os.path.exists(commission_path)
                assert srv._market_dirty is False
                assert srv._commissions_dirty is False
            finally:
                (
                    srv.MARKET_FILE,
                    srv.COMMISSIONS_FILE,
                    srv.MARKET_SAVE_SECONDS,
                    srv.COMMISSIONS_SAVE_SECONDS,
                    srv.market_orders,
                    srv._commissions,
                    srv._market_dirty,
                    srv._commissions_dirty,
                ) = saved

    asyncio.run(run())
    print("SAVE_LOOPS_FLUSH_OK")


if __name__ == "__main__":
    market_round_trip()
    market_invalid_row_refunds()
    market_version_and_backup()
    commission_round_trip()
    commission_invalid_and_version()
    guild_transfer_round_trip()
    no_double_refund_after_crash()
    save_loops_flush()
    print("ALL_ESCROW_PERSISTENCE_OK")
