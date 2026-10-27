"""Schema migrations must be idempotent, transactional and safe under concurrency."""

import sqlite3
import threading
from pathlib import Path

import pytest

from shelly_monitor import cli
from shelly_monitor.store import SCHEMA_VERSION, Store

DDL = (Path(__file__).parent / "fixtures" / "schema_0_3.sql").read_text()


@pytest.fixture
def db03(tmp_path):
    path = tmp_path / "v03.db"
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    return str(path)


def snapshot(path):
    conn = sqlite3.connect(path)
    try:
        return (conn.execute("SELECT ts, home, device_id, channel, is_on, power_w, online, energy_wh FROM samples "
                             "ORDER BY home, device_id, channel, ts").fetchall(),
                conn.execute("SELECT home, device_id, channel, name, model, room, last_seen FROM devices "
                             "ORDER BY home, device_id").fetchall(),
                conn.execute("SELECT * FROM homes").fetchall(), conn.execute("SELECT * FROM meta").fetchall())
    finally:
        conn.close()


def test_migrate_0_3_twice_keeps_data(db03):
    before = snapshot(db03)
    for _ in range(2):
        with Store(db03, home="Home A") as st:
            assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert snapshot(db03) == before
    with Store(db03) as st:
        cols = {r[1]: r for r in st.conn.execute("PRAGMA table_info(samples)")}
        assert cols["is_on"][3] == 0 and {"kind", "value"} <= set(cols)
        assert {"type", "kind", "channels_count", "online"} <= {r[1] for r in st.conn.execute("PRAGMA table_info(devices)")}
        names = {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master")}
        assert {"groups", "group_members", "idx_samples_home_dev_ch_ts"} <= names and "samples_old" not in names


def test_partial_upgrade_state_is_completed(db03):
    # a previous run (or another process) already added some columns, user_version not set
    conn = sqlite3.connect(db03)
    conn.execute("ALTER TABLE devices ADD COLUMN type TEXT")
    conn.execute("ALTER TABLE devices ADD COLUMN kind TEXT")
    conn.commit()
    conn.close()
    with Store(db03) as st:
        assert {"type", "kind", "channels_count", "online"} <= {r[1] for r in st.conn.execute("PRAGMA table_info(devices)")}
        assert len(list(st.samples())) == 4


def test_concurrent_openers(db03):
    errors = []

    def open_store():
        try:
            Store(db03, home="Home A").close()
        except Exception as e:  # pragma: no cover - the failure we guard against
            errors.append(e)

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(snapshot(db03)[0]) == 4


def test_failed_upgrade_rolls_back(db03, monkeypatch):
    from shelly_monitor import store as store_mod
    before = snapshot(db03)

    def boom(conn, home):
        conn.execute("ALTER TABLE devices ADD COLUMN type TEXT")
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_mod, "_upgrade", boom)
    with pytest.raises(RuntimeError):
        Store(db03)
    conn = sqlite3.connect(db03)
    assert "type" not in {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()
    assert snapshot(db03) == before


def test_cli_devices_on_migrated_db(db03, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHELLY_DB", db03)
    env = ["--env-file", str(tmp_path / "none")]
    for _ in range(2):
        assert cli.main(env + ["devices", "--header"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("home\tdevice:channel") and "Home A\taa:0\tBoiler\tCucina" in out[1] + out[2] + out[3]


def test_new_database_gets_version(tmp_path):
    with Store(str(tmp_path / "new.db")) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
