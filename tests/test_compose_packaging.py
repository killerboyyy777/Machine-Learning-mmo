"""Static checks for the compose packaging fixes (#743, #744).

Run from the repo root:  python tests/test_compose_packaging.py

Docker is not available in every environment, so these checks read the
checked-in packaging files directly. No third-party YAML parser is needed.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def _active(text):
    """Return the non-comment, non-blank lines of a compose block."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _service(name):
    """Return the indented body of a top-level compose service."""
    lines = COMPOSE.splitlines()
    heading = f"  {name}:"
    start = next((i for i, line in enumerate(lines) if line == heading), None)
    assert start is not None, f"compose service {name!r} is missing"
    body = []
    for line in lines[start + 1 :]:
        if re.match(r"^  \S", line):
            break
        body.append(line)
    return body


def _command(body):
    """Return the scalar args of a compose service's command list."""
    args = []
    in_command = False
    for line in body:
        if line == "    command:":
            in_command = True
        elif in_command:
            match = re.fullmatch(r'      - "(.*)"', line)
            if match:
                args.append(match.group(1))
            elif line.strip() and not line.startswith("      "):
                break
    return args


# #743: the image must contain a file at the opt-in mount target. Docker
# otherwise creates a directory there and save_scores rotation fails.
docker_lines = DOCKERFILE.splitlines()
assert "WORKDIR /app" in docker_lines, "Dockerfile lost /app workdir"
copy_end = max(i for i, line in enumerate(docker_lines) if line.startswith("COPY "))
seeds = [
    i
    for i, line in enumerate(docker_lines)
    if re.fullmatch(r"RUN printf '([^']*)' > scores\.json", line)
]
assert len(seeds) == 1, "Dockerfile must seed scores.json exactly once"
assert seeds[0] > copy_end, "seed must follow the COPY block"
payload = re.fullmatch(
    r"RUN printf '([^']*)' > scores\.json", docker_lines[seeds[0]]
).group(1)
assert json.loads(payload.replace("\\n", "\n")) == {}, "seed must be empty JSON"
assert "server-scores:/app/scores.json" in COMPOSE, "opt-in volume target changed"
print("DOCKERFILE_SEED_OK")

# #744: soak joins the server netns so the loopback-only GM gate accepts it.
soak = _service("soak")
soak_active = _active("\n".join(soak))
args = _command(soak)
assert any(
    line.startswith("    network_mode: service:server") for line in soak_active
), "soak left the server netns"
assert [a for a in args if a.startswith("ws://")] == [
    "ws://127.0.0.1:8765",
    "ws://127.0.0.1:8767",
], "soak URLs must use loopback"
assert "ml/conductor/soak.py" in args, "soak command changed"
assert not any("ws://server:" in a for a in args), "service-name URL remains"

server_active = _active("\n".join(_service("server")))
publishes = []
for line in server_active:
    match = re.match(r'^\s+- "(\d+:\d+)"', line)
    if match:
        publishes.append(match.group(1))
assert {"8765:8765", "8766:8766"} <= set(publishes), publishes
assert not any(p.startswith("8767:") for p in publishes), "GM published to host"
print("SOAK_LOOPBACK_OK")

# #759: every service carries explicit memory and CPU limits.
for name in ("server", "botfarm", "soak", "torch-farm"):
    active = _active("\n".join(_service(name)))
    limits = {}
    for line in active:
        match = re.match(r"^    (mem_limit|cpus):\s*(\S+)", line)
        if match:
            limits[match.group(1)] = match.group(2)
    assert "mem_limit" in limits and "cpus" in limits, (name, limits)
    assert re.fullmatch(r"\d+(?:m|g)", limits["mem_limit"]), (name, limits)
    assert float(limits["cpus"]) > 0, (name, limits)
print("COMPOSE_LIMITS_OK")

# The documented compose command still resolves to the soak service.
command = "docker compose --profile soak up soak"
assert command in README, "README dropped the soak command"
assert command in COMPOSE, "compose header dropped the soak command"
assert any(line.startswith('    profiles: ["soak"]') for line in soak_active)
assert "        condition: service_healthy" in soak_active, "health gate changed"
for phrase in (
    "network_mode: service:server",
    "ws://127.0.0.1:8767",
    "loopback-only GM gate",
):
    assert phrase in README, f"README no longer explains {phrase}"
assert "- run: python tests/test_compose_packaging.py" in WORKFLOW, "not in CI"
print("COMPOSE_COMMAND_OK")

# #816: the image must ship the top-level trees imported by probes, the
# conductor and the botfarm entry point.
for package in ("harness", "plugins"):
    assert (
        f"COPY {package}/ ./{package}/" in docker_lines
    ), f"Dockerfile lost COPY {package}/"
    assert (ROOT / package).is_dir(), f"{package}/ missing from repo"

botfarm_args = _command(_service("botfarm"))
assert "ml/ml_botfarm.py" in botfarm_args, "botfarm entry point changed"
entry = ROOT / "ml" / "ml_botfarm.py"
assert entry.is_file(), "botfarm entry point missing"
botfarm_source = entry.read_text(encoding="utf-8")
assert re.search(
    r"^\s*from plugins\.scripted import", botfarm_source, re.M
), "botfarm no longer uses the plugins.scripted fallback"

for name in ("arena_probe.py", "guild_probe.py"):
    probe_source = (ROOT / "ml" / name).read_text(encoding="utf-8")
    assert re.search(
        r"^from harness\.bridge import", probe_source, re.M
    ), f"{name} no longer uses harness.bridge"

for relative in ("harness/bridge.py", "ml/plugins/scripted.py"):
    assert (ROOT / relative).is_file(), f"{relative} missing from repo"
print("DOCKERFILE_HARNESS_PLUGINS_OK")

print("ALL_COMPOSE_PACKAGING_OK")
