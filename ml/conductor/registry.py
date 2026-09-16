"""Agent registry: tracks linear/torch agents with stable/experimental branches.

Each agent type (linear, torch) has two branches:
  - stable: the last known-good checkpoint (used for production runs)
  - experimental: the current training branch (evaluated via holdouts)

Holdout episodes alternate between branches to score which is better.
When experimental outperforms stable over a window, it gets promoted.
"""
import os
import json
import time
import threading
from pathlib import Path


class AgentEntry:
    """One agent in the registry."""

    __slots__ = ("agent_id", "agent_type", "branch", "checkpoint_path",
                 "created_at", "episodes", "total_reward", "last_active",
                 "alive")

    def __init__(self, agent_id, agent_type, branch, checkpoint_path):
        self.agent_id = agent_id
        self.agent_type = agent_type  # "linear" or "torch"
        self.branch = branch          # "stable" or "experimental"
        self.checkpoint_path = checkpoint_path
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
            "alive": self.alive,
        }


class Registry:
    """Central registry for all conductor-managed agents."""

    def __init__(self, base_dir, max_agents=50):
        self.base_dir = Path(base_dir)
        self.max_agents = max_agents
        self._agents = {}  # agent_id -> AgentEntry
        self._lock = threading.RLock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def register(self, agent_id, agent_type, branch="experimental", checkpoint=None):
        """Register a new agent. Returns the AgentEntry."""
        with self._lock:
            if agent_id in self._agents:
                return self._agents[agent_id]
            if len(self._agents) >= self.max_agents:
                raise RuntimeError(f"Registry full ({self.max_agents} agents)")
            cp_dir = self.base_dir / agent_id
            cp_dir.mkdir(parents=True, exist_ok=True)
            cp = checkpoint or str(cp_dir / "checkpoint.pt")
            entry = AgentEntry(agent_id, agent_type, branch, cp)
            self._agents[agent_id] = entry
            return entry

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
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

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
                )
                entry.episodes = a.get("episodes", 0)
                entry.total_reward = a.get("mean_reward", 0) * entry.episodes
                entry.alive = a.get("alive", True)
                self._agents[entry.agent_id] = entry
