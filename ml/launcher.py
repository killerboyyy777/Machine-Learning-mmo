"""One-click training launches (#64, control plane for #177).

Builds the argv for a trainer process from a validated request. The dashboard
never assembles a command line itself: it sends field values, and this module
decides what argv those values produce. That split is the security boundary --
the request carries no flags, no program name and no free-form string, so a
caller cannot smuggle a second command past validation.

Validation is strict on purpose: unknown keys are rejected instead of ignored,
because an ignored typo would silently train with a default the operator did
not choose. Checkpoints are the one path-shaped value, so they are resolved
against the repo root and refused if they land outside it.

Nothing here spawns anything; the caller owns the process. That keeps this
module importable and unit-testable without a running server.
"""

import ntpath
import os
from os.path import abspath, basename, commonpath, isabs, join, normcase

# The trainer this panel drives. Its flags are the ones #64 names: a
# checkpoint to resume from, a seed, and an agent count. --steps 0 means
# "run until stopped", which is what makes it a service rather than a
# one-shot soak.
TRAINER = "torch_farm"
TRAINER_SCRIPT = ("torch_agents", "torch_farm.py")

# gitignored by convention (.gitignore lists both names), so a checkpoint
# written by a previous run shows up here without anyone configuring a path.
DEFAULT_WEIGHTS = join("torch_agents", "ml_weights.json")
DEFAULT_BEST_WEIGHTS = join("torch_agents", "ml_best.json")

# Bound the shape of the fleet. A typo like agents=4000 would otherwise open
# 4000 websockets against the game server from a dashboard click.
MAX_AGENTS = 32
MAX_STEPS = 100_000_000

# Field order here is the order the dashboard renders the form in.
FIELDS = (
    {
        "key": "agents",
        "flag": "--agents",
        "type": "int",
        "default": 4,
        "min": 1,
        "max": MAX_AGENTS,
        "help": "concurrent agents training in this world",
    },
    {
        "key": "steps",
        "flag": "--steps",
        "type": "int",
        "default": 0,
        "min": 0,
        "max": MAX_STEPS,
        "help": "shared training steps; 0 runs until Stop is pressed",
    },
    {
        "key": "seed",
        "flag": "--seed",
        "type": "int",
        "default": 7,
        "min": 0,
        "max": 2**31 - 1,
        "help": "RNG seed for python and torch, recorded in the run record",
    },
    {
        "key": "weights",
        "flag": "--weights",
        "type": "path",
        "default": DEFAULT_WEIGHTS,
        "help": "checkpoint to resume from and keep writing",
    },
    {
        "key": "best_weights",
        "flag": "--best-weights",
        "type": "path",
        "default": DEFAULT_BEST_WEIGHTS,
        "help": "checkpoint that only ever moves when the score improves",
    },
)

# One click, one decision already made. A preset stages values into the form
# for review; it never launches by itself.
PRESETS = (
    {
        "name": "Quick probe",
        "help": "2 agents, 2000 steps. Does the pipeline work at all?",
        "values": {"agents": 2, "steps": 2000, "seed": 1},
    },
    {
        "name": "Balanced",
        "help": "4 agents, unlimited. The default fleet.",
        "values": {"agents": 4, "steps": 0, "seed": 7},
    },
    {
        "name": "Full fleet",
        "help": "16 agents, unlimited. Heaviest thing the box is asked to run.",
        "values": {"agents": 16, "steps": 0, "seed": 11},
    },
)

KEYS = frozenset(f["key"] for f in FIELDS)


def field(key):
    for spec in FIELDS:
        if spec["key"] == key:
            return spec
    return None


def defaults(repo_root=None):
    """Every field at its default, as the dashboard's initial form state."""
    return {f["key"]: f["default"] for f in FIELDS}


def _inside(repo_root, path):
    """True when path resolves to somewhere under repo_root."""
    root = normcase(abspath(repo_root))
    target = normcase(abspath(join(root, path) if not isabs(path) else path))
    try:
        return commonpath([root, target]) == root
    except ValueError:
        # Different drives on Windows: no shared prefix, so not inside.
        return False


def resolve_checkpoint(repo_root, path):
    """(abs_path, error) for a checkpoint path the operator supplied.

    Relative paths are taken against the repo root, never against the
    dashboard's working directory, so the same value means the same file
    however the server was started. Traversal out of the repo is refused:
    this value names a file the trainer will overwrite.
    """
    if not isinstance(path, str) or not path.strip():
        return None, "checkpoint path must be a non-empty string"
    text = path.strip()
    # os.path.isabs only knows this host's flavour: on POSIX a "C:/..." drive
    # path reads as relative and would slip through, so test the Windows
    # flavour too and refuse a drive-relative "C:name". The server may run on
    # either OS (CI is Linux, operators are usually on Windows).
    if isabs(text) or ntpath.isabs(text) or (len(text) > 1 and text[1] == ":"):
        return None, "checkpoint path must be relative to the repo root"
    if not _inside(repo_root, text):
        return None, "checkpoint path must stay inside the repo"
    return abspath(join(abspath(repo_root), text)), ""


def validate(request, repo_root):
    """(values, error). Rejects unknown keys instead of dropping them."""
    if not isinstance(request, dict):
        return None, "body must be a JSON object"
    unknown = sorted(set(request) - KEYS)
    if unknown:
        return None, "unknown field(s): " + ", ".join(unknown)

    values = defaults(repo_root)
    # An omitted path must land absolute like a supplied one, or the argv would
    # carry a relative default while every explicit value is rooted. Same
    # resolve, same refusal, whichever way the value arrived.
    for spec in FIELDS:
        if spec["type"] != "path" or spec["key"] in request:
            continue
        resolved, error = resolve_checkpoint(repo_root, values[spec["key"]])
        if error:
            return None, f"{spec['key']}: {error}"
        values[spec["key"]] = resolved
    for key, raw in request.items():
        spec = field(key)
        if spec["type"] == "int":
            # bool is an int subclass, and True would silently mean 1.
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None, f"{key} must be a whole number"
            if not spec["min"] <= raw <= spec["max"]:
                return None, f"{key} must be {spec['min']}-{spec['max']}"
            values[key] = raw
        else:
            resolved, error = resolve_checkpoint(repo_root, raw)
            if error:
                return None, f"{key}: {error}"
            values[key] = resolved
    return values, ""


def checkpoints(repo_root):
    """Checkpoint files an operator can pick from, newest first.

    Read-only scan of the repo's own conventional locations. Returns
    repo-relative paths so nothing absolute is ever handed to a client.
    """
    found = []
    for folder in ("torch_agents", "ml", "checkpoints"):
        directory = join(abspath(repo_root), folder)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith((".pt", ".pth", ".json")):
                continue
            rel = join(folder, name).replace("\\", "/")
            try:
                mtime = os.path.getmtime(join(directory, name))
            except OSError:
                continue
            found.append({"path": rel, "name": name, "mtime": mtime})
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found


def build_argv(values, repo_root, ws_url=None):
    """The argv list for a launch. No shell, no interpolation, no quoting.

    ws_url defaults to the game server the dashboard is being served from, so
    "Start Training" trains agents against the world the operator is looking
    at rather than whatever localhost happened to mean.
    """
    argv = [os.path.join(*TRAINER_SCRIPT)]
    if ws_url:
        argv += ["--url", ws_url]
    for spec in FIELDS:
        argv += [spec["flag"], str(values[spec["key"]])]
    # Only the script path is made absolute. Flag values that are already
    # absolute (checkpoints, resolved against the root by validate) must not
    # be re-rooted, and the flags themselves are literal.
    return [os.path.join(abspath(repo_root), argv[0])] + argv[1:]


def describe():
    """The schema the dashboard renders its form from."""
    return {
        "trainer": TRAINER,
        "fields": [
            {
                "key": f["key"],
                "type": f["type"],
                "default": f["default"],
                "min": f.get("min"),
                "max": f.get("max"),
                "help": f["help"],
            }
            for f in FIELDS
        ],
        "presets": list(PRESETS),
    }


def label(argv):
    """A short human summary of a launch, safe to show a client.

    Checkpoints arrive here already resolved to absolute paths, so they are
    reduced to a basename: a dashboard is reachable from the LAN, and no
    absolute path should leave this process.
    """
    parts = [basename(argv[0])]
    index = 1
    while index < len(argv):
        part = argv[index]
        if part.endswith((".pt", ".pth", ".json")):
            parts.append(basename(part))
        else:
            parts.append(part)
        index += 1
    return " ".join(parts)
