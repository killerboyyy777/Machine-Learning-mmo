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


def _plugin_policy_factory(name, base_config):
    """Build a restart-time policy rebuilder for a plugin slot.

    Returns factory(checkpoint_path, hparams) -> policy_fn: a FRESH
    plugin instance (never the shared slot instance, which serves every
    agent on the slot) loading checkpoint_path when the file exists,
    with hparams overlaid onto the slot config wherever they match the
    plugin schema (e.g. epsilon); unknown hparams are ignored. A missing
    checkpoint file keeps the slot's own checkpoint/fresh default.
    """

    def build(checkpoint_path, hparams):
        from ml.plugins import get, instantiate
        schema = get(name).config_schema()
        config = dict(base_config)
        for key, value in (hparams or {}).items():
            if key in schema:
                config[key] = value
        if checkpoint_path and Path(checkpoint_path).is_file():
            config["checkpoint"] = checkpoint_path
        return instantiate(name, **config).make_policy()

    return build


def _materialize_slot(slot, url):
    """Turn a slot spec into a runnable slot.

    Accepts either a plugin spec (``{"plugin": name, "config": {...},
    "env": {...}}``, see :func:`ml.plugins.parse_slot`) or a legacy raw
    runner (``{"env_factory": f, "policy_fn": p}``). Returns
    ``{"weight", "agent_type", "env_factory", "policy_fn", "step_timeout",
    "label"}`` plus ``policy_factory`` (plugin slots only, else None) and
    ``learn_hook`` (the shared plugin instance's learn, so the slot's
    agents learn into one policy; raw runners and scripted plugins get
    None via the base default). The plugin instance is built once and shared across the
    slot's agents (policies are stateless at act time)."""
    from .runners import make_env_factory
    weight = max(1, int(slot.get("weight", 1)))
    if "plugin" in slot:
        from ml.plugins import instantiate
        plugin = instantiate(slot["plugin"], **slot.get("config", {}))
        env_kwargs = {"url": url}
        env_kwargs.update(slot.get("env", {}))
        return {
            "weight": weight,
            "agent_type": slot.get("agent_type", plugin.agent_type),
            "label": slot["plugin"],
            "env_factory": make_env_factory(**env_kwargs),
            "policy_fn": plugin.make_policy(),
            # Fresh per-agent rebuilds on PBT restart (shared slot
            # instance must never reload: it serves the whole slot).
            "policy_factory": _plugin_policy_factory(
                slot["plugin"], slot.get("config", {})),
            "learn_hook": plugin.learn,
            "step_timeout": slot.get("step_timeout"),
        }
    return {
        "weight": weight,
        "agent_type": slot.get("agent_type", "custom"),
        "label": slot.get("label", "custom"),
        "env_factory": slot["env_factory"],
        "policy_fn": slot["policy_fn"],
        "policy_factory": None,  # raw runners have no checkpoint to reload
        "learn_hook": None,  # raw runners don't learn per step
        "step_timeout": slot.get("step_timeout"),
    }


class Conductor:
    """Top-level orchestrator for large-scale agent management."""

    def __init__(self, base_dir, max_agents=50, arrivals_per_minute=2.0,
                 mean_lifetime_episodes=100, floors=None, pbt=None, runner=None,
                 runners=None, url="ws://localhost:8765", resume=True,
                 reset="none"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.registry = Registry(str(self.base_dir / "registry"), max_agents)
        if reset != "none":
            # Reset ladder (#290, slice 6/6): wipe persisted state BEFORE
            # load. reset="all" deletes registry.json, so the load below
            # is a no-op and the run starts fresh.
            self.registry.reset_state(reset)
        if resume:
            # Resume-by-default (#293, slice 4/6): pick up the previous
            # run's population (entries, goals, parents) instead of
            # starting empty. Missing registry.json (first run) is a
            # no-op; pass resume=False for a guaranteed-fresh population.
            self.registry.load()
        self.mixer = Mixer(self.registry, floors or ["town_square", "graveyard", "d_10_f1"])
        self.metrics = MetricsLogger(str(self.base_dir / "metrics.jsonl"))
        self.churn = ChurnManager(self.registry, arrivals_per_minute, mean_lifetime_episodes)
        self.supervisor = Supervisor(self.registry, max_concurrent=max_agents,
                                     mixer=self.mixer, metrics=self.metrics,
                                     episode_hook=self.churn.report_episode)
        # Slots (#152): weighted round-robin over agent kinds. `runners`
        # is a list of slot specs (plugin or raw); legacy `runner` is one
        # slot. Empty = track-only mode (no supervised tasks).
        raw_slots = list(runners or [])
        if runner is not None:
            raw_slots.append(runner)
        self._slots = [_materialize_slot(s, url) for s in raw_slots]
        self._cycle = [i for i, s in enumerate(self._slots)
                       for _ in range(s["weight"])]
        self._cursor = 0
        # PBT is opt-in: pass a dict of PBTManager kwargs (e.g. {}) to
        # enable exploit/explore rounds, or None to run without a population.
        self.pbt = PBTManager(self.registry, self.metrics, **pbt) if pbt is not None else None

        self._running = False
        self._start_time = None

    def _next_slot(self):
        """Next slot in weighted round-robin order (None when slotless)."""
        if not self._cycle:
            return None
        slot = self._slots[self._cycle[self._cursor % len(self._cycle)]]
        self._cursor += 1
        return slot

    async def _maybe_start(self, agent_id):
        """Start a supervised task for a fresh agent.

        Returns True when a task is running afterwards. Never raises: a
        bad spawn is logged as spawn_error and skipped, so one raising
        factory can't end the whole run. Failed starts are marked dead
        (#230): without a task their episode-based lifetime never ages,
        so alive-without-task ghosts would inflate `alive` forever.
        """
        try:
            slot = self._next_slot()
            if slot is None:
                self.registry.mark_dead(agent_id)
                return False
            entry = self.registry.get(agent_id)
            if entry is not None:
                entry.agent_type = slot["agent_type"]
            # Log in as the stable character name (#291, slice 2/6), not
            # the incarnation id, so server-side scores/XP survive deaths.
            # The wrapped factory is what the supervisor stores in specs,
            # so PBT restarts keep the same character too. Falls back to
            # agent_id when the entry carries no character (pool full or
            # pre-#291 rows): identical to the old behavior.
            login = (entry.character if entry is not None else None) or agent_id
            base_factory = slot["env_factory"]
            if login != agent_id:
                env_factory = lambda aid, _f=base_factory, _n=login: _f(_n)
            else:
                env_factory = base_factory
            started = await self.supervisor.start_agent(
                agent_id,
                env_factory,
                slot["policy_fn"],
                step_timeout=slot.get("step_timeout"),
                policy_factory=slot.get("policy_factory"),
                learn_hook=slot.get("learn_hook"))
            if not started:
                self.registry.mark_dead(agent_id)
            return started
        except Exception as e:
            try:
                self.metrics.log("spawn_error", agent_id=agent_id,
                                 error=str(e)[-300:])
            except Exception:
                pass
            self.registry.mark_dead(agent_id)
            return False

    def report_fitness(self, agent_id, fitness, episodes=1, vector=None):
        """Feed an evaluation result into the PBT population (no-op when
        PBT is disabled). Agents must be enrolled first (see enroll_pbt).
        A per-step reward `vector` makes fitness goal-weighted (#288)."""
        if self.pbt is not None:
            self.pbt.report(agent_id, fitness, episodes, vector=vector)

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
            # Log + assign only for agents that actually started
            # (_maybe_start kills failed starts outright, #230).
            if entry and await self._maybe_start(agent_id):
                self.metrics.log_agent_spawn(agent_id, entry.agent_type, entry.branch)
                self.mixer.assign_one(agent_id)
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
                if entry and await self._maybe_start(aid):
                    self.metrics.log_agent_spawn(aid, entry.agent_type, entry.branch)
                    self.mixer.assign_one(aid)
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
                for op in ops:
                    # Reload the loser's task so the copied weights +
                    # hparams actually take effect at runtime (#228).
                    restarted = await self.supervisor.restart_agent(
                        op["loser"], hparams=op.get("hparams"))
                    try:
                        self.metrics.log("pbt_reload", agent_id=op["loser"],
                                         restarted=restarted)
                    except Exception:
                        pass
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
        snap = self.registry.snapshot()
        by_type = {}
        for a in snap["agents"]:
            cell = by_type.setdefault(a["agent_type"],
                                      {"alive": 0, "episodes": 0, "reward": 0.0})
            cell["episodes"] += a["episodes"]
            cell["reward"] += a.get("total_reward", a["mean_reward"] * a["episodes"])
            if a["alive"]:
                cell["alive"] += 1
        for cell in by_type.values():
            cell["mean_reward"] = round(cell["reward"] / max(1, cell["episodes"]), 4)
            del cell["reward"]
        return {
            "running": self._running,
            "uptime": time.time() - self._start_time if self._start_time else 0,
            "registry": snap,
            "by_type": by_type,
            "supervisor": self.supervisor.status(),
            "mixer": self.mixer.snapshot(),
            "pbt": self.pbt.snapshot() if self.pbt is not None else None,
        }
