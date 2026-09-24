"""Scripted behavior-tree baselines as plugins (moved from ml_botfarm.py).

Each role is a registered plugin (``gather``/``dungeon``/``market``/
``maker``/``commissioner``) sharing the ``ScriptedPolicy`` priority-list
machinery. No learning, just behavior trees over the env's valid-action
mask -- the fixed comparison point for RL runs.
"""

import random

try:
    from ..ml_env import ACTIONS, MAREN_NAME, MERCHANT_PRICES, QUEST_GIVER_NAME
except ImportError:
    from ml_env import ACTIONS, MAREN_NAME, MERCHANT_PRICES, QUEST_GIVER_NAME

from . import AgentPlugin, register


class ScriptedPolicy(AgentPlugin):
    """Fixed behavior-tree baseline: no learning, just a priority list over
    the env's valid-action mask. Serves as the fixed comparison point for
    RL runs (same obs/actions/rewards, zero training).

    Subclasses override plan() (yield candidate action indices in priority
    order; first non-None wins) plus optional state-dependent hooks.
    select() returns an action index.
    """

    name = "base"
    agent_type = "scripted"
    # Ordered fallback when nothing role-specific fires.
    WANDER = ("rest", "look")
    # Circuit breaker (#357): this many consecutive picks of the SAME action
    # with zero state change bans that action for STUCK_BAN_STEPS steps,
    # forcing a subgoal switch instead of a blind-repeat loop on a stale
    # mask. Safe idles (look/rest) never count and never ban -- they are
    # the hold behavior, not the loop.
    STUCK_LIMIT = 15
    STUCK_BAN_STEPS = 60
    STUCK_IDLE = ("look", "rest")
    STUCK_KEYS = (
        "score", "level", "xp", "gold", "room_id", "hp", "max_hp",
        "party_size", "is_dungeon", "dungeon_floor", "room_gold",
        "equipped", "armor", "offhand", "deaths", "last_combat",
        "market_orders", "quest_guard_active", "guard_charm_crafted",
        "quest_delver_active", "quest_delver_ready", "quest_remedy_active",
        "quest_remedy_ready", "quest_tonic_active", "quest_tonic_ready",
    )

    def __init__(self, **config):
        super().__init__(**config)
        self._stuck_sig = None
        self._stuck_n = 0
        self._stuck_ban = {}

    def _valid(self, env):
        return env.valid_action_mask()

    def _idx(self, action_name):
        try:
            return ACTIONS.index(action_name)
        except ValueError:
            return None

    def _first_valid(self, env, names, mask=None):
        mask = self._valid(env) if mask is None else mask
        for n in names:
            i = self._idx(n)
            if i is not None and mask[i]:
                return i
        return None

    def _random_move(self, env, mask=None, prefer=()):
        """A valid move action, preferring `prefer` names, else uniform
        among valid moves (deterministic priority would march every bot
        into the same wall-hugging loop)."""
        mask = self._valid(env) if mask is None else mask
        pick = self._first_valid(env, prefer, mask)
        if pick is not None:
            return pick
        moves = [i for i, a in enumerate(ACTIONS) if a.startswith("move_") and mask[i]]
        if moves:
            return random.choice(moves)
        return None

    def _heal_first(self, env, mask=None):
        """Heal/use/rest when hurt, else None. Shared by all roles."""
        s = env._state
        if s.get("hp", 1) >= s.get("max_hp", 1):
            return None
        return self._first_valid(env, ("heal", "use", "rest"), mask)

    def _stuck_state_sig(self, env):
        """Hashable snapshot of every state field a scripted action can
        observably change (combat log and quest flags included, so fights
        and quest progress never count as stuck)."""
        s = env._state
        vals = []
        for k in self.STUCK_KEYS:
            v = s.get(k)
            if isinstance(v, float):
                v = round(v, 3)
            elif isinstance(v, list):
                v = tuple(sorted(str(x) for x in v))
            vals.append(v)
        inv = tuple(sorted(str(n) for n in (s.get("inv_names") or [])))
        npc = tuple(sorted(str(n) for n in (s.get("npc_names") or [])))
        exits = tuple(sorted(str(e) for e in (s.get("exits") or [])))
        comms = tuple(sorted(
            (c.get("id"), str(c.get("poster")), str(c.get("target")),
             c.get("required_kills"), c.get("reward_gold"))
            for c in (s.get("open_commissions") or [])
            if isinstance(c, dict)
        ))
        # Full order tuples, not just the count: a refresh that swaps orders
        # at a constant count is still a changed world (#358).
        market = tuple(sorted(
            (str(o.get("seller")), str(o.get("item")), o.get("price"))
            for o in ((s.get("market_state") or {}).get("orders") or [])
            if isinstance(o, dict)
        ))
        gather = tuple(sorted(str(g) for g in (s.get("gatherables") or [])))
        return (tuple(vals), inv, npc, exits, comms, market, gather)

    def _check_stuck(self, env, action):
        """Count consecutive same-action picks with an unchanged snapshot.
        Returns True once the count reaches STUCK_LIMIT (also records the
        ban); safe-idle actions (look/rest) never count -- they are the
        hold behavior, not the loop."""
        if ACTIONS[action] in self.STUCK_IDLE:
            self._stuck_sig = None
            self._stuck_n = 0
            return False
        sig = (action, self._stuck_state_sig(env))
        if sig == self._stuck_sig:
            self._stuck_n += 1
        else:
            self._stuck_sig = sig
            self._stuck_n = 1
        if self._stuck_n >= self.STUCK_LIMIT:
            step = getattr(env, "_step_count", 0)
            self._stuck_ban[action] = step + self.STUCK_BAN_STEPS
            self._stuck_sig = None
            self._stuck_n = 0
            return True
        return False

    def _stuck_escalate(self, env, mask, banned):
        """Fallback when the circuit breaker fires: heal first, else move
        away, else look (forced refresh). Skips the just-banned action so
        the escalate cannot re-pick the stuck loop. Returns an index."""
        pick = self._heal_first(env, mask)
        if pick is not None and pick != banned:
            return pick
        moves = [i for i, a in enumerate(ACTIONS)
                 if a.startswith("move_") and mask and i < len(mask)
                 and mask[i] and i != banned]
        if moves:
            return moves[0]
        look = self._idx("look")
        if look is not None and look != banned:
            return look
        for action in self.plan(env, mask):
            if action is not None and action != banned:
                return action
        return self._idx("look")

    def select(self, env):
        mask = self._valid(env)
        step = getattr(env, "_step_count", 0)
        for expired in [a for a, until in self._stuck_ban.items() if step >= until]:
            del self._stuck_ban[expired]
        for action in self.plan(env, mask):
            if action is None:
                continue
            if self._stuck_ban.get(action, 0) > step:
                continue
            if not self._check_stuck(env, action):
                return action
            # Escalate returns an action INDEX (0 is a real move), so the
            # fallback needs an explicit None check, not `or` (#358).
            esc = self._stuck_escalate(env, mask, action)
            if esc is not None:
                return esc
            return self._idx("look")
        return self._idx("look")

    def plan(self, env, mask):
        """Yield candidate action indices in priority order. Subclasses
        override; base just wanders."""
        yield self._heal_first(env, mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class GatherPlugin(ScriptedPolicy):
    """Gather -> sell loop: pick up gold/loot, gather nodes, quicksell to
    the merchant, gear up when barehanded."""

    name = "gather"

    def plan(self, env, mask):
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("take", "gather"), mask)
        yield self._first_valid(env, ("sell", "market_post"), mask)
        yield self._first_valid(env, ("equip", "buy"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class DungeonPlugin(ScriptedPolicy):
    """Combat specialist: attack hostiles, loot, push into the dungeon,
    work the delver quest when the giver is present."""

    name = "dungeon"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("attack",), mask)
        yield self._first_valid(env, ("equip", "buy", "take"), mask)
        giver_here = QUEST_GIVER_NAME in (s.get("npc_names") or [])
        if giver_here:
            # Delver quest state gates the giver branch (#357): accept once
            # to get active, turn in only when ready. Active-but-not-ready
            # must fall through to the dungeon descent below -- the
            # pre-#357 mask-order form repeated quest2_turn_in forever
            # because failed turn-ins only send messages, never state.
            if not s.get("quest_delver_active"):
                yield self._first_valid(env, ("quest2_accept",), mask)
            elif s.get("quest_delver_ready"):
                yield self._first_valid(env, ("quest2_turn_in",), mask)
        yield self._first_valid(env, ("move_enter", "move_down"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class MarketPlugin(ScriptedPolicy):
    """Economic specialist: keep the market snapshot fresh, list high-margin
    holdings, buy fills, merchant-sell the rest."""

    name = "market"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("take",), mask)
        if not s.get("market_state"):
            yield self._first_valid(env, ("market_list",), mask)
        yield self._first_valid(env, ("market_post", "market_buy"), mask)
        yield self._first_valid(env, ("sell", "market_list"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class MakerPlugin(ScriptedPolicy):
    """Liquidity provider: keeps two-sided flow up so economic agents
    always have a counterparty. Unlike the flipper (waits for margin),
    the maker buys on the market every chance it gets, posts whatever
    the book takes, merchants the rest, and deepens its own stall --
    spread income over volume, not cherry-picked arbitrage."""

    name = "maker"

    def _broke(self, env):
        """True when the maker has no path to gold: pocket change below the
        cheapest merchant price, nothing held that could sell or post, and
        no own open orders to cancel or relist. Holdings-based (inventory
        plus own orders), never mask-based: sell needs a merchant in-room
        and cancel needs a fresh book snapshot, so mask validity conflates
        'cannot liquidate HERE' with 'nothing to liquidate' and camps
        holders forever (the broke branch never moves)."""
        s = env._state
        cheapest = min(MERCHANT_PRICES.values()) if MERCHANT_PRICES else 2
        if s.get("gold", 0) >= cheapest:
            return False
        if s.get("inv_names"):
            return False
        ms = s.get("market_state") or {}
        if any(o.get("seller") == env.name for o in (ms.get("orders") or [])):
            return False
        return True

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        # Broke bots still fight (#358): the hold below must never leave a
        # maker mauled by a hostile it could have attacked for free.
        yield self._first_valid(env, ("attack",), mask)
        if self._broke(env):
            # Hold solvently: refresh toward free income instead of spending
            # gold or wandering. take/gather earn without capital; rest/look
            # hold in place without moving; buy_arrows is merchant-gated the
            # same as buy, so it stays out of the broke branch.
            yield self._first_valid(env, ("take", "gather", "rest", "look"), mask)
            yield self._first_valid(env, ("market_list", "commission_list"), mask)
            return
        yield self._first_valid(env, ("take",), mask)
        if not s.get("market_state"):
            yield self._first_valid(env, ("market_list",), mask)
        yield self._first_valid(env, ("market_buy", "market_post"), mask)
        yield self._first_valid(env, ("sell", "market_expand"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class CommissionerPlugin(ScriptedPolicy):
    """Bounty driver: deterministic post -> kill -> fill -> cancel cycle so
    commission paths (caps, kill verification/consumption, collusion curve,
    expiry) execute under load instead of by epsilon-accident. Attacks
    anything hostile to earn verified kills, fills others' bounties when
    able (server rejections are signal, not failure), and cancels its own
    oldest when the board is full. Doubles as the regression driver for
    every future commission change."""

    name = "commissioner"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("attack", "take"), mask)
        if not s.get("open_commissions") and env._step_count % 2 == 0:
            # Empty board: refresh on even steps, (re)stock on odd steps.
            # Without the parity split the unconditional post below would
            # starve list on a quiet board (or vice versa) -- state only
            # changes on events, so a fixed priority would repeat one side
            # forever.
            yield self._first_valid(env, ("commission_list",), mask)
        yield self._first_valid(env, ("commission_fill",), mask)
        mine = [c for c in (s.get("open_commissions") or [])
                if c.get("poster") == env.name]
        if not mine:
            yield self._first_valid(env, ("commission_post",), mask)
        else:
            # Rotate stock: cancel the oldest, repost on later steps. The
            # churn feeds terminal-prune expiry as well as cancel paths.
            yield self._first_valid(env, ("commission_cancel",), mask)
        yield self._first_valid(env, ("commission_list",), mask)
        yield self._first_valid(env, ("equip", "buy"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class QuesterPlugin(ScriptedPolicy):
    """Quest-chain specialist (#335): accept the guard/remedy/tonic quests
    at their givers, gather-or-craft the required inputs, then turn in.
    One priority chain drives the full accept -> work -> turn-in loop."""

    name = "quester"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("attack", "take"), mask)
        npcs = s.get("npc_names") or []
        if QUEST_GIVER_NAME in npcs:
            if s.get("guard_charm_crafted"):
                yield self._first_valid(env, ("quest_turn_in",), mask)
            elif not s.get("quest_guard_active"):
                yield self._first_valid(env, ("quest_accept",), mask)
        if MAREN_NAME in npcs:
            if s.get("quest_remedy_ready"):
                yield self._first_valid(env, ("quest3_turn_in",), mask)
            elif not s.get("quest_remedy_active"):
                yield self._first_valid(env, ("quest3_accept",), mask)
            if s.get("quest_tonic_ready"):
                yield self._first_valid(env, ("quest4_turn_in",), mask)
            elif not s.get("quest_tonic_active"):
                yield self._first_valid(env, ("quest4_accept",), mask)
        yield self._first_valid(
            env,
            ("craft_charm", "craft_fortitude_tonic", "craft", "gather", "take"),
            mask,
        )
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class CrafterPlugin(ScriptedPolicy):
    """Production specialist (#335): gather or buy inputs, craft by recipe,
    then sell or use the output. Exercises the craft paths (arrows, iron,
    oils, tonics, charm, blades) under load."""

    name = "crafter"

    def plan(self, env, mask):
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("gather", "take"), mask)
        yield self._first_valid(env, ("buy", "buy_arrows"), mask)
        yield self._first_valid(
            env,
            (
                "craft_arrows",
                "craft_iron",
                "craft_iron_arrow",
                "craft_sharpening_oil",
                "craft_fortitude_tonic",
                "craft_ironhide_draught",
                "craft_serpentbrand",
                "craft_wardens_blade",
                "craft_charm",
                "craft",
            ),
            mask,
        )
        yield self._first_valid(env, ("sell", "market_post"), mask)
        yield self._first_valid(env, ("equip", "use"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class PartyLeaderPlugin(ScriptedPolicy):
    """Group leader (#335): form a party and invite whoever shares the room,
    then lead dungeon descents as a group. Alternates invite/accept so a
    lone leader keeps cycling until a partner accepts."""

    name = "party_leader"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("attack",), mask)
        if s.get("party_size", 1) <= 1:
            if env._step_count % 2 == 0:
                yield self._first_valid(env, ("party_invite",), mask)
            else:
                yield self._first_valid(env, ("party_accept",), mask)
        yield self._first_valid(env, ("equip", "buy", "take"), mask)
        yield self._first_valid(env, ("move_enter", "move_down"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


# Backward-compatible aliases (ml_botfarm and its tests import these).
GatherSellPolicy = GatherPlugin
DungeonClearerPolicy = DungeonPlugin
MarketFlipperPolicy = MarketPlugin
MarketMakerPolicy = MakerPlugin
CommissionerPolicy = CommissionerPlugin
QuesterPolicy = QuesterPlugin
CrafterPolicy = CrafterPlugin
PartyLeaderPolicy = PartyLeaderPlugin

SCRIPTED_POLICIES = {
    "gather": GatherSellPolicy,
    "dungeon": DungeonClearerPolicy,
    "market": MarketFlipperPolicy,
    "maker": MarketMakerPolicy,
    "commissioner": CommissionerPolicy,
    "quester": QuesterPolicy,
    "crafter": CrafterPolicy,
    "party_leader": PartyLeaderPolicy,
}
SCRIPTED_NAMES = tuple(SCRIPTED_POLICIES)
