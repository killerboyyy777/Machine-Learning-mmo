"""Agent churn: Poisson arrivals, geometric lifetimes, wave startup.

Agents arrive according to a Poisson process (rate = arrivals_per_minute).
Each tick also tops up toward the registry cap (at most top_up_per_tick),
so episode-driven deaths can't bleed the population below cap.
Each agent's lifetime is drawn from a geometric distribution (mean =
mean_lifetime_episodes) and counts COMPLETED EPISODES, not wall-clock
ticks -- an idle agent that never finishes episodes never ages. The
supervisor reports each completed episode via report_episode() (wired by
the conductor); without supervised tasks, lifetimes never expire.
Wave startup batches arrivals into waves for faster initial fill.
New spawns carry inheritable goal weights (#162 Phase 1): half fresh
Dirichlet samples, half mutations of recent goals (in-memory pool;
lineage files arrive in Phase 2).
"""
import asyncio
import random
import time
from collections import deque

from ml.ml_env import OFFSPRING_FRACTION, mutate_goal, sample_goal


def poisson_interval(rate_per_minute):
    """Draw the next inter-arrival time (in seconds) for a Poisson process."""
    if rate_per_minute <= 0:
        return float("inf")
    return random.expovariate(rate_per_minute / 60.0)


def geometric_lifetime(mean_episodes):
    """Draw an agent lifetime (in episodes) from a geometric distribution."""
    return max(1, int(random.expovariate(1.0 / mean_episodes)))


def wave_startup(count, wave_size, wave_delay_seconds):
    """Generate per-agent waits for a wave-based startup.

    Yields DIFFERENTIAL delays (seconds to wait since the previous spawn),
    batched into waves -- callers simply ``await asyncio.sleep(delay)``
    per agent in order. Total startup is ~ the last wave's absolute time,
    not the sum (the old absolute yields were slept sequentially for a
    total of 212s at 50/10/2.0). Within a wave, agents stagger by a small
    jitter so they don't thundering-herd the server.
    wave_size: agents per wave
    wave_delay_seconds: delay between waves
    """
    prev = 0.0
    for i in range(count):
        wave = i // wave_size
        delay = wave * wave_delay_seconds + random.uniform(0, wave_delay_seconds * 0.3)
        # Baseline tracks the max so far, not the last value: intra-wave
        # jitter can decrease, and resetting to it would overshoot the
        # promised last-wave total.
        yield max(0.0, delay - prev)
        prev = max(prev, delay)


class ChurnManager:
    """Manages agent lifecycle: arrivals, deaths, wave startup."""

    def __init__(self, registry, arrivals_per_minute=2.0, mean_lifetime_episodes=100,
                 top_up_per_tick=1):
        self.registry = registry
        self.arrivals_per_minute = arrivals_per_minute
        self.mean_lifetime_episodes = mean_lifetime_episodes
        # Spawn-to-target top-ups per tick (#214): episode-driven deaths
        # scale with episode throughput while the Poisson trickle is
        # wall-clock, so a fixed trickle converges below cap on fast
        # machines. 1/tick recovers up to 60/min -- fast against ~2/min
        # deaths, gentle against wave startup, no thundering herd.
        self.top_up_per_tick = max(0, top_up_per_tick)
        self._next_arrival = time.time()
        self._lifetimes = {}  # agent_id -> episodes_remaining
        self._recent_goals = deque(maxlen=50)  # (id, goal) parent pool

    def tick(self, dt):
        """Advance the Poisson arrival clock, then top up toward cap.
        Returns newly arrived ids.

        Only arrivals are time-driven; deaths come from report_episode().
        (dt is accepted for API stability; the clock reads wall time.)
        """
        now = time.time()
        arrived = []

        # Check for arrivals (skipped while the registry is full -- the
        # Poisson clock still advances so pressure resumes on room).
        if now >= self._next_arrival:
            self._next_arrival = now + poisson_interval(self.arrivals_per_minute)
            if len(self.registry.alive_agents()) < self.registry.max_agents:
                try:
                    arrived.append(self._spawn_one())
                except RuntimeError:
                    pass  # filled between check and spawn; retry next tick

        # Spawn-to-target top-up: fill toward max_agents, rate-capped per
        # tick. Each spawn re-checks the cap and tolerates a full registry
        # like the Poisson path, so a mass-death tick refills gradually
        # instead of stampeding.
        for _ in range(self.top_up_per_tick):
            if len(self.registry.alive_agents()) >= self.registry.max_agents:
                break
            try:
                arrived.append(self._spawn_one())
            except RuntimeError:
                break  # filled between check and spawn; retry next tick

        # Prune lifetime rows for dead/gone agents (crashed tasks never
        # report episodes, so without this their rows grow forever).
        # NOTE: O(rows) registry lookups per tick; trivial at current
        # caps, but snapshot aliveness once outside the loop if
        # max_agents ever grows 10x.
        for aid in list(self._lifetimes):
            entry = self.registry.get(aid)
            if entry is None or not entry.alive:
                self._lifetimes.pop(aid, None)

        return arrived

    def report_episode(self, agent_id, event=None):
        """Charge one completed episode against an agent's lifetime.

        Returns True when the lifetime expired (entry marked dead).
        Unknown or already-dead ids are ignored."""
        if agent_id not in self._lifetimes:
            return False
        self._lifetimes[agent_id] -= 1
        if self._lifetimes[agent_id] > 0:
            return False
        self._lifetimes.pop(agent_id, None)
        entry = self.registry.get(agent_id)
        if entry:
            entry.alive = False
        return True

    def _sample_goal(self):
        """Fresh Dirichlet goal, or a mutation of a recent one (offspring).

        Returns (goal, parent_id): parent_id names the mutated agent, or
        None for fresh samples. The pool holds (agent_id, goal) pairs so
        parentage survives to the lineage record (#293, slice 4/6).
        """
        if self._recent_goals and random.random() < OFFSPRING_FRACTION:
            parent_id, parent_goal = random.choice(self._recent_goals)
            return mutate_goal(parent_goal), parent_id
        return sample_goal(), None

    def _spawn_one(self, max_attempts=100):
        """Spawn a single agent with a random lifetime and goal weights.

        Ids are minted collision-proof (#277): a wider random space
        (6 digits, not 3) plus a retry-on-collision loop against the
        live registry. Same-millisecond bursts used to share the time
        prefix and collide on randint(0, 999); register() then returned
        the EXISTING entry, so the spawn silently reset a live agent's
        lifetime row and the top-up fill came up short. Exhaustion
        raises RuntimeError, which both tick paths already tolerate."""
        for _ in range(max_attempts):
            agent_id = (f"agent_{int(time.time() * 1000) % 100000}"
                        f"_{random.randint(0, 999999):06d}")
            if self.registry.get(agent_id) is None:
                break
        else:
            raise RuntimeError("could not mint a unique agent id")
        agent_type = random.choice(["linear", "torch"])
        goal, parent_id = self._sample_goal()
        entry = self.registry.register(agent_id, agent_type, goal=goal,
                                       parent_id=parent_id)
        # Lineage files at spawn (#293): goal.json + parent now; the ckpt
        # copy lands on the next registry.save() once training has written
        # a checkpoint file.
        self.registry.save_lineage(agent_id)
        self._lifetimes[agent_id] = geometric_lifetime(self.mean_lifetime_episodes)
        self._recent_goals.append((agent_id, goal))
        return agent_id

    async def wave_fill(self, target_count, wave_size=10, wave_delay=5.0):
        """Fill up to target_count agents using wave startup.

        Async (awaitable) so it never blocks the event loop -- the old
        blocking time.sleep version is gone (see #48)."""
        current = len(self.registry.alive_agents())
        needed = max(0, target_count - current)
        for delay in wave_startup(needed, wave_size, wave_delay):
            await asyncio.sleep(delay)
            self._spawn_one()
        return needed
