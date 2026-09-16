# Contributing

Everyone is welcome: non-coder players and config tuners on one side, ML
researchers and engine hackers on the other. Pick your lane below; both
lanes follow the same workflow and [Code of Conduct](CODE_OF_CONDUCT.md).

## Workflow (all contributors)

1. **Issues first.** Bug reports and feature requests use the YAML issue
   forms (`.github/ISSUE_TEMPLATE/`): fill in repro steps, logs, and the
   area/priority/effort dropdowns so triage is one click.
2. **Branch per change** from `master` (`feature/...`, `fix/...`), one
   concern per branch. `master` is protected: PRs need 1 review and green
   CI (see "Branch protection" below) -- direct pushes are for nobody.
3. **Prove it like CI does.** Before opening a PR, run at least:
   ```bash
   python tests/test_server_unit.py
   python tests/test_quest_grind.py
   ```
   and, if you touched the protocol/economy, the live suite against a
   fresh server (`echo '{}' > scores.json`, `TEXTMMO_GM_SEED=700`,
   then `python tests/test_live.py && python tests/test_env.py`).
4. **Keep the protocol stable.** The WebSocket JSON protocol is the
   contract between server, dashboard, bots, and agents. Changing a
   command, event, or observation shape? Say so in the PR and update
   `README.md`, `dashboard.html`, and the ML env in the same PR.
5. **Pre-commit hooks.** Install once (`pip install pre-commit &&
   pre-commit install`): black, ruff, mypy, and the fast test subset run
   on every commit (see `.pre-commit-config.yaml`).

## Lane 1 — Non-coders (config, dashboard, balance)

No code required:

* **Tune the game** without touching Python: `server_config.json`
  (economy, leveling, dungeons) and `ml/ml_config.json` (agent reward
  shaping). Delete a key to fall back to the default. Validate with
  `python -c "import json; json.load(open('server_config.json'))"`.
* **Build worlds** in `world.json` (rooms, NPCs, items, recipes); restart
  the server to load.
* **Report balance issues** as bugs with numbers: faucet/sink math, what
  an agent (or player) can extract per hour, and a replay or log.
* **Dashboard ideas** go in issues with a sketch of the panel and which
  `/api/state` fields it needs.

## Lane 2 — Researchers & engine hackers (agents, algorithms, experiments)

* **New agent or reward mode?** Reuse the harness: `TextMMOEnv` in `ml/`
  (fixed obs/action space), baselines in `ml/ml_botfarm.py --scripted`,
  eval via `python -m torch_agents.eval --checkpoint X --seeds 10`.
  Report mean/std over seeds, never a single run.
* **Checkpoints carry provenance**: git SHA, config hash, obs/action dims
  are embedded on save (see `checkpoint_version()`); mention them when
  comparing runs.
* **Conductor runs**: `python ml/conductor/soak.py --agents N --duration S`
  needs a live server; keep `--min-agents` gates honest (fail = fail).
* **Perf budget**: the server is single-process asyncio; a PR that adds
  per-command or per-tick work should note the cost (the nightly soak and
  the 30-agent farm are the canaries).

## Branch protection

`master` requires 1 review + green CI, no force pushes, no deletions
(enabled when the repo goes public / Pro; until then this is convention,
enforced by review). To (re)apply the rule programmatically after repo
creation or transfer, run:

```bash
bash scripts/enable-protection.sh
```

which issues the exact ruleset in `scripts/enable-protection.sh` (see
issue #132 for the required checks list). Never bypass it for yourself.

## Release process

Tag-driven: `git tag vX.Y && git push origin vX.Y` runs
`.github/workflows/release.yml`, which smoke-tests and publishes the
GitHub release (notes auto-drafted from PR labels via release-drafter).

## Labels & milestones

* **Labels**: `P0`–`P3` priority, `XS`–`L` effort, `area:*` subsystem
  (auto-applied by `.github/labeler.yml` from changed paths).
* **Milestones** track phases (`Phase A`–`Phase F`), not priority:
  closed phases stay closed as history; in-progress work lives on a
  `phase-*`/`fix-*` branch until green, then merges to `master`.
