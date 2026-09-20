"""Conductor unit tests (no live server needed).

Run from the repo root:  python tests/test_conductor.py
Covers: registry CRUD, churn spawn/death, mixer rebalance, metrics writes.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ml.conductor as cond
from ml.conductor import (
    AgentTask,
    ChurnManager,
    Conductor,
    MetricsLogger,
    Mixer,
    Registry,
    Supervisor,
)
from ml.conductor.churn import geometric_lifetime, poisson_interval, wave_startup

# #47: public classes are importable from the package root
assert {"Conductor", "Registry", "Supervisor", "ChurnManager", "Mixer",
        "MetricsLogger", "PBTManager", "AgentEntry", "AgentTask"} <= set(cond.__all__)
print("EXPORTS_OK")

tmpdir = tempfile.mkdtemp()
reg_path = os.path.join(tmpdir, "reg")

# --- Registry ---
reg = Registry(reg_path, max_agents=5)
reg.register("a1", "linear")
reg.register("a2", "torch")
reg.register("a3", "linear", branch="stable")
assert len(reg.alive_agents()) == 3
assert len(reg.alive_agents(agent_type="linear")) == 2
assert len(reg.alive_agents(branch="stable")) == 1
reg.record_episode("a1", 10.0)
reg.record_episode("a1", 20.0)
assert reg.get("a1").episodes == 2
assert reg.get("a1").mean_reward == 15.0
reg.promote("a2")
assert reg.get("a2").branch == "stable"
reg.demote("a3")
assert reg.get("a3").branch == "experimental"
reg.remove("a3")
assert reg.get("a3") is None
snap = reg.snapshot()
assert snap["alive"] == 2
reg.save()
reg2 = Registry(reg_path, max_agents=5)
reg2.load()
assert reg2.get("a1").episodes == 2
# #46: full-precision total survives the round-trip (not mean*episodes)
assert reg2.get("a1").total_reward == 30.0
assert reg2.get("a1").mean_reward == 15.0
print("REGISTRY_OK")

# --- #293 lineage checkpoints + atomic round-trip + resume ---
import json as _json3
import ml.conductor.churn as _churn
from unittest import mock as _mock
regr = Registry(os.path.join(tmpdir, "lineage"), max_agents=5)
cml = ChurnManager(regr, arrivals_per_minute=0)
cml._next_arrival = float("inf")
# Seed one parent, then force the offspring branch for the next spawn.
pgoal = {"w_score": 1.0}
regr.register("p0", "linear", goal=pgoal)
cml._recent_goals.append(("p0", pgoal))
with _mock.patch.object(_churn.random, "random", return_value=0.0):
    kid = cml._spawn_one()
kentry = regr.get(kid)
assert kentry.parent_id == "p0"  # parentage recorded, not just the weights
lin = regr.load_lineage(kid)
assert lin["parent_id"] == "p0"
assert lin["goal"] == kentry.goal
assert lin["ckpt"] is None  # fresh spawn: no checkpoint file yet
# Simulate training writing weights, then a save: the lineage ckpt
# snapshot follows ("weights change" is visible across saves).
with open(kentry.checkpoint_path, "wb") as f:
    f.write(b"v1")
regr.save()
assert open(regr.load_lineage(kid)["ckpt"], "rb").read() == b"v1"
with open(kentry.checkpoint_path, "wb") as f:
    f.write(b"v2")
regr.save()
assert open(regr.load_lineage(kid)["ckpt"], "rb").read() == b"v2"
# Atomic write: no .tmp litter, registry.json always parses.
assert not os.path.exists(os.path.join(tmpdir, "lineage", "registry.json.tmp"))
with open(os.path.join(tmpdir, "lineage", "registry.json")) as f:
    _json3.load(f)
# Resume: a fresh Registry on the same dir restores entries, goals,
# and parents (Conductor(resume=True) leans on exactly this).
regr2 = Registry(os.path.join(tmpdir, "lineage"), max_agents=5)
regr2.load()
assert regr2.get(kid).parent_id == "p0"
assert regr2.get(kid).goal == kentry.goal
assert regr2.get("p0") is not None
# Disk guard: removing an entry prunes its lineage dir on next save.
regr.remove("p0")
regr.save()
assert regr.load_lineage("p0") is None
print("LINEAGE_OK")

# --- Churn helpers ---
intervals = [poisson_interval(60.0) for _ in range(100)]
assert all(i >= 0 for i in intervals)
assert 0.3 < sum(intervals) / len(intervals) < 2.0
lifetimes = [geometric_lifetime(50) for _ in range(200)]
assert all(l >= 1 for l in lifetimes)
assert 30 < sum(lifetimes) / len(lifetimes) < 80
delays = list(wave_startup(25, 10, 5.0))
assert len(delays) == 25
assert all(d >= 0 for d in delays)
assert delays[10] > delays[9]
# Differential scheduling: 50 agents in waves of 10 every 2s starts
# in ~8.6s total, not the 212s the old absolute-then-sum behavior took.
assert sum(wave_startup(50, 10, 2.0)) < 60.0
print("CHURN_HELPERS_OK")

# --- ChurnManager tick (arrivals only; deaths come from episodes) ---
reg3 = Registry(os.path.join(tmpdir, "reg3"), max_agents=5)
cm = ChurnManager(reg3, arrivals_per_minute=1000, mean_lifetime_episodes=2,
                  top_up_per_tick=0)  # Poisson character, no top-up
cm._next_arrival = 0
arrived = cm.tick(1.0)
assert len(arrived) == 1
aid = arrived[0]
assert reg3.get(aid) is not None
# Ticking alone never kills, however many ticks pass...
cm._next_arrival = float("inf")
for _ in range(50):
    cm.tick(1.0)
assert reg3.get(aid).alive
# ...only completed episodes age lifetimes (forced to 1, one report kills)
cm._lifetimes[aid] = 1
assert cm.report_episode(aid) is True
assert not reg3.get(aid).alive
assert cm.report_episode(aid) is False  # unknown/gone ids are safe
assert cm.report_episode("nobody") is False
print("CHURN_TICK_OK")

# --- spawn-to-target top-up (#214): multi-arrival ticks fill toward cap ---
regt = Registry(os.path.join(tmpdir, "regt"), max_agents=5)
cmt = ChurnManager(regt, arrivals_per_minute=0, mean_lifetime_episodes=100,
                   top_up_per_tick=3)
cmt._next_arrival = float("inf")  # Poisson off: top-up alone
assert len(cmt.tick(1.0)) == 3
assert len(cmt.tick(1.0)) == 2  # only room for 2 more: capped, not overfilled
assert cmt.tick(1.0) == []  # at cap: nothing, no RuntimeError
assert len(regt.alive_agents()) == 5 and regt.snapshot()["total"] == 5
# top-up respects a dead slot the same way: kill one, next tick refills one
regt.get(regt.alive_agents()[0].agent_id).alive = False
assert len(cmt.tick(1.0)) == 1
assert len(regt.alive_agents()) == 5
print("CHURN_TOP_UP_OK")

# --- #277 _spawn_one is collision-proof under forced id reuse ---
import ml.conductor.churn as _churn
from unittest import mock as _mock
regr = Registry(os.path.join(tmpdir, "collide"), max_agents=10)
cmc = ChurnManager(regr, arrivals_per_minute=0, top_up_per_tick=5)
cmc._next_arrival = float("inf")  # Poisson off: direct spawns only
_frozen = {"return_value": 1234.567}
with _mock.patch.object(_churn.time, "time", **_frozen), \
     _mock.patch.object(_churn.random, "randint", side_effect=[7, 7, 8]):
    _a1 = cmc._spawn_one()  # draws 7 -> free
    _a2 = cmc._spawn_one()  # draws 7 -> taken, retries to 8
assert _a1 != _a2 and len(regr.alive_agents()) == 2
# pre-fix code returned the same id twice here (register hands back the
# existing entry) and left only 1 alive agent.
# Exhaustion still surfaces as RuntimeError (both tick paths tolerate it).
with _mock.patch.object(_churn.time, "time", **_frozen), \
     _mock.patch.object(_churn.random, "randint", return_value=7):
    try:
        cmc._spawn_one(max_attempts=5)
        _exhausted = False
    except RuntimeError:
        _exhausted = True
assert _exhausted
# Top-up fills to cap even when every first draw collides.
regr2 = Registry(os.path.join(tmpdir, "collide2"), max_agents=3)
cmt2 = ChurnManager(regr2, arrivals_per_minute=0, top_up_per_tick=3)
cmt2._next_arrival = float("inf")
with _mock.patch.object(_churn.time, "time", **_frozen), \
     _mock.patch.object(_churn.random, "randint",
                        side_effect=[1, 1, 2, 1, 2, 3]):
    assert len(cmt2.tick(1.0)) == 3
assert len(regr2.alive_agents()) == 3
print("SPAWN_COLLISION_OK")

# --- Mixer ---
reg4 = Registry(os.path.join(tmpdir, "reg4"), max_agents=10)
mx = Mixer.__new__(Mixer)
mx.registry = reg4
mx.floor_ids = ["f1", "f2"]
mx.min_per_floor = 1
mx.max_per_floor = 5
mx._floor_rewards = {"f1": [10, 10, 10], "f2": [2, 2, 2]}
mx._floor_agents = {"f1": ["a", "b", "c"], "f2": ["d", "e"]}
moves = mx.rebalance()
assert len(moves) > 0
snap = mx.snapshot()
assert snap["f1"]["mean_reward"] > snap["f2"]["mean_reward"]
print("MIXER_OK")

# --- Metrics ---
mlog = MetricsLogger(os.path.join(tmpdir, "test.jsonl"))
mlog.log_agent_spawn("a1", "linear", "experimental")
mlog.log_episode("a1", 1, 5.0, 100)
mlog.flush()
with open(os.path.join(tmpdir, "test.jsonl")) as f:
    lines = f.readlines()
assert len(lines) == 2
ev1 = json.loads(lines[0])
assert ev1["event"] == "agent_spawn" and ev1["agent_id"] == "a1"
ev2 = json.loads(lines[1])
assert ev2["event"] == "episode" and ev2["reward"] == 5.0
# time-based auto-flush: flush_every=0 writes through immediately
mlog2 = MetricsLogger(os.path.join(tmpdir, "t2.jsonl"),
                      buffer_size=10000, flush_every=0)
mlog2.log("ping")
with open(os.path.join(tmpdir, "t2.jsonl")) as f:
    assert len(f.readlines()) == 1
# size rotation: ~1KB budget keeps current + 2 siblings; old segments
# age out by design (bounded disk), newest events always survive
mlog3 = MetricsLogger(os.path.join(tmpdir, "rot.jsonl"),
                      buffer_size=10000, flush_every=0,
                      rotate_mb=0.001, keep_files=2)
for i in range(300):
    mlog3.log("tick", i=i, pad="x" * 100)
mlog3.flush()
import json as _json2
seen = []
for suffix in ("", ".1", ".2"):
    p = os.path.join(tmpdir, f"rot.jsonl{suffix}")
    if os.path.exists(p):
        with open(p) as f:
            seen.extend(_json2.loads(line)["i"] for line in f)
assert 0 < len(seen) < 300, len(seen)  # rotated (not everything kept)
assert max(seen) == 299  # newest data always preserved
assert not os.path.exists(os.path.join(tmpdir, "rot.jsonl.3"))
print("METRICS_OK")

# --- #45 supervisor steps counter increments per env step ---
class _FakeEnv:
    async def reset(self):
        await asyncio.sleep(0)  # yield like a real (network) env does
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(0)
        return ({"obs": 1}, 1.0, True, {})


async def _collect(task, n=2):
    # run() is infinite by design (stops only on cancel) -- take n
    # episodes (1 step each: the fake env is always done) then stop it.
    out = []
    gen = task.run()
    async for ev in gen:
        out.append(ev)
        if len(out) >= n:
            task.cancel()
            await gen.aclose()
            break
    return out


at = AgentTask("t1", _FakeEnv(), lambda obs, aid: 0, max_steps=5)
evs = asyncio.run(_collect(at))
assert at.steps == 2 and at.episodes == 2, (at.steps, at.episodes)
assert [e["steps"] for e in evs] == [1, 2]
print("SUPERVISOR_STEPS_OK")

# --- churn arrivals skip a full registry instead of raising ---
r_full = Registry(os.path.join(tmpdir, "full"), max_agents=1)
cm_full = ChurnManager(r_full, arrivals_per_minute=1000000,
                       mean_lifetime_episodes=10000)
cm_full._next_arrival = 0
assert len(cm_full.tick(1.0)) == 1  # fills the single slot
cm_full._next_arrival = 0
assert cm_full.tick(1.0) == []  # full: skipped, no RuntimeError
assert len(r_full.alive_agents()) == 1
# corpses don't wedge long runs: a dead slot is reusable for arrivals
dead_id = r_full.alive_agents()[0].agent_id
assert cm_full.report_episode(dead_id) is False  # long lifetime: no death yet
r_full.get(dead_id).alive = False
cm_full._next_arrival = 0
assert len(cm_full.tick(1.0)) == 1
assert len(r_full.alive_agents()) == 1 and r_full.snapshot()["total"] == 2
print("CHURN_FULL_OK")

# --- rebalance always terminates: uniform no-signal hung the old loop ---
import threading as _th

mxu = Mixer(Registry(os.path.join(tmpdir, "mxu"), max_agents=50),
            ["f1", "f2", "f3"])
mxu._floor_rewards = {"f1": [0, 0], "f2": [0, 0], "f3": [0, 0]}
mxu._floor_agents = {"f1": ["a", "b", "c"], "f2": ["d", "e", "f"],
                     "f3": ["g", "h", "i"]}
_out = []
_t = _th.Thread(target=lambda: _out.append(mxu.rebalance()), daemon=True)
_t.start()
_t.join(timeout=5)
assert not _t.is_alive(), "rebalance hung on uniform input"
assert _out[0] == []  # nowhere to go: every floor is a donor, none a receiver
assert sum(len(v) for v in mxu._floor_agents.values()) == 9  # conserved
print("MIXER_NO_HANG_OK")

# --- rebalance moves respect donor/receiver bounds ---
mxb = Mixer(Registry(os.path.join(tmpdir, "mxb"), max_agents=50),
            ["f1", "f2"])
mxb._floor_rewards = {"f1": [10, 10, 10], "f2": [0, 0, 0]}
mxb._floor_agents = {"f1": ["a"], "f2": ["b", "c", "d", "e", "f"]}
moves = mxb.rebalance()
assert sum(len(v) for v in mxb._floor_agents.values()) == 6  # conserved
assert len(moves) == 4  # f2 surplus 4 -> f1 deficit
assert all(m[1] == "f2" and m[2] == "f1" for m in moves)
assert len(mxb._floor_agents["f1"]) == 5  # took up toward (not past) target
assert len(mxb._floor_agents["f2"]) == 1  # gave down to (not below) target
print("MIXER_BOUNDS_OK")

# --- stopping tasks closes their envs (no ghost sockets) ---
class _CloseEnv(_FakeEnv):
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def _stop_check():
    r = Registry(os.path.join(tmpdir, "stop"), max_agents=5)
    sup = Supervisor(r)
    env = _CloseEnv()
    await sup.start_agent("c1", lambda aid: env, lambda obs, aid: 0)
    await asyncio.sleep(0.1)
    assert "c1" in sup._tasks
    await sup.stop_agent("c1")
    assert "c1" not in sup._tasks and env.closed
    # reap closes envs of churn-killed agents too
    env2 = _CloseEnv()
    r.register("c2", "linear")
    await sup.start_agent("c2", lambda aid: env2, lambda obs, aid: 0)
    await asyncio.sleep(0.1)
    r.get("c2").alive = False
    assert await sup.reap() == 1
    assert env2.closed

asyncio.run(_stop_check())
print("STOP_CLOSES_ENV_OK")

# --- #135 death accounting: crashes log agent_death with traceback ---
import json as _json


class _CrashEnv(_FakeEnv):
    async def step(self, action):
        await asyncio.sleep(0)
        raise RuntimeError("synthetic crash")


async def _crash_check():
    r = Registry(os.path.join(tmpdir, "crash"), max_agents=5)
    mlog = MetricsLogger(os.path.join(tmpdir, "crash.jsonl"))
    sup = Supervisor(r, metrics=mlog)
    r.register("boom", "linear")
    await sup.start_agent("boom", lambda aid: _CrashEnv(), lambda obs, aid: 0)
    for _ in range(50):
        if "boom" not in sup._tasks:
            break
        await asyncio.sleep(0.05)
    assert "boom" not in sup._tasks
    assert r.get("boom").alive is False
    mlog.flush()
    with open(os.path.join(tmpdir, "crash.jsonl")) as f:
        evs = [_json.loads(line) for line in f]
    deaths = [e for e in evs if e["event"] == "agent_death"]
    assert len(deaths) == 1 and deaths[0]["agent_id"] == "boom", evs
    assert "synthetic crash" in deaths[0].get("error", ""), deaths[0]
    return True


assert asyncio.run(_crash_check())
print("DEATH_CRASH_OK")


async def _reap_death_check():
    r = Registry(os.path.join(tmpdir, "reapdeath"), max_agents=5)
    mlog = MetricsLogger(os.path.join(tmpdir, "reapdeath.jsonl"))
    sup = Supervisor(r, metrics=mlog)
    r.register("old", "torch")
    env = _FakeEnv()
    env._state = {"room_id": "town_square", "is_dungeon": False,
                  "dungeon_floor": 0}
    await sup.start_agent("old", lambda aid: env, lambda obs, aid: 0)
    await asyncio.sleep(0.2)
    r.get("old").alive = False
    assert await sup.reap() == 1
    mlog.flush()
    with open(os.path.join(tmpdir, "reapdeath.jsonl")) as f:
        evs = [_json.loads(line) for line in f]
    deaths = [e for e in evs if e["event"] == "agent_death"]
    assert len(deaths) == 1 and deaths[0]["agent_id"] == "old", evs
    assert "error" not in deaths[0], deaths[0]  # churn, not crash
    return True


assert asyncio.run(_reap_death_check())
print("DEATH_REAP_OK")

# --- start_agent reports status; bad factories never propagate ---
async def _start_status():
    r = Registry(os.path.join(tmpdir, "st"), max_agents=5)
    sup = Supervisor(r, max_concurrent=1)
    env = _FakeEnv()

    def boom(aid):
        raise RuntimeError("factory exploded")

    assert await sup.start_agent("ok", lambda aid: env, lambda o, a: 0) is True
    assert await sup.start_agent("ok", lambda aid: env, lambda o, a: 0) is False
    assert await sup.start_agent("full", lambda aid: env, lambda o, a: 0) is False
    assert await sup.start_agent("boom", boom, lambda o, a: 0) is False
    await sup.stop_all()

asyncio.run(_start_status())
print("START_STATUS_OK")


async def _restart_reloads():
    # PBT exploit restarts the loser so copied weights take effect (#228);
    # dead entries are never resurrected by a restart.
    r = Registry(os.path.join(tmpdir, "rst"), max_agents=5)
    r.register("ok", "linear")
    sup = Supervisor(r, max_concurrent=1)
    assert await sup.start_agent("ok", lambda aid: _FakeEnv(), lambda o, a: 0) is True
    old = sup._tasks["ok"]
    assert await sup.restart_agent("ok") is True
    assert sup._tasks["ok"] is not old
    assert old.task.done()
    r.mark_dead("ok")
    assert await sup.restart_agent("ok") is False
    assert sup._tasks["ok"].agent_id == "ok"  # running task untouched
    assert await sup.restart_agent("ghost") is False
    await sup.stop_all()

asyncio.run(_restart_reloads())
print("RESTART_OK")


async def _restart_rebuilds_weights():
    # The production plugin factory reloads the checkpoint file on every
    # build (#228): pre-exploit weights never leak into the relaunched
    # policy, and hparams overlay onto the live policy (unknown keys and
    # missing files are tolerated, never fatal).
    from ml.ml_client import LinearQAgent
    from ml.ml_env import (OBS_SIZE, N_ACTIONS, ROOM_LIST, DIRECTIONS,
                           NPC_LIST, ITEM_LIST, flatten_obs)
    from ml.conductor.conductor import _plugin_policy_factory
    r = Registry(os.path.join(tmpdir, "rstw"), max_agents=5)
    entry = r.register("w", "linear")
    ckpt = entry.checkpoint_path

    def _obs():
        z = lambda n: [0.0] * n
        return {
            "room_onehot": z(len(ROOM_LIST)), "is_dungeon": 0.0,
            "floor_norm": 0.0, "exits_mask": z(len(DIRECTIONS)),
            "npc_presence": z(len(NPC_LIST)), "npc_unknown_count": 0.0,
            "item_presence": z(len(ITEM_LIST)),
            "inv_presence": z(len(ITEM_LIST)), "equipped_flag": 0.0,
            "inv_unknown_flag": 0.0, "hp_frac": 1.0, "gold_norm": 0.0,
            "score_norm": 0.0, "variety": 0.0, "allies_norm": 0.0,
            "party_norm": 0.0, "level_norm": 0.0, "xp_progress": 0.0,
            "market_norm": 0.0, "market_any": 0.0, "tax_rate": 0.0,
            "tax_min_norm": 0.0, "own_net_norm": 0.0,
            "flip_margin_norm": 0.0, "inv_value_norm": 0.0,
            "quest_active": 0.0, "quest_ready": 0.0,
            "quest_has_charm": 0.0, "quest_mat_bark": 0.0,
            "quest_mat_hide": 0.0, "quest_mat_ecto": 0.0,
            "quest_giver_here": 0.0, "quest2_active": 0.0,
            "quest2_ready": 0.0, "quest2_giver_here": 0.0,
            "quest3_active": 0.0, "quest3_ready": 0.0,
            "quest3_giver_here": 0.0, "quest4_active": 0.0,
            "quest4_ready": 0.0, "quest4_giver_here": 0.0,
            "arrows_norm": 0.0, "buff_attack": 0.0, "buff_dr": 0.0,
            "ammo_best_norm": 0.0, "defense_norm": 0.0,
        }

    obs = _obs()
    flat = flatten_obs(obs)
    assert len(flat) == OBS_SIZE, (len(flat), OBS_SIZE)
    hp_idx = flat.index(1.0)  # only hp_frac is nonzero

    def _save(which):
        a = LinearQAgent(OBS_SIZE, N_ACTIONS)
        if which == 2:
            a.weights[3][hp_idx] = 5.0
        a.save(ckpt)

    build = _plugin_policy_factory("linear", {"epsilon": 0.0})
    _save(1)
    assert build(ckpt, {})(obs, "w") == 0  # zeroed weights: ties -> 0
    _save(2)  # PBT winner->loser checkpoint copy
    assert build(ckpt, {"epsilon": 0.0})(obs, "w") == 3  # new weights live
    # hparams reach the live policy: epsilon=1 explores, bogus keys ignored
    wild = {build(ckpt, {"epsilon": 1.0, "bogus": 1})(obs, "w")
            for _ in range(50)}
    assert len(wild) > 1, wild
    # missing checkpoint file: fresh policy, no raise
    assert build(os.path.join(tmpdir, "nope.pt"), {})(obs, "w") == 0


asyncio.run(_restart_rebuilds_weights())
print("RESTART_WEIGHTS_OK")


async def _restart_uses_factory_and_reassigns():
    # restart_agent rebuilds via the stored factory with
    # (entry.checkpoint_path, hparams), installs the result, and puts the
    # agent back on exactly one floor (shutdown had dropped it).
    r = Registry(os.path.join(tmpdir, "rstf"), max_agents=5)
    entry = r.register("w2", "linear")
    mx = Mixer(r, ["f1", "f2"])
    sup = Supervisor(r, max_concurrent=1, mixer=mx)
    calls = []

    def factory(ckpt, hp):
        calls.append((ckpt, dict(hp)))
        return lambda o, a: 7

    env = _FakeEnv()
    assert await sup.start_agent("w2", lambda aid: env, lambda o, a: 0,
                                 policy_factory=factory) is True
    assert all("w2" not in v for v in mx._floor_agents.values())
    assert await sup.restart_agent("w2", hparams={"epsilon": 0.5}) is True
    assert calls == [(entry.checkpoint_path, {"epsilon": 0.5})], calls
    assert sup._tasks["w2"].policy_fn({"obs": 0}, "w2") == 7
    assert sum(v.count("w2") for v in mx._floor_agents.values()) == 1
    await sup.stop_all()


asyncio.run(_restart_uses_factory_and_reassigns())
print("RESTART_FACTORY_OK")


async def _maybe_start_check():
    import tempfile as _tf

    tmp2 = _tf.mkdtemp()
    cond = Conductor(os.path.join(tmp2, "c"), max_agents=5, runners=[
        {"env_factory": lambda aid: (_ for _ in ()).throw(RuntimeError("x")),
         "policy_fn": lambda o, a: 0},
    ])
    cond.registry.register("z1", "linear")
    # raising factory: False, no exception, nothing logged/assigned
    assert await cond._maybe_start("z1") is False
    assert cond.mixer._floor_agents == {"town_square": [], "graveyard": [],
                                        "d_10_f1": []}
    # healthy slot: True
    cond2 = Conductor(os.path.join(tmp2, "c2"), max_agents=5, runners=[
        {"plugin": "gather"},
    ])
    cond2.registry.register("z2", "linear")
    assert await cond2._maybe_start("z2") is True
    assert cond2.registry.get("z2").agent_type == "scripted"
    await cond2.supervisor.stop_all()

asyncio.run(_maybe_start_check())
print("MAYBE_START_OK")

# --- assign_one spreads single arrivals across floors ---
_mx2 = Mixer(Registry(os.path.join(tmpdir, "mx2"), max_agents=10),
             ["f1", "f2", "f3"])
for _aid in ["x", "y", "z", "w"]:
    _mx2.assign_one(_aid)
assert [_mx2._floor_agents[f] for f in ["f1", "f2", "f3"]] == [["x", "w"], ["y"], ["z"]]
print("MIXER_ASSIGN_ONE_OK")

# --- #48 wave_fill is awaitable and fills without blocking ---
async def _fill():
    r = Registry(os.path.join(tmpdir, "wf"), max_agents=5)
    cm = ChurnManager(r)
    n = await cm.wave_fill(3, wave_size=10, wave_delay=0.01)
    assert n == 3 and len(r.alive_agents()) == 3

asyncio.run(_fill())
print("WAVE_FILL_ASYNC_OK")

# --- supervisor episodes age churn lifetimes (episode, not wall-clock) ---
async def _hook_check():
    r = Registry(os.path.join(tmpdir, "hook"), max_agents=5)
    cm = ChurnManager(r, arrivals_per_minute=0)
    sup = Supervisor(r, episode_hook=cm.report_episode)
    aid = cm._spawn_one()
    cm._lifetimes[aid] = 2
    env = _FakeEnv()
    await sup.start_agent(aid, lambda i: env, lambda obs, x: 0)
    await asyncio.sleep(0.4)
    # episodes completed -> lifetime charged down (2 -> 1 -> 0 -> dead)
    assert r.get(aid).alive is False
    await sup.stop_all()

asyncio.run(_hook_check())
print("EPISODE_HOOK_OK")

# --- #50 watchdog: hung env.step ends the episode instead of wedging ---
import time as _time


class _SlowEnv:
    async def reset(self):
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(5)
        return ({}, 0.0, True, {})


at_slow = AgentTask("t-slow", _SlowEnv(), lambda obs, aid: 0,
                    max_steps=5, step_timeout=0.05)
_t0 = _time.time()
_evs_slow = asyncio.run(_collect(at_slow, n=1))
_dt = _time.time() - _t0
assert _dt < 2.0, _dt
assert "timeout" in (at_slow.last_error or "").lower(), at_slow.last_error
print("WATCHDOG_OK")

# --- #51 mixer auto-integration: supervisor feeds episode rewards ---
async def _sup_mixer():
    r = Registry(os.path.join(tmpdir, "sup"), max_agents=5)
    mx = Mixer(r, ["town_square"])
    sup = Supervisor(r, mixer=mx)
    env = _FakeEnv()
    env._state = {"room_id": "town_square", "is_dungeon": False,
                  "dungeon_floor": 0}
    await sup.start_agent("s1", lambda aid: env, lambda obs, aid: 0)
    await asyncio.sleep(0.3)
    assert len(mx._floor_rewards["town_square"]) > 0
    assert mx.snapshot()["town_square"]["mean_reward"] == 1.0
    await sup.stop_all()
    # ended tasks leave no corpses in floor lists
    assert mx._floor_agents == {"town_square": []}
    assert mx.snapshot()["town_square"]["agents"] == 0

asyncio.run(_sup_mixer())
print("MIXER_AUTO_OK")

# --- #210: completed episodes land in metrics.jsonl via the supervisor ---
async def _sup_episodes():
    mpath = os.path.join(tmpdir, "episodes.jsonl")
    mlog = MetricsLogger(mpath, buffer_size=10000, flush_every=0)
    r = Registry(os.path.join(tmpdir, "sup_ep"), max_agents=5)
    sup = Supervisor(r, metrics=mlog)
    await sup.start_agent("e1", lambda aid: _FakeEnv(), lambda obs, aid: 0)
    await asyncio.sleep(0.3)
    await sup.stop_all()
    mlog.flush()
    rows = [json.loads(line) for line in open(mpath) if line.strip()]
    eps = [row for row in rows if row.get("event") == "episode"]
    assert len(eps) >= 2, rows  # 1-step fake env completes many episodes
    assert all(row["agent_id"] == "e1" for row in eps)
    assert [row["episode"] for row in eps] == sorted(row["episode"] for row in eps)
    assert all(row["reward"] == 1.0 for row in eps)  # fake env pays 1.0/step

asyncio.run(_sup_episodes())
print("EPISODE_METRICS_OK")

async def _no_ghosts():
    # Failed starts die instead of lingering alive without tasks (#230):
    # a slotless conductor must end with alive == running == 0.
    cpath = os.path.join(tmpdir, "ghost")
    cond = Conductor(cpath, max_agents=4)
    await cond.run(duration_seconds=3, wave_size=4, wave_delay=0.1)
    st = cond.status()
    assert st["registry"]["alive"] == 0, st["registry"]
    assert st["supervisor"]["running"] == 0, st["supervisor"]
    # mark_dead is safe on unknown ids and keeps history rows.
    assert cond.registry.mark_dead("nope") is None
    await cond.supervisor.stop_all()

asyncio.run(_no_ghosts())
print("NO_GHOSTS_OK")
# --- #162 Phase 1: registry carries goals through save/load round-trip ---
import random as _random
_random.seed(11)
from ml.ml_env import GOAL_AXES, sample_goal
g_reg = sample_goal()
reg_goal = Registry(os.path.join(tmpdir, "reg_goal"), max_agents=5)
reg_goal.register("ga", "linear", goal=g_reg)
snap = reg_goal.snapshot()
assert snap["agents"][0]["goal"] == g_reg
reg_goal.save()
reg_goal2 = Registry(os.path.join(tmpdir, "reg_goal2"), max_agents=5)
reg_goal2.load(os.path.join(tmpdir, "reg_goal", "registry.json"))
assert reg_goal2.get("ga").goal == g_reg
print("REGISTRY_GOAL_OK")

# --- #162 Phase 1: churn spawns carry simplex goals (fresh + mutated) ---
_random.seed(21)
reg_ch = Registry(os.path.join(tmpdir, "reg_ch"), max_agents=50)
cm_ch = ChurnManager(reg_ch, arrivals_per_minute=100000, mean_lifetime_episodes=10000)
goals = []
for _ in range(20):
    cm_ch._next_arrival = 0  # Poisson due every tick...
    # ...plus one top-up each (cap 50, never reached here): 2 per tick.
    for aid in cm_ch.tick(1.0):
        goals.append(reg_ch.get(aid).goal)
assert len(goals) == 40, len(goals)
for g in goals:
    assert set(g) == {"w_" + a for a in GOAL_AXES}
    assert abs(sum(g.values()) - 1.0) < 1e-9
    assert all(x >= 0 for x in g.values())
assert len({tuple(sorted(g.items())) for g in goals}) > 1  # diversified
print("CHURN_GOALS_OK")

# --- #162 Phase 1: supervisor wires registry goals to envs + metric rows ---
async def _sup_goals():
    mpath = os.path.join(tmpdir, "episodes_goal.jsonl")
    mlog = MetricsLogger(mpath, buffer_size=10000, flush_every=0)
    r = Registry(os.path.join(tmpdir, "sup_goal"), max_agents=5)
    _random.seed(31)
    g_sup = sample_goal()
    r.register("sg1", "linear", goal=g_sup)
    sup = Supervisor(r, metrics=mlog)
    made = {}

    def _factory(aid):
        env = _FakeEnv()
        made[aid] = env
        return env

    await sup.start_agent("sg1", _factory, lambda obs, aid: 0)
    await asyncio.sleep(0.3)
    assert made["sg1"].goal == g_sup  # env carries the registry goal
    await sup.stop_all()
    mlog.flush()
    rows = [json.loads(line) for line in open(mpath) if line.strip()]
    eps = [row for row in rows if row.get("event") == "episode"]
    assert len(eps) >= 2, rows
    assert all(row.get("goal") == g_sup for row in eps)

asyncio.run(_sup_goals())
print("SUPERVISOR_GOALS_OK")

# --- #289: Dirichlet spawn distribution is uniform over the simplex ---
_random.seed(289)
_N289 = 5000
_acc289 = [0.0] * len(GOAL_AXES)
for _ in range(_N289):
    _g289 = sample_goal()
    for _i289, _a289 in enumerate(GOAL_AXES):
        _acc289[_i289] += _g289["w_" + _a289]
for _i289, _a289 in enumerate(GOAL_AXES):
    _mean289 = _acc289[_i289] / _N289
    assert abs(_mean289 - 1.0 / len(GOAL_AXES)) < 0.02, (_a289, _mean289)
print("GOAL_DIST_OK")

# --- #289: offspring mutate the parent goal (mechanism + fraction) ---
import ml.conductor.churn as _churn289
from unittest import mock as _mock289
r_os = Registry(os.path.join(tmpdir, "reg_os"), max_agents=60)
_parent289 = {"w_" + _a: (1.0 if _a == "score" else 0.0) for _a in GOAL_AXES}
cm_os = ChurnManager(r_os, arrivals_per_minute=0, mean_lifetime_episodes=10000)
cm_os._recent_goals.append(("p289", _parent289))
with _mock289.patch.object(_churn289, "OFFSPRING_FRACTION", 1.0):
    # lineage shape (#293): _sample_goal returns (goal, parent_id).
    _kid_pairs = [cm_os._sample_goal() for _ in range(50)]
    _kids289 = [g for g, _p in _kid_pairs]
    assert all(p == "p289" for _g, p in _kid_pairs)
# mutants keep the parent's dominant axis (fresh draws would scatter 1/7
# each way: all-50 agreement has probability (1/7)^50 ~ 0).
assert all(max(_g, key=_g.get) == "w_score" for _g in _kids289)
assert len({tuple(sorted(_g.items())) for _g in _kids289}) > 1  # noise, not clones
with _mock289.patch.object(_churn289, "OFFSPRING_FRACTION", 0.0):
    _fresh_pairs = [cm_os._sample_goal() for _ in range(50)]
    _fresh289 = [g for g, _p in _fresh_pairs]
    assert all(p is None for _g, p in _fresh_pairs)
assert any(max(_g, key=_g.get) != "w_score" for _g in _fresh289)
# production fraction: ~half the batch derives from the parent (100
# mutants with dominant score + ~100/7 fresh scoring by chance).
_random.seed(2891)
_dom289 = 0
for _ in range(200):
    _gdom, _pdom = cm_os._sample_goal()
    if max(_gdom, key=_gdom.get) == "w_score":
        _dom289 += 1
assert 95 <= _dom289 <= 135, _dom289
print("GOAL_OFFSPRING_OK")

print("ALL_CONDUCTOR_OK")
