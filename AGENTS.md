# AGENTS.md: Machine-Learning-mmo contributor notes

This file is for humans and agents working in this repo.

## Stack

- The Python server (`server.py`) runs the text MMO over WebSocket: port 8765 for the game, 8766 for dashboard HTTP, and 8767 for the GM loopback. The only hard dependency is `websockets`.
- The dashboard is static HTML (`dashboard.html`) with no build step and no CDN.
- The ML code lives in `ml/` (environment and conductor) and `torch_agents/` (CPU DQN farm).
- Tests in `tests/` are script-style suites plus fixtures and helpers. Not every `.py` file there is a standalone test. Use the explicit entrypoints in `.github/workflows/tests.yml`. Some need a live server, torch, Node, or Firefox.

## Rules

- Use ASCII only in code and code comments, with no emoji. Docs may use normal punctuation. Keep diffs tight and avoid drive-by refactors.
- Branch with the prefix `agent/<issue>-<slug>`, or `fix/` or `test/` by kind. Never commit to `master`. Open PRs and never merge them without the main-account review and green checks.
- Do not merge a base branch of a stacked PR with `gh pr merge --delete-branch`. The delete closes the child PR, and GitHub then refuses both `gh pr reopen` and a base change on a closed PR. Retarget the child to `master` first. To recover, recreate the ref at the merge SHA with `git push origin <sha>:refs/heads/<branch>`, reopen, and retarget.
- Prove changes before pushing. Name the suites and exit codes in the report.
- Keep black and ruff clean on changed files; CI also runs both across the repository, so a touched legacy file must be clean too.
- Workflows under `.github/workflows/` may be edited when the change requires it; think through the consequences first. Market buy orders and standing bids are real (`cmd_market_buy_order`). Do not add new buy-side trading to agent code unless asked.
- Audit the repo when needed and keep it fully functional, configured, and free of open issues. File verified findings as issues and track them to resolution.
- Clean up unneeded local branches and temp scripts as soon as possible, but never delete without a reason; keep artifacts that live work still needs.
- Keep the issue tracker current: update, close, delete, or split issues as work changes. Prefer subagents for research and audits, and keep going without check-in questions.
- Never add Co-Authored-By or any other commit trailer unless asked. Dependabot PRs need a rebase request before merge.

## Wiki

The GitHub wiki is a separate repository and is pushed directly, without a PR. Keep it in sync with every release and update. Clone `https://github.com/killerboyyy777/Machine-Learning-mmo.wiki.git` to a local directory, commit there, and push. Keep environment-, machine-, and harness-specific details out of this public repository; put them in private agent config or memory instead.

Keep only the newest release plan page, such as `v0.19-plan.md`. Older plans bloat the wiki but stay recoverable from the wiki repository's git history. When you publish a new plan, delete the previous one in the same push.

## Comments

Do not sign your work. No `// agent-name:`, `# overlord:`, `# killerboy:`, or similar signature comments anywhere in the codebase.

Write a comment only when it helps the next agent or human understand something the code alone does not say. A comment explains why the code exists, never what it does. If the what needs explaining, rename instead of commenting.

Ask these questions before writing a comment:

- Would an agent reading this cold misunderstand the intent without it?
- Does it record a decision, a trap, or a reason, such as a WebSocket protocol quirk, tick-loop timing, an agent training constraint, or a save format version?
- Will it still be true in six months?

If any answer is no, delete the comment.

The target is zero unnecessary comments. A harmless comment is still an unnecessary comment: if it fails any test above, delete it rather than leaving it in place. `tests/test_source_policy.py` enforces the mechanical subset (signature comments and commented-out code); the rest is enforced in review.

## Proof bar

State what you ran, the exit codes, and what you did not run. In the report, give the branch, the base SHA, the head SHA, a one-line summary of each item, and the files changed.
