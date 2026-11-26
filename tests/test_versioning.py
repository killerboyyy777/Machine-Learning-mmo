"""Checkpoint provenance includes canonical world data (#994).

Run from the repo root: python tests/test_versioning.py
A fixture tree keeps the test away from the checkout's own config files.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ml import versioning  # noqa: E402


def _fixture(tmp):
    root = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(root, "ml"))
    world_path = os.path.join(root, "world.json")
    with open(os.path.join(root, "server_config.json"), "w") as fh:
        json.dump({"XP_BASE": 1}, fh)
    with open(os.path.join(root, "ml", "ml_config.json"), "w") as fh:
        json.dump({"gamma": 0.99}, fh)
    with open(world_path, "w") as fh:
        json.dump(
            {
                "rooms": {"town_square": {"npcs": [{"id": "guard", "hp": 10}]}},
                "recipes": {"bread": {"inputs": {"wheat": 2}}},
            },
            fh,
        )
    return root, world_path


def _edit_world(path):
    with open(path) as fh:
        world = json.load(fh)
    world["rooms"]["town_square"]["npcs"][0]["hp"] = 11
    with open(path, "w") as fh:
        json.dump(world, fh)


def test_world_change_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        root, world_path = _fixture(tmp)
        first = versioning.checkpoint_version(4, 2, root=root)
        assert len(first["config_hash"]) == 16 and len(first["world_hash"]) == 16
        _edit_world(world_path)
        second = versioning.checkpoint_version(4, 2, root=root)
        assert second["world_hash"] != first["world_hash"]
        assert second["config_hash"] == first["config_hash"]
        assert versioning.version_notes(first, 4, 2, root=root) == ["world differs"]
        # Control: the same world produces no drift note and a stable hash.
        assert versioning.version_notes(second, 4, 2, root=root) == []
        assert versioning.checkpoint_version(4, 2, root=root)["world_hash"] == (
            second["world_hash"]
        )
    print("WORLD_DRIFT_OK")


def test_world_hash_is_canonical():
    with tempfile.TemporaryDirectory() as tmp:
        root, world_path = _fixture(tmp)
        first = versioning.checkpoint_version(4, 2, root=root)
        with open(world_path) as fh:
            world = json.load(fh)
        with open(world_path, "w") as fh:
            fh.write(
                json.dumps(
                    {"recipes": world["recipes"], "rooms": world["rooms"]}, indent=4
                )
            )
        assert versioning.checkpoint_version(4, 2, root=root)["world_hash"] == (
            first["world_hash"]
        )
        assert versioning.version_notes(first, 4, 2, root=root) == []
    print("WORLD_CANONICAL_OK")


def test_missing_world_and_legacy_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        root, world_path = _fixture(tmp)
        present = versioning.checkpoint_version(4, 2, root=root)
        os.remove(world_path)
        missing = versioning.checkpoint_version(4, 2, root=root)
        assert missing["world_hash"] != present["world_hash"]
        assert missing["config_hash"] == present["config_hash"]
        # Pre-world_hash checkpoints have no field; they must not be flagged
        # as drift merely because the field was added.
        legacy = {
            "git_sha": missing["git_sha"],
            "config_hash": missing["config_hash"],
        }
        notes = versioning.version_notes(legacy, 4, 2, root=root)
        assert notes == [], notes
        # Current checkpoints do report the missing-world drift.
        assert "world differs" in versioning.version_notes(present, 4, 2, root=root)
    print("WORLD_LEGACY_OK")


if __name__ == "__main__":
    test_world_change_is_reported()
    test_world_hash_is_canonical()
    test_missing_world_and_legacy_checkpoint()
    print("ALL_VERSIONING_OK")
