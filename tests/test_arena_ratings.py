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

        # --- History is a bounded ring ------------------------------------
        srv.ARENA_HISTORY[:] = [{"match_id": n} for n in range(srv.ARENA_HISTORY_MAX)]
        ring = mk_match(1, ["RateRingA", "RateRingB"])
        ring.scores = {1: 2, 2: 0}
        await srv._arena_finish(ring, 1, "Test ring.")
        assert len(srv.ARENA_HISTORY) == srv.ARENA_HISTORY_MAX, len(srv.ARENA_HISTORY)
        assert srv.ARENA_HISTORY[-1]["match_id"] == ring.id
        print("HISTORY_CAP_OK")

        # --- Ladder is ordered, and both surfaces expose it ---------------
        ladder = srv.arena_ladder()
        ratings = [row["rating"] for row in ladder]
        assert ratings == sorted(ratings, reverse=True), ratings
        snap = srv.world_snapshot()["arena"]
        assert {"queues", "live", "ladder", "recent", "rating_clamps"} <= set(
            snap
        ), sorted(snap)
        assert snap["live"] == [] and snap["ladder"], snap
        looker = mkplayer("RateLooker", 63199)
        await srv.cmd_arena(looker, {"action": "ladder"})
        frames = [f for f in inbox["RateLooker"] if f.get("type") == "arena_ladder"]
        assert frames, inbox["RateLooker"]
        assert frames[-1]["ladder"] and frames[-1]["you"] is None, frames[-1]
        await srv.cmd_arena(looker, {"action": "status"})
        status = [f for f in inbox["RateLooker"] if f.get("type") == "arena"][-1]
        assert status["rating"] == srv.ARENA_RATING_DEFAULT, status
        print("LADDER_OK")

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
