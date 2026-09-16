#!/usr/bin/env bash
# Re-apply the master branch protection ruleset (#96, spec in #132).
# Run after repo creation, transfer, or settings reset:
#     bash scripts/enable-protection.sh
# Requires: gh CLI authenticated as a repo admin, repo public or Pro.
# (Inline -f values do NOT work here: the API needs real JSON objects,
# so the payload goes through a file.)
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
        {"context": "Tests / Unit, grind, conductor (no server/torch needed)"},
        {"context": "Tests / Live protocol + ML env (fresh server, seed 700)"},
        {"context": "Tests / Checkpoint save/load round-trip (needs torch)"},
        {"context": "Tests / Dependency audit (pip-audit)"}
      ]}},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 1,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true}},
    {"type": "non_fast_forward"},
    {"type": "deletion"}
  ]
}
JSON
gh api --method POST repos/killerboyyy777/Machine-Learning-mmo/rulesets \
  --input "$PAYLOAD" --jq '{id, name, enforcement}'
echo "Branch protection ruleset applied."
