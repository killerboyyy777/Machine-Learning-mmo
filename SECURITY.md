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

**Do not open a public issue for security problems.** Use the repository's
**Security tab > Report a vulnerability**, which opens a private advisory that
only the maintainer can read. That is the reporting channel; there is no email
address to use instead, and none is published on the maintainer's profile.

If that button is missing, open a public issue that contains **no details** and
ask for a private channel. Do not describe the problem in the issue itself.

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
* `TEXTMMO_MAX_CONNECTIONS` (default 1000) is a **hard cap** on concurrent
  players, not a rate limit: it is checked once the WebSocket handshake has
  completed, and there is no per-IP throttle or handshake limiter. A churn or
  handshake flood is a known gap rather than a surprise; report a way to make
  the server leak resources behind that cap, not the cap itself.
* The dashboard (`http.server`, port 8766) **listens on `0.0.0.0` by default**,
  so any host that is reachable makes it reachable. Reads are unauthenticated
  by design. `/api/state` publishes live world state, including player names,
  guild vault totals and recent deaths; `/api/activity/stream` publishes the
  command log, which carries player names. That is an operator decision about
  exposure, not a vulnerability.
* The dashboard write routes (`/api/config`, `/api/train/start`,
  `/api/train/stop`) are stricter: they refuse any peer that is not loopback,
  refuse a non-loopback `Origin`, and require `application/json`. Report a way
  past those three checks.
* The GM WebSocket (port 8767) has **no authentication**. It binds
  `127.0.0.1` unless `TEXTMMO_GM_HOST` is set, and independently of the bind it
  closes every peer that is not loopback, so widening the bind does not widen
  access. Report a way to reach GM control from a non-loopback peer.
* Published containers are built by CI from a tag, not from a workstation. A
  report that an image behaves differently from the tagged source is in scope.
