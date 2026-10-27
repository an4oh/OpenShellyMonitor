# Public dashboard behind nginx with a password login

`shelly-monitor serve` has no authentication and binds 127.0.0.1. To reach it from the internet,
put nginx in front of it with TLS and this small login sidecar (Python stdlib only):

- `auth_server.py` — login page, signed session cookie (`sm_session`, HMAC-SHA256, 7 days,
  `Secure; HttpOnly; SameSite=Lax`), scrypt password hash. nginx asks it `/_auth` (204/401) via
  `auth_request`; it also serves `GET/POST /login` and `POST /logout`. Binds 127.0.0.1:18097.
- `set-password.py` — writes `~/.config/shelly-monitor/auth.env` (600) with the hash and a fresh
  cookie secret. Re-running it logs everybody out.
- `shelly-monitor-auth.service` — systemd user unit for the sidecar (hardened).
- `nginx-limit-zones.conf` — rate-limit zones (http context).
- `nginx-public.conf` (:80, ACME + redirect) and `nginx-public-ssl.conf` (:443, login gate, proxy to 8787).
- `test_auth.py` — `python3 -m pytest deploy/public/test_auth.py -q` (loopback only).

Environment: `SHELLY_AUTH_ENV` (secrets file path), `SHELLY_AUTH_PORT` (default 18097).

## Install (generic)

Replace `shelly.example.com` with your domain in both nginx files and the repository path in the unit.

1. Dashboard running on 127.0.0.1:8787 (`deploy/systemd/user/shelly-monitor-web.service`).
2. `python3 deploy/public/set-password.py`
3. `cp deploy/public/shelly-monitor-auth.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now shelly-monitor-auth`
   (for user units to run without a login session: `loginctl enable-linger $USER`)
4. `sudo cp deploy/public/nginx-limit-zones.conf /etc/nginx/conf.d/shelly-monitor-ratelimit.conf`
5. `sudo cp deploy/public/nginx-public.conf /etc/nginx/sites-available/shellymonitor-public`,
   symlink it into `sites-enabled`, `sudo mkdir -p /var/www/certbot`, `sudo nginx -t && sudo systemctl reload nginx`
6. `sudo certbot certonly --webroot -w /var/www/certbot -d shelly.example.com`
7. `sudo cp deploy/public/nginx-public-ssl.conf /etc/nginx/sites-available/shellymonitor-public-ssl`,
   symlink it, `sudo nginx -t && sudo systemctl reload nginx`
8. Check: `curl -sI https://shelly.example.com/` → `302` to `/login?next=/`.

The CSP in `nginx-public-ssl.conf` is provisional (`'unsafe-inline'`); tighten it with the sha256
hashes of the inline `<script>`/`<style>` of `src/shelly_monitor/web/index.html`. If nobody needs to
edit custom groups remotely, set `SHELLY_WEB_READONLY=1` on the dashboard.
