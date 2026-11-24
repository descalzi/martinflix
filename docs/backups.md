# Backup and recovery

## Backups on an installer-built stack

The controller backs up the stack every night at 01:00, local time, and whenever you press **Back up now** on the Backups page (it asks for your PIN). Choose where with `--backup-dir`, ideally another disk or a mounted NAS share such as `/mnt/martinflix/Backups`; it must already exist, because setup never creates a mount point. Without it, backups go to `~/.local/share/martinflix/backups` on the VM itself, which a failed disk would take with it. Each machine writes into its own subfolder named after the machine, so several machines can share one folder without mixing backups.

Each backup is a folder named by its start time, with one archive per app and a `manifest.json` listing each archive's size and SHA-256 checksum:

| Archive | Contents |
|---|---|
| `sonarr.zip`, `radarr.zip`, `prowlarr.zip` | The app's own backup (database and settings), made through its API so the database is consistent while the app runs. Sonarr's and Radarr's databases hold every series and film they track, including ones not released yet, with their monitoring, quality profiles, tags and history |
| `bazarr.zip` | Bazarr's own backup |
| `transmission.tar.gz` | `settings.json` and the torrent list, so seeding can resume |
| `plex.tar.gz` | `Preferences.xml` and Plex's library and blobs databases (libraries, watch history, watched status), copied with Plex's own SQLite while Plex runs. Plex's caches and downloaded artwork are left out; Plex rebuilds them |
| `martinflix.tar.gz` | Setup's options, the dashboard password hash, app keys, VPN credentials and the PIN hash. **Private**: readable only by your account |

A backup is written to a hidden `.partial` folder and renamed when complete, so a backup folder is either whole or absent. If one app can't be backed up, the others are still saved and the Backups page marks the backup **Incomplete** with the reason. Old backups are thinned automatically: the last 14 are kept, then one per week for 8 weeks. If the VM was off at 01:00, a backup runs about ten minutes after the controller starts.

A checksum proves each archive matches what was saved, not that it restores; a restore rehearsal does.

## Restoring

**From the dashboard.** On the Backups page, choose **Restore** beside a backup and enter your PIN. The controller can only read the state folder, so it runs setup to do the restore, in a container of its own, the same way as an update; the page shows setup's output as it goes. Setup:

1. checks every archive against the backup's checksums, and that none would write outside the state folder, before it changes anything;
2. stops the apps. The dashboard and controller keep running, so the page shows the restore as it goes, with the progress of any large archive;
3. puts each app's data back: Sonarr's, Radarr's and Prowlarr's database and `config.xml`, Bazarr's database and `config.yaml`, Transmission's settings and torrent list, Plex's `Preferences.xml` and databases, and MartinFlix's options, keys, VPN credentials, dashboard password and PIN;
4. starts the apps on the restored data and reconnects them, as an update does. The dashboard restarts last, and the page reconnects by itself.

Everything the restore replaces is moved, not deleted, into a `before-restore-<date>` folder in the state directory. Only the newest such folder is kept. Media files are never part of a backup, so they stay as they are. Options that describe the machine rather than the stack (its address and ports, the media and backup folders, the release channel) stay as they are now; everything else comes from the backup, including which apps are installed. The PIN and dashboard password become the ones from the backup.

**From a terminal**, add `--restore` with the backup's folder to setup. On the same machine:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:edge update --restore /mnt/martinflix/Backups/martinflix/2026-11-22_010000
```

**Onto a new machine**, the same option with `install` rebuilds the whole stack from a backup in one go, with no claim code, PIN or credentials to enter, because they come from the backup. Pass the media folder as for any new install; backups carry on into the folder the backup came from unless you pass `--backup-dir`:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:edge install --media-path /mnt/martinflix --restore /mnt/martinflix/Backups/martinflix/2026-11-22_010000
```

Add `--check` to either to check the backup and the plan without changing anything. In a terminal setup asks you to type `RESTORE` before it stops anything.

## Moving an existing stack onto MartinFlix

A media stack setup didn't install, such as the reference host's own Compose stacks, moves over with an export and a restore.

**1. Export**, on the machine running the old stack. The old apps keep running; nothing in them changes. The export finds them by their container names (`sonarr`, `radarr`, `prowlarr`, `bazarr`, `transmission-daemon`, `plex`; `--container app=name` for another name), asks each for a fresh backup, copies Plex's databases with Plex's own SQLite along with its artwork, video previews and agent data, and keeps Transmission's preferences but not its torrents, so let downloads finish first. It writes a normal backup into a `<host>-export` folder in the backup folder:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:edge export --backup-dir /mnt/martinflix/Backups
```

Add `--check` first to see what it found: each app, where the old stack keeps media, which folder Plex sees it in, and any app on another image tag. An app on another tag of the same image, such as Radarr's `nightly`, stays on that tag after the move, because its database may be newer than the latest release understands.

**2. Restore** the export with `--restore` as for any backup. On top of a usual restore, setup then adopts the old stack once:

- Sonarr and Radarr: every series and film under the old media folder moves to the same folder under this installation's `/data`, without moving any files. The old root folders are removed, and the new ones added.
- The old Transmission download clients and remote path mappings are removed in favour of this installation's own; Prowlarr's links to Sonarr and Radarr point at this installation's apps, and Bazarr syncs the new folders straight away.
- Plex also sees the media at the old stack's folder, read-only, so its libraries, watch history and artwork need no changes. Sonarr's and Radarr's Plex connections translate their paths for it.
- Every default setup would add to a new installation (indexers, Bazarr's providers, Plex's libraries) is skipped: the old stack has its own.
- The apps keep their logins until you choose your dashboard password, which then becomes theirs.

**Rehearse** on another machine first, with `update --restore <export> --keep-plex-identity` there. That keeps the rehearsal machine's own Plex server identity and sign-in, so it doesn't pose as the old server on your Plex account while that keeps running. Point the rehearsal machine at a media folder of its own, not the old stack's: two Sonarrs managing one library would fight over it. Sonarr and Radarr will report its files as missing there, which is expected.

**Switch over** on the old machine: stop the old app and Plex stacks, then install with `--restore`, the media folder the old stack used and your usual options. The old stack's own folders stay as they were, so it can be started again if needed.

## The prototype for the reference host

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
