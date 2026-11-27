"""Balance check scope per profile (#1050). No server and no torch.

Run from the repo root with python tests/test_balance_profile_scope.py.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ml.balance as bal  # noqa: E402


def _derived():
    derived = {
        "treasury": {
            "reserve": 500.0,
            "series": [
                {"t": 1800.0, "value": 500.0},
                {"t": 2400.0, "value": 500.0},
            ],
            "sunk_start": 0.0,
            "sunk_end": 10.0,
        },
        "conservation": {"items_drift": 0.0, "gold_drift": 0.0},
        "combat": {
            "deaths_per_hour": 1.0,
            "deaths_per_kill": 0.5,
            "wild_deaths_per_hour": 1.0,
            "civ_deaths_per_hour": 1.0,
            "civ_death_share_pct": 50.0,
            "companion_damage_share_pct": 30.0,
        },
        "craft": {
            "completions": 1,
            "input_loss": 0.0,
            "refund_excess": 0.0,
            "negative_inventory": 0,
        },
        "market": {
            "median_spread_pct": 10.0,
            "trade_volume": 1,
            "wash_trades": 0,
        },
        "liveness": {"min_alive": 20},
        "progression": {"quest_completions": 1, "level_counts": {"1": 20}},
        "acquisition": {"drop_rate": 0.0},
        "guilds": {
            "count": 1,
            "members": 2,
            "vault_gold_in": 6,
            "vault_gold_out": 2,
            "vault_items": 1,
            "audit_entries": 6,
            "conservation_drift": 0.0,
            "gold_conserved": True,
        },
        "arena": {
            "pairs_matched": True,
            "matches_completed": 2,
            "rating_conserved": True,
            "gold_delta": 0.0,
            "items_delta": 0,
            "civ_deaths_delta": 0,
            "wild_deaths_delta": 0,
            "determinism": True,
        },
        "npc_ai_v2": {
            "telemetry_present": True,
            "aggro_acquired": 5,
            "flee_delta": 0.0,
        },
        "stability": {
            "economic_slope": 0.001,
            "level_slope": 0.001,
            "score_slope": 0.001,
            "population_ratio": 1.0,
        },
    }
    return derived


def _zero_guilds():
    """A fresh ten minute world reads zero on every vault counter."""
    return {
        "count": 0,
        "members": 0,
        "vault_gold_in": 0,
        "vault_gold_out": 0,
        "vault_items": 0,
        "audit_entries": 0,
        "conservation_drift": 0.0,
        "gold_conserved": False,
    }


def _measurement(derived):
    return {
        "meta": {"profile": "smoke", "seed": 700},
        "samples": [],
        "derived": derived,
    }


def _rows(report):
    return {row["id"]: row for row in report["checks"]}


def _guild_ids():
    return [check[0] for check in bal.CHECKS if check[1] == "guilds"]


def _blocking_guild_failures(report):
    return [
        row["id"]
        for row in report["checks"]
        if row["status"] == "fail" and row.get("blocking") and "guilds" in row["id"]
    ]


def test_smoke_ignores_guild_bands():
    derived = _derived()
    derived["guilds"] = _zero_guilds()
    measurements = _measurement(derived)
    thresholds = bal.load_thresholds(profile="smoke")

    run = bal.evaluate_run(measurements, thresholds, "smoke", 700)
    assert run["verdict"] == "PASS", run["summary"]
    assert not _blocking_guild_failures(run), run["summary"]
    rows = _rows(run)
    for check_id in _guild_ids():
        row = rows[check_id]
        assert row["status"] == "skip" and row["blocking"] is False, row
        assert row["out_of_profile"] is True, row
        assert "not in profile smoke scenarios" in row["detail"], row

    report = bal.evaluate_profile(
        [{"seed": 700, "measurements": measurements}], thresholds, "smoke"
    )
    assert report["summary"]["verdict"] == "PASS", report["summary"]
    assert not _blocking_guild_failures(report), report["summary"]
    for row in report["checks"]:
        if "guilds" in row["id"]:
            assert row["status"] == "skip" and row["blocking"] is False, row
            assert row["out_of_profile"] is True, row
    print("SMOKE_GUILD_SCOPE_OK")


def test_gate_scores_every_guild_band():
    measurements = _measurement(_derived())
    thresholds = bal.load_thresholds(profile="gate")

    run = bal.evaluate_run(measurements, thresholds, "gate", 700)
    rows = _rows(run)
    for check_id in _guild_ids():
        row = rows[check_id]
        assert row["status"] == "pass", (check_id, row)
        assert row["blocking"] is True, row
        assert "out_of_profile" not in row, row

    report = bal.evaluate_profile(
        [{"seed": 700, "measurements": measurements}], thresholds, "gate"
    )
    rows = _rows(report)
    for check_id in _guild_ids():
        row = rows[f"{check_id}.median"]
        assert row["status"] == "pass", (check_id, row)
        assert row["blocking"] is True, row
    print("GATE_GUILD_SCOPE_OK")


def test_declared_scenario_without_data_fails_coverage():
    measurements = _measurement({"guilds": _zero_guilds()})
    report = bal.evaluate_run(
        measurements, bal.load_thresholds(profile="smoke"), "smoke", 700
    )
    failing = set(report["summary"]["failing"])
    assert report["verdict"] == "FAIL"
    for scenario in bal.PROFILES["smoke"]["coverage"]:
        assert f"coverage.{scenario}" in failing, failing
    assert "coverage.guilds" not in failing, failing
    print("SCOPE_COVERAGE_GUARD_OK")


def test_every_profile_scores_only_declared_scenarios():
    assert {check[1] for check in bal.CHECKS} <= set(bal.SCENARIOS)
    measurements = _measurement(_derived())
    for profile, spec in bal.PROFILES.items():
        assert set(spec["coverage"]) <= set(spec["scenarios"]), profile
        thresholds = bal.load_thresholds(profile=profile)
        report = bal.evaluate_run(measurements, thresholds, profile, 700)
        rows = _rows(report)
        for check in bal.CHECKS:
            check_id, scenario = check[0], check[1]
            row = rows[check_id]
            if scenario in spec["scenarios"]:
                assert row["status"] == "pass", (profile, check_id, row)
                assert "out_of_profile" not in row, row
            else:
                assert row["status"] == "skip", (profile, check_id, row)
                assert row["blocking"] is False, row
                assert row["out_of_profile"] is True, row
        names = {row["name"] for row in report["scenarios"]}
        assert names == set(spec["scenarios"]), (profile, names)
    print("PROFILE_SCENARIO_MAP_OK")


def main():
    test_smoke_ignores_guild_bands()
    test_gate_scores_every_guild_band()
    test_declared_scenario_without_data_fails_coverage()
    test_every_profile_scores_only_declared_scenarios()
    print("BALANCE_PROFILE_SCOPE_OK")


if __name__ == "__main__":
    main()
