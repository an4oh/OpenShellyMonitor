# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.8.0] - 2026-10-28

### Added
- Database backup and restore. `shelly-monitor backup [--out FILE|-] [--no-compress]`: consistent
  snapshot through the SQLite online backup API (safe while polling, WAL or `delete`), gzip by default,
  `-` writes to stdout. `shelly-monitor restore FILE [--yes]`: validates the file (SQLite, plain or gzip,
  `integrity_check`, shelly-monitor tables, schema not newer than this version), replaces the database
  atomically, removes leftover `-wal`/`-shm`, migrates older schemas; asks for confirmation and refuses
  while another process has the database open (non-empty `-wal` or no exclusive lock).
- Automatic backups: `SHELLY_BACKUP_DIR` (unset = off), `SHELLY_BACKUP_INTERVAL_HOURS` (default 24),
  `SHELLY_BACKUP_KEEP` (default 14). The poller (`run`, `serve --poll`) writes
  `shelly-monitor-YYYYmmdd-HHMMSS.db.gz` after a successful cycle when due and deletes the oldest;
  failures are warnings. The Azure template sets `SHELLY_BACKUP_DIR=/data/backups`.
- Dashboard: section *Backup del database* with download (`GET /api/backup`, also when read-only),
  restore upload (`POST /api/restore`: `X-Requested-With`, `Content-Length` required, at most
  `SHELLY_RESTORE_MAX_MB`, default 200; 403 with `SHELLY_WEB_READONLY=1`; JSON with samples before/after
  and schema version) and the state of automatic backups (`GET /api/backup/status`). With `serve --poll`
  a restore waits for the running cycle, pauses the poller and makes it reopen the database.
- `serve --poll`: the poller (same loop as `run`: aligned to `SHELLY_INTERVAL`, activity log, shared
  throttle) runs in a background thread of the dashboard process, with its own SQLite connection:
  one process, one port. Poller errors are logged and never stop the server; if the loop crashes it
  is restarted after 60 s (ERROR in the log). Configuration errors stop the command before listening.
- `GET /healthz` (never behind the login, meant as liveness probe): `{"ok", "last_ok_poll", "stale",
  "poller": "running"|"off"|"dead"}`. 503 only when `--poll` is on and the poller loop is dead (crashed,
  waiting for its restart); a cloud outage or a wrong key only sets `stale: true` (last successful
  cycle older than 5 x `SHELLY_INTERVAL`), so the container is not restarted in a loop.
- `serve` bound to an address other than 127.0.0.1/localhost without a password logs a WARNING.
- Optional built-in password login (new module `auth.py`, primitives shared with the nginx sidecar
  `deploy/public/auth_server.py`): enabled by `SHELLY_WEB_PASSWORD` (plain, hashed with scrypt at
  startup) or `SHELLY_WEB_PASSWORD_HASH` (`scrypt$N$r$p$salt$hash`). Signed cookie `sm_session`
  (HttpOnly, SameSite=Lax, `Secure` on https), lifetime `SHELLY_WEB_SESSION_DAYS` (default 7),
  bound to the password (a new password invalidates every session) and to
  `SHELLY_WEB_COOKIE_SECRET` (hex; random when unset: sessions end at every restart). `GET/POST
  /login` (own CSP by hash, `form-action 'self'`, sanitized `next`, 1 s delay on failure, 429 after
  10 failures in 10 minutes per client IP), `POST /logout` (revokes the cookie). Anonymous requests:
  pages redirect to `/login`, everything else answers 401. `SHELLY_WEB_TRUST_PROXY=1` takes the client
  IP and the scheme from `X-Forwarded-For`/`X-Forwarded-Proto`; `SHELLY_WEB_SECURE_COOKIE=1` forces
  `Secure`. `/api/config` reports `auth`; the dashboard shows an *Esci* button when it is on.
- `SHELLY_DB_JOURNAL=delete` for databases on network filesystems (SMB/NFS shares, where WAL does
  not work): rollback journal with `synchronous=NORMAL`; an existing WAL database is checkpointed and
  switched at the next read-write open. Default `wal`, unchanged.

### Changed
- SQLite busy timeout is 10 s on every connection (was 30 s, read-only connections included).
- `serve --help` and the startup log say whether the login is enabled.

### Fixed
- A client closing the connection mid-response (e.g. leaving the page) is logged at DEBUG instead of
  an ERROR with two tracebacks.

## [0.7.0] - 2026-10-27

### Added
- Activity log from Shelly Cloud (undocumented `POST /statistics/event-log`, added to the read-only
  allowlist): one extra request per home per cycle (shared throttle), events stored in table `events`
  deduplicated on `(home, cloud rowid)` (schema version 6, automatic migration). ON/OFF to the second,
  input/button and brightness events. `SHELLY_EVENT_LOG=0` disables it; a failure is logged and never
  stops the cycle. CLI `events`; API `GET /api/device/events`, `GET /api/device-events.csv`;
  *Registro attività* section in the device page.

### TODO
- Refine ON intervals in the reports with the activity log events (exact second instead of the
  sample minute); the computation is still sample based.

### Security
- Dashboard: date ranges longer than 1100 days and extreme dates (e.g. `to=9999-12-31`) answer 400
  instead of looping per day or failing with 500; `/api/export.csv` refuses more than 100000 samples (400).
- CSV exports (CLI and dashboard) prefix with `'` any text cell starting with `= + - @`, TAB or CR
  (home, room, device, group names), so spreadsheets do not run them as formulas.
- CSP without `'unsafe-inline'`: `script-src`/`style-src` allow only the sha256 of the inline blocks of
  `index.html`, computed at startup; inline `style=""` attributes moved to a CSS class.
- Errors produced by `http.server` itself (400 bad request line, 501 unknown method, ...) are JSON with
  the same security headers; 30 s socket timeout per connection.

## [0.6.0] - 2026-10-27

### Added
- Custom groups of channels, also across homes, stored only locally (`custom_groups`,
  `custom_group_members` keyed by the home's `key_id`; schema version 5, automatic migration).
  CLI `groups --create/--add/--remove/--delete`, `report --by group`; API `GET/POST /api/custom-groups`,
  `PUT /api/custom-groups/<id>/members`, `DELETE /api/custom-groups/<id>` (writes need
  `X-Requested-With: shelly-monitor` and JSON; `SHELLY_WEB_READONLY=1` makes them 403); `/api/config`.
- Filter `grp=c:<id>` / `grp=d:<home>/<group_id>` on every read endpoint and a "gruppo" selector in the
  dashboard header; "Ore per gruppo" panel; *Gruppi* page with member editor; custom groups in the
  device detail; "Ore per gruppo" table in the report PDF.
- The cloud client enforces an allowlist of read-only endpoints: shelly-monitor never writes to
  Shelly Cloud.

### Changed
- Dashboard: the Day/Month/Total toggle and the "Periodo" column are gone; tiles, bars, the channel
  table (all channels, also at 0) and "Esporta → Report CSV" show the total of the selected date range.
  `/api/report` keeps accepting `group` (default `total`); the CLI `--group` is unchanged.
- Overview: "Ore accese per canale" and "Dettaglio canali" are one table (device link, home, room,
  channel, model, ON hours with an inline bar, Wh, state, last sample); every row opens the device
  detail; the "per home" bars filter by home. `/api/report` rows carry `last_ts`.
- Database work in the dashboard is serialised: concurrent queries were several times slower
  because of GIL contention between SQLite reader threads (overview load ~7 s -> ~1.5 s on 100k samples).

## [0.5.0] - 2026-10-27

### Added
- PDF export with a built-in stdlib writer (`pdf.py`: PDF 1.4, Helvetica/Courier core fonts, WinAnsi
  with escaping, A4, zlib streams, xref; tables with repeated headers, bar charts, page numbers).
- Report PDF (`report --format pdf --out FILE`, `/api/report.pdf`): summary, hours per home and per
  room, hours per day, channel table sorted by hours, method notes. Device PDF (`/api/device.pdf`)
  and samples PDF (`export --format pdf --out FILE`). Italian labels in `pdfreport.LABELS_IT`.
- CSV: `/api/report.csv` (aggregated report) and `/api/device.csv` (transitions with duration);
  `report --out FILE`; `export --out` (alias of `-o/--output`).
- Dashboard: "Esporta" menu (report CSV, samples CSV, report PDF) and CSV/PDF buttons on device pages.

### Fixed
- Concurrent first opens of a database no longer fail with "database is locked" while switching to
  WAL; schema migrations run in one versioned transaction (`PRAGMA user_version`) and are idempotent
  (fixes "duplicate column name: type" when poller and dashboard upgraded at the same time).

## [0.4.0] - 2026-10-27

### Added
- Full parser: Gen1 `lights[]` (dimmers, RGBW2, bulbs) with power from `lights[].power` or `meters[]`
  and energy from `meters[].total`; Gen1 `rollers[]` and Gen2 `cover:N` (moving state, position);
  sensors `temperature:N`, `humidity:N`, `flood:N`, Gen1 `ext_temperature`/`ext_humidity`, H&T `tmp`.
- `samples.kind` (switch|light|rgbw|cover|sensor) and `samples.value` (position, brightness, reading);
  sensors store `is_on` NULL.
- Device registry: every channel of `get_all_lists` is stored (type, gen, kind, room, channels count,
  online), also never-sampled ones; devices absent from `all_status` are offline. Groups in
  `groups`/`group_members`.
- Gen1 online state from `cloud.connected`, falling back to a recent `_updated`.
- Dashboard *Devices* page (grouped by room, group or kind) and device detail (timeline, daily hours,
  value chart, last transitions); API `/api/devices` (enriched), `/api/groups`, `/api/device`.
- CLI `groups`; `devices` shows kind, online, room and groups (`--header`); export adds `kind`, `value`.

### Changed
- ON hours count switches and lights only; covers and sensors are excluded (cover moving time is in
  the device detail). Rows older than 0.4 (no kind) count as switches.
- Databases from 0.3 are migrated automatically (the `samples` table is rebuilt once to make `is_on`
  nullable).

## [0.3.0] - 2026-10-27

### Added
- `SHELLY_AUTH_KEYS=key1[@server];key2`: only keys in the configuration, home names are detected
  from the cloud (`/user/get_units_info` + `/user/get_settings`, `/user/get_sub_units` when the
  avatar is ambiguous), at start-up and hourly; fallback `home-N` with a warning.
- Stable home identity: `homes` table keyed by `key_id` = `sha256(auth_key)[:12]`, with `unit_id`,
  detected name and manual override. Renames relabel samples and devices in one transaction.
- `homes` command: list homes, `homes --name KEY_ID NAME` for a persistent name.
- `devices --name HOME/ID[:N]` accepts the home name or its `key_id`.
- `/api/homes` returns `key_id`, `unit_id`, `discovered_name`.

### Changed
- `SHELLY_HOMES` names and `SHELLY_HOME_NAME` (for `SHELLY_AUTH_KEY`) are fixed names that win over
  detection; with neither, a single `SHELLY_AUTH_KEY` home is named from the cloud too.
- Databases from 0.2: existing data is bound to the first configured key.
- Start-up cycle warning accounts for up to 5 requests per home in the hourly refresh cycle.

## [0.2.0] - 2026-10-27

### Added
- Multiple homes in one instance: `SHELLY_HOMES=Name=key[@server];...` (one cloud key per home);
  the single `SHELLY_AUTH_KEY` setup still works and is named by `SHELLY_HOME_NAME` (default `home`).
  A failing home is skipped without blocking the others.
- `SHELLY_REQUEST_GAP` (default 5 s, minimum 1.1 s): minimum time between two cloud requests,
  shared by all homes; start-up warning when a cycle cannot fit in `SHELLY_INTERVAL`.
- Rooms from `get_all_lists` (`devices.room`, `devices.room_id`); `home` and `room` columns in
  `report` (table/CSV/JSON), `devices` and `export`.
- `report --home NAME`, `report --by channel|room|home`, `report --all`; `devices --name [HOME/]ID[:N]`.
- `serve` command: read-only web dashboard (stdlib `http.server`, single embedded page, works offline)
  with JSON endpoints `/api/summary`, `/api/report`, `/api/daily`, `/api/devices`, `/api/homes` and
  `/api/export.csv` (`home=`/`room=` filters, `by=` on reports); home/room selectors and a
  "hours per home" panel with several homes; `SHELLY_HTTP_HOST`/`SHELLY_HTTP_PORT` or `--host`/`--port`.
- `web` service in docker-compose (localhost:8787), `shelly-monitor-web.service` and user-level systemd units.
- Gen2+ `light:N` and `rgbw:N` outputs (e.g. Plus RGBW PM) in addition to `switch:N`.
- Per-channel names: cloud ids `ID_N` (or the `channel` field) map to channel N.
- `samples.energy_wh` (cumulative `aenergy.total`); reports use the counter delta when available
  (`wh_source` = `counter` or `estimate`).

### Changed
- `samples.home`; `devices` keyed by `(home, device_id, channel)`. Databases from 0.1.x are
  migrated automatically, assigning their data to `SHELLY_HOME_NAME`.
- Report CSV columns: `period,home,device_id,name,channel,room,on_minutes,wh,wh_source,channels`;
  export CSV adds `home`, `room` and `energy_wh`.
- Default gap between cloud requests raised from 1.1 s to 5 s.
- Docker image uses `ENTRYPOINT ["shelly-monitor"]`: pass only the subcommand (`docker run IMAGE report`).

### Fixed
- Parse the real `/device/all_status` payload: device id, model (`code`) and online state
  (`cloud.connected`) are read from the device entry; `_dev_info` is only a fallback.
  Previously every device was recorded as offline and reports showed 0 minutes.
- Channels that were never ON are listed with 0 minutes in `report --group total`, in
  `report --all` (day/month) and in the dashboard; previously they were missing.
- Docker: the container user (uid 10001) owns `/data`; compose uses a named volume by default.
- Clean errors for an invalid `SHELLY_INTERVAL` (exit 2) and for `report`/`export` without a database (exit 1).
- `.env` parser: inline ` # comments` and `export` followed by any whitespace.

## [0.1.0] - 2026-10-27

### Added
- `poll` and `run` commands: one `POST /device/all_status` per cycle, Gen1 (`relays`/`meters`) and Gen2+ (`switch:N`) parsing, SQLite storage in WAL mode.
- `report` command: ON minutes and estimated Wh per device/channel, grouped by day, month or total; table, CSV and JSON output.
- `devices` command to list devices and set display names; best-effort cloud names refreshed at most hourly.
- `export` command for raw samples as CSV.
- Dockerfile, docker-compose and systemd unit.
