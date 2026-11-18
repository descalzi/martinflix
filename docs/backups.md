# Backup and recovery

The prototype captures the latest *existing* app-generated archive in each of the configured Sonarr, Radarr, Prowlarr, Bazarr and Tautulli backup directories, if found. It does not ask the apps to create a new backup. The manifest records source modification times, sizes, SHA-256 checksums and missing sources. An incomplete snapshot returns a non-zero exit code. Archives can contain API keys and credentials: protect both the destination and any copies.

Create a snapshot with `python3 backup.py create`; use `--destination /secure/path` to choose another location. The default is `~/.local/share/martinflix/backups`. Verify with `python3 backup.py verify /path/to/snapshot`. A manual dashboard run reads the default location or `MARTINFLIX_BACKUP_DIR`. The container deployment currently has no backup manifests mounted.

This is a **partial capture**, not disaster recovery. It excludes Seerr, BookOrbit and PostgreSQL, Plex, VPN and Tailscale state, Portainer, Transmission, host configuration, the media library, secrets and other data. Format and location of each application's archives, freshness, permissions, consistency, restore steps, retention, off-host copies and recovery time remain to be validated. A checksum verifies copied bytes only; it does not establish that a backup is fresh, internally valid or restorable. No pruning or scheduling is implemented.

Before relying on any backup, restore it into an isolated test instance and verify the app and its data. Document dependencies and credentials separately; avoid overwriting production data. For a complete system plan, inventory the live bind mounts and named volumes, choose app-consistent backups (especially PostgreSQL and SQLite), define storage and retention, and perform a full restore rehearsal on a new host. Do not retire the existing cron job until the replacement has passed that rehearsal.

## Recovery rehearsal on 18 November 2026

A private temporary capture passed SHA-256 verification for all five archived apps. Each ZIP passed CRC validation and the primary SQLite database extracted into temporary storage passed `PRAGMA integrity_check`. Sonarr, Radarr and Prowlarr archives were dated 13 November, Bazarr 15 November, and Tautulli 18 November. Temporary material was removed. Sonarr and Radarr also booted in separate temporary containers using the live image IDs, only the restored configuration and database, and `--network=none`. The restored library endpoints returned 54 series and 300 films.

A live BookOrbit logical dump restored into a separate network-disabled PostgreSQL container with pgvector 0.8.2. Queries confirmed 165 public tables and 1,836 rows. Recovery of the BookOrbit application and its filesystem data still needs an end-to-end trial.

A Seerr online SQLite backup plus the matching settings booted in a temporary container with networking disabled. The web root returned HTTP 307 and SQLite validation confirmed one user and 28 requests after boot. The status endpoint timed out in the offline trial. Integrations and authenticated browsing remain to be tested in a separate fixture.

The legacy cron uses a rolling `rsync --delete` mirror on the same NFS share. The script copies Seerr’s live files and omits BookOrbit. See the [live inventory](live-inventory.md) for the observed coverage.

## Next recovery checks

1. Find evidence of the most recent successful cron run. Identify a separately managed encrypted destination and measure how much data must be restored.
2. Extend the BookOrbit database rehearsal with a private recovery copy of app data. Boot BookOrbit against the recovered database and a small media fixture, then open representative book records. Keep app and database on an isolated network using separate names, credentials and storage.
3. Add Seerr’s verified SQLite online backup and matching settings to a repeatable capture workflow. Rehearse authenticated browsing and integrations using test identities and fixtures before cutover.
4. Rehearse the remaining services independently, then exercise the full stack on an isolated host. Use test downloads to prove the Gluetun dependants lose egress when the tunnel is down and regain it only through the tunnel. Verify media mount availability before starting anything that can write. Record elapsed recovery time, the age of restored data, and rollback steps before scheduling cutover.
