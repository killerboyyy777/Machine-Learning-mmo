# Text MMO Engine - TODO / Roadmap

> **The roadmap lives in GitHub Issues:
> https://github.com/killerboyyy777/Machine-Learning-mmo/issues —
> every point below used to live here and now has a corresponding issue.**
> This file is local-only and intentionally untracked (see `.gitignore`);
> do not commit it.

## Working with issues

- **Create:** green "New issue" button on the Issues page, or from a
  checkout: `gh issue create --title "..." --label "P1,S,area:tests"
  --body "..."`. Always add one `P0`–`P3` label, one effort label
  (`XS`/`S`/`M`/`L`), and at least one `area:*` label.
- **Find:** `gh issue list`, `gh issue view <number>` (full body +
  comments); `gh issue list --label P0`, `--label "area:market"`,
  `--search "treasury"`; on the web, the Issues tab filters by label,
  plus text search and sort by newest/recently updated.
- **Sort/prioritize:** the labels ARE the priority system — `P0` broken
  now, `P1` end-goal enabler, `P2` depth, `P3` far-future; effort
  `XS`→`L` second; `area:*` scopes the work (`server`, `ml-env`,
  `torch-agents`, `dashboard`, `market`, `quests`, `tests`, `docs`,
  `conductor`); `balance`/`bug` mark the kind. For planning runs like
  `gh issue list --label P1 --limit 100`, then pick by effort.
- **Close:** `gh issue close <number>`, or write `Closes #<number>` in a
  commit message / PR description.

## Priority system (mirrored by labels)

- **P0 — broken now.** Bugs, exploits, or data corruption affecting current
  runs. Fix before any long run.
- **P1 — end-goal enablers.** Required for the stated goal: a scaled,
  MMO-like agent simulation with reliably improving policies (conductor,
  learning quality, long-run observability, verification).
- **P2 — depth.** Content and tooling that enrich the sim once P0/P1 hold.
- **P3 — far-future.** Speculative or large features; do last, if ever.

## P0 — broken now (tracked as issues #1–#3)

- [ ] Dashboard `trade_count` double-counting → issue #1
- [ ] Quest-giver kills punishing → issue #2
- [ ] Alt-account commission collusion → issue #3

## P1 — end-goal enablers

- [ ] Add explicit crafting recipe profitability assertions
- [ ] Add quest coverage to `tests/test_server_unit.py`:
  accept, craft, turn-in, repeat, Depth Delver, healer remedy/tonic,
  and death-drop gold split/pickup
- [ ] Add action masking for obviously invalid ML actions
- [ ] Add historical player-count and score graphs
- [ ] Add a configurable tuning file for constants currently in `server.py`
- [ ] Add configurable reward-component options for ML training
- [ ] Crafting Economy Phase 5: ML Support
  - Gathering actions and observations
  - Commission actions and state
  - Recipe availability, mastery, buffs, and ammo observations
  - Reset/regenerate checkpoints after observation or action changes
  - Verify crafting-only training behavior
- [ ] Crafting Economy Phase 6: Verification
  - Pure gatherer progression test
  - Fighter-to-crafter supply test
  - Commission escrow/refund test
  - Ammo consumption test
  - Buff duration and stacking test
  - Crafting variety/diminishing-returns test
  - Long crafting-only botfarm comparison against combat baseline
- [ ] Conductor: large-scale agent orchestrator (`ml/conductor/`)
  - (full plan in the issue; mixed linear+torch registry, stable+
    experimental branches, adaptive mixer, 50-agent milestone)

## P2 — depth

- [ ] Update protocol help/README whenever new recipes or item types land
- [ ] Expose active buffs and complete ammo counts consistently in `stats`,
  dashboard player rows, and dashboard player tracking
- [ ] Add a dashboard quest panel using the existing `/api/state.quests` data
- [ ] Add crafting/material supply panels to the dashboard
- [ ] Crafting Economy Phase 1: Ammo And Buff Foundations
  - Remaining crafted ammo variants and ammo-specific effects
  - Buff duration, category replacement, stacking rules, and expiration
  - Expose ammo and buffs in stats, ML observations, and dashboard
- [ ] Crafting Economy Phase 2: Gathering
  - Safe gathering materials and fighter-only material drops
- [ ] Crafting Economy Phase 3: Crafting Mastery
  - Total crafts, per-recipe mastery, tier mastery, and discoveries
  - Replace flat craft rewards with tier-aware score and XP
  - Material-cost and recipe-profitability tests
- [ ] Dashboard And Documentation (crafting)
  - Crafting/Artisans dashboard tab
  - Gathering nodes, commissions, artisan rankings, buffs, and ammo supply
  - Profitability and material-demand panels
  - Update README, CHANGELOG, protocol docs, and tests for the crafting system

## P3 — far-future

- [ ] Optional SQLite persistence for scores/world state
- [ ] Prometheus-compatible metrics endpoint
- [ ] Dockerfile and deployment packaging
- [ ] Dashboard bot management: start/stop ML bots
- [ ] ML relative-position and threat-assessment observations
- [ ] Factions and guilds with shared resources and territory
- [ ] PvP arena with opt-in combat and ranking
- [ ] Seasonal world events, invasions, and resource rushes
- [ ] Historical replay system
- [ ] ML self-play and population-based training

## Notes

- GM stream: `ws://127.0.0.1:8767`, separate from game port 8765
- Reproducible treasury seed: `TEXTMMO_GM_SEED=700`
- ML checkpoints: `ml/ml_weights.json` and `ml/ml_best.json`
- PyTorch checkpoints: `torch_agents/ml_weights.json` and `torch_agents/ml_best.json`
- Reset leaderboard: replace `scores.json` with `{}`
- Unit tests: `python tests/test_server_unit.py`
- Live tests: start the server with seed 700, then run `python tests/test_live.py` and `python tests/test_env.py`
