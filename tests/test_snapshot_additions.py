"""Behavioral unit tests for additive snapshot keys in server.py.

Conventions: ASCII-only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


def test_commissions_snapshot():
    # 1. Commissions board key: all statuses present, newest-first, newest-100 cap
    # (construct >100, assert truncation keeps newest).
    orig_commissions = dict(srv._commissions)
    try:
        srv._commissions.clear()
        statuses = ["open", "filled", "cancelled"]
        # Create 120 commissions with sequential IDs 1..120 and varying statuses
        for i in range(1, 121):
            st = statuses[i % len(statuses)]
            srv._commissions[i] = {
                "id": i,
                "poster": f"player_{i}",
                "target": "wolf",
                "required_kills": 5,
                "reward_gold": 50,
                "reward_xp": 100,
                "status": st,
                "filled_by": "helper" if st == "filled" else None,
                "created_ts": 1000 + i,
            }

        snap = srv._commission_snapshot()
        # Cap check: length is 100
        assert (
            len(snap) == 100
        ), f"Expected 100 commissions in snapshot, got {len(snap)}"

        # Order check: newest-first (descending ID)
        ids = [c["id"] for c in snap]
        assert ids == list(
            range(120, 20, -1)
        ), "Snapshot is not newest-first or truncated improperly"

        # Truncation check: IDs 1..20 are absent, 21..120 are present
        assert 120 in ids, "Newest commission ID 120 missing"
        assert 21 in ids, "Newest commission ID 21 missing"
        assert 20 not in ids, "Oldest truncated commission ID 20 present"
        assert 1 not in ids, "Oldest truncated commission ID 1 present"

        # All statuses check
        snap_statuses = {c["status"] for c in snap}
        assert snap_statuses == {
            "open",
            "filled",
            "cancelled",
        }, f"Missing statuses in snapshot: {snap_statuses}"

        # Verify world_snapshot() commissions key matches
        world_snap = srv.world_snapshot()
        assert "commissions" in world_snap
        assert len(world_snap["commissions"]) == 100
        assert world_snap["commissions"][0]["id"] == 120

        print("COMMISSIONS_SNAPSHOT_OK")
    finally:
        srv._commissions.clear()
        srv._commissions.update(orig_commissions)


def test_recent_turnins():
    # 2. quests.recent_turnins: 50-deep ring, newest-first, oldest evicted past 50.
    orig_feed = list(srv.quest_turnin_feed)
    orig_seq = srv.quest_feed_seq
    try:
        srv.quest_turnin_feed.clear()
        srv.quest_feed_seq = 0

        # Simulate 60 quest turnins
        for i in range(1, 61):
            srv.quest_feed_seq += 1
            srv.quest_turnin_feed.append(
                {
                    "seq": srv.quest_feed_seq,
                    "t": "12:00:00",
                    "name": f"player_{i}",
                    "qid": "rat_catcher",
                }
            )
            while len(srv.quest_turnin_feed) > srv.TURNIN_FEED_SIZE:
                del srv.quest_turnin_feed[0]

        # Ring buffer size check: capped at 50
        assert (
            len(srv.quest_turnin_feed) == 50
        ), f"Expected 50 items in ring, got {len(srv.quest_turnin_feed)}"

        # Eviction check: seq 1..10 evicted, 11..60 present in ring
        ring_seqs = [t["seq"] for t in srv.quest_turnin_feed]
        assert ring_seqs == list(range(11, 61)), "Ring buffer eviction failed"
        assert 10 not in ring_seqs, "Evicted seq 10 still present in ring"
        assert 1 not in ring_seqs, "Evicted seq 1 still present in ring"

        # Check snapshot key quests.recent_turnins directly from world_snapshot()
        world_snap = srv.world_snapshot()
        recent = world_snap["quests"]["recent_turnins"]
        assert isinstance(recent, list)
        assert len(recent) <= 50, "recent_turnins exceeds 50-deep ring size"

        recent_seqs = [t["seq"] for t in recent]
        # Verify evicted items 1..10 are absent from recent_turnins
        for evicted_seq in range(1, 11):
            assert (
                evicted_seq not in recent_seqs
            ), f"Evicted seq {evicted_seq} present in recent_turnins"

        # Raw feed slice in snapshot is chronological (newest at recent[-1])
        assert (
            recent[-1]["seq"] == 60
        ), f"Expected newest seq 60 at end of recent_turnins feed, got {recent[-1]['seq']}"

        # In newest-first order (e.g. recent[::-1]): newest seq 60 is first, descending sequence
        newest_first = recent[::-1]
        assert (
            newest_first[0]["seq"] == 60
        ), f"Expected newest seq 60 first in newest-first order, got {newest_first[0]['seq']}"
        for idx in range(len(newest_first) - 1):
            assert (
                newest_first[idx]["seq"] > newest_first[idx + 1]["seq"]
            ), "newest_first order is not strictly descending"

        print("QUEST_TURNINS_SNAPSHOT_OK")
    finally:
        srv.quest_turnin_feed.clear()
        srv.quest_turnin_feed.extend(orig_feed)
        srv.quest_feed_seq = orig_seq


def test_recipes_snapshot():
    # 3. Recipes key: 17 precomputed views present with resolved item names.
    snap = srv.world_snapshot()
    recipes = snap.get("recipes", [])
    assert len(recipes) == 17, f"Expected 17 recipe views, got {len(recipes)}"

    for r in recipes:
        assert "id" in r and isinstance(r["id"], str)
        assert "result" in r and isinstance(r["result"], str)
        assert "result_qty" in r and isinstance(r["result_qty"], int)
        assert "inputs" in r and isinstance(r["inputs"], list)
        assert "tier" in r and isinstance(r["tier"], int)
        assert "category" in r and isinstance(r["category"], str)

        # Verify raw item ID vs resolved display name
        raw_rec = srv.RECIPES.get(r["id"], {})
        raw_result_id = raw_rec.get("result", r["id"])
        expected_result_name = srv.ITEM_DEFS.get(raw_result_id, {}).get(
            "name", raw_result_id
        )
        assert (
            r["result"] == expected_result_name
        ), f"Result name not resolved for {r['id']}"

        for inp in r["inputs"]:
            assert "item" in inp and isinstance(inp["item"], str)
            assert "qty" in inp and isinstance(inp["qty"], int)

    print("RECIPES_SNAPSHOT_OK")


def test_steam_medians():
    # 4. Steam medians: median math on a known fill set (odd + even counts)
    # computed from server.py's market history snapshot data.
    orig_history = list(srv.market_history)
    try:

        def get_steam_item_median_from_snapshot(item_name):
            snap = srv.world_snapshot()
            fills = snap.get("market", {}).get("history", [])
            prices = [h["price"] for h in fills if h.get("item") == item_name]
            if not prices:
                return None
            s = sorted(prices)
            n = len(s)
            m = n // 2
            return s[m] if n % 2 != 0 else (s[m - 1] + s[m]) / 2.0

        # Test empty fill set from snapshot
        srv.market_history.clear()
        assert get_steam_item_median_from_snapshot("Wolf Pelt") is None

        # Test odd count fill set from snapshot (3 fills: 10, 30, 20 -> sorted: 10, 20, 30 -> median 20)
        srv.market_history.extend(
            [
                {
                    "time": "12:00",
                    "ts": 100,
                    "buyer": "a",
                    "seller": "b",
                    "item": "Wolf Pelt",
                    "price": 10,
                    "tax": 1,
                    "payout": 9,
                },
                {
                    "time": "12:01",
                    "ts": 101,
                    "buyer": "a",
                    "seller": "b",
                    "item": "Wolf Pelt",
                    "price": 30,
                    "tax": 3,
                    "payout": 27,
                },
                {
                    "time": "12:02",
                    "ts": 102,
                    "buyer": "a",
                    "seller": "b",
                    "item": "Wolf Pelt",
                    "price": 20,
                    "tax": 2,
                    "payout": 18,
                },
                {
                    "time": "12:03",
                    "ts": 103,
                    "buyer": "a",
                    "seller": "b",
                    "item": "Iron Ore",
                    "price": 100,
                    "tax": 10,
                    "payout": 90,
                },
            ]
        )
        assert (
            get_steam_item_median_from_snapshot("Wolf Pelt") == 20.0
        ), "Odd count median failed on snapshot history"

        # Test even count fill set from snapshot (4 fills: 10, 30, 20, 40 -> sorted: 10, 20, 30, 40 -> median 25.0)
        srv.market_history.append(
            {
                "time": "12:04",
                "ts": 104,
                "buyer": "a",
                "seller": "b",
                "item": "Wolf Pelt",
                "price": 40,
                "tax": 4,
                "payout": 36,
            }
        )
        assert (
            get_steam_item_median_from_snapshot("Wolf Pelt") == 25.0
        ), "Even count median failed on snapshot history"

        print("STEAM_MEDIAN_SNAPSHOT_OK")
    finally:
        srv.market_history.clear()
        srv.market_history.extend(orig_history)


if __name__ == "__main__":
    test_commissions_snapshot()
    test_recent_turnins()
    test_recipes_snapshot()
    test_steam_medians()
    print("SNAPSHOT_ADDITIONS_OK")
