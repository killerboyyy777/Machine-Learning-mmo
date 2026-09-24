# Scout's Journal

## Cleared Areas
- 2026-03-31: Server crafting & command state handling (`server.py:cmd_craft`, `cmd_quest`, `cmd_equip`, `respawn_player`, `cmd_sell`, `cmd_drop`). Found equipped gear slot reference retention bug during crafting consumption and quest turn-in.

## Recurring Bug Families
- **Slot Reference Retention on Inventory Removal**: When items are consumed or removed from `player.inventory` (e.g. `cmd_craft`, `cmd_quest` turn_in), equipped slot references (`equipped`, `armor`, `offhand`) pointing to the removed item ID must be cleared when no remaining instances exist in `player.inventory`. `cmd_sell` and `cmd_market_post` were updated alongside `cmd_craft` and `cmd_quest` to maintain consistent multi-copy retention checks (`if slot == iid and iid not in player.inventory`).

## Rejected Hunches
- `ml_env.py:_first_inv_typed`: Evaluated whether memoized cache `_inv_type_cache` missed un-equipping. Confirmed `_build_obs` resets `_inv_type_cache = {}` at every step, so cache stale-read hunch was invalid.
