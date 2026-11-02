# Economic Playstyle Balance Audit

Scope: crafting, market trading, gathering, merchant selling, and commissioner playstyles in the Python text MMO at `C:\\Users\\Jona\\Downloads\\files`.
Sources reviewed: `server.py`, `world.json`, `ml/balance.py`, `ml/plugins/scripted.py`, `balance/balance_report.json`, `balance/smoke_report_token.json`.
Date: 2026-10-31
Base commit: `13983a2`

---

## 1. Summary

The economic game is **functionally broken in the release-gate configuration** and **marginal in the smoke configuration**.

- The 4-hour gate run (`balance/balance_report.json`) reports **0 crafts**, **0 market trades**, and a treasury band of **98.4%** (limit 10%). Every economic threshold in the gate fails.
- The 10-minute smoke run (`balance/smoke_report_token.json`) shows the system can produce trades (8) and at least one craft, but the treasury is still far from its reserve target and effective tax is 0%.
- Root cause: the scripted economic plugins (`MarketPlugin`, `CrafterPlugin`, `CommissionerPlugin`) cannot reliably reach the rooms or source the inputs their actions require, so the high-level economic loop never spins up.
- Gathering and merchant selling work, but pay so little that they barely cover rest/heal costs.

Top issues to fix before release:
1. `CrafterPlugin` cannot source crafting inputs.
2. `MarketPlugin` never intentionally visits the Market room, so standing bids and market-list refresh are almost never valid.
3. `CommissionerPlugin` never intentionally visits the Guild Hall, so bounties are almost never posted/filled.
4. `cmd_buy` / merchant stock sell no crafting materials, blocking a buy-to-craft path.
5. The treasury reserve sink is starved because trade volume is near zero.

---

## 2. Per-Issue Findings

### 2.1 Crafting produces almost nothing in gate runs (critical)

- **Evidence**: `balance/balance_report.json`, every run reports `craft.completions=0` (limit 1).
- **File/line**: `ml/plugins/scripted.py:353-386` (`CrafterPlugin.plan`).
- **Problem**: The crafter tries craft actions first, then `gather`/`take`, then `buy`/`buy_arrows`, then sell. It never fights, never navigates to rooms with guaranteed ground ingredients, and the `buy` action only purchases weapons, consumables, or arrows. There is no reliable path to obtain `rat_tail`, `wolf_pelt`, `iron_ore`, `treant_bark`, `troll_hide`, or `ectoplasm`.
- **Concrete change**: Give `CrafterPlugin` a real input-sourcing state machine:
  - Navigate to `old_shop`/`tannery` for pelts/tails if ground items are available.
  - Navigate to `smithy` for `iron_ore`.
  - Use `market_buy` to purchase missing mats when affordable, or add crafting mats to the merchant shop.
  - Prefer low-tier recipes (`arrows`, `field_ration`) that use gatherable inputs until mats are found.

### 2.2 Market trader cannot reach the market (critical)

- **Evidence**: Gate run `market.trade_volume=0`; smoke run had 8 trades only because `linear` agents can randomly stumble into activity.
- **File/line**: `ml/plugins/scripted.py:254-276` (`MarketPlugin.plan`); `server.py:6579-6584` (`cmd_market_buy_order` room gate).
- **Problem**: `cmd_market_buy_order` is only usable in room `market`. `MarketPlugin` never targets `market`; it only `random_move`s. The same applies to `market_list` refresh, which is the only way the plugin populates `market_state`. Until `market_list` fires, `market_post` pricing is blind and `market_buy`/`market_buy_order` are masked by location.
- **Concrete change**: Add explicit navigation to `market` in `MarketPlugin` (e.g., prefer `move_*` toward `market` when not in it and no immediate trade is possible). Raise `market_list` priority so the book is refreshed early.

### 2.3 Commissioner never reaches the Guild Hall (critical)

- **Evidence**: Gate run reports no commission fills; baseline had 5 open standing bounties but zero player-driven commission activity.
- **File/line**: `ml/plugins/scripted.py:280-314` (`CommissionerPlugin.plan`); `server.py:4853-4862` and `5018-5029` (`commission_post`/`commission_fill` room gate to `GUILD_ROOM`).
- **Problem**: Posting and filling commissions require being in `guild_hall`. `CommissionerPlugin` attacks, takes, then tries commission actions; it never walks to `guild_hall`. The plugin therefore spends most of its time failing room-gated commands.
- **Concrete change**: Add a `guild_hall` navigation goal to `CommissionerPlugin`, and only attempt `commission_post`/`commission_fill`/`commission_cancel` while in that room.

### 2.4 Merchant shop does not sell crafting inputs (high)

- **Evidence**: `world.json:336` merchant shop stock: `healing_herb`, `rusty_sword`, `old_shield`, `arrow`, `sheep_deed`.
- **File/line**: `server.py:4269-4291` (`cmd_buy`); `world.json:325-337`.
- **Problem**: A buy-to-craft economic path is impossible because no NPC sells `iron_ore`, `wolf_pelt`, `rat_tail`, `mountain_berry`, `mountain_herb`, `reed_fiber`, etc. Crafters and questers must depend entirely on drops/gathering.
- **Concrete change**: Add a crafting-material vendor in `artisan_row` or `market` selling low-tier inputs at prices above gather value but below crafted output value (e.g., `iron_ore` 6g, `wolf_pelt` 5g, `rat_tail` 2g, `mountain_berry` 3g).

### 2.5 Treasury band failure (98.4% vs 10% limit) (high)

- **Evidence**: `balance/balance_report.json` `treasury.band_pct=98.4` (limit 10%) on all three seeds.
- **File/line**: `server.py:3289-3308` (`_treasury_credit`); `server.py:2348` (`TREASURY_RESERVE=500`).
- **Problem**: The reserve sink only removes gold *above* the reserve. With zero trades, the only inflows are 1g minimum bid-broker fees and commission forfeits, which are negligible. The treasury therefore cannot move toward the 500g reserve target from its 700g seeded start, or it drifts further if death/life gold dynamics dominate.
- **Concrete change**: Fix the economic activity failures above first; the sink is a symptom, not the disease. If trade remains low, consider a smaller reserve (e.g., 300g) or a standing daily sink until volume justifies the current reserve.

### 2.6 `market_buy_order` broker fee is a regressive barrier (medium)

- **Evidence**: `server.py:1868-1874` (`_broker_fee`); `server.py:6595-6619`.
- **Problem**: `_broker_fee` is `max(1, int(value * 0.05 + 0.5))`. At the 2-4g price scale seen in smoke tests, every bid costs 1g fee upfront. That is 25-50% of the bid value, making small-stakes price discovery uneconomical.
- **Concrete change**: Lower `BROKER_FEE_PCT` to 2% or cap the minimum fee at 0 for bids under 5g, so low-value material markets can form.

### 2.7 Market spread metric is unmeasurable when volume is zero (medium)

- **Evidence**: `balance/balance_report.json` `market.median_spread_pct=null` (skipped).
- **File/line**: `ml/balance.py:706-719`.
- **Problem**: The spread calculation requires both an ask and a bid for the same item. With no traders, the metric is absent, not good. This hides whether the market would have tight spreads if it were used.
- **Concrete change**: Report spread as 0 when the book is empty, or add a separate "book depth" metric so the gate distinguishes "no market" from "wide market".

### 2.8 `cmd_give` is missing (medium)

- **Evidence**: `HANDLERS` dict at `server.py:7314-7352` contains no `give` entry; `grep "cmd_give"` returns nothing.
- **Problem**: Player-to-player item transfer is referenced implicitly by trading but is not implemented. This limits cooperative economic play (e.g., a gatherer handing mats to a crafter).
- **Concrete change**: Implement `cmd_give(player, msg)` requiring the target to be in the same room, validating the item exists in inventory, and delivering it to the target's pack or item bank.

### 2.9 `cmd_buy` cannot purchase iron/steel arrows (low)

- **Evidence**: `world.json:336` merchant shop sells only `arrow`; `ml_env.py:1819-1831` `buy_arrows` maps to buying "Arrow".
- **Problem**: Bow users cannot restock advanced ammo from the merchant, even though `iron_arrow` and `steel_arrow` exist as craftable upgrades.
- **Concrete change**: Add `iron_arrow` and `steel_arrow` to the merchant shop at prices between their material cost and crafted value.

### 2.10 High-tier crafted gear is effectively unreachable (low)

- **Evidence**: `world.json:816-851` recipes for `relic_aegis`, `deep_bulwark`, `stormforged_aegis`, `abyssal_warden_draught` require dungeon/warden drops (`dungeon_shard_10`, `warden_trophy`, `glacier_core`, `abyssal_heart`).
- **Problem**: These recipes exist but require endgame dungeon clears. There is no mid-tier market for the intermediate inputs, so the crafting progression ladder is empty between T1/T2 consumables and T5/T6 dungeon gear.
- **Concrete change**: Add T3/T4 crafted equipment that uses gatherable + overworld-drop mats (e.g., `serpentbrand` is good; promote more like it) so crafters have a reachable income path before dungeons.

---

## 3. Root-Cause Analysis of Stagnation

### 3.1 Zero crafts in gate runs

The gate profile uses `crafter` and `quester` scripted agents. Neither plugin solves the Traveling Salesman Problem of moving to the right room for inputs:

- `CrafterPlugin` (scripted.py:353) tries craft actions before it has verified inputs. The env's `_action_to_cmd` (ml_env.py:1867-1970) masks each craft action unless the exact recipe inputs are in inventory. Because the plugin does not navigate to ingredient sources, the mask is almost always zero for every craft action. The plugin then falls through to `gather`/`take`/`buy`/`random_move`, but those actions do not reliably produce the needed mats.
- `QuesterPlugin` (scripted.py:317) has the same navigation gap for the `guard_charm` and `tonic` quests.
- The generic `craft` action (ml_env.py:1867) is hard-coded to `reinforced_leather`, whose inputs (`wolf_pelt`, `rat_tail`) only come from combat drops. A non-combat crafter can never make it.

Result: the craft completion counter never increments.

### 3.2 Zero trades in gate runs

Market activity requires at least one agent to post an ask and another to buy it (or post a bid that crosses). The gate run uses `market` and `linear` agents:

- `MarketPlugin` (scripted.py:254) never walks to `market`, so `market_buy_order` is permanently masked by `cmd_market_buy_order`'s room gate (server.py:6579). It also deprioritizes `market_list`, so its `market_state` is stale or absent.
- `cmd_market_post` (server.py:6299) is room-agnostic and can list from anywhere, but the env masks `market_post` unless the bot has a sellable item with positive margin (ml_env.py:2073-2096). Margin requires a fresh `market_state`, which `MarketPlugin` rarely fetches.
- With no listings, `market_buy` has no orders to lift; with no bids, `market_buy_order` has nothing to cross.

Result: `market.trade_volume=0` and the treasury receives no trade tax.

### 3.3 Treasury drift

The treasury starts at 700g (seeded by `TEXTMMO_GM_SEED`). The reserve is 500g. The sink (`_treasury_credit`, server.py:3289) removes only gold above the reserve. In the gate run:

- Trade tax inflow is zero (no trades).
- Bid broker fees are minimal because no bids are posted.
- Commission forfeits are minimal because no commissions are filled or cancelled.
- Death penalties and rest/heal costs move gold between players and the floor, but do not sink it to the treasury.

Therefore the treasury cannot fall toward the 500g reserve, producing the 98.4% band violation. The sink design is correct in principle but the economy it is meant to drain is not running.

### 3.4 Why smoke passed where gate failed

The smoke profile has only 10 agents and 6 slots, with a higher ratio of `linear` agents. Linear agents explore randomly and can accidentally:
- Walk into `market` and post/fill orders.
- Walk into `smithy` and pick up `iron_ore`, then craft arrows.

The gate profile adds `crafter`, `quester`, `party_leader`, and more agents, but these scripted roles are *less* economically effective than random linear exploration because their priority lists assume navigation that does not exist.

---

## 4. Useless or Underused Mechanics

| Mechanic | Why it is useless/underused | File/line |
|---|---|---|
| `give` command | Not implemented; no handler exists. | `server.py:7314-7352` |
| `market_buy_modify` | Requires already having a bid in a market that rarely has bids. | `server.py:6675-6754` |
| `market_expand` | Stalls almost never fill because postings are rare. | `server.py:6430-6455` |
| Tick-based craft jobs (`craft start`/`status`/`cancel`/`collect`) | Used only if a crafter can start a job; with input starvation, jobs never begin. | `server.py:4575-4773` |
| `buy_arrows` / advanced ammo | Merchant sells only basic `arrow`; iron/steel arrows are craft-only and hard to source. | `world.json:336`; `ml_env.py:1819-1831` |
| High-tier recipes (`relic_aegis`, `stormforged_aegis`, etc.) | Require dungeon/warden drops with no intermediate crafted step for income. | `world.json:822-851` |
| Companion mule gather bonus | Works, but gather loop income is too low to justify adopting a mule. | `server.py:1670-1675`; `world.json:15-25` |
| Standing guild bounties | Mechanism exists, but player-driven commission volume is near zero because commissioners do not reach the Guild Hall. | `server.py:8534+` |

---

## 5. Recommended Priority Order

1. **Fix navigation in scripted plugins** (critical): `MarketPlugin` -> `market`, `CommissionerPlugin` -> `guild_hall`, `CrafterPlugin` -> ingredient rooms.
2. **Enable buy-to-craft** (high): Add crafting-material NPC vendor or allow `cmd_buy` to purchase from a broader material catalog.
3. **Verify gate metrics after fix**: Re-run `python ml/balance.py run --profile gate` and confirm `craft.completions >= 1`, `market.trade_volume >= 1`, and `treasury.band_pct <= 10`.
4. **Tune broker fee** (medium): Lower the regressive 1g minimum on low-value bids.
5. **Implement `give`** (medium): Enables cooperative economic play.
6. **Fill the crafting progression ladder** (low): Add more T3/T4 recipes using overworld mats.
