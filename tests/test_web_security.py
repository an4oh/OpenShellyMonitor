"""0.6.1: security hardening of the web dashboard (audit 2026-10-27: A1, B1, M2, M1, B2, B3)."""

import base64
import csv
import hashlib
import http.client
import io
import json
import re
import socket
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from shelly_monitor import web
from shelly_monitor.cloud import ChannelMeta, DeviceInfo, Sample
from shelly_monitor.config import key_id
from shelly_monitor.report import _safe_cell
from shelly_monitor.store import Store
from shelly_monitor.web import make_server

BASE = int(datetime(2026, 10, 1, 12, 0).timestamp())
Q = "from=2026-10-01&to=2026-10-31"
EVIL = "=cmd|' /C calc'!A0"
EVIL_HOME = "@casa"
EVIL_ROOM = "+stanza"
EVIL_GROUP = "-gruppo"
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def _start(db):
    srv = make_server(db, 60, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


def get(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


@pytest.fixture
def evil(tmp_path):
    """One home whose home/room/device/custom-group names are spreadsheet formulas."""
    db = str(tmp_path / "e.db")
    with Store(db) as st:
        st.sync_home(key_id("k"), "s", explicit=EVIL_HOME)
        for t in (0, 60, 120):
            st.save_poll([DeviceInfo("aa", "S3SW-001X8EU", "G2+", EVIL_HOME)],
                         [Sample(BASE + t, "aa", 0, True, 100.0, True, home=EVIL_HOME)], BASE + t)
        st.apply_lists(EVIL_HOME, {("aa", 0): ChannelMeta(EVIL, 1, EVIL_ROOM)})
        gid = st.save_custom_group(EVIL_GROUP)
        st.set_custom_group_members(gid, [(EVIL_HOME, "aa", 0)])
    srv = _start(db)
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# --- A1 + B1: date range and export size limits -------------------------------------------------

HUGE = "from=1970-01-01&to=9999-12-30"


@pytest.mark.parametrize("path", [
    "/api/summary?" + HUGE, "/api/report?" + HUGE, "/api/daily?" + HUGE, "/api/devices?" + HUGE,
    "/api/custom-groups?" + HUGE, "/api/device?home=home&id=aa&ch=0&" + HUGE,
    "/api/export.csv?" + HUGE, "/api/report.csv?" + HUGE, "/api/report.pdf?" + HUGE,
    "/api/device.csv?home=home&id=aa&ch=0&" + HUGE, "/api/device.pdf?home=home&id=aa&ch=0&" + HUGE,
    "/api/daily?from=2026-01-01&to=9999-12-31",  # OverflowError in parse_range
    "/api/export.csv?to=9999-12-31",
    "/api/daily?from=0001-01-01&to=2026-01-01",
])
def test_huge_or_extreme_range_is_400(evil, path):
    path = path.replace("home=home", "home=" + quote(EVIL_HOME))
    r, body = get(evil, path)
    assert r.status == 400, body[:200]
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    assert "error" in json.loads(body)


def test_range_limit_message_and_boundary(evil):
    r, body = get(evil, "/api/daily?" + HUGE)
    assert str(web.MAX_RANGE_DAYS) in json.loads(body)["error"]
    # exactly MAX_RANGE_DAYS days (inclusive) is accepted
    r, body = get(evil, "/api/daily?from=2024-01-01&to=2027-01-04")  # 1100 days
    assert r.status == 200 and len(json.loads(body)) == web.MAX_RANGE_DAYS
    assert get(evil, "/api/daily?from=2024-01-01&to=2027-01-05")[0].status == 400


def test_export_row_limit(evil, monkeypatch):
    monkeypatch.setattr(web, "MAX_EXPORT_ROWS", 2)  # the fixture has 3 samples
    r, body = get(evil, "/api/export.csv?" + Q)
    assert r.status == 400 and "2" in json.loads(body)["error"]
    monkeypatch.setattr(web, "MAX_EXPORT_ROWS", 3)
    assert get(evil, "/api/export.csv?" + Q)[0].status == 200


# --- M2: CSV injection --------------------------------------------------------------------------

def test_safe_cell():
    for s in ("=1+1", "+1", "-x", "@SUM(A1)", "\tx", "\rx", EVIL):
        assert _safe_cell(s) == "'" + s
    assert _safe_cell("Boiler") == "Boiler" and _safe_cell(None) == "" and _safe_cell("") == ""
    assert _safe_cell(-3.5) == "-3.5" and _safe_cell(0) == "0"  # numbers are never touched


def _fields(body: bytes) -> list[str]:
    return [f for row in csv.reader(io.StringIO(body.decode())) for f in row]


@pytest.mark.parametrize("path", [
    "/api/export.csv?" + Q,
    "/api/report.csv?" + Q,
    "/api/report.csv?" + Q + "&by=room",
    "/api/report.csv?" + Q + "&by=home",
    "/api/report.csv?" + Q + "&by=group",
    "/api/device.csv?home=" + quote(EVIL_HOME) + "&id=aa&ch=0&" + Q,
])
def test_csv_injection_neutralised(evil, path):
    r, body = get(evil, path)
    assert r.status == 200, body[:200]
    fields = _fields(body)
    bad = [f for f in fields if f.startswith(DANGEROUS)]
    assert bad == []
    assert any(f.startswith("'") for f in fields)


def test_csv_injection_payloads_quoted(evil):
    text = get(evil, "/api/export.csv?" + Q)[1].decode()
    assert "'" + EVIL_HOME in text and "'" + EVIL_ROOM in text
    assert "'" + EVIL in get(evil, "/api/report.csv?" + Q)[1].decode()
    assert "'" + EVIL_GROUP in get(evil, "/api/report.csv?" + Q + "&by=group")[1].decode()


# --- M1: CSP without 'unsafe-inline' ------------------------------------------------------------

def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def test_csp_hashes_match_served_page(evil):
    r, body = get(evil, "/")
    html = body.decode()
    csp = r.getheader("Content-Security-Policy")
    assert "unsafe-inline" not in csp and "unsafe-hashes" not in csp
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert scripts and styles
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    for s in scripts:
        assert _hash(s) in directives["script-src"]
    for s in styles:
        assert _hash(s) in directives["style-src"]
    assert directives["default-src"] == "'none'" and directives["frame-ancestors"] == "'none'"


def test_index_has_no_inline_style_attributes(evil):
    html = re.sub(r"<script>.*?</script>", "", get(evil, "/")[1].decode(), flags=re.S)
    tags = re.findall(r"<[a-zA-Z][^>]*>", html)
    assert not [t for t in tags if re.search(r"\sstyle\s*=", t)]
    assert not [t for t in tags if re.search(r"\son[a-z]+\s*=", t)]  # no inline event handlers either


def test_csp_on_api_responses(evil):
    r, _ = get(evil, "/api/summary?" + Q)
    assert "unsafe-inline" not in r.getheader("Content-Security-Policy")


# --- B3: error responses keep the security headers ---------------------------------------------

def _assert_secure_json(r, body):
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("Content-Security-Policy", "").startswith("default-src 'none'")
    assert r.getheader("Cache-Control") == "no-store"
    assert "error" in json.loads(body)


@pytest.mark.parametrize("method,status", [("OPTIONS", 501), ("PATCH", 501), ("TRACE", 501)])
def test_unsupported_methods_are_json_with_headers(evil, method, status):
    r, body = get(evil, "/api/summary", method)
    assert r.status == status
    _assert_secure_json(r, body)
    assert method.encode() not in body or b"<" not in body


def test_bad_request_line_has_headers(evil):
    s = socket.create_connection(("127.0.0.1", evil), timeout=5)
    s.sendall(b"GET /a b HTTP/1.1\r\nHost: x\r\n\r\n")  # 4 words: bad syntax
    raw = b""
    while chunk := s.recv(65536):
        raw += chunk
    s.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.") and b" 400 " in head.split(b"\r\n")[0]
    h = head.decode().lower()
    assert "content-type: application/json; charset=utf-8" in h
    assert "x-content-type-options: nosniff" in h and "content-security-policy:" in h
    assert "error" in json.loads(body)


@pytest.mark.parametrize("path,status", [("/nope", 404), ("/api/daily?from=x", 400)])
def test_404_400_have_headers(evil, path, status):
    r, body = get(evil, path)
    assert r.status == status
    _assert_secure_json(r, body)


def test_405_has_headers(evil):
    c = http.client.HTTPConnection("127.0.0.1", evil, timeout=5)
    c.request("PUT", "/api/custom-groups", body=b"{}", headers={"X-Requested-With": "shelly-monitor",
                                                               "Content-Type": "application/json"})
    r = c.getresponse()
    body = r.read()
    c.close()
    assert r.status == 405
    _assert_secure_json(r, body)


# --- B2: socket timeout, threaded server ------------------------------------------------------

def test_server_is_threaded_with_socket_timeout(tmp_path):
    srv = make_server(str(tmp_path / "x.db"), 60, "127.0.0.1", 0)
    try:
        assert isinstance(srv, ThreadingHTTPServer) and srv.daemon_threads
        assert srv.RequestHandlerClass.timeout == 30
    finally:
        srv.server_close()
