"""Multi-home configuration, polling, migration, throttle."""

import logging
import sqlite3

import pytest

from shelly_monitor import cli
from shelly_monitor.cloud import ChannelMeta, CloudError, Throttle, parse_device_lists
from shelly_monitor.config import ConfigError, cycle_warning, parse_homes, request_gap
from shelly_monitor.report import aggregate
from shelly_monitor.store import Store


# --- configuration ---------------------------------------------------------

def test_single_home_backwards_compatible():
    (h,) = parse_homes({"SHELLY_SERVER": "s1.shelly.cloud", "SHELLY_AUTH_KEY": "K"})
    assert (h.name, h.auth_key, h.server, h.fallback) == (None, "K", "s1.shelly.cloud", "home")
    (h,) = parse_homes({"SHELLY_SERVER": "s1", "SHELLY_AUTH_KEY": "SECRETKEY", "SHELLY_HOME_NAME": "Casa mare"})
    assert h.name == "Casa mare"
    assert "SECRETKEY" not in repr(h) and h.key_id in repr(h)


def test_auth_keys_and_merge_with_homes():
    import hashlib
    env = {"SHELLY_SERVER": "def.shelly.cloud",
           "SHELLY_AUTH_KEYS": " k1 ; k2@shelly-9-eu.shelly.cloud ;",
           "SHELLY_HOMES": "Montagna=k2;Lago=k3"}
    homes = parse_homes(env)
    assert [(h.name, h.auth_key, h.server, h.fallback) for h in homes] == [
        (None, "k1", "def.shelly.cloud", "home-1"),
        ("Montagna", "k2", "shelly-9-eu.shelly.cloud", "home-2"),  # explicit name wins, first server kept
        ("Lago", "k3", "def.shelly.cloud", "home-3"),
    ]
    assert homes[0].key_id == hashlib.sha256(b"k1").hexdigest()[:12]
    with pytest.raises(ConfigError, match="empty auth key"):
        parse_homes({"SHELLY_SERVER": "s", "SHELLY_AUTH_KEYS": "@s2"})
    with pytest.raises(ConfigError, match="no server"):
        parse_homes({"SHELLY_AUTH_KEYS": "k1"})


def test_shelly_homes_valid():
    homes = parse_homes({"SHELLY_SERVER": "default.shelly.cloud",
                         "SHELLY_HOMES": " Casa mare = k1 ; Montagna=k2@shelly-9-eu.shelly.cloud;"})
    assert [(h.name, h.auth_key, h.server) for h in homes] == [
        ("Casa mare", "k1", "default.shelly.cloud"), ("Montagna", "k2", "shelly-9-eu.shelly.cloud")]


@pytest.mark.parametrize("env, msg", [
    ({}, "SHELLY_AUTH_KEYS"),
    ({"SHELLY_SERVER": "s"}, "SHELLY_AUTH_KEYS"),
    ({"SHELLY_HOMES": "A=k1"}, "no server"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "A=k1;a=k2"}, "duplicate"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "A="}, "empty auth key"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "A=@s2"}, "empty auth key"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "=k1"}, "empty"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "A/B=k1"}, "'/'"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": "justakey"}, "Name=auth_key"),
    ({"SHELLY_SERVER": "s", "SHELLY_HOMES": " ; "}, "SHELLY_AUTH_KEYS"),
])
def test_shelly_homes_invalid(env, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_homes(env)


def test_request_gap_parsing():
    assert request_gap({}) == 5.0
    assert request_gap({"SHELLY_REQUEST_GAP": "1.1"}) == 1.1
    assert request_gap({"SHELLY_REQUEST_GAP": " 12 "}) == 12.0
    for bad in ("1", "0", "-3", "nan", "inf"):
        with pytest.raises(ConfigError, match=">= 1.1"):
            request_gap({"SHELLY_REQUEST_GAP": bad})
    with pytest.raises(ConfigError, match="number"):
        request_gap({"SHELLY_REQUEST_GAP": "abc"})


def test_request_gap_cli_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHELLY_SERVER", "s")
    monkeypatch.setenv("SHELLY_AUTH_KEY", "k")
    monkeypatch.setenv("SHELLY_REQUEST_GAP", "abc")
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "x.db"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--env-file", str(tmp_path / "none"), "poll"])
    assert e.value.code == 2 and "SHELLY_REQUEST_GAP" in capsys.readouterr().err


def test_cycle_warning():
    assert cycle_warning(2, 5, 60) is None
    assert "hourly" in cycle_warning(4, 10, 60)
    assert "overrun" in cycle_warning(3, 30, 60)


def test_make_targets_share_one_throttle(monkeypatch, caplog):
    monkeypatch.setenv("SHELLY_SERVER", "s")
    monkeypatch.setenv("SHELLY_HOMES", "A=k1;B=k2")
    monkeypatch.setenv("SHELLY_REQUEST_GAP", "40")
    monkeypatch.setenv("SHELLY_INTERVAL", "60")
    targets = cli.make_targets()
    assert [t.home.name for t in targets] == ["A", "B"]
    assert targets[0].cloud.throttle is targets[1].cloud.throttle and targets[0].cloud.throttle.gap == 40
    assert "exceeds SHELLY_INTERVAL" in caplog.text


# --- throttle with a fake clock ---------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.now += s


def test_throttle_respects_gap():
    fc = FakeClock()
    t = Throttle(5.0, clock=fc.clock, sleep=fc.sleep)
    starts = []
    for work in (0.5, 0.0, 7.0, 0.0):
        with t:
            starts.append(fc.now)
            fc.now += work  # duration of the request
    assert fc.sleeps == [5.0, 5.0, 5.0]  # gap measured from the end of the previous request
    ends = [s + w for s, w in zip(starts, (0.5, 0.0, 7.0, 0.0))]
    assert all(starts[i + 1] - ends[i] >= 5.0 for i in range(3))


def test_throttle_applies_to_every_call(monkeypatch):
    import io
    from shelly_monitor import cloud

    fc = FakeClock()
    t = Throttle(5.0, clock=fc.clock, sleep=fc.sleep)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cloud.urllib.request, "urlopen",
                        lambda req, timeout: Resp(b'{"isok": true, "data": {"devices_status": {}, "devices": {}}}'))
    a = cloud.ShellyCloud("s", "k1", throttle=t)
    b = cloud.ShellyCloud("s", "k2", throttle=t)
    a.all_status(); b.all_status(); a.device_lists(); b.device_lists()
    assert fc.sleeps == [5.0, 5.0, 5.0]


# --- polling -------------------------------------------------------------------

class FakeCloud:
    def __init__(self, outputs, lists=None, fail=False, identity=None):
        self.outputs, self.lists, self.fail, self.calls = outputs, lists or {}, fail, []
        self.identity = identity

    def discover_home(self):
        self.calls.append("discover")
        return self.identity

    def all_status(self):
        self.calls.append("all_status")
        if self.fail:
            raise CloudError("server error (HTTP 503)")
        return {"devices_status": {dev: {"id": dev, "cloud": {"connected": True},
                                         "switch:0": {"output": on}} for dev, on in self.outputs.items()}}

    def device_lists(self):
        self.calls.append("device_lists")
        return self.lists


def test_poll_two_homes_and_failure_isolated(tmp_path, caplog):
    a = FakeCloud({"aa": True}, {("aa", 0): ChannelMeta("Boiler", 1, "Kitchen")})
    b = FakeCloud({"aa": False, "bb": True})  # same device id in another home is a different channel
    broken = FakeCloud({}, fail=True)
    from shelly_monitor.config import Home
    targets = [cli.Target(Home("A", "ka", "s"), a), cli.Target(Home("Broken", "kx", "s"), broken),
               cli.Target(Home("B", "kb", "s"), b)]
    with Store(str(tmp_path / "m.db")) as st:
        assert cli.poll_once(targets, st) is False
        rows = [(r["home"], r["device_id"], r["is_on"]) for r in st.samples()]
        assert rows == [("A", "aa", 1), ("B", "aa", 0), ("B", "bb", 1)]
        devs = {(d["home"], d["device_id"]): (d["name"], d["room"]) for d in st.devices()}
        assert devs == {("A", "aa"): ("Boiler", "Kitchen"), ("B", "aa"): (None, None), ("B", "bb"): (None, None)}
        assert "'Broken'" in caplog.text
        assert a.calls == ["discover", "all_status", "device_lists"] == b.calls
        assert cli.poll_once([targets[0], targets[2]], st) is True
        assert a.calls[-1] == "all_status"  # lists refreshed at most hourly, per home


# --- device lists fixture --------------------------------------------------------

def test_real_all_lists_fixture(load_fixture):
    meta = parse_device_lists(load_fixture("real_all_lists.json")["data"]).channels
    assert meta[("aabbcc000001", 0)] == ChannelMeta("Boiler", 1, "Kitchen", "S3SW-001X8EU", "G3", "switch", 1)
    assert meta[("aabbcc000002", 1)] == ChannelMeta("Strip hall", 2, "Hall", "SNDC-0D4P10WW", "G2", "light", 4)
    assert meta[("aabbcc000002", 3)].room is None  # room_id -10
    assert meta[("aabbcc000004", 0)].kind == "input"
    assert (meta[("aabbcc000004", 100)].name, meta[("aabbcc000004", 100)].kind) == ("Probe", "sensor")
    assert len(meta) == 7


# --- migration ---------------------------------------------------------------------

def test_migration_from_0_1_x_assigns_home(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE samples (ts INTEGER NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL,
            is_on INTEGER NOT NULL, power_w REAL NULL, online INTEGER NOT NULL, energy_wh REAL NULL);
        CREATE INDEX idx_samples_dev_ch_ts ON samples (device_id, channel, ts);
        CREATE TABLE devices (device_id TEXT NOT NULL, channel INTEGER NOT NULL DEFAULT 0, name TEXT,
            model TEXT, gen TEXT, last_seen INTEGER, PRIMARY KEY (device_id, channel));
        INSERT INTO samples VALUES (1, 'd1', 0, 1, NULL, 1, NULL), (61, 'd1', 0, 0, NULL, 1, NULL);
        INSERT INTO devices VALUES ('d1', 0, 'Boiler', 'M', 'G2+', 61);
    """)
    old.commit(); old.close()
    with Store(path, home="Casa mare") as st:
        assert [r["home"] for r in st.samples()] == ["Casa mare", "Casa mare"]
        assert st.names() == {("Casa mare", "d1", 0): "Boiler"}
        idx = {r[1] for r in st.conn.execute("PRAGMA index_list(samples)")}
        assert "idx_samples_home_dev_ch_ts" in idx and "idx_samples_dev_ch_ts" not in idx
        (row,) = aggregate(st, None, None, "total", 120)
        assert (row.home, row.on_minutes) == ("Casa mare", 1.0)
    with Store(path, home="other") as st:  # idempotent: already migrated
        assert {r["home"] for r in st.samples()} == {"Casa mare"}


# --- reports by home / room ---------------------------------------------------------

@pytest.fixture
def two_homes(tmp_path):
    from shelly_monitor.cloud import DeviceInfo, Sample
    path = str(tmp_path / "h.db")
    with Store(path) as st:
        for ts in (0, 60, 120):
            st.save_poll([DeviceInfo("aa", "M", "G2+", "A")],
                         [Sample(1000 + ts, "aa", 0, True, None, True, home="A"),
                          Sample(1000 + ts, "aa", 1, ts < 60, None, True, home="A"),
                          Sample(1000 + ts, "bb", 0, ts < 120, None, True, home="B")], 1000 + ts)
        st.apply_lists("A", {("aa", 0): ChannelMeta("Boiler", 1, "Kitchen"),
                             ("aa", 1): ChannelMeta("Lamp", 1, "Kitchen")})
        st.apply_lists("B", {("bb", 0): ChannelMeta("Pump", None, None)})
    return path


def test_report_by_home_room_and_filter(two_homes):
    with Store(two_homes) as st:
        rows = aggregate(st, None, None, "total", 120)
        assert {(r.home, r.name, r.room, r.on_minutes) for r in rows} == {
            ("A", "Boiler", "Kitchen", 2.0), ("A", "Lamp", "Kitchen", 1.0), ("B", "Pump", None, 2.0)}
        by_home = aggregate(st, None, None, "total", 120, by="home")
        assert [(r.home, r.on_minutes, r.channels, r.device_id) for r in by_home] == [("A", 3.0, 2, None),
                                                                                       ("B", 2.0, 1, None)]
        by_room = aggregate(st, None, None, "total", 120, by="room")
        assert [(r.home, r.name, r.on_minutes) for r in by_room] == [("A", "Kitchen", 3.0), ("B", "(no room)", 2.0)]
        assert [r.name for r in aggregate(st, None, None, "total", 120, home="B")] == ["Pump"]
        assert [r.name for r in aggregate(st, None, None, "total", 120, room="Kitchen")] == ["Boiler", "Lamp"]


def test_cli_report_home_and_by(two_homes, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHELLY_DB", two_homes)
    env = ["--env-file", str(tmp_path / "none")]
    assert cli.main(env + ["report", "--by", "home", "--format", "csv"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("period,home,") and [l.split(",")[1] for l in lines[1:]] == ["A", "B"]
    assert cli.main(env + ["report", "--home", "A", "--by", "room"]) == 0
    out = capsys.readouterr().out
    assert "Kitchen" in out and "Pump" not in out
    assert cli.main(env + ["devices"]) == 0
    out = capsys.readouterr().out
    assert "A\taa:0\tBoiler\tKitchen" in out and "B\tbb:0\tPump\t-" in out
    with pytest.raises(SystemExit, match="several homes"):
        cli.main(env + ["devices", "--name", "aa", "X"])
    assert cli.main(env + ["devices", "--name", "B/bb", "Garden pump"]) == 0
    assert capsys.readouterr().out == "B/bb:0 -> Garden pump\n"
    assert cli.main(env + ["export", "--home", "B"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("ts,iso_time,home,device_id,channel,room") and len(lines) == 4
    assert all(",B,bb," in l for l in lines[1:])
