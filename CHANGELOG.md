# Changelog

All notable changes to the text MMO engine are recorded here.

## Unreleased
- Torch farm reward parity (#234): transitions carried count-loot,
  buy-only P&L, zero intrinsic and no curiosity while learn() shaped on
  those keys. Now mirrors single-agent targets (value loot, fill P&L,
  accept/progress intrinsic, per-step RND).
- Torch warmup gate (#222): learning waited for the FULL 10k replay
  buffer, so default --steps runs did zero gradient steps behind warmup
  logs. Now keys on a fillable minibatch (32) in solo + farm paths.
- RND novelty ordering (#245): the test never compared novel vs seen, so
  a dead novelty signal passed; now asserts raw-error ordering.
- Eval run tags (#233): challenger/baseline shared `Eval{seed}` names, so
  persisted score entries contaminated the paired comparison. Names now
  carry a per-checkpoint tag; seeds (pairing) unchanged.
- Eval sample std (#247): `report()` used population std, understating
  spread vs `compare()`; now sample std with test cover.
- PBT exploit reload (#228): the winner-copy never reached live losers;
  the supervisor now restarts loser tasks with fresh envs/policies that
  reload the checkpoint (dead entries refused, shutdown hardened to join
  tasks). Follow-up: the stored closure still ran stale in-memory weights
  and mutated hparams never reached the live policy -- restart now
  rebuilds the policy from the loser's checkpoint file with hparams
  overlaid (schema keys only) and re-assigns the loser to the mixer
  (covered by RESTART_WEIGHTS_OK / RESTART_FACTORY_OK).
- Conductor ghost agents (#230): failed starts stayed alive forever
  (episode-aged lifetimes never expire taskless entries); now marked dead
  at failed start, which also makes the alive-based soak gate sound
  (crashed tasks already kill their entries in recovery).
- Shutdown task lifecycle (#244): background loops are tracked and
  cancelled before the final scores save (no concurrent save_scores);
  they also start only after both listeners bind (refines #242).
- Startup listener hygiene (#242): a failed GM bind now closes the game
  listener before propagating instead of leaving it bound.
- Dashboard GM origin (#236): the console dialed hardcoded 127.0.0.1, so
  GM failed on any remote dashboard; now follows location.hostname.
- Dashboard market defaults (#246): `renderMarket` crashed on partial
  snapshots; now defaults like the Overview panel (node-verified).
- Quest config actually applies (#251): the `quests` section (plus other
  late-defined tunables) never took effect -- `_apply_config` ran before
  those globals existed. Single apply pass at module bottom + catalog
  sync; covered by QUEST_CONFIG_OK.
- Scripted delver gating (#235): DungeonPlugin ordered ungated turn_in
  before accept, making accept unreachable; now ordered by quest state.
- GM reward validation (#223, implemented in #252): `gm_reward` spent
  treasury gold before resolving the recipient (empty/offline targets
  burned funds); now validates first for both gold and items, covered by
  `tests/test_gm_unit.py`.
- Buff tick gating (#237): read-only commands (look/stats/inventory/who/
  leaderboard/help, list views, login, quest list) no longer consume
  action-based buffs.
- Gather cap truncation (#232): multi-yield harvests truncated to the
  remaining pack space instead of overflowing past 24 units.
- Party-switch relocation (#226): accepting a new invite while standing
  in the old party's dungeon now relocates to the entrance like
  party_leave, instead of haunting the wrong instance.
- Health probe thread-safety (#224): `/health` iterated the live players
  dict from the HTTP thread (intermittent RuntimeError); now served from
  the cached snapshot like `/api/state`.
- Login version warning (#243): `welcome` now carries `version_mismatch`
  when the client sent a different protocol version (additive field;
  old clients unaffected). (Wiki Protocol doc to follow.)
- Guard-charm pre-farm (#239): accepting with an already-crafted charm no
  longer wipes the crafted flag (no forced double craft).
- Party invite hygiene (#248): invites record the inviter and are purged
  when the inviter disconnects, so nobody joins a leaderless party.
- Login room cap (#227): fresh connections could overflow a full start
  room; logins now refuse like moves when the destination is at cap.
  Follow-up: the crowded check ran after entry creation and the token
  claim, so a rejected probe squatted the victim's `auth_token` (locking
  out the real owner) and polluted SCORES toward the cap. The capacity
  check now runs before any login state mutation.
- Config unknown-key warning (#241): typo'd/wrong-nesting keys vanished
  silently; now logged like bad values.
- Market expand accounting (#240): the stall-slot fee hit `tax_treasury`
  but skipped `tax_collected_lifetime` like every other sink; now both.
- Farm checkpoint resume (#231): `save_weights` wrote weights+bias only,
  resetting `training_steps` (and the epsilon schedule) on every restart.
  Now saves the full LinearQAgent format; covered by
  `tests/test_farm_resume.py`.
- Spawn-to-target churn (#214): each tick tops up toward the registry cap
  (at most `top_up_per_tick`, default 1) after the Poisson arrival, so
  episode-driven deaths can't bleed the population below cap on fast
  machines; soak `--arrivals` stays the healthy-state trickle.
- Starting purse (#258): brand-new characters log in with `STARTING_GOLD`
  (10, tunable in `server_config.json` economy) to break the 0-gold
  poverty trap; granted once per score entry (TTL-evicted entries re-grant
  on return -- a ~10g/week welcome-back stipend at most), never topped up
   otherwise.
- New unit suites (#252): training loops, conductor run, and GM treasury
  coverage in CI; `gm_reward` validates the destination before spending,
  so failed rewards no longer debit the treasury with nothing delivered.
- Quest-giver immunity (#193): player attacks on quest givers rejected
  pre-damage (GM slay untouched); repeated attempts, merchant control,
  and untouched quest flow covered by tests.
- Combat/party/dungeon balance + race pass (#195): same-tick double-kill
  guard (one payout split among contributors, never two); 60s dungeon
  re-entry delay after abandoning an uncleared descent (kills Floor-1
  reset farming); floor-clear credit + delver progress require
  contribution (idle walk-ins earn nothing); level-ups heal gained max HP
  only (no mid-combat full reset); sheltered escrow/bank wealth counts
  toward the death score penalty (movement unchanged); XP scales by the
  score variety x diminish curve; party-invite overwrite notifies the old
  leader, accept relocates out of the old dungeon, kill gold splits among
  present contributors only; commercial half-up tax rounding with 1g
  trades paying out in full.
- Env equip-mask fix (#229): the worn-weapon exclusion compared an item
  id against a display name, so it never fired and `equip` stayed valid
  after equipping. Now compares display names. Same dims.
- Env item presence fix (#221): `item_presence` / `inv_presence` looked
  ids up in a name->id map, so both 39-dim vectors were all-zeros forever
  -- policies were blind to ground loot and inventory. Now resolved via
  `ITEM_ID_TO_NAME` like the NPC line. Same dims (no checkpoint break),
  but inputs change distribution: retraining recommended.
- Docker full stack (#71): `Dockerfile` (lean `python:3.12-slim`,
  torch only via `INSTALL_TORCH=1`) + `compose.yaml` -- `docker compose up`
  runs server (8765) + dashboard (8766) + 4 scripted bots, with `soak`
  (conductor, 6 agents/2min) and `torch-farm` profiles. `TEXTMMO_GM_HOST`
  env (default `127.0.0.1`) lets compose reach `ws://server:8767`
  internally; GM stays unpublished to the host (no auth). Protocol
  unchanged (`PROTOCOL_VERSION = 1`).
- Env expressiveness (#194) + remedy/tonic quests (#183): `market_post`
  priced (undercut best ask, else merchant value + 1), `commission_post`
  parameterized from state (hostile target or `rat` default, escrow up to
  10g), affordability-aware `market_buy`, merchant-gated `sell` /
  `buy_arrows`; `combat` / `inventory` events parsed; dynamic shards
  resolve through live `ITEM_DEFS`. Remedy/tonic join the QUESTS mirror,
  stats flags, transitions, `by_quest`, quest3/4 obs (+6 dims → 181) and
  four new stage-2 actions, with torch aux targets in both trainer paths.
  Checkpoints restart fresh; no server changes. `party_leave` / `info`
  deliberately ungated (room-event size lags joins).
- Conductor episode metrics (#210): the supervisor logs every completed
  episode to `metrics.jsonl` (unsampled; rotation caps are the volume
  guard), so the reward non-collapse gate finally has input.
- Dashboard render fix (#211): `renderAll` called bare `render()` while
  every section is an `s => ...` arrow -- all tabs threw `s is undefined`
  behind a `live` status. One-line pass-through.
- Soak arrivals default 2.0 → 5.0/min (#214, part 1): post-#198 deaths
  are episode-driven (~2.2/min at 50 agents) while arrivals were a fixed
  wall-clock trickle, so the nightly converged to ~35/50 under a 40 gate.
  5/min holds the cap with headroom; part 2 is rate-capped
  spawn-to-target in `ChurnManager.tick`.
- P0 exploit closures (#188/#189/#190): explicit-id market self-buys
  rejected like auto-buys (listing survives, nothing minted); per-bounty
  XP capped at 500 with verified kills consumed on fill (one kill fills
  exactly one bounty); malformed input gets a one-line log + error reply
  instead of a traceback dump or a dropped connection. Follow-ups (#191/
  #192): commission regex anchored on `#id` with discounted-rate capture;
  env pack masks use the server's `stats` count (20-name list no longer
  desyncs near-cap packs); sellers paid out directly when online;
  per-poster open cap (5), per-bounty kill cap (100), collusion-history
  cap; market/invite TTL sweeps with dungeon-gold teardown; charm turn-in
  requires the charm in hand; explicit bool parsing in `_apply_config`;
  all bounds tunable via `server_config.json`.
- Soak readiness (#201): `server.py --config <path>` runs on an overlay
  config without touching the prod file (`COMMISSION_TTL_SECONDS` moved
  above the config load so overlays can set it); short-TTL soak overlay
  at `ml/conductor/soak_server_config.json`; `commissioner` scripted
  role (deterministic post → kill → fill → cancel cycle) in farm
  `--scripted` choices and `--slot`; loopback `gm_tables` snapshot plus
  `soak_report.json` (tables, treasury, log errors, per-type rewards)
  with the verdict still liveness-based; nightly soak runs the overlay
  with mixed role slots.
- Launcher cleanup (#178): removed redundant `torch_bot_longterm.bat`
  (one-liner subsumed by `torch_bot.bat --steps N`) and `torch_bots.bat`
  (N processes, one checkpoint file — the last-writer-wins race
  `torch_farm` exists to eliminate); `start.bat` now links
  `torch_farm.bat`.
- Soak lifecycle fixes: churn lifetimes count completed episodes
  (supervisor hook) instead of wall-clock ticks; wave startup sleeps
  differential delays (50 agents in ~9s, was 212s); mixer drops dead ids
  on task end; spawn failures log and skip instead of killing the run;
  linear checkpoints version-stamped like torch (shared
  `ml/versioning.py`); metrics rotation caps (`rotate_mb`/`keep_files`).
- Server bind flags (#173): `--host`/`--port`/`--http-port`/`--gm-port`
  (localhost training without firewall prompts; GM stays loopback-only).
- Scores backup rotation (#166): `scores.json.1`/`.2` rotate on every
  save; load falls back with a warning when the primary is missing or
  corrupt.
- Repo automation: release versioning from labels (enhancement=minor,
  default patch), Dependabot automerge for patch/minor on green CI,
  failure-log artifacts, test_dashboard/test_env_reset/test_eval_stats
  wired into CI, canonical Apache-2.0 LICENSE (detected), branch
  protection (4 green CI checks, no force-push/deletion), dependency
  action bumps (checkout/upload-artifact/gh-release majors).
- Dashboard Phase 2 (#128): Overview health status (Healthy/Idle),
  market-orders trend chart (new additive history fields), 10-item
  activity feed; canvas world map v2 (pan/zoom/click, room drawer) --
  no quadtree/rAF at 32 nodes; rooms grid kept unvirtualized.
- Dashboard foundation (#127): Overview tab (default landing) with
  client-side health cards, trend charts, and recent activity; Agents /
  Quests / Crafting / Config placeholder tabs pointing at follow-up
  issues; per-tab rendering (hidden tabs cost no DOM churn);
  `tests/test_dashboard.py` markup/JS consistency checks.
- Agent plugins (#62/#152): every agent ships as an `AgentPlugin`
  (`ml/plugins/`; built-ins `linear`, `torch`, `gather`, `dungeon`,
  `market`, `maker`; external dirs via discovery). Supervisor passes the
  env to 4-arg policies; conductor runs weighted multi-kind slots
  (`soak --slot gather --slot torch:checkpoint=X`, repeat for weight)
  with per-type status; `runners.py` kept as a thin compat layer.
- Protocol versioning (#72): `PROTOCOL_VERSION = 1` on server and env,
  sent on login and echoed in `welcome`; mismatches warn via
  `version_match` in step info, old clients unaffected.
- Eval statistics (#69): paired t-test (exact, no scipy), Cohen's d,
  95% CI, SIGNIFICANT/INCONCLUSIVE verdicts, `--out run.json` records.

## 0.6 - conductor, agent variety, reliability, governance (2026-09-16)
- P0 fix: trade_count double-count — each market fill now counts as one trade
  (buyer only), not two. Dashboard shows accurate fill volume.
- P0 fix: quest-giver kill penalty — killing registered quest givers now
  applies a -0.5 score penalty (XP/gold stay at zero). Never worth it.
- P0 fix: commission collusion — repeated poster+filler pairs now earn
  diminishing rewards (1/(1+prior_fills), floor 10%). Strangers always
  get full value. Commission list shows your effective rate per poster.
- Commission poster reward: poster now earns 10% of the bounty as score +
  XP when their commission is filled (online poster gets a notification).
  Posting is no longer a pure gold sink.
  Any escrow remainder (posted gold minus reduced payout) is sunk to the
  treasury as an additional collusion deterrent.
- Carry cap + gated `drop`: packs hold 24 units (worn gear and up to 5 arrows
  exempt); `take` / `gather` / `buy` / `market_buy` refuse at the cap without
  charging gold, while crafting, rewards, and GM grants bypass. `drop`
  (single unit, optional amount) only works with a full pack. Pack load rides
  in `stats`; agents get a matching `drop` action gated the same way in masks
  (49 total).
- Agent market disposition (quicksell vs hold vs speculate): the env now
  prices every holding at merchant value vs best-market-ask margin
  (`flip_margin()`; unlisted items nominal +1 for price discovery); `sell`
  takes the lowest margin, `market_post` the highest positive one, with
  keep rules (worn gear, quest charm, last herb, low bow arrows); new
  `flip_margin_norm` + `inv_value_norm` observation scalars (OBS 173 → 175)
  and `flip_margin` in step info so trainers can attribute the decision.
- Agent capability pass (`ml_env.py`): the env
  now parses `room_gold`, `gatherables`, `market_slots`, and commission
  lists into state; `take` prefers gold piles, `gather`/`market_expand`/
  `commission_fill`/`commission_cancel` are gated and targeted (richest
  non-own bounty, own oldest); `buy`/`market_post` are need-aware (weapon
  first, most-profitable listing, worn gear and quest charm excluded);
  `attack` skips non-hostile NPCs; mask + inventory lookups cached per
  observation. Dead `drop`/`give` actions already removed (48 total).
- Rest is now location-gated and paid: +5 HP only in rest areas (Town Square,
  Market, Healing Spring, Lake Shrine) for 2 gold. Sister Maren's `heal`
  (full restore, her tile only) now costs 5 gold.
- `commission_cancel` is poster-only: anyone could previously cancel anyone's
  open bounty and force the poster to forfeit half their escrow.
- Quests unified on one command: `quest` with `list` / `accept` / `turn_in`
  (the `quest_accept` / `quest_turn_in` aliases are gone); `quest list`
  shows every quest with giver, room, reward, and live state.
- Removed `say`, `drop`, and `give`: `say` had no mechanical effect, `drop`
  overlapped sell/market/craft sinks with no inventory cap to manage, and
  direct transfers run through the market now.
- Action masking for both agents: the env exposes `valid_action_mask()`
  (room exits, rest/heal location + gold, mat gating) and all four trainers
  (linear, botfarm, torch solo, torch farm) mask exploration and exploitation
  to it — no step is wasted on a guaranteed-error command. Net 48 actions.
- Equipment slots: separate weapon (attack), armor (damage reduction), and
  offhand/shield (small reduction) slots with strict item typing.
  Reinforced Leather (DR 2), Iron Plate (DR 3), and Old Shield (offhand DR 1)
  were converted from attack gear; worn defense stacks with buff reduction
  everywhere damage lands. `equip` routes by type, shops/drops/posts unequip,
  and `stats`/`inventory`/dashboard snapshots expose all three slots.
- Ammo families: the longbow fires any variant best-first with a flat ladder
  (arrow +0, iron +1, steel +2), reviving 2 items and 2 recipes; agents get
  `craft_iron_arrow` / `craft_steel_arrow` plus family-count/best-bonus
  observations.
- Gather materials sunk into recipes by difficulty: reed fiber → bandage,
  salt + springwater → rations, resin → oils, mountain herb → tonic, scrap
  iron → plate, heron feather → steel arrows. New pinnacle crafts needing
  intermediates + dungeon parts: Relic Aegis, Bulwark of the Deep, Warden's
  Elixir, and the Serpentbrand blade (mid-tier weapon filling the 4→13 gap
  left by the armor conversion). Obs 173 / actions 50; retraining needed.
- Commission fills now verify work: kills are logged per NPC name with
  timestamps (`kills_by_npc` in the score entry), and filling requires the
  filler's kills of the target since the bounty was posted. Posting 0g +
  huge XP for an alt to collect with zero kills is rejected; same-name
  self-fill was already blocked.
- Death penalty scales with wealth lost: flat 5.0 score floor plus 0.1 per
  gold removed (floor pile + vanished), so dying broke stings the same as
  before while dying rich costs real score (e.g. 1000g carried -> -55).
- Dungeon endgame rework: the scaling per-floor `Dungeon Blade` is gone.
  Floor 50 (the depth cap) is held by **The Warden of the Deep**, a fixed
  boss (350 HP / 28 attack, 10-minute respawn) dropping a `Warden's Trophy`
  plus 150 gold. The trophy crafts into the **`Warden's Blade`** (fixed 13
  damage, best weapon in the game: trophy + 2 Iron Ore + Serpent Scale,
  tier 4). ML agents get a matching `craft_wardens_blade` action.
  New NPC + items also grow the vocabularies; see below for current totals
  (checkpoints need retraining).
- Fixed farm staggered start running only the last bot: the loop variable
  was late-bound, so all N tasks shared one connection. Now each task binds
  its own bot (`bot=b`); verified live with 8 bots stepping 206-295 each.
- Bot farm operations: staggered bot starts (2s per index, so bots meet
  different initial states) and exit-code restart loop in `ml_botfarm.bat`
  (restarts only on clean exit 0; Ctrl+C exits 1 and stops); the farm prints
  a summary and exits 1 on interrupt, 0 on steps exhausted.
- Connection resilience widened to `websockets.ConnectionClosed` (was
  `ConnectionClosedError` only): clean server-side closes no longer crash
  the whole farm; the episode just ends.
- Market stall slots: each seller holds at most `MARKET_ORDER_SLOTS_BASE`
  (3) open orders; `market_expand` buys +1 slot for gold doubling per slot
  (50/100/200g…), fee to the GM treasury. Cap persisted per character in
  `scores.json`, visible in `stats` as `market_slots`; ML action space grows
  44 → 45 (`market_expand`), so older checkpoints restart fresh.
- Group-play shaping (reward only, no scripted behavior): per-step social
  bonus now scales per ally (`0.05 × allies × diminish`) instead of flat,
  plus a one-time formation bonus (`0.5 × diminish`, 500-step cooldown) when
  a solo agent joins/forms a party -- leave/rejoin cycling can't farm it.
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
- P1 batch complete (#4-#11, merged): 24-unit carry cap with gated `drop`
  action (N 48 → 49, `pack`/`pack_max` in stats), crafting profitability
  assertions, quest unit tests, `server_config.json` overrides, ML reward
  config (`ml/ml_config.json`), trimmed dashboard perf table, historical
  player-count/score graphs (30s sampling, ~1hr buffer), crafting ML
  support + verification tests, and the `ml/conductor` orchestrator
  (registry, supervisor, churn, mixer, metrics).
- Specialist reward modes (#36/#37): `TextMMOEnv(..., reward_mode=...)`
  with `score` (default, unchanged), `xp` (raw XP + per-level bonus), and
  `econ` (gold + inventory-value delta); tuned via `ml_config.json`.
- Scripted baselines (#34): `GatherSell`, `DungeonClearer`,
  `MarketFlipper`, `MarketMaker` behavior-tree policies
  (`--scripted .../mixed`) as fixed RL comparisons; no learning.
- RND curiosity (#35): frozen-target/predictor bonus riding the DQN TD
  target (`--rnd-lambda 0` disables fully); predictor persists in
  checkpoints with backward compat.
- Population-based training (#38): exploit (copy winner weights) +
  explore (mutate hyperparams), gated on episodes and fitness delta.
- Conductor hardening (#45-52): supervisor step counter + watchdog
  timeouts, registry full-precision rewards, package exports, async wave
  fill, per-key config coercion warnings, automatic mixer integration,
  and real `runners.py` (`make_env_factory`, linear/torch policies) so
  the conductor actually runs agents.
- Curriculum (#59): stages 0-3 (rats → dungeons → crafting → full
  economy) with mask-level gating and score-threshold auto-advance;
  default stage 3 behaves exactly as before.
- Evaluation + provenance (#53/#54): checkpoints embed git SHA, config
  hash, dims, timestamp; `python -m torch_agents.eval` runs fixed seeds
  with mean/std and baseline deltas.
- Ops (#55-58, #60): SIGTERM/SIGINT graceful shutdown (listeners close,
  sockets close, scores save), `/health` endpoint, pre-commit
  (black/ruff/mypy/fast tests), pip-audit CI job, nightly 50-agent soak
  workflow with metrics artifacts.
- Soak reliability fixes: bounded mixer rebalance (the old loop hung
  forever on uniform input), ghost-proof disconnect cleanup
  (`logged_in` cleared first, entry always popped), bounded env
  close/connect with retries, supervisor closes envs on stop/reap (no
  more ghost sockets), death/crash metrics in the supervisor, live-test
  death-resilient dungeon section. 9-hour validation soak: PASS, 30/30
  alive, 9,819 episodes, zero errors.
- Governance + public launch: CoC (Covenant 2.1), CONTRIBUTING (dual
  audience), SECURITY.md, CODEOWNERS, YAML issue forms, release-drafter
  notes, Dependabot automerge (patch/minor), branch protection ruleset
  (4 green CI checks, no force-push/deletion), canonical Apache-2.0
  LICENSE (detected), wiki (9 guides) with README slimmed to an index.
  Repo is public.

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