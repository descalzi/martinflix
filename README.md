<p align="center">
  <img src="web/assets/martinflix-masthead.webp" alt="MartinFlix home cinema" width="900">
</p>

# MartinFlix

A home media control room with a repeatable Docker setup. See which services are running, what’s airing next in Sonarr, and which monitored films are missing in Radarr.

[Quick start](#quick-start) · [What’s included](#whats-included) · [Operations](#operations)

## Quick start

> **[Open the MartinFlix setup guide](https://descalzi.github.io/martinflix/)** to install, update, or start from scratch. Each step has a default you can keep or change, and the guide generates the exact commands for your machine.

### 1. Prepare the VM

Use a Linux VM with [Docker Engine and the Compose plugin](https://docs.docker.com/engine/install/), and check that `docker info` works as your normal user. That’s all: MartinFlix runs entirely in Docker, so there’s nothing to clone and no Python to install. Docker downloads MartinFlix itself.

### 2. Choose how to connect, and install

Run one of the following as your normal user, not with sudo. The check creates no stack files or containers and explains how to fix missing prerequisites. Run the install once the check passes.

Every installation needs a media directory: an existing, writable folder for your series, films, and downloads, such as a mounted NAS share. Replace `/mnt/media` with yours.

**If your browser is on the VM**, use the defaults:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:stable install --media-path /mnt/media --check
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:stable install --media-path /mnt/media
```

**If your browser is on another machine**, add the VM’s LAN IPv4 address. Replace the example address with your VM’s address:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:stable install --media-path /mnt/media --dashboard-bind 192.168.1.50
```

Setup runs from the MartinFlix image in two short-lived containers: the first works out which of your folders setup needs, and the second runs the installer with exactly those folders, at the same paths, on the VM’s network, as your account. `:stable` follows released versions; use `:edge` in place of it to test every change as it lands.

Use LAN access on a trusted home network. HTTP sends credentials unencrypted, so keep these services off the public internet. Sonarr and Radarr use the same LAN address as the dashboard on ports 8989 and 7878 by default.

The installer starts Sonarr and Radarr, connects the dashboard using their generated keys, and finishes with an **Open your dashboard** section containing the URL and sign-in steps. Setup remembers the address, ports, media directory, and services you chose, so rerunning it resumes where it stopped.

**To set up a VPN or download apps**, follow the [public wizard](https://descalzi.github.io/martinflix/) or add `--enable-downloads` to both commands above for the default VPN-backed download setup. Use a dedicated, writable media directory on the VM. The download option asks for NordVPN’s manual OpenVPN service credentials before it starts Gluetun, Transmission, Prowlarr, and FlareSolverr. You can also add this option later to an existing first-run installation. [Media and download setup](docs/first-run.md#media-and-downloads)

### 3. Open the dashboard

Open the URL printed by the installer and sign in as **admin**. The default is **http://127.0.0.1:8765** on the VM. On first sign-in, run the password command printed by the installer. On a new install with default options, it is:

```sh
cat ~/.local/state/martinflix/dashboard-secrets/admin-password
```

Sign in with it, choose a password of at least eight characters, then sign in again with your new password. Setting or changing the dashboard password also updates the passwords used by installer-managed Sonarr and Radarr.

| Service | Default address on the VM |
| --- | --- |
| MartinFlix | `http://127.0.0.1:8765` |
| Sonarr | `http://127.0.0.1:8989` |
| Radarr | `http://127.0.0.1:7878` |

See the [first-run guide](docs/first-run.md) for using different ports or storage.

### 4. Updates and settings

MartinFlix updates itself. The dashboard checks for a new version every six hours and shows it on the Overview; open **Settings** to see what changed and press **Update now**, with your PIN. The same page changes Discover, Plex, the backup folder, the VPN exit and credentials, and the release channel, without a terminal.

To add apps or change the address or ports, rerun setup with `update` in place of `install`, adding only the options that change:

```sh
docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \
  ghcr.io/descalzi/martinflix:stable update --enable-bazarr
```

Add `--state-dir` if you installed to a custom location. To start fresh with the same state directory, see [Start from scratch](docs/first-run.md#start-from-scratch).

## What’s included

- **First-run stack** starts the dashboard, Sonarr, and Radarr, with live health checks for both apps. Fresh libraries start empty. The installer adds a MartinFlix HD quality profile and can also set up media folders, Gluetun, Transmission, Prowlarr, FlareSolverr, and Bazarr. Every app signs in with `admin` and your dashboard password, and changing the password in the dashboard updates them all. Full Docker status collection is a later stage.
- **Dashboard** opens on what’s coming next: each monitored series’ next episode and each film’s next release, under a status strip that names any problem. **Add** searches for series and films through Sonarr and Radarr and adds them with the MartinFlix profile, so no separate request app is needed.
- **Existing-host dashboard** adds Docker status and dependency groups. Use the [dashboard runbook](docs/dashboard.md) when connecting an existing stack.
- **Recovery tools** capture and verify selected app-generated archives. The [recovery notes](docs/backups.md) cover scope and restore rehearsals.

## Operations

| Guide | Use it for |
| --- | --- |
| [First-run setup](docs/first-run.md) | Installer options and isolated trials |
| [Dashboard deployment](docs/dashboard.md) | Existing-host setup, private API keys, and shutdown |
| [Backup and recovery](docs/backups.md) | Snapshot commands, coverage, and restore checks |
| [Live inventory](docs/live-inventory.md) | The reference host’s layout and service dependencies |
| [Roadmap](docs/roadmap.md) | Bootstrap, recovery, and migration milestones |

The legacy Compose definitions and `deploy/stackctl` are migration references. Review the [cutover plan](docs/legacy-cutover.md) before using them against a running stack.

## Verify locally

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py backup.py deploy/bootstrap/install.py
docker build -t ghcr.io/descalzi/martinflix:dev .
```

GitHub builds and publishes the image on every push to `main` (the edge channel) and every `v*` tag (the stable channel). A build from your own machine is version `dev`, which checks for no updates.

The wizard tests run inside the suite when Node.js 18 or newer is installed. To run them on their own:

```sh
node --test tests/wizard.test.js
```
