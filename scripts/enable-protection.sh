#!/usr/bin/env bash
# Re-apply the master branch protection ruleset (#96, spec in #132).
# Run after repo creation, transfer, or settings reset:
#     bash scripts/enable-protection.sh
# Requires: gh CLI authenticated as a repo admin, repo public or Pro.
# Notes (learned applying it):
# - Inline -f values do NOT work: the API needs real JSON objects, so the
#   payload goes through a file.
# - Status-check contexts are BARE job names ("Unit, ..."), not
#   "Workflow / Job" -- the prefixed form never matches and merges stay
#   blocked forever.
# - No pull_request rule on purpose: the owner cannot approve their own
#   PRs, so any approval requirement deadlocks solo-dev merges. Re-add
#   required_approving_review_count when a second human joins.
set -euo pipefail
PAYLOAD="$(mktemp)"
trap 'rm -f "$PAYLOAD"' EXIT
cat > "$PAYLOAD" <<'JSON'
{
  "name": "Master Branch Protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/master"]}},
  "rules": [
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "required_status_checks": [
        {"context": "Unit, grind, conductor (no server/torch needed)"},
        {"context": "Live protocol + ML env (fresh server, seed 700)"},
        {"context": "Checkpoint save/load round-trip (needs torch)"},
        {"context": "Dependency audit (pip-audit)"}
      ]}},
    {"type": "non_fast_forward"},
    {"type": "deletion"}
  ]
}
JSON
gh api --method POST repos/killerboyyy777/Machine-Learning-mmo/rulesets \
  --input "$PAYLOAD" --jq '{id, name, enforcement}'
echo "Branch protection ruleset applied."
