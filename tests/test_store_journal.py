"""SHELLY_DB_JOURNAL: rollback journal (DELETE) for databases on network filesystems."""

import sqlite3

import pytest

from shelly_monitor.cloud import DeviceInfo, Sample
from shelly_monitor.config import ConfigError, db_journal
from shelly_monitor.store import Store


def _mode(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()


def test_db_journal_parsing():
    assert db_journal({}) == "wal"
    assert db_journal({"SHELLY_DB_JOURNAL": " DELETE "}) == "delete"
    assert db_journal({"SHELLY_DB_JOURNAL": "wal"}) == "wal"
    with pytest.raises(ConfigError):
        db_journal({"SHELLY_DB_JOURNAL": "truncate"})


def test_default_is_wal_with_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("SHELLY_DB_JOURNAL", raising=False)
    with Store(str(tmp_path / "w.db")) as st:
        assert st.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert st.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_delete_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLY_DB_JOURNAL", "delete")
    path = str(tmp_path / "d.db")
    with Store(path) as st:
        assert st.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert st.conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert st.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        st.save_poll([DeviceInfo("d1", "M", "G2")], [Sample(10, "d1", 0, True, None, True)], 10)
    assert _mode(path) == "delete"
    assert not (tmp_path / "d.db-wal").exists()


def test_wal_database_switches_to_delete_keeping_data(tmp_path, monkeypatch):
    path = str(tmp_path / "x.db")
    monkeypatch.delenv("SHELLY_DB_JOURNAL", raising=False)
    st = Store(path)
    st.save_poll([DeviceInfo("d1", "M", "G2")], [Sample(10, "d1", 0, True, None, True)], 10)
    assert _mode(path) == "wal"  # persistent: stored in the database header
    st.close()
    monkeypatch.setenv("SHELLY_DB_JOURNAL", "delete")
    with Store(path) as st2:
        assert st2.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert [r["ts"] for r in st2.samples()] == [10]
    assert _mode(path) == "delete"
    assert not (tmp_path / "x.db-wal").exists()


def test_readonly_store_on_delete_database(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLY_DB_JOURNAL", "delete")
    path = str(tmp_path / "r.db")
    with Store(path) as st:
        st.save_poll([DeviceInfo("d1", "M", "G2")], [Sample(10, "d1", 0, True, None, True)], 10)
    with Store(path, readonly=True) as ro:
        assert ro.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert [r["ts"] for r in ro.samples()] == [10]
    assert _mode(path) == "delete"


def test_dashboard_reads_delete_database(tmp_path, monkeypatch):
    from shelly_monitor.web import Dashboard
    monkeypatch.setenv("SHELLY_DB_JOURNAL", "delete")
    path = str(tmp_path / "dash.db")
    with Store(path) as st:
        st.save_poll([DeviceInfo("d1", "M", "G2")], [Sample(10, "d1", 0, True, None, True)], 10)
    assert Dashboard(path, 60).summary({})["samples"] == 1
