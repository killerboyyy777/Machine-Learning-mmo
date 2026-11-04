"""Guild persistence tests for issue #588 (PR 1, no live server needed).

Run from the repo root:  python tests/test_guilds.py
Covers guilds.json load/save round-trip, the version gate, the rotated
generation, the derived member index, telemetry, and both read surfaces.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


def _read(path):
    with open(path) as f:
        return json.load(f)


with tempfile.TemporaryDirectory() as tmp:
    db = os.path.join(tmp, "guilds.json")
    old_file = srv.GUILDS_FILE
    old_blocked = srv.GUILDS_FILE_BLOCKED
    saved_guilds = srv.GUILDS
    saved_tombstones = srv.GUILD_TOMBSTONES
    srv.GUILDS_FILE = db
    srv.GUILDS_FILE_BLOCKED = False
    srv.GUILDS = {}
    srv.GUILD_TOMBSTONES = []
    try:
        # --- fresh start: nothing on disk -> empty world -------------------
        assert srv.load_guilds() == ({}, [])
        print("LOAD_EMPTY_OK")

        # --- save then load returns the same record ------------------------
        record = srv.new_guild_record(1, "Iron Choir", "Aria", 1234.0)
        srv.GUILDS = {1: record}
        srv._rebuild_guild_index()
        assert srv.save_guilds() is True
        loaded, tombstones = srv.load_guilds()
        assert loaded == srv.GUILDS, (loaded, srv.GUILDS)
        assert tombstones == []
        assert loaded[1]["members"]["aria"] == {"rank": "leader", "joined_ts": 1234.0}
        print("ROUND_TRIP_OK")

        # --- a second save rotates the previous primary to .1 -------------
        srv.GUILDS[1]["vault_gold"] = 120
        assert srv.save_guilds() is True
        assert _read(db)["guilds"]["1"]["vault_gold"] == 120
        assert _read(db + ".1")["guilds"]["1"]["vault_gold"] == 0
        srv.GUILDS[1]["vault_gold"] = 130
        assert srv.save_guilds() is True
        assert _read(db + ".1")["guilds"]["1"]["vault_gold"] == 120
        assert not os.path.exists(db + ".2")
        print("ROTATE_OK")

        # --- the member index is rebuilt from the loaded records ----------
        second = srv.new_guild_record(2, "Night Watch", "Bram", 2000.0)
        second["members"]["cid"] = {"rank": "member", "joined_ts": 2001.0}
        srv.GUILDS = {1: record, 2: second}
        assert srv.save_guilds() is True
        loaded, _ = srv.load_guilds()
        srv.GUILDS = loaded
        srv._rebuild_guild_index()
        assert srv.GUILD_BY_MEMBER == {"aria": 1, "bram": 2, "cid": 2}
        print("INDEX_OK")

        # --- corrupt primary falls back to the rotated generation ---------
        with open(db, "w") as f:
            f.write("{not json")
        loaded, _ = srv.load_guilds()
        assert list(loaded) == [1], loaded
        assert loaded[1]["name"] == "Iron Choir"
        print("RECOVER_OK")

        # --- telemetry is zeroed on an empty world ------------------------
        srv.GUILDS = {}
        srv._rebuild_guild_index()
        zero = srv.guild_telemetry()
        for key in (
            "count",
            "members_total",
            "online_members",
            "largest",
            "vault_gold_total",
            "vault_items_total",
            "invites_pending",
            "audit_entries",
            "vault_gold_in_total",
            "vault_gold_out_total",
            "audit_entries_written",
        ):
            assert zero[key] == 0, (key, zero[key])
        print("TELEMETRY_EMPTY_OK")

        # --- and counts staged records without touching the counters ------
        record["vault_gold"] = 120
        record["vault_items"] = ["sword", "herb", "herb"]
        second["vault_gold"] = 30
        srv.GUILDS = {1: record, 2: second}
        srv._rebuild_guild_index()
        telem = srv.guild_telemetry()
        assert telem["count"] == 2
        assert telem["members_total"] == 3
        assert telem["online_members"] == 0
        assert telem["largest"] == 2
        assert telem["vault_gold_total"] == 150
        assert telem["vault_items_total"] == 3
        assert telem["audit_entries"] == 0
        print("TELEMETRY_STAGED_OK")

        # --- an unknown version is left on disk and starts an empty world -
        with open(db, "w") as f:
            json.dump({"version": 2, "guilds": {"7": {"name": "Future"}}}, f)
        before = _read(db)
        srv.GUILDS_FILE_BLOCKED = False
        assert srv.load_guilds() == ({}, [])
        assert srv.GUILDS_FILE_BLOCKED is True
        assert srv.save_guilds() is False
        assert _read(db) == before
        print("UNKNOWN_VERSION_OK")

        # --- both read surfaces carry the same telemetry block ------------
        srv.GUILDS = {}
        srv._rebuild_guild_index()
        snap = srv.world_snapshot()
        assert set(srv.guild_telemetry()) <= set(snap["guilds"])
        assert snap["guilds"]["history"] == []
        assert snap["guilds"]["count"] == 0

        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        async def tables():
            await srv.cmd_gm_tables(object(), {})

        old_send = srv.send
        srv.send = fake_send
        try:
            asyncio.run(tables())
        finally:
            srv.send = old_send
        tab = inbox[-1]
        assert tab["type"] == "tables", tab
        assert tab["guilds"] == srv.guild_telemetry()
        print("SURFACES_OK")
    finally:
        srv.GUILDS_FILE = old_file
        srv.GUILDS_FILE_BLOCKED = old_blocked
        srv.GUILDS = saved_guilds
        srv.GUILD_TOMBSTONES = saved_tombstones
        srv._rebuild_guild_index()

print("ALL_GUILDS_OK")
