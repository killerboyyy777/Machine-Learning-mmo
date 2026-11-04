"""Guild ranks tests for issue #588 (PR 3, no live server).

Run from the repo root:  python tests/test_guild_ranks.py
Covers invite/join/kick/promote/demote, the one rank gate behind all of them,
INV-3 (one guild per character), INV-5 (invite expiry plus the prune sweep),
and the pending-invite telemetry counter.
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


def index_ok():
    for gid, record in srv.GUILDS.items():
        for key in record["members"]:
            assert srv.GUILD_BY_MEMBER.get(key) == gid, (key, gid)
    for key, gid in srv.GUILD_BY_MEMBER.items():
        assert key in srv.GUILDS[gid]["members"], (key, gid)


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = srv.GUILDS_FILE
        saved_guilds = srv.GUILDS
        saved_stones = srv.GUILD_TOMBSTONES
        saved_send = srv.send
        saved_dirty = srv._guilds_dirty
        saved_invites = dict(srv._pending_guild_invites)
        saved_party = dict(srv._pending_party_invites)
        saved_prune = srv._last_invite_prune
        keys0 = set(srv.SCORES)
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)

        def mkplayer(name, pid):
            pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
            pl.room = "market"
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
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv._pending_guild_invites.clear()
        srv._pending_party_invites.clear()
        srv._rebuild_guild_index()
        srv.send = fake_send
        players = []
        try:
            # --- command surface and help list the new verbs --------------
            assert srv.HANDLERS["guild"] is srv.cmd_guild
            aria = mkplayer("Aria", 70201)
            players.append(aria)
            inbox.clear()
            await srv.cmd_help(aria, {})
            help_text = inbox[-1]["text"]
            assert (
                "guild [create|info|invite|join|leave|kick|promote|demote|disband|vault]"
                in help_text
            ), help_text
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "audit"})
            assert texts(inbox)[-1] == (
                "Unknown guild action 'audit'. Use create, info, invite, join, "
                "leave, kick, promote, demote, disband, or vault."
            )
            assert srv.guild_telemetry()["invites_pending"] == 0
            print("SURFACE_OK")

            # --- the rank matrix lives in one helper ----------------------
            choir = make_guild(
                "Iron Choir",
                [
                    ("aria", "leader", 10.0),
                    ("bram", "officer", 20.0),
                    ("eve", "officer", 25.0),
                    ("cid", "member", 30.0),
                    ("dot", "member", 40.0),
                ],
            )
            gid = choir["id"]
            assert srv._guild_allows(choir, "aria", "invite") is True
            assert srv._guild_allows(choir, "bram", "invite") is True
            assert srv._guild_allows(choir, "cid", "invite") is False
            assert srv._guild_allows(choir, "nobody", "invite") is False
            assert srv._guild_allows(choir, "aria", "promote") is True
            assert srv._guild_allows(choir, "bram", "promote") is False
            assert srv._guild_allows(choir, "aria", "demote") is True
            assert srv._guild_allows(choir, "bram", "disband") is False
            assert srv._guild_allows(choir, "aria", "disband") is True
            assert srv._guild_allows(choir, "aria", "kick:officer") is True
            assert srv._guild_allows(choir, "aria", "kick:member") is True
            assert srv._guild_allows(choir, "aria", "kick:leader") is False
            assert srv._guild_allows(choir, "bram", "kick:member") is True
            assert srv._guild_allows(choir, "bram", "kick:officer") is False
            assert srv._guild_allows(choir, "bram", "kick:leader") is False
            assert srv._guild_allows(choir, "cid", "kick:member") is False
            assert srv._guild_allows(None, "aria", "invite") is False
            index_ok()
            print("PERMISSION_HELPER_OK")

            # --- invite: leader and officer yes, member and outsider no ---
            bram = mkplayer("Bram", 70202)
            eve = mkplayer("Eve", 70203)
            cid = mkplayer("Cid", 70204)
            dot = mkplayer("Dot", 70205)
            dune = mkplayer("Dune", 70206)
            faye = mkplayer("Faye", 70207)
            una = mkplayer("Una", 70208)
            host = mkplayer("Host", 70209)
            guest = mkplayer("Guest", 70210)
            players.extend([bram, eve, cid, dot, dune, faye, una, host, guest])
            inbox.clear()
            await srv.cmd_guild(cid, {"action": "invite", "target": "Faye"})
            assert texts(inbox)[-1] == (
                "Only guild leaders and officers can invite. "
                "Ask your leader to send it."
            )
            assert "faye" not in srv._pending_guild_invites
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "invite", "target": "Faye"})
            assert texts(inbox)[-1] == "You are not in a guild."
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "invite", "target": "Nobody"})
            assert (
                texts(inbox)[-1]
                == "No character named 'Nobody' exists. Check the spelling."
            )
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "invite", "target": "Faye"})
            invite = srv._pending_guild_invites["faye"]
            assert invite["guild_id"] == gid
            assert invite["inviter"] == "Aria"
            assert abs(invite["ts"] - time.time()) < 5
            assert texts(inbox)[0] == "Invitation sent to Faye."
            assert texts(inbox)[1] == (
                "Aria invites you to join Iron Choir. Send guild join to accept."
            )
            assert srv.guild_telemetry()["invites_pending"] == 1
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "invite", "target": "Una"})
            assert texts(inbox)[0] == "Invitation sent to Una."
            assert srv._pending_guild_invites["una"]["inviter"] == "Bram"
            assert srv.guild_telemetry()["invites_pending"] == 2
            # A live invite is reported, never stacked or silently replaced.
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "invite", "target": "Faye"})
            assert texts(inbox)[-1] == (
                "Faye already has a pending guild invitation. "
                "Wait for it to expire or be accepted."
            )
            assert len(srv._pending_guild_invites) == 2
            index_ok()
            print("INVITE_PERMISSIONS_OK")

            # --- invite refuses a guilded target and a full guild ---------
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "invite", "target": "Cid"})
            assert texts(inbox)[-1] == (
                "Cid is already in Iron Choir. Ask them to leave it first."
            )
            crammed = make_guild(
                "Crammed Choir",
                [("host", "leader", 0.5)]
                + [(f"q{i}", "member", 1.0) for i in range(19)],
            )
            assert len(crammed["members"]) == srv.GUILD_MAX_MEMBERS
            inbox.clear()
            await srv.cmd_guild(host, {"action": "invite", "target": "Guest"})
            assert texts(inbox)[-1] == (
                f"Crammed Choir is full ({srv.GUILD_MAX_MEMBERS} members). "
                "Kick someone first."
            )
            assert "guest" not in srv._pending_guild_invites
            index_ok()
            print("INVITE_REFUSALS_OK")

            # --- join consumes the invite and lands at member -------------
            inbox.clear()
            await srv.cmd_guild(faye, {"action": "join", "name": "Wrong Choir"})
            assert texts(inbox)[-1] == (
                "Your pending invitation is for Iron Choir. "
                "Use guild join Iron Choir."
            )
            assert "faye" in srv._pending_guild_invites
            srv._guilds_dirty = False
            before = time.time()
            inbox.clear()
            await srv.cmd_guild(faye, {"action": "join", "name": "Iron Choir"})
            assert texts(inbox)[-1] == "You join Iron Choir as a member."
            row = choir["members"]["faye"]
            assert row["rank"] == "member"
            assert before <= row["joined_ts"] <= time.time()
            assert srv.GUILD_BY_MEMBER["faye"] == gid
            assert "faye" not in srv._pending_guild_invites
            assert srv._guilds_dirty is True
            assert srv.guild_telemetry()["invites_pending"] == 1
            index_ok()
            # Joining twice is refused, and joining with no invite is too.
            inbox.clear()
            await srv.cmd_guild(faye, {"action": "join"})
            assert texts(inbox)[-1] == (
                "You are already in Iron Choir. Leave it first."
            )
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "join"})
            assert texts(inbox)[-1] == (
                "No pending guild invitation. Ask a member to invite you."
            )
            assert "dune" not in srv.GUILD_BY_MEMBER
            index_ok()
            print("JOIN_OK")

            # --- a full guild refuses the join, invite kept for later -----
            srv._pending_guild_invites["guest"] = {
                "guild_id": crammed["id"],
                "ts": time.time(),
                "inviter": "Host",
            }
            inbox.clear()
            await srv.cmd_guild(guest, {"action": "join", "name": "Crammed Choir"})
            assert texts(inbox)[-1] == (
                f"Crammed Choir is full ({srv.GUILD_MAX_MEMBERS} members). "
                "Ask a leader to free a seat."
            )
            assert "guest" not in crammed["members"]
            assert "guest" not in srv.GUILD_BY_MEMBER
            assert "guest" in srv._pending_guild_invites
            srv._pending_guild_invites.pop("guest", None)
            index_ok()
            print("JOIN_FULL_OK")

            # --- INV-5: a stale invite is refused on join and removed -----
            srv._pending_guild_invites["una"] = {
                "guild_id": gid,
                "ts": time.time() - srv.GUILD_INVITE_TTL_SECONDS - 1,
                "inviter": "Bram",
            }
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(una, {"action": "join"})
            assert texts(inbox)[-1] == (
                "That guild invitation expired. Ask for a fresh one."
            )
            assert "una" not in srv._pending_guild_invites
            assert "una" not in srv.GUILD_BY_MEMBER
            assert srv.guild_telemetry()["invites_pending"] == 0
            # And the tick's sweeper drops what a join never touched.
            srv._pending_guild_invites["una"] = {
                "guild_id": gid,
                "ts": time.time() - srv.GUILD_INVITE_TTL_SECONDS - 1,
                "inviter": "Bram",
            }
            srv._pending_guild_invites["guest"] = {
                "guild_id": gid,
                "ts": time.time(),
                "inviter": "Aria",
            }
            srv._last_invite_prune = 0.0
            pruned = srv.prune_invites(now=time.time())
            assert pruned >= 1, pruned
            assert "una" not in srv._pending_guild_invites
            assert "guest" in srv._pending_guild_invites
            srv._pending_guild_invites.pop("guest", None)
            assert srv.guild_telemetry()["invites_pending"] == 0
            index_ok()
            print("INVITE_TTL_OK")

            # --- kick: rank decides who may remove whom -------------------
            inbox.clear()
            await srv.cmd_guild(dot, {"action": "kick", "target": "Cid"})
            assert texts(inbox)[-1] == (
                "Only guild leaders and officers can remove members."
            )
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "kick", "target": "Eve"})
            assert texts(inbox)[-1] == ("Only the guild leader can remove an officer.")
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "kick", "target": "Aria"})
            assert texts(inbox)[-1] == (
                "Nobody can kick the guild leader. Ask them to disband instead."
            )
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(bram, {"action": "kick", "target": "Cid"})
            assert texts(inbox)[0] == "You remove Cid from Iron Choir."
            assert texts(inbox)[1] == "Bram removes you from Iron Choir."
            assert "cid" not in choir["members"]
            assert "cid" not in srv.GUILD_BY_MEMBER
            assert srv._guilds_dirty is True
            index_ok()
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "kick", "target": "Aria"})
            assert texts(inbox)[-1] == ("You cannot kick yourself. Use guild leave.")
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "kick", "target": "Dot"})
            assert texts(inbox)[0] == "You remove Dot from Iron Choir."
            assert "dot" not in srv.GUILD_BY_MEMBER
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "kick", "target": "Bram"})
            assert texts(inbox)[0] == "You remove Bram from Iron Choir."
            assert "bram" not in srv.GUILD_BY_MEMBER
            inbox.clear()
            await srv.cmd_guild(aria, {"action": "kick", "target": "Ghost"})
            assert texts(inbox)[-1] == (
                "No one in Iron Choir is named 'Ghost'. "
                "Check the roster with guild info."
            )
            inbox.clear()
            await srv.cmd_guild(dune, {"action": "kick", "target": "Aria"})
            assert texts(inbox)[-1] == "You are not in a guild."
            index_ok()
            print("KICK_OK")

            # --- promote/demote: leader only, one rung at a time ----------
            nlead = mkplayer("Nlead", 70211)
            noff = mkplayer("Noff", 70212)
            nmem = mkplayer("Nmem", 70213)
            nmem2 = mkplayer("Nmem2", 70214)
            players.extend([nlead, noff, nmem, nmem2])
            watch = make_guild(
                "Night Watch",
                [
                    ("nlead", "leader", 10.0),
                    ("noff", "officer", 20.0),
                    ("nmem", "member", 30.0),
                    ("nmem2", "member", 40.0),
                ],
            )
            inbox.clear()
            await srv.cmd_guild(nmem, {"action": "promote", "target": "Nmem2"})
            assert texts(inbox)[-1] == ("Only the guild leader can promote a member.")
            inbox.clear()
            await srv.cmd_guild(noff, {"action": "promote", "target": "Nmem"})
            assert texts(inbox)[-1] == ("Only the guild leader can promote a member.")
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "promote", "target": "Noff"})
            assert texts(inbox)[-1] == ("Noff is already an officer of Night Watch.")
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "promote", "target": "Nlead"})
            assert texts(inbox)[-1] == (
                "The leader cannot be promoted. Use guild leave to hand over."
            )
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "promote", "target": "Nmem"})
            assert texts(inbox)[-1] == ("Nmem is now an officer of Night Watch.")
            assert watch["members"]["nmem"]["rank"] == "officer"
            assert srv._guilds_dirty is True
            index_ok()
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "demote", "target": "Nmem2"})
            assert texts(inbox)[-1] == ("Nmem2 is already a member of Night Watch.")
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "demote", "target": "Nlead"})
            assert texts(inbox)[-1] == (
                "The leader cannot be demoted. Use guild leave to hand over."
            )
            inbox.clear()
            await srv.cmd_guild(noff, {"action": "demote", "target": "Nmem"})
            assert texts(inbox)[-1] == ("Only the guild leader can demote an officer.")
            srv._guilds_dirty = False
            inbox.clear()
            await srv.cmd_guild(nlead, {"action": "demote", "target": "Nmem"})
            assert texts(inbox)[-1] == ("Nmem is back to member of Night Watch.")
            assert watch["members"]["nmem"]["rank"] == "member"
            assert srv._guilds_dirty is True
            index_ok()
            print("PROMOTE_DEMOTE_OK")

            # --- INV-3: the index and the records never drift -------------
            index_ok()
            names = list(srv.GUILD_BY_MEMBER)
            assert len(names) == len(set(names))
            for entry in srv.SCORES.values():
                assert "guild" not in entry, entry
            print("INDEX_INVARIANT_OK")
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
            srv._pending_guild_invites.clear()
            srv._pending_guild_invites.update(saved_invites)
            srv._pending_party_invites.clear()
            srv._pending_party_invites.update(saved_party)
            srv._last_invite_prune = saved_prune


asyncio.run(main())
print("ALL_GUILD_RANKS_OK")
