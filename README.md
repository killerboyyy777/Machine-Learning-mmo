# Small Text MMO Engine

[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

[![BTC Donate](https://img.shields.io/badge/BTC-Donate-f7931a?logo=bitcoin&style=flat-square)](https://www.blockchain.com/explorer/addresses/btc/bc1qmkv939k2wqsej657cxj25ppwqdh65y2umnv3gg)
[![LTC Donate](https://img.shields.io/badge/LTC-Donate-a6a9aa?logo=litecoin&style=flat-square)](https://live.blockcypher.com/ltc/address/ltc1qjr49nr028mcajlt7prmmnqnjh0552qjj90zdq4)
[![Steam Donate](https://img.shields.io/badge/Steam-Donate-000000?logo=steam&style=flat-square)](https://steamcommunity.com/tradeoffer/new/?partner=1211192445&token=T9Hiu3Oz)

A minimal, hackable text-based MMORPG engine. Rooms, NPCs, items, combat,
loot, respawns, parties, a player market, and instanced dungeons — all driven
over WebSocket with plain JSON messages. Because the protocol is just JSON, a
human, a bot, and an LLM agent all look identical to the server. On top sits
an ML stack: Gym-style env (210 obs dims, 54 actions), linear + DQN agents
with curiosity and PBT, scripted baselines, a 50-agent conductor orchestrator,
and deterministic eval.

> **Authorship note:** this project is human-designed and human-led but AI
> coding assistants were used along the way for a lot of implementation.

## Setup

```bash
pip install -r requirements.txt
python3 server.py          # world on ws://0.0.0.0:8765
# open http://localhost:8766/ for the live dashboard
```

Run `python start.py` (any OS) for the server + banner, or add
`--roles "gather:8,dungeon:4,market:3,commissioner:2"` to also launch
bots. Edit `world.json` for your own rooms/NPCs/items (loaded once at
startup). `/api/state` also exposes `craft_history` and
`commission_history` rings (seq-ordered, newest last); each bounty on
the commissions board carries a `progress` summary (leading hunter,
kills vs required, display-only -- fills still verify from kill
timestamps). ML observations are unchanged by them.

## Docker (full stack, zero install)

```bash
docker compose up                  # server + dashboard + 4 scripted bots
# open http://localhost:8766/ (game on ws://localhost:8765)
docker compose --profile soak up soak        # one-shot conductor soak
docker compose --profile torch up --build    # torch farm (builds CPU torch)
```

`compose.yaml` builds one lean image (`python:3.12-slim` + `requirements.txt`;
torch only in the `torch` profile). The GM stream stays inside the compose
network (`TEXTMMO_GM_HOST=0.0.0.0` so `soak` can snapshot tables) and is not
published to the host — GM has no auth, so publish `8767` only on trusted
machines. Protocol v2 (`PROTOCOL_VERSION = 2`; see CHANGELOG).

## Quickstart

```bash
python3 server.py                      # terminal 1: world
python3 ml/ml_client.py                # terminal 2: learning agent
python3 ml/ml_botfarm.py --bots 4      # ...or a 4-bot training farm
python3 torch_agents/dqn_agent.py --steps 5000   # ...or the PyTorch DQN
```

```bash
python3 tests/test_server_unit.py   # offline checks
echo '{}' > scores.json
TEXTMMO_GM_SEED=700 python3 server.py   # terminal 1 (fresh world)
python3 tests/test_live.py              # terminal 2 (protocol E2E)
```

## Training runs

Every trainer writes a run record under `runs/<run_id>/` (gitignored):
`run.json` for identity, seed, hyper-parameters and git provenance, plus an
append-only `metrics.jsonl` series. That makes two runs comparable after the
fact instead of by scrolling back through stdout.

```bash
python3 ml/ml_client.py --seed 7 --steps 300    # reproducible run
python3 torch_agents/torch_farm.py --runs-dir /tmp/pilot-runs
python3 ml/ml_client.py --no-run-record         # train without recording
```

`--seed`, `--runs-dir` and `--no-run-record` work on `ml/ml_client.py`,
`ml/ml_botfarm.py`, `ml/conductor/soak.py`, `torch_agents/dqn_agent.py` and
`torch_agents/torch_farm.py`. `--seed` is independent of recording.

The dashboard Runs tab lists every run; tick up to 24 to overlay them on one
chart and diff them in a wide table. The column set follows the run kinds
present (`dqn`/`torch_farm`/`ml_client` report steps and reward,
`ml_botfarm` fitness and top score, `soak` episodes and mean reward), and each
metric is ranked in the direction that is actually better, so a loss is not
crowned as a winner. `GET /api/runs` serves the same data; it returns the runs
directory by name only, never an absolute path.

## Editing configuration in the browser

The dashboard Config tab edits `server_config.json` and `ml/ml_config.json`
directly, so you do not have to hand-edit JSON to try a value. It renders the
same 67 server tunables (and 6 ML ones) the loaders read, with the type, range
and help text for each field, and it flags which keys are overridden versus
still at their code default.

Three presets -- Balanced, Fast Training and Economy Focus -- stage their values
into the form for review; nothing is written until you press Save. A field with
a reset button can be dropped back to the code default, which removes the
override from the file rather than pinning a copy of it.

```text
GET  /api/config    read the schema, current values, presets (no-store)
POST /api/config    {"file": "server", "edits": {"economy.TAX_RATE": "0.2"}}
                   a null value drops the override
```

Two things to know before you edit a live server:

- Saves are written to disk but not applied to the running process. Both loaders
  read config at startup, so the reply and the UI both tell you to restart.
- Writes require a request from the machine the server runs on with a local
  `Origin`. Reached from anywhere else, `GET /api/config` is read-only and
  `editable` comes back `false`.

Files are edited surgically: only the lines you changed are rewritten, so
unrelated keys keep their formatting and a value that parses the same as what
is already there writes zero bytes. That matters because `ml/versioning.py`
hashes the raw config bytes into the checkpoint `config_hash`, so a
reformatted-but-equivalent file would invalidate every saved model.

## Starting training in the browser

The dashboard Agents tab has a Start training panel that launches
`torch_agents/torch_farm.py` without a terminal. You set the agent count,
step budget (0 means run until you press Stop), seed and checkpoints; pick a
preset to stage values into the form, then press Start. The panel shows the
pid, elapsed time and exit code, and Stop signals exactly the process the
server started.

```text
GET  /api/trainers      schema, presets, checkpoints, editable (LAN read)
GET  /api/train/status  running, pid, elapsed, exit code (LAN read)
POST /api/train/start   {"agents": 4, "steps": 0, "seed": 7}
POST /api/train/stop    {}
```

The request carries field values only. The server builds the command line
from them, refuses unknown keys and out-of-range numbers, and never runs a
shell. Checkpoints are restricted to `torch_agents/`, `ml/` and
`checkpoints/` and to `.pt`, `.pth`, `*weights.json` or `*best.json` names,
because the trainer overwrites whatever `--weights` points at; the defaults
match `torch_farm.py`'s own, so a dashboard fleet and a CLI fleet resume each
other. Start and Stop need a loopback browser, the same rule as config
writes; the reads stay LAN-visible.

The trainer runs as a child process in its own process group, so stopping it
can never signal the game server. Its stdout is detached, so a chatty trainer
does not flood the server log. Note that a running trainer is not remembered
across a server restart: restart the server only after pressing Stop.

## Docs (wiki)

The README is intentionally short — everything lives in the
[wiki](../../wiki):

| Guide | Contents |
|---|---|
| [Getting Started](../../wiki/Getting-Started) | Install, dashboard, first bot/LLM client |
| [Protocol](../../wiki/Protocol) | Every command + server message, GM stream |
| [Game Systems](../../wiki/Game-Systems) | Equipment, carry cap, ammo, world-building, dungeons, leveling, market, quests, scoring, multiplayer, capacity, accounts |
| [ML Guide](../../wiki/ML-Guide) | Env, reward modes, custom reward formula, curriculum, agents, DQN+curiosity, eval, PBT, conductor, soak tests |
| [Configuration](../../wiki/Configuration) | `server_config.json`, `ml_config.json`, env vars, ports |
| [Testing](../../wiki/Testing) | Suites, CI, live-test procedure |
| [Dashboard](../../wiki/Dashboard) | Panels, `/api/state`, GM tab, `/health`, Runs tab, Config tab |
| [Troubleshooting](../../wiki/Troubleshooting) | 0 players, wedges, ghosts, stale checkpoints, ports |

## Community

* [Contributing guide](CONTRIBUTING.md) — workflow for non-coders and
  researchers, protocol-stability rules, releases, labels/milestones.
* [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
* [Security policy](SECURITY.md) — how to report vulnerabilities
  (privately, never in a public issue).

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
