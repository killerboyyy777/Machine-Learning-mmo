"""Wheel runtime-asset checks (#773).

Run from the repo root:  python tests/test_wheel_assets.py

The static checks need only the standard library. When the optional `build`
package is importable, a wheel is built in a temporary copy of the checkout and
its archive is inspected. The offline CI job installs `build` so the archive
assertion runs on every pull request; local runs without it skip that part.
"""

import fnmatch
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "MANIFEST.in"

ASSETS = (
    "dashboard.html",
    "uplot.min.js",
    "world.json",
    "server_config.json",
    "plugins/README.md",
    "plugins/redteam.py",
    "ml/ml_config.json",
    "ml/conductor/soak_server_config.json",
)


def test_assets_exist():
    for rel in ASSETS:
        assert (ROOT / rel).is_file(), rel
    print("ASSETS_EXIST_OK")


def test_package_data_covers_assets():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["ml"]
    package_dir = (ROOT / "ml").resolve()
    for rel in ASSETS:
        target = str((ROOT / rel).resolve()).replace("\\", "/")
        matched = any(
            fnmatch.fnmatch(
                target, str((package_dir / pat).resolve()).replace("\\", "/")
            )
            for pat in patterns
        )
        assert matched, f"{rel} is not covered by a package-data pattern"
    print("PACKAGE_DATA_OK")


def test_manifest_includes_assets():
    text = MANIFEST.read_text(encoding="utf-8")
    for name in ("dashboard.html", "uplot.min.js", "world.json", "server_config.json"):
        assert f"include {name}" in text, name
    assert "recursive-include plugins *" in text
    assert "recursive-include ml *.json" in text
    print("MANIFEST_OK")


def _ignore_parts(directory, names):
    return {
        n for n in names if n in {".git", "balance", "build"} or n.endswith(".egg-info")
    }


def test_wheel_archive():
    if importlib.util.find_spec("build") is None:
        if os.environ.get("CI"):
            raise AssertionError("build is missing in CI; WHEEL_ARCHIVE_OK cannot run")
        print("WHEEL_BUILD_SKIPPED")
        return
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source"
        outdir = Path(tmp) / "dist"
        shutil.copytree(ROOT, source, ignore=_ignore_parts)
        outdir.mkdir()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(outdir),
                str(source),
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        wheels = list(outdir.glob("*.whl"))
        assert len(wheels) == 1, wheels
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
        missing = [rel for rel in ASSETS if rel not in names]
        assert not missing, f"wheel is missing runtime assets: {missing}"
    print("WHEEL_ARCHIVE_OK")


if __name__ == "__main__":
    test_assets_exist()
    test_package_data_covers_assets()
    test_manifest_includes_assets()
    test_wheel_archive()
    print("WHEEL_ASSETS_OK")
