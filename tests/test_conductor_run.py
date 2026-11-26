"""Conductor integration: supervised episodes flow end to end (no server).

Run from the repo root:  python tests/test_conductor_run.py
Covers the composition unit tests skip: Supervisor records episodes +
feeds the mixer + writes episode metrics rows (#210) + fires the churn
episode hook; Conductor.run performs wave startup, churn turnover, and
persists the registry -- all against fake envs.
"""

import asyncio
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor import (
    ChurnManager,
    Conductor,
    MetricsLogger,
    Mixer,
    Registry,
    Supervisor,
)


class FakeEnv:
    """Two-step episodes, then done. No network, no sleeps."""

    def __init__(self):
        self._state = {}
        self._n = 0

    async def reset(self):
        await asyncio.sleep(0)  # yield like a real (network) env does --
        # without this the task loop never yields and starves the loop
        self._n = 0
        return {"obs": 0}

    async def step(self, action):
        await asyncio.sleep(0)  # yield, see reset()
        self._n += 1
        return ({"obs": self._n}, 1.0, True, {})


async def _supervisor_flow(tmpdir):
    reg = Registry(os.path.join(tmpdir, "reg"), max_agents=5)
    mx = Mixer(reg, ["town_square"])
    mlog = MetricsLogger(os.path.join(tmpdir, "m.jsonl"))
    cm = ChurnManager(reg)
    sup = Supervisor(reg, mixer=mx, metrics=mlog, episode_hook=cm.report_episode)
    for aid in ("k1", "k2", "k3"):
        reg.register(aid, "linear")
        if aid != "k3":
            cm._lifetimes[aid] = 1  # one episode each, then churn death
        await sup.start_agent(aid, lambda i: FakeEnv(), lambda o, a: 0, max_steps=5)
    await asyncio.sleep(0.5)
    assert reg.get("k1").episodes >= 1 and reg.get("k2").episodes >= 1
    assert reg.get("k1").alive is False  # hook-fired lifetime expiry
    assert await sup.reap() >= 1  # dead tasks reaped
    assert "k1" not in sup._specs and "k2" not in sup._specs
    assert "k3" in sup._specs  # live agent keeps its restart spec
    mlog.flush()
    rows = [json.loads(line) for line in open(os.path.join(tmpdir, "m.jsonl"))]
    kinds = [r["event"] for r in rows]
    assert "episode" in kinds, kinds  # #210: episode rows are actually written
    assert "agent_death" in kinds
    await sup.stop_all()
    assert sup.status()["running"] == 0
    assert not sup._specs
    print("SUPERVISOR_FLOW_OK")


async def _conductor_run(tmpdir):
    cond = Conductor(
        os.path.join(tmpdir, "cond"),
        max_agents=3,
        arrivals_per_minute=6000,
        mean_lifetime_episodes=2,
        runners=[
            {
                "env_factory": lambda i: FakeEnv(),
                "policy_fn": lambda o, a: 0,
                "weight": 1,
            }
        ],
        url="ws://localhost:9",
    )
    await cond.run(duration_seconds=4, wave_size=10, wave_delay=0.01)
    snap = cond.registry.snapshot()
    total_eps = sum(a["episodes"] for a in snap["agents"])
    assert snap["alive"] >= 1, snap
    assert total_eps >= 3, snap  # real episodes completed, not just spawns
    assert os.path.isfile(os.path.join(tmpdir, "cond", "registry", "registry.json"))
    rows = [
        json.loads(line) for line in open(os.path.join(tmpdir, "cond", "metrics.jsonl"))
    ]
    assert any(r["event"] == "episode" for r in rows)
    st = cond.status()
    assert st["supervisor"]["running"] == 0  # clean shutdown, no stray tasks
    print("CONDUCTOR_RUN_OK")


async def _conductor_resume(tmpdir):
    d = os.path.join(tmpdir, "resume")
    c1 = Conductor(d, max_agents=3)
    aid = c1.churn._spawn_one()
    goal = dict(c1.registry.get(aid).goal or {})
    c1.registry.save()
    # resume=True (default): the next Conductor on the same dir picks up
    # the previous population with goals and parents intact.
    c2 = Conductor(d, max_agents=3)
    assert c2._pending_resume == [aid]  # loaded, but no task until run()
    got = c2.registry.get(aid)
    assert got is not None and got.goal == goal
    assert got.parent_id == c1.registry.get(aid).parent_id
    assert c2.registry.load_lineage(aid)["goal"] == goal
    # resume=False: guaranteed-fresh population even when a save exists.
    c3 = Conductor(d, max_agents=3, resume=False)
    assert c3.registry.snapshot()["total"] == 0
    print("CONDUCTOR_RESUME_OK")


def _live_ids(base):
    with open(os.path.join(base, "registry", "registry.json")) as fh:
        data = json.load(fh)
    return [a["agent_id"] for a in data["agents"] if a["alive"]]


async def _conductor_resume_restarts_tasks(tmpdir):
    """A second run restarts saved agents and fills only missing slots."""
    base = os.path.join(tmpdir, "resume-tasks")

    def runner():
        return {
            "env_factory": lambda i: FakeEnv(),
            "policy_fn": lambda o, a: 0,
            "weight": 1,
        }

    first = Conductor(
        base,
        max_agents=3,
        arrivals_per_minute=0,
        mean_lifetime_episodes=1000000,
        runners=[runner()],
        url="ws://localhost:9",
        resume=False,
    )
    await first.run(duration_seconds=1, wave_size=10, wave_delay=0.01)
    saved = _live_ids(base)
    assert len(saved) == 3, saved
    characters = {
        a["agent_id"]: a["character"]
        for a in first.registry.snapshot()["agents"]
        if a["alive"]
    }

    second = Conductor(
        base,
        max_agents=3,
        arrivals_per_minute=0,
        mean_lifetime_episodes=1000000,
        runners=[runner()],
        url="ws://localhost:9",
    )
    observed = []

    async def poll():
        for _ in range(100):
            observed.append(second.supervisor.status()["running"])
            await asyncio.sleep(0.02)

    await asyncio.gather(
        second.run(duration_seconds=2, wave_size=10, wave_delay=0.01), poll()
    )
    assert max(observed) >= len(saved), observed
    assert set(_live_ids(base)) == set(saved)  # no fresh full wave, no registry-full
    assert set(saved) <= set(second.churn._lifetimes)  # fresh episode lifetimes
    assert {
        aid: second.registry.get(aid).character for aid in saved
    } == characters  # stable login names survived the restart

    third = Conductor(
        base,
        max_agents=5,
        arrivals_per_minute=0,
        mean_lifetime_episodes=1000000,
        runners=[runner()],
        url="ws://localhost:9",
    )
    await third.run(duration_seconds=1, wave_size=10, wave_delay=0.01)
    grown = _live_ids(base)
    assert len(grown) == 5 and set(saved) <= set(grown), grown

    # Negative control: a persisted dead entry gets no task; only the gap
    # is refilled by a fresh spawn, and the alive count stays at the cap.
    reg_path = os.path.join(base, "registry", "registry.json")
    with open(reg_path) as fh:
        data = json.load(fh)
    dead = next(a["agent_id"] for a in data["agents"] if a["alive"])
    for row in data["agents"]:
        if row["agent_id"] == dead:
            row["alive"] = False
    with open(reg_path, "w") as fh:
        json.dump(data, fh)
    fourth = Conductor(
        base,
        max_agents=5,
        arrivals_per_minute=0,
        mean_lifetime_episodes=1000000,
        runners=[runner()],
        url="ws://localhost:9",
    )
    observed_dead = []

    async def poll_dead():
        for _ in range(100):
            observed_dead.append(fourth.supervisor.status()["running"])
            await asyncio.sleep(0.02)

    await asyncio.gather(
        fourth.run(duration_seconds=2, wave_size=10, wave_delay=0.01), poll_dead()
    )
    final = _live_ids(base)
    assert len(final) == 5 and dead not in final, final
    assert (set(grown) - {dead}) <= set(final), final
    assert max(observed_dead) >= 4, observed_dead
    print("CONDUCTOR_RESUME_TASKS_OK")


async def main():
    tmpdir = tempfile.mkdtemp()
    await _supervisor_flow(tmpdir)
    await _conductor_run(tmpdir)
    await _conductor_resume(tmpdir)
    # A geometric draw can be tiny even with a huge mean; pin it so the
    # population in this regression cannot expire mid-run on CI.
    with mock.patch("ml.conductor.churn.geometric_lifetime", return_value=10**9):
        await _conductor_resume_restarts_tasks(tmpdir)


asyncio.run(main())
print("ALL_CONDUCTOR_RUN_OK")
