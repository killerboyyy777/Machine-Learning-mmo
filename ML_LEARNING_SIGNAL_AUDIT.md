# ML Learning Signal Audit — Text MMO

**Repo:** `C:\Users\Jona\Downloads\files`  
**Date:** 2026-10-31  
**Files reviewed:** `ml/ml_env.py`, `ml/plugins/scripted.py`, `torch_agents/dqn_agent.py`, `torch_agents/torch_farm.py`, `torch_agents/eval.py`, `ml/balance.py`, relevant sections of `server.py`.

---

## 1. Summary

The learning pipeline is **architecturally sound** and, on paper, gives agents enough signal to learn the four target play-styles. The env exposes a fixed-size observation with quest state, market tax terms, gear, HP, and room topology; invalid actions are masked; rewards flow from the server’s score/xp/gold events; scripted baselines cover gather/combat/market/quest/party roles; and `torch_farm.py` correctly wires multiple runners to one shared DQN with checkpoints and run logging.

**Top issues that threaten actual learning:**

1. **TD-target normalization changes the Bellman objective** in a way that can prevent value convergence (`torch_agents/dqn_agent.py:506-549`).
2. **Quest/RND intrinsic bonuses are oversized** relative to the base score signal and can distort the policy (`torch_agents/dqn_agent.py:210-215, 497-501`).
3. **Market and inventory observations are too coarse** for real arbitrage or crafting value estimation (`ml/ml_env.py:2190-2210, 2294-2307`).
4. **Combat reward is sparse**: no reward for damage dealt or HP lost until a kill or death (`server.py:3783-4001`, `ml/ml_env.py:1413-1460`).
5. **Wealth scalars are unbounded/heuristic**, which can destabilize the MLP as agents accumulate gold/score (`ml/ml_env.py:2294-2307`).

Overall health: **good wiring, risky hyperparameters and representation choices.** With the quick wins below, agents should be able to learn; without them, training will likely plateau on scripted-baseline level or chase intrinsic bonuses instead of score.

---

## 2. Per-Issue Findings

### Issue 1: TD-target residual normalization breaks the standard Bellman fit
- **File/line:** `C:\Users\Jona\Downloads\files\torch_agents\dqn_agent.py`, lines 506–549.
- **Severity:** **High**
- **Details:** `learn()` computes true TD targets (`shaped + γ·target_q·(1-done)`), then normalizes the residual `td_targets - q_vals.detach()` per batch, clips it, and regresses `q_vals` toward `q_vals.detach() + normalized_residual`. The network is therefore never asked to predict actual returns; it only moves by a batch-normalized, clipped advantage. In low-variance batches the “degenerate” branch keeps the raw residual, but the default branch centers the residual, which changes the sign relationships within a batch and can make absolute Q-values drift. Action ranking may still improve, but value convergence is not guaranteed.
- **Recommended change:** Fit the Q-head to the true TD target. Use `adv_scale` only to normalize the *gradient* or loss magnitude (e.g., divide the TD loss by `adv_scale`), not to replace the target. If the current behavior is retained as an experiment, default `td_norm` to `False` until an ablation shows it improves end-of-run score.

### Issue 2: Intrinsic quest and RND bonuses dominate the score signal
- **File/line:** `C:\Users\Jona\Downloads\files\torch_agents\dqn_agent.py` lines 210–215, 246–254, 497–501; `C:\Users\Jona\Downloads\files\torch_agents/torch_farm.py` lines 321–334.
- **Severity:** **High**
- **Details:** Defaults are `intrinsic_lambda=1.0`, `intrinsic_accept=1.0`, `intrinsic_progress=2.0`. An early kill after variety/diminish often pays ~1–3 score points, so a single quest-accept/progress transition can be worth as much as or more than a kill, and it is paid *in addition* to the server score. As total score grows, the base score signal shrinks (`DIFFICULTY_K / (DIFFICULTY_K + score)`), while quest intrinsics remain constant, making the policy increasingly reward-hack quest stage transitions.
- **Recommended change:** Reduce defaults by an order of magnitude (e.g., `intrinsic_accept=0.1`, `intrinsic_progress=0.2`, `intrinsic_lambda=0.1`) or schedule them down with epsilon. Keep intrinsic rewards smaller than the expected server score from the corresponding turn-in.

### Issue 3: Loot auxiliary target is a heuristic, not real item value
- **File/line:** `C:\Users\Jona\Downloads\files\torch_agents/dqn_agent.py` lines 793–802; `C:\Users\Jona\Downloads\files\torch_agents/torch_farm.py` lines 226–236.
- **Severity:** **Medium**
- **Details:** For non-charm pickups, `loot_delta = min(1.0, cur_gold) / max(1.0, len(new_items))`. This ignores merchant value, market margin, stack size, and the identity of the item. The loot auxiliary head therefore learns a count/gold proxy rather than the true economic value of inventory, which weakens representation learning for crafting and trading decisions.
- **Recommended change:** Compute `loot_delta` from `ml_env.inventory_value` per new item, using merchant value (or `market_net` for listed items), and emit negative value when crafting consumes inputs. Cap it for stability.

### Issue 4: Market observation is too coarse for arbitrage
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 2190–2210, 2294–2307.
- **Severity:** **Medium**
- **Details:** The observation only carries order count, a binary `market_any`, tax terms, and the single best flip margin across all holdings. It does not encode per-item bid/ask prices, spreads, or which items are over/under-priced. The env’s `market_post`, `market_buy`, and `market_buy_order` mappings make the actual item/price decision heuristically, so the agent can only learn *when* to trigger those macros, not *what* to trade.
- **Recommended change:** Add a small fixed-size market embedding: top-N cheapest asks and highest bids per item category, or at least the best ask/bid for items currently in inventory. This keeps the vector fixed while giving the agent a price signal.

### Issue 5: Combat reward is sparse and only terminal
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 3783–4001; `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 1413–1460.
- **Severity:** **Medium**
- **Details:** Attacks generate score/XP/gold only on a kill; taking damage produces no negative reward until death. With default `TRAINING_DAMAGE_FRACTION=1.0` and the documented ~14:1 death:kill ratio at level 1, an agent receives almost no informative gradient during most fights. RND and quest intrinsics help exploration, but they do not reward surviving or dealing damage.
- **Recommended change:** Add a small shaped combat reward mode (or custom reward formula) that penalizes HP loss and rewards damage dealt/dealt-to-kill ratio. Alternatively, run early curriculum with `TRAINING_DAMAGE_FRACTION < 1.0` so agents survive long enough to discover combat patterns.

### Issue 6: Wealth scalars are unbounded and use hand-picked scales
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 2294–2307.
- **Severity:** **Medium**
- **Details:** `score_norm = score / 100`, `gold_norm = gold / 200`, `own_net_norm = own_net / 200`, `inv_value_norm = inv_value / 200`. These are not clipped or log-scaled. As agents accumulate wealth, inputs can grow well beyond the range the ReLU MLP was tuned for (e.g., `score_norm > 10`), which can push later layers into saturation and destabilize learning.
- **Recommended change:** Clip these features to `[0, 1]` after scaling, or replace the fixed denominators with running mean/std normalization computed in the trainer, or use `log1p` scaling for wealth features.

### Issue 7: Party/social signal is weak and hard to condition on
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 2168–2187, 2297–2298, 1444–1459.
- **Severity:** **Medium**
- **Details:** `party_invite` is only valid when another player is in the room, but `party_accept/leave/info` are ungated and there is no observation for pending invites or ally HP. The social reward is only `0.05` per ally, scaled by `diminish_factor`, and requires `other_players > 0`. Grouping is therefore hard to discover and not clearly better than solo play.
- **Recommended change:** Expose a pending-invite flag, the number of allies in the same room, and maybe average ally HP. Increase social/formation rewards during curriculum stage 1, or add a dedicated “party” reward mode so agents can learn that grouping enables dungeon clears.

### Issue 8: Action space delegates target selection to hardcoded heuristics
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 1788–2187.
- **Severity:** **Medium**
- **Details:** `buy`, `sell`, `market_post`, `market_buy_order`, `equip`, `use`, and `drop` all pick the target item via fixed rules. This is a valid macro-action design, but it caps what an agent can learn: it cannot choose which item to buy, which price to list, or which weapon to equip for a specific foe.
- **Recommended change:** Document these as macro-actions. For richer learning, add optional parameterized or item-indexed actions (e.g., `sell_<i>` for inventory slot `i`, `post_at_price_<tier>`), while keeping the existing macros for baseline compatibility.

### Issue 9: Valid-action mask can be stale for market actions
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 1701–1713, 2190–2210.
- **Severity:** **Low**
- **Details:** `market_buy` is masked only when `market_state["orders"]` is non-`None`. Before the first `market_list` snapshot, the mask allows a blind `market_buy`, which the server rejects with “No affordable orders.” The env does not penalize these errors, so the only cost is a wasted step.
- **Recommended change:** Issue `market_list` automatically after login and after every market action so the snapshot stays fresh, or add an “order book age” feature and mask market buy/sell when the book is stale.

### Issue 10: Shared replay buffer may over-sample correlated experiences
- **File/line:** `C:\Users\Jona\Downloads\files\torch_agents/torch_farm.py` lines 321–338.
- **Severity:** **Low**
- **Details:** All runners share one replay buffer and one policy. Because they use the same weights and similar starting states (mitigated by spawn spread), the buffer can contain many correlated transitions, reducing effective sample diversity.
- **Recommended change:** Consider per-runner replay buffers sampled proportionally, or add prioritized replay using TD error / RND error to surface informative transitions.

### Issue 11: Curriculum thresholds are low and reward-mode-agnostic
- **File/line:** `C:\Users\Jona\Downloads\files\ml/ml_env.py` lines 366–367, 1726–1736.
- **Severity:** **Low**
- **Details:** Auto-advance thresholds are `(0, 10, 30, 60)` score. Stage 3 (full economy) is reached quickly, and the thresholds do not consider the reward mode; an “econ” agent might unlock combat actions late while a “xp” agent unlocks market actions before it can use them.
- **Recommended change:** Tune thresholds per reward mode, or base promotion on domain-specific milestones (e.g., first kill for “xp”, first craft for “econ”).

### Issue 12: Eval harness is comparative-only on the live shared server
- **File/line:** `C:\Users\Jona\Downloads\files\torch_agents/eval.py` lines 174–241, 296–301.
- **Severity:** **Low**
- **Details:** `compare()` correctly refuses a significance claim when `isolated=False`, which is the default live-server case. This is the right safety call, but it means the harness cannot statistically prove policy improvement without a per-seed server reset.
- **Recommended change:** Add a `--fresh-server` option that restarts `server.py` per seed, or document that live eval is descriptive-only.

### Issue 13: Balance harness does not measure policy improvement
- **File/line:** `C:\Users\Jona\Downloads\files\ml/balance.py` lines 69–108.
- **Severity:** **Low / informational**
- **Details:** `balance.py` checks economy, combat, craft, market, and liveness thresholds, but it never compares a trained agent against the scripted baselines. A world can pass all balance checks while the RL policy is no better than random.
- **Recommended change:** Add a trained-vs-scripted score comparison to the gate profile (e.g., run `torch_agents/eval.py` and `ml/plugins/scripted.py` on the same seeds and require the trained mean to exceed the scripted mean).

---

## 3. Domain-by-Domain Signal Assessment

### Combat
**Verdict: learnable, but sparse.**  
Kills produce clear score/XP/gold events, death produces a strong penalty, and the observation includes HP, gear, arrows, buffs, and the adaptive descent hint. The main gap is the lack of intermediate reward for damage or survival. RND and the variety/diminish curve help by rewarding exploration and discouraging rat grinding, but early agents will die repeatedly before seeing a positive combat reward. **Signal is sufficient with damage/HP shaping or reduced training damage; marginal as-is.**

### Crafting
**Verdict: strong signal.**  
Every craft completion awards 8 score points (`server.py:4417`), and the major crafts are tied to quest turn-ins that pay additional score/gold/XP. The observation carries mat flags, quest active/ready flags, and inventory presence; the action mask prevents guaranteed-error crafts. The loot auxiliary head would be more useful if it used real item value, but the core reward chain is clear. **Agents can learn crafting, especially the charm/remedy/tonic loops.**

### Trading
**Verdict: basic signal, limited depth.**  
Market fills pay score, XP, and gold, and the tax terms are faithfully mirrored in the observation. The env’s `flip_margin` and `inv_value` features give the agent a sell-vs-hold signal. However, the observation lacks a price book, and the actions hard-code which item to trade and at what price. Agents can learn a simple “sell when margin positive, buy when cheap” merchant loop, but not cross-item arbitrage or dynamic pricing. **Good enough for a baseline economy loop; not enough for sophisticated trading.**

### Questing
**Verdict: very strong signal.**  
Four repeatable quests have fixed givers, objectives, and rewards. The observation exposes active/ready flags, mat flags, and giver presence; turn-ins deliver immediate score/XP/gold. The agent also gets intrinsic bonuses on accept/progress transitions to bootstrap the longer chains. **This is the most learnable system in the game.**

---

## 4. Quick Wins (no re-architecture required)

1. **Tune intrinsic/RND scale** — drop `intrinsic_accept`/`intrinsic_progress` to ~0.1–0.2 and schedule `rnd_lambda` down so the score signal dominates (`torch_agents/dqn_agent.py:210-215`).
2. **Fix or disable td_norm by default** — either fit to true TD targets or set `td_norm=False` until an ablation proves the normalized-residual variant works (`torch_agents/dqn_agent.py:506-549`).
3. **Clip/log wealth features** — cap `score_norm`, `gold_norm`, `own_net_norm`, and `inv_value_norm` at 1.0 or use `log1p` (`ml/ml_env.py:2294-2307`).
4. **Improve loot target** — use `ml_env.inventory_value` for new items and negative value for consumed mats (`torch_agents/dqn_agent.py:793-802`).
5. **Add HP/combat shaping** — either via a custom reward formula or a new reward mode that penalizes HP loss and rewards damage/kills (`ml/ml_env.py` reward modes).
6. **Refresh market snapshot** — issue `market_list` automatically after login and market actions so the mask and `market_state` stay fresh (`ml/ml_env.py:reset` and `_action_to_cmd` market handlers).
7. **Add trained-vs-scripted eval to balance gate** — run the scripted baseline and trained checkpoint on the same seeds and require the trained agent to beat it (`ml/balance.py` profiles).
8. **Lower training damage during early curriculum** — set `TRAINING_DAMAGE_FRACTION` below 1.0 for initial combat learning so agents survive long enough to discover kills (`server.py:159` via config).

---

**Overall:** The world rules provide enough signal for combat, crafting, trading, and questing, but the trainer’s default hyperparameters and a few representation choices currently stand between “can learn in principle” and “reliably improves over the scripted baselines.” The quick wins above are low-risk and should be applied before any larger redesign.
