"""Checkpoint provenance helper, shared by all agents (#53).

Reproducibility metadata embedded on every save: git SHA, config hash
(server_config.json + ml/ml_config.json), obs/action dims, timestamp.
Stdlib only and torch-free on purpose -- both ml_client (linear) and
torch_agents.dqn_agent import this, so it must load anywhere.
Best effort throughout: "unknown"/"missing" markers, never crashes.
"""

import hashlib
import os
import subprocess
import time


def checkpoint_version(obs_size, n_actions):
    here = os.path.dirname(os.path.abspath(__file__))
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
    h = hashlib.sha256()
    for p in (
        os.path.join(os.path.dirname(here), "server_config.json"),
        os.path.join(here, "ml_config.json"),
    ):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(f"missing:{os.path.basename(p)}".encode())
    return {
        "git_sha": sha,
        "config_hash": h.hexdigest()[:16],
        "obs_size": obs_size,
        "n_actions": n_actions,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def version_notes(saved, obs_size, n_actions):
    """Human-readable drift notes between a saved version dict and the
    live build (dims, git, config)."""
    if not saved:
        return []
    cur = checkpoint_version(obs_size, n_actions)
    notes = []
    if saved.get("obs_size") != obs_size or saved.get("n_actions") != n_actions:
        notes.append(
            f"dims {saved.get('obs_size')}/{saved.get('n_actions')} "
            f"vs current {obs_size}/{n_actions}"
        )
    if saved.get("git_sha") != cur["git_sha"]:
        notes.append(
            f"git {str(saved.get('git_sha'))[:8]} " f"vs current {cur['git_sha'][:8]}"
        )
    if saved.get("config_hash") != cur["config_hash"]:
        notes.append("config differs")
    return notes
