# Security

This is a single-operator game server. There is no hosted service, no user account with a password, and no personal data beyond the world save the operator chose to keep. The operator is trusted by design. The `scores.json`, `world.json`, and config files are theirs, and a malicious operator is not a threat model.

There is no supported-version matrix and no coordinated-disclosure window. Only `master` and the latest tag matter, and a fix ships when it ships.

## What is worth reporting

Real defects break a boundary the design assumes:

- A way past the dashboard write gates at `/api/config`, `/api/train/start`, and `/api/train/stop`. Those routes refuse non-loopback peers, refuse a non-loopback `Origin`, and require `application/json`.
- A way to reach GM control on port 8767 from a non-loopback peer. The GM handler closes every non-loopback connection regardless of the bind address.
- A way to make the server leak resources behind `TEXTMMO_MAX_CONNECTIONS`. That is a hard cap on concurrent players, not a rate limit, and it has no per-IP or handshake throttle.

Use **Security > Report a vulnerability** for those. Game balance problems, such as a profitable loop or a mob that cannot lose, are ordinary issues with reproduction steps, not security defects.

## What is not a defect

The dashboard listens on `0.0.0.0` by default and reads are unauthenticated. `/api/state` publishes player names, guild vault totals, and recent deaths. `/api/activity/stream` publishes the command log. That is an exposure decision the operator makes, not a vulnerability. The same goes for publishing the GM port: the peer gate still refuses anything that is not loopback.
