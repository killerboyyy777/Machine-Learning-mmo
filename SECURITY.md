# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| master  | :white_check_mark: |
| tags `v*` (latest) | :white_check_mark: |
| older tags | :x: (upgrade first, then report) |

Only the current `master` branch and the latest tagged release receive
security fixes.

## Reporting a Vulnerability

**Do not open a public issue for security problems.** Instead:

1. Go to the repository's **Security tab > Report a vulnerability**
   (private advisory), or
2. Contact the maintainer directly via the email on their GitHub profile.

Include:

* What you did, step by step (commands sent, rooms/NPCs involved)
* What you expected vs what happened (privilege escalation, data loss,
  remote crash, treasury/market theft, auth-token bypass, ...)
* `server.py` revision (`git rev-parse HEAD`), config files in use
  (`server_config.json`, `ml_config.json`), relevant logs
* Whether it needs other players/bots online to trigger

You will get an initial response within 7 days. If the issue is confirmed,
we will coordinate a fix and credit you in the release notes (unless you
prefer to stay anonymous).

## Disclosure Policy

* We ask for up to 90 days of coordinated disclosure before you publish
  details of a confirmed vulnerability.
* Once a fix is released, details may be published freely.
* Cheat/exploit reports that only affect game balance (not security --
  e.g. a profitable market loop) belong in **public issues** with repro
  steps, not here. When in doubt, report privately and we will route it.

## Scope Notes

* The game server trusts its local operator completely: `scores.json`,
  `world.json`, and the config files are operator-controlled. A malicious
  operator is out of scope.
* Denial-of-service via sheer connection volume is rate-limited by
  `TEXTMMO_MAX_CONNECTIONS` (default 1000), not eliminated; report bypasses of
  it here.
* The dashboard (`http.server`, port 8766) **listens on `0.0.0.0` by default**,
  so any host that is reachable makes it reachable. Reads are unauthenticated
  by design: `/api/state` and the SSE stream publish live world state, which
  includes player names, guild vault totals and recent deaths. That is an
  operator decision about exposure, not a vulnerability. The write routes
  (`/api/config`, `/api/train/start`, `/api/train/stop`) are stricter - they
  refuse any peer that is not loopback, refuse a non-loopback `Origin`, and
  require `application/json` - so report a way past those three checks here.
* The GM WebSocket (port 8767) has **no authentication**. It binds
  `127.0.0.1` unless `TEXTMMO_GM_HOST` is set, and setting it to `0.0.0.0` is
  how the compose `soak` profile reaches it inside a Docker network. Publishing
  that port to an untrusted network is operator error, not a server defect;
  report a way to reach GM control from a non-GM port here.
* Published containers are built by CI from a tag, not from a workstation. A
  report that an image behaves differently from the tagged source is in scope.
