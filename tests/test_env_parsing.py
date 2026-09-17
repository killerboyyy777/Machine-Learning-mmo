"""Offline env fidelity tests (#191): no server needed.

Run from the repo root:  python tests/test_env_parsing.py
Covers: commission_list parsing (plain + discounted-rate lines), and pack
masking from the server's authoritative pack count (the 20-name inv list
truncates near the 24-unit cap, so name-counting leaves take/gather/buy
mask-valid when the server will reject them).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))

from ml_env import TextMMOEnv, _parse_commissions, pack_full, pack_units


def test_discounted_commission_parses():
    plain = "#12: slay 5x Giant Rat -- reward 25g + 50xp (posted by Alice)"
    disc = "#13: slay 5x Giant Rat \u2014 reward 25g + 50xp (your rate: x0.5) (posted by Alice)"
    hyphen = "#14: slay 1x Wolf - reward 10g + 5xp (your rate: x0.1) (posted by Bob)"
    rows = _parse_commissions("\n".join([plain, disc, hyphen]))
    assert len(rows) == 3, rows
    assert rows[0]["id"] == 12 and rows[0]["rate"] is None and rows[0]["poster"] == "Alice"
    assert rows[1]["id"] == 13 and rows[1]["rate"] == 0.5 and rows[1]["poster"] == "Alice"
    assert rows[1]["gold"] == 25 and rows[1]["xp"] == 50
    assert rows[2]["id"] == 14 and rows[2]["rate"] == 0.1 and rows[2]["target"] == "Wolf"
    assert _parse_commissions("No open commissions right now.") == []
    print("COMMISSION_PARSE_OK")


def _env_with_pack(names, units):
    e = TextMMOEnv("ParseT")
    e._state["inv_names"] = list(names)
    e._state["pack_units"] = units
    e._state["pack_max"] = 24
    return e


def test_pack_masks_from_server_count():
    names20 = [f"Trinket {i}" for i in range(20)]  # truncated list, as stats sends it
    # Full 24-unit pack reading as 20 names: name-counting says valid,
    # server truth says full (this is the desync that wasted agent steps).
    e = _env_with_pack(names20, 24)
    assert pack_units(e._state) == 24
    assert pack_full(e._state)
    e._state["item_names"] = ["Rusty Nail"]
    e._state["gatherables"] = ["Pine Timber Node"]
    assert e._action_to_cmd("take") is None
    assert e._action_to_cmd("gather") is None
    assert e._action_to_cmd("buy_arrows") is None
    # gold piles ignore the cap: still takeable from a full pack
    e._state["room_gold"] = 5
    assert e._action_to_cmd("take") == {"cmd": "take", "item": "gold"}
    e._state["room_gold"] = 0
    # 23 units: still room, everything valid (name-counting agrees here)
    e._state["pack_units"] = 23
    assert not pack_full(e._state)
    # 20 units: everything valid again
    e._state["pack_units"] = 20
    assert not pack_full(e._state)
    assert e._action_to_cmd("take") == {"cmd": "take", "item": "Rusty Nail"}
    assert e._action_to_cmd("gather") == {"cmd": "gather"}
    assert e._action_to_cmd("buy_arrows") == {"cmd": "buy", "item": "arrow"}
    # legacy fallback (no stats yet): counts names
    e._state["pack_units"] = None
    assert pack_units(e._state) == 20
    assert not pack_full(e._state)
    print("PACK_MASK_OK")


test_discounted_commission_parses()
test_pack_masks_from_server_count()
print("ALL_ENV_PARSE_OK")
