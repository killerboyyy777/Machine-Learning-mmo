# Scout's Journal

## Cleared Areas
- 2026-03-31: Party Management (`cmd_party_accept`, `cmd_party_leave`, `_leave_party_on_disconnect`, party invites/relocation). Verified leadership reassignment bug in `cmd_party_accept` when leader accepts new party invite and fixed with regression test.

## Recurring Bug Families
- Incomplete state cleanup during multi-step transitions: `cmd_party_accept` cleaned up member set and relocation but omitted leader reassignment when the leaving player was the leader.

## Rejected Hunches & Non-Bugs
- Commission escrow calculation in `cmd_commission_fill`: checked `min(escrow, eff_gold)` and `tax_treasury` sink, logic holds correctly as `eff_gold` is derived from `reward_gold * mult` (`mult <= 1.0`).
