"""Adaptive mixer: reallocates agents across dungeon floors.

Monitors per-floor performance and shifts agents toward more rewarding floors.
Uses a simple proportional controller: floors with higher average reward get
more agents, floors with lower reward lose agents.
"""


class Mixer:
    """Adaptive floor reallocation for dungeon-based agent distribution."""

    def __init__(self, registry, floor_ids, min_per_floor=1, max_per_floor=10):
        self.registry = registry
        self.floor_ids = list(floor_ids)
        self.min_per_floor = min_per_floor
        self.max_per_floor = max_per_floor
        self._floor_rewards = {f: [] for f in floor_ids}  # recent rewards per floor
        self._floor_agents = {f: [] for f in floor_ids}  # agent_ids per floor
        self._cursor = 0  # round-robin cursor shared by assign calls

    def assign_one(self, agent_id):
        """Assign one agent, continuing round-robin across calls (so single
        arrivals spread over floors instead of piling on floor zero)."""
        floor = self.floor_ids[self._cursor % len(self.floor_ids)]
        self._cursor += 1
        self._floor_agents[floor].append(agent_id)
        return floor

    def assign_initial(self, agent_ids):
        """Round-robin assign agents to floors."""
        for aid in agent_ids:
            self.assign_one(aid)

    def remove_agent(self, agent_id):
        """Forget an agent whose task ended (crash, cancel, churn death).

        Without this, dead ids pile up in _floor_agents: rebalance() keeps
        "moving" corpses and snapshot counts inflate forever. Idempotent.
        """
        for agents in self._floor_agents.values():
            if agent_id in agents:
                agents.remove(agent_id)

    def record_reward(self, floor_id, agent_id, reward):
        """Record an episode reward for a specific floor."""
        if floor_id in self._floor_rewards:
            self._floor_rewards[floor_id].append(reward)
            if len(self._floor_rewards[floor_id]) > 100:
                self._floor_rewards[floor_id] = self._floor_rewards[floor_id][-100:]

    def rebalance(self):
        """Reallocate agents across floors based on performance.

        Returns list of (agent_id, old_floor, new_floor) moves.

        Donors give down to (never below) their target, receivers take up
        to (never above) theirs, paired off in a single bounded pass: every
        step moves >= 1 agent and shrinks the remaining surplus, so this
        always terminates. (The old move-while-over loop could pop an agent
        onto its own floor when it held the smallest surplus and spin
        forever, starving the conductor's event loop.)"""
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
            target[f] = max(
                self.min_per_floor, min(self.max_per_floor, int(prop * total_agents))
            )

        counts = {f: len(self._floor_agents[f]) for f in self.floor_ids}
        donors = [
            [f, counts[f] - target[f]] for f in self.floor_ids if counts[f] > target[f]
        ]
        receivers = [
            [f, target[f] - counts[f]] for f in self.floor_ids if counts[f] < target[f]
        ]
        di, ri = 0, 0
        while di < len(donors) and ri < len(receivers):
            df, ds = donors[di]
            rf, rd = receivers[ri]
            n = min(ds, rd)
            for _ in range(n):
                agent_id = self._floor_agents[df].pop()
                self._floor_agents[rf].append(agent_id)
                moves.append((agent_id, df, rf))
            donors[di][1] -= n
            receivers[ri][1] -= n
            if donors[di][1] <= 0:
                di += 1
            if receivers[ri][1] <= 0:
                ri += 1
        return moves

    def snapshot(self):
        return {
            f: {
                "agents": len(self._floor_agents[f]),
                "mean_reward": round(
                    sum(self._floor_rewards[f]) / max(1, len(self._floor_rewards[f])), 4
                ),
            }
            for f in self.floor_ids
        }
