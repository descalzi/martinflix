# Dashboard deployment

Run these steps on a Linux Docker host as the account that can run `docker ps` and will own the status timer. The dashboard uses host networking to reach the loopback Sonarr and Radarr ports. Keep port 8765 free. The default bind is the MartinFlix LAN address, `192.168.5.204`. Set `MARTINFLIX_LAN_ADDRESS` before running Compose on a host with a different address. HTTP Basic credentials travel unencrypted. Keep the port on your trusted home network and avoid router port forwarding.

```sh
set -eu
umask 077
secrets_dir="$HOME/.config/martinflix/dashboard-secrets"
install -d -m 0700 "$secrets_dir"
if [ ! -e "$secrets_dir/admin-password" ]; then
    (set -C; openssl rand -hex 32 > "$secrets_dir/admin-password")
fi

install -D -m 0700 deploy/dashboard/collect-status "$HOME/.local/libexec/martinflix-collect-status"
install -D -m 0644 deploy/dashboard/martinflix-dashboard-status.service "$HOME/.config/systemd/user/martinflix-dashboard-status.service"
install -D -m 0644 deploy/dashboard/martinflix-dashboard-status.timer "$HOME/.config/systemd/user/martinflix-dashboard-status.timer"
loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user start martinflix-dashboard-status.service
systemctl --user enable --now martinflix-dashboard-status.timer

export MARTINFLIX_UID="$(id -u)" MARTINFLIX_GID="$(id -g)"
docker compose -f deploy/dashboard/compose.yaml up --build -d
```

Linger keeps the user timer running after logout and starts it at boot. The collector atomically publishes names, states, and display status once a minute. The dashboard reports an unavailable snapshot after three minutes without a refresh. Check collector failures with `journalctl --user -u martinflix-dashboard-status.service` and app startup with `docker compose -f deploy/dashboard/compose.yaml logs dashboard`.

The dashboard UID and GID match the account owning the private mounts. Both mounts and the container filesystem are read-only. The container exposes the dashboard, status, and optional monitoring routes to authenticated clients. Docker access stays with the scheduled host collector.

## Optional app monitoring

Save each API key from the corresponding application's settings as one line in `sonarr-key` or `radarr-key` inside the secrets directory, with mode 0600. The app reads them from disk on each monitoring request. It sends the key only to the configured local HTTP service and declines redirects. The defaults on MartinFlix are Sonarr at 127.0.0.1:8989 and Radarr at 127.0.0.1:7878. App API keys may have broader permissions than the dashboard uses, so keep them private.

The current deployment has no backup manifests configured. The existing backup scheduler and archives remain independent. Use the [backup and recovery notes](backups.md) to interpret prototype snapshots.

## Stop or remove the dashboard

```sh
docker compose -f deploy/dashboard/compose.yaml down
systemctl --user disable --now martinflix-dashboard-status.timer
```

This preserves the private credentials and local status snapshot for a later restart.
