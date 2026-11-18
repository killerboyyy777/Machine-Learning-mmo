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

from ml.conductor.supervisor import AgentTask, Supervisor


class FakeEnv:
    """Minimal env: two steps per episode, mask of two actions."""

    def __init__(self):
        self.resets = 0
        self.steps = 0

    async def reset(self):
        self.resets += 1
        return {"episode": self.resets}

    async def step(self, action):
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

    def log_agent_death(self, agent_id, episodes, total_reward, error=None):
        self.deaths.append((agent_id, error))

    def log_episode(self, *args, **kwargs):
        pass

    def log(self, *args, **kwargs):
        pass


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
    """Budget or not, an exception is a crash the supervisor owns."""

    def boom(obs, agent_id):
        raise ValueError("plugin is broken")

    async def main():
        task = AgentTask("boom", FakeEnv(), boom, policy_timeout=1.0)
        try:
            await _drain(task)
        except ValueError as exc:
            return str(exc), task
        raise AssertionError("the raise was swallowed")

    message, task = asyncio.run(main())
    assert message == "plugin is broken", message
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


test_inline_budget_is_unchanged()
test_budget_ends_the_wait_and_keeps_the_loop_alive()
test_raising_policy_still_reaches_the_supervisor()
test_arity_dispatch_survives_the_thread_hop()
test_supervisor_carries_the_budget_and_its_specs()
print("ALL_POLICY_BUDGET_OK")
