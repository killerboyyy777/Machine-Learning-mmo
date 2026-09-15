# Changelog

All notable changes to the text MMO engine are recorded here.

## Unreleased
- Social reward now scales with the server difficulty curve (`diminish_factor`
  in `ml_env.py`, mirroring `compute_diminish`): full +0.1 grouped bonus at
  score 0, fading with marginal score gains, so idling in a party can never
  dominate real play late in a run.
- DQN long-run defaults: replay buffer 500 → 10000 transitions and epsilon
  decay 2000 → 50000 steps, so exploration and experience diversity last
  beyond the first minutes of training (applies to single-agent and farm;
  `torch_farm.py` uses these defaults).
- Long-term sustainability bounds: score entries untouched for 7 days are
  evicted past a 2000-entry cap (never online players or banked-gold
  creditors, track/score history goes with them), completed/cancelled
  commissions older than 1 hour are pruned (open escrow bounties never),
  dungeons are capped at 50 floors, and stale party invitations die with the
  connection — so fresh-name farming can't grow memory or `scores.json`
  without limit.
- Renamed `torch/` to `torch_agents/`: the old directory name shadowed the
  real PyTorch package, so `import torch` resolved to the local folder and the
  DQN could never load. All launchers, imports, docs, and `requirements.txt`
  updated; `numpy==2.5.3` (verified) added to `requirements.txt` since PyTorch
  needs it for tensor conversion.
- Buff observations wired end to end: the ML env now parses the `buffs` map
  from `stats` events and `flatten_obs()` carries `buff_attack` / `buff_dr`
  (OBS_SIZE 156 → 158; old checkpoints warn and restart fresh instead of
  crashing, but retraining is recommended).
- Fixed `tests/test_persistence.py` and script-mode launches: `ml_client.py`
  / `ml_botfarm.py` fall back to absolute imports when run without a parent
  package, and `torch_agents/torch_farm.py` bootstraps its own `sys.path`
  for both `dqn_agent` and `ml_env`.
- Quest giver category system added: `QUEST_GIVERS` registry plus
  `is_quest_giver` / `get_quest_giver` / `get_quest_givers_in_room` helpers.
  Kill rewards, quest lookups, and the dashboard catalog all read from this
  one table, so adding a new quest NPC is a single-dict-entry change.
  Killing a registered quest giver now pays only 0.1 pts / 0.1 XP / 0 gold,
  making quest-NPC farming never worth it.
- Gathering system added: 8 respawnable `gather_nodes` in `world.json`
  (timber, feathers, fiber, springwater, herbs, resin, scrap iron, salt),
  a `gather` command with per-node yield ranges and cooldowns ticking on the
  NPC loop, plus score/XP awards and room `gatherables` visibility.
- Commission system added: `commission_post` / `commission_list` /
  `commission_fill` / `commission_cancel` with true gold escrow (poster locks
  the reward up front; unfunded bounties are rejected), half-escrow refunds
  on cancel, self-fill rejection, a single combined score award, XP routed
  through `award_xp` (level-ups fire), and gold paid straight to the live
  filler instead of the offline bank.
- Crafted buff foundation added: consumables can carry a `buff` definition;
  using one consumes it, applies a category-scoped action-duration effect,
  replaces an existing effect in that category, exposes active buffs in
  `stats`, and applies attack bonuses / incoming-damage reduction without
  changing ordinary healing-item behavior.
- Crafting foundation: `cmd_craft` now supports optional recipe `output_qty`
  (default 1), so bundle recipes consume inputs once and create multiple
  outputs. The existing arrow recipe now produces 5 arrows and carries
  tier/category metadata; existing recipes remain behavior-compatible.
- First usable artisan consumables added to `world.json`: Field Ration,
  Field Bandage, Sharpening Oil, Fortitude Tonic, Greater Sharpening Oil, and
  Ironhide Draught. Buff items now apply action-duration attack or
  damage-reduction effects through generic `cmd_use`; effects replace within
  their category, appear in `stats`, and are tested with real crafting.
- Added `torch/torch_farm.py` and `torch/torch_farm.bat`: multiple Torch
  agents now share one DQN, replay buffer, optimizer, and checkpoint writer
  in a single asyncio process. This replaces the unsafe multi-process pattern
  where four agents independently overwrote the same checkpoint; a 4-agent,
  523-transition run crossed replay warm-up and completed successfully.
- Fixed an existing `ml_env.step()` regression where quest-info construction
  referenced undefined `obs` and returned undefined `done`; it now builds and
  returns the current observation and correct episode status.
- Checkpoint persistence verified and improved for both agents: the linear ML
  client now saves atomically with `training_steps`, shape metadata, and
  validated bias/action dimensions; its exploration schedule resumes across
  runs. The Torch DQN now saves atomically with Q/target/optimizer state,
  training/learning steps, best score, and shape metadata; its global step and
  exploration schedule also resume. Added `tests/test_persistence.py` and
  verified cross-process resume (steps 5 -> 10) for both agents.
- Added `torch/torch_batch_loop.bat`: runs exactly four Torch agents in
  parallel, waits for all four worker markers, then starts four fresh named
  agents in the next round. Supports steps-per-agent and optional batch-count
  arguments; per-agent logs are retained under `%TEMP%`.
- Verification pass completed across the current server, ML environment,
  linear ML client, botfarm, and PyTorch DQN: all Python modules compile;
  unit, quest-grind, live protocol, and ML env suites pass; ammo and crafting
  behavior pass direct live checks; `ml_client` and `ml_botfarm` complete live
  runs; Torch completes a 510-step live run through replay warm-up and finite
  backprop losses while selecting all 35 actions.
- Fixed `ml_botfarm.py`: `--checkpoint-every` was previously ignored, and an
  invalid current checkpoint prevented fallback to a valid best checkpoint.
  Evaluation now respects the configured step interval and fallback loading.
- Hardened World-map rendering against both legacy exit dictionaries and the
  current exit-object arrays, preventing a malformed/empty exit payload from
  stopping the rest of the dashboard from rendering.
- Dashboard HTML fix: removed duplicated line in `renderRooms()` that was
  causing the entire dashboard to display blank.
- World tab now includes a connected surface-world map. Room nodes are laid
  out from Town Square, connections are labeled with exit directions, and
  player counts are shown on each node. Private dungeon instances remain in
  the Dungeons tab rather than being mixed into the shared map.
- World dashboard now renders a connected surface-world SVG map with rooms,
  labeled exits, player counts, and Town Square highlighted. Private dungeon
  floors remain in the Dungeons tab rather than being mixed into the shared
  surface graph.
- World expanded 4x (8 → 32 rooms) across six new regions: Harbor Lane /
  Salt Docks / Chandlery / Tide Pools (east of Old Shop), Artisan Row /
  Tannery / Smithy / Deep Forge (west of Market), Stillwater Lake / Reed
  Beds / Lake Shrine / Heron Nest (east of Healing Spring), Pinewood
  Thicket / Frozen Pass / Lumber Camp / Storm Summit (north of Deep
  Forest), Highland Trail / Eagle Roost / Stone Circle / Sky Nest (east of
  Mountain Pass), and Crypt Hall / Burial Chamber / Bone Pit / Deep
  Catacombs (below the Graveyard). 15 new NPCs in matching tiers
  (Mud Crab → Storm Eagle / Ice Wraith), 6 new items (Oak Longbow, Iron
  Plate, Serpent Scale, Frost Crystal, Iron Ore, Mountain Berry) plus a
  third recipe (2x Iron Ore + Wolf Pelt → Iron Plate, reachable by agents
  via the new `craft_iron` env action), a new Arrow item with a bow-ammo
  rule (Oak Longbow declares `"ammo": "arrow"`: 1 arrow consumed per shot,
  empty quiver refuses to fire; arrows sold by the merchant for 2g, dropped
  by Cave Bandits, stocked at the Lumber Camp; agents get a `buy_arrows`
  action and an `arrows_norm` count scalar. Arrows are also craftable via
  `craft_arrows`: 1x Iron Ore → 1 Arrow. The agent action space grows
  34 → 35; observation remains 124 dims), and
  ground loot
  seeded through the new regions. Only existing direction words used, so
  movement actions are unchanged; observation grows 70 → 124 dims
  (old checkpoints need retraining). Dashboard room cards now render exit
  tags (`north → Forest Edge`, ...) from live snapshot data -- the snapshot
  `rooms` entries carry an `exits` list, so connections stay correct for any
  `world.json` with zero hardcoding. Verified: exit-bidirectionality +
  reference audit, unit + grind suites, and live `test_live.py` /
  `test_env.py` runs against the big map.
- Five new GM powers (all treasury-priced, dashboard GM tab only):
  `gm_announce` (server-wide broadcast, 25 flat), `gm_heal` (full heal,
  2 tax/missing HP), `gm_teleport` (relocate to any static room, 50 flat),
  `gm_slay` (kill a living NPC anywhere incl. stuck dungeon guards, 1 tax/HP
  min 10, loot drops, floors unseal), `gm_kick` (disconnect, free).
- GM tab rebuilt around an action picker: one focused sub-form per power
  with player/item/room/NPC autocomplete, quick presets, and live
  per-action tax-cost previews (heal/slay priced from live snapshot data).
  Also fixed two latent bugs: the Spawn-boss button referenced a nonexistent
  strength input, and the treasury readout wrote to a nonexistent element
  (cost previews always read "insufficient").
- Dashboard **Track character** panel: pick one online character from a
  dropdown to follow their recent actions (per-character log, last 30 each,
  exposed as `recent_actions` in `/api/state`), with a live header (level,
  room, score, HP, gold). Survives busy servers where the world-wide feed
  scrolls past a single client's history.
- Tests moved into the repo (`tests/test_server_unit.py`,
  `tests/test_live.py`, `tests/test_env.py`, all runnable from the repo
  root with path bootstraps + run docs); `test_env.py` seeds GM gold via
  the dedicated GM port now that the game port rejects `gm_*`.
- New `requirements.txt` (`websockets==17.1`, with the CPU-only PyTorch
  install line documented for `torch/dqn_agent.py`); README setup +
  a "Running tests" section updated to match.
- All `.bat` launchers audited and fixed: `torch_bots.bat` had an off-by-one
  loop, broken `%bot_name%` expansion, and pointed at the pre-CLI script
  (rewritten with delayed expansion, input guard, correct names);
  `torch_bot.bat`/`start.bat` gained titles, unbuffered output, exit codes,
  usage hints, and the GM stream port; `ml_*.bat` needed no changes.
- `torch/torch.py` rescued into a working agent: renamed to
  `torch/dqn_agent.py` (the old name shadowed the real PyTorch package so
  `import torch` could never resolve), real PyTorch CPU installed, fixed
  fatal train-loop bugs (Q-values read off the heads dict instead of the
  Q-head, `.get()` called on the flattened observation list, `None` losses
  logged during replay warm-up, frozen epsilon), added a real CLI
  (`--demo`/`--name`/`--steps`/`--save-every`), fixed the `.bat` launchers
  (correct script name, working multi-bot loop, `ml/` import path), and
  corrected the docs (separate `torch/ml_weights.json` checkpoints, not
  shared with `ml_client.py`). Verified: offline `learn()` losses finite,
  live `--demo` smoke test passes (67-float obs, 30 actions).
- Repeatable quests (server): `quest` + `quest_accept` / `quest_turn_in`
  with a `quest` selector field, both quests given by the Town Guard in Town
  Square (accept and turn-in require standing by him). Charm quest: craft the
  Ancient Guardian Charm (second recipe: Treant Bark + Troll Hide + Ectoplasm)
  for 50 XP / 25 gold / 15 score. Delver quest: clear `QUEST_DELVER_FLOORS`
  new dungeon floors for 30 XP / 15 gold / 10 score. Quest flags in `stats`
  events and `scores.json`; `/api/state` exposes a `quests` section
  (catalog, live actives, lifetime completions, turn-ins/min — not yet
  rendered by the dashboard). ML side: symbolic `QUESTS` catalog mirror in
  `ml_env.py`, 7-feature charm block + 3-feature delver block (OBS 58 → 70),
  `quest_accept` / `quest_turn_in` / `craft_charm` / `quest2_accept` /
  `quest2_turn_in` actions (N 27 → 32) with per-step quest transitions in
  `step()` info, and a fifth (quest-value) auxiliary head in the torch DQN.
- GM commands isolated on a dedicated loopback stream (`ws://127.0.0.1:8767`,
  `GMStream` + `GM_HANDLERS`): the game port rejects all `gm_*` outright and
  the pre-login GM exemption is gone, so game clients can never execute GM
  actions — only the dashboard's GM tab, which now connects to the GM port.
- Level visibility: server logs, command log, and disconnect lines carry
  `(lvN)`; level-ups broadcast to the room and are logged; welcome message,
  `who`, room tags, activity feed, and perf/scores tables show levels.
- GM tab usability: player/item/room autocomplete datalists (fed by a new
  snapshot `catalog`), quick-preset buttons for gold/buffs/boss strength,
  and live per-action tax-cost display.
- Dashboard Market tab now shows a **buy & sell history** table (time, buyer,
  seller, item, price, tax, seller payout) for the latest completed trades.
- Market tax is now 10% with a **minimum of 1 gold** (`TAX_MINIMUM`), so cheap
  trades still feed the treasury; the dashboard shows the rate as "10% (min 1)".
- ML env is tax-aware: `market_tax()`/`market_net()` mirror the server formula
  exactly, the observation adds live `tax_rate`, `tax_min_norm`, and the exact
  after-tax net of the agent's own listings (`own_net_norm`, OBS 55 → 58), and
  `step()` info carries `gold_delta`, tax terms, detected buy fills, and own
  orders. `torch/dqn_agent.py` uses these for exact after-tax market P&L targets (buys
  cost full price, fills net price-minus-tax) instead of the old gold-delta
  proxy; stale `ml/` weights were reset for the new observation size.

## 0.5 — parties, instanced dungeons, player market, GM mode, leveling (2026-09-14)

### New: parties + instanced dungeons
- `Party` dataclass with leader/members (max `PARTY_MAX_MEMBERS` = 4);
  commands `party_invite`, `party_accept`, `party_leave`, `party_info`.
- Dungeons are now **instanced per party**, not one shared map. Entering the
  graveyard's `enter` doorway auto-creates a solo party and instantiates a
  private, infinitely-deep staircase (`Dungeon` class, `d_{id}_f{n}` rooms).
- Floors are built lazily on first entry (`Dungeon.floor()`), so depth is
  unbounded with no startup cost. Guard HP/ATK/count scale by floor as before.
- Exit rules: floor 1 always has an `up` escape; deeper uncleared floors
  are sealed until every guard dies (`check_dungeon_clear`), then expose
  `up`/`down`. Guard respawn re-seals a floor and withdraws the blade.
- Leaving/destroying a solo party destroys its dungeon instance
  (`_delete_party`); stranded players are relocated via `_relocate_from_dungeon`.
- Removed the old static `generate_dungeon()`; `DUNGEON_ROOMS` kept as an
  empty set for `ml_env` compat.

### New: player market + GM treasury
- Market commands work from anywhere in the world: `market_post`,
  `market_list`, `market_cancel`, `market_buy` (with no `id`, auto-buys the
  cheapest affordable order).
- Trades pay `TAX_RATE` (10%) into `tax_treasury` (spendable) and track
  `tax_collected_lifetime` (stat) + per-seller `tax_paid` + `trades_completed`.
- Offline sellers are paid via `gold_bank`, credited on next login.
- `TEXTMMO_GM_SEED` env var seeds the treasury at startup.

### New: GM mode (loopback-only, no auth)
- `gm_reward` inject gold (1:1) or drop an item to a player/room.
- `gm_buff` activates a timed **2× XP or gold** world event
  (`GM_BUFF_COST_PER_MINUTE` = 50 tax/min, capped 60 min).
- `gm_boss` spawns an Elite N-star Menace in a room
  (`GM_BOSS_COST_PER_STRENGTH` = 100 tax/star; HP 30+40N, ATK 5+4N; herbs on
  death; respawn_seconds 0 = stays dead).
- Gate is purely loopback (`_is_gm`: host in 127.0.0.1/::1). GM commands work
  pre-login; all other commands still require login.

### New: leveling & XP
- `xp_to_next(level) = round(100 × 1.5^(level-1))`; XP from kills, discovery,
  dungeon floor clears, crafting, and market trades. XP buff doubles gains in
  `award_xp`.
- Level-up grants +5 max HP (+1 base attack) and a full heal
  (`_apply_level_up`, `level_up` event). `sync_player_level` applies persisted
  level on login. Levels persist per-entry in `scores.json`.

### Dashboard
- Rewritten `dashboard.html` with tabs: World / Market / Dungeons / GM.
- Market tab: treasury, lifetime tax, tax rate, trades, open orders.
- Dungeons tab: buffs, live GM bosses, per-instance floor progress.
- GM tab: WebSocket console that sends GM commands locally (no auth).

### ML
- `ml_env.py` obs/action space extended for all 0.5 systems: is_dungeon +
  floor features, exit mask incl. `enter`/`up`/`down`, inventory presence,
  unknown-NPC count (dungeon guards), party/market/level scalars; actions
  include `move_up/down/enter`, `buy/sell/equip/use/craft`,
  `market_post/buy/cancel/list`, `party_invite/accept/leave/info`.
- Observation vector rebuilt (55 floats), `ml_weights.json`/`ml_best.json`
  reset to match.

### Removed
- `client.py` and `bot_example.py` deleted (README/start.bat updated); the
  WebSocket protocol + README serve as the client reference, and `ml/` holds
  the scripts that still ship.

## 0.4 — name security, respawn balance, reliability fixes (2026-09-14)

### New: name security & identity tokens
- Only one connection may hold a given name at a time. Duplicate logins are
  rejected with a clear error.
- Optional per-name `token` on login. First login with a token claims the name;
  future logins without the same token are rejected. Tokens persist in
  `scores.json` across server restarts.
- New env knob `TEXTMMO_REQUIRE_TOKEN` (truthy values: `1`, `true`, `yes`, `on`):
  when set, every name *must* include a token on first login or it cannot be
  claimed.
- Bot-farm characters remain unaffected: each uses a unique name and no token.

### New: balanced NPC respawn system
- All NPCs now respawn on a timer after death, including dungeon guards. The
  world never permanently runs out of content.
- Dungeon guard respawn timers scale with depth: `20 + 10 × floor` seconds
  (floor 1 = 30 s, floor 10 = 120 s).
- When a dungeon guard respawns on a previously cleared floor:
  - The floor **re-seals** (exits locked, moves blocked).
  - The `Dungeon Blade` reward is withdrawn from the floor.
  - Players must fight the guard again to re-clear.
- New helper `respawn_npc(npc)` used by both the NPC AI loop and the test
  suite, keeping respawn + re-seal logic in one place.

### Fixed: head-of-line blocking (high-risk)
- Each player now gets a bounded outbound queue (`deque`, default 64 entries,
  env `TEXTMMO_OUTBOUND_QUEUE`).
- `send()` no longer awaits `ws.send` directly; it only enqueues a JSON string.
  A dedicated `_outbound_writer` task drains the queue and awaits `ws.send`.
- When a slow client's queue fills, the *oldest* message is dropped rather than
  stalling every other player's event loop.

### Fixed: dashboard thread race (high-risk)
- `world_snapshot()` is now built exclusively on the event-loop thread and
  stored as a cached JSON string (`world_snapshot_json`).
- The `ThreadingHTTPServer` handler serves the cached string directly — no
  live structure iteration from the HTTP thread.
- A new `dashboard_refresh_loop()` updates the cache every
  `SNAPSHOT_REFRESH_SECONDS` (default 1.0).

### Fixed: fragile background tasks (high-risk)
- New `_run_resilient(task_name, coro_factory)` wrapper. If `npc_ai_loop`,
  `scores_save_loop`, or the dashboard refresh loop crashes with any exception
  other than `CancelledError`, it logs the error and restarts after
  `TASK_RESTART_DELAY` (2.0 s).
- A crash in one task can no longer permanently freeze NPC AI, score
  persistence, or the dashboard.

### Fixed: `cmd_drop` not syncing room
- `drop` now calls `sync_room` so other players see dropped loot immediately.

### Misc
- Player outbound queue and `outbound_event` fields added to the `Player`
  dataclass; `collections.deque` imported.
- `name_owners` dict (name → connection id) manages the name-reservation
  registry.
- `handle_connection` creates/cancels the writer task on connect/disconnect and
  releases `name_owners` on logout.
- `CHANGELOG.md` created; README updated with identity/auth docs, dungeon respawn
  rule, new env knobs, and the outbound queue / resilient-task sections.

## 0.3 — dungeon & scaling (2026-09-14)

- Procedural dungeon generation (`generate_dungeon()`): 10 floors (configurable
  via `TEXTMMO_DUNGEON_DEPTH`), HP ×1.35/floor, attack ×1.5/floor, 1→4 guards,
  loot: relics + dungeon blade.
- Sealed floors (exits hidden in `room_view`, moves rejected), `check_dungeon_clear`
  unlocks on full kill, blade reward.
- Per-room player cap (`TEXTMMO_MAX_ROOM_PLAYERS`, default 12) and global cap
  (`TEXTMMO_MAX_CONNECTIONS`, default 1000).
- Room-members and player-name indexes for O(1) lookups.
- Debounced `scores.json` flush every 5 s; `TEXTMMO_SCORES_SAVE_SECONDS`.
- `cmd_drop` now syncs room to other players.

## 0.2 — dashboard & polish (2026-09-14)

- Live dashboard on port 8766: world map, per-client stats, leaderboard,
  activity feed; `/api/state` endpoint.
- `start.bat` launch script for Windows.
- ML code moved to `ml/` subfolder.
- README: dashboard docs, LLM agent setup, scoring, multiplayer mechanics.

## 0.1 — initial release (2026-09-13)

- Rooms, NPCs, items, combat, loot, respawn, player-to-player `give`.
- WebSocket JSON protocol: `login`, `look`, `move`, `attack`, `take`, `drop`,
  `equip`, `use`, `rest`, `buy`, `sell`, `craft`, `give`, `inventory`,
  `stats`, `say`, `who`, `leaderboard`, `help`.
- Scoring with variety decay and global difficulty curve.
- `client.py`, `bot_example.py`, `world.json`.
- ML environment (`ml_env.py`) with fixed-size observations and discrete
  action space.
- Online Q-learning agent (`ml_client.py`), multi-bot farm (`ml_botfarm.py`).