# Role / Class / Playstyle Viability Audit

**Scope:** `ml/plugins/scripted.py`, `server.py`, `world.json`, `ml/balance.py`  
**Date:** 2026-10-31  
**Auditor:** subagent balance review

---

## 1. Summary

Overall the scripted role catalog is coherent: each role has a distinct economic or social niche and most are exercised by at least one balance profile. The biggest threats to viability are not individual role logic but **systemic gaps between the automation and recently-added mechanics**:

- **Companions are almost invisible to the automation stack.** No scripted role summons or adopts them, the ML env action space has no companion actions, and yet `ml/balance.py` asks for 20–40% companion damage share. That check can only skip/fail in automated runs.
- **There are no player classes/archetypes in `world.json`.** Every player uses the same base stats and level curve, so the design goal about meaningful class differences is currently unmet.
- **Several high-value mechanics are under-exercised:** `market_expand`, `drop`, `flee`, top-tier crafting recipes, and companion DroneLang scripting.
- **A few roles have local stuck-loop risks** that the existing stuck-circuit-breaker mostly mitigates, but the mitigation is reactive rather than preventive.

Top issues by severity:

| # | Issue | Severity |
|---|-------|----------|
| 1 | `combat.companion_damage_share_pct` threshold (20–40%) cannot be satisfied because no automated agent uses companions. | **Critical** |
| 2 | `world.json` defines companions but no role/class system; classes are absent entirely. | **High** |
| 3 | Smoke and baseline profiles omit `commissioner`, `quester`, and `party_leader`, so those paths are not exercised in shorter gates. | **High** |
| 4 | `party_leader` can spin on `party_accept` when no one accepts; `quester` can spin on `quest_turn_in` if the charm is sold. | **Medium** |
| 5 | High-tier crafting recipes and economic sinks (`market_expand`, `drop`) are not driven by any scripted role. | **Medium** |

---

## 2. Role-by-Role Viability Table

| Role | Used in smoke? | Used in gate? | Used in baseline? | Used in stability? | Dead-ends / stuck loops? | Contributes to a balance metric? |
|------|----------------|---------------|-------------------|--------------------|--------------------------|----------------------------------|
| `gather` | yes (`ml/balance.py:175`) | yes (`:200`) | yes (`:269`) | yes (`:232`) | Low risk: full pack can block `take`/`gather`, falls back to `sell`/`market_post` or movement. | economy, combat (deaths), craft? (no direct craft), market (sell volume) |
| `dungeon` | yes (`:176`) | yes (`:201`) | yes (`:270`) | yes (`:233`) | Low risk: delver turn-in only yielded when ready (`scripted.py:247`). | combat, progression (dungeon clears) |
| `market` | yes (`:177`) | yes (`:202`) | yes (`:271`) | yes (`:234`) | Minor: stops listing once `market_state` exists, but trades still work. | market (spread, volume, wash trades), economy (treasury/sinks) |
| `commissioner` | **no** | yes (`:203`) | yes (`:272`) | yes (`:235`) | Cancel/post parity may churn if away from guild hall; masked off by env, so safe. | market, economy, combat (kill churn) |
| `quester` | **no** | yes (`:205`) | **no** (`:266-273`) | yes (`:237`) | Medium: can repeatedly yield `quest_turn_in` if `guard_charm_crafted=True` but charm was sold/dropped; server rejects, stuck breaker should catch it. | progression (quest completions) |
| `crafter` | yes (`:178`) | yes (`:204`) | yes (`:273`) | yes (`:236`) | Low risk: craft actions are masked unless mats present; falls through to gather/take/buy. | craft (completions), market |
| `party_leader` | **no** | yes (`:206`) | **no** (`:266-273`) | yes (`:238`) | Medium: alternates `party_invite`/`party_accept` while alone; if another player is present but never accepts, `party_accept` errors repeatedly until stuck breaker bans it. | party (group clears), combat |
| `linear` (filler, not scripted) | yes (`:173`) | yes (`:198`) | yes (`:267`) | yes (`:230`) | Learning-driven; can learn any valid action including those scripted roles ignore. | all metrics indirectly |

**Interpretation:** The release gate (`gate`) and 24-hour `stability` profiles exercise every scripted role. The PR `smoke` and `baseline` profiles leave out `commissioner`, `quester`, and `party_leader`, which means bugs in bounties, quest chains, or party formation can slip through shorter gates.

---

## 3. Per-Issue Findings

### 3.1 Critical: companion damage-share threshold is untestable

- **File / line:** `ml/balance.py:88-89`
- **Severity:** critical
- **Detail:** The combat threshold requires `companion_damage_share_pct` to be between 20% and 40%. This telemetry value is produced only when companions are actively fighting (`server.py:2046-2078`, companion attack code). However:
  - No scripted role in `ml/plugins/scripted.py` uses companion actions.
  - `ml/ml_env.py:767-797` does not include `companion_adopt`, `companion_summon`, `companion_dismiss`, or any companion action in `ACTIONS`.
  - The ML client cannot learn a behavior that has no action index.
- **Recommended change:** Either (a) add companion-adopt/summon actions to `ml/ml_env.py` and give at least one scripted role (e.g., `dungeon` or `party_leader`) a companion-summon step, or (b) remove `companion_damage_share_pct` from the blocking checks until companion automation is implemented. If (a), start with `wolf` (boss-drop acquisition from `treant`) since it is the only companion that drops in the world; `mule` and `sheep` require crafting or 200g shop purchase, which automated agents rarely reach.

### 3.2 High: player classes/archetypes are absent

- **File / line:** `world.json` (no `classes` or `archetypes` key)
- **Severity:** high
- **Detail:** The design goal asks that player classes/archetypes in `world.json` have meaningful differences. The file currently defines only companions, NPCs, items, recipes, and rooms. Every `Player` object uses identical base stats (`server.py:1718-1720`: `max_hp=20`, `base_attack=3`, no class field). The only differentiation is gear and buffs.
- **Recommended change:** If classes are intended, add a `classes` section to `world.json` (e.g., `warrior`, `ranger`, `artificer`, `merchant`) with starting stat deltas or kit items, and read it in `cmd_login` / `get_score_entry`. If classes are intentionally not in scope, document that explicitly so the design goal is marked N/A rather than unmet.

### 3.3 High: smoke/baseline do not exercise commissioner, quester, or party_leader

- **File / line:** `ml/balance.py:172-180` (smoke), `:266-273` (baseline)
- **Severity:** high
- **Detail:** `smoke` runs only 10 minutes with slots `linear, linear, gather, dungeon, market, crafter`. `baseline` uses `linear, linear, gather, dungeon, market, commissioner`. Neither runs `quester` or `party_leader`; `smoke` also skips `commissioner`. Because these are exactly the mechanics (bounties, repeatable quests, parties) that the audit asks to be accessible and rewarding, their absence from the shortest gates is a coverage hole.
- **Recommended change:** Include at least one `quester` and one `party_leader` slot in `smoke` and `baseline`, even if it means reducing a `linear` slot. For `smoke` (10 min, 10 agents), consider slots: `linear, gather, dungeon, market, commissioner, crafter, quester, party_leader` plus two `linear`.

### 3.4 Medium: party_leader can loop on unaccepted invites

- **File / line:** `ml/plugins/scripted.py:397-409`, `ml/ml_env.py:2168-2176`
- **Severity:** medium
- **Detail:** `party_leader` yields `party_invite` on even steps and `party_accept` on odd steps while `party_size <= 1`. `party_invite` is masked unless another player is in the room; `party_accept` is **ungated** in the env (`ml/ml_env.py:2173-2176`) and returns an error if there is no pending invite. If a leader shares a room with another player who never accepts, the leader will repeatedly pick `party_accept`, error, and rely on the stuck breaker to ban the action. This works but is wasteful.
- **Recommended change:** Gate `party_accept` in `ml/ml_env.py` on a recent successful `party_invite` echo or on `party_size > 1` (with a small tolerance for snapshot lag). Alternatively, make `party_leader` move rooms if `party_accept` has failed several times.

### 3.5 Medium: quester can loop on a sold charm

- **File / line:** `ml/plugins/scripted.py:331-334`, `server.py:5940-5999`
- **Severity:** medium
- **Detail:** `QuesterPlugin` yields `quest_turn_in` whenever `guard_charm_crafted` is true. The server requires both the flag **and** the `ancient_guardian_charm` item in inventory (`server.py:5944-5947`). If the charm was sold or dropped, the plugin keeps yielding `quest_turn_in` until the stuck breaker fires.
- **Recommended change:** In `QuesterPlugin`, only yield `quest_turn_in` if both `guard_charm_crafted` is true **and** the charm name appears in `inv_names`. The env already exposes `inv_names`, so this check is cheap.

### 3.6 Medium: high-tier crafting recipes are not exercised by scripted roles

- **File / line:** `ml/plugins/scripted.py:361-386`, `world.json:822-851`
- **Severity:** medium
- **Detail:** `CrafterPlugin` lists `craft_wardens_blade` and `craft_serpentbrand`, but never targets `stormforged_aegis`, `abyssal_warden_draught`, `wardens_elixir`, `relic_aegis`, or `deep_bulwark`. These are top-tier sinks that require rare drops (glacier cores, abyssal hearts, warden trophies, dungeon shard 10).
- **Recommended change:** Extend `CrafterPlugin.plan()` to include the high-tier recipes when the required materials are present. Also add `craft_abyssal_warden_draught`, `craft_wardens_elixir`, `craft_relic_aegis`, and `craft_deep_bulwark` to `ml/ml_env.py` so learning agents can discover them.

### 3.7 Medium: `market_expand` and `drop` are orphaned actions

- **File / line:** `ml/ml_env.py:783` (`market_expand`), `:791` (`drop`); `ml/plugins/scripted.py`
- **Severity:** medium
- **Detail:** `market_expand` is in `ACTIONS` but no scripted role uses it. `drop` is only valid when the pack is full and is also unused by scripted roles. This leaves two inventory-pressure valves unexercised in the automated baseline.
- **Recommended change:** Add `market_expand` to `MarketPlugin` when stall slots are full and affordable. Add `drop` to `GatherPlugin`/`CrafterPlugin` fallback logic when the pack is full and no sellable/marketable items remain.

### 3.8 Low: `flee` command exists but is not in the ML action space

- **File / line:** `server.py:1776-1852`, `ml/ml_env.py:767-797`
- **Severity:** low
- **Detail:** `cmd_flee` is a player survival tool, but there is no `flee` action index, so neither scripted nor learning agents can use it. Combat roles rely on `_heal_first` instead.
- **Recommended change:** Add `flee` to `ACTIONS`, gate it on `hp < 25% max_hp` or outnumbered, and include it in `DungeonPlugin`/`PartyLeaderPlugin` combat plans. This directly exercises a distinct playstyle (survival vs. DPS).

---

## 4. Useless or Underused Mechanics / Companions

### 4.1 Companions

| Companion | Passive | Acquisition | Current automation usage | Verdict |
|-----------|---------|-------------|------------------------|---------|
| Wolf | `pack_fervor`: +10% companion attack, +10% companion XP | Boss drop from Ancient Treant (`world.json:394`) | None | **Useless in automation** |
| Mule | `carry_beast`: +2 inventory cap, +1 gather yield | Crafted via `mule_deed` recipe (`world.json:852-857`) | None | **Useless in automation** |
| Sheep | `gentle_blessing`: +20% heal/buff duration | Shop purchase (200g deed, `server.py:336`) | None | **Useless in automation** |

The companion subsystem itself is well-implemented (`server.py:2046-2778`), but because the ML env and scripted roles cannot interact with it, all three companions are effectively dead content in balance/soak runs. The only automated contact is NPC wolves/treants dropping the `wolf_pup` item, which then sits in inventory.

### 4.2 Quests

- **Guard charm (`guard_charm`)**: driven by `quester` in gate/stability; reward is attractive (50 XP / 25g / 15 pts).
- **Delver (`delver`)**: driven by `dungeon` role; reward 40 XP / 20g / 15 pts.
- **Remedy (`remedy`)**: driven by `quester`; reward 20 XP / 10g / 8 pts.
- **Tonic (`tonic`)**: driven by `quester`; reward 40 XP / 20g / 12 pts.

All four quests are viable and repeatable. The main risk is that `smoke`/`baseline` do not run `quester`, so remedy/tonic chains are not exercised in short gates.

### 4.3 Bounties / Commissions

- `commission_post`, `commission_list`, `commission_fill`, `commission_cancel` are fully implemented and economically sound (escrow, collusion curve, self-deal block).
- They are exercised only in `gate`, `stability`, and `baseline`; **not in `smoke`**.
- Standing guild bounties (`server.py:302-315`) run automatically and help keep the board populated when player-driven commissioners are absent.

### 4.4 Party mechanics

- Parties are accessible and rewarding: shared dungeon instances, ally attack bonus (`server.py:168-169`, `1747-1748`), teamwork XP multiplier on kills (`server.py:170-171`, `3866-3867`), and group-vs-solo clear logging.
- However, `party_leader` is not in `smoke` or `baseline`, so automated evidence of group clears comes only from the longer gates.

### 4.5 Classes

- Not applicable as currently implemented; see issue 3.2.

---

## 5. Recommended Priority Order

1. **Fix companion automation gap** — add companion actions to `ml/ml_env.py` and at least one scripted role, or relax the companion damage-share gate.
2. **Decide on player classes** — add a `classes` section to `world.json` and starting-kit logic, or explicitly remove the class design goal.
3. **Expand smoke/baseline coverage** — include `quester` and `party_leader` (and `commissioner` in smoke).
4. **Harden role logic** — quester charm-inventory check, party_leader accept gating.
5. **Exercise high-tier sinks** — extend crafter recipes and add `market_expand`/`drop`/`flee` to relevant roles.
