"""Scripted behavior-tree baselines as plugins (moved from ml_botfarm.py).

Each role is a registered plugin (``gather``/``dungeon``/``market``/
``commissioner``/``quester``/``crafter``/``party_leader``) sharing the
``ScriptedPolicy`` priority-list
machinery. No learning, just behavior trees over the env's valid-action
mask -- the fixed comparison point for RL runs.
"""

import os
import random
import sys
from typing import ClassVar

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from ..ml_env import (
        ACTIONS,
        MAREN_NAME,
        MAREN_ROOM,
        QUEST_GIVER_NAME,
        QUEST_GIVER_ROOM,
        pack_full,
        reachable_path,
    )
except ImportError:
    from ml_env import (
        ACTIONS,
        MAREN_NAME,
        MAREN_ROOM,
        QUEST_GIVER_NAME,
        QUEST_GIVER_ROOM,
        pack_full,
        reachable_path,
    )

import server as srv

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
        if not moves:
            return None
        exits = (srv.ROOMS.get(env._state.get("room_id"), {}) or {}).get("exits") or {}
        safe = [
            i
            for i in moves
            if self._survivable(env, exits.get(ACTIONS[i][len("move_") :]))
        ]
        return random.choice(safe or moves)

    def _in_room(self, env, room):
        return env._state.get("room_id") == room

    def _room_hostiles(self, room_id):
        """Hostile instances in a room, from the live table.

        Reads server.npcs, not world.json: MOB_EXTRA_SPAWNS stacks several
        copies of one template in every wild room, so static rows understate
        a room's danger by that multiplier. Scored against static rows, a
        hunter walked into rooms that killed it on arrival (#590: wild 8.7
        deaths/h, almost all of it one-tick fights).
        """
        return sum(
            1
            for npc in (getattr(srv, "npcs", {}) or {}).values()
            if npc.get("room") == room_id and npc.get("hostile")
        )

    def _room_incoming(self, room_id):
        """Damage per tick from the hostile instances in a room."""
        return sum(
            max(0, int(npc.get("attack") or 0))
            for npc in (getattr(srv, "npcs", {}) or {}).values()
            if npc.get("room") == room_id and npc.get("hostile")
        )

    def _survivable(self, env, room_id, ticks=1):
        """True when the room cannot kill the agent in one tick of contact.

        Crossing a room costs at least one tick of every resident hostile's
        damage, and a level-1 agent has 20 hp: four Glacier Wyrms hit for 52,
        so a route that steps through them is death before any fight maths
        matters. Winning the fight itself is the attack mask's call.
        """
        hp = max(1, int(env._state.get("hp") or 1))
        incoming = self._room_incoming(room_id)
        return incoming <= 0 or incoming * ticks < hp

    def _safe_path(self, start, target):
        """Shortest exit path that does not route through a hostile room.

        The target itself is allowed to be hostile: callers that deliberately
        enter dangerous rooms (dungeon, wild hunt) opt out of this helper.
        """
        if start == target:
            return []
        if start not in srv.ROOMS or target not in srv.ROOMS:
            return None
        queue = [(start, [])]
        seen = {start}
        while queue:
            room_id, path = queue.pop(0)
            for direction, dest in (srv.ROOMS.get(room_id, {}).get("exits") or {}).items():
                if dest in seen or dest not in srv.ROOMS:
                    continue
                next_path = path + [direction]
                if dest == target:
                    return next_path
                if self._room_hostiles(dest):
                    continue
                seen.add(dest)
                queue.append((dest, next_path))
        return None

    def _navigate_toward(self, env, room, mask, avoid_hostiles=True):
        """Yield the next move_<dir> action along the shortest path from the
        current room to `room`, using the world's directed exits. Only yields
        when not already in the target room and a path exists."""
        if self._in_room(env, room):
            return
        start = env._state.get("room_id")
        if start is None or start not in srv.ROOMS or room not in srv.ROOMS:
            return
        path = (
            self._safe_path(start, room)
            if avoid_hostiles
            else self._reachable_safely(env, start, room)
        )
        if not path:
            return
        action = f"move_{path[0]}"
        i = self._idx(action)
        if i is not None and mask[i]:
            yield i

    def _reachable_safely(self, env, start, target):
        """Shortest path that only crosses rooms the agent survives.

        Unlike `_safe_path`, hostile rooms are allowed when their live
        incoming damage stays under what the agent can take, so a hunter can
        still reach weak wild rooms instead of refusing every one.
        """
        if start == target:
            return []
        if start not in srv.ROOMS or target not in srv.ROOMS:
            return None
        queue = [(start, [])]
        seen = {start}
        while queue:
            room_id, path = queue.pop(0)
            exits = srv.ROOMS.get(room_id, {}).get("exits") or {}
            for direction, dest in exits.items():
                if dest in seen or dest not in srv.ROOMS:
                    continue
                if not self._survivable(env, dest):
                    continue
                next_path = path + [direction]
                if dest == target:
                    return next_path
                seen.add(dest)
                queue.append((dest, next_path))
        return None

    def _safe_random_move(self, env, mask=None):
        """A valid move preferring destinations with no hostiles, then the
        least damaging one.

        Picking blind among hostile exits is what fed the death loop: with
        every exit dangerous the agent chose uniformly, half the time into a
        room that hit harder than the one it left.
        """
        mask = self._valid(env) if mask is None else mask
        s = env._state
        exits = (srv.ROOMS.get(s.get("room_id"), {}) or {}).get("exits") or {}
        options = []
        for direction, dest in exits.items():
            i = self._idx(f"move_{direction}")
            if i is None or not mask[i]:
                continue
            options.append((self._room_incoming(dest), i))
        if not options:
            return self._random_move(env, mask)
        calm = [i for incoming, i in options if incoming == 0]
        if calm:
            return random.choice(calm)
        least = min(incoming for incoming, _ in options)
        return random.choice([i for incoming, i in options if incoming == least])

    def _path_attack_cost(self, start, target):
        """Sum hostile attack along the plain shortest path to `target`."""
        if start == target:
            return 0
        path = reachable_path(srv.ROOMS, start, target)
        if not path:
            return 10 ** 6
        cost = 0
        here = start
        for direction in path:
            dest = (srv.ROOMS.get(here, {}).get("exits") or {}).get(direction)
            if dest is None:
                return 10 ** 6
            cost += self._room_incoming(dest)
            here = dest
        return cost

    def _leave_hostile_room(self, env, mask=None):
        """Flee the current room if a resident hostile is chewing on us.

        Non-combat roles must move before spending steps on economy actions:
        the server tick auto-attacks one player per hostile room regardless
        of what that player is doing, so standing still bleeds them out.
        """
        if self._room_hostiles(env._state.get("room_id")):
            move = self._safe_random_move(env, mask)
            if move is not None:
                yield move

    def _flee_unwinnable(self, env, mask=None):
        """Move out of a room whose hostiles are all too strong to fight.

        The env attack mask only offers `attack` when the whole room can be
        beaten, so a room without it costs HP every tick the agent stays.
        Wild zones included: the old stand-your-ground window parked agents
        in four-mob rooms they could not clear and fed a respawn loop.
        """
        room = env._state.get("room_id")
        if not self._room_hostiles(room):
            return None
        mask = self._valid(env) if mask is None else mask
        attack = self._idx("attack")
        if attack is not None and mask[attack]:
            return None
        return self._safe_random_move(env, mask)

    def _dungeon_entrance_room(self):
        """Return the room players must stand in to use move_enter/move_down
        into the dungeon, or None if the world has no recognizable entrance."""
        room = getattr(srv, "DUNGEON_ENTRANCE_ROOM", None)
        if room and room in srv.ROOMS:
            return room
        for r in srv.ROOMS:
            if "dungeon" in r:
                return r
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
        comms = tuple(
            sorted(
                (
                    c.get("id"),
                    str(c.get("poster")),
                    str(c.get("target")),
                    c.get("kills"),
                    c.get("gold"),
                )
                for c in (s.get("open_commissions") or [])
                if isinstance(c, dict)
            )
        )
        # Full order tuples, not just the count: a refresh that swaps orders
        # at a constant count is still a changed world (#358). Bids ride
        # alongside asks (#158) so bidding bots see their own book move.
        market = tuple(sorted(
            [("ask", str(o.get("seller")), str(o.get("item")), o.get("price"))
             for o in ((s.get("market_state") or {}).get("orders") or [])
             if isinstance(o, dict)]
            + [("bid", str(b.get("buyer")), str(b.get("item")), b.get("price"))
               for b in ((s.get("market_state") or {}).get("bids") or [])
               if isinstance(b, dict)]
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
            if len(self._stuck_ban) > 1000:
                self._stuck_ban.clear()
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
        yield self._flee_unwinnable(env, mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class GatherPlugin(ScriptedPolicy):
    """Gather -> sell loop: pick up gold/loot, gather nodes, quicksell to
    the merchant, gear up when barehanded."""

    name = "gather"

    CRAFT_MATS: ClassVar = {
        "Iron Ore",
        "Wolf Pelt",
        "Rat Tail",
        "Mountain Berry",
        "Mountain Herb",
        "Reed Fiber",
        "Pine Resin",
        "Serpent Scale",
        "Heron Feather",
        "Treant Bark",
        "Troll Hide",
        "Ectoplasm",
    }

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._flee_unwinnable(env, mask)
        yield self._first_valid(env, ("flee",), mask)
        yield from self._leave_hostile_room(env, mask)
        yield self._first_valid(env, ("take", "gather"), mask)
        # Post high-margin items before merchant-selling; protect crafting
        # inputs unless we are desperate for pack space.
        yield self._first_valid(env, ("market_post",), mask)
        inv = set(s.get("inv_names") or [])
        if pack_full(s) or not (inv & self.CRAFT_MATS):
            yield self._first_valid(env, ("sell",), mask)
        yield self._first_valid(env, ("equip", "buy"), mask)
        if pack_full(s):
            yield self._first_valid(env, ("drop",), mask)
        yield self._safe_random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class DungeonPlugin(ScriptedPolicy):
    """Combat specialist: attack hostiles, loot, push into the dungeon,
    work the delver quest when the giver is present."""

    name = "dungeon"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        # The dungeon entrance is itself a hostile room (the graveyard ghost),
        # so the flee rule below pushed the role back out every time it
        # arrived: it ping-ponged at the door and never cleared a floor.
        if not s.get("is_dungeon") and self._in_room(
            env, self._dungeon_entrance_room()
        ):
            yield self._first_valid(env, ("move_enter", "move_down"), mask)
        yield self._flee_unwinnable(env, mask)
        # Companion playstyle: acquire and summon before descending so the
        # pet contributes damage instead of sitting as dead content.
        if not s.get("companion"):
            if self._in_room(env, "market"):
                yield self._first_valid(env, ("companion_adopt",), mask)
            elif s.get("gold", 0) >= 10:
                yield from self._navigate_toward(env, "market", mask)
        else:
            yield self._first_valid(env, ("companion_summon",), mask)
        hp_frac = s.get("hp", 1) / max(1, s.get("max_hp", 1))
        if hp_frac < 0.25 and not s.get("shelter"):
            yield self._first_valid(env, ("flee",), mask)
        yield self._first_valid(env, ("attack",), mask)
        yield self._first_valid(env, ("equip", "buy", "take"), mask)
        giver_here = QUEST_GIVER_NAME in (s.get("npc_names") or [])
        if giver_here:
            # Delver quest state gates the giver branch (#357): accept once
            # to get active, turn in only when ready. Active-but-not-ready
            # must fall through to the dungeon descent below -- the
            # pre-#357 mask-order logic repeated quest2_turn_in forever
            # because failed turn-ins only send messages, never state.
            if not s.get("quest_delver_active"):
                yield self._first_valid(env, ("quest2_accept",), mask)
            elif s.get("quest_delver_ready"):
                yield self._first_valid(env, ("quest2_turn_in",), mask)
        if not s.get("is_dungeon"):
            entrance = self._dungeon_entrance_room()
            if entrance and not self._in_room(env, entrance):
                yield from self._navigate_toward(env, entrance, mask)
        yield self._first_valid(env, ("move_enter", "move_down"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class HunterPlugin(ScriptedPolicy):
    """Wild-zone specialist: seek the least dangerous wild room, fight the
    resident mobs, loot, and sell the spoils. Gives the wild-zone death and
    income paths a dedicated driver instead of leaving them unvisited."""

    name = "hunter"

    def _wild_room(self, env):
        best = None
        best_score = None
        start = env._state.get("room_id")
        for room_id, room in srv.ROOMS.items():
            if not room.get("wild"):
                continue
            if not self._survivable(env, room_id):
                continue
            resident = self._room_incoming(room_id)
            # Path danger matters more than the target room: the northern
            # wild rooms sit behind Ice Wraiths and Treants a level-1
            # hunter cannot survive, while the southern route is weak mobs.
            score = self._path_attack_cost(start, room_id) + resident
            if best_score is None or score < best_score:
                best, best_score = room_id, score
        return best

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._flee_unwinnable(env, mask)
        # A bare-hands hunter cannot beat even the weakest wild mob: the
        # mob's average hit outpaces the player's 3 base attack, so every
        # trip is a death loop. Gear first, then the pet, then the hunt.
        if not s.get("equipped") and not self._in_room(env, "market"):
            yield from self._navigate_toward(env, "market", mask)
        if self._in_room(env, "market"):
            yield self._first_valid(env, ("equip",), mask)
            yield self._first_valid(env, ("buy",), mask)
        if not s.get("companion"):
            has_deed = any(
                "wolf pup" in n.lower() for n in (s.get("inv_names") or [])
            )
            if self._in_room(env, "market") or has_deed:
                yield self._first_valid(env, ("companion_adopt",), mask)
            elif s.get("gold", 0) >= 10:
                yield from self._navigate_toward(env, "market", mask)
        else:
            yield self._first_valid(env, ("companion_summon",), mask)
        # Clear transit rooms instead of walking through them: the weak
        # civilian mobs on the way give kills and stop the bleed.
        yield self._first_valid(env, ("attack",), mask)
        target = self._wild_room(env)
        if target and not self._in_room(env, target):
            yield from self._navigate_toward(env, target, mask, avoid_hostiles=False)
        yield self._first_valid(env, ("attack",), mask)
        yield self._first_valid(env, ("take",), mask)
        yield self._first_valid(env, ("market_post", "sell"), mask)
        yield self._first_valid(env, ("equip", "use"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class MarketPlugin(ScriptedPolicy):
    """Economic specialist: keep the market snapshot fresh, list high-margin
    holdings, buy fills, post standing bids that force sellers down (#158),
    merchant-sell the rest."""

    name = "market"

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._flee_unwinnable(env, mask)
        yield self._first_valid(env, ("flee",), mask)
        yield from self._leave_hostile_room(env, mask)
        yield self._first_valid(env, ("take",), mask)
        if not s.get("market_state"):
            # No snapshot yet: refresh from anywhere (list is global).
            yield self._first_valid(env, ("market_list",), mask)
        in_market = self._in_room(env, "market")
        if not in_market:
            yield from self._navigate_toward(env, "market", mask)
        if in_market:
            # Post asks before buying so fills have something to hit.
            yield self._first_valid(env, ("market_post",), mask)
            yield self._first_valid(env, ("market_buy",), mask)
            yield self._first_valid(env, ("market_buy_order",), mask)
            yield self._first_valid(env, ("market_cancel",), mask)
            yield self._first_valid(env, ("market_expand",), mask)
        # Post/buy/sell work from any room; listing refreshes a stale book.
        yield self._first_valid(env, ("market_post", "market_buy"), mask)
        yield self._first_valid(env, ("sell", "market_list"), mask)
        yield self._safe_random_move(env, mask)
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
        yield self._flee_unwinnable(env, mask)
        yield self._first_valid(env, ("flee",), mask)
        yield self._first_valid(env, ("attack", "take"), mask)
        if not s.get("open_commissions") and getattr(env, "_step_count", 0) % 2 == 0:
            # Empty board: refresh on even steps, (re)stock on odd steps.
            # Without the parity split the unconditional post below would
            # starve list on a quiet board (or vice versa) -- state only
            # changes on events, so a fixed priority would repeat one side
            # forever.
            yield self._first_valid(env, ("commission_list",), mask)
        if not self._in_room(env, "guild_hall"):
            # Post/fill are guild-gated (env masks them off out here);
            # walk over so the gated steps below can fire.
            yield from self._navigate_toward(env, "guild_hall", mask)
        # Fill is guild-gated via the env mask; cancel/list stay global.
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
        yield self._safe_random_move(env, mask)
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
        yield self._flee_unwinnable(env, mask)
        yield self._first_valid(env, ("flee",), mask)
        yield self._first_valid(env, ("attack", "take"), mask)
        npcs = s.get("npc_names") or []
        if QUEST_GIVER_NAME in npcs:
            if s.get("guard_charm_crafted"):
                yield self._first_valid(env, ("quest_turn_in",), mask)
            elif not s.get("quest_guard_active"):
                yield self._first_valid(env, ("quest_accept",), mask)
        elif s.get("quest_guard_active") or s.get("guard_charm_crafted"):
            # Quest underway but the giver is elsewhere: walk over so the
            # accept/turn-in steps above can fire.
            yield from self._navigate_toward(env, QUEST_GIVER_ROOM, mask)
        if MAREN_NAME in npcs:
            if s.get("quest_remedy_ready"):
                yield self._first_valid(env, ("quest3_turn_in",), mask)
            elif not s.get("quest_remedy_active"):
                yield self._first_valid(env, ("quest3_accept",), mask)
            if s.get("quest_tonic_ready"):
                yield self._first_valid(env, ("quest4_turn_in",), mask)
            elif not s.get("quest_tonic_active"):
                yield self._first_valid(env, ("quest4_accept",), mask)
        elif (s.get("quest_remedy_active") or s.get("quest_remedy_ready")
                or s.get("quest_tonic_active") or s.get("quest_tonic_ready")):
            yield from self._navigate_toward(env, MAREN_ROOM, mask)

        yield self._first_valid(
            env,
            ("craft_charm", "craft_fortitude_tonic", "craft", "gather", "take"),
            mask,
        )
        yield self._safe_random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


@register
class CrafterPlugin(ScriptedPolicy):
    """Production specialist (#335): gather or buy inputs, craft by recipe,
    then sell or use the output. Exercises the craft paths (arrows, iron,
    oils, tonics, charm, blades) under load."""

    name = "crafter"

    CRAFT_NAMES = (
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
    )

    SOURCE_ROOMS = (
        ("smithy", ("iron_ore",)),
    )

    def plan(self, env, mask):
        s = env._state
        yield self._heal_first(env, mask)
        yield self._flee_unwinnable(env, mask)
        yield self._first_valid(env, ("flee",), mask)
        yield from self._leave_hostile_room(env, mask)
        # Craft when materials are available; the env masks craft actions off
        # unless inputs are in hand, so this does not spam guaranteed errors.
        yield self._first_valid(env, self.CRAFT_NAMES, mask)
        yield self._first_valid(env, ("gather", "take"), mask)
        # If nothing is craftable or gatherable, head to a source room for mats.
        # inv_names holds display names, so resolve mat ids through ITEM_DEFS.
        have_ids = set()
        for n in s.get("inv_names") or []:
            iid = srv.find_item_by_name(list(srv.ITEM_DEFS), n)
            if iid:
                have_ids.add(iid)
        current_room = s.get("room_id")
        for room, mats in self.SOURCE_ROOMS:
            if room not in srv.ROOMS or current_room == room:
                continue
            if not any(m in have_ids for m in mats):
                yield from self._navigate_toward(env, room, mask)
        yield self._first_valid(env, ("buy", "buy_arrows"), mask)
        yield self._first_valid(env, ("sell", "market_post"), mask)
        yield self._first_valid(env, ("equip", "use"), mask)
        yield self._safe_random_move(env, mask)
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
        yield self._flee_unwinnable(env, mask)
        if not s.get("companion"):
            if self._in_room(env, "market"):
                yield self._first_valid(env, ("companion_adopt",), mask)
            elif s.get("gold", 0) >= 10:
                yield from self._navigate_toward(env, "market", mask)
        else:
            yield self._first_valid(env, ("companion_summon",), mask)
        yield self._first_valid(env, ("attack",), mask)
        if s.get("party_size", 1) <= 1:
            if getattr(env, "_step_count", 0) % 2 == 0:
                yield self._first_valid(env, ("party_invite",), mask)
            else:
                yield self._first_valid(env, ("party_accept",), mask)
        yield self._first_valid(env, ("equip", "buy", "take"), mask)
        entrance = self._dungeon_entrance_room()
        if entrance and not s.get("is_dungeon") and not self._in_room(env, entrance):
            yield from self._navigate_toward(env, entrance, mask)
        yield self._first_valid(env, ("move_enter", "move_down"), mask)
        yield self._random_move(env, mask)
        yield self._first_valid(env, self.WANDER, mask)


# Backward-compatible aliases (ml_botfarm and its tests import these).
GatherSellPolicy = GatherPlugin
DungeonClearerPolicy = DungeonPlugin
HunterPolicy = HunterPlugin
MarketFlipperPolicy = MarketPlugin
CommissionerPolicy = CommissionerPlugin
QuesterPolicy = QuesterPlugin
CrafterPolicy = CrafterPlugin
PartyLeaderPolicy = PartyLeaderPlugin

SCRIPTED_POLICIES = {
    "gather": GatherSellPolicy,
    "dungeon": DungeonClearerPolicy,
    "hunter": HunterPolicy,
    "market": MarketFlipperPolicy,
    "commissioner": CommissionerPolicy,
    "quester": QuesterPolicy,
    "crafter": CrafterPolicy,
    "party_leader": PartyLeaderPolicy,
}
SCRIPTED_NAMES = tuple(SCRIPTED_POLICIES)
