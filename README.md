<p align="center">
  <img src="web/assets/martinflix-masthead.webp" alt="MartinFlix home cinema" width="900">
</p>

# MartinFlix

A home media control room with a repeatable Docker setup. See which services are running, what’s airing next in Sonarr, and which monitored films are missing in Radarr.

[Quick start](#quick-start) · [What’s included](#whats-included) · [Operations](#operations)

## Quick start

### 1. Prepare the VM

Use a Linux VM. Open a terminal and run `git --version` and `python3 --version`. If either command is missing, install Git and Python using your Linux distribution’s package manager. MartinFlix needs Python 3.10 or newer.

### 2. Clone, choose how to connect, and install

```sh
git clone git@github.com:descalzi/martinflix.git
cd martinflix
```

Run one of the following sets of commands as your normal user. The check creates no stack files or containers and explains how to fix missing prerequisites. Run the install command once the check passes.

**If your browser is on the VM**, use the defaults:

```sh
python3 deploy/bootstrap/install.py --check
python3 deploy/bootstrap/install.py
```

**If your browser is on another machine**, set the VM’s LAN IPv4 address before the first install. Replace the example address with your VM’s address:

```sh
VM_IP=192.168.1.50
python3 deploy/bootstrap/install.py --check --dashboard-bind "$VM_IP"
python3 deploy/bootstrap/install.py --dashboard-bind "$VM_IP"
```

Use LAN access on a trusted home network. HTTP Basic sends credentials unencrypted, so keep the dashboard off the public internet. Sonarr and Radarr’s setup screens remain local to the VM.

The installer starts Sonarr and Radarr, connects the dashboard using their generated keys, and prints the dashboard URL and password file. Rerun with the same options to resume setup.

### 3. Open the dashboard

Open the URL printed by the installer and sign in as **admin**. The default is **http://127.0.0.1:8765** on the VM. View the generated password on the VM:

```sh
cat ~/.local/state/martinflix-first-run/dashboard-secrets/admin-password
```

| Service | Local address |
| --- | --- |
| MartinFlix | `http://127.0.0.1:8765` |
| Sonarr | `http://127.0.0.1:8989` |
| Radarr | `http://127.0.0.1:7878` |

See the [first-run guide](docs/first-run.md) for using different ports or storage.

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
