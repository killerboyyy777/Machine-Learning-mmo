"""Guild audit tests for issue #588 (PR 5, no live server).

Run from the repo root:  python tests/test_guild_audit.py
Covers the single `_guild_audit` writer, one entry per successful mutation and
none per refusal, INV-6 (append-only, strictly increasing seq, 500-entry cap)
across a save/load round trip, `gold_after` as the conservation trail, the
`guild audit [n]` reader (members only), and the capped `guild_feed` ring.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv

AUDIT_KEYS = [
    "seq",
    "ts",
    "actor",
    "action",
    "target",
    "amount",
    "item",
    "items",
    "rank_from",
    "rank_to",
    "gold_after",
]


class FakeWS:
    remote_address = ("127.0.0.1", 1)


def texts(inbox):
    return [m["text"] for m in inbox if isinstance(m, dict) and "text" in m]


def index_ok():
    for gid, record in srv.GUILDS.items():
        for key in record["members"]:
            assert srv.GUILD_BY_MEMBER.get(key) == gid, (key, gid)
    for key, gid in srv.GUILD_BY_MEMBER.items():
        assert key in srv.GUILDS[gid]["members"], (key, gid)


def frozen(entries):
    return [json.dumps(entry, sort_keys=True) for entry in entries]


def seqs(record):
    return [entry["seq"] for entry in record["audit"]]


def added(record, before):
    return record["audit"][before:]


def write_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f)


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = srv.GUILDS_FILE
        saved_blocked = srv.GUILDS_FILE_BLOCKED
        saved_guilds = srv.GUILDS
        saved_stones = srv.GUILD_TOMBSTONES
        saved_send = srv.send
        saved_dirty = srv._guilds_dirty
        saved_invites = dict(srv._pending_guild_invites)
        saved_written = srv.GUILD_AUDIT_ENTRIES_WRITTEN
        saved_feed = list(srv.guild_feed)
        keys0 = set(srv.SCORES)
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        def mkplayer(name, pid, gold=0, inventory=None, room="guild_hall"):
            pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
            pl.room = room
            pl.gold = gold
            pl.inventory = list(inventory or [])
            srv.players[pid] = pl
            srv.add_member(pl)
            # Login mints the score entry, and display names ride on it.
            srv.get_score_entry(name)
            return pl

        def unplayer(pl):
            srv.remove_member(pl)
            srv.players.pop(pl.id, None)

        def make_guild(name, rows):
            gid = max(srv.GUILDS) + 1 if srv.GUILDS else 1
            record = srv.new_guild_record(gid, name, rows[0][0])
            record["members"] = {
                key: {"rank": rank, "joined_ts": ts} for key, rank, ts in rows
            }
            srv.GUILDS[gid] = record
            srv._rebuild_guild_index()
            return record

        srv.GUILDS_FILE = os.path.join(tmp, "guilds.json")
        srv.GUILDS_FILE_BLOCKED = False
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv._pending_guild_invites.clear()
        srv.GUILD_AUDIT_ENTRIES_WRITTEN = 0
        srv.guild_feed.clear()
        srv._rebuild_guild_index()
        srv.send = fake_send
        players = []
        try:
            # --- surface: the new action is routed, listed and read-only ---
            assert srv.HANDLERS["guild"] is srv.cmd_guild
            assert srv._command_ticks_buffs("guild", {"action": "audit"}) is False
            aria = mkplayer("Aria", 70501, gold=1000, inventory=["rat_tail"])
            bram = mkplayer("Bram", 70502, gold=100)
            cid = mkplayer("Cid", 70503, gold=50)
            dune = mkplayer("Dune", 70504, gold=10)
            eve = mkplayer("Eve", 70505, gold=10, inventory=["rat_tail"])
            fay = mkplayer("Fay", 70506, gold=10)
            players.extend([aria, bram, cid, dune, eve, fay])
            inbox.clear()
            await srv.cmd_help(aria, {})
            help_text = inbox[-1]["text"]
            assert (
                "guild [create|info|invite|join|leave|kick|promote|demote|"
                "disband|vault|audit]" in help_text
            ), help_text
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "frobnicate"})
            assert texts(inbox)[-1] == (
                "Unknown guild action 'frobnicate'. Use create, info, invite, join, "
                "leave, kick, promote, demote, disband, vault, audit, "
                "permissions, transfer, or accept."
            )
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == 0
            assert len(srv.guild_feed) == 0
            print("SURFACE_OK")

            # --- the writer: one entry, one shape, seq from 1, feed +1 -----
            scratch = srv.new_guild_record(900, "Scratch Guild", "Zed")
            first = srv._guild_audit(scratch, "Zed", "create")
            assert list(first) == AUDIT_KEYS, list(first)
            assert first["seq"] == 1
            assert first["actor"] == "Zed"
            assert first["action"] == "create"
            assert first["target"] is None
            assert first["item"] is None and first["items"] is None
            assert first["gold_after"] == 0
            assert isinstance(first["ts"], float) and first["ts"] > 0
            assert scratch["audit"] == [first]
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == 1
            assert len(srv.guild_feed) == 1
            assert srv.guild_feed[-1].startswith("Scratch Guild: #1 ")
            assert "\n" not in srv.guild_feed[-1]
            scratch["vault_gold"] = 40
            second = srv._guild_audit(scratch, "Zed", "deposit_gold", amount=40)
            assert second["seq"] == 2
            assert second["gold_after"] == 40
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == 2
            print("WRITER_OK")

            # --- seq is one past the highest usable seq, junk tail or not --
            tail = srv.new_guild_record(902, "Tail Guild", "Zed")
            tail["audit"] = [
                {"seq": 7, "action": "create"},
                {"seq": 3, "action": "join"},
                "junk",
                12,
            ]
            assert srv._guild_audit(tail, "Zed", "join")["seq"] == 8, tail["audit"]
            tail["audit"] = ["junk", None, {}]
            assert srv._guild_audit(tail, "Zed", "join")["seq"] == 1
            tail["audit"] = []
            assert srv._guild_audit(tail, "Zed", "join")["seq"] == 1
            print("SEQ_TAIL_OK")

            # --- create writes exactly one entry --------------------------
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "create", "name": "Iron Choir"})
            choir = srv.GUILDS[max(srv.GUILDS)]
            assert len(choir["audit"]) == 1, choir["audit"]
            entry = choir["audit"][0]
            assert entry["action"] == "create"
            assert entry["actor"] == "Aria"
            assert entry["target"] is None
            assert entry["amount"] is None and entry["item"] is None
            assert entry["gold_after"] == 0
            assert srv._guilds_dirty is True
            print("CREATE_OK")

            # --- every successful mutation appends exactly one entry ------
            snap = frozen(choir["audit"])

            async def step(player, msg, action, target, actor=None):
                before = len(choir["audit"])
                srv._guilds_dirty = False
                inbox.clear()
                await srv.cmd_guild(player, msg)
                rows = added(choir, before)
                assert len(rows) == 1, (msg, rows)
                row = rows[0]
                assert row["action"] == action, (msg, row)
                assert row["actor"] == (actor or player.name), (msg, row)
                assert row["target"] == target, (msg, row)
                assert row["seq"] == before + 1, (msg, row)
                assert srv._guilds_dirty is True, msg
                return row

            await step(aria, {"action": "invite", "target": "Bram"}, "invite", "Bram")
            await step(bram, {"action": "join"}, "join", None)
            await step(aria, {"action": "invite", "target": "Cid"}, "invite", "Cid")
            await step(cid, {"action": "join"}, "join", None)
            row = await step(
                aria, {"action": "promote", "target": "Bram"}, "promote", "Bram"
            )
            assert row["rank_from"] == "member" and row["rank_to"] == "officer", row
            row = await step(
                aria, {"action": "demote", "target": "Bram"}, "demote", "Bram"
            )
            assert row["rank_from"] == "officer" and row["rank_to"] == "member", row
            await step(aria, {"action": "promote", "target": "Bram"}, "promote", "Bram")
            row = await step(aria, {"action": "kick", "target": "Cid"}, "kick", "Cid")
            assert row["rank_from"] == "member" and row["rank_to"] is None, row
            row = await step(
                aria,
                {"action": "vault", "op": "deposit", "gold": 120},
                "deposit_gold",
                None,
            )
            assert row["amount"] == 120 and row["item"] is None, row
            assert row["gold_after"] == 120, row
            row = await step(
                aria,
                {"action": "vault", "op": "withdraw", "gold": 20},
                "withdraw_gold",
                None,
            )
            assert row["amount"] == 20 and row["gold_after"] == 100, row
            row = await step(
                aria,
                {"action": "vault", "op": "deposit", "item": "rat tail"},
                "deposit_item",
                None,
            )
            assert row["item"] == "rat_tail" and row["amount"] == 1, row
            assert row["gold_after"] == 100, row
            row = await step(
                aria,
                {"action": "vault", "op": "withdraw", "item": "rat tail"},
                "withdraw_item",
                None,
            )
            assert row["item"] == "rat_tail" and row["amount"] == 1, row
            row = await step(bram, {"action": "leave"}, "leave", None)
            assert row["gold_after"] == 100, row
            assert frozen(choir["audit"][: len(snap)]) == snap
            assert seqs(choir) == list(range(1, len(choir["audit"]) + 1))
            index_ok()
            print("MUTATIONS_OK")

            # --- refusals write nothing ----------------------------------
            before_len = len(choir["audit"])
            before_written = srv.GUILD_AUDIT_ENTRIES_WRITTEN
            srv._guilds_dirty = False
            refusals = [
                (aria, {"action": "create", "name": "Second Choir"}),
                (aria, {"action": "create", "name": "Iron Choir"}),
                (bram, {"action": "join"}),
                (cid, {"action": "invite", "target": "Fay"}),
                (cid, {"action": "invite", "target": "Nobody"}),
                (fay, {"action": "join"}),
                (fay, {"action": "leave"}),
                (aria, {"action": "kick", "target": "Aria"}),
                (bram, {"action": "kick", "target": "Aria"}),
                (bram, {"action": "promote", "target": "Aria"}),
                (aria, {"action": "promote", "target": "Aria"}),
                (aria, {"action": "demote", "target": "Aria"}),
                (bram, {"action": "disband"}),
                (aria, {"action": "vault", "op": "deposit", "gold": 0}),
                (aria, {"action": "vault", "op": "deposit", "gold": "abc"}),
                (aria, {"action": "vault", "op": "withdraw", "gold": 10000}),
                (cid, {"action": "vault", "op": "withdraw", "gold": 1}),
                (fay, {"action": "vault", "op": "deposit", "gold": 1}),
                (aria, {"action": "vault", "op": "deposit", "item": "nothing"}),
                (aria, {"action": "vault", "op": "withdraw", "item": "nothing"}),
            ]
            for player, msg in refusals:
                inbox.clear()
                await srv.cmd_guild(player, msg)
                assert inbox[-1]["type"] == "error", (msg, inbox[-1])
            # Hall-gated refusals, then the reads that also write nothing.
            aria.room = "market"
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit", "gold": 5})
            assert texts(inbox)[-1] == (
                "Guild vaults are opened at the Adventurers Guild hall "
                "(south of Artisan Row)."
            )
            aria.room = "guild_hall"
            for msg in (
                {"action": "info"},
                {"action": "vault"},
                {"action": "audit"},
            ):
                inbox.clear()
                await srv.cmd_guild(aria, msg)
                assert inbox[-1]["type"] in ("guild", "message"), msg
            assert len(choir["audit"]) == before_len
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == before_written
            assert srv._guilds_dirty is False
            index_ok()
            print("REFUSED_OK")

            # --- INV-6: earlier entries survive later mutations -------------
            snap = frozen(choir["audit"])
            for _ in range(5):
                await srv.cmd_guild(
                    aria, {"action": "vault", "op": "deposit", "gold": 1}
                )
                await srv.cmd_guild(
                    aria, {"action": "vault", "op": "withdraw", "gold": 1}
                )
            assert frozen(choir["audit"][: len(snap)]) == snap
            assert seqs(choir) == list(range(1, len(choir["audit"]) + 1))
            print("APPEND_ONLY_OK")

            # --- gold_after replays to the live vault balance --------------
            replay = 0
            for row in choir["audit"]:
                if row["action"] == "deposit_gold":
                    replay += row["amount"]
                elif row["action"] == "withdraw_gold":
                    replay -= row["amount"]
                else:
                    continue
                assert row["gold_after"] == replay, (row, replay)
            assert replay == choir["vault_gold"], (replay, choir["vault_gold"])
            assert replay == 100, replay
            print("GOLD_AFTER_OK")

            # --- the cap trims oldest-first and seq survives the trim ------
            bulk = srv.new_guild_record(901, "Bulk Guild", "Zed")
            written0 = srv.GUILD_AUDIT_ENTRIES_WRITTEN
            total = srv.GUILD_AUDIT_CAP + 7
            for _ in range(total):
                srv._guild_audit(bulk, "Zed", "join")
            assert len(bulk["audit"]) == srv.GUILD_AUDIT_CAP
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == written0 + total
            rows = seqs(bulk)
            assert rows == list(range(total - srv.GUILD_AUDIT_CAP + 1, total + 1)), rows
            assert rows[0] == 8 and rows[-1] == total
            indices = list(range(1, len(rows)))
            assert all(rows[i] > rows[i - 1] for i in indices), rows
            kept = srv._guild_audit(bulk, "Zed", "join")
            assert kept["seq"] == total + 1
            assert len(bulk["audit"]) == srv.GUILD_AUDIT_CAP
            assert bulk["audit"][0]["seq"] == 9
            print("CAP_OK")

            # --- save/load round trip keeps every entry and its order ------
            srv.GUILDS = {choir["id"]: choir}
            srv._rebuild_guild_index()
            assert srv.save_guilds() is True
            loaded, stones = srv.load_guilds()
            assert stones == []
            assert loaded[choir["id"]]["audit"] == choir["audit"]
            assert seqs(loaded[choir["id"]]) == seqs(choir)
            assert frozen(loaded[choir["id"]]["audit"]) == frozen(choir["audit"])
            # A missing, empty or malformed audit normalizes to a safe list.
            assert srv._normalize_guild_record(1, {"name": "No Log"})["audit"] == []
            assert (
                srv._normalize_guild_record(1, {"name": "No Log", "audit": []})["audit"]
                == []
            )
            for junk in ("nope", 5, {"seq": 1}, [None, "x", 3]):
                record = srv._normalize_guild_record(
                    1, {"name": "No Log", "audit": junk}
                )
                assert record["audit"] == [], junk
            # A file over the cap is trimmed on load, not kept whole.
            rows = []
            for i in range(1, srv.GUILD_AUDIT_CAP + 101):
                rows.append(
                    {
                        "seq": i,
                        "ts": 0.0,
                        "actor": "Aria",
                        "action": "join",
                        "target": None,
                        "amount": None,
                        "item": None,
                        "items": None,
                        "rank_from": None,
                        "rank_to": None,
                        "gold_after": 0,
                    }
                )
            write_json(
                srv.GUILDS_FILE,
                {
                    "version": srv.GUILD_VERSION,
                    "guilds": {"1": {"name": "Trim Guild", "audit": rows}},
                    "tombstones": [],
                },
            )
            loaded, _ = srv.load_guilds()
            assert len(loaded[1]["audit"]) == srv.GUILD_AUDIT_CAP
            assert loaded[1]["audit"][0]["seq"] == 101
            assert loaded[1]["audit"][-1]["seq"] == srv.GUILD_AUDIT_CAP + 100
            srv.GUILDS = {choir["id"]: choir}
            srv._rebuild_guild_index()
            print("ROUNDTRIP_OK")

            # --- the reader: members only, newest first, clamped -----------
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit"})
            body = inbox[-1]["text"]
            assert body.startswith("Guild audit, newest first:\n"), body
            lines = body.split("\n")[1:]
            newest = choir["audit"][-1]["seq"]
            assert len(lines) == min(srv.GUILD_AUDIT_SHOW_DEFAULT, len(choir["audit"]))
            assert lines[0].startswith(f"#{newest} "), lines[0]
            assert lines[0].endswith("withdrew 1 gold."), lines[0]
            assert lines[1].startswith(f"#{newest - 1} "), lines[1]
            assert "{" not in body and '"seq"' not in body
            assert "None" not in body
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit", "n": 2})
            assert len(inbox[-1]["text"].split("\n")[1:]) == 2
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit", "n": 0})
            assert len(inbox[-1]["text"].split("\n")[1:]) == 1
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit", "n": -4})
            assert len(inbox[-1]["text"].split("\n")[1:]) == 1
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit", "n": 9999})
            assert len(inbox[-1]["text"].split("\n")[1:]) == min(
                srv.GUILD_AUDIT_SHOW_MAX, len(choir["audit"])
            )
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit", "n": "nope"})
            assert len(inbox[-1]["text"].split("\n")[1:]) == min(
                srv.GUILD_AUDIT_SHOW_DEFAULT, len(choir["audit"])
            )
            # The oldest line still renders: a create has no amount or item.
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "audit", "n": srv.GUILD_AUDIT_SHOW_MAX}
            )
            assert len(choir["audit"]) <= srv.GUILD_AUDIT_SHOW_MAX
            oldest = inbox[-1]["text"].split("\n")[-1]
            assert oldest.startswith("#1 "), oldest
            assert oldest.endswith(" Aria founded the guild."), oldest
            quiet = make_guild("Quiet Hall", [("cid", "leader", 1.0)])
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "audit"})
            assert texts(inbox)[-1] == "Quiet Hall has no recorded events."
            srv.GUILDS.pop(quiet["id"], None)
            srv._rebuild_guild_index()
            before_len = len(choir["audit"])
            before_written = srv.GUILD_AUDIT_ENTRIES_WRITTEN
            inbox.clear()
            await srv.cmd_guild(fay, {"action": "audit"})
            assert texts(inbox)[-1] == "You are not in a guild."
            assert len(choir["audit"]) == before_len
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == before_written
            index_ok()
            print("READ_OK")

            # --- guild_feed grows one per entry and stays capped -----------
            written0 = srv.GUILD_AUDIT_ENTRIES_WRITTEN
            feed0 = len(srv.guild_feed)
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit", "gold": 2})
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == written0 + 1
            assert len(srv.guild_feed) == min(50, feed0 + 1)
            for _ in range(60):
                srv._guild_audit(scratch, "Zed", "join")
            assert len(srv.guild_feed) == 50
            assert all(isinstance(line, str) for line in srv.guild_feed)
            assert all("#" in line and "\n" not in line for line in srv.guild_feed)
            print("FEED_OK")

            # --- disband records the vault it returned ---------------------
            choir["vault_items"] = ["rat_tail", "healing_herb"]
            choir["vault_gold"] = 120
            gid = choir["id"]
            before = len(choir["audit"])
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "disband"})
            rows = added(choir, before)
            assert len(rows) == 1, rows
            assert rows[0]["action"] == "disband"
            assert rows[0]["actor"] == "Aria"
            assert rows[0]["amount"] == 120, rows[0]
            assert rows[0]["item"] is None, rows[0]
            assert rows[0]["items"] == 2, rows[0]
            assert rows[0]["gold_after"] == 0, rows[0]
            assert srv._guild_audit_line(rows[0]).endswith(
                "disbanded the guild (120 gold, 2 item(s))."
            ), rows[0]
            assert gid not in srv.GUILDS
            index_ok()
            print("DISBAND_OK")

            # --- succession and a last-member dissolve both log -----------
            watch = make_guild(
                "Night Watch", [("dune", "leader", 10.0), ("eve", "member", 20.0)]
            )
            watch["vault_gold"] = 30
            before = len(watch["audit"])
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "leave"})
            rows = added(watch, before)
            assert [row["action"] for row in rows] == ["leave", "succession"], rows
            assert rows[0]["actor"] == "Dune" and rows[0]["target"] is None
            assert rows[1]["actor"] == "Dune"
            assert rows[1]["target"] == "Eve", rows[1]
            assert rows[1]["rank_from"] == "member"
            assert rows[1]["rank_to"] == "leader"
            assert watch["members"]["eve"]["rank"] == "leader"
            assert seqs(watch) == [1, 2]
            before = len(watch["audit"])
            inbox.clear()
            await srv.cmd_guild(eve, {"action": "leave"})
            rows = added(watch, before)
            assert [row["action"] for row in rows] == ["leave", "disband"], rows
            assert rows[1]["amount"] == 30, rows[1]
            assert rows[1]["item"] is None, rows[1]
            assert rows[1]["items"] == 0, rows[1]
            assert rows[1]["gold_after"] == 0, rows[1]
            assert watch["id"] not in srv.GUILDS
            index_ok()
            print("SUCCESSION_OK")

            # --- telemetry, index and the SCORES rule still hold ----------
            index_ok()
            for entry in srv.SCORES.values():
                assert "guild" not in entry, entry
            telemetry = srv.guild_telemetry()
            total_entries = sum(len(record["audit"]) for record in srv.GUILDS.values())
            assert telemetry["audit_entries"] == total_entries, telemetry
            assert telemetry["audit_entries_written"] == srv.GUILD_AUDIT_ENTRIES_WRITTEN
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN > 0
            print("INDEX_INVARIANT_OK")
        finally:
            for pl in players:
                unplayer(pl)
            for key in set(srv.SCORES) - keys0:
                srv.SCORES.pop(key, None)
            srv.GUILDS_FILE = saved_file
            srv.GUILDS_FILE_BLOCKED = saved_blocked
            srv.GUILDS = saved_guilds
            srv.GUILD_TOMBSTONES = saved_stones
            srv._rebuild_guild_index()
            srv.send = saved_send
            srv._guilds_dirty = saved_dirty
            srv._pending_guild_invites.clear()
            srv._pending_guild_invites.update(saved_invites)
            srv.GUILD_AUDIT_ENTRIES_WRITTEN = saved_written
            srv.guild_feed.clear()
            srv.guild_feed.extend(saved_feed)


asyncio.run(main())
print("ALL_GUILD_AUDIT_OK")
