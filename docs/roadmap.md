# Roadmap and migration boundary

## Available now

- One-command [first-run bootstrap](first-run.md) for Sonarr, Radarr, and dashboard monitoring, verified in a disposable Compose project with empty storage.

- Local read-only view of expected container names and Docker-reported state.
- Live Sonarr/Radarr monitored lists via privately mounted API keys on MartinFlix.
- Link to existing Seerr and a view of snapshots produced by the prototype backup command.
- Copied Compose/stackctl configuration, [live inventory](live-inventory.md), and legacy runbook. Effective cutover settings still need validation.
- Partial app-archive snapshot and checksum verification.

## Before migration

Inventory actual running containers, ownership, image versions, mounts, networks, secrets, host services and scheduled jobs. Compare them to `deploy/` and record discrepancies. Confirm media paths and storage, VPN fail-closed behaviour and host prerequisites. Audit copied definitions for credentials and portability. Create and test app-consistent backups and recovery, including PostgreSQL and Seerr. Validate deployment on an isolated host and prepare a rollback plan. Only then schedule an explicit cutover. The old runbook's statements are historical and its commands use the old directory.

## Next capabilities

1. Extend the first-run bootstrap with media mounts, project-scoped Docker status, and verified app integrations. Run a fresh-host VM trial.
2. Real recovery workflows, off-host encrypted backups, retention, scheduling, visibility, and tested restores.
3. Authenticated admin and household roles, using an appropriate identity provider; protect any network-facing interface.
4. Restricted, auditable service operations with clear progress and cancellation; preserve VPN and dependency safety. Do not expose a general shell or Docker socket to the browser.
5. Integrate Seerr requests and library views with role-aware access. Add monitoring for mounts, VPN connectivity and application health; do not infer VPN safety from container state.

Keep existing services and cron unchanged until the replacement is verified and the migration is deliberately performed.
