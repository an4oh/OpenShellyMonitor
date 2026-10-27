# OpenShellyMonitor

OpenShellyMonitor polls Shelly Cloud once a minute and tells you how long each of your Shelly
devices was ON.

- Polls [Shelly Cloud](https://shelly-api-docs.shelly.cloud/cloud-control-api/) every minute for all
  devices of your account.
- Stores ON/OFF state and power (when the device reports it) in a local SQLite file.
- Reports ON time per day, month and device/channel, plus an activity log with the exact
  timestamps recorded by the cloud.
- Web dashboard, CSV and PDF export, several homes in one database.

Typical use: splitting the running time of a shared boiler, heat pump or pump between two flats.

The package and the command are called `shelly-monitor`. It only reads from Shelly Cloud: it never
switches, renames or reconfigures a device.

## Requirements

- Python >= 3.11. No runtime dependencies (standard library only).
- Or Docker with Docker Compose.
- A Shelly Cloud account with your devices in it.

## Get your Shelly Cloud key

1. Open the Shelly web app: <https://control.shelly.cloud>.
2. Go to **Settings > Home tab > Access & permissions > Cloud key > Get key**.
3. Copy the **key** and the **server** shown on the page. The server looks like
   `shelly-XX-eu.shelly.cloud`.

A key belongs to one home of your account. If you have several homes, switch to each home in the
app and get its key.

Keep the key secret: anyone who has it can control your devices.

## Configure

Settings are environment variables. The command also reads a `.env` file from the current
directory (`--env-file PATH` for another one). Real environment variables win over `.env`.

```sh
cp .env.example .env
chmod 600 .env
```

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `SHELLY_SERVER` | - | yes, unless every key has `@server` | Cloud server, e.g. `shelly-XX-eu.shelly.cloud` |
| `SHELLY_AUTH_KEYS` | - | one of the three key variables | Keys, one per home: `key1[@server];key2[@server]`. Home names are detected from the cloud |
| `SHELLY_HOMES` | - | one of the three key variables | Keys with a name you choose: `Name=key[@server];Other=key2`. Can be combined with `SHELLY_AUTH_KEYS` |
| `SHELLY_AUTH_KEY` | - | one of the three key variables | A single key (older setups) |
| `SHELLY_HOME_NAME` | `home` | no | Fixed name for `SHELLY_AUTH_KEY`; also the home name of data from versions before 0.3 |
| `SHELLY_INTERVAL` | `60` | no | Seconds between polls (integer, min 5) |
| `SHELLY_REQUEST_GAP` | `5` | no | Minimum seconds between two cloud requests, all homes together (min 1.1) |
| `SHELLY_EVENT_LOG` | `1` | no | Read the cloud activity log every cycle (one more request per home). `0` disables it |
| `SHELLY_DB` | `./shelly-monitor.db` | no | SQLite file |
| `SHELLY_LOG_LEVEL` | `INFO` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Logs go to stderr |
| `SHELLY_HTTP_HOST` | `127.0.0.1` | no | Dashboard bind address (`serve --host` wins) |
| `SHELLY_HTTP_PORT` | `8787` | no | Dashboard port (`serve --port` wins) |
| `SHELLY_WEB_READONLY` | `0` | no | `1` makes the dashboard refuse every write (custom groups become CLI-only) |

Example `.env` for two homes:

```sh
SHELLY_SERVER=shelly-XX-eu.shelly.cloud
# Home A: name detected from the cloud
SHELLY_AUTH_KEYS=KEY_OF_HOME_A
# Home B: fixed name, different server
SHELLY_HOMES=Home B=KEY_OF_HOME_B@shelly-YY-eu.shelly.cloud
SHELLY_INTERVAL=60
SHELLY_REQUEST_GAP=5
SHELLY_DB=./shelly-monitor.db
```

Each key only returns the devices of its own home, so list one key per home.
The cloud rate limit is per account, so all homes share one throttle (`SHELLY_REQUEST_GAP`).

Home names are detected from the cloud. To see or change them:

```sh
shelly-monitor homes                              # key_id, name, unit_id, server, channels, last sample
shelly-monitor homes --name 3f9a1c2b7d4e "Home A" # KEY_ID or current name; wins over detection
```

`key_id` is the first 12 hex characters of the SHA-256 of the key. The key itself is never stored.

## Run

Pick one of the three ways below. All of them run the poller unattended (`shelly-monitor run`),
which polls every `SHELLY_INTERVAL` seconds and stops cleanly on SIGTERM.

### a) venv or pipx

With pipx:

```sh
pipx install git+https://github.com/an4oh/OpenShellyMonitor.git
mkdir -p ~/shelly-monitor && cd ~/shelly-monitor
# create .env here (see Configure)
shelly-monitor poll     # one cycle, to check the key; exit code 1 if a home was skipped
shelly-monitor run      # poll forever (Ctrl+C to stop)
```

With a venv, from a clone:

```sh
git clone https://github.com/an4oh/OpenShellyMonitor.git ~/shelly-monitor
cd ~/shelly-monitor
python3 -m venv .venv
.venv/bin/pip install .
cp .env.example .env    # then edit it
.venv/bin/shelly-monitor poll
.venv/bin/shelly-monitor run
```

### b) Docker Compose

```sh
git clone https://github.com/an4oh/OpenShellyMonitor.git ~/shelly-monitor
cd ~/shelly-monitor
cp .env.example .env    # then edit it
# set TZ in docker-compose.yml to your timezone (reports group days in that timezone)
docker compose up -d --build
docker compose logs -f shelly-monitor
```

The compose file starts two containers: the poller and `web`, the dashboard on
`http://127.0.0.1:8787/` (localhost only). The database is in the named volume `shelly-data`
(`/data/shelly-monitor.db` inside the containers). Run commands inside the poller container:

```sh
docker compose exec shelly-monitor shelly-monitor devices --header
docker compose exec shelly-monitor shelly-monitor report --group month
```

The containers run as uid/gid `10001`. To keep the database in a host folder, replace the volume
with `- ./data:/data` in both services and run `mkdir -p data && sudo chown 10001:10001 data`.

### c) systemd

Both setups use a long-running service, restarted on failure, plus an optional dashboard service.

**User services** (no root), files in `deploy/systemd/user/`:

```sh
pipx install git+https://github.com/an4oh/OpenShellyMonitor.git
mkdir -p ~/.config/shelly-monitor && cp .env ~/.config/shelly-monitor/env && chmod 600 ~/.config/shelly-monitor/env
mkdir -p ~/.config/systemd/user && cp deploy/systemd/user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now shelly-monitor shelly-monitor-web
loginctl enable-linger "$USER"     # keep running after logout
```

The database is `~/.local/state/shelly-monitor/shelly-monitor.db`. The units expect the command
at `~/.local/bin/shelly-monitor` (the pipx default).

**System services**, files in `deploy/systemd/`:

```sh
sudo useradd --system --home /var/lib/shelly-monitor shelly-monitor
sudo pipx install --global git+https://github.com/an4oh/OpenShellyMonitor.git   # pipx >= 1.5
sudo install -m 600 -o shelly-monitor .env /etc/shelly-monitor.env
sudo cp deploy/systemd/shelly-monitor.service deploy/systemd/shelly-monitor-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shelly-monitor shelly-monitor-web
```

The database is `/var/lib/shelly-monitor/shelly-monitor.db`. The units expect the command at
`/usr/local/bin/shelly-monitor`; if you installed it elsewhere (e.g. a venv), edit `ExecStart`.

### Check that it works

After a minute or two:

```sh
shelly-monitor devices --header    # every channel with home, kind, online state and last sample
```

```
home	device:channel	name	room	kind	online	groups	model	gen	last_sample
Home A	a1b2c3d4e5f6:0	Boiler	-	switch	online	-	SNSW-102P16EU	G2	2026-10-02 23:59:00
Home B	0f1e2d3c4b5a:0	Heat pump	-	switch	online	-	SNSW-102P16EU	G2	2026-10-02 23:59:00
```

Read commands use `SHELLY_DB`: run them from the folder of your `.env`, or set `SHELLY_DB` to the
database of your setup. Logs:

```sh
journalctl --user -u shelly-monitor -f      # systemd user
sudo journalctl -u shelly-monitor -f        # systemd system
docker compose logs -f shelly-monitor       # Docker
```

To give a channel your own name (stored locally, never sent to the cloud):

```sh
shelly-monitor devices --name a1b2c3d4e5f6 "Boiler"            # channel 0, single home
shelly-monitor devices --name "Home A/a1b2c3d4e5f6:1" "Pump"   # HOME/ID:N with several homes
```

## Reports

```sh
shelly-monitor report --from 2026-10-01 --to 2026-10-02 --group day
```

```
period      home    device     ch  on_min  on_h       wh
----------  ------  ---------  --  ------  ----  -------
2026-10-01  Home A  Boiler      0   480.0  8.00  12000.0
2026-10-01  Home A  Pump        1   480.0  8.00   3200.0
2026-10-01  Home B  Heat pump   0   360.0  6.00   5400.0
2026-10-02  Home A  Boiler      0   480.0  8.00  12000.0
2026-10-02  Home A  Pump        1   479.0  7.98   3193.3
2026-10-02  Home B  Heat pump   0   360.0  6.00   5400.0
```

```sh
shelly-monitor report --group month --by home
```

```
period   home    channels  on_min   on_h       wh
-------  ------  --------  ------  -----  -------
2026-10  Home A         2  1919.0  31.98  30393.3
2026-10  Home B         1   720.0  12.00  10800.0
```

Options (all verified with `shelly-monitor report --help`):

- `--from` / `--to YYYY-MM-DD`: inclusive days.
- `--group day|month|total`: period of each row.
- `--by channel|room|home|group`: one row per channel (default), room, home or custom group.
- `--home HOME`: only one home.
- `--all`: also list channels with 0 minutes ON (always on with `--group total`).
- `--format table|csv|json|pdf` and `--out FILE` (required for `pdf`).

```sh
shelly-monitor report --group total --format csv --out report.csv
shelly-monitor report --from 2026-10-01 --to 2026-10-31 --format pdf --out report.pdf
shelly-monitor export --from 2026-10-01 --home "Home A" -o samples.csv   # raw samples
shelly-monitor export --format pdf -o samples.pdf                        # first 5000 rows
```

The report PDF has a summary, hours per home and room, a chart of hours per day, the channel table
and notes on the method. PDF labels are in Italian. `report --format pdf` uses `--from`, `--to` and
`--home` only.

Custom groups add up channels of your choice, also across homes, for example a "Flat A" made of
the boiler of one home and a pump of another. They live only in the local database:

```sh
shelly-monitor groups --create "Flat A"
shelly-monitor groups --add "Flat A" "Home A/a1b2c3d4e5f6" "Home B/0f1e2d3c4b5a:0"
shelly-monitor report --by group --group month
```

A channel in several groups counts in each of them: do not add group totals together.

Activity log of one channel, newest first:

```sh
shelly-monitor events --home "Home A" --device a1b2c3d4e5f6 --channel 0 --from 2026-10-01 --limit 3
```

```
time	device:channel	event	detail
2026-10-01 04:00:01.234	a1b2c3d4e5f6:0	on/off	ON
2026-10-01 03:00:01.234	a1b2c3d4e5f6:0	on/off	OFF
2026-10-01 02:00:01.234	a1b2c3d4e5f6:0	on/off	ON
```

## Web dashboard

```sh
shelly-monitor serve                         # http://127.0.0.1:8787/
shelly-monitor serve --host 127.0.0.1 --port 8787
```

It shows, for the selected date range: ON hours, energy, channels ON now, hours per home, room and
group, hours per day, and a table of all channels. Each channel opens a detail page with a per-day
timeline, the last transitions and the activity log. A *Devices* page lists every channel with its
state; a *Gruppi* page edits custom groups. CSV and PDF exports are in the "Esporta" menu.
The interface is in Italian.

It opens the database read-only and can run next to the poller. It is a single HTML page with no
external fonts or scripts.

**The dashboard has no authentication.** Anyone who can reach the port can read your data and edit
custom groups. Keep the default bind `127.0.0.1`, or put it behind a reverse proxy that adds
authentication and TLS. Set `SHELLY_WEB_READONLY=1` if people who should not edit groups can reach it.

[deploy/public/README.md](deploy/public/README.md) describes a ready setup: nginx, HTTPS with
Let's Encrypt and a small password login, e.g. on `shelly.example.com`.

## How it works

Endpoints, all read-only (the client refuses any other):

- `POST /device/all_status`: state of every device of the home, every cycle.
- `POST /statistics/event-log`: the cloud activity log, every cycle (undocumented; off with
  `SHELLY_EVENT_LOG=0`).
- `POST /interface/device/get_all_lists`: device names and rooms, once an hour (undocumented).
- `POST /user/get_units_info`, `/user/get_settings`, `/user/get_sub_units`: home name detection,
  once an hour (undocumented).

Per home and per cycle: 1 request, 2 with the activity log, up to 6 in the hourly refresh cycle.
Requests are spaced by `SHELLY_REQUEST_GAP`. If a cycle cannot fit in `SHELLY_INTERVAL`, a warning
is logged at start-up. A failed request (HTTP 429, 5xx, network) is logged and that home is skipped
for the cycle; there are no aggressive retries.

ON time: every pair of consecutive samples of a channel is an interval. The interval counts as ON
only if its first sample is ON **and** the device is online. Each interval is capped at
`2 x SHELLY_INTERVAL`, so gaps in polling do not inflate the total. Intervals go to the day or month
(local time of the host or container) in which they start.

Energy (Wh) comes from the device's cumulative counter when there is one; otherwise it is estimated
as power x time while ON. Treat it as an estimate, not a billing meter.

Activity log: the cloud keeps only the last 5 events per channel. OpenShellyMonitor stores them
locally, deduplicated, so the history starts when you enable it. More than 5 events on one channel
between two polls are lost. Reports are still computed from the samples.

Offline devices: a device missing from `all_status` is offline; otherwise its `cloud.connected`
flag is used. Offline time never counts as ON.

Devices: Gen1 and Gen2+ are supported. Relays, dimmers, RGBW and bulbs count in ON hours. Covers
(position, moving time) and sensors (temperature, humidity, flood) are recorded but not counted.
Input-only devices (i3, i4) are listed with their online state only.

## Data & privacy

- Everything stays on your machine, in one SQLite file (`SHELLY_DB`).
- No telemetry. The only outgoing requests go to your Shelly Cloud server.
- Cloud keys are never written to the database; only their `key_id` fingerprint is.

The database runs in WAL mode, so do not copy it with `cp` while the poller is running. Use
`sqlite3 .backup` (from the `sqlite3` command-line package), which is safe on a live database:

```sh
sqlite3 ~/shelly-monitor/shelly-monitor.db ".backup '/path/to/backup/shelly-monitor-$(date +%F).db'"
```

## Development

```sh
pip install -e '.[dev]'
python3 -m pytest -q
```

Tests use JSON fixtures in `tests/fixtures/` and never touch the network.

```
src/shelly_monitor/cloud.py     Shelly Cloud client and payload parser
src/shelly_monitor/store.py     SQLite storage and migrations
src/shelly_monitor/report.py    ON time and energy aggregation
src/shelly_monitor/cli.py       command line
src/shelly_monitor/web.py       dashboard server (page in web/index.html)
```

## Contributing

Issues and pull requests are welcome. Please add a test for every change and keep the runtime
free of third-party dependencies.

## License

MIT, see [LICENSE](LICENSE).
