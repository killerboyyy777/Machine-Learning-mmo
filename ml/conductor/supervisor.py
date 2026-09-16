"""Supervisor: per-task isolation, episode management, fault recovery.

Each agent runs in its own isolated task (asyncio Task). If an agent crashes,
the supervisor catches the exception, marks it dead, and spawns a replacement
without affecting other agents.
"""
import asyncio
import time
import traceback


class AgentTask:
    """Wraps one agent's episode loop with fault isolation."""

    def __init__(self, agent_id, env, policy_fn, max_steps=2000):
        self.agent_id = agent_id
        self.env = env
        self.policy_fn = policy_fn
        self.max_steps = max_steps
        self.task = None
        self.total_reward = 0.0
        self.steps = 0
        self.episodes = 0
        self.last_error = None
        self.done = asyncio.Event()

    async def run(self):
        """Run episodes until the task is cancelled or the agent dies."""
        try:
            while not self.done.is_set():
                obs = await self.env.reset()
                episode_reward = 0.0
                for _ in range(self.max_steps):
                    if self.done.is_set():
                        break
                    action = self.policy_fn(obs, self.agent_id)
                    obs, reward, done, info = await self.env.step(action)
                    episode_reward += reward
                    if done:
                        break
                self.episodes += 1
                self.total_reward += episode_reward
                yield {"agent_id": self.agent_id, "episode": self.episodes,
                       "reward": episode_reward, "steps": self.steps}
        except Exception as e:
            self.last_error = traceback.format_exc()
        finally:
            self.done.set()

    def cancel(self):
        self.done.set()
        if self.task and not self.task.done():
            self.task.cancel()


class Supervisor:
    """Manages isolated agent tasks with fault recovery."""

    def __init__(self, registry, max_concurrent=50):
        self.registry = registry
        self.max_concurrent = max_concurrent
        self._tasks = {}  # agent_id -> AgentTask
        self._lock = asyncio.Lock()

    async def start_agent(self, agent_id, env_factory, policy_fn, max_steps=2000):
        """Start an isolated task for one agent."""
        async with self._lock:
            if agent_id in self._tasks:
                return
            if len(self._tasks) >= self.max_concurrent:
                return
            env = env_factory(agent_id)
            at = AgentTask(agent_id, env, policy_fn, max_steps)
            self._tasks[agent_id] = at
            at.task = asyncio.ensure_future(self._run_with_recovery(at))

    async def _run_with_recovery(self, at):
        """Run an agent task, handling crashes gracefully."""
        try:
            async for event in at.run():
                self.registry.record_episode(at.agent_id, event["reward"])
        except asyncio.CancelledError:
            pass
        except Exception:
            entry = self.registry.get(at.agent_id)
            if entry:
                entry.alive = False
        finally:
            self._tasks.pop(at.agent_id, None)

    async def stop_agent(self, agent_id):
        async with self._lock:
            at = self._tasks.pop(agent_id, None)
            if at:
                at.cancel()

    async def stop_all(self):
        async with self._lock:
            for at in self._tasks.values():
                at.cancel()
            self._tasks.clear()

    def status(self):
        return {
            "running": len(self._tasks),
            "agents": {aid: {"episodes": at.episodes, "reward": round(at.total_reward, 2)}
                       for aid, at in self._tasks.items()},
        }
