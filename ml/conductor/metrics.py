"""JSONL metrics logger for conductor runs.

Writes one JSON object per line, suitable for offline analysis with
jq, pandas, or custom tools. Events include agent spawn/death,
episode completions, promotions, and rebalance moves.
"""
import json
import time
import os
import threading


class MetricsLogger:
    """Append-only JSONL metrics writer."""

    def __init__(self, path, buffer_size=50, flush_every=60.0):
        self.path = path
        self.buffer_size = buffer_size
        self.flush_every = flush_every
        self._buffer = []
        self._lock = threading.RLock()
        self._last_flush = time.time()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _write_line(self, event):
        event["_ts"] = time.time()
        line = json.dumps(event, default=str) + "\n"
        with open(self.path, "a") as f:
            f.write(line)

    def log(self, event_type, **kwargs):
        """Log a metric event (auto-flushes on size or every flush_every)."""
        event = {"event": event_type, **kwargs}
        with self._lock:
            self._buffer.append(event)
            if (len(self._buffer) >= self.buffer_size
                    or time.time() - self._last_flush >= self.flush_every):
                self.flush()

    def flush(self):
        with self._lock:
            for event in self._buffer:
                self._write_line(event)
            self._buffer.clear()
            self._last_flush = time.time()

    def log_agent_spawn(self, agent_id, agent_type, branch):
        self.log("agent_spawn", agent_id=agent_id, agent_type=agent_type, branch=branch)

    def log_agent_death(self, agent_id, episodes, total_reward):
        self.log("agent_death", agent_id=agent_id, episodes=episodes,
                 total_reward=round(total_reward, 4))

    def log_episode(self, agent_id, episode, reward, steps):
        self.log("episode", agent_id=agent_id, episode=episode,
                 reward=round(reward, 4), steps=steps)

    def log_promotion(self, agent_id, from_branch, to_branch):
        self.log("promotion", agent_id=agent_id,
                 from_branch=from_branch, to_branch=to_branch)

    def log_rebalance(self, moves):
        self.log("rebalance", moves=[{"agent": a, "from": f, "to": t} for a, f, t in moves])

    def log_wave(self, wave_num, agents_added, total_alive):
        self.log("wave", wave=wave_num, agents_added=agents_added,
                 total_alive=total_alive)
