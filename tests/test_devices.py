"""0.4: full device coverage (Gen1, covers, sensors), registry, groups, device API."""

import http.client
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from shelly_monitor import cli, web
from shelly_monitor.cloud import DeviceInfo, Sample, kind_for, parse_all_status, parse_device_lists
from shelly_monitor.report import aggregate
from shelly_monitor.store import Store

TS = int(datetime(2026, 10, 27, 13, 14, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def parsed(load_fixture):
    devices, samples = parse_all_status(load_fixture("gen1_mixed_status.json")["data"], TS, home="H2", stale_after=120)
    return {d.device_id: d for d in devices}, {(s.device_id, s.channel): s for s in samples}


def test_gen1_lights_relays_and_sensors(parsed):
    devices, s = parsed
    dimmer = s[("a1a1a1000001", 0)]
    assert (dimmer.kind, dimmer.is_on, dimmer.power_w, dimmer.value) == ("light", True, 42.5, 18.0)
    assert dimmer.energy_wh == pytest.approx(6652 / 60)  # Gen1 meters.total is in watt-minutes
    assert ("a1a1a1000001", 900) not in s and ("a1a1a1000001", 0) in s  # internal tmp ignored
    strips = [s[("a1a1a1000002", i)] for i in range(4)]
    assert [(x.kind, x.is_on, x.power_w) for x in strips] == [("light", False, 0.0), ("light", True, 7.2),
                                                             ("light", False, 0.0), ("light", False, 0.0)]
    assert s[("a1a1a1000003", 0)].kind == "rgbw"
    relay = s[("a1a1a1000004", 0)]
    assert (relay.kind, relay.is_on, relay.power_w, relay.energy_wh) == ("switch", True, 1830.5, None)
    assert (s[("a1a1a1000004", 100)].kind, s[("a1a1a1000004", 100)].value) == ("sensor", 21.5)
    assert s[("a1a1a1000004", 1100)].value == 55.0
    assert s[("a1a1a1000006", 0)].kind == "light"  # bulb in white mode
    # i3: inputs only -> a device (online flag) without samples; cloud says disconnected
    assert not [k for k in s if k[0] == "a1a1a1000005"]
    assert devices["a1a1a1000005"].online is False and devices["a1a1a1000001"].online is True
    assert "1700000000009" not in devices  # cloud virtual thermostat


def test_covers_and_gen2_sensors(parsed):
    _, s = parsed
    roller = s[("a1a1a1000007", 0)]  # Gen1 roller mode: rollers[], relays ignored
    assert (roller.kind, roller.is_on, roller.value, roller.power_w) == ("cover", True, 35.0, 120.0)
    assert ("a1a1a1000007", 1) not in s
    cover = s[("b2b2b2000001", 0)]
    assert (cover.kind, cover.is_on, cover.value, cover.energy_wh) == ("cover", True, 40.0, 463.154)
    flood = s[("b2b2b2000002", 0)]
    assert (flood.kind, flood.is_on, flood.value) == ("sensor", None, 1.0)
    assert (s[("b2b2b2000003", 100)].value, s[("b2b2b2000003", 1100)].value) == (17.75, 48.2)
    assert s[("b2b2b2000003", 0)].kind == "switch"


def test_gen1_online_fallback_on_updated():
    entry = {"_updated": "2026-10-27 13:13:30", "wifi_sta": {}, "relays": [{"ison": True}]}
    data = {"devices_status": {"x": entry}}
    (d,), _ = parse_all_status(data, TS, stale_after=120)
    assert d.online is True
    entry["_updated"] = "2026-10-27 12:00:00"
    (d,), (smp,) = parse_all_status(data, TS, stale_after=120)
    assert d.online is False and smp.online is False
    (d,), _ = parse_all_status(data, TS)  # no interval known: assume reachable
    assert d.online is True


@pytest.mark.parametrize("type_, category, mode, channel, kind", [
    ("SHDM-2", "light", "white", 0, "light"), ("SHRGBW2", "light", "color", 0, "rgbw"),
    ("SHIX3-1", "inputs_reader", None, 1, "input"), ("SNSW-102P16EU", "roller", "roller", 0, "cover"),
    ("SNSW-102P16EU", "relay", "relay", 1, "switch"), ("S4SN-0071A", "sensor", "sensor", 0, "sensor"),
    ("S3SW-001X16EU", "relay", "relay", 100, "sensor"), ("SHSW-1", None, "", 0, "switch"),
    ("SHHT-1", None, None, 0, "sensor"), ("XYZ-1", None, None, 0, "unknown"),
])
def test_kind_mapping(type_, category, mode, channel, kind):
    assert kind_for(type_, category, mode, channel) == kind


@pytest.fixture
def registry(tmp_path, load_fixture):
    db = str(tmp_path / "reg.db")
    devices, samples = parse_all_status(load_fixture("gen1_mixed_status.json")["data"], TS, home="H2", stale_after=120)
    lists = parse_device_lists(load_fixture("h2_lists_small.json")["data"])
    with Store(db) as st:
        st.save_poll(devices, samples, TS, home="H2")
        st.apply_lists("H2", lists)
        st.set_online("H2", devices)
    return db, lists


def test_registry_complete_with_groups(registry):
    db, lists = registry
    with Store(db) as st:
        rows = {(r["device_id"], r["channel"]): r for r in st.devices("H2")}
        # every channel of the lists + sampled channels the lists do not know (add-on probes, roller relay mode)
        assert set(lists.channels) <= set(rows)
        extra = set(rows) - set(lists.channels)
        assert extra == {("a1a1a1000004", 100), ("a1a1a1000004", 1100), ("b2b2b2000003", 100),
                         ("b2b2b2000003", 1100), ("a1a1a1000007", 0)}
        off = rows[("c3c3c3000001", 0)]  # in the lists, not in all_status
        assert (off["online"], off["last_seen"], off["kind"], off["room"]) == (0, None, "light", None)
        assert rows[("a1a1a1000005", 2)]["kind"] == "input" and rows[("a1a1a1000005", 2)]["online"] == 0
        assert rows[("a1a1a1000002", 2)]["room"] == "Kitchen" and rows[("a1a1a1000002", 2)]["type"] == "SHRGBW2"
        assert rows[("b2b2b2000001", 0)]["kind"] == "cover"
        groups = {g["name"]: g for g in st.groups("H2")}
        assert (groups["All strips"]["members"], groups["Garage"]["members"]) == (4, 2)
        cg = st.channel_groups("H2")
        assert cg[("H2", "a1a1a1000002", 3)] == ["All strips"] and cg[("H2", "b2b2b2000001", 0)] == ["Garage"]


def test_cli_devices_and_groups(registry, monkeypatch, tmp_path, capsys):
    db, lists = registry
    monkeypatch.setenv("SHELLY_DB", db)
    env = ["--env-file", str(tmp_path / "none")]
    assert cli.main(env + ["devices", "--home", "H2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(lists.channels) + 5
    strip = next(l for l in lines if "\ta1a1a1000002:1\t" in l).split("\t")
    assert strip[2:7] == ["Strip 2", "Hall", "light", "online", "All strips"]
    assert "offline" in next(l for l in lines if "c3c3c3000001:0" in l)
    assert cli.main(env + ["groups"]) == 0
    out = capsys.readouterr().out
    assert "H2\t7\tAll strips\tlight\t4" in out and "H2\t11\tGarage\troller\t2" in out
    assert cli.main(env + ["export"]) == 0
    head, *rows = capsys.readouterr().out.splitlines()
    assert head == "ts,iso_time,home,device_id,channel,room,kind,is_on,value,power_w,online,energy_wh"
    assert any(",b2b2b2000002,0,Garage,sensor,,1.0," in r for r in rows)


def test_report_counts_only_switches_and_lights(tmp_path):
    with Store(str(tmp_path / "k.db")) as st:
        for t in (0, 60, 120):
            st.save_poll([], [Sample(TS + t, "sw", 0, True, None, True, kind="switch"),
                              Sample(TS + t, "cv", 0, True, None, True, kind="cover", value=50.0),
                              Sample(TS + t, "se", 0, None, None, True, kind="sensor", value=20.0)], TS + t)
        rows = aggregate(st, None, None, "total", 120, include_zero=True)
        assert [(r.device_id, r.on_minutes) for r in rows] == [("sw", 2.0)]
        moving = aggregate(st, None, None, "total", 120, kinds=None, device=("cv", 0))
        assert [(r.device_id, r.on_minutes) for r in moving] == [("cv", 2.0)]


def test_migration_from_0_3(tmp_path):
    path = str(tmp_path / "v03.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE samples (ts INTEGER NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL,
            is_on INTEGER NOT NULL, power_w REAL NULL, online INTEGER NOT NULL, energy_wh REAL NULL,
            home TEXT NOT NULL);
        CREATE INDEX idx_samples_home_dev_ch_ts ON samples (home, device_id, channel, ts);
        CREATE TABLE devices (home TEXT NOT NULL, device_id TEXT NOT NULL, channel INTEGER NOT NULL DEFAULT 0,
            name TEXT, model TEXT, gen TEXT, room_id INTEGER, room TEXT, last_seen INTEGER,
            PRIMARY KEY (home, device_id, channel));
        CREATE TABLE homes (key_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, server TEXT, unit_id INTEGER,
            discovered_name TEXT, name_override TEXT, updated INTEGER);
        INSERT INTO samples VALUES (1, 'aa', 0, 1, NULL, 1, NULL, 'Home A'), (61, 'aa', 0, 0, NULL, 1, NULL, 'Home A');
        INSERT INTO devices VALUES ('Home A', 'aa', 0, 'Boiler', 'M', 'G2+', NULL, NULL, 61);
    """)
    old.commit(); old.close()
    with Store(path) as st:
        cols = {r[1]: r for r in st.conn.execute("PRAGMA table_info(samples)")}
        assert {"kind", "value"} <= set(cols) and cols["is_on"][3] == 0
        assert {"type", "kind", "channels_count", "online"} <= {r[1] for r in st.conn.execute("PRAGMA table_info(devices)")}
        assert {"groups", "group_members"} <= {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master")}
        assert [(r["ts"], r["is_on"], r["kind"]) for r in st.samples()] == [(1, 1, None), (61, 0, None)]
        (row,) = aggregate(st, None, None, "total", 120)  # legacy rows count as switches
        assert row.on_minutes == 1.0
        st.save_poll([], [Sample(121, "se", 0, None, None, True, home="Home A", kind="sensor", value=19.5)], 121)
        assert [r["is_on"] for r in st.samples(device=("se", 0))] == [None]
        idx = {r[1] for r in st.conn.execute("PRAGMA index_list(samples)")}
        assert "idx_samples_home_dev_ch_ts" in idx


# --- web API ---------------------------------------------------------------------------

@pytest.fixture
def api(registry):
    db, _ = registry
    base = int(datetime(2026, 10, 1, 12, 0).timestamp())
    with Store(db) as st:  # a switch with a history: ON 2 min, OFF, gap, ON
        for t, on in ((0, True), (60, True), (120, False), (180, False), (900, True), (960, True)):
            st.save_poll([], [Sample(base + t, "b2b2b2000003", 0, on, 10.0, True, home="H2", kind="switch")], base + t)
        for t, v in ((0, 20.0), (60, 20.0), (120, 20.5)):
            st.save_poll([], [Sample(base + t, "b2b2b2000003", 100, None, None, True, home="H2", kind="sensor",
                                     value=v)], base + t)
    srv = web.make_server(db, 60, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, json.loads(body)


def test_api_devices(api):
    status, rows = get(api, "/api/devices?home=H2&from=2026-10-01&to=2026-10-31")
    assert status == 200
    by = {(r["device_id"], r["channel"]): r for r in rows}
    pump = by[("b2b2b2000003", 0)]
    assert (pump["kind"], pump["room"], pump["type"], pump["gen"], pump["online"]) == ("switch", "Garage",
                                                                                      "S3SW-001X16EU", "G3", True)
    assert pump["on_h"] == round(5 / 60, 2) and pump["state"] == "on"  # 3 min on Oct 1 + 2 min capped gap to Oct 27
    assert by[("b2b2b2000001", 0)]["state"] == "moving" and by[("b2b2b2000001", 0)]["value"] == 40.0
    assert by[("b2b2b2000002", 0)]["kind"] == "sensor" and by[("b2b2b2000002", 0)]["value"] == 1.0
    assert by[("c3c3c3000001", 0)]["online"] is False and by[("c3c3c3000001", 0)]["last_seen"] is None
    assert by[("a1a1a1000002", 0)]["groups"] == ["All strips"]
    _, strips = get(api, "/api/devices?home=H2&group=All%20strips")
    assert sorted(r["channel"] for r in strips) == [0, 1, 2, 3]
    _, found = get(api, "/api/devices?q=kitchen")
    assert {r["room"] for r in found} == {"Kitchen"}
    _, garage = get(api, "/api/devices?home=H2&room=Garage")
    assert {r["device_id"] for r in garage} >= {"b2b2b2000001", "b2b2b2000002", "b2b2b2000003"}
    _, groups = get(api, "/api/groups?home=H2")
    assert {(g["name"], g["room"], g["members"]) for g in groups} == {("All strips", "Hall", 4),
                                                                     ("Garage", "Garage", 2)}


def test_api_device_detail(api):
    status, d = get(api, "/api/device?home=H2&id=b2b2b2000003&ch=0&from=2026-10-01&to=2026-10-02")
    assert status == 200
    assert (d["name"], d["room"], d["kind"], d["groups"], d["samples"]) == ("Pump", "Garage", "switch", [], 6)
    assert [(t.get("is_on"), t.get("gap", False)) for t in d["transitions"]] == [
        (True, False), (False, False), (None, True), (True, False)]
    assert d["daily"] == [{"day": "2026-10-01", "on_h": round(3 / 60, 2), "wh": 0.5},
                          {"day": "2026-10-02", "on_h": 0.0, "wh": None}]
    _, sensor = get(api, "/api/device?home=H2&id=b2b2b2000003&ch=100&from=2026-10-01&to=2026-10-01")
    assert [t["value"] for t in sensor["transitions"]] == [20.0, 20.5]
    assert get(api, "/api/device?home=H2&id=nope&ch=0")[0] == 404
    assert get(api, "/api/device?home=H2&id=b2b2b2000003&ch=x")[0] == 400
    assert get(api, "/api/device?home=H2&id=b2b2b2000003&ch=0&from=bad")[0] == 400


def test_api_device_truncates(api, monkeypatch):
    monkeypatch.setattr(web.Dashboard, "MAX_TRANSITIONS", 3)
    _, d = get(api, "/api/device?home=H2&id=b2b2b2000003&ch=0&from=2026-10-01&to=2026-10-01")
    assert d["truncated"] is True and len(d["transitions"]) == 3 and d["transitions"][-1]["is_on"] is True
