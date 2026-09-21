"""Snapshot API contract for the canonical dashboard (issue #207).

Consumer-driven: every key/shape dashboard.html reads from GET /api/state
(plus /api/activity/stream event fields) is pinned here. Server evolution
must stay additive -- required keys present with JSON scalar/container
types, extra keys always allowed. Run from the repo root:
    python tests/test_snapshot_contract.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


def need(d, keys, where):
    missing = [k for k in keys if k not in d]
    assert not missing, f"{where} missing keys: {missing}"


s = srv.world_snapshot()

# --- top level: additive subset, extras allowed ---
need(s, {"server", "rooms", "players", "scores", "activity", "market",
          "buffs", "bosses", "dungeons", "quests", "recipes",
          "commissions", "catalog", "history"}, "snapshot")

# --- server block: header stats, ports, uptime ticker seed ---
need(s["server"], {"ws_port", "gm_port", "uptime", "start_ts",
                   "players_online", "connections"}, "server")
assert isinstance(s["server"]["start_ts"], (int, float))

# --- rooms: all 32, map + drawer fields, shelter bool ---
assert len(s["rooms"]) == 32, f"room count {len(s['rooms'])}"
for r in s["rooms"]:
    need(r, {"id", "name", "exits", "players", "npcs", "items",
             "gold", "shelter"}, f"room {r.get('id')}")
    assert isinstance(r["shelter"], bool), f"room {r['id']} shelter not bool"
    for p in r["players"]:
        need(p, {"name", "level"}, f"room {r['id']} player")

# --- market block + orders + fills ---
need(s["market"], {"treasury", "collected_lifetime", "tax_rate",
                   "tax_min", "trade_count", "orders", "history"}, "market")
for o in s["market"]["orders"]:
    need(o, {"id", "seller", "item", "price", "ts"}, "order")
for h in s["market"]["history"]:
    need(h, {"time", "ts", "buyer", "seller", "item", "price",
             "tax", "payout"}, "fill")

# --- buffs / bosses / dungeons ---
need(s["buffs"], {"xp", "gold"}, "buffs")
for b in s["bosses"]:
    need(b, {"name", "room", "hp", "max_hp", "attack"}, "boss")
for d in s["dungeons"]:
    need(d, {"id", "party", "max_floor_reached", "floors"}, "dungeon")

# --- quests: catalog + live counts + turn-in feed ---
need(s["quests"], {"catalog", "active", "completions",
                   "turnins_last_min", "recent_turnins"}, "quests")
assert s["quests"]["catalog"], "quest catalog empty"
for c in s["quests"]["catalog"]:
    need(c, {"id", "giver_name", "room", "reward_xp",
             "reward_gold"}, f"quest {c.get('id')}")
for t in s["quests"]["recent_turnins"]:
    need(t, {"t", "name", "qid"}, "turn-in")

# --- recipes: browser fields, names resolved ---
assert s["recipes"], "recipe list empty"
for r in s["recipes"]:
    need(r, {"id", "result", "result_qty", "inputs",
             "tier", "category"}, f"recipe {r.get('id')}")
    for i in r["inputs"]:
        need(i, {"item", "qty"}, f"recipe {r['id']} input")

# --- commissions board (cap 100, newest first not required) ---
assert isinstance(s["commissions"], list)
for c in s["commissions"]:
    need(c, {"id", "poster", "target", "required_kills", "reward_gold",
             "reward_xp", "status", "filled_by", "created_ts"},
         f"commission {c.get('id')}")
assert len(s["commissions"]) <= 100, "commissions snapshot uncapped"

# --- catalog datalists ---
need(s["catalog"], {"players", "items", "rooms"}, "catalog")

# --- history samples (may be empty on fresh boot) ---
for h in s["history"]:
    need(h, {"ts", "players_online", "top_scores",
             "market_orders"}, "history sample")

# --- live activity entry shape, seeded then cleaned up ---
n_log = len(srv.command_log)
n_per = len(srv.track_log.get("ContractProbe", []))
had_score = "contractprobe" in {k.lower() for k in srv.SCORES}
try:
    srv.log_command("ContractProbe", "look", {})
    entry = srv.command_log[-1]
    need(entry, {"seq", "time", "name", "cmd", "detail",
                 "level"}, "activity entry")
    assert isinstance(entry["seq"], int) and entry["seq"] > 0
    # Same dicts feed the SSE stream: JSON-serializable as one frame.
    import json as _json
    frame = f"id: {entry['seq']}\ndata: {_json.dumps(entry)}\n\n"
    assert frame.startswith("id: ")
finally:
    while len(srv.command_log) > n_log:
        srv.command_log.pop()
    per = srv.track_log.get("ContractProbe", [])
    while len(per) > n_per:
        per.pop()
    if not had_score:
        for k in [k for k in srv.SCORES if k.lower() == "contractprobe"]:
            del srv.SCORES[k]

# --- players/scores entries, if anyone is online ---
for p in s["players"]:
    need(p, {"name", "level", "score", "kills", "deaths", "gold",
             "hp", "max_hp", "room", "last_action",
             "recent_actions"}, f"player {p.get('name')}")
for e in s["scores"]:
    need(e, {"name", "score", "level"}, "score entry")

print("SNAPSHOT_CONTRACT_OK")
