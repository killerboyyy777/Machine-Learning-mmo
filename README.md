# Small Text MMO Engine

[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

A minimal, hackable text-based MMORPG engine. Rooms, NPCs, items, combat,
loot, respawns, parties, a player market, instanced dungeons, and 1v1/2v2 arena
matches — all driven
over WebSocket with plain JSON messages. Because the protocol is just JSON, a
human, a bot, and an LLM agent all look identical to the server. On top sits
an ML stack: Gym-style env (220 obs dims, 59 actions), linear + DQN agents
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
machines. Protocol v2 (`PROTOCOL_VERSION = 2`; see the
[Protocol](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki/Protocol)
page).

### Prebuilt images

Images are published to GitHub Container Registry, so a server needs only
Docker:

```bash
docker run --detach --publish 8765:8765 --publish 8766:8766 \
  --env TEXTMMO_GM_SEED=700 \
  ghcr.io/killerboyyy777/machine-learning-mmo:edge
# open http://localhost:8766/ (game on ws://localhost:8765)
```

`edge` is the newest build of this branch; `latest`, `0.11` and `0.11.0` are
cut from release tags. Both are `linux/amd64` and `linux/arm64`. The GM port
`8767` stays unpublished, as above.

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

## Arena

`arena enter` queues a session for a 1v1 (`arena enter 2v2` for two-a-side),
`arena status` reports the queue, the match id, the round and both team
rosters, and `arena leave` forfeits. A table pairs as soon as enough live
sessions are queued; a session that disconnects forfeits and frees its
opponents at once.

A match lives in its own instanced room (`a_<match_id>`) that is not a key in
`ROOMS`, so the world map, its NPCs, the ML action space and `/api/state`
never see it. Rounds are 90 seconds, first to two rounds, and a round that
times out is scored on remaining HP. Attacking a player resolves only inside a
fighting match, with the same weapon roll and armour reduction as an NPC hit;
outside a match the target is still "not here".

A match moves no value: a downed fighter loses no gold, no items and no gear,
does not enter the death log, and does not count toward the civilian or wild
death counters. Gathering, trading, crafting, quests, healing and item use are
refused during a match.

The room view for an arena room carries an `arena` block
(`match_id, team_size, state, round, rounds_to_win, scores, teams, hp`), and
`arena status` replies with a `{"type": "arena"}` event carrying the same
shape plus the player's own team and rating.

### Ratings

Winning or losing a match moves rating on a zero-sum Elo ladder: one delta is
computed from each side's mean rating and the other is its negation, so a match
never mints rating. A draw splits the expectation and is counted as a draw, not
as a win for whichever side gained rating. Rating starts at 1000 and
never drops below 100; the clamp is the one place the sum stops balancing, so
`arena.json` counts clamps (`rating_clamps`) rather than hiding them.

The ladder is durable, so it does not live in `scores.json` (which evicts
entries on a TTL). `arena.json` holds the ratings, a 200-row match history and
that clamp count, written atomically with a `.1` generation, and a corrupt or
future-versioned file loads as an empty ladder with a warning instead of
stopping the server. A forfeit or a disconnect is a loss with the ordinary
delta, so leaving is never cheaper than losing.

`arena ladder` returns `{"type": "arena_ladder", "ladder", "you"}`, and
`/api/state` carries an additive `arena` block (`queues`, `live`, `ladder`,
`ladder_size`, `recent`, `rating_clamps`) for the dashboard next release.

### Watching

`arena watch <match_id>` attaches a session to a live match read-only;
`arena unwatch` (or `arena leave`) detaches. A watcher is not in the match
roster, so no seating, scoring or HP path can see it, and every command but
`arena` and `help` is refused while watching. The match id is the one in
`arena status` or the dashboard's live list.

A watcher receives the score line and the combat lines as they happen, plus
`{"type": "arena_view"}` on attach: match id, team size, state, round, scores,
both teams with name, HP and rating, and the last twelve match events. It
deliberately carries no room id, exits, items, gold or inventories, so a
spectator cannot read anything a fighter holds. When the match ends the
watcher gets `{"type": "arena_end"}` with the winner and final score and its
own state is cleared. An audience is capped at eight sessions per match.

The same view is served to HTTP clients: `/api/state` carries the lists, and
`GET /api/arena/<match_id>` returns one live match in the `arena_view` shape
(404 once it has ended). Both come from the same builder the socket path uses,
so the dashboard panel and a connected client cannot disagree.

The dashboard's Arena tab renders the live matches, the ladder and recent
results, and its follow box polls the per-match endpoint every two seconds.

## Balance measurement

`ml/balance.py` runs a mixed-agent profile against a live server, samples
`/api/state` plus the loopback GM stream, scores a scenario matrix against
explicit thresholds, writes a report, and exits nonzero when a check fails.

```bash
python3 ml/balance.py matrix                 # scenarios, profiles, skips
TEXTMMO_GM_SEED=700 python3 server.py &     # fresh world
python3 ml/balance.py run --profile smoke   # 10 min, 10 agents
python3 ml/balance.py run --profile gate    # 4h x 20 agents x seeds 700/701/702
python3 ml/balance.py baseline --seed 700 --commit "$(git rev-parse --short HEAD)" \
  --out balance/baseline-08f7453.json
```

Every later run is compared to the stored v0.10.0 baseline:

```bash
python3 ml/balance.py evaluate --measurements balance/measurements-smoke-700.json \
  --baseline balance/baseline-08f7453.json
```

Thresholds live in `DEFAULT_THRESHOLDS`; `--thresholds FILE` deep-merges a
JSON override without editing code. Scenarios that depend on unlanded work
(NPC AI v2 #536) or missing telemetry (acquisition/drop rates #532, item and
gold conservation) are reported as skipped with the reason, never silently
passed. The coverage check still fails a scenario that was expected to
produce a measurement and produced none.

## Architecture

The engine is built as a stateless-ish WebSocket server that manages a persistent world state.
- **Server**: Python based, handling JSON-RPC like messages.
- **Dashboard**: Static HTML/JS providing real-time monitoring via `/api/state`.
- **ML Stack**: A layered approach with a Gym environment wrapping the game protocol, allowing DQN and other agents to play the game.

See the [wiki](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki) for deep-dives into the protocol and ML implementation.

## Release history

Releases live in [GitHub Releases](https://github.com/killerboyyy777/Machine-Learning-mmo/releases):
each one carries the notes for that version and the tag it was cut from.

Through v0.13.0 the project also kept a `CHANGELOG.md` in this repository. It
was retired so a version has one history rather than two. The file is still
readable at the last commit that carried it:
[`CHANGELOG.md` at 5677bc6](https://github.com/killerboyyy777/Machine-Learning-mmo/blob/5677bc6/CHANGELOG.md).
That is where the detail behind the early releases lives - anything before
v0.6.0, and the longer form of the terse v0.6.0 to v0.8.0 notes.

## Community

* [Contributing guide](CONTRIBUTING.md) — workflow for non-coders and
  researchers, protocol-stability rules, releases, labels/milestones.
* [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
* [Security policy](SECURITY.md) — how to report vulnerabilities
  (privately, never in a public issue).

## Support

The useful contribution here is not money. It is an issue with a reproduction, an
agent run that hits something the tests do not, or a pull request that leaves the
tree cleaner than it found it. Nothing else is expected.

If you want to send something anyway, these addresses are the only channel. They
are tips, not a product: there is no paid tier, no priority support and no
promise attached to them.

```text
BTC  bc1qmkv939k2wqsej657cxj25ppwqdh65y2umnv3gg
LTC  ltc1qjr49nr028mcajlt7prmmnqnjh0552qjj90zdq4
XMR  82oJRDdiSWWbem3HiYx7ZdDdiPkYQAW4LaGNHpNcJ9DCendQ3XcxHNYQiRMtfghYtSMmARPGqKe2ddSrhtjviTraEyGwgZ2
```

Crypto transfers are irreversible: send only what you are content to
part with, and do not send from an exchange that may freeze the withdrawal.

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
