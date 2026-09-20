"""Agent registry: tracks linear/torch agents with stable/experimental branches.

Each agent type (linear, torch) has two branches:
  - stable: the last known-good checkpoint (used for production runs)
  - experimental: the current training branch (evaluated via holdouts)

Holdout episodes alternate between branches to score which is better.
When experimental outperforms stable over a window, it gets promoted.
"""
import os
import json
import shutil
import time
import threading
from pathlib import Path


# Reset ladder for the resume CLI (#290, slice 6/6): increasing wipe
# scope over the registry tree, applied before load. Modes after `none`
# are cumulative: lineage forgets ancestry, cell additionally forgets
# learned weights (agents restart fresh), all wipes the whole tree.
# Metrics live outside the registry tree; the caller clears them on all.
RESET_MODES = ("none", "lineage", "cell", "all")


class AgentEntry:
    """One agent in the registry."""

    __slots__ = ("agent_id", "agent_type", "branch", "checkpoint_path",
                 "created_at", "episodes", "total_reward", "last_active",
                 "alive", "goal", "character", "parent_id")

    def __init__(self, agent_id, agent_type, branch, checkpoint_path, goal=None,
                 character=None, parent_id=None):
        self.agent_id = agent_id
        self.agent_type = agent_type  # "linear" or "torch"
        self.branch = branch          # "stable" or "experimental"
        self.checkpoint_path = checkpoint_path
        # Inheritable goal weights (#162 Phase 1): {w_axis: float} simplex,
        # sampled/mutated at spawn, persisted below, logged per episode.
        self.goal = dict(goal) if goal else None
        # Stable role name (#291, slice 2/6): the server-side character
        # this incarnation plays as. Incarnation ids stay unique per
        # spawn (collision-proof since #277); the character name is what
        # persists across deaths so server scores/XP keep continuity.
        self.character = character
        # Lineage parent (#293, slice 4/6): agent_id this entry mutated from,
        # None for fresh Dirichlet samples. Persisted per lineage on disk.
        self.parent_id = parent_id
        self.created_at = time.time()
        self.episodes = 0
        self.total_reward = 0.0
        self.last_active = time.time()
        self.alive = True

    @property
    def mean_reward(self):
        return self.total_reward / max(1, self.episodes)

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "branch": self.branch,
            "checkpoint": self.checkpoint_path,
            "episodes": self.episodes,
            "mean_reward": round(self.mean_reward, 4),
            # Full-precision accumulator: load() prefers this over
            # reconstructing from the rounded mean (see #46).
            "total_reward": self.total_reward,
            "alive": self.alive,
            "goal": dict(self.goal) if self.goal else None,
            "character": self.character,
            "parent_id": self.parent_id,
        }


class Registry:
    """Central registry for all conductor-managed agents."""

    def __init__(self, base_dir, max_agents=50):
        self.base_dir = Path(base_dir)
        self.max_agents = max_agents
        self._agents = {}  # agent_id -> AgentEntry
        self._lock = threading.RLock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def register(self, agent_id, agent_type, branch="experimental", checkpoint=None,
                 goal=None, character=None, parent_id=None):
        """Register a new agent. Returns the AgentEntry."""
        with self._lock:
            if agent_id in self._agents:
                return self._agents[agent_id]
            # Cap counts LIVE agents only: churn-killed entries stay in the
            # dict as history, so counting corpses would wedge long runs
            # (no arrivals ever again once max_agents have died).
            alive = sum(1 for a in self._agents.values() if a.alive)
            if alive >= self.max_agents:
                raise RuntimeError(f"Registry full ({self.max_agents} agents)")
            cp_dir = self.base_dir / agent_id
            cp_dir.mkdir(parents=True, exist_ok=True)
            cp = checkpoint or str(cp_dir / "checkpoint.pt")
            entry = AgentEntry(agent_id, agent_type, branch, cp, goal=goal,
                               character=character, parent_id=parent_id)
            self._agents[agent_id] = entry
            return entry

    def alloc_character(self):
        """Claim the smallest free stable role name (role_0, role_1, ...).

        A name is free when no ALIVE entry holds it; dead entries keep
        their history rows but release the name, so the next incarnation
        respawns as the same server-side character (#291). Returns None
        when the pool (sized by max_agents) is full -- callers fall back
        to the incarnation id as the login name."""
        with self._lock:
            taken = {a.character for a in self._agents.values()
                     if a.alive and a.character}
            for i in range(self.max_agents):
                name = f"role_{i}"
                if name not in taken:
                    return name
            return None

    def get(self, agent_id):
        with self._lock:
            return self._agents.get(agent_id)

    def alive_agents(self, agent_type=None, branch=None):
        with self._lock:
            agents = [a for a in self._agents.values() if a.alive]
            if agent_type:
                agents = [a for a in agents if a.agent_type == agent_type]
            if branch:
                agents = [a for a in agents if a.branch == branch]
            return list(agents)

    def record_episode(self, agent_id, reward):
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry:
                entry.episodes += 1
                entry.total_reward += reward
                entry.last_active = time.time()

    def promote(self, agent_id):
        """Promote experimental -> stable."""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry and entry.branch == "experimental":
                entry.branch = "stable"
                return True
            return False

    def demote(self, agent_id):
        """Demote stable -> experimental."""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry and entry.branch == "stable":
                entry.branch = "experimental"
                return True
            return False

    def remove(self, agent_id):
        with self._lock:
            entry = self._agents.pop(agent_id, None)
            if entry:
                entry.alive = False
                return entry
            return None

    def mark_dead(self, agent_id):
        """Kill an entry that will never run (failed starts, #230)."""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry:
                entry.alive = False
            return entry

    def snapshot(self):
        """Return a JSON-serializable snapshot of the registry."""
        with self._lock:
            return {
                "total": len(self._agents),
                "alive": sum(1 for a in self._agents.values() if a.alive),
                "agents": [a.to_dict() for a in self._agents.values()],
            }

    def save(self, path=None):
        path = path or str(self.base_dir / "registry.json")
        with self._lock:
            data = self.snapshot()
        # Atomic tmp+replace (#293): a crash mid-write must never leave a
        # truncated registry behind. Same pattern as the agent checkpoints.
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        with self._lock:
            for aid in self._agents:
                self.save_lineage(aid)
            self.prune_lineages()

    def lineage_dir(self, agent_id):
        """On-disk lineage record for one agent."""
        return self.base_dir / "lineages" / agent_id

    def save_lineage(self, agent_id):
        """Persist one agent's lineage: goal weights, parent link, and a
        copy of the current checkpoint file (when one exists yet -- fresh
        spawns have none until their first save).

        Overwrites in place, so each lineage holds the LATEST snapshot
        only: the disk guard against unbounded ckpt growth. Returns True
        on success, False for unknown ids."""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry is None:
                return False
            goal = dict(entry.goal) if entry.goal else {}
            parent_id = entry.parent_id
            checkpoint_path = entry.checkpoint_path
        d = self.lineage_dir(agent_id)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "goal.json", "w") as f:
            json.dump(goal, f, indent=2)
        with open(d / "parent_id", "w") as f:
            f.write(parent_id or "")
        if checkpoint_path and os.path.isfile(checkpoint_path):
            shutil.copyfile(checkpoint_path, d / "ckpt")
        return True

    def load_lineage(self, agent_id):
        """Read back a lineage record. Returns None for unknown ids."""
        d = self.lineage_dir(agent_id)
        if not d.is_dir():
            return None
        try:
            with open(d / "goal.json") as f:
                goal = json.load(f)
        except (OSError, ValueError):
            goal = {}
        try:
            with open(d / "parent_id") as f:
                parent_id = f.read().strip() or None
        except OSError:
            parent_id = None
        ckpt = d / "ckpt"
        return {"goal": goal, "parent_id": parent_id,
                "ckpt": str(ckpt) if ckpt.is_file() else None}

    def prune_lineages(self):
        """Drop lineage dirs for ids no longer in the registry (removed
        entries must not pin disk forever). Returns the pruned count."""
        base = self.base_dir / "lineages"
        if not base.is_dir():
            return 0
        pruned = 0
        with self._lock:
            known = set(self._agents)
        for child in base.iterdir():
            if child.name not in known:
                shutil.rmtree(child, ignore_errors=True)
                pruned += 1
        return pruned

    def reset_state(self, mode="none"):
        """Wipe persisted state per the reset ladder. Returns the list of
        removed paths (for logging). Unknown modes fail fast."""
        if mode not in RESET_MODES:
            raise ValueError(
                f"unknown reset mode {mode!r} (want one of {RESET_MODES})")
        removed = []
        with self._lock:
            if mode == "none":
                return removed
            if mode == "all":
                shutil.rmtree(self.base_dir, ignore_errors=True)
                removed.append(str(self.base_dir))
                self._agents.clear()
                return removed
            lin = self.base_dir / "lineages"
            if lin.is_dir():
                shutil.rmtree(lin, ignore_errors=True)
                removed.append(str(lin))
            if mode == "cell":
                for entry in self._agents.values():
                    cp = entry.checkpoint_path
                    if not cp or not os.path.isfile(cp):
                        continue
                    try:
                        Path(cp).relative_to(self.base_dir)
                    except ValueError:
                        continue  # foreign path: not ours to wipe
                    os.remove(cp)
                    removed.append(cp)
        return removed

    def load(self, path=None):
        path = path or str(self.base_dir / "registry.json")
        if not os.path.exists(path):
            return
        with open(path) as f:
            data = json.load(f)
        with self._lock:
            for a in data.get("agents", []):
                entry = AgentEntry(
                    a["agent_id"], a["agent_type"],
                    a.get("branch", "experimental"),
                    a.get("checkpoint", ""),
                    goal=a.get("goal"),
                    character=a.get("character"),
                    parent_id=a.get("parent_id"),
                )
                entry.episodes = a.get("episodes", 0)
                if "total_reward" in a:
                    entry.total_reward = a["total_reward"]
                else:
                    # Pre-#46 snapshots only stored the rounded mean.
                    entry.total_reward = a.get("mean_reward", 0) * entry.episodes
                entry.alive = a.get("alive", True)
                self._agents[entry.agent_id] = entry
