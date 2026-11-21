"""Red-team exploit hunter drop-in plugin (#624).

Watches reward signals for value beyond honest play (an abnormal gold or
profit delta) and emits bounded, serializable findings. Each finding carries
a compact repro of the recent transitions, in the same row shape the Phase 1
transcript writer uses, so a researcher can replay it without this server.

This is a drop-in for the v0.16 plugin runtime: ``--slot redteam:...`` loads
it like any other plugin, no conductor change. It observes and reports; it
abstains from the reward (returns None) and places no orders, so it cannot
change the world it is auditing.

Note: the slot shares one plugin instance across all its agents. A finding's
agent_id is therefore best-effort (the last agent that acted); the
authoritative per-agent repro is the transcript produced by the runtime,
which ml.repro.compact_repro folds by agent_id and incarnation.
"""

import random

from ml.plugins import AgentPlugin, register
from ml.repro import SCHEMA_VERSION, _finite, compact_repro


@register
class RedTeam(AgentPlugin):
    """Explore a world and report suspicious reward signals."""

    name = "redteam"
    agent_type = "custom"

    @classmethod
    def config_schema(cls):
        return {
            "epsilon": {
                "type": float,
                "default": 0.25,
                "help": "chance to pick a random valid action",
            },
            "gold_spike": {
                "type": float,
                "default": 50.0,
                "help": "gold_delta at or above this is a finding",
            },
            "profit_spike": {
                "type": float,
                "default": 40.0,
                "help": "profit at or above this is a finding",
            },
            "buffer": {
                "type": int,
                "default": 8,
                "help": "recent transitions kept for a repro",
            },
            "max_findings": {
                "type": int,
                "default": 20,
                "help": "bounded finding buffer",
            },
            "seed": {
                "type": int,
                "default": 0,
                "help": "exploration RNG seed (reproducible)",
            },
        }

    def __init__(self, **config):
        super().__init__(**config)
        self._limit = max(1, int(self.buffer if self.buffer is not None else 8))
        self._max_findings = max(
            1, int(self.max_findings if self.max_findings is not None else 20)
        )
        self._gold_spike = _finite(self.gold_spike)
        if self._gold_spike is None:
            self._gold_spike = 50.0
        self._profit_spike = _finite(self.profit_spike)
        if self._profit_spike is None:
            self._profit_spike = 40.0
        self._rng = random.Random(int(self.seed or 0))
        self._buffer = []
        self._findings = []
        self._agent_id = None
        self._episode = 1
        self._step = 0

    def act(self, obs, agent_id, mask=None):
        """Pick a valid action, exploring at random with probability epsilon."""
        self._agent_id = agent_id
        valid = [i for i, allowed in enumerate(mask) if allowed] if mask else [0]
        if not valid:
            return 0
        if self._rng.random() < float(self.epsilon or 0.0):
            return valid[self._rng.randrange(len(valid))]
        return valid[0]

    def reward(self, signals):
        """Flag abnormal value, but never change the env's reward."""
        if not isinstance(signals, dict):
            return None
        kind = None
        value = None
        gold = _finite(signals.get("gold_delta"))
        profit = _finite(signals.get("profit"))
        if gold is not None and gold >= self._gold_spike:
            kind, value = "gold_spike", gold
        elif profit is not None and profit >= self._profit_spike:
            kind, value = "profit_spike", profit
        if kind is None:
            return None
        finding = {
            "schema_version": SCHEMA_VERSION,
            "defect_class": kind,
            "agent_id": self._agent_id,
            "episode": self._episode,
            "value": value,
            "trigger": {
                key: signals.get(key)
                for key in ("gold_delta", "profit", "score_delta", "deaths")
            },
            "repro": compact_repro(list(self._buffer), defect_class=kind),
        }
        if len(self._findings) < self._max_findings:
            self._findings.append(finding)
        return None

    def learn(self, prev_obs, action, reward, next_obs, done):
        """Keep a bounded transition buffer for the next finding's repro."""
        self._buffer.append(
            {
                "agent_id": self._agent_id,
                "episode": self._episode,
                "step": self._step,
                "prev_obs": prev_obs,
                "action": action,
                "reward": reward,
                "obs": next_obs,
                "done": bool(done),
            }
        )
        if len(self._buffer) > self._limit:
            self._buffer.pop(0)
        self._step += 1
        if done:
            self._episode += 1
            self._step = 0
        return None

    def findings(self):
        """A copy of the bounded finding buffer."""
        return [dict(item) for item in self._findings]

    def drain_findings(self):
        """Return and clear the finding buffer."""
        out = self.findings()
        self._findings.clear()
        return out
