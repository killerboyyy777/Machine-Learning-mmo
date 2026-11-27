"""Build and package one standalone server bundle (#841).

PyInstaller freezes server.py with the dashboard, world and config files.
The runtime hook keeps mutable state outside the frozen program. Run from
the repo root:

    python scripts/build_bundle.py --target linux-x86_64
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "server.py"
HOOK = ROOT / "scripts" / "bundle_runtime_hook.py"
WORK = ROOT / "build" / "bundle"
DIST = ROOT / "dist" / "bundle"

# Source path relative to the repo root, destination inside the bundle.
ASSETS = (
    ("dashboard.html", "."),
    ("uplot.min.js", "."),
    ("world.json", "."),
    ("server_config.json", "."),
    ("ml/ml_config.json", "ml"),
)

EXPECTED_SYSTEM = {
    "linux-x86_64": "Linux",
    "windows-x86_64": "Windows",
    "macos-arm64": "Darwin",
}


def resolve_version():
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag.startswith("v"):
        return tag[1:]
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def build_exe():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "small-text-mmo",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        "--runtime-hook",
        str(HOOK),
        "--hidden-import",
        "http.server",
        "--hidden-import",
        "ml.modes.arena",
    ]
    for rel, dest in ASSETS:
        cmd.extend(["--add-data", f"{ROOT / rel}{os.pathsep}{dest}"])
    cmd.append(str(ENTRY))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller failed with code {proc.returncode}")
    suffix = ".exe" if os.name == "nt" else ""
    exe = DIST / f"small-text-mmo{suffix}"
    if not exe.is_file():
        raise SystemExit(f"missing bundled executable: {exe}")
    return exe


def package(exe, target, version):
    folder = DIST / f"package-{target}"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    shutil.copy2(exe, folder / exe.name)
    (folder / "README.txt").write_text(
        "Small text MMO standalone server\n\n"
        "Run the executable in this folder, then open http://localhost:8766/.\n"
        "Game clients connect to ws://localhost:8765.\n"
        "Mutable state goes in the data folder beside the executable.\n"
        "Set TEXTMMO_DATA_DIR before starting to choose another folder.\n",
        encoding="ascii",
    )
    zip_path = DIST / f"small-text-mmo-{version}-{target}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum = DIST / f"{zip_path.name}.sha256"
    checksum.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    print(f"BUNDLE_ZIP {zip_path}")
    print(f"BUNDLE_SHA256 {checksum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(EXPECTED_SYSTEM))
    args = parser.parse_args()
    expected = EXPECTED_SYSTEM[args.target]
    if platform.system() != expected:
        raise SystemExit(
            f"target {args.target} needs {expected}, got {platform.system()}"
        )
    for rel, _ in ASSETS:
        if not (ROOT / rel).is_file():
            raise SystemExit(f"missing bundle asset: {rel}")
    exe = build_exe()
    package(exe, args.target, resolve_version())


if __name__ == "__main__":
    main()
