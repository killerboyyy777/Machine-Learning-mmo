"""Standalone checks for the read-only market price-discovery helper (#628)."""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))

from market_equilibrium import (  # noqa: E402
    gold_gini,
    mid_deviation_pct,
    observed_mids,
    recipe_costs,
    reference_prices,
    rolling_volatility,
    vendor_bounds,
    write_equilibrium,
)

WORLD = {
    "items": {
        "arrow": {"name": "Arrow", "value": 1},
        "iron_arrow": {"name": "Iron Arrow", "value": 3},
        "wolf_pelt": {"name": "Wolf Pelt", "value": 2},
        "rat_tail": {"name": "Rat Tail", "value": 1},
        "reinforced_leather": {"name": "Reinforced Leather", "value": 9},
        "no_value": {"name": "Mystery"},
    },
    "npcs": {"merchant": {"shop": {"arrow": 2, "iron_arrow": 6}}},
    "recipes": {
        "leather": {
            "inputs": {"wolf_pelt": 1, "rat_tail": 2},
            "result": "reinforced_leather",
        },
        "unknown": {"inputs": {"no_value": 1}, "result": "no_value"},
    },
}


def test_vendor_bounds_and_recipe_costs():
    bounds = vendor_bounds(WORLD)
    assert bounds["arrow"] == {"sell": 1.0, "buy": 2.0}
    assert bounds["wolf_pelt"] == {"sell": 2.0, "buy": None}
    # An item with neither a value nor a shop price has no band at all.
    assert "no_value" not in bounds
    costs = recipe_costs(WORLD)
    assert costs["reinforced_leather"] == 4.0
    assert "no_value" not in costs
    print("VENDOR_RECIPE_OK")


def test_reference_band_raised_to_recipe_cost():
    refs = reference_prices(WORLD)
    # Arrow: sell 1, buy 2 -> mid 1.5.
    assert refs["arrow"] == {"low": 1.0, "high": 2.0, "mid": 1.5}
    # Reinforced leather: sell 9, recipe cost 4 -> low stays 9, no buy.
    assert refs["reinforced_leather"] == {"low": 9.0, "high": None, "mid": 9.0}
    # An item with no value and no cost contributes no reference.
    assert "no_value" not in refs
    print("REFERENCE_BAND_OK")


def test_observed_mids_map_names_and_use_bid():
    names_to_ids = {"Arrow": "arrow", "Iron Arrow": "iron_arrow"}
    state = {
        "orders": [{"item": "Arrow", "price": 4}, {"item": "Iron Arrow", "price": 10}],
        "bids": [{"item": "Arrow", "price": 2}],
    }
    mids = observed_mids(state, names_to_ids=names_to_ids)
    assert mids["arrow"] == 3.0  # (4 + 2) / 2
    assert mids["iron_arrow"] == 10.0  # ask only
    print("OBSERVED_MIDS_OK")


def test_observed_mids_pick_best_ask_and_bid():
    # Orders arrive append-ordered, not price-sorted (#705 review): the last
    # row must not win. Best ask is the minimum, best bid the maximum.
    names_to_ids = {"Arrow": "arrow"}
    state = {
        "orders": [
            {"item": "Arrow", "price": 9},
            {"item": "Arrow", "price": 5},
            {"item": "Arrow", "price": 7},
        ],
        "bids": [
            {"item": "Arrow", "price": 3},
            {"item": "Arrow", "price": 7},
            {"item": "Arrow", "price": 4},
        ],
    }
    mids = observed_mids(state, names_to_ids=names_to_ids)
    assert mids["arrow"] == 6.0  # (best ask 5 + best bid 7) / 2
    ask_only = {
        "orders": [{"item": "Arrow", "price": 9}, {"item": "Arrow", "price": 5}]
    }
    assert observed_mids(ask_only, names_to_ids=names_to_ids)["arrow"] == 5.0
    print("OBSERVED_BEST_PRICES_OK")


def test_deviation_volatility_and_gini():
    assert mid_deviation_pct(3.0, 1.5) == 100.0
    assert mid_deviation_pct(2.0, None) is None
    assert mid_deviation_pct(2.0, 0.0) is None
    assert rolling_volatility([1, 2, 1, 2]) > 0.0
    assert rolling_volatility([5]) == 0.0
    assert gold_gini([]) == 0.0
    assert gold_gini([10, 10, 10, 10]) == 0.0
    assert 0.0 < gold_gini([0, 0, 0, 100]) <= 1.0
    print("METRICS_OK")


def test_write_equilibrium_from_measurement():
    with tempfile.TemporaryDirectory() as tmp:
        world_path = os.path.join(tmp, "world.json")
        with open(world_path, "w", encoding="utf-8") as handle:
            json.dump(WORLD, handle)
        measurement = {
            "samples": [
                {
                    "state": {
                        "market": {
                            "orders": [{"item": "Arrow", "price": 2}],
                            "bids": [],
                        },
                        "players": [{"gold": 0}, {"gold": 10}],
                    }
                },
                {
                    "state": {
                        "market": {
                            "orders": [{"item": "Arrow", "price": 4}],
                            "bids": [{"item": "Arrow", "price": 2}],
                        },
                        "players": [{"gold": 5}, {"gold": 5}],
                    }
                },
            ]
        }
        measure_path = os.path.join(tmp, "measurements-gate-700.json")
        with open(measure_path, "w", encoding="utf-8") as handle:
            json.dump(measurement, handle)
        out = write_equilibrium(measure_path, world_path=world_path)
        assert os.path.basename(out) == "equilibrium-gate-700.json"
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        assert report["schema_version"] == 1
        arrow = report["items"]["Arrow"]
        assert arrow["last_mid"] == 3.0
        assert arrow["deviation_pct"] == 100.0
        assert arrow["samples"] == 2
        assert report["gold_gini_latest"] == 0.0
        # No order was placed: no market mutation API is even imported.
        assert (
            "place_order"
            not in open(
                os.path.join(ROOT, "ml", "market_equilibrium.py"), encoding="utf-8"
            ).read()
        )
    print("WRITE_EQUILIBRIUM_OK")


def test_write_equilibrium_explicit_out_path():
    with tempfile.TemporaryDirectory() as tmp:
        world_path = os.path.join(tmp, "world.json")
        with open(world_path, "w", encoding="utf-8") as handle:
            json.dump(WORLD, handle)
        measure_path = os.path.join(tmp, "measurements-gate-701.json")
        with open(measure_path, "w", encoding="utf-8") as handle:
            json.dump({"samples": []}, handle)
        out_path = os.path.join(tmp, "explicit-report.json")
        out = write_equilibrium(measure_path, world_path=world_path, out_path=out_path)
        assert out == os.path.abspath(out_path)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        assert report["source"] == "measurements-gate-701.json"
        assert report["schema_version"] == 1
    print("WRITE_EQUILIBRIUM_EXPLICIT_OK")


def test_write_equilibrium_keeps_source_without_measurements_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        world_path = os.path.join(tmp, "world.json")
        with open(world_path, "w", encoding="utf-8") as handle:
            json.dump(WORLD, handle)
        source = {"samples": [], "marker": "source-still-here"}
        measure_path = os.path.join(tmp, "gate-700.json")
        with open(measure_path, "w", encoding="utf-8") as handle:
            json.dump(source, handle)
        out = write_equilibrium(measure_path, world_path=world_path)
        assert os.path.basename(out) == "gate-700-equilibrium.json"
        assert out != measure_path
        with open(measure_path, encoding="utf-8") as handle:
            assert json.load(handle) == source
        with open(out, encoding="utf-8") as handle:
            assert json.load(handle)["schema_version"] == 1
    print("WRITE_EQUILIBRIUM_KEEPS_SOURCE_OK")


def main():
    test_vendor_bounds_and_recipe_costs()
    test_reference_band_raised_to_recipe_cost()
    test_observed_mids_map_names_and_use_bid()
    test_observed_mids_pick_best_ask_and_bid()
    test_deviation_volatility_and_gini()
    test_write_equilibrium_from_measurement()
    test_write_equilibrium_explicit_out_path()
    test_write_equilibrium_keeps_source_without_measurements_prefix()
    print("ALL_MARKET_EQUILIBRIUM_OK")


if __name__ == "__main__":
    main()
