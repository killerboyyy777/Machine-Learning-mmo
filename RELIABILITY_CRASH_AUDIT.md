# Reliability / Crash Audit Summary

Audited files:

- `C:\Users\Jona\Downloads\files\server.py`
- `C:\Users\Jona\Downloads\files\ml\balance.py`
- `C:\Users\Jona\Downloads\files\ml\ml_env.py`
- `C:\Users\Jona\Downloads\files\ml\plugins\scripted.py`
- `C:\Users\Jona\Downloads\files\tests\test_server_unit.py`
- `C:\Users\Jona\Downloads\files\tests\test_gm_unit.py`

Overall the server is well-defended at the top of `handle_connection` and `handle_gm_connection`, so most individual command bugs surface as a generic error to the client rather than a server crash. The biggest risks are **cross-thread reads of mutable global state in the dashboard/snapshot paths**, **fragile event parsing in the ML env reader**, and a handful of **unvalidated inputs that can loop, consume items, or raise unhandled exceptions**.

Severity summary: **1 critical, 3 high, 8 medium, 5 low**.

---

## Detailed Findings

### 1. Cross-thread snapshot builders read mutable global dicts without copies
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` — `world_snapshot()` lines 7576-7757, `_quest_snapshot()` lines 7494-7519, `_commission_snapshot()` lines 7538-7558
- **Severity:** Critical
- **Issue:** `world_snapshot()` is called from the HTTP dashboard thread, while the async game loop mutates `players`, `dungeons`, `market_orders`, `market_bids`, `SCORES`, and `_commissions`. Iterating `players.values()`, `dungeons.values()`, etc. directly from another thread can raise `RuntimeError: dictionary changed size during iteration` or read torn state. `refresh_snapshot_json()` silently swallows all exceptions, so the dashboard and `/health` endpoint can become permanently stale.
- **Recommended fix:** Snapshot-copy all mutable iterables at the start of `world_snapshot()` and the other snapshot builders: `list(players.values())`, `list(dungeons.values())`, `list(market_orders)`, `list(market_bids)`, `list(_commissions.values())`, `list(SCORES.values())`. Also log the specific exception in `refresh_snapshot_json()` instead of `pass`.

---

### 2. ML env reader task dies on a single malformed event
- **File/line:** `C:\Users\Jona\Downloads\files\ml\ml_env.py` lines 986-992
- **Severity:** High
- **Issue:** `_reader()` does `self._apply_event(json.loads(raw))` with no exception handling around either `json.loads` or `_apply_event`. A single invalid JSON payload or a stats/score/xp event missing an expected key kills the reader coroutine. The agent then receives no further state updates and will eventually time out or act on stale observations.
- **Recommended fix:** Wrap the body of the `async for raw in self.ws:` loop in `try/except Exception: continue` so one bad message does not terminate the reader.

---

### 3. ML env `step()` crashes on out-of-range action index
- **File/line:** `C:\Users\Jona\Downloads\files\ml\ml_env.py` line 1230
- **Severity:** High
- **Issue:** `action = ACTIONS[action_idx]` is unbounded. A policy bug, corrupted checkpoint, or adversarial input can pass an index `>= N_ACTIONS`, raising `IndexError` and killing the training process.
- **Recommended fix:** Validate `0 <= action_idx < N_ACTIONS` and raise a clear `ValueError` (or clamp + log) before indexing.

---

### 4. ML env `_apply_event` uses bare key access for server events
- **File/line:** `C:\Users\Jona\Downloads\files\ml\ml_env.py` lines 1014-1102
- **Severity:** High
- **Issue:** The `"stats"` branch reads `event["hp"]`, `event["max_hp"]`, `event["gold"]`, etc.; `"score"` reads `event["gained"]` and `event["total"]`; `"xp"` mixes `.get` and direct access; `"level_up"` uses direct access. Any future server change or truncated event crashes the reader task.
- **Recommended fix:** Use `.get(..., default)` for every optional field in `_apply_event`.

---

### 5. `server_config.json` top-level non-dict crashes import
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 335-341
- **Severity:** Medium
- **Issue:** `_apply_config()` calls `cfg.values()` after `json.load`. If the file is a JSON array or other non-dict, `AttributeError` propagates and the module fails to import, preventing the server from starting.
- **Recommended fix:** Add `if not isinstance(cfg, dict): print warning; return` before iterating.

---

### 6. `cmd_buy` can raise `KeyError` on malformed shop data
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` line 4279
- **Severity:** Medium
- **Issue:** `price = merchant["shop"][iid]` assumes the resolved item is present in the merchant’s shop. If `world.json` is inconsistent, the handler raises `KeyError`. `handle_connection` catches it, but the client gets an unhelpful generic error.
- **Recommended fix:** Use `merchant["shop"].get(iid)` and return a clean `"The merchant doesn't sell that."` error if missing.

---

### 7. `cmd_login` and `cmd_move` trust `player.room` / `dest` to exist in `ROOMS`
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 3608, 3736
- **Severity:** Medium
- **Issue:** `cmd_login` accesses `ROOMS[dest]['name']`; `cmd_move` accesses `ROOMS[player.room]["exits"]`. A stale or corrupted room id causes `KeyError`.
- **Recommended fix:** Use `ROOMS.get(dest, {}).get('name', dest)` and `ROOMS.get(player.room, {}).get('exits', {})` with an error path for unknown rooms.

---

### 8. `cmd_drop` can loop on an arbitrarily large `amount`
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 4058-4067
- **Severity:** Medium
- **Issue:** `amount = int(msg.get("amount", 1))` is not clamped to the number of items actually held. A message like `{"cmd":"drop","item":"x","amount":999999999}` loops hundreds of millions of times, pegging CPU.
- **Recommended fix:** Clamp `amount = max(1, min(amount, player.inventory.count(iid)))` before the loop.

---

### 9. `cmd_use` consumes an item even if it has no effect
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 4153-4193
- **Severity:** Medium
- **Issue:** `player.inventory.remove(iid)` happens before checking the item definition. If the item has neither a `"buff"` nor a positive `"heal_amount"`, the item disappears and nothing happens.
- **Recommended fix:** Validate that the item has a usable effect **before** removing it, and restore the item (or reject) if not.

---

### 10. Market sweep is O(N²) and can stall the tick loop
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 6236-6297
- **Severity:** Medium
- **Issue:** `_sweep_crossed_orders()` rescans the full bid and ask lists on every iteration. With many crossed orders for one item, each tick can become pathologically slow.
- **Recommended fix:** Maintain per-item sorted indexes (e.g., `sortedcontainers` or manual heaps/lists) so matching is O(log N) or O(M) where M is fills, not O(M·N).

---

### 11. Dungeon move can crash if the dungeon instance was deleted
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` line 3479
- **Severity:** Medium
- **Issue:** `_dungeon_move_or_fail()` assumes `d = dungeon_for_room(player.room)` is non-None. If the party/dungeon was deleted while the player is still in its room (e.g., last member disconnect + relog race), `d.floors.get(...)` raises `AttributeError`.
- **Recommended fix:** Guard `if d is None: await send(player, {"type":"error","text":"That dungeon no longer exists."}); return`.

---

### 12. Craft output quantity is unbounded from `world.json`
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` line 4814 (`_craft_output_qty`)
- **Severity:** Medium
- **Issue:** A malicious or broken recipe with a huge `output_qty` causes `player.inventory.extend([result] * output_qty)`, which can allocate enormous memory and overflow the pack cap.
- **Recommended fix:** Cap `output_qty` in `_craft_output_qty()` to a sane maximum (e.g., `INVENTORY_CAP` or a fixed recipe cap).

---

### 13. `balance.py` evaluation crashes on missing thresholds
- **File/line:** `C:\Users\Jona\Downloads\files\ml\balance.py` lines 488-495, used at line 1035
- **Severity:** Medium
- **Issue:** `_check_row()` calls `resolve_threshold()`, which raises `KeyError` if a threshold path is missing. The exception is not caught, so a typo in a threshold override file aborts the whole evaluation.
- **Recommended fix:** Catch `KeyError` in `_check_row()` and emit a `fail` row with a clear `"threshold not configured"` detail instead of crashing.

---

### 14. `cmd_gm_tables` counts connections instead of logged-in players
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` line 7150
- **Severity:** Low
- **Issue:** The `"players_online"` field uses `len(players)`, which includes sockets that have not yet logged in.
- **Recommended fix:** Use `sum(1 for p in players.values() if p.logged_in)`.

---

### 15. `MAX_TOTAL_CONNECTIONS` env override can reject everyone
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 181, 8813
- **Severity:** Low
- **Issue:** `_env_int()` does not validate the result. Setting `TEXTMMO_MAX_CONNECTIONS=0` or a negative value makes the server reject every connection.
- **Recommended fix:** Validate `MAX_TOTAL_CONNECTIONS = max(1, _env_int(...))`.

---

### 16. `refresh_snapshot_json()` silently hides all snapshot errors
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` lines 7763-7768
- **Severity:** Low
- **Issue:** All exceptions during snapshot serialization are swallowed, so RuntimeErrors from concurrent dict mutation and other bugs go unlogged.
- **Recommended fix:** Log the exception (e.g., `traceback.print_exc()` or `print`) before `pass`.

---

### 17. `cmd_gather` can crash on non-numeric `respawn_seconds`
- **File/line:** `C:\Users\Jona\Downloads\files\server.py` line 4113
- **Severity:** Low
- **Issue:** `node["respawn_at"] = time.time() + float(node.get("respawn_seconds", 30))` assumes the value is numeric.
- **Recommended fix:** Wrap in `try/except (TypeError, ValueError)` and fall back to `30`.

---

### 18. Unit tests leave global state behind
- **File/line:** `C:\Users\Jona\Downloads\files\tests\test_server_unit.py`, `C:\Users\Jona\Downloads\files\tests\test_gm_unit.py`
- **Severity:** Low
- **Issue:** Both tests mutate global `srv.players`, `srv.SCORES`, `srv.COMPANION_PLAYERS`, etc. and do not fully clean up. If the modules are imported or re-run in the same interpreter, leftover entries can cause flaky behavior.
- **Recommended fix:** Add a shared cleanup helper that resets all global tables to a known baseline in a `finally` block or per-test teardown.
