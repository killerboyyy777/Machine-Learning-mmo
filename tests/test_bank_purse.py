"""Bank versus purse checks for #153 (no live server needed).

Run from the repo root:  python tests/test_bank_purse.py

Every test here needs the bank command, interest tick or bank observation, so
the suite fails on a checkout that predates #153.
"""

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


def mkplayer(name, pid, room="town_square"):
    pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
    pl.room = room
    pl.gold = 0
    srv.players[pid] = pl
    srv.add_member(pl)
    return pl


def unplayer(pl):
    srv.remove_member(pl)
    srv.players.pop(pl.id, None)


def capture_send():
    sent = []
    original = srv.send

    async def capture(player, payload):
        sent.append(payload)

    srv.send = capture
    return sent, original


def test_deposit_withdraw_and_stats():
    sent, original = capture_send()
    pl = mkplayer("BankUnitA", 96001)
    entry = srv.get_score_entry(pl.name)
    entry["gold_bank"] = 0
    pl.gold = 100
    try:
        asyncio.run(srv.cmd_bank(pl, {"action": "balance"}))
        balance = [m for m in sent if m.get("type") == "bank"][-1]
        assert balance["bank"] == 0 and balance["purse"] == 100, balance

        asyncio.run(srv.cmd_bank(pl, {"action": "deposit", "gold": 40}))
        assert (pl.gold, entry["gold_bank"]) == (60, 40)
        assert any(
            m.get("action") == "deposit" for m in sent if m.get("type") == "bank"
        )
        stats = [m for m in sent if m.get("type") == "stats"][-1]
        assert stats["bank"] == 40, stats

        pl.room = "deep_forest"
        asyncio.run(srv.cmd_bank(pl, {"action": "withdraw", "gold": 5}))
        assert (pl.gold, entry["gold_bank"]) == (60, 40)

        pl.room = "market"
        asyncio.run(srv.cmd_bank(pl, {"action": "withdraw", "gold": 0}))
        asyncio.run(srv.cmd_bank(pl, {"action": "withdraw", "gold": 100}))
        asyncio.run(srv.cmd_bank(pl, {"action": "withdraw", "gold": True}))
        assert (pl.gold, entry["gold_bank"]) == (60, 40)

        asyncio.run(srv.cmd_bank(pl, {"action": "withdraw", "gold": 40}))
        assert (pl.gold, entry["gold_bank"]) == (100, 0)
        assert any(
            m.get("action") == "withdraw" for m in sent if m.get("type") == "bank"
        )
        print("BANK_DEPOSIT_WITHDRAW_OK")
    finally:
        srv.send = original
        unplayer(pl)


def test_interest_tranches_and_treasury_floor():
    keys = (
        "BANK_INTEREST_RATES",
        "BANK_INTEREST_TRANCHE_SIZE",
        "BANK_INTEREST_CAP",
        "BANK_INTEREST_SOURCE",
        "tax_treasury",
        "_last_bank_interest_ts",
        "SCORES",
    )
    saved = {key: getattr(srv, key) for key in keys}
    try:
        srv.BANK_INTEREST_RATES = [2.0, 1.0, 0.5]
        srv.BANK_INTEREST_TRANCHE_SIZE = 10000
        srv.BANK_INTEREST_CAP = 500
        srv.BANK_INTEREST_SOURCE = 0
        srv.tax_treasury = 250.0
        srv.SCORES = {}
        entry = srv.get_score_entry("BankInterest")
        entry["gold_bank"] = 30000

        assert srv.bank_interest_amount(10000) == 200
        assert srv.bank_interest_amount(20000) == 300
        assert srv.bank_interest_amount(30000) == 350
        srv.BANK_INTEREST_CAP = 100
        assert srv.bank_interest_amount(20000) == 100
        srv.BANK_INTEREST_CAP = 500

        paid = asyncio.run(srv.tick_bank_interest(now=1000.0, force=True))
        assert len(paid) == 1 and paid[0]["amount"] == 250, paid
        assert paid[0]["source"] == "treasury"
        assert entry["gold_bank"] == 30250
        assert srv.tax_treasury == 0.0
        again = asyncio.run(srv.tick_bank_interest(now=1001.0, force=True))
        assert again == [], again
        assert srv.tax_treasury >= 0.0
        print("BANK_INTEREST_TREASURY_OK")
    finally:
        for key, value in saved.items():
            setattr(srv, key, value)


def test_interest_mint_is_opt_in():
    keys = ("BANK_INTEREST_SOURCE", "tax_treasury", "SCORES")
    saved = {key: getattr(srv, key) for key in keys}
    try:
        srv.BANK_INTEREST_SOURCE = 1
        srv.tax_treasury = 50.0
        srv.SCORES = {}
        entry = srv.get_score_entry("BankMint")
        entry["gold_bank"] = 10000
        paid = asyncio.run(srv.tick_bank_interest(now=2000.0, force=True))
        assert len(paid) == 1 and paid[0]["amount"] == 200, paid
        assert paid[0]["source"] == "mint"
        assert entry["gold_bank"] == 10200
        assert srv.tax_treasury == 50.0
        print("BANK_INTEREST_MINT_OK")
    finally:
        for key, value in saved.items():
            setattr(srv, key, value)


def test_death_hits_purse_only():
    pl = mkplayer("BankDeath", 96002, room="deep_forest")
    entry = srv.get_score_entry(pl.name)
    pl.gold = 100
    entry["gold_bank"] = 500
    entry["score"] = 1000.0
    floor_before = srv.room_gold.get("deep_forest", 0)
    try:
        asyncio.run(srv.respawn_player(pl))
        assert entry["gold_bank"] == 500, entry["gold_bank"]
        assert pl.gold == 50, pl.gold
        assert srv.room_gold.get("deep_forest", 0) == floor_before + 40
        penalty = srv.DEATH_PENALTY + srv.DEATH_SCORE_PER_GOLD_LOST * 550
        assert round(entry["score"], 2) == round(1000.0 - penalty, 2), entry["score"]
        print("BANK_DEATH_OK")
    finally:
        unplayer(pl)


def test_floor_gold_overflow_sink_long_horizon():
    """Repeated capped-floor inflows must not fund interest past the reserve."""
    keys = (
        "BANK_INTEREST_RATES",
        "BANK_INTEREST_TRANCHE_SIZE",
        "BANK_INTEREST_CAP",
        "BANK_INTEREST_SOURCE",
        "tax_treasury",
        "tax_collected_lifetime",
        "tax_sunk_lifetime",
        "_last_bank_interest_ts",
        "SCORES",
    )
    saved = {key: getattr(srv, key) for key in keys}
    room = "town_square"
    pile_before = srv.room_gold.get(room, 0)
    item_id = "reserve_sink_probe"
    item_before = srv.ITEM_DEFS.get(item_id)
    death_log_len = len(srv.DEATH_LOG)
    try:
        srv.BANK_INTEREST_RATES = [2.0, 1.0, 0.5]
        srv.BANK_INTEREST_TRANCHE_SIZE = 10000
        srv.BANK_INTEREST_CAP = 500
        srv.BANK_INTEREST_SOURCE = 0
        srv.tax_treasury = srv.TREASURY_RESERVE
        srv.tax_collected_lifetime = 0.0
        srv.tax_sunk_lifetime = 0.0
        srv.SCORES = {}
        srv.ITEM_DEFS[item_id] = {"value": 100}
        srv.room_gold[room] = srv.GROUND_GOLD_CAP
        entry = srv.get_score_entry("ReserveSinkBank")
        balance = 10000
        entry["gold_bank"] = balance

        cycles = 8
        for cycle in range(cycles):
            if cycle % 2 == 0:
                dier = mkplayer(f"ReserveSinkDeath{cycle}", 97000 + cycle, room=room)
                dier.gold = 250  # 40% drop lands on the full floor pile
                try:
                    asyncio.run(srv.respawn_player(dier))
                finally:
                    unplayer(dier)
            else:
                srv._ground_value_to_floor(room, item_id)
            assert srv.room_gold[room] == srv.GROUND_GOLD_CAP
            assert srv.tax_treasury <= srv.TREASURY_RESERVE, srv.tax_treasury

        gross = cycles * 100
        assert srv.tax_collected_lifetime == gross, srv.tax_collected_lifetime
        assert srv.tax_sunk_lifetime == gross, srv.tax_sunk_lifetime
        assert srv.tax_treasury == srv.TREASURY_RESERVE

        paid = []
        for tick in range(cycles):
            paid.extend(
                asyncio.run(srv.tick_bank_interest(now=float(20000 + tick), force=True))
            )
        total_paid = sum(p["amount"] for p in paid)
        assert total_paid <= srv.TREASURY_RESERVE, total_paid
        assert entry["gold_bank"] <= balance + srv.TREASURY_RESERVE, entry["gold_bank"]
        print("FLOOR_GOLD_RESERVE_SINK_OK")
    finally:
        srv.room_gold[room] = pile_before
        if item_before is None:
            srv.ITEM_DEFS.pop(item_id, None)
        else:
            srv.ITEM_DEFS[item_id] = item_before
        del srv.DEATH_LOG[death_log_len:]
        for key, value in saved.items():
            setattr(srv, key, value)


def test_bank_persistence_old_and_current():
    saved_scores = srv.SCORES
    saved_file = srv.SCORES_FILE
    saved_dirty = srv._scores_dirty
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "scores.json")
        try:
            srv.SCORES_FILE = path
            srv.SCORES = {}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": srv.SCORES_VERSION,
                        "scores": {
                            "oldplayer": {"display_name": "OldPlayer", "score": 3},
                            "broken": {"display_name": "Broken", "gold_bank": -9},
                        },
                    },
                    fh,
                )
            srv.SCORES = srv.load_scores()
            old = srv.get_score_entry("OldPlayer")
            broken = srv.get_score_entry("Broken")
            assert old["gold_bank"] == 0, old
            assert broken["gold_bank"] == 0, broken

            srv.SCORES = {}
            entry = srv.get_score_entry("BankSave")
            entry["gold_bank"] = 77
            assert srv.save_scores() is True
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            assert blob["scores"]["banksave"]["gold_bank"] == 77, blob
            print("BANK_PERSISTENCE_OK")
        finally:
            srv.SCORES = saved_scores
            srv.SCORES_FILE = saved_file
            srv._scores_dirty = saved_dirty


def test_snapshot_and_gm_stream():
    pl = mkplayer("BankSnap", 96003)
    pl.gold = 12
    entry = srv.get_score_entry(pl.name)
    entry["gold_bank"] = 88
    try:
        snap = srv.world_snapshot()
        assert snap["bank"]["total"] >= 88, snap["bank"]
        row = [r for r in snap["bank"]["players"] if r["name"] == pl.name][0]
        assert row == {"name": pl.name, "purse": 12, "bank": 88}, row
        assert snap["bank"]["funding"] in ("treasury", "mint")
        online = [r for r in snap["players"] if r["name"] == pl.name][0]
        assert online["bank"] == 88, online

        sent, original = capture_send()
        dummy = object()
        srv.GM_STREAMS.add(dummy)
        try:
            asyncio.run(srv.cmd_gm_tables(dummy, {}))
            tables = [m for m in sent if m.get("type") == "tables"][0]
            assert tables["bank"]["funding"] in ("treasury", "mint")
            saved_source = srv.BANK_INTEREST_SOURCE
            saved_treasury = srv.tax_treasury
            try:
                srv.BANK_INTEREST_SOURCE = 0
                srv.tax_treasury = 100.0
                asyncio.run(srv.tick_bank_interest(now=3000.0, force=True))
                assert any(m.get("type") == "bank_interest" for m in sent), sent
            finally:
                srv.BANK_INTEREST_SOURCE = saved_source
                srv.tax_treasury = saved_treasury
        finally:
            srv.GM_STREAMS.discard(dummy)
            srv.send = original
        print("BANK_SNAPSHOT_GM_OK")
    finally:
        unplayer(pl)


def test_ml_bank_state_mask_and_observation():
    sys.path.insert(0, os.path.join(ROOT, "ml"))
    import ml_env

    assert "bank_deposit" in ml_env.ACTIONS
    assert "bank_withdraw" in ml_env.ACTIONS
    dep = ml_env.ACTIONS.index("bank_deposit")
    wd = ml_env.ACTIONS.index("bank_withdraw")
    env = ml_env.TextMMOEnv("BankML")
    env._apply_event({"type": "stats", "gold": 100, "bank": 55})
    assert env._state["bank"] == 55, env._state["bank"]

    env._state["room_id"] = "town_square"
    env._state["gold"] = 100
    env._state["bank"] = 0
    obs = env._build_obs()
    assert obs["bank_norm"] == 0.0
    assert len(ml_env.flatten_obs(obs)) == ml_env.OBS_SIZE
    mask = env.valid_action_mask()
    assert mask[dep] == 1 and mask[wd] == 0, (mask[dep], mask[wd])
    assert env._action_to_cmd("bank_deposit") == {
        "cmd": "bank",
        "action": "deposit",
        "gold": 40,
    }

    env._state["gold"] = 5
    env._state["bank"] = 80
    obs = env._build_obs()
    mask = env.valid_action_mask()
    assert obs["bank_norm"] == 0.4
    assert mask[dep] == 0 and mask[wd] == 1, (mask[dep], mask[wd])
    assert env._action_to_cmd("bank_withdraw") == {
        "cmd": "bank",
        "action": "withdraw",
        "gold": 40,
    }

    env._state["room_id"] = "deep_forest"
    env._build_obs()
    mask = env.valid_action_mask()
    assert mask[dep] == 0 and mask[wd] == 0
    print("ML_BANK_MASK_OK")


if __name__ == "__main__":
    test_deposit_withdraw_and_stats()
    test_interest_tranches_and_treasury_floor()
    test_interest_mint_is_opt_in()
    test_death_hits_purse_only()
    test_floor_gold_overflow_sink_long_horizon()
    test_bank_persistence_old_and_current()
    test_snapshot_and_gm_stream()
    test_ml_bank_state_mask_and_observation()
    print("BANK_PURSE_OK")
