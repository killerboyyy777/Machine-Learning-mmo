"""Freight identity is case-insensitive; bad backups never veto saves.

Run from the repo root:  python tests/test_freight_identity.py
"""

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402

SENT = []
ITEM = sorted(srv.ITEM_DEFS)[0]
ORIGIN, DEST = "market", "smithy"


async def _send(player, payload):
    SENT.append(payload)


async def _noop(*args, **kwargs):
    return None


srv.send = _send
srv.broadcast_room = _noop
srv.sync_room = _noop


def _player(name, pid, room=ORIGIN, gold=100, inventory=()):
    return srv.Player(
        ws=None,
        id=pid,
        name=name,
        logged_in=True,
        hp=20,
        max_hp=20,
        room=room,
        gold=gold,
        inventory=list(inventory),
    )


def _run(player, **msg):
    asyncio.run(srv.cmd_freight(player, msg))


def _reset():
    srv.FREIGHT.clear()
    srv._freight_station_at = 0.0
    for key in srv.FREIGHT_STATS:
        srv.FREIGHT_STATS[key] = 0
    SENT.clear()


def _row(cid, poster, hauler, state="open", refund=0):
    return {
        "id": cid,
        "poster": poster,
        "poster_kind": "player",
        "hauler": hauler,
        "origin": ORIGIN,
        "destination": DEST,
        "items": [ITEM],
        "fee": 20,
        "deposit": 5,
        "bonus": 0,
        "bonus_until": 0.0,
        "posted_at": 1.0,
        "expires_at": 1000.0,
        "state": state,
        "refund": refund,
        "delivered_at": 0.0,
        "lost_at": 0.0,
    }


_saved = (
    srv.FREIGHT,
    srv._freight_counter,
    srv._freight_dirty,
    srv.FREIGHT_FILE_BLOCKED,
    srv.FREIGHT_MAX_OPEN_PER_POSTER,
    dict(srv.FREIGHT_STATS),
)
try:
    # --- writes and reads use the login key, not the spelling of the moment --
    _reset()
    alice = _player("Alice", 920, inventory=[ITEM])

    def _post_alice():
        alice.inventory = [ITEM]
        _run(alice, action="post", destination=DEST, fee=10, items=[ITEM])

    _post_alice()
    rec = next(iter(srv.FREIGHT.values()))
    assert rec["poster"] == "alice", rec
    relog = _player("aLiCe", 921)
    _run(relog, action="status", id=rec["id"])
    assert rec["id"] in srv.FREIGHT and any(
        "poster alice" in m.get("text", "") for m in SENT
    ), SENT

    # A different spelling of the same name may claim a terminal contract.
    rec["state"] = "delivered"
    gold_before = relog.gold
    _run(relog, action="claim", id=rec["id"])
    assert ITEM in relog.inventory and relog.gold == gold_before, relog.gold
    assert rec["id"] not in srv.FREIGHT
    print("FREIGHT_CASE_CLAIM_OK")

    # Negative control: an unrelated name still cannot touch it.
    _reset()
    _post_alice()
    rec = next(iter(srv.FREIGHT.values()))
    stranger = _player("Bob", 922)
    _run(stranger, action="cancel", id=rec["id"])
    assert rec["id"] in srv.FREIGHT and ITEM not in stranger.inventory
    print("FREIGHT_CASE_STRANGER_REFUSED_OK")

    # The open cap counts both spellings together.
    old_cap = srv.FREIGHT_MAX_OPEN_PER_POSTER
    try:
        srv.FREIGHT_MAX_OPEN_PER_POSTER = 1
        _reset()
        _post_alice()
        assert len(srv.FREIGHT) == 1, SENT
        _run(
            _player("ALICE", 923, inventory=[ITEM]),
            action="post",
            destination=DEST,
            fee=10,
            items=[ITEM],
        )
        assert len(srv.FREIGHT) == 1, srv.FREIGHT
        assert any("already have" in m.get("text", "") for m in SENT), SENT
    finally:
        srv.FREIGHT_MAX_OPEN_PER_POSTER = old_cap
    print("FREIGHT_CASE_CAP_OK")

    # Hauler identity is normalized on write and checked on delivery/death.
    _reset()
    _post_alice()
    rec = next(iter(srv.FREIGHT.values()))
    hauler = _player("Hauler", 924, gold=20)
    _run(hauler, action="accept", id=rec["id"])
    assert rec["hauler"] == "hauler", rec
    relog_hauler = _player("HAULER", 925, room=DEST, gold=0)
    _run(relog_hauler, action="deliver", id=rec["id"])
    assert rec["state"] == "delivered", rec
    print("FREIGHT_CASE_HAULER_OK")

    # --- mixed-case rows saved by an older build migrate on load ------------
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "freight.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "version": srv.FREIGHT_VERSION,
                    "contracts": [_row(7, "Alice", "Hauler", "lost", 9)],
                    "stats": {},
                },
                f,
            )
        srv.FREIGHT_FILE_BLOCKED = False
        loaded = srv.load_freight(path)
        assert loaded[7]["poster"] == "alice" and loaded[7]["hauler"] == "hauler"
        srv.FREIGHT = loaded
        low = _player("alice", 926, gold=0)
        assert srv._freight_flush_refunds(low) == 9 and low.gold == 9
        print("FREIGHT_CASE_LOAD_MIGRATION_OK")

        # #978: an unknown-version backup must not veto a healthy primary.
        srv.FREIGHT = {}
        srv.FREIGHT_FILE_BLOCKED = False
        with open(path, "w") as f:
            json.dump(
                {
                    "version": srv.FREIGHT_VERSION,
                    "contracts": [_row(8, "Alice", None)],
                    "stats": {},
                },
                f,
            )
        with open(path + ".1", "w") as f:
            json.dump({"version": srv.FREIGHT_VERSION + 1, "contracts": []}, f)
        assert set(srv.load_freight(path)) == {8}
        assert srv.FREIGHT_FILE_BLOCKED is False, "backup blocked saves"
        assert srv.save_freight(path) is True, "save refused after backup mismatch"
        print("FREIGHT_BAD_BACKUP_SAVES_OK")

        # Negative control: a bad primary still blocks writes.
        srv.FREIGHT = {}
        srv.FREIGHT_FILE_BLOCKED = False
        with open(path, "w") as f:
            json.dump({"version": srv.FREIGHT_VERSION + 1, "contracts": []}, f)
        assert srv.load_freight(path) == {}
        assert srv.FREIGHT_FILE_BLOCKED is True
        assert srv.save_freight(path) is False
        print("FREIGHT_BAD_PRIMARY_BLOCKS_OK")
finally:
    (
        srv.FREIGHT,
        srv._freight_counter,
        srv._freight_dirty,
        srv.FREIGHT_FILE_BLOCKED,
        srv.FREIGHT_MAX_OPEN_PER_POSTER,
        stats,
    ) = _saved
    srv.FREIGHT_STATS.clear()
    srv.FREIGHT_STATS.update(stats)

print("ALL_FREIGHT_IDENTITY_OK")
