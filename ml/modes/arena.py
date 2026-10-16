"""Arena mode skeleton (#474): first consumer of the engine/mode seam.

Ruleset: staged PvP bouts. Characters spawn at the staging room, kills
pay flat arena points into the mode ledger, deaths respawn back at
staging with the streak reset. Nothing here touches engine score, XP,
or gold -- the ledger is the entire scoring surface, which is what
proves the world-state separation rests on more than convention.

The staging room reuses an existing world room until instanced arena
rooms land; swapping one constant then is the whole migration.
"""

from ml.modes import BaseMode, register

STAGING_ROOM = "guild_hall"
KILL_POINTS = 5.0


@register
class ArenaMode(BaseMode):
    """Staged PvP bouts with isolated scoring."""

    name = "arena"
    version = 1

    @classmethod
    def config_schema(cls):
        return {
            "kill_points": {
                "type": float,
                "default": KILL_POINTS,
                "help": "ledger points per kill",
            },
            "staging_room": {
                "type": str,
                "default": STAGING_ROOM,
                "help": "login/respawn room until instanced arenas land",
            },
        }

    def initial_state(self):
        return {"arena_score": 0.0, "kills": 0, "deaths": 0, "streak": 0}

    def spawn_room(self, ctx):
        return self.staging_room

    def score_kill(self, ctx, killer, victim, base_points, base_xp):
        ledger = ctx.mode_state
        ledger["kills"] = ledger.get("kills", 0) + 1
        ledger["streak"] = ledger.get("streak", 0) + 1
        ledger["arena_score"] = ledger.get("arena_score", 0.0) + self.kill_points
        return self.kill_points, 0.0

    def score_death(self, ctx, character, base_penalty):
        ledger = ctx.mode_state
        ledger["deaths"] = ledger.get("deaths", 0) + 1
        ledger["streak"] = 0
        return 0.0

    def on_kill(self, ctx, killer, victim):
        """No side effects yet: score_kill already credited the ledger."""

    def on_death(self, ctx, character):
        """No side effects yet: score_death already counted the death."""
