"""Tannery fighter-material regression for #1046 (no live server needed).

Run from the repo root: python tests/test_tannery_fighter_material.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


def test_room_item_guard():
    for iid in sorted(srv.FIGHTER_MATERIALS):
        bad = {**srv.WORLD, "room_items": {"tannery": [iid]}}
        errors = srv.validate_world(bad)
        assert any("tannery" in e and "fighter-only" in e for e in errors), (
            iid,
            errors,
        )
    print("ROOM_ITEM_GUARD_OK")


def test_non_fighter_cannot_take_tannery_fighter_material():
    sent = []
    original_send = srv.send

    async def capture(player, payload):
        sent.append(payload)

    ground = srv.room_items["tannery"]
    saved_ground = list(ground)
    srv.send = capture
    player = srv.Player(id=94001, name="TanneryNonFighter", logged_in=True)
    player.room = "tannery"
    srv.players[player.id] = player
    srv.add_member(player)
    try:
        asyncio.run(srv.cmd_take(player, {"item": "wolf_pelt"}))
        assert "wolf_pelt" not in player.inventory, player.inventory
        assert player.hp == player.max_hp
        assert any(
            m.get("type") == "error" and "here to take" in m.get("text", "")
            for m in sent
        ), sent
    finally:
        srv.send = original_send
        ground[:] = saved_ground
        srv.remove_member(player)
        srv.players.pop(player.id, None)
    print("TANNERY_TAKE_OK")


def main():
    test_room_item_guard()
    test_non_fighter_cannot_take_tannery_fighter_material()


if __name__ == "__main__":
    main()
