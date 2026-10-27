from shelly_monitor.cloud import ChannelMeta, DeviceInfo, Sample
from shelly_monitor.store import Store


def test_insert_and_query(tmp_path):
    with Store(str(tmp_path / "t.db")) as st:
        assert st.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        st.save_poll([DeviceInfo("d1", "SHSW-1", "G1")],
                     [Sample(10, "d1", 0, True, 5.0, True), Sample(10, "d1", 1, False, None, False)], 10)
        st.save_poll([DeviceInfo("d1", "SHSW-1", "G1")], [Sample(70, "d1", 0, False, None, True)], 70)
        rows = list(st.samples())
        assert [(r["ts"], r["channel"], r["is_on"]) for r in rows] == [(10, 0, 1), (70, 0, 0), (10, 1, 0)]
        assert [r["ts"] for r in st.samples(11, 100)] == [70]
        dev = st.devices()[0]
        assert dev["last_seen"] == 70 and dev["model"] == "SHSW-1"


def test_names_per_channel_manual_wins(tmp_path):
    with Store(str(tmp_path / "t.db")) as st:
        st.save_poll([DeviceInfo("d1", "M", "G2+")],
                     [Sample(1, "d1", 0, True, None, True), Sample(1, "d1", 1, False, None, True)], 1)
        st.apply_lists("home", {("d1", 0): ChannelMeta("Cloud 0", 1, "Kitchen"),
                                ("d1", 1): ChannelMeta("Cloud 1", None, None),
                                ("ghost", 0): ChannelMeta("No samples", 2, "Attic")})
        st.rename("home", "d1", 0, "Flat A")
        st.apply_lists("home", {("d1", 0): ChannelMeta("Cloud renamed", 2, "Living"),
                                ("d1", 1): ChannelMeta("Cloud 1 renamed", None, None)})
        # every channel of the lists is registered, also never-sampled ones
        assert st.names() == {("home", "d1", 0): "Flat A", ("home", "d1", 1): "Cloud 1",
                              ("home", "ghost", 0): "No samples"}
        st.save_poll([DeviceInfo("d1", "M", "G2+")], [Sample(5, "d1", 0, True, None, True)], 5)
        assert st.names()[("home", "d1", 0)] == "Flat A"
        assert [(r["device_id"], r["channel"], r["model"], r["room"]) for r in st.devices()] == \
            [("d1", 1, "M", None), ("d1", 0, "M", "Living"), ("ghost", 0, None, "Attic")]


def test_energy_column_and_migration_from_0_1_0(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE samples (ts INTEGER NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL,
            is_on INTEGER NOT NULL, power_w REAL NULL, online INTEGER NOT NULL);
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, name TEXT, model TEXT, gen TEXT, last_seen INTEGER);
        INSERT INTO samples VALUES (1, 'd1', 0, 1, NULL, 1);
        INSERT INTO devices VALUES ('d1', 'Old name', 'M', 'G2', 1);
    """)
    old.commit(); old.close()
    with Store(path) as st:
        st.save_poll([], [Sample(2, "d1", 0, True, 3.0, True, 1234.5)], 2)
        assert [r["energy_wh"] for r in st.samples()] == [None, 1234.5]
        assert st.names() == {("home", "d1", 0): "Old name"}


def test_meta(tmp_path):
    with Store(str(tmp_path / "t.db")) as st:
        assert st.get_meta("k") is None
        st.set_meta("k", "1")
        st.set_meta("k", "2")
        assert st.get_meta("k") == "2"
