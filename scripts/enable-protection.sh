#!/usr/bin/env bash
# Re-apply the master branch protection ruleset (#96, spec in #132).
# Run after repo creation, transfer, or settings reset:
#     bash scripts/enable-protection.sh
# Requires: gh CLI authenticated as a repo admin, repo public or Pro.
set -euo pipefail
gh api --method POST repos/killerboyyy777/Machine-Learning-mmo/rulesets \
  -f name="Master Branch Protection" \
  -f target="branch" \
  -f enforcement="active" \
  -f conditions='{"ref_name":{"include":["refs/heads/master"]}}' \
  -f rules='[{"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":true,"required_status_checks":[{"context":"Tests / Unit, grind, conductor (no server/torch needed)"},{"context":"Tests / Live protocol + ML env (fresh server, seed 700)"},{"context":"Tests / Checkpoint save/load round-trip (needs torch)"},{"context":"Tests / Dependency audit (pip-audit)"}]}},{"type":"pull_request","parameters":{"required_approving_review_count":1,"dismiss_stale_reviews_on_push":true,"require_code_owner_review":false,"require_last_push_approval":false,"required_review_thread_resolution":true}},{"type":"non_fast_forward"},{"type":"deletion"}]'
echo "Branch protection ruleset applied."
