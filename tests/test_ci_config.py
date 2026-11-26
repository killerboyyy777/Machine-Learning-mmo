"""CI configuration checks (#748): structural assertions on workflow files.

Run from the repo root:  python tests/test_ci_config.py

Uses only the standard library. PyYAML is not a declared dependency, so these
are text and regex assertions rather than a YAML parse. The checks cover the
remainder of #748: job timeouts, test/** push triggers, Dependabot ecosystems,
the area:ci label rule, and the absence of mutable default-branch action refs.
The tag/pyproject version guard from #1021 is exercised as a subprocess.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
LABELER = ROOT / ".github" / "labeler.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
GUARD = ROOT / "scripts" / "check_release_version.py"

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


def test_release_version_guard():
    text = _workflow("release.yml")
    assert 'python scripts/check_release_version.py --tag "$GITHUB_REF_NAME"' in text
    assert text.index("check_release_version.py") < text.index("python -m build")
    assert "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text

    # A fixed fixture keeps the check independent of the version currently
    # pinned in the repo, which is bumped before a release tag is cut.
    def run(tag):
        return subprocess.run(
            [sys.executable, str(GUARD), "--tag", tag, "--pyproject", str(pyproject)],
            capture_output=True,
            text=True,
        )

    with tempfile.TemporaryDirectory() as tmp:
        pyproject = Path(tmp) / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "0.41.1"\n', encoding="utf-8")

        match = run("v0.41.1")
        assert match.returncode == 0, match.stderr
        assert "RELEASE_VERSION_OK" in match.stdout and "0.41.1" in match.stdout

        mismatch = run("v0.41.2")
        assert mismatch.returncode == 1, mismatch.stdout
        assert "tag does not match pyproject version" in mismatch.stderr
        assert "v0.41.2" in mismatch.stderr and "0.41.1" in mismatch.stderr
        assert not mismatch.stdout

        malformed = run("0.41.1")
        assert malformed.returncode == 1
        assert "v-prefixed version tag" in malformed.stderr
    print("RELEASE_VERSION_GUARD_OK")


if __name__ == "__main__":
    test_every_job_has_timeout()
    test_test_branch_triggers()
    test_dependabot_ecosystems()
    test_labeler_area_ci()
    test_no_mutable_branch_refs()
    test_release_version_guard()
    print("CI_CONFIG_OK")
