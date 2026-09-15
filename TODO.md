# Text MMO Engine - TODO / Roadmap

A forward-looking work list. Completed work is recorded in `CHANGELOG.md`;
only items that still need doing (are open, blocked, or half-done) belong
here, so a future reader is never scanning checkboxes that are already true.

## Priority system

Items are sorted by **priority first, effort second**:

- **P0 — broken now.** Bugs, exploits, or data corruption affecting current
  runs. Fix before any long run.
- **P1 — end-goal enablers.** Required for the stated goal: a scaled,
  MMO-like agent simulation with reliably improving policies (conductor,
  learning quality, long-run observability, verification).
- **P2 — depth.** Content and tooling that enrich the sim once P0/P1 hold.
- **P3 — far-future.** Speculative or large features; do last, if ever.

Effort tags (second sort key): `[XS]` minutes–hours, `[S]` half day–two
days, `[M]` two–five days, `[L]` several days–weeks.

## P0 — broken now

- [ ] [XS] Fix dashboard `trade_count` double-counting buyer and seller entries
- [ ] [XS] Make killing quest-giver NPCs actively punishing: negative score
  on top of near-zero XP/gold, so it's never worth it (currently +0.1 pts /
  +0.1 XP / 0 gold in the kill-split block — small but still positive; check
  `award_xp` clamps negatives safely before wiring it through)
- [ ] [M] Repeated alt-account abuse beyond self-fill rejection (e.g.
  two-account collusion)

## P1 — end-goal enablers

- [ ] [XS] Add explicit crafting recipe profitability assertions
- [ ] [S] Add quest coverage to `tests/test_server_unit.py`:
  accept, craft, turn-in, repeat, Depth Delver, healer remedy/tonic,
  and death-drop gold split/pickup
- [ ] [S] Add action masking for obviously invalid ML actions
- [ ] [S] Add historical player-count and score graphs
- [ ] [S] Add a configurable tuning file for constants currently in `server.py`
- [ ] [S] Add configurable reward-component options for ML training
- [ ] [M] Crafting Economy Phase 5: ML Support
  - Gathering actions and observations
  - Commission actions and state
  - Recipe availability, mastery, buffs, and ammo observations
  - Reset/regenerate checkpoints after observation or action changes
  - Verify crafting-only training behavior
- [ ] [M] Crafting Economy Phase 6: Verification
  - Pure gatherer progression test
  - Fighter-to-crafter supply test
  - Commission escrow/refund test
  - Ammo consumption test
  - Buff duration and stacking test
  - Crafting variety/diminishing-returns test
  - Long crafting-only botfarm comparison against combat baseline
- [ ] [L] Conductor: large-scale agent orchestrator (`ml/conductor/`)
  - Goal: run 50 agents first, then scale to MMO-like hundreds, with
    realistic churn and reliably improving policies. Single supervisor
    process, shared policies, managed cohorts. Decisions already taken:
    mixed linear+torch population with a plugin registry for future types;
    stable+experimental branches per type; adaptive population mix;
    50-agent milestone first.
  - Population model (50-agent start; controller reallocates within floors):
    ~28 linear-stable, ~6 linear-experimental (higher epsilon floor,
    stronger formation scale), ~4 torch-stable (shared DQN), ~2
    torch-experimental (alt aux lambdas); ~6 holdouts (no training updates;
    alternate episodes between stable and experimental weights per type to
    score both branches fairly); ~4 fresh rotating names (newbies for
    discovery data, share their type's stable policy).
  - Plugin architecture (no env/server changes): `registry.py`
    (`register(name, factory, default_cfg)`; interface `act(features,
    epsilon) -> int`, `update(...)`, `save(path)/load(path)`,
    `obs_size`/`n_actions`); `cells.py` (population from config `{type,
    branch, count, hyperparams, weights_path}`; per-cell-branch checkpoints
    such as `ml_weights_linear_stable.json`); `churn.py` (Poisson arrivals
    toward per-cell targets, geometric lifetimes of 20–60 min plus
    rarely-leaving residents, wave starts with jitter); `supervisor.py`
    (one event loop, one policy object per cell-branch, one checkpoint
    writer; every agent task in try/except with backoff respawn);
    `metrics.py` (JSONL per minute: steps/sec, reward rate, trades/min,
    treasury flow, party rate, discovery rate, per-branch holdout fitness).
  - Stable + experimental promotion (per type): promote experimental to
    stable only on holdout-confirmed margin over a rolling window; keep
    last-K stables for rollback; experimental never writes stable directly.
  - Adaptive mixer: reallocate cell targets every N minutes from
    per-branch holdout fitness (reward rate + score velocity + task
    participation); mostly toward the winner, >=5% floor per cell, <=10%
    shift per cycle; fresh-name fraction follows the discovery signal;
    proportional, fully logged, reviewable.
  - Stage 1 acceptance: all 50 step concurrently with wave startup under
    60s; mid-run connection kill respawns alone; per-branch holdout
    fitness with promotion or documented no-promotion; multi-agent market
    volume; hour-long soak with stable steps/sec and bounded scores.json.
  - Deferred to Stage 2+ (gated on Stage 1 metrics): torch past a handful
    of agents; 150+ scale; server-side spawn pressure; market pagination;
    one-hot obs ceiling (embeddings/locality).

## P2 — depth

- [ ] [XS] Update protocol help/README whenever new recipes or item types land
- [ ] [XS] Seed the GitHub wiki (Home, Protocol, World/Quests, Crafting/Economy,
  Dungeons/Parties, Agents, Operations) — BLOCKED: the wiki backend repo
  (`...-mmo.wiki.git`) returns "Repository not found" while the repository is
  private; revisit if it goes public (Home.md draft staged, wiki flag enabled)
- [ ] [XS] Enable branch protection requiring green CI on `master` — BLOCKED:
  GitHub returns 403 (needs Pro or a public repository) while private;
  revisit if it goes public or with a paid plan (exact check contexts
  documented; CI already annotates PRs non-blocking in the meantime)
- [ ] [S] Expose active buffs and complete ammo counts consistently in `stats`,
  dashboard player rows, and dashboard player tracking
- [ ] [S] Add a dashboard quest panel using the existing `/api/state.quests` data
- [ ] [S] Add crafting/material supply panels to the dashboard
- [ ] [M] Crafting Economy Phase 1: Ammo And Buff Foundations
  - Remaining crafted ammo variants and ammo-specific effects
  - Buff duration, category replacement, stacking rules, and expiration
  - Expose ammo and buffs in stats, ML observations, and dashboard
- [ ] [M] Crafting Economy Phase 2: Gathering
  - Safe gathering materials and fighter-only material drops
- [ ] [M] Crafting Economy Phase 3: Crafting Mastery
  - Total crafts, per-recipe mastery, tier mastery, and discoveries
  - Replace flat craft rewards with tier-aware score and XP
  - Material-cost and recipe-profitability tests
- [ ] [M] Dashboard And Documentation (crafting)
  - Crafting/Artisans dashboard tab
  - Gathering nodes, commissions, artisan rankings, buffs, and ammo supply
  - Profitability and material-demand panels
  - Update README, CHANGELOG, protocol docs, and tests for the crafting system

## P3 — far-future

- [ ] [M] Optional SQLite persistence for scores/world state
- [ ] [M] Prometheus-compatible metrics endpoint
- [ ] [M] Dockerfile and deployment packaging
- [ ] [M] Dashboard bot management: start/stop ML bots
- [ ] [M] ML relative-position and threat-assessment observations
- [ ] [L] Factions and guilds with shared resources and territory
- [ ] [L] PvP arena with opt-in combat and ranking
- [ ] [L] Seasonal world events, invasions, and resource rushes
- [ ] [L] Historical replay system
- [ ] [L] ML self-play and population-based training

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
