"""Policy budget and fault isolation (#176 phase 2).

Run from the repo root:  python tests/test_policy_budget.py

A plugin is synchronous by contract, so an inline policy_fn runs on the
event loop: one wedged policy stops every agent in the fleet, and the
step watchdog (which only covers `await env.step`) cannot see it. These
cases pin the budget that fixes that, and pin the old inline behaviour
when no budget is configured.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor.supervisor import (
    POLICY_FAULT_LIMIT,
    AgentTask,
    PolicyFailure,
    Supervisor,
)


class FakeEnv:
    """Minimal env: two steps per episode, mask of two actions."""

    def __init__(self):
        self.resets = 0
        self.steps = 0

    async def reset(self):
        # Yield like a real env does: AgentTask.run() loops episodes forever,
        # so an env that never suspends turns a healthy agent into a busy loop
        # that starves the event loop and everything watching it.
        await asyncio.sleep(0)
        self.resets += 1
        return {"episode": self.resets}

    async def step(self, action):
        await asyncio.sleep(0)
        self.steps += 1
        return {"step": self.steps}, 1.0, self.steps >= 2, {}

    def valid_action_mask(self):
        return [True, True]

    async def close(self):
        pass


class FakeRegistry:
    def __init__(self):
        self.episodes = []

    def get(self, agent_id):
        return None

    def record_episode(self, agent_id, reward):
        self.episodes.append((agent_id, reward))


class FakeMetrics:
    def __init__(self):
        self.deaths = []
        self.events = []

    def log_agent_death(self, agent_id, episodes, total_reward, error=None):
        self.deaths.append((agent_id, error))

    def log_episode(self, *args, **kwargs):
        pass

    def log(self, event, **fields):
        self.events.append((event, fields))


async def _drain(task):
    async for _ in task.run():
        pass


def _wedged(obs, agent_id):
    time.sleep(5.0)
    return 0


def test_inline_budget_is_unchanged():
    """No budget: the policy runs inline, exactly as before #176."""

    async def main():
        env = FakeEnv()
        task = AgentTask("inline", env, lambda obs, aid: 0, max_steps=10)
        assert task.policy_timeout is None
        async for event in task.run():
            return event
        raise AssertionError("episode never completed")

    event = asyncio.run(main())
    assert event["episode"] == 1, event
    assert event["steps"] == 2, event
    print("POLICY_BUDGET_INLINE_OK")


def test_budget_ends_the_wait_and_keeps_the_loop_alive():
    """A wedged policy costs its own task, not the event loop."""

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(60):
                await asyncio.sleep(0.01)
                ticks += 1

        task = AgentTask("wedged", FakeEnv(), _wedged, policy_timeout=0.2)
        results = await asyncio.gather(_drain(task), ticker(), return_exceptions=True)
        return results[0], ticks, task

    result, ticks, task = asyncio.run(main())
    assert isinstance(result, RuntimeError), result
    assert "policy timeout after 0.2s" in str(result), result
    assert "policy timeout after 0.2s" in task.last_error, task.last_error
    # The loop kept turning while the policy hung: a wedged plugin is a
    # dead agent, never a dead fleet.
    assert ticks >= 10, ticks
    assert task.done.is_set()
    print("POLICY_BUDGET_TIMEOUT_OK")


def test_raising_policy_still_reaches_the_supervisor():
    """Budget or not, an exception is a crash the supervisor owns.

    Since #176 it arrives as PolicyFailure so the supervisor can tell a
    broken plugin from a bad episode; the plugin's own exception is kept as
    the cause and named in the message.
    """

    def boom(obs, agent_id):
        raise ValueError("plugin is broken")

    async def main():
        task = AgentTask("boom", FakeEnv(), boom, policy_timeout=1.0)
        try:
            await _drain(task)
        except PolicyFailure as exc:
            return exc, task
        raise AssertionError("the raise was swallowed")

    exc, task = asyncio.run(main())
    assert isinstance(exc.__cause__, ValueError), exc.__cause__
    assert str(exc.__cause__) == "plugin is broken", exc.__cause__
    assert "ValueError" in task.last_error, task.last_error
    print("POLICY_BUDGET_RAISE_OK")


def test_arity_dispatch_survives_the_thread_hop():
    """2, 3 and 4 argument policies all arrive intact off the loop."""

    seen = {}

    def two(obs, agent_id):
        seen.setdefault("two", []).append((obs, agent_id))
        return 0

    def three(obs, agent_id, mask):
        seen.setdefault("three", []).append((obs, agent_id, mask))
        return 1

    def four(obs, agent_id, mask, env):
        seen.setdefault("four", []).append((obs, agent_id, mask, env))
        return 1

    async def run_one(policy):
        task = AgentTask("arity", FakeEnv(), policy, max_steps=4, policy_timeout=2.0)
        async for event in task.run():
            return event
        raise AssertionError("episode never completed")

    for name, policy in (("two", two), ("three", three), ("four", four)):
        asyncio.run(run_one(policy))
        assert name in seen, (name, seen)
        # The first call of the episode sees the reset observation; later
        # calls see a step observation, so check the shape of the first.
        first = seen[name][0]
        assert first[0] == {"episode": 1}, first
        assert first[1] == "arity", first
        if name != "two":
            assert first[2] == [True, True], first
        if name == "four":
            assert isinstance(first[3], FakeEnv), first
        assert len(seen[name]) >= 2, (name, seen[name])
    print("POLICY_BUDGET_ARITY_OK")


def test_supervisor_carries_the_budget_and_its_specs():
    """The supervisor default reaches the task, and the spec tuple stays whole."""

    async def main():
        metrics = FakeMetrics()
        sup = Supervisor(FakeRegistry(), metrics=metrics, policy_timeout=0.2)
        started = await sup.start_agent("sup", lambda aid: FakeEnv(), _wedged)
        assert started
        task = sup._tasks["sup"]
        assert task.policy_timeout == 0.2, task.policy_timeout
        assert len(sup._specs["sup"]) == 8, sup._specs["sup"]
        await asyncio.sleep(0.6)
        assert "sup" not in sup._tasks
        return metrics

    metrics = asyncio.run(main())
    assert metrics.deaths, "an isolated timeout must be logged as a death"
    agent_id, error = metrics.deaths[0]
    assert agent_id == "sup", agent_id
    assert "policy timeout after 0.2s" in (error or ""), error
    print("POLICY_BUDGET_SUPERVISOR_OK")


def test_shared_policy_is_called_one_at_a_time():
    """Agents in a slot share one plugin instance, so its calls serialise."""

    async def main():
        overlap = {"active": 0, "peak": 0}

        def shared(obs, agent_id):
            overlap["active"] += 1
            overlap["peak"] = max(overlap["peak"], overlap["active"])
            time.sleep(0.02)
            overlap["active"] -= 1
            return 0

        registry = FakeRegistry()
        sup = Supervisor(registry, policy_timeout=5.0)
        for name in ("share-a", "share-b"):
            assert await sup.start_agent(name, lambda aid: FakeEnv(), shared)
        # Both agents resolved to the worker that belongs to the closure.
        assert sup._policy_executors[shared] is not None
        await asyncio.sleep(0.6)
        for name in ("share-a", "share-b"):
            await sup.stop_agent(name)
        return overlap, registry

    overlap, registry = asyncio.run(main())
    assert overlap["peak"] == 1, overlap
    assert {aid for aid, _ in registry.episodes} == {
        "share-a",
        "share-b",
    }, registry.episodes
    print("POLICY_BUDGET_SHARED_POLICY_OK")


def test_restart_retires_unused_policy_executors():
    """PBT restarts rebuild the closure, so its old worker must be retired.

    Restart overlaps the old task's teardown with the replacement's launch:
    a worker may only be shut down once no task still uses its closure. Three
    restarts used to leave up to four workers alive.
    """

    def fresh_policy():
        def policy(obs, agent_id):
            return 0

        return policy

    def factory(ckpt, hp):
        return fresh_policy(), None, None

    class AliveRegistry(FakeRegistry):
        class Entry:
            alive = True
            goal = None
            checkpoint_path = None

        def get(self, agent_id):
            return self.Entry()

    async def main():
        sup = Supervisor(AliveRegistry(), policy_timeout=1.0)
        assert await sup.start_agent(
            "rr",
            lambda aid: FakeEnv(),
            lambda o, a: 0,
            policy_factory=factory,
        )
        retired = []
        for _ in range(3):
            retired.extend(list(sup._policy_executors.values()))
            assert await sup.restart_agent("rr") is True
            assert len(sup._policy_executors) == 1, sup._policy_executors
            assert len(sup._policy_users) == 1, sup._policy_users
        await sup.stop_all()
        assert not sup._policy_executors, sup._policy_executors
        assert not sup._policy_users, sup._policy_users
        return retired

    retired = asyncio.run(main())
    for executor in retired:
        try:
            executor.submit(lambda: None)
        except RuntimeError:
            continue
        raise AssertionError("retired policy worker still accepts work")
    print("POLICY_BUDGET_RESTART_EXECUTORS_OK")


def test_unhashable_policy_retires_its_own_executor():
    """An unhashable closure has no shared-worker entry; its own must die."""

    class Unhashable:
        __hash__ = None

        def __call__(self, obs, agent_id):
            return 0

    sup = Supervisor(FakeRegistry())
    task = AgentTask("unhashable", FakeEnv(), Unhashable(), policy_timeout=1.0)
    executor = task.policy_executor()
    try:
        executor.submit(lambda: None).result()
    except RuntimeError:
        raise AssertionError("executor should be alive before release")
    sup._release_policy(task)
    try:
        executor.submit(lambda: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unhashable policy leaked its worker")
    print("POLICY_BUDGET_UNHASHABLE_RELEASE_OK")


def test_wedged_plugins_do_not_starve_healthy_ones():
    """More wedged calls than the loop's default pool holds leave a healthy
    agent untouched: each plugin closure owns its worker."""
    import threading

    release = threading.Event()

    def make_wedged():
        def plug(obs, agent_id):
            release.wait(10.0)
            return 0

        return plug

    async def main():
        registry = FakeRegistry()
        sup = Supervisor(registry, max_concurrent=80, policy_timeout=0.2)
        for i in range(40):
            assert await sup.start_agent(
                f"wedge-{i}", lambda aid: FakeEnv(), make_wedged()
            )
        assert await sup.start_agent(
            "healthy", lambda aid: FakeEnv(), lambda obs, aid: 0
        )
        try:
            await asyncio.sleep(0.8)
            return [e for e in registry.episodes if e[0] == "healthy"]
        finally:
            release.set()
            for name in list(sup._tasks):
                await sup.stop_agent(name)

    episodes = asyncio.run(main())
    # 40 blocked workers are more than the default executor's capacity, so a
    # shared pool would have queued this agent behind them and reaped it.
    assert len(episodes) >= 2, episodes
    print("POLICY_BUDGET_STARVATION_OK")


def test_policy_raising_timeout_is_not_a_budget_expiry():
    """A plugin may raise TimeoutError itself; that is a crash, not a budget."""

    def trap(obs, agent_id):
        raise TimeoutError("the plugin did not like that")

    async def main():
        task = AgentTask("trap", FakeEnv(), trap, policy_timeout=2.0)
        try:
            await _drain(task)
        except PolicyFailure as exc:
            return exc, task
        raise AssertionError("the plugin's TimeoutError was swallowed")

    exc, task = asyncio.run(main())
    assert isinstance(exc.__cause__, TimeoutError), exc.__cause__
    assert str(exc.__cause__) == "the plugin did not like that", exc.__cause__
    assert "policy timeout after" not in (task.last_error or ""), task.last_error
    print("POLICY_BUDGET_PLUGIN_TIMEOUT_OK")


def test_a_raising_policy_is_retired_after_the_fault_limit():
    """Three consecutive faults retire the plugin instead of respawning it."""

    def boom(obs, agent_id):
        raise ValueError("plugin is broken")

    async def wait_for_death(sup, name):
        for _ in range(100):
            if name not in sup._tasks:
                return True
            await asyncio.sleep(0.02)
        return False

    async def main():
        registry = FakeRegistry()
        metrics = FakeMetrics()
        sup = Supervisor(registry, max_concurrent=2, metrics=metrics)
        started = 0
        for i in range(POLICY_FAULT_LIMIT):
            name = f"boom-{i}"
            started += await sup.start_agent(name, lambda aid: FakeEnv(), boom)
            assert await wait_for_death(sup, name)
        disabled = sup.policy_disabled(boom)
        refused = await sup.start_agent("late", lambda aid: FakeEnv(), boom)
        return started, disabled, refused, metrics, sup

    started, disabled, refused, metrics, sup = asyncio.run(main())
    assert started == POLICY_FAULT_LIMIT, started
    assert disabled is not None and "ValueError" in disabled, disabled
    # A retired plugin is refused fresh agents (the conductor asks here
    # before it would hand the slot another agent).
    assert refused is False
    assert [e for e, _ in metrics.events if e == "plugin_disabled"], metrics.events
    assert len(metrics.deaths) == POLICY_FAULT_LIMIT, metrics.deaths
    assert boom in sup.disabled_policies()
    print("POLICY_FAULT_LIMIT_OK")


def test_a_completed_episode_resets_the_fault_count():
    """An occasional fault is survivable: only consecutive faults retire."""

    seen = {}

    def flaky(obs, agent_id):
        # One faulted agent, three clean ones, then two more faults: three
        # failures in total but never three in a row.
        seen[agent_id] = seen.get(agent_id, 0) + 1
        if agent_id in ("flaky-0", "flaky-4", "flaky-5") and seen[agent_id] == 1:
            raise ValueError("not this one")
        return 0

    async def run_one(sup, registry, name):
        """Let one agent run until it records an episode, then stop it.

        An agent that keeps completing episodes never goes idle on its own,
        so the test has to stop it rather than wait for the task to end.
        """
        before = len(registry.episodes)
        await sup.start_agent(name, lambda aid: FakeEnv(), flaky)
        for _ in range(100):
            if len(registry.episodes) > before or name not in sup._tasks:
                break
            await asyncio.sleep(0.02)
        await sup.stop_agent(name)

    async def main():
        registry = FakeRegistry()
        sup = Supervisor(registry, max_concurrent=1)
        for i in range(6):
            await run_one(sup, registry, f"flaky-{i}")
        return sup, registry

    sup, registry = asyncio.run(main())
    assert sup.policy_disabled(flaky) is None, sup.disabled_policies()
    # Flaky-1..3 and the second step of each ran a full episode, which is
    # what cleared the count between the faults.
    assert len(registry.episodes) >= 3, registry.episodes
    print("POLICY_FAULT_RESET_OK")


test_inline_budget_is_unchanged()
test_budget_ends_the_wait_and_keeps_the_loop_alive()
test_raising_policy_still_reaches_the_supervisor()
test_arity_dispatch_survives_the_thread_hop()
test_supervisor_carries_the_budget_and_its_specs()
test_shared_policy_is_called_one_at_a_time()
test_restart_retires_unused_policy_executors()
test_unhashable_policy_retires_its_own_executor()
test_wedged_plugins_do_not_starve_healthy_ones()
test_policy_raising_timeout_is_not_a_budget_expiry()
test_a_raising_policy_is_retired_after_the_fault_limit()
test_a_completed_episode_resets_the_fault_count()
print("ALL_POLICY_BUDGET_OK")
