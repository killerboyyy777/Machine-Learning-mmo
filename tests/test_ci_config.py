"""CI configuration checks (#748): structural assertions on workflow files.

Run from the repo root:  python tests/test_ci_config.py

Uses only the standard library. PyYAML is not a declared dependency, so these
are text and regex assertions rather than a YAML parse. The checks cover the
remainder of #748: job timeouts, test/** push triggers, Dependabot ecosystems,
the area:ci label rule, and the absence of mutable default-branch action refs.
#982 adds a check that the offline job runs the policy budget suite and builds
a wheel, so neither can silently drop out of CI.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
LABELER = ROOT / ".github" / "labeler.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

WORKFLOWS = (
    "tests.yml",
    "security.yml",
    "security-weekly.yml",
    "release.yml",
    "docker.yml",
    "docker-publish.yml",
    "soak.yml",
    "label.yml",
    "automerge.yml",
)


def _workflow(name):
    return (WF / name).read_text(encoding="utf-8")


def test_every_job_has_timeout():
    for name in WORKFLOWS:
        text = _workflow(name)
        runs = len(re.findall(r"^\s+runs-on:", text, re.MULTILINE))
        timeouts = len(re.findall(r"^\s+timeout-minutes:", text, re.MULTILINE))
        assert runs == timeouts, (name, runs, timeouts)
        assert runs > 0, name
    print("TIMEOUTS_OK")


def test_test_branch_triggers():
    for name in ("tests.yml", "docker.yml", "security.yml"):
        text = _workflow(name)
        assert '"test/**"' in text, name
        assert re.search(r"branches:.*test/\*\*", text), name
    assert '"test/**"' not in _workflow("docker-publish.yml")
    print("BRANCH_TRIGGERS_OK")


def test_dependabot_ecosystems():
    text = DEPENDABOT.read_text(encoding="utf-8")
    for eco in ("pip", "github-actions", "docker"):
        assert "package-ecosystem:" in text and eco in text, eco
    assert ">=" in text and "< 3.16" in text
    print("DEPENDABOT_OK")


def test_labeler_area_ci():
    text = LABELER.read_text(encoding="utf-8")
    assert "area:ci:" in text
    assert ".github/workflows/**" in text
    assert ".github/dependabot.yml" in text
    assert text.count("README.md") == 1, text.count("README.md")
    print("LABELER_OK")


def test_no_mutable_branch_refs():
    for name in WORKFLOWS:
        text = _workflow(name)
        assert "@main" not in text, name
        assert "@master" not in text, name
    print("NO_MUTABLE_REFS_OK")


def test_offline_covers_policy_budget_and_wheel():
    offline = _workflow("tests.yml").split("  live:", 1)[0]
    assert "python tests/test_policy_budget.py" in offline
    assert "python tests/test_wheel_assets.py" in offline
    assert re.search(r"pip install[^\n]*build==", offline), "build not installed"
    assert re.search(
        r"pip install[^\n]*build==[^\n]*setuptools==", offline
    ), "wheel backend not installed"
    print("OFFLINE_COVERAGE_OK")


if __name__ == "__main__":
    test_every_job_has_timeout()
    test_test_branch_triggers()
    test_dependabot_ecosystems()
    test_labeler_area_ci()
    test_no_mutable_branch_refs()
    test_offline_covers_policy_budget_and_wheel()
    print("CI_CONFIG_OK")
