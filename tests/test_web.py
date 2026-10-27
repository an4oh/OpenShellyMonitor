import http.client
import json
import threading
from datetime import datetime

import pytest

from shelly_monitor.cloud import DeviceInfo, Sample
from shelly_monitor.store import Store
from shelly_monitor.web import make_server

BASE = int(datetime(2026, 10, 1, 12, 0).timestamp())


@pytest.fixture
def server(tmp_path):
    db = str(tmp_path / "w.db")
    with Store(db) as st:
        st.save_poll([DeviceInfo("aa", "S3SW-001X8EU", "G2+"), DeviceInfo("bb", "SNDC-0D4P10WW", "G2+")], [
            Sample(BASE, "aa", 0, True, 600.0, True),
            Sample(BASE, "bb", 0, True, 0.0, True, 100.0),
        ], BASE)
        st.save_poll([], [
            Sample(BASE + 60, "aa", 0, True, 600.0, True),
            Sample(BASE + 60, "bb", 0, False, 0.0, False, 104.0),
        ], BASE + 60)
        st.save_poll([DeviceInfo("cc", "S3SW-001X8EU", "G2+")], [
            Sample(BASE, "cc", 0, False, None, True), Sample(BASE + 60, "cc", 0, False, None, True),
        ], BASE + 60)
        st.rename("home", "aa", 0, "Boiler A")
    srv = make_server(db, 60, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    yield srv.server_address[1], db
    srv.shutdown()
    srv.server_close()


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


Q = "from=2026-10-01&to=2026-10-31"


def test_index(server):
    r, body = get(server[0], "/")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert r.getheader("Cache-Control") == "no-store"
    assert b"shelly-monitor" in body and b"fonts.googleapis" not in body and b"<script src" not in body


def test_summary(server):
    r, body = get(server[0], "/api/summary?" + Q)
    s = json.loads(body)
    assert r.status == 200 and r.getheader("Cache-Control") == "no-store"
    assert s["on_hours"] == round(2 / 60, 2)  # aa: 1 min, bb: 1 min
    assert s["wh"] == 14.0  # aa estimate 600*60/3600=10, bb counter 104-100=4
    assert (s["wh_counter_channels"], s["wh_estimate_channels"]) == (1, 1)
    assert (s["channels"], s["channels_on"], s["devices"], s["devices_offline"]) == (3, 1, 3, 1)
    assert s["last_sample_ts"] == BASE + 60 and s["samples"] == 6 and s["interval_s"] == 60
    assert s["days"] == 31


def test_report(server):
    r, body = get(server[0], "/api/report?" + Q + "&group=total")
    rows = {x["device_id"]: x for x in json.loads(body)}
    a, b = rows["aa"], rows["bb"]
    assert (a["name"], a["model"], a["on_min"], a["wh"], a["wh_source"], a["state"], a["online"]) == \
        ("Boiler A", "S3SW-001X8EU", 1.0, 10.0, "estimate", "on", True)
    assert (b["name"], b["wh"], b["wh_source"], b["state"], b["online"]) == ("bb", 4.0, "counter", "off", False)
    c = rows["cc"]  # never ON: listed with 0 minutes
    assert (c["on_min"], c["on_h"], c["wh"], c["state"], c["model"]) == (0.0, 0.0, None, "off", "S3SW-001X8EU")
    r, body = get(server[0], "/api/report?" + Q + "&group=day")
    day_rows = json.loads(body)
    assert {x["period"] for x in day_rows} == {"2026-10-01"}
    assert {x["device_id"] for x in day_rows} == {"aa", "bb", "cc"}


def test_daily_fills_days(server):
    r, body = get(server[0], "/api/daily?from=2026-10-01&to=2026-10-03")
    assert json.loads(body) == [
        {"date": "2026-10-01", "on_h": round(2 / 60, 2)},
        {"date": "2026-10-02", "on_h": 0.0},
        {"date": "2026-10-03", "on_h": 0.0},
    ]


def test_export_csv(server):
    r, body = get(server[0], "/api/export.csv?" + Q)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/csv")
    assert r.getheader("Content-Disposition").startswith("attachment;")
    lines = body.decode().splitlines()
    assert lines[0].startswith("ts,iso_time,home,device_id") and len(lines) == 7
    r, body = get(server[0], "/api/export.csv?from=2026-11-01&to=2026-11-02")
    assert len(body.decode().splitlines()) == 1


def test_devices(server):
    r, body = get(server[0], "/api/devices")
    names = {(d["device_id"], d["channel"]): d["name"] for d in json.loads(body)}
    assert names == {("aa", 0): "Boiler A", ("bb", 0): None, ("cc", 0): None}


@pytest.mark.parametrize("path", [
    "/api/summary?from=2026-13-01&to=2026-10-31",
    "/api/report?from=abc",
    "/api/daily?from=2026-10-31&to=2026-10-01",
    "/api/report?" + Q + "&group=week",
    "/api/export.csv?to=31/10/2026",
])
def test_bad_request(server, path):
    r, body = get(server[0], path)
    assert r.status == 400 and "error" in json.loads(body)


@pytest.mark.parametrize("path", ["/nope", "/api/", "/../pyproject.toml", "/web/index.html", "/%2e%2e/etc/passwd"])
def test_not_found(server, path):
    r, _ = get(server[0], path)
    assert r.status == 404


def test_read_only_and_empty_db(tmp_path):
    srv = make_server(str(tmp_path / "missing.db"), 60, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert json.loads(get(port, "/api/report?" + Q)[1]) == []
        assert json.loads(get(port, "/api/summary?" + Q)[1])["samples"] == 0
        assert not (tmp_path / "missing.db").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_cli_flags(monkeypatch, tmp_path):
    from shelly_monitor import cli
    called = {}
    monkeypatch.setattr("shelly_monitor.web.serve",
                        lambda db, iv, host, port, **kw: called.update(host=host, port=port) or 0)
    monkeypatch.setenv("SHELLY_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("SHELLY_HTTP_PORT", "9000")
    env = str(tmp_path / "none")
    assert cli.main(["--env-file", env, "serve"]) == 0 and called == {"host": "0.0.0.0", "port": 9000}
    assert cli.main(["--env-file", env, "serve", "--host", "127.0.0.1", "--port", "8000"]) == 0
    assert called == {"host": "127.0.0.1", "port": 8000}


@pytest.fixture
def homes_server(tmp_path):
    from shelly_monitor.cloud import ChannelMeta
    db = str(tmp_path / "h.db")
    with Store(db) as st:
        for t in (0, 60, 120):
            st.save_poll([DeviceInfo("aa", "S3SW-001X8EU", "G2+", "Casa mare"), DeviceInfo("bb", "M", "G2+", "Monte")],
                         [Sample(BASE + t, "aa", 0, True, None, True, home="Casa mare"),
                          Sample(BASE + t, "aa", 1, False, None, True, home="Casa mare"),
                          Sample(BASE + t, "bb", 0, t < 60, None, True, home="Monte")], BASE + t)
        st.apply_lists("Casa mare", {("aa", 0): ChannelMeta("Boiler", 1, "Cucina"),
                                     ("aa", 1): ChannelMeta("Luce", 2, "Bagno")})
    srv = make_server(db, 60, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_api_homes(homes_server):
    r, body = get(homes_server, "/api/homes")
    assert r.status == 200
    homes = json.loads(body)
    assert [(h["home"], h["channels"], h["rooms"], h["room_count"]) for h in homes] == [
        ("Casa mare", 2, ["Bagno", "Cucina"], 2), ("Monte", 1, [], 0)]
    assert all({"key_id", "unit_id", "discovered_name", "last_seen"} <= set(h) for h in homes)
    assert all("server" not in h for h in homes)


def test_api_home_filter_and_by(homes_server):
    from urllib.parse import quote
    rows = json.loads(get(homes_server, "/api/report?" + Q + "&home=" + quote("Casa mare"))[1])
    assert {(x["home"], x["name"], x["room"], x["on_min"]) for x in rows} == {
        ("Casa mare", "Boiler", "Cucina", 2.0), ("Casa mare", "Luce", "Bagno", 0.0)}
    s = json.loads(get(homes_server, "/api/summary?" + Q + "&home=Monte")[1])
    assert (s["channels"], s["on_hours"], s["samples"], s["home"]) == (1, round(1 / 60, 2), 3, "Monte")
    s = json.loads(get(homes_server, "/api/summary?" + Q)[1])
    assert (s["channels"], s["homes"]) == (3, 2)
    by_home = json.loads(get(homes_server, "/api/report?" + Q + "&by=home")[1])
    assert [(x["home"], x["on_min"], x["channels"]) for x in by_home] == [("Casa mare", 2.0, 2), ("Monte", 1.0, 1)]
    assert "device_id" not in by_home[0]
    by_room = json.loads(get(homes_server, "/api/report?" + Q + "&by=room&home=" + quote("Casa mare"))[1])
    assert [(x["room"], x["on_min"]) for x in by_room] == [("Bagno", 0.0), ("Cucina", 2.0)]
    room = json.loads(get(homes_server, "/api/daily?from=2026-10-01&to=2026-10-01&room=Cucina")[1])
    assert room == [{"date": "2026-10-01", "on_h": round(2 / 60, 2)}]
    csv_lines = get(homes_server, "/api/export.csv?" + Q + "&home=Monte")[1].decode().splitlines()
    assert len(csv_lines) == 4 and all(",Monte,bb," in l for l in csv_lines[1:])
    assert get(homes_server, "/api/report?" + Q + "&by=floor")[0].status == 400
    assert json.loads(get(homes_server, "/api/report?" + Q + "&home=Nowhere")[1]) == []


def test_dashboard_has_no_group_toggle(server):
    _, body = get(server[0], "/")
    html = body.decode()
    assert 'id="group-seg"' not in html and "data-group=" not in html and '"Periodo"' not in html
    assert "/api/report?\" + q + \"&group=total\"" in html


def test_report_default_group_is_total(server):
    r, body = get(server[0], "/api/report?" + Q)
    assert {x["period"] for x in json.loads(body)} == {"total"}
