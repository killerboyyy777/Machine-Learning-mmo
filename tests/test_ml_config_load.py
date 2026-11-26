"""ML config loader validation regression (#988).

Run from the repo root:  python tests/test_ml_config_load.py

The runtime loader must apply the same config_schema rules as the dashboard
writer before it mutates ml_env globals. Invalid entries keep the shipped
default so a hand-edited ml_config.json cannot raise TypeError mid-training.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml.ml_env as ml_env  # noqa: E402

KEYS = ("SOCIAL_PER_ALLY", "FORMATION_COOLDOWN_STEPS", "CURRICULUM_THRESHOLDS")
DEFAULTS = {key: getattr(ml_env, key) for key in KEYS}


def _advance():
    """Run the curriculum promotion path against the currently loaded config."""

    class _Probe:
        curriculum_auto = True
        curriculum_stage = 0
        _state = {"score": 5.0}

    probe = _Probe()
    ml_env.TextMMOEnv._maybe_advance_curriculum(probe)
    return probe.curriculum_stage


def _load(cfg, probe=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ml_config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        try:
            with mock.patch.object(ml_env, "_ML_CONFIG_FILE", str(path)):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    ml_env._apply_ml_config()
                stage = probe() if probe is not None else None
            values = {key: getattr(ml_env, key) for key in KEYS}
            return values, out.getvalue(), stage
        finally:
            for key, value in DEFAULTS.items():
                setattr(ml_env, key, value)


def test_valid_entries_are_applied():
    cfg = {
        "_comment": "ignored",
        "reward_shaping": {
            "SOCIAL_PER_ALLY": 0.2,
            "FORMATION_COOLDOWN_STEPS": 200,
        },
        "curriculum": {"CURRICULUM_THRESHOLDS": [0, 5, 15, 30]},
    }
    values, output, stage = _load(cfg, probe=_advance)
    assert output == "", output
    assert values["SOCIAL_PER_ALLY"] == 0.2
    assert values["FORMATION_COOLDOWN_STEPS"] == 200
    assert values["CURRICULUM_THRESHOLDS"] == (0, 5, 15, 30)
    assert stage == 1  # a valid early threshold actually promotes the probe
    print("ML_CONFIG_VALID_OK")


def test_invalid_entries_keep_defaults_and_cannot_crash_training():
    cases = (
        (
            {"curriculum": {"CURRICULUM_THRESHOLDS": [0, "bad", 30, 60]}},
            "not a number",
        ),
        (
            {"curriculum": {"CURRICULUM_THRESHOLDS": "[0, 10, 30, 60]"}},
            "expected a JSON list",
        ),
        (
            {"curriculum": {"CURRICULUM_THRESHOLDS": [0, 10, 30]}},
            "needs exactly 4 numbers",
        ),
        (
            {"curriculum": {"CURRICULUM_THRESHOLDS": [0, -1, 30, 60]}},
            "outside 0-100000",
        ),
        (
            {"curriculum": {"CURRICULUM_THRESHOLDS": [0, 10, 30, 100001]}},
            "outside 0-100000",
        ),
        ({"reward_shaping": {"SOCIAL_PER_ALLY": 200.0}}, "outside 0.0-100.0"),
        ({"reward_shaping": {"FORMATION_COOLDOWN_STEPS": 0}}, "outside 1-1000000"),
    )
    for cfg, fragment in cases:
        values, output, stage = _load(cfg, probe=_advance)
        assert values == DEFAULTS, (cfg, values)
        assert fragment in output, (cfg, output)
        assert "keeping default" in output, (cfg, output)
        assert stage == 0, (cfg, stage)
    print("ML_CONFIG_INVALID_OK")


def test_key_in_the_wrong_section_is_ignored():
    cfg = {"reward_shaping": {"CURRICULUM_THRESHOLDS": [0, 5, 15, 30]}}
    values, output, stage = _load(cfg, probe=_advance)
    assert values == DEFAULTS
    assert output == "", output
    assert stage == 0
    print("ML_CONFIG_SECTION_OK")


if __name__ == "__main__":
    test_valid_entries_are_applied()
    test_invalid_entries_keep_defaults_and_cannot_crash_training()
    test_key_in_the_wrong_section_is_ignored()
    print("ALL_ML_CONFIG_LOAD_OK")
