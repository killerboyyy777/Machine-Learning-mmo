"""NPC AI v2 telemetry counters for issue #589 (no live server).

Run from the repo root:  python tests/test_npc_ai_telemetry.py
Covers the process-global counters behind the gate's npc_ai_v2 checks: the
since-on-first-activity rule, the event whitelist (leash_returned is emitted
by the state machine but deliberately uncounted), the bump at npc_apply
execution time, the time_in_combat_s accrual, the snapshot shape, and that
both /api/state and the GM tables carry the block.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv

COUNTERS = (
    "aggro_acquired",
    "target_switched",
    "target_cleared",
    "leash_broken",
    "flee_started",
    "flee_succeeded",
)


def snap():
    return dict(srv.NPC_AI_STATS)


def live_npc(state, alive=True):
    return {
        "id": time.time_ns(),
        "name": "w1",
        "room": "Forest",
        "alive": alive,
        "state": state,
    }


async def main():
    saved = snap()
    saved_send = srv.send
    try:
        # --- fresh import: zeros, no since --------------------------------
        for key in COUNTERS:
            assert srv.NPC_AI_STATS[key] == 0, key
        assert srv.NPC_AI_STATS["time_in_combat_s"] == 0.0
        assert srv.NPC_AI_STATS["since"] == 0.0
        print("INIT_OK")

        # --- a no-tracking accrual must not stamp `since` -----------------
        assert srv._npc_ai_engaged_time([], 5.0) == 0.0
        assert (
            srv._npc_ai_engaged_time(
                [live_npc("IDLE"), live_npc("AGGRO", alive=False)], 5.0
            )
            == 0.0
        )
        assert srv.NPC_AI_STATS["since"] == 0.0
        assert srv._npc_ai_engaged_time([], -1.0) == 0.0
        print("IDLE_NO_STAMP_OK")

        # --- note whitelist: listed events count, others are dropped ------
        srv._npc_ai_note("aggro_acquired")
        assert srv.NPC_AI_STATS["aggro_acquired"] == 1
        assert srv.NPC_AI_STATS["since"] > 0.0
        since = srv.NPC_AI_STATS["since"]
        srv._npc_ai_note("leash_returned")  # emitted by the machine, not counted
        srv._npc_ai_note("target_switched")
        srv._npc_ai_note("bogus")
        assert srv.NPC_AI_STATS["leash_broken"] == 0
        assert srv.NPC_AI_STATS["target_switched"] == 1
        assert srv.NPC_AI_STATS["since"] == since
        print("NOTE_OK")

        # --- npc_apply bumps from the executed intent only ----------------
        npc = live_npc("IDLE")
        await srv.npc_apply(
            npc,
            {
                "kind": "state",
                "state": "AGGRO",
                "target": "p1",
                "event": "aggro_acquired",
            },
            {},
            time.time(),
        )
        assert npc["state"] == "AGGRO"
        assert npc.get("engaged_since") is not None
        assert srv.NPC_AI_STATS["aggro_acquired"] == 2
        await srv.npc_apply(
            npc,
            {"kind": "state", "state": "FLEEING", "event": "flee_started"},
            {},
            time.time(),
        )
        assert srv.NPC_AI_STATS["flee_started"] == 1
        await srv.npc_apply(
            npc,
            {"kind": "state", "state": "IDLE", "event": "flee_succeeded"},
            {},
            time.time(),
        )
        assert srv.NPC_AI_STATS["flee_succeeded"] == 1
        assert npc.get("engaged_since") is None
        print("APPLY_OK")

        # --- a baseline intent (no event) writes nothing ------------------
        base = snap()
        await srv.npc_apply(npc, {"kind": "state", "state": "AGGRO"}, {}, time.time())
        assert snap() == base
        print("NO_EVENT_OK")

        # --- engaged time counts AGGRO/CHASING npcs per tick --------------
        engaged = srv._npc_ai_engaged_time(
            [
                live_npc("AGGRO"),
                live_npc("CHASING"),
                live_npc("IDLE"),
                live_npc("AGGRO", alive=False),
            ],
            10.0,
        )
        assert engaged == 20.0
        assert srv.NPC_AI_STATS["time_in_combat_s"] == 20.0
        print("ENGAGED_OK")

        # --- snapshot shape ----------------------------------------------
        shot = srv._npc_ai_snapshot()
        assert set(shot) == set(srv.NPC_AI_STATS)
        assert shot["time_in_combat_s"] == round(
            srv.NPC_AI_STATS["time_in_combat_s"], 3
        )
        assert shot["since"] == srv.NPC_AI_STATS["since"]
        print("SNAPSHOT_OK")

        # --- both surfaces carry the block --------------------------------
        assert srv.world_snapshot()["npc_ai"] == shot
        inbox = []

        async def stub(player, msg):
            inbox.append(msg)

        srv.send = stub
        await srv.cmd_gm_tables({}, {})
        tables = inbox[-1]
        assert tables["type"] == "tables"
        assert tables["npc_ai"] == shot
        print("SURFACES_OK")
    finally:
        srv.NPC_AI_STATS.clear()
        srv.NPC_AI_STATS.update(saved)
        srv.send = saved_send


asyncio.run(main())
print("ALL_NPC_AI_TELEMETRY_OK")
