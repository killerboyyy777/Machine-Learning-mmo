# v0.10.0 economy baseline (08f7453)

Fresh world, 20 agents, 1 hour, seed 700. Captured with the nightly soak
driver (`ml/conductor/soak.py`) and the same slot mix as `soak.yml`
(linear x2, gather, dungeon, market, commissioner), so later balance
scenarios have a v0.10.0 reference point.

## Run

- Master: `08f7453` (empty `scores.json`, no registry resume)
- Window: 2026-10-22T09:58:33Z to 2026-10-22T10:58:42Z (3600s)
- Ports: game 18770, dashboard 18771, GM 18772 (loopback)
- Config: `server_config.json` + `ml/conductor/soak_server_config.json`
  (short TTL overlay; caps unchanged)
- Seed: 700 (agent RNG via `--seed`; `TEXTMMO_GM_SEED=700` is seed money,
  so the treasury starts at 700, per the repo's fresh-world recipe)
- Python: 3.14.2 (local; repo/CI pins 3.12)

```text
TEXTMMO_GM_SEED=700 python server.py --port 18770 --http-port 18771 --gm-port 18772 --config ml/conductor/soak_server_config.json
python ml/conductor/soak.py --agents 20 --duration 3600 --seed 700 --min-agents 16 \
  --url ws://127.0.0.1:18770 --gm-url ws://127.0.0.1:18772 \
  --slot linear --slot linear --slot gather --slot dungeon --slot market --slot commissioner
```

## Headline

| Metric | Value |
| --- | --- |
| Agents alive at end | 20/20 |
| Episodes | 871 |
| Mean reward / episode | -0.1293 |
| Market trades | 33 |
| Open orders at end | 0 |
| Treasury (start / end) | 700.0 / 700.0 |
| Tax collected lifetime | 35.0 |
| Sunk lifetime | 35.0 |
| Effective tax | 22.22% |
| Banked gold (mean / median / total) | 20.65 / 5.5 / 413 |
| Kills / deaths | 1080 / 917 |
| Levels | L1:14 / L2:6 |
| Solo / group dungeon clears | 66 / 0 |
| Commissions (open / done) | 5 / 0 |
| Quest completions | 4 |
| Craft history | 0 |
| Server errors (handler / traceback) | 0 / 0 |

## By agent type

| Type | Alive | Episodes | Mean reward |
| --- | --- | --- | --- |
| linear | 10 | 364 | -0.0855 |
| scripted | 10 | 507 | -0.1607 |

## Notes

- Per-agent stats come from `scores.json` (the live state was sampled after
  the agents disconnected); carried gold is not persisted, so gold is the
  banked balance only.
- `TEXTMMO_GM_SEED=700` is seed money, not world RNG: the treasury starts at
  700. The "no seeded treasury" economy scenario will need it set to 0.
- Treasury stayed at 700 while 35 gold was collected and sunk (`_treasury_credit`
  banks only up to the 500 reserve), so this baseline is the reserve-sink
  steady state, not a zero-start curve.
- Open orders read 0 at capture: the soak overlay expires orders at 300s,
  and logout clears a player's orders.
- Raw logs and the full soak run (`soak_report.json`, status JSONL, server
  log, `scores.json`) stay outside git; this file is the distilled snapshot.
- Liveness gate was 16/20 (nightly uses 40/50); the balance read is the
  per-type rewards plus the economy counters above.
