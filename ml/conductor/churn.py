"""Agent churn: Poisson arrivals, geometric lifetimes, wave startup.

Agents arrive according to a Poisson process (rate = arrivals_per_minute).
Each agent's lifetime is drawn from a geometric distribution (mean = mean_lifetime_episodes).
Wave startup batches arrivals into waves for faster initial fill.
"""
import asyncio
import random
import time


def poisson_interval(rate_per_minute):
    """Draw the next inter-arrival time (in seconds) for a Poisson process."""
    if rate_per_minute <= 0:
        return float("inf")
    return random.expovariate(rate_per_minute / 60.0)


def geometric_lifetime(mean_episodes):
    """Draw an agent lifetime (in episodes) from a geometric distribution."""
    return max(1, int(random.expovariate(1.0 / mean_episodes)))


def wave_startup(count, wave_size, wave_delay_seconds):
    """Generate arrival times for a wave-based startup.

    Yields (delay_seconds) for each agent, batched into waves.
    wave_size: agents per wave
    wave_delay_seconds: delay between waves
    """
    for i in range(count):
        wave = i // wave_size
        delay = wave * wave_delay_seconds + random.uniform(0, wave_delay_seconds * 0.3)
        yield delay


class ChurnManager:
    """Manages agent lifecycle: arrivals, deaths, wave startup."""

    def __init__(self, registry, arrivals_per_minute=2.0, mean_lifetime_episodes=100):
        self.registry = registry
        self.arrivals_per_minute = arrivals_per_minute
        self.mean_lifetime_episodes = mean_lifetime_episodes
        self._next_arrival = time.time()
        self._lifetimes = {}  # agent_id -> episodes_remaining

    def tick(self, dt):
        """Update churn state. Returns list of newly arrived agent_ids."""
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

        # Check for deaths (lifetime expired)
        dead = []
        for aid, remaining in list(self._lifetimes.items()):
            self._lifetimes[aid] = remaining - 1
            if self._lifetimes[aid] <= 0:
                dead.append(aid)

        for aid in dead:
            self._lifetimes.pop(aid, None)
            entry = self.registry.get(aid)
            if entry:
                entry.alive = False

        return arrived

    def _spawn_one(self):
        """Spawn a single agent with a random lifetime."""
        agent_id = f"agent_{int(time.time() * 1000) % 100000}_{random.randint(0, 999)}"
        agent_type = random.choice(["linear", "torch"])
        entry = self.registry.register(agent_id, agent_type)
        self._lifetimes[agent_id] = geometric_lifetime(self.mean_lifetime_episodes)
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
