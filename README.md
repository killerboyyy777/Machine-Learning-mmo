# Small Text MMO Engine

[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

[![BTC Donate](https://img.shields.io/badge/BTC-Donate-f7931a?logo=bitcoin&style=flat-square)](https://www.blockchain.com/explorer/addresses/btc/bc1qmkv939k2wqsej657cxj25ppwqdh65y2umnv3gg)
[![LTC Donate](https://img.shields.io/badge/LTC-Donate-a6a9aa?logo=litecoin&style=flat-square)](https://live.blockcypher.com/ltc/address/ltc1qjr49nr028mcajlt7prmmnqnjh0552qjj90zdq4)
[![Steam Donate](https://img.shields.io/badge/Steam-Donate-000000?logo=steam&style=flat-square)](https://steamcommunity.com/tradeoffer/new/?partner=1211192445&token=T9Hiu3Oz)

A minimal, hackable text-based MMORPG engine. Rooms, NPCs, items, combat,
loot, respawns, parties, a player market, and instanced dungeons — all driven
over WebSocket with plain JSON messages. Because the protocol is just JSON, a
human, a bot, and an LLM agent all look identical to the server. On top sits
an ML stack: Gym-style env (175 obs dims, 49 actions), linear + DQN agents
with curiosity and PBT, scripted baselines, a 50-agent conductor orchestrator,
and deterministic eval.

> **Authorship note:** this project is human-designed and human-led but AI
> coding assistants where used along the way for a lot of implementation.

## Setup

```bash
pip install -r requirements.txt
python3 server.py          # world on ws://0.0.0.0:8765
# open http://localhost:8766/ for the live dashboard
```

On Windows you can also just run `start.bat`. Edit `world.json` for your
own rooms/NPCs/items (loaded once at startup).

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
machines. Protocol unchanged (`PROTOCOL_VERSION = 1`).

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

## Docs (wiki)

The README is intentionally short — everything lives in the
[wiki](../../wiki):

| Guide | Contents |
|---|---|
| [Getting Started](../../wiki/Getting-Started) | Install, dashboard, first bot/LLM client |
| [Protocol](../../wiki/Protocol) | Every command + server message, GM stream |
| [Game Systems](../../wiki/Game-Systems) | Equipment, carry cap, ammo, world-building, dungeons, leveling, market, quests, scoring, multiplayer, capacity, accounts |
| [ML Guide](../../wiki/ML-Guide) | Env, reward modes, curriculum, agents, DQN+curiosity, eval, PBT, conductor, soak tests |
| [Configuration](../../wiki/Configuration) | `server_config.json`, `ml_config.json`, env vars, ports |
| [Testing](../../wiki/Testing) | Suites, CI, live-test procedure |
| [Dashboard](../../wiki/Dashboard) | Panels, `/api/state`, GM tab, `/health` |
| [Troubleshooting](../../wiki/Troubleshooting) | 0 players, wedges, ghosts, stale checkpoints, ports |

## Community

* [Contributing guide](CONTRIBUTING.md) — workflow for non-coders and
  researchers, protocol-stability rules, releases, labels/milestones.
* [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
* [Security policy](SECURITY.md) — how to report vulnerabilities
  (privately, never in a public issue).

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
