"""Arena spectators (#654): a read-only audience around a live match.

Run from the repo root:  python tests/test_arena_spectator.py

The property that matters is negative: a watcher sees the fight and changes
nothing. So the proof is threefold -- the payload is an allowlist, no world
verb runs while watching, and a watched match produces exactly the same
history row as an unwatched one.
"""

import asyncio
import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


VIEW_KEYS = {
    "type",
    "match_id",
    "team_size",
    "state",
    "round",
    "rounds_to_win",
    "scores",
    "teams",
    "events",
}


async def main():
    inbox = {}

    async def fake_send(player, payload):
        inbox.setdefault(player.name, []).append(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    saved_send = srv.send
    srv.send = fake_send

    def mkplayer(name, pid, room="town_square", hp=20, max_hp=20, attack=6):
        pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
        pl.hp, pl.max_hp, pl.room = hp, max_hp, room
        pl.base_attack = attack
        srv.players[pid] = pl
        srv.add_member(pl)
        spawned.append(pl)
        return pl

    def frames(name, kind):
        return [f for f in inbox.get(name, []) if f.get("type") == kind]

    def last(name, kind):
        got = frames(name, kind)
        return got[-1] if got else None

    spawned = []
    saved_matches = dict(srv.ARENA_MATCHES)
    saved_queues = {size: list(rows) for size, rows in srv.ARENA_QUEUES.items()}
    saved_ratings = dict(srv.ARENA_RATINGS)
    history_len = len(srv.ARENA_HISTORY)
    death_log_len = len(srv.DEATH_LOG)

    try:
        duel_a = mkplayer("SpecOne", 64001, attack=40)
        duel_b = mkplayer("SpecTwo", 64002, attack=40)
        eye = mkplayer("SpecEye", 64003)
        await srv._arena_start(1, [duel_a.id, duel_b.id])
        match = srv.match_for_player(duel_a)
        assert match is not None

        # --- attach -------------------------------------------------
        await srv.cmd_arena(eye, {"action": "watch", "match": str(match.id)})
        assert eye.arena_spectating and eye.arena_watch == match.id
        view = last("SpecEye", "arena_view")
        assert view is not None and view["match_id"] == match.id
        assert match.spectators == {eye.id}
        # The bare id and the room-style id are the same reference.
        await srv.cmd_arena(eye, {"action": "watch", "match": f"a_{match.id}"})
        assert eye.arena_watch == match.id
        print("SPECTATOR_ATTACH_OK")

        # --- the payload is an allowlist ----------------------------
        assert set(view) == VIEW_KEYS, sorted(view)
        assert "id" not in view and "room" not in view and "gold" not in view
        for row in view["teams"]["team1"] + view["teams"]["team2"]:
            assert set(row) == {"name", "hp", "rating"}, row
        assert {r["name"] for r in view["teams"]["team1"]} == {"SpecOne"}
        assert {r["name"] for r in view["teams"]["team2"]} == {"SpecTwo"}
        print("SPECTATOR_NO_LEAK_OK")

        # --- read-only ----------------------------------------------
        assert srv.arena_watch_blocks(eye, "move")
        assert srv.arena_watch_blocks(eye, "attack")
        assert srv.arena_watch_blocks(eye, "say")
        assert not srv.arena_watch_blocks(eye, "arena")
        assert not srv.arena_watch_blocks(duel_a, "move")
        assert "arena_watch_blocks(player, cmd)" in inspect.getsource(
            srv.handle_connection
        ), "dispatch dropped the spectator guard"
        print("SPECTATOR_READONLY_OK")

        # --- the feed carries what the match says --------------------
        await srv._arena_begin_round(match)
        assert match.events, "a round start must reach the feed"
        assert len(match.events) <= srv.ARENA_SPECTATOR_EVENTS
        assert "Round 1" in match.events[-1]
        assert last("SpecEye", "message") is not None
        duel_b.hp = 1
        await srv.arena_attack(duel_a, "SpecTwo")
        assert match.scores[1] == 1
        hit = last("SpecEye", "combat")
        assert hit is not None and "SpecOne" in hit["text"], hit
        print("SPECTATOR_EVENTS_OK")

        # --- the audience is capped ---------------------------------
        for index in range(srv.ARENA_SPECTATOR_MAX - 1):
            filler = mkplayer(f"SpecFill{index}", 64100 + index)
            filler.arena_watch = match.id
            filler.arena_spectating = True
            match.spectators.add(filler.id)
        assert len(match.spectators) == srv.ARENA_SPECTATOR_MAX
        late = mkplayer("SpecLate", 64004)
        await srv.cmd_arena(late, {"action": "watch", "match": str(match.id)})
        assert not late.arena_spectating and late.arena_watch == 0
        err = last("SpecLate", "error")
        assert err is not None and "audience" in err["text"], err
        print("SPECTATOR_CAP_OK")

        # --- unwatch puts the session back in the world --------------
        await srv.cmd_arena(eye, {"action": "unwatch"})
        assert not eye.arena_spectating and eye.arena_watch == 0
        assert eye.id not in match.spectators
        await srv.cmd_arena(eye, {"action": "unwatch"})
        assert last("SpecEye", "error") is not None
        assert last("SpecEye", "arena_view") is not None

        # --- watching changes nothing -------------------------------
        async def play(watched):
            left = mkplayer("NeutX", 64301, attack=60)
            right = mkplayer("NeutY", 64302, attack=60)
            await srv._arena_start(1, [left.id, right.id])
            live = srv.match_for_player(left)
            watcher = None
            if watched:
                watcher = mkplayer("NeutWatch", 64303)
                await srv.cmd_arena(watcher, {"action": "watch", "match": str(live.id)})
            # Drive the match clock by hand: every tick is past its timer, so
            # one call per phase (countdown, fighting, pause) plays a round.
            guard = 0
            while srv.match_for_player(left) is not None and guard < 30:
                guard += 1
                if live.state == "fighting":
                    right.hp = 1
                await srv._arena_tick_match(live, time.time() + 1000)
            row = srv.ARENA_HISTORY[-1]
            return (
                {
                    "winner": row["winner"],
                    "scores": row["scores"],
                    "teams": row["teams"],
                    "deltas": row["deltas"],
                },
                live.id,
                watcher,
            )

        srv.ARENA_RATINGS.clear()
        plain, _, _ = await play(False)
        assert plain["winner"] == 1 and plain["scores"] == {"team1": 2, "team2": 0}
        srv.ARENA_RATINGS.clear()
        watched, watched_id, watcher = await play(True)
        assert watched == plain, (plain, watched)
        print("SPECTATOR_NEUTRAL_OK")

        # --- teardown closes the feed -------------------------------
        ended = last("NeutWatch", "arena_end")
        assert ended is not None, "a watcher must learn the match is over"
        assert ended["match_id"] == watched_id and ended["winner"] == 1
        assert watcher.arena_watch == 0 and not watcher.arena_spectating
        assert watched_id not in srv.ARENA_MATCHES
        print("SPECTATOR_TEARDOWN_OK")

        # --- no death, no value, no world counter -------------------
        assert len(srv.DEATH_LOG) == death_log_len
        for pl in spawned:
            entry = srv.SCORES.get(pl.name.lower()) or {}
            assert entry.get("deaths", 0) == 0, (pl.name, entry)
            assert entry.get("civ_deaths", 0) == 0, pl.name
            assert entry.get("wild_deaths", 0) == 0, pl.name

        print("ARENA_SPECTATOR_OK")
    finally:
        srv.send = saved_send
        srv.ARENA_MATCHES.clear()
        srv.ARENA_MATCHES.update(saved_matches)
        for size, rows in saved_queues.items():
            srv.ARENA_QUEUES[size][:] = rows
        srv.ARENA_RATINGS.clear()
        srv.ARENA_RATINGS.update(saved_ratings)
        del srv.ARENA_HISTORY[history_len:]
        for pl in spawned:
            srv.remove_member(pl)
            srv.players.pop(pl.id, None)
            srv.SCORES.pop(pl.name.lower(), None)
            srv.name_owners.pop(pl.name.lower(), None)
            srv.LAST_HIT.pop(pl.id, None)


asyncio.run(main())
