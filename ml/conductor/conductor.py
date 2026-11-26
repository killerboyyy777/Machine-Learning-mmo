"""Conductor orchestrator: runs the full agent lifecycle.

Stage 1: 50 agents, wave startup under 60s, fault isolation,
holdout-gated promotion, market-volume hour soak.
"""

import asyncio
import time
from pathlib import Path

from .churn import ChurnManager, wave_startup
from .metrics import MetricsLogger
from .mixer import Mixer
from .pbt import PBTManager
from .reload import CheckpointWatcher
from .registry import Registry
from .supervisor import Supervisor


def _plugin_policy_factory(name, base_config):
    """Build a restart-time rebuilder for a plugin slot.

    Returns factory(checkpoint_path, hparams) -> (policy_fn, learn_hook,
    reward_hook): all three come from a FRESH plugin instance (never the
    shared slot instance, which serves every agent on the slot). The
    bundle lets a restart swap the hooks with the policy, so a stateful
    plugin trains the weights it actually acts on. The instance loads
    checkpoint_path when the file exists, with hparams overlaid onto the
    slot config wherever they match the plugin schema (e.g. epsilon);
    unknown hparams are ignored. A missing checkpoint file keeps the
    slot's own checkpoint/fresh default.
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
        plugin = instantiate(name, **config)
        return (plugin.make_policy(), plugin.learn, plugin.reward)

    return build


def _materialize_slot(slot, url):
    """Turn a slot spec into a runnable slot.

    Accepts either a plugin spec (``{"plugin": name, "config": {...},
    "env": {...}}``, see :func:`ml.plugins.parse_slot`) or a legacy raw
    runner (``{"env_factory": f, "policy_fn": p}``). Returns
    ``{"weight", "agent_type", "env_factory", "policy_fn", "step_timeout",
    "policy_timeout", "label"}`` plus ``policy_factory`` (plugin slots
    only, else None) and
    ``learn_hook`` (the shared plugin instance's learn, so the slot's
    agents learn into one policy; raw runners and scripted plugins get
    None via the base default) and ``reward_hook`` (the plugin's reward
    function, #68; always present, abstaining by default). The plugin
    instance is built once and shared across the
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
                slot["plugin"], slot.get("config", {})
            ),
            "learn_hook": plugin.learn,
            # #68: a plugin may supply its own reward function. Attached
            # to the env by the supervisor, so it must not be a bound
            # method of a per-agent instance -- it is the slot's shared
            # plugin, matching learn_hook above.
            "reward_hook": plugin.reward,
            "step_timeout": slot.get("step_timeout"),
            "policy_timeout": slot.get("policy_timeout"),
        }
    return {
        "weight": weight,
        "agent_type": slot.get("agent_type", "custom"),
        "label": slot.get("label", "custom"),
        "env_factory": slot["env_factory"],
        "policy_fn": slot["policy_fn"],
        "policy_factory": None,  # raw runners have no checkpoint to reload
        "learn_hook": None,  # raw runners don't learn per step
        "reward_hook": None,  # raw runners supply their own env reward
        "step_timeout": slot.get("step_timeout"),
        "policy_timeout": slot.get("policy_timeout"),
    }


class Conductor:
    """Top-level orchestrator for large-scale agent management."""

    def __init__(
        self,
        base_dir,
        max_agents=50,
        arrivals_per_minute=2.0,
        mean_lifetime_episodes=100,
        floors=None,
        pbt=None,
        runner=None,
        runners=None,
        url="ws://localhost:8765",
        resume=True,
        reset="none",
        transcript_dir=None,
        telemetry=None,
        reload=None,
        structured_log=None,
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry = telemetry
        self.structured_log = structured_log
        self._watcher = CheckpointWatcher(reload, interval=5.0) if reload else None

        self.registry = Registry(str(self.base_dir / "registry"), max_agents)
        if reset != "none":
            # Reset ladder (#290, slice 6/6): wipe persisted state BEFORE
            # load. reset="all" deletes registry.json, so the load below
            # is a no-op and the run starts fresh.
            self.registry.reset_state(reset)
        self._pending_resume = []
        if resume:
            # Resume-by-default (#293, slice 4/6): pick up the previous
            # run's population (entries, goals, parents) instead of
            # starting empty. Missing registry.json (first run) is a
            # no-op; pass resume=False for a guaranteed-fresh population.
            self.registry.load()
            # Live entries have no task yet. run() restarts them before its
            # startup wave so they do not silently count against the cap
            # (#995).
            self._pending_resume = [a.agent_id for a in self.registry.alive_agents()]
        self.mixer = Mixer(
            self.registry, floors or ["town_square", "graveyard", "d_10_f1"]
        )
        self.metrics = MetricsLogger(str(self.base_dir / "metrics.jsonl"))
        self.churn = ChurnManager(
            self.registry, arrivals_per_minute, mean_lifetime_episodes
        )
        self.supervisor = Supervisor(
            self.registry,
            max_concurrent=max_agents,
            mixer=self.mixer,
            metrics=self.metrics,
            episode_hook=self.churn.report_episode,
            transcript_dir=transcript_dir,
            structured_log=structured_log,
        )
        # Slots (#152): weighted round-robin over agent kinds. `runners`
        # is a list of slot specs (plugin or raw); legacy `runner` is one
        # slot. Empty = track-only mode (no supervised tasks).
        raw_slots = list(runners or [])
        if runner is not None:
            raw_slots.append(runner)
        self._slots = [_materialize_slot(s, url) for s in raw_slots]
        self._cycle = [i for i, s in enumerate(self._slots) for _ in range(s["weight"])]
        self._cursor = 0
        # PBT is opt-in: pass a dict of PBTManager kwargs (e.g. {}) to
        # enable exploit/explore rounds, or None to run without a population.
        self.pbt = (
            PBTManager(self.registry, self.metrics, **pbt) if pbt is not None else None
        )

        self._running = False
        self._start_time = None

    def _next_slot(self):
        """Next slot in weighted round-robin order (None when slotless).

        Slots whose policy the supervisor retired are skipped (#176): a
        plugin that raised on every attempt is not handed another agent.
        When every slot is retired this returns None, and the caller's
        existing "no slot" path marks the agent dead instead of silently
        respawning it into the same failure.
        """
        if not self._cycle:
            return None
        for _ in range(len(self._cycle)):
            slot = self._slots[self._cycle[self._cursor % len(self._cycle)]]
            self._cursor += 1
            if self.supervisor.policy_disabled(slot["policy_fn"]):
                continue
            return slot
        return None

    async def _maybe_start(self, agent_id, episode_offset=0):
        """Start a supervised task for an agent.

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

                def env_factory(aid, _f=base_factory, _n=login):
                    return _f(_n)

            else:
                env_factory = base_factory
            started = await self.supervisor.start_agent(
                agent_id,
                env_factory,
                slot["policy_fn"],
                step_timeout=slot.get("step_timeout"),
                policy_timeout=slot.get("policy_timeout"),
                policy_factory=slot.get("policy_factory"),
                learn_hook=slot.get("learn_hook"),
                reward_hook=slot.get("reward_hook"),
                episode_offset=episode_offset,
                telemetry=self.telemetry,
            )
            if not started:
                self.registry.mark_dead(agent_id)
            return started
        except Exception as e:
            try:
                self.metrics.log("spawn_error", agent_id=agent_id, error=str(e)[-300:])
            except Exception:
                pass
            self.registry.mark_dead(agent_id)
            return False

    async def _resume_live_entries(self):
        """Restart tasks for entries restored by registry.load() (#995).

        A stop/save leaves entries alive; without a task they still occupy
        the cap and the startup wave then fails with "Registry full".
        Rehydrate each one with its goal, character, checkpoint and a fresh
        episode lifetime, then let run() spawn only the missing slots.
        """
        pending, self._pending_resume = self._pending_resume, []
        for agent_id in pending:
            entry = self.registry.get(agent_id)
            if entry is None or not entry.alive:
                continue
            self.churn.seed_lifetime(agent_id)
            if not await self._maybe_start(agent_id, episode_offset=entry.episodes):
                continue
            self.metrics.log_agent_spawn(agent_id, entry.agent_type, entry.branch)
            self.mixer.assign_one(agent_id)
            if not entry.checkpoint_path or not Path(entry.checkpoint_path).is_file():
                continue
            # The slot's plugin loads its own checkpoint; the agent's saved
            # weights live at entry.checkpoint_path and need a rebuild.
            if not await self.supervisor.restart_agent(agent_id):
                self.registry.mark_dead(agent_id)
                await self.supervisor.stop_agent(agent_id)
                self.mixer.remove_agent(agent_id)

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

    async def _reload_checkpoints(self):
        try:
            changed = self._watcher.poll()
        except Exception as e:
            self.metrics.log("checkpoint_reload_error", error=str(e)[-300:])
            return
        for path, digest in changed:
            for entry in self.registry.alive_agents():
                if entry.checkpoint_path != path:
                    continue
                # A watcher reload must carry PBT's current genes, or an
                # exploit's checkpoint copy undoes them (#849).
                hparams = (
                    self.pbt.hparams_for(entry.agent_id)
                    if self.pbt is not None
                    else None
                )
                try:
                    if hparams is not None:
                        await self.supervisor.restart_agent(
                            entry.agent_id, hparams=hparams
                        )
                    else:
                        await self.supervisor.restart_agent(entry.agent_id)
                    self.metrics.log(
                        "checkpoint_reload",
                        agent_id=entry.agent_id,
                        path=path,
                        sha256=digest,
                    )
                    if self.structured_log is not None:
                        self.structured_log.log(
                            "checkpoint_reload",
                            ids=[entry.agent_id],
                            path=path,
                            sha256=digest,
                        )
                except Exception as e:
                    self.metrics.log(
                        "checkpoint_reload_error",
                        agent_id=entry.agent_id,
                        path=path,
                        error=str(e)[-300:],
                    )

    async def run(self, duration_seconds=3600, wave_size=10, wave_delay=5.0):
        """Run the conductor for a fixed duration.

        Stage 1 defaults: 50 agents, 1-hour soak, waves of 10.
        """
        self._running = True
        self._start_time = time.time()
        target = self.registry.max_agents

        print(f"[conductor] Starting up to {target} agents in waves of {wave_size}...")
        if self.structured_log is not None:
            self.structured_log.log("startup", target=target, wave_size=wave_size)
        await self._resume_live_entries()
        needed = max(0, target - len(self.registry.alive_agents()))
        wave_count = 0
        for delay in wave_startup(needed, wave_size, wave_delay):
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
                if self.structured_log is not None:
                    self.structured_log.log(
                        "wave",
                        wave=wave_count // wave_size,
                        agents_added=wave_size,
                        total_alive=alive,
                    )

        print(
            f"[conductor] Startup complete: {len(self.registry.alive_agents())} agents"
        )

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

            if self._watcher is not None:
                await self._reload_checkpoints()

            if now - last_rebalance >= rebalance_interval:
                moves = self.mixer.rebalance()
                if moves:
                    self.metrics.log_rebalance(moves)
                    if self.structured_log is not None:
                        self.structured_log.log("rebalance", moves=moves)
                last_rebalance = now

            # Periodic PBT exploit/explore (no-op when disabled)
            if self.pbt is not None and now - last_pbt >= pbt_interval:
                ops = self.pbt.step()
                for op in ops:
                    # Reload the loser's task so the copied weights +
                    # hparams actually take effect at runtime (#228).
                    restarted = await self.supervisor.restart_agent(
                        op["loser"], hparams=op.get("hparams")
                    )
                    try:
                        self.metrics.log(
                            "pbt_reload", agent_id=op["loser"], restarted=restarted
                        )
                    except Exception:
                        pass
                if ops:
                    print(
                        f"[conductor] PBT: {len(ops)} exploit(s) "
                        + ", ".join(f"{o['loser']}<-{o['winner']}" for o in ops)
                    )
                    if self.structured_log is not None:
                        self.structured_log.log(
                            "promotion",
                            ids=[o["loser"] for o in ops],
                            ops=len(ops),
                        )
                last_pbt = now

            alive = len(self.registry.alive_agents())
            if alive < target * 0.8:
                print(f"[conductor] Low agent count: {alive}/{target}")

        self._running = False
        await self.supervisor.stop_all()
        self.registry.save()
        self.metrics.flush()
        elapsed = time.time() - self._start_time
        print(
            f"[conductor] Run complete: {elapsed:.0f}s, "
            f"{self.registry.snapshot()['alive']} agents alive"
        )
        if self.structured_log is not None:
            self.structured_log.log(
                "run_complete",
                scope="conductor",
                elapsed=round(elapsed, 3),
                alive=self.registry.snapshot()["alive"],
            )

    def stop(self):
        self._running = False

    def status(self):
        snap = self.registry.snapshot()
        by_type = {}
        for a in snap["agents"]:
            cell = by_type.setdefault(
                a["agent_type"], {"alive": 0, "episodes": 0, "reward": 0.0}
            )
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
