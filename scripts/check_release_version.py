"""Preflight for tag-driven PyPI publishing (#1021).

A vX.Y.Z tag must carry the same version as pyproject.toml. Without this
check the publish job uploads the static version in the tree, which may
already exist on PyPI.
"""

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def project_version(pyproject):
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        return data["project"]["version"]
    except KeyError as exc:
        raise ValueError(f"{pyproject} has no project.version") from exc


def tag_version(tag):
    if not tag.startswith("v") or not tag[1:]:
        raise ValueError(f"tag {tag!r} is not a v-prefixed version tag")
    return tag[1:]


def check(tag, pyproject):
    """Return the pinned version, or raise ValueError when tag and file disagree."""
    tagged = tag_version(tag)
    pinned = project_version(pyproject)
    if tagged != pinned:
        raise ValueError(
            "tag does not match pyproject version: "
            f"{tag} carries {tagged}, {pyproject.name} has {pinned}"
        )
    return pinned


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="GITHUB_REF_NAME of the tag")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=ROOT / "pyproject.toml",
        help="pyproject.toml to compare (defaults to the repository copy)",
    )
    args = parser.parse_args(argv)
    try:
        pinned = check(args.tag, args.pyproject)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"RELEASE_VERSION_OK: {args.tag} matches pyproject version {pinned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
