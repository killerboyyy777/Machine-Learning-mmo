"""CI configuration checks (#748): structural assertions on workflow files.

Run from the repo root:  python tests/test_ci_config.py

Uses only the standard library. PyYAML is not a declared dependency, so these
are text and regex assertions rather than a YAML parse. The checks cover the
remainder of #748: job timeouts, test/** push triggers, Dependabot ecosystems,
the area:ci label rule, and the absence of mutable default-branch action refs.
#982 adds a check that the offline job runs the policy budget suite and builds
a wheel, so neither can silently drop out of CI.
#1038 adds a check that every tests/test_*.py script has a step in tests.yml.
#141 adds docs.yml and guards its three check names and pinned tool versions.
#1079 checks that the docs jobs run their checks and that lychee fails on
broken links. It also probes a mutated docs.yml copy as a subprocess.
#841 adds a check that the release gate builds and smoke tests a bundle before
its checksums reach the release. The tag/pyproject version guard from #1021 is
exercised as a subprocess.
"""

import os
import re
import shutil
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
    "docs.yml",
)


def _workflow(name):
    return (WF / name).read_text(encoding="utf-8")


def _docs_steps(text):
    steps = {}
    job = None
    collecting = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            job = header.group(1)
            steps[job] = []
            collecting = False
        elif line.startswith("    steps:"):
            collecting = True
        elif collecting and line.startswith("      "):
            if line.startswith("      -"):
                steps[job].append([line.strip()[2:]])
            elif steps[job]:
                steps[job][-1].append(line.strip())
        elif collecting:
            collecting = False
    return steps


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


def test_every_test_script_is_wired():
    scripts = {p.name for p in (ROOT / "tests").glob("test_*.py")}
    workflow = _workflow("tests.yml")
    wired = set(re.findall(r"python tests/(test_[\w]+\.py)", workflow))
    missing = sorted(scripts - wired)
    assert not missing, "test scripts without a CI step: " + ", ".join(missing)
    print("TEST_SCRIPT_COVERAGE_OK")


def test_docs_check_names_and_pins():
    text = _workflow("docs.yml")
    assert "name: Docs Check" in text
    for name in ("Link check (offline)", "Spelling (codespell)", "README render"):
        assert "name: " + name in text, name
    for job in ("links", "spell", "render"):
        assert re.search(r"^  " + job + r":$", text, re.MULTILINE), job
    assert '"**/*.md"' in text
    assert "lycheeverse/lychee-action@v2.9.0" in text
    assert "lycheeVersion: v0.24.2" in text
    assert "pip install codespell==2.4.1" in text
    assert "pip install markdown==3.8" in text

    steps = _docs_steps(text)
    links = [
        step
        for step in steps["links"]
        if any(line == "uses: lycheeverse/lychee-action@v2.9.0" for line in step)
    ]
    assert len(links) == 1, "lychee step missing or duplicated"
    args = [line for line in links[0] if line.startswith("args:")]
    assert len(args) == 1, "lychee args missing"
    assert "--offline" in args[0], "offline flag missing"
    assert "--include-fragments=anchor-only" in args[0], "anchor flag missing"
    assert "fail: true" in links[0], "lychee cannot fail on broken links"

    spell = [
        step
        for step in steps["spell"]
        if any(line == "run: codespell $(git ls-files '*.md')" for line in step)
    ]
    assert len(spell) == 1, "codespell run step missing or duplicated"
    print("DOCS_CHECK_NAMES_OK")


def test_docs_workflow_yaml():
    try:
        import yaml
    except ImportError:
        print("DOCS_YAML_PARSE_SKIPPED")
        return
    data = yaml.safe_load(_workflow("docs.yml"))
    assert isinstance(data, dict)
    triggers = data.get("on", data.get(True))
    assert "push" in triggers and "pull_request" in triggers
    assert "**/*.md" in triggers["pull_request"]["paths"]
    jobs = data["jobs"]
    assert set(jobs) == {"links", "spell", "render"}, set(jobs)
    for job in jobs.values():
        assert job["runs-on"] == "ubuntu-latest"
        assert job["timeout-minutes"] > 0
    print("DOCS_YAML_OK")


def test_docs_guard_rejects_disabled_checks(root=None):
    base = Path(root) if root is not None else ROOT
    with tempfile.TemporaryDirectory() as tmp:
        for sub in (".github", "tests", "scripts"):
            shutil.copytree(base / sub, Path(tmp) / sub)
        workflow = Path(tmp) / ".github" / "workflows" / "docs.yml"
        text = workflow.read_text(encoding="utf-8")
        run_step = "      - run: codespell $(git ls-files '*.md')\n"
        assert run_step in text, "probe setup: codespell step absent"
        text = text.replace(run_step, "")
        fail_line = "          fail: true"
        assert fail_line in text, "probe setup: lychee fail flag absent"
        text = text.replace(fail_line, "          fail: false")
        workflow.write_text(text, encoding="utf-8")
        env = dict(os.environ, DOCS_GUARD_PROBE="1")
        proc = subprocess.run(
            [sys.executable, "tests/test_ci_config.py"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "guard accepted disabled checks: " + proc.stdout
    print("DOCS_GUARD_REJECTION_OK")


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


def test_release_bundle_assets():
    text = _workflow("release.yml")
    assert re.search(r"^  bundle:\n", text, re.MULTILINE)
    assert "needs: tests" in text
    assert "scripts/build_bundle.py --target" in text
    assert "scripts/smoke_bundle.py --bundle-dir" in text
    assert "needs: [tests, bundle]" in text
    assert "files: bundles/*" in text
    assert "*.sha256" in text
    for script in (
        "scripts/build_bundle.py",
        "scripts/smoke_bundle.py",
        "scripts/bundle_runtime_hook.py",
    ):
        assert (ROOT / script).is_file(), script
    print("RELEASE_BUNDLE_OK")


if __name__ == "__main__":
    test_every_job_has_timeout()
    test_test_branch_triggers()
    test_dependabot_ecosystems()
    test_labeler_area_ci()
    test_no_mutable_branch_refs()
    test_offline_covers_policy_budget_and_wheel()
    test_every_test_script_is_wired()
    test_docs_check_names_and_pins()
    test_docs_workflow_yaml()
    if not os.environ.get("DOCS_GUARD_PROBE"):
        test_docs_guard_rejects_disabled_checks()
    test_release_version_guard()
    test_release_bundle_assets()
    print("CI_CONFIG_OK")
