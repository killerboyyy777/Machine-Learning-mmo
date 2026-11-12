"""Guild membership lifecycle tests for issue #588 (PR 2, no live server).

Run from the repo root:  python tests/test_guild_membership.py
Covers guild create/info/leave/disband, the hall gate, name rules,
tombstones, deterministic succession, dissolve, and vault return.
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1)


def texts(inbox):
    return [m["text"] for m in inbox if isinstance(m, dict) and "text" in m]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = srv.GUILDS_FILE
        saved_guilds = srv.GUILDS
        saved_stones = srv.GUILD_TOMBSTONES
        saved_send = srv.send
        saved_dirty = srv._guilds_dirty
        keys0 = set(srv.SCORES)
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        def mkplayer(name, pid, room="guild_hall", gold=0, inventory=None):
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

        def record(name):
            return srv._guild_for_name(name)

        srv.GUILDS_FILE = os.path.join(tmp, "guilds.json")
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv._rebuild_guild_index()
        srv.send = fake_send
        players = []
        try:
            # --- command surface is wired like every other sub-action ------
            assert srv.HANDLERS["guild"] is srv.cmd_guild
            assert srv._command_ticks_buffs("guild", {}) is False
            assert srv._command_ticks_buffs("guild", {"action": "info"}) is False
            assert srv._command_ticks_buffs("guild", {"action": "leave"}) is True
            aria = mkplayer("Aria", 70101)
            players.append(aria)
            inbox.clear()
            await srv.cmd_help(aria, {})
            help_text = inbox[-1]["text"]
            assert (
                "guild [create|info|invite|join|leave|kick|promote|demote|"
                "disband|vault|audit]" in help_text
            ), help_text
            print("SURFACE_OK")

            # --- create writes founder + leader and the index --------------
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "create", "name": "Iron Choir"})
            gid = max(srv.GUILDS)
            choir = srv.GUILDS[gid]
            assert choir["name"] == "Iron Choir"
            assert choir["name_key"] == "iron choir"
            assert choir["founder"] == "Aria"
            assert choir["members"] == {
                "aria": {"rank": "leader", "joined_ts": choir["created_ts"]}
            }, choir["members"]
            assert srv.GUILD_BY_MEMBER["aria"] == gid
            assert texts(inbox)[-1] == "Guild Iron Choir founded. You are its leader."
            assert srv._guilds_dirty is True
            print("CREATE_OK")

            # --- a second create by the same character is refused ----------
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "create", "name": "Second Choir"})
            assert texts(inbox)[-1] == "You are already in Iron Choir. Leave it first."
            assert len(srv.GUILDS) == 1
            print("ONE_GUILD_PER_CHARACTER_OK")

            # --- creation is a hall act, everything else is not ------------
            roam = mkplayer("Roam", 70102, room="town_square")
            players.append(roam)
            inbox.clear()
            await srv.cmd_guild(roam, {"action": "create", "name": "Road Wardens"})
            assert "signed at the Adventurers Guild hall" in texts(inbox)[-1]
            assert record("Road Wardens") is None
            inbox.clear()
            await srv.cmd_guild(roam, {"action": "info", "name": "Iron Choir"})
            assert inbox[-1]["type"] == "guild", inbox[-1]
            print("HALL_GATE_OK")

            # --- name rules: taken, reserved, length, printable ------------
            dupe = mkplayer("Bram", 70103)
            players.append(dupe)
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "iron choir"})
            assert texts(inbox)[-1] == "The guild name 'iron choir' is taken."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "Adventurers Guild"})
            assert texts(inbox)[-1] == "The guild name 'Adventurers Guild' is reserved."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "guild"})
            assert texts(inbox)[-1] == "The guild name 'guild' is reserved."
            # A character's display name is part of the reserved namespace.
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "Bram"})
            assert texts(inbox)[-1] == "The guild name 'Bram' is reserved."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "ab"})
            assert texts(inbox)[-1] == "Guild names are 3-24 printable characters."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "x" * 25})
            assert texts(inbox)[-1] == "Guild names are 3-24 printable characters."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "bad\x07name"})
            assert texts(inbox)[-1] == "Guild names are 3-24 printable characters."
            assert len(srv.GUILDS) == 1
            print("NAME_RULES_OK")

            # --- info: public fields without a roster for non-members ------
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "info", "name": "Iron Choir"})
            public = inbox[-1]
            assert public["type"] == "guild"
            assert public["name"] == "Iron Choir"
            assert public["founder"] == "Aria"
            assert public["leader"] == "Aria"
            assert public["member_count"] == 1
            assert "members" not in public and "vault_gold" not in public, public
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "info"})
            own = inbox[-1]
            assert own["members"][0]["name"] == "Aria"
            assert own["members"][0]["rank"] == "leader"
            assert own["vault_gold"] == 0 and own["vault_items"] == 0
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "info", "name": "No Such Guild"})
            assert texts(inbox)[-1] == "No guild named 'No Such Guild'."
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "info"})
            assert texts(inbox)[-1].startswith("You are not in a guild.")
            print("INFO_OK")

            # --- succession is deterministic, officers first --------------
            bob = mkplayer("Bob", 70104, room="market")
            zed = mkplayer("Zed", 70105, room="market")
            amy = mkplayer("Amy", 70106, room="market")
            players.extend([bob, zed, amy])
            # Bob is the earliest joiner but a plain member: officers win.
            # Amy and Zed tie on joined_ts: the lower-case name breaks it.
            choir["members"]["bob"] = {"rank": "member", "joined_ts": 50.0}
            choir["members"]["zed"] = {"rank": "officer", "joined_ts": 100.0}
            choir["members"]["amy"] = {"rank": "officer", "joined_ts": 100.0}
            srv._rebuild_guild_index()
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "leave"})
            assert texts(inbox)[0] == "You leave Iron Choir."
            assert "Amy is now the leader of Iron Choir." in texts(inbox), texts(inbox)
            assert choir["members"]["amy"]["rank"] == "leader"
            assert choir["members"]["zed"]["rank"] == "officer"
            assert "aria" not in choir["members"]
            assert "aria" not in srv.GUILD_BY_MEMBER
            assert srv.GUILD_BY_MEMBER["amy"] == gid
            assert srv._guilds_dirty is True
            print("SUCCESSION_TIEBREAK_OK")

            # --- an officer beats an earlier-joined member ----------------
            inbox.clear()
            await srv.cmd_guild(amy, {"action": "leave"})
            assert choir["members"]["zed"]["rank"] == "leader", choir["members"]
            assert choir["members"]["bob"]["rank"] == "member"
            assert "amy" not in srv.GUILD_BY_MEMBER
            print("SUCCESSION_OFFICER_FIRST_OK")

            # --- officers are exhausted, the member takes over ------------
            inbox.clear()
            await srv.cmd_guild(zed, {"action": "leave"})
            assert choir["members"]["bob"]["rank"] == "leader", choir["members"]
            print("SUCCESSION_MEMBER_OK")

            # --- the last member leaving dissolves the guild --------------
            choir["vault_gold"] = 7
            choir["vault_items"] = ["rat_tail"]
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(bob, {"action": "leave"})
            assert gid not in srv.GUILDS
            assert "iron choir" not in srv.GUILD_BY_MEMBER
            assert texts(inbox)[0].startswith(
                "Iron Choir had no members left and is dissolved."
            )
            assert (
                "Its vault (7 gold, 1 item(s)) is returned to you." in texts(inbox)[0]
            )
            assert bob.gold == 7
            assert bob.inventory == ["rat_tail"]
            assert srv._guilds_dirty is True
            assert any(t["name_key"] == "iron choir" for t in srv.GUILD_TOMBSTONES)
            print("DISSOLVE_LAST_MEMBER_OK")

            # --- a tombstoned name cannot be rechartered yet --------------
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "Iron Choir"})
            assert texts(inbox)[-1] == (
                "The guild name 'Iron Choir' was disbanded recently "
                "and cannot be reused yet."
            )
            assert record("Iron Choir") is None
            assert srv.save_guilds() is True
            reloaded, stored_stones = srv.load_guilds()
            assert reloaded == {}
            assert any(t["name_key"] == "iron choir" for t in stored_stones)
            print("TOMBSTONE_BLOCKS_OK")

            # --- and the tombstone expires after the cooldown -------------
            for stone in srv.GUILD_TOMBSTONES:
                if stone["name_key"] == "iron choir":
                    stone["disbanded_ts"] = time.time() - 31 * 24 * 3600
            inbox.clear()
            await srv.cmd_guild(dupe, {"action": "create", "name": "Iron Choir"})
            assert texts(inbox)[-1].startswith("Guild Iron Choir founded.")
            recharter = srv.GUILD_BY_MEMBER["bram"]
            assert srv.GUILDS[recharter]["name"] == "Iron Choir"
            print("TOMBSTONE_EXPIRY_OK")

            # --- disband: non-member, non-leader, wrong room --------------
            night = mkplayer("Night", 70107)
            cid = mkplayer("Cid", 70108)
            players.extend([night, cid])
            inbox.clear()
            await srv.cmd_guild(night, {"action": "create", "name": "Night Watch"})
            night_guild = record("Night Watch")
            night_guild["members"]["cid"] = {"rank": "member", "joined_ts": 5.0}
            night_guild["vault_gold"] = 120
            night_guild["vault_items"] = ["wolf_pup", "rat_tail", "rat_tail"]
            srv._rebuild_guild_index()
            inbox.clear()
            roam.room = "guild_hall"
            await srv.cmd_guild(roam, {"action": "disband"})
            assert texts(inbox)[-1] == "You are not in a guild."
            roam.room = "town_square"
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "disband"})
            assert texts(inbox)[-1] == "Only the guild leader can disband a guild."
            night.room = "town_square"
            inbox.clear()
            await srv.cmd_guild(night, {"action": "disband"})
            assert "signed at the Adventurers Guild hall" in texts(inbox)[-1]
            assert record("Night Watch") is not None
            assert record("Night Watch")["vault_gold"] == 120
            print("DISBAND_GUARDS_OK")

            # --- disband returns the whole vault to the leader ------------
            night.room = "guild_hall"
            night.gold = 5
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(night, {"action": "disband"})
            assert record("Night Watch") is None
            assert "night watch" not in srv.GUILD_BY_MEMBER
            assert sorted(night.inventory) == ["rat_tail", "rat_tail", "wolf_pup"]
            assert night.gold == 125
            assert texts(inbox)[0] == (
                "Night Watch is disbanded. Its vault (120 gold, 3 item(s)) "
                "is returned to you."
            )
            assert srv._guilds_dirty is True
            assert any(t["name_key"] == "night watch" for t in srv.GUILD_TOMBSTONES)
            print("DISBAND_RETURNS_VAULT_OK")

            # --- a full pack banks the items instead of losing them ------
            ash = mkplayer("Ash", 70109, inventory=["rat_tail"] * 24)
            players.append(ash)
            inbox.clear()
            await srv.cmd_guild(ash, {"action": "create", "name": "Ash Company"})
            ash_guild = record("Ash Company")
            ash_guild["vault_gold"] = 40
            ash_guild["vault_items"] = ["iron_ore", "iron_ore"]
            assert srv._pack_full(ash)
            inbox.clear()
            await srv.cmd_guild(ash, {"action": "disband"})
            assert record("Ash Company") is None
            banked = srv.SCORES["ash"]["item_bank"]
            assert banked == ["iron_ore", "iron_ore"], banked
            assert ash.inventory == ["rat_tail"] * 24
            assert ash.gold == 40
            assert (
                sorted(ash.inventory + banked)
                == ["iron_ore", "iron_ore"] + ["rat_tail"] * 24
            )
            print("DISBAND_FULL_PACK_OK")

            # --- the guild table is capped -------------------------------
            live_guilds = srv.GUILDS
            srv.GUILDS = {
                i: srv.new_guild_record(i, f"Cap{i}", f"founder{i}")
                for i in range(srv.GUILD_MAX_GUILDS)
            }
            srv._rebuild_guild_index()
            capper = mkplayer("Capper", 70110)
            players.append(capper)
            inbox.clear()
            await srv.cmd_guild(capper, {"action": "create", "name": "One Too Many"})
            assert texts(inbox)[-1] == (
                f"The world already has {srv.GUILD_MAX_GUILDS} guilds. "
                "Try again once one disbands."
            )
            assert len(srv.GUILDS) == srv.GUILD_MAX_GUILDS
            srv.GUILDS = live_guilds
            srv._rebuild_guild_index()
            print("GUILD_CAP_OK")

            # --- the tombstone ring is capped, oldest evicted ------------
            now = time.time()
            srv.GUILD_TOMBSTONES[:] = [
                {"name": f"Old {i}", "name_key": f"old {i}", "disbanded_ts": now}
                for i in range(srv.GUILD_TOMBSTONE_CAP)
            ]
            ring = mkplayer("Ring", 70111)
            players.append(ring)
            inbox.clear()
            await srv.cmd_guild(ring, {"action": "create", "name": "Ring Bearers"})
            await srv.cmd_guild(ring, {"action": "disband"})
            assert len(srv.GUILD_TOMBSTONES) == srv.GUILD_TOMBSTONE_CAP
            assert not any(t["name_key"] == "old 0" for t in srv.GUILD_TOMBSTONES)
            assert srv.GUILD_TOMBSTONES[-1]["name_key"] == "ring bearers"
            print("TOMBSTONE_CAP_OK")

            # --- invariants: one entry per member, no guild on SCORES ----
            for gid2, rec2 in srv.GUILDS.items():
                for key in rec2["members"]:
                    assert srv.GUILD_BY_MEMBER.get(key) == gid2, (key, gid2)
            assert len(set(srv.GUILD_BY_MEMBER)) == len(srv.GUILD_BY_MEMBER)
            for entry in srv.SCORES.values():
                assert "guild" not in entry, entry
            print("INDEX_INVARIANT_OK")

            # --- unknown actions name the usable ones --------------------
            inbox.clear()
            await srv.cmd_guild(ring, {"action": "frobnicate"})
            assert texts(inbox)[-1] == (
                "Unknown guild action 'frobnicate'. Use create, info, invite, join, "
                "leave, kick, promote, demote, disband, vault, audit, "
                "permissions, transfer, or accept."
            )
            print("UNKNOWN_ACTION_OK")
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


asyncio.run(main())
print("ALL_GUILD_MEMBERSHIP_OK")
