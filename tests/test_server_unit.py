"""Unit tests for server.py systems (no live server needed).

Run from the repo root:  python tests/test_server_unit.py
Covers: XP curve, level-ups (single + multi), XP buffs, instanced dungeon
generation + seal/respawn rules, market tax math, GM treasury spending
(buff/boss/reward plus announce/heal/teleport/slay/kick).
"""
import asyncio
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv

async def main():
    # --- Leveling curve ---
    assert srv.xp_to_next(1) == 100
    assert srv.xp_to_next(2) == 150
    assert srv.xp_to_next(3) == 225
    assert srv.xp_to_next(4) == 338
    assert srv.xp_to_next(5) == 506
    print("LEVEL_CURVE_OK")

    inbox = []
    async def fake_send(p, payload):
        inbox.append(payload if isinstance(payload, dict) else json.loads(payload))
    orig_send = srv.send

    class FakeWS:
        remote_address = ("127.0.0.1", 1234)
    srv.send = fake_send

    p = srv.Player(ws=FakeWS(), id=9999, name="Tester", logged_in=True)
    srv.add_member(p)
    entry = srv.get_score_entry("Tester")
    entry["level"] = 1; entry["xp"] = 0.0; entry["xp_to_next"] = srv.xp_to_next(1)

    leveled = await srv.award_xp("Tester", 100, "unit")
    assert leveled == [2], leveled
    assert entry["level"] == 2
    assert p.max_hp == 25 and p.base_attack == 4 and p.hp == 25
    assert entry["xp_to_next"] == 150
    lv_evts = [e for e in inbox if e["type"] == "level_up"]
    assert lv_evts and lv_evts[-1]["level"] == 2 and lv_evts[-1]["max_hp"] == 25
    print("LEVEL_UP_OK")

    entry["xp"] = 0.0; entry["xp_to_next"] = srv.xp_to_next(entry["level"])
    leveled = await srv.award_xp("Tester", 400, "unit")
    assert leveled == [3, 4], leveled
    assert entry["level"] == 4 and entry["xp"] == 25.0
    print("MULTI_LEVEL_OK")

    e2 = srv.get_score_entry("BuffGirl")
    base = e2["xp"]
    await srv.award_xp("BuffGirl", 10, "no")
    assert e2["xp"] - base == 10, "no buff yet"
    srv.buffs["xp"] = time.time() + 60
    base = e2["xp"]
    await srv.award_xp("BuffGirl", 10, "yes")
    assert e2["xp"] - base == 20, "2x buff should double xp"
    srv.buffs["xp"] = 0
    print("BUFF_OK")

    # --- Quantity-aware recipes ---
    crafter = srv.Player(ws=FakeWS(), id=10001, name="RecipeTester", logged_in=True)
    crafter.inventory = ["iron_ore"]
    srv.add_member(crafter)
    await srv.cmd_craft(crafter, {"recipe": "arrows"})
    assert crafter.inventory.count("arrow") == 5
    assert any(m.get("type") == "message" and "5x Arrow" in m.get("text", "") for m in inbox)
    print("RECIPE_OUTPUT_QTY_OK")

    # Crafted buff items consume normally and apply category-scoped effects.
    crafter.inventory.extend(["iron_ore", "wolf_pelt", "resin"])
    await srv.cmd_craft(crafter, {"recipe": "sharpening_oil"})
    assert "sharpening_oil" in crafter.inventory
    base_attack = crafter.attack
    await srv.cmd_use(crafter, {"item": "sharpening oil"})
    assert crafter.attack == base_attack + 2
    assert crafter.active_buffs["attack"]["remaining"] == 10
    assert "sharpening_oil" not in crafter.inventory
    print("CRAFTED_BUFF_OK")

    # --- Equipment slots: weapon / armor / offhand coexist, defense stacks ---
    knight = srv.Player(ws=FakeWS(), id=10002, name="SlotTester", logged_in=True)
    knight.inventory = ["rusty_sword", "reinforced_leather", "old_shield", "rat_tail"]
    srv.add_member(knight)
    await srv.cmd_equip(knight, {"item": "reinforced leather"})
    await srv.cmd_equip(knight, {"item": "old shield"})
    assert knight.armor == "reinforced_leather" and knight.offhand == "old_shield"
    assert knight.equipped is None
    assert srv._player_defense(knight) == 3  # leather 2 + shield 1
    no_sword_attack = knight.attack
    await srv.cmd_equip(knight, {"item": "rusty sword"})
    assert knight.equipped == "rusty_sword" and knight.armor == "reinforced_leather"
    assert knight.attack == no_sword_attack + 4  # sword adds attack; armor never does
    await srv.cmd_equip(knight, {"item": "rat tail"})
    assert inbox[-1]["type"] == "error"  # junk is not wearable
    print("EQUIP_SLOTS_OK")

    # --- Ammo families: best variant fires first with its flat bonus ---
    archer = srv.Player(ws=FakeWS(), id=10003, name="AmmoTester", logged_in=True)
    archer.inventory = ["oak_longbow", "arrow", "iron_arrow", "steel_arrow"]
    srv.add_member(archer)
    await srv.cmd_equip(archer, {"item": "oak longbow"})
    assert archer.equipped == "oak_longbow"
    srv.npcs["ammo_dummy"] = {
        "id": "ammo_dummy", "name": "Ammo Test Dummy", "room": archer.room,
        "hp": 1000, "max_hp": 1000, "attack": 0, "hostile": True, "behavior": "idle",
        "loot": [], "gold": 0, "respawn_seconds": 60,
        "alive": True, "respawn_at": None, "contributors": {},
    }
    await srv.cmd_attack(archer, {"target": "ammo test"})
    assert "steel_arrow" not in archer.inventory  # best-first consumption
    assert "iron_arrow" in archer.inventory and "arrow" in archer.inventory
    hp_after_shot = srv.npcs["ammo_dummy"]["hp"]
    assert hp_after_shot < 1000  # steel +2 bonus damage applied
    archer.inventory = ["oak_longbow"]
    await srv.cmd_attack(archer, {"target": "ammo test"})
    assert inbox[-1]["type"] == "error"  # empty quiver refuses to fire
    assert srv.npcs["ammo_dummy"]["hp"] == hp_after_shot  # no shot fired
    del srv.npcs["ammo_dummy"]
    print("AMMO_FAMILY_OK")

    # --- Carry cap: exemptions, exact enforcement, drop-to-make-room ---
    assert srv.INVENTORY_CAP == 24 and srv.AMMO_EXEMPT_COUNT == 5
    packer = srv.Player(ws=FakeWS(), id=10007, name="PackTester", logged_in=True)
    packer.room = "market"
    srv.add_member(packer)
    packer.inventory = ["rat_tail"] * 18 + ["oak_longbow", "arrow", "arrow",
                                            "arrow", "arrow", "arrow", "arrow"]
    await srv.cmd_equip(packer, {"item": "oak longbow"})
    # 18 tails + bow + 6 arrows - 1 worn bow - 5 exempt arrows = 19 units
    assert srv._inventory_units(packer) == 19
    assert not srv._pack_full(packer)
    packer.inventory.extend(["rat_tail"] * 5)
    assert srv._inventory_units(packer) == 24
    assert srv._pack_full(packer)
    packer.gold = 1000
    await srv.cmd_buy(packer, {"item": "healing herb"})  # blocked, gold untouched
    assert inbox[-1]["type"] == "error" and packer.gold == 1000
    assert "healing_herb" not in packer.inventory
    srv.room_items["market"].append("wolf_pelt")
    gold_before = packer.gold
    await srv.cmd_take(packer, {"item": "wolf pelt"})  # blocked at cap
    assert inbox[-1]["type"] == "error" and "pack is full" in inbox[-1]["text"].lower()
    assert "wolf_pelt" not in packer.inventory and packer.gold == gold_before
    assert "wolf_pelt" in srv.room_items["market"]  # ground untouched
    srv.room_items["market"].remove("wolf_pelt")
    await srv.cmd_drop(packer, {"item": "rat tail", "amount": 3})
    assert packer.inventory.count("rat_tail") == 20
    assert srv.room_items["market"].count("rat_tail") == 3
    assert not srv._pack_full(packer)
    await srv.cmd_drop(packer, {"item": "rat tail"})  # below cap: refused
    assert inbox[-1]["type"] == "error"
    assert packer.inventory.count("rat_tail") == 20  # nothing dropped
    for _ in range(3):
        srv.room_items["market"].remove("rat_tail")
    print("CARRY_CAP_OK")

    # --- Gatherer progression: harvest, cooldown, respawn, repeat ---
    gatherer = srv.Player(ws=FakeWS(), id=10004, name="GatherTester", logged_in=True)
    gatherer.room = "lumber_camp"
    srv.add_member(gatherer)
    node = srv.gather_nodes["pine_timber_node"]
    node["available"] = True
    node["respawn_at"] = None
    n0 = len(gatherer.inventory)
    await srv.cmd_gather(gatherer, {})
    assert "pine_timber" in gatherer.inventory and len(gatherer.inventory) > n0
    assert node["available"] is False and node["respawn_at"] is not None
    await srv.cmd_gather(gatherer, {})  # node on cooldown
    assert inbox[-1]["type"] == "error"
    node["available"] = True  # respawn tick
    node["respawn_at"] = None
    await srv.cmd_gather(gatherer, {"node": "pine timber"})
    assert gatherer.inventory.count("pine_timber") >= 2  # second harvest lands
    print("GATHER_OK")

    # multi-yield truncates to remaining pack space, never overflows (#232)
    from unittest import mock as _mock
    capper = srv.Player(ws=FakeWS(), id=10006, name="CapGatherer", logged_in=True)
    capper.room = "lumber_camp"
    srv.add_member(capper)
    capper.inventory = ["iron_ore"] * (srv.INVENTORY_CAP - 1)
    node["available"] = True
    node["respawn_at"] = None
    with _mock.patch.object(srv.random, "randint", return_value=3):
        await srv.cmd_gather(capper, {"node": "pine timber"})
    assert srv._inventory_units(capper) == srv.INVENTORY_CAP, srv._inventory_units(capper)
    assert capper.inventory.count("pine_timber") == 1  # truncated 3 -> 1
    srv.remove_member(capper)
    print("GATHER_CAP_OK")

    # --- Buff duration, replacement (no stacking), and expiry ---
    juicer = srv.Player(ws=FakeWS(), id=10005, name="BuffTester", logged_in=True)
    srv.add_member(juicer)
    juicer.inventory = ["sharpening_oil", "greater_sharpening_oil"]
    await srv.cmd_use(juicer, {"item": "sharpening oil"})
    assert juicer.active_buffs["attack"]["amount"] == 2
    await srv.cmd_use(juicer, {"item": "greater sharpening oil"})
    assert juicer.active_buffs["attack"]["amount"] == 4  # replaces, never stacks
    assert juicer.active_buffs["attack"]["remaining"] == 20
    for _ in range(19):
        srv._tick_player_buffs(juicer)
    assert juicer.active_buffs["attack"]["remaining"] == 1
    srv._tick_player_buffs(juicer)
    assert "attack" not in juicer.active_buffs  # expired and removed
    assert srv._player_buff_amount(juicer, "attack") == 0
    print("BUFF_STACK_OK")

    # read-only commands never consume action buffs (#237)
    for cmd in ("look", "stats", "inventory", "who", "leaderboard", "help",
                "commission_list", "party_info", "market_list", "login"):
        assert srv._command_ticks_buffs(cmd, {}) is False, cmd
    assert srv._command_ticks_buffs("quest", {"action": "list"}) is False
    for cmd in ("move", "attack", "take", "quest", "craft", "rest",
                "market_buy", "party_leave", "commission_fill"):
        assert srv._command_ticks_buffs(cmd, {"action": "accept"}) is True, cmd
    print("BUFF_TICK_GATE_OK")

    # --- Dungeon instance ---
    d = srv.Dungeon(party_id=1)
    srv.dungeons[d.id] = d
    f1 = d.floor(1)
    assert d.room_id(1) == f"d_{d.id}_f1"
    assert len(f1.guards) >= 1
    assert len(d.floor(3).guards) == 2
    hp3 = round(srv.DUNGEON_BASE_HP * (1 + srv.DUNGEON_HP_GROWTH) ** 2)
    assert all(g["max_hp"] == hp3 for g in d.floor(3).guards)
    assert srv.dungeon_for_room(f"d_{d.id}_f5") is d
    assert srv.floor_from_room(f"d_{d.id}_f5") == 5
    assert srv.floor_from_room("graveyard") is None
    print("DUNGEON_GEN_OK")

    rid1 = d.room_id(1)
    assert not d.floors[1].cleared
    for g in d.floors[1].guards:
        g["alive"] = False
    assert srv.check_dungeon_clear(rid1)
    assert d.floors[1].cleared
    assert not any(i.startswith("dungeon_blade_") for i in d.floors[1].items)
    g0 = d.floors[1].guards[0]
    srv.respawn_npc(g0)
    assert d.floors[1].cleared == False
    print("DUNGEON_SEAL_OK")

    # --- Warden boss on the last floor: fixed stats, trophy loot, no scaling ---
    w = d.floor(srv.DUNGEON_MAX_FLOOR)
    assert len(w.guards) == 1
    boss = w.guards[0]
    assert boss["name"] == "The Warden of the Deep"
    assert boss["max_hp"] == srv.WARDEN_HP and boss["attack"] == srv.WARDEN_ATK
    assert boss["loot"] == ["warden_trophy"]
    assert boss["respawn_seconds"] == srv.WARDEN_RESPAWN_SECONDS
    assert d.floor(srv.DUNGEON_MAX_FLOOR + 10) is w  # depth clamps at the cap
    assert srv.ITEM_DEFS["wardens_blade"]["damage"] == 13
    assert srv.RECIPES["wardens_blade"]["inputs"]["warden_trophy"] == 1
    print("WARDEN_OK")

    # --- Scripted Warden kill: trophy drops, floor clears + reseals, blade crafts ---
    slayer = srv.Player(ws=FakeWS(), id=10006, name="WardenSlayer", logged_in=True)
    slayer.base_attack = 25
    slayer.max_hp = 500
    slayer.hp = 500
    slayer.inventory = ["iron_plate", "old_shield"]
    srv.add_member(slayer)
    await srv.cmd_equip(slayer, {"item": "iron plate"})
    await srv.cmd_equip(slayer, {"item": "old shield"})
    assert srv._player_defense(slayer) == 4
    wroom = d.room_id(srv.DUNGEON_MAX_FLOOR)
    srv.remove_member(slayer)
    slayer.room = wroom
    srv.add_member(slayer)
    for _ in range(60):
        if not boss["alive"]:
            break
        await srv.cmd_attack(slayer, {"target": "warden"})
    assert not boss["alive"], "geared slayer must drop the Warden"
    assert "warden_trophy" in d.floor(srv.DUNGEON_MAX_FLOOR).items
    assert d.floor(srv.DUNGEON_MAX_FLOOR).cleared is True
    assert slayer.hp > 0  # armor + HP pool outlast the boss
    srv.respawn_npc(boss)
    assert boss["alive"] and d.floor(srv.DUNGEON_MAX_FLOOR).cleared is False
    boss["alive"] = False  # leave the floor clear for the loot step
    await srv.cmd_take(slayer, {"item": "warden's trophy"})
    assert "warden_trophy" in slayer.inventory
    slayer.inventory.extend(["iron_ore", "iron_ore", "serpent_scale"])
    await srv.cmd_craft(slayer, {"recipe": "wardens_blade"})
    assert "wardens_blade" in slayer.inventory
    print("WARDEN_KILL_OK")

    # --- Market tax ---
    srv.tax_treasury = 0.0; srv.tax_collected_lifetime = 0.0
    srv.market_orders.clear()
    buyer = srv.Player(ws=FakeWS(), id=20002, name="Buyer", logged_in=True)
    buyer.gold = 1000
    oid = next(srv._id_counter)
    srv.market_orders.append({"id": oid, "seller": "Seller", "item": "rusty_sword", "price": 100, "ts": 0})
    await srv.cmd_market_buy(buyer, {"id": oid})
    tax = round(100 * srv.TAX_RATE)
    assert srv.tax_treasury == tax and srv.tax_collected_lifetime == tax, (srv.tax_treasury, tax)
    assert buyer.gold == 900
    assert "rusty_sword" in buyer.inventory
    assert srv.SCORES["seller"]["tax_paid"] == tax
    assert srv.SCORES["buyer"]["trades_completed"] == 1
    assert srv.SCORES["seller"].get("trades_completed", 0) == 0
    assert srv.SCORES["seller"]["gold_bank"] == 90
    print("MARKET_TAX_OK")

    # --- Market expand accounts lifetime (#240) ---
    srv.tax_treasury = 0.0; srv.tax_collected_lifetime = 0.0
    expander = srv.Player(ws=FakeWS(), id=20003, name="Expander", logged_in=True)
    expander.gold = 1000
    slots0 = srv.get_score_entry("Expander").get("market_slots", srv.MARKET_ORDER_SLOTS_BASE)
    price0 = srv.market_slot_price(slots0)
    await srv.cmd_market_expand(expander, {})
    assert srv.tax_treasury == price0 and srv.tax_collected_lifetime == price0, (srv.tax_treasury, srv.tax_collected_lifetime)
    assert srv.get_score_entry("Expander")["market_slots"] == slots0 + 1
    print("MARKET_EXPAND_LIFETIME_OK")

    # --- GM spend from treasury ---
    p_gm = srv.Player(ws=FakeWS(), id=30001, name="GMBot", logged_in=True)
    assert srv._is_gm(p_gm)
    srv.tax_treasury = 100.0
    await srv.cmd_gm_buff(p_gm, {"type": "xp", "minutes": 1})
    assert srv.tax_treasury == 50.0
    assert srv._buff_active("xp")
    srv.buffs["xp"] = 0

    srv.tax_treasury = 100.0
    await srv.cmd_gm_boss(p_gm, {"room": "graveyard", "strength": 1})
    assert srv.tax_treasury == 0.0
    boss = [n for n in srv.npcs.values() if str(n["id"]).startswith("boss_")]
    assert boss and boss[0]["max_hp"] == 70
    print("GM_SPEND_OK")

    # --- New GM commands: announce / heal / teleport / slay / kick ---
    class KickWS(FakeWS):
        def __init__(self):
            self.closed = False
        async def close(self):
            self.closed = True

    def mkplayer(name, pid, ws=None, hp=20, max_hp=20, room="town_square"):
        pl = srv.Player(ws=ws or FakeWS(), id=pid, name=name, logged_in=True)
        pl.hp, pl.max_hp, pl.room = hp, max_hp, room
        srv.players[pid] = pl
        srv.add_member(pl)
        return pl

    def unplayer(pl):
        srv.remove_member(pl)
        srv.players.pop(pl.id, None)

    # --- Quest-giver immunity (#193): attacks rejected, givers unharmed ---
    griefer = mkplayer("Griefer", 60001, room="town_square")
    guard_hp0 = srv.npcs["guard"]["hp"]
    for _ in range(3):  # repeated attempts hold the rule
        await srv.cmd_attack(griefer, {"target": "guard"})
        assert inbox[-1]["type"] == "error" and "protection" in inbox[-1]["text"], inbox[-1]
    assert srv.npcs["guard"]["alive"] and srv.npcs["guard"]["hp"] == guard_hp0
    assert srv.npcs["guard"]["contributors"] == {}
    assert srv.get_score_entry("Griefer")["score"] == 0  # not even the -0.5 fired
    griefer2 = mkplayer("Griefer2", 60002, room="healing_spring")
    await srv.cmd_attack(griefer2, {"target": "maren"})
    assert inbox[-1]["type"] == "error" and "protection" in inbox[-1]["text"], inbox[-1]
    assert srv.npcs["healer"]["alive"]
    # Non-givers still take damage (no blanket pacifism; merchant can't retaliate).
    shopper = mkplayer("Shopper", 60003, room="market")
    merchant_hp0 = srv.npcs["merchant"]["hp"]
    await srv.cmd_attack(shopper, {"target": "merchant"})
    assert inbox[-1]["type"] == "combat", inbox[-1]
    assert srv.npcs["merchant"]["hp"] < merchant_hp0
    # Quest flow untouched: giver present, accept works after attempts.
    await srv.cmd_quest(griefer, {"action": "accept", "quest": "guard_charm"})
    assert srv.get_score_entry("Griefer").get("quest_guard_active")
    unplayer(griefer)
    unplayer(griefer2)
    unplayer(shopper)
    print("GIVER_IMMUNITY_OK")

    # --- Same-tick double-kill pays once (#195.1): B's full attack runs
    # inside A's hit-send; A must then see alive=False and skip its block.
    racer_a = mkplayer("RacerA", 60004, room="town_square")
    racer_b = mkplayer("RacerB", 60005, room="town_square")
    racer_a.gold = 0
    racer_b.gold = 0
    srv.npcs["race_dummy"] = {
        "id": "race_dummy", "name": "Race Dummy", "room": "town_square",
        "hp": 1, "max_hp": 20, "attack": 0, "hostile": True, "behavior": "idle",
        "loot": ["rat_tail"], "gold": 10, "respawn_seconds": 60,
        "alive": True, "respawn_at": None, "contributors": {},
    }
    tails_before = srv.room_items["town_square"].count("rat_tail")
    saved_send = srv.send
    interleaved = {}

    async def send_then_b(p, payload):
        await saved_send(p, payload)
        if (payload.get("type") == "combat" and "You hit" in payload.get("text", "")
                and "b_ran" not in interleaved):
            interleaved["b_ran"] = True
            await srv.cmd_attack(racer_b, {"target": "race dummy"})

    srv.send = send_then_b
    await srv.cmd_attack(racer_a, {"target": "race dummy"})
    srv.send = saved_send
    assert interleaved.get("b_ran")  # the race actually happened
    # Exactly one kill block ran: one loot drop, one split payout.
    assert srv.room_items["town_square"].count("rat_tail") == tails_before + 1
    srv.room_items["town_square"].remove("rat_tail")
    assert racer_a.gold + racer_b.gold == 10  # single payout split, not doubled
    assert racer_a.gold == 5 and racer_b.gold == 5
    del srv.npcs["race_dummy"]
    unplayer(racer_a)
    unplayer(racer_b)
    print("DOUBLE_KILL_OK")

    # --- Corpses don't retaliate (#265): whoever checks second skips the
    # kill block AND the retaliation branch (50 attack would one-shot).
    loser_a = mkplayer("LoserA", 60025, room="town_square", hp=20, max_hp=20)
    loser_b = mkplayer("LoserB", 60026, room="town_square", hp=20, max_hp=20)
    srv.npcs["corpse_dummy"] = {
        "id": "corpse_dummy", "name": "Corpse Dummy", "room": "town_square",
        "hp": 1, "max_hp": 20, "attack": 50, "hostile": True, "behavior": "idle",
        "loot": [], "gold": 0, "respawn_seconds": 60,
        "alive": True, "respawn_at": None, "contributors": {},
    }
    saved_send2 = srv.send
    interleaved2 = {}

    async def send_then_b2(p, payload):
        await saved_send2(p, payload)
        if (payload.get("type") == "combat" and "You hit" in payload.get("text", "")
                and "b_ran" not in interleaved2):
            interleaved2["b_ran"] = True
            await srv.cmd_attack(loser_b, {"target": "corpse dummy"})

    srv.send = send_then_b2
    await srv.cmd_attack(loser_a, {"target": "corpse dummy"})
    srv.send = saved_send2
    assert interleaved2.get("b_ran")
    assert not srv.npcs["corpse_dummy"]["alive"]
    # No retaliation anywhere: hp untouched AND no death-respawn masking
    # (a 50-attack retaliation that kills resets hp to full via respawn,
    # so hp alone can't prove it -- deaths must stay zero too).
    assert loser_a.hp == 20 and loser_b.hp == 20
    assert srv.get_score_entry("LoserA")["deaths"] == 0
    assert srv.get_score_entry("LoserB")["deaths"] == 0
    del srv.npcs["corpse_dummy"]
    unplayer(loser_a)
    unplayer(loser_b)
    print("CORPSE_NO_RETALIATION_OK")

    # --- Floor-1 reset farming delay (#195.2): leaving an uncleared
    # descent stamps re-entry delay; cleared/unstamped leaves don't.
    farmer = mkplayer("Farmer", 60006, room="graveyard")
    await srv._enter_dungeon(farmer)
    assert srv.dungeon_for_room(farmer.room) is not None
    await srv.cmd_party_leave(farmer, {})
    fentry = srv.get_score_entry("Farmer")
    assert fentry.get("dungeon_left_ts", 0) > 0  # live guards: stamped
    room_before = farmer.room
    await srv._enter_dungeon(farmer)
    assert inbox[-1]["type"] == "error" and "archway rejects" in inbox[-1]["text"], inbox[-1]
    assert farmer.room == room_before  # not moved
    fentry["dungeon_left_ts"] -= (srv.DUNGEON_REENTER_DELAY_SECONDS + 1)
    await srv._enter_dungeon(farmer)
    assert srv.dungeon_for_room(farmer.room) is not None
    # Leaving a party with no dungeon stamps nothing.
    host = mkplayer("HostA", 60007)
    guest = mkplayer("GuestA", 60008)
    await srv.cmd_party_invite(host, {"target": "GuestA"})
    await srv.cmd_party_accept(guest, {})
    await srv.cmd_party_leave(host, {})
    assert "dungeon_left_ts" not in srv.get_score_entry("HostA")
    # Tidy every party touched above.
    for p in list(srv.parties.values()):
        if farmer.id in p.member_ids or host.id in p.member_ids or guest.id in p.member_ids:
            srv._delete_party(p)
    unplayer(farmer)
    unplayer(host)
    unplayer(guest)
    print("REENTER_DELAY_OK")

    # --- Disconnect stamps like leave (#195.2 review): quitting to title
    # with live guards in the instance delays re-entry after relog.
    quitter = mkplayer("Quitter", 60024, room="graveyard")
    await srv._enter_dungeon(quitter)
    assert srv.dungeon_for_room(quitter.room) is not None
    await srv._leave_party_on_disconnect(quitter)
    qentry = srv.get_score_entry("Quitter")
    assert qentry.get("dungeon_left_ts", 0) > 0
    # Relog state: fresh party, town room -- the gate still holds.
    quitter.party_id = None
    quitter.room = "graveyard"
    await srv._enter_dungeon(quitter)
    assert inbox[-1]["type"] == "error" and "archway rejects" in inbox[-1]["text"], inbox[-1]
    for p in list(srv.parties.values()):
        if quitter.id in p.member_ids:
            srv._delete_party(p)
    unplayer(quitter)
    print("DISCONNECT_STAMP_OK")

    # --- Contribution-gated clear credit (#195.3): the killer earns the
    # floor + delver readiness; the idle witness present at the clear
    # earns nothing and its baseline never advances.
    killer = mkplayer("Killer", 60009, hp=200, max_hp=200)
    leecher = mkplayer("Leecher", 60010, hp=200, max_hp=200)
    await srv.cmd_party_invite(killer, {"target": "Leecher"})
    await srv.cmd_party_accept(leecher, {})
    await srv.cmd_quest(killer, {"action": "accept", "quest": "delver"})
    await srv.cmd_quest(leecher, {"action": "accept", "quest": "delver"})
    await srv.cmd_move(killer, {"dir": "south"})
    await srv.cmd_move(leecher, {"dir": "south"})
    await srv._enter_dungeon(killer)  # leecher at the entrance comes along
    assert srv.dungeon_for_room(leecher.room) is not None
    assert killer.room == leecher.room
    for _ in range(60):
        guard = next((g for g in srv.npcs_in_room(killer.room) if g["alive"]), None)
        if guard is None:
            break
        await srv.cmd_attack(killer, {"target": guard["id"]})
    assert not any(g["alive"] for g in srv.npcs_in_room(killer.room))
    kentry = srv.get_score_entry("Killer")
    lentry = srv.get_score_entry("Leecher")
    assert kentry.get("dungeon_floors_cleared", 0) == 1, kentry.get("dungeon_floors_cleared")
    assert lentry.get("dungeon_floors_cleared", 0) == 0
    assert srv.quest_delver_ready(kentry) and not srv.quest_delver_ready(lentry)
    for p in list(srv.parties.values()):
        if killer.id in p.member_ids or leecher.id in p.member_ids:
            srv._delete_party(p)
    unplayer(killer)
    unplayer(leecher)
    print("CLEAR_CREDIT_OK")

    # --- Level-up heals gained max HP only, never to full (#195.4) ---
    leveler = mkplayer("Leveler", 60011, hp=5, max_hp=20)
    lentry = srv.get_score_entry("Leveler")
    lentry["level"] = 1
    lentry["xp"] = 0.0
    lentry["xp_to_next"] = srv.xp_to_next(1)
    leveled = await srv.award_xp("Leveler", 100000, "test surge")
    assert len(leveled) >= 2, leveled  # multi-level surge
    assert leveler.max_hp == 20 + srv.LEVEL_HP_PER_LEVEL * len(leveled)
    assert leveler.hp == 5 + srv.LEVEL_HP_PER_LEVEL * len(leveled)
    assert leveler.hp < leveler.max_hp  # the old full-heal is gone
    unplayer(leveler)
    print("LEVEL_HEAL_OK")

    # --- Sheltered wealth counts toward the death penalty (#195.5) ---
    shelter = mkplayer("Shelter", 60012)
    shelter.gold = 1000
    await srv.cmd_commission_post(shelter, {"target": "rat", "required_kills": 1,
                                            "reward_gold": 100, "reward_xp": 0})
    shelter.gold = 0  # everything sheltered or spent: carried is empty
    sentry = srv.get_score_entry("Shelter")
    sentry["score"] = 1000.0
    await srv.respawn_player(shelter)
    expect = srv.DEATH_PENALTY + srv.DEATH_SCORE_PER_GOLD_LOST * 100
    assert sentry["score"] == 1000.0 - expect, (sentry["score"], expect)
    assert any(c["status"] == "open" and c.get("escrow", 0) == 100
               for c in srv._commissions.values())  # escrow itself untouched
    broke = mkplayer("Broke", 60013)
    broke.gold = 0
    bentry = srv.get_score_entry("Broke")
    bentry["score"] = 1000.0
    await srv.respawn_player(broke)
    assert bentry["score"] == 1000.0 - srv.DEATH_PENALTY
    for c in list(srv._commissions.values()):
        if c["poster"] == "Shelter" and c["status"] == "open":
            await srv.cmd_commission_cancel(shelter, {"commission_id": c["id"]})
    unplayer(shelter)
    unplayer(broke)
    print("SHELTER_PENALTY_OK")

    # --- XP grind decays like score (#195.6): repeat loops earn strictly
    # less per iteration as variety collapses and score climbs.
    grinder = mkplayer("Grinder", 60014)
    gentry = srv.get_score_entry("Grinder")
    gentry["history"] = []
    gains = []
    for _ in range(30):
        gentry["history"].append(("farm", "loop"))
        x0 = gentry["xp"]
        await srv.award_points(grinder, 10, "farm loop")
        await srv.award_xp("Grinder", 100, "farm loop")
        gains.append(gentry["xp"] - x0)
    assert gains[-1] < gains[0]
    assert sum(gains) < 30 * 100
    assert gentry["level"] < 1 + 30
    unplayer(grinder)
    print("XP_GRIND_OK")

    # --- Invite overwrite notifies the old leader (#195.7a) ---
    inv_a = mkplayer("InvA", 60015)
    inv_b = mkplayer("InvB", 60016)
    inv_c = mkplayer("InvC", 60017)
    await srv.cmd_party_invite(inv_a, {"target": "InvC"})
    inbox.clear()
    await srv.cmd_party_invite(inv_b, {"target": "InvC"})
    assert any(m.get("type") == "message" and "replaced" in m.get("text", "")
               for m in inbox), inbox[-3:]
    for p in list(srv.parties.values()):
        if inv_a.id in p.member_ids or inv_b.id in p.member_ids or inv_c.id in p.member_ids:
            srv._delete_party(p)
    unplayer(inv_a)
    unplayer(inv_b)
    unplayer(inv_c)
    print("INVITE_OVERWRITE_OK")

    # --- Accept relocates out of the old dungeon (#195.7b) ---
    diver = mkplayer("Diver", 60018, room="graveyard")
    await srv._enter_dungeon(diver)
    assert srv.dungeon_for_room(diver.room) is not None
    import time as _time
    shore = mkplayer("Shore", 60019)
    shore_party = srv._auto_create_party(shore)
    srv._pending_party_invites[diver.id] = {"party": shore_party, "ts": _time.time()}
    await srv.cmd_party_accept(diver, {})
    assert diver.room == srv.DUNGEON_ENTRANCE_ROOM  # not stranded in d_X_fY
    assert srv.dungeon_for_room(diver.room) is None
    for p in list(srv.parties.values()):
        if diver.id in p.member_ids or shore.id in p.member_ids:
            srv._delete_party(p)
    unplayer(diver)
    unplayer(shore)
    print("ACCEPT_RELOCATE_OK")

    # --- Move-gap gold forfeits to the present split (#195.7c) ---
    striker = mkplayer("Striker", 60020, room="town_square")
    drifter = mkplayer("Drifter", 60021, room="town_square")
    striker.gold = 0
    drifter.gold = 0
    srv.npcs["share_dummy"] = {
        "id": "share_dummy", "name": "Share Dummy", "room": "town_square",
        "hp": 20, "max_hp": 20, "attack": 0, "hostile": True, "behavior": "idle",
        "loot": [], "gold": 10, "respawn_seconds": 60,
        "alive": True, "respawn_at": None, "contributors": {},
    }
    await srv.cmd_attack(drifter, {"target": "share dummy"})  # contributor...
    await srv.cmd_move(drifter, {"dir": "east"})  # ...then leaves before the kill
    for _ in range(60):
        dummy = srv.npcs.get("share_dummy")
        if dummy is None or not dummy["alive"]:
            break
        await srv.cmd_attack(striker, {"target": "share dummy"})
    assert not srv.npcs["share_dummy"]["alive"]
    assert striker.gold == 10 and drifter.gold == 0  # faucet-neutral forfeit
    del srv.npcs["share_dummy"]
    unplayer(striker)
    unplayer(drifter)
    print("GOLD_SHARE_OK")

    # --- Commercial tax rounding + 1g payouts (#195.8) ---
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))
    from ml_env import market_tax as _env_tax
    assert _env_tax(1) == 0
    assert _env_tax(2) == 1
    assert _env_tax(10) == 1
    assert _env_tax(25) == 3  # half-up: banker's round(2.5) would say 2
    assert _env_tax(100) == 10
    t_saved = (srv.tax_treasury, srv.tax_collected_lifetime)
    srv.tax_treasury = 0.0
    srv.tax_collected_lifetime = 0.0
    penny = mkplayer("Penny", 60022, room="market")
    penny.inventory.append("healing_herb")
    await srv.cmd_market_post(penny, {"item": "Healing Herb", "price": 1})
    poid = max(o["id"] for o in srv.market_orders if o["seller"] == "Penny")
    dime = mkplayer("Dime", 60023, room="market")
    dime.gold = 200
    penny_gold0 = penny.gold
    await srv.cmd_market_buy(dime, {"id": poid})
    assert penny.gold == penny_gold0 + 1  # full 1g payout, 0 tax (was 0)
    assert srv.tax_treasury == 0.0 and srv.tax_collected_lifetime == 0.0
    assert dime.gold == 199  # conservation: 1 + 0 == price
    srv.tax_treasury, srv.tax_collected_lifetime = t_saved
    unplayer(penny)
    unplayer(dime)
    print("TAX_ROUND_OK")

    # announce: empty text rejected, real text charged + broadcast
    srv.tax_treasury = 100.0
    n0 = len(inbox)
    await srv.cmd_gm_announce(p_gm, {"text": "   "})
    assert srv.tax_treasury == 100.0
    assert inbox[-1]["type"] == "error"
    listener = mkplayer("Listener", 40001)
    await srv.cmd_gm_announce(p_gm, {"text": "hello world"})
    assert srv.tax_treasury == 100.0 - srv.GM_ANNOUNCE_COST
    new = inbox[n0:]
    assert any(m.get("type") == "message" and "hello world" in m.get("text", "") for m in new), new
    assert any(m.get("type") == "message" and "announcement sent" in m.get("text", "") for m in new), new
    unplayer(listener)
    print("GM_ANNOUNCE_OK")

    # heal: full-HP rejected free, wounded healed at 2/missing HP
    srv.tax_treasury = 100.0
    assert srv.GM_HEAL_COST_PER_HP == 2
    patient = mkplayer("Patient", 40002, hp=20, max_hp=20)
    await srv.cmd_gm_heal(p_gm, {"player": "Patient"})
    assert srv.tax_treasury == 100.0
    patient.hp = 10
    await srv.cmd_gm_heal(p_gm, {"player": "Patient"})
    assert patient.hp == 20
    assert srv.tax_treasury == 100.0 - 10 * srv.GM_HEAL_COST_PER_HP
    await srv.cmd_gm_heal(p_gm, {"player": "Nobody"})
    assert inbox[-1]["type"] == "error"
    unplayer(patient)
    print("GM_HEAL_OK")

    # teleport: bad room / same room rejected free, real move charged
    srv.tax_treasury = 100.0
    assert srv.GM_TELEPORT_COST == 50
    traveller = mkplayer("Traveller", 40003, room="town_square")
    await srv.cmd_gm_teleport(p_gm, {"player": "Traveller", "room": "nope"})
    assert srv.tax_treasury == 100.0
    await srv.cmd_gm_teleport(p_gm, {"player": "Traveller", "room": "town_square"})
    assert srv.tax_treasury == 100.0
    await srv.cmd_gm_teleport(p_gm, {"player": "Traveller", "room": "market"})
    assert traveller.room == "market"
    assert srv.tax_treasury == 100.0 - srv.GM_TELEPORT_COST
    unplayer(traveller)
    print("GM_TELEPORT_OK")

    # slay: unknown/ambiguous/dead rejected, real kill charged + looted
    srv.tax_treasury = 1000.0
    dummy_id = "unit_dummy"
    srv.npcs[dummy_id] = {
        "id": dummy_id, "name": "Unit Test Dummy", "room": "town_square",
        "hp": 10, "max_hp": 10, "attack": 1, "hostile": True, "behavior": "idle",
        "loot": ["healing_herb"], "gold": 0, "respawn_seconds": 60,
        "alive": True, "respawn_at": None, "contributors": {},
    }
    await srv.cmd_gm_slay(p_gm, {"target": "zzz_no_such_npc"})
    assert inbox[-1]["type"] == "error"
    await srv.cmd_gm_slay(p_gm, {"target": "unit test"})
    assert not srv.npcs[dummy_id]["alive"]
    assert srv.npcs[dummy_id]["respawn_at"] is not None
    assert "healing_herb" in srv.room_items["town_square"]
    assert srv.tax_treasury == 1000.0 - max(srv.GM_SLAY_MIN_COST, 10 * srv.GM_SLAY_COST_PER_HP)
    srv.room_items["town_square"].remove("healing_herb")
    await srv.cmd_gm_slay(p_gm, {"target": "unit test"})
    assert inbox[-1]["type"] == "error"  # already dead
    del srv.npcs[dummy_id]
    print("GM_SLAY_OK")

    # kick: offline rejected, online closed + confirmed, free
    srv.tax_treasury = 100.0
    await srv.cmd_gm_kick(p_gm, {"player": "GhostNobody"})
    assert inbox[-1]["type"] == "error"
    kickws = KickWS()
    kickme = mkplayer("KickMe", 40004, ws=kickws)
    await srv.cmd_gm_kick(p_gm, {"player": "KickMe", "reason": "unit test"})
    assert kickws.closed
    assert srv.tax_treasury == 100.0
    assert any("kicked" in m.get("text", "") for m in inbox if m.get("type") == "message")
    unplayer(kickme)
    print("GM_KICK_OK")

    # commissions: fill needs verified kills since posting, no self-dealing
    poster = mkplayer("Poster", 40005)
    poster.gold = 100
    before_cids = set(srv._commissions)
    await srv.cmd_commission_post(poster, {"target": "rat", "required_kills": 1, "reward_gold": 10, "reward_xp": 5})
    cid = max(set(srv._commissions) - before_cids)
    assert poster.gold == 90  # escrow locked
    filler = mkplayer("Filler", 40006, room="old_shop")
    await srv.cmd_commission_fill(filler, {"commission_id": cid})
    assert inbox[-1]["type"] == "error"  # no verified kills yet
    await srv.cmd_commission_fill(poster, {"commission_id": cid})
    assert inbox[-1]["type"] == "error"  # own bounty
    for _ in range(40):
        if not srv.npcs["rat"]["alive"]:
            break
        filler.hp = filler.max_hp
        await srv.cmd_attack(filler, {"target": "rat"})
    assert not srv.npcs["rat"]["alive"]
    assert srv.verified_npc_kills("Filler", "rat", 0) >= 1
    g0 = filler.gold
    s0 = srv.get_score_entry("Filler")["score"]
    await srv.cmd_commission_fill(filler, {"commission_id": cid})
    assert srv._commissions[cid]["status"] == "completed"
    assert filler.gold - g0 == 10  # full escrow paid out
    assert srv.get_score_entry("Filler")["score"] > s0
    unplayer(poster)
    unplayer(filler)
    print("COMMISSION_VERIFY_OK")

    # --- Commission cancel: poster cancels, half refund ---
    canposter = mkplayer("CanPoster", 40020)
    canposter.gold = 200
    before_cids = set(srv._commissions)
    await srv.cmd_commission_post(canposter, {"target": "wolf", "required_kills": 1, "reward_gold": 100, "reward_xp": 0})
    ccid = max(set(srv._commissions) - before_cids)
    assert canposter.gold == 100  # escrowed 100
    await srv.cmd_commission_cancel(canposter, {"commission_id": ccid})
    assert srv._commissions[ccid]["status"] == "cancelled"
    assert canposter.gold == 150  # half refund: 50
    # Non-poster cannot cancel
    other = mkplayer("Other", 40021)
    await srv.cmd_commission_cancel(other, {"commission_id": ccid})
    assert inbox[-1]["type"] == "error"
    unplayer(canposter)
    unplayer(other)
    print("COMMISSION_CANCEL_OK")

    # --- Crafted equipment provides stat bonus ---
    gearhead = mkplayer("Gearhead", 40022)
    gearhead.inventory = ["wolf_pelt", "rat_tail", "rat_tail"]
    base_atk = gearhead.attack
    base_def = srv._player_defense(gearhead)
    await srv.cmd_craft(gearhead, {"recipe": "reinforced_leather"})
    assert "reinforced_leather" in gearhead.inventory
    await srv.cmd_equip(gearhead, {"item": "reinforced leather"})
    assert srv._player_defense(gearhead) == base_def + srv.ITEM_DEFS["reinforced_leather"]["defense"]
    assert gearhead.armor == "reinforced_leather"
    unplayer(gearhead)
    print("CRAFTED_GEAR_OK")

    # death penalty scales with gold removed (flat floor when broke)
    broke = mkplayer("Broke", 40007)
    srv.get_score_entry("Broke")["score"] = 100.0
    broke.gold = 0
    await srv.respawn_player(broke)
    assert srv.get_score_entry("Broke")["score"] == 95.0
    assert broke.gold == 0
    rich = mkplayer("Rich", 40008, room="market")
    srv.get_score_entry("Rich")["score"] = 1000.0
    rich.gold = 1000
    await srv.respawn_player(rich)
    # 400 dropped as floor pile + 100 vanished -> penalty 5 + 0.1*500 = 55
    assert srv.get_score_entry("Rich")["score"] == 945.0
    assert rich.gold == 500
    assert srv.room_gold.get("market", 0) >= 400
    assert rich.room == "town_square"
    unplayer(broke)
    unplayer(rich)
    print("DEATH_SCALE_OK")

    # --- XP loss on death (#334): percentual of total XP, de-levels ---
    old_pct = srv.XP_LOSS_PCT
    srv.XP_LOSS_PCT = 10.0
    loser = mkplayer("XpLoser", 40009, room="market")
    lentry = srv.get_score_entry("XpLoser")
    lentry["level"] = 5
    lentry["xp"] = 0.0
    lentry["xp_to_next"] = srv.xp_to_next(5)
    lentry["score"] = 0.0
    before_total = srv.total_xp(lentry)
    await srv.respawn_player(loser)
    assert lentry["level"] < 5, lentry["level"]
    assert abs(srv.total_xp(lentry) - before_total * 0.9) < 1.0
    assert loser.max_hp == 20 + srv.LEVEL_HP_PER_LEVEL * (lentry["level"] - 1)
    assert loser.base_attack == 3 + srv.LEVEL_ATK_PER_LEVEL * (lentry["level"] - 1)
    unplayer(loser)
    srv.XP_LOSS_PCT = old_pct
    print("XP_LOSS_OK")

    # --- Item drops are zone-gated (#334): risk rooms scatter the pack, ---
    # --- safe lands keep the gold-only rule.                              ---
    srv.ROOMS["market"]["risk"] = True
    risker = mkplayer("RiskTaker", 40010, room="market")
    risker.gold = 0
    risker.inventory = ["treant_bark", "iron_ore", "rat_tail", "rusty_sword"]
    risker.equipped = "rusty_sword"
    rentry = srv.get_score_entry("RiskTaker")
    rentry["score"] = 100.0
    rentry["level"] = 1
    rentry["xp"] = 0.0
    rentry["xp_to_next"] = srv.xp_to_next(1)
    await srv.respawn_player(risker)
    assert "treant_bark" not in risker.inventory
    assert "iron_ore" not in risker.inventory
    assert "rat_tail" not in risker.inventory
    assert "rusty_sword" in risker.inventory  # equipped slot protected first
    ground = srv.room_items["market"]
    assert ground.count("treant_bark") == 1 and ground.count("iron_ore") == 1
    # floor-value penalty: treant_bark 10 + iron_ore 8 + rat_tail 1 = 19
    expect_pen = srv.DEATH_PENALTY + srv.DEATH_SCORE_PER_GOLD_LOST * 19
    assert abs(rentry["score"] - (100.0 - expect_pen)) < 1e-9, (rentry["score"], expect_pen)
    del srv.ROOMS["market"]["risk"]
    for iid in ["treant_bark", "iron_ore", "rat_tail"]:
        if iid in srv.room_items["market"]:
            srv.room_items["market"].remove(iid)
    unplayer(risker)

    safer = mkplayer("SafeKeeper", 40011, room="town_square")
    safer.gold = 0
    safer.inventory = ["treant_bark", "iron_ore"]
    sentry = srv.get_score_entry("SafeKeeper")
    sentry["score"] = 100.0
    sentry["level"] = 1
    sentry["xp"] = 0.0
    sentry["xp_to_next"] = srv.xp_to_next(1)
    await srv.respawn_player(safer)
    assert "treant_bark" in safer.inventory and "iron_ore" in safer.inventory
    assert sentry["score"] == 100.0 - srv.DEATH_PENALTY
    unplayer(safer)
    print("ITEM_DROP_ZONE_OK")

    # --- Quests never un-accept on death (#334) ---
    quester = mkplayer("DeadQuester", 40012, room="town_square")
    qentry = srv.get_score_entry("DeadQuester")
    qentry["quest_guard_active"] = True
    qentry["quest_delver_active"] = True
    qentry["level"] = 1
    qentry["xp"] = 0.0
    qentry["xp_to_next"] = srv.xp_to_next(1)
    qentry["score"] = 0.0
    await srv.respawn_player(quester)
    assert srv.get_score_entry("DeadQuester").get("quest_guard_active") is True
    assert srv.get_score_entry("DeadQuester").get("quest_delver_active") is True
    unplayer(quester)
    print("QUEST_SURVIVES_DEATH_OK")

    # --- Death telegraph preview (#334): worst-case accounting in stats ---
    srv.ROOMS["market"]["risk"] = True
    previewer = mkplayer("Preview", 40013, room="market")
    previewer.gold = 100
    previewer.inventory = ["treant_bark", "rusty_sword"]
    pentry = srv.get_score_entry("Preview")
    pentry["level"] = 3
    pentry["xp"] = 0.0
    pentry["xp_to_next"] = srv.xp_to_next(3)
    pv = srv.death_preview(previewer)
    assert pv["risk_zone"] is True
    assert pv["gold_dropped"] == 40 and pv["gold_lost"] == 10
    assert "Treant Bark" in pv["items_at_risk"]
    assert pv["xp_loss_pct"] == srv.XP_LOSS_PCT
    assert pv["level_after"] < 3
    del srv.ROOMS["market"]["risk"]
    unplayer(previewer)
    print("DEATH_PREVIEW_OK")

    # --- death_preview defensive lookup (#353): an inventory id missing ---
    # --- from ITEM_DEFS must not crash the per-tick stats view.          ---
    srv.ROOMS["market"]["risk"] = True
    ghost = mkplayer("GhostItem", 40014, room="market")
    ghost.gold = 0
    ghost.inventory = ["bogus_item_id", "rat_tail"]
    gview = srv.stats_view(ghost)
    assert gview["death_preview"]["items_at_risk"] == ["bogus_item_id", "Rat Tail"], \
        gview["death_preview"]["items_at_risk"]
    del srv.ROOMS["market"]["risk"]
    unplayer(ghost)
    print("DEATH_PREVIEW_DEFENSIVE_OK")

    srv.send = orig_send

    # --- Crafting recipe profitability: low-tier recipes should not destroy ---
    # --- value; high-tier pinnacles (T4+) are prestige sinks by design.    ---
    for rid, recipe in srv.RECIPES.items():
        tier = recipe.get("tier", 0)
        cat = recipe.get("category", "")
        if cat in ("quest", "ammo") or tier >= 4:
            continue
        input_value = sum(srv.ITEM_DEFS.get(iid, {}).get("value", 0) * qty
                         for iid, qty in recipe["inputs"].items())
        output_qty = max(1, int(recipe.get("output_qty", 1)))
        output_value = srv.ITEM_DEFS.get(recipe["result"], {}).get("value", 0) * output_qty
        assert output_value >= input_value, (
            f"Recipe {rid} (T{tier}) destroys value: inputs={input_value}, output={output_value}")
    print("CRAFT_PROFITABILITY_OK")

    # --- Quest system: list, accept, turn_in, giver check, repeat ---
    srv.send = fake_send  # re-patch after GM tests restored orig_send
    inbox.clear()
    qtester = srv.Player(ws=FakeWS(), id=10010, name="Quester", logged_in=True)
    qtester.room = "town_square"
    srv.add_member(qtester)
    qentry = srv.get_score_entry("Quester")

    # List shows all quests
    await srv.cmd_quest(qtester, {"action": "list"})
    quest_list_msg = [m for m in inbox if "guard_charm" in m.get("text", "") and "delver" in m.get("text", "")]
    assert quest_list_msg, "quest list should show guard_charm and delver"

    # Accept requires giver present
    qtester.room = "lumber_camp"  # no guard here
    await srv.cmd_quest(qtester, {"action": "accept", "quest": "guard_charm"})
    err_msgs = [m for m in inbox if m.get("type") == "error"]
    assert err_msgs and "isn't here" in err_msgs[-1]["text"]
    assert not qentry.get("quest_guard_active")

    # Accept with giver present
    qtester.room = "town_square"
    await srv.cmd_quest(qtester, {"action": "accept", "quest": "guard_charm"})
    assert qentry.get("quest_guard_active")
    ok_msgs = [m for m in inbox if m.get("type") == "message" and "Ah" in m.get("text", "")]
    assert ok_msgs

    # Cannot accept same quest twice
    await srv.cmd_quest(qtester, {"action": "accept", "quest": "guard_charm"})
    dup_msgs = [m for m in inbox if m.get("type") == "message" and "already have" in m.get("text", "")]
    assert dup_msgs

    # Turn_in without conditions fails
    await srv.cmd_quest(qtester, {"action": "turn_in", "quest": "guard_charm"})
    fail_msgs = [m for m in inbox if m.get("type") == "message" and "haven't crafted" in m.get("text", "")]
    assert fail_msgs

    # Simulate charm crafted, turn_in succeeds
    qentry["guard_charm_crafted"] = True
    qtester.inventory.append(srv.QUEST_CHARM_RESULT)
    old_xp = qentry["xp"]
    old_gold = qtester.gold
    await srv.cmd_quest(qtester, {"action": "turn_in", "quest": "guard_charm"})
    assert not qentry.get("quest_guard_active")
    assert qentry["quest_guard_completions"] == 1
    assert qentry["xp"] > old_xp
    assert qtester.gold > old_gold
    assert srv.QUEST_CHARM_RESULT not in qtester.inventory

    # Can repeat: accept again
    await srv.cmd_quest(qtester, {"action": "accept", "quest": "guard_charm"})
    assert qentry.get("quest_guard_active")
    qentry["guard_charm_crafted"] = False
    print("QUEST_OK")

    # wash trade (#188): explicit-id self-buy rejected, listing survives,
    # legit buy pays score/XP exactly once per side
    srv.buffs["xp"] = 0
    srv.buffs["gold"] = 0
    herb_name = srv.ITEM_DEFS["healing_herb"]["name"]
    wash = mkplayer("Wash", 50001, room="market")
    wash.gold = 200
    wash.inventory.append("healing_herb")
    wash_xp0 = srv.get_score_entry("Wash")["xp"]
    await srv.cmd_market_post(wash, {"item": herb_name, "price": 10})
    oid = max(o["id"] for o in srv.market_orders if o["seller"] == "Wash")
    await srv.cmd_market_buy(wash, {"id": oid})
    assert inbox[-1]["type"] == "error" and "own listing" in inbox[-1]["text"], inbox[-1]
    assert any(o["id"] == oid for o in srv.market_orders)  # not consumed
    assert srv.get_score_entry("Wash")["xp"] == wash_xp0  # nothing minted
    patsy = mkplayer("Patsy", 50002, room="market")
    patsy.gold = 200
    patsy_xp0 = srv.get_score_entry("Patsy")["xp"]
    seller_xp0 = srv.get_score_entry("Wash")["xp"]
    await srv.cmd_market_buy(patsy, {"id": oid})
    assert not any(o["id"] == oid for o in srv.market_orders)
    # XP diminish (#195.6) scales the fixed 3 XP by each side's curve, so
    # assert one payment each (no double mint), not exact equality.
    assert srv.get_score_entry("Patsy")["xp"] > patsy_xp0
    assert srv.get_score_entry("Wash")["xp"] > seller_xp0
    unplayer(wash)
    unplayer(patsy)
    print("WASH_TRADE_OK")

    # commission XP cap + kill consumption (#189)
    rich = mkplayer("RichPoster", 50003)
    rich.gold = 1000000
    await srv.cmd_commission_post(rich, {"target": "rat", "required_kills": 1,
                                         "reward_gold": 10, "reward_xp": 999999})
    assert inbox[-1]["type"] == "error" and "capped" in inbox[-1]["text"].lower(), inbox[-1]
    assert rich.gold == 1000000  # no escrow taken on rejection
    before = set(srv._commissions)
    await srv.cmd_commission_post(rich, {"target": "rat", "required_kills": 1,
                                         "reward_gold": 10, "reward_xp": 500})
    cid_a = max(set(srv._commissions) - before)
    assert srv._commissions[cid_a]["reward_xp"] == 500
    before = set(srv._commissions)
    await srv.cmd_commission_post(rich, {"target": "rat", "required_kills": 1,
                                         "reward_gold": 10, "reward_xp": 5})
    cid_b = max(set(srv._commissions) - before)
    killer = mkplayer("Killer", 50004, room="old_shop")
    # kill recorded AFTER both postings, so both bounties can see it --
    # the first fill must consume it, leaving the second one empty
    srv.record_npc_kill("Killer", "Giant Rat")
    assert srv.verified_npc_kills("Killer", "rat", 0) >= 1
    await srv.cmd_commission_fill(killer, {"commission_id": cid_a})
    assert srv._commissions[cid_a]["status"] == "completed"
    # the single verified kill was consumed: second bounty can't reuse it
    await srv.cmd_commission_fill(killer, {"commission_id": cid_b})
    assert inbox[-1]["type"] == "error" and "verified" in inbox[-1]["text"], inbox[-1]
    assert srv._commissions[cid_b]["status"] == "open"
    unplayer(rich)
    unplayer(killer)
    print("COMMISSION_CAP_OK")

    # impossible-bounty escrow lock: kill counts no session could reach are
    # rejected before any escrow is taken (open bounties are never pruned)
    cap_poster = mkplayer("CapPoster", 50006)
    cap_poster.gold = 1000000
    n_comms = len(srv._commissions)
    await srv.cmd_commission_post(cap_poster, {"target": "rat", "required_kills": srv.COMMISSION_MAX_KILLS + 1,
                                               "reward_gold": 10, "reward_xp": 1})
    assert inbox[-1]["type"] == "error" and "capped" in inbox[-1]["text"].lower(), inbox[-1]
    assert len(srv._commissions) == n_comms
    assert cap_poster.gold == 1000000
    # at-cap bounty posts fine (0g so the later cancel is treasury-neutral)
    await srv.cmd_commission_post(cap_poster, {"target": "rat", "required_kills": srv.COMMISSION_MAX_KILLS,
                                               "reward_gold": 0, "reward_xp": 0})
    cid_cap = max(srv._commissions)
    await srv.cmd_commission_cancel(cap_poster, {"commission_id": cid_cap})
    assert srv._commissions[cid_cap]["status"] == "cancelled"
    unplayer(cap_poster)
    print("COMMISSION_KILL_CAP_OK")

    # case-variant self-deal: score entries are shared across case variants,
    # so "CaseAlice"/"casealice" are one economic actor everywhere
    calice = mkplayer("CaseAlice", 50007)
    calice.gold = 1000
    await srv.cmd_commission_post(calice, {"target": "rat", "required_kills": 1,
                                           "reward_gold": 100, "reward_xp": 10})
    cid_case = max(srv._commissions)
    calice_lower = mkplayer("casealice", 50008)
    srv.record_npc_kill("casealice", "Giant Rat")
    await srv.cmd_commission_fill(calice_lower, {"commission_id": cid_case})
    assert inbox[-1]["type"] == "error" and "own commission" in inbox[-1]["text"], inbox[-1]
    assert srv._commissions[cid_case]["status"] == "open"
    # ...but the same variant MAY cancel (it is the poster)
    inbox.clear()
    await srv.cmd_commission_cancel(calice_lower, {"commission_id": cid_case})
    assert srv._commissions[cid_case]["status"] == "cancelled", inbox[-1]
    # market self-deal, same rule
    calice.inventory.append("healing_herb")
    await srv.cmd_market_post(calice, {"item": herb_name, "price": 10})
    coid = max(o["id"] for o in srv.market_orders if o["seller"] == "CaseAlice")
    await srv.cmd_market_buy(calice_lower, {"id": coid})
    assert inbox[-1]["type"] == "error" and "own listing" in inbox[-1]["text"], inbox[-1]
    assert any(o["id"] == coid for o in srv.market_orders)
    srv.market_orders[:] = [o for o in srv.market_orders if o["id"] != coid]
    unplayer(calice)
    unplayer(calice_lower)
    print("CASE_VARIANT_OK")

    # zero bounty pays zero (floors must not mint from an empty bounty);
    # collusion remainder + cancel forfeit land in the treasury instead of
    # sitting on dead records forever
    t_saved = (srv.tax_treasury, srv.tax_collected_lifetime)
    srv.tax_treasury = 0.0
    srv.tax_collected_lifetime = 0.0
    zposter = mkplayer("ZeroPoster", 50009)
    zposter.gold = 1000
    zentry = srv.get_score_entry("ZeroPoster")
    zscore0, zxp0 = zentry["score"], zentry["xp"]
    await srv.cmd_commission_post(zposter, {"target": "rat", "required_kills": 1,
                                            "reward_gold": 0, "reward_xp": 0})
    cid_z = max(srv._commissions)
    zfill = mkplayer("ZeroFiller", 50010)
    zfill_gold0 = zfill.gold
    zfentry = srv.get_score_entry("ZeroFiller")
    zfscore0 = zfentry["score"]
    srv.record_npc_kill("ZeroFiller", "Giant Rat")
    inbox.clear()
    await srv.cmd_commission_fill(zfill, {"commission_id": cid_z})
    assert srv._commissions[cid_z]["status"] == "completed"
    assert zfill.gold == zfill_gold0  # no 1g mint
    assert zfentry["score"] == zfscore0
    assert "+0g, +0xp" in inbox[0]["text"], inbox[0]
    filled_note = next(m["text"] for m in inbox if "was filled by" in m.get("text", ""))
    assert "+0 score, +0xp" in filled_note, filled_note
    assert zentry["score"] == zscore0 and zentry["xp"] == zxp0  # no poster mint
    assert srv.tax_treasury == 0.0
    # repeat-pair collusion: first fill full (no remainder), second fill
    # halved with the other half sunk to the treasury
    tposter = mkplayer("TreasPoster", 50011)
    tposter.gold = 100000
    tfill = mkplayer("TreasFiller", 50012)
    tfill_gold0 = tfill.gold
    for round_ in (1, 2):
        await srv.cmd_commission_post(tposter, {"target": "rat", "required_kills": 1,
                                                "reward_gold": 100, "reward_xp": 0})
        cid_t = max(srv._commissions)
        srv.record_npc_kill("TreasFiller", "Giant Rat")
        await srv.cmd_commission_fill(tfill, {"commission_id": cid_t})
        assert srv._commissions[cid_t]["status"] == "completed"
        assert srv._commissions[cid_t]["escrow"] == 0
    assert tfill.gold == tfill_gold0 + 150  # 100 + 50 (collab penalty)
    assert srv.tax_treasury == 50.0 and srv.tax_collected_lifetime == 50.0
    # cancel: half refunded live, half forfeited to the treasury
    await srv.cmd_commission_post(tposter, {"target": "rat", "required_kills": 1,
                                            "reward_gold": 100, "reward_xp": 0})
    cid_c = max(srv._commissions)
    gold_before_cancel = tposter.gold
    inbox.clear()
    await srv.cmd_commission_cancel(tposter, {"commission_id": cid_c})
    assert tposter.gold == gold_before_cancel + 50
    assert srv.tax_treasury == 100.0 and srv.tax_collected_lifetime == 100.0
    assert srv._commissions[cid_c]["escrow"] == 0
    assert "forfeited to the treasury" in inbox[-2]["text"], inbox[-2]
    unplayer(zposter)
    unplayer(zfill)
    unplayer(tposter)
    unplayer(tfill)
    srv.tax_treasury, srv.tax_collected_lifetime = t_saved
    print("COMMISSION_ECON_OK")

    # world validator: live data clean, bad refs reported, dynamic shards ok
    assert srv.validate_world(srv.WORLD) == []
    bad_world = {"rooms": {"a": {"exits": {"north": "nowhere"}}}, "items": {},
                 "start_room": "a",
                 "room_items": {"a": ["ghost_item"]},
                 "gather_nodes": {"n": {"room": "a", "item": "ghost_item"}},
                 "npcs": {"b": {"room": "nowhere"}},
                 "recipes": {"r": {"result": "ghost", "inputs": {"dungeon_shard_10": 1}}}}
    errs = srv.validate_world(bad_world)
    assert len(errs) == 5, errs  # exit, room_item, node yield, npc room, recipe result (shard input passes)
    assert srv.validate_world({"rooms": {}, "items": {}}) == ["no rooms defined"]
    print("VALIDATE_WORLD_OK")

    # online sellers spend proceeds immediately (#192.1)
    seller = mkplayer("OnlineSeller", 50020, room="market")
    seller.inventory.append("healing_herb")
    await srv.cmd_market_post(seller, {"item": herb_name, "price": 10})
    soid = max(o["id"] for o in srv.market_orders if o["seller"] == "OnlineSeller")
    buyer2 = mkplayer("SpenderBuyer", 50021, room="market")
    buyer2.gold = 200
    seller_gold0 = seller.gold
    await srv.cmd_market_buy(buyer2, {"id": soid})
    payout = 10 - max(srv.TAX_MINIMUM, round(10 * srv.TAX_RATE))
    assert seller.gold == seller_gold0 + payout, (seller.gold, seller_gold0, payout)
    assert srv.get_score_entry("OnlineSeller").get("gold_bank", 0) == 0
    unplayer(seller)
    unplayer(buyer2)
    print("SELLER_PAYOUT_OK")

    # per-poster open cap, market/invite TTL sweeps, collusion cap (#192.2)
    t_saved2 = (srv.tax_treasury, srv.tax_collected_lifetime)
    capper = mkplayer("OpenCapper", 50022)
    capper.gold = 100000
    inbox.clear()
    for _ in range(srv.COMMISSION_MAX_OPEN_PER_POSTER):
        await srv.cmd_commission_post(capper, {"target": "rat", "required_kills": 1,
                                               "reward_gold": 1, "reward_xp": 0})
    assert not any(m.get("type") == "error" for m in inbox), inbox[-1]
    n_open_total = len(srv._commissions)
    await srv.cmd_commission_post(capper, {"target": "rat", "required_kills": 1,
                                           "reward_gold": 1, "reward_xp": 0})
    assert inbox[-1]["type"] == "error" and "max" in inbox[-1]["text"].lower(), inbox[-1]
    assert len(srv._commissions) == n_open_total
    my_open = [c for c in srv._commissions.values()
               if c["status"] == "open" and c["poster"] == "OpenCapper"]
    await srv.cmd_commission_cancel(capper, {"commission_id": my_open[0]["id"]})
    inbox.clear()
    await srv.cmd_commission_post(capper, {"target": "rat", "required_kills": 1,
                                           "reward_gold": 1, "reward_xp": 0})
    assert any(m.get("type") == "message" and "posted" in m.get("text", "") for m in inbox), inbox[-1]
    for c in list(srv._commissions.values()):
        if c["poster"] == "OpenCapper" and c["status"] == "open":
            await srv.cmd_commission_cancel(capper, {"commission_id": c["id"]})
    unplayer(capper)
    print("OPEN_CAP_OK")

    # market TTL: aged order returns to the online seller, stays for offline
    ager = mkplayer("AgedSeller", 50023, room="market")
    ager.inventory.append("healing_herb")
    await srv.cmd_market_post(ager, {"item": herb_name, "price": 10})
    aoid = max(o["id"] for o in srv.market_orders if o["seller"] == "AgedSeller")
    assert "healing_herb" not in ager.inventory
    for o in srv.market_orders:
        if o["id"] == aoid:
            o["ts"] -= (srv.MARKET_ORDER_TTL_SECONDS + 1)
    srv._last_market_prune = 0.0
    assert await srv.prune_market_orders() == 1
    assert not any(o["id"] == aoid for o in srv.market_orders)
    assert "healing_herb" in ager.inventory
    ager.inventory.append("healing_herb")
    await srv.cmd_market_post(ager, {"item": herb_name, "price": 10})
    boid = max(o["id"] for o in srv.market_orders if o["seller"] == "AgedSeller")
    for o in srv.market_orders:
        if o["id"] == boid:
            o["ts"] -= (srv.MARKET_ORDER_TTL_SECONDS + 1)
    unplayer(ager)  # goes offline holding a live listing
    srv._last_market_prune = 0.0
    assert await srv.prune_market_orders() == 0
    assert any(o["id"] == boid for o in srv.market_orders)
    srv.market_orders[:] = [o for o in srv.market_orders if o["id"] != boid]
    print("MARKET_TTL_OK")

    # invite TTL: stale accept rejected, sweep drops non-responder rows
    inviter = mkplayer("Inviter", 50024)
    invitee = mkplayer("Invitee", 50025)
    await srv.cmd_party_invite(inviter, {"target": "Invitee"})
    assert invitee.id in srv._pending_party_invites
    srv._pending_party_invites[invitee.id]["ts"] -= (srv.PARTY_INVITE_TTL_SECONDS + 1)
    inbox.clear()
    await srv.cmd_party_accept(invitee, {})
    assert inbox[-1]["type"] == "error" and "expired" in inbox[-1]["text"].lower(), inbox[-1]
    await srv.cmd_party_invite(inviter, {"target": "Invitee"})
    srv._pending_party_invites[invitee.id]["ts"] -= (srv.PARTY_INVITE_TTL_SECONDS + 1)
    srv._last_invite_prune = 0.0
    assert srv.prune_invites() == 1
    assert invitee.id not in srv._pending_party_invites
    for p in list(srv.parties.values()):
        if inviter.id in p.member_ids:
            srv._delete_party(p)
    unplayer(inviter)
    unplayer(invitee)
    print("INVITE_TTL_OK")

    # disconnect purges invites the leaver sent (#248): no joining a party
    # whose inviter is offline
    leaver = mkplayer("Leaver", 50026)
    joiner = mkplayer("Joiner", 50027)
    await srv.cmd_party_invite(leaver, {"target": "Joiner"})
    assert joiner.id in srv._pending_party_invites
    await srv._leave_party_on_disconnect(leaver)
    assert joiner.id not in srv._pending_party_invites
    inbox.clear()
    await srv.cmd_party_accept(joiner, {})
    assert inbox[-1]["type"] == "error" and "no pending" in inbox[-1]["text"].lower(), inbox[-1]
    for p in list(srv.parties.values()):
        if leaver.id in p.member_ids:
            srv._delete_party(p)
    unplayer(leaver)
    unplayer(joiner)
    print("INVITE_DISCONNECT_OK")

    # party switch relocates out of the old dungeon instance (#226)
    import time as _time
    leadA = mkplayer("LeadA", 50030)
    leadB = mkplayer("LeadB", 50031)
    switcher = mkplayer("Switcher", 50032)
    partyA = srv._auto_create_party(leadA)
    dA = srv.Dungeon(party_id=partyA.id)
    srv.dungeons[dA.id] = dA
    partyA.dungeon_id = dA.id
    partyA.member_ids.add(switcher.id)
    switcher.party_id = partyA.id
    srv.remove_member(switcher)
    switcher.room = dA.room_id(2)
    srv.add_member(switcher)
    partyB = srv._auto_create_party(leadB)
    srv._pending_party_invites[switcher.id] = {"party": partyB, "ts": _time.time(),
                                               "inviter": leadB.id}
    await srv.cmd_party_accept(switcher, {})
    assert switcher.party_id == partyB.id
    assert switcher.room == srv.DUNGEON_ENTRANCE_ROOM, switcher.room
    assert switcher.id not in partyA.member_ids
    for p in (partyA, partyB):
        if p.id in srv.parties:
            srv._delete_party(p)
    for p in (leadA, leadB, switcher):
        unplayer(p)
    print("PARTY_SWITCH_RELOCATE_OK")

    # collusion cap: seeded history evicts least-frequent first, keeps newcomer
    clposter = mkplayer("CollabPoster", 50026)
    clposter.gold = 100000
    clentry = srv.get_score_entry("CollabPoster")
    clentry["collab_fills"] = {f"filler{i}": 1 for i in range(srv.COMMISSION_COLLAB_CAP)}
    await srv.cmd_commission_post(clposter, {"target": "rat", "required_kills": 1,
                                             "reward_gold": 10, "reward_xp": 0})
    cid_cl = max(srv._commissions)
    clfiller = mkplayer("CollabNew", 50027)
    srv.record_npc_kill("CollabNew", "Giant Rat")
    await srv.cmd_commission_fill(clfiller, {"commission_id": cid_cl})
    assert srv._commissions[cid_cl]["status"] == "completed"
    collab = clentry["collab_fills"]
    assert len(collab) == srv.COMMISSION_COLLAB_CAP, len(collab)
    assert "collabnew" in collab
    unplayer(clposter)
    unplayer(clfiller)
    srv.tax_treasury, srv.tax_collected_lifetime = t_saved2
    print("COLLAB_CAP_OK")

    # charm turn-in requires the charm in hand, not just the flag (#192.4)
    charmer = mkplayer("Charmer", 50028, room="town_square")
    chentry = srv.get_score_entry("Charmer")
    await srv.cmd_quest(charmer, {"action": "accept", "quest": "guard_charm"})
    assert chentry.get("quest_guard_active")
    chentry["guard_charm_crafted"] = True  # flag set, charm dropped/sold
    assert srv.QUEST_CHARM_RESULT not in charmer.inventory
    inbox.clear()
    await srv.cmd_quest(charmer, {"action": "turn_in", "quest": "guard_charm"})
    assert inbox[-1]["type"] == "message" and "no longer in your pack" in inbox[-1]["text"], inbox[-1]
    assert chentry.get("quest_guard_active")  # still active, nothing consumed
    charmer.inventory.append(srv.QUEST_CHARM_RESULT)
    await srv.cmd_quest(charmer, {"action": "turn_in", "quest": "guard_charm"})
    assert not chentry.get("quest_guard_active")
    assert srv.QUEST_CHARM_RESULT not in charmer.inventory
    unplayer(charmer)
    print("CHARM_GATE_OK")

    # pre-crafted charm survives accept (#239): no double craft
    prefarm = mkplayer("PreFarmer", 50029, room="town_square")
    pfentry = srv.get_score_entry("PreFarmer")
    prefarm.inventory.append(srv.QUEST_CHARM_RESULT)  # crafted before accepting
    await srv.cmd_quest(prefarm, {"action": "accept", "quest": "guard_charm"})
    assert pfentry.get("quest_guard_active")
    assert pfentry.get("guard_charm_crafted") is True
    inbox.clear()
    await srv.cmd_quest(prefarm, {"action": "turn_in", "quest": "guard_charm"})
    assert not pfentry.get("quest_guard_active")
    assert srv.QUEST_CHARM_RESULT not in prefarm.inventory
    unplayer(prefarm)
    print("CHARM_PREFARM_OK")

    # bool coercion: string "false" must not enable boolean gates (#192.3)
    import json as _json
    import os as _os
    cfg_path = _os.path.join(_os.path.dirname(srv.CONFIG_FILE), "test_bool_cfg_tmp.json")
    real_cfg, real_val = srv.CONFIG_FILE, srv.AUTH_TOKEN_REQUIRED
    try:
        with open(cfg_path, "w") as f:
            _json.dump({"flags": {"AUTH_TOKEN_REQUIRED": "false"}}, f)
        srv.CONFIG_FILE = cfg_path
        srv._apply_config()
        assert srv.AUTH_TOKEN_REQUIRED is False, srv.AUTH_TOKEN_REQUIRED
        with open(cfg_path, "w") as f:
            _json.dump({"flags": {"AUTH_TOKEN_REQUIRED": "yes"}}, f)
        srv._apply_config()
        assert srv.AUTH_TOKEN_REQUIRED is True, srv.AUTH_TOKEN_REQUIRED
        with open(cfg_path, "w") as f:
            _json.dump({"flags": {"AUTH_TOKEN_REQUIRED": "0"}}, f)
        srv._apply_config()
        assert srv.AUTH_TOKEN_REQUIRED is False, srv.AUTH_TOKEN_REQUIRED
    finally:
        srv.CONFIG_FILE = real_cfg
        srv.AUTH_TOKEN_REQUIRED = real_val
        if _os.path.exists(cfg_path):
            _os.remove(cfg_path)
    print("BOOL_CONFIG_OK")

    # unknown keys warn instead of vanishing (#241)
    import io as _io
    import contextlib as _ctx
    cfg_path2 = _os.path.join(_os.path.dirname(srv.CONFIG_FILE), "test_unknown_cfg_tmp.json")
    real_cfg3 = srv.CONFIG_FILE
    try:
        with open(cfg_path2, "w") as f:
            _json.dump({"scoring": {"DUNGEON_MAX_FLOORs": 60}}, f)
        srv.CONFIG_FILE = cfg_path2
        buf = _io.StringIO()
        with _ctx.redirect_stdout(buf):
            srv._apply_config()
        assert "DUNGEON_MAX_FLOORs" in buf.getvalue(), buf.getvalue()
    finally:
        srv.CONFIG_FILE = real_cfg3
        if _os.path.exists(cfg_path2):
            _os.remove(cfg_path2)
    print("UNKNOWN_CONFIG_OK")

    # quest config section applies to globals AND catalog (#251)
    cfg_path3 = _os.path.join(_os.path.dirname(srv.CONFIG_FILE), "test_quest_cfg_tmp.json")
    real_cfg4 = srv.CONFIG_FILE
    saved_q = (srv.QUEST_DELVER_FLOORS, srv.QUEST_DELVER_XP,
               srv.QUESTS["delver"]["floors_required"])
    try:
        with open(cfg_path3, "w") as f:
            _json.dump({"quests": {"QUEST_DELVER_FLOORS": 5,
                                   "QUEST_DELVER_XP": 99}}, f)
        srv.CONFIG_FILE = cfg_path3
        srv._apply_config()
        srv._refresh_quests()
        assert srv.QUEST_DELVER_FLOORS == 5, srv.QUEST_DELVER_FLOORS
        assert srv.QUESTS["delver"]["floors_required"] == 5
        assert srv.QUESTS["delver"]["reward_xp"] == 99
    finally:
        srv.CONFIG_FILE = real_cfg4
        (srv.QUEST_DELVER_FLOORS, srv.QUEST_DELVER_XP) = saved_q[:2]
        srv._refresh_quests()
        assert srv.QUESTS["delver"]["floors_required"] == saved_q[2]
        if _os.path.exists(cfg_path3):
            _os.remove(cfg_path3)
    print("QUEST_CONFIG_OK")

    # --config overlay: short TTLs apply, untouched keys keep prod defaults
    import os as _os2
    overlay = _os2.path.join(_os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                             "ml", "conductor", "soak_server_config.json")
    real_cfg2 = srv.CONFIG_FILE
    saved_ttls = (srv.MARKET_ORDER_TTL_SECONDS, srv.PARTY_INVITE_TTL_SECONDS,
                  srv.COMMISSION_TTL_SECONDS)
    try:
        args = srv.parse_args(["--config", overlay])
        srv.CONFIG_FILE = args.config
        srv._apply_config()
        assert srv.MARKET_ORDER_TTL_SECONDS == 300, srv.MARKET_ORDER_TTL_SECONDS
        assert srv.PARTY_INVITE_TTL_SECONDS == 60, srv.PARTY_INVITE_TTL_SECONDS
        assert srv.COMMISSION_TTL_SECONDS == 600, srv.COMMISSION_TTL_SECONDS
        assert srv.COMMISSION_MAX_XP == 500  # not in overlay: prod default kept
    finally:
        srv.CONFIG_FILE = real_cfg2
        (srv.MARKET_ORDER_TTL_SECONDS, srv.PARTY_INVITE_TTL_SECONDS,
         srv.COMMISSION_TTL_SECONDS) = saved_ttls
    print("CONFIG_FLAG_OK")

    # market stall + cancel share one case-insensitive identity (#192 market)
    caseseller = mkplayer("CaseSeller", 50029, room="market")
    caseseller.inventory.extend(["healing_herb"] * 4)
    for _ in range(3):
        await srv.cmd_market_post(caseseller, {"item": herb_name, "price": 10})
    caseseller_low = mkplayer("caseseller", 50030, room="market")
    caseseller_low.inventory.append("healing_herb")
    await srv.cmd_market_post(caseseller_low, {"item": herb_name, "price": 10})
    assert inbox[-1]["type"] == "error" and "stall full" in inbox[-1]["text"].lower(), inbox[-1]
    void = max(o["id"] for o in srv.market_orders if o["seller"] == "CaseSeller")
    await srv.cmd_market_cancel(caseseller_low, {"id": void})
    assert not any(o["id"] == void for o in srv.market_orders)
    assert "healing_herb" in caseseller_low.inventory  # item returns to the cancelling variant
    srv.market_orders[:] = [o for o in srv.market_orders if o["seller"].lower() != "caseseller"]
    unplayer(caseseller)
    unplayer(caseseller_low)
    print("MARKET_CASE_OK")

    # relic craft with ungenerated dynamic mats errors cleanly (#190)
    crafter = mkplayer("Crafter", 50005, room="town_square")
    await srv.cmd_craft(crafter, {"recipe": "relic_aegis"})
    assert inbox[-1]["type"] == "error" and "dungeon_shard_10" in inbox[-1]["text"], inbox[-1]

    # Ghost-equip regression test: equipping an ingredient then crafting clears slot
    crafter.inventory = ["dungeon_shard_10", "iron_plate", "ectoplasm", "ectoplasm"]
    await srv.cmd_equip(crafter, {"item": "Iron Plate Armor"})
    assert crafter.armor == "iron_plate"
    assert srv._player_defense(crafter) == 3
    await srv.cmd_craft(crafter, {"recipe": "relic_aegis"})
    assert "iron_plate" not in crafter.inventory
    assert crafter.armor is None
    assert srv._player_defense(crafter) == 0

    # Extended regression: weapon-slot, offhand-slot, multi-copy retention, stats_view assertion
    crafter.inventory = [
        "warden_trophy", "iron_ore", "iron_ore", "serpent_scale",  # for wardens_blade (weapon)
        "warden_trophy", "troll_hide", "troll_hide", "pine_timber", "pine_timber",  # for deep_bulwark (offhand)
        "iron_plate", "iron_plate",  # 2x iron_plate for multi-copy test
        "dungeon_shard_10", "ectoplasm", "ectoplasm",  # for relic_aegis with multi-copy iron_plate
    ]
    # Equip iron_ore? iron_ore is not equippable, but warden's trophy is junk.
    # Let's craft warden's blade, equip it, then craft deep_bulwark which uses warden_trophy (not blade).
    # To test weapon-slot ghost equip: craft serpentbrand (weapon, uses serpent_scale, iron_ore, resin), equip it, craft something using iron_ore when 1 iron_ore held.
    crafter.inventory = ["iron_ore", "mountain_berry", "mountain_herb"]  # for fortitude_tonic (quest tonic)
    # Let's use recipe "iron_plate" which uses "iron_ore" x2, "wolf_pelt" x1, "scrap_iron" x1.
    # Or craft_wardens_blade which uses warden_trophy, iron_ore x2, serpent_scale x1.
    # What if we have a weapon recipe that consumes a weapon?
    # Recipe 'relic_aegis' consumes iron_plate (armor).
    # Recipe 'deep_bulwark' consumes warden_trophy, troll_hide x2, pine_timber x2 -> produces deep_bulwark (offhand).
    # Let's test offhand: equip deep_bulwark, but is deep_bulwark an input for any recipe? No.
    # But any item in offhand slot (e.g. if we set crafter.offhand = "iron_ore" or "troll_hide"):
    # If troll_hide is in offhand slot and consumed by deep_bulwark craft:
    crafter.inventory = ["warden_trophy", "troll_hide", "troll_hide", "pine_timber", "pine_timber"]
    crafter.offhand = "troll_hide"
    await srv.cmd_craft(crafter, {"recipe": "deep_bulwark"})
    assert "troll_hide" not in crafter.inventory
    assert crafter.offhand is None  # cleared offhand slot!

    # Weapon-slot test:
    crafter.inventory = ["warden_trophy", "iron_ore", "iron_ore", "serpent_scale"]
    crafter.equipped = "warden_trophy"
    await srv.cmd_craft(crafter, {"recipe": "wardens_blade"})
    assert "warden_trophy" not in crafter.inventory
    assert crafter.equipped is None  # cleared weapon slot!

    # Multi-copy retention test: 2x iron_plate in inventory, 1 equipped. Craft relic_aegis (uses 1x iron_plate).
    crafter.inventory = ["iron_plate", "iron_plate", "dungeon_shard_10", "ectoplasm", "ectoplasm"]
    await srv.cmd_equip(crafter, {"item": "Iron Plate Armor"})
    assert crafter.armor == "iron_plate"
    await srv.cmd_craft(crafter, {"recipe": "relic_aegis"})
    assert crafter.inventory.count("iron_plate") == 1
    assert crafter.armor == "iron_plate"  # 1 copy remains in inventory, slot retained!

    # Stats_view outbound assertion
    st = srv.stats_view(crafter)
    assert st["armor"] == "Iron Plate Armor"
    assert st["equipped"] is None
    assert st["offhand"] is None

    # Quest turn_in slot clearing regression test
    sister = srv.Player(ws=FakeWS(), id=50006, name="SisterPlayer", logged_in=True, room="healing_spring")
    srv.add_member(sister)
    s_entry = srv.get_score_entry("SisterPlayer")
    await srv.cmd_quest(sister, {"action": "accept", "quest": "remedy"})
    sister.inventory = ["healing_herb", "healing_herb", "healing_herb"]
    sister.equipped = "healing_herb"
    await srv.cmd_quest(sister, {"action": "turn_in", "quest": "remedy"})
    assert "healing_herb" not in sister.inventory
    assert sister.equipped is None  # cleared equipped slot on quest turn_in!
    st_s = srv.stats_view(sister)
    assert st_s["equipped"] is None

    unplayer(sister)
    unplayer(crafter)
    print("RELIC_CRAFT_OK")

    # malformed input never drops the connection (#190): drive the real
    # connection loop with a scripted socket, then check error replies
    # landed and the player table has no residue. NOTE: the real send()
    # path is required here (the fake_send patch bypasses the outbound
    # queue + writer task this block exercises).
    _patched_send = srv.send
    srv.send = orig_send

    class ScriptWS:
        def __init__(self, raws):
            self._raws = list(raws)
            self.sent = []
            self.remote_address = ("127.0.0.1", 1)

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for raw in self._raws:
                yield raw
            # let the outbound writer flush the queue before the loop
            # ends (cleanup cancels it immediately after)
            await asyncio.sleep(0.3)
            for _ in range(20):
                if len(self.sent) >= 5:
                    break
                await asyncio.sleep(0.1)

        async def send(self, payload):
            self.sent.append(payload if isinstance(payload, dict) else json.loads(payload))

    n_players_before = len(srv.players)
    sws = ScriptWS([json.dumps([]), json.dumps(42), json.dumps("hi"),
                    json.dumps({"cmd": ["x"]}),
                    json.dumps({"cmd": "login", "name": 123})])
    await srv.handle_connection(sws)
    errs = [m for m in sws.sent if m.get("type") == "error"]
    assert len(errs) == 5, sws.sent
    assert len(srv.players) == n_players_before
    srv.send = _patched_send
    print("MALFORMED_OK")

    # login obeys room capacity like moves do (#227)
    fillers = [mkplayer(f"CapFill{i}", 51000 + i, room=srv.START_ROOM)
               for i in range(srv.MAX_PLAYERS_PER_ROOM)]
    assert len(srv.players_in_room(srv.START_ROOM)) == srv.MAX_PLAYERS_PER_ROOM
    newcomer = srv.Player(ws=FakeWS(), id=51999, name="", logged_in=False)
    srv.players[51999] = newcomer
    await srv.cmd_login(newcomer, {"name": "CrowdedOut"})
    assert inbox[-1]["type"] == "error" and "crowded" in inbox[-1]["text"].lower(), inbox[-1]
    assert not newcomer.logged_in
    assert len(srv.players_in_room(srv.START_ROOM)) == srv.MAX_PLAYERS_PER_ROOM
    # rejected logins leave no entry/token state (#227 follow-up): a
    # crowded probe carrying a token must not squat the name for its owner
    squatter = srv.Player(ws=FakeWS(), id=51998, name="", logged_in=False)
    srv.players[51998] = squatter
    await srv.cmd_login(squatter, {"name": "Squatted", "token": "evil"})
    assert inbox[-1]["type"] == "error" and "crowded" in inbox[-1]["text"].lower(), inbox[-1]
    assert "squatted" not in srv.SCORES
    srv.players.pop(51998, None)
    for f in fillers:
        unplayer(f)
    srv.players.pop(51999, None)
    print("LOGIN_CAP_OK")

    # version mismatch warns, matching versions stay quiet (#243)
    versioned = srv.Player(ws=FakeWS(), id=52000, name="", logged_in=False)
    srv.players[52000] = versioned
    inbox.clear()
    await srv.cmd_login(versioned, {"name": "Versioned", "protocol_version": 999})
    welcome = [m for m in inbox if m.get("type") == "welcome"]
    assert welcome and "version_mismatch" in welcome[0], inbox
    assert versioned.logged_in
    srv.remove_member(versioned)
    srv.players.pop(52000, None)
    del srv.SCORES["versioned"]
    unversioned = srv.Player(ws=FakeWS(), id=52001, name="", logged_in=False)
    srv.players[52001] = unversioned
    inbox.clear()
    await srv.cmd_login(unversioned, {"name": "Unversioned"})
    welcome = [m for m in inbox if m.get("type") == "welcome"]
    assert welcome and "version_mismatch" not in welcome[0], inbox
    srv.remove_member(unversioned)
    srv.players.pop(52001, None)
    del srv.SCORES["unversioned"]
    print("VERSION_WARN_OK")

    # failed GM bind closes the game listener, propagates (#242)
    import socket as _socket
    squat = _socket.socket()
    squat.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    squat.bind(("127.0.0.1", 0))
    squat.listen(1)
    squat_port = squat.getsockname()[1]
    saved_ports = (srv.PORT, srv.GM_PORT, srv.HTTP_PORT)
    saved_scores = {}
    for _sfx in ("", ".1", ".2"):
        _p = srv.SCORES_FILE + _sfx
        saved_scores[_sfx] = open(_p, "rb").read() if _os.path.exists(_p) else None
    try:
        srv.PORT, srv.GM_PORT, srv.HTTP_PORT = 18771, squat_port, 18772
        try:
            await srv.main()  # already inside the suite's event loop
            raised = False
        except OSError:
            raised = True
        assert raised, "main() must propagate the GM bind failure"
        probe = _socket.socket()
        try:
            probe.bind(("127.0.0.1", 18771))  # free again: listener closed
        finally:
            probe.close()
    finally:
        squat.close()
        srv.PORT, srv.GM_PORT, srv.HTTP_PORT = saved_ports
        for _sfx, _data in saved_scores.items():
            _p = srv.SCORES_FILE + _sfx
            if _data is None:
                if _os.path.exists(_p):
                    _os.remove(_p)
            else:
                with open(_p, "wb") as f:
                    f.write(_data)
    print("STARTUP_LEAK_OK")

    # shutdown cancels background tasks before the final save (#244)
    saved_ports2 = (srv.PORT, srv.GM_PORT, srv.HTTP_PORT)
    saved_scores2 = {}
    for _sfx in ("", ".1", ".2"):
        _p = srv.SCORES_FILE + _sfx
        saved_scores2[_sfx] = open(_p, "rb").read() if _os.path.exists(_p) else None
    srv.PORT, srv.GM_PORT, srv.HTTP_PORT = 18781, 18782, 18783
    try:
        main_task = asyncio.create_task(srv.main())
        for _ in range(100):
            await asyncio.sleep(0.1)
            try:
                _probe2 = _socket.socket()
                _probe2.connect(("127.0.0.1", 18781))
                _probe2.close()
                break
            except OSError:
                pass
        else:
            raise AssertionError("test server did not boot")
        main_task.cancel()
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        lingering = [t for t in asyncio.all_tasks()
                     if not t.done() and getattr(t.get_coro(), "__qualname__", "") == "_run_resilient"]
        assert not lingering, f"background tasks survive shutdown: {lingering}"
    finally:
        srv.PORT, srv.GM_PORT, srv.HTTP_PORT = saved_ports2
        for _sfx, _data in saved_scores2.items():
            _p = srv.SCORES_FILE + _sfx
            if _data is None:
                if _os.path.exists(_p):
                    _os.remove(_p)
            else:
                with open(_p, "wb") as f:
                    f.write(_data)
    print("SHUTDOWN_TASKS_OK")

    # /health reads the cached snapshot, never the live dict (#224)
    import json as _json2
    hp1 = mkplayer("HealthOne", 52002)
    hp2 = mkplayer("HealthTwo", 52003)
    srv.refresh_snapshot_json()
    snap = _json2.loads(srv.world_snapshot_json)
    live = sum(1 for p in srv.players.values() if p.logged_in)
    assert snap["server"]["players_online"] == live >= 2, snap["server"]
    unplayer(hp1)
    unplayer(hp2)
    print("HEALTH_SNAPSHOT_OK")

    # safety net logs one line, never a traceback (disk-fill vector when
    # TEXTMMO_LOG_FILE is set): capture stdout through a real dispatch
    import io as _io
    from contextlib import redirect_stdout as _redirect_stdout
    srv.send = orig_send
    spam_ws = ScriptWS([json.dumps({"cmd": "login", "name": 123})])
    buf = _io.StringIO()
    with _redirect_stdout(buf):
        await srv.handle_connection(spam_ws)
    srv.send = _patched_send
    logged = buf.getvalue()
    assert "Traceback" not in logged, logged
    assert "handler error on login: AttributeError" in logged, logged
    assert any(m.get("type") == "error" for m in spam_ws.sent)
    print("LOG_SPAM_OK")

    # --- Starting purse (#258): granted once at first login, never topped up ---
    purse_name = "PurseTester"
    p1 = srv.Player(ws=FakeWS(), id=70001, name="", logged_in=False)
    await srv.cmd_login(p1, {"name": purse_name})
    assert p1.gold == srv.STARTING_GOLD, p1.gold
    assert srv.get_score_entry(purse_name).get("starting_purse_claimed") is True
    assert any("stakes you" in m.get("text", "") for m in inbox), inbox[-3:]
    # relog with an empty pack: same entry, no second purse
    p1.gold = 0
    srv.remove_member(p1)
    srv.name_owners.pop(purse_name.lower(), None)
    p2 = srv.Player(ws=FakeWS(), id=70002, name="", logged_in=False)
    await srv.cmd_login(p2, {"name": purse_name})
    assert p2.gold == 0, p2.gold
    # evicted entry (7d TTL path): counts as new again, re-grants once
    srv.remove_member(p2)
    srv.name_owners.pop(purse_name.lower(), None)
    srv.SCORES.pop(purse_name.lower(), None)
    p3 = srv.Player(ws=FakeWS(), id=70003, name="", logged_in=False)
    await srv.cmd_login(p3, {"name": purse_name})
    assert p3.gold == srv.STARTING_GOLD, p3.gold
    srv.remove_member(p3)
    srv.name_owners.pop(purse_name.lower(), None)
    srv.SCORES.pop(purse_name.lower(), None)
    print("STARTING_PURSE_OK")

asyncio.run(main())
