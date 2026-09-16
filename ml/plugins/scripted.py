"""Scripted behavior-tree baselines as plugins (moved from ml_botfarm.py).

Each role is a registered plugin (``gather``/``dungeon``/``market``/
``maker``) sharing the ``ScriptedPolicy`` priority-list machinery. No
learning, just behavior trees over the env's valid-action mask -- the
fixed comparison point for RL runs.
"""
import random

try:
    from ..ml_env import ACTIONS, QUEST_GIVER_NAME
except ImportError:
    from ml_env import ACTIONS, QUEST_GIVER_NAME

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
        moves = [i for i, a in enumerate(ACTIONS)
                 if a.startswith("move_") and mask[i]]
        if moves:
            return random.choice(moves)
        return None

    def _heal_first(self, env, mask=None):
        """Heal/use/rest when hurt, else None. Shared by all roles."""
        s = env._state
        if s.get("hp", 1) >= s.get("max_hp", 1):
            return None
        return self._first_valid(env, ("heal", "use", "rest"), mask)

    def select(self, env):
        mask = self._valid(env)
        for step in self.plan(env, mask):
            if step is not None:
                return step
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
            yield self._first_valid(env, ("quest2_turn_in", "quest2_accept"), mask)
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

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._first_valid(env, ("take",), mask)
        if not s.get("market_state"):
            yield self._first_valid(env, ("market_list",), mask)
        yield self._first_valid(env, ("market_buy", "market_post"), mask)
        yield self._first_valid(env, ("sell", "market_expand"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


# Backward-compatible aliases (ml_botfarm and its tests import these).
GatherSellPolicy = GatherPlugin
DungeonClearerPolicy = DungeonPlugin
MarketFlipperPolicy = MarketPlugin
MarketMakerPolicy = MakerPlugin

SCRIPTED_POLICIES = {
    "gather": GatherSellPolicy,
    "dungeon": DungeonClearerPolicy,
    "market": MarketFlipperPolicy,
    "maker": MarketMakerPolicy,
}
SCRIPTED_NAMES = tuple(SCRIPTED_POLICIES)
