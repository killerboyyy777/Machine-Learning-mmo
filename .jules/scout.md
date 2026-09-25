# Scout Journal

## Cleared Areas
- 2026-09-25: Server Combat / Death & Equipment (`server.py:death_preview`, `server.py:respawn_player`). Audited equipment slot filtering against inventory duplicates in risk zones.

## Findings
- Duplicate equipped items in inventory were treated as equipped across all instances (`if iid not in (player.equipped, player.armor, player.offhand)`), shielding unequipped duplicate items from risk-zone death drops and omitting them from death previews. Fixed by consuming one instance per equipped slot using `_unequipped_inventory(player)`.

## Hunch Log & Rejected Hypotheses
- `cmd_use` / `cmd_equip`: Hypothesized that consuming a duplicate consumable might clear equipped slots, but consumables cannot be equipped. Verified clear logic checks `iid not in player.inventory`.
