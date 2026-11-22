import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv
from server import Player, add_member


class FixedRng:
    """Deterministic stand-in for srv._FLEE_RNG: always returns `value`."""

    def __init__(self, value):
        self.value = value

    def randint(self, low, high):
        return self.value


async def test_flee_trigger_hp():
    srv.npcs.clear()
    # Setup: Player with low HP in a hostile room
    player = Player(name="TestFleeHP", hp=2, max_hp=20, room="town_square")
    # Manually add a hostile to town_square for testing
    # We need to bypass the global NPCs dict if we want to be safe,
    # but since this is a unit test, we can just inject.
    srv.npcs["test_mob"] = {
        "id": "test_mob",
        "name": "Angry Mob",
        "room": "town_square",
        "hostile": True,
        "alive": True,
    }
    add_member(player)

    # Should trigger flee (2/20 = 10% < 25%)
    # Mock send to avoid websocket errors
    async def mock_send(p, msg):
        pass

    srv.send = mock_send

    # Use a mock msg
    try:
        await srv.cmd_flee(player, {})
    except Exception as e:
        print(f"Flee failed: {e}")
        return False

    # Check if player moved (assuming town_square has exits)
    # town_square exits: {"north": "...", ...}
    # Let's check if player.room changed
    if player.room != "town_square":
        print("Successfully fled due to low HP")
        return True
    else:
        print("Failed to flee due to low HP")
        return False


async def test_flee_trigger_outnumbered():
    srv.npcs.clear()
    # Setup: Healthy player outnumbered
    player = Player(name="TestFleeOut", hp=20, max_hp=20, room="town_square")
    # 3 hostiles vs 1 player
    srv.npcs["mob1"] = {
        "id": "mob1",
        "name": "Mob 1",
        "room": "town_square",
        "hostile": True,
        "alive": True,
    }
    srv.npcs["mob2"] = {
        "id": "mob2",
        "name": "Mob 2",
        "room": "town_square",
        "hostile": True,
        "alive": True,
    }
    srv.npcs["mob3"] = {
        "id": "mob3",
        "name": "Mob 3",
        "room": "town_square",
        "hostile": True,
        "alive": True,
    }
    add_member(player)

    async def mock_send(p, msg):
        pass

    srv.send = mock_send

    try:
        await srv.cmd_flee(player, {})
    except Exception as e:
        print(f"Flee failed: {e}")
        return False

    if player.room != "town_square":
        print("Successfully fled due to being outnumbered")
        return True
    else:
        print("Failed to flee due to being outnumbered")
        return False


async def test_no_flee_healthy():
    srv.npcs.clear()
    # Setup: Healthy player, not outnumbered
    player = Player(name="TestNoFlee", hp=20, max_hp=20, room="town_square")
    # One hostile vs 1 player (not outnumbered)
    srv.npcs["mob1"] = {
        "id": "mob1",
        "name": "Mob 1",
        "room": "town_square",
        "hostile": True,
        "alive": True,
    }
    add_member(player)

    captured_errors = []

    async def mock_send(p, msg):
        if isinstance(msg, dict) and msg.get("type") == "error":
            captured_errors.append(msg.get("text"))

    srv.send = mock_send

    try:
        await srv.cmd_flee(player, {})
    except Exception as e:
        print(f"Error: {e}")
        return False

    # If the player didn't move and we got the danger error, it's correct.
    if player.room == "town_square" and any(
        "not in enough danger" in e for e in captured_errors
    ):
        print("Correctly did not flee")
        return True
    else:
        print(
            f"Incorrectly fled or no error: room={player.room}, errors={captured_errors}"
        )
        return False


async def test_flee_dungeon():
    """Fleeing in an instance is a percentile roll, not a guaranteed exit.

    A success leaves the instance (floor 1 -> graveyard, or an uncleared
    deeper floor -> the floor below, bypassing the old absolute seal). A
    failure costs the turn and lands one free enemy attack without moving.
    """
    srv.npcs.clear()
    dungeon = srv.Dungeon(party_id=0)
    srv.dungeons[dungeon.id] = dungeon
    saved_send, saved_rng, saved_apply = srv.send, srv._FLEE_RNG, srv.npc_apply
    sent = []

    async def mock_send(p, msg):
        sent.append(msg)

    applied = []

    async def mock_apply(npc, intent, ctx, now):
        applied.append((npc, intent, ctx))

    srv.send = mock_send
    srv.npc_apply = mock_apply
    delvers = []
    try:
        dungeon.floor(1)
        delver = Player(name="TestFleeFloor1", hp=2, max_hp=20, room=dungeon.room_id(1))
        delvers.append(delver)
        add_member(delver)
        srv._FLEE_RNG = FixedRng(1)  # force success
        await srv.cmd_flee(delver, {})
        assert delver.room == srv.DUNGEON_ENTRANCE_ROOM, (delver.room, sent)
        assert not [m for m in sent if m.get("type") == "error"], sent
        print("Dungeon floor 1 flee left the instance")

        dungeon.floor(2)  # guards alive: sealed, but flee no longer blocks
        sent.clear()
        stuck = Player(name="TestFleeSealed", hp=2, max_hp=20, room=dungeon.room_id(2))
        delvers.append(stuck)
        add_member(stuck)
        srv._FLEE_RNG = FixedRng(100)  # force failure
        await srv.cmd_flee(stuck, {})
        assert stuck.room == dungeon.room_id(2), stuck.room
        assert applied, "failed flee should land one free enemy swing"
        assert applied[-1][1]["kind"] == "attack", applied[-1]
        assert "stumble" in " ".join(
            m.get("text", "") for m in sent if isinstance(m, dict)
        ), sent
        print("Failed dungeon flee costs a free swing and stays put")

        sent.clear()
        applied.clear()
        srv._FLEE_RNG = FixedRng(1)  # force success
        await srv.cmd_flee(stuck, {})
        assert stuck.room == dungeon.room_id(1), (stuck.room, sent)
        assert not applied, applied
        print("Dungeon floor 2 flee bypasses the seal on success")
        return True
    finally:
        srv.send = saved_send
        srv._FLEE_RNG = saved_rng
        srv.npc_apply = saved_apply
        for p in delvers:
            srv.remove_member(p)
        srv.dungeons.pop(dungeon.id, None)


def test_flee_odds_monotonic():
    """Odds fall with depth, rise with level, and fall when sealed (#436)."""
    shallow = srv._flee_success_pct(1, 1, True)
    deep = srv._flee_success_pct(10, 1, True)
    assert deep < shallow, (deep, shallow)
    low_level = srv._flee_success_pct(5, 1, True)
    high_level = srv._flee_success_pct(5, 10, True)
    assert high_level > low_level, (high_level, low_level)
    sealed = srv._flee_success_pct(5, 1, False)
    assert sealed < low_level, (sealed, low_level)
    boss = srv._flee_success_pct(srv.DUNGEON_MAX_FLOOR, 10, True, True)
    assert 0 < boss <= srv.FLEE_BOSS_PCT, boss
    assert all(
        0 <= v <= 100 for v in (shallow, deep, low_level, high_level, sealed, boss)
    )
    print("Flee odds scale with depth, level, seal and boss")
    return True


def test_flee_roll_is_deterministic():
    assert srv._flee_roll(50, FixedRng(1)) is True
    assert srv._flee_roll(50, FixedRng(100)) is False
    assert srv._flee_roll(100, FixedRng(100)) is True
    assert srv._flee_roll(0, FixedRng(1)) is False
    print("Flee roll is deterministic under an injected RNG")
    return True


async def main():
    results = []
    results.append(await test_flee_trigger_hp())
    results.append(await test_flee_trigger_outnumbered())
    results.append(await test_no_flee_healthy())
    results.append(await test_flee_dungeon())
    results.append(test_flee_odds_monotonic())
    results.append(test_flee_roll_is_deterministic())

    if all(results):
        print("All flee tests passed!")
        sys.exit(0)
    else:
        print(f"Some tests failed: {results}")
        sys.exit(1)


if __name__ == "__main__":
    import sys

    asyncio.run(main())
