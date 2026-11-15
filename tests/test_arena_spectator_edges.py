"""Additional arena spectator lifecycle and isolation regressions.

Run from the repository root:
    python tests/test_arena_spectator_edges.py

These exercise command-boundary and lifecycle edges not covered by the
original spectator suite. They lock in the read-only queue guard and stale
audience pruning fixed in PR #666, alongside switching, disconnect, and
invalid-match cleanup. Spectators may inspect or leave, but may not queue;
stale sessions must not consume the bounded audience.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


async def main():
    inbox = {}

    async def fake_send(player, payload):
        inbox.setdefault(player.name, []).append(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    saved_send = srv.send
    saved_matches = dict(srv.ARENA_MATCHES)
    saved_queues = {size: list(rows) for size, rows in srv.ARENA_QUEUES.items()}
    spawned = []
    failures = []

    def check(condition, label):
        if condition:
            print(label + "_OK")
        else:
            failures.append(label)
            print(label + "_FAIL")

    def player(name, pid):
        value = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
        srv.players[pid] = value
        srv.add_member(value)
        spawned.append(value)
        return value

    srv.send = fake_send
    try:
        # Use empty live matches: these cases concern audience state, not
        # combat or rating math (which the primary suite already covers).
        first = srv.ArenaMatch(1)
        second = srv.ArenaMatch(1)
        srv.ARENA_MATCHES[first.id] = first
        srv.ARENA_MATCHES[second.id] = second
        watcher = player("EdgeWatcher", 64901)

        # Re-watching another match must remove the old subscription.
        await srv.cmd_arena(watcher, {"action": "watch", "match": str(first.id)})
        await srv.cmd_arena(watcher, {"action": "watch", "match": f"a_{second.id}"})
        check(
            watcher.arena_watch == second.id
            and watcher.id not in first.spectators
            and watcher.id in second.spectators,
            "SPECTATOR_SWITCH_MATCH",
        )

        # A disconnected watcher must not remain in the audience or receive
        # subsequent events.
        await srv.arena_on_disconnect(watcher)
        check(
            not watcher.arena_spectating
            and watcher.arena_watch == 0
            and watcher.id not in second.spectators,
            "SPECTATOR_DISCONNECT_DETACH",
        )

        # Stale session IDs must be pruned before enforcing the audience cap.
        second.spectators.update(range(65000, 65000 + srv.ARENA_SPECTATOR_MAX))
        newcomer = player("EdgeNewcomer", 64902)
        await srv.cmd_arena(newcomer, {"action": "watch", "match": str(second.id)})
        check(
            newcomer.arena_spectating
            and newcomer.arena_watch == second.id
            and second.spectators == {newcomer.id},
            "SPECTATOR_STALE_CAPACITY",
        )

        # The top-level dispatch allows the arena verb for status/unwatch, but
        # that must not turn the whole cmd_arena action space into a write
        # bypass. Entering the queue is not a read-only spectator operation.
        await srv.cmd_arena(newcomer, {"action": "enter", "mode": "1v1"})
        check(
            newcomer.arena_spectating
            and srv.arena_queued_size(newcomer) == 0
            and all(newcomer.id not in q for q in srv.ARENA_QUEUES.values())
            and any(
                item.get("type") == "error"
                and "watching a match" in item.get("text", "")
                for item in inbox.get(newcomer.name, [])
            ),
            "SPECTATOR_CANNOT_QUEUE",
        )

        # Invalid and ended match IDs must never attach a session.
        invalid = player("EdgeInvalid", 64903)
        await srv.cmd_arena(invalid, {"action": "watch", "match": "not-a-match"})
        check(
            not invalid.arena_spectating and invalid.arena_watch == 0,
            "SPECTATOR_INVALID_ID",
        )
        srv.ARENA_MATCHES.pop(first.id, None)
        ended = player("EdgeEnded", 64904)
        await srv.cmd_arena(ended, {"action": "watch", "match": str(first.id)})
        check(
            not ended.arena_spectating and ended.arena_watch == 0,
            "SPECTATOR_ENDED_MATCH",
        )
    finally:
        srv.send = saved_send
        srv.ARENA_MATCHES.clear()
        srv.ARENA_MATCHES.update(saved_matches)
        for size, rows in saved_queues.items():
            srv.ARENA_QUEUES[size][:] = rows
        for value in spawned:
            srv.detach_spectator(value)
            srv.remove_member(value)
            srv.players.pop(value.id, None)
            srv.SCORES.pop(value.name.lower(), None)
            srv.name_owners.pop(value.name.lower(), None)
            srv.LAST_HIT.pop(value.id, None)

    if failures:
        raise AssertionError("arena spectator regressions: " + ", ".join(failures))
    print("ARENA_SPECTATOR_EDGES_OK")


asyncio.run(main())
