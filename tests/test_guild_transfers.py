"""Guild permissions and inter-guild transfers (#600).

Run from the repo root:  python tests/test_guild_transfers.py

The permission table is the risky half: a guild may re-rank a verb, but the
defaults must reproduce exactly the matrix #588 shipped, and a record written
before this existed must behave as it always did. Transfers are the gold half:
nothing is escrowed, a proposal is a claim on the sender's vault, and an accept
moves the gold once or not at all.
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


def vault_gold(record):
    return record["vault_gold"]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        saved = (
            srv.GUILDS_FILE,
            srv.GUILDS,
            srv.GUILD_TOMBSTONES,
            srv.send,
            srv.GUILD_TRANSFERS,
            srv.GUILD_TRANSFERS_COMPLETED,
            srv.GUILD_TRANSFERS_EXPIRED,
            srv.GUILD_GOLD_TRANSFERRED,
        )
        inbox = []

        async def fake_send(player, payload):
            inbox.append(payload)
            await asyncio.sleep(0)

        def mkplayer(name, pid, gold=0, room="guild_hall"):
            pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
            pl.room = room
            pl.gold = gold
            pl.inventory = []
            srv.players[pid] = pl
            srv.add_member(pl)
            srv.get_score_entry(name)
            return pl

        def make_guild(name, rows):
            gid = max(srv.GUILDS) + 1 if srv.GUILDS else 1
            record = srv.new_guild_record(gid, name, rows[0][0])
            record["members"] = {
                key: {"rank": rank, "joined_ts": ts} for key, rank, ts in rows
            }
            srv.GUILDS[gid] = record
            srv._rebuild_guild_index()
            return record

        async def say(pl, **msg):
            inbox.clear()
            await srv.cmd_guild(pl, msg)
            return texts(inbox)

        srv.GUILDS_FILE = os.path.join(tmp, "guilds.json")
        srv.GUILDS = {}
        srv.GUILD_TOMBSTONES = []
        srv.GUILD_TRANSFERS = {}
        srv.GUILD_TRANSFERS_COMPLETED = 0
        srv.GUILD_TRANSFERS_EXPIRED = 0
        srv.GUILD_GOLD_TRANSFERRED = 0
        srv._rebuild_guild_index()
        srv.send = fake_send
        players = []
        try:
            leader = mkplayer("Aria", 70601, gold=100)
            officer = mkplayer("Bram", 70602, gold=100)
            member = mkplayer("Cid", 70603, gold=100)
            other_leader = mkplayer("Dune", 70604, gold=100)
            other_member = mkplayer("Ede", 70605, gold=100)
            players = [leader, officer, member, other_leader, other_member]

            mine = make_guild(
                "Iron Pact",
                [("aria", "leader", 1), ("bram", "officer", 2), ("cid", "member", 3)],
            )
            theirs = make_guild(
                "Salt Company", [("dune", "leader", 4), ("ede", "member", 5)]
            )
            mine["vault_gold"] = 200
            theirs["vault_gold"] = 10

            # --- defaults reproduce the #588 matrix exactly ---
            assert srv._guild_permissions(mine) == {}
            assert srv._guild_allows(mine, "Cid", "deposit") is True
            assert srv._guild_allows(mine, "Cid", "withdraw") is False
            assert srv._guild_allows(mine, "Bram", "withdraw") is True
            assert srv._guild_allows(mine, "Bram", "kick:member") is True
            assert srv._guild_allows(mine, "Cid", "transfer") is False
            assert srv._guild_allows(mine, "Aria", "transfer") is True
            print("DEFAULTS_OK")

            # --- an old record without the field normalises to an empty table ---
            legacy = srv._normalize_guild_record(
                99,
                {
                    "name": "Legacy",
                    "members": {"zed": {"rank": "leader", "joined_ts": 1}},
                    "vault_gold": 5,
                    "vault_items": [],
                    "audit": [],
                    "withdrawn_today": {"day": "", "gold": 0, "items": 0},
                },
            )
            assert legacy["permissions"] == {}, legacy["permissions"]
            junk = srv._normalize_guild_record(
                98,
                {
                    "name": "Junk",
                    "members": {"zed": {"rank": "leader", "joined_ts": 1}},
                    "vault_gold": 5,
                    "permissions": {
                        "withdraw": "member",
                        "nope": "leader",
                        "kick": "king",
                    },
                },
            )
            assert junk["permissions"] == {"withdraw": "member"}, junk["permissions"]
            print("NORMALIZE_OK")

            # --- only the leader may edit the table ---
            out = await say(
                member, action="permissions", verb="withdraw", rank="member"
            )
            assert any("Only the guild leader" in t for t in out), out
            assert srv._guild_permissions(mine) == {}
            print("LEADER_ONLY_OK")

            out = await say(leader, action="permissions", verb="nope", rank="member")
            assert any("Unknown permission" in t for t in out), out
            out = await say(leader, action="permissions", verb="withdraw", rank="king")
            assert any("Unknown rank" in t for t in out), out
            assert srv._guild_permissions(mine) == {}
            print("VALIDATED_OK")

            # --- setting a flag changes the gate, and the default clears it ---
            out = await say(
                leader, action="permissions", verb="withdraw", rank="member"
            )
            assert any("withdraw now needs member" in t for t in out), out
            assert srv._guild_allows(mine, "Cid", "withdraw") is True
            assert srv.GUILDS[mine["id"]]["permissions"] == {"withdraw": "member"}
            out = await say(
                leader, action="permissions", verb="withdraw", rank="officer"
            )
            assert srv._guild_permissions(mine) == {}, srv._guild_permissions(mine)
            assert srv._guild_allows(mine, "Cid", "withdraw") is False
            out = await say(leader, action="permissions")
            assert any("Iron Pact permissions:" in t for t in out), out
            print("PERMISSION_TOGGLE_OK")

            # --- a member cannot send, a leader can, nothing moves yet ---
            total_before = vault_gold(mine) + vault_gold(theirs)
            out = await say(member, action="transfer", guild="Salt Company", amount=50)
            assert any("needs leader" in t for t in out), out
            assert srv.GUILD_TRANSFERS == {}
            out = await say(leader, action="transfer", guild="Iron Pact", amount=50)
            assert any("cannot send gold to itself" in t for t in out), out
            out = await say(leader, action="transfer", guild="Nowhere", amount=50)
            assert any("No guild by that name" in t for t in out), out
            out = await say(leader, action="transfer", guild="Salt Company", amount=500)
            assert any("vault holds 200" in t for t in out), out
            assert srv.GUILD_TRANSFERS == {}
            print("PROPOSAL_REFUSED_OK")

            out = await say(leader, action="transfer", guild="Salt Company", amount=50)
            assert any("#1" in t and "50 gold" in t for t in out), out
            assert len(srv.GUILD_TRANSFERS) == 1
            assert vault_gold(mine) == 200 and vault_gold(theirs) == 10
            assert vault_gold(mine) + vault_gold(theirs) == total_before
            print("PROPOSAL_MOVES_NOTHING_OK")

            # --- only the receiving guild, at the needed rank, completes it ---
            out = await say(leader, action="accept", id=1)
            assert any("not for your guild" in t for t in out), out
            out = await say(other_member, action="accept", id=1)
            assert any("needs leader" in t for t in out), out
            assert vault_gold(mine) == 200 and vault_gold(theirs) == 10
            out = await say(other_leader, action="accept", id="1")
            assert any("sends 50 gold" in t for t in out), out
            assert vault_gold(mine) == 150 and vault_gold(theirs) == 60
            assert vault_gold(mine) + vault_gold(theirs) == total_before
            assert srv.GUILD_TRANSFERS == {}
            assert srv.GUILD_TRANSFERS_COMPLETED == 1
            assert srv.GUILD_GOLD_TRANSFERRED == 50
            actions = [row["action"] for row in mine["audit"]]
            assert (
                "transfer_proposed" in actions and "transfer_sent" in actions
            ), actions
            assert "transfer_received" in [row["action"] for row in theirs["audit"]]
            print("TRANSFER_COMPLETES_OK")

            # --- a claim the vault no longer backs moves nothing ---
            out = await say(leader, action="transfer", guild="Salt Company", amount=100)
            assert any("#2" in t for t in out), out
            mine["vault_gold"] = 5
            out = await say(other_leader, action="accept", id=2)
            assert any("no longer holds" in t for t in out), out
            assert vault_gold(mine) == 5 and vault_gold(theirs) == 60
            assert srv.GUILD_TRANSFERS == {}
            print("STALE_CLAIM_OK")

            # --- expiry drops a proposal without moving gold ---
            mine["vault_gold"] = 200
            out = await say(leader, action="transfer", guild="Salt Company", amount=30)
            assert any("#3" in t for t in out), out
            srv.GUILD_TRANSFERS[3]["ts"] = 0.0
            srv._guild_transfer_prune(now=srv.GUILD_TRANSFER_TTL_SECONDS + 1)
            assert srv.GUILD_TRANSFERS == {}
            assert srv.GUILD_TRANSFERS_EXPIRED == 1
            out = await say(other_leader, action="accept", id=3)
            assert any("expire after an hour" in t for t in out), out
            assert vault_gold(mine) == 200 and vault_gold(theirs) == 60
            print("EXPIRY_OK")

            # --- telemetry reports the counters, and the total never drifts ---
            tele = srv.guild_telemetry()
            assert tele["transfers_pending"] == 0, tele
            assert tele["transfers_completed"] == 1, tele
            assert tele["transfers_expired"] == 1, tele
            assert tele["gold_transferred"] == 50, tele
            assert tele["permissions_customised"] == 0, tele
            assert tele["vault_gold_total"] == vault_gold(mine) + vault_gold(theirs)
            print("TELEMETRY_OK")

            # --- the receiving guild can see an offer, and it pays once ------
            out = await say(leader, action="transfer", guild="Salt Company", amount=25)
            offer = [t for t in out if "guild accept" in t][-1]
            tid = int(offer.split("#")[1].split(":")[0])
            assert any(
                row["action"] == "transfer_incoming" for row in theirs["audit"]
            ), theirs["audit"]
            print("TARGET_SEES_OFFER_OK")

            out = await say(other_leader, action="accept", id=tid)
            assert any("sends 25 gold" in t for t in out), out
            assert vault_gold(mine) == 175 and vault_gold(theirs) == 85
            out = await say(other_leader, action="accept", id=tid)
            assert any("No transfer" in t for t in out), out
            assert vault_gold(mine) == 175 and vault_gold(theirs) == 85
            print("NO_DOUBLE_ACCEPT_OK")

            # --- an id that now names a different guild pays nobody ----------
            out = await say(leader, action="transfer", guild="Salt Company", amount=25)
            tid = int(
                [t for t in out if "guild accept" in t][-1].split("#")[1].split(":")[0]
            )
            theirs["name_key"] = "some other company"
            out = await say(other_leader, action="accept", id=tid)
            assert any("not for your guild" in t for t in out), out
            assert vault_gold(mine) == 175 and vault_gold(theirs) == 85
            theirs["name_key"] = "salt company"
            srv.GUILD_TRANSFERS.pop(tid, None)
            print("ID_REUSE_REFUSED_OK")

            # --- a guild mid-disband cannot receive gold ---------------------
            out = await say(leader, action="transfer", guild="Salt Company", amount=10)
            tid = int(
                [t for t in out if "guild accept" in t][-1].split("#")[1].split(":")[0]
            )
            srv.GUILD_DISSOLVING.add(theirs["id"])
            out = await say(other_leader, action="accept", id=tid)
            assert any("disbanding" in t for t in out), out
            assert vault_gold(mine) == 175 and vault_gold(theirs) == 85
            srv.GUILD_DISSOLVING.discard(theirs["id"])
            assert srv.GUILD_TRANSFERS == {}
            print("DISSOLVING_REFUSED_OK")

            # --- disband is not a rank a guild may hand out ------------------
            out = await say(leader, action="permissions", verb="disband", rank="member")
            assert any("stays with the leader" in t for t in out), out
            assert "disband" not in srv._guild_permissions(mine)
            print("DISBAND_NOT_CONFIGURABLE_OK")

            tele = srv.guild_telemetry()
            assert tele["transfers_completed"] == 2, tele
            assert tele["gold_transferred"] == 75, tele
            assert tele["transfers_pending"] == 0, tele
            print("SECOND_TRANSFER_COUNTED_OK")

            # --- one guild cannot fill another's queue ----------------------
            assert srv.GUILD_TRANSFERS == {}
            for _ in range(3):
                out = await say(
                    leader, action="transfer", guild="Salt Company", amount=5
                )
                assert any("Proposal #" in t for t in out), out
            assert len(srv.GUILD_TRANSFERS) == 3
            out = await say(leader, action="transfer", guild="Salt Company", amount=5)
            assert any("Wait for an answer" in t for t in out), out
            assert len(srv.GUILD_TRANSFERS) == 3
            print("PAIR_CAP_OK")

            # --- the sender must still be the guild that promised ------------
            tid = min(srv.GUILD_TRANSFERS)
            mine["name_key"] = "renamed pact"
            out = await say(other_leader, action="accept", id=tid)
            assert any("no longer exists" in t for t in out), out
            mine["name_key"] = "iron pact"
            assert tid not in srv.GUILD_TRANSFERS
            srv.GUILD_TRANSFERS.clear()
            print("SOURCE_KEY_REFUSED_OK")

            # --- a dissolving guild's vault is closed ------------------------
            srv.GUILD_DISSOLVING.add(mine["id"])
            out = await say(leader, action="vault", op="deposit", gold=5)
            assert any("vault is closed" in t for t in out), out
            srv.GUILD_DISSOLVING.discard(mine["id"])
            print("DISSOLVING_VAULT_CLOSED_OK")

            # --- a member-rank withdraw still meets the daily cap ------------
            capman = mkplayer("Fay", 70606)
            players.append(capman)
            capguild = make_guild("Cap Company", [("fay", "member", 9)])
            capguild["permissions"] = {"withdraw": "member"}
            capguild["vault_items"] = ["rat_tail"] * 6
            taken = 0
            for _ in range(4):
                out = await say(capman, action="vault", op="withdraw", item="rat_tail")
                if any("may withdraw at most" in t for t in out):
                    break
                taken += 1
            assert taken == srv.GUILD_OFFICER_DAILY_ITEMS, taken
            assert len(capguild["vault_items"]) == 3, capguild["vault_items"]
            print("MEMBER_WITHDRAW_CAPPED_OK")

            print("GUILD_TRANSFERS_OK")
        finally:
            (
                srv.GUILDS_FILE,
                srv.GUILDS,
                srv.GUILD_TOMBSTONES,
                srv.send,
                srv.GUILD_TRANSFERS,
                srv.GUILD_TRANSFERS_COMPLETED,
                srv.GUILD_TRANSFERS_EXPIRED,
                srv.GUILD_GOLD_TRANSFERRED,
            ) = saved
            for pl in players:
                srv.remove_member(pl)
                srv.players.pop(pl.id, None)
            srv._rebuild_guild_index()


asyncio.run(main())
