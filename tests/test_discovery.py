"""Home name detection and home identity (key fingerprint)."""

import copy
import io
import json
import logging
import sqlite3

import pytest

from shelly_monitor import cli, cloud
from shelly_monitor.cloud import ChannelMeta, DeviceInfo, HomeIdentity, Sample, Throttle, match_unit
from shelly_monitor.config import Home, key_id
from shelly_monitor.store import Store

BG03 = {"image": "./images/home_def/app_bg_03_thumb.png"}


@pytest.fixture
def units(load_fixture):
    return load_fixture("units_info.json")["data"]


@pytest.fixture
def settings(load_fixture):
    return load_fixture("settings.json")["data"]


def with_avatar(settings, avatar):
    s = copy.deepcopy(settings)
    s["settings"]["avatar"] = avatar
    return s


def test_unique_match(units, settings):
    assert match_unit(units, settings) == HomeIdentity(100003, "Casa Monte")


def test_account_avatar_match(units, settings):
    assert match_unit(units, with_avatar(settings, "color-green")) == HomeIdentity(100001, "user@example.com")


def test_ambiguous_resolved_by_subs(units, settings, load_fixture):
    s = with_avatar(settings, BG03)
    assert match_unit(units, s) == "ambiguous"
    assert match_unit(units, s, load_fixture("sub_units.json")["data"]) == HomeIdentity(100002, "Casa Lago")
    assert match_unit(units, s, {}) == HomeIdentity(100004, "Rifugio")


def test_ambiguous_unresolved_and_no_match(units, settings):
    u = copy.deepcopy(units)
    u["own_units"][3]["subs"] = 1  # both bg_03 homes now have one share
    assert match_unit(u, with_avatar(settings, BG03), {"sub_x": {}}) == "ambiguous"
    assert match_unit(units, with_avatar(settings, {"image": "other.png"})) is None
    assert match_unit({}, settings) is None and match_unit(units, {}) is None


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_cloud(monkeypatch, responses):
    calls = []

    def urlopen(req, timeout):
        path = req.full_url.split(".cloud", 1)[1]
        calls.append(path)
        return Resp(json.dumps(responses[path]).encode())

    monkeypatch.setattr(cloud.urllib.request, "urlopen", urlopen)
    return cloud.ShellyCloud("s.shelly.cloud", "k", throttle=Throttle(0)), calls


def test_discover_home_calls(monkeypatch, load_fixture):
    responses = {"/user/get_units_info": load_fixture("units_info.json"),
                 "/user/get_settings": load_fixture("settings.json"),
                 "/user/get_sub_units": load_fixture("sub_units.json")}
    c, calls = fake_cloud(monkeypatch, responses)
    assert c.discover_home() == HomeIdentity(100003, "Casa Monte")
    assert calls == ["/user/get_units_info", "/user/get_settings"]  # no sub_units when unambiguous

    responses["/user/get_settings"] = {"isok": True, "data": with_avatar(load_fixture("settings.json")["data"], BG03)}
    c, calls = fake_cloud(monkeypatch, responses)
    assert c.discover_home() == HomeIdentity(100002, "Casa Lago")
    assert calls[-1] == "/user/get_sub_units"

    responses["/user/get_sub_units"] = {"isok": True, "data": []}  # no shares: list instead of dict
    c, _ = fake_cloud(monkeypatch, responses)
    assert c.discover_home() == HomeIdentity(100004, "Rifugio")


# --- identity, names, renames -------------------------------------------------------

class FakeCloud:
    def __init__(self, identity=None, outputs=None):
        self.identity, self.outputs = identity, outputs or {"aa": True}

    def discover_home(self):
        return self.identity

    def all_status(self):
        return {"devices_status": {d: {"id": d, "cloud": {"connected": True}, "switch:0": {"output": on}}
                                   for d, on in self.outputs.items()}}

    def device_lists(self):
        return {}


def target(key, identity=None, explicit=None, fallback="home-1"):
    return cli.Target(Home(explicit, key, "s", fallback), FakeCloud(identity))


def test_discovered_names_and_fallback(tmp_path, caplog):
    t1 = target("k1", HomeIdentity(7, "Casa Lago"))
    t2 = target("k2", None, fallback="home-2")
    t3 = target("k3", HomeIdentity(9, "Rifugio"), explicit="Baita")  # explicit config name wins
    with Store(str(tmp_path / "d.db")) as st:
        cli.poll_once([t1, t2, t3], st)
        assert [t.name for t in (t1, t2, t3)] == ["Casa Lago", "home-2", "Baita"]
        rows = {r["key_id"]: (r["name"], r["unit_id"], r["discovered_name"]) for r in st.home_rows()}
        assert rows == {key_id("k1"): ("Casa Lago", 7, "Casa Lago"), key_id("k2"): ("home-2", None, None),
                        key_id("k3"): ("Baita", 9, "Rifugio")}
        assert {r["home"] for r in st.samples()} == {"Casa Lago", "home-2", "Baita"}
    assert f"homes --name {key_id('k2')}" in caplog.text


def test_email_name_kept_and_logged(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    t = target("k1", HomeIdentity(1, "user@example.com"))
    with Store(str(tmp_path / "e.db")) as st:
        cli.poll_once([t], st)
    assert t.name == "user@example.com"
    assert any(r.levelno == logging.INFO and "e-mail" in r.getMessage() for r in caplog.records)


def test_rename_moves_samples_and_devices(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "r.db")
    t = target("k1", HomeIdentity(7, "Casa Lago"))
    with Store(db) as st:
        cli.poll_once([t], st)
        st.rename("Casa Lago", "aa", 0, "Boiler")
        # the home is renamed in the cloud: detected name changes
        t.cloud.identity = HomeIdentity(7, "Lago")
        cli.refresh_identity(t, st)
        assert t.name == "Lago"
        assert {r["home"] for r in st.samples()} == {"Lago"}
        assert st.names() == {("Lago", "aa", 0): "Boiler"}
    monkeypatch.setenv("SHELLY_DB", db)
    env = ["--env-file", str(tmp_path / "none")]
    assert cli.main(env + ["homes", "--name", key_id("k1"), "Lake house"]) == 0
    assert capsys.readouterr().out.strip() == f"{key_id('k1')} -> Lake house"
    with Store(db) as st:
        assert {r["home"] for r in st.samples()} == {"Lake house"}
        cli.refresh_identity(t, st)  # override wins over detection
        assert t.name == "Lake house"
    assert cli.main(env + ["devices", "--name", key_id("k1") + "/aa", "Pump"]) == 0
    assert cli.main(env + ["homes"]) == 0
    out = capsys.readouterr().out
    assert "Lake house/aa:0 -> Pump" in out and f"{key_id('k1')}\tLake house (manual)\t7\ts\t1\t" in out
    assert cli.main(env + ["homes", "--name", "nope", "X"]) == 1


def test_names_stay_unique(tmp_path):
    t1, t2 = target("k1", HomeIdentity(1, "Casa")), target("k2", HomeIdentity(2, "Casa"), fallback="home-2")
    with Store(str(tmp_path / "u.db")) as st:
        cli.poll_once([t1, t2], st)
    assert (t1.name, t2.name) == ("Casa", "Casa (2)")


def test_migration_from_0_2_binds_first_key(tmp_path):
    path = str(tmp_path / "v02.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE samples (ts INTEGER NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL,
            is_on INTEGER NOT NULL, power_w REAL NULL, online INTEGER NOT NULL, energy_wh REAL NULL,
            home TEXT NOT NULL);
        CREATE INDEX idx_samples_home_dev_ch_ts ON samples (home, device_id, channel, ts);
        CREATE TABLE devices (home TEXT NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL DEFAULT 0,
            name TEXT, model TEXT, gen TEXT, room_id INTEGER, room TEXT, last_seen INTEGER,
            PRIMARY KEY (home, device_id, channel));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO samples VALUES (1, 'aa', 0, 1, NULL, 1, NULL, 'Home A');
        INSERT INTO devices VALUES ('Home A', 'aa', 0, 'Boiler', 'M', 'G2+', NULL, NULL, 1);
    """)
    old.commit(); old.close()
    t1 = target("k1", HomeIdentity(1234567, "Home A"))
    t2 = target("k2", HomeIdentity(5, "Seconda"), fallback="home-2")
    with Store(path, home="Home A") as st:
        cli.prepare_homes([t1, t2], st)
        assert st.home_row(key_id("k1"))["name"] == "Home A"
        cli.poll_once([t1, t2], st)
        assert [r["home"] for r in st.samples() if r["ts"] == 1] == ["Home A"]
        assert st.names()[("Home A", "aa", 0)] == "Boiler"
        assert st.home_row(key_id("k1"))["unit_id"] == 1234567
        assert {r["name"] for r in st.home_rows()} == {"Home A", "Seconda"}


def test_api_homes_identity(tmp_path):
    import http.client
    import threading
    from shelly_monitor.web import make_server
    db = str(tmp_path / "w.db")
    t = target("k1", HomeIdentity(7, "Casa Lago"))
    with Store(db) as st:
        cli.poll_once([t], st)
    srv = make_server(db, 60, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/homes")
        (h,) = json.loads(c.getresponse().read())
        assert (h["home"], h["key_id"], h["unit_id"], h["discovered_name"]) == ("Casa Lago", key_id("k1"), 7,
                                                                                 "Casa Lago")
    finally:
        srv.shutdown()
        srv.server_close()


def test_migration_does_not_steal_fixed_names(tmp_path):
    path = str(tmp_path / "v02b.db")
    with Store(path) as st:
        st.save_poll([], [Sample(1, "aa", 0, True, None, True, home="A"),
                          Sample(1, "bb", 0, True, None, True, home="B")], 1)
    t1 = target("k1", None, explicit="B")
    t2 = target("k2", None, explicit="A", fallback="home-2")
    with Store(path, home="A") as st:
        cli.prepare_homes([t1, t2], st)
        assert (t1.name, t2.name) == ("B", "A")
        assert {(r["home"], r["device_id"]) for r in st.samples()} == {("A", "aa"), ("B", "bb")}
