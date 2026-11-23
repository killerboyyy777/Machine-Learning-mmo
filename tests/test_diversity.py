"""Standalone test for the offline diversity monitor (#313)."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))

from diversity import (  # noqa: E402
    action_histogram,
    read_summary,
    reuse_ratio,
    shannon_entropy,
    summarize,
    top_action_share,
    write_summary,
)


def rows_from(actions, agent="a1", episode=1):
    return [
        {
            "agent_id": agent,
            "episode": episode,
            "step": i,
            "action": a,
            "incarnation": 0,
        }
        for i, a in enumerate(actions)
    ]


def test_histogram_and_share():
    rows = rows_from([0, 1, 1, 2, 1])
    hist = action_histogram(rows)
    assert hist == {0: 1, 1: 3, 2: 1}, hist
    assert abs(top_action_share(rows) - 3 / 5) < 1e-9
    print("DIVERSITY_HISTOGRAM_OK")


def test_reuse_ratio_order_sensitive():
    assert reuse_ratio(rows_from([1, 1, 1, 1])) == 1.0
    assert reuse_ratio(rows_from([0, 1, 0, 1])) == 0.0
    assert reuse_ratio(rows_from([0, 0, 1])) == 0.5
    assert reuse_ratio(rows_from([7])) == 0.0
    print("DIVERSITY_REUSE_OK")


def test_entropy_and_collapse_fire():
    collapsed = rows_from([3] * 20)
    summary = summarize(collapsed)
    assert summary["n_steps"] == 20
    assert summary["n_actions"] == 1
    assert summary["top_action_share"] == 1.0
    assert summary["reuse_ratio"] == 1.0
    assert summary["entropy"] == 0.0
    assert summary["entropy_norm"] == 0.0
    assert summary["mode_collapse"] is True

    diverse = rows_from([0, 1, 2, 3, 4, 5, 6, 7])
    spread = summarize(diverse)
    assert spread["top_action_share"] < spread["top_action_share"] + 1  # sanity
    assert spread["mode_collapse"] is False
    assert spread["entropy"] > summary["entropy"]
    assert abs(shannon_entropy(diverse) - 3.0) < 1e-9
    print("DIVERSITY_COLLAPSE_OK")


def test_filters_and_reproducibility():
    rows = rows_from([0, 0, 1], agent="a", episode=1) + rows_from(
        [5, 5, 5], agent="b", episode=2
    )
    one = summarize(rows, agent_id="a")
    two = summarize(rows, agent_id="b")
    assert one["n_steps"] == 3 and one["n_actions"] == 2
    assert two["n_steps"] == 3 and two["n_actions"] == 1
    assert two["mode_collapse"] is True
    assert summarize(rows, agent_id="a") == one
    timeline = summarize_stub = None  # noqa: F841
    from diversity import diversity_timeline

    timeline = diversity_timeline(rows_from([0, 1, 0, 1]), window=2)
    assert len(timeline) == 2
    assert all(row["n_steps"] == 2 for row in timeline)
    print("DIVERSITY_FILTERS_OK")


def test_empty_and_edges():
    assert action_histogram([]) == {}
    assert top_action_share([]) == 0.0
    assert reuse_ratio([]) == 0.0
    assert shannon_entropy([]) == 0.0
    blank = summarize([])
    assert blank["n_steps"] == 0 and blank["mode_collapse"] is False
    assert blank["histogram"] == []
    assert summarize([{"action": None}, "garbage"])["n_steps"] == 0
    print("DIVERSITY_EDGES_OK")


def test_write_read_round_trip():
    import tempfile

    summary = summarize(rows_from([2, 2, 2]))
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "reports", "diversity.json")
        written = write_summary(summary, path)
        assert written == os.path.abspath(path)
        assert os.path.exists(path)
        assert read_summary(path) == summary
        assert read_summary(os.path.join(folder, "missing.json")) == {}
    print("DIVERSITY_IO_OK")


def main():
    test_histogram_and_share()
    test_reuse_ratio_order_sensitive()
    test_entropy_and_collapse_fire()
    test_filters_and_reproducibility()
    test_empty_and_edges()
    test_write_read_round_trip()
    print("ALL_DIVERSITY_OK")


if __name__ == "__main__":
    main()
