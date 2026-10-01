"""Run records: a durable index of training invocations (dashboard #65).

Before this, nothing in the repo identified "this training run".  Checkpoints
are overwritten in place (ml_weights.json / ml_farm_weights.json), the
per-step progress series only ever reached stdout, hyper-parameters were never
persisted, and no seed was ever recorded.  Comparing two runs therefore
meant keeping notes by hand.

A run record is one small directory per invocation:

    runs/<run_id>/run.json      identity, provenance, latest metric values
    runs/<run_id>/metrics.jsonl one appended line per sample

`run_id` is a local-time stamp (20261001-091530, disambiguated with -2, -3)
so it sorts chronologically as a string and reads well in a table.  Writers
append samples while training; the dashboard reads the index fresh, which is
what makes a finished run comparable to the one that is still going.

Stdlib only, and a flat module in ml/ like versioning.py, so server.py and
both trainers can import it without torch.
"""

import json
import os
import random
import time

RUNS_DIRNAME = "runs"
MANIFEST_NAME = "run.json"
SAMPLES_NAME = "metrics.jsonl"

# Rank direction for the dashboard's "which run is best" verdict.  Score-like
# totals want to be high; errors want to be low.  Anything unlisted counts as
# higher-is-better, so a new metric shows a number instead of vanishing.
LOWER_IS_BETTER = ("loss", "error", "mae", "rmse", "perplexity", "divergence")

METRIC_ORDER = (
    "score",
    "fitness",
    "best_score",
    "reward",
    "score_hr",
    "episodes",
    "steps",
    "training_steps",
    "duration",
)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_root():
    return os.path.join(repo_root(), RUNS_DIRNAME)


def higher_is_better(field):
    return not any(tok in str(field).lower() for tok in LOWER_IS_BETTER)


def _utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)) if epoch else ""


def _atomic_write_json(path, obj):
    # Same tmp+os.replace dance as scores.json and the DQN checkpoint: a
    # dashboard read must never catch a half-written manifest.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True) + "\n")


def new_run_id(root):
    base = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 1000):
        rid = base if n == 1 else f"{base}-{n}"
        if not os.path.exists(os.path.join(root, rid)):
            return rid
    return f"{base}-{int(time.time() * 1000)}"


def valid_run_id(run_id):
    """Accept only the shape new_run_id() mints: YYYYMMDD-HHMMSS with an
    optional -N suffix.

    Run ids reach the reader from a dashboard query string and are then joined
    onto the runs root, so this is the only thing standing between
    "?runs=../../.." and a file read outside the index.  Enforced inside
    read_run/read_samples rather than at the call site so a new caller cannot
    forget it.
    """
    s = str(run_id or "")
    if len(s) < 15 or not s.isascii():
        return False
    if not (s[:8].isdigit() and s[8] == "-" and s[9:15].isdigit()):
        return False
    return len(s) == 15 or (s[15] == "-" and s[16:].isdigit())


def seed_everything(seed):
    """Seed python RNG and, when torch is already importable, its RNG too.

    Torch is imported lazily and never required: the linear trainers and
    server.py call this too, and a hard torch dependency would drag the
    whole install into the dashboard process.
    """
    if seed is None:
        return False
    random.seed(seed)
    try:
        import torch
    except ImportError:
        return True
    try:
        torch.manual_seed(int(seed))
    except (RuntimeError, TypeError, ValueError):
        return True
    if getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
        torch.cuda.manual_seed_all(int(seed))
    return True


def rate_per_hour(total, elapsed):
    """Score-per-hour.  Returns None (not 0) while elapsed is unusable."""
    if total is None or not elapsed or elapsed <= 0:
        return None
    return round(float(total) * 3600.0 / float(elapsed), 3)


class Run:
    """One training invocation.  Use as a context manager so a crash is
    recorded as status=failed instead of leaving a permanent 'running'."""

    def __init__(self, root, manifest):
        self.root = root
        self.manifest = manifest
        self.path = os.path.join(root, manifest["run_id"])
        os.makedirs(self.path, exist_ok=True)
        self._write()

    # -- internals ---------------------------------------------------------
    @property
    def run_id(self):
        return self.manifest["run_id"]

    @property
    def samples_path(self):
        return os.path.join(self.path, SAMPLES_NAME)

    def _write(self):
        m = self.manifest
        ended = m.get("ended") or 0.0
        m["elapsed"] = round((ended or time.time()) - m["started"], 3)
        _atomic_write_json(os.path.join(self.path, MANIFEST_NAME), m)

    # -- writer API --------------------------------------------------------
    def note(self, **fields):
        """Set manifest fields (checkpoint paths, hparams, free-form notes)
        without touching the metric log."""
        self.manifest.update(fields)
        self._write()
        return self

    def record(self, **metrics):
        """Append one sample and fold it into the manifest's latest values."""
        sample = {"_ts": round(time.time(), 3)}
        sample.update(metrics)
        _append_jsonl(self.samples_path, sample)
        self.manifest["metrics"].update(metrics)
        self.manifest["elapsed"] = round(time.time() - self.manifest["started"], 3)
        if "score" in metrics:
            hr = rate_per_hour(metrics["score"], self.manifest["elapsed"])
            if hr is not None:
                self.manifest["metrics"]["score_hr"] = hr
        self._write()
        return sample

    def finish(self, status="finished", **extra):
        """Close the run.  Numeric extras are metrics and land in the same bag
        record() writes, so a final summary cannot end up outside the field
        list the dashboard ranks on; everything else (paths, labels) stays a
        manifest field."""
        for key, val in extra.items():
            if (
                val is None
                or isinstance(val, (int, float))
                and not isinstance(val, bool)
            ):
                self.manifest["metrics"][key] = val
            else:
                self.manifest[key] = val
        self.manifest["status"] = status
        self.manifest["ended"] = round(time.time(), 3)
        self.manifest["ended_at"] = _utc(self.manifest["ended"])
        self._write()
        return self.manifest

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        if exc_type is None:
            self.finish("finished")
        else:
            self.finish("failed", error=f"{exc_type.__name__}: {exc}")
        return False


def _provenance():
    """git SHA + config hash, borrowed from versioning.checkpoint_version.

    Only those two fields are kept: the obs/action sizes that helper also
    returns describe a checkpoint, and a run record is not one.  Best-effort
    so a run still records when git is absent or the helper cannot be found:
    a missing git, an unreadable config and an unimportable helper are all
    reasons to lose provenance, never reasons to lose the run.
    """
    try:
        try:
            from versioning import checkpoint_version
        except ImportError:
            # soak.py imports this module as ml.runlog, which leaves ml/ off
            # sys.path; without the fallback its runs record no provenance.
            from ml.versioning import checkpoint_version

        prov = checkpoint_version(0, 0)
        return {
            "git_sha": prov.get("git_sha", ""),
            "config_hash": prov.get("config_hash", ""),
        }
    except Exception:  # noqa: BLE001 - see the docstring
        return {"git_sha": "", "config_hash": ""}


def start_run(kind, root=None, label="", seed=None, hparams=None, **fields):
    """Open a run record.  `kind` names the trainer (torch_farm, dqn,
    ml_botfarm, ml_client, soak).  `hparams` is the argparse namespace, which
    is the only record of the CLI knobs that shape a run."""
    root = root or default_root()
    os.makedirs(root, exist_ok=True)
    now = time.time()
    manifest = {
        "run_id": new_run_id(root),
        "kind": str(kind),
        "label": str(label or ""),
        "status": "running",
        "seed": seed,
        "started": round(now, 3),
        "started_at": _utc(now),
        "ended": 0.0,
        "ended_at": "",
        "elapsed": 0.0,
        "checkpoint": "",
        "best_checkpoint": "",
        "error": "",
        "hparams": _jsonable(hparams or {}),
        "metrics": {},
    }
    manifest.update(_provenance())
    manifest.update(fields)
    return Run(root, manifest)


def _jsonable(value):
    """argparse namespaces hold paths and None; the manifest must be plain
    JSON because the dashboard reads it with no knowledge of argparse."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# -- reader API ------------------------------------------------------------
def read_run(root, run_id):
    if not valid_run_id(run_id):
        return None
    try:
        with open(os.path.join(root, run_id, MANIFEST_NAME), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_samples(root, run_id):
    out = []
    if not valid_run_id(run_id):
        return out
    try:
        with open(os.path.join(root, run_id, SAMPLES_NAME), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn final line from a killed writer
    except OSError:
        return []
    return out


def list_runs(root=None):
    """Every run in the index, newest first.  A directory without a readable
    manifest, or a run.json from an older shape, is skipped rather than
    allowed to break the whole tab."""
    root = root or default_root()
    runs = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        if not os.path.isdir(os.path.join(root, name)):
            continue
        m = read_run(root, name)
        if not isinstance(m, dict):
            continue
        m.setdefault("run_id", name)
        m.setdefault("metrics", {})
        # A run that was never closed (killed trainer) still has to age.
        if not m.get("ended"):
            m["elapsed"] = round(time.time() - (m.get("started") or time.time()), 3)
        runs.append(m)
    runs.sort(
        key=lambda r: (r.get("started") or 0, r.get("run_id") or ""), reverse=True
    )
    return runs


def sample_fields(runs):
    """Metric names seen across the index, ordered so the score-like ones lead.

    Manifests only: record() folds every sample into the manifest's latest
    values, so the manifests already know every field a run ever logged.
    """
    seen = set()
    for r in runs:
        seen.update((r.get("metrics") or {}).keys())
    names = [f for f in METRIC_ORDER if f in seen]
    names += sorted(f for f in seen if f not in names and not f.startswith("_"))
    return names


def field_meta(runs):
    return [
        {"name": f, "higher_is_better": higher_is_better(f)}
        for f in sample_fields(runs)
    ]


def runs_payload(root=None, ids=None):
    """Everything the Runs tab needs in one read.  `series` (the per-sample
    history behind the comparison chart) is only included for the runs the
    client asked about, so a long history of cheap runs stays cheap."""
    root = root or default_root()
    runs = list_runs(root)
    payload = {
        "root": root,
        "generated": round(time.time(), 3),
        "runs": runs,
        "fields": field_meta(runs),
    }
    if ids:
        payload["series"] = {i: read_samples(root, i) for i in ids}
    return payload
