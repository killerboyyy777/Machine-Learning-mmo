"""Standalone tests for the seedable dungeon generator (#67)."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))

from dungeon_generator import (  # noqa: E402
    DEFAULT_CONFIG,
    MAX_FORMULA_FLOORS,
    SCHEMA_VERSION,
    floor_factory,
    floor_params,
    generate_plan,
    normalize_config,
    read_plan,
    write_plan,
)


def test_config_normalization():
    assert normalize_config({}) == DEFAULT_CONFIG
    bad_configs = (
        {"floors": 0},
        {"floors": MAX_FORMULA_FLOORS + 1},
        {"bogus": 1},
        {"layout": "multi"},
        {"guard_names": []},
        {"loot_table": "not-a-list"},
        {"seed": "not-an-int"},
    )
    for bad in bad_configs:
        try:
            normalize_config(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad config accepted: {bad}")
    print("DUNGEON_CONFIG_OK")


def test_params_match_builtin_without_jitter():
    cfg = normalize_config({"jitter_pct": 0})
    for n in range(1, 10):
        p = floor_params(cfg, n)
        assert p["hp"] == round(20 * 1.35 ** (n - 1)), (n, p["hp"])
        assert p["atk"] == round(4 * 1.5 ** (n - 1)), (n, p["atk"])
        assert p["count"] == min(1 + (n - 1) // 2, 4), (n, p["count"])
        assert p["gold"] == round(3 * 1.35 ** (n - 1)), (n, p["gold"])
        assert p["respawn_seconds"] == 20 + n * 10
        assert p["loot"] == [f"dungeon_shard_{n}"]
        # A guard with a state-machine field gets full chase/leash/flee AI.
        for field in ("aggro_radius", "leash_radius", "flee_below_pct"):
            assert field not in p
    print("DUNGEON_PARAMS_OK")


def test_seed_reproducible_and_order_independent():
    a = generate_plan({"seed": 7, "floors": 6})
    b = generate_plan({"seed": 7, "floors": 6})
    c = generate_plan({"seed": 8, "floors": 6})
    assert a["fingerprint"] == b["fingerprint"]
    assert a["fingerprint"] != c["fingerprint"]
    assert a["floors"] == b["floors"]
    single = floor_params(normalize_config({"seed": 7}), 4)
    assert single == a["floors"][3]["params"]
    print("DUNGEON_SEED_OK")


def test_loot_table_and_guard_names():
    cfg = normalize_config(
        {
            "seed": 3,
            "jitter_pct": 0,
            "loot_table": ["iron_ore", "wolf_pelt"],
            "guard_names": ["Elite {n}-{k}"],
        }
    )
    plan = generate_plan(cfg)
    for row in plan["floors"]:
        for guard in row["guards"]:
            assert guard["loot"][0] in ("iron_ore", "wolf_pelt")
            assert guard["name"].startswith("Elite ")
    print("DUNGEON_LOOT_OK")


def test_plan_round_trip():
    plan = generate_plan({"seed": 11, "floors": 3})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "plans", "p.json")
        written = write_plan(plan, path)
        assert written == os.path.abspath(path)
        assert read_plan(path) == plan
        assert read_plan(os.path.join(tmp, "missing.json")) == {}
    print("DUNGEON_PLAN_IO_OK")


def test_factory_override_and_passthrough():
    plan = generate_plan({"seed": 5, "floors": 3, "jitter_pct": 0})
    factory = floor_factory(plan)
    builtin = {
        "hp": 20,
        "atk": 4,
        "count": 1,
        "gold": 3,
        "loot": ["dungeon_shard_1"],
        "respawn_seconds": 30,
        "name": "Dungeon Guard {n}-{k}",
    }
    out = factory(0, 1, dict(builtin))
    params = plan["floors"][0]["params"]
    assert out["hp"] == params["hp"]
    assert out["name"] == params["name"]
    assert factory(0, 4, dict(builtin)) == builtin
    try:
        floor_factory({"schema_version": SCHEMA_VERSION + 99})
    except ValueError:
        pass
    else:
        raise AssertionError("bad schema accepted")
    print("DUNGEON_FACTORY_OK")


def main():
    test_config_normalization()
    test_params_match_builtin_without_jitter()
    test_seed_reproducible_and_order_independent()
    test_loot_table_and_guard_names()
    test_plan_round_trip()
    test_factory_override_and_passthrough()
    print("ALL_DUNGEON_GENERATOR_OK")


if __name__ == "__main__":
    main()
