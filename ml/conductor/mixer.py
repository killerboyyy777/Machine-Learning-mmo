"""Adaptive mixer: reallocates agents across dungeon floors.

Monitors per-floor performance and shifts agents toward more rewarding floors.
Uses a simple proportional controller: floors with higher average reward get
more agents, floors with lower reward lose agents.
"""
import random


class Mixer:
    """Adaptive floor reallocation for dungeon-based agent distribution."""

    def __init__(self, registry, floor_ids, min_per_floor=1, max_per_floor=10):
        self.registry = registry
        self.floor_ids = list(floor_ids)
        self.min_per_floor = min_per_floor
        self.max_per_floor = max_per_floor
        self._floor_rewards = {f: [] for f in floor_ids}  # recent rewards per floor
        self._floor_agents = {f: [] for f in floor_ids}   # agent_ids per floor

    def assign_initial(self, agent_ids):
        """Round-robin assign agents to floors."""
        for i, aid in enumerate(agent_ids):
            floor = self.floor_ids[i % len(self.floor_ids)]
            self._floor_agents[floor].append(aid)

    def record_reward(self, floor_id, agent_id, reward):
        """Record an episode reward for a specific floor."""
        if floor_id in self._floor_rewards:
            self._floor_rewards[floor_id].append(reward)
            # Keep only recent rewards
            if len(self._floor_rewards[floor_id]) > 100:
                self._floor_rewards[floor_id] = self._floor_rewards[floor_id][-100:]

    def rebalance(self):
        """Reallocate agents across floors based on performance.

        Returns list of (agent_id, old_floor, new_floor) moves.
        """
        moves = []
        floor_means = {}
        for f in self.floor_ids:
            rewards = self._floor_rewards[f]
            floor_means[f] = sum(rewards) / max(1, len(rewards))

        total_agents = sum(len(v) for v in self._floor_agents.values())
        if total_agents == 0:
            return moves

        # Proportional allocation
        total_mean = sum(floor_means.values()) or 1
        target = {}
        for f in self.floor_ids:
            prop = floor_means[f] / total_mean
            target[f] = max(self.min_per_floor,
                            min(self.max_per_floor, int(prop * total_agents)))

        # Move agents from overpopulated to underpopulated floors
        for f in self.floor_ids:
            while len(self._floor_agents[f]) > target[f]:
                agent_id = self._floor_agents[f].pop()
                # Find the floor with the most deficit
                dest = min(self.floor_ids,
                          key=lambda x: len(self._floor_agents[x]) - target.get(x, 0))
                self._floor_agents[dest].append(agent_id)
                moves.append((agent_id, f, dest))

        return moves

    def snapshot(self):
        return {
            f: {
                "agents": len(self._floor_agents[f]),
                "mean_reward": round(sum(self._floor_rewards[f]) / max(1, len(self._floor_rewards[f])), 4),
            }
            for f in self.floor_ids
        }
