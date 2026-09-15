# Text MMO Engine - TODO / Roadmap

A forward-looking work list. Completed work is recorded in `CHANGELOG.md`;
only items that still need doing (are open, blocked, or half-done) belong
here, so a future reader is never scanning checkboxes that are already true.

## Priority: Small ("XS" — minutes to two days)

- [ ] Fix dashboard `trade_count` double-counting buyer and seller entries
- [ ] Add quest coverage to `tests/test_server_unit.py`:
  accept, craft, turn-in, repeat, Depth Delver, healer remedy/tonic,
  and death-drop gold split/pickup
- [ ] Add explicit crafting recipe profitability assertions
- [ ] Update protocol help/README whenever new recipes or item types land
- [ ] Make killing quest-giver NPCs actively punishing: negative score on top
  of near-zero XP/gold, so it's never worth it (currently +0.1 pts / +0.1 XP /
  0 gold in the kill-split block — small but still positive; check `award_xp`
  clamps negatives safely before wiring it through)
- [ ] Expose active buffs and complete ammo counts consistently in `stats`,
  dashboard player rows, and dashboard player tracking
- [ ] Add a dashboard quest panel using the existing `/api/state.quests` data
- [ ] Add crafting/material supply panels to the dashboard
- [ ] Add historical player-count and score graphs
- [ ] Add a configurable tuning file for constants currently in `server.py`
- [ ] Add configurable reward-component options for ML training

## Priority: Medium (two to five days)

### Crafting Economy Phase 1: Ammo And Buff Foundations
- [ ] Add remaining crafted ammo variants and ammo-specific effects
- [ ] Add buff duration, category replacement, stacking rules, and expiration
- [ ] Expose ammo and buffs in stats, ML observations, and dashboard

### Crafting Economy Phase 2: Gathering
- [ ] Add respawnable gathering-node definitions
- [ ] Add `gather` command and node cooldowns
- [ ] Add safe gathering materials and fighter-only material drops
- [ ] Add gathering score, XP, variety signatures, and tests

### Crafting Economy Phase 3: Crafting Mastery
- [ ] Add total crafts, per-recipe mastery, tier mastery, and discoveries
- [ ] Replace flat craft rewards with tier-aware score and XP
- [ ] Add material-cost and recipe-profitability tests

### Crafting Economy Phase 5: ML Support
- [ ] Add gathering actions and observations
- [ ] Add commission actions and state
- [ ] Add recipe availability, mastery, buffs, and ammo observations
- [ ] Reset/regenerate checkpoints after observation or action changes
- [ ] Verify crafting-only training behavior

### Dashboard And Documentation
- [ ] Add Crafting/Artisans dashboard tab
- [ ] Show gathering nodes, commissions, artisan rankings, buffs, and ammo supply
- [ ] Add profitability and material-demand panels
- [ ] Update README, CHANGELOG, protocol docs, and tests for the crafting system

## Priority: Large (several days to multiple weeks)

### Crafting Economy Phase 4: Player Commissions
- [ ] Repeated alt-account abuse beyond self-fill rejection (e.g. two-account collusion)

### Crafting Economy Phase 6: Verification
- [ ] Pure gatherer progression test
- [ ] Fighter-to-crafter supply test
- [ ] Commission escrow/refund test
- [ ] Ammo consumption test
- [ ] Buff duration and stacking test
- [ ] Crafting variety/diminishing-returns test
- [ ] Long crafting-only botfarm comparison against combat baseline

### Conductor: large-scale agent orchestrator (`ml/conductor/`)
Goal: run 50 agents first, then scale to MMO-like hundreds, with realistic
churn and reliably improving policies. Single supervisor process, shared
policies, managed cohorts. Decisions already taken: mixed linear+torch
population with a plugin registry for future types; stable+experimental
branches per type; adaptive population mix; 50-agent milestone first.

#### Population model (50-agent start; controller reallocates within floors)
- [ ] Learner cells per `(type x branch)`: ~28 linear-stable, ~6
  linear-experimental (higher epsilon floor, stronger formation scale),
  ~4 torch-stable (shared DQN), ~2 torch-experimental (alt aux lambdas)
- [ ] Holdouts (~6): no training updates; alternate episodes between stable
  and experimental weights per type to score both branches fairly
- [ ] Fresh rotating names (~4): newbies for discovery data, share their
  type's stable policy (they generate data, holdouts judge it)

#### Plugin architecture (no env/server changes)
- [ ] `registry.py`: `register(name, factory, default_cfg)`; required
  interface `act(features, epsilon) -> int`, `update(...)`,
  `save(path)/load(path)`, `obs_size`/`n_actions` attrs (both current
  agents already satisfy it); new type = new module + one register call
- [ ] `cells.py`: build population from config `{type, branch, count,
  hyperparams, weights_path}`; per-cell-branch checkpoints
  (e.g. `ml_weights_linear_stable.json`); old single files become legacy
- [ ] `churn.py`: Poisson arrivals toward per-cell targets, geometric
  lifetimes (mean 20-60 min) plus rarely-leaving residents, wave starts
  with jitter (replaces `2s x idx`, which takes ~7 min at 200 bots)
- [ ] `supervisor.py`: one event loop, one policy object per cell-branch,
  one checkpoint writer; every agent task wrapped in try/except with
  backoff respawn (one poisoned connection must never kill the run)
- [ ] `metrics.py`: JSONL per minute — steps/sec, reward rate, trades/min,
  treasury flow, party rate, discovery rate, per-branch holdout fitness

#### Stable + experimental promotion (per type)
- [ ] Promote experimental -> stable only when experimental holdout fitness
  beats stable by a margin over a rolling window; keep last-K stables for
  rollback; experimental never writes to stable directly

#### Adaptive mixer (targets "best training results")
- [ ] Controller reallocates cell targets every N minutes from per-branch
  holdout fitness (reward rate + score velocity + task participation):
  mostly toward the winner, >=5% floor per cell (never starve a branch),
  <=10% shift per cycle (no oscillation)
- [ ] Fresh-name fraction follows the discovery signal: rich discovery
  rewards -> more fresh blood; dry -> shift to residents for economy depth
- [ ] Keep the rule proportional and fully logged (reviewable, not a
  black-box meta-learner)

#### Stage 1 acceptance (50 agents, current machine)
- [ ] All 50 step concurrently, each cell populated per config; wave
  startup under 60s
- [ ] Kill one bot's connection mid-run -> respawned alone, run continues
- [ ] Holdout fitness reported per branch; promotion or documented
  no-promotion with reason
- [ ] Multi-agent market volume (orders + fills + treasury flow)
- [ ] Hour-long soak: stable steps/sec, bounded `scores.json`, no error spam

#### Deferred to Stage 2+ (gated on Stage 1 metrics)
- [ ] Torch past a handful of agents; 150+ scale; server-side spawn
  pressure; market pagination; one-hot obs ceiling (embeddings/locality)

### Larger Features
- [ ] Factions and guilds with shared resources and territory
- [ ] PvP arena with opt-in combat and ranking
- [ ] Seasonal world events, invasions, and resource rushes
- [ ] Optional SQLite persistence for scores/world state
- [ ] Prometheus-compatible metrics endpoint
- [ ] Dockerfile and deployment packaging
- [ ] Historical replay system
- [ ] Dashboard bot management: start/stop ML bots
- [ ] ML self-play and population-based training
- [ ] ML relative-position and threat-assessment observations

## Known Issues / Tech Debt

- Room player list in the game protocol is names-only; level details are in the dashboard snapshot.
- Dungeon guard respawn can briefly re-seal a floor during combat.
- ML `drop` currently chooses the first inventory item; it does not use the ground-item helper.
- Party invite requires the target to be in the same room and can be timing-sensitive.
- Dashboard `trade_count` currently double-counts completed fills because both buyer and seller entries increment it.
- Quest unit/live coverage is incomplete.
- The dashboard has no dedicated quest panel yet, although `/api/state.quests` exists.

## Notes

- GM stream: `ws://127.0.0.1:8767`, separate from game port 8765
- Reproducible treasury seed: `TEXTMMO_GM_SEED=700`
- ML checkpoints: `ml/ml_weights.json` and `ml/ml_best.json`
- PyTorch checkpoints: `torch_agents/ml_weights.json` and `torch_agents/ml_best.json`
- Reset leaderboard: replace `scores.json` with `{}`
- Unit tests: `python tests/test_server_unit.py`
- Live tests: start the server with seed 700, then run `python tests/test_live.py` and `python tests/test_env.py`
