# AGENTS.md - Machine-Learning-mmo contributor notes (humans + agents)

## Stack
- Python server (server.py), text MMO over WebSocket (:8765 game,
  :8766 dashboard HTTP, :8767 GM loopback). Only hard dep: websockets.
- Dashboard is static HTML (dashboard.html, no build, no CDN).
- ML: ml/ env + conductor, torch_agents/ DQN farm (CPU).
- Tests: tests/ pytest-style scripts, python tests/<file>.py each green.

## Rules
- ASCII-only, no emoji. Keep diffs tight, no drive-by refactors.
- Branches: agent/<issue>-<slug> (or fix/, test/ prefix by kind).
  Never commit to master. Open PRs, never merge.
- Prove before pushing: named suites + exit codes in the report.
- Lint: black + ruff clean on CHANGED files (legacy exempt).
- Never touch .github/workflows/ unasked. Never invent market buy
  orders (sell-side only).

## Proof bar
- State what you ran, exit codes, and what you did NOT run.
- Report: branch, base SHA, head SHA, per-item one-liners, files changed.
