"""Database backup and restore: store snapshots, validation, auto backup, CLI and dashboard."""

import gzip
import http.client
import io
import json
import logging
import os
import sqlite3
import threading
import time

import pytest

from shelly_monitor import cli
from shelly_monitor.backup import auto_backup, backup_filename, db_in_use, prune
from shelly_monitor.cloud import DeviceInfo, Sample
from shelly_monitor.config import BackupConfig, ConfigError, Home, backup_config, restore_max_mb
from shelly_monitor.poller import Maintenance
from shelly_monitor.store import SCHEMA_VERSION, RestoreError, Store, restore_from
from shelly_monitor.web import make_server


def _db(path, n=3, home="home"):
    with Store(str(path)) as st:
        for i in range(n):
            st.save_poll([DeviceInfo("d1", "M", "G2", home=home)],
                         [Sample(100 + i, "d1", 0, True, 1.0, True, home=home)], 100 + i, home=home)
    return str(path)


def _count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    finally:
        c.close()


def _gunzip(src, dst):
    with gzip.open(src, "rb") as f, open(dst, "wb") as out:
        out.write(f.read())
    return str(dst)


# --- store: backup_to ---------------------------------------------------------------------
def test_backup_to_gzip_is_valid_snapshot(tmp_path):
    db = _db(tmp_path / "a.db", 5)
    with Store(db, readonly=True) as st:
        n = st.backup_to(str(tmp_path / "b.db.gz"))
    assert n == os.path.getsize(tmp_path / "b.db.gz")
    raw = _gunzip(tmp_path / "b.db.gz", tmp_path / "b.db")
    c = sqlite3.connect(raw)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    c.close()
    assert _count(raw) == 5
    assert not (tmp_path / "b.db.gz.part").exists()


def test_backup_to_raw_and_fileobj(tmp_path):
    db = _db(tmp_path / "a.db", 2)
    with Store(db) as st:
        st.backup_to(str(tmp_path / "raw.db"), compress=False)
        buf = io.BytesIO()
        n = st.backup_to(buf)
    assert open(tmp_path / "raw.db", "rb").read(16) == b"SQLite format 3\x00"
    assert n == len(buf.getvalue()) and gzip.decompress(buf.getvalue())[:16] == b"SQLite format 3\x00"


def test_backup_ignores_uncommitted_writes(tmp_path):
    """The snapshot is consistent while another connection has an open write transaction (WAL)."""
    db = _db(tmp_path / "a.db", 2)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO samples (ts, device_id, channel, is_on, online, home) VALUES (1, 'x', 0, 1, 1, 'h')")
    try:
        with Store(db, readonly=True) as st:
            st.backup_to(str(tmp_path / "b.db"), compress=False)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert _count(str(tmp_path / "b.db")) == 2


# --- store: restore_from ------------------------------------------------------------------
def test_restore_replaces_database_and_reports_counts(tmp_path):
    db = _db(tmp_path / "live.db", 2)
    src = _db(tmp_path / "src.db", 7)
    with Store(src) as st:
        st.backup_to(str(tmp_path / "src.db.gz"))
    (tmp_path / "live.db-shm").write_bytes(b"stale")
    res = restore_from(str(tmp_path / "src.db.gz"), db)
    assert res["samples_before"] == 2 and res["samples_after"] == 7
    assert res["schema_version"] == SCHEMA_VERSION and res["migrated"] is False
    assert _count(db) == 7
    assert not (tmp_path / "live.db-shm").exists() and not (tmp_path / "live.db.restore-tmp").exists()


def test_restore_into_missing_database(tmp_path):
    src = _db(tmp_path / "src.db", 1)
    res = restore_from(src, str(tmp_path / "new.db"))
    assert res["samples_before"] is None and res["samples_after"] == 1


def test_restore_older_schema_is_migrated(tmp_path):
    src = _db(tmp_path / "src.db", 1)
    c = sqlite3.connect(src)
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()
    db = str(tmp_path / "live.db")
    res = restore_from(src, db)
    assert res["schema_version"] == 5 and res["migrated"] is True
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    c.close()


def test_restore_wal_flagged_raw_copy(tmp_path):
    """A raw copy of a WAL database (header in WAL mode) is accepted."""
    src = _db(tmp_path / "src.db", 3)
    c = sqlite3.connect(src)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    raw = tmp_path / "copy.db"
    raw.write_bytes(open(src, "rb").read())
    assert restore_from(str(raw), str(tmp_path / "live.db"))["samples_after"] == 3


def _future(tmp_path):
    p = _db(tmp_path / "future.db", 1)
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    c.commit()
    c.close()
    return p


def _corrupt(tmp_path):
    p = _db(tmp_path / "corrupt.db", 300)
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    data = bytearray(open(p, "rb").read())
    page = 4096
    for i in range(page * 2, len(data), 7):  # trash every page after the first two
        data[i] = 0xA5
    open(p, "wb").write(bytes(data))
    return p


def _foreign(tmp_path):
    p = str(tmp_path / "other.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    return p


def _garbage(tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(b"hello, I am not a database" * 100)
    return str(p)


def _bad_gzip(tmp_path):
    p = tmp_path / "bad.gz"
    p.write_bytes(b"\x1f\x8b" + b"\x00" * 50)
    return str(p)


@pytest.mark.parametrize("make,needle", [
    (_garbage, "not a SQLite database"),
    (_bad_gzip, "gzip"),
    (_foreign, "missing tables"),
    (_future, "newer"),
    (_corrupt, ""),
])
def test_restore_rejects_bad_files_and_keeps_database(tmp_path, make, needle):
    db = _db(tmp_path / "live.db", 4)
    src = make(tmp_path)
    with pytest.raises(RestoreError) as e:
        restore_from(src, db)
    assert needle in str(e.value)
    assert _count(db) == 4
    assert not (tmp_path / "live.db.restore-tmp").exists()


def test_restore_size_limit(tmp_path):
    db = _db(tmp_path / "live.db", 1)
    src = _db(tmp_path / "src.db", 50)
    with pytest.raises(RestoreError, match="limit"):
        restore_from(src, db, max_bytes=1024)
    assert _count(db) == 1


# --- config -------------------------------------------------------------------------------
def test_backup_config_defaults_and_errors():
    assert backup_config({}) == BackupConfig(None, 24.0, 14)
    assert backup_config({"SHELLY_BACKUP_DIR": " /b ", "SHELLY_BACKUP_INTERVAL_HOURS": "0",
                          "SHELLY_BACKUP_KEEP": "3"}) == BackupConfig("/b", 0.0, 3)
    for env in ({"SHELLY_BACKUP_INTERVAL_HOURS": "-1"}, {"SHELLY_BACKUP_INTERVAL_HOURS": "x"},
                {"SHELLY_BACKUP_KEEP": "0"}, {"SHELLY_BACKUP_KEEP": "1.5"}):
        with pytest.raises(ConfigError):
            backup_config(env)
    assert restore_max_mb({}) == 200 and restore_max_mb({"SHELLY_RESTORE_MAX_MB": "5"}) == 5
    with pytest.raises(ConfigError):
        restore_max_mb({"SHELLY_RESTORE_MAX_MB": "0"})


# --- automatic backup ---------------------------------------------------------------------
def test_backup_filename():
    t = time.mktime((2026, 10, 28, 13, 5, 9, 0, 0, -1))
    assert backup_filename(t) == "shelly-monitor-20261028-130509.db.gz"
    assert backup_filename(t, compress=False) == "shelly-monitor-20261028-130509.db"


def test_auto_backup_interval_meta_and_prune(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    db = _db(tmp_path / "a.db", 2)
    bdir = tmp_path / "backups"
    cfg = BackupConfig(str(bdir), 24.0, 2)
    base = time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
    with Store(db) as st:
        assert auto_backup(st, BackupConfig(None, 24.0, 2), now=base) is None
        p1 = auto_backup(st, cfg, now=base)
        assert p1 is not None and p1.is_file()
        assert auto_backup(st, cfg, now=base + 3600) is None  # interval not elapsed
        last = json.loads(st.get_meta("last_backup"))
        assert last["ts"] == int(base) and last["path"] == str(p1) and last["size"] == p1.stat().st_size
        (bdir / "notes.txt").write_text("keep me")
        for day in (1, 2, 3):
            auto_backup(st, cfg, now=base + day * 86400)
    names = sorted(os.listdir(bdir))
    assert names == ["notes.txt", backup_filename(base + 2 * 86400), backup_filename(base + 3 * 86400)]
    assert "automatic backup" in caplog.text


def test_auto_backup_errors_are_warnings(tmp_path, caplog):
    db = _db(tmp_path / "a.db", 1)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with Store(db) as st:
        assert auto_backup(st, BackupConfig(str(blocker / "sub"), 0, 3)) is None
    assert any(r.levelno == logging.WARNING and "backup" in r.getMessage() for r in caplog.records)


def test_prune_only_touches_backup_files(tmp_path):
    for n in ("shelly-monitor-20260101-000000.db.gz", "shelly-monitor-20260102-000000.db.gz",
              "shelly-monitor-20260103-000000.db", "other.db.gz"):
        (tmp_path / n).write_bytes(b"x")
    assert prune(tmp_path, 1) == 2
    assert sorted(os.listdir(tmp_path)) == ["other.db.gz", "shelly-monitor-20260103-000000.db"]


class OkCloud:
    throttle = type("T", (), {"gap": 1.1})()

    def all_status(self):
        return {"devices_status": {"aabbccddeeff": {"id": "aabbccddeeff", "code": "S3SW-001X16EU",
                                                    "switch:0": {"output": True}, "cloud": {"connected": True}}}}

    def discover_home(self):
        return None

    def device_lists(self):
        return None


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_poll_forever_writes_auto_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("SHELLY_EVENT_LOG", "0")
    monkeypatch.setenv("SHELLY_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("SHELLY_BACKUP_INTERVAL_HOURS", "0")
    stop = threading.Event()
    t = threading.Thread(target=cli.poll_forever, args=([cli.Target(Home("A", "ka", "s"), OkCloud())], 60, stop))
    t.start()
    try:
        assert _wait(lambda: (tmp_path / "bk").is_dir() and os.listdir(tmp_path / "bk"))
    finally:
        stop.set()
        t.join(5)
    assert not t.is_alive()


def test_restore_waits_for_cycle_and_poller_reopens(tmp_path, monkeypatch):
    """A restore runs between two cycles; the poller then writes into the restored database."""
    db = str(tmp_path / "p.db")
    monkeypatch.setenv("SHELLY_DB", db)
    monkeypatch.setenv("SHELLY_EVENT_LOG", "0")
    src = _db(tmp_path / "src.db", 10, home="A")
    maint = Maintenance()
    stop, cycles = threading.Event(), []
    t = threading.Thread(target=cli.poll_forever,
                         args=([cli.Target(Home("A", "ka", "s"), OkCloud())], 0.05, stop, lambda: cycles.append(1),
                               maint))
    t.start()
    try:
        assert _wait(lambda: len(cycles) >= 2)
        with maint.exclusive():
            assert maint.store is None  # the poller's connection was closed
            res = restore_from(src, db, home="A")
            n = len(cycles)
            time.sleep(0.2)
            assert len(cycles) == n  # no cycle while the restore holds the lock
        assert res["samples_after"] == 10
        assert _wait(lambda: len(cycles) >= n + 2)
    finally:
        stop.set()
        t.join(5)
    assert _count(db) > 10


# --- CLI ----------------------------------------------------------------------------------
def _main(tmp_path, *argv):
    return cli.main(["--env-file", str(tmp_path / "none"), *argv])


def test_cli_backup_default_name_and_stdout(tmp_path, monkeypatch, capsysbinary):
    db = _db(tmp_path / "a.db", 3)
    monkeypatch.setenv("SHELLY_DB", db)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(out)
    assert _main(tmp_path, "backup") == 0
    files = os.listdir(out)
    assert len(files) == 1 and files[0].startswith("shelly-monitor-") and files[0].endswith(".db.gz")
    assert _count(_gunzip(out / files[0], tmp_path / "x.db")) == 3
    capsysbinary.readouterr()
    assert _main(tmp_path, "backup", "--out", "-") == 0
    assert gzip.decompress(capsysbinary.readouterr().out)[:16] == b"SQLite format 3\x00"
    assert _main(tmp_path, "backup", "--out", str(tmp_path / "r.db"), "--no-compress") == 0
    assert _count(str(tmp_path / "r.db")) == 3


def test_cli_restore_yes_and_confirmation(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path / "live.db", 1)
    src = _db(tmp_path / "src.db", 6)
    monkeypatch.setenv("SHELLY_DB", db)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: False)
    assert _main(tmp_path, "restore", src) == 1
    assert _count(db) == 1
    assert _main(tmp_path, "restore", src, "--yes") == 0
    assert _count(db) == 6
    out = capsys.readouterr()
    assert "stop" in out.err.lower() and "6" in out.out


def test_cli_restore_refuses_bad_file(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path / "live.db", 2)
    monkeypatch.setenv("SHELLY_DB", db)
    assert _main(tmp_path, "restore", _garbage(tmp_path), "--yes") == 1
    assert "not a SQLite database" in capsys.readouterr().err and _count(db) == 2


def test_cli_restore_refuses_database_in_use(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path / "live.db", 2)
    src = _db(tmp_path / "src.db", 5)
    monkeypatch.setenv("SHELLY_DB", db)
    st = Store(db)  # another process' connection: WAL file present
    st.save_poll([], [Sample(500, "d1", 0, True, None, True)], 500)
    try:
        assert db_in_use(db) is not None
        assert _main(tmp_path, "restore", src, "--yes") == 1
        assert "in use" in capsys.readouterr().err.lower()
    finally:
        st.close()
    assert db_in_use(db) is None


def test_db_in_use_detects_lock_in_delete_mode(tmp_path):
    db = _db(tmp_path / "live.db", 1)
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("BEGIN")
    c.execute("SELECT COUNT(*) FROM samples").fetchone()  # shared lock held
    try:
        assert db_in_use(db) is not None
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert db_in_use(db) is None


# --- dashboard ----------------------------------------------------------------------------
class Srv:
    def __init__(self, db, **kw):
        self.srv = make_server(db, 60, "127.0.0.1", 0, **kw)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def served(tmp_path):
    servers = []

    def _start(db, **kw):
        s = Srv(db, **kw)
        servers.append(s)
        return s
    yield _start
    for s in servers:
        s.close()


H = {"X-Requested-With": "shelly-monitor", "Content-Type": "application/gzip"}


def test_api_backup_download(tmp_path, served):
    db = _db(tmp_path / "a.db", 4)
    s = served(db, readonly=True)
    r, data = s.req("GET", "/api/backup")
    assert r.status == 200 and r.getheader("Content-Type") == "application/gzip"
    cd = r.getheader("Content-Disposition")
    assert cd.startswith('attachment; filename="shelly-monitor-') and cd.endswith('.db.gz"')
    assert int(r.getheader("Content-Length")) == len(data)
    (tmp_path / "dl.gz").write_bytes(data)
    assert _count(_gunzip(tmp_path / "dl.gz", tmp_path / "dl.db")) == 4


def test_api_backup_missing_db_is_404(tmp_path, served):
    r, _ = served(str(tmp_path / "none.db")).req("GET", "/api/backup")
    assert r.status == 404


def test_api_restore(tmp_path, served):
    db = _db(tmp_path / "a.db", 2)
    src = _db(tmp_path / "src.db", 9)
    with Store(src) as st:
        st.backup_to(str(tmp_path / "src.db.gz"))
    body = (tmp_path / "src.db.gz").read_bytes()
    s = served(db)
    r, data = s.req("POST", "/api/restore", body, H)
    res = json.loads(data)
    assert r.status == 200 and res["samples_before"] == 2 and res["samples_after"] == 9
    assert res["schema_version"] == SCHEMA_VERSION
    assert _count(db) == 9
    r, data = s.req("GET", "/api/summary?from=1970-01-01&to=1970-01-02")
    assert r.status == 200
    assert not [f for f in os.listdir(tmp_path) if "upload" in f or "restore-tmp" in f]


def test_api_restore_rejections(tmp_path, served, monkeypatch):
    db = _db(tmp_path / "a.db", 2)
    s = served(db)
    garbage = b"definitely not a database" * 10
    r, data = s.req("POST", "/api/restore", garbage, H)
    assert r.status == 400 and "SQLite" in json.loads(data)["error"]
    r, _ = s.req("POST", "/api/restore", garbage, {"Content-Type": "application/gzip"})
    assert r.status == 403
    r, _ = s.req("POST", "/api/restore", garbage, {**H, "Content-Type": "text/plain"})
    assert r.status == 415
    monkeypatch.setenv("SHELLY_RESTORE_MAX_MB", "1")
    s2 = served(db)
    r, _ = s2.req("POST", "/api/restore", b"x" * (1024 * 1024 + 1), H)
    assert r.status == 413
    assert _count(db) == 2


def test_api_restore_requires_content_length(tmp_path, served):
    db = _db(tmp_path / "a.db", 1)
    s = served(db)
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    c.putrequest("POST", "/api/restore")
    for k, v in H.items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    assert r.status == 411
    c.close()


def test_api_restore_readonly_is_403(tmp_path, served):
    db = _db(tmp_path / "a.db", 1)
    r, _ = served(db, readonly=True).req("POST", "/api/restore", b"x" * 10, H)
    assert r.status == 403 and _count(db) == 1


def test_api_backup_and_restore_need_login(tmp_path, served):
    from shelly_monitor.auth import WebAuth
    db = _db(tmp_path / "a.db", 1)
    s = served(db, auth=WebAuth.from_env({"SHELLY_WEB_PASSWORD": "test-pass-1234"}))
    for method, path in (("GET", "/api/backup"), ("GET", "/api/backup/status"), ("POST", "/api/restore")):
        r, _ = s.req(method, path, b"x" if method == "POST" else None, H)
        assert r.status == 401, path


def test_api_backup_status(tmp_path, served, monkeypatch):
    db = _db(tmp_path / "a.db", 1)
    monkeypatch.setenv("SHELLY_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("SHELLY_BACKUP_KEEP", "5")
    with Store(db) as st:
        auto_backup(st, backup_config())
    r, data = served(db).req("GET", "/api/backup/status")
    body = json.loads(data)
    assert r.status == 200
    assert body["auto"] == {"enabled": True, "dir": str(tmp_path / "bk"), "interval_hours": 24.0, "keep": 5}
    assert body["last"]["size"] > 0 and body["last"]["path"].endswith(".db.gz")
    assert body["restore_max_mb"] == 200 and body["readonly"] is False


def test_api_restore_with_poller_maintenance(tmp_path, served):
    """The web restore takes the maintenance lock and closes the poller's connection."""
    db = _db(tmp_path / "a.db", 1)
    src = _db(tmp_path / "src.db", 3)
    maint = Maintenance()
    maint.store = Store(db, shared=True)
    s = served(db, maintenance=maint)
    r, data = s.req("POST", "/api/restore", open(src, "rb").read(), {**H, "Content-Type": "application/octet-stream"})
    assert r.status == 200 and maint.store is None and _count(db) == 3
