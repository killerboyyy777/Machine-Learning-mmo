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
    crafter.inventory.extend(["iron_ore", "wolf_pelt"])
    await srv.cmd_craft(crafter, {"recipe": "sharpening_oil"})
    assert "sharpening_oil" in crafter.inventory
    base_attack = crafter.attack
    await srv.cmd_use(crafter, {"item": "sharpening oil"})
    assert crafter.attack == base_attack + 2
    assert crafter.active_buffs["attack"]["remaining"] == 10
    assert "sharpening_oil" not in crafter.inventory
    print("CRAFTED_BUFF_OK")

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
    assert srv.SCORES["seller"]["trades_completed"] == 1 and srv.SCORES["buyer"]["trades_completed"] == 1
    assert srv.SCORES["seller"]["gold_bank"] == 90
    print("MARKET_TAX_OK")

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
    print("ALL_OK")

asyncio.run(main())
