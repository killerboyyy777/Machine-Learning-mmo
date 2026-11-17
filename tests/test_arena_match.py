"""Arena match core (#652): queue, pair, fight, tear down.

Run from the repo root:  python tests/test_arena_match.py

The phase's acceptance is a 2v2 that runs start to finish with real clients,
and a match that moves no value: no loot, no gold, no gear, no death log, no
civ/wild counter. These cases drive the real handlers, not a mock match, so a
regression in dispatch or in the room view shows up here.
"""

import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


async def main():
    inbox = {}

    async def fake_send(player, payload):
        inbox.setdefault(player.name, []).append(payload)

    saved_send = srv.send
    srv.send = fake_send

    def mkplayer(name, pid, room="town_square", hp=20, max_hp=20, attack=6):
        player = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
        player.hp, player.max_hp, player.room = hp, max_hp, room
        player.base_attack = attack
        srv.players[pid] = player
        srv.add_member(player)
        return player

    def teardown(players_list):
        for player in players_list:
            srv.remove_member(player)
            srv.players.pop(player.id, None)
            srv.SCORES.pop(player.name.lower(), None)
            srv.name_owners.pop(player.name.lower(), None)
            srv.LAST_HIT.pop(player.id, None)

    def frames(name, kind):
        return [f for f in inbox.get(name, []) if f.get("type") == kind]

    def text_of(name):
        return [f.get("text", "") for f in inbox.get(name, [])]

    saved_matches = dict(srv.ARENA_MATCHES)
    saved_queues = {k: list(v) for k, v in srv.ARENA_QUEUES.items()}
    civ_before = {
        name: (entry.get("civ_deaths", 0), entry.get("wild_deaths", 0))
        for name, entry in srv.SCORES.items()
    }
    deaths_before = len(srv.DEATH_LOG)
    try:
        # --- Queue and pairing -------------------------------------------
        one = mkplayer("ArenaOne", 62001)
        two = mkplayer("ArenaTwo", 62002)
        await srv.cmd_arena(one, {"action": "enter", "mode": "2v2"})
        assert srv.arena_queued_size(one) == 2
        assert not srv.match_for_player(one)
        await srv.cmd_arena(two, {"action": "enter", "mode": "2v2"})
        assert srv.arena_queued_size(two) == 2
        # A stale entry never holds a slot: an offline session is pruned.
        ghost = mkplayer("ArenaGhost", 62003)
        await srv.cmd_arena(ghost, {"action": "enter", "mode": "1v1"})
        ghost.logged_in = False
        three = mkplayer("ArenaThree", 62004)
        four = mkplayer("ArenaFour", 62005)
        await srv.cmd_arena(three, {"action": "enter", "mode": "2v2"})
        await srv.cmd_arena(four, {"action": "enter", "mode": "2v2"})
        assert len(srv.ARENA_MATCHES) == 1, srv.ARENA_MATCHES
        match = next(iter(srv.ARENA_MATCHES.values()))
        assert match.team_size == 2 and len(match.players) == 4
        assert not srv.ARENA_QUEUES[2]
        fighters = [one, two, three, four]
        assert all(
            p.arena_match == match.id and p.room == match.room_id for p in fighters
        )
        assert match.room_id not in srv.ROOMS
        print("PAIRING_OK")

        # --- The room view knows what it is ------------------------------
        view = srv.room_view(match.room_id)
        assert view["type"] == "room" and view["id"] == match.room_id
        assert view["arena"]["match_id"] == match.id
        assert sorted(view["arena"]["teams"]["team1"]) == ["ArenaOne", "ArenaTwo"]
        assert sorted(view["arena"]["teams"]["team2"]) == ["ArenaFour", "ArenaThree"]
        assert not view["exits"] and not view["npcs"] and view["gold"] == 0
        # Instanced rooms are invisible to the world and the ML action space.
        assert match.room_id not in srv.world_snapshot()["rooms"]
        # Walking out is refused; leaving is the forfeit path.
        await srv.cmd_move(one, {"dir": "north"})
        assert "arena leave" in text_of("ArenaOne")[-1]
        one.hp = 1
        await srv.cmd_flee(one, {})
        assert "no way out" in text_of("ArenaOne")[-1].lower()
        one.hp = one.max_hp
        print("ARENA_ROOM_OK")

        # --- A round: player-versus-player, no world side effects ---------
        await srv._arena_begin_round(match)
        assert match.round == 1 and match.state == "fighting"
        assert match.team_of(one.id) == 1 and match.team_of(three.id) == 2
        # Exercise the real WebSocket dispatcher: companion adoption and
        # equipment changes submitted during a match must be rejected.
        assert "companion" in srv.ARENA_BLOCKED_COMMANDS
        assert "equip" in srv.ARENA_BLOCKED_COMMANDS
        handler_calls = []
        saved_companion_handler = srv.HANDLERS["companion"]
        saved_arena_disconnect = srv.arena_on_disconnect
        companion_key = "arenadispatchguard"
        companion_store_before = copy.deepcopy(srv.COMPANION_PLAYERS.get(companion_key))
        acquisition_stats_before = copy.deepcopy(srv.ACQUISITION_STATS)

        async def companion_spy(player, msg):
            handler_calls.append((player, msg))

        class ArenaCommandWS(FakeWS):
            def __aiter__(self):
                async def messages():
                    player = next(p for p in srv.players.values() if p.ws is self)
                    player.name = "ArenaDispatchGuard"
                    player.logged_in = True
                    player.arena_match = match.id
                    player.gold = 123
                    player.inventory = [
                        "rusty_sword",
                        "reinforced_leather",
                        "old_shield",
                    ]
                    player.equipped = None
                    player.armor = None
                    player.offhand = None
                    srv.COMPANION_PLAYERS[companion_key] = {
                        "companions": [],
                        "active": 0,
                        "updated_at": time.time(),
                    }
                    companion_store_expected = copy.deepcopy(
                        srv.COMPANION_PLAYERS[companion_key]
                    )
                    yield json.dumps(
                        {
                            "cmd": "companion",
                            "action": "adopt",
                            "species": "wolf",
                        }
                    )
                    # The dispatcher has resumed the iterator only after it
                    # has either invoked the handler or rejected the command.
                    assert not handler_calls
                    assert player.gold == 123
                    assert (
                        srv.COMPANION_PLAYERS[companion_key] == companion_store_expected
                    )
                    assert srv.ACQUISITION_STATS == acquisition_stats_before

                    # Fighters cannot swap weapon, armor or offhand mid-match.
                    for item in ("rusty sword", "reinforced leather", "old shield"):
                        yield json.dumps({"cmd": "equip", "item": item})
                        assert (
                            player.equipped,
                            player.armor,
                            player.offhand,
                        ) == (None, None, None)
                        assert (
                            "You cannot 'equip' during a match."
                            in text_of("ArenaDispatchGuard")[-1]
                        )

                return messages()

        srv.HANDLERS["companion"] = companion_spy

        async def no_arena_cleanup(player):
            return None

        srv.arena_on_disconnect = no_arena_cleanup
        try:
            await srv.handle_connection(ArenaCommandWS())
            assert not handler_calls
        finally:
            srv.HANDLERS["companion"] = saved_companion_handler
            srv.arena_on_disconnect = saved_arena_disconnect
            if companion_store_before is None:
                srv.COMPANION_PLAYERS.pop(companion_key, None)
            else:
                srv.COMPANION_PLAYERS[companion_key] = companion_store_before
        # Team-mates are not targets.
        await srv.cmd_attack(one, {"target": "ArenaTwo"})
        assert "team-mate" in text_of("ArenaOne")[-1]
        three.hp = 1
        await srv.cmd_attack(one, {"target": "ArenaThree"})
        assert three.hp == 0, three.hp
        assert match.state == "pause" and match.scores[1] == 1
        assert any("ArenaThree is down" in t for t in text_of("ArenaOne"))
        assert len(srv.DEATH_LOG) == deaths_before
        for fighter in fighters:
            assert fighter.room == match.room_id
            assert srv.SCORES.get(fighter.name.lower(), {}).get("civ_deaths", 0) == 0
        print("PVP_ROUND_OK")

        # --- Round two, then the match ends and everyone goes home ---------
        match.timer = time.time() - 1
        await srv._arena_tick_match(match, time.time())
        assert (match.state, match.round) == ("fighting", 2), (match.state, match.round)
        assert all(p.hp == p.max_hp for p in fighters)
        three.hp = 1
        await srv.cmd_attack(one, {"target": "ArenaThree"})
        assert match.scores[1] == 2
        match.timer = time.time() - 1
        await srv._arena_tick_match(match, time.time())
        assert match.id not in srv.ARENA_MATCHES
        for fighter in fighters:
            assert fighter.arena_match == 0 and fighter.room == "town_square"
            assert fighter.hp == 20
        gold_before = 0
        assert all(p.gold == gold_before for p in fighters)
        assert not srv.room_items.get(match.room_id)
        assert not srv.room_gold.get(match.room_id)
        assert len(srv.DEATH_LOG) == deaths_before
        print("MATCH_TEARDOWN_OK")

        # --- Disconnect forfeits and frees the others ---------------------
        one.room, two.room = "town_square", "town_square"
        three.room, four.room = "town_square", "town_square"
        for player in fighters:
            await srv.cmd_arena(player, {"action": "enter", "mode": "2v2"})
        match = next(iter(srv.ARENA_MATCHES.values()))
        await srv.arena_on_disconnect(one)
        assert match.id not in srv.ARENA_MATCHES
        assert one.arena_match == 0
        for player in (two, three, four):
            assert player.arena_match == 0 and player.room == "town_square"
        # A queued session leaves no entry behind.
        two.logged_in = False
        await srv.cmd_arena(three, {"action": "enter", "mode": "1v1"})
        await srv.arena_on_disconnect(three)
        assert not srv.ARENA_QUEUES[1]
        two.logged_in = True
        print("DISCONNECT_FORFEIT_OK")

        # --- Leaving forfeits; an idle round times out -------------------
        await srv.cmd_arena(three, {"action": "enter", "mode": "1v1"})
        await srv.cmd_arena(four, {"action": "enter", "mode": "1v1"})
        match = next(iter(srv.ARENA_MATCHES.values()))
        await srv.cmd_arena(three, {"action": "leave"})
        assert match.id not in srv.ARENA_MATCHES
        assert four.arena_match == 0 and four.room == "town_square"
        assert any("leaves the arena" in t for t in text_of("ArenaFour"))
        await srv.cmd_arena(three, {"action": "enter", "mode": "1v1"})
        await srv.cmd_arena(four, {"action": "enter", "mode": "1v1"})
        match = next(iter(srv.ARENA_MATCHES.values()))
        match.state = "fighting"
        four.hp = 5
        match.timer = time.time() - 1
        await srv._arena_tick_match(match, time.time())
        assert match.state == "pause" and match.scores[1] == 1
        match.timer = time.time() - 1
        await srv._arena_tick_match(match, time.time())
        assert match.state == "fighting", match.state
        print("LEAVE_AND_TIMEOUT_OK")

        # --- Outside a match, a player is not a target -------------------
        await srv.cmd_arena(three, {"action": "leave"})
        assert not srv.ARENA_MATCHES
        inbox.clear()
        four.hp = four.max_hp = 20
        assert three.room == four.room == "town_square"
        await srv.cmd_attack(three, {"target": "ArenaFour"})
        assert four.hp == 20
        assert "No 'ArenaFour' here to attack." in text_of("ArenaThree")[-1]
        assert "gather" in srv.ARENA_BLOCKED_COMMANDS
        assert "companion" in srv.ARENA_BLOCKED_COMMANDS
        assert "equip" in srv.ARENA_BLOCKED_COMMANDS
        assert "attack" not in srv.ARENA_BLOCKED_COMMANDS
        print("NO_WORLD_PVP_OK")

        # --- The one command index actually names the arena --------------
        await srv.cmd_help(three, {})
        help_text = frames("ArenaThree", "help")[-1]["text"]
        assert "arena [enter|leave|status|ladder|watch|unwatch]" in help_text, help_text
        print("HELP_OK")

        print("ARENA_MATCH_OK")
    finally:
        srv.send = saved_send
        srv.ARENA_MATCHES.clear()
        srv.ARENA_MATCHES.update(saved_matches)
        for size, entries in saved_queues.items():
            srv.ARENA_QUEUES[size][:0] = entries
        for name in [n for n in srv.SCORES if n.startswith("arena")]:
            srv.SCORES.pop(name, None)
        # Only survivors: the cleanup above removes this test's own arena-named
        # rows, and a local scores.json can hold arena probe entries that share
        # the same prefix. Comparing the whole dict then fails on the missing
        # keys, not on a counter that moved.
        moved = {
            name: (
                civ_before[name],
                (entry.get("civ_deaths", 0), entry.get("wild_deaths", 0)),
            )
            for name, entry in srv.SCORES.items()
            if name in civ_before
            and civ_before[name]
            != (entry.get("civ_deaths", 0), entry.get("wild_deaths", 0))
        }
        assert not moved, moved


asyncio.run(main())
