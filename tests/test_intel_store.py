"""Shared intel pool persistence tests for issue #504 (PR 1, no live server).

Run from the repo root:  python tests/test_intel_store.py
Covers intel.json load/save round-trip, the version gate, the single rotated
generation, corrupt-primary recovery, tolerant normalisation, the caps and
both read surfaces. PR 1 adds no verb: nothing here logs or queries.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


def _read(path):
    with open(path) as f:
        return json.load(f)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _row(seq, **kw):
    now = time.time()
    row = {
        "seq": seq,
        "ts": now,
        "guild": 1,
        "observer": "aria",
        "room": "deep_forest",
        "kind": "hostile",
        "subject": "treant",
        "value": "alive",
        "confidence": 100,
        "source": "sensor",
        "repeats": 1,
        "first_ts": now,
    }
    row.update(kw)
    return row


def _write_file(path, rows, seq=None):
    data = {
        "version": srv.INTEL_VERSION,
        "seq": seq if seq is not None else max(r["seq"] for r in rows),
        "observations": rows,
    }
    with open(path, "w") as f:
        json.dump(data, f)


with tempfile.TemporaryDirectory() as tmp:
    db = os.path.join(tmp, "intel.json")
    old_file = srv.INTEL_FILE
    old_blocked = srv.INTEL_FILE_BLOCKED
    old_rows = srv.INTEL_ROWS
    old_seq = srv.INTEL_SEQ
    old_dirty = srv._intel_dirty
    srv.INTEL_FILE = db
    srv.INTEL_FILE_BLOCKED = False
    srv.INTEL_ROWS = []
    srv.INTEL_SEQ = 0
    srv._intel_dirty = False
    try:
        # --- fresh start: nothing on disk -> empty pool --------------------
        assert srv.load_intel() == ([], 0)
        assert srv.INTEL_FILE_BLOCKED is False
        print("LOAD_EMPTY_OK")

        # --- save then load returns the same rows, and re-saves the same
        # bytes (the writer is the only shape a reader ever sees) ----------
        rows = [_row(1), _row(2, kind="node", subject="ore_vein", value="ready")]
        rows.append(_row(3, kind="traffic", subject=None, value="1-3", confidence=80))
        srv.INTEL_ROWS = rows
        srv.INTEL_SEQ = 3
        assert srv.save_intel() is True
        first = _bytes(db)
        loaded, seq = srv.load_intel()
        assert loaded == rows, (loaded, rows)
        assert seq == 3
        srv.INTEL_ROWS = loaded
        srv.INTEL_SEQ = seq
        assert srv.save_intel() is True
        assert _bytes(db) == first, "save/load round trip changed the bytes"
        print("ROUND_TRIP_OK")

        # --- exactly one rotated generation ------------------------------
        srv.INTEL_ROWS = [_row(4)]
        assert srv.save_intel() is True
        assert os.path.exists(db + ".1")
        assert not os.path.exists(db + ".2")
        assert len(_read(db + ".1")["observations"]) == 3
        assert len(_read(db)["observations"]) == 1
        print("ROTATE_OK")

        # --- an unknown version is left on disk and blocks saving --------
        with open(db, "w") as f:
            json.dump({"version": 2, "seq": 9, "observations": [_row(9)]}, f)
        before = _bytes(db)
        srv.INTEL_FILE_BLOCKED = False
        assert srv.load_intel() == ([], 0)
        assert srv.INTEL_FILE_BLOCKED is True
        assert srv.save_intel() is False
        assert _bytes(db) == before, "a blocked save rewrote the newer file"
        print("UNKNOWN_VERSION_OK")

        # --- corrupt primary recovers from intel.json.1 ------------------
        _write_file(db + ".1", [_row(1, confidence=42)])
        with open(db, "w") as f:
            f.write("{not json")
        srv.INTEL_FILE_BLOCKED = False
        loaded, seq = srv.load_intel()
        assert len(loaded) == 1 and loaded[0]["confidence"] == 42, loaded
        assert seq == 1
        print("RECOVER_OK")

        # --- tolerant normalisation: extra keys and missing optionals keep
        # the row, anything unservable is dropped -------------------------
        now = time.time()
        raw = [
            dict(_row(1), hp=99, gold=5),
            {
                "seq": 2,
                "ts": now,
                "guild": 1,
                "observer": "Aria",
                "room": "deep_forest",
                "kind": "hostile",
                "subject": "treant",
                "value": "alive",
                "confidence": 100,
                "source": "sensor",
            },
            _row(3, kind="market"),
            _row(4, room="atlantis"),
            _row(5, ts=now + 3600),
            _row(6, confidence=250),
            _row(7, confidence=-5),
            _row(8, source="player_report"),
            _row(9, kind="traffic", subject="Aria", value="0"),
            _row(1, subject="other"),
            "not a row",
        ]
        _write_file(db, [r for r in raw if isinstance(r, dict)], seq=20)
        loaded, seq = srv.load_intel()
        assert [r["seq"] for r in loaded] == [1, 2, 6, 7, 9], loaded
        assert seq == 20, "the file high-water must survive a load"
        by_seq = {r["seq"]: r for r in loaded}
        assert "hp" not in by_seq[1] and "gold" not in by_seq[1]
        assert by_seq[1]["subject"] == "treant", "first duplicate seq must win"
        assert by_seq[2]["observer"] == "aria"
        assert by_seq[2]["repeats"] == 1 and by_seq[2]["first_ts"] == now
        assert by_seq[6]["confidence"] == 100 and by_seq[7]["confidence"] == 0
        assert by_seq[9]["subject"] is None
        print("NORMALIZE_OK")

        # --- the per-observer cap keeps that observer's newest rows -------
        rows = [_row(i + 1, ts=now - 1000 + i) for i in range(40)]
        assert srv._intel_compact(rows) == 40 - srv.INTEL_MAX_PER_OBSERVER
        assert len(rows) == srv.INTEL_MAX_PER_OBSERVER
        assert rows[-1]["seq"] == 40 and rows[0]["seq"] == 40 - 24 + 1
        print("OBSERVER_CAP_OK")

        # --- the per-guild cap keeps that guild's newest rows -------------
        observers = -(-srv.INTEL_MAX_PER_GUILD // srv.INTEL_MAX_PER_OBSERVER) + 1
        rows = [
            _row(i + 1, ts=now - 5000 + i, observer=f"bot{i % observers}")
            for i in range(srv.INTEL_MAX_PER_GUILD + 50)
        ]
        assert srv._intel_compact(rows) == 50
        assert len(rows) == srv.INTEL_MAX_PER_GUILD
        assert rows[-1]["seq"] == srv.INTEL_MAX_PER_GUILD + 50
        print("GUILD_CAP_OK")

        # --- the world cap evicts fairly across guilds --------------------
        guilds = srv.INTEL_MAX_ENTRIES // srv.INTEL_MAX_PER_GUILD + 1
        per_observer = -(-srv.INTEL_MAX_PER_GUILD // srv.INTEL_MAX_PER_OBSERVER)
        rows = []
        seq = 0
        for gid in range(1, guilds + 1):
            for i in range(srv.INTEL_MAX_PER_GUILD):
                seq += 1
                rows.append(
                    _row(
                        seq,
                        ts=now - 10000 + i,
                        guild=gid,
                        observer=f"g{gid}o{i % per_observer}",
                    )
                )
        assert len(rows) > srv.INTEL_MAX_ENTRIES
        srv._intel_compact(rows)
        assert len(rows) == srv.INTEL_MAX_ENTRIES, len(rows)
        sizes = {}
        for row in rows:
            sizes[row["guild"]] = sizes.get(row["guild"], 0) + 1
        assert len(sizes) == guilds, sizes
        assert min(sizes.values()) > 0, "a guild was wiped out"
        assert max(sizes.values()) - min(sizes.values()) <= 1, sizes
        print("WORLD_CAP_OK")

        # --- the TTL drops rows and reports how many ---------------------
        rows = [
            _row(1, ts=now - srv.INTEL_TTL_SECONDS - 5),
            _row(2, ts=now - 60),
            _row(3, ts=now),
        ]
        assert srv._intel_prune(rows) == 1
        assert [r["seq"] for r in rows] == [2, 3]
        print("PRUNE_OK")

        # --- a load applies the caps and the TTL before serving ----------
        stale = _row(1, ts=now - srv.INTEL_TTL_SECONDS - 5)
        bulk = [
            _row(i + 2, ts=now - 1000 + i, observer=f"bot{i % observers}")
            for i in range(srv.INTEL_MAX_PER_GUILD + 50)
        ]
        _write_file(db, [stale] + bulk, seq=9999)
        loaded, seq = srv.load_intel()
        assert len(loaded) == srv.INTEL_MAX_PER_GUILD, len(loaded)
        assert all(r["seq"] != 1 for r in loaded)
        assert seq == 9999
        print("LOAD_TRIM_OK")

        # --- snapshot: 7 counters, 2 age gauges, since -------------------
        srv.INTEL_ROWS = []
        empty = srv._intel_snapshot()
        assert set(empty) == {"since", "counters", "ages"}, empty
        assert len(empty["counters"]) == 7
        for key, value in empty["counters"].items():
            assert value == 0, (key, value)
        assert empty["ages"] == {"oldest_age_s": None, "median_age_s": None}
        assert isinstance(empty["since"], float)
        assert 0 < empty["since"] <= time.time()
        assert time.time() - empty["since"] < 3600

        srv.INTEL_ROWS = [_row(1, ts=now), _row(2, ts=now - 600)]
        snap = srv._intel_snapshot()
        assert 599 <= snap["ages"]["oldest_age_s"] <= 601, snap["ages"]
        assert 299 <= snap["ages"]["median_age_s"] <= 301, snap["ages"]
        print("SNAPSHOT_OK")

        # --- both read surfaces serve the same block ---------------------
        srv.INTEL_ROWS = []
        assert srv.world_snapshot()["intel"] == srv._intel_snapshot()
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        old_send = srv.send
        srv.send = fake_send
        try:
            asyncio.run(srv.cmd_gm_tables(object(), {}))
        finally:
            srv.send = old_send
        tab = inbox[-1]
        assert tab["type"] == "tables", tab
        assert tab["intel"] == srv._intel_snapshot(), tab["intel"]
        print("SURFACES_OK")
    finally:
        srv.INTEL_FILE = old_file
        srv.INTEL_FILE_BLOCKED = old_blocked
        srv.INTEL_ROWS = old_rows
        srv.INTEL_SEQ = old_seq
        srv._intel_dirty = old_dirty

print("ALL_INTEL_STORE_OK")
