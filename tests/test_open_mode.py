"""Standalone test for the shared-reward open mode (#626)."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ml.open_mode import (  # noqa: E402
    SharedReward,
    emergence_verdict,
    read_report,
    specialisation_report,
    write_report,
)


def transcript_rows(actions_by_agent, elapsed_s):
    """Build #624-shaped rows: {agent_id, action, elapsed_s} per step."""
    rows = []
    step = 0
    for agent, actions in actions_by_agent.items():
        for action in actions:
            rows.append(
                {
                    "agent_id": agent,
                    "episode": 1,
                    "step": step,
                    "action": action,
                    "incarnation": 0,
                    "elapsed_s": elapsed_s,
                }
            )
            step += 1
    return rows


def test_shared_reward_is_equal_and_accumulates():
    shared = SharedReward(weights={"score_gain": 1.0, "kills": 2.0})
    hook_a = shared.hook("a")
    hook_b = shared.hook("b")
    assert hook_a({"score_gain": 3.0, "kills": 1.0}) == 5.0
    assert hook_b({"score_gain": 1.0, "kills": 1.0}) == 4.0
    assert shared.snapshot()["n_agents"] == 2
    assert shared.snapshot()["totals"] == {"score_gain": 4.0, "kills": 2.0}
    print("OPEN_SHARED_EQUAL_OK")


def test_shared_reward_per_capita_and_bad_values():
    shared = SharedReward(weights={"gold_delta": 1.0}, per_capita=True)
    hook = shared.hook("a")
    hook({"gold_delta": 10.0})
    hook({"gold_delta": float("nan")})
    hook({"gold_delta": None})
    hook("not a dict")
    assert shared.shared_value() == 10.0
    assert shared.snapshot()["n_agents"] == 1
    per_total = SharedReward(weights={"gold_delta": 1.0}, per_capita=False)
    per_total.observe("a", {"gold_delta": 5.0})
    per_total.observe("b", {"gold_delta": 5.0})
    assert per_total.shared_value() == 10.0
    print("OPEN_SHARED_EDGES_OK")


def test_report_bins_and_entropy():
    # Bin 0: two agents each use four actions evenly (high entropy).
    # Bin 2: each collapses to one action (zero entropy).
    rows = transcript_rows(
        {"a": [0, 1, 2, 3], "b": [4, 5, 6, 7]}, elapsed_s=10
    ) + transcript_rows({"a": [0, 0, 0, 0], "b": [4, 4, 4, 4]}, elapsed_s=3700)
    report = specialisation_report(rows, window_s=1800)
    assert report["window_s"] == 1800
    assert len(report["bins"]) == 2, report
    first, last = report["bins"]
    assert first["n_agents"] == 2 and first["n_steps"] == 8
    assert first["mean_action_entropy"] == 1.0, first
    assert last["mean_action_entropy"] == 0.0, last
    assert last["bin_start_s"] == 3600
    print("OPEN_REPORT_BINS_OK")


def test_verdict_pairs_entropy_with_throughput():
    rows = transcript_rows({"a": [0, 1, 2, 3]}, elapsed_s=10) + transcript_rows(
        {"a": [0, 0, 0, 0]}, elapsed_s=3700
    )
    held = [
        {"elapsed_s": 10, "kills": 5, "gold_moved": 100, "quests": 2},
        {"elapsed_s": 3700, "kills": 5, "gold_moved": 100, "quests": 2},
    ]
    report = specialisation_report(rows, throughput=held, window_s=1800)
    verdict = emergence_verdict(report)
    assert verdict["status"] == "specialisation_candidate", verdict
    assert verdict["entropy_fell"] is True and verdict["throughput_held"] is True

    dropped = [
        {"elapsed_s": 10, "kills": 5, "gold_moved": 100, "quests": 2},
        {"elapsed_s": 3700, "kills": 0, "gold_moved": 1, "quests": 0},
    ]
    report2 = specialisation_report(rows, throughput=dropped, window_s=1800)
    verdict2 = emergence_verdict(report2)
    assert verdict2["status"] == "no_emergence", verdict2
    assert verdict2["entropy_fell"] is True and verdict2["throughput_held"] is False
    print("OPEN_VERDICT_PAIR_OK")


def test_verdict_entropy_alone_is_not_enough():
    rows = transcript_rows({"a": [0, 1, 2, 3]}, elapsed_s=10) + transcript_rows(
        {"a": [0, 1, 2, 3]}, elapsed_s=3700
    )
    report = specialisation_report(rows, window_s=1800)
    verdict = emergence_verdict(report)
    assert verdict["status"] == "no_emergence", verdict
    assert verdict["entropy_fell"] is False
    assert emergence_verdict({"bins": []})["status"] == "insufficient_data"
    print("OPEN_VERDICT_ALONE_OK")


def test_report_edges_and_io():
    assert specialisation_report([])["bins"] == []
    assert specialisation_report([None, {"action": None}, {}])["bins"] == []
    bare = specialisation_report([{"agent_id": "a", "action": 2, "elapsed_s": 0}])
    assert bare["bins"][0]["mean_action_entropy"] == 0.0
    assert bare["bins"][0]["throughput"] == {
        "kills": 0.0,
        "gold_moved": 0.0,
        "quests": 0.0,
    }

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "reports", "open.json")
        written = write_report(bare, path)
        assert written == os.path.abspath(path)
        assert os.path.exists(path)
        assert read_report(path) == bare
        assert read_report(os.path.join(folder, "missing.json")) == {}
    print("OPEN_IO_OK")


def main():
    test_shared_reward_is_equal_and_accumulates()
    test_shared_reward_per_capita_and_bad_values()
    test_report_bins_and_entropy()
    test_verdict_pairs_entropy_with_throughput()
    test_verdict_entropy_alone_is_not_enough()
    test_report_edges_and_io()
    print("ALL_OPEN_MODE_OK")


if __name__ == "__main__":
    main()
