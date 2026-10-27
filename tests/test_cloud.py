import io
import logging
import urllib.error

import pytest

from shelly_monitor import cloud
from shelly_monitor.cloud import ChannelMeta, CloudError, ShellyCloud, normalize_server, parse_all_status


def test_gen1(load_fixture):
    devices, samples = parse_all_status(load_fixture("gen1.json")["data"], 100)
    assert {d.device_id for d in devices} == {"dc4f2276846a", "a1b2c3d4e5f6"}
    by = {(s.device_id, s.channel): s for s in samples}
    assert by[("dc4f2276846a", 0)].is_on is True
    assert by[("dc4f2276846a", 0)].power_w == 1830.5
    assert by[("dc4f2276846a", 1)].is_on is False
    off = by[("a1b2c3d4e5f6", 0)]
    assert off.online is False and off.power_w is None
    assert all(s.ts == 100 for s in samples)


def test_gen2(load_fixture):
    devices, samples = parse_all_status(load_fixture("gen2.json")["data"], 5)
    assert devices[0].model == "SPSW-002PE16EU" and devices[0].gen == "G2"
    assert [(s.channel, s.is_on, s.power_w) for s in samples] == [(0, True, 742.1), (1, False, 0.0)]


def test_mixed_uses_dev_info_id_and_ignores_non_relays(load_fixture, caplog):
    caplog.set_level(logging.DEBUG)
    devices, samples = parse_all_status(load_fixture("mixed.json")["data"], 1)
    # every device of the payload is reported (online flag), only outputs give samples
    assert sorted(d.device_id for d in devices) == ["1643370677417", "84cca87c0144", "dc4f2276846a", "e8db84aaaaaa"]
    assert sorted({s.device_id for s in samples}) == ["84cca87c0144", "dc4f2276846a", "e8db84aaaaaa"]
    sensor = next(s for s in samples if s.device_id == "e8db84aaaaaa")
    assert (sensor.kind, sensor.channel, sensor.is_on, sensor.value) == ("sensor", 0, None, 20.5)
    assert len(samples) == 3
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "no output or sensor" in caplog.text


def test_malformed_never_raises(load_fixture, caplog):
    devices, samples = parse_all_status(load_fixture("malformed.json")["data"], 1)
    assert [d.device_id for d in devices] == ["good"]
    assert samples[0].is_on is True and samples[0].power_w is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 5


@pytest.mark.parametrize("data", [None, [], {}, {"devices_status": []}])
def test_missing_devices_status(data):
    assert parse_all_status(data, 1) == ([], [])


def test_normalize_server():
    assert normalize_server(" https://shelly-1-eu.shelly.cloud/ ") == "shelly-1-eu.shelly.cloud"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(monkeypatch, handler):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        return handler(req)

    monkeypatch.setattr(cloud.urllib.request, "urlopen", fake_urlopen)
    return ShellyCloud("shelly-1-eu.shelly.cloud", "SECRET", throttle=cloud.Throttle(0)), calls


def test_request_format(monkeypatch):
    client, calls = make_client(monkeypatch, lambda r: FakeResp(b'{"isok": true, "data": {"devices_status": {}}}'))
    assert client.all_status() == {"devices_status": {}}
    req = calls[0]
    assert req.full_url == "https://shelly-1-eu.shelly.cloud/device/all_status"
    assert req.get_method() == "POST"
    assert req.data == b"auth_key=SECRET"
    ua = req.get_header("User-agent")
    assert ua.startswith("shelly-monitor/") and ua.endswith("(+contact-on-request)")


@pytest.mark.parametrize("code", [429, 500, 503, 401])
def test_http_errors_raise_cloud_error(monkeypatch, code):
    def handler(req):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)

    client, _ = make_client(monkeypatch, handler)
    with pytest.raises(CloudError):
        client.all_status()


def test_isok_false(monkeypatch):
    client, _ = make_client(monkeypatch, lambda r: FakeResp(b'{"isok": false, "errors": {"x": 1}}'))
    with pytest.raises(CloudError):
        client.all_status()


def test_device_names_best_effort(monkeypatch):
    body = (b'{"isok": true, "data": {"devices": {"abc": {"id": "abc", "name": "Boiler A"}, '
            b'"abc_2": {"id": "abc_2", "name": "Lamp 2"}}}}')
    client, _ = make_client(monkeypatch, lambda r: FakeResp(body))
    assert client.device_lists().channels == {("abc", 0): ChannelMeta("Boiler A", None, None, kind="unknown"),
                                              ("abc", 2): ChannelMeta("Lamp 2", None, None, kind="unknown")}

    def fail(req):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    client, _ = make_client(monkeypatch, fail)
    assert not client.device_lists()


def test_real_gen2_fixture(load_fixture, caplog):
    caplog.set_level(logging.DEBUG)
    devices, samples = parse_all_status(load_fixture("real_gen2.json")["data"], 42)
    assert {(d.device_id, d.model, d.gen, d.online) for d in devices} == {
        ("aabbcc000001", "S3SW-001X8EU", "G2+", True),
        ("aabbcc000002", "SNDC-0D4P10WW", "G2+", True),
        ("aabbcc000003", "SNDC-0D4P10WW", "G2+", True),
        ("aabbcc000004", "SNSN-0D24X", "G2+", True),  # i4: inputs only, no samples
    }
    got = [(s.device_id, s.channel, s.is_on, s.power_w, s.online, s.energy_wh) for s in samples]
    assert sorted(got) == [
        ("aabbcc000001", 0, False, None, True, None),
        ("aabbcc000002", 0, False, None, True, None),
        ("aabbcc000002", 1, False, None, True, None),
        ("aabbcc000002", 2, False, None, True, None),
        ("aabbcc000002", 3, False, None, True, None),
        ("aabbcc000003", 0, False, 0.0, True, 6319.839),
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "aabbcc000004" in caplog.text and "1700000000005" in caplog.text  # i4 and thermostat ignored at DEBUG


def test_online_from_cloud_connected_and_fallbacks():
    data = {"devices_status": {
        "a": {"id": "a", "cloud": {"connected": False}, "switch:0": {"output": True}},
        "b": {"id": "b", "switch:0": {"output": True}},
        "c": {"_dev_info": {"id": "c", "online": False}, "light:0": {"output": True}},
    }}
    _, samples = parse_all_status(data, 1)
    assert {s.device_id: s.online for s in samples} == {"a": False, "b": True, "c": False}
