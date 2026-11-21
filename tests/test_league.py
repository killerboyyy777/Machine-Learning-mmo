"""Tests for the ML-side league bookkeeping (#625).

Standalone script: sys.path setup, plain asserts, one *_OK marker per area.
No server is involved -- the whole point of ml/league.py is that the PFSP
sampling and the fixed-seed metric can be checked in isolation.
"""

import json
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ml.league import (  # noqa: E402
    OpponentBook,
    PFSPMatcher,
    expected_score,
    ingest_arena_history,
    paired_seed_summary,
)


def test_book_records_and_round_trips():
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "league", "book.json")
        book = OpponentBook(path)
        book.record("champ", "rival_a", "win")
        book.record("champ", "rival_a", "loss")
        book.record("champ", "rival_a", 0.5)
        book.record("champ", "rival_b", 1)
        assert book.stats("champ", "rival_a") == {
            "games": 3,
            "wins": 1,
            "losses": 1,
            "draws": 1,
        }
        assert book.stats("champ", "rival_b")["wins"] == 1
        assert book.stats("champ", "never")["games"] == 0
        assert book.entrants() == ["champ"]
        book.save()
        assert os.path.exists(path)
        on_disk = json.load(open(path, encoding="utf-8"))
        assert on_disk["version"] == 1

        reloaded = OpponentBook(path)
        assert reloaded.stats("champ", "rival_a")["losses"] == 1
        assert reloaded.snapshot() == book.snapshot()
    print("LEAGUE_BOOK_OK")


def test_book_survives_corrupt_file():
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "book.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        book = OpponentBook(path)
        assert book.snapshot() == {}
        book.record("a", "b", "win")
        assert book.stats("a", "b")["games"] == 1
    print("LEAGUE_CORRUPT_OK")


def test_pfsp_samples_toward_losses_with_a_floor():
    book = OpponentBook()
    matcher = PFSPMatcher(book, prior=0.1, alpha=1.0)
    # entrant loses every game to nemesis, wins every game to pushover.
    for _ in range(10):
        matcher.record_outcome("e", "nemesis", "loss")
        matcher.record_outcome("e", "pushover", "win")
    weights = matcher.weights("e", ["nemesis", "pushover", "unseen"])
    assert abs(weights[0] - 1.1) < 1e-9
    assert abs(weights[1] - 0.1) < 1e-9
    assert abs(weights[2] - 0.1) < 1e-9
    # The unseen opponent still has the prior floor, so it can be sampled.
    draws = matcher.sample(
        "e", ["nemesis", "pushover", "unseen"], 3000, random.Random(7)
    )
    counts = {name: draws.count(name) for name in ("nemesis", "pushover", "unseen")}
    assert counts["nemesis"] > counts["pushover"]
    assert counts["unseen"] > 0
    # Same seed reproduces the same matchups (variance control).
    again = matcher.sample(
        "e", ["nemesis", "pushover", "unseen"], 3000, random.Random(7)
    )
    assert again == draws
    print("PFSP_SAMPLING_OK")


def test_pfsp_weighted_win_rate():
    book = OpponentBook()
    matcher = PFSPMatcher(book, prior=0.0, alpha=1.0)
    matcher.record_outcome("e", "nemesis", "win")
    matcher.record_outcome("e", "nemesis", "win")
    matcher.record_outcome("e", "pushover", "loss")
    matcher.record_outcome("e", "pushover", "loss")
    matcher.record_outcome("e", "pushover", "loss")
    # Win where never lost (weight 0), lose where always lost (weight 3).
    rate = matcher.weighted_win_rate("e", ["nemesis", "pushover"])
    assert abs(rate - 0.0) < 1e-9
    assert matcher.weighted_win_rate("e", []) == 0.0
    print("PFSP_WINRATE_OK")


def test_pfsp_weighted_win_rate_uniform_fallback():
    matcher = PFSPMatcher(OpponentBook(), prior=0.0, alpha=1.0)
    matcher.record_outcome("e", "a", "win")
    matcher.record_outcome("e", "b", "win")
    assert matcher.weights("e", ["a", "b"]) == [0.0, 0.0]
    assert matcher.weighted_win_rate("e", ["a", "b"]) == 1.0
    assert matcher.weighted_win_rate("e", []) == 0.0
    print("PFSP_WINRATE_UNIFORM_FALLBACK_OK")


def test_ingest_arena_history():
    book = OpponentBook()
    rows = [
        {"winner": 1, "teams": {"team1": ["a", "b"], "team2": ["c"]}},
        {"winner": 2, "teams": {"team1": ["c"], "team2": ["a"]}},
        {"winner": 0, "teams": {"team1": ["b"], "team2": ["c"]}},
        {"winner": 1, "teams": {}},  # no teams: skipped
        "garbage",  # not a dict: skipped
    ]
    assert ingest_arena_history(book, rows) == 3
    # a beat c in bout 1 and again in bout 2 (team2 = [a] won).
    assert book.stats("a", "c") == {"games": 2, "wins": 2, "losses": 0, "draws": 0}
    assert book.stats("c", "a") == {"games": 2, "wins": 0, "losses": 2, "draws": 0}
    # c lost bout 1 to b, then drew b: its own row shows both.
    assert book.stats("c", "b") == {"games": 2, "wins": 0, "losses": 1, "draws": 1}
    # draw bout recorded from both sides.
    assert book.stats("b", "c")["draws"] == 1
    # int keys are accepted too (arena has used both).
    book2 = OpponentBook()
    ingest_arena_history(book2, [{"winner": 1, "teams": {1: ["x"], 2: ["y"]}}])
    assert book2.stats("x", "y")["wins"] == 1
    print("ARENA_HISTORY_OK")


def test_expected_score_matches_server_formula():
    assert abs(expected_score(1000, 1000) - 0.5) < 1e-12
    assert abs(expected_score(1400, 1000) - (1 / (1 + 10 ** (-1)))) < 1e-12
    assert expected_score(1000, float("nan")) == 0.5
    assert expected_score(None, 1000) == 0.5
    print("ELO_EXPECTED_OK")


def test_paired_seed_summary():
    result = paired_seed_summary([1, 0, 0.5, 1], [0, 1, 0.5, 0])
    assert result["n"] == 4
    assert abs(result["champion_win_rate"] - 0.625) < 1e-12
    assert abs(result["baseline_win_rate"] - 0.375) < 1e-12
    assert abs(result["mean_diff"] - 0.25) < 1e-12
    assert result["seeds_won"] == 2
    assert result["std_diff"] > 0
    empty = paired_seed_summary([], [])
    assert empty["n"] == 0 and empty["mean_diff"] == 0.0
    try:
        paired_seed_summary([1], [0, 1])
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched seed sequences must be refused")
    print("PAIRED_SEED_OK")


def main():
    test_book_records_and_round_trips()
    test_book_survives_corrupt_file()
    test_pfsp_samples_toward_losses_with_a_floor()
    test_pfsp_weighted_win_rate()
    test_pfsp_weighted_win_rate_uniform_fallback()
    test_ingest_arena_history()
    test_expected_score_matches_server_formula()
    test_paired_seed_summary()
    print("ALL_LEAGUE_OK")


if __name__ == "__main__":
    main()
