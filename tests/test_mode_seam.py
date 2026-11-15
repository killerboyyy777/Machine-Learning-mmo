"""Game-mode seam (#66): the engine calls the mode contract, unchanged.

Run from the repo root:  python tests/test_mode_seam.py

The seam is provable by parity: BaseMode is passthrough, so a base-mode
session must behave exactly like the pre-seam engine. These cases pin the
wiring (login, respawn, kill, tick), the room-override rule, and the fact
that a mode ledger is private to the mode and never persisted with the score.
"""

import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


class RecordingMode(srv.BaseMode):
    """Records every hook the engine fires. Never registered."""

    name = "recording"

    def __init__(self):
        self.calls = []
        self.login_room = None
        self.respawn_room = None
        self.kill_award = (2.5, 3.5)
        self.death_penalty = 4.0

    def on_login(self, ctx, character):
        self.calls.append(("on_login", character))
        return self.login_room

    def on_respawn(self, ctx, character):
        self.calls.append(("on_respawn", character))
        return self.respawn_room

    def on_tick(self, ctx, character):
        self.calls.append(("on_tick", character))

    def score_kill(self, ctx, killer, victim, base_points, base_xp):
        self.calls.append(("score_kill", killer, victim, base_points, base_xp))
        return self.kill_award

    def on_kill(self, ctx, killer, victim):
        self.calls.append(("on_kill", killer, victim))

    def score_death(self, ctx, character, base_penalty):
        self.calls.append(("score_death", character, base_penalty))
        return self.death_penalty

    def on_death(self, ctx, character):
        self.calls.append(("on_death", character))


async def main():
    inbox = []

    async def fake_send(player, payload):
        inbox.append(payload if isinstance(payload, dict) else json.loads(payload))

    saved_send = srv.send
    srv.send = fake_send

    def mkplayer(name, pid, room="town_square", hp=20, max_hp=20):
        pl = srv.Player(ws=FakeWS(), id=pid, name=name, logged_in=True)
        pl.hp, pl.max_hp, pl.room = hp, max_hp, room
        srv.players[pid] = pl
        srv.add_member(pl)
        return pl

    def unplayer(pl, name=None):
        srv.remove_member(pl)
        srv.players.pop(pl.id, None)
        srv.SCORES.pop((name or pl.name).lower(), None)
        srv.name_owners.pop((name or pl.name).lower(), None)

    default_mode = srv.DEFAULT_MODE
    try:
        # --- Base mode is passthrough ------------------------------------
        assert isinstance(default_mode, srv.BaseMode) and default_mode.name == "base"
        p = mkplayer("SeamBase", 61001)
        assert srv.player_mode(p) is default_mode
        ctx = srv.mode_context(p)
        assert ctx.world["start_room"] == srv.START_ROOM
        assert len(ctx.world["rooms"]) == len(srv.ROOMS)
        assert ctx.world["rooms"][srv.START_ROOM]["wild"] is False
        try:
            ctx.world["start_room"] = "market"
            raise AssertionError("mode world view must be read-only")
        except TypeError:
            pass
        # The cached world view is handed over as-is, not re-frozen per call.
        assert srv.mode_context(p).world is ctx.world
        # Placement accepts static rooms and live instances, nothing else:
        # arena rooms will be instances, which are deliberately outside ROOMS.
        assert srv.mode_room_ok(srv.START_ROOM)
        assert not srv.mode_room_ok("no_such_room")
        assert not srv.mode_room_ok(None)
        assert not srv.mode_room_ok("d_999999_f1")
        srv.dungeons[999999] = object()
        assert srv.mode_room_ok("d_999999_f1")
        del srv.dungeons[999999]
        assert ctx.mode_state == {}
        assert srv.mode_on_login(p) is None
        assert srv.mode_on_respawn(p) is None
        srv.mode_award_kill(p, "Test Rat", 5.0, 5.0)
        srv.mode_note_death(p, 10.0)
        srv.mode_tick(p)
        # Base mode is passthrough: no hook awards anything, so no zeros pile up.
        assert srv.mode_context(p).mode_state == {}
        # Session-only: neither attribute reaches the player payload.
        assert "mode" not in srv.stats_view(p)
        assert "mode_store" not in srv.stats_view(p)
        unplayer(p, "SeamBase")
        print("BASE_MODE_PASSTHROUGH_OK")

        # --- Login fires the hook and honours a real room override --------
        rec = RecordingMode()
        srv.DEFAULT_MODE = rec
        rec.login_room = "market"
        joiner = srv.Player(ws=FakeWS(), id=61002, logged_in=False)
        await srv.cmd_login(joiner, {"name": "SeamLogin"})
        assert joiner.logged_in and joiner.mode is rec
        assert ("on_login", "SeamLogin") in rec.calls, rec.calls
        assert joiner.room == "market", joiner.room
        assert isinstance(srv.mode_context(joiner).mode_state, dict)
        unplayer(joiner, "SeamLogin")

        # An override naming no real room falls back to the engine's choice.
        rec2 = RecordingMode()
        rec2.login_room = "no_such_room"
        srv.DEFAULT_MODE = rec2
        stray = srv.Player(ws=FakeWS(), id=61003, logged_in=False)
        await srv.cmd_login(stray, {"name": "SeamStray"})
        assert stray.room == srv.START_ROOM, stray.room
        unplayer(stray, "SeamStray")
        print("LOGIN_HOOK_OK")

        # --- Respawn: mode scores the same death, then places the body ----
        srv.DEFAULT_MODE = rec
        rec.respawn_room = "forge"
        dead = mkplayer("SeamDead", 61004, room="graveyard", hp=0)
        dead.gold = 12
        entry = srv.get_score_entry("SeamDead")
        await srv.respawn_player(dead)
        assert dead.room == "forge", dead.room
        assert ("on_respawn", "SeamDead") in rec.calls
        base_penalty = [c[2] for c in rec.calls if c[0] == "score_death"][-1]
        assert base_penalty > 0, base_penalty
        assert ("on_death", "SeamDead") in rec.calls
        engine = srv.mode_context(dead).mode_state["_engine"]
        assert engine["penalty"] == rec.death_penalty, engine
        # The engine's own bookkeeping is untouched by the mode.
        assert entry["deaths"] == 1 and entry["civ_deaths"] == 1
        unplayer(dead, "SeamDead")

        # Base mode still wakes up in town.
        srv.DEFAULT_MODE = default_mode
        plain = mkplayer("SeamPlain", 61005, room="graveyard", hp=0)
        await srv.respawn_player(plain)
        assert plain.room == srv.START_ROOM, plain.room
        assert srv.mode_context(plain).mode_state == {}
        unplayer(plain, "SeamPlain")
        print("RESPAWN_HOOK_OK")

        # --- Kill scoring lands on the mode ledger, not the score entry ---
        srv.DEFAULT_MODE = rec
        killer = mkplayer("SeamKiller", 61006)
        killer.base_attack = 50
        srv.npcs["seam_dummy"] = {
            "id": "seam_dummy",
            "name": "Seam Dummy",
            "room": "town_square",
            "hp": 1,
            "max_hp": 20,
            "attack": 0,
            "hostile": False,
            "behavior": "idle",
            "loot": [],
            "gold": 0,
            "respawn_seconds": 60,
            "alive": True,
            "respawn_at": None,
            "contributors": {},
        }
        await srv.cmd_attack(killer, {"target": "seam dummy"})
        assert not srv.npcs["seam_dummy"]["alive"]
        assert ("on_kill", "SeamKiller", "Seam Dummy") in rec.calls, rec.calls
        scored = [c for c in rec.calls if c[0] == "score_kill"][-1]
        assert scored[1] == "SeamKiller" and scored[2] == "Seam Dummy"
        assert scored[3] > 0 and scored[4] > 0
        ledger = srv.mode_context(killer).mode_state
        assert ledger["_engine"]["points"] == rec.kill_award[0], ledger
        assert ledger["_engine"]["xp"] == rec.kill_award[1], ledger
        del srv.npcs["seam_dummy"]
        unplayer(killer, "SeamKiller")
        print("KILL_HOOK_OK")

        # --- Tick is wired into the one gameplay heartbeat ----------------
        before = len(rec.calls)
        srv.mode_tick(killer)
        assert rec.calls[before:] == [("on_tick", "SeamKiller")]
        loop_src = inspect.getsource(srv.npc_ai_loop)
        assert "mode_tick(player)" in loop_src, "npc_ai_loop dropped the mode tick"
        print("TICK_HOOK_OK")

        # --- initial_state() seeds the ledger, exactly once ---------------
        class SeededMode(srv.BaseMode):
            name = "seeded"

            def initial_state(self):
                return {"streak": 0, "kills": 0}

        seeded = mkplayer("SeamSeed", 61007)
        seeded.mode = SeededMode()
        first = srv.mode_context(seeded).mode_state
        assert first == {"streak": 0, "kills": 0}, first
        first["streak"] = 3
        # Second context is the same ledger, not a fresh seed.
        assert srv.mode_context(seeded).mode_state["streak"] == 3
        # An emptied ledger stays emptied: seeding keys on absence, so a mode
        # that legitimately clears its state is not re-seeded under its feet.
        first.clear()
        assert srv.mode_context(seeded).mode_state == {}
        unplayer(seeded, "SeamSeed")
        print("INITIAL_STATE_SEED_OK")

        # --- Engine totals never land on a mode's own key names -----------
        class CollidingMode(srv.BaseMode):
            name = "colliding"

            def initial_state(self):
                return {"score": 0.0, "points": 0.0, "xp": 0.0}

            def score_kill(self, ctx, killer, victim, base_points, base_xp):
                return 1.5, 2.5

            def score_death(self, ctx, character, base_penalty):
                return 3.0

        collider = mkplayer("SeamCollide", 61009)
        collider.mode = CollidingMode()
        assert srv.mode_context(collider).mode_state == {
            "score": 0.0,
            "points": 0.0,
            "xp": 0.0,
        }
        srv.mode_award_kill(collider, "Collide Rat", 4.0, 4.0)
        ledger = srv.mode_context(collider).mode_state
        assert ledger["_engine"] == {"points": 1.5, "xp": 2.5}, ledger
        srv.mode_note_death(collider, 9.0)
        ledger = srv.mode_context(collider).mode_state
        assert ledger["_engine"] == {"points": 1.5, "xp": 2.5, "penalty": 3.0}, ledger
        # The mode's own keys are exactly as the mode left them: the engine
        # writes only under "_engine", whatever the mode named its fields.
        assert (ledger["score"], ledger["points"], ledger["xp"]) == (0.0, 0.0, 0.0)
        unplayer(collider, "SeamCollide")
        print("ENGINE_LEDGER_KEYS_OK")

        # --- A raising hook costs its own character, not the engine -------
        class BrokenMode(srv.BaseMode):
            name = "broken"

            def on_tick(self, ctx, character):
                raise RuntimeError("boom")

            def score_kill(self, ctx, killer, victim, base_points, base_xp):
                raise RuntimeError("boom")

        broken = mkplayer("SeamBroken", 61008)
        broken.mode = BrokenMode()
        srv.mode_tick(broken)  # must not raise into the heartbeat
        # The corrupt-ledger ValueError from isolate() is contained the same way.
        broken.mode_store["broken"] = []
        srv.mode_tick(broken)
        broken.mode_store["broken"] = {}
        # A kill through a broken mode still pays the engine: containment is
        # about the mode's ledger, never the player's award.
        broken.base_attack = 50
        srv.npcs["broken_dummy"] = {
            "id": "broken_dummy",
            "name": "Broken Dummy",
            "room": "town_square",
            "hp": 1,
            "max_hp": 20,
            "attack": 0,
            "hostile": False,
            "behavior": "idle",
            "loot": [],
            "gold": 0,
            "respawn_seconds": 60,
            "alive": True,
            "respawn_at": None,
            "contributors": {},
        }
        gold_before = broken.gold
        await srv.cmd_attack(broken, {"target": "broken dummy"})
        assert not srv.npcs["broken_dummy"]["alive"]
        assert srv.get_score_entry(broken.name)["kills"] == 1
        assert broken.gold >= gold_before
        del srv.npcs["broken_dummy"]
        unplayer(broken, "SeamBroken")
        print("HOOK_FAILURE_CONTAINED_OK")

        # --- Ledgers are per mode name and survive a bad store only ------
        store = {}
        a = srv.isolate(store, "one")
        b = srv.isolate(store, "two")
        assert a is not b and a == {} and store["one"] is a
        store["corrupt"] = []
        try:
            srv.isolate(store, "corrupt")
            raise AssertionError("corrupt ledger must raise")
        except ValueError:
            pass
        print("LEDGER_ISOLATION_OK")

        print("MODE_SEAM_OK")
    finally:
        srv.send = saved_send
        srv.DEFAULT_MODE = default_mode


asyncio.run(main())
