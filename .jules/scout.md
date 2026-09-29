# Scout's Journal

## Cleared Areas
- 2026-09-29: `cmd_craft` in `server.py` - verified unregistered item result handling on recipe craft.

## Recurring Bug Families
- **Direct Dict Indexing vs `_iname`**: Accessing `ITEM_DEFS[result]['name']` directly instead of using `_iname(result)` or `result_name` (which safely falls back to the raw ID if unlisted in `ITEM_DEFS`).
