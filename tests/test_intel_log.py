"""Shared intel pool write-path tests for issue #504 (PR 2, no live server).

Run from the repo root:  python tests/test_intel_log.py
Covers the `intel log` verb end to end: guild scoping, no rank or presence
gate, coalescing, both per-scope caps and the world cap through the write
path, the TTL, death notes, the pool drop on disband, and the save/load
round trip of what the verb wrote. PR 3 owns the read path, so the three
query counters must stay at zero here.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv

READ_COUNTERS = ("queries_served", "cache_hits", "cache_misses")


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


def clear_pool(rows=None, seq=0):
    srv.INTEL_ROWS[:] = list(rows or [])
    srv.INTEL_SEQ = seq


def read_json(path):
    with open(path) as f:
        return json.load(f)


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

        db = os.path.join(tmp, "intel.json")
        srv.INTEL_FILE = db
        srv.INTEL_FILE_BLOCKED = False
        srv.INTEL_SEQ = 0
        srv.INTEL_ROWS[:] = []
        srv._intel_dirty = False
        srv.GUILDS_FILE = os.path.join(tmp, "guilds.json")
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv.send = fake_send
        for key in srv.INTEL_STATS:
            srv.INTEL_STATS[key] = 0
        players = []
        try:
            # --- two guilds: Aria leads one, Dana the other ---------------
            aria = mkplayer("Aria", 70401, "deep_forest")
            bob = mkplayer("Bob", 70402, "forest_edge")
            cid = mkplayer("Cid", 70403, "market")
            dana = mkplayer("Dana", 70404, "graveyard")
            players.extend([aria, bob, cid, dana])
            now = time.time()
            choir = srv.new_guild_record(1, "Intel Choir", "Aria", now)
            choir["members"]["bob"] = {"rank": "member", "joined_ts": now}
            srv.GUILDS[1] = choir
            srv.GUILDS[2] = srv.new_guild_record(2, "Other Choir", "Dana", now)
            srv._rebuild_guild_index()
            gid = 1

            # --- the verb is wired like every other top-level command -----
            assert srv.HANDLERS["intel"] is srv.cmd_intel
            assert "intel" not in srv.SCORE_ARG_EXTRACTORS
            assert srv._command_ticks_buffs("intel", {}) is False
            inbox.clear()
            await srv.cmd_help(aria, {})
            assert "intel [log]" in inbox[-1]["text"], inbox[-1]["text"]
            assert set(srv._intel_snapshot()["counters"]) == {
                "observations_logged",
                "observations_refused",
                "queries_served",
                "cache_hits",
                "cache_misses",
                "entries_evicted",
                "entries_expired",
            }
            for key in READ_COUNTERS:
                assert srv.INTEL_STATS[key] == 0, key
            print("SURFACE_OK")

            # --- a guildless caller is refused and the counter moves ------
            refused = srv.INTEL_STATS["observations_refused"]
            srv._intel_dirty = False
            inbox.clear()
            await srv.cmd_intel(cid, {"action": "log", "kind": "hostile"})
            assert texts(inbox) == [
                "You are not in a guild. Join one to share intel."
            ], texts(inbox)
            assert srv.INTEL_STATS["observations_refused"] == refused + 1
            assert srv.INTEL_STATS["observations_logged"] == 0
            assert srv.INTEL_ROWS == [] and srv._intel_dirty is False
            print("NO_GUILD_OK")

            # --- refusals name what exists and never write a row ----------
            refused = srv.INTEL_STATS["observations_refused"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "query"})
            assert texts(inbox) == ["Unknown intel action 'query'. Use log."], texts(
                inbox
            )
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log", "kind": "market"})
            expected = (
                "Unknown intel kind 'market'. Use hostile, npc_death, "
                "player_death, node, or traffic."
            )
            assert texts(inbox) == [expected], texts(inbox)
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log", "kind": "player_death"})
            assert texts(inbox) == [
                "player_death notes are recorded by the server when they happen."
            ], texts(inbox)
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "log", "kind": "hostile", "room": "forest_edge"}
            )
            assert texts(inbox) == [
                "You can only log the room you stand in, not 'forest_edge'."
            ], texts(inbox)
            assert srv.INTEL_STATS["observations_refused"] == refused + 4
            assert srv.INTEL_ROWS == []
            print("REFUSALS_OK")

            # --- a member logs one sighting from their own room -----------
            n_log = srv.INTEL_STATS["observations_logged"]
            srv._intel_dirty = False
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "log", "kind": "hostile", "subject": "treant"}
            )
            reply = inbox[-1]
            assert reply["type"] == "intel" and reply["action"] == "log", reply
            assert reply["room"] == "deep_forest" and reply["logged"] == 1, reply
            assert reply["text"] == "Logged 1 observation(s) from Deep Forest.", reply
            assert srv.INTEL_STATS["observations_logged"] == n_log + 1
            assert len(srv.INTEL_ROWS) == 1
            sighting = srv.INTEL_ROWS[0]
            assert sighting["seq"] > 0
            assert sighting["guild"] == gid
            assert sighting["observer"] == "aria"
            assert sighting["room"] == "deep_forest"
            assert sighting["kind"] == "hostile" and sighting["subject"] == "treant"
            assert sighting["value"] == "alive"
            assert sighting["confidence"] == 100 and sighting["source"] == "sensor"
            assert sighting["repeats"] == 1
            assert sighting["first_ts"] == sighting["ts"]
            assert srv._intel_dirty is True, "a write must dirty the intel file"
            print("LOG_ONE_OK")

            # --- a plain member logs too: no rank gate, no hall gate ------
            n_log = srv.INTEL_STATS["observations_logged"]
            inbox.clear()
            await srv.cmd_intel(
                bob, {"action": "log", "kind": "hostile", "subject": "wolf"}
            )
            assert inbox[-1]["logged"] == 1, inbox[-1]
            assert srv.INTEL_STATS["observations_logged"] == n_log + 1
            assert srv.INTEL_ROWS[-1]["observer"] == "bob"
            assert srv.INTEL_ROWS[-1]["subject"] == "wolf"
            print("MEMBER_LOGS_OK")

            # --- a repeat inside the window refreshes, never appends ------
            n_log = srv.INTEL_STATS["observations_logged"]
            count = len(srv.INTEL_ROWS)
            first_ts = sighting["first_ts"]
            observed_ts = sighting["ts"]
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "log", "kind": "hostile", "subject": "treant"}
            )
            assert len(srv.INTEL_ROWS) == count, "a coalesced repeat appended a row"
            assert srv.INTEL_ROWS[0] is sighting
            assert sighting["repeats"] == 2
            assert sighting["first_ts"] == first_ts
            assert sighting["ts"] >= observed_ts
            assert srv.INTEL_STATS["observations_logged"] == n_log + 1
            print("COALESCE_OK")

            # --- one row per hostile present, plus the traffic bucket -----
            clear_pool()
            hostiles = [n for n in srv.npcs_in_room("deep_forest") if n.get("hostile")]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log"})
            assert inbox[-1]["logged"] == len(hostiles) + 1, inbox[-1]
            assert {r["subject"] for r in srv.INTEL_ROWS} == {
                n["id"] for n in hostiles
            } | {None}
            assert [r["kind"] for r in srv.INTEL_ROWS].count("traffic") == 1
            print("SCAN_ALL_OK")

            # --- the window itself: 200s coalesces, 301s later appends ----
            clear_pool()
            srv._intel_dirty = False
            t0 = time.time() - 1000
            first = srv._intel_append(
                gid, "Aria", "deep_forest", "hostile", "treant", "alive", now=t0
            )
            again = srv._intel_append(
                gid, "aria", "deep_forest", "hostile", "treant", "alive", now=t0 + 200
            )
            assert again is first
            assert first["repeats"] == 2 and first["first_ts"] == t0
            assert first["ts"] == t0 + 200
            assert len(srv.INTEL_ROWS) == 1
            later = srv._intel_append(
                gid, "Aria", "deep_forest", "hostile", "treant", "alive", now=t0 + 501
            )
            assert later is not first
            assert len(srv.INTEL_ROWS) == 2, "a repeat past the window must append"
            assert later["seq"] > first["seq"] and later["repeats"] == 1
            print("COALESCE_WINDOW_OK")

            # --- kind/subject/value narrow the scan; a miss is refused ----
            n_refused = srv.INTEL_STATS["observations_refused"]
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log", "kind": "node"})
            assert texts(inbox) == ["Nothing in Deep Forest matches that."], texts(
                inbox
            )
            assert srv.INTEL_STATS["observations_refused"] == n_refused + 1
            clear_pool()
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "log", "kind": "hostile", "subject": "troll"}
            )
            assert texts(inbox) == ["Nothing in Deep Forest matches that."], texts(
                inbox
            )
            clear_pool()
            inbox.clear()
            await srv.cmd_intel(
                aria, {"action": "log", "kind": "hostile", "value": "dead"}
            )
            assert texts(inbox) == ["Nothing in Deep Forest matches that."], texts(
                inbox
            )
            clear_pool()
            move(aria, "smithy")
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log", "kind": "node"})
            assert inbox[-1]["logged"] == 2, inbox[-1]
            assert {(r["kind"], r["value"]) for r in srv.INTEL_ROWS} == {
                ("node", "ready")
            }
            assert {r["subject"] for r in srv.INTEL_ROWS} == {
                "iron_ore_node",
                "scrap_iron_node",
            }
            print("SCAN_FILTERS_OK")

            # --- a bare log is the whole room: one traffic row as a bucket
            clear_pool()
            move(aria, "town_square")
            inbox.clear()
            await srv.cmd_intel(aria, {"action": "log"})
            assert inbox[-1]["logged"] == 1, inbox[-1]
            assert inbox[-1]["text"] == "Logged 1 observation(s) from Town Square."
            traffic = srv.INTEL_ROWS[0]
            assert traffic["kind"] == "traffic"
            assert traffic["subject"] is None
            assert traffic["value"] == "1-3", "the caller is in the room"
            print("TRAFFIC_BUCKET_OK")

            # --- per-observer cap: the oldest row of that observer goes ---
            clear_pool()
            srv._intel_dirty = False
            t = time.time() - 500
            for i in range(srv.INTEL_MAX_PER_OBSERVER):
                srv._intel_append(
                    gid, "Aria", "deep_forest", "hostile", f"mob{i}", "alive", now=t + i
                )
            n_evicted = srv.INTEL_STATS["entries_evicted"]
            assert len(srv.INTEL_ROWS) == srv.INTEL_MAX_PER_OBSERVER
            srv._intel_append(
                gid, "Aria", "deep_forest", "hostile", "mob_last", "alive", now=t + 100
            )
            assert len(srv.INTEL_ROWS) == srv.INTEL_MAX_PER_OBSERVER
            assert srv.INTEL_STATS["entries_evicted"] == n_evicted + 1
            assert "mob0" not in {r["subject"] for r in srv.INTEL_ROWS}
            assert "mob_last" in {r["subject"] for r in srv.INTEL_ROWS}
            print("OBSERVER_CAP_OK")

            # --- per-guild cap: 216 rows in one guild settle at 200 -------
            clear_pool()
            per_observer = srv.INTEL_MAX_PER_OBSERVER
            n_evicted = srv.INTEL_STATS["entries_evicted"]
            t = time.time() - 4000
            seq = 0
            for o in range(9):
                for i in range(per_observer):
                    seq += 1
                    srv._intel_append(
                        gid,
                        f"bot{o}",
                        "deep_forest",
                        "hostile",
                        f"mob{o}_{i}",
                        "alive",
                        now=t + seq,
                    )
            assert len(srv.INTEL_ROWS) == srv.INTEL_MAX_PER_GUILD, len(srv.INTEL_ROWS)
            assert srv.INTEL_STATS["entries_evicted"] == n_evicted + (216 - 200)
            print("GUILD_CAP_OK")

            # --- world cap: fair eviction takes from the largest guild ----
            clear_pool()
            guilds = srv.INTEL_MAX_ENTRIES // srv.INTEL_MAX_PER_GUILD
            t0 = time.time() - 10000
            seeded = []
            seq = 0
            for g in range(1, guilds + 1):
                for i in range(srv.INTEL_MAX_PER_GUILD):
                    seq += 1
                    seeded.append(
                        row(
                            seq,
                            t0 + i,
                            guild=g,
                            observer=f"g{g}o{i % 9}",
                            subject=f"mob{g}_{i}",
                        )
                    )
            clear_pool(seeded, seq)
            assert len(srv.INTEL_ROWS) == srv.INTEL_MAX_ENTRIES
            n_evicted = srv.INTEL_STATS["entries_evicted"]
            newest = srv._intel_append(
                999, "Aria", "deep_forest", "hostile", "treant", "alive"
            )
            assert newest is not None
            assert len(srv.INTEL_ROWS) == srv.INTEL_MAX_ENTRIES
            assert srv.INTEL_STATS["entries_evicted"] == n_evicted + 1
            assert 1 not in {
                r["seq"] for r in srv.INTEL_ROWS
            }, "the oldest row survived"
            assert newest in srv.INTEL_ROWS
            print("WORLD_CAP_OK")

            # --- TTL: an expired row goes on the next append --------------
            clear_pool(
                [
                    row(1, time.time() - srv.INTEL_TTL_SECONDS - 60, subject="stale"),
                    row(2, time.time() - 10, subject="still_fresh"),
                ],
                2,
            )
            n_expired = srv.INTEL_STATS["entries_expired"]
            srv._intel_append(gid, "Aria", "deep_forest", "node", "ore_vein", "ready")
            assert srv.INTEL_STATS["entries_expired"] == n_expired + 1
            assert "stale" not in {r["subject"] for r in srv.INTEL_ROWS}
            assert "still_fresh" in {r["subject"] for r in srv.INTEL_ROWS}
            print("TTL_OK")

            # --- a death writes a note into the dead player's own pool ----
            clear_pool()
            n_log = srv.INTEL_STATS["observations_logged"]
            move(aria, "deep_forest")
            await srv.respawn_player(aria)
            deaths = [r for r in srv.INTEL_ROWS if r["kind"] == "player_death"]
            assert len(deaths) == 1, deaths
            note = deaths[0]
            assert note["guild"] == gid, "the note must land in the dead player's guild"
            assert note["observer"] == "aria"
            assert note["room"] == "deep_forest", "the room the player died in"
            assert note["subject"] is None and note["value"] is None
            assert note["source"] == "sensor" and note["confidence"] == 100
            assert aria.room == srv.START_ROOM
            assert srv.INTEL_STATS["observations_logged"] == n_log + 1
            assert not [r for r in srv.INTEL_ROWS if r["guild"] == 2]
            n_log = srv.INTEL_STATS["observations_logged"]
            move(cid, "market")
            await srv.respawn_player(cid)
            assert not [r for r in srv.INTEL_ROWS if r["observer"] == "cid"]
            assert srv.INTEL_STATS["observations_logged"] == n_log
            print("DEATH_NOTE_OK")

            # --- a kill writes a npc_death note to the killer's own pool --
            clear_pool()
            move(aria, "graveyard")
            ghost = srv.find_npc_in_room("graveyard", "ghost")
            assert ghost is not None, "graveyard lost its ghost"
            ghost_id = ghost["id"]
            ghost_saved = (ghost["hp"], ghost["alive"])
            n_log = srv.INTEL_STATS["observations_logged"]
            ghost["hp"] = 1
            inbox.clear()
            await srv.cmd_attack(aria, {"target": "ghost"})
            kills = [r for r in srv.INTEL_ROWS if r["kind"] == "npc_death"]
            assert len(kills) == 1, kills
            kill_note = kills[0]
            assert kill_note["guild"] == gid, "the note must land in the killer's guild"
            assert kill_note["observer"] == "aria", "the player who landed the blow"
            assert kill_note["room"] == "graveyard"
            assert kill_note["subject"] == ghost_id
            assert kill_note["value"] is None
            assert kill_note["source"] == "sensor" and kill_note["confidence"] == 100
            assert srv.INTEL_STATS["observations_logged"] == n_log + 1
            assert ghost["alive"] is False and ghost["respawn_at"] > time.time()
            # a second swing at the corpse is refused and moves no counter
            n_log = srv.INTEL_STATS["observations_logged"]
            inbox.clear()
            await srv.cmd_attack(aria, {"target": "ghost"})
            assert texts(inbox) == ["No 'ghost' here to attack."], texts(inbox)
            assert srv.INTEL_STATS["observations_logged"] == n_log
            assert len(srv.INTEL_ROWS) == 1
            # a guildless killer writes nothing at all
            move(cid, "old_shop")
            rat = srv.find_npc_in_room("old_shop", "rat")
            assert rat is not None, "old_shop lost its rat"
            rat_saved = (rat["hp"], rat["alive"])
            rat["hp"] = 1
            n_log = srv.INTEL_STATS["observations_logged"]
            inbox.clear()
            await srv.cmd_attack(cid, {"target": "rat"})
            assert rat["alive"] is False, texts(inbox)
            assert not [r for r in srv.INTEL_ROWS if r["observer"] == "cid"]
            assert srv.INTEL_STATS["observations_logged"] == n_log
            assert len(srv.INTEL_ROWS) == 1
            for npc, hp, alive in (
                (ghost, ghost_saved[0], ghost_saved[1]),
                (rat, rat_saved[0], rat_saved[1]),
            ):
                npc["hp"] = hp
                npc["alive"] = alive
                npc["respawn_at"] = None
                npc["contributors"] = {}
            print("NPC_DEATH_OK")

            # --- disband drops that guild's pool and nothing else ---------
            clear_pool(
                [
                    row(1, time.time() - 20, guild=1, observer="aria"),
                    row(2, time.time() - 20, guild=2, observer="dana"),
                ],
                2,
            )
            srv._intel_dirty = False
            move(dana, "guild_hall")
            inbox.clear()
            await srv.cmd_guild(dana, {"action": "disband"})
            assert texts(inbox) == ["Other Choir is disbanded."], texts(inbox)
            assert [r["guild"] for r in srv.INTEL_ROWS] == [1]
            assert srv._intel_dirty is True
            print("DISBAND_DROP_OK")

            # --- what the verb wrote survives save and load --------------
            clear_pool(
                [
                    row(1, time.time() - 30),
                    row(2, time.time() - 20, kind="traffic", subject=None, value="1-3"),
                ],
                2,
            )
            assert srv.save_intel() is True
            stored = read_json(db)
            assert stored["version"] == srv.INTEL_VERSION and stored["seq"] == 2
            assert [r["kind"] for r in stored["observations"]] == ["hostile", "traffic"]
            clear_pool()
            loaded, seq = srv.load_intel()
            assert seq == 2
            clear_pool(loaded, seq)
            assert [r["kind"] for r in srv.INTEL_ROWS] == ["hostile", "traffic"]
            assert srv.INTEL_ROWS[1]["value"] == "1-3"
            print("ROUND_TRIP_OK")

            # --- the three read counters belong to PR 3 and stay at zero --
            for key in READ_COUNTERS:
                assert srv.INTEL_STATS[key] == 0, (key, srv.INTEL_STATS[key])
            assert srv.INTEL_STATS["observations_logged"] > 0
            assert srv.INTEL_STATS["observations_refused"] > 0
            assert srv.INTEL_STATS["entries_evicted"] > 0
            assert srv.INTEL_STATS["entries_expired"] > 0
            snapshot = srv._intel_snapshot()
            assert set(snapshot) == {"since", "counters", "ages"}
            print("COUNTERS_OK")
        finally:
            for pl in players:
                unplayer(pl)
            for key in set(srv.SCORES) - keys0:
                srv.SCORES.pop(key, None)
            srv.INTEL_FILE = saved["INTEL_FILE"]
            srv.INTEL_FILE_BLOCKED = saved["INTEL_FILE_BLOCKED"]
            srv.INTEL_ROWS[:] = saved["INTEL_ROWS"]
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
print("ALL_INTEL_LOG_OK")
