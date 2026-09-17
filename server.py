"""Small Text MMO Engine 0.5 — instanced party dungeons, parties, player
market with GM treasury, leveling, repeatable quests, gathering nodes,
crafted buffs, ammo variants, and escrowed player commissions.

Protocol is plain JSON over WebSocket. See README.md for the full list.
"""
import asyncio
import json
import os
import random
import signal
import sys
import time
import traceback
import itertools
from collections import deque
from dataclasses import dataclass, field

import websockets
from os import path
from os.path import join, dirname, abspath

WORLD_FILE = join(dirname(abspath(__file__)), "world.json")
SCORES_FILE = join(dirname(abspath(__file__)), "scores.json")

NPC_TICK_SECONDS = 3
HOST = "0.0.0.0"
PORT = 8765
GM_HOST = "127.0.0.1"
GM_PORT = 8767

# Wire-protocol version, echoed in every `welcome` event (#72). Bump on any
# breaking protocol change. Clients SHOULD send their version on login;
# mismatches only warn (see ml_env version_match) -- old version-less
# clients keep working unchanged.
PROTOCOL_VERSION = 1

# Scoring anti-grind tuning
ACTION_WINDOW = 20
MIN_HISTORY_FOR_VARIETY = 5
MIN_VARIETY = 0.2
DIFFICULTY_K = 50.0
DEATH_PENALTY = 5.0
DEATH_GOLD_DROP_PCT = 40   # of carried gold drops as a floor pile on death
DEATH_GOLD_LOST_PCT = 10   # of carried gold vanishes permanently on death
DEATH_SCORE_PER_GOLD_LOST = 0.1  # extra score penalty per gold removed on death (floor: DEATH_PENALTY)
ALLY_ATTACK_BONUS_PER_PLAYER = 1
ALLY_ATTACK_BONUS_CAP = 3
TEAMWORK_BONUS_PER_EXTRA_CONTRIBUTOR = 0.2
TEAMWORK_BONUS_CAP_CONTRIBUTORS = 3


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_TOTAL_CONNECTIONS = _env_int("TEXTMMO_MAX_CONNECTIONS", 1000)
MAX_PLAYERS_PER_ROOM = _env_int("TEXTMMO_MAX_ROOM_PLAYERS", 12)
OUTBOUND_QUEUE_MAX = _env_int("TEXTMMO_OUTBOUND_QUEUE", 64)
AUTH_TOKEN_REQUIRED = str(os.environ.get("TEXTMMO_REQUIRE_TOKEN", "")).strip().lower() in ("1", "true", "yes", "on")
SNAPSHOT_REFRESH_SECONDS = 1.0
TASK_RESTART_DELAY = 2.0

DUNGEON_ENTRANCE_ROOM = "graveyard"
DUNGEON_ENTRANCE_DIR = "enter"
DUNGEON_BASE_HP = 20
DUNGEON_BASE_ATK = 4
DUNGEON_HP_GROWTH = 0.35
DUNGEON_ATK_GROWTH = 0.5
# Long-term bound: one deep-diving party must not accumulate floor objects
# (plus per-floor ITEM_DEFS registrations) without limit. At the cap the
# stairs simply crumble; guards already outscale players well before it.
DUNGEON_MAX_FLOOR = 50
# The last floor is home to a fixed boss instead of formula guards. Its
# stats are deliberately off-formula (the ×1.35/×1.5 curve would be absurd
# at floor 50) and it respawns slowly so its trophy stays prestigious.
WARDEN_HP = 350
WARDEN_ATK = 28
WARDEN_GOLD = 150
WARDEN_RESPAWN_SECONDS = 600
PARTY_MAX_MEMBERS = 4

TAX_RATE = 0.10
TAX_MINIMUM = 1

# Player-market stall slots: each seller may hold this many open orders.
# Extra slots are bought with `market_expand` for gold (fee -> treasury):
# the n-th extra slot costs MARKET_SLOT_PRICE_BASE * 2**(n-1), so expanding
# is cheap early and a real late-game gold sink.
MARKET_ORDER_SLOTS_BASE = 3
MARKET_SLOT_PRICE_BASE = 50

# Commission bounds (also overridable via server_config.json "commissions").
# XP is minted, not escrowed: 500 ~= 10x the richest quest payout (guard,
# 50 XP), so one bounty can never exceed ~an hour of top-end grinding; the
# poster's 10% coordination cut then stays <= 50 XP by construction (#189).
COMMISSION_MAX_XP = 500
# Open bounties hold real escrow and are never pruned, so an unfillable
# bounty (required_kills:999999 against a 2000-entry kill log) locks its
# escrow forever and pollutes the list. 100 kills ~= a long grinding
# session; anything above is grief-shaped, not a real bounty.
COMMISSION_MAX_KILLS = 100

XP_BASE = 100
XP_GROWTH = 1.5
LEVEL_HP_PER_LEVEL = 5
LEVEL_ATK_PER_LEVEL = 1

# ---------------------------------------------------------------------------
# Optional config file: server_config.json overrides any of the above
# constants.  Delete a key (or the whole file) to fall back to the default.
# ---------------------------------------------------------------------------
CONFIG_FILE = join(dirname(abspath(__file__)), "server_config.json")

def _apply_config():
    """Load server_config.json and override matching global constants."""
    if not os.path.isfile(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not load {CONFIG_FILE}: {e}")
        return
    g = globals()
    for section in cfg.values():
        if not isinstance(section, dict):
            continue
        for key, val in section.items():
            if key not in g:
                continue
            try:
                g[key] = type(g[key])(val)  # coerce to original type
            except (TypeError, ValueError) as e:
                print(f"Warning: {os.path.basename(CONFIG_FILE)}: "
                      f"skipping {key}={val!r} ({e}); keeping default {g[key]!r}")
    print(f"Config loaded from {os.path.basename(CONFIG_FILE)}")

_apply_config()

GM_BUFF_COST_PER_MINUTE = 50
GM_BOSS_COST_PER_STRENGTH = 100
GM_BUFF_MULT = 2.0
GM_ANNOUNCE_COST = 25
GM_HEAL_COST_PER_HP = 2
GM_TELEPORT_COST = 50
GM_SLAY_COST_PER_HP = 1
GM_SLAY_MIN_COST = 10


def xp_to_next(level):
    return round(XP_BASE * XP_GROWTH ** (level - 1))


# ---------------------------------------------------------------------------
# World data
# ---------------------------------------------------------------------------

with open(WORLD_FILE) as f:
    WORLD = json.load(f)

ROOMS = WORLD["rooms"]
ITEM_DEFS = WORLD["items"]
START_ROOM = WORLD["start_room"]

# The dungeon entrance is a dynamic exit on the graveyard (the ML env rebuilds
# its action space from these rooms on import, so it sees "enter" too).
ROOMS[DUNGEON_ENTRANCE_ROOM].setdefault("exits", {}).setdefault(DUNGEON_ENTRANCE_DIR, "dungeon_entrance")

# room_id -> list of item ids currently lying on the ground (static rooms)
room_items = {rid: list(WORLD.get("room_items", {}).get(rid, [])) for rid in ROOMS}

# Loose gold lying on the ground (death drops), room_id -> amount. Dungeon
# rooms default to 0 via .get and vanish with their instance if destroyed.
room_gold = {rid: 0 for rid in ROOMS}

# Gathering nodes have their own respawn timers and do not share ground loot
# state. This keeps safe harvesting independent from NPC respawns. They tick
# on the same npc_ai_loop as NPC enemies (NPC_TICK_SECONDS), so nodes respawn
# about as fast as npc enemies.
gather_nodes = {
    node_id: {**node, "id": node_id, "available": True, "respawn_at": None}
    for node_id, node in WORLD.get("gather_nodes", {}).items()
}

# npc_id -> live npc state (mutable copy of the template)
npcs = {}
for nid, tmpl in WORLD["npcs"].items():
    npcs[nid] = {**tmpl, "id": nid, "alive": True, "respawn_at": None, "contributors": {}}

RECIPES = WORLD.get("recipes", {})


def validate_world(data):
    """Check world.json cross-references; return a list of error strings.

    Bad node/NPC/recipe references used to surface as confusing room-wide
    runtime errors. Fail fast at startup instead, so broken data is fixed
    where it lives rather than debugged from player symptoms.
    """

    errors = []
    rooms = data.get("rooms") or {}
    items = data.get("items") or {}

    def _known_item(iid):
        # Dungeon shards register on demand when their floor is first built
        # (ITEM_DEFS.setdefault in _build_floor), so dungeon_shard_{n} ids
        # are valid before floor n exists.
        return iid in items or (
            isinstance(iid, str) and iid.startswith("dungeon_shard_")
            and iid[len("dungeon_shard_"):].isdigit())
    if not rooms:
        return ["no rooms defined"]
    start = data.get("start_room")
    if start not in rooms:
        errors.append(f"start_room {start!r} is not a room")
    for rid, room in rooms.items():
        for direction, dest in (room.get("exits") or {}).items():
            if dest not in rooms and dest != "dungeon_entrance":
                errors.append(f"room {rid!r} exit {direction!r} points at unknown room {dest!r}")
    for rid, ids in (data.get("room_items") or {}).items():
        if rid not in rooms:
            errors.append(f"room_items for unknown room {rid!r}")
        for iid in ids or []:
            if not _known_item(iid):
                errors.append(f"room {rid!r} holds unknown item {iid!r}")
    for nid, node in (data.get("gather_nodes") or {}).items():
        if node.get("room") not in rooms:
            errors.append(f"gather node {nid!r} sits in unknown room {node.get('room')!r}")
        if not _known_item(node.get("item")):
            errors.append(f"gather node {nid!r} yields unknown item {node.get('item')!r}")
    for nid, npc in (data.get("npcs") or {}).items():
        if npc.get("room") not in rooms:
            errors.append(f"npc {nid!r} sits in unknown room {npc.get('room')!r}")
    for rname, rec in (data.get("recipes") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        if not _known_item(rec.get("result")):
            errors.append(f"recipe {rname!r} produces unknown item {rec.get('result')!r}")
        for iid in rec.get("inputs") or {}:
            if not _known_item(iid):
                errors.append(f"recipe {rname!r} needs unknown item {iid!r}")
    return errors


_WORLD_ERRORS = validate_world(WORLD)
if _WORLD_ERRORS:
    print("world.json failed validation:")
    for _e in _WORLD_ERRORS:
        print(f"  - {_e}")
    sys.exit(f"Refusing to start with invalid world data ({len(_WORLD_ERRORS)} errors).")

_id_counter = itertools.count(1)

# ---------------------------------------------------------------------------
# Parties + instanced dungeons + commissions
# ---------------------------------------------------------------------------


@dataclass
class DungeonFloor:
    guards: list = field(default_factory=list)
    items: list = field(default_factory=list)
    cleared: bool = False


class Dungeon:
    """One private, infinitely-deep staircase owned by a party. Floors are
    built lazily the first time anyone walks onto them."""

    def __init__(self, party_id):
        self.id = next(_id_counter)
        self.party_id = party_id
        self.floors = {}

    def room_id(self, floor_no):
        return f"d_{self.id}_f{floor_no}"

    def floor(self, floor_no):
        floor_no = min(floor_no, DUNGEON_MAX_FLOOR)
        if floor_no not in self.floors:
            self.floors[floor_no] = self._build_floor(floor_no)
        return self.floors[floor_no]

    def _build_floor(self, floor_no):
        f = DungeonFloor()
        n = floor_no
        room_id = self.room_id(n)
        if n >= DUNGEON_MAX_FLOOR:
            # The last floor belongs to the Warden: a single fixed boss whose
            # trophy is crafted into the Warden's Blade (fixed damage, never
            # scaling). No formula guards and no scaling relics down here.
            f.guards.append({
                "id": f"dg_{self.id}_{n}_warden",
                "name": "The Warden of the Deep",
                "room": room_id,
                "hp": WARDEN_HP, "max_hp": WARDEN_HP, "attack": WARDEN_ATK,
                "hostile": True, "behavior": "idle",
                "loot": ["warden_trophy"],
                "gold": WARDEN_GOLD,
                "respawn_seconds": WARDEN_RESPAWN_SECONDS,
                "alive": True, "respawn_at": None, "contributors": {},
                "dungeon_id": self.id,
            })
            return f
        hp = round(DUNGEON_BASE_HP * (1 + DUNGEON_HP_GROWTH) ** (n - 1))
        atk = round(DUNGEON_BASE_ATK * (1 + DUNGEON_ATK_GROWTH) ** (n - 1))
        count = min(1 + (n - 1) // 2, 4)
        gold = round(3 * (1 + DUNGEON_HP_GROWTH) ** (n - 1))
        shard = f"dungeon_shard_{n}"
        # Relic items are registered on demand so loot scales with depth
        # without pre-generating thousands of floors at startup. (There is no
        # scaling blade anymore: the Warden's Blade is crafted from the
        # floor-50 Warden's trophy at a fixed damage.)
        ITEM_DEFS.setdefault(shard, {"name": f"Dungeon Relic +{2 + n * 4}", "type": "junk", "value": 2 + n * 4})
        for k in range(count):
            f.guards.append({
                "id": f"dg_{self.id}_{n}_{k}",
                "name": f"Dungeon Guard {n}-{k}",
                "room": room_id,
                "hp": hp, "max_hp": hp, "attack": atk,
                "hostile": True, "behavior": "idle",
                "loot": [shard],
                "gold": gold,
                "respawn_seconds": 20 + n * 10,
                "alive": True, "respawn_at": None, "contributors": {},
                "dungeon_id": self.id,
            })
        return f


@dataclass
class Commission:
    """Escrowed bounty posted by a player."""
    id: int
    poster: str
    target: str
    required_kills: int
    reward_gold: int
    reward_xp: int
    status: str = "open"
    created_ts: float = field(default_factory=time.time)


@dataclass
class Party:
    id: int
    leader_id: int
    member_ids: set = field(default_factory=set)
    dungeon_id: int = None


parties = {}      # party_id -> Party
dungeons = {}     # dungeon_id -> Dungeon
_commissions = {}  # commission_id -> Commission data
_party_counter = itertools.count(1)
_pending_party_invites = {}   # invitee player.id -> Party (invitation)
_commission_counter = itertools.count(1)

# Terminal (completed/cancelled) commissions older than this are pruned.
# Open bounties hold real escrow and are never pruned.
COMMISSION_TTL_SECONDS = 3600
_last_commission_prune = 0.0


def prune_commissions(now=None):
    """Drop old terminal commissions so the table can't grow forever."""
    global _last_commission_prune
    now = now if now is not None else time.time()
    if now - _last_commission_prune < 60:
        return 0
    _last_commission_prune = now
    pruned = 0
    for cid, c in list(_commissions.items()):
        if c.get("status") not in ("completed", "cancelled"):
            continue
        latest = max(c.get("created_ts", now), c.get("filled_ts", 0) or 0)
        if now - latest > COMMISSION_TTL_SECONDS:
            del _commissions[cid]
            pruned += 1
    return pruned

# Live NPC indexes (static world NPCs live in `npcs`; dungeon guards live in
# per-instance floor objects). This helper is how command/AI code sees every
# NPC in a room regardless of where it lives.
DUNGEON_ROOMS = frozenset()   # kept for ml_env compatibility (no static wings)


def dungeon_for_room(room_id):
    if isinstance(room_id, str) and room_id.startswith("d_"):
        parts = room_id.split("_")
        if len(parts) >= 3 and parts[2].startswith("f"):
            try:
                return dungeons.get(int(parts[1]))
            except ValueError:
                return None
    return None


def floor_from_room(room_id):
    if isinstance(room_id, str) and room_id.startswith("d_"):
        parts = room_id.split("_")
        if len(parts) >= 3 and parts[2].startswith("f"):
            try:
                return int(parts[2][1:])
            except ValueError:
                return None
    return None


def all_npcs():
    for npc in npcs.values():
        yield npc
    for d in dungeons.values():
        for f in d.floors.values():
            for g in f.guards:
                yield g


def npcs_in_room(room_id):
    d = dungeon_for_room(room_id)
    if d:
        f = d.floors.get(floor_from_room(room_id))
        return [g for g in (f.guards if f else []) if g["alive"]]
    return [n for n in npcs.values() if n["room"] == room_id and n["alive"]]


def _party_size_in_room(room_id):
    for p in players_in_room(room_id):
        if p.party_id and p.party_id in parties:
            return len(parties[p.party_id].member_ids)
    return 1


def _ground_items(room_id):
    d = dungeon_for_room(room_id)
    if d:
        f = d.floors.get(floor_from_room(room_id))
        return f.items if f else []
    return room_items[room_id]


def _add_ground(room_id, iid):
    d = dungeon_for_room(room_id)
    if d:
        d.floor(floor_from_room(room_id)).items.append(iid)
    else:
        room_items[room_id].append(iid)


def _remove_ground(room_id, iid):
    ground = _ground_items(room_id)
    if iid in ground:
        ground.remove(iid)


def gather_nodes_in_room(room_id):
    return [n for n in gather_nodes.values() if n["room"] == room_id and n["available"]]


# ---------------------------------------------------------------------------
# Scoring system
# ---------------------------------------------------------------------------

SCORES_BACKUP_GENERATIONS = 2  # rotated copies kept: scores.json.1 (+ .2)


def load_scores():
    # Prefer the primary file; fall back to rotated backups so one torn
    # write or corrupt save never loses the whole world (#166).
    candidates = [SCORES_FILE] + [f"{SCORES_FILE}.{i}"
                                  for i in range(1, SCORES_BACKUP_GENERATIONS + 1)]
    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if path != SCORES_FILE:
                        print(f"Warning: {os.path.basename(SCORES_FILE)} missing/corrupt, "
                              f"recovered from {os.path.basename(path)}")
                    return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return {}


def save_scores():
    # Atomic write: a kill mid-flush must never leave a truncated scores.json.
    # The previous good copy rotates aside first, so there is always a
    # fallback generation even if this write itself goes bad.
    tmp = SCORES_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(SCORES, f)
        for i in range(SCORES_BACKUP_GENERATIONS, 0, -1):
            src = SCORES_FILE if i == 1 else f"{SCORES_FILE}.{i - 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, f"{SCORES_FILE}.{i}")
                except OSError:
                    pass
        os.replace(tmp, SCORES_FILE)
    except OSError:
        pass


SCORES = load_scores()
SCORES_SAVE_SECONDS = 5.0
_scores_dirty = False

# Long-term bounds: fresh-name bot farming must not grow SCORES (and
# scores.json) without limit. Entries untouched for TTL_SECONDS are evicted;
# if the table still exceeds ENTRY_MAX, the stalest go first. Online players
# and entries owed banked gold are never evicted.
SCORE_ENTRY_MAX = 2000
SCORE_ENTRY_TTL_SECONDS = 7 * 24 * 3600
_last_score_prune = 0.0


def mark_scores_dirty():
    global _scores_dirty
    _scores_dirty = True


def prune_score_entries(now=None):
    """Evict stale score entries plus their track/score history."""
    global _last_score_prune
    now = now if now is not None else time.time()
    if now - _last_score_prune < 60:
        return 0
    _last_score_prune = now
    online = {p.name.lower() for p in players.values() if p.logged_in and p.name}
    evicted = 0
    for key in sorted(SCORES.keys(), key=lambda k: SCORES[k].get("last_seen", 0)):
        if len(SCORES) <= SCORE_ENTRY_MAX:
            entry = SCORES[key]
            if (now - entry.get("last_seen", now)) <= SCORE_ENTRY_TTL_SECONDS:
                continue
        else:
            entry = SCORES[key]
        if key in online:
            continue
        if entry.get("gold_bank", 0):
            continue
        del SCORES[key]
        for tk in [k for k in track_log if k.lower() == key]:
            del track_log[tk]
        _score_history.pop(key, None)
        evicted += 1
        mark_scores_dirty()
    return evicted


async def scores_save_loop():
    global _scores_dirty
    while True:
        await asyncio.sleep(SCORES_SAVE_SECONDS)
        prune_score_entries()
        if _scores_dirty:
            save_scores()
            _scores_dirty = False


def get_score_entry(name):
    key = name.lower()
    if key not in SCORES:
        SCORES[key] = {}
    entry = SCORES[key]
    entry["last_seen"] = time.time()
    entry.setdefault("display_name", name)
    entry.setdefault("score", 0.0)
    entry.setdefault("history", [])          # recent (cmd, arg) signatures, most-recent last
    entry.setdefault("rooms_visited", [])
    entry.setdefault("kills", 0)
    entry.setdefault("deaths", 0)
    entry.setdefault("level", 1)
    entry.setdefault("xp", 0.0)
    entry.setdefault("xp_to_next", xp_to_next(1))
    entry.setdefault("gold_bank", 0)         # coin earned while offline
    entry.setdefault("trades_completed", 0)  # player-market trades (buy+sell)
    entry.setdefault("tax_paid", 0.0)        # market tax they bore (seller side)
    entry.setdefault("market_slots", MARKET_ORDER_SLOTS_BASE)  # open sell-order cap (expandable)
    entry.setdefault("dungeon_floors_cleared", 0)
    entry.setdefault("quest_guard_active", False)
    entry.setdefault("guard_charm_crafted", False)
    entry.setdefault("quest_delver_active", False)
    entry.setdefault("quest_delver_baseline", 0)
    entry.setdefault("quest_guard_completions", 0)
    entry.setdefault("quest_delver_completions", 0)
    entry.setdefault("quest_remedy_active", False)
    entry.setdefault("quest_tonic_active", False)
    entry.setdefault("quest_remedy_completions", 0)
    entry.setdefault("quest_tonic_completions", 0)
    entry.setdefault("crafts_tier", {})
    entry.setdefault("craft_profitability", {})
    entry.setdefault("kills_by_npc", {})  # lower npc name -> [kill timestamps] for commission verification
    entry.setdefault("collab_fills", {})  # filler_name -> count (poster tracks how many times this filler collected)
    return entry


def record_action(name, signature):
    """Log one performance-relevant action for variety tracking."""
    entry = get_score_entry(name)
    hist = entry["history"]
    hist.append(list(signature))
    if len(hist) > ACTION_WINDOW:
        del hist[0]


# Cap per NPC-name kill timestamp log (commission verification window).
COMMISSION_KILL_LOG_CAP = 2000


def record_npc_kill(name, npc_name):
    """Log one killing blow for commission verification."""
    entry = get_score_entry(name)
    log = entry.setdefault("kills_by_npc", {})
    key = (npc_name or "").lower()
    tss = log.setdefault(key, [])
    tss.append(time.time())
    if len(tss) > COMMISSION_KILL_LOG_CAP:
        del tss[:-COMMISSION_KILL_LOG_CAP]
    mark_scores_dirty()


def verified_npc_kills(name, target, since_ts):
    """Kills of NPCs matching `target` (same substring rules as attacking)
    credited to `name` at or after `since_ts`."""
    entry = get_score_entry(name)
    log = entry.get("kills_by_npc", {})
    frag = (target or "").lower()
    if not frag:
        return 0
    return sum(1 for key, tss in log.items() if frag in key for ts in tss if ts >= since_ts)


def consume_npc_kills(name, target, since_ts, count):
    """Remove the oldest `count` verified kill timestamps (same matching
    as verified_npc_kills) so one kill can satisfy exactly one bounty
    fill instead of unlimited repeat fills (#189)."""
    entry = get_score_entry(name)
    log = entry.get("kills_by_npc", {})
    frag = (target or "").lower()
    if not frag or count <= 0:
        return 0
    # Oldest first across all matching NPC-name buckets.
    hits = sorted(
        (ts, key) for key, tss in log.items() if frag in key for ts in tss
        if ts >= since_ts)
    used = hits[:max(0, count)]
    for _, key in used:
        tss = log.get(key, [])
        # Remove one occurrence: the consumed timestamp itself.
        for i, ts in enumerate(tss):
            if ts >= since_ts:
                del tss[i]
                break
    if used:
        mark_scores_dirty()
    return len(used)


def collusion_multiplier(poster_name, filler_name):
    """Scale commission rewards to penalise repeated poster+filler pairs.

    First fill between two names pays full reward; each subsequent fill
    between the same pair halves the effective gold and XP.  Strangers
    always get full value, so legitimate cross-player bounties are
    unaffected.  The floor is 10 % so that even serial collaborators
    still get *something* (avoiding feel-bad zero-reward completions).
    The escrow remainder (posted gold minus reduced payout) is sunk to
    the treasury as an additional collusion deterrent."""
    # Case-normalised: score entries are keyed by lower-cased name, so
    # "Alice" and "alice" are the same economic actor. Exact-case keys
    # here would let case variants reset the collusion curve for free.
    poster = get_score_entry(poster_name)
    prev = poster.get("collab_fills", {}).get((filler_name or "").lower(), 0)
    return max(0.1, 1.0 / (1 + prev))


def compute_variety(entry):
    hist = entry["history"]
    if len(hist) < MIN_HISTORY_FOR_VARIETY:
        return 1.0
    unique = len({tuple(a) for a in hist})
    variety = unique / len(hist)
    return max(MIN_VARIETY, variety)


def compute_diminish(entry):
    return DIFFICULTY_K / (DIFFICULTY_K + entry["score"])


async def award_points_to_name(name, base_points, reason):
    """Core scoring function, keyed by character name."""
    entry = get_score_entry(name)
    variety = compute_variety(entry)
    diminish = compute_diminish(entry)
    gained = base_points * variety * diminish
    entry["score"] += gained
    record_score_point(name, entry["score"])
    mark_scores_dirty()
    for p in players_by_name.get(name.lower(), ()):
        if p.logged_in:
            await send(p, {
                "type": "score",
                "gained": round(gained, 2),
                "total": round(entry["score"], 2),
                "variety": round(variety, 2),
                "reason": reason,
            })
    return gained


async def award_points(player, base_points, reason):
    """Convenience wrapper for the common case."""
    return await award_points_to_name(player.name, base_points, reason)


async def apply_death_penalty(player, gold_lost=0):
    """Score penalty scales with wealth lost: a flat floor for broke
    characters, plus per gold removed (dropped pile + vanished)."""
    entry = get_score_entry(player.name)
    entry["deaths"] += 1
    penalty = DEATH_PENALTY + DEATH_SCORE_PER_GOLD_LOST * max(0, gold_lost)
    entry["score"] = max(0.0, entry["score"] - penalty)
    record_score_point(player.name, entry["score"])
    mark_scores_dirty()
    await send(player, {
        "type": "score",
        "gained": -round(penalty, 2),
        "total": round(entry["score"], 2),
        "variety": None,
        "reason": "died",
    })


# ---------------------------------------------------------------------------
# Buffs (GM-triggered world events)
# ---------------------------------------------------------------------------

buffs = {"xp": 0.0, "gold": 0.0}


def _buff_active(kind):
    return time.time() < buffs.get(kind, 0.0)


def _buff_mult(kind):
    return GM_BUFF_MULT if _buff_active(kind) else 1.0


def sync_player_level(player):
    """Bring a freshly-logged-in player's stats in line with persisted level."""
    if not player.name:
        return
    entry = get_score_entry(player.name)
    player.max_hp = 20 + LEVEL_HP_PER_LEVEL * (entry["level"] - 1)
    player.base_attack = 3 + LEVEL_ATK_PER_LEVEL * (entry["level"] - 1)
    player.hp = player.max_hp


async def _apply_level_up(entry, levels):
    total = sum(levels)
    for p in players_by_name.get(entry["display_name"].lower(), ()):
        if not p.logged_in:
            continue
        for new_level in levels:
            p.max_hp += LEVEL_HP_PER_LEVEL
            p.base_attack += LEVEL_ATK_PER_LEVEL
        p.hp = p.max_hp
        await send(p, {
            "type": "level_up",
            "level": entry["level"],
            "max_hp": p.max_hp,
            "attack": p.base_attack,
            "text": f"You reach level {entry['level']}! +{LEVEL_HP_PER_LEVEL} max HP, "
                    f"+{LEVEL_ATK_PER_LEVEL} attack, and you feel fully healed."
        })
        await broadcast_room(p.room, {
            "type": "message",
            "text": f"{p.name} reaches level {entry['level']}!"
        }, exclude=p)
        vlog(f"{p.name} reached level {entry['level']}")


async def award_xp(name, amount, reason):
    """Award XP (works offline). Runs the closed-form level curve."""
    if amount <= 0:
        return []
    amount *= _buff_mult("xp")
    entry = get_score_entry(name)
    entry["xp"] += amount
    leveled = []
    while entry["xp"] >= entry["xp_to_next"]:
        entry["xp"] -= entry["xp_to_next"]
        entry["level"] += 1
        entry["xp_to_next"] = xp_to_next(entry["level"])
        leveled.append(entry["level"])
    mark_scores_dirty()
    if leveled:
        await _apply_level_up(entry, leveled)
    for p in players_by_name.get(name.lower(), ()):
        if p.logged_in:
            await send(p, {
                "type": "xp",
                "gained": round(amount, 2),
                "total": round(entry["xp"], 2),
                "level": entry["level"],
                "xp_to_next": entry["xp_to_next"],
                "reason": reason,
            })
    return leveled


# ---------------------------------------------------------------------------
# Player state
# ---------------------------------------------------------------------------

def _player_buff_amount(player, category):
    value = player.active_buffs.get(category, {})
    return int(value.get("amount", 0)) if value.get("remaining", 0) > 0 else 0


def _tick_player_buffs(player):
    """Advance action-based crafted buffs once for an accepted command."""
    expired = []
    for category, value in player.active_buffs.items():
        value["remaining"] = max(0, int(value.get("remaining", 0)) - 1)
        if value["remaining"] == 0:
            expired.append(category)
    for category in expired:
        del player.active_buffs[category]


# Arrow families: a bow declaring "arrow" accepts any member, best first.
# Bonus damage per shot for the fancier variants (flat ladder).
AMMO_BONUS = {"arrow": 0, "iron_arrow": 1, "steel_arrow": 2}

# Carry cap: pack load counts every inventory unit except worn gear and up
# to AMMO_EXEMPT_COUNT arrows (quivers ride free). At the cap, take/gather/
# buy/market_buy refuse with a "pack full" error (gold is never charged on
# rejection); craft, quest/commission rewards, and GM grants always go
# through. `drop` exists solely to shed load, so it only works at the cap.
INVENTORY_CAP = 24
AMMO_EXEMPT_COUNT = 5


def _inventory_units(player):
    """Pack load in units after exemptions."""
    units = len(player.inventory)
    for slot in (player.equipped, player.armor, player.offhand):
        if slot and slot in player.inventory:
            units -= 1
    arrows = sum(1 for iid in player.inventory if iid in AMMO_BONUS)
    units -= min(arrows, AMMO_EXEMPT_COUNT)
    return max(0, units)


def _pack_full(player):
    return _inventory_units(player) >= INVENTORY_CAP


def _pack_full_error():
    return f"Your pack is full ({INVENTORY_CAP} units). Drop something first."


def _player_defense(player):
    """Damage reduction from worn gear (armor + offhand slots)."""
    total = 0
    for slot in (player.armor, player.offhand):
        if slot and slot in ITEM_DEFS:
            total += int(ITEM_DEFS[slot].get("defense", 0))
    return total


def _player_damage_reduction(player):
    """Total incoming-damage reduction: worn gear plus active buffs."""
    return _player_defense(player) + _player_buff_amount(player, "damage_reduction")


@dataclass
class Player:
    ws: object = None
    id: int = 0
    name: str = ""
    logged_in: bool = False
    hp: int = 20
    max_hp: int = 20
    base_attack: int = 3
    gold: int = 0
    inventory: list = field(default_factory=list)
    equipped: object = None   # weapon slot
    armor: object = None      # armor slot (damage reduction)
    offhand: object = None    # offhand slot (shields; small damage reduction)
    room: str = START_ROOM
    party_id: int = None
    active_buffs: dict = field(default_factory=dict)
    outbound: deque = field(default_factory=lambda: deque(maxlen=OUTBOUND_QUEUE_MAX))
    outbound_event: object = None

    def __post_init__(self):
        if self.outbound_event is None:
            try:
                self.outbound_event = asyncio.Event()
            except RuntimeError:
                self.outbound_event = None

    @property
    def attack(self):
        bonus = 0
        if self.equipped and self.equipped in ITEM_DEFS:
            bonus += ITEM_DEFS[self.equipped].get("damage", 0)
        allies = len([p for p in players_in_room(self.room) if p is not self])
        bonus += min(allies, ALLY_ATTACK_BONUS_CAP) * ALLY_ATTACK_BONUS_PER_PLAYER
        bonus += _player_buff_amount(self, "attack")
        return self.base_attack + bonus


players = {}          # pid -> Player
name_owners = {}      # lower name -> pid
room_members = {}     # room_id -> set of pid
players_by_name = {}  # lower name -> list of Player


def add_member(player):
    room_members.setdefault(player.room, set()).add(player.id)
    if player.name:
        players_by_name.setdefault(player.name.lower(), [])
        if player not in players_by_name[player.name.lower()]:
            players_by_name[player.name.lower()].append(player)


def remove_member(player):
    if player.room in room_members:
        room_members[player.room].discard(player.id)
    if player.name:
        lst = players_by_name.get(player.name.lower(), [])
        if player in lst:
            lst.remove(player)

# ---------------------------------------------------------------------------
# Market state
# ---------------------------------------------------------------------------

market_orders = []
MARKET_HISTORY_SIZE = 50
market_history = []
tax_treasury = float(os.environ.get("TEXTMMO_GM_SEED", 0) or 0)
tax_collected_lifetime = 0.0


def item_suggested_price(iid):
    return ITEM_DEFS.get(iid, {}).get("value", 1) or 1


def market_slot_price(current_slots):
    """Gold cost of the next stall slot: doubling from the base price."""
    extra = max(0, current_slots - MARKET_ORDER_SLOTS_BASE)
    return MARKET_SLOT_PRICE_BASE * (2 ** extra)


def _market_order_dict(order):
    iid = order["item"]
    return {
        "id": order["id"], "seller": order["seller"],
        "item": ITEM_DEFS.get(iid, {}).get("name", iid),
        "price": order["price"], "ts": order.get("ts", 0),
    }


def _market_trade_dict(trade):
    return dict(trade)


async def send(player, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    if hasattr(player, "outbound") and player.outbound is not None:
        try:
            if len(player.outbound) >= player.outbound.maxlen:
                player.outbound.popleft()
            player.outbound.append(payload)
            ev = getattr(player, "outbound_event", None)
            if ev is not None:
                ev.set()
            return
        except Exception:
            pass
    try:
        await player.ws.send(payload)
    except Exception:
        pass


async def _outbound_writer(player):
    while True:
        try:
            ev = player.outbound_event
            if ev is None:
                return
            if not player.outbound:
                ev.clear()
                await ev.wait()
                continue
            ev.clear()
            while player.outbound:
                msg = player.outbound.popleft()
                try:
                    await player.ws.send(msg)
                except Exception:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.1)


async def broadcast_room(room_id, payload, exclude=None):
    for p in players_in_room(room_id):
        if exclude is not None and p is exclude:
            continue
        await send(p, payload)


def players_in_room(room_id):
    out = []
    for pid in list(room_members.get(room_id, ())):
        p = players.get(pid)
        if p and p.logged_in:
            out.append(p)
    return out


def find_npc_in_room(room_id, name_fragment):
    frag = (name_fragment or "").lower()
    if not frag:
        return None
    for n in npcs_in_room(room_id):
        if frag in n["name"].lower() or frag in n["id"].lower():
            return n
    return None


def find_item_by_name(item_ids, name_fragment):
    frag = (name_fragment or "").lower()
    if not frag:
        return None
    for iid in item_ids:
        name = ITEM_DEFS.get(iid, {}).get("name", iid)
        if frag in name.lower() or frag in iid.lower():
            return iid
    return None


def find_merchant_in_room(room_id):
    for n in npcs_in_room(room_id):
        if "shop" in n:
            return n
    return None


def find_shop_item(shop, name_fragment):
    frag = (name_fragment or "").lower()
    if not frag:
        return None
    for iid in shop:
        name = ITEM_DEFS.get(iid, {}).get("name", iid)
        if frag in name.lower() or frag in iid.lower():
            return iid
    return None


def find_recipe(name_fragment):
    frag = (name_fragment or "").lower().replace(" ", "_")
    if not frag:
        return None, None
    for rid, recipe in RECIPES.items():
        result_name = ITEM_DEFS.get(recipe["result"], {}).get("name", recipe["result"])
        if frag in rid.lower() or frag in result_name.lower() or frag in recipe["result"].lower():
            return rid, recipe
    return None, None


def find_player_in_room(room_id, name_fragment):
    frag = (name_fragment or "").lower()
    if not frag:
        return None
    for p in players_in_room(room_id):
        if frag in p.name.lower():
            return p
    return None


def find_player_anywhere(name_fragment):
    frag = (name_fragment or "").lower()
    if not frag:
        return None
    for p in players.values():
        if p.logged_in and frag in p.name.lower():
            return p
    return None


def dungeon_room_view(room_id, dungeon):
    floor_no = floor_from_room(room_id)
    f = dungeon.floors.get(floor_no) if floor_no else None
    guards = [g["name"] for g in (f.guards if f else []) if g["alive"]]
    items = [ITEM_DEFS[i]["name"] for i in (f.items if f else [])]
    exits = {}
    if f and f.cleared:
        exits["up"] = "up"
        exits["down"] = "down"
    elif floor_no == 1:
        exits["up"] = "up"
    return {
        "type": "room",
        "id": room_id,
        "name": f"Dungeon Floor {floor_no}",
        "description": "A damp stone hall. Torchlight flickers.",
        "exits": exits,
        "npcs": guards,
        "items": items,
        "gold": room_gold.get(room_id, 0),
        "players": [p.name for p in players_in_room(room_id)],
        "is_dungeon": True,
        "dungeon_floor": floor_no,
        "party_size": _party_size_in_room(room_id),
    }


def room_view(room_id):
    d = dungeon_for_room(room_id)
    if d:
        return dungeon_room_view(room_id, d)
    room = ROOMS[room_id]
    return {
        "type": "room",
        "id": room_id,
        "name": room["name"],
        "description": room.get("description", ""),
        "exits": dict(room.get("exits", {})),
        "npcs": [n["name"] for n in npcs_in_room(room_id)],
        "items": [ITEM_DEFS[i]["name"] for i in room_items[room_id]],
        "gold": room_gold.get(room_id, 0),
        "players": [p.name for p in players_in_room(room_id)],
        "is_dungeon": False,
        "dungeon_floor": 0,
        "party_size": _party_size_in_room(room_id),
        "gatherables": [
            {"id": n["id"], "item": ITEM_DEFS[n["item"]]["name"],
             "min_yield": n.get("min_yield", 1), "max_yield": n.get("max_yield", 1)}
            for n in gather_nodes_in_room(room_id)
        ],
    }


def check_dungeon_clear(room_id):
    d = dungeon_for_room(room_id)
    if not d:
        return False
    floor_no = floor_from_room(room_id)
    f = d.floors.get(floor_no)
    if not f or f.cleared:
        return False
    if any(g["alive"] for g in f.guards):
        return False
    f.cleared = True
    return True


def stats_view(player):
    entry = get_score_entry(player.name) if player.name else None
    party = None
    if player.party_id and player.party_id in parties:
        party = parties[player.party_id]
    return {
        "type": "stats",
        "hp": player.hp,
        "max_hp": player.max_hp,
        "attack": player.attack,
        "gold": player.gold,
        "pack": _inventory_units(player),
        "pack_max": INVENTORY_CAP,
        "equipped": ITEM_DEFS[player.equipped]["name"] if player.equipped else None,
        "armor": ITEM_DEFS[player.armor]["name"] if player.armor else None,
        "offhand": ITEM_DEFS[player.offhand]["name"] if player.offhand else None,
        "defense": _player_defense(player),
        "score": round(entry["score"], 2) if entry else 0,
        "variety": round(compute_variety(entry), 2) if entry else 1.0,
        "level": entry["level"] if entry else 1,
        "xp": round(entry["xp"], 2) if entry else 0,
        "xp_to_next": entry["xp_to_next"] if entry else xp_to_next(1),
        "party_size": len(party.member_ids) if party else 1,
        "inv": [ITEM_DEFS[i]["name"] for i in player.inventory][:20],
        "market_orders": len(market_orders),
        "market_slots": entry.get("market_slots", MARKET_ORDER_SLOTS_BASE) if entry else MARKET_ORDER_SLOTS_BASE,
        "quest_guard_active": bool(entry.get("quest_guard_active", False)) if entry else False,
        "guard_charm_crafted": bool(entry.get("guard_charm_crafted", False)) if entry else False,
        "quest_delver_active": bool(entry.get("quest_delver_active", False)) if entry else False,
        "quest_delver_ready": bool(quest_delver_ready(entry)) if entry else False,
        "quest_remedy_active": bool(entry.get("quest_remedy_active", False)) if entry else False,
        "quest_remedy_ready": bool(entry.get("quest_remedy_active", False) and _quest_has_inputs(player, QUESTS["remedy"]["inputs"])) if entry else False,
        "quest_tonic_active": bool(entry.get("quest_tonic_active", False)) if entry else False,
        "quest_tonic_ready": bool(entry.get("quest_tonic_active", False) and _quest_has_inputs(player, QUESTS["tonic"]["inputs"])) if entry else False,
        "buffs": {
            category: {"amount": value["amount"], "remaining": value["remaining"]}
            for category, value in player.active_buffs.items()
            if value.get("remaining", 0) > 0
        },
    }


async def sync_room(room_id):
    view = room_view(room_id)
    for p in players_in_room(room_id):
        await send(p, view)


async def respawn_player(player):
    # Death drop: 40% of carried gold stays as a floor pile where you died,
    # 10% vanishes permanently, the rest stays with you.
    dropped = (player.gold * DEATH_GOLD_DROP_PCT) // 100
    lost = (player.gold * DEATH_GOLD_LOST_PCT) // 100
    player.gold -= (dropped + lost)
    await apply_death_penalty(player, dropped + lost)
    death_room = player.room
    if dropped > 0:
        room_gold[death_room] = room_gold.get(death_room, 0) + dropped
    player.hp = player.max_hp
    remove_member(player)
    player.room = START_ROOM
    add_member(player)
    text = "You died and wake up back in Town Square."
    if dropped > 0 or lost > 0:
        text += f" You dropped {dropped} gold where you fell and lost {lost} gold outright."
    await send(player, {"type": "death", "text": text})
    await send(player, room_view(player.room))
    await send(player, stats_view(player))
    if death_room != player.room:
        await sync_room(death_room)


def respawn_npc(npc):
    """Bring an NPC back to life. Dungeon guard respawn re-seals the floor."""
    npc["alive"] = True
    npc["hp"] = npc["max_hp"]
    npc["respawn_at"] = None
    npc["contributors"] = {}
    rid = npc["room"]
    d = dungeon_for_room(rid)
    if d:
        floor_no = floor_from_room(rid)
        f = d.floors.get(floor_no)
        if f and f.cleared:
            f.cleared = False


async def credit_gold(name, amount):
    entry = get_score_entry(name)
    found = False
    for p in players_by_name.get(name.lower(), ()):
        if p.logged_in:
            p.gold += amount
            await send(p, stats_view(p))
            found = True
    if not found:
        entry["gold_bank"] = entry.get("gold_bank", 0) + amount
    mark_scores_dirty()


def _auto_create_party(player):
    if player.party_id and player.party_id in parties:
        return parties[player.party_id]
    pid = next(_party_counter)
    party = Party(id=pid, leader_id=player.id, member_ids={player.id})
    parties[pid] = party
    player.party_id = pid
    return party


def _delete_party(party):
    if party.dungeon_id and party.dungeon_id in dungeons:
        del dungeons[party.dungeon_id]
    for mid in list(party.member_ids):
        p = players.get(mid)
        if p and p.party_id == party.id:
            p.party_id = None
    if party.id in parties:
        del parties[party.id]


def _relocate_from_dungeon(player):
    if dungeon_for_room(player.room):
        remove_member(player)
        player.room = DUNGEON_ENTRANCE_ROOM
        add_member(player)


def _dungeon_move(player, dungeon, floor_no):
    remove_member(player)
    player.room = dungeon.room_id(floor_no)
    dungeon.floor(floor_no)
    add_member(player)


async def _enter_dungeon(player):
    party = _auto_create_party(player)
    if party.dungeon_id not in dungeons:
        d = Dungeon(party_id=party.id)
        dungeons[d.id] = d
        party.dungeon_id = d.id
    else:
        d = dungeons[party.dungeon_id]
    # Party members enter together: bring everyone already in the party who
    # is standing at the entrance.
    for mid in list(party.member_ids):
        m = players.get(mid)
        if m and m.room == DUNGEON_ENTRANCE_ROOM and m is not player:
            remove_member(m)
            m.room = d.room_id(1)
            d.floor(1)
            add_member(m)
            await send(m, room_view(m.room))
            await send(m, stats_view(m))
    _dungeon_move(player, d, 1)
    await send(player, room_view(player.room))
    await send(player, stats_view(player))


async def _dungeon_move_or_fail(player, d, direction):
    floor_no = floor_from_room(player.room)
    f = d.floors.get(floor_no)
    if direction == "up":
        if floor_no == 1 or (f and f.cleared):
            if floor_no == 1:
                remove_member(player)
                player.room = DUNGEON_ENTRANCE_ROOM
                add_member(player)
            else:
                _dungeon_move(player, d, floor_no - 1)
            await send(player, room_view(player.room))
            await send(player, stats_view(player))
        else:
            await send(player, {"type": "error", "text": "The exit is sealed. Clear every guard on this floor first."})
        return
    if direction == "down":
        if f and f.cleared:
            if floor_no + 1 > DUNGEON_MAX_FLOOR:
                await send(player, {"type": "error", "text": "The stairs below have crumbled into darkness. This is as deep as anyone can go."})
            else:
                _dungeon_move(player, d, floor_no + 1)
                await send(player, room_view(player.room))
                await send(player, stats_view(player))
        else:
            await send(player, {"type": "error", "text": "The exit is sealed. Clear every guard on this floor first."})
        return
    await send(player, {"type": "error", "text": f"You can't go '{direction}' from here."})


def _party_member_players(party):
    out = []
    for mid in party.member_ids:
        p = players.get(mid)
        if p:
            out.append(p)
    return out


async def _notify_party(party, text):
    for p in _party_member_players(party):
        if p.logged_in:
            await send(p, {"type": "message", "text": text})

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_login(player, msg):
    name = (msg.get("name") or "").strip()
    if not name:
        await send(player, {"type": "error", "text": "login requires a 'name'"})
        return
    if player.logged_in:
        await send(player, {"type": "error", "text": f"Already logged in as {player.name}. Use a fresh connection to switch."})
        return
    token = msg.get("token")
    if name_owners.get(name.lower()) not in (None, player.id):
        await send(player, {"type": "error", "text": f"The name '{name}' is already in use right now."})
        return
    entry = get_score_entry(name)
    stored = entry.get("auth_token")
    if stored:
        if token != stored:
            await send(player, {"type": "error", "text": f"The name '{name}' is protected by a token. Login rejected."})
            return
    else:
        if AUTH_TOKEN_REQUIRED and not token:
            await send(player, {"type": "error", "text": "This server requires a login token."})
            return
        if token:
            entry["auth_token"] = token
            mark_scores_dirty()
    if entry.get("gold_bank", 0):
        player.gold += entry["gold_bank"]
        entry["gold_bank"] = 0
        mark_scores_dirty()
    player.name = name
    player.logged_in = True
    player.room = START_ROOM if dungeon_for_room(player.room) else player.room
    if player.room not in entry["rooms_visited"]:
        entry["rooms_visited"].append(player.room)
    sync_player_level(player)
    name_owners[name.lower()] = player.id
    add_member(player)
    lvl = entry["level"]
    await send(player, {"type": "welcome", "text": f"Welcome, {name} (level {lvl}).",
                        "protocol_version": PROTOCOL_VERSION,
                        "client_version": msg.get("protocol_version")})
    await send(player, room_view(player.room))
    await send(player, stats_view(player))
    await broadcast_room(player.room, {"type": "message", "text": f"{name} appears."}, exclude=player)


async def cmd_look(player, msg):
    await send(player, room_view(player.room))


async def cmd_move(player, msg):
    direction = (msg.get("dir") or "").lower()
    if direction == DUNGEON_ENTRANCE_DIR and player.room == DUNGEON_ENTRANCE_ROOM:
        await _enter_dungeon(player)
        return
    d = dungeon_for_room(player.room)
    if d:
        await _dungeon_move_or_fail(player, d, direction)
        return
    exits = ROOMS[player.room]["exits"]
    if direction not in exits:
        await send(player, {"type": "error", "text": f"You can't go '{direction}' from here."})
        return
    dest = exits[direction]
    if dest not in ROOMS:
        await send(player, {"type": "error", "text": f"You can't go '{direction}' from here."})
        return
    # Room capacity
    if len(players_in_room(dest)) >= MAX_PLAYERS_PER_ROOM:
        await send(player, {"type": "error", "text": f"{ROOMS[dest]['name']} is too crowded."})
        return
    remove_member(player)
    await broadcast_room(player.room, {"type": "message", "text": f"{player.name} leaves."}, exclude=player)
    player.room = dest
    add_member(player)
    entry = get_score_entry(player.name)
    if player.room not in entry["rooms_visited"]:
        entry["rooms_visited"].append(player.room)
        await award_points(player, 5, f"discovered {ROOMS[player.room]['name']}")
        await award_xp(player.name, 5, f"discovered {ROOMS[player.room]['name']}")
    await send(player, room_view(player.room))
    await send(player, stats_view(player))
    await broadcast_room(player.room, {"type": "message", "text": f"{player.name} arrives."}, exclude=player)


async def cmd_attack(player, msg):
    target_name = msg.get("target", "")
    npc = find_npc_in_room(player.room, target_name)
    if not npc:
        await send(player, {"type": "error", "text": f"No '{target_name}' here to attack."})
        return
    # Ranged weapons declare their ammo family root (e.g. Oak Longbow
    # needs "arrow"). Any family member fires, best variant first, adding
    # its flat bonus damage to the shot.
    ammo_id = ITEM_DEFS.get(player.equipped, {}).get("ammo") if player.equipped else None
    ammo_bonus = 0
    if ammo_id:
        family = [iid for iid in AMMO_BONUS if iid in player.inventory] if ammo_id in AMMO_BONUS else ([ammo_id] if ammo_id in player.inventory else [])
        if not family:
            await send(player, {"type": "error", "text": f"You need {ITEM_DEFS[ammo_id]['name']}s to fire the {ITEM_DEFS[player.equipped]['name']}."})
            return
        best = max(family, key=lambda iid: AMMO_BONUS.get(iid, 0))
        player.inventory.remove(best)
        ammo_bonus = AMMO_BONUS.get(best, 0)
    dmg = random.randint(1, player.attack) + ammo_bonus
    npc["hp"] -= dmg
    npc["contributors"][player.name] = npc["contributors"].get(player.name, 0) + dmg
    await send(player, {"type": "combat", "text": f"You hit {npc['name']} for {dmg}."})
    await broadcast_room(player.room, {"type": "combat", "text": f"{player.name} hits {npc['name']} for {dmg}."}, exclude=player)
    if npc["hp"] <= 0:
        npc["alive"] = False
        respawn_secs = npc.get("respawn_seconds", 30)
        npc["respawn_at"] = time.time() + respawn_secs if respawn_secs else None
        for loot_id in npc.get("loot", []):
            _add_ground(player.room, loot_id)
        contributors = npc["contributors"]
        total_dmg = sum(contributors.values()) or 1
        num_contributors = len(contributors)
        teamwork_multiplier = 1.0 + TEAMWORK_BONUS_PER_EXTRA_CONTRIBUTOR * min(
            num_contributors - 1, TEAMWORK_BONUS_CAP_CONTRIBUTORS
        )
        pool = (npc["max_hp"] * 0.5 + npc["attack"] * 3) * teamwork_multiplier
        gold_share = round(npc.get("gold", 0) * _buff_mult("gold") / max(1, num_contributors))
        for cname, dmg_dealt in contributors.items():
            share = dmg_dealt / total_dmg
            pts = pool * share
            xp = pts
            # Registered quest-giving NPCs penalize: killing them is never
            # worth it.  Score goes negative; XP and gold stay at zero.
            is_quest_npc = is_quest_giver(npc["id"])
            if is_quest_npc:
                pts = -0.5
                xp = 0
                gold_share = 0
                reason = f"defeated {npc['name']} (quest NPC - penalty)"
            elif cname == player.name:
                reason = f"defeated {npc['name']}"
                get_score_entry(cname)["kills"] += 1
                record_npc_kill(cname, npc["name"])
                if player.hp <= player.max_hp * 0.3:
                    pts *= 1.5
                    xp *= 1.5
                    reason += " (narrow victory)"
            else:
                reason = f"helped defeat {npc['name']}"
            await award_points_to_name(cname, pts, reason)
            await award_xp(cname, xp, reason)
        team_note = "" if num_contributors <= 1 else f" ({num_contributors} contributors share the credit)"
        await broadcast_room(player.room, {
            "type": "combat",
            "text": f"{npc['name']} dies! Loot drops on the ground.{team_note}"
        })
        for cname in contributors:
            for p in players_by_name.get(cname.lower(), ()):
                if p.logged_in:
                    p.gold += gold_share
                    await send(p, stats_view(p))
        if str(npc["id"]).startswith("boss_"):
            npcs.pop(npc["id"], None)
        npc["contributors"] = {}
        await sync_room(player.room)
        if check_dungeon_clear(player.room):
            floor_no = floor_from_room(player.room)
            clear_pts = 15 + 5 * floor_no
            clear_xp = 20 + 15 * floor_no
            for p in players_in_room(player.room):
                get_score_entry(p.name)["dungeon_floors_cleared"] += 1
                await award_points(p, clear_pts, f"cleared Dungeon Floor {floor_no}")
                await award_xp(p.name, clear_xp, f"cleared Dungeon Floor {floor_no}")
            await broadcast_room(player.room, {
                "type": "message",
                "text": "The hall falls silent. The sealed exits grind open, revealing the way onward and a gleaming blade."
            })
            await sync_room(player.room)
    else:
        if npc["attack"] > 0:
            retaliation = random.randint(1, npc["attack"])
            retaliation = max(0, retaliation - _player_damage_reduction(player))
            player.hp -= retaliation
            await send(player, {"type": "combat", "text": f"{npc['name']} hits you for {retaliation}."})
            if player.hp <= 0:
                await respawn_player(player)
            else:
                await send(player, stats_view(player))


async def cmd_take(player, msg):
    item_name = msg.get("item", "")
    # Loose gold piles (death drops) are picked up by name, optionally with
    # an amount: {"cmd": "take", "item": "gold"} takes all,
    # {"cmd": "take", "item": "gold", "amount": 5} takes up to 5.
    if str(item_name or "").strip().lower() in ("gold", "gold pile", "coins"):
        pile = room_gold.get(player.room, 0)
        if pile <= 0:
            await send(player, {"type": "error", "text": "No loose gold here to take."})
            return
        try:
            want = int(msg.get("amount", pile))
        except (TypeError, ValueError):
            want = pile
        take = max(0, min(pile, want))
        if take <= 0:
            await send(player, {"type": "error", "text": "No loose gold here to take."})
            return
        room_gold[player.room] = pile - take
        player.gold += take
        await send(player, {"type": "message", "text": f"You pick up {take} gold."})
        await send(player, stats_view(player))
        await sync_room(player.room)
        return
    iid = find_item_by_name(_ground_items(player.room), item_name)
    if not iid:
        await send(player, {"type": "error", "text": f"No '{item_name}' here to take."})
        return
    if _pack_full(player):
        await send(player, {"type": "error", "text": _pack_full_error()})
        return
    _remove_ground(player.room, iid)
    player.inventory.append(iid)
    await send(player, {"type": "message", "text": f"You take {ITEM_DEFS[iid]['name']}."})
    await send(player, stats_view(player))
    await sync_room(player.room)


async def cmd_drop(player, msg):
    """Shed load onto the ground — but only when the pack is actually full.
    Drop exists solely to make room, so below the cap it refuses."""
    if not _pack_full(player):
        await send(player, {"type": "error", "text": f"Your pack isn't full — drop is only for making room ({_inventory_units(player)}/{INVENTORY_CAP} units)."})
        return
    iid = find_item_by_name(player.inventory, msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    try:
        amount = int(msg.get("amount", 1))
    except (TypeError, ValueError):
        amount = 1
    dropped = 0
    for _ in range(max(1, amount)):
        if iid not in player.inventory:
            break
        player.inventory.remove(iid)
        dropped += 1
    if dropped <= 0:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    if player.equipped == iid and iid not in player.inventory:
        player.equipped = None
    if player.armor == iid and iid not in player.inventory:
        player.armor = None
    if player.offhand == iid and iid not in player.inventory:
        player.offhand = None
    for _ in range(dropped):
        _add_ground(player.room, iid)
    await send(player, {"type": "message", "text": f"You drop {dropped}x {ITEM_DEFS[iid]['name']}."})
    await send(player, stats_view(player))
    await sync_room(player.room)


async def cmd_gather(player, msg):
    """Harvest one available non-combat node in the current surface room."""
    requested = str(msg.get("node") or msg.get("item") or "").strip().lower()
    cands = gather_nodes_in_room(player.room)
    node = None
    if requested:
        node = next((n for n in cands
                     if requested in (n["id"].lower(), n["item"].lower(),
                                      ITEM_DEFS[n["item"]]["name"].lower())), None)
    else:
        node = cands[0] if cands else None
    if not node:
        await send(player, {"type": "error", "text": "No available gathering node matches that here."})
        return
    if _pack_full(player):
        await send(player, {"type": "error", "text": _pack_full_error()})
        return
    node["available"] = False
    node["respawn_at"] = time.time() + float(node.get("respawn_seconds", 30))
    quantity = random.randint(int(node.get("min_yield", 1)), int(node.get("max_yield", 1)))
    player.inventory.extend([node["item"]] * quantity)
    item_name = ITEM_DEFS[node["item"]]["name"]
    await send(player, {"type": "message", "text": f"You gather {quantity}x {item_name}."})
    await award_points(player, float(node.get("score", 2)), f"gathered {item_name}")
    await award_xp(player.name, float(node.get("xp", 2)), f"gathered {item_name}")
    await send(player, stats_view(player))
    await sync_room(player.room)


async def cmd_equip(player, msg):
    iid = find_item_by_name(player.inventory, msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    itype = ITEM_DEFS[iid].get("type")
    if itype == "weapon":
        player.equipped = iid
    elif itype == "armor":
        player.armor = iid
    elif itype == "offhand":
        player.offhand = iid
    else:
        await send(player, {"type": "error", "text": f"You can't equip '{ITEM_DEFS[iid]['name']}'."})
        return
    await send(player, {"type": "message", "text": f"You equip {ITEM_DEFS[iid]['name']}."})
    await send(player, stats_view(player))


async def cmd_use(player, msg):
    iid = find_item_by_name(player.inventory, msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    if not iid or ITEM_DEFS[iid].get("type") != "consumable":
        await send(player, {"type": "error", "text": f"You can't use '{msg.get('item', '')}'."})
        return
    player.inventory.remove(iid)
    definition = ITEM_DEFS[iid]
    effect = definition.get("buff")
    if effect:
        category = effect["category"]
        player.active_buffs[category] = {
            "amount": int(effect.get("amount", 0)),
            "remaining": int(effect.get("duration_actions", 1)),
        }
        await send(player, {
            "type": "message",
            "text": f"You use {definition['name']}: {effect.get('description', 'a temporary effect')} "
                    f"({player.active_buffs[category]['remaining']} actions).",
        })
        await send(player, stats_view(player))
        return
    heal = definition.get("heal_amount", 0)
    before = player.hp
    player.hp = min(player.max_hp, player.hp + heal)
    await send(player, {"type": "message", "text": f"You use {definition['name']} and recover {player.hp - before} HP."})
    await send(player, stats_view(player))


REST_COST = 2

async def cmd_rest(player, msg):
    """Recover 5 HP, but only in rooms designed for it, for a small fee."""
    if not ROOMS.get(player.room, {}).get("rest_area"):
        await send(player, {"type": "error", "text": "You can't rest here. Find a rest area (Town Square, Market, Healing Spring, Lake Shrine)."})
        return
    if player.hp >= player.max_hp:
        await send(player, {"type": "message", "text": "You are already fully rested."})
        return
    if player.gold < REST_COST:
        await send(player, {"type": "error", "text": f"Resting costs {REST_COST} gold."})
        return
    player.gold -= REST_COST
    player.hp = min(player.max_hp, player.hp + 5)
    await send(player, {"type": "message", "text": f"You rest and recover 5 HP ({REST_COST} gold)."})
    await send(player, stats_view(player))


HEAL_COST = 5

async def cmd_heal(player, msg):
    """Full heal for a fee, but only on the same tile as Sister Maren."""
    healer = find_npc_in_room(player.room, "healer")
    if not healer:
        await send(player, {"type": "error", "text": "No healer here. Sister Maren tends the wounded at the Healing Spring."})
        return
    if player.hp >= player.max_hp:
        await send(player, {"type": "message", "text": "Sister Maren smiles: you are already whole."})
        return
    if player.gold < HEAL_COST:
        await send(player, {"type": "error", "text": f"Sister Maren's healing costs {HEAL_COST} gold."})
        return
    player.gold -= HEAL_COST
    player.hp = player.max_hp
    await send(player, {"type": "message", "text": f"Sister Maren lays hands on you. You feel fully healed ({HEAL_COST} gold)."})
    await send(player, stats_view(player))


async def cmd_buy(player, msg):
    merchant = find_merchant_in_room(player.room)
    if not merchant:
        # Buys require standing by a merchant; no remote or market-room sale.
        await send(player, {"type": "error", "text": "No merchant here."})
        return
    iid = find_shop_item(merchant["shop"], msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "The merchant doesn't sell that."})
        return
    price = merchant["shop"][iid]
    if player.gold < price:
        await send(player, {"type": "error", "text": f"You need {price} gold."})
        return
    if _pack_full(player):
        await send(player, {"type": "error", "text": _pack_full_error()})
        return
    player.gold -= price
    player.inventory.append(iid)
    await send(player, {"type": "message", "text": f"You buy {ITEM_DEFS[iid]['name']} for {price} gold."})
    await send(player, stats_view(player))


async def cmd_sell(player, msg):
    iid = find_item_by_name(player.inventory, msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    merchant = find_merchant_in_room(player.room)
    if not merchant:
        await send(player, {"type": "error", "text": "No merchant here."})
        return
    value = ITEM_DEFS.get(iid, {}).get("value", 1)
    player.inventory.remove(iid)
    if player.equipped == iid:
        player.equipped = None
    if player.armor == iid:
        player.armor = None
    if player.offhand == iid:
        player.offhand = None
    player.gold += value
    await send(player, {"type": "message", "text": f"You sell {ITEM_DEFS[iid]['name']} for {value} gold."})
    await send(player, stats_view(player))


async def cmd_craft(player, msg):
    rid, recipe = find_recipe(msg.get("recipe", ""))
    if not recipe:
        await send(player, {"type": "error", "text": "No such recipe."})
        return
    have = {iid: player.inventory.count(iid) for iid in set(player.inventory)}
    for iid, qty in recipe["inputs"].items():
        if have.get(iid, 0) < qty:
            # .get fallback: dynamic ids (e.g. dungeon_shard_10 pre-Floor-10)
            # aren't in ITEM_DEFS yet -- name the id instead of KeyError.
            need = ITEM_DEFS.get(iid, {}).get("name", iid)
            await send(player, {"type": "error", "text": f"You need {qty}x {need} to craft that."})
            return
    for iid, qty in recipe["inputs"].items():
        for _ in range(qty):
            player.inventory.remove(iid)
    result = recipe["result"]
    try:
        output_qty = max(1, int(recipe.get("output_qty", 1)))
    except (TypeError, ValueError):
        output_qty = 1
    player.inventory.extend([result] * output_qty)
    result_name = ITEM_DEFS.get(result, {}).get("name", result)
    output_text = f"{output_qty}x {result_name}" if output_qty > 1 else result_name
    await send(player, {"type": "message", "text": f"You craft {output_text}!"})
    entry = get_score_entry(player.name)
    input_value = sum(ITEM_DEFS.get(iid, {}).get("value", 0) * qty for iid, qty in recipe["inputs"].items())
    output_value = ITEM_DEFS.get(result, {}).get("value", 0) * output_qty
    net_profit = output_value - input_value
    entry.setdefault("craft_profitability", {})[recipe["result"]] = entry["craft_profitability"].get(recipe["result"], 0) + net_profit
    entry.setdefault("crafts_tier", {}).setdefault(recipe.get("tier", 0), 0)
    entry["crafts_tier"][recipe.get("tier", 0)] += 1
    mark_scores_dirty()
    await award_points(player, 8, f"crafted {ITEM_DEFS[result]['name']}")
    await award_xp(player.name, 8, f"crafted {ITEM_DEFS[result]['name']}")
    if entry["quest_guard_active"] and result == "ancient_guardian_charm":
        entry["guard_charm_crafted"] = True
        mark_scores_dirty()
        await send(player, {"type": "message", "text": "The Ancient Guardian Charm feels warm in your hands... "
              "perhaps the Town Guard will find it useful?"})
    await send(player, stats_view(player))


async def cmd_commission_post(player, msg):
    target = (msg.get("target") or "").strip().lower()
    try:
        required_kills = int(msg.get("required_kills") or 0)
    except (TypeError, ValueError):
        required_kills = 0
    try:
        reward_gold = int(msg.get("reward_gold") or 0)
    except (TypeError, ValueError):
        reward_gold = 0
    try:
        reward_xp = int(msg.get("reward_xp") or 0)
    except (TypeError, ValueError):
        reward_xp = 0
    if not target:
        # ML env posts with no args; default to a simple rat bounty.
        target = "rat"
        required_kills = required_kills or 1
    if required_kills <= 0:
        required_kills = 1
    # Unfillable bounties lock escrow forever (open listings are never
    # pruned), so reject kill counts no session could realistically reach.
    if required_kills > COMMISSION_MAX_KILLS:
        await send(player, {"type": "error", "text": f"Bounties are capped at {COMMISSION_MAX_KILLS} kills (asked {required_kills}). Split it into smaller bounties."})
        return
    if reward_gold < 0 or reward_xp < 0:
        await send(player, {"type": "error", "text": "Rewards cannot be negative."})
        return
    # XP is minted, not escrowed: cap per-bounty XP so posters can't print
    # arbitrary amounts for fillers (and their own 10% cut) to harvest.
    if reward_xp > COMMISSION_MAX_XP:
        await send(player, {"type": "error", "text": f"XP reward capped at {COMMISSION_MAX_XP} per bounty (asked {reward_xp})."})
        return
    # True escrow: the poster locks the gold up front. Posting what you
    # cannot cover is rejected instead of minting gold at fill time.
    if player.gold < reward_gold:
        await send(player, {"type": "error", "text": f"You need {reward_gold} gold to escrow that bounty (you have {player.gold})."})
        return
    player.gold -= reward_gold
    cid = next(_commission_counter)
    commission = {
        "id": cid, "poster": player.name, "target": target,
        "required_kills": required_kills, "reward_gold": reward_gold,
        "reward_xp": reward_xp, "escrow": reward_gold,
        "status": "open", "created_ts": time.time(),
    }
    _commissions[cid] = commission
    mark_scores_dirty()
    await send(player, {"type": "message", "text": f"Commission #{cid} posted: slay {required_kills}x {target} for {reward_gold}g + {reward_xp}xp ({reward_gold}g held in escrow)."})
    await broadcast_room(player.room, {"type": "message", "text": f"{player.name} posted commission #{cid}."}, exclude=player)
    await send(player, stats_view(player))


async def cmd_commission_list(player, msg):
    open_cmds = [c for c in _commissions.values() if c["status"] == "open"]
    if not open_cmds:
        await send(player, {"type": "message", "text": "No open commissions right now."})
        await send(player, stats_view(player))
        return
    lines = []
    for c in open_cmds:
        mult = collusion_multiplier(c["poster"], player.name)
        eg = max(1, int(c["reward_gold"] * mult))
        ex = max(0, int(c["reward_xp"] * mult))
        tag = "" if mult >= 1.0 else f" (your rate: x{mult:.1f})"
        lines.append(f"#{c['id']}: slay {c['required_kills']}x {c['target']} — reward {eg}g + {ex}xp{tag} (posted by {c['poster']})")
    await send(player, {"type": "message", "text": "Open commissions:\n" + "\n".join(lines)})
    await send(player, stats_view(player))


async def cmd_commission_fill(player, msg):
    global tax_treasury, tax_collected_lifetime
    cid_raw = msg.get("commission_id", msg.get("id", ""))
    try:
        cid = int(str(cid_raw).strip())
    except (TypeError, ValueError):
        # ML env fills with no args: take the first open commission.
        open_cmds = [c for c in _commissions.values() if c["status"] == "open"]
        if not open_cmds:
            await send(player, {"type": "error", "text": "No open commissions to fill."})
            return
        cid = open_cmds[0]["id"]
    commission = _commissions.get(cid)
    if not commission:
        await send(player, {"type": "error", "text": f"Commission #{cid} not found."})
        return
    if commission["status"] != "open":
        await send(player, {"type": "error", "text": f"Commission #{cid} is already {commission['status']}."})
        return
    # No self-dealing: filling your own bounty would mint score for nothing.
    # Compared case-insensitively -- score entries (kills, collab history)
    # are shared across case variants, so "Alice" filling "alice"'s bounty
    # is the same actor paying itself.
    if commission["poster"].lower() == player.name.lower():
        await send(player, {"type": "error", "text": f"You cannot fill your own commission #{cid}."})
        return
    # No free payouts: the filler must have slain the required kills of the
    # target since this commission was posted (verified from kill timestamps).
    have = verified_npc_kills(player.name, commission["target"], commission["created_ts"])
    if have < commission["required_kills"]:
        await send(player, {"type": "error", "text": f"Commission #{cid} needs {commission['required_kills']}x {commission['target']} slain since posting ({have} verified)."})
        return
    # Consume the kills this fill uses: without this, one kill's timestamps
    # satisfy unlimited repeat fills of matching bounties.
    consume_npc_kills(player.name, commission["target"], commission["created_ts"],
                      commission["required_kills"])
    # Anti-collusion: repeated poster+filler pairs earn diminishing rewards.
    # Any escrow remainder (posted gold minus reduced payout) is sunk to the
    # treasury as an additional collusion deterrent.
    mult = collusion_multiplier(commission["poster"], player.name)
    # The max(1, ...) floors keep collusion-discounted payouts from
    # feel-bad zeroing -- but ONLY when the bounty actually offers that
    # reward. A 0g/0xp bounty must pay 0, not mint 1g/1xp from nothing
    # while its message claims a cut of a bounty that never existed.
    offered_gold = commission["reward_gold"]
    offered_xp = commission["reward_xp"]
    eff_gold = max(1, int(offered_gold * mult)) if offered_gold > 0 else 0
    eff_xp = max(0, int(offered_xp * mult))
    commission["status"] = "filled"
    commission["filled_by"] = player.name
    commission["filled_ts"] = time.time()
    poster_entry = get_score_entry(commission["poster"])
    collab = poster_entry.setdefault("collab_fills", {})
    filler_key = player.name.lower()
    collab[filler_key] = collab.get(filler_key, 0) + 1
    mark_scores_dirty()
    collab_note = "" if mult >= 1.0 else f" (collab penalty x{mult:.1f})"
    await send(player, {"type": "message", "text": f"You completed commission #{cid}: +{eff_gold}g, +{eff_xp}xp.{collab_note}"})
    await award_points(player, eff_xp + eff_gold, f"completed commission #{cid}")
    await award_xp(player.name, eff_xp, f"completed commission #{cid}")
    # Escrow remainder (posted gold minus reduced payout) is sunk to the
    # treasury as documented -- previously it sat on the completed record
    # forever and never arrived.
    escrow = commission.get("escrow", offered_gold)
    player.gold += min(escrow, eff_gold)
    remainder = max(0, escrow - eff_gold)
    commission["escrow"] = 0
    if remainder:
        tax_treasury += remainder
        tax_collected_lifetime += remainder
    commission["status"] = "completed"
    # Poster reward: 10% of the bounty as score + XP for coordinating.
    # Gated like the filler floors: no mint from a zero bounty.
    poster_score = max(1, int(offered_gold * 0.1)) if offered_gold > 0 else 0
    poster_xp = max(1, int(offered_xp * 0.1)) if offered_xp > 0 else 0
    poster_name = commission["poster"]
    await award_points_to_name(poster_name, poster_score, f"commission #{cid} filled by {player.name}")
    await award_xp(poster_name, poster_xp, f"commission #{cid} filled by {player.name}")
    for pp in players_by_name.get(poster_name.lower(), ()):
        if pp.logged_in:
            treasury_note = f" {remainder}g collusion remainder sunk to treasury." if remainder else ""
            await send(pp, {"type": "message", "text": f"Your commission #{cid} was filled by {player.name}! +{poster_score} score, +{poster_xp}xp.{treasury_note}"})
    mark_scores_dirty()
    await send(player, stats_view(player))


async def cmd_commission_cancel(player, msg):
    global tax_treasury, tax_collected_lifetime
    cid_raw = msg.get("commission_id", msg.get("id", ""))
    try:
        cid = int(str(cid_raw).strip())
    except (TypeError, ValueError):
        await send(player, {"type": "error", "text": "commission_cancel needs a 'commission_id'."})
        return
    commission = _commissions.get(cid)
    if not commission:
        await send(player, {"type": "error", "text": f"Commission #{cid} not found."})
        return
    if commission["status"] != "open":
        await send(player, {"type": "error", "text": f"Commission #{cid} is already {commission['status']} and cannot be cancelled."})
        return
    # Only the poster may cancel: otherwise anyone could grief bounties and
    # force the poster to forfeit half their escrow for nothing.
    # Case-insensitive (same shared-identity reason as the fill check).
    if commission["poster"].lower() != player.name.lower():
        await send(player, {"type": "error", "text": f"Only {commission['poster']} can cancel commission #{cid}."})
        return
    commission["status"] = "cancelled"
    # Refund half of the actually-escrowed gold (credit_gold pays live
    # characters directly and banks it for offline ones). The forfeited
    # half is the cancellation fee: sink it to the treasury instead of
    # leaving it on the dead record, where it silently left the economy.
    escrow = commission.get("escrow", commission["reward_gold"])
    refund = escrow // 2
    forfeit = escrow - refund
    commission["escrow"] = 0
    if forfeit:
        tax_treasury += forfeit
        tax_collected_lifetime += forfeit
    await credit_gold(commission["poster"], refund)
    mark_scores_dirty()
    await send(player, {"type": "message", "text": f"Commission #{cid} cancelled. Half the escrow ({refund}g) returned; {forfeit}g forfeited to the treasury."})
    await send(player, stats_view(player))


async def cmd_inventory(player, msg):
    await send(player, {
        "type": "inventory",
        "items": [ITEM_DEFS[i]["name"] for i in player.inventory],
        "equipped": ITEM_DEFS[player.equipped]["name"] if player.equipped else None,
        "armor": ITEM_DEFS[player.armor]["name"] if player.armor else None,
        "offhand": ITEM_DEFS[player.offhand]["name"] if player.offhand else None,
    })


async def cmd_stats(player, msg):
    await send(player, stats_view(player))


async def cmd_who(player, msg):
    online = sorted([p.name for p in players.values() if p.logged_in])
    await send(player, {"type": "who", "players": online})


async def cmd_leaderboard(player, msg):
    scored = sorted(SCORES.values(), key=lambda e: e.get("score", 0), reverse=True)[:10]
    await send(player, {"type": "leaderboard", "entries": [
        {"name": e.get("display_name"), "score": round(e.get("score", 0), 2), "level": e.get("level", 1)}
        for e in scored
    ]})


async def cmd_help(player, msg):
    await send(player, {"type": "help", "text": (
        "Commands: login look move attack take gather drop equip use rest heal buy sell craft "
        "commission_post commission_list commission_fill commission_cancel "
        "inventory stats who leaderboard help party_invite party_accept party_leave party_info "
        "market_post market_list market_cancel market_buy market_expand quest"
    )})

# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------

QUEST_GUARD_NPC = "guard"
QUEST_CHARM_RESULT = "ancient_guardian_charm"
QUEST_CHARM_INPUTS = {"treant_bark": 1, "troll_hide": 1, "ectoplasm": 1}
QUEST_GUARD_XP = 50
QUEST_GUARD_GOLD = 25
QUEST_GUARD_POINTS = 15
QUEST_DELVER_XP = 30
QUEST_DELVER_GOLD = 15
QUEST_DELVER_POINTS = 10
QUEST_DELVER_FLOORS = 1
# Sister Maren's remedy quest: gather herbs for the healing spring.
QUEST_REMEDY_INPUTS = {"healing_herb": 3}
QUEST_REMEDY_XP = 20
QUEST_REMEDY_GOLD = 10
QUEST_REMEDY_POINTS = 8
# Sister Maren's tonic quest: brew a Fortitude Tonic (iron + berries).
QUEST_TONIC_INPUTS = {"fortitude_tonic": 1}
QUEST_TONIC_XP = 40
QUEST_TONIC_GOLD = 20
QUEST_TONIC_POINTS = 12

quest_turnin_times = []

# Quest giver category system - makes it easy to add/change quest NPCs.
# Add new entries here to create new quest givers; kill penalties,
# quest lookups, and dashboard catalogs all read from this one table.
QUEST_GIVERS = {
    "guard": {
        "name": "Town Guard",
        "room": "town_square",
        "description": "A stalwart guard who needs help protecting the town.",
    },
    "healer": {
        "name": "Sister Maren",
        "room": "healing_spring",
        "description": "A gentle healer who tends the wounded and always needs remedies.",
    },
    # New givers are one entry following the schema above.
}


def is_quest_giver(npc_id: str) -> bool:
    """Check if an NPC is a registered quest giver."""
    return npc_id in QUEST_GIVERS


def get_quest_giver(npc_id: str) -> dict | None:
    """Get quest giver info by NPC ID."""
    return QUEST_GIVERS.get(npc_id)


def get_quest_givers_in_room(room_id: str) -> list[dict]:
    """Get all quest givers present in a room."""
    givers = []
    for npc_id, info in QUEST_GIVERS.items():
        if info.get("room") == room_id:
            givers.append({"npc_id": npc_id, **info})
    return givers


QUESTS = {
    "guard_charm": {
        "giver_npc": "guard",
        "giver_name": QUEST_GIVERS["guard"]["name"],
        "room": QUEST_GIVERS["guard"]["room"],
        "inputs": dict(QUEST_CHARM_INPUTS),
        "result": QUEST_CHARM_RESULT,
        "result_name": "Ancient Guardian Charm",
        "objective": "craft",
        "brief": "craft an Ancient Guardian Charm (Treant Bark + Troll Hide + Ectoplasm)",
        "reward_xp": QUEST_GUARD_XP,
        "reward_gold": QUEST_GUARD_GOLD,
        "reward_points": QUEST_GUARD_POINTS,
        "repeatable": True,
    },
    "delver": {
        "giver_npc": "guard",
        "giver_name": QUEST_GIVERS["guard"]["name"],
        "room": QUEST_GIVERS["guard"]["room"],
        "inputs": {},
        "result": None,
        "result_name": None,
        "objective": "clear_dungeon_floors",
        "brief": "clear dungeon floors in your party's instance",
        "floors_required": QUEST_DELVER_FLOORS,
        "reward_xp": QUEST_DELVER_XP,
        "reward_gold": QUEST_DELVER_GOLD,
        "reward_points": QUEST_DELVER_POINTS,
        "repeatable": True,
    },
    "remedy": {
        "giver_npc": "healer",
        "giver_name": QUEST_GIVERS["healer"]["name"],
        "room": QUEST_GIVERS["healer"]["room"],
        "inputs": dict(QUEST_REMEDY_INPUTS),
        "result": None,
        "result_name": None,
        "objective": "gather",
        "brief": "bring 3 Healing Herbs",
        "reward_xp": QUEST_REMEDY_XP,
        "reward_gold": QUEST_REMEDY_GOLD,
        "reward_points": QUEST_REMEDY_POINTS,
        "repeatable": True,
    },
    "tonic": {
        "giver_npc": "healer",
        "giver_name": QUEST_GIVERS["healer"]["name"],
        "room": QUEST_GIVERS["healer"]["room"],
        "inputs": dict(QUEST_TONIC_INPUTS),
        "result": None,
        "result_name": None,
        "objective": "craft",
        "brief": "brew 1 Fortitude Tonic (Iron Ore + Mountain Berry) and bring it",
        "reward_xp": QUEST_TONIC_XP,
        "reward_gold": QUEST_TONIC_GOLD,
        "reward_points": QUEST_TONIC_POINTS,
        "repeatable": True,
    },
}


def quest_delver_ready(entry):
    """True when an accepted Depth Delver quest has enough new clears."""
    return (entry.get("dungeon_floors_cleared", 0)
            - entry.get("quest_delver_baseline", 0) >= QUEST_DELVER_FLOORS)


def _quest_has_inputs(player, inputs):
    """True when the player's inventory covers every required input."""
    return all(player.inventory.count(iid) >= qty for iid, qty in (inputs or {}).items())


def _quest_ready(entry, player, qid):
    """True when an active quest's objective is complete and turn-inable."""
    if not _quest_active(entry, qid):
        return False
    if qid == "guard_charm":
        return bool(entry.get("guard_charm_crafted"))
    if qid == "delver":
        return quest_delver_ready(entry)
    return _quest_has_inputs(player, QUESTS[qid].get("inputs", {}))


async def cmd_quest(player, msg):
    """Single quest command: list what's available, accept one, turn one in."""
    action = msg.get("action", "")
    entry = get_score_entry(player.name)

    if action == "list":
        lines = []
        for qid, q in QUESTS.items():
            if _quest_active(entry, qid):
                state = "ready to turn in" if _quest_ready(entry, player, qid) else "active"
            else:
                state = "available"
            lines.append(
                f"{qid}: {q['giver_name']} in {ROOMS[q['room']]['name']} — "
                f"{q.get('brief', q['objective'])} "
                f"(reward {q['reward_xp']} XP + {q['reward_gold']} gold) [{state}]"
            )
        await send(player, {"type": "message", "text": "Quests:\n" + "\n".join(lines)})
        await send(player, stats_view(player))
        return

    qid = str(msg.get("quest", "guard_charm") or "guard_charm").lower()
    if qid not in QUESTS:
        await send(player, {"type": "error", "text": f"Unknown quest '{qid}'. Known: {', '.join(sorted(QUESTS))}."})
        return
    quest = QUESTS[qid]

    def _giver_present():
        return find_npc_in_room(player.room, quest["giver_npc"]) is not None

    def _record_turnin():
        entry[qid_key(entry, qid, "completions")] = entry.get(qid_key(entry, qid, "completions"), 0) + 1
        quest_turnin_times.append(time.time())
        mark_scores_dirty()

    if action == "accept":
        if _quest_active(entry, qid):
            await send(player, {"type": "message", "text": f"You already have an active {quest['giver_name']} quest ({qid})."})
            return
        if not _giver_present():
            await send(player, {"type": "error", "text": f"The {quest['giver_name']} isn't here. Find them in {ROOMS[quest['room']]['name']} to accept this quest."})
            return
        _set_quest_active(entry, qid, True)
        mark_scores_dirty()
        if qid == "guard_charm":
            await send(player, {"type": "message", "text": "Town Guard: Ah, adventurer! We need protectors for our walls. "
                  "Bring me an Ancient Guardian Charm, crafted from Treant Bark, Troll Hide, and Ectoplasm. "
                  f"Return it to me for a reward of {QUEST_GUARD_XP} XP and {QUEST_GUARD_GOLD} gold. This quest can be repeated."})
        elif qid == "delver":
            await send(player, {"type": "message", "text": "Town Guard: The deeps stir below the graveyard. "
                  f"Clear {QUEST_DELVER_FLOORS} dungeon floor{'s' if QUEST_DELVER_FLOORS != 1 else ''} in your party's instance, "
                  f"then report back for {QUEST_DELVER_XP} XP and {QUEST_DELVER_GOLD} gold. Repeatable."})
        elif qid == "remedy":
            await send(player, {"type": "message", "text": "Sister Maren: The spring's remedies run low, friend. "
                  "Bring me 3 Healing Herbs from the wilds and I will make it worth your while: "
                  f"{QUEST_REMEDY_XP} XP and {QUEST_REMEDY_GOLD} gold. Come back any time."})
        else:
            await send(player, {"type": "message", "text": "Sister Maren: The wounded need something stronger than herbs. "
                  "Brew a Fortitude Tonic (Iron Ore and Mountain Berry) and bring it to me for "
                  f"{QUEST_TONIC_XP} XP and {QUEST_TONIC_GOLD} gold. I will always have work for you."})
        await send(player, stats_view(player))
    elif action == "turn_in":
        if not _quest_active(entry, qid):
            await send(player, {"type": "message", "text": "You don't have that quest active."})
            return
        if qid == "guard_charm":
            if not entry.get("guard_charm_crafted"):
                await send(player, {"type": "message", "text": "You haven't crafted the Ancient Guardian Charm yet. "
                      "Gather 1 Treant Bark, 1 Troll Hide, and 1 Ectoplasm, then craft it."})
                return
        elif qid == "delver":
            if not quest_delver_ready(entry):
                have = entry.get("dungeon_floors_cleared", 0) - entry.get("quest_delver_baseline", 0)
                await send(player, {"type": "message", "text": f"The deeps are not yet quiet ({have}/{QUEST_DELVER_FLOORS} floors cleared). "
                      "Descend through the graveyard archway and clear a floor."})
                return
        else:
            need = quest.get("inputs", {})
            missing = [f"{qty}x {ITEM_DEFS[iid]['name']}" for iid, qty in need.items()
                       if player.inventory.count(iid) < qty]
            if missing:
                await send(player, {"type": "message", "text": f"Sister Maren still needs: {', '.join(missing)}."})
                return
        if not _giver_present():
            await send(player, {"type": "error", "text": f"The {quest['giver_name']} isn't here. Return to {ROOMS[quest['room']]['name']} to turn in."})
            return
        if qid == "guard_charm":
            if QUEST_CHARM_RESULT in player.inventory:
                player.inventory.remove(QUEST_CHARM_RESULT)
            entry["guard_charm_crafted"] = False
            entry["quest_guard_active"] = False
        elif qid == "delver":
            entry["quest_delver_active"] = False
            entry["quest_delver_baseline"] = entry.get("dungeon_floors_cleared", 0)
        else:
            for iid, qty in quest.get("inputs", {}).items():
                for _ in range(qty):
                    player.inventory.remove(iid)
            _set_quest_active(entry, qid, False)
        _record_turnin()
        await send(player, {"type": "message", "text": f"{quest['giver_name']}: Excellent work! Here's your reward: "
              f"{quest['reward_xp']} XP and {quest['reward_gold']} gold. "
              "Return whenever you'd like to repeat the quest."})
        await award_points(player, quest["reward_points"], f"completed quest {qid}")
        await award_xp(player.name, quest["reward_xp"], f"completed quest {qid}")
        await credit_gold(player.name, quest["reward_gold"])
        await send(player, stats_view(player))
    else:
        await send(player, {"type": "error", "text": "Unknown quest action. Use 'list', 'accept', or 'turn_in'."})


def qid_key(entry, qid, kind):
    return {"completions": {
        "guard_charm": "quest_guard_completions",
        "delver": "quest_delver_completions",
        "remedy": "quest_remedy_completions",
        "tonic": "quest_tonic_completions",
    }[qid]}[kind]


def _quest_active(entry, qid):
    if qid == "guard_charm":
        return bool(entry.get("quest_guard_active"))
    if qid == "delver":
        return bool(entry.get("quest_delver_active"))
    if qid == "remedy":
        return bool(entry.get("quest_remedy_active"))
    return bool(entry.get("quest_tonic_active"))


def _set_quest_active(entry, qid, active):
    if qid == "guard_charm":
        entry["quest_guard_active"] = bool(active)
        if active:
            entry["guard_charm_crafted"] = False
    elif qid == "delver":
        entry["quest_delver_active"] = bool(active)
        if active:
            entry["quest_delver_baseline"] = entry.get("dungeon_floors_cleared", 0)
    elif qid == "remedy":
        entry["quest_remedy_active"] = bool(active)
    else:
        entry["quest_tonic_active"] = bool(active)
    mark_scores_dirty()


async def cmd_party_invite(player, msg):
    target = find_player_in_room(player.room, msg.get("target", ""))
    if not target or target is player:
        await send(player, {"type": "error", "text": "No one here by that name."})
        return
    party = _auto_create_party(player)
    if len(party.member_ids) >= PARTY_MAX_MEMBERS:
        await send(player, {"type": "error", "text": "Your party is full."})
        return
    _pending_party_invites[target.id] = party
    await send(target, {"type": "message", "text": f"{player.name} invites you to a party. Send party_accept to join."})
    await send(player, {"type": "message", "text": f"Invitation sent to {target.name}."})


async def cmd_party_accept(player, msg):
    party = _pending_party_invites.pop(player.id, None)
    if not party or party.id not in parties:
        await send(player, {"type": "error", "text": "No pending party invitation."})
        return
    if len(party.member_ids) >= PARTY_MAX_MEMBERS:
        await send(player, {"type": "error", "text": "That party is full."})
        return
    if player.party_id and player.party_id in parties:
        old = parties[player.party_id]
        old.member_ids.discard(player.id)
        if not old.member_ids:
            _delete_party(old)
    party.member_ids.add(player.id)
    player.party_id = party.id
    leader = players.get(party.leader_id)
    if leader and leader.logged_in:
        await send(leader, {"type": "message", "text": "You are now the party leader."})
    await _notify_party(party, f"{player.name} joins the party.")
    await send(player, stats_view(player))


async def cmd_party_leave(player, msg):
    party = parties.get(player.party_id) if player.party_id else None
    if not party:
        await send(player, {"type": "error", "text": "You are not in a party."})
        return
    party.member_ids.discard(player.id)
    player.party_id = None
    _relocate_from_dungeon(player)
    await send(player, {"type": "message", "text": "You leave the party."})
    if not party.member_ids:
        _delete_party(party)
    elif party.leader_id == player.id:
        party.leader_id = next(iter(party.member_ids))
    await send(player, room_view(player.room))
    await send(player, stats_view(player))


async def cmd_party_info(player, msg):
    party = parties.get(player.party_id) if player.party_id else None
    if not party:
        await send(player, {"type": "error", "text": "You are not in a party."})
        return
    members = []
    for mid in party.member_ids:
        m = players.get(mid)
        members.append({"name": m.name if m else "?", "level": get_score_entry(m.name)["level"] if m and m.name else 1})
    leader = players.get(party.leader_id)
    await send(player, {
        "type": "party",
        "id": party.id,
        "leader": leader.name if leader else "?",
        "members": members,
        "dungeon_id": party.dungeon_id,
    })


async def cmd_market_list(player, msg):
    await send(player, {
        "type": "market",
        "orders": [_market_order_dict(o) for o in market_orders],
        "treasury": round(tax_treasury, 2),
        "tax_treasury": round(tax_treasury, 2),
        "tax_collected_lifetime": round(tax_collected_lifetime, 2),
        "collected_lifetime": round(tax_collected_lifetime, 2),
        "tax_rate": TAX_RATE,
        "tax_min": TAX_MINIMUM,
    })


async def cmd_market_post(player, msg):
    iid = find_item_by_name(player.inventory, msg.get("item", ""))
    if not iid:
        await send(player, {"type": "error", "text": "You don't have that."})
        return
    try:
        price = int(msg.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        price = item_suggested_price(iid)
    entry = get_score_entry(player.name)
    slots = entry.get("market_slots", MARKET_ORDER_SLOTS_BASE)
    own_open = sum(1 for o in market_orders if o["seller"] == player.name)
    if own_open >= slots:
        nxt = market_slot_price(slots)
        await send(player, {"type": "error", "text": f"Market stall full ({own_open}/{slots}). Use market_expand (next slot {nxt} gold) or cancel an order."})
        return
    player.inventory.remove(iid)
    if player.equipped == iid:
        player.equipped = None
    if player.armor == iid:
        player.armor = None
    if player.offhand == iid:
        player.offhand = None
    oid = next(_id_counter)
    market_orders.append({"id": oid, "seller": player.name, "item": iid, "price": price, "ts": time.time()})
    mark_scores_dirty()
    await send(player, {"type": "message", "text": f"Listed {ITEM_DEFS[iid]['name']} for {price} gold (order #{oid})."})
    await send(player, stats_view(player))


async def cmd_market_cancel(player, msg):
    try:
        oid = int(msg.get("id", 0))
    except (TypeError, ValueError):
        await send(player, {"type": "error", "text": "market_cancel needs an 'id'."})
        return
    for o in list(market_orders):
        if o["id"] == oid and o["seller"] == player.name:
            market_orders.remove(o)
            player.inventory.append(o["item"])
            mark_scores_dirty()
            await send(player, {"type": "message", "text": f"Cancelled order #{oid}."})
            await send(player, stats_view(player))
            return
    await send(player, {"type": "error", "text": f"No order #{oid} of yours."})


async def cmd_market_expand(player, msg):
    """Buy +1 market stall slot. Fee goes to the GM treasury (gold sink)."""
    global tax_treasury
    entry = get_score_entry(player.name)
    slots = entry.get("market_slots", MARKET_ORDER_SLOTS_BASE)
    price = market_slot_price(slots)
    if player.gold < price:
        await send(player, {"type": "error", "text": f"Next market slot costs {price} gold (you have {player.gold})."})
        return
    player.gold -= price
    tax_treasury += price
    entry["market_slots"] = slots + 1
    mark_scores_dirty()
    await send(player, {"type": "message", "text": f"Market stall expanded to {slots + 1} slots for {price} gold (next: {market_slot_price(slots + 1)} gold)."})
    await send(player, stats_view(player))


async def cmd_market_buy(player, msg):
    global tax_treasury, tax_collected_lifetime
    oid = msg.get("id", None)
    choice = None
    if oid is not None:
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            await send(player, {"type": "error", "text": f"No order #{msg.get('id')}."})
            return
        for o in market_orders:
            if o["id"] == oid:
                choice = o
                break
        if not choice:
            await send(player, {"type": "error", "text": f"No order #{oid}."})
            return
        # Same self-deal rule as auto-buy: buying your own listing would
        # mint score/XP to yourself for just the tax cost (#188).
        # Case-insensitive: "Alice" and "alice" share one score entry.
        if choice["seller"].lower() == player.name.lower():
            await send(player, {"type": "error",
                                "text": f"Order #{oid} is your own listing."})
            return
    else:
        affordable = [o for o in market_orders if o["seller"].lower() != player.name.lower() and player.gold >= o["price"]]
        if not affordable:
            await send(player, {"type": "error", "text": "No affordable orders."})
            return
        choice = min(affordable, key=lambda o: o["price"])
    if player.gold < choice["price"]:
        await send(player, {"type": "error", "text": "You can't afford that."})
        return
    if _pack_full(player):
        await send(player, {"type": "error", "text": _pack_full_error()})
        return
    price = choice["price"]
    tax = max(TAX_MINIMUM, round(price * TAX_RATE))
    seller_payout = price - tax
    player.gold -= price
    tax_treasury += tax
    tax_collected_lifetime += tax
    market_orders.remove(choice)
    player.inventory.append(choice["item"])
    buyer_entry = get_score_entry(player.name)
    seller_entry = get_score_entry(choice["seller"])
    buyer_entry["trades_completed"] = buyer_entry.get("trades_completed", 0) + 1
    seller_entry["tax_paid"] = seller_entry.get("tax_paid", 0.0) + tax
    seller_entry["gold_bank"] = seller_entry.get("gold_bank", 0) + seller_payout
    market_history.append({
        "time": time.strftime("%H:%M:%S"), "ts": time.time(),
        "buyer": player.name, "seller": choice["seller"],
        "item": ITEM_DEFS.get(choice["item"], {}).get("name", choice["item"]),
        "price": price, "tax": tax, "payout": seller_payout,
    })
    while len(market_history) > MARKET_HISTORY_SIZE:
        del market_history[0]
    mark_scores_dirty()
    await send(player, {"type": "message", "text": f"You buy {ITEM_DEFS.get(choice['item'], {}).get('name', choice['item'])} for {price} gold."})
    await award_points(player, 2, "made a market purchase")
    await award_xp(player.name, 3, "made a market purchase")
    await award_points_to_name(choice["seller"], 2, "made a market sale")
    await award_xp(choice["seller"], 3, "made a market sale")
    await credit_gold(choice["seller"], 0)
    await send(player, stats_view(player))

# ---------------------------------------------------------------------------
# GM commands (treasury-priced, loopback GM stream only)
# ---------------------------------------------------------------------------

def _is_gm(player):
    if isinstance(player, GMStream):
        return True
    return (getattr(player, "name", "") or "").startswith("GM")


async def _spend_tax(player, cost, what):
    global tax_treasury
    if tax_treasury < cost:
        await send(player, {"type": "message", "text": f"GM: insufficient treasury for {what} ({cost} needed, {round(tax_treasury,2)} available)."})
        return False
    tax_treasury = round(tax_treasury - cost, 2)
    mark_scores_dirty()
    return True


async def cmd_gm_reward(player, msg):
    gold = msg.get("gold", None)
    item = msg.get("item", None)
    if gold is not None:
        try:
            gold = int(gold)
        except (TypeError, ValueError):
            gold = 0
        if gold <= 0:
            await send(player, {"type": "error", "text": "gm_reward needs a positive 'gold' amount."})
            return
        if not await _spend_tax(player, gold, f"reward {gold}g"):
            return
        target = msg.get("player", "")
        p = find_player_anywhere(target) if target else None
        if p:
            p.gold += gold
            await send(p, stats_view(p))
            await send(p, {"type": "message", "text": f"The GM grants you {gold} gold."})
        elif target:
            entry = get_score_entry(target)
            entry["gold_bank"] = entry.get("gold_bank", 0) + gold
            mark_scores_dirty()
        await send(player, {"type": "message", "text": f"GM: rewarded {gold} gold to {target or '?'}. Treasury now {round(tax_treasury,2)}."})
        return
    if item:
        iid = find_item_by_name(list(ITEM_DEFS.keys()), str(item))
        if not iid:
            await send(player, {"type": "error", "text": f"Unknown item '{item}'."})
            return
        cost = ITEM_DEFS.get(iid, {}).get("value", 1) or 1
        if not await _spend_tax(player, cost, f"reward item {iid}"):
            return
        target = msg.get("player", "")
        room = msg.get("room", "")
        if target:
            p = find_player_anywhere(target)
            if p:
                p.inventory.append(iid)
                await send(p, stats_view(p))
            else:
                await send(player, {"type": "error", "text": f"Player '{target}' not found."})
                return
        elif room and room in ROOMS:
            _add_ground(room, iid)
            await sync_room(room)
        else:
            await send(player, {"type": "error", "text": "gm_reward needs a 'player' or 'room' for items."})
            return
        await send(player, {"type": "message", "text": f"GM: rewarded {iid} ({cost} tax spent). Treasury now {round(tax_treasury,2)}."})
        return
    await send(player, {"type": "error", "text": "gm_reward needs 'gold' or 'item'."})


def ITHERE_ITEM_NAMES_MSG(iid):
    return ITEM_DEFS.get(iid, {}).get("name", iid)


async def cmd_gm_buff(player, msg):
    kind = str(msg.get("type", "")).lower()
    if kind not in ("xp", "gold"):
        await send(player, {"type": "error", "text": "gm_buff needs 'type' xp|gold."})
        return
    try:
        minutes = int(msg.get("minutes", 5))
    except (TypeError, ValueError):
        minutes = 5
    minutes = max(1, min(60, minutes))
    cost = minutes * GM_BUFF_COST_PER_MINUTE
    if not await _spend_tax(player, cost, f"{kind} buff x{minutes}m"):
        return
    buffs[kind] = time.time() + minutes * 60
    await send(player, {"type": "message", "text": f"GM: {kind.upper()}x2 world event for {minutes}m. Treasury now {round(tax_treasury,2)}."})
    await broadcast_all({"type": "message", "text": f"World event: double {kind} for {minutes} minutes!"})


async def cmd_gm_boss(player, msg):
    room = msg.get("room", "")
    if room not in ROOMS:
        await send(player, {"type": "error", "text": f"Unknown room '{room}'."})
        return
    try:
        strength = int(msg.get("strength", 1))
    except (TypeError, ValueError):
        strength = 1
    strength = max(1, min(5, strength))
    cost = strength * GM_BOSS_COST_PER_STRENGTH
    if not await _spend_tax(player, cost, f"boss s{strength}"):
        return
    bid = f"boss_{next(_id_counter)}"
    npcs[bid] = {
        "id": bid, "name": f"Elite Menace {strength}*", "room": room,
        "hp": 30 + 40 * strength, "max_hp": 30 + 40 * strength,
        "attack": 5 + 4 * strength, "hostile": True, "behavior": "idle",
        "loot": ["healing_herb"], "gold": 10 * strength,
        "respawn_seconds": 0, "alive": True, "respawn_at": None, "contributors": {},
    }
    await sync_room(room)
    await send(player, {"type": "message", "text": f"GM: Elite {strength}* menace spawned in {room}. Treasury now {round(tax_treasury,2)}."})


async def cmd_gm_announce(player, msg):
    text = (msg.get("text") or "").strip()
    if not text:
        await send(player, {"type": "error", "text": "gm_announce needs 'text'."})
        return
    if not await _spend_tax(player, GM_ANNOUNCE_COST, "announce"):
        return
    await broadcast_all({"type": "message", "text": f"ANNOUNCEMENT: {text[:200]}"})
    await send(player, {"type": "message", "text": "GM: announcement sent."})


async def cmd_gm_heal(player, msg):
    name = (msg.get("player") or "").strip()
    if not name:
        await send(player, {"type": "error", "text": "gm_heal needs a 'player' name."})
        return
    target = find_player_anywhere(name)
    if not target or not target.logged_in:
        await send(player, {"type": "error", "text": f"'{name}' is not online."})
        return
    missing = target.max_hp - target.hp
    if missing <= 0:
        await send(player, {"type": "error", "text": f"{target.name} is already at full HP (no charge)."})
        return
    cost = missing * GM_HEAL_COST_PER_HP
    if not await _spend_tax(player, cost, f"heal {target.name}"):
        return
    target.hp = target.max_hp
    await send(target, stats_view(target))
    await send(target, {"type": "message", "text": "The GM restores you to full health."})
    await send(player, {"type": "message", "text": f"GM: {target.name} healed ({cost} tax spent)."})


async def cmd_gm_teleport(player, msg):
    name = (msg.get("player") or "").strip()
    room = (msg.get("room") or "").strip()
    if not name or room not in ROOMS:
        await send(player, {"type": "error", "text": "gm_teleport needs a 'player' and a valid 'room'."})
        return
    target = find_player_anywhere(name)
    if not target or not target.logged_in:
        await send(player, {"type": "error", "text": f"'{name}' is not online."})
        return
    if target.room == room:
        await send(player, {"type": "error", "text": f"{target.name} is already in {room} (no charge)."})
        return
    if len(players_in_room(room)) >= MAX_PLAYERS_PER_ROOM:
        await send(player, {"type": "error", "text": f"{room} is full."})
        return
    if not await _spend_tax(player, GM_TELEPORT_COST, f"teleport {target.name}"):
        return
    remove_member(target)
    target.room = room
    add_member(target)
    await send(target, room_view(target.room))
    await send(target, stats_view(target))
    await send(player, {"type": "message", "text": f"GM: {target.name} teleported to {room}."})


async def cmd_gm_slay(player, msg):
    target = (msg.get("target") or "").strip().lower()
    if not target:
        await send(player, {"type": "error", "text": "gm_slay needs a 'target'."})
        return
    matches = [n for n in all_npcs() if n.get("alive") and (target in n["name"].lower() or target in str(n["id"]).lower())]
    if not matches:
        await send(player, {"type": "error", "text": f"No living NPC matches '{target}'."})
        return
    if len(matches) > 1 and target not in [str(m["id"]).lower() for m in matches]:
        # Ambiguous unless exact id given; require more specific text.
        names = ", ".join(m["name"] for m in matches[:5])
        await send(player, {"type": "error", "text": f"Ambiguous target '{target}': {names}. Be more specific."})
        return
    npc = next((m for m in matches if str(m["id"]).lower() == target), matches[0])
    if not npc.get("alive"):
        await send(player, {"type": "error", "text": f"{npc['name']} is already dead."})
        return
    cost = max(GM_SLAY_MIN_COST, (npc.get("max_hp", 10) or 10) * GM_SLAY_COST_PER_HP)
    if not await _spend_tax(player, cost, f"slay {npc['name']}"):
        return
    npc["alive"] = False
    npc["respawn_at"] = time.time() + (npc.get("respawn_seconds", 60) or 60) if npc.get("respawn_seconds", 60) else None
    for loot_id in npc.get("loot", []):
        _add_ground(npc["room"], loot_id)
    await broadcast_room(npc["room"], {"type": "combat", "text": f"Divine lightning strikes {npc['name']} dead."})
    await sync_room(npc["room"])
    unsealed = check_dungeon_clear(npc["room"])
    await send(player, {
        "type": "message",
        "text": f"GM: {npc['name']} slain ({cost} tax spent)."
                + (" Floor unsealed." if unsealed else "")
                + f" Treasury now {round(tax_treasury, 2)}."
    })


async def cmd_gm_kick(player, msg):
    """Disconnect a player. Free -- moderation, not economy."""
    name = (msg.get("player") or "").strip()
    if not name:
        await send(player, {"type": "error", "text": "gm_kick needs a 'player' name."})
        return
    target = find_player_anywhere(name)
    if not target or not target.logged_in:
        await send(player, {"type": "error", "text": f"'{name}' is not online to kick."})
        return
    reason = (msg.get("reason") or "kicked by the GM").strip() or "kicked by the GM"
    if not await _spend_tax(player, 0, f"kick {target.name}"):
        return
    await send(target, {"type": "message", "text": f"You have been kicked by the GM ({reason})."})
    await send(player, {"type": "message", "text": f"GM: {target.name} kicked ({reason})."})
    await asyncio.sleep(0.2)
    await target.ws.close()


async def broadcast_all(payload):
    for p in list(players.values()):
        if p.logged_in:
            await send(p, payload)


# --- GM stream (dedicated WebSocket port, loopback-only) ---------------

class GMStream:
    """Minimal player-like object for the GM-only WebSocket stream."""
    __slots__ = ("ws", "id", "name", "outbound", "outbound_event")
    def __init__(self, ws):
        self.ws = ws
        self.id = next(_id_counter)
        self.name = "<gm-dashboard>"
        self.outbound = deque(maxlen=OUTBOUND_QUEUE_MAX)
        self.outbound_event = asyncio.Event()


GM_HANDLERS = {
    "gm_reward": cmd_gm_reward,
    "gm_buff": cmd_gm_buff,
    "gm_boss": cmd_gm_boss,
    "gm_announce": cmd_gm_announce,
    "gm_heal": cmd_gm_heal,
    "gm_teleport": cmd_gm_teleport,
    "gm_slay": cmd_gm_slay,
    "gm_kick": cmd_gm_kick,
}


async def handle_gm_connection(ws):
    # Loopback-only gate.
    try:
        host = getattr(getattr(ws, "remote_address", None), "__getitem__", lambda i: "?")(0) if getattr(ws, "remote_address", None) else "?"
    except Exception:
        host = "?"
    if host not in ("127.0.0.1", "::1", "localhost"):
        try:
            await ws.close()
        except Exception:
            pass
        return
    player = GMStream(ws)
    writer_task = asyncio.create_task(_outbound_writer(player))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send(player, {"type": "error", "text": "invalid JSON"})
                continue
            cmd = msg.get("cmd")
            handler = GM_HANDLERS.get(cmd)
            if not handler:
                await send(player, {"type": "error", "text": f"Unknown GM command '{cmd}'."})
                continue
            try:
                await handler(player, msg)
            except Exception as e:
                await send(player, {"type": "error", "text": f"GM command failed: {e}"})
    except Exception:
        pass
    finally:
        writer_task.cancel()
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):
            pass


HANDLERS = {
    "login": cmd_login,
    "look": cmd_look,
    "move": cmd_move,
    "attack": cmd_attack,
    "take": cmd_take,
    "gather": cmd_gather,
    "drop": cmd_drop,
    "equip": cmd_equip,
    "use": cmd_use,
    "rest": cmd_rest,
    "heal": cmd_heal,
    "buy": cmd_buy,
    "sell": cmd_sell,
    "craft": cmd_craft,
    "inventory": cmd_inventory,
    "stats": cmd_stats,
    "who": cmd_who,
    "leaderboard": cmd_leaderboard,
    "help": cmd_help,
    "party_invite": cmd_party_invite,
    "party_accept": cmd_party_accept,
    "party_leave": cmd_party_leave,
    "party_info": cmd_party_info,
    "market_list": cmd_market_list,
    "market_post": cmd_market_post,
    "market_cancel": cmd_market_cancel,
    "market_buy": cmd_market_buy,
    "market_expand": cmd_market_expand,
    "quest": cmd_quest,
    "commission_post": cmd_commission_post,
    "commission_list": cmd_commission_list,
    "commission_fill": cmd_commission_fill,
    "commission_cancel": cmd_commission_cancel,
}

SCORE_ARG_EXTRACTORS = {
    "attack": lambda msg: str(msg.get("target", "")).lower(),
    "move": lambda msg: str(msg.get("dir", "")).lower(),
    "take": lambda msg: str(msg.get("item", "")).lower(),
    "gather": lambda msg: str(msg.get("node") or msg.get("item", "")).lower(),
    "drop": lambda msg: str(msg.get("item", "")).lower(),
    "craft": lambda msg: str(msg.get("recipe", "")).lower(),
    "buy": lambda msg: str(msg.get("item", "")).lower(),
    "sell": lambda msg: str(msg.get("item", "")).lower(),
    "rest": lambda msg: "rest",
    "heal": lambda msg: "heal",
    "market_post": lambda msg: str(msg.get("item", "")).lower(),
    "market_buy": lambda msg: str(msg.get("id", "")).lower(),
    "quest": lambda msg: str(msg.get("action", "")).lower(),
    "commission_post": lambda msg: str(msg.get("target") or msg.get("required_kills", "")).lower(),
    "commission_fill": lambda msg: str(msg.get("commission_id", "")).lower(),
    "commission_cancel": lambda msg: str(msg.get("commission_id", "")).lower(),
}


# ---------------------------------------------------------------------------
# Telemetry + live dashboard
# ---------------------------------------------------------------------------

HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8766
ACTIVITY_LOG_SIZE = 100
TRACK_LOG_SIZE = 30
SCORE_HISTORY_SIZE = 40

VERBOSE = os.environ.get("TEXTMMO_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")


def vlog(msg):
    if VERBOSE:
        print(f"[log] {msg}")


START_TIME = time.time()
command_log = []
track_log = {}
_score_history = {}
_dashboard_history = []  # [{ts, players_online, top_scores: [{name, score}]}]
_HISTORY_SAMPLE_INTERVAL = 30  # seconds between samples
_HISTORY_MAX_SAMPLES = 120     # ~1 hour of samples at 30s intervals


def log_command(name, cmd, msg):
    """Record one client command with a short human-readable detail."""
    if cmd == "login":
        name = name or str(msg.get("name", ""))
        detail = name
    elif cmd in ("move", "attack", "take", "drop", "equip", "use", "buy", "sell", "craft"):
        detail = str(msg.get("dir") or msg.get("target") or msg.get("item") or msg.get("recipe") or "")
    elif cmd.startswith("market_"):
        detail = str(msg.get("item") or msg.get("id") or "")
    elif cmd.startswith("party_"):
        detail = str(msg.get("target") or "")
    elif cmd.startswith("quest"):
        detail = str(msg.get("action") or cmd)
    elif cmd.startswith("commission"):
        detail = str(msg.get("target") or msg.get("commission_id") or msg.get("id") or "")
    elif cmd == "gather":
        detail = str(msg.get("node") or msg.get("item") or "")
    else:
        detail = ""
    entry = get_score_entry(name) if name and name != "<new>" else None
    lvl = entry["level"] if entry else 1
    entry_log = {"time": time.strftime("%H:%M:%S"), "name": name or "<new>", "cmd": cmd, "detail": detail, "level": lvl}
    command_log.append(entry_log)
    if len(command_log) > ACTIVITY_LOG_SIZE:
        del command_log[0]
    if name and name != "<new>":
        per = track_log.setdefault(name, [])
        per.append(entry_log)
        while len(per) > TRACK_LOG_SIZE:
            del per[0]


def record_score_point(name, score):
    hist = _score_history.setdefault(name.lower(), [])
    hist.append((time.time(), round(score, 2)))
    while len(hist) > SCORE_HISTORY_SIZE:
        del hist[0]


def _quest_snapshot():
    """Dashboard quest panel: catalog, live active counts, lifetime turn-ins."""
    now = time.time()
    while quest_turnin_times and quest_turnin_times[0] < now - 60:
        del quest_turnin_times[0]
    active = {qid: 0 for qid in QUESTS}
    completions = {qid: 0 for qid in QUESTS}
    for e in SCORES.values():
        for qid in QUESTS:
            if _quest_active(e, qid):
                active[qid] += 1
            completions[qid] += e.get(qid_key(e, qid, "completions"), 0)
    return {
        "catalog": [
            {**{"id": qid}, **{k: v for k, v in q.items() if k != "inputs"}, "inputs": q.get("inputs", {})}
            for qid, q in QUESTS.items()
        ],
        "active": active,
        "completions": completions,
        "turnins_last_min": len(quest_turnin_times),
    }


def world_snapshot():
    rooms = []
    for rid, r in ROOMS.items():
        rooms.append({
            "id": rid, "name": r["name"],
            "exits": [{"dir": d, "to": t, "to_name": ROOMS.get(t, {}).get("name", t)} for d, t in r.get("exits", {}).items()],
            "players": [{"name": p.name, "level": get_score_entry(p.name)["level"]} for p in players_in_room(rid)],
            "npcs": [{"name": n["name"], "alive": n["alive"], "hp": n["hp"], "max_hp": n["max_hp"]} for n in npcs.values() if n["room"] == rid],
            "items": [ITEM_DEFS[i]["name"] for i in room_items.get(rid, [])],
            "gold": room_gold.get(rid, 0),
        })
    online_players = []
    for p in players.values():
        if not p.logged_in:
            continue
        e = get_score_entry(p.name)
        online_players.append({
            "name": p.name, "level": e["level"], "score": round(e["score"], 2),
            "score_history": [s for _, s in _score_history.get(p.name.lower(), [])],
            "kills": e.get("kills", 0), "deaths": e.get("deaths", 0),
            "gold": p.gold, "hp": p.hp, "max_hp": p.max_hp, "defense": _player_defense(p),
            "variety": round(compute_variety(e), 2), "room": p.room,
            "last_action": (track_log.get(p.name, [{}])[-1].get("cmd", "") if track_log.get(p.name) else ""),
            "recent_actions": list(track_log.get(p.name, [])),
        })
    scores = sorted(
        [{"name": e.get("display_name"), "score": round(e.get("score", 0), 2), "level": e.get("level", 1)} for e in SCORES.values()],
        key=lambda x: x["score"], reverse=True,
    )
    dungeon_views = []
    for d in dungeons.values():
        floors = []
        max_floor = 0
        for fno, f in sorted(d.floors.items()):
            max_floor = max(max_floor, fno)
            floors.append({"floor": fno, "guards_alive": sum(1 for g in f.guards if g["alive"]),
                           "guards_total": len(f.guards), "cleared": f.cleared})
        members = []
        party = parties.get(d.party_id)
        if party:
            for mid in party.member_ids:
                m = players.get(mid)
                if m:
                    members.append(m.name)
        dungeon_views.append({"id": d.id, "party": members, "max_floor_reached": max_floor, "floors": floors})
    bosses = [{"name": n["name"], "room": n["room"], "hp": n["hp"], "max_hp": n["max_hp"], "attack": n["attack"]}
              for n in npcs.values() if str(n["id"]).startswith("boss_") and n["alive"]]
    buffs_view = {}
    for k in ("xp", "gold"):
        remaining = max(0, int(buffs.get(k, 0) - time.time()))
        buffs_view[k] = remaining
    return {
        "server": {"ws_port": PORT, "gm_port": GM_PORT, "uptime": int(time.time() - START_TIME),
                   "players_online": len(online_players), "connections": len(players)},
        "rooms": rooms, "players": online_players, "scores": scores,
        "activity": list(command_log),
        "market": {"treasury": round(tax_treasury, 2), "collected_lifetime": round(tax_collected_lifetime, 2),
                   "tax_rate": TAX_RATE, "tax_min": TAX_MINIMUM, "trade_count": sum(e.get("trades_completed", 0) for e in SCORES.values()),
                   "orders": [{"id": o["id"], "seller": o["seller"], "item": ITEM_DEFS.get(o["item"], {}).get("name", o["item"]),
                               "price": o["price"], "ts": o.get("ts", 0)} for o in market_orders],
                   "history": list(market_history)},
        "buffs": buffs_view,
        "bosses": bosses,
        "dungeons": dungeon_views,
        "quests": _quest_snapshot(),
        "catalog": {"players": sorted([p.name for p in players.values() if p.logged_in]),
                    "items": sorted([v["name"] for v in ITEM_DEFS.values()]),
                    "rooms": sorted(list(ROOMS.keys()))},
        "history": list(_dashboard_history),
    }


world_snapshot_json = None


def refresh_snapshot_json():
    global world_snapshot_json
    try:
        world_snapshot_json = json.dumps(world_snapshot())
    except Exception:
        pass


async def dashboard_refresh_loop():
    last_sample = 0.0
    while True:
        await asyncio.sleep(SNAPSHOT_REFRESH_SECONDS)
        now = time.time()
        if now - last_sample >= _HISTORY_SAMPLE_INTERVAL:
            last_sample = now
            top = sorted(
                [{"name": e.get("display_name", "?"), "score": round(e.get("score", 0), 2)}
                 for e in SCORES.values()],
                key=lambda x: x["score"], reverse=True,
            )[:5]
            _dashboard_history.append({
                "ts": now,
                "players_online": sum(1 for p in players.values() if p.logged_in),
                "top_scores": top,
                # Market snapshot for volume charts (additive; old readers
                # ignore unknown keys, so this never breaks the protocol).
                "market_orders": len(market_orders),
                "treasury": round(tax_treasury, 2),
                "trades": sum(e.get("trades_completed", 0) for e in SCORES.values()),
            })
            if len(_dashboard_history) > _HISTORY_MAX_SAMPLES:
                del _dashboard_history[0]
        refresh_snapshot_json()


def _memory_mb():
    """Process RSS in MB (stdlib only). None where unreadable."""
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def start_dashboard():
    import threading
    here = dirname(abspath(__file__))
    html_path = join(here, "dashboard.html")

    class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if VERBOSE:
                print(f"[http] {self.address_string()}: {fmt % args}")

        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/state":
                state = world_snapshot_json if world_snapshot_json is not None else "{}"
                self._send(state.encode(), "application/json")
            elif path == "/health":
                # Liveness probe for CI/Docker/monitoring (#56).
                body = json.dumps({
                    "status": "ok",
                    "uptime": round(time.time() - START_TIME, 1),
                    "players_online": sum(1 for p in players.values() if p.logged_in),
                    "memory_mb": _memory_mb(),
                })
                self._send(body.encode(), "application/json")
            elif path in ("/", "/dashboard.html") and os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            else:
                self.send_error(404)

    try:
        refresh_snapshot_json()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Live dashboard: http://localhost:{HTTP_PORT}/")
    except OSError as e:
        print(f"Warning: dashboard could not start on port {HTTP_PORT}: {e}")


# ---------------------------------------------------------------------------
# NPC AI loop — runs independently of any player connection
# ---------------------------------------------------------------------------

async def npc_ai_loop():
    while True:
        await asyncio.sleep(NPC_TICK_SECONDS)
        now = time.time()
        prune_commissions(now)
        for node in list(gather_nodes.values()):
            if not node["available"] and node["respawn_at"] and now >= node["respawn_at"]:
                node["available"] = True
                node["respawn_at"] = None
                await sync_room(node["room"])
        for npc in list(all_npcs()):
            if not npc.get("alive", False):
                if npc["respawn_at"] and now >= npc["respawn_at"]:
                    respawn_npc(npc)
                    await broadcast_room(npc["room"], {"type": "message", "text": f"{npc['name']} respawns."})
                    await sync_room(npc["room"])
                continue
            room_id = npc["room"]
            targets = players_in_room(room_id)
            if npc["hostile"] and targets:
                victim = random.choice(targets)
                dmg = random.randint(1, npc["attack"])
                dmg = max(0, dmg - _player_damage_reduction(victim))
                victim.hp -= dmg
                await send(victim, {"type": "combat", "text": f"{npc['name']} attacks you for {dmg}."})
                await broadcast_room(room_id, {"type": "combat", "text": f"{npc['name']} attacks {victim.name} for {dmg}."}, exclude=victim)
                if victim.hp <= 0:
                    await respawn_player(victim)
            elif not npc["hostile"] and npc.get("behavior") == "wander" and random.random() < 0.1:
                exits = ROOMS.get(room_id, {}).get("exits", {})
                if exits:
                    dest = random.choice(list(exits.values()))
                    if dest in ROOMS:
                        npc["room"] = dest
                        await sync_room(room_id)
                        await sync_room(dest)


async def _leave_party_on_disconnect(player):
    # A pending invitation can never be accepted after disconnect, so drop
    # it instead of leaking one dict entry per abandoned invite.
    _pending_party_invites.pop(player.id, None)
    party = parties.get(player.party_id) if player.party_id else None
    if not party:
        return
    party.member_ids.discard(player.id)
    player.party_id = None
    if not party.member_ids:
        _delete_party(party)
    elif party.leader_id == player.id:
        party.leader_id = next(iter(party.member_ids))


async def handle_connection(ws):
    if MAX_TOTAL_CONNECTIONS is not None and len(players) >= MAX_TOTAL_CONNECTIONS:
        try:
            await ws.send(json.dumps({"type": "error", "text": "Server is full right now. Try again shortly."}))
        finally:
            await ws.close()
        return
    pid = next(_id_counter)
    player = Player(ws=ws, id=pid)
    player.outbound_event = asyncio.Event()
    players[pid] = player
    writer_task = asyncio.create_task(_outbound_writer(player))
    vlog(f"connection {pid} opened from {getattr(ws, 'remote_address', '?')}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send(player, {"type": "error", "text": "invalid JSON"})
                continue
            # Top guard (#190): malformed input must error, never drop the
            # connection. Non-dict JSON dies on .get(); non-string cmd dies
            # on startswith()/dict lookup (unhashable); wrong-type fields
            # die inside handlers (.strip() on 123 etc.).
            if not isinstance(msg, dict):
                await send(player, {"type": "error", "text": "message must be a JSON object"})
                continue
            cmd = msg.get("cmd")
            if not isinstance(cmd, str):
                await send(player, {"type": "error", "text": "missing command 'cmd'"})
                continue
            if cmd.startswith("gm_"):
                await send(player, {"type": "error", "text": f"GM actions are only available through the dashboard's GM stream (ws://127.0.0.1:{GM_PORT})."})
                continue
            if not player.logged_in and cmd != "login":
                await send(player, {"type": "error", "text": "You must 'login' first."})
                continue
            handler = HANDLERS.get(cmd)
            if not handler:
                await send(player, {"type": "error", "text": f"Unknown command '{cmd}'."})
                continue
            try:
                log_command(player.name, cmd, msg)
                if cmd in SCORE_ARG_EXTRACTORS:
                    record_action(player.name, (cmd, SCORE_ARG_EXTRACTORS[cmd](msg)))
                if player.logged_in:
                    _tick_player_buffs(player)
                await handler(player, msg)
            except Exception as e:
                # Untrusted input reaches this net on EVERY malformed message,
                # so a traceback here is a disk-fill vector (stdout/stderr go
                # to TEXTMMO_LOG_FILE when set): one line always, full
                # traceback only in verbose mode for real debugging.
                if VERBOSE:
                    traceback.print_exc()
                else:
                    print(f"handler error on {cmd}: {type(e).__name__}: {e}", flush=True)
                await send(player, {"type": "error", "text": f"command '{cmd}' failed on that input"})
    except websockets.ConnectionClosed:
        pass
    finally:
        # Mark logged-out FIRST so a half-finished cleanup below can never
        # leave a ghost behind that still counts as online. The entry is
        # removed at the end; pop() (not del) tolerates double cleanup.
        was_logged_in = player.logged_in
        player.logged_in = False
        writer_task.cancel()
        try:
            await writer_task
        except BaseException:
            # CancelledError (a BaseException, not an Exception) is the
            # normal outcome here -- and ANY failure at this point must
            # still fall through to the cleanup below, otherwise the
            # player's entry, name lock, room slot and party seat leak
            # (relogin then fails with "already in use" forever).
            pass
        try:
            if was_logged_in:
                await broadcast_room(player.room, {"type": "message", "text": f"{player.name} disappears."})
                remove_member(player)
                if dungeon_for_room(player.room):
                    player.room = DUNGEON_ENTRANCE_ROOM
                if name_owners.get(player.name.lower()) == pid:
                    del name_owners[player.name.lower()]
                await _leave_party_on_disconnect(player)
                lvl = get_score_entry(player.name)["level"]
                vlog(f"player {player.name} (lv{lvl}) disconnected")
            else:
                vlog(f"connection {pid} closed before login")
        finally:
            players.pop(pid, None)


async def _run_resilient(task_name, coro_factory):
    """Run a background coroutine forever; restart on crash."""
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{task_name}] crashed with {type(e).__name__}: {e} — "
                  f"restarting in {TASK_RESTART_DELAY}s", flush=True)
            await asyncio.sleep(TASK_RESTART_DELAY)


async def main():
    log_file = os.environ.get("TEXTMMO_LOG_FILE")
    if log_file:
        _logf = open(log_file, "a", buffering=1)
        sys.stdout = _logf
        sys.stderr = _logf
        print(f"[server] logging to {log_file}", flush=True)
    start_dashboard()
    asyncio.create_task(_run_resilient("npc_ai", npc_ai_loop))
    asyncio.create_task(_run_resilient("scores_save", scores_save_loop))
    asyncio.create_task(_run_resilient("dashboard_snapshot", dashboard_refresh_loop))
    # Graceful shutdown (#55): SIGTERM/SIGINT break the wait below so the
    # finally chain runs -- listeners close, player sockets close, scores
    # persist, exit 0. Platforms without handler support fall back to
    # KeyboardInterrupt in __main__.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError, OSError, RuntimeError):
            pass
    try:
        if VERBOSE:
            print(f"Server 0.5 (instanced dungeon, parties, market, GM) on ws://{HOST}:{PORT}")
            print(f"GM stream on ws://{GM_HOST}:{GM_PORT}")
        game_server = await websockets.serve(handle_connection, HOST, PORT)
        gm_server = await websockets.serve(handle_gm_connection, GM_HOST, GM_PORT)
        try:
            await stop.wait()
        finally:
            print("Shutting down: closing listeners...", flush=True)
            game_server.close()
            gm_server.close()
            await game_server.wait_closed()
            await gm_server.wait_closed()
            for p in list(players.values()):
                ws = getattr(p, "ws", None)
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
    finally:
        save_scores()
        print("Scores saved. Bye.", flush=True)


def parse_args(argv=None):
    """CLI flags (#173). Host covers the game port and the dashboard HTTP
    server together; the GM stream stays loopback-only regardless."""
    import argparse
    ap = argparse.ArgumentParser(description="Text MMO engine.")
    ap.add_argument("--host", default=HOST,
                    help="bind address for game + dashboard "
                         "(default 0.0.0.0; use 127.0.0.1 for "
                         "localhost-only training without firewall prompts)")
    ap.add_argument("--port", type=int, default=PORT, help="game port")
    ap.add_argument("--http-port", type=int, default=HTTP_PORT,
                    help="dashboard port")
    ap.add_argument("--gm-port", type=int, default=GM_PORT, help="GM port")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _args = parse_args()
    HOST, PORT = _args.host, _args.port
    HTTP_HOST, HTTP_PORT = _args.host, _args.http_port
    GM_PORT = _args.gm_port
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, OSError) as e:
        print(f"Could not start server: {e}")

