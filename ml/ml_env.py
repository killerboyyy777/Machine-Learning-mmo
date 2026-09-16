"""
A thin, Gym-style wrapper around the text MMO's WebSocket protocol, for
plugging in a machine-learning agent instead of a hand-written bot.

An ML client is NOT special-cased by the server -- it's just another
WebSocket connection speaking the same JSON protocol as any client. This
file only exists to turn that protocol into fixed-size numeric observations
and a small discrete action space, which is the part an RL agent actually
needs and a hand-written bot doesn't.

Reward defaults to the change in the server's own `score` between steps --
including any assist payouts from other players' kills that land on you
asynchronously. This means an RL agent trained against this env is directly
optimizing the same score an anti-hardcoding curve already governs (see
README.md "Scoring"): the agent inherits that pressure for free. On top of
that, two small group-play shaping terms (reward only -- no behavior is
scripted): a per-ally bonus while grouped and a one-time bonus on
joining/forming a party (cooldown-gated so leave/rejoin cycling can't farm
it). Both scale with `diminish_factor`, mirroring the server's difficulty
curve, so they track the score signal instead of dwarfing it late in a run.

Specialist reward modes (constructor flag `reward_mode`, same obs/actions):
"xp" pays raw XP gained plus XP_LEVEL_BONUS per level-up (pure combat/quest
agent, ignores gold); "econ" pays gold_delta plus ECON_INV_LAMBDA times the
merchant-value delta of carried items (pure market/craft/loot agent, ignores
XP). Both are tuned via ml_config.json like the social terms.

Curriculum (flags `curriculum_stage` 0-3 + `curriculum_auto`): progressive
action-space unlock -- rats (0), +dungeons (1), +crafting (2), full
economy (3). Locked actions map to None like any other invalid action, so
masks shrink/expand with the stage. Auto-advance promotes on score
thresholds (CURRICULUM_THRESHOLDS). Default is stage 3 / manual: today's
behavior, byte-identical.

The observation covers everything a fixed-size vector can reasonably carry
about the 0.5 systems (instanced party dungeons, the player market, parties,
and leveling), plus the Town Guard repeatable quest:
  - room one-hot over the static surface world + is_dungeon / floor number
  - exit mask across ALL directions including "up"/"down"/"enter"
  - static NPC + ground-item presence, plus a count of unknown NPCs (dungeon
    guards are spawned dynamically and aren't part of the startup vocab)
  - inventory presence over the known item catalog + unknown-inv flag
  - scalars: hp/gold/score/variety, allies, party size, level + XP progress,
    market order count + presence, equipped-flag,
  - market-tax terms: the server's tax rate, the minimum-tax floor, and the
    exact after-tax net value of our own standing sell orders.
  - quest terms: whether each quest is active, whether its objective is
    ready, which charm mats we hold / whether we hold the charm, and whether
    the quest giver is standing in our room (accept/turn-in both require him).
  - ammo terms: arrow count as a scalar (bows consume 1 arrow per shot, so
    unlike other items the count -- not just presence -- drives decisions).

Market tax (10% with a 1-gold minimum, see server.py TAX_RATE/TAX_MINIMUM)
is first-class here, not an ignored detail:
  - `market_tax(price)` / `market_net(price)` reproduce the server formula
    exactly, so any importer can price the tax into decisions;
  - the observation carries the live tax terms plus our listings' net worth;
  - `step()` returns an info dict with per-step `gold_delta`, the live tax
    terms, any detected buy fill (`market_fill`), and our standing orders
    with nets (`own_orders`) so trainers can attribute tax-aware P&L.

Disposition (quicksell vs hold vs speculate) is decided, not hardcoded:
  - every holding carries merchant value vs best-market-ask margin
    (`flip_margin()`; unlisted items count nominal +1 so first listings do
    price discovery); `sell` takes the lowest margin, `market_post` the
    highest positive one, holding is whatever is picked neither for;
  - keep rules protect worn gear, the quest charm, a last healing herb,
    and bow arrows while low;
  - the observation carries `flip_margin_norm` + `inv_value_norm` and
    `step()` info carries `flip_margin`, so trainers can see (and shape)
    whether each disposition matched the opportunity.

Actions added on top of the old (move/attack/take/rest/look) set:
  - move_up / move_down / move_enter   (dungeon travel; "enter" opens the
    party's private instance from the graveyard)
  - buy / sell / equip / use / craft   (basic gear + consumption loop;
    "craft" builds reinforced_leather, "craft_charm" builds the
    Ancient Guardian Charm for the Town Guard quest, "craft_iron" builds
    Iron Plate Armor from 2x Iron Ore + Wolf Pelt, and "craft_arrows"
    converts 1x Iron Ore into 1 Arrow; "buy" stocks healing
    herbs while "buy_arrows" stocks arrows -- bows like the Oak Longbow
    consume 1 arrow per shot and refuse to fire empty)
  - market_post / market_buy / market_cancel / market_list / market_expand
    (stall slots are capped per seller; market_expand buys +1 for gold)
  - party_invite / party_accept / party_leave / party_info
  - heal                               (full heal, only on Sister Maren's tile)
  - quest_accept / quest_turn_in / quest_list   (Town Guard repeatable quests;
    both quests share the giver/room and the accept -> objective -> turn-in
    pattern. quest_accept/quest_turn_in default to "guard_charm": craft the
    charm from 1x Treant Bark + 1x Troll Hide + 1x Ectoplasm, turn in by the
    guard for fixed XP + gold + score, repeatable. quest2_accept/quest2_turn_in
    drive the "delver" quest instead: clear dungeon floors, report back.)

Two ways to use this:
  - Fresh name every reset() -> standard episodic RL (clean score=0 start
    each episode, matches most RL library assumptions).
  - Same name every reset() -> continual learning against a persistent
    character whose score never resets, which arguably fits this game's
    philosophy (no permanent solution, always more to learn) better than
    artificial episode boundaries. Pick whichever your training setup wants.

Usage (see the __main__ block at the bottom for a full random-agent demo):

    import asyncio
    from ml_env import TextMMOEnv, ACTIONS, N_ACTIONS

    async def main():
        env = TextMMOEnv("MLBot1")
        obs = await env.reset()
        for _ in range(200):
            action = my_policy(obs)          # returns an int in range(N_ACTIONS)
            obs, reward, done, info = await env.step(action)
            if done:
                obs = await env.reset()
        await env.close()

    asyncio.run(main())
"""

import asyncio
import json
import os
import random
import re
import sys

# ml_env.py lives in the ml/ subfolder but reuses the engine's already-loaded
# world data for the observation vocab. Make the repo root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
import server as srv  # reuses the already-loaded world data for vocab

DEFAULT_URL = "ws://localhost:8765"

# --- Fixed vocab, built once from world.json --------------------------------
# Room ids come straight from room events, but NPCs/items are reported by
# display name over the protocol, so we need name -> id lookups to turn
# them into stable indices for a fixed-size observation vector.
ITEM_ID_TO_NAME = {k: v["name"] for k, v in srv.WORLD["items"].items()}
NPC_ID_TO_NAME = {k: v["name"] for k, v in srv.WORLD["npcs"].items()}
NPC_LIST = sorted(NPC_ID_TO_NAME.keys())
ITEM_LIST = sorted(ITEM_ID_TO_NAME.keys())
ROOM_LIST = sorted(srv.ROOMS.keys())

# Shopkeepers and their wares, read live from world.json (server is
# authoritative; this only mirrors it so buy decisions can weigh needs).
MERCHANT_NAMES = sorted({v["name"] for v in srv.WORLD["npcs"].values() if v.get("shop")})
MERCHANT_PRICES = {}
for _npc in srv.WORLD["npcs"].values():
    for _iid, _price in ((_npc.get("shop") or {}).items()):
        MERCHANT_PRICES.setdefault(_iid, _price)

# Stall-slot terms, mirroring server.py (getattr fallbacks as elsewhere).
MARKET_SLOTS_BASE = getattr(srv, "MARKET_ORDER_SLOTS_BASE", 3)
MARKET_SLOT_PRICE_BASE = getattr(srv, "MARKET_SLOT_PRICE_BASE", 50)


def market_slot_price(slots):
    """Gold cost of the next stall slot (doubling from the base price)."""
    return MARKET_SLOT_PRICE_BASE * (2 ** max(0, slots - MARKET_SLOTS_BASE))


def merchant_value(iid):
    """Gold the merchant pays for an item (mirrors cmd_sell)."""
    return srv.ITEM_DEFS.get(iid, {}).get("value", 1) or 1


def best_market_ask(orders, iid, exclude_seller=None):
    """Cheapest open ask for an item from other sellers (None if unlisted)."""
    name = srv.ITEM_DEFS.get(iid, {}).get("name", iid)
    asks = [o["price"] for o in orders or []
            if o.get("item") == name and o.get("seller") != exclude_seller]
    return min(asks) if asks else None


def flip_margin(iid, orders, exclude_seller=None):
    """Expected profit of listing over merchant sale: best ask minus tax
    minus merchant value. None when no comparable ask exists (the market
    hasn't priced it yet -- first listings do price discovery)."""
    ask = best_market_ask(orders, iid, exclude_seller)
    if ask is None:
        return None
    return market_net(ask) - merchant_value(iid)


# Item ids the agent must never liquidate blindly (resolved once; rules
# degrade gracefully to "no exception" if the catalog ever renames them).
_HEALING_HERB_ID = srv.find_item_by_name(list(srv.ITEM_DEFS), "healing herb")

# Display names that must never be attacked (merchants, quest givers,
# healers). Everything else in a room -- hostile static NPCs and unknown
# dynamic ones like dungeon guards -- is a legal target.
NON_HOSTILE_NAMES = {v["name"] for v in srv.WORLD["npcs"].values() if not v.get("hostile")}

# Open-commission list lines look like:
#   #12: slay 5x Giant Rat -- reward 25g + 50xp (posted by Alice)
_COMMISSION_RE = re.compile(
    r"#(\d+):\s*slay\s+(\d+)\s*x\s+(.+?)\s*[-\u2013\u2014]+\s*reward\s+(\d+)\s*g\s*\+\s*(\d+)\s*xp\s*\(\s*posted by\s+([^)]+)\)",
    re.IGNORECASE,
)


def _parse_commissions(text):
    """Best-effort parse of a commission_list message; [] when unparseable."""
    out = []
    for m in _COMMISSION_RE.finditer(text or ""):
        cid, kills, target, gold, xp, poster = m.groups()
        out.append({"id": int(cid), "kills": int(kills), "target": target.strip(),
                    "gold": int(gold), "xp": int(xp), "poster": poster.strip()})
    return out

# Market tax terms, mirroring server.py so the env (and any importer) can
# compute exactly what a trade nets. The getattr fallbacks keep this file
# importable even if the server module ever drops the constants.
TAX_RATE = getattr(srv, "TAX_RATE", 0.10)
TAX_MINIMUM = getattr(srv, "TAX_MINIMUM", 1)

# Server difficulty curve (server.py compute_diminish): marginal score gains
# shrink as total score grows, so the social bonus below is scaled by the
# same factor -- otherwise a constant bonus would dominate the (diminishing)
# score signal late in a run and reward idling in a party over playing well.
DIFFICULTY_K = getattr(srv, "DIFFICULTY_K", 50.0)


def diminish_factor(score):
    """Server's difficulty multiplier at a given total score."""
    return DIFFICULTY_K / (DIFFICULTY_K + max(0.0, score))


# Group-play shaping (reward only -- no behavior is scripted, so agents must
# still discover *how* to group via invite/accept and staying together).
SOCIAL_PER_ALLY = 0.05  # per-step, per ally beyond self, times diminish
FORMATION_BONUS = 0.5  # one-time join/form bonus, times diminish ...
FORMATION_COOLDOWN_STEPS = 500  # ... paid at most this often (env steps), so
# leave/rejoin cycling can't farm it.

# Specialist reward modes (reward only -- observation/action space unchanged).
# "score" (default): change in server score + group-play shaping above.
# "xp": raw XP gained this step + XP_LEVEL_BONUS per level-up. Pure
#   combat/quest signal: ignores gold, score, and social terms.
# "econ": gold_delta + ECON_INV_LAMBDA * inventory-value delta (merchant
#   value of carried items). Pure economic signal: market arbitrage, craft
#   margins, and loot all show up; combat XP does not.
REWARD_MODES = ("score", "xp", "econ")
XP_LEVEL_BONUS = 5.0  # extra reward per level-up in "xp" mode
ECON_INV_LAMBDA = 1.0  # weight of inventory-value delta in "econ" mode

# Curriculum stages (progressive action-space unlock, #59): 0 = surface
# rats (combat/loot/rest/heal/shop/party only), 1 adds dungeon travel and
# the delver quest, 2 adds gathering/crafting and the guard charm quest,
# 3 unlocks the full economy (market + commissions). Gating is mask-level
# (locked actions map to None like any other invalid action), so the same
# policy architecture trains through every stage. Auto-advance promotes on
# score thresholds when curriculum_auto is on; default is stage 3 / manual
# (today's behavior, byte-identical).
CURRICULUM_STAGES = ("rats", "dungeons", "crafting", "full")
CURRICULUM_THRESHOLDS = (0, 10, 30, 60)  # min score to enter each stage
# Unlock sets live below ACTIONS (they match on action names).

# ---------------------------------------------------------------------------
# Optional config file: ml_config.json overrides reward shaping constants.
# ---------------------------------------------------------------------------
_ML_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_config.json")

def _apply_ml_config():
    if not os.path.isfile(_ML_CONFIG_FILE):
        return
    try:
        with open(_ML_CONFIG_FILE) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    g = globals()
    for section in cfg.values():
        if not isinstance(section, dict):
            continue
        for key, val in section.items():
            if key not in g:
                continue
            try:
                g[key] = type(g[key])(val)
            except (TypeError, ValueError) as e:
                print(f"Warning: ml_config.json: skipping {key}={val!r} "
                      f"({e}); keeping default {g[key]!r}")

_apply_ml_config()


def market_tax(price):
    """Gold taken by the treasury on a sale at `price` (server formula:
    10% with a minimum of 1 gold)."""
    return max(TAX_MINIMUM, round(price * TAX_RATE))


def market_net(price):
    """Gold the seller actually receives for a sale at `price`."""
    return price - market_tax(price)


def inventory_value(names):
    """Merchant value of a carried item list (unresolvable names count 0).

    Used by the "econ" reward mode to price loot pickups, crafts, and
    sales into the step reward."""
    total = 0
    for n in names or []:
        iid = srv.find_item_by_name(list(srv.ITEM_DEFS), n)
        if iid:
            total += merchant_value(iid)
    return total

# --- Quest catalog (mirrors server.py QUESTS) --------------------------------
# What the Town Guard quest IS, in one place, so models (and their trainers)
# don't have to reverse-engineer it from reward traces:
#   giver:      Town Guard ("guard") in town_square -- accept AND turn-in both
#               require standing in his room;
#   goal:       craft 1x Ancient Guardian Charm from 1x Treant Bark +
#               1x Troll Hide + 1x Ectoplasm (craft_charm action);
#   reward:     fixed every completion (repeatable): XP + gold + score;
#   flow:       quest_accept -> collect mats -> craft_charm -> quest_turn_in.
# Values below prefer the live server constants (getattr fallbacks keep this
# importable even if the server module ever drops them).
QUEST_GIVER_ID = getattr(srv, "QUEST_GUARD_NPC", "guard")
QUEST_GIVER_NAME = "Town Guard"
QUEST_GIVER_ROOM = "town_square"
QUEST_RESULT_ID = getattr(srv, "QUEST_CHARM_RESULT", "ancient_guardian_charm")
QUEST_INPUT_IDS = list(getattr(srv, "QUEST_CHARM_INPUTS", {"treant_bark": 1, "troll_hide": 1, "ectoplasm": 1}).keys())
QUEST_REWARD_XP = getattr(srv, "QUEST_GUARD_XP", 50)
QUEST_REWARD_GOLD = getattr(srv, "QUEST_GUARD_GOLD", 25)
QUEST_REWARD_POINTS = getattr(srv, "QUEST_GUARD_POINTS", 15)

QUESTS = {
    "guard_charm": {
        "giver_id": QUEST_GIVER_ID,
        "giver_name": QUEST_GIVER_NAME,
        "room": QUEST_GIVER_ROOM,
        "inputs": list(QUEST_INPUT_IDS),
        "result": QUEST_RESULT_ID,
        "reward_xp": QUEST_REWARD_XP,
        "reward_gold": QUEST_REWARD_GOLD,
        "reward_points": QUEST_REWARD_POINTS,
        "repeatable": True,
    },
    "delver": {
        "giver_id": QUEST_GIVER_ID,
        "giver_name": QUEST_GIVER_NAME,
        "room": QUEST_GIVER_ROOM,
        "inputs": [],
        "result": None,
        "reward_xp": getattr(srv, "QUEST_DELVER_XP", 30),
        "reward_gold": getattr(srv, "QUEST_DELVER_GOLD", 15),
        "reward_points": getattr(srv, "QUEST_DELVER_POINTS", 10),
        "repeatable": True,
    },
}

QUEST_DELVER_REWARD_XP = QUESTS["delver"]["reward_xp"]
QUEST_DELVER_REWARD_GOLD = QUESTS["delver"]["reward_gold"]
QUEST_DELVER_REWARD_POINTS = QUESTS["delver"]["reward_points"]


def quest_charm_cost():
    """Merchant value of the charm mats (what selling them would pay).

    Treant Bark 10 + Troll Hide 18 + Ectoplasm 6 = 34 base. Used to teach
    profitability: the charm turn-in pays QUEST_REWARD_GOLD gold, so the
    gold-terms trade is reward minus cost (currently negative -- the quest
    pays in XP/score instead, which is exactly the tradeoff to learn)."""
    need = getattr(srv, "QUEST_CHARM_INPUTS", {"treant_bark": 1, "troll_hide": 1, "ectoplasm": 1})
    total = 0
    for iid, qty in need.items():
        total += srv.ITEM_DEFS.get(iid, {}).get("value", 0) * qty
    return total


def quest_charm_net():
    """Gold-terms P&L of one charm cycle: turn-in gold minus mat value."""
    return QUEST_REWARD_GOLD - quest_charm_cost()


def quest_stage(obs, quest="guard_charm"):
    """Human-readable quest stage for a structured obs dict (debug/logs).

    guard_charm stages: "no_quest", "collect" (active, mats missing),
    "ready_turn_in" (active + charm crafted), "charm_no_quest" (holding a
    charm but no active quest -- accept first, then craft counts).
    delver stages: "no_quest", "collect" (active, floors missing),
    "ready_turn_in" (active + floors cleared).
    """
    if quest == "delver":
        active = bool(obs.get("quest2_active"))
        ready = bool(obs.get("quest2_ready"))
        if active and ready:
            return "ready_turn_in"
        if active:
            return "collect"
        return "no_quest"
    active = bool(obs.get("quest_active"))
    ready = bool(obs.get("quest_ready"))
    has_charm = bool(obs.get("quest_has_charm"))
    if active and ready:
        return "ready_turn_in"
    if active:
        return "collect"
    if has_charm:
        return "charm_no_quest"
    return "no_quest"

# Every direction used anywhere in world.json -- including the dungeon
# travel spellings "up"/"down" and the graveyard dungeon doorway "enter".
DIRECTIONS = sorted({d for room in srv.ROOMS.values() for d in room["exits"]})
DIRECTIONS = sorted(set(DIRECTIONS) | {srv.DUNGEON_ENTRANCE_DIR, "up", "down"})

# Discrete action space. "attack"/"take"/"buy"/... act on whichever npc/item
# is first in the room's list -- a fixed-size discrete space can't easily
# parameterize "attack THIS specific one of N targets" without a more complex
# action head, so this is the practical MVP. Extend ACTION_TO_CMD below if
# you want to add targeted actions, multi-discrete spaces, etc.
#
# Client-verifiable gating: actions whose prerequisites are visibly missing
# (wrong room, no gold, no mats) map to None instead of a guaranteed-error
# command. Agents read valid_action_mask() and only ever pick valid actions,
# so training steps are never wasted on sure rejections. Anything the client
# can't verify (merchant stock, party timing) still goes through and errors
# as a learning signal.

# Cost/location rules mirrored from the server (getattr fallbacks keep this
# importable even if the server module ever drops the constants).
REST_COST = getattr(srv, "REST_COST", 2)
HEAL_COST = getattr(srv, "HEAL_COST", 5)
HEALER_NAME = "Sister Maren"

# Carry-cap mirror (server authoritative; this only gates masks the same way
# cmd_take/gather/buy/market_buy reject: worn gear and up to 5 arrows ride free).
INVENTORY_CAP = getattr(srv, "INVENTORY_CAP", 24)
AMMO_EXEMPT_COUNT = getattr(srv, "AMMO_EXEMPT_COUNT", 5)


def pack_units(state):
    """Pack load in units, mirroring the server's exemptions."""
    inv = state.get("inv_names") or []
    units = len(inv)
    for slot in (state.get("equipped"), state.get("armor"), state.get("offhand")):
        if slot and slot in inv:
            units -= 1
    arrows = sum(1 for n in inv if "arrow" in n.lower())
    units -= min(arrows, AMMO_EXEMPT_COUNT)
    return max(0, units)


def pack_full(state):
    return pack_units(state) >= INVENTORY_CAP
ACTIONS = (
    [f"move_{d}" for d in DIRECTIONS]
    + ["attack", "take", "rest", "look",
       "buy", "sell", "equip", "use", "craft",
       "market_post", "market_buy", "market_cancel", "market_list",
       "party_invite", "party_accept", "party_leave", "party_info",
        # Quest + gear-craft actions appended last so existing indices never shift.
        "quest_accept", "quest_turn_in", "quest_list", "craft_charm",
        "quest2_accept", "quest2_turn_in", "craft_iron", "buy_arrows",
        "craft_arrows",
        # New crafting actions for buffs and ammo.
        "craft_sharpening_oil", "craft_fortitude_tonic", "craft_greater_sharpening_oil",
        "craft_ironhide_draught",
        # Gathering and commission actions.
        "gather", "commission_post", "commission_list", "commission_fill", "commission_cancel",
        # Market stall expansion (buy +1 sell-order slot; fee -> treasury).
        "market_expand",
        # Healer visit (full heal, but only on Sister Maren's tile).
        "heal",
        # Endgame craft: Warden's Trophy + mats -> fixed-damage blade.
        "craft_wardens_blade",
        # Ammo variant crafts (feed the ammo family) + mid-tier blade.
        "craft_iron_arrow", "craft_steel_arrow", "craft_serpentbrand",
        # Shedding load: only valid with a full pack (server rule).
        "drop"]
)
N_ACTIONS = len(ACTIONS)

# Curriculum unlock sets (action names gated below their stage).
STAGE1_UNLOCK = {"move_enter", "move_down", "quest2_accept", "quest2_turn_in"}
STAGE2_UNLOCK = ({"gather", "quest_accept", "quest_turn_in"}
                 | {a for a in ACTIONS if a == "craft" or a.startswith("craft_")})
STAGE3_UNLOCK = {"market_post", "market_buy", "market_cancel", "market_expand",
                 "commission_post", "commission_list", "commission_fill",
                 "commission_cancel"}


def flatten_obs(obs):
    """Turn the structured observation dict into one flat list of floats,
    e.g. for np.array(flatten_obs(obs)) or torch.tensor(...). Kept as a
    plain list (no numpy dependency in this file) so it's usable regardless
    of what ML framework you're using."""
    return (
        obs["room_onehot"]
        + [obs["is_dungeon"], obs["floor_norm"]]
        + obs["exits_mask"]
        + obs["npc_presence"]
        + [obs["npc_unknown_count"]]
        + obs["item_presence"]
        + obs["inv_presence"]
        + [obs["equipped_flag"], obs["inv_unknown_flag"]]
        + [obs["hp_frac"], obs["gold_norm"], obs["score_norm"], obs["variety"],
           obs["allies_norm"], obs["party_norm"], obs["level_norm"],
           obs["xp_progress"], obs["market_norm"], obs["market_any"],
           obs["tax_rate"], obs["tax_min_norm"], obs["own_net_norm"],
           obs["flip_margin_norm"], obs["inv_value_norm"]]
        # Quest block appended last so earlier indices never shift.
        + [obs["quest_active"], obs["quest_ready"], obs["quest_has_charm"],
           obs["quest_mat_bark"], obs["quest_mat_hide"], obs["quest_mat_ecto"],
           obs["quest_giver_here"]]
        + [obs["quest2_active"], obs["quest2_ready"], obs["quest2_giver_here"]]
        + [obs["arrows_norm"]]
        + [obs["buff_attack"], obs["buff_dr"]]
        + [obs["ammo_best_norm"], obs["defense_norm"]]
    )


OBS_SIZE = (
    len(ROOM_LIST) + 2                     # room one-hot + is_dungeon + floor
    + len(DIRECTIONS)                      # exit mask
    + len(NPC_LIST) + 1                    # npc presence + unknown-npc count
    + len(ITEM_LIST) + len(ITEM_LIST) + 2  # ground + inventory presence + flags
    + 15                                   # scalars (hp_frac, gold_norm, score_norm, variety,
                                           # allies_norm, party_norm, level_norm, xp_progress,
                                           # market_norm, market_any, tax_rate, tax_min_norm,
                                           # own_net_norm, flip_margin_norm, inv_value_norm)
    + 7                                    # quest block (active/ready/has_charm/3 mats/giver_here)
    + 3                                    # delver quest block (active/ready/giver_here)
    + 1                                    # arrows_norm (ammo-family count; bows eat one per shot)
    + 2                                    # buff block (attack active, damage-reduction active)
    + 2                                    # gear block (best ammo bonus, worn defense)
)


class TextMMOEnv:
    """One instance = one connected character. Create several instances
    (different names) for multi-agent training; they're independent
    WebSocket connections into the same shared, persistent world, so
    multiple ML agents (and/or bots, and/or humans) can occupy it together
    -- including sharing party dungeons and trading on the same market."""

    def __init__(self, name, url=DEFAULT_URL, step_delay=0.15, max_steps=None,
                 reward_mode="score", curriculum_stage=3, curriculum_auto=False,
                 connect_timeout=10.0, close_timeout=5.0, connect_retries=3):
        if reward_mode not in REWARD_MODES:
            raise ValueError(f"reward_mode must be one of {REWARD_MODES}, got {reward_mode!r}")
        if curriculum_stage not in (0, 1, 2, 3):
            raise ValueError(f"curriculum_stage must be 0-3, got {curriculum_stage!r}")
        self.name = name
        self.url = url
        self.step_delay = step_delay
        self.max_steps = max_steps
        self.reward_mode = reward_mode
        self.curriculum_stage = curriculum_stage
        self.curriculum_auto = curriculum_auto
        # reset() timeouts: a half-dead socket's close handshake (or a
        # stalled listener's accept) must never wedge an agent task
        # forever -- supervisor tasks have no other way out of reset().
        self.connect_timeout = connect_timeout
        self.close_timeout = close_timeout
        self.connect_retries = connect_retries
        self.ws = None
        self._reader_task = None
        self._state = {
            "room_id": None, "exits": [], "npc_names": [], "item_names": [],
            "player_names": [], "is_dungeon": False, "dungeon_floor": 0,
            "party_size": 1,
            "hp": 0, "max_hp": 1, "gold": 0, "score": 0.0, "variety": 1.0,
            "level": 1, "xp": 0.0, "xp_to_next": 100.0,
            "equipped": None, "armor": None, "offhand": None, "defense": 0,
            "inv_names": [],
            "market_orders": 0, "market_state": None,
            "tax_rate": TAX_RATE, "tax_min": TAX_MINIMUM,
            "other_players": 0,
            # Quest state, mirrored from the server's stats event. Until the
            # first stats lands these stay False (no quest assumed).
            "quest_guard_active": False, "guard_charm_crafted": False,
            "quest_delver_active": False, "quest_delver_ready": False,
            "buff_attack_amount": 0, "buff_damage_reduction_amount": 0,
            # Market/gathering/commission mirrors (server is authoritative;
            # these only let action mapping see what events already said).
            "room_gold": 0, "gatherables": [],
            "market_slots": 3, "open_commissions": [],
        }
        self._pending_reward = 0.0
        self._pending_xp = 0.0  # accumulated "xp" event gains (for "xp" mode)
        self._pending_levels = 0  # accumulated "level_up" events (for "xp" mode)
        self._step_count = 0
        self._last_formation_step = -10 ** 9  # paid-formation cooldown cursor
        self._mask_cache = None  # refreshed by every _build_obs()
        self._inv_type_cache = {}  # ditto: item-type -> first display name
        self._flip_table = []  # ditto: per-holding value/margin rows

    async def _reader(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                t = event.get("type")
                if t == "room":
                    self._state["room_id"] = event["id"]
                    self._state["exits"] = event["exits"]
                    self._state["npc_names"] = event["npcs"]
                    self._state["item_names"] = event["items"]
                    self._state["room_gold"] = event.get("gold", 0) or 0
                    self._state["gatherables"] = event.get("gatherables") or []
                    self._state["player_names"] = event["players"]
                    self._state["is_dungeon"] = event.get("is_dungeon", False)
                    self._state["dungeon_floor"] = event.get("dungeon_floor") or 0
                    self._state["party_size"] = event.get("party_size", 1)
                    self._state["other_players"] = max(0, len(event["players"]) - 1)
                elif t == "stats":
                    self._state["hp"] = event["hp"]
                    self._state["max_hp"] = event["max_hp"]
                    self._state["gold"] = event["gold"]
                    self._state["score"] = event["score"]
                    self._state["variety"] = event.get("variety", 1.0) or 1.0
                    self._state["level"] = event.get("level", 1)
                    self._state["xp"] = event.get("xp", 0.0)
                    self._state["xp_to_next"] = event.get("xp_to_next", 100.0)
                    self._state["party_size"] = event.get("party_size", self._state["party_size"])
                    self._state["inv_names"] = event.get("inv", [])
                    self._state["equipped"] = event.get("equipped")
                    self._state["armor"] = event.get("armor")
                    self._state["offhand"] = event.get("offhand")
                    self._state["defense"] = event.get("defense", 0)
                    self._state["market_orders"] = event.get("market_orders", 0)
                    self._state["market_slots"] = event.get("market_slots", self._state.get("market_slots", 3))
                    # Quest flags ride along on every stats event (server's
                    # stats_view always includes them now).
                    if "quest_guard_active" in event:
                        self._state["quest_guard_active"] = bool(event["quest_guard_active"])
                    if "guard_charm_crafted" in event:
                        self._state["guard_charm_crafted"] = bool(event["guard_charm_crafted"])
                    if "quest_delver_active" in event:
                        self._state["quest_delver_active"] = bool(event["quest_delver_active"])
                    if "quest_delver_ready" in event:
                        self._state["quest_delver_ready"] = bool(event["quest_delver_ready"])
                    # Crafted buffs ride along on every stats event as a
                    # {category: {amount, remaining}} dict (server's
                    # stats_view always includes it now).
                    buffs = event.get("buffs") or {}
                    atk = buffs.get("attack") or {}
                    dr = buffs.get("damage_reduction") or {}
                    self._state["buff_attack_amount"] = int(atk.get("amount", 0)) if atk.get("remaining", 0) > 0 else 0
                    self._state["buff_damage_reduction_amount"] = int(dr.get("amount", 0)) if dr.get("remaining", 0) > 0 else 0
                elif t == "score":
                    # Covers assist payouts from other players' kills too --
                    # those can arrive at any time, not just right after our
                    # own action, which is why reward is accumulated here
                    # rather than only diffed inside step().
                    self._pending_reward += event["gained"]
                    self._state["score"] = event["total"]
                elif t == "market":
                    self._state["market_state"] = event
                    self._state["tax_rate"] = event.get("tax_rate", self._state["tax_rate"])
                    self._state["tax_min"] = event.get("tax_min", self._state["tax_min"])
                elif t == "party":
                    self._state["party_info"] = event
                elif t == "message":
                    # Only structured use: commission_list replies, parsed
                    # best-effort into the open-bounty list (stale until the
                    # next list call; fill/cancel treat it as advisory).
                    text = event.get("text", "")
                    if "Open commissions:" in text:
                        self._state["open_commissions"] = _parse_commissions(text)
                    elif "No open commissions" in text:
                        self._state["open_commissions"] = []
                elif t == "xp":
                    self._pending_xp += event.get("gained", 0.0) or 0.0
                    self._state["level"] = event.get("level", self._state["level"])
                    self._state["xp"] = event.get("total", self._state["xp"])
                    self._state["xp_to_next"] = event.get("xp_to_next", self._state["xp_to_next"])
                elif t == "level_up":
                    self._pending_levels += 1
                    self._state["level"] = event.get("level", self._state["level"])
                elif t == "death":
                    self._state["hp"] = self._state["max_hp"]
                elif t == "error":
                    # Could log or track error rate
                    pass
        except websockets.ConnectionClosed:
            pass

    async def _send(self, cmd, **kwargs):
        await self.ws.send(json.dumps({"cmd": cmd, **kwargs}))

    async def _close_ws(self):
        """Drop the current socket, never hanging: cancel the reader, then
        close with a timeout; abandon the socket on any failure."""
        ws, self.ws = self.ws, None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), self.close_timeout)
        except Exception:
            pass

    async def reset(self):
        await self._close_ws()

        last_error = None
        for _ in range(max(1, self.connect_retries)):
            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.url), self.connect_timeout)
                break
            except Exception as e:
                last_error = e
                self.ws = None
        else:
            raise ConnectionError(
                f"{self.name}: connect failed after {self.connect_retries} tries: {last_error}")

        self._reader_task = asyncio.create_task(self._reader())
        await self._send("login", name=self.name)
        await self._send("stats")
        await self._send("market_list")
        await asyncio.sleep(self.step_delay * 2)  # let the initial snapshot land

        self._pending_reward = 0.0
        self._pending_xp = 0.0
        self._pending_levels = 0
        self._step_count = 0
        return self._build_obs()

    async def step(self, action_idx):
        action = ACTIONS[action_idx]
        cmd = self._action_to_cmd(action)
        gold_before = self._state["gold"]
        inv_before = list(self._state["inv_names"] or [])
        inv_value_before = inventory_value(inv_before)
        quest_active_before = bool(self._state.get("quest_guard_active"))
        quest_crafted_before = bool(self._state.get("guard_charm_crafted"))
        delver_active_before = bool(self._state.get("quest_delver_active"))
        delver_ready_before = bool(self._state.get("quest_delver_ready"))
        party_before = self._state.get("party_size", 1)
        episode_done = False
        if cmd:
            try:
                await self._send(**cmd)
            except websockets.ConnectionClosed:
                # Either side can end the connection (server restart/kick,
                # clean handshake, dropped socket). End the episode instead
                # of crashing the whole farm process.
                episode_done = True
        await asyncio.sleep(self.step_delay)

        xp_gained = self._pending_xp
        levels_gained = self._pending_levels
        gold_delta = self._state["gold"] - gold_before
        inv_delta = inventory_value(self._state["inv_names"]) - inv_value_before
        reward = self._compute_reward(
            self._pending_reward, xp_gained, levels_gained,
            gold_delta, inv_delta, party_before)
        self._pending_reward = 0.0
        self._pending_xp = 0.0
        self._pending_levels = 0
        self._step_count += 1
        self._maybe_advance_curriculum()
        if episode_done:
            done = True
            obs = self._build_obs()
        else:
            done = self.max_steps is not None and self._step_count >= self.max_steps
            obs = self._build_obs()
        quest_active_after = bool(self._state.get("quest_guard_active"))
        quest_crafted_after = bool(self._state.get("guard_charm_crafted"))
        delver_active_after = bool(self._state.get("quest_delver_active"))
        delver_ready_after = bool(self._state.get("quest_delver_ready"))
        guard_accepted = (not quest_active_before) and quest_active_after
        guard_turned = quest_active_before and (not quest_active_after)
        guard_crafted = (not quest_crafted_before) and quest_crafted_after
        delver_accepted = (not delver_active_before) and delver_active_after
        delver_turned = delver_active_before and (not delver_active_after)
        delver_became_ready = (not delver_ready_before) and delver_ready_after
        next_obs = self._build_obs()
        info = {
            "action": action,
            "gold_delta": gold_delta,
            "reward_mode": self.reward_mode,
            "curriculum_stage": self.curriculum_stage,
            "xp_gained": xp_gained,
            "levels_gained": levels_gained,
            "inv_delta": inv_delta,
            "tax_rate": self._state["tax_rate"],
            "tax_min": self._state["tax_min"],
            # Buy fill detected this step (or None): {"side": "buy", "cost": N}.
            "market_fill": self._detect_fill(action, gold_before, inv_before),
            # Our standing sell orders with exact after-tax net. Snapshot may
            # be stale until the next market_list; trainers should diff ids
            # across steps to spot completed sales.
            "own_orders": self._own_orders(),
            # Best list-over-merchant margin currently held (0 when nothing
            # sellable beats the merchant): trainers can attribute whether a
            # sell/post/hold step matched the opportunity.
            "flip_margin": max(
                [(r["margin"] if r["margin"] is not None else 1) for r in self._holdings()
                 if self._sellable(r)], default=0),
            # Quest transitions this step, for reward shaping / logging.
            # accepted: False->True on quest_accept; turned_in: True->False
            # on quest_turn_in (reward lands via score/xp/gold events);
            # crafted_charm: charm flag flipped (or charm newly in inv).
            # Top-level keys are any-quest (backward compatible); by_quest
            # breaks them out per quest id for multi-quest shaping.
            "quest": {
                "accepted": guard_accepted or delver_accepted,
                "turned_in": guard_turned or delver_turned,
                "crafted_charm": guard_crafted,
                "delver_became_ready": delver_became_ready,
                "active": quest_active_after or delver_active_after,
                "ready": quest_crafted_after or delver_ready_after,
                "stage": quest_stage(next_obs),
                "stage2": quest_stage(next_obs, "delver"),
                "by_quest": {
                    "guard_charm": {
                        "accepted": guard_accepted,
                        "turned_in": guard_turned,
                        "crafted_charm": guard_crafted,
                    },
                    "delver": {
                        "accepted": delver_accepted,
                        "turned_in": delver_turned,
                        "became_ready": delver_became_ready,
                    },
                },
            },
        }
        done = episode_done or (self.max_steps is not None and self._step_count >= self.max_steps)
        info["action_mask"] = 1 if cmd is not None else 0
        return next_obs, reward, done, info

    def _compute_reward(self, score_gain, xp_gain, levels, gold_delta,
                          inv_delta, party_before):
        """Step reward for the configured `reward_mode`, from accumulated
        event pendings and state diffs (pure function of its arguments plus
        the social/formation state -- no I/O, unit-testable).

        - "xp": raw XP plus XP_LEVEL_BONUS per level-up. Ignores gold,
          score, and social terms by design (pure combat/quest agent).
        - "econ": gold flow plus ECON_INV_LAMBDA times carried-value flow.
          Ignores XP and score by design (pure market/craft/loot agent).
        - "score" (default): server score gain plus group-play shaping."""
        if self.reward_mode == "xp":
            return xp_gain + XP_LEVEL_BONUS * levels
        if self.reward_mode == "econ":
            return gold_delta + ECON_INV_LAMBDA * inv_delta
        reward = score_gain
        # Social reward: only when the agent is grouped with other connected
        # players (party members are physically close by design, and
        # other_players > 0 means other bots/agents are in the world).
        # Scaled by the server's difficulty curve so it tracks the score
        # signal instead of dwarfing it late in a run, and by group size so
        # bigger parties pay more than duos.
        if self._state.get("other_players", 0) > 0 and self._state.get("party_size", 1) > 1:
            allies = self._state.get("party_size", 1) - 1
            reward += SOCIAL_PER_ALLY * allies * diminish_factor(self._state.get("score", 0.0))
        # Formation bonus: joining/forming a party pays once per cooldown so
        # the act of grouping is discoverable through the reward trace.
        party_now = self._state.get("party_size", 1)
        if party_now > 1 and party_before <= 1:
            if self._step_count - self._last_formation_step >= FORMATION_COOLDOWN_STEPS:
                reward += FORMATION_BONUS * diminish_factor(self._state.get("score", 0.0))
                self._last_formation_step = self._step_count
        return reward

    def _own_orders(self):
        """Our standing sell orders with exact after-tax net, from the latest
        `market` snapshot."""
        ms = self._state.get("market_state") or {}
        out = []
        for o in ms.get("orders") or []:
            if o.get("seller") == self.name:
                price = o.get("price", 0)
                out.append({"id": o.get("id"), "price": price, "net": market_net(price)})
        return out

    def _holdings(self):
        """One row per inventory item: display name, id, merchant value, and
        market flip margin (None = unlisted, i.e. price discovery).

        Cached per observation in _build_obs; mappings read the table instead
        of re-scanning inventory x open orders on every mask evaluation."""
        return getattr(self, "_flip_table", [])

    def _sellable(self, row):
        """Keep rules shared by quicksell and speculate: never worn gear,
        never the quest charm, never a last healing herb, never bow arrows
        while running low."""
        s = self._state
        if row["name"] in {s.get("equipped"), s.get("armor"), s.get("offhand")} - {None}:
            return False
        if row["iid"] == QUEST_RESULT_ID:
            return False
        if _HEALING_HERB_ID and row["iid"] == _HEALING_HERB_ID:
            if sum(1 for r in self._holdings() if r["iid"] == _HEALING_HERB_ID) <= 1:
                return False
        if "arrow" in row["name"].lower():
            equipped_iid = srv.find_item_by_name(list(srv.ITEM_DEFS), s.get("equipped") or "")
            if equipped_iid and srv.ITEM_DEFS.get(equipped_iid, {}).get("ammo"):
                arrows = sum(1 for r in self._holdings() if "arrow" in r["name"].lower())
                if arrows <= 5:
                    return False
        return True

    def _refresh_holdings(self):
        s = self._state
        orders = ((s.get("market_state") or {}).get("orders") or [])
        rows = []
        for name in s.get("inv_names") or []:
            iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
            if not iid:
                continue
            rows.append({"name": name, "iid": iid,
                         "value": merchant_value(iid),
                         "margin": flip_margin(iid, orders, exclude_seller=self.name)})
        self._flip_table = rows

    def _detect_fill(self, action, gold_before, inv_before):
        """Heuristic fill detection for our own market_buy: inventory grew and
        gold dropped => we bought at gold_before - gold_after."""
        if action != "market_buy":
            return None
        after = self._state["inv_names"] or []
        if len(after) > len(inv_before):
            return {"side": "buy", "cost": max(0.0, gold_before - self._state["gold"])}
        return None

    async def close(self):
        await self._close_ws()

    def _first_inv_typed(self, item_type, exclude_equipped=True):
        """First inventory display name of a given item type (or None).

        Memoized per observation (cleared in _build_obs): mask computation
        calls this dozens of times per step for the same unchanged state."""
        if exclude_equipped and item_type in self._inv_type_cache:
            return self._inv_type_cache[item_type]
        for name in self._state["inv_names"] or []:
            iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
            if not iid:
                continue
            if srv.ITEM_DEFS[iid].get("type") != item_type:
                continue
            if exclude_equipped and iid == self._state["equipped"]:
                continue
            if exclude_equipped:
                self._inv_type_cache[item_type] = name
            return name
        if exclude_equipped:
            self._inv_type_cache[item_type] = None
        return None

    def _first_ground_item(self):
        return (self._state["item_names"] or [None])[0]

    def valid_action_mask(self):
        """1/0 per action in ACTIONS order: 1 when the action maps to a real
        command in the current state (never a guaranteed-error pick).

        Served from the per-observation cache refreshed by _build_obs
        (computed on demand before the first observation lands)."""
        if getattr(self, "_mask_cache", None) is None:
            for attr, default in (("_flip_table", []), ("_inv_type_cache", {})):
                if not hasattr(self, attr):
                    setattr(self, attr, default)
            self._refresh_holdings()
            self._mask_cache = [1 if self._action_to_cmd(a) is not None else 0 for a in ACTIONS]
        return list(self._mask_cache)

    def _curriculum_gated(self, action):
        """True when the action is locked below the current curriculum stage."""
        stage = self.curriculum_stage
        if stage >= 3:
            return False
        if stage < 1 and action in STAGE1_UNLOCK:
            return True
        if stage < 2 and action in STAGE2_UNLOCK:
            return True
        return stage < 3 and action in STAGE3_UNLOCK

    def _maybe_advance_curriculum(self):
        """Promote through score thresholds when curriculum_auto is on."""
        if not self.curriculum_auto:
            return
        th = CURRICULUM_THRESHOLDS
        if len(th) < 4:
            return  # misconfigured thresholds: stay put instead of crashing
        score = self._state.get("score", 0.0)
        while (self.curriculum_stage < 3
               and score >= th[self.curriculum_stage + 1]):
            self.curriculum_stage += 1

    def _action_to_cmd(self, action):
        s = self._state
        if self._curriculum_gated(action):
            return None
        if action.startswith("move_"):
            # Only exits that actually exist here; walking into walls would
            # just bounce off a server error.
            if action[len("move_"):] in (s.get("exits") or []):
                return {"cmd": "move", "dir": action[len("move_"):]}
            return None
        if action == "rest":
            # Rest areas only, and only if we can pay the fee.
            if srv.ROOMS.get(s.get("room_id"), {}).get("rest_area") and s.get("gold", 0) >= REST_COST:
                return {"cmd": "rest"}
            return None
        if action == "heal":
            # Sister Maren's tile only, and only if we can pay her fee.
            if HEALER_NAME in (s.get("npc_names") or []) and s.get("gold", 0) >= HEAL_COST:
                return {"cmd": "heal"}
            return None
        if action == "attack":
            # First hostile thing only: never punch merchants, quest givers,
            # or healers. Unknown/dynamic names (dungeon guards) count as
            # hostile -- players ride the separate player_names list.
            for name in s["npc_names"] or []:
                if name not in NON_HOSTILE_NAMES:
                    return {"cmd": "attack", "target": name}
            return None
        if action == "take":
            # Loose gold piles first (fungible, no merchant trip needed, and
            # gold ignores the pack cap), then the first ground item.
            if s.get("room_gold", 0) > 0:
                return {"cmd": "take", "item": "gold"}
            if pack_full(s):
                return None
            item = self._first_ground_item()
            if item:
                return {"cmd": "take", "item": item}
            return None
        if action == "drop":
            # Shed the cheapest sellable unit -- but only with a full pack,
            # mirroring the server rule (below the cap this always errors).
            if not pack_full(s):
                return None
            cands = sorted(
                ((r["value"], r["name"]) for r in self._holdings() if self._sellable(r)),
                key=lambda t: t[0],
            )
            if not cands:
                return None
            return {"cmd": "drop", "item": cands[0][1]}
        if action == "look":
            return {"cmd": "look"}
        if action == "buy":
            # Need-aware, cheapest-first: a weapon when barehanded, a healing
            # herb when hurt or herb-less, arrows when the wielded bow runs
            # low. Merchant presence and gold still validated server-side.
            if MERCHANT_NAMES and not any(m in (s.get("npc_names") or []) for m in MERCHANT_NAMES):
                return None
            if pack_full(s):
                return None
            gold = s.get("gold", 0)

            def _cheapest(pred):
                cands = [(iid, MERCHANT_PRICES[iid]) for iid in MERCHANT_PRICES if pred(iid)]
                return min(cands, key=lambda t: t[1], default=(None, None))

            if not s.get("equipped"):
                iid, price = _cheapest(lambda i: srv.ITEM_DEFS.get(i, {}).get("type") == "weapon")
                if iid and gold >= price:
                    return {"cmd": "buy", "item": srv.ITEM_DEFS[iid]["name"]}
            herbs = sum(1 for n in (s.get("inv_names") or []) if "healing herb" in n.lower())
            iid, price = _cheapest(lambda i: srv.ITEM_DEFS.get(i, {}).get("type") == "consumable")
            if iid and gold >= price and (s.get("hp", 1) < s.get("max_hp", 1) or herbs == 0):
                return {"cmd": "buy", "item": srv.ITEM_DEFS[iid]["name"]}
            equipped_iid = srv.find_item_by_name(list(srv.ITEM_DEFS), s.get("equipped") or "")
            if equipped_iid and srv.ITEM_DEFS.get(equipped_iid, {}).get("ammo"):
                arrows = sum(1 for n in (s.get("inv_names") or []) if "arrow" in n.lower())
                iid, price = _cheapest(lambda i: "arrow" in srv.ITEM_DEFS.get(i, {}).get("name", "").lower())
                if iid and arrows < 5 and gold >= price:
                    return {"cmd": "buy", "item": srv.ITEM_DEFS[iid]["name"]}
            return None
        if action == "buy_arrows":
            # Stock ammunition for bows (Oak Longbow consumes 1 arrow per
            # shot; attacking empty-handed errors). Pack-full buys bounce
            # server-side; merchant presence and gold still validated there.
            if pack_full(s):
                return None
            return {"cmd": "buy", "item": "arrow"}
        if action == "sell":
            # Quicksell the lowest-margin holding: when nothing carries a
            # market premium, merchant gold now beats waiting on a listing.
            # Keep rules: worn gear, the quest charm, a last healing herb,
            # and bow arrows while low never quicksell.
            cands = [r for r in self._holdings() if self._sellable(r)]
            if not cands:
                return None
            cands.sort(key=lambda r: (r["margin"] if r["margin"] is not None else -1, -r["value"]))
            return {"cmd": "sell", "item": cands[0]["name"]}
        if action == "equip":
            # One action fills all three gear slots over successive steps:
            # weapon first, then armor, then offhand. The server routes each
            # piece by type, so a single generic command covers the full kit.
            if not s.get("equipped"):
                name = self._first_inv_typed("weapon")
                if name:
                    return {"cmd": "equip", "item": name}
            if not s.get("armor"):
                name = self._first_inv_typed("armor")
                if name:
                    return {"cmd": "equip", "item": name}
            if not s.get("offhand"):
                name = self._first_inv_typed("offhand")
                if name:
                    return {"cmd": "equip", "item": name}
            return None
        if action == "use":
            name = self._first_inv_typed("consumable")
            if name:
                return {"cmd": "use", "item": name}
            return None
        if action == "craft":
            # Builds reinforced_leather (wolf_pelt + rat tails). The check
            # above makes sure we only try when we actually have the mats.
            return {"cmd": "craft", "recipe": "reinforced_leather"}
        if action == "craft_charm":
            # Builds the Ancient Guardian Charm for the Town Guard quest
            # (1x Treant Bark + 1x Troll Hide + 1x Ectoplasm). Gate on mats
            # so the model doesn't waste steps on guaranteed-error crafts;
            # a missing-mat step becomes a no-op like other gated actions.
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = getattr(srv, "QUEST_CHARM_INPUTS", {"treant_bark": 1, "troll_hide": 1, "ectoplasm": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "ancient_guardian_charm"}
            return None
        if action == "craft_iron":
            # Builds Iron Plate Armor (now an armor-slot piece: defense 3).
            # Same mat gating as craft_charm; reads the live recipe so
            # world.json stays the single source of truth.
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("iron_plate", {}).get("inputs", {"iron_ore": 2, "wolf_pelt": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "iron_plate"}
            return None
        if action == "craft_arrows":
            # One Iron Ore produces one Arrow through the server's generic
            # recipe system. Gate on ore so the agent avoids guaranteed errors.
            if any(srv.find_item_by_name(list(srv.ITEM_DEFS), name) == "iron_ore"
                   for name in (s["inv_names"] or [])):
                return {"cmd": "craft", "recipe": "arrows"}
            return None
        if action == "craft_sharpening_oil":
            # 1x Iron Ore + 1x Wolf Pelt -> 1 Sharpening Oil (tier 2, attack +2)
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("sharpening_oil", {}).get("inputs", {"iron_ore": 1, "wolf_pelt": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "sharpening_oil"}
            return None
        if action == "craft_fortitude_tonic":
            # 1x Iron Ore + 1x Mountain Berry -> 1 Fortitude Tonic (tier 2, damage reduction +1)
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("fortitude_tonic", {}).get("inputs", {"iron_ore": 1, "mountain_berry": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "fortitude_tonic"}
            return None
        if action == "craft_greater_sharpening_oil":
            # 2x Iron Ore + 1x Serpent Scale -> 1 Greater Sharpening Oil (tier 3, attack +4)
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("greater_sharpening_oil", {}).get("inputs", {"iron_ore": 2, "serpent_scale": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "greater_sharpening_oil"}
            return None
        if action == "craft_ironhide_draught":
            # 1x Troll Hide + 1x Iron Ore + 1x Ectoplasm -> 1 Ironhide Draught (tier 3, damage reduction +2)
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("ironhide_draught", {}).get("inputs", {"troll_hide": 1, "iron_ore": 1, "ectoplasm": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "ironhide_draught"}
            return None
        if action == "craft_wardens_blade":
            # 1x Warden's Trophy + 2x Iron Ore + 1x Serpent Scale -> Warden's
            # Blade (tier 4, fixed damage 13, never scaling).
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("wardens_blade", {}).get("inputs", {"warden_trophy": 1, "iron_ore": 2, "serpent_scale": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "wardens_blade"}
            return None
        if action == "craft_iron_arrow":
            # 1x Iron Ore -> 1 Iron Arrow (+1 bow damage). Same gating pattern.
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("iron_arrows", {}).get("inputs", {"iron_ore": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "iron_arrows"}
            return None
        if action == "craft_steel_arrow":
            # 2x Iron Ore + 1x Serpent Scale + 1x Heron Feather -> Steel Arrow (+2).
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("steel_arrows", {}).get("inputs", {"iron_ore": 2, "serpent_scale": 1, "heron_feather": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "steel_arrows"}
            return None
        if action == "craft_serpentbrand":
            # 2x Serpent Scale + 2x Iron Ore + 1x Resin -> Serpentbrand (10 dmg).
            have = {}
            for name in s["inv_names"] or []:
                iid = srv.find_item_by_name(list(srv.ITEM_DEFS), name)
                if iid:
                    have[iid] = have.get(iid, 0) + 1
            need = srv.RECIPES.get("serpentbrand", {}).get("inputs", {"serpent_scale": 2, "iron_ore": 2, "resin": 1})
            if all(have.get(iid, 0) >= qty for iid, qty in need.items()):
                return {"cmd": "craft", "recipe": "serpentbrand"}
            return None
        if action == "gather":
            # Only when the latest room snapshot shows nodes; the server
            # picks the first available one (a depleted pick just errors).
            # A full pack bounces server-side without consuming the node,
            # so skip it here too.
            if pack_full(s):
                return None
            if s.get("gatherables"):
                return {"cmd": "gather"}
            return None
        if action == "commission_post":
            # Post a new escrowed bounty
            return {"cmd": "commission_post"}
        if action == "commission_list":
            # List open commissions
            return {"cmd": "commission_list"}
        if action == "commission_fill":
            # Fill the richest open bounty not posted by us (server still
            # verifies kills and rejects self-deals; its error is signal).
            cands = [c for c in (s.get("open_commissions") or []) if c["poster"] != self.name]
            if not cands:
                return None
            best = max(cands, key=lambda c: (c["gold"] + c["xp"], -c["id"]))
            return {"cmd": "commission_fill", "commission_id": best["id"]}
        if action == "commission_cancel":
            # Cancel our own oldest open bounty, if the last listing showed one.
            mine = [c for c in (s.get("open_commissions") or []) if c["poster"] == self.name]
            if not mine:
                return None
            return {"cmd": "commission_cancel", "commission_id": min(m["id"] for m in mine)}
        if action == "quest_accept":
            # Accept the Town Guard's guard_charm quest. The server requires
            # standing in the guard's room; sending it elsewhere just yields
            # an error (and a look-resync), which is itself a learning signal.
            return {"cmd": "quest", "action": "accept", "quest": "guard_charm"}
        if action == "quest_turn_in":
            # Turn in the crafted charm for the fixed XP + gold + score.
            # Likewise requires the guard's room; the server validates.
            return {"cmd": "quest", "action": "turn_in", "quest": "guard_charm"}
        if action == "quest2_accept":
            # Accept the Depth Delver quest (clear dungeon floors).
            return {"cmd": "quest", "action": "accept", "quest": "delver"}
        if action == "quest2_turn_in":
            # Turn in cleared floors for the fixed XP + gold + score.
            return {"cmd": "quest", "action": "turn_in", "quest": "delver"}
        if action == "quest_list":
            # Read the unified quest catalog (what/where/state).
            return {"cmd": "quest", "action": "list"}
        if action == "market_post":
            # Speculate on the highest-margin holding: list only when the
            # market beats the merchant (margin > 0) or the item is unlisted
            # (nominal +1: first listings do price discovery). Stall cap
            # respected -- expand instead once full. Same keep rules as sell.
            own_open = sum(1 for o in ((s.get("market_state") or {}).get("orders") or [])
                           if o.get("seller") == self.name)
            if own_open >= s.get("market_slots", MARKET_SLOTS_BASE):
                return None
            cands = [(r["margin"] if r["margin"] is not None else 1, r["value"], r["name"])
                     for r in self._holdings() if self._sellable(r)]
            cands = [c for c in cands if c[0] > 0]
            if not cands:
                return None
            cands.sort(key=lambda c: (-c[0], -c[1]))
            return {"cmd": "market_post", "item": cands[0][2], "price": 0}
        if action == "market_buy":
            # A full pack bounces server-side (gold never charged), so skip.
            if pack_full(s):
                return None
            return {"cmd": "market_buy"}
        if action == "market_cancel":
            # Cancel our own cheapest standing order if we have any.
            ms = s.get("market_state")
            if ms and ms.get("orders"):
                mine = [o for o in ms["orders"] if o["seller"] == self.name]
                if mine:
                    return {"cmd": "market_cancel", "id": mine[0]["id"]}
            return None
        if action == "market_list":
            return {"cmd": "market_list"}
        if action == "market_expand":
            # Buy +1 stall slot, but only when the stall is actually full
            # and the next slot is affordable (mirrors the server's doubling
            # price from the 50g base; the server has the final word).
            slots = s.get("market_slots", MARKET_SLOTS_BASE)
            own_open = sum(1 for o in ((s.get("market_state") or {}).get("orders") or [])
                           if o.get("seller") == self.name)
            if own_open < slots:
                return None
            if s.get("gold", 0) >= market_slot_price(slots):
                return {"cmd": "market_expand"}
            return None
        if action == "party_invite":
            # Invite the first other player visible in the room.
            for pname in s.get("player_names") or []:
                if pname != self.name:
                    return {"cmd": "party_invite", "target": pname}
            return None
        if action == "party_accept":
            return {"cmd": "party_accept"}
        if action == "party_leave":
            return {"cmd": "party_leave"}
        if action == "party_info":
            return {"cmd": "party_info"}
        return None

    def _build_obs(self):
        s = self._state
        room_onehot = [1.0 if rid == s["room_id"] else 0.0 for rid in ROOM_LIST]
        is_dungeon = 1.0 if s["is_dungeon"] else 0.0
        floor_norm = min(s["dungeon_floor"], 20) / 20.0

        exits_mask = [1.0 if d in s["exits"] else 0.0 for d in DIRECTIONS]
        static_names = set(NPC_ID_TO_NAME.values())
        unknown_npcs = [n for n in s["npc_names"] if n not in static_names]
        npc_presence = [1.0 if NPC_ID_TO_NAME[nid] in s["npc_names"] else 0.0 for nid in NPC_LIST]
        item_ids = {v: k for k, v in ITEM_ID_TO_NAME.items()}
        item_presence = [1.0 if item_ids.get(i, None) in s["item_names"] else 0.0 for i in ITEM_LIST]

        inv_presence = [1.0 if item_ids.get(i, None) in (s["inv_names"] or []) else 0.0 for i in ITEM_LIST]
        equipped_flag = 1.0 if s["equipped"] else 0.0
        inv_unknown = 1.0 if any(n not in ITEM_ID_TO_NAME.values() for n in (s["inv_names"] or [])) else 0.0

        ms = s.get("market_state")
        market_norm = min(s["market_orders"], 20) / 20.0
        market_any = 1.0 if (ms and ms.get("orders")) else 0.0
        orders = (ms or {}).get("orders") or []
        own_net = sum(
            market_net(o.get("price", 0))
            for o in orders
            if o.get("seller") == self.name
        )

        # --- Disposition features: quicksell vs hold vs speculate ---
        # Best flip margin across sellable holdings (unknown-ask items count
        # +1 nominal: first listings do price discovery) and total merchant
        # value of everything carried. These let the policy learn *which*
        # disposition pays instead of acting blind.
        self._refresh_holdings()
        sellable = [r for r in self._flip_table if self._sellable(r)]
        best_margin = max([(r["margin"] if r["margin"] is not None else 1)
                           for r in sellable], default=0)
        flip_margin_norm = min(max(0.0, best_margin) / 50.0, 1.0)
        inv_value_norm = min(sum(r["value"] for r in self._flip_table) / 200.0, 1.0)

        # --- Quest features: what the quest is + where we stand in it ---
        # Active/ready come straight from the server's stats event; the rest
        # are derived locally so the model sees *why* it can/can't progress.
        quest_active = 1.0 if s.get("quest_guard_active") else 0.0
        quest_ready = 1.0 if s.get("guard_charm_crafted") else 0.0
        inv_ids = set()
        for n in (s["inv_names"] or []):
            iid = srv.find_item_by_name(list(srv.ITEM_DEFS), n)
            if iid:
                inv_ids.add(iid)
        quest_has_charm = 1.0 if QUEST_RESULT_ID in inv_ids else 0.0
        quest_mat_bark = 1.0 if "treant_bark" in inv_ids else 0.0
        quest_mat_hide = 1.0 if "troll_hide" in inv_ids else 0.0
        quest_mat_ecto = 1.0 if "ectoplasm" in inv_ids else 0.0
        quest_giver_here = 1.0 if QUEST_GIVER_NAME in (s["npc_names"] or []) else 0.0
        # Delver quest shares the giver/room, so its giver flag mirrors the
        # same presence check through its own key (same pattern, per quest).
        quest2_active = 1.0 if s.get("quest_delver_active") else 0.0
        quest2_ready = 1.0 if s.get("quest_delver_ready") else 0.0
        quest2_giver_here = quest_giver_here
        # Arrow count matters (bows eat one per shot), unlike other items
        # where binary presence suffices -- hence a scalar, not just the
        # inv_presence flag. Counts the whole ammo family (any variant
        # fires); ammo_best tracks the best loaded bonus (+0/+1/+2).
        ammo_bonus = getattr(srv, "AMMO_BONUS", {"arrow": 0, "iron_arrow": 1, "steel_arrow": 2})
        held_bonus = [ammo_bonus.get(srv.find_item_by_name(list(srv.ITEM_DEFS), n), -1)
                      for n in (s["inv_names"] or [])]
        held_bonus = [b for b in held_bonus if b >= 0]
        arrows_norm = min(len(held_bonus), 20) / 20.0
        ammo_best_norm = (max(held_bonus) / 2.0) if held_bonus else 0.0
        defense_norm = min(float(s.get("defense", 0) or 0), 10.0) / 10.0

        obs = {
            "room_onehot": room_onehot,
            "is_dungeon": is_dungeon,
            "floor_norm": floor_norm,
            "exits_mask": exits_mask,
            "npc_presence": npc_presence,
            "npc_unknown_count": min(len(unknown_npcs), 5) / 5.0,
            "item_presence": item_presence,
            "inv_presence": inv_presence,
            "equipped_flag": equipped_flag,
            "inv_unknown_flag": inv_unknown,
            "hp_frac": s["hp"] / max(1, s["max_hp"]),
            "gold_norm": s["gold"] / 200.0,       # arbitrary scale, tune to taste
            "score_norm": s["score"] / 100.0,     # arbitrary scale, tune to taste
            "variety": s["variety"],
            "allies_norm": min(s["other_players"], 5) / 5.0,
            "party_norm": min(s["party_size"], srv.PARTY_MAX_MEMBERS) / srv.PARTY_MAX_MEMBERS,
            "level_norm": min(s["level"], 20) / 20.0,
            "xp_progress": min(1.0, s["xp"] / max(1.0, s["xp_to_next"])),
            "market_norm": market_norm,
            "market_any": market_any,
            "tax_rate": s["tax_rate"],
            "tax_min_norm": s["tax_min"] / 10.0,
            "own_net_norm": own_net / 200.0,   # after-tax value of our listings
            "flip_margin_norm": flip_margin_norm,  # best list-over-merchant margin held
            "inv_value_norm": inv_value_norm,  # merchant value of everything carried
            "quest_active": quest_active,
            "quest_ready": quest_ready,
            "quest_has_charm": quest_has_charm,
            "quest_mat_bark": quest_mat_bark,
            "quest_mat_hide": quest_mat_hide,
            "quest_mat_ecto": quest_mat_ecto,
            "quest_giver_here": quest_giver_here,
            "quest2_active": quest2_active,
            "quest2_ready": quest2_ready,
            "quest2_giver_here": quest2_giver_here,
            "arrows_norm": arrows_norm,
            # Buff features
            "buff_attack": 1.0 if s.get("buff_attack_amount", 0) > 0 else 0.0,
            "buff_dr": 1.0 if s.get("buff_damage_reduction_amount", 0) > 0 else 0.0,
            "ammo_best_norm": ammo_best_norm,
            "defense_norm": defense_norm,
            # Not part of flatten_obs() -- handy for debugging/logging only:
            "room_id": s["room_id"],
            "score_raw": s["score"],
            "level": s["level"],
            "gold_raw": s["gold"],
            "inv_names": list(s["inv_names"] or []),
            "quest_stage": quest_stage({
                "quest_active": quest_active,
                "quest_ready": quest_ready,
                "quest_has_charm": quest_has_charm,
            }),
            "quest2_stage": quest_stage({
                "quest2_active": quest2_active,
                "quest2_ready": quest2_ready,
            }, "delver"),
        }
        # Refresh per-observation caches for the mask below (holdings table
        # is already fresh from the disposition features above).
        self._inv_type_cache = {}
        self._mask_cache = [1 if self._action_to_cmd(a) is not None else 0 for a in ACTIONS]
        return obs


# ---------------------------------------------------------------------------
# Smoke-test / demo: a random agent, to prove the env works end to end and
# to show the minimum viable training loop shape.
# ---------------------------------------------------------------------------

async def _demo():
    env = TextMMOEnv(f"MLDemo{random.randint(1000,9999)}", max_steps=40)
    obs = await env.reset()
    print(f"Observation size: {len(flatten_obs(obs))} floats, {N_ACTIONS} actions: {ACTIONS}")

    total_reward = 0.0
    done = False
    while not done:
        action = random.randrange(N_ACTIONS)
        obs, reward, done, info = await env.step(action)
        total_reward += reward
        if reward:
            print(f"  action={ACTIONS[action]:<16} reward={reward:+.3f}  room={obs['room_id']}  hp_frac={obs['hp_frac']:.2f}")

    print(f"\nTotal reward over {env._step_count} random steps: {total_reward:.2f}")
    print(f"Final score: {obs['score_raw']:.2f}")
    await env.close()


if __name__ == "__main__":
    asyncio.run(_demo())
