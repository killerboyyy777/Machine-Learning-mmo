# Small Text MMO Engine

[![Tests](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml/badge.svg)](https://github.com/killerboyyy777/Machine-Learning-mmo/actions/workflows/tests.yml)

[![BTC Donate](https://img.shields.io/badge/BTC-Donate-f7931a?logo=bitcoin&style=flat-square)](https://www.blockchain.com/explorer/addresses/btc/bc1qmkv939k2wqsej657cxj25ppwqdh65y2umnv3gg)
[![LTC Donate](https://img.shields.io/badge/LTC-Donate-a6a9aa?logo=litecoin&style=flat-square)](https://live.blockcypher.com/ltc/address/ltc1qjr49nr028mcajlt7prmmnqnjh0552qjj90zdq4)
[![Steam Donate](https://img.shields.io/badge/Steam-Donate-000000?logo=steam&style=flat-square)](https://steamcommunity.com/tradeoffer/new/?partner=1211192445&token=T9Hiu3Oz)

A minimal, hackable text-based MMORPG engine. Rooms, NPCs, items, combat,
loot, respawns, parties, a player market, and instanced dungeons — all driven
over WebSocket with plain JSON messages. Because the protocol is just JSON, a
human, a bot, and an LLM agent all look identical to the server.

> **Authorship note:** this project is human-designed and human-led but AI
> coding assistants where used along the way for a lot of implementation.

## Setup

```bash
pip install -r requirements.txt
python3 server.py
```

Starts the world on `ws://0.0.0.0:8765`. Edit `world.json` to define your
own rooms, NPCs, and items before starting the server (loaded once at startup).
Dungeons are **instanced**: a private, infinitely-deep staircase is created per
party the first time someone uses the `enter` doorway on the graveyard — see
"Parties & the dungeon" below.

On Windows you can also just run `start.bat` — it starts the engine, shows
where to find the game and the dashboard, and turns on live console logging
(every client command, HTTP requests, connect/disconnect events).

### Play it yourself

Any client is a WebSocket connection speaking JSON (see "Writing your own
client" below). Type commands like `look`, `north`, `attack rat`,
`take sword`, `equip sword`, `say hello`, `who` — either interactively in
your own client or by scripting them directly.

### Live dashboard

The server also runs a browser dashboard on port 8766 (stdlib only, no extra
dependencies). Start the engine, then open `http://localhost:8766/`:

```bash
python3 server.py
# open http://localhost:8766/ in a browser
```

It refreshes every second and shows:

- **World** — a connected surface-world map (rooms and labeled exits), plus
  every room's players (with HP bars), NPCs (alive/dead/hp), and ground loot.
- **Market** — GM treasury, lifetime tax collected, tax rate, completed
  trades, the open sell orders list, and a buy & sell history table.
- **Dungeons** — world event buffs (XP/gold), live GM bosses, and every
  instanced party dungeon with its floor progress (guards alive, cleared).
  Dungeon floors are intentionally not merged into the surface map because
  each party owns a private, dynamically generated instance.
- **GM** — a console on the dedicated loopback GM stream with an action
  picker (reward gold/item, timed 2× world buffs, elite boss spawns,
  server-wide announcements, heals, teleports, NPC slays, kicks), player /
  item / room / NPC autocomplete, quick presets, and live per-action tax
  cost previews.
- **Performance / Top 10 / Live activity** — one row per online client
  (score, score sparkline, kills, deaths, gold, HP, variety, level, room,
  last command), the all-time leaderboard, and a live feed of every command
  every client sends.
- **Track character** — pick one online character from a dropdown to follow
  their recent actions (up to `TRACK_LOG_SIZE` = 30 per character, newest
  first), with a live header (level, room, score, HP, gold). Unlike the
  world-wide feed, a tracked character's history survives busy servers.

The JSON behind it is at `http://localhost:8766/api/state` if you want to
poll it from your own tools (per-player `recent_actions` included).
Telemetry lives in the "Telemetry + live dashboard" block of `server.py`
(`HTTP_PORT`, `ACTIVITY_LOG_SIZE`, `TRACK_LOG_SIZE`, `SCORE_HISTORY_SIZE`).
Set `TEXTMMO_VERBOSE=1` (as `start.bat` does) to also print every client
command, HTTP request, and connect/disconnect event to the server's console.

### Writing your own client

Any client is just a WebSocket connection speaking JSON. There are three steps:

**1. Connect and log in.**

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://localhost:8765") as ws:
        await ws.send(json.dumps({"cmd": "login", "name": "MyBot"}))
        event = json.loads(await ws.recv())  # first message is always a room snapshot
        print(event)
```

**2. Send commands, receive responses.**

Every command is a JSON object with a `"cmd"` field. The server always
replies with a JSON object that has a `"type"` field. The most important
type is `"room"` — it contains the full state of your current room (exits,
NPCs, items, other players) and is pushed after every move, look, login,
and whenever the room changes.

```python
# look around
await ws.send(json.dumps({"cmd": "look"}))
event = json.loads(await ws.recv())

# move north
await ws.send(json.dumps({"cmd": "move", "dir": "north"}))
event = json.loads(await ws.recv())

# attack an NPC by name
await ws.send(json.dumps({"cmd": "attack", "target": "rat"}))
event = json.loads(await ws.recv())

# pick up an item by name
await ws.send(json.dumps({"cmd": "take", "item": "sword"}))
event = json.loads(await ws.recv())
```

**3. Handle the main loop.**

A working client reads responses in a loop, updates its state from `"room"`
and `"stats"` events, and decides what to do next. On `"error"`, send a
`"look"` to resync — never assume your cached state is still valid after an
error.

```python
async def main():
    async with websockets.connect("ws://localhost:8765") as ws:
        await ws.send(json.dumps({"cmd": "login", "name": "MyBot"}))

        while True:
            event = json.loads(await ws.recv())
            t = event.get("type")

            if t == "room":
                print(f"In: {event['name']}")
                print(f"  Exits: {event['exits']}")
                print(f"  NPCs: {event['npcs']}")
                print(f"  Items: {event['items']}")
                # your logic here: attack, take, move, etc.

            elif t == "stats":
                print(f"HP {event['hp']}/{event['max_hp']}  Gold {event['gold']}")

            elif t == "combat":
                print(event["text"])

            elif t == "error":
                await ws.send(json.dumps({"cmd": "look"}))  # resync

            elif t == "death":
                await ws.send(json.dumps({"cmd": "look"}))
```

The full list of commands and response types is in the Protocol section below.

### Setting up an LLM agent

An LLM agent speaks the same WebSocket protocol as any other client. The
recommended approach is to give the LLM a system prompt describing the game
and let it issue JSON commands directly.

**System prompt template:**

```
You are a character in a text-based MMO. You communicate by sending JSON
commands over a WebSocket connection. The server responds with JSON messages.

Available commands:
{"cmd": "login", "name": "YourName"}
{"cmd": "look"}
{"cmd": "move", "dir": "north"}
{"cmd": "attack", "target": "goblin"}
{"cmd": "take", "item": "sword"}
{"cmd": "drop", "item": "sword"}
{"cmd": "equip", "item": "sword"}
{"cmd": "use", "item": "healing herb"}
{"cmd": "rest"}
{"cmd": "heal"}
{"cmd": "buy", "item": "healing herb"}
{"cmd": "buy", "item": "arrow"}
{"cmd": "sell", "item": "wolf pelt"}
{"cmd": "inventory"}
{"cmd": "stats"}
{"cmd": "who"}
{"cmd": "quest", "action": "list"}
{"cmd": "craft", "recipe": "ancient_guardian_charm"}
{"cmd": "craft", "recipe": "sharpening_oil"}
{"cmd": "commission_post", "target": "rat", "required_kills": 5, "reward_gold": 25, "reward_xp": 50}
{"cmd": "commission_list"}
{"cmd": "commission_fill", "commission_id": 1}
{"cmd": "commission_cancel", "commission_id": 1}
{"cmd": "quest_turn_in"}

After each server response, decide your next action and send one JSON command.
Key messages from the server:
- "type": "room" — your room's full state (exits, NPCs, items, players)
- "type": "combat" — combat result (check for "dies" to detect kills)
- "type": "stats" — your HP, attack, gold, score
- "type": "error" — invalid command (use "look" to resync)
- "type": "death" — you died and respawned
```

**Quick-start Python bridge:**

```python
import asyncio, json, websockets

async def run_llm_agent(name="LLMAgent"):
    async with websockets.connect("ws://localhost:8765") as ws:
        await ws.send(json.dumps({"cmd": "login", "name": name}))
        resp = json.loads(await ws.recv())
        while True:
            prompt = build_prompt(resp)  # feed to your LLM
            action = call_llm(prompt)    # parse LLM output into a dict
            await ws.send(json.dumps(action))
            resp = json.loads(await ws.recv())

asyncio.run(run_llm_agent())
```

The LLM sees the same `room` snapshots, combat text, and errors as everyone
else. Treat `error` responses as a cue to `look` again and resync state.

## Protocol

Every message is JSON. Client → server messages have a `cmd` field:

```json
{"cmd": "login", "name": "Alice"}
{"cmd": "login", "name": "Bob", "token": "my-secret"}
{"cmd": "look"}
{"cmd": "move", "dir": "north"}
{"cmd": "move", "dir": "enter"}          # enter the party dungeon from graveyard
{"cmd": "move", "dir": "up"}             # retreat from dungeon floor 1 / sealed floors
{"cmd": "move", "dir": "down"}           # descend (only once the floor is cleared)
{"cmd": "attack", "target": "goblin"}
{"cmd": "take", "item": "sword"}
{"cmd": "drop", "item": "sword"}              # shed load — only works with a full pack (24 units)
{"cmd": "equip", "item": "sword"}
{"cmd": "use", "item": "healing herb"}
{"cmd": "rest"}                            # +5 HP, rest areas only, 2 gold
{"cmd": "heal"}                            # full heal, Sister Maren's tile only, 5 gold
{"cmd": "buy", "item": "healing herb"}
{"cmd": "buy", "item": "arrow"}              # ammunition for bows (2 gold)
{"cmd": "sell", "item": "wolf pelt"}
{"cmd": "craft", "recipe": "reinforced leather"}
{"cmd": "inventory"}
{"cmd": "stats"}
{"cmd": "who"}
{"cmd": "leaderboard"}
{"cmd": "help"}
{"cmd": "party_invite", "target": "Bob"}    # party commands
{"cmd": "party_accept"}
{"cmd": "party_leave"}
{"cmd": "party_info"}
{"cmd": "market_post", "item": "wolf pelt", "price": 25}   # works from anywhere
{"cmd": "market_list"}
{"cmd": "market_cancel", "id": 42}
{"cmd": "market_buy"}                      # auto-buys the cheapest affordable
{"cmd": "market_buy", "id": 42}
{"cmd": "market_expand"}                   # buy +1 sell-order slot (fee -> treasury)
{"cmd": "gather"}                          # harvest a gathering node in this room
{"cmd": "gather", "node": "pine_timber"}
{"cmd": "commission_post", "target": "rat", "required_kills": 5, "reward_gold": 25, "reward_xp": 50}
{"cmd": "commission_list"}
{"cmd": "commission_fill", "commission_id": 42}   # pays only with verified kills of the target since posting (no self-fills); repeated poster+filler pairs earn diminishing rewards (escrow remainder sunk to treasury)
{"cmd": "commission_cancel", "commission_id": 42}
{"cmd": "quest", "action": "list"}         # what quests exist, where, and their state
{"cmd": "quest", "action": "accept"}       # Town Guard quest (default: guard_charm)
{"cmd": "quest", "action": "turn_in"}
{"cmd": "quest", "action": "accept", "quest": "delver"} # Depth Delver quest (clear floors)
{"cmd": "quest", "action": "turn_in", "quest": "delver"}
```

### Equipment slots

Three slots coexist: **weapon** (attack), **armor** (damage reduction), and
**offhand** (shields; small damage reduction). `equip` routes each piece by
its `type` (`weapon` / `armor` / `offhand`); selling, dropping, or posting a
worn piece unequips it. Incoming damage is reduced by worn defense plus any
active damage-reduction buff. Former attack armors are now real armor:
Reinforced Leather (DR 2), Iron Plate (DR 3), Old Shield (offhand, DR 1).

### Carry cap

Packs hold **24 units**: worn gear and up to 5 arrows ride free. At the cap,
`take` / `gather` / `buy` / `market_buy` refuse with a "pack full" error
(gold is never charged on rejection); crafting, quest/commission rewards,
and GM grants always go through. `drop` (single unit, optional `"amount"`)
exists solely to shed load, so it only works with a full pack. Pack load
rides in `stats` as `pack` / `pack_max`.

### Ammunition

Ranged weapons declare their ammo family root in `world.json` (currently the
Oak Longbow needs `"ammo": "arrow"`). Any family member fires, best variant
first, adding its flat bonus: arrow +0, iron arrow +1, steel arrow +2.
Every attack with such a weapon equipped consumes 1 unit from inventory;
firing empty-handed is rejected with an error and costs nothing. Arrows are
ordinary junk otherwise (sellable, marketable, droppable): buy them from the
merchant (2 gold), loot them from Cave Bandits, pick them up at the Lumber
Camp, or craft all three variants. ML agents get `buy_arrows` /
`craft_iron_arrow` / `craft_steel_arrow` actions plus `arrows_norm`
(family count) and `ammo_best_norm` (best loaded bonus) scalars.

GM commands (only accepted on the dedicated loopback GM stream
`ws://127.0.0.1:8767`, i.e. the dashboard's GM tab — no login required,
no auth; the game port rejects all `gm_*` outright):

{"cmd": "gm_reward", "gold": 50, "player": "Alice"}          # spend tax to credit gold
{"cmd": "gm_reward", "item": "healing_herb", "player": "Bob"}
{"cmd": "gm_reward", "item": "healing_herb", "room": "deep_forest"}
{"cmd": "gm_buff", "type": "xp", "minutes": 10}              # 2x XP/gold world event
{"cmd": "gm_buff", "type": "gold", "minutes": 10}
{"cmd": "gm_boss", "room": "deep_forest", "strength": 3}     # elite boss 1-5 stars
{"cmd": "gm_announce", "text": "Double XP weekend!"}         # server-wide broadcast (25 flat)
{"cmd": "gm_heal", "player": "Alice"}                        # full heal (2 tax/missing HP)
{"cmd": "gm_teleport", "player": "Bob", "room": "market"}    # relocate (50 flat, surface only)
{"cmd": "gm_slay", "target": "rat"}                          # kill an NPC anywhere (1 tax/HP, min 10)
{"cmd": "gm_kick", "player": "Griefer", "reason": "spam"}    # disconnect (free)

Server → client messages have a `type` field:

- `room` — full room snapshot (description, exits, NPCs, items, players).
  Pushed after login, look, move, and whenever the room changes. Includes
  `is_dungeon`, `dungeon_floor`, and `party_size`.
- `stats` — hp/max_hp/attack/gold/equipped weapon + `score`, `variety`,
  `level`, `xp`, `xp_to_next`, `party_size`, `inv`, `market_orders`, and the
  quest flags (`quest_guard_active` / `guard_charm_crafted` for the charm
  quest, `quest_delver_active` / `quest_delver_ready` for the delver quest).
- `score` — sent when your score changes.
- `xp` — sent when your XP changes (gained/total/reason).
- `level_up` — you leveled up (+5 max HP, +1 attack, full heal).
- `combat` — a line of combat text (pattern-match `"dies"` for kills).
- `message` — room events (someone arrived, left, spoke).
- `death` — you died and respawned at start room.
- `party` — response to `party_info` (party id, leader, members, dungeon meta).
- `market` — response to `market_list` (orders, treasury, tax rate + minimum).
- `error` — invalid command (bad direction, missing target, etc).
- `leaderboard`, `inventory`, `who`, `help`, `welcome` — self-explanatory.

## Adding NPCs / expanding the world

Everything lives in `world.json`:

- **Rooms** need `name`, `description`, and `exits` (direction → room id).
- **NPCs** need `room`, `hp`/`max_hp`, `attack`, `hostile`, `behavior`
  (`"idle"` or `"wander"`), `loot`, `gold`, and `respawn_seconds`.
- **Items** need `name`, `type` (`"weapon"` grants attack bonus via `damage`),
  and any custom fields you want.
- **Recipes** for crafting go in the `recipes` section. Each recipe has
  `inputs` and a `result`; optional `output_qty` creates a bundle (default 1),
  while optional `tier` and `category` fields classify recipes for the
  expanding crafting economy. Gather materials are sunk into recipes by
  difficulty: easy nodes feed tier-1 (reed fiber → bandage, salt + springwater
  → rations), medium nodes feed tier-2 (resin → oils, mountain herb → tonic,
  scrap iron → plate), heron feathers fletch steel arrows (tier 3).

  Multi-step pinnacles need crafted intermediates plus dungeon parts: Relic
  Aegis (floor-10 shard + Iron Plate + ectoplasm), Bulwark of the Deep
  (Warden's Trophy + troll hides + timber), Warden's Elixir (frost crystals +
  Ironhide Draught + ectoplasm), and the mid-tier Serpentbrand blade
  (serpent scales + iron + resin).

Consumable items may define a `buff` object with a `category`, `amount`,
`duration_actions`, and optional `description`. Using one consumes the item and
replaces the active effect in that category. Buffs are action-based, exposed
in `stats`, and currently support attack bonuses and incoming-damage reduction.

## Parties & the dungeon

Dungeons are **instanced and per-party**, not one shared map. The graveyard
has an `enter` doorway (`world.json` already shows the exit). Entering
auto-creates a solo party for you (or reuses your existing party) and
instantiates a **private, infinitely-deep staircase** owned by that party:
every member walks the exact same floors, and other players/bots get their
own instance. The entrance is `enter` from `graveyard`; floors are built
lazily the first time anyone steps onto them.

Rules:

- **Auto-party**: solo players enter alone; parties enter together and share
  one instance. `party_invite`/`party_accept`/`party_leave`/`party_info`
  manage membership (max `PARTY_MAX_MEMBERS` = 4). Leaving a solo party
  destroys its dungeon instance.
- **Enemies scale without bound — until floor 50.** HP grows `×1.35` per floor,
  attack `×1.5`, and guard count rises 1→4. Kills drop a `Dungeon Relic`
  worth more each floor. There is no level cap; nobody can farm it forever.
- **The Warden of the Deep** holds the last floor (50): a single fixed boss
  (350 HP / 28 attack, 10-minute respawn) dropping a `Warden's Trophy` plus
  150 gold. The trophy crafts into the **`Warden's Blade`** (fixed 13 damage,
  best weapon in the game — 2× Iron Ore + Serpent Scale, tier 4). No scaling
  blades exist; the stairs crumble below floor 50.
- **Exit rules**: floor 1 **always** has an `up` exit back to the graveyard
  (the retreat path). Deeper floors show *no exits* until the floor is
  cleared — once you descend, you're committed to that floor. A cleared
  floor shows `up` (and `down` to the next one). Moving `up` from a deep
  uncleared floor is rejected with `The exit is sealed...`.
- **You only see a floor once you're in it.** Room snapshots describe just
  the room you're standing in; the next floor's layout, enemies, and loot
  are revealed only when you enter.
- **Cleared floors stay open — until the guards respawn.** Guards always
  come back, on a per-floor timer that scales with depth
  (`20 + 10 × floor` seconds: 30 s on floor 1, 120 s on floor 10). When a
  guard returns to a cleared floor, the floor **re-seals**: the exits lock
  again, the blade reward is withdrawn, and you must fight the guard again
  to escape.

Because floors are generated on demand at runtime, the ML env (`ml_env.py`)
picks up the dungeon automatically: its observation/action space already
includes the dungeon rooms, guards, relic/blade items, and the
`enter`/`up`/`down` moves.

## Leveling & XP

Characters earn XP from kills, room discovery, dungeon floor clears,
crafting, market trades, and quest turn-ins. The curve is closed-form:
`xp_to_next(level) = round(100 × 1.5^(level-1))`. Leveling up grants
**+5 max HP and +1 attack** and fully heals you. Levels persist in
`score_entries` (restored on login). XP is deliberately *not* multiplied by
the variety/difficulty curves — score is the anti-grind metric, level is a
loose progression metric. Tuning constants: `LEVEL_HP_PER_LEVEL`,
`LEVEL_ATK_PER_LEVEL`, `XP_BASE`, `XP_GROWTH`.

## Player market & GM treasury

Any logged-in player can post, buy, and cancel sell orders **from anywhere in
the world**:

- `market_post` removes the item from your inventory and lists it at your
  price (or the server's suggested price if you omit it).
- `market_buy` charges your gold, takes the tax, pays the seller
  (offline sellers are credited a `gold_bank` paid out on their next login),
  and puts the item in your inventory. With no `id` it auto-buys the
  cheapest affordable order.
- Every completed sale pays `TAX_RATE` (10%, minimum `TAX_MINIMUM` = 1 gold) into the **GM treasury**
  (`tax_treasury`), tracked separately from the lifetime stat
  (`tax_collected_lifetime`). An operator can seed initial liquidity with
  `TEXTMMO_GM_SEED=<gold>`.
- Each seller holds at most `MARKET_ORDER_SLOTS_BASE` (3) open orders. Extra
  stall slots are bought with `market_expand`: the fee doubles per slot
  (50g, 100g, 200g, …) and goes to the GM treasury, so big traders fund the
  world events. Your slot count rides in `stats` as `market_slots`.

The GM treasury is spent on **GM actions**, which are only accepted on the
dedicated GM stream (`ws://127.0.0.1:8767`, loopback-only) that the
dashboard's GM tab connects to. The game port rejects all `gm_*` commands,
so clients can never execute them:

- `gm_reward` — inject gold to a character (1:1) or drop an item to a
  player/room (costs the item's floor value).
- `gm_buff` — activate a timed **2× XP or gold** world event (50 tax/min).
- `gm_boss` — spawn an Elite `N`-star Menace in a room (100 tax/star of
  strength; HP 30+40N, attack 5+4N, drops herbs, never respawns).
- `gm_announce` — server-wide broadcast to every online player (25 flat).
- `gm_heal` — restore a player to full HP (2 tax per missing HP; full-HP
  targets are rejected free).
- `gm_teleport` — move an online player to any static surface room (50 flat;
  respects the per-room player cap, no discovery XP awarded).
- `gm_slay` — kill a living NPC by name anywhere (1 tax per max HP, min 10;
  loot drops on the ground, dungeon floors unseal if it was the last guard,
  no score/XP awarded). Doubles as unsticking sealed floors.
- `gm_kick` — disconnect a player with an optional reason (free;
  moderation, not economy).

There is deliberately **no auth/token on GM actions** — the user asked for a
local-only, auth-free panel. The only gate is the loopback-only GM stream:
game clients, even local ones, always get "GM actions are only available
through the dashboard's GM stream".

## Quests

Quests are repeatable objectives from specific NPCs. All live quests share
the same pattern — accept by the NPC, complete the objective, turn in by the
NPC — and take a `quest` field (`guard_charm` default) to select which one.
Send `{"cmd": "quest", "action": "list"}` any time to see every quest, its
giver and room, and whether you hold it or it is ready to turn in. New quest
givers only need a `QUEST_GIVERS` entry; killing one pays almost nothing
(0.1 pts/XP, no gold), so farming givers is never worth it.

**1. Town Guard's charm quest** (`guard_charm`):

1. **Accept** — stand in Town Square with the Town Guard and send
   `{"cmd": "quest", "action": "accept"}`.
2. **Craft** — gather 1× Treant Bark (Ancient Treant, deep forest), 1× Troll
   Hide (Rock Troll, mountain pass), and 1× Ectoplasm (Restless Ghost —
   haunts the graveyard but wanders), then
   `{"cmd": "craft", "recipe": "ancient_guardian_charm"}` anywhere.
   Crafting the charm during an active quest flags it ready.
3. **Turn in** — back by the guard, `{"cmd": "quest", "action": "turn_in"}`
   for a fixed reward: **50 XP, 25 gold, 15 score**. Then repeat.

**2. Depth Delver quest** (`delver`):

1. **Accept** — same guard, `{"cmd": "quest_accept", "quest": "delver"}`.
   Your current cleared-floor count is recorded as the baseline.
2. **Clear** — descend the instanced dungeon and clear
   `QUEST_DELVER_FLOORS` (default 1) new floor(s).
3. **Turn in** — report back, `{"cmd": "quest_turn_in", "quest": "delver"}`
   for a fixed reward: **30 XP, 15 gold, 10 score**. The baseline resets,
   so it repeats forever.

**3. Sister Maren's remedy quest** (`remedy`, from the Healer at the Healing Spring):

1. **Accept** — `{"cmd": "quest", "action": "accept", "quest": "remedy"}` by her side.
2. **Gather** — bring 3× Healing Herb (picked up around the wilds).
3. **Turn in** — herbs are consumed, reward: **20 XP, 10 gold, 8 score**. Repeatable.

**4. Sister Maren's tonic quest** (`tonic`):

1. **Accept** — `{"cmd": "quest", "action": "accept", "quest": "tonic"}`.
2. **Brew** — craft 1× Fortitude Tonic (Iron Ore + Mountain Berry).
3. **Turn in** — tonic is consumed, reward: **40 XP, 20 gold, 12 score**. Repeatable.

Sister Maren also heals: `{"cmd": "heal"}` restores you fully, but only on
her tile. `rest` (+5 HP anywhere) still works as field dressing.

Quest state rides along in `stats` events and persists in `scores.json`.
Tuning constants (`QUEST_GUARD_XP/GOLD/POINTS`, `QUEST_DELVER_*`, inputs,
giver) live in the "Quests" block of `server.py` and are mirrored
symbolically in `ml_env.py` (`QUESTS`) so agents get a stable description
instead of inferring rules from rewards. The `/api/state` snapshot exposes
a `quests` section (catalog, live active counts, lifetime completions,
turn-ins/min) — not yet rendered by the dashboard (see Known Issues).

## Scoring system

Scores persist across restarts in `scores.json`. Query with `{"cmd": "stats"}`
or `{"cmd": "leaderboard"}`. Points come from killing NPCs (weighted by
difficulty), discovering rooms, with a wealth-scaled penalty for death (see below).

Death also splits your carried gold: **40% drops as a loose pile where you
fell** (pick it up with `{"cmd": "take", "item": "gold"}` — optional
`"amount"` takes only part), **10% vanishes permanently**, you keep the rest.
Piles are visible in `room` snapshots (`gold` field) and on the dashboard.
The score penalty scales with what you lost: a flat 5.0 floor plus 0.1 per
gold removed, so dying broke costs 5 while dying with 1000g costs 55.

The system resists grinding the same loop forever via two mechanics:
- **Variety decay** — repeating the same actions multiplies your gains down.
- **Global difficulty curve** — marginal reward shrinks as your total score
  grows.

Tuning constants live at the top of `server.py`.

## Multiplayer mechanics

- **Ally attack bonus** — other players in your room add +1 attack (capped at +3).
- **Teamwork kill splitting** — damage contributors share a boosted point pool
  on kill. Cooperating is strictly better than solo grinding.
- **Parties** — group up (`party_invite` → `party_accept`) to share one
  instanced dungeon. Kill credit is shared normally; party size expands the
  ally bonus and lets the group clear deep floors together.

## Setting up an ML agent

All the ML components live in the `ml/` folder: `ml_env.py` (the Gym-shaped
async wrapper with fixed-size observations and a discrete action space),
`ml_client.py` (a simple online Q-learning agent with no external ML
dependencies), `ml_botfarm.py` (the multi-bot training farm), the trained
`ml_weights.json`/`ml_best.json`, plus `ml_client.bat` / `ml_botfarm.bat`
launchers for Windows. Run them from the repo root
(`python3 ml/ml_client.py ...`) or double-click a `.bat`.

```bash
python3 server.py          # terminal 1
python3 ml/ml_client.py    # terminal 2
```

Run `python3 ml/ml_env.py` for a random-agent demo. See `ml_env.py` for the
observation/action space documentation.

### Training an ML farm (multiple concurrent bots)

`ml/ml_botfarm.py` runs any number of concurrent ML agents in one process.
They share a single policy but each is its own character/WebSocket
connection into the same world, so you can watch them from the dashboard.
It trains continuously until you stop it (Ctrl+C), and it **always keeps
the best version** of the policy:

- `ml_weights.json` — the current policy, whatever the farm is training now.
- `ml_best.json` — a snapshot of the weights the moment the farm achieved
  its best average reward so far. It's only overwritten when a later
  checkpoint is strictly better, so a bad run or crash can't destroy your
  best policy.

```bash
python3 server.py                      # terminal 1
python3 ml/ml_botfarm.py --bots 4      # terminal 2 — 4 concurrent bots until Ctrl+C
```

Options include `--bots N` (how many bots play concurrently; default 4),
`--name-prefix P` (character names become P0, P1, ...  — reuse a prefix to
keep training the same persistent characters), `--steps N` (default 0 =
until stopped), `--reward-window`/`--eval-every` (how fitness is judged),
and the same epsilon knobs as `ml_client.py`. Bots start staggered (2s per
index) so they meet different initial states. Each run logs a rolling
status line (`[farm] steps=... eps=... best=... bots: ...`) and a summary.
`ml_botfarm.bat` restarts the farm only on clean exit (code 0); Ctrl+C exits
1 and stops. For large populations with churn, see the conductor plan in
`TODO.md`.

## PyTorch DQN agent (`torch_agents/`)

A PyTorch-based Deep Q-Network agent that trains on the same observation/action
space as the `ml/` agents, using the server's score as the reward signal. Because
the reward inherits the game's anti-grind variety and diminishing-returns curves,
the agent learns the same resistance to hardcoded loops that governs all players.

### Observation space

Same as `flatten_obs()` in `ml_env.py` — a 175-dimensional vector covering:

- Room one-hot (32 static rooms)
- Exit mask across all directions (including `up`/`down`/`enter`)
- NPC presence (23 static NPCs + unknown-count)
- Item presence (39 ground items + 39 inventory items + flags)
- Scalars: `hp_frac`, `gold_norm`, `score_norm`, `variety`, `allies_norm`,
  `party_norm`, `level_norm`, `xp_progress`, `market_norm`, `market_any`,
  plus market-tax terms: live `tax_rate`, `tax_min_norm`, and `own_net_norm`
  (the exact after-tax net value of the agent's standing sell orders, using
  the server's 10%-with-1-gold-minimum formula), plus disposition terms:
  `flip_margin_norm` (best list-over-merchant margin held) and
  `inv_value_norm` (merchant value of everything carried) — these let the
  policy learn quicksell-vs-hold-vs-speculate instead of acting blind
- Quest block (7 dims): `quest_active`, `quest_ready`, `quest_has_charm`,
  `quest_mat_bark`, `quest_mat_hide`, `quest_mat_ecto`, `quest_giver_here`
- Delver block (3 dims): `quest2_active`, `quest2_ready`, `quest2_giver_here`
  (same guard/room; readiness = enough new dungeon-floor clears)
- Ammo (1 dim): `arrows_norm` (whole-family arrow count — any variant fires,
  so the count itself is observed, not just presence)
- Buffs (2 dims): `buff_attack`, `buff_dr` (whether an attack or
  damage-reduction consumable effect is currently active)
- Gear (2 dims): `ammo_best_norm` (best loaded arrow bonus), `defense_norm`
  (worn armor + offhand damage reduction)

### Action space (49 actions — covers all game mechanics)

Agents only ever pick valid actions: the env exposes `valid_action_mask()`
(1 per action whose prerequisites are visibly met — right room, enough gold,
mats held) and both trainers mask exploration and exploitation to it, so no
step is wasted on a guaranteed-error command. Anything the client can't
verify still goes through and errors as a learning signal.

| Category | Actions |
|---|---|
| Movement | `move_north`, `move_south`, `move_east`, `move_west`, `move_enter`, `move_up`, `move_down` |
| Combat | `attack` |
| Items | `take`, `drop` |
| Survival | `rest`, `heal` |
| Information | `look` |
| Market | `buy`, `buy_arrows`, `sell`, `equip`, `use`, `craft`, `craft_charm`, `craft_iron`, `craft_arrows`, `craft_sharpening_oil`, `craft_fortitude_tonic`, `craft_greater_sharpening_oil`, `craft_ironhide_draught`, `craft_wardens_blade`, `craft_iron_arrow`, `craft_steel_arrow`, `craft_serpentbrand`, `market_post`, `market_buy`, `market_cancel`, `market_list`, `market_expand` |
| Gathering | `gather` |
| Commissions | `commission_post`, `commission_list`, `commission_fill`, `commission_cancel` |
| Parties | `party_invite`, `party_accept`, `party_leave`, `party_info` |
| Quests | `quest_accept`, `quest_turn_in`, `quest_list`, `craft_charm`, `quest2_accept`, `quest2_turn_in` |

### Quests (Town Guard repeatable quest)

Talk to the Town Guard in Town Square to accept his quest, craft an
Ancient Guardian Charm from 1x Treant Bark + 1x Troll Hide + 1x Ectoplasm
(`craft_charm`), and turn it in by the guard for a fixed repeatable reward
(50 XP + 25 gold + 15 score). Accept and turn-in both require standing in
the guard's room, and the turn-in pays score — so score-optimizing agents
learn the quest loop directly. Models see it three ways:

- **Symbolic:** `ml_env.QUESTS` mirrors `server.QUESTS` (giver, room,
  inputs, result, fixed reward, repeatable) plus `quest_stage(obs)` which
  reports `no_quest` / `collect` / `ready_turn_in` / `charm_no_quest`.
- **Vector:** the 7-dim quest observation block above tells the policy
  *where it stands* (active? mats held? charm crafted? guard here?).
- **Reward:** `step()` returns `info["quest"]` with `accepted` / `turned_in` /
  `crafted_charm` transitions; the torch agent regresses a quest head toward
  the turn-in value as auxiliary shaping.

Commands: `{"cmd": "quest", "action": "accept" | "turn_in" | "list"}` (a
`quest_list` env action reads the catalog). The delver
quest reuses the same commands with `"quest": "delver"`
(`quest2_accept` / `quest2_turn_in` in the env); its readiness is new
dungeon-floor clears since accept, tracked in `info["quest"]` the same way
(`became_ready` instead of `crafted_charm`).

### Key design

- **Architecture:** 3-layer MLP (input → 128 → 128) with 5 heads:
  49 Q-values + gold + loot + market + quest predictors
- **Connection resilience:** `ml_env.step()` now catches `websockets.exceptions.ConnectionClosedError`, sets `done=True`, and returns a terminal observation so the farm/bot continues rather than crashing.
- **Exploration:** epsilon-greedy with linear decay
- **Learning:** online TD update with Huber loss + experience replay (10000 transitions)
- **Reward:** change in server score (covers kills, discoveries, market trades,
  crafting, quest turn-ins, assist payouts — inherits the anti-grind curve automatically)
  plus group-play shaping (reward only): per-ally bonus while grouped and a
  one-time formation bonus on joining/forming a party, both diminish-scaled
- **Market P&L (tax-aware):** buys cost full price; filled listings net
  `market_net(price)` after the server's 10%-with-1-minimum tax, so the
  market head learns true profitability instead of raw prices
- **Quest shaping:** the quest head regresses toward the fixed turn-in score
  on turn-in steps (0 otherwise), forcing the shared trunk to encode quest
  stage; turn-ins also pay real score so the TD target learns quest value.
  A small intrinsic bonus fires on accept/progress transitions to bootstrap
  the long chains (state-machine-gated, so it can't be farmed); the loot
  target on charm-craft steps is the charm's gold-terms net (25g reward −
  34g mat value = −9g), teaching sell-mats-vs-quest profitability.
- **Second quest:** Depth Delver (same guard/room/pattern): accept, clear
  dungeon floors, turn in for 30 XP + 15 gold + 10 score, repeatable.
- **Persistence:** weights to `ml_weights.json` (shared with `ml_client.py`);
  best model to `ml_best.json`. Note: the quest and world expansions changed
  OBS_SIZE (now 175) and N_ACTIONS (now 49), so older checkpoints need retraining.
- **Training:** call `agent.train(total_steps=N)` from Python, or run
  `torch_agents\torch_batch_loop.bat` after starting the server

### Quick start

```bash
python3 server.py                                    # terminal 1 (world)
python3 torch_agents/dqn_agent.py --demo           # terminal 2 (smoke test)
# or, to train:
python3 torch_agents/dqn_agent.py --name TorchBot --steps 5000
```

Weights and checkpoints are saved as `torch_agents/ml_weights.json` /
`torch_agents/ml_best.json` (`torch.save` format — separate from the JSON weights
used by `ml/ml_client.py`, so the two trainers don't share policies).

```bash
python3 server.py                              # terminal 1
torch_agents/torch_bot.bat --name MyAgent      # terminal 2 — trains one agent
```

### Relevant files

- `torch_agents/dqn_agent.py` — DQN agent + training loop
- `torch_agents/torch_bot.bat` — launcher (starts agent after server is running)
- `torch_agents/torch_bots.bat` — launcher (starts N concurrent agents)
- `torch_agents/torch_batch_loop.bat` — launcher (four concurrent agents, then four fresh agents after each batch)
- `torch_agents/ml_weights.json` / `torch_agents/ml_best.json` — saved policy checkpoints

For continuous batch training, start the server first and run:

```bat
torch_agents\torch_batch_loop.bat 2000
```

This runs four agents concurrently for 2,000 steps each, waits for all four
to finish, then starts the next batch with fresh character names. Use a
second argument to limit the number of batches during a test, for example
`torch_agents\torch_batch_loop.bat 10 3`. Per-agent logs and completion markers are
written under `%TEMP%\textmmo_torch_batch_*`.

## Running tests

The `tests/` folder holds the verification suite (stdlib + `websockets`
only). Run from the repo root:

```bash
python3 tests/test_server_unit.py   # no server needed: XP, dungeon gen/seal,
                                    # market tax, GM spending
python3 tests/test_quest_grind.py   # no server needed: proves quest-turn-in
                                    # loops decay (variety + difficulty curve)
```

```bash
# live tests need a fresh server: reset scores.json to {}, then
TEXTMMO_GM_SEED=700 python3 server.py   # terminal 1
python3 tests/test_live.py              # terminal 2: full protocol E2E
python3 tests/test_env.py               # terminal 2: ML env E2E (parties,
                                        # dungeons, market across two agents)
```

## Capacity limits / scaling

The server keeps per-room and per-name indexes (no full `players` scan on
every event) and writes `scores.json` at most once every few seconds instead
of on every point award, so a bot swarm / ML farm can grow to hundreds of
connections without bogging down.

- `MAX_TOTAL_CONNECTIONS` (default 1000) — new connections beyond this are rejected.
- `MAX_PLAYERS_PER_ROOM` (default 12) — move into a full room is rejected.

Both are env-overridable (useful for a bigger box without code edits):
`TEXTMMO_MAX_CONNECTIONS` and `TEXTMMO_MAX_ROOM_PLAYERS`. The score flush
interval is `SCORES_SAVE_SECONDS` (default 5.0).

Long-term state is bounded so fresh-name bot farming can't grow memory or
`scores.json` without limit:

- `SCORE_ENTRY_MAX` (default 2000) / `SCORE_ENTRY_TTL_SECONDS` (default 7 days) —
  score entries untouched for the TTL are evicted (never online players or
  entries owed banked gold); their track/score history goes with them.
- `COMMISSION_TTL_SECONDS` (default 1 hour) — completed/cancelled commissions
  are pruned. Open bounties hold real escrow and are never pruned. Repeated
  poster+filler pairs earn diminishing rewards (1/(1+prior_fills), floor 10%);
  any escrow remainder is sunk to the treasury.
- `DUNGEON_MAX_FLOOR` (default 50) — the stairs crumble below this; one
  deep-diving party can't accumulate floor objects (or per-floor item
  registrations) forever.
- Stale party invitations are dropped on disconnect.

### Resilient background tasks

NPC AI, score persistence, and the dashboard snapshot each run in their own
background task. If any of them crashes, the engine automatically restarts it
after a short delay rather than letting the world silently degrade. A bug in
one system can't permanently freeze another.

### Outbound queue (head-of-line blocking fix)

Each player gets a bounded outbound queue (`TEXTMMO_OUTBOUND_QUEUE`, default 64).
When a slow or stuck client falls behind, old messages are dropped rather than
stalling the whole event loop for everyone else. Never happens in normal play
— only matters at scale.

### Accounts & identity

Every name can only be used by one connection at a time. Trying to `login`
with a name someone is already using returns:

```json
{"type": "error", "text": "The name 'Naruto' is already in use right now."}
```

You must open a fresh connection to switch characters.

#### Token-protected names (optional)

When a name is first used with a `token`, it is claimed:

```json
{"cmd": "login", "name": "Sasuke", "token": "my-secret-key"}
```

Any subsequent attempt without the same token (or with no token) is rejected:

```json
{"type": "error", "text": "The name 'Sasuke' is protected by a token. Login rejected."}
```

Tokens are persisted in `scores.json` and survive server restarts.

To **require** every name to use a token (e.g. in a competitive server), set:

```bash
TEXTMMO_REQUIRE_TOKEN=1 python3 server.py
```

Without a token on the very first login, the name is not claimed — anyone can
take it next time it is free.

#### Login in the protocol

```json
{"cmd": "login", "name": "Alice"}
{"cmd": "login", "name": "Bob", "token": "my-secret"}
```

A token is purely your choice; it never affects gameplay, score, or the ML
agent. In-game interaction with *other players' characters* via `attack`
and other live commands is always allowed and unaffected - tokens
only prevent a different *script* from taking over someone's name.

## Community

* [Contributing guide](CONTRIBUTING.md) — workflow for non-coders and
  researchers, protocol-stability rules, releases, labels/milestones.
* [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
* [Security policy](SECURITY.md) — how to report vulnerabilities
  (privately, never in a public issue).
