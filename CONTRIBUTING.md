# Contributing

Everyone is welcome. Non-coder players and config tuners sit on one side, ML researchers and engine hackers on the other. Pick a lane below. Both lanes follow the same workflow and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Workflow for all contributors

1. **Issues first.** Report bugs and request features through the YAML issue forms in `.github/ISSUE_TEMPLATE/`. Fill in reproduction steps, logs, and the area, priority, and effort dropdowns so triage takes one click.
2. **One branch per change.** Branch from `master` with a name like `feature/...` or `fix/...`, and keep one concern per branch. `master` is protected, so a PR merge needs four green CI checks on an up-to-date branch. Use the PR flow even though direct pushes are technically allowed.
3. **Prove it the way CI does.** Before opening a PR, run at least:
   ```bash
   python tests/test_server_unit.py
   python tests/test_quest_grind.py
   ```
   If you touched the protocol or the economy, also run the live suite against a fresh server. Clear `scores.json` with `echo '{}' > scores.json`, start the server with `TEXTMMO_GM_SEED=700`, then run `python tests/test_live.py && python tests/test_env.py`.
4. **Keep the protocol stable.** The WebSocket JSON protocol is the contract between the server, dashboard, bots, and agents. If you change a command, event, or observation shape, say so in the PR and update `README.md`, `dashboard.html`, and the ML env in the same PR.
5. **Ship docs with the change.** Every PR updates the docs it affects. Do not open a "docs later" PR. Use `README.md` and the wiki for behavior, the PR body for anything user-visible (release notes are written from it), docstrings for new modules and functions, and `.github/` docs for workflow changes. A PR with stale docs is not done.
6. **Install the pre-commit hooks once.** Run `pip install pre-commit && pre-commit install`. Black, ruff, mypy, and a fast test subset then run on every commit. See `.pre-commit-config.yaml`.

## Lane 1: non-coders (config, dashboard, balance)

No code required.

- **Tune the game** without touching Python. `server_config.json` covers the economy, leveling, and dungeons. `ml/ml_config.json` covers agent reward shaping. Delete a key to fall back to its default. Validate a file with `python -c "import json; json.load(open('server_config.json'))"`.
- **Build worlds** in `world.json`: rooms, NPCs, items, and recipes. Restart the server to load changes.
- **Report balance issues** as bugs with numbers: faucet and sink math, what an agent or player can extract per hour, and a replay or log.
- **Suggest dashboard panels** in an issue with a sketch and the `/api/state` fields it needs.

## Lane 2: researchers and engine hackers (agents, algorithms, experiments)

- **New agent or reward mode?** Reuse the harness. `TextMMOEnv` in `ml/` fixes the observation and action space, `ml/ml_botfarm.py --scripted` holds the baselines, and `python -m torch_agents.eval --checkpoint X --seeds 10` runs evaluation. Report the mean and standard deviation over seeds, never a single run.
- **Checkpoints carry provenance.** The git SHA, config hash, canonical `world.json` hash, and observation and action dimensions are embedded on save (see `checkpoint_version()`). The world hash is separate so checkpoints saved before it existed keep their old config hash and are treated as world-compatible. Mention the hashes when you compare runs.
- **Conductor runs** need a live server: `python ml/conductor/soak.py --agents N --duration S`. Keep the `--min-agents` gates honest; a failed gate is a failure. The tool resumes prior state by default. Select wipes with `--reset none|lineage|cell|all` and `--resume/--no-resume`.
- **PBT is library-only.** `Conductor(pbt={})` enables `PBTManager`, but the shipped soak CLI does not enroll agents or feed fitness, so a soak emits no exploit/explore rounds; wire those hooks yourself for a PBT experiment.
- **Mind the perf budget.** The server is single-process asyncio. If your PR adds per-command or per-tick work, note its cost. Watch the nightly soak and the 30-agent farm for regressions.

## Observability

These surfaces are off by default, so a normal test run does not exercise them. Check them when you change the server, dashboard, or conductor:

- `TEXTMMO_METRICS=1` or `python server.py --metrics` exposes `/metrics` on the dashboard port. The route has no auth and serves aggregate gauges only.
- `TEXTMMO_TELEMETRY=1` or `python ml/conductor/soak.py --telemetry-dir DIR` writes `telemetry.jsonl` and `telemetry.json` for a soak run.
- `TEXTMMO_STRUCTURED_LOG=1` or `--structured-log-dir DIR` writes the conductor's `structured.jsonl` event log.

Run `python tests/test_metrics_route.py`, `python tests/test_telemetry.py`, and `python tests/test_structured_log.py` alongside the suites in step 3.

## Branch protection

The "Master Branch Protection" ruleset guards `master`. A PR merge needs four green CI checks on an up-to-date branch, and force pushes and deletions are forbidden.

There is deliberately no approval requirement. The owner cannot approve their own PR, so any approval count above zero deadlocks every merge. This was verified the hard way. Add `required_approving_review_count` when a second human joins. The convention stays branches, PRs, and green CI, then `gh pr merge --squash` once clean. Never bypass the rule for yourself.

To apply or reapply the rule after creating or transferring the repo, run:

```bash
bash scripts/enable-protection.sh
```

That script issues the exact ruleset defined in `scripts/enable-protection.sh`. Issue #132 lists the required checks.

## Release process

Releases are tag-driven. `git tag vX.Y && git push origin vX.Y` runs `.github/workflows/release.yml`, which smoke-tests and publishes the GitHub release with notes generated from the merged pull requests. The same tag runs `.github/workflows/docker-publish.yml` to build the versioned container images. Bump `version` in `pyproject.toml` in a merged PR before tagging; the publish job compares the tag with that version and fails before building when they differ.

## Labels and milestones

- **Labels.** `P0` to `P3` for priority, `XS` to `L` for effort, and `area:*` for subsystem. `.github/labeler.yml` applies area labels from changed paths.
- **Milestones** track phases from `Phase A` to `Phase F`, not priority. A closed phase stays closed as history. Work in progress lives on a `phase-*` or `fix-*` branch until green, then merges to `master`.
