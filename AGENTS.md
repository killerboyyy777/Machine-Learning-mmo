# AGENTS.md - Machine-Learning-mmo contributor notes (humans + agents)

## Stack
- Python server (server.py), text MMO over WebSocket (:8765 game,
  :8766 dashboard HTTP, :8767 GM loopback). Only hard dep: websockets.
- Dashboard is static HTML (dashboard.html, no build, no CDN).
- ML: ml/ env + conductor, torch_agents/ DQN farm (CPU).
- Tests: `tests/` contains script-style suites plus fixtures/helpers; not every `.py` file is a standalone test. Use the explicit entrypoints in `.github/workflows/tests.yml`; some require a live server, torch, Node, or Firefox.

## Rules
- ASCII-only, no emoji. Keep diffs tight, no drive-by refactors.
- Branches: agent/<issue>-<slug> (or fix/, test/ prefix by kind).
  Never commit to master. Open PRs, never merge.
- Stacked PRs: never merge a base branch with `gh pr merge --delete-branch`.
  The delete closes the child PR and GitHub then refuses both `gh pr reopen`
  and a base change on a closed PR. Retarget the child to master first. To
  recover: recreate the ref at the merge SHA
  (`git push origin <sha>:refs/heads/<branch>`), reopen, retarget.
- Prove before pushing: named suites + exit codes in the report.
- Lint: black + ruff clean on CHANGED files (legacy exempt).
- Never touch .github/workflows/ unasked. Market buy orders and standing
  bids are real (`cmd_market_buy_order`); do not add new buy-side trading
  to agent code unasked.

## Wiki
- The GitHub wiki is a separate repo, pushed directly (no PR). Keep it in sync
  with every release/update: clone
  `C:\Users\Jona\AppData\Local\Temp\opencode\mmo-wiki\`, commit, push.
- Keep only the newest release plan page (e.g. `v0.19-plan.md`). Older plans
  bloat the wiki; they stay recoverable from the wiki repo's git history.
  When a new plan is published, delete the previous one in the same push.

## Comments
Do not sign your work. No `// agent-name:`, `# overlord:`, `# killerboy:` or similar signature comments anywhere in the codebase.

Comments are rare and earn their place: write one only if it helps the next agent (or human) understand something the code alone does not say. A comment describes WHY the code exists -- never WHAT it does. If the what needs explaining, rename instead of commenting.

Questions to ask before writing a comment:
- Would an agent reading this cold misunderstand the intent without it?
- Does it record a decision, a trap, or a reason (WebSocket protocol quirk, tick-loop timing, agent training constraint, save format version)?
- Will it still be true in six months?

If any answer is no, delete the comment.

## Proof bar
- State what you ran, exit codes, and what you did NOT run.
- Report: branch, base SHA, head SHA, per-item one-liners, files changed.
