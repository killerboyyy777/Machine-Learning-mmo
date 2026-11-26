"""Crafting economy checks for #17 and #18 (no live server needed).

Run from the repo root:  python tests/test_craft_economy.py

The suite pins the split between safe gathered inputs and combat-only
materials, plus the craft counters, mastery, discovery and tier rewards.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


def mkplayer(name, pid, room="town_square"):
    pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
    pl.room = room
    srv.players[pid] = pl
    srv.add_member(pl)
    return pl


def unplayer(pl):
    srv.remove_member(pl)
    srv.players.pop(pl.id, None)


def fresh_entry(name):
    entry = srv.get_score_entry(name)
    entry["crafts_total"] = 0
    entry["crafts_by_recipe"] = {}
    entry["craft_discoveries"] = []
    entry["crafts_tier"] = {}
    entry["craft_profitability"] = {}
    entry["gathered_units"] = {}
    return entry


def test_material_sources():
    world_carriers = {
        iid: [
            nid
            for nid, npc in srv.WORLD["npcs"].items()
            if iid in (npc.get("loot") or [])
        ]
        for iid in srv.FIGHTER_MATERIALS
    }
    dungeon = srv.Dungeon(party_id=990001)
    dungeon_loot = set()
    for floor in range(1, srv.DUNGEON_MAX_FLOOR + 1):
        for guard in dungeon.floor(floor).guards:
            dungeon_loot.update(guard.get("loot") or [])
    for iid, carriers in world_carriers.items():
        assert carriers or iid in dungeon_loot, iid
        assert all(not srv.is_quest_giver(nid) for nid in carriers), iid
        assert all(node["item"] != iid for node in srv.gather_nodes.values()), iid
    for rid, ids in srv.WORLD["room_items"].items():
        assert not (set(ids or []) & srv.FIGHTER_MATERIALS), rid

    for iid, room in (
        ("healing_herb", "guild_hall"),
        ("mountain_berry", "artisan_row"),
    ):
        nodes = [
            n
            for n in srv.gather_nodes.values()
            if n["item"] == iid and n["room"] == room
        ]
        assert nodes, iid
        assert not srv.ROOMS[room].get("wild"), room
        if room == "guild_hall":
            assert srv.ROOMS[room].get("shelter"), room
        assert all(
            1 <= n["min_yield"] <= n["max_yield"] <= 3 and n["respawn_seconds"] >= 20
            for n in nodes
        ), nodes
        definition = srv.ITEM_DEFS[iid]
        used = any(
            iid in (recipe.get("inputs") or {}) for recipe in srv.RECIPES.values()
        )
        consumable = definition.get("type") == "consumable" and (
            definition.get("heal_amount", 0) > 0 or definition.get("buff")
        )
        assert used or consumable, iid

    bad = {**srv.WORLD, "gather_nodes": dict(srv.WORLD["gather_nodes"])}
    bad["gather_nodes"]["bad_pelt_node"] = {
        "room": "guild_hall",
        "item": "wolf_pelt",
    }
    errors = srv.validate_world(bad)
    assert any("fighter-only" in e for e in errors), errors
    print("MATERIAL_SOURCES_OK")


def test_safe_gather_and_fighter_guard():
    sent = []
    original_send = srv.send

    async def capture(player, payload):
        sent.append(payload)

    srv.send = capture
    player = mkplayer("SafeGatherTester", 95001, room="guild_hall")
    entry = fresh_entry(player.name)
    node = next(
        n
        for n in srv.gather_nodes.values()
        if n["room"] == "guild_hall" and n["item"] == "healing_herb"
    )
    node["available"] = True
    node["respawn_at"] = None
    try:
        asyncio.run(srv.cmd_gather(player, {"node": "healing_herb"}))
        harvested = player.inventory.count("healing_herb")
        assert 1 <= harvested <= 2, harvested
        assert entry["gathered_units"]["healing_herb"] == harvested
        assert any(
            m.get("type") == "message" and "You gather" in m.get("text", "")
            for m in sent
        ), sent
        assert not node["available"] and node["respawn_at"] > time.time()
        assert srv.merchant_sell_value("iron_ore", gathered=True) == 2

        sent.clear()
        fake = {
            "id": "fake_pelt_node",
            "room": "guild_hall",
            "item": "wolf_pelt",
            "min_yield": 1,
            "max_yield": 1,
            "respawn_seconds": 30,
            "available": True,
            "respawn_at": None,
        }
        srv.gather_nodes[fake["id"]] = fake
        try:
            asyncio.run(srv.cmd_gather(player, {"node": "wolf_pelt"}))
            assert "wolf_pelt" not in player.inventory
            assert fake["available"], "a refused node must not start its cooldown"
            assert any(
                m.get("type") == "error" and "comes from combat" in m.get("text", "")
                for m in sent
            ), sent
        finally:
            del srv.gather_nodes[fake["id"]]
    finally:
        srv.send = original_send
        unplayer(player)
        srv.SCORES.pop(player.name.lower(), None)
    print("SAFE_GATHER_OK")


def test_craft_mastery_and_tier_rewards():
    awarded = []
    original_points = srv.award_points_to_name
    original_xp = srv.award_xp

    async def fake_points(name, base, reason):
        awarded.append(("score", name, base))

    async def fake_xp(name, amount, reason):
        awarded.append(("xp", name, amount))

    srv.award_points_to_name = fake_points
    srv.award_xp = fake_xp
    name = "CraftMasteryTester"
    entry = fresh_entry(name)
    try:
        recipe = srv.RECIPES["sharpening_oil"]
        asyncio.run(
            srv._record_craft_completion(
                name,
                "sharpening_oil",
                recipe["result"],
                recipe["inputs"],
                recipe["tier"],
                1,
                entry,
            )
        )
        assert entry["crafts_total"] == 1
        assert entry["crafts_by_recipe"]["sharpening_oil"] == 1
        assert entry["crafts_tier"]["2"] == 1
        assert entry["craft_discoveries"] == ["sharpening_oil"]
        assert awarded == [
            ("score", name, 22),
            ("xp", name, 22),
        ], awarded

        awarded.clear()
        asyncio.run(
            srv._record_craft_completion(
                name,
                "sharpening_oil",
                recipe["result"],
                recipe["inputs"],
                recipe["tier"],
                1,
                entry,
            )
        )
        assert entry["crafts_total"] == 2
        assert entry["crafts_by_recipe"]["sharpening_oil"] == 2
        assert entry["crafts_tier"]["2"] == 2
        assert entry["craft_discoveries"] == ["sharpening_oil"]
        assert awarded == [("score", name, 12), ("xp", name, 12)], awarded

        awarded.clear()
        asyncio.run(
            srv._record_craft_completion(
                name,
                "steel_arrows",
                "steel_arrow",
                {"iron_ore": 2},
                "3",
                1,
                entry,
            )
        )
        assert entry["crafts_total"] == 3
        assert entry["crafts_tier"]["3"] == 1
        assert entry["craft_discoveries"] == ["sharpening_oil", "steel_arrows"]
        assert awarded == [("score", name, 26), ("xp", name, 26)], awarded

        assert srv.craft_reward_for(1) == 8
        assert [srv.craft_reward_for(t) for t in range(7)] == sorted(
            srv.craft_reward_for(t) for t in range(7)
        )
        assert srv.craft_reward_for(99) == srv.craft_reward_for(srv.CRAFT_MAX_TIER)
    finally:
        srv.award_points_to_name = original_points
        srv.award_xp = original_xp
        srv.SCORES.pop(name.lower(), None)
    print("CRAFT_MASTERY_OK")


def test_material_cost_and_profitability():
    for rid, recipe in srv.RECIPES.items():
        inputs = recipe.get("inputs") or {}
        assert inputs, rid
        assert all(iid in srv.ITEM_DEFS for iid in inputs), rid
        assert all(int(qty) > 0 for qty in inputs.values()), rid
        assert srv._craft_input_value(inputs) > 0, rid
        assert 1 <= srv._craft_output_qty(recipe) <= srv.INVENTORY_CAP, rid

    awarded = []
    original_points = srv.award_points_to_name
    original_xp = srv.award_xp

    async def fake_points(name, base, reason):
        awarded.append(("score", name, base))

    async def fake_xp(name, amount, reason):
        awarded.append(("xp", name, amount))

    srv.award_points_to_name = fake_points
    srv.award_xp = fake_xp
    name = "CraftProfitTester"
    entry = fresh_entry(name)
    recipe = srv.RECIPES["arrows"]
    net = srv._craft_output_value(recipe["result"], 5) - srv._craft_input_value(
        recipe["inputs"]
    )
    try:
        for _ in range(2):
            asyncio.run(
                srv._record_craft_completion(
                    name,
                    "arrows",
                    recipe["result"],
                    recipe["inputs"],
                    recipe["tier"],
                    5,
                    entry,
                )
            )
        assert entry["craft_profitability"]["arrow"] == 2 * net
        assert entry["crafts_total"] == 2
        assert entry["crafts_by_recipe"]["arrows"] == 2
        assert entry["crafts_tier"]["1"] == 2
    finally:
        srv.award_points_to_name = original_points
        srv.award_xp = original_xp
        srv.SCORES.pop(name.lower(), None)
    print("CRAFT_PROFITABILITY_OK")


def main():
    test_material_sources()
    test_safe_gather_and_fighter_guard()
    test_craft_mastery_and_tier_rewards()
    test_material_cost_and_profitability()


if __name__ == "__main__":
    main()
