"""Guild vault tests for issue #588 (PR 4, no live server).

Run from the repo root:  python tests/test_guild_vault.py
Covers gold deposit/withdraw (INV-1), item deposit/withdraw with the pack-full
bank path (INV-2), the item cap (INV-7), officer daily caps and their UTC date
reset, the rank matrix, the hall gate, and the vault telemetry counters.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


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


def vault_gold(record):
    return record["vault_gold"]


def vault_items(record):
    return list(record["vault_items"])


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = srv.GUILDS_FILE
        saved_guilds = srv.GUILDS
        saved_stones = srv.GUILD_TOMBSTONES
        saved_send = srv.send
        saved_dirty = srv._guilds_dirty
        saved_in = srv.GUILD_VAULT_GOLD_IN_TOTAL
        saved_out = srv.GUILD_VAULT_GOLD_OUT_TOTAL
        saved_audit = srv.GUILD_AUDIT_ENTRIES_WRITTEN
        saved_feed = list(srv.guild_feed)
        keys0 = set(srv.SCORES)
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)
            # Yield so asyncio.gather actually interleaves concurrent commands.
            await asyncio.sleep(0)

        def mkplayer(name, pid, gold=0, inventory=None, room="guild_hall"):
            pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
            pl.room = room
            pl.gold = gold
            pl.inventory = list(inventory or [])
            srv.players[pid] = pl
            srv.add_member(pl)
            # Login mints the score entry, and item banks ride on it.
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
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv.GUILD_VAULT_GOLD_IN_TOTAL = 0
        srv.GUILD_VAULT_GOLD_OUT_TOTAL = 0
        srv.GUILD_AUDIT_ENTRIES_WRITTEN = 0
        srv.guild_feed.clear()
        srv._rebuild_guild_index()
        srv.send = fake_send
        players = []
        try:
            # --- command surface, help, buff gate, and permission matrix ---
            assert srv.HANDLERS["guild"] is srv.cmd_guild
            assert srv._command_ticks_buffs("guild", {"action": "vault"}) is False
            assert (
                srv._command_ticks_buffs("guild", {"action": "vault", "op": ""})
                is False
            )
            assert (
                srv._command_ticks_buffs(
                    "guild", {"action": "vault", "op": "deposit", "gold": 1}
                )
                is True
            )
            assert (
                srv._command_ticks_buffs(
                    "guild", {"action": "vault", "op": "withdraw", "gold": 1}
                )
                is True
            )
            aria = mkplayer("Aria", 70401, gold=1000, inventory=["rat_tail"])
            bram = mkplayer("Bram", 70402, gold=500)
            cid = mkplayer("Cid", 70403, gold=100, inventory=["rat_tail"])
            dune = mkplayer("Dune", 70404, gold=100, inventory=["rat_tail"])
            players.extend([aria, bram, cid, dune])
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
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "bogus"})
            assert texts(inbox)[-1] == (
                "Unknown vault action 'bogus'. Use deposit or withdraw."
            )
            choir = make_guild(
                "Iron Choir",
                [
                    ("aria", "leader", 10.0),
                    ("bram", "officer", 20.0),
                    ("cid", "member", 30.0),
                ],
            )
            assert srv._guild_allows(choir, "aria", "deposit") is True
            assert srv._guild_allows(choir, "bram", "deposit") is True
            assert srv._guild_allows(choir, "cid", "deposit") is True
            assert srv._guild_allows(choir, "dune", "deposit") is False
            assert srv._guild_allows(choir, "aria", "withdraw") is True
            assert srv._guild_allows(choir, "bram", "withdraw") is True
            assert srv._guild_allows(choir, "cid", "withdraw") is False
            assert srv._guild_allows(choir, "dune", "withdraw") is False
            index_ok()
            print("SURFACE_OK")

            # --- INV-1: gold deposit and withdraw move exactly once -------
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit", "gold": 120})
            assert texts(inbox)[0] == (
                "You deposit 120 gold. The vault holds 120 gold."
            )
            assert aria.gold == 880
            assert vault_gold(choir) == 120
            assert aria.gold + vault_gold(choir) == 1000
            assert srv.GUILD_VAULT_GOLD_IN_TOTAL == 120
            assert srv.GUILD_VAULT_GOLD_OUT_TOTAL == 0
            assert srv._guilds_dirty is True
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit", "gold": 30})
            assert texts(inbox)[0] == "You deposit 30 gold. The vault holds 150 gold."
            assert aria.gold == 850
            assert aria.gold + vault_gold(choir) == 1000
            assert srv.GUILD_VAULT_GOLD_IN_TOTAL == 150
            assert srv._guilds_dirty is True
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "withdraw", "gold": 50})
            assert texts(inbox)[0] == (
                "You withdraw 50 gold. The vault holds 100 gold."
            )
            assert aria.gold == 900
            assert vault_gold(choir) == 100
            assert aria.gold + vault_gold(choir) == 1000
            assert srv.GUILD_VAULT_GOLD_IN_TOTAL == 150
            assert srv.GUILD_VAULT_GOLD_OUT_TOTAL == 50
            assert srv._guilds_dirty is True
            telemetry = srv.guild_telemetry()
            assert telemetry["vault_gold_total"] == 100
            assert telemetry["vault_gold_in_total"] == 150
            assert telemetry["vault_gold_out_total"] == 50
            assert (
                telemetry["vault_gold_in_total"] - telemetry["vault_gold_out_total"]
                == telemetry["vault_gold_total"]
            )
            index_ok()
            print("GOLD_OK")

            # --- bad amounts never move a coin ----------------------------
            assert srv._guild_gold_amount(3) == 3
            assert srv._guild_gold_amount("7") == 7
            assert srv._guild_gold_amount(True) is None
            assert srv._guild_gold_amount(False) is None
            assert srv._guild_gold_amount(1.5) is None
            assert srv._guild_gold_amount("abc") is None
            assert srv._guild_gold_amount(None) is None
            before = (
                aria.gold,
                vault_gold(choir),
                srv.GUILD_VAULT_GOLD_IN_TOTAL,
                srv.GUILD_VAULT_GOLD_OUT_TOTAL,
            )
            bad_deposits = [0, -5, 1.5, True, "abc", None]
            for amount in bad_deposits:
                inbox.clear()
                await srv.cmd_guild(
                    aria, {"action": "vault", "op": "deposit", "gold": amount}
                )
                assert texts(inbox)[-1] == (
                    "Enter a whole amount of gold above zero."
                ), amount
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit"})
            assert texts(inbox)[-1] == "Choose gold or an item to move."
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "deposit", "gold": 10000}
            )
            assert texts(inbox)[-1] == "You only have 900 gold."
            bad_withdraws = [0, -1, 2.5, False, "x", None]
            for amount in bad_withdraws:
                inbox.clear()
                await srv.cmd_guild(
                    aria, {"action": "vault", "op": "withdraw", "gold": amount}
                )
                assert texts(inbox)[-1] == (
                    "Enter a whole amount of gold above zero."
                ), amount
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "withdraw"})
            assert texts(inbox)[-1] == "Choose gold or an item to move."
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "withdraw", "gold": 10000}
            )
            assert texts(inbox)[-1] == "The guild vault holds only 100 gold."
            after = (
                aria.gold,
                vault_gold(choir),
                srv.GUILD_VAULT_GOLD_IN_TOTAL,
                srv.GUILD_VAULT_GOLD_OUT_TOTAL,
            )
            assert after == before, (before, after)
            index_ok()
            print("GOLD_BAD_OK")

            # --- INV-2: item deposit/withdraw conserve the multiset ------
            aria.inventory = ["rat_tail", "healing_herb", "rusty_sword"]
            choir["vault_items"] = []
            multiset_before = sorted(aria.inventory + vault_items(choir))
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "deposit", "item": "rat tail"}
            )
            assert texts(inbox)[0] == (
                "You deposit Rat Tail. The vault holds 1 item(s)."
            )
            assert aria.inventory == ["healing_herb", "rusty_sword"]
            assert vault_items(choir) == ["rat_tail"]
            assert sorted(aria.inventory + vault_items(choir)) == multiset_before
            assert srv._guilds_dirty is True
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "deposit", "item": "herb"}
            )
            assert texts(inbox)[0] == (
                "You deposit Healing Herb. The vault holds 2 item(s)."
            )
            assert aria.inventory == ["rusty_sword"]
            assert srv._guilds_dirty is True
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "withdraw", "item": "rat tail"}
            )
            assert texts(inbox)[0] == (
                "You withdraw Rat Tail. The vault holds 1 item(s)."
            )
            assert aria.inventory == ["rusty_sword", "rat_tail"]
            assert vault_items(choir) == ["healing_herb"]
            assert sorted(aria.inventory + vault_items(choir)) == multiset_before
            assert srv._guilds_dirty is True
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "withdraw", "item": "not there"}
            )
            assert texts(inbox)[-1] == "The guild vault holds no such item."
            assert choir["vault_items"] == ["healing_herb"]
            index_ok()
            print("ITEMS_OK")

            # --- a full pack banks the withdrawn item, never loses it -----
            aria.inventory = ["rat_tail"] * srv.INVENTORY_CAP
            choir["vault_items"] = ["rusty_sword"]
            srv.SCORES["aria"].pop("item_bank", None)
            assert srv._pack_full(aria) is True
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "withdraw", "item": "rusty sword"}
            )
            assert texts(inbox)[0] == (
                "You withdraw Rusty Sword. Your pack is full, so it is waiting "
                "in your item bank. The vault holds 0 item(s)."
            )
            assert vault_items(choir) == []
            assert aria.inventory == ["rat_tail"] * srv.INVENTORY_CAP
            assert srv.SCORES["aria"]["item_bank"] == ["rusty_sword"]
            assert srv._guilds_dirty is True
            index_ok()
            print("ITEM_FULL_PACK_OK")

            # --- INV-7: the 65th item is refused -------------------------
            choir["vault_items"] = ["rat_tail"] * srv.GUILD_VAULT_ITEM_CAP
            aria.inventory = ["healing_herb"]
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "deposit", "item": "healing herb"}
            )
            assert texts(inbox)[-1] == (
                f"The guild vault is full ({srv.GUILD_VAULT_ITEM_CAP} items)."
            )
            assert aria.inventory == ["healing_herb"]
            assert len(choir["vault_items"]) == srv.GUILD_VAULT_ITEM_CAP
            index_ok()
            print("ITEM_CAP_OK")

            # --- officer daily caps and their UTC date reset -------------
            today = srv._guild_utc_day()
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "vault", "op": "deposit", "gold": 500})
            assert texts(inbox)[0] == (
                "You deposit 500 gold. The vault holds 600 gold."
            )
            assert bram.gold == 0
            assert vault_gold(choir) == 600
            assert srv._guilds_dirty is True
            choir["withdrawn_today"] = {"day": today, "gold": 0, "items": 0}
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(
                bram, {"action": "vault", "op": "withdraw", "gold": 100}
            )
            assert texts(inbox)[0] == (
                "You withdraw 100 gold. The vault holds 500 gold."
            )
            assert bram.gold == 100
            assert vault_gold(choir) == 500
            assert choir["withdrawn_today"] == {"day": today, "gold": 100, "items": 0}
            assert srv._guilds_dirty is True
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "vault", "op": "withdraw", "gold": 1})
            assert texts(inbox)[-1] == (
                "Officers may withdraw at most 100 gold per day (asked 1). "
                "It resets at midnight UTC."
            )
            assert bram.gold == 100
            assert vault_gold(choir) == 500
            for _ in range(srv.GUILD_OFFICER_DAILY_ITEMS):
                inbox.clear()
                await srv.cmd_guild(
                    bram, {"action": "vault", "op": "withdraw", "item": "rat tail"}
                )
                assert texts(inbox)[0].startswith("You withdraw Rat Tail.")
            assert bram.inventory == ["rat_tail"] * srv.GUILD_OFFICER_DAILY_ITEMS
            assert choir["withdrawn_today"]["items"] == srv.GUILD_OFFICER_DAILY_ITEMS
            assert len(choir["vault_items"]) == (
                srv.GUILD_VAULT_ITEM_CAP - srv.GUILD_OFFICER_DAILY_ITEMS
            )
            inbox.clear()
            await srv.cmd_guild(
                bram, {"action": "vault", "op": "withdraw", "item": "rat tail"}
            )
            assert texts(inbox)[-1] == (
                "Officers may withdraw at most 3 items per day (asked 1). "
                "It resets at midnight UTC."
            )
            assert bram.inventory == ["rat_tail"] * srv.GUILD_OFFICER_DAILY_ITEMS
            assert len(choir["vault_items"]) == (
                srv.GUILD_VAULT_ITEM_CAP - srv.GUILD_OFFICER_DAILY_ITEMS
            )
            # A new UTC date key resets both caps without a timer.
            choir["withdrawn_today"]["day"] = "2000-01-01"
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "vault", "op": "withdraw", "gold": 5})
            assert texts(inbox)[0] == ("You withdraw 5 gold. The vault holds 495 gold.")
            assert choir["withdrawn_today"] == {"day": today, "gold": 5, "items": 0}
            assert srv._guilds_dirty is True
            # The leader is never capped.
            inbox.clear()
            await srv.cmd_guild(
                aria, {"action": "vault", "op": "withdraw", "gold": 200}
            )
            assert texts(inbox)[0] == (
                "You withdraw 200 gold. The vault holds 295 gold."
            )
            assert choir["withdrawn_today"]["gold"] == 5
            index_ok()
            print("OFFICER_CAP_OK")

            # --- member deposits, but cannot withdraw --------------------
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "vault", "op": "deposit", "gold": 10})
            assert texts(inbox)[0] == ("You deposit 10 gold. The vault holds 305 gold.")
            assert cid.gold == 90
            vault_before = vault_gold(choir)
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "vault", "op": "withdraw", "gold": 5})
            assert texts(inbox)[-1] == (
                "Only guild leaders and officers can withdraw from the vault. "
                "Ask your leader for a promotion."
            )
            assert vault_gold(choir) == vault_before
            assert cid.gold == 90
            inbox.clear()
            await srv.cmd_guild(
                cid, {"action": "vault", "op": "deposit", "item": "rat tail"}
            )
            assert texts(inbox)[0] == (
                "You deposit Rat Tail. The vault holds "
                f"{len(choir['vault_items'])} item(s)."
            )
            assert cid.inventory == []
            items_before = vault_items(choir)
            inbox.clear()
            await srv.cmd_guild(
                cid, {"action": "vault", "op": "withdraw", "item": "rat tail"}
            )
            assert texts(inbox)[-1] == (
                "Only guild leaders and officers can withdraw from the vault. "
                "Ask your leader for a promotion."
            )
            assert vault_items(choir) == items_before
            # The bare status view is read-only and never dirties the file.
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "vault"})
            status = texts(inbox)[-1]
            assert status.startswith(
                f"Iron Choir vault: {vault_gold(choir)} gold, "
                f"{len(choir['vault_items'])} item(s)."
            ), status
            assert "Only guild leaders and officers may withdraw." in status
            assert srv._guilds_dirty is False
            # A non-member is refused for every vault direction.
            for msg in (
                {"action": "vault", "op": "deposit", "gold": 10},
                {"action": "vault", "op": "withdraw", "gold": 10},
                {"action": "vault", "op": "deposit", "item": "rat tail"},
                {"action": "vault", "op": "withdraw", "item": "rat tail"},
            ):
                inbox.clear()
                await srv.cmd_guild(dune, msg)
                assert texts(inbox)[-1] == "You are not in a guild."
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "vault"})
            assert texts(inbox)[-1] == "You are not in a guild."
            assert cid.gold == 90
            index_ok()
            print("MEMBER_OK")

            # --- both vault directions are hall-gated --------------------
            aria.room = "market"
            before = (
                aria.gold,
                vault_gold(choir),
                list(aria.inventory),
                vault_items(choir),
            )
            for msg in (
                {"action": "vault", "op": "deposit", "gold": 10},
                {"action": "vault", "op": "withdraw", "gold": 10},
                {"action": "vault", "op": "deposit", "item": "healing herb"},
                {"action": "vault", "op": "withdraw", "item": "rat tail"},
            ):
                inbox.clear()
                await srv.cmd_guild(aria, msg)
                assert texts(inbox)[-1] == (
                    "Guild vaults are opened at the Adventurers Guild hall "
                    "(south of Artisan Row)."
                )
            after = (
                aria.gold,
                vault_gold(choir),
                list(aria.inventory),
                vault_items(choir),
            )
            assert after == before
            aria.room = "guild_hall"
            index_ok()
            print("HALL_GATE_OK")

            # --- guild info still reports the vault to members only ------
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "info"})
            payload = inbox[-1]
            assert payload["type"] == "guild", payload
            assert payload["vault_gold"] == vault_gold(choir)
            assert payload["vault_items"] == len(choir["vault_items"])
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "info", "name": "Iron Choir"})
            public = inbox[-1]
            assert public["type"] == "guild", public
            assert "vault_gold" not in public
            assert "vault_items" not in public
            index_ok()
            print("INFO_OK")

            # --- INV-8: interleaved withdrawals cannot double-spend ------
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "vault", "op": "deposit", "gold": 60})
            assert vault_gold(choir) == 365
            assert aria.gold == 1040

            async def race_withdraw():
                await srv.cmd_guild(
                    aria, {"action": "vault", "op": "withdraw", "gold": 365}
                )

            inbox.clear()
            await asyncio.gather(race_withdraw(), race_withdraw())
            race_texts = texts(inbox)
            assert (
                race_texts.count("You withdraw 365 gold. The vault holds 0 gold.") == 1
            ), race_texts
            assert (
                race_texts.count("The guild vault holds only 0 gold.") == 1
            ), race_texts
            assert vault_gold(choir) == 0
            assert aria.gold == 1405
            index_ok()
            print("RACE_OK")

            # --- telemetry and the index stay consistent -----------------
            index_ok()
            for entry in srv.SCORES.values():
                assert "guild" not in entry, entry
            telemetry = srv.guild_telemetry()
            assert telemetry["vault_gold_total"] == vault_gold(choir)
            assert (
                telemetry["vault_gold_in_total"] - telemetry["vault_gold_out_total"]
                == telemetry["vault_gold_total"]
            ), telemetry
            assert (
                aria.gold + bram.gold + cid.gold + dune.gold + vault_gold(choir) == 1700
            )
            # Every successful vault op above wrote exactly one entry and every
            # refusal wrote none, so the counter matches the record.
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN == len(choir["audit"]) > 0
            assert srv.GUILD_AUDIT_ENTRIES_WRITTEN <= srv.GUILD_AUDIT_CAP
            assert 0 < len(srv.guild_feed) <= 50
            print("INDEX_INVARIANT_OK")

            # --- a disband return lands in the ledger as an out ----------
            in_before = srv.GUILD_VAULT_GOLD_IN_TOTAL
            out_before = srv.GUILD_VAULT_GOLD_OUT_TOTAL
            total_before = srv.guild_telemetry()["vault_gold_total"]
            eira = mkplayer("Eira", 70405, gold=1000)
            players.append(eira)
            bell = make_guild("Silent Bell", [("eira", "leader", 10.0)])
            inbox.clear()
            await srv.cmd_guild(eira, {"action": "vault", "op": "deposit", "gold": 400})
            assert vault_gold(bell) == 400
            inbox.clear()
            await srv.cmd_guild(
                eira, {"action": "vault", "op": "withdraw", "gold": 150}
            )
            assert vault_gold(bell) == 250
            assert srv.GUILD_VAULT_GOLD_IN_TOTAL == in_before + 400
            out_before_disband = srv.GUILD_VAULT_GOLD_OUT_TOTAL
            returned = vault_gold(bell)
            assert returned > 0
            inbox.clear()
            await srv.cmd_guild(eira, {"action": "disband"})
            assert texts(inbox)[0] == (
                "Silent Bell is disbanded. Its vault (250 gold, 0 item(s)) "
                "is returned to you."
            )
            assert eira.gold == 1000
            assert bell["vault_gold"] == 0
            assert srv.GUILD_VAULT_GOLD_OUT_TOTAL == out_before_disband + returned
            assert srv.GUILD_VAULT_GOLD_OUT_TOTAL - out_before == 150 + returned
            telemetry = srv.guild_telemetry()
            assert (
                telemetry["vault_gold_in_total"] - telemetry["vault_gold_out_total"]
                == telemetry["vault_gold_total"]
            ), telemetry
            assert telemetry["vault_gold_total"] == total_before
            index_ok()
            print("DISBAND_LEDGER_OK")
        finally:
            for pl in players:
                unplayer(pl)
            for key in set(srv.SCORES) - keys0:
                srv.SCORES.pop(key, None)
            srv.GUILDS_FILE = saved_file
            srv.GUILDS = saved_guilds
            srv.GUILD_TOMBSTONES = saved_stones
            srv._rebuild_guild_index()
            srv.send = saved_send
            srv._guilds_dirty = saved_dirty
            srv.GUILD_VAULT_GOLD_IN_TOTAL = saved_in
            srv.GUILD_VAULT_GOLD_OUT_TOTAL = saved_out
            srv.GUILD_AUDIT_ENTRIES_WRITTEN = saved_audit
            srv.guild_feed.clear()
            srv.guild_feed.extend(saved_feed)


asyncio.run(main())
print("ALL_GUILD_VAULT_OK")
