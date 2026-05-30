# Deploy / rebuild guide

Everything the running box needs that lives **outside the application code**, so
a rebuild is reproducible. One-shot:

```bash
sudo bash deploy/install.sh
# then copy the data dir (gitignored): rsync -av OLDHOST:/var/www/horseracing/data/ ./data/
```

## What's in here

| File | Installs to | Purpose |
|---|---|---|
| `racing.service` | `/etc/systemd/system/` | the app unit (port 8006). Ordered `After=redis-server`. |
| `nginx-horseracing.conf` | `/etc/nginx/sites-available/horseracing` | TLS vhost → `127.0.0.1:8006` with WebSocket upgrade for Socket.IO |
| `schedules.seed.json` | `data/schedules.json` (if absent) | the 6 cron schedules incl. daily `prepare_upcoming` |
| `../requirements.txt` | system python | runtime deps (web + Socket.IO/Redis + xgboost/sklearn/bs4) |

## Critical gotchas

- **Deps must be SYSTEM-WIDE.** The unit has `ProtectHome=true`, so packages in
  `~/.local` are invisible to it. Install with `sudo /usr/bin/python3 -m pip
  install -r requirements.txt`.
- **Pinned `h11==0.14.0` / `wsproto==1.2.0`** — newer wsproto forces h11≥0.16
  which breaks httpcore/httpx. Don't `pip install -U` blindly.
- **Redis must be running** (Socket.IO fan-out + status layer). `enabled` at
  boot and the unit is ordered after it.
- **Port 8006** (not the documented 8005, which is held by an unrelated service).
- **TLS cert** is the shared `lvoyage.aero` multi-domain cert (includes
  `horseracing.privatedns.org` as a SAN); managed by Certbot, not this repo.
- **`data/` is gitignored** (DB + caches) — copy it separately or re-scrape.

## Verify after deploy

```bash
systemctl is-enabled racing nginx redis-server      # all -> enabled
curl -s http://127.0.0.1:8006/api/health             # app up
curl -sI https://horseracing.privatedns.org/ -o /dev/null -w '%{http_code}\n'
```
