"""Standalone test for the same-frame generalist vs purist pilot (#419)."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml import generalist  # noqa: E402


def test_archetype_and_cross_rewards():
    signals = {"score_delta": 2, "deaths": 0, "profit": 3, "xp_delta": 4, "social": 5}
    assert generalist.archetype_reward(signals, "fighter") == 2
    assert generalist.archetype_reward(signals, "trader") == 3
    assert generalist.archetype_reward(signals, "explorer") == 4
    assert generalist.archetype_reward(signals, "social") == 5
    assert generalist.cross_archetype_reward(signals) == 14
    weights = {"fighter": 2.0, "trader": 0.0}
    assert generalist.cross_archetype_reward(signals, weights) == 4
    assert generalist.archetype_reward({"score_delta": 1, "deaths": -1}, "fighter") == 0
    print("GENERALIST_REWARD_OK")


def test_plugins_and_manifest():
    gen = generalist.GeneralistPlugin()
    pur = generalist.PuristPlugin()
    signals = {"score_delta": 1, "profit": 2, "xp_delta": 3, "social": 4}
    assert gen.reward(signals) == 10
    assert pur.reward(signals) == 1
    trader = generalist.PuristPlugin(archetype="trader")
    assert trader.reward(signals) == 2
    from ml.plugins import manifest_problems

    assert manifest_problems(generalist.GeneralistPlugin) == []
    assert manifest_problems(generalist.PuristPlugin) == []
    names = generalist.register_plugins()
    assert names == ["generalist", "purist"]
    assert generalist.register_plugins() == ["generalist", "purist"]
    print("GENERALIST_PLUGINS_OK")


def test_same_frame_curve():
    frames = [
        {"score_delta": 1, "profit": 1, "xp_delta": 1, "social": 1} for _ in range(3)
    ]
    curves = generalist.same_frame_curve(frames, bin_steps=2)
    assert curves["generalist"] == [8, 12], curves["generalist"]
    assert curves["fighter"] == [2, 3]
    assert curves["trader"] == [2, 3]
    assert generalist.same_frame_curve([], bin_steps=2) == {
        name: [] for name in list(generalist.ARCHETYPES) + ["generalist"]
    }
    print("GENERALIST_CURVE_OK")


def test_pilot_verdict():
    growing = [
        {"score_delta": 1, "profit": 1, "xp_delta": 1, "social": 1} for _ in range(9)
    ]
    report = generalist.pilot_report(generalist.same_frame_curve(growing, bin_steps=3))
    assert report["verdict"] == "promising", report
    assert report["final_lead"] == 27, report
    assert report["bins"] == 3
    assert report["lead_curve"] == [9, 18, 27]

    flat = [{"score_delta": 1} for _ in range(9)]
    tied = generalist.pilot_report(generalist.same_frame_curve(flat, bin_steps=3))
    assert tied["verdict"] == "inconclusive", tied
    assert tied["final_lead"] == 0

    losing = [{"score_delta": 1, "deaths": -2, "profit": -1} for _ in range(9)]
    loss = generalist.pilot_report(generalist.same_frame_curve(losing, bin_steps=3))
    assert loss["verdict"] == "inconclusive", loss
    assert loss["final_lead"] < 0

    short = generalist.pilot_report(
        generalist.same_frame_curve(growing[:2], bin_steps=3)
    )
    assert short["verdict"] == "insufficient_data", short
    print("GENERALIST_VERDICT_OK")


def test_report_io():
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "reports", "pilot.json")
        report = {"verdict": "promising", "final_lead": 1.5}
        assert generalist.write_report(report, path) == os.path.abspath(path)
        assert generalist.read_report(path) == report
        assert generalist.read_report(os.path.join(folder, "missing.json")) == {}
    print("GENERALIST_IO_OK")


def main():
    test_archetype_and_cross_rewards()
    test_plugins_and_manifest()
    test_same_frame_curve()
    test_pilot_verdict()
    test_report_io()
    print("ALL_GENERALIST_OK")


if __name__ == "__main__":
    main()
