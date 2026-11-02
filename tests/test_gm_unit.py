"""GM command unit tests: treasury flows with fake players (no server).

Run from the repo root:  python tests/test_gm_unit.py
Covers the treasury paths test_server_unit skips: gm_reward gold/item
delivery to online/offline/missing targets, no-spend-on-error (treasury
is debited only when something is actually delivered), gm_tables counts,
and gm_kick delivery. Complements the buff/boss/announce/heal/teleport/
slay coverage already in test_server_unit.py.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeHeaders:
    def __init__(self, origin=None):
        self.origin = origin

    def get(self, key, default=None):
        if str(key).lower() == "origin" and self.origin is not None:
            return self.origin
        return default


class FakeGMWS:
    def __init__(self, origin=None):
        self.remote_address = ("127.0.0.1", 1234)
        self.closed = False
        self.inbox = asyncio.Queue()
        self.ended = False
        self.request = SimpleNamespace(headers=FakeHeaders(origin))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.ended and self.inbox.empty():
            raise StopAsyncIteration
        return await self.inbox.get()

    async def send(self, payload):
        self.sent = getattr(self, "sent", []) + [payload]

    async def close(self):
        self.closed = True


def mkplayer(name, pid, gold=0):
    p = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
    p.gold = gold
    p.room = "town_square"
    srv.players[pid] = p
    srv.add_member(p)
    return p


def unplayer(p):
    try:
        srv.remove_member(p)
    except Exception:
        pass
    srv.players.pop(p.id, None)
    srv.SCORES.pop(p.name.lower(), None)


async def main():
    inbox = []

    async def fake_send(p, payload):
        inbox.append(payload if isinstance(payload, dict) else json.loads(payload))

    srv.send = fake_send
    gm = mkplayer("GMBot", 40001)
    rich = None
    t0, tl0 = srv.tax_treasury, srv.tax_collected_lifetime
    srv.tax_treasury = 1000.0

    try:
        # --- gold to an online player: debited once, delivered, messaged ---
        rich = mkplayer("Paid1", 40002)
        await srv.cmd_gm_reward(gm, {"gold": 50, "player": "Paid1"})
        assert rich.gold == 50, rich.gold
        assert srv.tax_treasury == 950.0, srv.tax_treasury
        assert any("Treasury now 950.0" in m.get("text", "") for m in inbox), inbox[-3:]
        print("GM_GOLD_ONLINE_OK")

        # --- gold to an offline name: banked, not paid live ---
        await srv.cmd_gm_reward(gm, {"gold": 30, "player": "Gone1"})
        assert srv.get_score_entry("Gone1")["gold_bank"] == 30
        assert srv.tax_treasury == 920.0, srv.tax_treasury
        print("GM_GOLD_OFFLINE_OK")

        # --- no target, no spend: failure delivers nothing, debits nothing ---
        before = srv.tax_treasury
        await srv.cmd_gm_reward(gm, {"gold": 50})
        assert srv.tax_treasury == before, (before, srv.tax_treasury)
        assert inbox[-1]["type"] == "error", inbox[-1]
        print("GM_GOLD_NO_TARGET_OK")

        # --- unknown item: failure debits nothing ---
        before = srv.tax_treasury
        await srv.cmd_gm_reward(gm, {"item": "no_such_thing_xyz", "player": "Paid1"})
        assert srv.tax_treasury == before, (before, srv.tax_treasury)
        assert inbox[-1]["type"] == "error", inbox[-1]
        print("GM_ITEM_UNKNOWN_OK")

        # --- unknown player for item: failure debits nothing ---
        before = srv.tax_treasury
        await srv.cmd_gm_reward(gm, {"item": "healing_herb", "player": "NobodyHere"})
        assert srv.tax_treasury == before, (before, srv.tax_treasury)
        assert inbox[-1]["type"] == "error", inbox[-1]
        print("GM_ITEM_NO_PLAYER_OK")

        # --- item to online player: treasury debited by merchant value ---
        herb_value = srv.ITEM_DEFS["healing_herb"].get("value", 1) or 1
        before = srv.tax_treasury
        n_inv = len(rich.inventory)
        await srv.cmd_gm_reward(gm, {"item": "healing_herb", "player": "Paid1"})
        assert len(rich.inventory) == n_inv + 1 and rich.inventory[-1] == "healing_herb"
        assert srv.tax_treasury == round(before - herb_value, 2), (before, srv.tax_treasury)
        print("GM_ITEM_ONLINE_OK")

        # --- insufficient treasury: error, no debit ---
        srv.tax_treasury = 5.0
        await srv.cmd_gm_reward(gm, {"gold": 50, "player": "Paid1"})
        assert srv.tax_treasury == 5.0, srv.tax_treasury
        assert "insufficient treasury" in inbox[-1].get("text", ""), inbox[-1]
        print("GM_INSUFFICIENT_OK")

        # --- gm_tables: staged state counted exactly ---
        cid = next(srv._commission_counter)
        srv._commissions[cid] = {"id": cid, "poster": "Paid1", "status": "open"}
        srv.market_orders.append({"id": 424242, "seller": "Paid1",
                                  "item": "arrow", "price": 3, "ts": 0.0})
        srv._pending_party_invites["ghosttest"] = {"party": None, "ts": 0.0}
        srv.room_gold["d_9_f9"] = 5
        try:
            await srv.cmd_gm_tables(gm, {})
            tab = inbox[-1]
            assert tab["type"] == "tables", tab
            assert tab["commissions_open"] >= 1 and tab["market_orders"] >= 1
            assert tab["pending_invites"] >= 1 and tab["dungeon_gold_keys"] >= 1
            assert "treasury" in tab and "treasury_lifetime" in tab
        finally:
            srv._commissions.pop(cid, None)
            srv.market_orders[:] = [o for o in srv.market_orders if o["id"] != 424242]
            srv._pending_party_invites.pop("ghosttest", None)
            srv.room_gold.pop("d_9_f9", None)
        print("GM_TABLES_OK")

        # --- gm_kick: unknown errors; online kick notifies + closes socket ---
        await srv.cmd_gm_kick(gm, {"player": "NobodyHere"})
        assert inbox[-1]["type"] == "error", inbox[-1]
        kickme = mkplayer("KickMe2", 40003)
        await srv.cmd_gm_kick(gm, {"player": "KickMe2", "reason": "unit test"})
        assert kickme.ws.closed, "kick must close the target socket"
        assert any("kicked" in m.get("text", "") for m in inbox[-2:]), inbox[-2:]
        unplayer(kickme)
        print("GM_KICK_OK")

        # --- GM stream origin gate: cross-origin browser refused ------------
        inbox.clear()
        evil = FakeGMWS(origin="http://evil.example")
        await srv.handle_gm_connection(evil)
        assert evil.closed, "cross-origin GM connection must be refused"
        assert inbox == [], f"refused GM stream must not dispatch: {inbox}"
        print("GM_EVIL_ORIGIN_REJECTED_OK")

        # --- GM stream accept paths: no origin / loopback origin --------------
        calls = []

        async def fake_gm_pong(player, msg):
            calls.append(msg)
            await srv.send(player, {"type": "message", "text": "GM: pong"})

        real_handlers = srv.GM_HANDLERS
        srv.GM_HANDLERS = {"gm_pong": fake_gm_pong}
        try:
            for origin, label in (
                (None, "no-origin"),
                ("http://127.0.0.1:8766", "loopback"),
            ):
                inbox.clear()
                calls.clear()
                ws = FakeGMWS(origin=origin)
                task = asyncio.create_task(srv.handle_gm_connection(ws))
                await asyncio.sleep(0)
                await ws.inbox.put(json.dumps({"cmd": "gm_pong"}))
                await ws.inbox.put(json.dumps(["not", "a", "dict"]))
                ws.ended = True
                await task
                assert not ws.closed, f"{label} GM connection must be accepted"
                assert len(calls) == 1, f"{label}: handler must run once: {calls}"
                assert any(
                    m.get("text") == "GM: pong" for m in inbox
                ), f"{label}: expected handler ack: {inbox}"
                assert any(
                    "JSON objects" in m.get("text", "") for m in inbox
                ), f"{label}: expected non-dict guard error: {inbox}"
                print(f"GM_ACCEPT_{label.upper()}_OK")
        finally:
            srv.GM_HANDLERS = real_handlers

        # --- GM stream per-connection rate limit / cooldown -------------------
        inbox.clear()
        calls.clear()
        srv.GM_HANDLERS = {"gm_pong": fake_gm_pong}
        ws = FakeGMWS(origin=None)
        task = asyncio.create_task(srv.handle_gm_connection(ws))
        await asyncio.sleep(0)
        await ws.inbox.put(json.dumps({"cmd": "gm_pong"}))
        await ws.inbox.put(json.dumps({"cmd": "gm_pong"}))
        ws.ended = True
        await task
        assert len(calls) == 1, f"cooldown must drop the second GM command: {calls}"
        assert any("rate limit" in m.get("text", "") for m in inbox), inbox
        print("GM_RATE_LIMIT_OK")
        srv.GM_HANDLERS = real_handlers
    finally:
        unplayer(gm)
        if rich is not None:
            unplayer(rich)
        srv.SCORES.pop("gone1", None)
        srv.tax_treasury, srv.tax_collected_lifetime = t0, tl0

    print("ALL_GM_UNIT_OK")


asyncio.run(main())
