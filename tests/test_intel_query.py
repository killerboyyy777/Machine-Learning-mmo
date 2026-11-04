"""Shared intel pool read-path tests for issue #504 (PR 3, no live server).

Run from the repo root:  python tests/test_intel_query.py
Covers the `intel query` verb: the guild gate, guild scoping, the
kind/room/subject filters, the limit and max_age clamps, newest-first order,
age labels and section 8 buckets, the `live` label, the empty answer, the
query/hit/miss counters and their identity, and that a read writes nothing.
Bare `intel` must query, not log.
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv

ROW_KEYS = {
    "room",
    "kind",
    "subject",
    "value",
    "confidence",
    "repeats",
    "ts",
    "age_s",
    "age_label",
    "bucket",
    "live",
}


class FakeWS:
    remote_address = ("127.0.0.1", 1)


def texts(inbox):
    return [m["text"] for m in inbox if isinstance(m, dict) and "text" in m]


def row(seq, ts, **kw):
    rec = {
        "seq": seq,
        "ts": ts,
        "guild": 1,
        "observer": "aria",
        "room": "deep_forest",
        "kind": "hostile",
        "subject": "treant",
        "value": "alive",
        "confidence": 100,
        "source": "sensor",
        "repeats": 1,
        "first_ts": ts,
    }
    rec.update(kw)
    return rec


def seed(rows):
    srv.INTEL_ROWS[:] = rows
    srv.INTEL_QUERY_CACHE.clear()


def fingerprint(inbox):
    """(kind, subject, ts) per row: ages may tick between two replies."""
    return [(r["kind"], r["subject"], r["ts"]) for r in inbox[-1]["rows"]]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            "INTEL_FILE": srv.INTEL_FILE,
            "INTEL_FILE_BLOCKED": srv.INTEL_FILE_BLOCKED,
            "INTEL_ROWS": srv.INTEL_ROWS,
            "INTEL_SEQ": srv.INTEL_SEQ,
            "_intel_dirty": srv._intel_dirty,
            "INTEL_STATS": dict(srv.INTEL_STATS),
            "GUILDS": srv.GUILDS,
            "GUILD_TOMBSTONES": srv.GUILD_TOMBSTONES,
            "GUILDS_FILE": srv.GUILDS_FILE,
            "_guilds_dirty": srv._guilds_dirty,
            "send": srv.send,
        }
        keys0 = set(srv.SCORES)
        base = dict(srv.INTEL_STATS)
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        def mkplayer(name, pid, room, gold=0):
            pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
            pl.room = room
            pl.gold = gold
            srv.players[pid] = pl
            srv.add_member(pl)
            srv.get_score_entry(name)
            return pl

        def unplayer(pl):
            srv.remove_member(pl)
            srv.players.pop(pl.id, None)

        def move(pl, room):
            srv.remove_member(pl)
            pl.room = room
            srv.add_member(pl)

        srv.INTEL_FILE = os.path.join(tmp, "intel.json")
        srv.INTEL_FILE_BLOCKED = False
        srv.INTEL_SEQ = 0
        srv.INTEL_ROWS[:] = []
        srv.INTEL_QUERY_CACHE.clear()
        srv._intel_dirty = False
        srv.GUILDS_FILE = os.path.join(tmp, "guilds.json")
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv.send = fake_send
        for key in srv.INTEL_STATS:
            srv.INTEL_STATS[key] = 0
        players = []
        try:
            # --- two guilds: Aria and Bob in one, Dana in the other -------
            aria = mkplayer("Aria", 70501, "deep_forest")
            bob = mkplayer("Bob", 70502, "forest_edge")
            cid = mkplayer("Cid", 70503, "market")
            dana = mkplayer("Dana", 70504, "graveyard")
            players.extend([aria, bob, cid, dana])
            now = time.time()
            choir = srv.new_guild_record(1, "Intel Choir", "Aria", now)
            choir["members"]["bob"] = {"rank": "member", "joined_ts": now}
            srv.GUILDS[1] = choir
            srv.GUILDS[2] = srv.new_guild_record(2, "Other Choir", "Dana", now)
            srv._rebuild_guild_index()
            gid = 1

            # --- the verb is wired, and bare intel is a query ------------
            assert srv.HANDLERS["intel"] is srv.cmd_intel
            assert "intel" not in srv.SCORE_ARG_EXTRACTORS
            inbox.clear()
            await srv.cmd_help(aria, {})
            assert "intel [log|query]" in inbox[-1]["text"], inbox[-1]["text"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "watch"})
            assert texts(inbox) == [
                "Unknown intel action 'watch'. Use log or query."
            ], texts(inbox)
            assert srv.INTEL_STATS["queries_served"] == base["queries_served"]
            seed([row(1, time.time() - 30)])
            inbox.clear()
            await srv.cmd_intel(aria, {})
            reply = inbox[-1]
            assert reply["action"] == "query", reply
            assert reply["count"] == 1 and len(reply["rows"]) == 1, reply
            assert srv.INTEL_STATS["observations_logged"] == 0, "a query logged"
            print("SURFACE_OK")

            # --- a guildless character is refused, and nothing moves ------
            before = dict(srv.INTEL_STATS)
            inbox.clear()
            await srv.cmd_intel(cid, {"action": "query"})
            assert texts(inbox) == [srv.INTEL_NO_GUILD], texts(inbox)
            assert srv.INTEL_STATS == before, "a refused read moved a counter"
            inbox.clear()
            await srv.cmd_intel(cid, {})
            assert texts(inbox) == [srv.INTEL_NO_GUILD], texts(inbox)
            assert srv.INTEL_STATS == before, "a refused read moved a counter"
            print("NO_GUILD_OK")

            # --- bad input is refused by name, and nothing moves ---------
            before = dict(srv.INTEL_STATS)
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "atlantis"})
            assert texts(inbox) == ["No room 'atlantis' in the world."], texts(inbox)
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "market"})
            expected = (
                "Unknown intel kind 'market'. Use hostile, npc_death, "
                "player_death, node, or traffic."
            )
            assert texts(inbox) == [expected], texts(inbox)
            assert srv.INTEL_STATS == before, "a refused read moved a counter"
            print("REFUSALS_OK")

            # --- guild scoping: never the other guild's rows, either way --
            t = time.time()
            seed(
                [
                    row(1, t - 30, guild=1, observer="aria"),
                    row(2, t - 20, guild=2, observer="dana", subject="wolf"),
                    row(3, t - 10, guild=1, room="forest_edge", observer="bob"),
                ]
            )
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "deep_forest"})
            replies = inbox[-1]["rows"]
            assert [r["subject"] for r in replies] == ["treant"], replies
            inbox.clear()
            await srv.cmd_intel(dana, {"action": "query", "room": "deep_forest"})
            assert [r["subject"] for r in inbox[-1]["rows"]] == ["wolf"], inbox[-1]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "forest_edge"})
            assert [r["subject"] for r in inbox[-1]["rows"]] == ["treant"], inbox[-1]
            print("SCOPE_OK")

            # --- an any-room read: neither room is the reader's own -------
            assert aria.room == "deep_forest"
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "query", "room": "forest_edge", "kind": "hostile"}
            )
            assert inbox[-1]["room"] == "forest_edge", inbox[-1]
            assert inbox[-1]["count"] == 1, inbox[-1]
            print("ANY_ROOM_OK")

            # --- kind, room and subject narrow the same pool -------------
            t = time.time()
            seed(
                [
                    row(1, t - 40, kind="hostile", subject="treant"),
                    row(
                        2,
                        t - 30,
                        kind="node",
                        subject="iron_ore_node",
                        value="ready",
                    ),
                    row(3, t - 20, kind="traffic", subject=None, value="1-3"),
                    row(4, t - 10, kind="hostile", subject="wolf"),
                ]
            )
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "hostile"})
            assert {r["subject"] for r in inbox[-1]["rows"]} == {"treant", "wolf"}
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "node"})
            assert [r["subject"] for r in inbox[-1]["rows"]] == ["iron_ore_node"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "subject": "wolf"})
            assert [r["kind"] for r in inbox[-1]["rows"]] == ["hostile"]
            assert inbox[-1]["subject"] == "wolf", inbox[-1]
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "query", "kind": "node", "subject": "wolf"}
            )
            assert inbox[-1]["count"] == 0, inbox[-1]
            print("FILTERS_OK")

            # --- newest first, and the limit keeps the newest --------------
            t = time.time()
            seed([row(i, t - (6 - i) * 10, subject=f"mob{i}") for i in range(1, 6)])
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query"})
            ages = [r["age_s"] for r in inbox[-1]["rows"]]
            assert ages == sorted(ages), ages
            assert [r["subject"] for r in inbox[-1]["rows"]] == [
                "mob5",
                "mob4",
                "mob3",
                "mob2",
                "mob1",
            ], inbox[-1]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "n": 2})
            assert [r["subject"] for r in inbox[-1]["rows"]] == ["mob5", "mob4"]
            assert inbox[-1]["limit"] == 2, inbox[-1]
            print("ORDER_OK")

            # --- the limit is clamped, never unbounded --------------------
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "n": 999})
            assert inbox[-1]["limit"] == srv.INTEL_QUERY_MAX_N, inbox[-1]
            assert inbox[-1]["count"] == 5, inbox[-1]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "n": 0})
            assert inbox[-1]["limit"] == 1, inbox[-1]
            assert inbox[-1]["count"] == 1, inbox[-1]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query"})
            assert inbox[-1]["limit"] == srv.INTEL_QUERY_DEFAULT_N, inbox[-1]
            print("LIMIT_OK")

            # --- max_age defaults to 30m and may rise only to the TTL -----
            t = time.time()
            seed(
                [
                    row(1, t - 10, subject="near"),
                    row(2, t - 4000, subject="far"),
                    row(3, t - srv.INTEL_TTL_SECONDS - 60, subject="expired"),
                ]
            )
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query"})
            assert [r["subject"] for r in inbox[-1]["rows"]] == ["near"], inbox[-1]
            assert inbox[-1]["max_age"] == srv.INTEL_QUERY_DEFAULT_MAX_AGE
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "max_age": 999999})
            assert inbox[-1]["max_age"] == srv.INTEL_TTL_SECONDS, inbox[-1]
            got = [r["subject"] for r in inbox[-1]["rows"]]
            assert got == ["near", "far"], "a row past the TTL was served"
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "max_age": 0})
            assert inbox[-1]["max_age"] == 1, inbox[-1]
            print("MAX_AGE_OK")

            # --- every row carries an age and a section 8 bucket ----------
            t = time.time()
            seed(
                [
                    row(1, t - 10, subject="live"),
                    row(2, t - 120, subject="fresh"),
                    row(3, t - 900, subject="recent"),
                    row(4, t - 5000, subject="old"),
                ]
            )
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "query", "max_age": srv.INTEL_TTL_SECONDS}
            )
            rows = inbox[-1]["rows"]
            assert [r["subject"] for r in rows] == ["live", "fresh", "recent", "old"]
            assert [r["bucket"] for r in rows] == [
                "live",
                "fresh",
                "recent",
                "old",
            ], [r["bucket"] for r in rows]
            for got, want in zip(rows, (10, 120, 900, 5000)):
                assert abs(got["age_s"] - want) <= 2, (got["age_s"], want)
                assert got["age_label"].endswith(" ago"), got
            assert rows[0]["age_label"] == f"{rows[0]['age_s']}s ago", rows[0]
            assert rows[2]["age_label"].endswith("m ago"), rows[2]
            assert rows[3]["age_label"].endswith("h ago"), rows[3]
            print("AGES_OK")

            # --- live needs a guild mate in the room right now ------------
            # Aria is a guild member and stands in deep_forest.
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "deep_forest"})
            assert inbox[-1]["rows"][0]["live"] is True, inbox[-1]["rows"][0]
            assert inbox[-1]["rows"][1]["live"] is False, "a 120s row cannot be live"
            # Same filters, same memo entry: the mate left, so live is now
            # false even though the cached rows are the same.
            move(aria, "market")
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "deep_forest"})
            assert inbox[-1]["rows"][0]["live"] is False, inbox[-1]["rows"][0]
            assert inbox[-1]["rows"][0]["bucket"] == "fresh", inbox[-1]["rows"][0]
            # Bob is guild 1 and stands in forest_edge; Dana is guild 2.
            t = time.time()
            seed([row(1, t - 10, room="forest_edge", subject="mate")])
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "forest_edge"})
            assert inbox[-1]["rows"][0]["live"] is True, inbox[-1]["rows"][0]
            seed([row(1, t - 10, room="graveyard", subject="stranger")])
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "room": "graveyard"})
            assert inbox[-1]["rows"][0]["live"] is False, inbox[-1]["rows"][0]
            move(aria, "deep_forest")
            print("LIVE_OK")

            # --- an empty answer says nothing about safety ----------------
            seed([row(1, time.time() - 10)])
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "npc_death"})
            assert inbox[-1]["count"] == 0 and inbox[-1]["rows"] == [], inbox[-1]
            assert inbox[-1]["text"] == "No intel for Deep Forest within 30m.", inbox[
                -1
            ]
            for word in ("clear", "safe"):
                assert word not in inbox[-1]["text"].lower(), inbox[-1]["text"]
            print("EMPTY_OK")

            # --- the memo: a hit skips the scan, staleness is a miss ------
            t = time.time()
            seed(
                [
                    row(1, t - 10, subject="treant"),
                    row(
                        2, t - 900, kind="node", subject="iron_ore_node", value="ready"
                    ),
                ]
            )
            hits = srv.INTEL_STATS["cache_hits"]
            misses = srv.INTEL_STATS["cache_misses"]
            served = srv.INTEL_STATS["queries_served"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "hostile"})
            first = fingerprint(inbox)
            assert srv.INTEL_STATS["cache_misses"] == misses + 1, "a scan was a hit"
            assert srv.INTEL_STATS["cache_hits"] == hits
            assert srv.INTEL_STATS["queries_served"] == served + 1
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "hostile"})
            assert fingerprint(inbox) == first, "a hit returned other rows"
            assert srv.INTEL_STATS["cache_hits"] == hits + 1, "a hit was not counted"
            assert srv.INTEL_STATS["cache_misses"] == misses + 1
            assert srv.INTEL_STATS["queries_served"] == served + 2
            key = srv._intel_query_cache_key(gid, "deep_forest", "hostile", None)
            assert key in srv.INTEL_QUERY_CACHE, srv.INTEL_QUERY_CACHE.keys()
            # Age the memo past its bound and evict the row it still holds:
            # a stale entry must be a miss, so the served rows are rebuilt
            # from the pool rather than replayed.
            srv.INTEL_QUERY_CACHE[key]["built"] -= srv.INTEL_LIVE_SECONDS + 1
            srv.INTEL_ROWS[:] = [r for r in srv.INTEL_ROWS if r["subject"] != "treant"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query", "kind": "hostile"})
            assert inbox[-1]["rows"] == [], "a stale memo entry served a dead row"
            assert srv.INTEL_STATS["cache_misses"] == misses + 2
            assert srv.INTEL_STATS["cache_hits"] == hits + 1
            assert srv.INTEL_STATS["queries_served"] == served + 3
            print("CACHE_OK")

            # --- a read carries no identity and writes nothing ------------
            t = time.time()
            seed(
                [
                    row(1, t - 10, subject="treant"),
                    row(2, t - 20, kind="traffic", subject=None, value="1-3"),
                ]
            )
            before = dict(srv.INTEL_STATS)
            dirty = srv._intel_dirty
            scores = set(srv.SCORES)
            pool = list(srv.INTEL_ROWS)
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query"})
            for got in inbox[-1]["rows"]:
                assert set(got) == ROW_KEYS, set(got)
                assert "observer" not in got and "guild" not in got, got
                assert "seq" not in got, got
            assert srv.INTEL_STATS["queries_served"] == before["queries_served"] + 1
            for counter in (
                "observations_logged",
                "observations_refused",
                "entries_evicted",
                "entries_expired",
            ):
                assert srv.INTEL_STATS[counter] == before[counter], counter
            assert srv._intel_dirty is dirty
            assert set(srv.SCORES) == scores
            assert srv.INTEL_ROWS == pool
            print("INV5_OK")

            # --- the gate identity: one hit or one miss per served query --
            served = srv.INTEL_STATS["queries_served"] - base["queries_served"]
            counted = (
                srv.INTEL_STATS["cache_hits"]
                - base["cache_hits"]
                + srv.INTEL_STATS["cache_misses"]
                - base["cache_misses"]
            )
            assert served == counted, (served, counted)
            assert srv.INTEL_STATS["cache_hits"] > base["cache_hits"]
            assert srv.INTEL_STATS["cache_misses"] > base["cache_misses"]
            print("COUNTERS_OK")
        finally:
            for pl in players:
                unplayer(pl)
            for key in set(srv.SCORES) - keys0:
                srv.SCORES.pop(key, None)
            srv.INTEL_FILE = saved["INTEL_FILE"]
            srv.INTEL_FILE_BLOCKED = saved["INTEL_FILE_BLOCKED"]
            srv.INTEL_ROWS[:] = saved["INTEL_ROWS"]
            srv.INTEL_QUERY_CACHE.clear()
            srv.INTEL_SEQ = saved["INTEL_SEQ"]
            srv._intel_dirty = saved["_intel_dirty"]
            srv.INTEL_STATS.clear()
            srv.INTEL_STATS.update(saved["INTEL_STATS"])
            srv.GUILDS = saved["GUILDS"]
            srv.GUILD_TOMBSTONES = saved["GUILD_TOMBSTONES"]
            srv.GUILDS_FILE = saved["GUILDS_FILE"]
            srv._guilds_dirty = saved["_guilds_dirty"]
            srv._rebuild_guild_index()
            srv.send = saved["send"]


asyncio.run(main())
print("ALL_INTEL_QUERY_OK")
