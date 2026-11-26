# Small text MMO engine

[![Docker Pulls](https://ghcr-badge.elias.eu.org/shield/killerboyyy777/Machine-Learning-mmo/machine-learning-mmo)](https://github.com/killerboyyy777/Machine-Learning-mmo/pkgs/container/machine-learning-mmo)
[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

A small, hackable text MMORPG engine. It needs Python 3.12 or newer; CI covers 3.12 and 3.14. Rooms, NPCs, items, loot, respawns, parties, a player market, instanced dungeons, and 1v1 and 2v2 arena matches all run over WebSocket with plain JSON messages. Because the protocol is just JSON, a human, a bot, and an LLM agent look identical to the server on the game port; GM control is a separate loopback-only surface.

On top sits an ML stack: a Gym-style environment with (224 obs dims, 67 actions), linear and DQN agents with curiosity, scripted baselines, a 50-agent conductor, and deterministic evaluation. Population-based training (PBT) is a conductor library API only: the shipped `soak.py` CLI does not enroll agents or report fitness, so it produces no exploit/explore rounds.

> A human designs and leads this project. AI coding assistants wrote much of the implementation.

## Setup

Install the released package and run the console script:

```bash
pip install small-text-mmo-engine
small-text-mmo              # world on ws://0.0.0.0:8765
# open http://localhost:8766/ for the live dashboard
```

Or run from a checkout:

```bash
pip install -r requirements.txt
python3 server.py          # world on ws://0.0.0.0:8765
# open http://localhost:8766/ for the live dashboard
```

Both paths write mutable state to the directory you launch from; see [State files](#state-files). Run `python start.py` on any OS to start the server with a banner. Add `--roles "gather:8,dungeon:4,market:3,commissioner:2"` to also start bots. Edit `world.json` for your own rooms, NPCs, and items. The [wiki](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki) covers game systems, the ML guide, and the API.

## Configuration

Environment variables are read at startup. A boolean option is enabled by `1`, `true`, `yes`, or `on` (case-insensitive); unset or any other value leaves it off.

| Variable | Default | Effect |
| --- | --- | --- |
| `TEXTMMO_GM_SEED` | `0` | World RNG seed and starting GM treasury. Set it for reproducible runs and the live tests. |
| `TEXTMMO_DATA_DIR` | current directory | Directory for mutable state files and their backups (`scores.json`, `arena.json`, `guilds.json`, `freight.json`, `companions.json`, `intel.json`, `market.json`, `commissions.json`). Bundled inputs (`world.json`, `server_config.json`, `ml/ml_config.json`) stay with the installed package. |
| `TEXTMMO_GM_HOST` | `127.0.0.1` | Bind address for the GM stream on port 8767. That stream has no auth, so `0.0.0.0` opts into Docker-network reachability only; keep the port unpublished (see [SECURITY.md](SECURITY.md)). |
| `TEXTMMO_REQUIRE_TOKEN` | off | Security-sensitive: require a login token for every account. Unset leaves the server-wide gate off, but a name with existing progress still needs a token. |
| `TEXTMMO_ALLOW_LEGACY_TOKEN_CLAIM` | off | Security-sensitive: let an account with no stored token hash claim a token at login. Off by default; only a server that never issued tokens should enable it. |
| `TEXTMMO_MAX_CONNECTIONS` | `1000` | Cap on total connections. |
| `TEXTMMO_MAX_ROOM_PLAYERS` | `12` | Cap on players per room. |
| `TEXTMMO_OUTBOUND_QUEUE` | `64` | Cap on the outbound queue. |
| `TEXTMMO_VERBOSE` | off | Print extra server logs and full tracebacks for handler errors. |
| `TEXTMMO_LOG_FILE` | unset | Append server stdout and stderr to this file instead of the console. |
| `TEXTMMO_METRICS` | off | Serve `/metrics` on the dashboard port. Passing `--metrics` to `server.py` does the same; see [Observability](#observability). |
| `TEXTMMO_TELEMETRY` | off | Conductor/soak only: write `telemetry.jsonl` for a run. |
| `TEXTMMO_STRUCTURED_LOG` | off | Conductor/soak only: write the `structured.jsonl` event log. |
| `TEXTMMO_RELOAD` | off | Conductor/soak only: watch the reload directory for changed checkpoints. |

## State files

The server keeps durable state in JSON files in `TEXTMMO_DATA_DIR`, which defaults to the directory you start the server from. A pip-installed server therefore writes state to a user-writable directory, never into `site-packages`. Set `TEXTMMO_DATA_DIR` to keep runs in separate directories. Saves go through a temporary file, and most files rotate the previous copy to a `.1` name first, so a crash mid-write cannot leave a truncated file.

| File | Holds | If you delete it |
| --- | --- | --- |
| `scores.json` | Short-lived per-player scores, flushed every five seconds and evicted on a TTL. | Player score entries reset; the next start begins with an empty table. |
| `arena.json` | Arena ratings, recent match history, and the clamp count. | The ladder, history, and clamps reset to empty. |
| `guilds.json` | Guilds, their vaults, and pending transfer offers with their rolling caps. | Guilds, vault balances and pending offers are lost. |
| `market.json` | Open sell orders and escrowed standing bids. | Listed items and bid escrow are lost; restore the file or its `.1` backup. |
| `commissions.json` | Open and recent bounties with their prepaid escrow. | Bounties and their escrow are lost; restore the file or its `.1` backup. |
| `companions.json` | Player companions. | Companion progress is lost. |
| `intel.json` | Bot-cadence intel rows. | Intel history is lost. |
| `freight.json` | Freight contracts and the contract counter. | Freight state is lost. |

`world.json` and `server_config.json` are inputs you edit, not saved state. Checkpoints record a canonical `world.json` hash alongside the config hash, so changing rooms, NPCs, items or recipes marks older checkpoints as world drift; checkpoints saved before that field existed are treated as compatible. The rotated backups are safe to delete only while their primary file is present and valid: `scores.json.1` and `scores.json.2` are its two generations, while `guilds.json`, `market.json`, `commissions.json`, `intel.json`, `freight.json`, and `arena.json` keep one `.1` copy each (`companions.json` has none). A missing or unreadable primary falls back to a backup with a warning; a file written by a newer version is left on disk but not loaded. If both the primary and its backups are gone, that state is lost permanently.

Market and commission rows that fail validation are dropped with a warning, and their escrow is banked to the owner for the next login. An unknown version loads empty without blocking later saves. `scores.json` now carries a `version` field and a `scores` object; an unversioned file is read as version 1, invalid entries are skipped with a warning instead of breaking login, and a foreign version is left untouched and blocks later saves until it is moved away. Companions loaded with an out-of-range level are clamped to 1-10, and rows with non-finite XP or timestamps are dropped.

`server_config.json` overrides are limited to keys declared in `config_schema.py` and must fall inside that file's min/max bounds; out-of-range values are skipped with a warning and the code default stays in force. `AUTH_TOKEN_REQUIRED` remains a valid legacy boolean override outside the schema. `GROUND_ITEM_CAP` (default 200) and `GROUND_ITEM_TTL_SECONDS` (default 21600) bound the ground piles in persistent rooms: excess or expired drops become gold on that room's floor, so their value is not destroyed. `GROUND_GOLD_CAP` (default 100000) caps each floor pile; overflow is banked in the GM treasury. Freight poster and hauler names are stored and compared case-insensitively, and a future-version freight backup beside a healthy primary no longer blocks saves. A socket whose outbound writer fails is closed immediately, releasing its room and connection slots.

## Docker

The full stack needs no local Python install.

```bash
docker compose up                  # server + dashboard + 4 scripted bots
# open http://localhost:8766/ (game on ws://localhost:8765)
docker compose --profile soak up soak        # one-shot conductor soak
docker compose --profile torch up --build    # torch farm (builds CPU torch)
```

`compose.yaml` builds one small image from `python:3.14-slim` and `requirements.txt`. Torch is only in the `torch` profile. The `soak` service shares the server's network namespace (`network_mode: service:server`) and snapshots tables over loopback at `ws://127.0.0.1:8767`, which passes the server's loopback-only GM gate. Port 8767 is not published to the host, and GM has no authentication, so leave it that way; publishing it does not bypass the gate (see [SECURITY.md](SECURITY.md)). The server uses protocol v2 (`PROTOCOL_VERSION = 2`; see the [Protocol](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki/Protocol) page).

### Prebuilt images

Images are published to GitHub Container Registry, so a server needs only Docker.

```bash
docker run --detach --publish 8765:8765 --publish 8766:8766 \
  --env TEXTMMO_GM_SEED=700 \
  ghcr.io/killerboyyy777/machine-learning-mmo:edge
# open http://localhost:8766/ (game on ws://localhost:8765)
```

The publishing workflow builds images for `linux/amd64` and `linux/arm64` and pushes these tags to GHCR:

- `edge` follows pushes to `master`. It is a moving tag for the newest development build.
- `edge-<short-sha>` names the specific master commit used for a build. Use it to pin an edge image instead of following the moving tag.
- `X.Y.Z` and `X.Y` come from version tags such as `vX.Y.Z`.
- `latest` moves only for stable `v*` release tags. Prerelease tags do not move it.

Use `edge` for the current development image. Replace the tag with a version or commit tag to pin a build. Port 8767 stays unpublished, as above.

## Quickstart

```bash
python3 server.py                      # terminal 1: world
python3 ml/ml_client.py                # terminal 2: learning agent
python3 ml/ml_botfarm.py --bots 4      # or a 4-bot training farm
# Optional, only for the PyTorch DQN / Torch agents:
pip install -r requirements-train.txt
python3 torch_agents/dqn_agent.py --steps 5000   # or the PyTorch DQN
```

Trainer CLIs reject out-of-range settings with an argparse error before opening a server connection or a run record: `--steps < 0`, `--save-every 0`, `--epsilon-decay-steps 0`, epsilon endpoints outside `[0, 1]`, `--reward-window < 1`, and `--eval-every <= 0`. Shared farms reserve a step before awaiting the environment step, so `--steps` caps total attempts across all bots; a step that loses its connection still spends its slot. The torch farm stops the remaining runners when one crashes and exits 1 for a failed run; a clean stop, Ctrl+C, and step-limit completion exit 0. Policies pass an integer action index to `TextMMOEnv.step()`; booleans are rejected.

```bash
python3 tests/test_server_unit.py   # offline checks
echo '{}' > scores.json
TEXTMMO_GM_SEED=700 python3 server.py   # terminal 1 (fresh world)
python3 tests/test_live.py              # terminal 2 (protocol E2E)
```

## Ammo

A bow fires the best arrow in the ammo family you carry: Arrow (+0), Iron Arrow (+1), Steel Arrow (+2), Fire Arrow (+3) or Blood Arrow (+4). One arrow is consumed per shot, and the best variant in the family wins regardless of the order you hold them.

Fire Arrows set the target ablaze for two world ticks, chipping 2 HP per tick. The burn cannot land the killing blow, so loot, gold and credit stay with direct shots. Blood Arrows restore 2 HP to the shooter on each hit, capped at maximum HP. Both effects ride on top of the flat bonus and are crafted with the `fire_arrows` and `blood_arrows` recipes.

`stats` lists the count of every ammo variant, including zero counts, and the dashboard Players table and character tracker show the same view. The ML observation's `ammo_best_norm` scales the best loaded bonus by the family maximum (4), so it stays in [0, 1] as variants are added.

## Drop-in agents

The conductor loads custom agents from Python files in the top-level `plugins/` directory. Each file registers an `AgentPlugin` subclass with a unique ASCII name, an `agent_type` of `linear`, `scripted`, `torch`, or `custom`, a typed `config_schema()`, and an `act()` policy. Admission refuses invalid manifests and reports the reason without discarding other valid plugins. The [plugin guide](plugins/README.md) has the full contract and a runnable example.

Start a conductor with a plugin slot. The directory defaults to `<repo>/plugins`; override it with `--plugin-dir`.

```bash
python3 ml/conductor/soak.py --agents 4 --slot hello:speed=5
```

A plugin may also implement the optional episode, reward, learning, and checkpoint hooks described in the plugin guide. Plugins run inside the conductor process. Admission validates a plugin before use, but it is not a security sandbox. A blocking or failing policy can still affect the run: with `--policy-timeout` set, a hung call is abandoned on a daemon worker, so it cannot hold the process open at shutdown, though the call itself keeps running until the process exits.

## Companions

Adopt a wolf, mule, or sheep companion, then summon it. Companions earn a share of your XP and level up to 10; the wiki has the details. The `stats` command returns a read-only companion sheet with `name, species, level, hp, max_hp, attack, defense, passive, script_state`. The dashboard Agents tab and the TUI Agents panel show the same fields. DroneLang scripts drive companion actions through `companion script set|show|clear`.

## Guilds

Found and run a guild at the Adventurers Guild hall. A guild has a shared vault, three ranks (member, officer, leader), a configurable permission table, and a 500-entry audit log. `guild transfer` proposes gold to another guild and `guild accept` completes it. Nothing is escrowed, and the server re-checks the sending vault when the offer is accepted. Either side can close a proposal early. The receiver runs `guild decline` (leader or officer) and the sender runs `guild cancel`. Both write terminal audit rows on each side and notify the other guild.

An offer costs the target an audit row whether or not anyone accepts it. Both the queue and the day are bounded: three offers waiting with one guild, twenty in total, an hour to answer, and per target ten received from any one sender and twenty from everyone combined in a rolling day. Declining frees the queue, not the day. Pending offers and the rolling caps are stored in `guilds.json` and survive a restart. The wiki covers the vault, rank, and withdrawal rules.

## Arena

`arena enter` queues a session for a 1v1 match, and `arena enter 2v2` queues for two-a-side. `arena status` reports the queue, the match id, the round, and both team rosters. `arena leave` forfeits. A table pairs as soon as enough live sessions are queued. A session that disconnects forfeits and frees its opponents at once.

A match lives in its own instanced room named `a_<match_id>`. That room is not a key in `ROOMS`, so the world map, its NPCs, the ML action space, and `/api/state` never see it. Rounds last 90 seconds, first to two rounds, and a timed-out round is scored on remaining HP. Attacking a player resolves only inside a fighting match, with the same weapon roll and armour reduction as an NPC hit. Outside a match the target is still "not here".

A match moves no value. A downed fighter loses no gold, items, or gear, does not enter the death log, and does not count toward the civilian or wild death counters. Gathering, trading, crafting, quests, healing, item use, and equipment changes are refused during a match.

The room view for an arena room carries an `arena` block with `match_id, team_size, state, round, rounds_to_win, scores, teams, hp`. `arena status` replies with a `{"type": "arena"}` event carrying the same shape plus the player's own team, rating, rank, match count, and whether that standing is still provisional.

### Ratings

Winning or losing a match moves rating on a zero-sum Elo ladder. The server computes one delta from each side's mean rating and makes the other its negation, so a match never mints rating. A draw splits the expectation and counts as a draw, not as a win for whichever side gained rating. Rating starts at 1000 and never drops below 100. The clamp is the one place the sum stops balancing, so `arena.json` counts clamps in `rating_clamps` instead of hiding them.

The ladder is durable, so it does not live in `scores.json`, which evicts entries on a TTL. `arena.json` holds the ratings, a 200-row match history, and the clamp count, written atomically with a `.1` generation. A corrupt or future-versioned file loads as an empty ladder with a warning instead of stopping the server. A forfeit or disconnect is a loss with the ordinary delta, so leaving is never cheaper than losing.

A player with fewer than five completed matches is provisional. Their rating still moves and still shows, but every provisional row sorts below every established one, so one lucky first win cannot outrank a season of wins. The `min_matches` field in the payloads below gives the count needed.

`arena ladder` returns `{"type": "arena_ladder", "ladder", "ladder_size", "ranked_size", "min_matches", "you"}`. `/api/state` carries an additive `arena` block with `queues`, `live`, `ladder`, `ladder_size`, `ranked_size`, `provisional_size`, `min_matches`, `recent`, and `rating_clamps` for the dashboard. Provisional ladder rows carry `"provisional": true`. The `you` object carries `rank` (null while unrated) next to `rating`, `matches`, and `provisional`.

### Watching

`arena watch <match_id>` attaches a session to a live match read-only. `arena unwatch` or `arena leave` detaches. A watcher is not in the match roster, so no seating, scoring, or HP path can see it. Every command but `arena` and `help` is refused while watching. The match id is the one in `arena status` or the dashboard's live list.

A watcher receives the score line and the combat lines as they happen, plus a `{"type": "arena_view"}` event on attach. That event carries the match id, team size, state, round, scores, both teams with name, HP and rating, and the last twelve match events. It deliberately carries no room id, exits, items, gold, or inventories, so a spectator cannot read anything a fighter holds. When the match ends the watcher gets `{"type": "arena_end"}` with the winner and final score, and its own state is cleared. An audience is capped at eight sessions per match.

HTTP clients get the same view. `/api/state` carries the lists, and `GET /api/arena/<match_id>` returns one live match in the `arena_view` shape, or 404 once it has ended. Both come from the builder the socket path uses, so the dashboard panel and a connected client cannot disagree.

The dashboard Arena tab renders live matches, the ladder, and recent results. Its follow box polls the per-match endpoint every two seconds. The terminal UI mirrors it as the sixth tab over the same snapshot block.

### Scripted arena agents

The conductor's opt-in `arena` scripted role queues eligible agents for 2v2 matches and fights with the normal attack action. An agent queues only when it has at least 80% HP and is idle in a non-hostile, non-dungeon room. It stays queued while waiting, leaves if it loses that eligibility, and abandons a queue when the wait exceeds 120 agent selections. While fighting it attacks when possible and otherwise uses its normal idle fallback of `rest` or `look`. The role sends ordinary arena commands through the pending-command channel and adds no environment actions or observation fields.

Run four agents with the role using the conductor's repeatable slot option.

```bash
python3 ml/conductor/soak.py --agents 4 --slot arena
```

## Balance measurement

`ml/balance.py` runs a mixed-agent profile against a live server. It samples `/api/state` and the loopback GM stream, scores a scenario matrix against explicit thresholds, writes a report, and exits nonzero when a check fails.

```bash
python3 ml/balance.py matrix                 # scenarios, profiles, skips
TEXTMMO_GM_SEED=700 python3 server.py &     # fresh world
python3 ml/balance.py run --profile smoke   # 10 min, 10 agents
python3 ml/balance.py run --profile gate    # 4h x 20 agents x seeds 700/701/702
python3 ml/balance.py baseline --seed 700 --commit "$(git rev-parse --short HEAD)" \
  --out balance/baseline-08f7453.json
```

Every later run is compared with the stored v0.10.0 baseline.

```bash
python3 ml/balance.py evaluate --measurements balance/measurements-smoke-700.json \
  --baseline balance/baseline-08f7453.json
```

The gate profile also runs the arena probe. It queues four fresh fighters for two 2v2 matches and checks that both matches complete, rating deltas are conserved, gold and item totals stay unchanged, the civilian and wild death counters do not move, and repeating the probe produces the same fingerprint. An unavailable arena command or a failed probe is a blocking coverage failure, not a passing skip.

Thresholds live in `DEFAULT_THRESHOLDS`. `--thresholds FILE` deep-merges a JSON override without editing code. The release gate requires measured coverage for economy, combat, craft, acquisition (#532), guilds (#588), arena (#656), market, liveness, and NPC AI v2 (#536). A missing probe or telemetry for any required scenario is a blocking failure, not a passing skip. The `progression` and `party` scenarios are observational because the server does not expose every metric needed to threshold them. Item and gold conservation and companion damage share are blocking checks.

## Generalist doctrine pilot

`ml/generalist.py` supplies a generalist whose TRAINING reward is one signal summed across four archetype rewards, plus purist specialists trained on one archetype each. Registering the plugins is an explicit opt-in; the module has no runtime, gate, or reward-default hook. Its same-frame curves and pilot report are informational and never gate a run.

Training reward is not an evaluation score. Because the generalist reward adds all four archetype rewards while a purist uses one, comparing those sums head-to-head makes the generalist lead whenever several axes are positive, even though no generalist policy acted or learned. `same_frame_curve` records those same-frame training-reward curves only; it cannot by itself establish that a generalist outperforms a purist.

`pilot_report` makes the distinction explicit. With `objective` set, its curves are cumulative outcomes on one common objective from separate per-policy trajectories on matched seeds and frames, and a positive lead is a genuine outcome difference. Without `objective`, curves are treated as training rewards and the generalist is reduced to a weighted average across its active archetypes, so summing more axes cannot manufacture a lead and the verdict stays inconclusive. Curves of unequal length are rejected as `misaligned_data` instead of being compared over a common prefix.

The same-frame training-reward comparison is provisional: it carries no honest performance claim. Before v0.19 either confirm a lead that survives this normalization on a common objective, or drop the claim that the pilot compares generalist and purist performance.

## Observability

Three measurement surfaces exist, all off by default. A normal server or soak run writes none of them.

- **`/metrics`.** The dashboard port serves Prometheus text with aggregate gauges only, such as players online, live connections, rooms, living NPCs, and market trades. No per-player data is exposed. Set `TEXTMMO_METRICS=1` or pass `--metrics` to `server.py` to enable the route. The dashboard has no auth, so leave it off unless you need it.
- **Conductor telemetry.** `ml/conductor/soak.py` writes `telemetry.jsonl` and a summary `telemetry.json` in the run directory. Enable it with `TEXTMMO_TELEMETRY=1` or `--telemetry-dir DIR`. This is a conductor feature, not a server feature.
- **Structured event log.** The conductor can append `structured.jsonl` records for episodes and run events. Enable it with `TEXTMMO_STRUCTURED_LOG=1` or `--structured-log-dir DIR`.

CI covers these with `tests/test_metrics_route.py`, `tests/test_telemetry.py`, and `tests/test_structured_log.py`.

## Architecture

The engine is a WebSocket server that keeps world state in memory.

- **Server.** Python reads a simple JSON message format: clients send a `cmd` command and receive typed events with a `type` field. It is not JSON-RPC.
- **Dashboard.** Static HTML and JavaScript provide live monitoring through `/api/state`.
- **ML stack.** A Gym environment wraps the game protocol, so DQN and other agents can play.

The [wiki](https://github.com/killerboyyy777/Machine-Learning-mmo/wiki) documents the protocol and the ML implementation in detail.

## Release history

Releases live in [GitHub Releases](https://github.com/killerboyyy777/Machine-Learning-mmo/releases). Each carries the notes for its version and the tag it was cut from.

Through v0.13.0 the project also kept a `CHANGELOG.md` in this repository. It was retired so a version has one history rather than two. The file is still readable at the last commit that carried it: [`CHANGELOG.md` at 5677bc6](https://github.com/killerboyyy777/Machine-Learning-mmo/blob/5677bc6/CHANGELOG.md). That is where the detail behind the early releases lives, including anything before v0.6.0 and the longer form of the terse v0.6.0 to v0.8.0 notes.

## Community

- [Contributing guide](CONTRIBUTING.md) covers the workflow for non-coders and researchers, protocol-stability rules, releases, and labels and milestones.
- [Code of Conduct](CODE_OF_CONDUCT.md) is Contributor Covenant v2.1.
- [Security policy](SECURITY.md) explains how to report vulnerabilities privately, never in a public issue.

## Support

The useful contribution here is not money. It is an issue with a reproduction, an agent run that hits something the tests miss, or a pull request that improves the code. Nothing else is expected.

If you want to send something anyway, these addresses are the only channel. They are tips, not a product. There is no paid tier, no priority support, and no promise attached.

```text
BTC  bc1qmkv939k2wqsej657cxj25ppwqdh65y2umnv3gg
LTC  ltc1qjr49nr028mcajlt7prmmnqnjh0552qjj90zdq4
XMR  82oJRDdiSWWbem3HiYx7ZdDdiPkYQAW4LaGNHpNcJ9DCendQ3XcxHNYQiRMtfghYtSMmARPGqKe2ddSrhtjviTraEyGwgZ2
```

Crypto transfers are irreversible. Send only what you are content to part with, and do not send from an exchange that may freeze the withdrawal.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE). Vendored third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
