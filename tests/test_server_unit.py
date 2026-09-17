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
    assert srv.get_score_entry("Patsy")["xp"] - patsy_xp0 == 3.0
    assert srv.get_score_entry("Wash")["xp"] - seller_xp0 == 3.0
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

asyncio.run(main())
