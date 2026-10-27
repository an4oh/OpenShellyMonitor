"""Tests for the login sidecar. Loopback only: the server under test binds 127.0.0.1:0."""
import http.client
import re
import threading
from urllib.parse import urlencode

import pytest

import auth_server as a

SECRET = b"k" * 32


def test_cookie_valid():
    assert a.verify_cookie_value(SECRET, a.make_cookie_value(SECRET))


def test_cookie_expired():
    v = a.make_cookie_value(SECRET, now=1000, ttl=10)
    assert not a.verify_cookie_value(SECRET, v, now=2000)


def test_cookie_tampered():
    exp, sig = a.make_cookie_value(SECRET).split(".")
    assert not a.verify_cookie_value(SECRET, f"{int(exp) + 999}.{sig}")
    assert not a.verify_cookie_value(b"other" * 7, f"{exp}.{sig}")


@pytest.mark.parametrize("v", [None, "", "abc", "1.2.3", "x.y", "-5.aa", "9" * 20 + ".aa"])
def test_cookie_malformed(v):
    assert not a.verify_cookie_value(SECRET, v)


def test_get_cookie():
    assert a.get_cookie("a=1; sm_session=xyz; b=2") == "xyz"
    assert a.get_cookie("sm_sessionx=1") is None
    assert a.get_cookie(None) is None


@pytest.mark.parametrize("raw,exp", [
    ("//evil.com", "/"), ("/\\evil", "/"), ("https://x", "/"), ("/ok?a=b", "/ok?a=b"),
    ("", "/"), (None, "/"), ("/a\nSet-Cookie: x", "/"), ("/api/report.csv?from=2026-10-01", "/api/report.csv?from=2026-10-01"),
    ("/" + "a" * 3000, "/"),
])
def test_next(raw, exp):
    assert a.sanitize_next(raw) == exp


@pytest.fixture(scope="module")
def pwhash():
    return a.hash_password("right-pass-123", n=2 ** 10)


def test_password(pwhash):
    assert a.verify_password("right-pass-123", pwhash)
    assert not a.verify_password("wrong", pwhash)
    assert not a.verify_password("", pwhash)
    assert not a.verify_password("x", "broken")


def test_load_env(tmp_path):
    p = tmp_path / "auth.env"
    p.write_text("# comment\nSHELLY_AUTH_PW_HASH='scrypt$1$2$3$aa$bb'\n\nSHELLY_AUTH_COOKIE_SECRET=00ff\n")
    assert a.load_env(p) == {"SHELLY_AUTH_PW_HASH": "scrypt$1$2$3$aa$bb", "SHELLY_AUTH_COOKIE_SECRET": "00ff"}


@pytest.fixture
def server(pwhash):
    srv = a.make_server("127.0.0.1", 0, pwhash, SECRET, fail_delay=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


def test_end_to_end(server):
    assert req(server, "GET", "/_auth")[0] == 401
    st, h, body = req(server, "GET", "/login?next=/api/report?a=b&c=d")
    assert st == 200 and b'value="/api/report?a=b&amp;c=d"' in body and b"noindex" in body
    assert b"Shelly Monitor" in body
    assert h["Cache-Control"] == "no-store" and "Python" not in h.get("Server", "")

    st, _, body = req(server, "POST", "/login", urlencode({"password": "no", "next": "/"}))
    assert st == 401 and b'role="alert"' in body

    st, h, _ = req(server, "POST", "/login", urlencode({"password": "right-pass-123", "next": "//evil.com"}))
    assert st == 303 and h["Location"] == "/"
    sc = h["Set-Cookie"]
    assert sc.startswith("sm_session=")
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=604800"):
        assert attr in sc
    cookie = sc.split(";")[0]
    assert req(server, "GET", "/_auth", headers={"Cookie": cookie})[0] == 204
    assert req(server, "GET", "/_auth", headers={"Cookie": cookie + "x"})[0] == 401

    st, h, _ = req(server, "POST", "/logout", "", headers={"Cookie": cookie})
    assert st == 303 and h["Location"] == "/login" and "Max-Age=0" in h["Set-Cookie"]

    assert req(server, "GET", "/anything")[0] == 404
    assert req(server, "POST", "/anything", "")[0] == 404


def test_oversized_body_is_capped(server, pwhash):
    # Content-Length larger than MAX_BODY: only MAX_BODY bytes are read, the request still fails cleanly
    # (Connection: close, otherwise the unread tail would be parsed as a second request; nginx caps
    # the body well below MAX_BODY anyway)
    body = urlencode({"password": "x" * (a.MAX_BODY * 2), "next": "/"})
    assert req(server, "POST", "/login", body, headers={"Connection": "close"})[0] == 401


def test_csp_login(server):
    _, h, body = req(server, "GET", "/login")
    csp = h["Content-Security-Policy"]
    assert "form-action 'self'" in csp and "'unsafe-inline'" not in csp
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    style_src = re.search(r"style-src ([^;]*)", csp).group(1)
    blocks = re.findall(r"<script>(.*?)</script>", body.decode(), re.S)
    styles = re.findall(r"<style>(.*?)</style>", body.decode(), re.S)
    assert len(blocks) >= 1 and script_src.count("'sha256-") == body.count(b"<script") == len(blocks)
    assert len(styles) >= 1 and style_src.count("'sha256-") == body.count(b"<style") == len(styles)
    for blk in blocks:
        assert a._csp_hash(blk) in script_src
    for blk in styles:
        assert a._csp_hash(blk) in style_src
    assert b" style=" not in body  # inline style attributes would need 'unsafe-hashes'
    assert req(server, "POST", "/login", "password=x")[1]["Content-Security-Policy"] == csp
