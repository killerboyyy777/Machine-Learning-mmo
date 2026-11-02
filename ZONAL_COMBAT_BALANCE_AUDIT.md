# Zonal Combat Balance Audit

Scope: evaluate whether the world in `C:\Users\Jona\Downloads\files` meets its stated zonal-combat design goals.
Files reviewed:
- `C:\Users\Jona\Downloads\files\server.py` (respawn, death/loot, combat handlers, flags)
- `C:\Users\Jona\Downloads\files\world.json` (rooms, NPCs, items, recipes, gather nodes)
- `C:\Users\Jona\Downloads\files\ml\balance.py` (DEFAULT_THRESHOLDS, SCENARIOS, CHECKS)
- `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py` (scripted agent roles)
- `C:\Users\Jona\Downloads\files\ml\ml_env.py` (ML observation / action space)
- `C:\Users\Jona\Downloads\files\balance\balance_report.json` (latest gate run)
- `C:\Users\Jona\Downloads\files\balance\smoke_report_token.json` (latest smoke run)

---

## 1. Summary

**Overall health: mixed.**

The zonal distinction is mechanically present and mostly aligned with the design goals:
- Civilian rooms apply a 2x respawn slowdown (`CIV_NPC_RESPAWN_MULT = 2.0`) and drop only gold on death.
- Wild rooms use base respawn timers and scatter 100% of the unequipped pack on death.
- XP loss on death is a flat 5% of total XP for everyone.

However, several issues undermine the goals:
1. **Wild zones are not the dominant source of death.** The latest smoke run shows civilian deaths/hour (~46.8) more than double wild deaths/hour (~18.0), because wild rooms are few, far from spawn, and not where the scripted agents spend their time.
2. **Top civilian mobs rival wild mobs in lethality.** A troll, ice wraith, stone golem, or storm eagle can one-shot or near-one-shot a low-level player, so "forgiving" civilian zones still carry end-of-line lethal threats.
3. **ML agents cannot learn to flee.** `cmd_flee` exists server-side but is absent from `ml/ml_env.py` ACTIONS, so the only survival tools agents see are heal/use/rest.
4. **A balance threshold is unmeasurable.** `companion_damage_share_pct` has a 20-40% band in `ml/balance.py`, but the server exposes no companion-damage telemetry, so the check is always skipped.
5. **The `shelter` flag is mechanically inert** and several crafted combat sub-systems (ammo variants, buff oils/tonics, offhand shields) are under-used by the baseline scripted agents.

---

## 2. Per-Issue Findings

### Issue 1: Wild zones under-deliver on "brutal" death pressure
- **Severity:** medium
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\world.json`: only 10 wild rooms, 6 of which have hostile NPCs.
  - `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py:217-251`: DungeonPlugin pushes into the dungeon, not wild zones; GatherPlugin avoids combat; no role pathfinds to wild rooms.
  - `C:\Users\Jona\Downloads\files\balance\smoke_report_token.json:109-139`: `civ_deaths_per_hour = 46.769`, `wild_deaths_per_hour = 17.988`.
- **Detail:** Wild rooms are map extremities (tideline/summit/crypt branches). Agents respawn in `town_square` and never receive a strong push toward wild content, so most death traffic happens in the heavily connected civilian forest/crypt/mountain chain.
- **Recommended change:** Either (a) add 2-3 mid-map wild transition rooms between civilian hubs and the current wild end-zones, or (b) tune scripted/ML reward so that wild kills grant a higher score/XP multiplier, or (c) add a `wild_death_share_pct_min` threshold (e.g. 20%) to `ml/balance.py` DEFAULT_THRESHOLDS so future changes are gated on wild zones actually killing players.

### Issue 2: High-level civilian mobs rival wild mobs in lethality
- **Severity:** medium
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\world.json:398-409`: troll 90 HP / 12 ATK / 120s respawn in `mountain_pass` (civilian).
  - `C:\Users\Jona\Downloads\files\world.json:518-529`: ice wraith 70 HP / 10 ATK / 110s respawn in `storm_summit` (civilian).
  - `C:\Users\Jona\Downloads\files\world.json:542-553`: stone golem 75 HP / 10 ATK / 110s respawn in `stone_circle` (civilian).
  - `C:\Users\Jona\Downloads\files\world.json:554-565`: storm eagle 70 HP / 11 ATK / 120s respawn in `sky_nest` (civilian).
  - Compare wild apex: glacier wyrm 105/13, tempest roc 95/12, abyssal maw 100/12.
- **Detail:** A level-1 player has 20 HP and 3-7 attack. A single troll can deal up to 12 damage per tick and survives ~10-20 player hits. The slower civilian respawn does not make the encounter forgiving while it is alive.
- **Recommended change:** Move the troll, ice wraith, and storm eagle to wild rooms, or reduce their stats by ~25% and shift the top-end loot to wild mobs. Keep civilian mobs capped around 50 HP / 7 ATK so a low-level player can realistically survive a mistake.

### Issue 3: `cmd_flee` exists but is unreachable for ML agents
- **Severity:** high
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\server.py:1776-1852`: `cmd_flee` implementation, trigger at `FLEE_THRESHOLD = 0.25` HP or outnumbered.
  - `C:\Users\Jona\Downloads\files\ml\ml_env.py:767-798`: ACTIONS tuple contains `attack`, `use`, `heal`, `rest`, but no `flee`.
  - `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py`: no plugin calls or plans `flee`.
- **Detail:** Active, learnable combat requires that agents can discover disengagement. Right now an agent's only response to near-death is to burn a consumable or rest; it cannot learn to exit a fight. This also means the `FLEE_THRESHOLD` constant is dead code for the ML pipeline.
- **Recommended change:** Add `"flee"` to `ml/ml_env.py` ACTIONS after `rest`, expose it in `valid_action_mask()` when hostiles are present and the danger condition holds, and add a scripted fallback in `ScriptedPolicy._heal_first()` (e.g., yield `flee` when HP is below `FLEE_THRESHOLD`).

### Issue 4: `shelter` flag has no game effect
- **Severity:** low
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\world.json`: many rooms set `"shelter": true`, including the wild transition rooms `tideline_lighthouse`, `sunken_tunnel`, `summit_gatehouse`, `howling_tunnel`.
  - `C:\Users\Jona\Downloads\files\server.py:950-963`: `_room_is_risk()` checks `room.get("risk")` or `room.get("wild")`; `shelter` is never read.
  - `C:\Users\Jona\Downloads\files\server.py:2975-2999`, `7583`: `shelter` is echoed in room views but never used for combat, rest, or death rules.
- **Detail:** The flag is purely descriptive. Players and agents have no reason to care whether a room is "sheltered".
- **Recommended change:** Either give `shelter` a function (e.g., `"shelter": true` rooms never scatter items on death, even if wild) or remove the field from `world.json` to avoid misleading designers.

### Issue 5: No `"risk"` rooms in world.json
- **Severity:** low
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\server.py:950-963`: `_room_is_risk()` supports both `risk` and `wild`.
  - `C:\Users\Jona\Downloads\files\world.json`: no room sets `"risk": true`.
- **Detail:** The dual-flag design is half-unused. Wild already implies item scatter, so the extra `risk` concept is redundant in practice.
- **Recommended change:** If the intent is to have "sheltered but dangerous" rooms (indoor wild), use `wild + shelter` consistently and document it. If `risk` is not needed, delete the `room.get("risk")` branch to reduce conceptual surface area.

### Issue 6: `companion_damage_share_pct` threshold cannot be evaluated
- **Severity:** high
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\ml\balance.py:78-89`: `companion_damage_share_pct_min = 20.0`, `companion_damage_share_pct_max = 40.0`.
  - `C:\Users\Jona\Downloads\files\ml\balance.py:804-806`: derived value is `_num(telemetry.get("companion_damage_share_pct"))`.
  - `C:\Users\Jona\Downloads\files\balance\balance_report.json:171-183` and `balance/smoke_report_token.json:164-176`: the check is always `skip` because telemetry is null.
- **Detail:** A threshold that is always skipped cannot gate balance. It also hides whether companions are meaningfully participating in combat.
- **Recommended change:** Either (a) implement companion damage attribution and expose it via dashboard/gm_tables telemetry, or (b) remove the 20-40% band from DEFAULT_THRESHOLDS until the telemetry lands. Do not leave a blocking check that can never fail.

### Issue 7: `MOB_EXTRA_SPAWNS = 1` doubles hostile density everywhere
- **Severity:** medium
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\server.py:38`: `MOB_EXTRA_SPAWNS = 1`.
  - `C:\Users\Jona\Downloads\files\server.py:480-502`: `_sync_extra_spawns()` clones every hostile template once.
- **Detail:** Extra spawns double population in both civilian and wild zones, which partially negates the "slow respawn / forgiving" feel of civilian rooms: when the base mob is already present, the extra clone keeps the room dangerous twice as long. The current wild mobs are already apex-level; doubling them makes wild rooms even more punishing, which may explain why scripted agents avoid them.
- **Recommended change:** Consider applying extra spawns only to NPCs whose room is wild, or reduce `MOB_EXTRA_SPAWNS` to 0 for civilian rooms and keep 1 for wild rooms.

### Issue 8: Scripted agents ignore several combat sub-systems
- **Severity:** medium
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py:217-251`: GatherPlugin never attacks; if a hostile enters its room it just takes damage.
  - `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py:226-251`: DungeonPlugin attacks and heals but never uses crafted buffs, better ammo, or flee.
  - `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py:94-135`: `_stuck_state_sig` tracks combat text, but no plan uses buff durations or ammo quality.
- **Detail:** Combat is only "learnable" if the baseline demonstrates the mechanics. Right now oil/tonic buffs, iron/steel arrows, offhand shields, and flee are not exercised by the reference scripted policies, so RL agents have no inductive bias toward them.
- **Recommended change:** Extend the scripted plans:
  - Use `use` on `sharpening_oil` / `fortitude_tonic` before attacking tough mobs.
  - Prefer equipping the best available weapon/offhand/ammo.
  - Add `flee` when low HP and hostiles remain.
  - Make GatherPlugin fight back when attacked (yield `attack` when `npc_names` contains a hostile).

### Issue 9: Balance thresholds are too weak to enforce the zonal fantasy
- **Severity:** medium
- **Files/lines:**
  - `C:\Users\Jona\Downloads\files\ml\balance.py:78-89`: `civ_deaths_per_hour_min = 0.5`, `wild_deaths_per_hour_min = 0.5`, `deaths_per_kill_max = 1.5`.
- **Detail:** The current thresholds mostly confirm "someone died somewhere." They do not verify that wild zones are more lethal per-capita than civilian zones, or that civilian zones are actually forgiving. The 0.5 floor is trivially met by any running server.
- **Recommended change:** Add `wild_death_share_pct_min` (e.g. 15-25%) and consider a `civ_deaths_per_hour_max` or `wild_to_civ_death_ratio_min` to ensure wild zones punch above their population weight. Tighten `deaths_per_kill_max` to ~1.0 if the design intent is that fights are generally winnable with basic gear.

---

## 3. Threshold Assessment for `ml/balance.py`

| Threshold | Current value | Appropriate? | Notes |
|-----------|---------------|--------------|-------|
| `civ_deaths_per_hour_min` | 0.5 | Too loose | Smoke measured 46.8; this only checks that civilian zones are not completely safe. OK as a floor, but does not validate "forgiving". |
| `civ_death_share_pct_max` | 95.0 | Reasonable | Smoke: 72.2%. Prevents wild zones from being irrelevant, but does not ensure they are brutal. |
| `wild_deaths_per_hour_min` | 0.5 | Too loose | Smoke: 18.0. Met trivially. Should be replaced or augmented with a share/ratio check. |
| `deaths_per_kill_max` | 1.5 | Too loose | Smoke: 0.84. At 1.5 the game would feel almost unwinnable; the real target should be closer to 1.0. |
| `companion_damage_share_pct_min/max` | 20 / 40 | Not evaluable | Telemetry missing; remove or implement. |

**Verdict:** The thresholds prove the server is running and agents are dying, but they do not tightly enforce the zonal fantasy. Add telemetry for companions, add a wild-death-share check, and tighten the deaths-per-kill ceiling.

---

## 4. Useless or Underused Mechanics

### Currently useless
- **`shelter` flag** (`world.json`, `server.py`): exposed in room views but has no mechanical effect on combat, death, rest, or respawn.
- **`"risk"` room flag** (`world.json`): supported by `_room_is_risk()` but no room uses it.
- **`flee` command for ML agents** (`server.py:1776-1852`, `ml/ml_env.py:767-798`): implemented server-side, absent from the ML action space and all scripted plugins.

### Underused by baseline agents
- **Crafted buffs** (`sharpening_oil`, `greater_sharpening_oil`, `fortitude_tonic`, `ironhide_draught`, `wardens_elixir`): scripted plans only use `use` through `_heal_first()`, which targets heal items, not buffs.
- **Ammo variants** (`iron_arrow`, `steel_arrow`): recipes exist but scripted agents do not proactively craft or equip them; the only bow pickup is at `lumber_camp`.
- **Offhand shields** (`old_shield`, `deep_bulwark`): `cmd_equip` supports the slot, but DungeonPlugin only yields `equip` generically and GatherPlugin never fights.
- **Companions in combat**: wolf passives (`pack_fervor`) and companion assist exist, but companion damage share is unmeasured and the scripted plugins do not target with companions.
- **`rest_area` rest command**: functional but costs 2g for 5 HP, which is economically unattractive compared to Sister Maren's 5g full heal or consumables; usage is low in smoke data.

---

## 5. Concrete Recommended Tuning Patch (summary)

1. **World data:**
   - Reduce stats of civilian apex mobs (troll, ice wraith, storm eagle) by ~25% or move them to wild rooms.
   - Add 1-2 hostile NPCs to currently empty wild transition rooms (`tideline_lighthouse`, `sunken_tunnel`, `summit_gatehouse`, `howling_tunnel`) so wild zones start earlier.

2. **Server constants:**
   - Consider `MOB_EXTRA_SPAWNS` that scales with `wild` (e.g., 0 for civilian rooms, 1 for wild rooms).
   - Decide the fate of `shelter`/`risk` flags and either implement or remove them.

3. **ML action space:**
   - Add `"flee"` to `ml/ml_env.py` ACTIONS and `valid_action_mask()`.
   - Expose `wild_flag` and `defense_norm` already present in obs; ensure agents can see danger.

4. **Scripted baselines:**
   - Add flee and buff/oil usage to `ScriptedPolicy`.
   - Make GatherPlugin fight back when attacked.

5. **Balance thresholds:**
   - Add `wild_death_share_pct_min` or `wild_to_civ_death_ratio_min`.
   - Tighten `deaths_per_kill_max` to ~1.0.
   - Remove or implement `companion_damage_share_pct` telemetry.

---

*Report generated from a read-only review of the repository. No files were modified.*
