<p align="center">
  <img src="web/assets/martinflix-masthead.webp" alt="MartinFlix home cinema" width="900">
</p>

# MartinFlix

A home media control room with a repeatable Docker setup. See which services are running, what’s airing next in Sonarr, and which monitored films are missing in Radarr.

[Quick start](#quick-start) · [What’s included](#whats-included) · [Operations](#operations)

## Quick start

> **[Open the MartinFlix setup guide](https://descalzi.github.io/martinflix/)** to install, update, or start from scratch. Each step has a default you can keep or change, and the guide generates the exact commands for your machine.

### 1. Prepare the VM

Use a Linux VM. Open a terminal and run `git --version` and `python3 --version`. If either command is missing, install Git and Python using your Linux distribution’s package manager. The installer accepts **Python 3.9 or newer**, including 3.9.2. Use a currently supported Linux release for a new VM.

If `python3 --version` shows an older version, run `cat /etc/os-release` to identify your distribution. Install a newer Python through its package manager. On Debian or Ubuntu, `sudo apt update` followed by `sudo apt install python3` installs the version available for your release. If that version is still too old, use a newer Linux release or install a supported Python alongside the system version and run the installer with it, for example `python3.11 deploy/bootstrap/install.py --check`. [Python version support](https://devguide.python.org/versions/)

### 2. Clone, choose how to connect, and install

```sh
git clone --depth 1 https://github.com/descalzi/martinflix.git
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

Use LAN access on a trusted home network. HTTP sends credentials unencrypted, so keep these services off the public internet. Sonarr and Radarr use the same LAN address as the dashboard on ports 8989 and 7878 by default.

The installer starts Sonarr and Radarr, connects the dashboard using their generated keys, and finishes with an **Open your dashboard** section containing the URL and sign-in steps. Setup remembers the address, ports, media directory, and services you chose, so rerunning it resumes where it stopped.

**To set up media, a VPN, or download apps**, follow the [public wizard](https://descalzi.github.io/martinflix/) or add `--media-path` and `--enable-downloads` to both commands above for the default VPN-backed download setup. Use a dedicated, writable media directory on the VM. The download option asks for NordVPN’s manual OpenVPN service credentials before it starts Gluetun, Transmission, Prowlarr, and FlareSolverr. You can also add this option later to an existing first-run installation. [Media and download setup](docs/first-run.md#media-and-downloads)

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

### 4. Update later

From your checkout, pull the latest version and reapply it. The update keeps your saved settings, app data, and passwords:

```sh
git pull --ff-only
python3 deploy/bootstrap/install.py --update --check
python3 deploy/bootstrap/install.py --update
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
```

The wizard tests run inside the suite when Node.js 18 or newer is installed. To run them on their own:

```sh
node --test tests/wizard.test.js
```
