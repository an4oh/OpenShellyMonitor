"""SQLite storage."""

from __future__ import annotations

import contextlib
import gzip
import os
import re
import sqlite3
import tempfile
import time
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

from .cloud import ON_KINDS, ChannelMeta, DeviceInfo, DeviceLists, Event, Sample
from .config import DEFAULT_HOME_NAME, db_journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL,
    is_on INTEGER NULL,               -- NULL for sensors
    power_w REAL NULL,
    online INTEGER NOT NULL,
    energy_wh REAL NULL,
    home TEXT NOT NULL,
    kind TEXT NULL,                   -- switch|light|rgbw|cover|sensor (NULL in rows older than 0.4 = switch)
    value REAL NULL                   -- cover position %, brightness, sensor reading
);
CREATE INDEX IF NOT EXISTS idx_samples_home_dev_ch_ts ON samples (home, device_id, channel, ts);
CREATE TABLE IF NOT EXISTS devices (
    home TEXT NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL DEFAULT 0,
    name TEXT,
    model TEXT,
    gen TEXT,
    room_id INTEGER,
    room TEXT,
    last_seen INTEGER,                -- time of the last sample (NULL: never sampled)
    type TEXT,                        -- cloud type code from get_all_lists
    kind TEXT,                        -- switch|light|rgbw|cover|sensor|input|unknown
    channels_count INTEGER,
    online INTEGER,                   -- in the last all_status and connected
    PRIMARY KEY (home, device_id, channel)
);
CREATE TABLE IF NOT EXISTS groups (
    home TEXT NOT NULL,
    group_id TEXT NOT NULL,
    name TEXT,
    type TEXT,
    room_id INTEGER,
    PRIMARY KEY (home, group_id)
);
CREATE TABLE IF NOT EXISTS group_members (
    home TEXT NOT NULL,
    group_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL,
    PRIMARY KEY (home, group_id, device_id, channel)
);
CREATE TABLE IF NOT EXISTS homes (
    key_id TEXT PRIMARY KEY,          -- sha256(auth_key)[:12]: stable identity of a home
    name TEXT NOT NULL UNIQUE,        -- display name, also stored in samples.home / devices.home
    server TEXT,
    unit_id INTEGER,
    discovered_name TEXT,
    name_override TEXT,               -- set by `homes --name`, wins over everything
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS custom_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT,
    created INTEGER
);
CREATE TABLE IF NOT EXISTS custom_group_members (
    group_id INTEGER NOT NULL,
    home_key_id TEXT NOT NULL,        -- homes.key_id: stable across home renames
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL,
    PRIMARY KEY (group_id, home_key_id, device_id, channel)
);
CREATE TABLE IF NOT EXISTS events (  -- activity log from /statistics/event-log (0.7)
    home TEXT NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,           -- epoch milliseconds, UTC
    event INTEGER NOT NULL,           -- cloud `e`: 1 turn, 4 sensor, 5 cover, 8 brightness, 13 input, ...
    is_on INTEGER NULL,               -- only for event 1
    payload TEXT NOT NULL,            -- raw `p` (JSON list)
    cloud_rowid INTEGER NOT NULL,     -- cloud `rowid`, increasing per home: deduplication key
    PRIMARY KEY (home, cloud_rowid)
);
CREATE INDEX IF NOT EXISTS idx_events_home_dev_ch_ts ON events (home, device_id, channel, ts_ms);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_LO, _HI = -(2**62), 2**62
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


SCHEMA_VERSION = 6  # PRAGMA user_version of the current schema (5 = 0.6: custom groups, 6: events)


BUSY_TIMEOUT_MS = 10_000


def _set_journal(conn: sqlite3.Connection, mode: str, attempts: int = 50) -> None:
    """Switch the journal mode (persistent in the file) when it differs. Changing it needs an
    exclusive lock and SQLite does not apply the busy timeout to it, so concurrent openers retry
    briefly. Leaving WAL checkpoints the WAL file into the database first."""
    for i in range(attempts):
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != mode:
                got = conn.execute(f"PRAGMA journal_mode={mode.upper()}").fetchone()[0].lower()
                if got != mode:
                    raise sqlite3.OperationalError(f"database is locked (journal mode still {got})")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or i == attempts - 1:
                raise
            time.sleep(0.05 + 0.01 * i)


def _statements(script: str) -> list[str]:
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


def _migrate(conn: sqlite3.Connection, home: str) -> None:
    """Bring any older database (0.1 - 0.3) to the current schema. Existing rows of
    0.1 databases are assigned to `home`.

    Idempotent and safe with concurrent processes (poller and dashboard starting
    together): the whole upgrade runs in one IMMEDIATE transaction, every step
    re-reads the table layout under that lock, and the result is marked with
    PRAGMA user_version so later opens skip it. SQLite DDL is transactional: a
    failure leaves the database as it was.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    previous = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control (no implicit COMMIT before DDL)
    try:
        conn.execute("BEGIN IMMEDIATE")  # waits (connection timeout) for another process migrating
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                _upgrade(conn, home)
                for stmt in _statements(SCHEMA):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous


def _upgrade(conn: sqlite3.Connection, home: str) -> None:
    cols = _columns(conn, "samples")
    if cols and "energy_wh" not in cols:
        conn.execute("ALTER TABLE samples ADD COLUMN energy_wh REAL NULL")
    if cols and "home" not in _columns(conn, "samples"):
        # ADD COLUMN with a constant default is O(1): existing rows read the default.
        conn.execute("ALTER TABLE samples ADD COLUMN home TEXT NOT NULL DEFAULT " + _sql_literal(home))
        conn.execute("DROP INDEX IF EXISTS idx_samples_dev_ch_ts")
    dcols = _columns(conn, "devices")
    if dcols and "home" not in dcols:
        conn.execute("ALTER TABLE devices RENAME TO devices_old")
        conn.execute(next(x for x in _statements(SCHEMA) if "TABLE IF NOT EXISTS devices" in x))
        channel = "channel" if "channel" in dcols else "0"
        conn.execute(
            "INSERT INTO devices (home, device_id, channel, name, model, gen, last_seen) "
            f"SELECT ?, device_id, {channel}, name, model, gen, last_seen FROM devices_old",
            (home,),
        )
        conn.execute("DROP TABLE devices_old")
    for col, decl in (("type", "TEXT"), ("kind", "TEXT"), ("channels_count", "INTEGER"), ("online", "INTEGER")):
        dcols = _columns(conn, "devices")  # re-read before every ADD COLUMN
        if dcols and col not in dcols:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {decl}")
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(samples)")}
    if info and (info["is_on"][3] or "kind" not in info or "value" not in info):
        # 0.4: is_on becomes nullable (sensors) and kind/value are added. SQLite cannot drop
        # NOT NULL in place, so the table is rebuilt once.
        conn.execute("DROP TABLE IF EXISTS samples_old")
        conn.execute("ALTER TABLE samples RENAME TO samples_old")
        conn.execute("DROP INDEX IF EXISTS idx_samples_home_dev_ch_ts")
        conn.execute(next(x for x in _statements(SCHEMA) if "TABLE IF NOT EXISTS samples" in x))
        keep = [c for c in ("ts", "device_id", "channel", "is_on", "power_w", "online", "energy_wh", "home",
                            "kind", "value") if c in info]
        cols_sql = ", ".join(keep)
        conn.execute(f"INSERT INTO samples ({cols_sql}) SELECT {cols_sql} FROM samples_old")
        conn.execute("DROP TABLE samples_old")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _filters(home: str | None, room: str | None, alias: str = "") -> tuple[str, list]:
    sql, args = "", []
    if home is not None:
        sql += f" AND {alias}home = ?"
        args.append(home)
    if room is not None:
        sql += (f" AND EXISTS (SELECT 1 FROM devices d WHERE d.home = {alias or 'samples.'}home"
                f" AND d.device_id = {alias or 'samples.'}device_id AND d.channel = {alias or 'samples.'}channel"
                " AND d.room = ?)")
        args.append(room)
    return sql, args


class Store:
    def __init__(self, path: str, readonly: bool = False, home: str = DEFAULT_HOME_NAME,
                 journal: str | None = None, shared: bool = False):
        """`home` is only used to label data migrated from a 0.1.x database. `journal` is the
        journal mode of read-write connections, `wal` or `delete` (default: SHELLY_DB_JOURNAL);
        read-only connections use whatever the file has. `shared`: the read-write connection may
        be closed by another thread (the in-process poller's store, closed by a restore)."""
        self.path = str(path)
        timeout = BUSY_TIMEOUT_MS / 1000
        if readonly:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            return
        mode = journal or db_journal()
        self.conn = sqlite3.connect(path, timeout=timeout, check_same_thread=not shared)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _set_journal(self.conn, mode)
        if mode == "delete":
            self.conn.execute("PRAGMA synchronous = NORMAL")
        _migrate(self.conn, home)

    def close(self) -> None:
        self.conn.close()

    def backup_to(self, dest, compress: bool = True) -> int:
        """Write a consistent snapshot of the database to `dest` (a path, or a binary file object),
        gzip-compressed unless `compress` is False. Uses the SQLite online backup API, so it is safe
        with WAL and rollback journals while other connections write. A path is written to
        `dest.part` and then renamed. Returns the number of bytes written."""
        with tempfile.TemporaryDirectory(prefix="shelly-backup-") as tmp:
            snap = os.path.join(tmp, "snapshot.db")
            out = sqlite3.connect(snap)
            try:
                self.conn.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")  # a standalone file, also from a WAL source
            finally:
                out.close()
            if not isinstance(dest, (str, os.PathLike)):
                return _copy_out(snap, dest, compress)
            part = f"{os.fspath(dest)}.part"
            try:
                with open(part, "wb") as f:
                    n = _copy_out(snap, f, compress)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(part, dest)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part)
                raise
            return n

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def save_poll(self, devices: Iterable[DeviceInfo], samples: Iterable[Sample], ts: int,
                  home: str | None = None) -> int:
        """Store samples and online flags. With `home`, devices of that home missing
        from `devices` (not in all_status: offline in the cloud) are marked offline."""
        samples = list(samples)
        devices = list(devices)
        info = {(d.home, d.device_id): d for d in devices}
        with self.conn:
            for s in samples:
                d = info.get((s.home, s.device_id))
                self.conn.execute(
                    "INSERT INTO devices (home, device_id, channel, model, gen, last_seen, kind, online) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(home, device_id, channel) DO UPDATE SET "
                    "model=COALESCE(excluded.model, devices.model), gen=COALESCE(devices.gen, excluded.gen), "
                    "last_seen=excluded.last_seen, kind=COALESCE(devices.kind, excluded.kind), "
                    "online=excluded.online",
                    (s.home, s.device_id, s.channel, d.model if d else None, d.gen if d else None, ts,
                     s.kind, int(s.online)),
                )
            self._set_online(home, devices)
            self.conn.executemany(
                "INSERT INTO samples (ts, home, device_id, channel, is_on, power_w, online, energy_wh, kind, value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(s.ts, s.home, s.device_id, s.channel, None if s.is_on is None else int(s.is_on), s.power_w,
                  int(s.online), s.energy_wh, s.kind, s.value) for s in samples],
            )
        return len(samples)

    def _set_online(self, home: str | None, devices: list[DeviceInfo]) -> None:
        if home is not None:
            self.conn.execute("UPDATE devices SET online = 0 WHERE home = ?", (home,))
        for d in devices:
            self.conn.execute("UPDATE devices SET online = ? WHERE home = ? AND device_id = ?",
                              (int(d.online), d.home, d.device_id))

    def set_online(self, home: str, devices: Iterable[DeviceInfo]) -> None:
        """Online flag of every channel of `home`: devices missing from all_status are offline."""
        with self.conn:
            self._set_online(home, list(devices))

    def apply_lists(self, home: str, lists: DeviceLists | dict[tuple[str, int], ChannelMeta]) -> None:
        """Register every channel of get_all_lists for `home` (also never-sampled ones)
        and replace the home's groups.

        Names fill only channels without a name (manual names win); rooms, type
        and kind follow the cloud.
        """
        if isinstance(lists, dict):
            lists = DeviceLists(channels=lists)
        with self.conn:
            for (device_id, channel), m in lists.channels.items():
                self.conn.execute(
                    "INSERT INTO devices (home, device_id, channel, name, model, gen, room_id, room, type, kind, "
                    "channels_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(home, device_id, channel) DO UPDATE SET name=COALESCE(devices.name, excluded.name), "
                    "model=COALESCE(devices.model, excluded.model), gen=COALESCE(excluded.gen, devices.gen), "
                    "room_id=excluded.room_id, room=excluded.room, type=COALESCE(excluded.type, devices.type), "
                    "kind=COALESCE(excluded.kind, devices.kind), "
                    "channels_count=COALESCE(excluded.channels_count, devices.channels_count)",
                    (home, device_id, channel, m.name, m.type, m.gen, m.room_id, m.room, m.type, m.kind,
                     m.channels_count),
                )
            if lists.groups or lists.channels:
                self.conn.execute("DELETE FROM group_members WHERE home = ?", (home,))
                self.conn.execute("DELETE FROM groups WHERE home = ?", (home,))
            for g in lists.groups:
                self.conn.execute("INSERT OR REPLACE INTO groups (home, group_id, name, type, room_id) "
                                  "VALUES (?, ?, ?, ?, ?)", (home, g.group_id, g.name, g.type, g.room_id))
                self.conn.executemany(
                    "INSERT OR IGNORE INTO group_members (home, group_id, device_id, channel) VALUES (?, ?, ?, ?)",
                    [(home, g.group_id, dev, ch) for dev, ch in g.members])

    def groups(self, home: str | None = None) -> list[dict]:
        sql, args = ("WHERE g.home = ?", [home]) if home is not None else ("", [])
        out = []
        for g in self.conn.execute(
                f"SELECT g.*, (SELECT COUNT(*) FROM group_members m WHERE m.home = g.home AND m.group_id = g.group_id)"
                f" AS members FROM groups g {sql} ORDER BY g.home, g.name", args):
            out.append(dict(g))
        return out

    def channel_groups(self, home: str | None = None) -> dict[tuple[str, str, int], list[str]]:
        sql, args = ("WHERE m.home = ?", [home]) if home is not None else ("", [])
        out: dict[tuple[str, str, int], list[str]] = {}
        try:
            rows = self.conn.execute(
                "SELECT m.home, m.device_id, m.channel, g.name FROM group_members m JOIN groups g "
                f"ON g.home = m.home AND g.group_id = m.group_id {sql} ORDER BY g.name", args).fetchall()
        except sqlite3.OperationalError:  # read-only database not migrated yet
            return out
        for r in rows:
            out.setdefault((r["home"], r["device_id"], r["channel"]), []).append(r["name"])
        return out

    def rename(self, home: str, device_id: str, channel: int, name: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO devices (home, device_id, channel, name) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(home, device_id, channel) DO UPDATE SET name=excluded.name",
                (home, device_id, channel, name),
            )

    def devices(self, home: str | None = None, room: str | None = None,
                kinds: Iterable[str] | None = None, channels: set | None = None) -> list[sqlite3.Row]:
        """Registered channels. `kinds` filters by kind (NULL kind = legacy switch);
        `channels` is an optional set of (home, device_id, channel) to keep."""
        sql, args = _filters(home, None)
        if room is not None:
            sql += " AND room = ?"
            args.append(room)
        if kinds is not None:
            kinds = list(kinds)
            sql += f" AND COALESCE(kind, 'switch') IN ({','.join('?' * len(kinds))})"
            args += kinds
        rows = self.conn.execute(
            f"SELECT * FROM devices WHERE 1=1{sql} ORDER BY home, COALESCE(name, device_id), device_id, channel", args
        ).fetchall()
        if channels is not None:
            rows = [r for r in rows if (r["home"], r["device_id"], r["channel"]) in channels]
        return rows

    def homes(self) -> list[dict]:
        """Homes that have channels, with identity from the `homes` table when known."""
        ident = {r["name"]: r for r in self.home_rows()}
        out = []
        for r in self.conn.execute(
            "SELECT home, COUNT(*) AS channels, MAX(last_seen) AS last_seen FROM devices "
            "GROUP BY home ORDER BY home"
        ):
            rooms = [x[0] for x in self.conn.execute(
                "SELECT DISTINCT room FROM devices WHERE home = ? AND room IS NOT NULL ORDER BY room", (r["home"],))]
            h = ident.get(r["home"])
            out.append({"home": r["home"], "channels": r["channels"], "rooms": rooms, "last_seen": r["last_seen"],
                        "key_id": h["key_id"] if h else None, "unit_id": h["unit_id"] if h else None,
                        "discovered_name": h["discovered_name"] if h else None,
                        "server": h["server"] if h else None})
        return out

    def channel_info(self) -> dict[tuple[str, str, int], sqlite3.Row]:
        return {(r["home"], r["device_id"], r["channel"]): r for r in self.conn.execute("SELECT * FROM devices")}

    def names(self) -> dict[tuple[str, str, int], str]:
        return {k: r["name"] for k, r in self.channel_info().items() if r["name"]}

    def samples(self, from_ts: int | None = None, to_ts: int | None = None,
                home: str | None = None, room: str | None = None, kinds: Iterable[str] | None = None,
                device: tuple[str, int] | None = None, channels: set | None = None) -> Iterator[sqlite3.Row]:
        """Samples with from_ts <= ts < to_ts, ordered by home, device, channel, ts.

        `kinds` keeps only those kinds (NULL kind = legacy switch); `device` is (device_id, channel);
        `channels` an optional set of (home, device_id, channel)."""
        sql, args = _filters(home, room)
        if kinds is not None:
            kinds = list(kinds)
            sql += f" AND COALESCE(kind, 'switch') IN ({','.join('?' * len(kinds))})"
            args += kinds
        if device is not None:
            sql += " AND device_id = ? AND channel = ?"
            args += list(device)
        rows = self.conn.execute(
            "SELECT ts, home, device_id, channel, is_on, power_w, online, energy_wh, kind, value FROM samples "
            f"WHERE ts >= ? AND ts < ?{sql} ORDER BY home, device_id, channel, ts",
            [from_ts if from_ts is not None else _LO, to_ts if to_ts is not None else _HI, *args],
        )
        if channels is None:
            yield from rows
        else:
            yield from (r for r in rows if (r["home"], r["device_id"], r["channel"]) in channels)

    def latest(self, home: str | None = None, room: str | None = None,
               kinds: Iterable[str] | None = ON_KINDS, channels: set | None = None
               ) -> dict[tuple[str, str, int], sqlite3.Row]:
        """Last sample of every sampled channel of `kinds` (index seek per channel)."""
        out = {}
        for d in self.devices(home, room, kinds, channels):
            if d["last_seen"] is None:
                continue
            row = self.conn.execute(
                "SELECT ts, is_on, online, power_w, kind, value FROM samples "
                "WHERE home = ? AND device_id = ? AND channel = ? "
                "ORDER BY ts DESC LIMIT 1",
                (d["home"], d["device_id"], d["channel"]),
            ).fetchone()
            if row:
                out[(d["home"], d["device_id"], d["channel"])] = row
        return out

    def count_samples(self, from_ts: int | None = None, to_ts: int | None = None,
                      home: str | None = None, room: str | None = None, channels: set | None = None) -> int:
        if channels is not None:  # one index range count per channel
            return sum(self.conn.execute(
                "SELECT COUNT(*) FROM samples WHERE home = ? AND device_id = ? AND channel = ? AND ts >= ? AND ts < ?",
                (h, dev, ch, from_ts if from_ts is not None else _LO, to_ts if to_ts is not None else _HI),
            ).fetchone()[0] for h, dev, ch in channels
                if (home is None or h == home) and (room is None or self._room(h, dev, ch) == room))
        sql, args = _filters(home, room)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM samples WHERE ts >= ? AND ts < ?{sql}",
            [from_ts if from_ts is not None else _LO, to_ts if to_ts is not None else _HI, *args],
        ).fetchone()[0]

    def _room(self, home: str, device_id: str, channel: int) -> str | None:
        row = self.conn.execute("SELECT room FROM devices WHERE home = ? AND device_id = ? AND channel = ?",
                                (home, device_id, channel)).fetchone()
        return row[0] if row else None

    # --- activity log --------------------------------------------------------

    def save_events(self, events: Iterable[Event]) -> int:
        """Store activity log events; already known (home, rowid) are ignored. Returns how many are new."""
        rows = [(e.home, e.device_id, e.channel, e.ts_ms, e.event, None if e.is_on is None else int(e.is_on),
                 e.payload, e.rowid) for e in events]
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO events (home, device_id, channel, ts_ms, event, is_on, payload, cloud_rowid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return self.conn.total_changes - before

    @staticmethod
    def _event_range(lo: int | None, hi: int | None) -> tuple[int, int]:
        """Seconds [lo, hi) -> milliseconds."""
        return (lo * 1000 if lo is not None else _LO, hi * 1000 if hi is not None else _HI)

    def events_for(self, home: str, device_id: str, channel: int, lo: int | None = None, hi: int | None = None,
                   limit: int | None = None) -> list[dict]:
        """Events of one channel with lo <= ts < hi (seconds), newest first."""
        try:
            rows = self.conn.execute(
                "SELECT ts_ms, event, is_on, payload, cloud_rowid FROM events "
                "WHERE home = ? AND device_id = ? AND channel = ? AND ts_ms >= ? AND ts_ms < ? "
                "ORDER BY ts_ms DESC, cloud_rowid DESC LIMIT ?",
                (home, device_id, channel, *self._event_range(lo, hi), -1 if limit is None else limit)).fetchall()
        except sqlite3.OperationalError:  # read-only connection on a database older than 0.7
            return []
        return [{"ts_ms": r["ts_ms"], "event": r["event"], "is_on": None if r["is_on"] is None else bool(r["is_on"]),
                 "payload": r["payload"], "rowid": r["cloud_rowid"]} for r in rows]

    def count_events(self, home: str, device_id: str, channel: int, lo: int | None = None,
                     hi: int | None = None) -> int:
        try:
            return self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE home = ? AND device_id = ? AND channel = ? "
                "AND ts_ms >= ? AND ts_ms < ?", (home, device_id, channel, *self._event_range(lo, hi))).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def event_tags(self, home: str) -> set[tuple[str, int]]:
        """Registered output channels of `home` (peripherals, channel >= 100, have no activity log)."""
        return {(r[0], r[1]) for r in self.conn.execute(
            "SELECT device_id, channel FROM devices WHERE home = ? AND channel < 100", (home,))}

    # --- cloud groups as channel sets ------------------------------------------

    def cloud_group_channels(self, home: str, group_id: str) -> set[tuple[str, str, int]]:
        try:
            return {(home, r[0], r[1]) for r in self.conn.execute(
                "SELECT device_id, channel FROM group_members WHERE home = ? AND group_id = ?", (home, group_id))}
        except sqlite3.OperationalError:
            return set()

    # --- custom groups --------------------------------------------------------

    def _key_names(self) -> dict[str, str]:
        return {r["key_id"]: r["name"] for r in self.home_rows()}

    def custom_groups(self) -> list[dict]:
        """Custom groups with their members (resolved to current home names)."""
        try:
            groups = self.conn.execute("SELECT * FROM custom_groups ORDER BY name").fetchall()
        except sqlite3.OperationalError:  # read-only connection on a database older than 0.6
            return []
        names = self._key_names()
        info = self.channel_info()
        out = []
        for g in groups:
            members = []
            for m in self.conn.execute("SELECT * FROM custom_group_members WHERE group_id = ? "
                                       "ORDER BY home_key_id, device_id, channel", (g["id"],)):
                home = names.get(m["home_key_id"])
                d = info.get((home, m["device_id"], m["channel"])) if home else None
                members.append({"key_id": m["home_key_id"], "home": home, "device_id": m["device_id"],
                                "channel": m["channel"], "name": d["name"] if d is not None else None,
                                "kind": d["kind"] if d is not None else None})
            out.append({"id": g["id"], "name": g["name"], "color": g["color"], "created": g["created"],
                        "members": members})
        return out

    def custom_group_channels(self) -> dict[str, set[tuple[str, str, int]]]:
        """Group name -> set of (home name, device_id, channel)."""
        return {g["name"]: {(m["home"], m["device_id"], m["channel"]) for m in g["members"] if m["home"]}
                for g in self.custom_groups()}

    def custom_group(self, key) -> dict | None:
        """By id (int or digits) or by name."""
        for g in self.custom_groups():
            if str(g["id"]) == str(key) or g["name"] == key:
                return g
        return None

    def save_custom_group(self, name: str, color: str | None = None, group_id: int | None = None) -> int:
        """Create (group_id None) or rename/recolour a custom group. Raises ValueError, KeyError."""
        name = (name or "").strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("name must be 1-60 characters")
        if color is not None and not _COLOR.match(color):
            raise ValueError("color must be #rrggbb")
        try:
            with self.conn:
                if group_id is None:
                    cur = self.conn.execute("INSERT INTO custom_groups (name, color, created) VALUES (?, ?, ?)",
                                            (name, color, int(time.time())))
                    return cur.lastrowid
                cur = self.conn.execute("UPDATE custom_groups SET name = ?, color = COALESCE(?, color) WHERE id = ?",
                                        (name, color, group_id))
                if cur.rowcount == 0:
                    raise KeyError(group_id)
                return group_id
        except sqlite3.IntegrityError:
            raise ValueError(f"a group named {name!r} already exists") from None

    def set_custom_group_members(self, group_id: int, members: Iterable[tuple[str, str, int]]) -> int:
        """Replace the members: (home key_id or home name, device_id, channel). Every member must be a
        registered channel of a home bound to a key. Raises ValueError, KeyError."""
        if not self.conn.execute("SELECT 1 FROM custom_groups WHERE id = ?", (group_id,)).fetchone():
            raise KeyError(group_id)
        rows = self.home_rows()
        by_key = {r["key_id"]: r["name"] for r in rows}
        by_name = {r["name"]: r["key_id"] for r in rows}
        info = self.channel_info()
        resolved = set()
        for home, device_id, channel in members:
            key = home if home in by_key else by_name.get(home)
            if key is None:
                raise ValueError(f"unknown home {home!r}")
            if not isinstance(channel, int) or (by_key[key], device_id, channel) not in info:
                raise ValueError(f"unknown channel {home}/{device_id}:{channel}")
            resolved.add((key, device_id, channel))
        with self.conn:
            self.conn.execute("DELETE FROM custom_group_members WHERE group_id = ?", (group_id,))
            self.conn.executemany("INSERT INTO custom_group_members (group_id, home_key_id, device_id, channel) "
                                  "VALUES (?, ?, ?, ?)", [(group_id, *m) for m in sorted(resolved)])
        return len(resolved)

    def delete_custom_group(self, group_id: int) -> None:
        with self.conn:
            cur = self.conn.execute("DELETE FROM custom_groups WHERE id = ?", (group_id,))
            if cur.rowcount == 0:
                raise KeyError(group_id)
            self.conn.execute("DELETE FROM custom_group_members WHERE group_id = ?", (group_id,))

    # --- homes ---------------------------------------------------------------

    def home_rows(self) -> list[sqlite3.Row]:
        try:
            return self.conn.execute("SELECT * FROM homes ORDER BY name").fetchall()
        except sqlite3.OperationalError:  # read-only connection on a database not migrated yet
            return []

    def home_row(self, key: str) -> sqlite3.Row | None:
        """Home by key_id or by current name."""
        return self.conn.execute("SELECT * FROM homes WHERE key_id = ? OR name = ? ORDER BY key_id = ? DESC",
                                 (key, key, key)).fetchone()

    def adopt_legacy_home(self, key_id: str, server: str, legacy_name: str,
                          reserved: set[str] = frozenset()) -> str | None:
        """Bind data written before the homes table existed to the first configured key.

        Runs only while `homes` is empty. Picks `legacy_name` if data exists under
        it, else the only home name present. Names in `reserved` (fixed names of
        other configured keys) are never adopted: those keys own that data.
        Returns the adopted name.
        """
        if self.conn.execute("SELECT 1 FROM homes LIMIT 1").fetchone():
            return None
        names = {r[0] for r in self.conn.execute("SELECT DISTINCT home FROM devices")}
        names |= {r[0] for r in self.conn.execute("SELECT DISTINCT home FROM samples")}
        names -= set(reserved)
        name = legacy_name if legacy_name in names else (next(iter(names)) if len(names) == 1 else None)
        if name is not None:
            with self.conn:
                self.conn.execute("INSERT INTO homes (key_id, name, server, updated) VALUES (?, ?, ?, ?)",
                                  (key_id, name, server, int(time.time())))
        return name

    def _unique_name(self, name: str, key_id: str) -> str:
        taken = {r[0].casefold() for r in self.conn.execute("SELECT name FROM homes WHERE key_id != ?", (key_id,))}
        candidate, n = name, 2
        while candidate.casefold() in taken:
            candidate, n = f"{name} ({n})", n + 1
        return candidate

    def _rename_data(self, old: str, new: str) -> None:
        self.conn.execute("UPDATE samples SET home = ? WHERE home = ?", (new, old))
        # A target name that already has rows (orphan data) is merged; conflicting rows keep the target's.
        self.conn.execute("UPDATE OR IGNORE devices SET home = ? WHERE home = ?", (new, old))
        self.conn.execute("DELETE FROM devices WHERE home = ?", (old,))
        self.conn.execute("UPDATE OR IGNORE events SET home = ? WHERE home = ?", (new, old))
        self.conn.execute("DELETE FROM events WHERE home = ?", (old,))

    def sync_home(self, key_id: str, server: str | None, explicit: str | None = None,
                  fallback: str = DEFAULT_HOME_NAME, discovered: str | None = None,
                  unit_id: int | None = None) -> str:
        """Resolve the display name of a home and apply it everywhere.

        Priority: `homes --name` override > explicit config name > name detected
        from the cloud (now or earlier) > current name > fallback. Names are made
        unique across keys. If the name changes, samples and devices follow, in
        the same transaction. Returns the name.
        """
        with self.conn:
            row = self.conn.execute("SELECT * FROM homes WHERE key_id = ?", (key_id,)).fetchone()
            if discovered is None and row is not None:
                discovered, unit_id = row["discovered_name"], row["unit_id"]
            override = row["name_override"] if row is not None else None
            current = row["name"] if row is not None else None
            name = self._unique_name(override or explicit or discovered or current or fallback, key_id)
            now = int(time.time())
            if row is None:
                self.conn.execute(
                    "INSERT INTO homes (key_id, name, server, unit_id, discovered_name, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (key_id, name, server, unit_id, discovered, now))
            else:
                if name != current:
                    self._rename_data(current, name)
                self.conn.execute(
                    "UPDATE homes SET name = ?, server = COALESCE(?, server), unit_id = ?, discovered_name = ?, "
                    "updated = ? WHERE key_id = ?", (name, server, unit_id, discovered, now, key_id))
        return name

    def set_home_override(self, key: str, name: str | None) -> str:
        """Persistent manual name (None clears it). Returns the resulting name."""
        row = self.home_row(key)
        if row is None:
            raise KeyError(key)
        with self.conn:
            self.conn.execute("UPDATE homes SET name_override = ? WHERE key_id = ?", (name, row["key_id"]))
        return self.sync_home(row["key_id"], None, fallback=row["name"])

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


# --- backup and restore ----------------------------------------------------------------------
GZIP_MAGIC = b"\x1f\x8b"
SQLITE_MAGIC = b"SQLite format 3\x00"
REQUIRED_TABLES = ("samples", "devices", "homes")
_CHUNK = 1 << 20


class RestoreError(ValueError):
    """The file cannot be restored; the message is meant for the user. The database is untouched."""


class _Counter:
    def __init__(self, f):
        self.f, self.n = f, 0

    def write(self, b) -> int:
        self.n += len(b)
        self.f.write(b)
        return len(b)

    def flush(self) -> None:
        self.f.flush()


def _copy_out(src: str, f, compress: bool) -> int:
    counter = _Counter(f)
    with open(src, "rb") as fin:
        if compress:
            with gzip.GzipFile(filename="", mode="wb", fileobj=counter, compresslevel=6) as gz:
                while chunk := fin.read(_CHUNK):
                    gz.write(chunk)
        else:
            while chunk := fin.read(_CHUNK):
                counter.write(chunk)
    counter.flush()
    return counter.n


def _unpack(src: str, dest: str, max_bytes: int | None) -> None:
    with open(src, "rb") as f:
        gz = f.read(2) == GZIP_MAGIC
    total = 0
    try:
        with (gzip.open(src, "rb") if gz else open(src, "rb")) as fin, open(dest, "wb") as fout:
            while chunk := fin.read(_CHUNK):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise RestoreError(f"the database is larger than the limit ({max_bytes / 1e6:.0f} MB)")
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
    except (gzip.BadGzipFile, EOFError, zlib.error) as e:
        raise RestoreError(f"not a valid gzip file ({e or 'truncated'})") from None


def _validate(path: str) -> int:
    """Check a candidate database; returns its PRAGMA user_version."""
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise RestoreError("not a SQLite database")
    try:
        conn = sqlite3.connect(path)  # private temporary file: clear a WAL flag, so read-only opening works
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            problems = [r[0] for r in conn.execute("PRAGMA integrity_check(5)")]
            if problems != ["ok"]:
                raise RestoreError("the database is corrupt (integrity check: " + "; ".join(problems)[:300] + ")")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RestoreError(f"the backup comes from a newer version of shelly-monitor (schema {version}, "
                                   f"this version supports up to {SCHEMA_VERSION}): upgrade first")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            if missing:
                raise RestoreError("not a shelly-monitor database (missing tables: " + ", ".join(missing) + ")")
            return version
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise RestoreError(f"not a valid SQLite database ({e})") from None


def _sample_count(path: str) -> int | None:
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            return conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def restore_from(src, db_path, *, max_bytes: int | None = None, home: str = DEFAULT_HOME_NAME,
                 journal: str | None = None) -> dict:
    """Replace the database at `db_path` with the backup `src` (a .db or .db.gz path).

    The backup is unpacked to `db_path.restore-tmp` (at most `max_bytes` uncompressed), validated
    (SQLite, integrity check, schema version not newer than this code, shelly-monitor tables),
    then renamed over the database in one step; leftover -wal/-shm/-journal files of the old
    database are removed and the new one is opened read-write (journal mode, migrations).
    Nothing else may have the database open: callers stop or lock the other users first.
    Raises RestoreError (database untouched) for files that cannot be restored."""
    db_path = os.fspath(db_path)
    tmp = db_path + ".restore-tmp"
    try:
        _unpack(os.fspath(src), tmp, max_bytes)
        version = _validate(tmp)
        before = _sample_count(db_path)
        os.replace(tmp, db_path)
        for suffix in ("-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(db_path + suffix)
        with Store(db_path, home=home, journal=journal) as st:
            after = st.count_samples()
            now = st.conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return {"ok": True, "samples_before": before, "samples_after": after, "schema_version": version,
            "schema_version_now": now, "migrated": version < now}
