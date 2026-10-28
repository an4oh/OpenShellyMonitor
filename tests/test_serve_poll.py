"""`serve --poll`: background poller in the dashboard process, and GET /healthz."""

import http.client
import json
import logging
import threading
import time

import pytest

from shelly_monitor import cli, web
from shelly_monitor.cloud import CloudError
from shelly_monitor.config import Home
from shelly_monitor.poller import BackgroundPoller
from shelly_monitor.store import Store
from shelly_monitor.web import make_server


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_poller_marks_successful_cycles_and_stops():
    def target(stop, mark_ok):
        while not stop.is_set():
            mark_ok()
            stop.wait(0.01)

    p = BackgroundPoller(target, restart_delay=0.05)
    assert p.last_ok is None and not p.running
    p.start()
    assert _wait(lambda: p.last_ok is not None) and p.running
    p.stop()
    assert not p.running


def test_poller_restarts_after_crash(caplog):
    calls = []

    def target(stop, mark_ok):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")
        stop.wait()

    p = BackgroundPoller(target, restart_delay=0.05)
    p.start()
    try:
        assert _wait(lambda: len(calls) >= 3)
        assert p.running
    finally:
        p.stop()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "restart" in r.getMessage()]
    assert len(errors) == 2


class FakePoller:
    def __init__(self, last_ok=None, started=None, running=True):
        self.last_ok, self.started, self.running = last_ok, started or time.time(), running


def _health(tmp_path, poller, interval=60):
    srv = make_server(str(tmp_path / "none.db"), interval, "127.0.0.1", 0, poller=poller)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        return r.status, json.loads(r.read()), r
    finally:
        srv.shutdown()
        srv.server_close()


def test_healthz_without_poller(tmp_path):
    status, body, r = _health(tmp_path, None)
    assert status == 200 and body == {"ok": True, "last_ok_poll": None, "stale": False, "poller": "off"}
    assert r.getheader("Cache-Control") == "no-store"


def test_healthz_recent_poll(tmp_path):
    now = time.time()
    status, body, _ = _health(tmp_path, FakePoller(last_ok=now - 100))
    assert status == 200 and body == {"ok": True, "last_ok_poll": int(now - 100), "stale": False,
                                      "poller": "running"}


def test_healthz_just_started_without_poll_yet(tmp_path):
    status, body, _ = _health(tmp_path, FakePoller(last_ok=None))
    assert status == 200 and body["stale"] is False and body["last_ok_poll"] is None


@pytest.mark.parametrize("last_ok", [time.time() - 5 * 60 - 30, None])
def test_healthz_stale_is_still_200(tmp_path, last_ok):
    """Cloud down or a wrong key: restarting the container would not help, so no 503."""
    status, body, _ = _health(tmp_path, FakePoller(last_ok=last_ok, started=time.time() - 1000))
    assert status == 200 and body["ok"] is True and body["stale"] is True and body["poller"] == "running"


def test_healthz_dead_poller_is_503(tmp_path):
    status, body, _ = _health(tmp_path, FakePoller(last_ok=time.time(), running=False))
    assert status == 503 and body["ok"] is False and body["poller"] == "dead" and body["stale"] is False


def test_poller_not_running_while_waiting_restart():
    def target(stop, mark_ok):
        raise RuntimeError("boom")

    p = BackgroundPoller(target, restart_delay=5)
    p.start()
    try:
        assert _wait(lambda: not p.running)
    finally:
        p.stop()


class FailingCloud:
    throttle = type("T", (), {"gap": 1.1})()

    def all_status(self):
        raise CloudError("connection refused")

    def discover_home(self):
        raise CloudError("connection refused")

    def device_lists(self):
        return None


class OkCloud(FailingCloud):
    def all_status(self):
        return {"devices_status": {}}


def test_poll_forever_marks_only_successful_cycles(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("SHELLY_EVENT_LOG", "0")
    for cloud, expect_ok in ((FailingCloud(), False), (OkCloud(), True)):
        stop, marks = threading.Event(), []

        def mark():
            marks.append(1)
            stop.set()

        t = threading.Thread(target=cli.poll_forever,
                             args=([cli.Target(Home("A", "ka", "s"), cloud)], 5, stop, mark))
        t.start()
        if not expect_ok:
            assert _wait(lambda: "cycle skipped" in caplog.text)
            stop.set()
        t.join(5)
        assert not t.is_alive() and bool(marks) is expect_ok


def test_cmd_serve_poll_wires_poller(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLY_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SHELLY_AUTH_KEY", "k")
    monkeypatch.setenv("SHELLY_SERVER", "unreachable.invalid")
    monkeypatch.setenv("SHELLY_INTERVAL", "60")
    seen = {}
    monkeypatch.setattr(web, "serve", lambda *a, **k: seen.update(k) or 0)
    assert cli.main(["--env-file", str(tmp_path / "none"), "serve", "--poll", "--port", "0"]) == 0
    assert isinstance(seen["poller"], BackgroundPoller) and not seen["poller"].running
    seen.clear()
    assert cli.main(["--env-file", str(tmp_path / "none"), "serve", "--port", "0"]) == 0
    assert seen["poller"] is None


def test_cmd_serve_poll_config_error_before_listening(tmp_path, monkeypatch, capsys):
    for k in ("SHELLY_AUTH_KEY", "SHELLY_AUTH_KEYS", "SHELLY_HOMES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(web, "serve", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--env-file", str(tmp_path / "none"), "serve", "--poll"])
    assert e.value.code == 2


def test_cmd_serve_auth_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SHELLY_WEB_PASSWORD_HASH", "nope")
    monkeypatch.setattr(web, "serve", lambda *a, **k: pytest.fail("server started"))
    assert cli.main(["--env-file", str(tmp_path / "none"), "serve"]) == 2
    assert "SHELLY_WEB_PASSWORD_HASH" in capsys.readouterr().err


def test_serve_help_mentions_poll_and_login(capsys):
    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])
    out = capsys.readouterr().out
    assert "--poll" in out and "SHELLY_WEB_PASSWORD" in out and "no authentication" in out


def test_serve_logs_login_state(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    class Srv:
        server_address = ("127.0.0.1", 1)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(web, "make_server", lambda *a, **k: Srv())
    monkeypatch.setattr(web.signal, "signal", lambda *a: None)
    web.serve(str(tmp_path / "x.db"), 60, "127.0.0.1", 0, auth=None)
    assert "no authentication" in caplog.text and "without a password" not in caplog.text
    caplog.clear()
    web.serve(str(tmp_path / "x.db"), 60, "127.0.0.1", 0, auth=object())
    assert "password login enabled" in caplog.text
    caplog.clear()
    web.serve(str(tmp_path / "x.db"), 60, "0.0.0.0", 0, auth=None)
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("dashboard reachable from the network without a password: set SHELLY_WEB_PASSWORD "
               "or bind to 127.0.0.1" in r.getMessage() for r in warn)
    caplog.clear()
    web.serve(str(tmp_path / "x.db"), 60, "0.0.0.0", 0, auth=object())
    assert "without a password" not in caplog.text
