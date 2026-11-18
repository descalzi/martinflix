<p align="center">
  <img src="web/assets/martinflix-masthead.webp" alt="MartinFlix home cinema" width="900">
</p>

# MartinFlix

A home media control room with a repeatable Docker setup. See which services are running, what’s airing next in Sonarr, and which monitored films are missing in Radarr.

[Quick start](#quick-start) · [What’s included](#whats-included) · [Operations](#operations)

## Quick start

### 1. Check the VM

Use a Linux VM with Git, Python 3.10+, Docker Engine, and the Docker Compose plugin. Run these as the account that will own the stack:

```sh
python3 --version
docker info
docker compose version
```

If Docker or Compose is missing, follow the [Docker installation instructions](https://docs.docker.com/engine/install/) for your Linux distribution.

### 2. Clone and install

```sh
git clone git@github.com:descalzi/martinflix.git
cd martinflix
python3 deploy/bootstrap/install.py
```

The installer starts Sonarr and Radarr, waits for them to initialize, and connects the dashboard using the keys they generate. It prints the service URLs and dashboard password file. Rerun the same command to resume or verify the setup.

### 3. Open the dashboard

On the VM, open **http://127.0.0.1:8765** and sign in as **admin**. View the generated password locally:

```sh
cat ~/.local/state/martinflix-first-run/dashboard-secrets/admin-password
```

| Service | Local address |
| --- | --- |
| MartinFlix | `http://127.0.0.1:8765` |
| Sonarr | `http://127.0.0.1:8989` |
| Radarr | `http://127.0.0.1:7878` |

For a browser on another machine, rerun the installer with `--dashboard-bind` and the VM’s LAN IPv4 address, then use that address on port 8765. The dashboard uses HTTP Basic, which sends credentials unencrypted over the network. Keep it on a trusted LAN and off the public internet. App setup screens stay on loopback. See the [first-run guide](docs/first-run.md) for using different ports or storage.

## What’s included

- **First-run stack** starts the dashboard, Sonarr, and Radarr. Fresh libraries start empty. Media mounts, Docker status collection, Prowlarr, VPN/download clients, and Seerr are later bootstrap stages.
- **Existing-host dashboard** includes Docker status and dependency groups, filtered poster views, and a Seerr link. Use the [dashboard runbook](docs/dashboard.md) when connecting an existing stack.
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
```
