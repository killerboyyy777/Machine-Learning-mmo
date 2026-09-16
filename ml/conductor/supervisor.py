"""Supervisor: per-task isolation, episode management, fault recovery.

Each agent runs in its own isolated task (asyncio Task). If an agent crashes,
the supervisor catches the exception, marks it dead, and spawns a replacement
without affecting other agents.
"""
import asyncio
import inspect
import time
import traceback


def _env_floor(env):
    """Best-effort floor/cell id for an env (TextMMOEnv state shape).

    Dungeon floors become "d<N>", surface rooms keep their room id,
    anything unrecognized becomes "unknown" (Mixer drops those)."""
    s = getattr(env, "_state", None) or {}
    if s.get("is_dungeon"):
        return f"d{s.get('dungeon_floor', 0) or 0}"
    return s.get("room_id") or "unknown"


class AgentTask:
    """Wraps one agent's episode loop with fault isolation."""

    def __init__(self, agent_id, env, policy_fn, max_steps=2000, step_timeout=None):
        self.agent_id = agent_id
        self.env = env
        self.policy_fn = policy_fn
        self.max_steps = max_steps
        # Watchdog (#50): max wall-clock seconds per env.step; a hung
        # step ends the episode instead of wedging the task forever.
        # None disables. Only guards the async env call -- a blocking
        # sync policy_fn would wedge the loop itself (keep policies fast).
        self.step_timeout = step_timeout
        self._policy_takes_mask = None  # probed from the fn signature once
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
                    if self._policy_takes_mask is None:
                        try:
                            n_params = len(inspect.signature(self.policy_fn).parameters)
                        except (TypeError, ValueError):
                            n_params = 2
                        self._policy_takes_mask = n_params >= 3
                    if self._policy_takes_mask:
                        try:
                            mask = self.env.valid_action_mask()
                        except Exception:
                            mask = None
                        action = self.policy_fn(obs, self.agent_id, mask)
                    else:
                        action = self.policy_fn(obs, self.agent_id)
                    try:
                        if self.step_timeout is not None:
                            obs, reward, done, info = await asyncio.wait_for(
                                self.env.step(action), self.step_timeout)
                        else:
                            obs, reward, done, info = await self.env.step(action)
                    except asyncio.TimeoutError:
                        self.last_error = (
                            f"step timeout after {self.step_timeout}s "
                            f"(episode {self.episodes + 1})")
                        break
                    episode_reward += reward
                    self.steps += 1
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

    async def aclose(self):
        """Stop the task AND close its env. Without the env close, the
        socket (and its reader task) stays open server-side: the server
        keeps a live, logged-in ghost that counts as online forever."""
        self.cancel()
        env_close = getattr(self.env, "close", None)
        if env_close is not None:
            try:
                await env_close()
            except Exception:
                pass


class Supervisor:
    """Manages isolated agent tasks with fault recovery."""

    def __init__(self, registry, max_concurrent=50, mixer=None, floor_fn=None,
                 step_timeout=None):
        self.registry = registry
        self.max_concurrent = max_concurrent
        # Mixer auto-integration (#51): every completed episode is fed to
        # the mixer via floor_fn(env) -- no manual record_reward calls.
        self._mixer = mixer
        self._floor_fn = floor_fn or _env_floor
        self._step_timeout = step_timeout
        self._tasks = {}  # agent_id -> AgentTask
        self._lock = asyncio.Lock()

    async def start_agent(self, agent_id, env_factory, policy_fn, max_steps=2000,
                          step_timeout=None):
        """Start an isolated task for one agent."""
        async with self._lock:
            if agent_id in self._tasks:
                return
            if len(self._tasks) >= self.max_concurrent:
                return
            env = env_factory(agent_id)
            at = AgentTask(agent_id, env, policy_fn, max_steps,
                           step_timeout=step_timeout if step_timeout is not None
                           else self._step_timeout)
            self._tasks[agent_id] = at
            at.task = asyncio.ensure_future(self._run_with_recovery(at))

    async def _run_with_recovery(self, at):
        """Run an agent task, handling crashes gracefully."""
        try:
            async for event in at.run():
                self.registry.record_episode(at.agent_id, event["reward"])
                if self._mixer is not None:
                    try:
                        floor = self._floor_fn(at.env)
                    except Exception:
                        floor = None
                    if floor:
                        self._mixer.record_reward(floor, at.agent_id, event["reward"])
        except asyncio.CancelledError:
            pass
        except Exception:
            entry = self.registry.get(at.agent_id)
            if entry:
                entry.alive = False
        finally:
            self._tasks.pop(at.agent_id, None)

    async def _shutdown(self, at):
        """Cancel a task and close its env (idempotent, never raises,
        never blocks longer than a bounded close)."""
        if at is None:
            return
        try:
            await asyncio.wait_for(at.aclose(), 15.0)
        except Exception:
            pass

    async def stop_agent(self, agent_id):
        async with self._lock:
            at = self._tasks.pop(agent_id, None)
        await self._shutdown(at)

    async def reap(self):
        """Stop tasks whose registry entry is dead or gone (churn deaths).
        Returns the number of reaped tasks."""
        async with self._lock:
            dead = []
            for aid in self._tasks:
                entry = self.registry.get(aid)
                if entry is None or not entry.alive:
                    dead.append(aid)
            tasks = [self._tasks.pop(aid, None) for aid in dead]
        for at in tasks:
            await self._shutdown(at)
        return len(dead)

    async def stop_all(self):
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for at in tasks:
            await self._shutdown(at)

    def status(self):
        return {
            "running": len(self._tasks),
            "agents": {aid: {"episodes": at.episodes, "reward": round(at.total_reward, 2)}
                       for aid, at in self._tasks.items()},
        }
