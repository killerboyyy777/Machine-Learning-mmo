"""Population-Based Training: exploit (copy the best) + explore (mutate).

Sits on top of the conductor's registry: each member is a registered agent
with a hyperparameter dict and a tracked fitness. Periodically (see
PBTManager.step), the bottom exploit_frac of sufficiently-trained members
copies the top member's checkpoint weights, adopts its hyperparameters,
and perturbs the numeric ones -- the classic PBT loop. Winners are never
touched; exploit only fires when the winner beats the loser by min_delta,
so noise doesn't churn the population.

Checkpoint copy goes winner.checkpoint_path -> loser.checkpoint_path via
the registry (files may not exist yet for fresh members -- then only the
hyperparameters transfer, which is still a valid exploit step).
"""
import os
import random
import shutil


class PBTManager:
    """Exploit/explore loop over registry-backed population members."""

    def __init__(self, registry, metrics=None, exploit_frac=0.2,
                 min_episodes=10, min_delta=0.01, perturb=(0.8, 1.2),
                 mutate_keys=None):
        self.registry = registry
        self.metrics = metrics
        self.exploit_frac = exploit_frac
        self.min_episodes = min_episodes
        self.min_delta = min_delta
        self.perturb = perturb
        self.mutate_keys = mutate_keys  # None = all numeric hparams
        self._members = {}  # agent_id -> {"hparams": dict, "fitness": float, "episodes": int}

    def register(self, agent_id, hparams=None):
        """Enroll a registered agent in the population."""
        self._members[agent_id] = {
            "hparams": dict(hparams or {}),
            "fitness": float("-inf"),
            "episodes": 0,
        }

    def report(self, agent_id, fitness, episodes=1):
        """Record an evaluation result (fitness = higher is better)."""
        m = self._members.get(agent_id)
        if m is None:
            return
        m["fitness"] = float(fitness)
        m["episodes"] += episodes

    def mutate(self, hparams):
        """Perturb numeric hyperparameters by a uniform factor in range."""
        out = dict(hparams)
        lo, hi = self.perturb
        for k, v in out.items():
            if self.mutate_keys is not None and k not in self.mutate_keys:
                continue
            if isinstance(v, bool):
                continue
            if isinstance(v, float):
                out[k] = v * random.uniform(lo, hi)
            elif isinstance(v, int):
                out[k] = int(round(v * random.uniform(lo, hi)))
        return out

    def _copy_weights(self, winner_id, loser_id):
        """Copy the winner's checkpoint file onto the loser's. Returns True
        when a file actually transferred."""
        winner = self.registry.get(winner_id)
        loser = self.registry.get(loser_id)
        if not winner or not loser:
            return False
        src, dst = winner.checkpoint_path, loser.checkpoint_path
        if not src or not os.path.isfile(src):
            return False
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copyfile(src, dst)
            return True
        except OSError:
            return False

    def step(self):
        """One exploit/explore round. Returns the list of exploit ops:
        [{"loser": id, "winner": id, "hparams": {...}, "weights_copied": bool}]."""
        ranked = sorted(
            ((aid, m) for aid, m in self._members.items()
             if m["episodes"] >= self.min_episodes and m["fitness"] > float("-inf")),
            key=lambda t: t[1]["fitness"],
        )
        if len(ranked) < 2:
            return []
        n = min(max(1, int(len(ranked) * self.exploit_frac)), len(ranked) // 2)
        ops = []
        for i in range(n):
            loser_id = ranked[i][0]
            winner_id = ranked[-1 - i][0]
            loser, winner = ranked[i][1], ranked[-1 - i][1]
            if winner["fitness"] <= loser["fitness"] + self.min_delta:
                continue
            new_hparams = self.mutate(winner["hparams"])
            copied = self._copy_weights(winner_id, loser_id)
            loser["hparams"] = new_hparams
            loser["episodes"] = 0  # re-prove after adopting new genes
            op = {"loser": loser_id, "winner": winner_id,
                  "hparams": dict(new_hparams), "weights_copied": copied}
            ops.append(op)
        if ops and self.metrics is not None:
            self.metrics.log("pbt_exploit", ops=ops)
        return ops

    def snapshot(self):
        return {aid: {"fitness": m["fitness"], "episodes": m["episodes"],
                      "hparams": dict(m["hparams"])}
                for aid, m in self._members.items()}
