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
from .pbt import PBTManager


class Conductor:
    """Top-level orchestrator for large-scale agent management."""

    def __init__(self, base_dir, max_agents=50, arrivals_per_minute=2.0,
                 mean_lifetime_episodes=100, floors=None, pbt=None, runner=None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.registry = Registry(str(self.base_dir / "registry"), max_agents)
        self.mixer = Mixer(self.registry, floors or ["town_square", "graveyard", "d_10_f1"])
        self.supervisor = Supervisor(self.registry, max_concurrent=max_agents,
                                     mixer=self.mixer)
        # Runner (#52/#60): {"env_factory": f, "policy_fn": p} (+ optional
        # "step_timeout"). When set, run() starts a supervised task per
        # spawned agent and reaps tasks of churn-killed agents -- without
        # it the conductor only tracks the population without running it.
        self._runner = runner
        self.churn = ChurnManager(self.registry, arrivals_per_minute, mean_lifetime_episodes)
        self.metrics = MetricsLogger(str(self.base_dir / "metrics.jsonl"))
        # PBT is opt-in: pass a dict of PBTManager kwargs (e.g. {}) to
        # enable exploit/explore rounds, or None to run without a population.
        self.pbt = PBTManager(self.registry, self.metrics, **pbt) if pbt is not None else None

        self._running = False
        self._start_time = None

    async def _maybe_start(self, agent_id):
        """Start a supervised task for a fresh agent when a runner is set."""
        if self._runner is None:
            return
        await self.supervisor.start_agent(
            agent_id,
            self._runner["env_factory"],
            self._runner["policy_fn"],
            step_timeout=self._runner.get("step_timeout"))

    def report_fitness(self, agent_id, fitness, episodes=1):
        """Feed an evaluation result into the PBT population (no-op when
        PBT is disabled). Agents must be enrolled first (see enroll_pbt)."""
        if self.pbt is not None:
            self.pbt.report(agent_id, fitness, episodes)

    def enroll_pbt(self, agent_id, hparams=None):
        """Enroll a registered agent in the PBT population."""
        if self.pbt is not None:
            self.pbt.register(agent_id, hparams)

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
                await self._maybe_start(agent_id)
            wave_count += 1
            if wave_count % wave_size == 0:
                alive = len(self.registry.alive_agents())
                print(f"[conductor] Wave complete: {alive}/{target} agents alive")
                self.metrics.log_wave(wave_count // wave_size, wave_size, alive)

        print(f"[conductor] Startup complete: {len(self.registry.alive_agents())} agents")

        # Main loop: churn + rebalance + PBT exploit/explore
        end_time = self._start_time + duration_seconds
        rebalance_interval = 60.0  # seconds between rebalance checks
        pbt_interval = 300.0  # seconds between PBT exploit/explore rounds
        last_rebalance = time.time()
        last_pbt = time.time()

        while self._running and time.time() < end_time:
            await asyncio.sleep(1.0)
            now = time.time()

            # Churn: spawn new agents, kill expired ones
            arrived = self.churn.tick(1.0)
            for aid in arrived:
                entry = self.registry.get(aid)
                if entry:
                    self.metrics.log_agent_spawn(aid, entry.agent_type, entry.branch)
                    await self._maybe_start(aid)
            await self.supervisor.reap()

            # Periodic rebalance
            if now - last_rebalance >= rebalance_interval:
                moves = self.mixer.rebalance()
                if moves:
                    self.metrics.log_rebalance(moves)
                last_rebalance = now

            # Periodic PBT exploit/explore (no-op when disabled)
            if self.pbt is not None and now - last_pbt >= pbt_interval:
                ops = self.pbt.step()
                if ops:
                    print(f"[conductor] PBT: {len(ops)} exploit(s) "
                          + ", ".join(f"{o['loser']}<-{o['winner']}" for o in ops))
                last_pbt = now

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
            "pbt": self.pbt.snapshot() if self.pbt is not None else None,
        }
