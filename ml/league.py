"""ML-side league bookkeeping for #625.

The arena ratings and bout core are server-authoritative (#652/#653): the
server keeps the Elo ladder and rates every finished bout. This module does
NOT build a second ladder that could disagree with that one. It keeps the
piece the server has no concept of -- who has played whom, and with what
result -- so a matchmaker can sample opponents weighted toward the ones a
policy loses to (PFSP-style prioritized fictitious self-play). Ratings used
in the champion metric are supplied by the caller from the server snapshot;
:func:`expected_score` only converts a rating gap into an expected rate.

Everything is pure or file-backed with no server dependency, so it can be
tested in isolation and stays inert until a conductor opts in.
"""

import json
import math
import os
import random

DEFAULT_PRIOR = 0.1
DEFAULT_ALPHA = 1.0
ELO_SCALE = 400.0

_WIN = frozenset(("win", "w", 1, 1.0, True))
_LOSS = frozenset(("loss", "lose", "l", 0, 0.0, False))


def _outcome(value):
    """Normalize a bout outcome to a 'wins' / 'losses' / 'draws' row key.

    Accepts the words and the 1/0/0.5 numbers used by arena history so a
    recorded bout can be fed straight in. Unknown values are a draw rather
    than a crash: a malformed row must not poison the sampling table.
    """
    if value in _WIN:
        return "wins"
    if value in _LOSS:
        return "losses"
    return "draws"


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def expected_score(rating, opponent):
    """Elo expected score of `rating` against `opponent` in [0, 1].

    The formula is the server's (#653) so a rating gap from the authoritative
    ladder can be read as an expected win rate without storing a local copy.
    """
    try:
        exponent = (float(opponent) - float(rating)) / ELO_SCALE
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(exponent):
        return 0.5
    return 1.0 / (1.0 + 10.0**exponent)


class OpponentBook:
    """Per-entrant counts of bouts against each opponent.

    Stored separately from arena.json: the server owns that file, and two
    writers would lose updates. This is a private sampling ledger.
    """

    def __init__(self, path=None):
        self.path = os.path.abspath(path) if path else None
        self._rows = {}
        if self.path and os.path.exists(self.path):
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            rows = data.get("opponents", data) if isinstance(data, dict) else {}
            if not isinstance(rows, dict):
                return
            clean = {}
            for entrant, opponents in rows.items():
                if not isinstance(opponents, dict):
                    continue
                clean[str(entrant)] = {
                    str(other): {
                        key: int(counts.get(key, 0))
                        for key in ("games", "wins", "losses", "draws")
                    }
                    for other, counts in opponents.items()
                    if isinstance(counts, dict)
                }
            self._rows = clean
        except (OSError, ValueError, TypeError):
            # A missing or corrupt ledger must never stop a run; start clean.
            self._rows = {}

    def save(self):
        """Atomic write, so a killed process leaves a readable ledger."""
        if not self.path:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_json(self.path, {"version": 1, "opponents": self._rows})

    def record(self, entrant, opponent, outcome):
        row = self._rows.setdefault(str(entrant), {}).setdefault(
            str(opponent), {"games": 0, "wins": 0, "losses": 0, "draws": 0}
        )
        row["games"] += 1
        row[_outcome(outcome)] += 1

    def stats(self, entrant, opponent):
        return dict(
            self._rows.get(str(entrant), {}).get(
                str(opponent), {"games": 0, "wins": 0, "losses": 0, "draws": 0}
            )
        )

    def entrants(self):
        return sorted(self._rows)

    def snapshot(self):
        return {
            str(entrant): {str(k): dict(v) for k, v in opponents.items()}
            for entrant, opponents in self._rows.items()
        }


class PFSPMatcher:
    """Sample opponents weighted toward the ones an entrant loses to.

    Weight for an opponent is `prior + alpha * loss_rate`, where loss_rate is
    the entrant's losses against that opponent over their games there. A new
    matchup has loss_rate 0, so every candidate starts at the same prior and
    sampling is uniform until results arrive. The prior is the exploration
    floor: a policy that beats everyone still meets fresh opponents, and a
    bad matchup is never sampled exclusively.
    """

    def __init__(self, book=None, prior=DEFAULT_PRIOR, alpha=DEFAULT_ALPHA):
        self.book = book if book is not None else OpponentBook()
        self.prior = max(0.0, float(prior))
        self.alpha = max(0.0, float(alpha))

    def weights(self, entrant, candidates):
        weights = []
        for opponent in candidates:
            stats = self.book.stats(entrant, opponent)
            games = stats["games"]
            loss_rate = stats["losses"] / games if games else 0.0
            weights.append(self.prior + self.alpha * loss_rate)
        return weights

    def sample(self, entrant, candidates, k=1, rng=None):
        """Sample `k` opponents; `rng` makes a run reproducible."""
        candidates = list(candidates)
        if not candidates or k <= 0:
            return []
        chooser = rng if rng is not None else random
        weights = self.weights(entrant, candidates)
        if not any(w > 0 for w in weights):
            # Degenerate prior=0 and no losses: fall back to uniform so the
            # matchmaker still produces a bout.
            weights = [1.0] * len(candidates)
        return chooser.choices(candidates, weights=weights, k=k)

    def record_outcome(self, entrant, opponent, outcome):
        self.book.record(entrant, opponent, outcome)

    def weighted_win_rate(self, entrant, candidates):
        """Win rate under the PFSP sampling distribution, in [0, 1].

        This is the second half of the #625 metric: the champion's win rate
        where the weights are the very ones used to pick its opponents.
        """
        candidates = list(candidates)
        weights = self.weights(entrant, candidates)
        total = sum(weights)
        if total <= 0:
            if not candidates:
                return 0.0
            weights = [1.0] * len(candidates)
            total = float(len(candidates))
        rate = 0.0
        for opponent, weight in zip(candidates, weights):
            stats = self.book.stats(entrant, opponent)
            games = stats["games"]
            win_rate = stats["wins"] / games if games else 0.0
            rate += weight * win_rate
        return rate / total

    def snapshot(self):
        return {
            "prior": self.prior,
            "alpha": self.alpha,
            "opponents": self.book.snapshot(),
        }


def _team_names(teams, key):
    """Read a team's names across the int/str keys arena history has used."""
    if not isinstance(teams, dict):
        return []
    names = teams.get(key) or teams.get(1 if key == "team1" else 2) or []
    if not names:
        names = teams.get("1" if key == "team1" else "2") or []
    return [str(name) for name in names]


def ingest_arena_history(book, rows):
    """Fold the server's authoritative `arena.recent` rows into a book.

    Each finished bout contributes one pairing record per cross-team pair,
    from BOTH perspectives: a win for the winner over the loser and a loss
    for the loser against the winner. PFSP samples from the entrant's own
    record, so the loser's losses must be in its own row. Bouts without a
    winner (0) are recorded as draws so they still count as encounters.
    Returns the number of bout rows consumed.
    """
    consumed = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        teams = row.get("teams") or {}
        one = _team_names(teams, "team1")
        two = _team_names(teams, "team2")
        if not one or not two:
            continue
        winner = row.get("winner")
        for left in one:
            for right in two:
                if winner == 1:
                    book.record(left, right, "win")
                    book.record(right, left, "loss")
                elif winner == 2:
                    book.record(right, left, "win")
                    book.record(left, right, "loss")
                else:
                    book.record(left, right, "draw")
                    book.record(right, left, "draw")
        consumed += 1
    return consumed


def paired_seed_summary(champion, baseline):
    """Compare two per-seed score sequences with paired variance control.

    Both sequences are aligned by seed index so the comparison is paired:
    each seed is its own control, which is how the plan keeps variance down
    on deterministic seeds. Scores are 1 (win), 0 (loss) or 0.5 (draw).
    """
    champion = [float(x) for x in champion]
    baseline = [float(x) for x in baseline]
    if len(champion) != len(baseline):
        raise ValueError("champion and baseline must have one score per seed")
    n = len(champion)
    if not n:
        return {
            "n": 0,
            "champion_win_rate": 0.0,
            "baseline_win_rate": 0.0,
            "mean_diff": 0.0,
            "std_diff": 0.0,
            "seeds_won": 0,
        }
    diffs = [c - b for c, b in zip(champion, baseline)]
    mean_diff = sum(diffs) / n
    var = sum((d - mean_diff) ** 2 for d in diffs) / n
    return {
        "n": n,
        "champion_win_rate": sum(champion) / n,
        "baseline_win_rate": sum(baseline) / n,
        "mean_diff": mean_diff,
        "std_diff": math.sqrt(var),
        "seeds_won": sum(1 for d in diffs if d > 0),
    }
