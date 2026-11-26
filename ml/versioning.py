"""Checkpoint provenance helper, shared by all agents (#53).

Reproducibility metadata embedded on every save: git SHA, config hash
(server_config.json + ml/ml_config.json), canonical world.json hash,
obs/action dims, timestamp. The world hash is a separate field so old
checkpoints keep their config hash and are treated as world-compatible.
Stdlib only and torch-free on purpose -- both ml_client (linear) and
torch_agents.dqn_agent import this, so it must load anywhere.
Best effort throughout: "unknown"/"missing" markers, never crashes.
"""

import hashlib
import json
import os
import subprocess
import time


def _data_paths(root=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return (
        os.path.join(root, "server_config.json"),
        os.path.join(root, "ml", "ml_config.json"),
        os.path.join(root, "world.json"),
    )


def _world_hash(path):
    """Hash canonical world content, ignoring key order and whitespace."""
    h = hashlib.sha256()
    try:
        with open(path, encoding="utf-8") as f:
            world = json.load(f)
    except (OSError, ValueError):
        h.update(b"missing:world.json")
    else:
        h.update(
            json.dumps(
                world, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        )
    return h.hexdigest()[:16]


def checkpoint_version(obs_size, n_actions, root=None):
    sha = "unknown"
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError) as e:
        print(f"checkpoint_version: git lookup failed ({e}); using 'unknown'")
    server_path, ml_path, world_path = _data_paths(root)
    h = hashlib.sha256()
    for p in (server_path, ml_path):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(f"missing:{os.path.basename(p)}".encode())
    return {
        "git_sha": sha,
        "config_hash": h.hexdigest()[:16],
        "world_hash": _world_hash(world_path),
        "obs_size": obs_size,
        "n_actions": n_actions,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def version_notes(saved, obs_size, n_actions, root=None):
    """Human-readable drift notes between a saved version dict and the
    live build (git, config, world). Dims are deliberately absent: both
    loaders enforce them structurally before this is ever called, so a
    dims note could never print. Checkpoints saved before world_hash
    existed carry no field and are assumed compatible; config_hash is
    unchanged so their older provenance stays comparable."""
    if not saved:
        return []
    cur = checkpoint_version(obs_size, n_actions, root=root)
    notes = []
    if saved.get("git_sha") != cur["git_sha"]:
        notes.append(
            f"git {str(saved.get('git_sha'))[:8]} " f"vs current {cur['git_sha'][:8]}"
        )
    if saved.get("config_hash") != cur["config_hash"]:
        notes.append("config differs")
    if saved.get("world_hash") and saved["world_hash"] != cur["world_hash"]:
        notes.append("world differs")
    return notes
