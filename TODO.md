# Text MMO Engine - TODO / Roadmap

A forward-looking work list. Completed work is recorded in `CHANGELOG.md`;
only items that still need doing (are open, blocked, or half-done) belong
here, so a future reader is never scanning checkboxes that are already true.

## Priority: Small ("XS" — minutes to two days)

- [ ] Fix dashboard `trade_count` double-counting buyer and seller entries
- [ ] Add quest coverage to `tests/test_server_unit.py`:
  accept, craft, turn-in, repeat, Depth Delver, healer remedy/tonic,
  and death-drop gold split/pickup
- [ ] Add ammo and buff unit tests
- [ ] Add explicit crafting recipe profitability assertions
- [ ] Update protocol help/README whenever new recipes or item types land
- [ ] Expose active buffs and complete ammo counts consistently in `stats`,
  dashboard player rows, and dashboard player tracking
- [ ] Add a dashboard quest panel using the existing `/api/state.quests` data
- [ ] Add action masking for obviously invalid ML actions
- [ ] Add crafting/material supply panels to the dashboard
- [ ] Add historical player-count and score graphs
- [ ] Add a configurable tuning file for constants currently in `server.py`
- [ ] Add configurable reward-component options for ML training

## Priority: Medium (two to five days)

### Crafting Economy Phase 1: Ammo And Buff Foundations
- [ ] Add ammo families and compatible ammo validation
- [ ] Add remaining crafted ammo variants and ammo-specific effects
- [ ] Add buff duration, category replacement, stacking rules, and expiration
- [ ] Expose ammo and buffs in stats, ML observations, and dashboard

### Crafting Economy Phase 2: Gathering
- [ ] Add respawnable gathering-node definitions
- [ ] Add `gather` command and node cooldowns
- [ ] Add safe gathering materials and fighter-only material drops
- [ ] Add gathering score, XP, variety signatures, and tests

### Crafting Economy Phase 3: Crafting Mastery
- [ ] Add tiered consumable, ammo, utility, and below-dungeon equipment recipes
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
