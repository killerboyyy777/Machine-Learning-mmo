# Scout's Journal

## Cleared Areas
- 2026-03-31: Server crafting & command state handling (`server.py:cmd_craft`, `cmd_equip`, `respawn_player`, `cmd_sell`, `cmd_drop`). Found equipped gear slot reference retention bug during crafting consumption.

## Recurring Bug Families
- **Slot Reference Retention on Inventory Removal**: When items are consumed or removed from `player.inventory` (e.g. `cmd_craft`), equipped slot references (`equipped`, `armor`, `offhand`) pointing to the removed item ID are not cleared if the item is no longer in `player.inventory`. `cmd_sell` and `cmd_drop` properly clear slot references, but `cmd_craft` and `respawn_player` do not consistently check if a consumed item empties out from inventory.

## Rejected Hunches
- `ml_env.py:_first_inv_typed`: Evaluated whether memoized cache `_inv_type_cache` missed un-equipping. Confirmed `_build_obs` resets `_inv_type_cache = {}` at every step, so cache stale-read hunch was invalid.
