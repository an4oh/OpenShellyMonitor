"""0.6: custom groups (store, report by group, HTTP API with write guards, CLI)."""

import http.client
import json
import threading

import pytest

from shelly_monitor import cli
from shelly_monitor.cloud import Sample
from shelly_monitor.config import key_id
from shelly_monitor.report import aggregate
from shelly_monitor.store import SCHEMA_VERSION, Store
from shelly_monitor.web import make_server

T0 = 1_790_000_000


@pytest.fixture
def two_homes(tmp_path):
    """Two homes bound to keys; A/aa:0 ON 2 min, A/aa:1 ON 1 min, B/bb:0 ON 2 min, B/cc:0 never ON."""
    db = str(tmp_path / "g.db")
    with Store(db) as st:
        st.sync_home(key_id("ka"), "s", explicit="Casa A")
        st.sync_home(key_id("kb"), "s", explicit="Casa B")
        for t in (0, 60, 120):
            st.save_poll([], [Sample(T0 + t, "aa", 0, True, 100.0, True, home="Casa A"),
                              Sample(T0 + t, "aa", 1, t < 60, None, True, home="Casa A"),
                              Sample(T0 + t, "bb", 0, True, None, True, home="Casa B"),
                              Sample(T0 + t, "cc", 0, False, None, True, home="Casa B")], T0 + t)
    return db


def test_store_crud_and_cross_home_aggregation(two_homes):
    with Store(two_homes) as st:
        assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 5
        gid = st.save_custom_group("Appartamento A", "#38bdf8")
        st.set_custom_group_members(gid, [(key_id("ka"), "aa", 0), ("Casa B", "bb", 0), ("Casa B", "cc", 0)])
        other = st.save_custom_group("Luci")
        st.set_custom_group_members(other, [(key_id("ka"), "aa", 0), (key_id("ka"), "aa", 1)])
        rows = {r.name: r for r in aggregate(st, None, None, "total", 120, include_zero=True, by="group")}
        assert (rows["Appartamento A"].on_minutes, rows["Appartamento A"].channels) == (4.0, 3)
        assert (rows["Luci"].on_minutes, rows["Luci"].channels) == (3.0, 2)  # aa:0 counted in both groups
        assert rows["Appartamento A"].wh == pytest.approx(100 * 120 / 3600, abs=0.01)
        with pytest.raises(ValueError, match="already exists"):
            st.save_custom_group("Luci")
        with pytest.raises(ValueError, match="1-60"):
            st.save_custom_group(" " * 3)
        with pytest.raises(ValueError, match="unknown channel"):
            st.set_custom_group_members(gid, [("Casa A", "zz", 0)])
        with pytest.raises(ValueError, match="unknown home"):
            st.set_custom_group_members(gid, [("Nowhere", "aa", 0)])
        # members follow a home rename (identity = key_id)
        st.set_home_override(key_id("kb"), "Casa Monte")
        g = st.custom_group("Appartamento A")
        assert {m["home"] for m in g["members"]} == {"Casa A", "Casa Monte"}
        st.delete_custom_group(other)
        assert [g["name"] for g in st.custom_groups()] == ["Appartamento A"]
        assert st.conn.execute("SELECT COUNT(*) FROM custom_group_members WHERE group_id = ?", (other,)).fetchone()[0] == 0


@pytest.fixture
def server(two_homes, request):
    readonly = getattr(request, "param", False)
    srv = make_server(two_homes, 60, "127.0.0.1", 0, readonly=readonly)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"X-Requested-With": "shelly-monitor", "Content-Type": "application/json"}
    h.update(headers or {})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    c.request(method, path, body=data, headers={k: v for k, v in h.items() if v is not None})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, json.loads(out) if out else None


Q = "from=2026-09-01&to=2026-09-30"  # T0 is 2026-09-21


def test_api_crud(server):
    st, g = call(server, "POST", "/api/custom-groups", {"name": "Appartamento A"})
    assert st == 200 and g["name"] == "Appartamento A" and g["members"] == []
    st, g = call(server, "PUT", f"/api/custom-groups/{g['id']}/members",
                 {"members": [{"key_id": key_id("ka"), "device_id": "aa", "channel": 0},
                              {"home": "Casa B", "device_id": "bb", "channel": 0}]})
    assert st == 200 and len(g["members"]) == 2
    st, groups = call(server, "GET", "/api/custom-groups?" + Q)
    assert st == 200 and groups[0]["on_h"] == round(4 / 60, 2) and groups[0]["members"][1]["home"] == "Casa B"
    st, g2 = call(server, "POST", "/api/custom-groups", {"id": g["id"], "name": "Flat A", "color": "#a3e635"})
    assert st == 200 and (g2["name"], g2["color"]) == ("Flat A", "#a3e635")
    # the group filters every view
    st, s = call(server, "GET", f"/api/summary?{Q}&grp=c:{g['id']}")
    assert (s["channels"], s["on_hours"], s["samples"]) == (2, round(4 / 60, 2), 6)
    st, rows = call(server, "GET", f"/api/report?{Q}&grp=c:{g['id']}")
    assert {(r["home"], r["device_id"]) for r in rows} == {("Casa A", "aa"), ("Casa B", "bb")}
    assert all(r["last_ts"] == T0 + 120 for r in rows)
    st, by = call(server, "GET", f"/api/report?{Q}&by=group")
    assert [(r["name"], r["channels"]) for r in by] == [("Flat A", 2)]
    st, d = call(server, "GET", "/api/device?home=Casa%20B&id=bb&ch=0&" + Q)
    assert d["custom_groups"] == ["Flat A"]
    assert call(server, "GET", f"/api/summary?{Q}&grp=c:999")[0] == 404
    assert call(server, "GET", f"/api/summary?{Q}&grp=x")[0] == 400
    st, _ = call(server, "DELETE", f"/api/custom-groups/{g['id']}")
    assert st == 200 and call(server, "GET", "/api/custom-groups")[1] == []
    assert call(server, "DELETE", f"/api/custom-groups/{g['id']}")[0] == 404


def test_api_validation_and_guards(server):
    assert call(server, "POST", "/api/custom-groups", {"name": ""})[0] == 400
    assert call(server, "POST", "/api/custom-groups", {"name": "x" * 61})[0] == 400
    assert call(server, "POST", "/api/custom-groups", {"name": "ok", "color": "red"})[0] == 400
    assert call(server, "POST", "/api/custom-groups", b"{not json")[0] == 400
    assert call(server, "POST", "/api/custom-groups", {"name": "A"}, {"X-Requested-With": None})[0] == 403
    assert call(server, "POST", "/api/custom-groups", {"name": "A"}, {"Content-Type": "text/plain"})[0] == 415
    assert call(server, "POST", "/api/custom-groups", b"name=A",
                {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    st, g = call(server, "POST", "/api/custom-groups", {"name": "A"})
    assert call(server, "POST", "/api/custom-groups", {"name": "A"})[0] == 400  # duplicate
    assert call(server, "PUT", f"/api/custom-groups/{g['id']}/members",
                {"members": [{"home": "Casa A", "device_id": "nope", "channel": 0}]})[0] == 400
    assert call(server, "PUT", f"/api/custom-groups/{g['id']}/members", {"members": [{"home": "Casa A"}]})[0] == 400
    assert call(server, "PUT", "/api/custom-groups/999/members", {"members": []})[0] == 404
    assert call(server, "PUT", f"/api/custom-groups/{g['id']}", {"name": "x"})[0] == 405
    assert call(server, "POST", "/api/other", {"x": 1})[0] == 404
    assert call(server, "GET", "/api/config")[1]["readonly"] is False


@pytest.mark.parametrize("server", [True], indirect=True)
def test_api_readonly(server):
    assert call(server, "POST", "/api/custom-groups", {"name": "A"})[0] == 403
    assert call(server, "DELETE", "/api/custom-groups/1")[0] == 403
    assert call(server, "GET", "/api/config")[1]["readonly"] is True
    assert call(server, "GET", "/api/custom-groups")[0] == 200


def test_readonly_from_env(two_homes, monkeypatch):
    monkeypatch.setenv("SHELLY_WEB_READONLY", "1")
    srv = make_server(two_homes, 60, "127.0.0.1", 0)
    try:
        assert srv.RequestHandlerClass.groups.dash.readonly is True
    finally:
        srv.server_close()


def test_cli_custom_groups(two_homes, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHELLY_DB", two_homes)
    env = ["--env-file", str(tmp_path / "none")]
    assert cli.main(env + ["groups", "--create", "Appartamento A"]) == 0
    assert cli.main(env + ["groups", "--add", "Appartamento A", "Casa A/aa:0", f"{key_id('kb')}/bb", "Casa B/cc"]) == 0
    assert cli.main(env + ["groups", "--remove", "Appartamento A", "Casa B/cc"]) == 0
    assert cli.main(env + ["groups"]) == 0
    out = capsys.readouterr().out
    assert "Appartamento A: 3 channel(s)" in out and "Appartamento A: 2 channel(s)" in out
    assert "custom\tCasa A,Casa B\t1\tAppartamento A\t-\t2" in out
    assert cli.main(env + ["report", "--by", "group", "--group", "total", "--format", "csv"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("total,,,Appartamento A,,,4.0,")
    assert cli.main(env + ["groups", "--add", "Nope", "Casa A/aa"]) == 1
    assert cli.main(env + ["groups", "--add", "Appartamento A", "Casa A/zz"]) == 2
    assert cli.main(env + ["groups", "--delete", "Appartamento A"]) == 0
    capsys.readouterr()
    pdf = tmp_path / "r.pdf"
    assert cli.main(env + ["groups", "--create", "G"]) == 0
    assert cli.main(env + ["groups", "--add", "G", "Casa A/aa"]) == 0
    assert cli.main(env + ["report", "--format", "pdf", "--out", str(pdf)]) == 0
    import re, zlib
    text = b"".join(zlib.decompress(x) for x in re.findall(rb"stream\n(.*?)\nendstream", pdf.read_bytes(), re.S))
    assert b"(Ore per gruppo)" in text


def test_migration_to_5_is_idempotent(tmp_path):
    import sqlite3
    path = str(tmp_path / "v4.db")
    with Store(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE custom_groups")
    conn.execute("DROP TABLE custom_group_members")
    conn.execute("PRAGMA user_version = 4")
    conn.commit(); conn.close()
    for _ in range(2):
        with Store(path) as st:
            assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert st.custom_groups() == []
