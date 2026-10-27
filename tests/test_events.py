"""Activity log ("Registro attività") from POST /statistics/event-log."""

import http.client
import io
import json
import logging
import sqlite3
import threading
import urllib.parse
from datetime import datetime

import pytest

from shelly_monitor import cli, cloud
from shelly_monitor.cloud import CloudError, Event, ShellyCloud, parse_event_log
from shelly_monitor.config import Home, cycle_warning
from shelly_monitor.store import SCHEMA_VERSION, Store
from shelly_monitor.web import make_server


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(monkeypatch, replies):
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        reply = replies[len(calls) - 1] if isinstance(replies, list) else replies
        return FakeResp(json.dumps(reply).encode())

    monkeypatch.setattr(cloud.urllib.request, "urlopen", urlopen)
    return ShellyCloud("s.shelly.cloud", "SECRET", throttle=cloud.Throttle(0)), calls


def form(req) -> dict:
    return dict(urllib.parse.parse_qsl(req.data.decode()))


# --- parser ----------------------------------------------------------------------

def test_parse_fixture(load_fixture):
    events = parse_event_log(load_fixture("activity_log.json"), "Casa")
    assert len(events) == 22
    by = {e.rowid: e for e in events}
    off = by[55]
    assert off == Event(1793104441840, "Casa", "aabbccddee01", 0, 1, False, '["aabbccddee01",0,false]', 55)
    assert by[52].is_on is True
    assert (by[22].event, by[22].is_on) == (8, None)  # brightness
    ch1 = by[38]  # tag with _1 suffix: channel 1
    assert (ch1.device_id, ch1.channel, ch1.event, ch1.is_on) == ("aabbccddee03", 1, 13, None)
    assert by[39].channel == 1 and by[39].is_on is False
    assert by[286258].event == 5 and by[278025].event == 4
    assert all(isinstance(e.ts_ms, int) for e in events)


def test_parse_skips_malformed(caplog):
    data = {"result": {
        "aa": [
            {"tag": "aa", "t": 1000, "e": 1, "p": "not json", "rowid": 1},
            {"tag": "aa", "t": 1000, "e": 1, "p": '["aa",0,"yes"]', "rowid": 2},   # turn without bool
            {"tag": "aa", "t": "x", "e": 1, "p": '["aa",0,true]', "rowid": 3},     # bad time
            {"tag": "aa", "t": 1000, "e": 1, "p": '["aa",0,true]'},                # no rowid
            {"tag": "aa", "t": 1000, "e": 1, "p": '{"a": 1}', "rowid": 5},         # not a list
            "garbage",
            {"tag": "aa", "t": 2000, "e": 1, "p": '["aa",0,true]', "rowid": 7},
        ],
        "bb": "not a list",
    }}
    events = parse_event_log(data, "h")
    assert [e.rowid for e in events] == [7]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) >= 6


@pytest.mark.parametrize("data", [None, [], {}, {"result": []}, {"result": None}])
def test_parse_empty(data):
    assert parse_event_log(data, "h") == []


# --- client ----------------------------------------------------------------------

def test_event_log_request_form(monkeypatch, load_fixture):
    client, calls = make_client(monkeypatch, load_fixture("activity_log.json"))
    data = client.event_log(["aa", "bb_1"], limit=100)
    assert "aabbccddee01" in data["result"]
    req = calls[0]
    assert req.full_url == "https://s.shelly.cloud/statistics/event-log"
    assert form(req) == {"tags[0]": "aa", "tags[1]": "bb_1", "limit": "100", "auth_key": "SECRET"}


def test_event_log_errors_raise(monkeypatch):
    client, _ = make_client(monkeypatch, {"errors": ["VALIDATION_ERRORS"], "result": None})
    with pytest.raises(CloudError):
        client.event_log(["aa"])
    client, _ = make_client(monkeypatch, {"isok": False, "errors": {"x": 1}})
    with pytest.raises(CloudError):
        client.event_log(["aa"])


def test_events_paginates_with_pointer_and_caps(monkeypatch):
    def page(rowid, pointer=None):
        out = {"errors": [], "result": {"aa": [{"tag": "aa", "t": rowid * 1000, "e": 1,
                                                 "p": '["aa",0,true]', "rowid": rowid}]}}
        if pointer:
            out["pointer"] = pointer
        return out

    client, calls = make_client(monkeypatch, [page(3, "P1"), page(2, "P2"), page(1)])
    events = client.events(["aa"], "h", limit=1)
    assert [e.rowid for e in events] == [3, 2, 1]
    assert form(calls[1]) == {"pointer": "P1", "limit": "1", "auth_key": "SECRET"}  # no tags with a pointer
    assert len(calls) == 3

    client, calls = make_client(monkeypatch, [page(i, f"P{i}") for i in range(10, 0, -1)])
    assert len(client.events(["aa"], "h", limit=1, max_pages=5)) == 5
    assert len(calls) == 5


def test_events_without_tags_makes_no_request(monkeypatch):
    client, calls = make_client(monkeypatch, {})
    assert client.events([], "h") == []
    assert calls == []


def test_event_log_endpoint_is_allowlisted():
    assert "/statistics/event-log" in cloud.READ_ONLY_ENDPOINTS


# --- store -----------------------------------------------------------------------

def ev(rowid, ts_ms, is_on=True, event=1, dev="aa", ch=0, home="h"):
    return Event(ts_ms, home, dev, ch, event, is_on if event == 1 else None, f'["{dev}",{ch},true]', rowid)


def test_save_events_idempotent_and_query(tmp_path):
    with Store(str(tmp_path / "e.db")) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        evs = [ev(1, 1_000_500), ev(2, 2_000_000, False), ev(3, 3_000_000, event=13), ev(4, 4_000_000, ch=1)]
        assert st.save_events(evs) == 4
        assert st.save_events(evs + [ev(5, 5_000_000)]) == 1
        rows = st.events_for("h", "aa", 0)
        assert [r["rowid"] for r in rows] == [5, 3, 2, 1]  # newest first
        assert rows[-1]["ts_ms"] == 1_000_500 and rows[-1]["is_on"] == 1 and rows[1]["is_on"] is None
        assert [r["rowid"] for r in st.events_for("h", "aa", 0, lo=2000, hi=4000)] == [3, 2]  # seconds, hi exclusive
        assert [r["rowid"] for r in st.events_for("h", "aa", 0, limit=2)] == [5, 3]
        assert st.count_events("h", "aa", 0) == 4 and st.count_events("h", "aa", 0, lo=2000, hi=4000) == 2
        # same cloud rowid in another home is a different event
        assert st.save_events([ev(1, 1_000_000, home="other")]) == 1


def test_events_follow_home_rename(tmp_path):
    with Store(str(tmp_path / "r.db")) as st:
        st.sync_home("k1", "s", explicit="Old")
        st.save_events([ev(1, 1000, home="Old")])
        st.sync_home("k1", "s", explicit="New")
        assert st.count_events("New", "aa", 0) == 1 and st.count_events("Old", "aa", 0) == 0


def test_readonly_store_on_old_db_returns_nothing(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    with Store(str(path), readonly=True) as st:
        assert st.events_for("h", "aa", 0) == [] and st.count_events("h", "aa", 0) == 0


# --- poller ----------------------------------------------------------------------

class FakeCloud:
    def __init__(self, events=None, fail_events=False):
        self.events_out, self.fail_events, self.tag_calls = events or [], fail_events, []

    def discover_home(self):
        return None

    def all_status(self):
        return {"devices_status": {"aa": {"id": "aa", "switch:0": {"output": True}, "switch:1": {"output": False}}}}

    def device_lists(self):
        return cloud.parse_device_lists({"devices": {"bb": {"id": "bb", "name": "B"},
                                                     "cc_100": {"id": "cc_100", "channel": 100}}})

    def events(self, tags, home):
        self.tag_calls.append(sorted(tags))
        if self.fail_events:
            raise CloudError("event-log: HTTP 500")
        return [Event(e.ts_ms, home, e.device_id, e.channel, e.event, e.is_on, e.payload, e.rowid)
                for e in self.events_out]


def test_poll_stores_events(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("SHELLY_EVENT_LOG", raising=False)
    caplog.set_level(logging.INFO)
    fc = FakeCloud([ev(10, 1000), ev(11, 2000, False)])
    t = cli.Target(Home("A", "ka", "s"), fc)
    with Store(str(tmp_path / "p.db")) as st:
        assert cli.poll_once([t], st) is True
        # tags: sampled channels + registered channels, peripherals (channel >= 100) excluded
        assert fc.tag_calls == [["aa", "aa_1", "bb"]]
        assert st.count_events("A", "aa", 0) == 2
        assert "2 new activity log events" in caplog.text
        caplog.clear()
        assert cli.poll_once([t], st) is True
        assert "new activity log events" not in caplog.text  # nothing new: no INFO line


def test_poll_event_log_failure_does_not_stop_cycle(tmp_path, caplog):
    fc = FakeCloud(fail_events=True)
    with Store(str(tmp_path / "f.db")) as st:
        assert cli.poll_once([cli.Target(Home("A", "ka", "s"), fc)], st) is True
        assert len(list(st.samples())) == 2
    assert any(r.levelno == logging.WARNING and "activity log" in r.getMessage() for r in caplog.records)


def test_poll_event_log_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLY_EVENT_LOG", "0")
    fc = FakeCloud([ev(10, 1000)])
    with Store(str(tmp_path / "d.db")) as st:
        cli.poll_once([cli.Target(Home("A", "ka", "s"), fc)], st)
        assert fc.tag_calls == [] and st.count_events("A", "aa", 0) == 0


def test_cycle_warning_counts_event_log_request():
    assert "cycles will overrun" not in cycle_warning(4, 10, 60)
    assert "cycles will overrun" in cycle_warning(4, 10, 60, per_cycle=2)


# --- CLI -------------------------------------------------------------------------

def test_cli_events(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "c.db")
    monkeypatch.setenv("SHELLY_DB", db)
    ts = int(datetime(2026, 10, 26, 14, 31, 0).timestamp() * 1000) + 840
    with Store(db) as st:
        st.save_events([ev(1, ts, False, home="A"), ev(2, ts - 5000, event=13, home="A"),
                        ev(3, ts, dev="zz", home="A")])
    env = str(tmp_path / "none.env")
    assert cli.main(["--env-file", env, "events", "--home", "A", "--device", "aa",
                     "--from", "2026-10-26", "--to", "2026-10-26"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("time")
    assert "2026-10-26 14:31:00" in out[1] and "OFF" in out[1]
    assert "input" in out[2] and len(out) == 3


# --- dashboard -------------------------------------------------------------------

BASE = int(datetime(2026, 10, 1, 12, 0).timestamp())


@pytest.fixture
def server(tmp_path):
    db = str(tmp_path / "w.db")
    with Store(db) as st:
        st.save_poll([cloud.DeviceInfo("aa", "S3SW", "G2+")], [cloud.Sample(BASE, "aa", 0, True, None, True)], BASE)
        st.save_events([
            Event(BASE * 1000 + 1500, "home", "aa", 0, 1, True, '["aa",0,true]', 1),
            Event(BASE * 1000 + 900, "home", "aa", 0, 13, None, '["aa",0,"S","v_eve"]', 2),
            Event(BASE * 1000 + 3000, "home", "aa", 0, 8, None, '["aa",0,15]', 3),
            Event(BASE * 1000 + 4000, "home", "aa", 0, 1, False, '["aa",0,false]', 4),
            Event(BASE * 1000 + 5000, "home", "aa", 0, 13, None, '["=cmd",0,"=HYPERLINK()","x"]', 5),
        ])
    srv = make_server(db, 60, "127.0.0.1", 0)
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
    return r, body


Q = "home=home&id=aa&ch=0&from=2026-10-01&to=2026-10-31"


def test_api_device_events(server):
    r, body = get(server, "/api/device/events?" + Q)
    assert r.status == 200
    d = json.loads(body)
    assert d["total"] == 5 and d["truncated"] is False
    rows = d["events"]
    assert [x["rowid"] for x in rows] == [5, 4, 3, 1, 2]
    assert rows[1] == {"ts_ms": BASE * 1000 + 4000, "event": 1, "kind": "turn", "is_on": False, "arg": False,
                       "source": None, "rowid": 4}
    assert rows[2]["kind"] == "brightness" and rows[2]["arg"] == 15
    assert rows[4]["kind"] == "input" and rows[4]["arg"] == "S" and rows[4]["source"] == "v_eve"


@pytest.mark.parametrize("q", ["home=home&id=aa", "home=home&id=aa&ch=x", Q.replace("2026-10-31", "2030-01-01")])
def test_api_device_events_bad_request(server, q):
    assert get(server, "/api/device/events?" + q)[0].status == 400


def test_api_device_events_unknown_device(server):
    assert get(server, "/api/device/events?home=home&id=zz&ch=0")[0].status == 404


def test_device_events_csv(server):
    r, body = get(server, "/api/device-events.csv?" + Q)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/csv")
    assert "attachment" in r.getheader("Content-Disposition")
    lines = body.decode().splitlines()
    assert lines[0] == "ts_ms,iso_time,home,device_id,channel,event,kind,is_on,arg,source,payload"
    assert len(lines) == 6
    assert "'=HYPERLINK()" in lines[1]
    assert all(",=" not in line for line in lines[1:])


def test_index_has_activity_log_section():
    from shelly_monitor.web import _index_html
    html = _index_html().decode()
    assert 'id="dv-ev"' in html and "Registro attività" in html and "/api/device/events?" in html
    assert "innerHTML" not in html


def test_migration_from_schema_5_adds_events(tmp_path):
    path = str(tmp_path / "v5.db")
    with Store(path) as st:
        st.conn.execute("DROP TABLE events")
        st.conn.execute("PRAGMA user_version = 5")
        st.conn.commit()
    with Store(path) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        assert st.save_events([ev(1, 1000)]) == 1
