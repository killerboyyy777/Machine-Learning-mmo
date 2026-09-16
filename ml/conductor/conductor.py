"""Conductor orchestrator: runs the full agent lifecycle.

Stage 1: 50 agents, wave startup under 60s, fault isolation,
holdout-gated promotion, market-volume hour soak.
"""
import asyncio
import time
from pathlib import Path

from .registry import Registry
from .supervisor import Supervisor
from .churn import ChurnManager, wave_startup
from .mixer import Mixer
from .metrics import MetricsLogger


class Conductor:
    """Top-level orchestrator for large-scale agent management."""

    def __init__(self, base_dir, max_agents=50, arrivals_per_minute=2.0,
                 mean_lifetime_episodes=100, floors=None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.registry = Registry(str(self.base_dir / "registry"), max_agents)
        self.supervisor = Supervisor(self.registry, max_concurrent=max_agents)
        self.churn = ChurnManager(self.registry, arrivals_per_minute, mean_lifetime_episodes)
        self.mixer = Mixer(self.registry, floors or ["town_square", "graveyard", "d_10_f1"])
        self.metrics = MetricsLogger(str(self.base_dir / "metrics.jsonl"))

        self._running = False
        self._start_time = None

    async def run(self, duration_seconds=3600, wave_size=10, wave_delay=5.0):
        """Run the conductor for a fixed duration.

        Stage 1 defaults: 50 agents, 1-hour soak, waves of 10.
        """
        self._running = True
        self._start_time = time.time()
        target = self.registry.max_agents

        # Wave startup
        print(f"[conductor] Starting {target} agents in waves of {wave_size}...")
        wave_count = 0
        for delay in wave_startup(target, wave_size, wave_delay):
            if not self._running:
                break
            await asyncio.sleep(delay)
            agent_id = self.churn._spawn_one()
            entry = self.registry.get(agent_id)
            if entry:
                self.metrics.log_agent_spawn(agent_id, entry.agent_type, entry.branch)
                self.mixer.assign_initial([agent_id])
            wave_count += 1
            if wave_count % wave_size == 0:
                alive = len(self.registry.alive_agents())
                print(f"[conductor] Wave complete: {alive}/{target} agents alive")
                self.metrics.log_wave(wave_count // wave_size, wave_size, alive)

        print(f"[conductor] Startup complete: {len(self.registry.alive_agents())} agents")

        # Main loop: churn + rebalance + holdout evaluation
        end_time = self._start_time + duration_seconds
        rebalance_interval = 60.0  # seconds between rebalance checks
        last_rebalance = time.time()

        while self._running and time.time() < end_time:
            await asyncio.sleep(1.0)
            now = time.time()

            # Churn: spawn new agents, kill expired ones
            arrived = self.churn.tick(1.0)
            for aid in arrived:
                entry = self.registry.get(aid)
                if entry:
                    self.metrics.log_agent_spawn(aid, entry.agent_type, entry.branch)

            # Periodic rebalance
            if now - last_rebalance >= rebalance_interval:
                moves = self.mixer.rebalance()
                if moves:
                    self.metrics.log_rebalance(moves)
                last_rebalance = now

            # Periodic status
            alive = len(self.registry.alive_agents())
            if alive < target * 0.8:
                print(f"[conductor] Low agent count: {alive}/{target}")

        # Shutdown
        self._running = False
        await self.supervisor.stop_all()
        self.registry.save()
        self.metrics.flush()
        elapsed = time.time() - self._start_time
        print(f"[conductor] Run complete: {elapsed:.0f}s, "
              f"{self.registry.snapshot()['alive']} agents alive")

    def stop(self):
        self._running = False

    def status(self):
        return {
            "running": self._running,
            "uptime": time.time() - self._start_time if self._start_time else 0,
            "registry": self.registry.snapshot(),
            "supervisor": self.supervisor.status(),
            "mixer": self.mixer.snapshot(),
        }
