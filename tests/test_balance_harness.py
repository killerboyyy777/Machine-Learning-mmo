"""Balance gate harness unit tests (#537): no server, no torch.

Covers the evaluation half only, which is the half that must be trusted:
the conservation identity, treasury/market/combat metric math, threshold
resolution and profile overrides, the report schema, the PASS/FAIL exit
code of ``evaluate``, cross-run determinism, and the scenario matrix's
declared skips.

Run from the repo root:  python tests/test_balance_harness.py
"""

import copy
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ml.balance as bal


def _player(name, deaths, level=1):
    return {"name": name, "level": level, "deaths": deaths, "kills": 1, "gold": 5}


def make_samples(alive=8, final_deaths=(1, 1), craft_seq=(3,)):
    """Three samples: spawn, steady state, end. Two tracked players."""
    base = {
        "server": {"players_online": 2},
        "market": {
            "reserve": 500.0,
            "tax_effective_pct": 22.22,
            "trade_count": 0,
            "sunk_lifetime": 0.0,
            "orders": [],
            "bids": [],
            "history": [],
        },
        "quests": {"completions": {"wolf": 0}},
        "craft_history": [],
        "dungeon_clears": {"solo_clears": 0, "group_clears": 0},
        "scores": [],
    }

    def sample(
        t, deaths, treasury, sunk, trades, orders=(), bids=(), history=(), craft=()
    ):
        state = copy.deepcopy(base)
        state["players"] = [
            _player("alpha", deaths[0]),
            _player("beta", deaths[1]),
        ]
        state["market"].update(
            market_treasury=treasury,
            treasury=treasury,
            sunk_lifetime=sunk,
            trade_count=trades,
            orders=list(orders),
            bids=list(bids),
            history=list(history),
        )
        state["craft_history"] = [{"seq": seq} for seq in craft]
        state["quests"] = {"completions": {"wolf": int(trades)}}
        return {
            "t": t,
            "alive": alive,
            "episodes": int(t // 100),
            "state": state,
            "tables": {"treasury": treasury, "treasury_lifetime": sunk},
        }

    book_ask = [{"item": "Wolf Pelt", "price": 110.0}]
    book_bid = [{"item": "Wolf Pelt", "price": 100.0}]
    trades = [{"buyer": "alpha", "seller": "beta", "item": "Wolf Pelt", "price": 100}]
    return [
        sample(0.0, (0, 0), 500.0, 0.0, 0),
        sample(1800.0, (1, 1), 510.0, 10.0, 6, book_ask, book_bid, trades),
        sample(
            2400.0,
            final_deaths,
            505.0,
            15.0,
            12,
            book_ask,
            book_bid,
            trades,
            craft=craft_seq,
        ),
    ]


LEDGER = {
    "items": {"created": 10, "consumed": 3, "destroyed": 1, "held": 6},
    "gold": {"created": 100, "consumed": 20, "destroyed": 10, "held": 70},
    "craft_input_loss": 0,
    "negative_inventory": 0,
    "craft_refunded_total": 3,
    "craft_escrowed_total": 5,
}

TELEMETRY = {"companion_damage_share_pct": 25.0}


def make_measurements(profile="smoke", seed=700, alive=8):
    samples = make_samples(alive=alive)
    return {
        "meta": {"profile": profile, "seed": seed, "agents": 10, "duration_s": 2400},
        "samples": samples,
        "derived": bal.derive(samples, LEDGER, TELEMETRY),
    }


def _failing(report):
    return set(report["summary"]["failing"])


# --- conservation math ------------------------------------------------------


def test_conservation_identity():
    assert bal.conservation_drift(10, 3, 1, 6) == 0
    assert bal.conservation_drift(10, 3, 1, 7) == -1
    assert bal.conservation_drift(10, 3, 1, 5) == 1
    leaked = dict(
        LEDGER, items={"created": 10, "consumed": 3, "destroyed": 1, "held": 6.5}
    )
    derived = bal.derive(make_samples(), leaked, TELEMETRY)
    assert derived["conservation"]["items_drift"] == -0.5
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    # -0.5 is as wrong as +0.5: the identity allows no drift either way.
    assert "conservation.items_drift" in _failing(report)
    print("CONSERVATION_IDENTITY_OK")


# --- derive ---------------------------------------------------------------


def test_derive_metrics():
    derived = bal.derive(make_samples(), LEDGER, TELEMETRY)
    assert derived["treasury"]["reserve"] == 500.0
    assert derived["treasury"]["sunk_start"] == 0.0
    assert derived["treasury"]["sunk_end"] == 15.0
    assert derived["treasury"]["tax_effective_pct"] == 22.22
    assert derived["treasury"]["series"][-1] == {"t": 2400.0, "value": 505.0}
    # Two players x 2400s = 1.333 agent-hours; two deaths => 1.5/h.
    assert derived["combat"]["deaths"] == 2
    assert abs(derived["combat"]["agent_hours"] - 1.333) < 0.01
    assert abs(derived["combat"]["deaths_per_hour"] - 1.5) < 0.01
    assert derived["combat"]["companion_damage_share_pct"] == 25.0
    # (ask 110, bid 100) => 9.52% spread; trade_count 0 -> 12.
    assert derived["market"]["median_spread_pct"] == 9.524
    assert derived["market"]["trade_volume"] == 12
    assert derived["market"]["wash_trades"] == 0
    assert derived["craft"]["completions"] == 1
    assert derived["craft"]["refund_excess"] == 0
    assert derived["progression"]["quest_completions"] == 12
    assert derived["liveness"]["min_alive"] == 8
    assert derived["liveness"]["final_alive"] == 8
    assert derived["determinism"]["fingerprint"]
    print("DERIVE_METRICS_OK")


def test_derive_without_ledger_skips_not_passes():
    derived = bal.derive(make_samples())
    assert derived["conservation"]["items_drift"] is None
    assert derived["craft"]["input_loss"] is None
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["conservation.items_drift"] == "skip"
    assert statuses["conservation.gold_drift"] == "skip"
    # Absence of a ledger cannot turn the rest of the smoke gate green.
    assert "conservation.items_drift" not in _failing(report)
    print("NO_LEDGER_SKIP_OK")


def test_zero_craft_and_unexercised_sink():
    samples = make_samples(craft_seq=())
    for index, treasury in enumerate((0.0, 10.0, 17.0)):
        samples[index]["state"]["market"]["treasury"] = treasury
        samples[index]["state"]["market"]["sunk_lifetime"] = 0.0
        samples[index]["tables"]["treasury"] = treasury
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    # An empty feed is a measured zero, not missing data.
    assert derived["craft"]["completions"] == 0
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["craft.completions"] == "pass"
    # Treasury never reached the reserve, so the sink path is untested.
    assert statuses["treasury.sink_grew"] == "skip"
    print("ZERO_CRAFT_SINK_SKIP_OK")


def test_wash_trade_detected():
    samples = make_samples()
    samples[-1]["state"]["market"]["history"] = [
        {"buyer": "alpha", "seller": "alpha", "item": "Wolf Pelt", "price": 5}
    ]
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    assert derived["market"]["wash_trades"] == 1
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    assert "market.wash_trades" in _failing(report)
    print("WASH_TRADE_OK")


# --- evaluation / report schema -------------------------------------------


def test_evaluate_pass_and_schema():
    measurements = make_measurements()
    thresholds = bal.load_thresholds(profile="smoke")
    report = bal.evaluate_run(measurements, thresholds, "smoke", 700)
    assert report["verdict"] == "PASS", report["summary"]
    assert set(report) >= {"seed", "verdict", "checks", "scenarios", "summary"}
    assert report["summary"]["fail"] == 0
    ids = {check["id"] for check in report["checks"]}
    for expected in (
        "conservation.items_drift",
        "treasury.band",
        "combat.deaths_per_hour",
        "craft.completions",
        "market.median_spread",
        "liveness.min_agents",
    ):
        assert expected in ids, expected
    for check in report["checks"]:
        assert set(check) >= {
            "id",
            "scenario",
            "metric",
            "op",
            "value",
            "limit",
            "status",
            "blocking",
            "provisional",
        }
        assert check["status"] in ("pass", "fail", "skip")

    profile = bal.evaluate_profile(
        [{"seed": 700, "measurements": measurements}], thresholds, "smoke"
    )
    assert profile["schema"] == bal.REPORT_SCHEMA
    assert profile["profile"] == "smoke"
    assert profile["provisional_thresholds"] is True
    assert profile["thresholds"]["treasury"]["band_pct_of_reserve"] == 10.0
    assert profile["seeds"] == [700]
    assert profile["summary"]["verdict"] == "PASS"
    assert profile["scenarios"]
    print("EVALUATE_PASS_SCHEMA_OK")


def test_evaluate_fail_paths():
    measurements = make_measurements()
    derived = copy.deepcopy(measurements["derived"])
    derived["combat"]["deaths_per_hour"] = 20.0
    derived["market"]["median_spread_pct"] = 40.0
    derived["treasury"]["reserve"] = 1000.0
    broken = dict(measurements, derived=derived)
    report = bal.evaluate_run(
        broken, bal.load_thresholds(profile="smoke"), "smoke", 700
    )
    failing = _failing(report)
    assert report["verdict"] == "FAIL"
    assert {
        "combat.deaths_per_hour",
        "market.median_spread",
        "treasury.band",
    } <= failing, failing
    assert report["summary"]["fail"] == len(failing)
    print("EVALUATE_FAIL_OK")


def test_empty_run_cannot_pass_gate():
    empty = {"meta": {"profile": "smoke", "seed": 700}, "samples": [], "derived": {}}
    report = bal.evaluate_run(empty, bal.load_thresholds(profile="smoke"), "smoke", 700)
    assert report["verdict"] == "FAIL"
    failing = _failing(report)
    for scenario in ("combat", "craft", "market", "liveness"):
        assert f"coverage.{scenario}" in failing, (scenario, failing)
    # Economy's thresholds need a 30-minute window, so smoke records it as
    # observed/skipped instead of failing coverage on a 10-minute run.
    assert "coverage.economy" not in failing
    print("EMPTY_RUN_FAILS_OK")


def test_profile_overrides_and_threshold_file():
    short = make_measurements()
    short["meta"]["duration_s"] = 600
    # Smoke keeps the 30-minute convergence window (a 10-minute run cannot
    # reach it, so the check reports skip instead of a fake pass) and
    # tolerates zero craft completions in 10 minutes.
    smoke = bal.load_thresholds(profile="smoke")
    assert smoke["treasury"]["steady_after_s"] == 1800.0
    assert smoke["craft"]["min_completions"] == 0
    assert bal.load_thresholds(profile="gate")["craft"]["min_completions"] == 1
    # A stored override wins over the profile default.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "thresholds.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"combat": {"deaths_per_hour_max": 99.0}}, fh)
        overridden = bal.load_thresholds(path, "smoke")
    assert overridden["combat"]["deaths_per_hour_max"] == 99.0
    assert overridden["combat"]["deaths_per_hour_min"] == 0.5
    # A typo in a threshold path is an error, never a silent pass.
    try:
        bal.resolve_threshold(overridden, ("combat", "nope"))
        raise AssertionError("missing threshold must raise")
    except KeyError:
        print("THRESHOLD_OVERRIDE_OK")


# --- determinism ----------------------------------------------------------


def test_determinism_repeats():
    measurements = make_measurements(profile="gate", alive=16)
    thresholds = bal.load_thresholds(profile="gate")
    same = bal.evaluate_profile(
        [
            {"seed": 700, "measurements": measurements},
            {"seed": 700, "measurements": copy.deepcopy(measurements)},
        ],
        thresholds,
        "gate",
    )
    det = [c for c in same["checks"] if c["id"] == "determinism.same_seed.700"]
    assert len(det) == 1 and det[0]["status"] == "pass", det

    other = copy.deepcopy(measurements)
    other["derived"]["determinism"]["fingerprint"] = "different"
    different = bal.evaluate_profile(
        [
            {"seed": 700, "measurements": measurements},
            {"seed": 700, "measurements": other},
        ],
        thresholds,
        "gate",
    )
    assert "determinism.same_seed.700" in _failing(different)
    assert different["summary"]["verdict"] == "FAIL"
    print("DETERMINISM_OK")


# --- CLI ------------------------------------------------------------------


def _write_measurements(tmp, measurements):
    path = os.path.join(tmp, "measurements.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(measurements, fh)
    return path


def test_cli_exit_codes():
    with tempfile.TemporaryDirectory() as tmp:
        passing = _write_measurements(tmp, make_measurements())
        out = os.path.join(tmp, "report.json")
        code = bal.main(["evaluate", "--measurements", passing, "--out", out])
        assert code == 0, code
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        assert report["schema"] == bal.REPORT_SCHEMA
        assert report["summary"]["verdict"] == "PASS"

        broken = make_measurements()
        broken["derived"]["combat"]["deaths_per_hour"] = 50.0
        failing = _write_measurements(tmp, broken)
        code = bal.main(["evaluate", "--measurements", failing])
        assert code == 1, code

        missing = os.path.join(tmp, "missing.json")
        with open(missing, "w", encoding="utf-8") as fh:
            json.dump({"meta": {}}, fh)
        assert bal.main(["evaluate", "--measurements", missing]) == 2
        assert (
            bal.main(["evaluate", "--measurements", passing, "--profile", "nope"]) == 2
        )
        assert bal.main(["matrix"]) == 0
    print("CLI_EXIT_CODES_OK")


def test_baseline_comparison():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baseline.json")
        # The shape of the committed v0.10.0 baseline: derived blocks with
        # market/players/soak, no native samples.
        baseline = {
            "meta": {
                "name": "v0.10.0 economy baseline",
                "master_sha": "08f7453",
                "seed": 700,
                "duration_s": 3600.0,
                "agents": 20,
            },
            "derived": {
                "market": {
                    "treasury": 700.0,
                    "sunk_lifetime": 35.0,
                    "trade_count": 33,
                    "tax_effective_pct": 22.22,
                },
                "players": {"deaths": 917},
                "soak": {"alive": 20},
            },
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(baseline, fh)
        measurements = make_measurements()
        report = bal.evaluate_profile(
            [{"seed": 700, "measurements": measurements}],
            bal.load_thresholds(profile="smoke"),
            "smoke",
            baseline=bal._load_baseline(path),
        )
    block = report["baseline"]
    assert block["meta"]["master_sha"] == "08f7453"
    rows = {row["metric"]: row for row in block["deltas"]}
    assert rows["treasury.end"]["baseline"] == 700.0
    assert rows["treasury.end"]["current"] == 505.0
    assert rows["market.trade_count"]["baseline"] == 33
    assert rows["combat.deaths"]["baseline"] == 917
    assert rows["liveness.final_alive"]["current"] == 8
    print("BASELINE_COMPARE_OK")


# --- matrix ---------------------------------------------------------------


def test_matrix_catalog():
    gate = bal.PROFILES["gate"]
    assert gate["duration_s"] == 14400.0
    assert gate["agents"] == 20
    assert gate["seeds"] == (700, 701, 702)
    smoke = bal.PROFILES["smoke"]
    assert smoke["duration_s"] == 600.0
    assert smoke["agents"] == 10
    # Scenarios that depend on unlanded work are declared, never dropped.
    assert bal.SCENARIOS["npc_ai_v2"]["unlanded"]
    assert "#536" in bal.SCENARIOS["npc_ai_v2"]["unlanded"]
    assert bal.SCENARIOS["acquisition"]["unlanded"]
    assert bal.SCENARIOS["progression"]["observed_only"]
    thresholds = bal.load_thresholds(profile="gate")
    assert thresholds["liveness"]["min_agents"] == 16
    assert thresholds["combat"]["deaths_per_hour_min"] == 0.5
    assert thresholds["combat"]["deaths_per_hour_max"] == 3.0
    assert thresholds["market"]["max_median_spread_pct"] == 25.0
    print("MATRIX_CATALOG_OK")


def test_committed_baseline_is_readable():
    path = os.path.join(ROOT, "balance", "baseline-08f7453.json")
    assert os.path.isfile(path), "the reference baseline must stay committed"
    with open(path, encoding="utf-8") as fh:
        baseline = json.load(fh)
    assert baseline["meta"]["master_sha"] == "08f7453"
    assert baseline["meta"]["seed"] == 700
    assert baseline["derived"]["market"]["reserve"] == 500.0
    print("COMMITTED_BASELINE_OK")


def test_import_has_no_heavy_deps():
    assert "torch" not in sys.modules
    assert "websockets" not in sys.modules
    print("NO_HEAVY_DEPS_OK")


def main():
    test_conservation_identity()
    test_derive_metrics()
    test_derive_without_ledger_skips_not_passes()
    test_wash_trade_detected()
    test_zero_craft_and_unexercised_sink()
    test_evaluate_pass_and_schema()
    test_evaluate_fail_paths()
    test_empty_run_cannot_pass_gate()
    test_profile_overrides_and_threshold_file()
    test_determinism_repeats()
    test_cli_exit_codes()
    test_baseline_comparison()
    test_matrix_catalog()
    test_committed_baseline_is_readable()
    test_import_has_no_heavy_deps()
    print("BALANCE_HARNESS_OK")


if __name__ == "__main__":
    main()
