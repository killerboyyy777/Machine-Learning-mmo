## What
<!-- One paragraph: what changes and why. -->

## Verification
<!-- Paste the relevant suite output. Unit/grind run offline; live/env need a
fresh server (`TEXTMMO_GM_SEED=700 python server.py`, `scores.json` = `{}`). -->
- [ ] `python tests/test_server_unit.py` → ALL_OK
- [ ] `python tests/test_quest_grind.py` → ALL_GRIND_OK
- [ ] Live suites (if protocol/server touched): `test_live.py`, `test_env.py`
- [ ] Docs updated if player-visible behavior changed (README protocol/commands,
      CHANGELOG Unreleased; TODO only gains still-open items)

## Balance / economy notes
<!-- If this touches rewards, costs, drops, or spawn rules: expected effect on
gold flow, difficulty curve, or agent training. -->
