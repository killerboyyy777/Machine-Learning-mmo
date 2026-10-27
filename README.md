# Small Text MMO Engine

[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

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
bots. Edit `world.json` for your own rooms/NPCs/items. See the
[wiki](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki) for
details on game systems, ML guide, and API documentation.

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

## Companions

Adopt a wolf, mule, or sheep companion, then summon it. Companions earn a
share of your XP and level up to 10 (see the wiki). `stats`
returns a read-only companion sheet
(`name, species, level, hp, max_hp, attack, defense, passive, script_state`),
and the dashboard Agents tab plus the TUI Agents panel show the same fields.
DroneLang scripts drive companion actions via
`companion script set|show|clear`.

## Architecture

The engine is built as a stateless-ish WebSocket server that manages a persistent world state.
- **Server**: Python based, handling JSON-RPC like messages.
- **Dashboard**: Static HTML/JS providing real-time monitoring via `/api/state`.
- **ML Stack**: A layered approach with a Gym environment wrapping the game protocol, allowing DQN and other agents to play the game.

See the [wiki](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki) for deep-dives into the protocol and ML implementation.

## Community

* [Contributing guide](CONTRIBUTING.md) — workflow for non-coders and
  researchers, protocol-stability rules, releases, labels/milestones.
* [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
* [Security policy](SECURITY.md) — how to report vulnerabilities
  (privately, never in a public issue).

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
