"""Soak CLI numeric contract (#996).

Run from the repo root: python tests/test_soak_args.py
Invalid settings must end in argparse with a clear message before any RNG
seeding or conductor construction.
"""

import contextlib
import io
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml.conductor import soak  # noqa: E402


def _parse(extra=()):
    old = sys.argv
    sys.argv = ["soak.py", *extra]
    try:
        return soak.parse_args()
    finally:
        sys.argv = old


def test_defaults_and_accepted_boundaries():
    with mock.patch.object(soak, "seed_everything") as seed:
        args = _parse()
    assert seed.call_count == 1
    assert args.agents == 50 and args.wave_size == 10 and args.policy_timeout == 5.0
    boundary = [
        "--agents",
        "1",
        "--duration",
        "0",
        "--min-agents",
        "0",
        "--max-steps",
        "1",
        "--step-timeout",
        "0.001",
        "--policy-timeout",
        "0",
        "--arrivals",
        "0",
        "--lifetime",
        "1",
        "--wave-size",
        "1",
        "--wave-delay",
        "0",
        "--status-every",
        "0",
    ]
    args = _parse(boundary)
    assert args.agents == 1 and args.duration == 0 and args.min_agents == 0
    assert args.max_steps == 1 and args.policy_timeout == 0  # 0 disables the budget
    assert args.lifetime == 1 and args.wave_size == 1 and args.status_every == 0
    print("SOAK_ARGS_BOUNDARIES_OK")


def test_invalid_values_fail_before_startup():
    cases = (
        (["--agents", "0"], "--agents must be >= 1"),
        (["--agents", "-2"], "--agents must be >= 1"),
        (["--duration", "-1"], "--duration must be >= 0"),
        (["--duration", "nan"], "--duration must be a finite number"),
        (["--min-agents", "-1"], "--min-agents must be between 0 and --agents"),
        (["--min-agents", "51"], "--min-agents must be between 0 and --agents"),
        (["--max-steps", "0"], "--max-steps must be >= 1"),
        (["--max-steps", "-3"], "--max-steps must be >= 1"),
        (["--step-timeout", "0"], "--step-timeout must be > 0"),
        (["--step-timeout", "-2"], "--step-timeout must be > 0"),
        (["--step-timeout", "inf"], "--step-timeout must be a finite number"),
        (["--policy-timeout", "-1"], "--policy-timeout must be >= 0"),
        (["--policy-timeout", "nan"], "--policy-timeout must be a finite number"),
        (["--arrivals", "-0.1"], "--arrivals must be >= 0"),
        (["--lifetime", "0"], "--lifetime must be >= 1"),
        (["--wave-size", "0"], "--wave-size must be >= 1"),
        (["--wave-delay", "-1"], "--wave-delay must be >= 0"),
        (["--status-every", "-1"], "--status-every must be >= 0"),
    )
    for extra, fragment in cases:
        with mock.patch.object(
            soak, "seed_everything", side_effect=AssertionError("startup ran")
        ):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    _parse(extra)
                except SystemExit as exc:
                    assert exc.code == 2, (extra, exc.code)
                else:
                    raise AssertionError(f"invalid setting accepted: {extra}")
        assert fragment in err.getvalue(), (extra, err.getvalue())
    print("SOAK_ARGS_INVALID_OK")


if __name__ == "__main__":
    test_defaults_and_accepted_boundaries()
    test_invalid_values_fail_before_startup()
    print("ALL_SOAK_ARGS_OK")
