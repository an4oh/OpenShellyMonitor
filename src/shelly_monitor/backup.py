"""Automatic backups, backup file names and the "database in use" check of the CLI restore."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import BackupConfig
from .store import Store

log = logging.getLogger(__name__)

META_LAST = "last_backup"  # meta key: {"ts", "path", "size"} of the last automatic backup
NAME_RE = re.compile(r"^shelly-monitor-\d{8}-\d{6}\.db(\.gz)?$")


def backup_filename(ts: float | None = None, compress: bool = True) -> str:
    """shelly-monitor-YYYYmmdd-HHMMSS.db.gz (local time)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if ts is None else ts))
    return f"shelly-monitor-{stamp}.db" + (".gz" if compress else "")


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def prune(folder, keep: int) -> int:
    """Delete the oldest backup files (shelly-monitor-YYYYmmdd-HHMMSS.db[.gz]) beyond `keep`.
    Other files are never touched. Returns how many were deleted."""
    names = sorted(n for n in os.listdir(folder) if NAME_RE.match(n))
    old = names[:-keep] if keep > 0 else names
    for n in old:
        os.unlink(os.path.join(folder, n))
    return len(old)


def last_backup(store: Store) -> dict | None:
    raw = store.get_meta(META_LAST)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def auto_backup(store: Store, cfg: BackupConfig, now: float | None = None) -> Path | None:
    """Write a backup to `cfg.dir` when `cfg.interval_hours` have passed since the last one (time
    kept in the meta table), then delete the oldest beyond `cfg.keep`. Errors are logged as
    warnings and never raised. Returns the new file, or None."""
    if not cfg.dir:
        return None
    now = time.time() if now is None else now
    try:
        last = last_backup(store)
        if last and now - float(last.get("ts", 0)) < cfg.interval_hours * 3600:
            return None
        folder = Path(cfg.dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / backup_filename(now)
        size = store.backup_to(path)
        store.set_meta(META_LAST, json.dumps({"ts": int(now), "path": str(path), "size": size}))
        removed = prune(folder, cfg.keep)
        log.info("automatic backup %s (%s)%s", path, fmt_size(size),
                 f", {removed} old backup(s) deleted" if removed else "")
        return path
    except Exception as e:  # never stop polling for a backup
        log.warning("automatic backup to %s failed: %s", cfg.dir, e)
        return None


def db_in_use(path: str) -> str | None:
    """Why the database at `path` looks open in another process (None: it does not). A non-empty
    WAL file means a connection is open; otherwise an exclusive lock must be obtainable quickly."""
    wal = path + "-wal"
    if os.path.isfile(wal) and os.path.getsize(wal) > 0:
        return "in use: a non-empty write-ahead log (-wal) shows another process has it open"
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(path, timeout=0.5, isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return f"in use: cannot lock it ({e})"
    return None
