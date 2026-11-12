# Security

This is a single-operator game server. There is no hosted service, no user
account with a password, and no personal data beyond the world save the operator
chose to keep. The operator is trusted by design: `scores.json`, `world.json`
and the config files are theirs, and a malicious operator is not a threat model.

So there is no supported-version matrix and no coordinated-disclosure window.
Only `master` and the latest tag matter, and a fix ships when it ships.

## What is actually worth reporting

Real defects are the ones that break the boundary the design assumes:

* a way past the dashboard write gates (`/api/config`, `/api/train/start`,
  `/api/train/stop`), which refuse non-loopback peers, refuse a non-loopback
  `Origin`, and require `application/json`;
* a way to reach GM control (port 8767) from a non-loopback peer, since the GM
  handler closes every non-loopback connection regardless of the bind address;
* a way to make the server leak resources behind `TEXTMMO_MAX_CONNECTIONS`,
  which is a hard cap on concurrent players rather than a rate limit and has no
  per-IP or handshake throttle.

Use **Security > Report a vulnerability** for those. Game balance problems - a
profitable loop, a mob that cannot lose - are ordinary issues with repro steps,
not security.

## What is not a defect

The dashboard listens on `0.0.0.0` by default and reads are unauthenticated:
`/api/state` publishes player names, guild vault totals and recent deaths, and
`/api/activity/stream` publishes the command log. That is an exposure decision
the operator makes, not a vulnerability. The same goes for publishing the GM
port: the peer gate still refuses anything that is not loopback.
