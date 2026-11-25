"""Arena ratings (#653): a zero-sum Elo ladder that outlives a restart.

Run from the repo root:  python tests/test_arena_ratings.py

Rating is a durable record, so it cannot live in scores.json (which evicts
entries on a TTL). It keeps arena.json instead, and the two properties that
matter are that a match moves rating between the two sides rather than
minting it, and that the ladder reloads after a restart without a corrupt
file taking the server down with it.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


def write_text(path, text):
    """Blocking write kept out of the async body (load_arena is blocking too)."""
    with open(path, "w") as f:
        f.write(text)


async def main():
    inbox = {}

    async def fake_send(player, payload):
        row = payload if isinstance(payload, dict) else json.loads(payload)
        inbox.setdefault(player.name, []).append(row)

    saved_send = srv.send
    srv.send = fake_send

    saved_ratings = dict(srv.ARENA_RATINGS)
    saved_history = list(srv.ARENA_HISTORY)
    saved_clamps = srv.ARENA_RATING_CLAMPS
    saved_dirty = srv._arena_dirty
    spawned = []
    created = []

    def mkplayer(name, pid, room="town_square", hp=20):
        pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
        pl.hp, pl.max_hp, pl.room = hp, hp, room
        srv.players[pid] = pl
        srv.add_member(pl)
        spawned.append(pl)
        return pl

    def mk_match(team_size, names):
        """A match with a roster already in place, as if pairing had run."""
        match = srv.ArenaMatch(team_size)
        for i, name in enumerate(names):
            team = 1 if i < team_size else 2
            pid = 63000 + i
            match.players[pid] = {
                "name": name,
                "team": team,
                "entry_room": "town_square",
                "entry_hp": 20,
            }
            pl = mkplayer(name, pid)
            pl.arena_match = match.id
            pl.arena_team = team
        srv.ARENA_MATCHES[match.id] = match
        created.append(match.id)
        return match

    def rating_of(name):
        return srv._arena_rating(name)

    try:
        srv.ARENA_RATINGS.clear()
        srv.ARENA_HISTORY.clear()
        srv.ARENA_RATING_CLAMPS = 0
        srv._arena_dirty = False

        # --- A match moves rating between sides, never into existence -----
        duel = mk_match(1, ["RateAlpha", "RateBeta"])
        duel.scores = {1: 2, 2: 0}
        await srv._arena_finish(duel, 1, "Test ends.")
        a, b = rating_of("RateAlpha"), rating_of("RateBeta")
        assert round(a + b, 2) == 2 * srv.ARENA_RATING_DEFAULT, (a, b)
        assert a > srv.ARENA_RATING_DEFAULT > b, (a, b)
        assert round(a - srv.ARENA_RATING_DEFAULT, 2) == round(
            srv.ARENA_RATING_DEFAULT - b, 2
        ), (a, b)
        history = srv.ARENA_HISTORY[-1]
        assert history["match_id"] == duel.id and history["winner"] == 1
        assert round(sum(history["deltas"].values()), 2) == 0.0, history["deltas"]
        assert duel.id not in srv.ARENA_MATCHES
        print("RATING_CONSERVED_OK")

        # --- A draw splits the expectation instead of paying a winner -----
        draw = mk_match(1, ["RateDrawA", "RateDrawB"])
        draw.scores = {1: 1, 2: 1}
        await srv._arena_finish(draw, 0, "Test draws.")
        assert rating_of("RateDrawA") == srv.ARENA_RATING_DEFAULT
        assert rating_of("RateDrawB") == srv.ARENA_RATING_DEFAULT
        assert srv.ARENA_HISTORY[-1]["winner"] == 0
        print("RATING_DRAW_OK")

        # --- A draw between unequal sides moves rating but is not a win ---
        # The counters come from the result, not from the sign of the delta:
        # an underdog that draws gains rating and has still not won.
        srv.ARENA_RATINGS["rateunder"] = {
            "name": "RateUnder",
            "rating": 800.0,
            "peak": 800.0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "matches": 0,
        }
        srv.ARENA_RATINGS["ratefav"] = {
            "name": "RateFav",
            "rating": 1200.0,
            "peak": 1200.0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "matches": 0,
        }
        uneven = mk_match(1, ["RateUnder", "RateFav"])
        uneven.scores = {1: 1, 2: 1}
        await srv._arena_finish(uneven, 0, "Test uneven draw.")
        under = srv.ARENA_RATINGS["rateunder"]
        fav = srv.ARENA_RATINGS["ratefav"]
        assert under["rating"] > 800.0 and fav["rating"] < 1200.0, (under, fav)
        assert (under["wins"], under["losses"], under["draws"]) == (0, 0, 1), under
        assert (fav["wins"], fav["losses"], fav["draws"]) == (0, 0, 1), fav
        assert round(under["rating"] + fav["rating"], 2) == 2000.0, (under, fav)
        print("DRAW_COUNTERS_OK")

        # --- Both members of a side move together -------------------------
        teams = mk_match(2, ["RateTeamA", "RateTeamB", "RateTeamC", "RateTeamD"])
        teams.scores = {1: 2, 2: 1}
        await srv._arena_finish(teams, 1, "Test teams.")
        delta_a = round(rating_of("RateTeamA") - srv.ARENA_RATING_DEFAULT, 2)
        delta_b = round(rating_of("RateTeamB") - srv.ARENA_RATING_DEFAULT, 2)
        delta_c = round(rating_of("RateTeamC") - srv.ARENA_RATING_DEFAULT, 2)
        assert delta_a == delta_b > 0, (delta_a, delta_b)
        assert delta_c < 0 and round(delta_a * 2 + delta_c * 2, 2) == 0.0
        print("RATING_TEAMS_OK")

        # --- The floor is honoured and counted, not silently averaged -----
        srv.ARENA_RATINGS["floorbot"] = {
            "name": "FloorBot",
            "rating": srv.ARENA_RATING_FLOOR,
            "peak": srv.ARENA_RATING_FLOOR,
            "wins": 0,
            "losses": 0,
            "matches": 0,
        }
        srv.ARENA_RATINGS["ceilingbot"] = {
            "name": "LowBot",
            "rating": 120.0,
            "peak": 120.0,
            "wins": 0,
            "losses": 0,
            "matches": 0,
        }
        clamps_before = srv.ARENA_RATING_CLAMPS
        floor_match = mk_match(1, ["FloorBot", "LowBot"])
        floor_match.scores = {1: 0, 2: 2}
        await srv._arena_finish(floor_match, 2, "Test floor.")
        assert rating_of("FloorBot") == srv.ARENA_RATING_FLOOR, rating_of("FloorBot")
        assert srv.ARENA_RATING_CLAMPS == clamps_before + 1
        # The clamp is the one place zero-sum bends: the winner is credited
        # its full gain while the loser cannot pay past the floor.
        assert rating_of("LowBot") > 120.0, rating_of("LowBot")
        print("RATING_FLOOR_OK")

        # --- Round trip through the file, including the .1 generation -----
        tmpdir = tempfile.mkdtemp(prefix="arena-rating-")
        path = os.path.join(tmpdir, "arena.json")
        assert srv.save_arena(path)
        assert srv.save_arena(path)  # second save rotates the first to .1
        assert os.path.exists(path) and os.path.exists(path + ".1")
        loaded, loaded_history, loaded_clamps = srv.load_arena(path)
        assert loaded["ratealpha"] == srv.ARENA_RATINGS["ratealpha"], loaded
        assert len(loaded_history) == len(srv.ARENA_HISTORY)
        assert loaded_clamps == srv.ARENA_RATING_CLAMPS
        assert loaded["floorbot"]["rating"] == srv.ARENA_RATING_FLOOR
        print("STORE_ROUNDTRIP_OK")

        # A failed final replace must not leave the live path absent (#811).
        real_replace = os.replace

        def fail_live_replace(src, dst):
            if dst == path:
                raise OSError("simulated replace failure")
            return real_replace(src, dst)

        srv.os.replace = fail_live_replace
        try:
            assert srv.save_arena(path) is False
            assert os.path.exists(path), "live file vanished on failed replace"
            assert os.path.exists(path + ".1")
            assert not os.path.exists(path + ".tmp"), "temp file left behind"
        finally:
            srv.os.replace = real_replace
        print("ARENA_FAILED_REPLACE_KEEPS_LIVE_OK")

        # --- A corrupt file loads as an empty ladder, not a crash ---------
        write_text(path, "{not json")
        recovered, _, _ = srv.load_arena(path)
        assert recovered["ratealpha"] == srv.ARENA_RATINGS["ratealpha"], recovered
        write_text(path, "{not json")
        try:
            os.remove(path + ".1")
        except OSError:
            pass
        empty, empty_history, empty_clamps = srv.load_arena(path)
        assert empty == {} and empty_history == [] and empty_clamps == 0
        future = os.path.join(tmpdir, "future.json")
        write_text(
            future, json.dumps({"version": srv.ARENA_VERSION + 1, "ratings": {"x": {}}})
        )
        assert srv.load_arena(future) == ({}, [], 0)
        print("STORE_CORRUPT_OK")

        # --- Non-finite stored values cost their field, not the startup ---
        # json.load accepts NaN and Infinity, so a bad file reaches the
        # normalizer as floats; int(nan) raises and an inf rating would win
        # every comparison forever.
        poisoned = os.path.join(tmpdir, "nonfinite.json")
        write_text(
            poisoned,
            json.dumps(
                {
                    "version": srv.ARENA_VERSION,
                    "ratings": {
                        "broken": {
                            "name": "Broken",
                            "rating": "inf",
                            "peak": "inf",
                            "wins": "nan",
                            "losses": "inf",
                            "draws": -5,
                            "matches": 1e300,
                        },
                        "sparse": {"name": "Sparse", "rating": 1200},
                    },
                    "history": [],
                    "clamps": float("nan"),
                }
            ),
        )
        rows, _, clamps = srv.load_arena(poisoned)
        broken = rows["broken"]
        assert broken["rating"] == srv.ARENA_RATING_DEFAULT, broken
        assert broken["peak"] == srv.ARENA_RATING_DEFAULT, broken
        assert (broken["wins"], broken["losses"]) == (0, 0), broken
        assert broken["draws"] == 0 and broken["matches"] == srv.ARENA_COUNT_MAX, broken
        assert clamps == 0, clamps
        assert rows["sparse"]["wins"] == 0 and rows["sparse"]["draws"] == 0, rows[
            "sparse"
        ]
        print("STORE_NONFINITE_OK")

        # --- History is a bounded ring ------------------------------------
        srv.ARENA_HISTORY[:] = [{"match_id": n} for n in range(srv.ARENA_HISTORY_MAX)]
        ring = mk_match(1, ["RateRingA", "RateRingB"])
        ring.scores = {1: 2, 2: 0}
        await srv._arena_finish(ring, 1, "Test ring.")
        assert len(srv.ARENA_HISTORY) == srv.ARENA_HISTORY_MAX, len(srv.ARENA_HISTORY)
        assert srv.ARENA_HISTORY[-1]["match_id"] == ring.id
        print("HISTORY_CAP_OK")

        # --- Ladder is ordered, capped, and both surfaces expose it -------
        ladder = srv.arena_ladder()
        ratings = [row["rating"] for row in ladder]
        assert ratings == sorted(ratings, reverse=True), ratings
        snap = srv.world_snapshot()["arena"]
        assert {"queues", "live", "ladder", "recent", "rating_clamps"} <= set(
            snap
        ), sorted(snap)
        assert snap["live"] == [] and snap["ladder"], snap
        assert snap["ladder_size"] == len(ladder), snap
        looker = mkplayer("RateLooker", 63199)
        await srv.cmd_arena(looker, {"action": "ladder"})
        frames = [f for f in inbox["RateLooker"] if f.get("type") == "arena_ladder"]
        assert frames, inbox["RateLooker"]
        frame = frames[-1]
        assert frame["ladder"] and frame["you"] is None, frame
        assert len(frame["ladder"]) <= srv.ARENA_LADDER_SHOW, len(frame["ladder"])
        assert frame["ladder_size"] == len(ladder), frame
        # A rated caller gets a rank even when the cap cuts them out of the table.
        srv.ARENA_RATINGS["ratelooker"] = {
            "name": "RateLooker",
            "rating": srv.ARENA_RATING_FLOOR,
            "peak": srv.ARENA_RATING_DEFAULT,
            "wins": 0,
            "losses": 9,
            "matches": 9,
        }
        await srv.cmd_arena(looker, {"action": "ladder"})
        bottom = [f for f in inbox["RateLooker"] if f.get("type") == "arena_ladder"][-1]
        rank = bottom["you"]["rank"]
        assert 1 <= rank <= bottom["ranked_size"], (rank, bottom["ranked_size"])
        assert bottom["you"]["provisional"] is False, bottom["you"]
        assert bottom["you"]["matches"] == 9, bottom["you"]
        shown = bottom["ladder"]
        assert all(not row["provisional"] for row in shown[: rank - 1]), shown
        if rank <= len(shown):
            assert shown[rank - 1]["name"] == "RateLooker", shown[rank - 1]
        assert len(bottom["ladder"]) == min(
            bottom["ladder_size"], srv.ARENA_LADDER_SHOW
        )
        await srv.cmd_arena(looker, {"action": "status"})
        status = [f for f in inbox["RateLooker"] if f.get("type") == "arena"][-1]
        assert status["rating"] == srv.ARENA_RATING_FLOOR, status
        print("LADDER_OK")

        # --- A forfeit keeps its roster slot and still cancels out --------
        srv.ARENA_RATINGS.pop("ratelooker", None)
        for name in ("ForfeitWin", "ForfeitQuit"):
            srv.ARENA_RATINGS.pop(name.lower(), None)
        quit_duel = mk_match(1, ["ForfeitQuit", "ForfeitWin"])
        quitter = srv.players[63000]
        before_quit = rating_of("ForfeitQuit")
        before_win = rating_of("ForfeitWin")
        await srv._arena_forfeit(quit_duel, quitter, "leaves the arena")
        delta = round(rating_of("ForfeitWin") - before_win, 2)
        assert delta > 0, delta
        assert round(rating_of("ForfeitQuit") - before_quit, 2) == -delta
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 2 and row["match_id"] == quit_duel.id, row
        assert len(row["teams"]["team1"]) == 1 and len(row["teams"]["team2"]) == 1, row
        assert "ForfeitQuit" in row["teams"]["team1"], row
        assert round(sum(row["deltas"].values()), 2) == 0.0, row["deltas"]
        assert quitter.arena_match == 0 and quitter.arena_team == 0
        assert quit_duel.id not in srv.ARENA_MATCHES
        print("FORFEIT_DUEL_OK")

        # --- A disconnect in 2v2 is an ordinary loss for that side --------
        for i in range(4):
            srv.ARENA_RATINGS.pop(f"dcdrop{i}".lower(), None)
        team_match = mk_match(2, ["DcdropA", "DcdropB", "DcdropC", "DcdropD"])
        dropper = srv.players[63000]
        before_team = {name: rating_of(name) for name in ("DcdropC", "DcdropD")}
        await srv.arena_on_disconnect(dropper)
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 2 and row["match_id"] == team_match.id, row
        assert len(row["teams"]["team1"]) == 2 and len(row["teams"]["team2"]) == 2, row
        assert row["teams"]["team1"] == ["DcdropB", "DcdropA"], row["teams"]
        c_delta = round(rating_of("DcdropC") - before_team["DcdropC"], 2)
        d_delta = round(rating_of("DcdropD") - before_team["DcdropD"], 2)
        assert c_delta > 0 and c_delta == d_delta, (c_delta, d_delta)
        assert round(sum(row["deltas"].values()), 2) == 0.0, row["deltas"]
        assert dropper.arena_match == 0 and team_match.id not in srv.ARENA_MATCHES
        print("FORFEIT_DISCONNECT_OK")

        # --- A forfeit during a decided pause keeps the scoreboard winner -
        # The leaver is on the winning side: deriving the winner from the
        # leaver's team would invert a match that is already decided.
        for name in ("DecidedQuit", "DecidedStay"):
            srv.ARENA_RATINGS.pop(name.lower(), None)
        decided = mk_match(1, ["DecidedQuit", "DecidedStay"])
        decided.scores = {1: 2, 2: 0}
        decided.state = "pause"
        quitter = srv.players[63000]
        before_quit = rating_of("DecidedQuit")
        before_stay = rating_of("DecidedStay")
        await srv._arena_forfeit(decided, quitter, "leaves the arena")
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 1 and row["match_id"] == decided.id, row
        assert row["scores"] == {"team1": 2, "team2": 0}, row
        assert rating_of("DecidedQuit") > before_quit, row
        assert rating_of("DecidedStay") < before_stay, row
        assert any(
            "Team 1 wins 2-0." in frame.get("text", "")
            for frame in inbox["DecidedStay"]
        ), inbox["DecidedStay"]
        print("FORFEIT_DECIDED_OK")

        # --- A forfeit while scores are tied still awards the opponent ----
        for name in ("TiedQuit", "TiedStay"):
            srv.ARENA_RATINGS.pop(name.lower(), None)
        tied = mk_match(1, ["TiedQuit", "TiedStay"])
        tied.scores = {1: 1, 2: 1}
        tied.state = "pause"
        quitter = srv.players[63000]
        await srv._arena_forfeit(tied, quitter, "leaves the arena")
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 2 and row["match_id"] == tied.id, row
        assert rating_of("TiedStay") > srv.ARENA_RATING_DEFAULT, row
        print("FORFEIT_TIED_OK")

        # --- A disconnect during a decided pause keeps the scoreboard -----
        for name in ("DcWinStay", "DcWinQuit"):
            srv.ARENA_RATINGS.pop(name.lower(), None)
        dc_decided = mk_match(1, ["DcWinStay", "DcWinQuit"])
        dc_decided.scores = {1: 0, 2: 2}
        dc_decided.state = "pause"
        dropper = srv.players[63001]
        await srv.arena_on_disconnect(dropper)
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 2 and row["match_id"] == dc_decided.id, row
        assert rating_of("DcWinQuit") > srv.ARENA_RATING_DEFAULT, row
        assert rating_of("DcWinStay") < srv.ARENA_RATING_DEFAULT, row
        assert any(
            "Team 2 wins 0-2." in frame.get("text", "") for frame in inbox["DcWinStay"]
        ), inbox["DcWinStay"]
        print("DISCONNECT_DECIDED_OK")

        # --- A disconnect while tied still awards the opposing team -------
        for name in ("DcTiedQuit", "DcTiedStay"):
            srv.ARENA_RATINGS.pop(name.lower(), None)
        dc_tied = mk_match(1, ["DcTiedQuit", "DcTiedStay"])
        dc_tied.scores = {1: 1, 2: 1}
        dc_tied.state = "pause"
        dropper = srv.players[63000]
        await srv.arena_on_disconnect(dropper)
        row = srv.ARENA_HISTORY[-1]
        assert row["winner"] == 2 and row["match_id"] == dc_tied.id, row
        assert rating_of("DcTiedStay") > srv.ARENA_RATING_DEFAULT, row
        print("DISCONNECT_TIED_OK")

        # --- A first win cannot outrank an established record -------------
        srv.ARENA_RATINGS.clear()
        srv.ARENA_RATINGS["luckyshot"] = {
            "name": "LuckyShot",
            "rating": 1200.0,
            "peak": 1200.0,
            "wins": 1,
            "losses": 0,
            "matches": 1,
        }
        srv.ARENA_RATINGS["veteran"] = {
            "name": "Veteran",
            "rating": 1100.0,
            "peak": 1300.0,
            "wins": 40,
            "losses": 20,
            "matches": srv.ARENA_LADDER_MIN_MATCHES,
        }
        standings = srv.arena_ladder()
        assert [row["name"] for row in standings] == ["Veteran", "LuckyShot"], standings
        assert standings[0]["provisional"] is False, standings[0]
        assert standings[1]["provisional"] is True, standings[1]
        assert srv.arena_rank("veteran") == 1, srv.arena_rank("veteran")
        assert srv.arena_rank("luckyshot") == 2, srv.arena_rank("luckyshot")
        assert srv.arena_rank("nobody") is None
        standing_snap = srv.world_snapshot()["arena"]
        assert standing_snap["ladder_size"] == 2, standing_snap
        assert standing_snap["ranked_size"] == 1, standing_snap
        assert standing_snap["provisional_size"] == 1, standing_snap
        assert standing_snap["min_matches"] == srv.ARENA_LADDER_MIN_MATCHES

        newcomer = mkplayer("RateStanding", 63210)
        await srv.cmd_arena(newcomer, {"action": "status"})
        fresh = [f for f in inbox["RateStanding"] if f.get("type") == "arena"][-1]
        assert fresh["matches"] == 0 and fresh["rank"] is None, fresh
        assert fresh["provisional"] is False, fresh
        srv.ARENA_RATINGS["ratestanding"] = {
            "name": "RateStanding",
            "rating": 1250.0,
            "peak": 1250.0,
            "wins": 1,
            "losses": 0,
            "matches": 1,
        }
        await srv.cmd_arena(newcomer, {"action": "ladder"})
        frame = [f for f in inbox["RateStanding"] if f.get("type") == "arena_ladder"][
            -1
        ]
        assert frame["you"]["rank"] == 2, frame
        assert frame["you"]["provisional"] is True, frame
        assert frame["ranked_size"] == 1, frame
        assert frame["min_matches"] == srv.ARENA_LADDER_MIN_MATCHES, frame
        assert [row["name"] for row in frame["ladder"]] == [
            "Veteran",
            "RateStanding",
            "LuckyShot",
        ], frame["ladder"]
        srv.ARENA_RATINGS["ratestanding"]["matches"] = srv.ARENA_LADDER_MIN_MATCHES
        await srv.cmd_arena(newcomer, {"action": "ladder"})
        frame = [f for f in inbox["RateStanding"] if f.get("type") == "arena_ladder"][
            -1
        ]
        assert frame["you"]["provisional"] is False, frame
        assert srv.arena_rank("ratestanding") == 1, srv.arena_rank("ratestanding")
        assert [row["name"] for row in frame["ladder"]] == [
            "RateStanding",
            "Veteran",
            "LuckyShot",
        ], frame["ladder"]
        print("STANDING_OK")

        print("ARENA_RATINGS_OK")
    finally:
        srv.send = saved_send
        srv.ARENA_RATINGS.clear()
        srv.ARENA_RATINGS.update(saved_ratings)
        srv.ARENA_HISTORY[:] = saved_history
        srv.ARENA_RATING_CLAMPS = saved_clamps
        srv._arena_dirty = saved_dirty
        for pl in spawned:
            srv.remove_member(pl)
            srv.players.pop(pl.id, None)
            srv.SCORES.pop(pl.name.lower(), None)
            srv.name_owners.pop(pl.name.lower(), None)
        for match_id in created:
            srv.ARENA_MATCHES.pop(match_id, None)


asyncio.run(main())
