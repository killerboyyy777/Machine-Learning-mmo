"""Telemetry export for the conductor/soak path (#73).

OpenTelemetry-shaped JSONL samples plus an aggregate JSON report, written
with the standard library only. Off by default: no directory, no file and
no WebSocket ping when the collector is not enabled.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 1
METRIC_NAMES = ("agent_step_ms", "env_reset_ms", "ws_rtt_ms")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def rtt_ms(seconds):
    return round(float(seconds) * 1000.0, 3)


def _percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    index = min(
        len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1))))
    )
    return sorted_values[index]


def summarize(samples, enabled=True):
    metrics = {}
    for name in METRIC_NAMES:
        values = sorted(
            sample["value_ms"] for sample in samples if sample.get("event") == name
        )
        if not values:
            continue
        count = len(values)
        metrics[name] = {
            "count": count,
            "mean_ms": round(sum(values) / count, 3),
            "p50_ms": round(_percentile(values, 0.50), 3),
            "p95_ms": round(_percentile(values, 0.95), 3),
            "max_ms": round(values[-1], 3),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": _utc_now(),
        "enabled": bool(enabled),
        "metrics": metrics,
    }


def _write_json(path, obj):
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = os.path.abspath(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, sort_keys=True, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return os.path.abspath(path)


class Telemetry:
    """Collect raw timing samples and emit a shareable aggregate report."""

    def __init__(self, enabled, path=None):
        self.enabled = bool(enabled)
        self.path = path
        self.samples = []

    def record(self, metric, value_ms, agent_id=None, episode=None, step=None):
        if not self.enabled:
            return
        try:
            row = {
                "schema_version": SCHEMA_VERSION,
                "event": metric,
                "value_ms": round(float(value_ms), 3),
                "_ts": time.time(),
            }
            if agent_id is not None:
                row["agent_id"] = agent_id
            if episode is not None:
                row["episode"] = episode
            if step is not None:
                row["step"] = step
            self.samples.append(row)
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:
            pass

    def write_jsonl(self):
        if self.path is None:
            return None
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = os.path.abspath(self.path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                for row in self.samples:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            pass
        return os.path.abspath(self.path)

    def flush(self):
        return self.write_jsonl()

    def write_report(self, path):
        return _write_json(path, summarize(self.samples, enabled=self.enabled))


async def record_ws_rtt(telemetry, ws, agent_id=None):
    if telemetry is None:
        return
    try:
        rtt = await (await ws.ping())
        telemetry.record("ws_rtt_ms", rtt_ms(rtt), agent_id=agent_id)
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Telemetry aggregate report.")
    parser.add_argument("--input", required=True, help="raw telemetry.jsonl samples")
    parser.add_argument("--out", required=True, help="aggregate telemetry.json path")
    args = parser.parse_args(argv)
    samples = []
    try:
        with open(args.input, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    samples.append(row)
    except OSError:
        pass
    print(_write_json(args.out, summarize(samples)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
