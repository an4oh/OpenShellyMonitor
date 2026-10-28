"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import __version__
from .cloud import EVENT_KINDS, CloudError, ShellyCloud, Throttle, event_details, parse_all_status, split_channel_id
from .backup import auto_backup, backup_filename, db_in_use, fmt_size
from .config import (ConfigError, Home, backup_config, cycle_warning, default_home_name, event_log_enabled,
                     parse_homes, request_gap)
from .poller import Maintenance
from .report import BY, aggregate, parse_range, render, write_samples_csv
from .store import RestoreError, Store, restore_from

log = logging.getLogger("shelly_monitor")

NAMES_REFRESH_SECONDS = 3600


def _strip_inline_comment(value: str) -> str:
    """Remove a ' #...' comment that is outside quotes."""
    quote = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments (full-line or ' # inline'),
    optional 'export' prefix and quotes. Existing environment variables are never overridden.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        head, sep, rest = line.partition("export")
        if not head and rest[:1].isspace():
            line = rest.lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), _strip_inline_comment(value.strip())
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and not any(c.isspace() for c in key):
            os.environ.setdefault(key, value)


def interval() -> int:
    raw = os.environ.get("SHELLY_INTERVAL", "60")
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 5:
        print(f"shelly-monitor: SHELLY_INTERVAL must be an integer >= 5 seconds (got {raw!r})", file=sys.stderr)
        raise SystemExit(2)
    return value


def db_path() -> str:
    return os.environ.get("SHELLY_DB", "./shelly-monitor.db")


def existing_db() -> str:
    """DB path for read-only commands: refuse to create an empty database."""
    path = db_path()
    if not os.path.isfile(path):
        print(f"shelly-monitor: database not found: {path} (run 'poll' or 'run' first, or set SHELLY_DB)",
              file=sys.stderr)
        raise SystemExit(1)
    return path


def open_store(path: str | None = None, shared: bool = False) -> Store:
    """Read-write store; data from 0.1.x databases is assigned to SHELLY_HOME_NAME."""
    return Store(path or db_path(), home=default_home_name(), shared=shared)


@dataclass
class Target:
    """A configured home and its client. `name` is the current display name."""
    home: Home
    cloud: ShellyCloud
    name: str = ""


def make_targets(step: int | None = None) -> list[Target]:
    """One client per home, all sharing one throttle (the rate limit is per account)."""
    try:
        homes = parse_homes()
        gap = request_gap()
    except ConfigError as e:
        print(f"shelly-monitor: configuration error: {e}", file=sys.stderr)
        raise SystemExit(2)
    warning = cycle_warning(len(homes), gap, step if step is not None else interval(),
                            per_cycle=2 if event_log_enabled() else 1)
    if warning:
        log.warning("%s", warning)
    throttle = Throttle(gap)
    return [Target(h, ShellyCloud(h.server, h.auth_key, throttle=throttle)) for h in homes]


def prepare_homes(targets: list[Target], store: Store) -> None:
    """Bind legacy data to the first key, then apply override/config/stored names."""
    if targets:
        first = targets[0].home
        reserved = {t.home.name for t in targets if t.home.name and t.home is not first}
        adopted = store.adopt_legacy_home(first.key_id, first.server,
                                          first.name or default_home_name(), reserved)
        if adopted:
            log.info("existing data of home %r bound to key %s", adopted, first.key_id)
    for t in targets:
        t.name = store.sync_home(t.home.key_id, t.home.server, explicit=t.home.name, fallback=t.home.fallback)


def refresh_identity(t: Target, store: Store) -> None:
    """Detect the home's name from the cloud (hourly) and apply it."""
    try:
        found = t.cloud.discover_home()
    except CloudError as e:
        log.warning("home %r (key %s): name detection failed: %s", t.name, t.home.key_id, e)
        return
    if found is None:
        log.warning("home key %s: cannot tell which home of the account it belongs to; using %r. "
                    "Set a name with: shelly-monitor homes --name %s \"NAME\"", t.home.key_id, t.name, t.home.key_id)
        name = store.sync_home(t.home.key_id, t.home.server, explicit=t.home.name, fallback=t.home.fallback)
    else:
        if "@" in found.name:
            log.info("home key %s: the cloud name of this home looks like an e-mail address (%s); "
                     "rename it with 'shelly-monitor homes --name %s NAME' if you prefer",
                     t.home.key_id, found.name, t.home.key_id)
        name = store.sync_home(t.home.key_id, t.home.server, explicit=t.home.name, fallback=t.home.fallback,
                               discovered=found.name, unit_id=found.unit_id)
    if name != t.name:
        log.info("home key %s: name %r -> %r", t.home.key_id, t.name, name)
    t.name = name


def poll_once(targets: list[Target], store: Store) -> bool:
    """One polling cycle: one all_status per home, in sequence (the shared client
    throttle keeps the account under the rate limit). Once an hour per home the
    name is detected and names/rooms are refreshed. A failing home is logged and
    skipped. Returns True only if every home succeeded."""
    all_ok = True
    events_on = event_log_enabled()
    if any(not t.name for t in targets):
        prepare_homes(targets, store)
    for t in targets:
        meta_key = f"lists_refreshed_at:{t.home.key_id}"
        last = store.get_meta(meta_key)
        refresh = last is None or int(time.time()) - int(last) >= NAMES_REFRESH_SECONDS
        if refresh:
            refresh_identity(t, store)
        ts = int(time.time())
        try:
            data = t.cloud.all_status()
        except CloudError as e:
            log.warning("home %r: cycle skipped: %s", t.name, e)
            all_ok = False
            continue
        devices, samples = parse_all_status(data, ts, home=t.name, stale_after=2 * interval())
        n = store.save_poll(devices, samples, ts, home=t.name)
        log.info("home %r: stored %d samples from %d devices", t.name, n, len(devices))
        if refresh:
            lists = t.cloud.device_lists()
            if lists:
                store.apply_lists(t.name, lists)
                store.set_online(t.name, devices)  # channels registered just now
            store.set_meta(meta_key, str(ts))
        if events_on:
            poll_events(t, store, {(s.device_id, s.channel) for s in samples})
    return all_ok


def poll_events(t: Target, store: Store, sampled: set[tuple[str, int]]) -> int:
    """One activity log request for every known output channel of the home. Failures are
    logged and never stop the cycle (the samples are already stored). Returns new events."""
    channels = {(dev, ch) for dev, ch in sampled if ch < 100} | store.event_tags(t.name)
    tags = sorted(dev if ch == 0 else f"{dev}_{ch}" for dev, ch in channels)
    try:
        n = store.save_events(t.cloud.events(tags, t.name))
    except CloudError as e:
        log.warning("home %r: activity log not read: %s", t.name, e)
        return 0
    except Exception as e:  # undocumented endpoint: never let it break polling
        log.warning("home %r: activity log not read (unexpected %s: %s)", t.name, type(e).__name__, e)
        return 0
    if n:
        log.info("home %r: %d new activity log events", t.name, n)
    return n


def date_range(args) -> tuple[int | None, int | None]:
    """--from is inclusive, --to is inclusive (whole day)."""
    try:
        return parse_range(args.date_from, args.date_to)
    except ValueError as e:
        raise SystemExit(f"invalid date range: {e} (expected YYYY-MM-DD)")


def cmd_poll(args) -> int:
    targets = make_targets()
    with open_store() as store:
        prepare_homes(targets, store)
        return 0 if poll_once(targets, store) else 1


def poll_forever(targets: list[Target], step: float, stop: threading.Event, mark_ok=None,
                 maint: Maintenance | None = None) -> None:
    """Poll every `step` seconds, aligned to the clock, until `stop` is set. Opens its own
    read-write store, kept in `maint.store` and used only under `maint.lock` (a restore closes it
    between two cycles; it is reopened at the next one). `mark_ok()` is called after every cycle in
    which every home succeeded; then an automatic backup is written when one is due."""
    maint = maint or Maintenance()
    backups = backup_config()
    announced = False
    try:
        while not stop.is_set():
            with maint.lock:
                if maint.store is None:
                    maint.store = open_store(shared=True)
                    prepare_homes(targets, maint.store)
                    if not announced:
                        announced = True
                        log.info("shelly-monitor %s polling, %d home(s) %s, interval %gs, request gap %gs, db %s",
                                 __version__, len(targets),
                                 ", ".join(f"{t.name!r} [{t.home.key_id}]" for t in targets), step,
                                 targets[0].cloud.throttle.gap, db_path())
                    else:
                        log.info("database reopened after a restore")
                try:
                    if poll_once(targets, maint.store):
                        if mark_ok is not None:
                            mark_ok()
                        auto_backup(maint.store, backups)
                except Exception:  # keep the daemon alive on unexpected bugs, but make them loud
                    log.exception("unexpected error during poll")
            now = time.time()
            stop.wait((int(now // step) + 1) * step - now)
    finally:
        with maint.lock:
            if maint.store is not None:
                maint.store.close()
                maint.store = None


def cmd_run(args) -> int:
    step = interval()
    targets = make_targets(step)
    backup_config()  # configuration errors stop here
    stop = threading.Event()

    def _stop(signum, frame):
        log.info("received signal %d, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_forever(targets, step, stop)
    return 0


def cmd_report(args) -> int:
    lo, hi = date_range(args)
    step = interval()
    if args.format == "pdf":
        return write_pdf(args, "report_pdf")
    with open_store(existing_db()) as store:
        rows = aggregate(store, lo, hi, args.group, max_gap=2 * step,
                         include_zero=args.all or args.group == "total", home=args.home, by=args.by)
    text = render(rows, args.format, by=args.by)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


def write_pdf(args, builder: str) -> int:
    """Build a PDF with the dashboard queries and write it to --out."""
    from . import pdfreport
    from .web import BadRequest, Dashboard

    if not args.out:
        print("shelly-monitor: --out FILE is required with --format pdf", file=sys.stderr)
        return 2
    path = existing_db()
    open_store(path).close()  # migrate before the read-only queries
    q = {k: v for k, v in (("from", args.date_from), ("to", args.date_to), ("home", args.home)) if v}
    try:
        data = getattr(pdfreport, builder)(Dashboard(path, interval()), q)
    except BadRequest as e:
        print(f"shelly-monitor: {e}", file=sys.stderr)
        return 2
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"{args.out}: {len(data)} bytes", file=sys.stderr)
    return 0


def parse_device_ref(value: str) -> tuple[str | None, str, int]:
    """'[home/]ID' -> channel 0; '[home/]ID:N' or '[home/]ID_N' -> channel N. home is None if omitted."""
    home = None
    if "/" in value:
        home, _, value = value.partition("/")
        if not home:
            raise SystemExit(f"invalid device reference: empty home name")
    if ":" in value:
        device_id, _, channel = value.rpartition(":")
        if device_id and channel.isdigit():
            return home, device_id, int(channel)
        raise SystemExit(f"invalid device reference {value!r}, expected [home/]ID, ID:N or ID_N")
    return (home, *split_channel_id(value))


def resolve_home(store: Store, home: str | None) -> str:
    """Home name from a name or key_id prefix; optional with a single home."""
    known = [h["home"] for h in store.homes()] or [default_home_name()]
    if home is not None:
        row = store.home_row(home)
        return row["name"] if row is not None else home
    if len(known) == 1:
        return known[0]
    raise SystemExit(f"several homes in the database ({', '.join(known)}): use HOME/ID[:N]")


def cmd_devices(args) -> int:
    with open_store() as store:
        if args.name:
            home, device_id, channel = parse_device_ref(args.name[0])
            home = resolve_home(store, home)
            store.rename(home, device_id, channel, args.name[1])
            print(f"{home}/{device_id}:{channel} -> {args.name[1]}")
            return 0
        groups = store.channel_groups(args.home)
        if args.header:
            print("home\tdevice:channel\tname\troom\tkind\tonline\tgroups\tmodel\tgen\tlast_sample")
        for r in store.devices(args.home):
            seen = datetime.fromtimestamp(r["last_seen"]).isoformat(sep=" ") if r["last_seen"] else "-"
            online = "-" if r["online"] is None else ("online" if r["online"] else "offline")
            g = ",".join(groups.get((r["home"], r["device_id"], r["channel"]), [])) or "-"
            print(f"{r['home']}\t{r['device_id']}:{r['channel']}\t{r['name'] or '-'}\t{r['room'] or '-'}\t"
                  f"{r['kind'] or '-'}\t{online}\t{g}\t{r['type'] or r['model'] or '-'}\t{r['gen'] or '-'}\t{seen}")
    return 0


def cmd_homes(args) -> int:
    with open_store() as store:
        if args.name:
            key, name = args.name
            if not name.strip() or "/" in name:
                print("shelly-monitor: a home name must be non-empty and must not contain '/'", file=sys.stderr)
                return 2
            try:
                new = store.set_home_override(key, name.strip())
            except KeyError:
                print(f"shelly-monitor: unknown home {key!r} (see 'shelly-monitor homes')", file=sys.stderr)
                return 1
            print(f"{store.home_row(new)['key_id']} -> {new}")
            return 0
        stats = {h["home"]: h for h in store.homes()}
        print("key_id\tname\tunit_id\tserver\tchannels\tlast_sample")
        for r in store.home_rows():
            st = stats.get(r["name"], {})
            seen = datetime.fromtimestamp(st["last_seen"]).isoformat(sep=" ") if st.get("last_seen") else "-"
            name = r["name"] + (" (manual)" if r["name_override"] else "")
            print(f"{r['key_id']}\t{name}\t{r['unit_id'] if r['unit_id'] is not None else '-'}\t"
                  f"{r['server'] or '-'}\t{st.get('channels', 0)}\t{seen}")
        bound = {r["name"] for r in store.home_rows()}
        for name, st in stats.items():  # data not bound to a key yet (before the first poll of 0.3)
            if name not in bound:
                seen = datetime.fromtimestamp(st["last_seen"]).isoformat(sep=" ") if st.get("last_seen") else "-"
                print(f"-\t{name}\t-\t-\t{st['channels']}\t{seen}")
    return 0


def _member_refs(store: Store, refs: list[str]) -> list[tuple[str, str, int]]:
    out = []
    for ref in refs:
        home, device_id, channel = parse_device_ref(ref)
        home = resolve_home(store, home)
        row = store.home_row(home)
        if row is None:
            raise SystemExit(f"home {home!r} is not bound to a cloud key yet (run 'poll' once)")
        out.append((row["key_id"], device_id, channel))
    return out


def cmd_groups(args) -> int:
    with open_store() as store:
        if args.create:
            try:
                gid = store.save_custom_group(args.create)
            except ValueError as e:
                print(f"shelly-monitor: {e}", file=sys.stderr)
                return 2
            print(f"created custom group {args.create!r} (id {gid})")
            return 0
        if args.delete:
            g = store.custom_group(args.delete)
            if g is None:
                print(f"shelly-monitor: unknown custom group {args.delete!r}", file=sys.stderr)
                return 1
            store.delete_custom_group(g["id"])
            print(f"deleted custom group {g['name']!r}")
            return 0
        if args.add or args.remove:
            name, refs = (args.add or args.remove)[0], (args.add or args.remove)[1:]
            g = store.custom_group(name)
            if g is None:
                print(f"shelly-monitor: unknown custom group {name!r} (create it with --create)", file=sys.stderr)
                return 1
            current = {(m["key_id"], m["device_id"], m["channel"]) for m in g["members"]}
            change = set(_member_refs(store, refs))
            members = current | change if args.add else current - change
            try:
                n = store.set_custom_group_members(g["id"], sorted(members))
            except ValueError as e:
                print(f"shelly-monitor: {e}", file=sys.stderr)
                return 2
            print(f"{g['name']}: {n} channel(s)")
            return 0
        print("source\thome\tgroup_id\tname\ttype\tmembers")
        for g in store.groups(args.home):
            print(f"cloud\t{g['home']}\t{g['group_id']}\t{g['name'] or '-'}\t{g['type'] or '-'}\t{g['members']}")
        for g in store.custom_groups():
            homes = sorted({m["home"] or m["key_id"] for m in g["members"]})
            if args.home and args.home not in homes:
                continue
            print(f"custom\t{','.join(homes) or '-'}\t{g['id']}\t{g['name']}\t-\t{len(g['members'])}")
    return 0


def cmd_export(args) -> int:
    lo, hi = date_range(args)
    if args.format == "pdf":
        return write_pdf(args, "samples_pdf")
    path = existing_db()
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        with open_store(path) as store:
            write_samples_csv(store, lo, hi, out, home=args.home)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


EVENT_LABELS = {"turn": "on/off", "brightness": "brightness", "input": "input", "cover": "cover", "sensor": "sensor"}


def event_text(e: dict) -> tuple[str, str]:
    """(kind label, detail) of a stored event row."""
    kind = EVENT_KINDS.get(e["event"])
    arg, source = event_details(e["payload"])
    if kind == "turn":
        return "on/off", "ON" if e["is_on"] else "OFF"
    if kind == "brightness" and isinstance(arg, (int, float)):
        return "brightness", f"{arg:g} %"
    if kind is None:
        return f"event {e['event']}", e["payload"]
    detail = " ".join(str(x) for x in (arg, source) if x is not None)
    return EVENT_LABELS[kind], detail or e["payload"]


def cmd_events(args) -> int:
    lo, hi = date_range(args)
    home_ref, device_id, channel = parse_device_ref(args.device)
    if args.channel is not None:
        channel = args.channel
    with open_store(existing_db()) as store:
        home = resolve_home(store, args.home or home_ref)
        rows = store.events_for(home, device_id, channel, lo, hi, args.limit)
    print("time\tdevice:channel\tevent\tdetail")
    for e in rows:
        when = datetime.fromtimestamp(e["ts_ms"] / 1000)
        kind, detail = event_text(e)
        print(f"{when:%Y-%m-%d %H:%M:%S}.{e['ts_ms'] % 1000:03d}\t{device_id}:{channel}\t{kind}\t{detail}")
    return 0


def cmd_serve(args) -> int:
    from . import web
    from .auth import WebAuth
    from .poller import BackgroundPoller

    host = args.host or os.environ.get("SHELLY_HTTP_HOST", "127.0.0.1")
    try:
        port = args.port if args.port is not None else int(os.environ.get("SHELLY_HTTP_PORT", "8787"))
    except ValueError:
        print("shelly-monitor: SHELLY_HTTP_PORT must be an integer", file=sys.stderr)
        return 2
    try:
        auth = WebAuth.from_env()
    except ConfigError as e:
        print(f"shelly-monitor: configuration error: {e}", file=sys.stderr)
        return 2
    step = interval()
    poller = None
    maint = Maintenance()
    if args.poll:
        targets = make_targets(step)  # configuration errors stop here, before listening
        backup_config()
        poller = BackgroundPoller(lambda stop, mark_ok: poll_forever(targets, step, stop, mark_ok, maint))
    return web.serve(db_path(), step, host, port, migrate_home=default_home_name(), poller=poller, auth=auth,
                     maintenance=maint)


def cmd_backup(args) -> int:
    path = existing_db()
    compress = not args.no_compress
    out = args.out or backup_filename(compress=compress)
    with Store(path, readonly=True) as st:
        if out == "-":
            n = st.backup_to(sys.stdout.buffer, compress=compress)
            sys.stdout.buffer.flush()
        else:
            n = st.backup_to(out, compress=compress)
    print(f"backup of {path} written to {'stdout' if out == '-' else out} ({fmt_size(n)})", file=sys.stderr)
    return 0


def _confirm(prompt: str) -> bool:
    """Interactive yes/no; False when stdin is not a terminal."""
    if not sys.stdin.isatty():
        print("shelly-monitor: not a terminal: pass --yes to restore without confirmation", file=sys.stderr)
        return False
    return input(prompt).strip().lower() in ("y", "yes", "s", "si")


def cmd_restore(args) -> int:
    db = db_path()
    if not os.path.isfile(args.file):
        print(f"shelly-monitor: backup file not found: {args.file}", file=sys.stderr)
        return 1
    print("shelly-monitor: stop the poller ('run', 'serve --poll') and the dashboard ('serve') that use "
          f"{db} before restoring; to restore a running 'serve', use the dashboard instead.", file=sys.stderr)
    reason = db_in_use(db)
    if reason:
        print(f"shelly-monitor: refusing to restore: {db} is {reason}. Stop the processes using it and retry.",
              file=sys.stderr)
        return 1
    if not args.yes and not _confirm(f"Replace {db} with {args.file}? Its current data is lost. [y/N] "):
        print("shelly-monitor: restore cancelled", file=sys.stderr)
        return 1
    try:
        res = restore_from(args.file, db, home=default_home_name())
    except RestoreError as e:
        print(f"shelly-monitor: restore refused, database unchanged: {e}", file=sys.stderr)
        return 1
    before = "none" if res["samples_before"] is None else res["samples_before"]
    migrated = f", migrated to {res['schema_version_now']}" if res["migrated"] else ""
    print(f"restored {db} from {args.file}: samples {before} -> {res['samples_after']}, "
          f"schema {res['schema_version']}{migrated}")
    return 0


SERVE_HELP = """\
Web dashboard. Login: when SHELLY_WEB_PASSWORD (plain text, hashed at startup) or
SHELLY_WEB_PASSWORD_HASH (scrypt$N$r$p$salt$hash) is set, every page and API needs a password
login (cookie session, SHELLY_WEB_SESSION_DAYS, default 7); otherwise there is no authentication:
keep it on 127.0.0.1 or behind a reverse proxy with its own login. Without SHELLY_WEB_COOKIE_SECRET
(hex) the sessions end at every restart. SHELLY_WEB_READONLY=1 disables custom group edits.
GET /healthz is always public."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shelly-monitor", description="Log Shelly Cloud relay states and report ON time.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--env-file", default=".env", help="path of the .env file to load (default: ./.env)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("poll", help="run a single polling cycle").set_defaults(func=cmd_poll)
    sub.add_parser("run", help="poll forever, aligned to SHELLY_INTERVAL").set_defaults(func=cmd_run)

    def add_range(sp):
        sp.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="first day (inclusive)")
        sp.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last day (inclusive)")

    rp = sub.add_parser("report", help="ON time per device/channel")
    add_range(rp)
    rp.add_argument("--group", choices=["day", "month", "total"], default="day")
    rp.add_argument("--format", choices=["table", "csv", "json", "pdf"], default="table",
                    help="pdf: full report (summary, homes, rooms, days, channels); needs --out")
    rp.add_argument("--out", metavar="FILE", help="write to FILE instead of stdout (required for pdf)")
    rp.add_argument("--home", help="only this home")
    rp.add_argument("--by", choices=BY, default="channel",
                    help="one row per channel (default), room, home or custom group")
    rp.add_argument("--all", action="store_true",
                    help="also list channels with 0 minutes ON in day/month groups (always on for total)")
    rp.set_defaults(func=cmd_report)

    dp = sub.add_parser("devices", help="list devices, or rename one")
    dp.add_argument("--name", nargs=2, metavar=("REF", "NAME"),
                    help="set a display name: REF is [HOME/]ID (channel 0), [HOME/]ID:N or [HOME/]ID_N; "
                         "HOME is optional with a single home")
    dp.add_argument("--home", help="only list this home")
    dp.add_argument("--header", action="store_true", help="print a header line")
    gp = sub.add_parser("groups", help="list cloud and custom groups; manage custom groups")
    gp.add_argument("--home", help="only groups with channels of this home")
    gm = gp.add_mutually_exclusive_group()
    gm.add_argument("--create", metavar="NAME", help="create a custom group")
    gm.add_argument("--delete", metavar="NAME", help="delete a custom group (name or id)")
    gm.add_argument("--add", nargs="+", metavar=("NAME", "REF"),
                    help="add channels HOME/ID[:N] (HOME = name or key_id) to a custom group")
    gm.add_argument("--remove", nargs="+", metavar=("NAME", "REF"), help="remove channels from a custom group")
    gp.set_defaults(func=cmd_groups)
    dp.set_defaults(func=cmd_devices)

    hp = sub.add_parser("homes", help="list homes (one per cloud key), or rename one")
    hp.add_argument("--name", nargs=2, metavar=("KEY_ID", "NAME"),
                    help="persistent display name for a home (KEY_ID or current name); wins over detection")
    hp.set_defaults(func=cmd_homes)

    ep = sub.add_parser("export", help="raw samples as CSV or PDF")
    add_range(ep)
    ep.add_argument("-o", "--out", "--output", dest="out", metavar="FILE",
                    help="output file (default: stdout; required for pdf)")
    ep.add_argument("--format", choices=["csv", "pdf"], default="csv")
    ep.add_argument("--home", help="only this home")
    ep.set_defaults(func=cmd_export)

    vp = sub.add_parser("events", help="activity log of one channel (from the cloud event-log)")
    vp.add_argument("--device", required=True, metavar="ID", help="device id ([HOME/]ID, ID:N or ID_N)")
    vp.add_argument("--channel", type=int, help="channel (default 0, or from --device)")
    vp.add_argument("--home", help="home name or key_id (optional with a single home)")
    vp.add_argument("--limit", type=int, default=1000, help="at most this many rows, newest first (default 1000)")
    add_range(vp)
    vp.set_defaults(func=cmd_events)

    bp = sub.add_parser("backup", help="write a consistent backup of the database (safe while polling)")
    bp.add_argument("--out", metavar="FILE",
                    help="output file, '-' = stdout (default: ./shelly-monitor-YYYYmmdd-HHMMSS.db.gz)")
    bp.add_argument("--no-compress", action="store_true", help="plain SQLite file instead of gzip")
    bp.set_defaults(func=cmd_backup)

    rsp = sub.add_parser("restore", help="replace the database with a backup (.db or .db.gz); "
                                         "stop the poller and the dashboard first")
    rsp.add_argument("file", metavar="FILE", help="backup file")
    rsp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rsp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("serve", help="web dashboard (password login if SHELLY_WEB_PASSWORD is set, "
                                      "otherwise no authentication)",
                        description=SERVE_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--host", help="bind address (default: SHELLY_HTTP_HOST or 127.0.0.1)")
    sp.add_argument("--port", type=int, help="port (default: SHELLY_HTTP_PORT or 8787)")
    sp.add_argument("--poll", action="store_true",
                    help="also poll the cloud in the same process (same as 'run', in a background thread): "
                         "one container, one port")
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file)
    logging.basicConfig(
        level=os.environ.get("SHELLY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except sqlite3.OperationalError as e:
        print(f"shelly-monitor: cannot use database {db_path()}: {e}", file=sys.stderr)
        return 1
    except ConfigError as e:
        print(f"shelly-monitor: configuration error: {e}", file=sys.stderr)
        return 2
