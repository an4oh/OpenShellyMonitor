"""Built-in password login of the dashboard (shelly_monitor.auth + web routing)."""

import base64
import hashlib
import http.client
import json
import re
import threading
import time
from urllib.parse import quote

import pytest

from shelly_monitor import auth
from shelly_monitor.auth import WebAuth, hash_password, sanitize_next, verify_password
from shelly_monitor.cloud import DeviceInfo, Sample
from shelly_monitor.config import ConfigError
from shelly_monitor.store import Store
from shelly_monitor.web import make_server

FAST = {"n": 2 ** 4, "r": 8, "p": 1}  # cheap scrypt parameters for tests
PW = "correct horse"


# --- primitives -----------------------------------------------------------------------------

def test_hash_and_verify():
    h = hash_password(PW, **FAST)
    assert h.startswith("scrypt$16$8$1$")
    assert verify_password(PW, h)
    assert not verify_password("wrong", h)
    assert not verify_password(PW, "garbage")
    assert not verify_password(PW, "bcrypt$1$2$3$aa$bb")


def test_default_hash_format_compatible_with_sidecar():
    h = hash_password("x")
    scheme, n, r, p, salt, dk = h.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2 ** 15), "8", "1") and len(salt) == 32 and len(dk) == 64


@pytest.mark.parametrize("value,expected", [
    (None, "/"), ("", "/"), ("/", "/"), ("/?home=a#x", "/?home=a#x"), ("//evil.example", "/"),
    ("/\\evil", "/"), ("https://evil.example/", "/"), ("evil", "/"), ("/a\nb", "/"), ("/" + "a" * 3000, "/"),
])
def test_sanitize_next(value, expected):
    assert sanitize_next(value) == expected


def _auth(**kw):
    kw.setdefault("fail_delay", 0)
    return WebAuth(hash_password(PW, **FAST), b"s" * 32, **kw)


def test_cookie_roundtrip_expiry_and_tamper():
    a = _auth(session_days=1)
    v = a.issue(now=1000)
    assert a.valid(v, now=1001)
    assert not a.valid(v, now=1000 + 86400 + 1)
    assert not a.valid(v[:-1] + ("0" if v[-1] != "0" else "1"), now=1001)
    assert not a.valid(None) and not a.valid("") and not a.valid("1.2") and not a.valid("a.b.c.d")


def test_cookie_bound_to_password_and_secret():
    a = _auth()
    v = a.issue()
    other_pw = WebAuth(hash_password("another", **FAST), b"s" * 32, fail_delay=0)
    other_secret = WebAuth(a.pw_hash, b"t" * 32, fail_delay=0)
    assert a.valid(v) and not other_pw.valid(v) and not other_secret.valid(v)
    # the tag in the cookie does not reveal anything usable without the secret
    assert PW not in v and a.pw_hash.split("$")[-1] not in v


def test_plain_password_sessions_survive_restart_with_fixed_secret():
    env = {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_COOKIE_SECRET": "ab" * 32}
    a1, a2 = WebAuth.from_env(env, scrypt_params=FAST), WebAuth.from_env(env, scrypt_params=FAST)
    assert a1.valid(a2.issue())


def test_revoke():
    a = _auth()
    v = a.issue()
    a.revoke(v)
    assert not a.valid(v)


def test_from_env():
    assert WebAuth.from_env({}) is None
    a = WebAuth.from_env({"SHELLY_WEB_PASSWORD": PW}, scrypt_params=FAST)
    assert a is not None and a.check(PW) and not a.check("x")
    assert a.session_seconds == 7 * 86400 and not a.trust_proxy and not a.secure_cookie
    h = hash_password(PW, **FAST)
    b = WebAuth.from_env({"SHELLY_WEB_PASSWORD_HASH": h, "SHELLY_WEB_COOKIE_SECRET": "00" * 32,
                          "SHELLY_WEB_SESSION_DAYS": "2", "SHELLY_WEB_TRUST_PROXY": "1",
                          "SHELLY_WEB_SECURE_COOKIE": "1"})
    assert b.check(PW) and b.session_seconds == 2 * 86400 and b.trust_proxy and b.secure_cookie
    assert b.secret == bytes(32)


@pytest.mark.parametrize("env", [
    {"SHELLY_WEB_PASSWORD_HASH": "not-a-hash"},
    {"SHELLY_WEB_PASSWORD_HASH": "scrypt$x$8$1$aa$bb"},
    {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_COOKIE_SECRET": "zz"},
    {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_COOKIE_SECRET": "abcd"},  # too short
    {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_SESSION_DAYS": "0"},
    {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_SESSION_DAYS": "x"},
    {"SHELLY_WEB_PASSWORD": PW, "SHELLY_WEB_PASSWORD_HASH": "scrypt$16$8$1$aa$bb"},
])
def test_from_env_invalid(env):
    with pytest.raises(ConfigError):
        WebAuth.from_env(env, scrypt_params=FAST)


def test_rate_limiter():
    lim = auth.RateLimiter(max_failures=3, window=600)
    for _ in range(3):
        assert not lim.blocked("1.2.3.4", now=100)
        lim.fail("1.2.3.4", now=100)
    assert lim.blocked("1.2.3.4", now=101) and not lim.blocked("5.6.7.8", now=101)
    assert not lim.blocked("1.2.3.4", now=100 + 601)


def test_login_page_csp_hashes_match():
    page = auth.render_page("/?a=<b>").decode()
    csp = auth.login_csp()
    assert "unsafe-inline" not in csp and "form-action 'self'" in csp
    for tag in ("script", "style"):
        for block in re.findall(rf"<{tag}>(.*?)</{tag}>", page, re.S):
            h = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
            assert f"'sha256-{h}'" in csp
    assert 'value="/?a=&lt;b&gt;"' in page


# --- web routing ----------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "a.db")
    with Store(path) as st:
        st.save_poll([DeviceInfo("aa", "M", "G2")], [Sample(1000, "aa", 0, True, None, True)], 1000)
    return path


def _serve(db, a, readonly=False):
    srv = make_server(db, 60, "127.0.0.1", 0, readonly=readonly, auth=a)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


@pytest.fixture
def site(db):
    a = _auth()
    srv = _serve(db, a)
    yield srv.server_address[1], a
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def login(port, password=PW, nxt="/", headers=None):
    body = f"password={quote(password)}&next={quote(nxt)}"
    return req(port, "POST", "/login", body,
               {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})


def cookie_of(r):
    sc = r.getheader("Set-Cookie") or ""
    return sc.split(";")[0]


def test_anonymous_html_redirects_to_login(site):
    port, _ = site
    r, _ = req(port, "GET", "/?home=a")
    assert r.status == 302 and r.getheader("Location") == "/login?next=" + quote("/?home=a", safe="")
    r, _ = req(port, "GET", "/index.html")
    assert r.status == 302


@pytest.mark.parametrize("path", ["/api/homes", "/api/summary", "/api/config", "/api/export.csv", "/nope"])
def test_anonymous_api_is_401(site, path):
    r, body = req(site[0], "GET", path)
    assert r.status == 401 and json.loads(body)["error"]
    assert b"aa" not in body


def test_anonymous_writes_are_401(site):
    r, _ = req(site[0], "POST", "/api/custom-groups", b'{"name":"x"}',
               {"Content-Type": "application/json", "X-Requested-With": "shelly-monitor"})
    assert r.status == 401


def test_healthz_is_public(site):
    r, body = req(site[0], "GET", "/healthz")
    assert r.status == 200 and json.loads(body) == {"ok": True, "last_ok_poll": None, "stale": False,
                                                  "poller": "off"}


def test_login_page(site):
    r, body = req(site[0], "GET", "/login?next=%2Fdevices%3Fx%3D1")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert len(r.headers.get_all("Content-Security-Policy")) == 1
    assert r.getheader("Content-Security-Policy") == auth.login_csp()
    assert b'value="/devices?x=1"' in body
    r, body = req(site[0], "GET", "/login?next=//evil.example")
    assert b'value="/"' in body


def test_login_flow_and_logout(site):
    port, a = site
    r, body = login(port, "wrong")
    assert r.status == 401 and b"Wrong password" in body and r.getheader("Set-Cookie") is None
    r, _ = login(port, nxt="/?home=a")
    assert r.status == 303 and r.getheader("Location") == "/?home=a"
    sc = r.getheader("Set-Cookie")
    assert sc.startswith("sm_session=") and "HttpOnly" in sc and "SameSite=Lax" in sc and "Path=/" in sc
    assert "Secure" not in sc and f"Max-Age={7 * 86400}" in sc
    ck = {"Cookie": cookie_of(r)}
    r, body = req(port, "GET", "/", headers=ck)
    assert r.status == 200 and b"shelly-monitor" in body
    assert len(r.headers.get_all("Content-Security-Policy")) == 1
    r, body = req(port, "GET", "/api/config", headers=ck)
    assert r.status == 200 and json.loads(body)["auth"] is True
    r, _ = req(port, "GET", "/api/homes", headers=ck)
    assert r.status == 200
    # already signed in: the login page goes back to next
    r, _ = req(port, "GET", "/login?next=/x", headers=ck)
    assert r.status == 302 and r.getheader("Location") == "/x"
    # logout needs the anti-CSRF header, clears the cookie and revokes it
    r, _ = req(port, "POST", "/logout", headers=ck)
    assert r.status == 403
    r, _ = req(port, "POST", "/logout", headers={**ck, "X-Requested-With": "shelly-monitor"})
    assert r.status == 200 and "Max-Age=0" in r.getheader("Set-Cookie")
    r, _ = req(port, "GET", "/api/homes", headers=ck)
    assert r.status == 401


def test_login_next_is_sanitized(site):
    r, _ = login(site[0], nxt="//evil.example/x")
    assert r.status == 303 and r.getheader("Location") == "/"


def test_login_rate_limit(site):
    port, _ = site
    for _ in range(10):
        assert login(port, "bad")[0].status == 401
    r, body = login(port, "bad")
    assert r.status == 429 and b"Too many" in body
    assert login(port)[0].status == 429  # also the right password, until the window passes


def test_rate_limit_uses_forwarded_for_only_with_trust_proxy(db):
    for trust, expect in ((False, 429), (True, 303)):
        a = _auth(trust_proxy=trust)
        a.limiter.max_failures = 2
        srv = _serve(db, a)
        port = srv.server_address[1]
        try:
            for i in range(2):
                login(port, "bad", headers={"X-Forwarded-For": f"10.0.0.{i}, 127.0.0.1"})
            assert login(port, headers={"X-Forwarded-For": "10.0.0.9"})[0].status == expect
        finally:
            srv.shutdown()
            srv.server_close()


def test_secure_cookie(db):
    for kw, headers, secure in (({"trust_proxy": True}, {"X-Forwarded-Proto": "https"}, True),
                                ({}, {"X-Forwarded-Proto": "https"}, False),
                                ({"secure_cookie": True}, {}, True)):
        srv = _serve(db, _auth(**kw))
        try:
            r, _ = login(srv.server_address[1], headers=headers)
            assert r.status == 303 and ("; Secure" in r.getheader("Set-Cookie")) is secure
        finally:
            srv.shutdown()
            srv.server_close()


def test_login_body_limits(site):
    port, _ = site
    r, _ = req(port, "POST", "/login", "password=" + "a" * 5000,
               {"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 413


def test_fail_delay(db):
    a = WebAuth(hash_password(PW, **FAST), b"s" * 32, fail_delay=0.3)
    srv = _serve(db, a)
    try:
        t0 = time.monotonic()
        assert login(srv.server_address[1], "bad")[0].status == 401
        assert time.monotonic() - t0 >= 0.3
    finally:
        srv.shutdown()
        srv.server_close()


def test_csrf_on_groups_still_required_when_logged_in(site):
    port, _ = site
    ck = {"Cookie": cookie_of(login(port)[0])}
    r, _ = req(port, "POST", "/api/custom-groups", b'{"name":"x"}', {**ck, "Content-Type": "application/json"})
    assert r.status == 403
    r, _ = req(port, "POST", "/api/custom-groups", b'{"name":"x"}',
               {**ck, "Content-Type": "application/json", "X-Requested-With": "shelly-monitor"})
    assert r.status == 200


def test_without_auth_nothing_changes(db):
    srv = _serve(db, None)
    port = srv.server_address[1]
    try:
        assert req(port, "GET", "/")[0].status == 200
        r, body = req(port, "GET", "/api/config")
        assert json.loads(body)["auth"] is False
        assert req(port, "GET", "/login")[0].status == 404
        assert req(port, "POST", "/logout", headers={"X-Requested-With": "shelly-monitor"})[0].status == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_index_has_logout_without_inline_handlers():
    from shelly_monitor.web import _index_html
    html = _index_html().decode()
    assert 'id="logout"' in html and "innerHTML" not in html
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html)  # no inline event handler attributes


def test_sidecar_points_to_shared_primitives():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "deploy/public/auth_server.py").read_text()
    assert "shelly_monitor/auth.py" in "\n".join(src.splitlines()[:3])
