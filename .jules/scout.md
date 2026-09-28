# Scout's Journal

## Cleared Subsystems
- 2026-03-29: Room and world snapshot views (room_view, dungeon_room_view, world_snapshot). Handled missing ITEM_DEFS keys for ground items (e.g., dynamic dungeon shards).

## Bug Families & Insights
- Dynamic item IDs (e.g., dungeon_shard_N) can exist in room_items prior to item def registration when floors are initialized on demand. Always use .get(item_id, {}).get("name", item_id) for item name lookups in view builders rather than direct indexing ITEM_DEFS[item_id]["name"].
