# martinflix cutover runbook

Run these on **martinflix**, from `/mnt/martinflix/Data/Syncthing/Docker`.
Nothing here has been run yet. Stop at the first step that fails — each stack is
independent, so a failure leaves the rest of the box on the old scripts.

    cd /mnt/martinflix/Data/Syncthing/Docker

> **While migrating, do not run `restart_all.sh` or `restart_apps.sh`.** They
> `docker rm` containers by name and would fight with compose. They stay valid
> as a rollback path until every stack is migrated.

---

## 0. Snapshot for rollback  (read-only, do this first)

    mkdir -p state/martinflix
    for c in $(docker ps -a --format '{{.Names}}'); do   # -a: include stopped ones
      printf '%-22s %s\n' "$c" "$(docker inspect $c --format '{{.Image}}')"
    done | tee state/martinflix/pre-migration-digests.txt

Confirm the tool sees the host correctly — this changes nothing:

    ./stackctl status
    ./stackctl update --all --dry-run

---

## 1. Optional: rotate bookorbit secrets first

Doing this *before* the cutover means one container recreation instead of two.
Everyone logged into bookorbit will need to log in again.

    nano ~/.config/docker-stacks/martinflix.env
    # replace both `change-this-to-a-long-random-secret` values with output of:
    openssl rand -hex 32

Leave `POSTGRES_PASSWORD` alone — the running database already has it baked in;
changing it needs an `ALTER USER` inside `bookorbit-db` first.

NordVPN credentials need new ones generated in their dashboard. Until then they
remain the pair that was sitting in cleartext on the share.

---

## 2. core — tailscale, portainer

**Do this from the LAN, not over tailscale SSH** — the tailscale container is
recreated. Its state is a bind mount with `TS_AUTH_ONCE=true`, so it
re-authenticates from disk, but don't be attached through the thing you're
restarting.

    ./stackctl up core --adopt

Verify:

    docker exec tailscale tailscale status | head -3
    curl -sk -o /dev/null -w 'portainer %{http_code}\n' https://localhost:9443/

Rollback:

    docker compose -p martinflix-core -f hosts/martinflix/10-core.yml down
    ./tailscale.sh && ./portainer.sh

---

## 3. web — homepage, seerr, tautulli

    ./stackctl up web --adopt

Verify:

    for p in 3000 5055 8181; do
      curl -s -o /dev/null -w "$p %{http_code}\n" --max-time 8 http://localhost:$p/
    done

Check homepage still renders its widgets — it reads `/app/config`, and this is
the service most likely to expose a wrong-config-path mistake.

Rollback:

    docker compose -p martinflix-web -f hosts/martinflix/50-web.yml down
    ./homepage.sh && ./seerr.sh && ./tautulli.sh

---

## 4. books — bookorbit + postgres

Postgres data is a bind mount (`/var/lib/bookorbit/postgres`), so it survives
recreation. This stack picks up any secrets you rotated in step 1.

    ./stackctl up books --adopt

Verify:

    docker inspect bookorbit-db  --format 'db  {{.State.Health.Status}}'
    docker inspect bookorbit-app --format 'app {{.State.Health.Status}}'
    curl -s -o /dev/null -w 'bookorbit %{http_code}\n' http://localhost:3001/api/v1/health

Rollback:

    docker compose -p martinflix-books -f hosts/martinflix/40-books.yml down
    docker compose -f bookorbit.yml up -d

---

## 5. plex

    ./stackctl up plex --adopt

Verify — and check a client can actually play something, not just that the port
answers:

    curl -s -o /dev/null -w 'plex %{http_code}\n' http://localhost:32400/identity

Rollback:

    docker compose -p martinflix-plex -f hosts/martinflix/20-plex.yml down
    ./plex.sh

---

## 6. arr — the risky one

Seven containers, and the network namespace changes from `container:nordvpn` to
`service:nordvpn`. All four VPN-attached services are recreated together.

    ./stackctl up arr --adopt

### Verify the tunnel BEFORE anything downloads

This is the check the old scripts never did. The two addresses must differ:

    echo "host public IP : $(curl -s --max-time 10 ifconfig.me)"
    echo "vpn public IP  : $(docker exec nordvpn cat /tmp/gluetun/ip)"

    docker inspect nordvpn --format 'gluetun health: {{.State.Health.Status}}'

If the VPN IP is empty, equal to the host IP, or gluetun isn't `healthy`, stop
and roll back — transmission is sharing your real address.

Then the services:

    for p in 9091 9696 6767 8989 7878; do
      curl -s -o /dev/null -w "$p %{http_code}\n" --max-time 8 http://localhost:$p/
    done

9091 transmission · 9696 prowlarr · 6767 bazarr (all via the VPN container)
8989 sonarr · 7878 radarr (bridge, direct)

Also confirm prowlarr can still reach flaresolverr — they share the VPN
namespace, so it's on `127.0.0.1:8191` from prowlarr's point of view.

Rollback:

    docker compose -p martinflix-arr -f hosts/martinflix/30-arr.yml down
    ./nordvpn-gluetun.sh && ./transmission.sh && ./flaresolverr.sh \
      && ./prowlarr.sh && ./bazarr.sh && ./sonarr.sh && ./radarr.sh

---

## 7. After everything is green

    ./stackctl status          # all five stacks, all containers
    ./stackctl update --all    # first real update run

Then the tidy-up, in your own time:

    mkdir -p archive
    git -C . 2>/dev/null || true
    mv decommed overseerr.sh readarr.sh ollama.sh openweb.sh home_assistant \
       nordvpn.sh nordvpn-dockerfile.sh stacks.yml shelfarr.yml archive/
    mv bazarr.sh bookshelf.sh flaresolverr.sh homepage.sh plex.sh portainer.sh \
       prowlarr.sh radarr.sh seerr.sh sonarr.sh syncthing.sh tailscale.sh \
       tautulli.sh transmission.sh nordvpn-gluetun.sh upgrade_container.sh \
       restart_all.sh restart_apps.sh restart_chunkyboy.sh bookorbit.yml \
       traefik.yml archive/

Keep `archive/` for a month before deleting it.

Permissions on the share are currently `777`. Once the old cleartext copies are
in `archive/`, tighten:

    chmod 755 . hosts hosts/* lib
    chmod 644 hosts/*/*.yml hosts/*/stacks.conf lib/*.sh
    chmod 755 stackctl

And the old `.env` in this directory still holds the bookorbit secrets — delete
it once `books` is migrated and confirmed working.

---

## Notes

- `bookshelf` is flagged `manual` and excluded from `up --all`. Start it with
  `./stackctl up bookshelf` when you want it back on 8787.
- `stacks` and `shelfarr` are not migrated — both have been stopped since ~July.
  gluetun's forwards for 7788/5056 are commented out in `30-arr.yml`; uncomment
  if you revive either.
- Per-stack logs from the last run: `state/martinflix/logs/<stack>.log`
