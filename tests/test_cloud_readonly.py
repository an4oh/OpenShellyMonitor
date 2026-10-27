"""shelly-monitor never writes to Shelly Cloud."""

import io
import json
import re
from pathlib import Path

import pytest

from shelly_monitor import cloud

ALLOWED = {"/device/all_status", "/interface/device/get_all_lists", "/user/get_units_info",
           "/user/get_settings", "/user/get_sub_units", "/user/get_user_key", "/statistics/event-log"}


def test_allowlist_is_exactly_the_read_endpoints():
    assert cloud.READ_ONLY_ENDPOINTS == ALLOWED


def test_source_calls_only_allowlisted_paths():
    src = Path(cloud.__file__).read_text()
    called = set(re.findall(r'_post\(\s*"([^"]+)"', src))
    assert called and called <= ALLOWED
    for word in ("relay/control", "set_", "/save", "rename", "bulk_control", "/v2/devices/api/set"):
        assert not any(word in path for path in called)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_every_public_method_hits_only_allowlisted_endpoints(monkeypatch, load_fixture):
    seen = []
    replies = {"/user/get_units_info": load_fixture("units_info.json"),
               "/user/get_settings": {"isok": True, "data": {"settings": {"avatar": "none"}}}}

    def urlopen(req, timeout):
        path = req.full_url.split(".cloud", 1)[1]
        seen.append((req.get_method(), path))
        return Resp(json.dumps(replies.get(path, {"isok": True, "data": {"devices_status": {}, "devices": {}},
                                                  "result": {}})).encode())

    monkeypatch.setattr(cloud.urllib.request, "urlopen", urlopen)
    c = cloud.ShellyCloud("s.shelly.cloud", "k", throttle=cloud.Throttle(0))
    public = [n for n in dir(c) if not n.startswith("_") and callable(getattr(c, n))]
    assert set(public) == {"all_status", "device_lists", "discover_home", "event_log", "events"}  # no write method
    args = {"event_log": (["aa"],), "events": (["aa"], "h")}
    for name in public:
        getattr(c, name)(*args.get(name, ()))
    assert seen and {p for _, p in seen} <= ALLOWED and {m for m, _ in seen} == {"POST"}


def test_non_allowlisted_path_is_refused(monkeypatch):
    monkeypatch.setattr(cloud.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call"))
    c = cloud.ShellyCloud("s.shelly.cloud", "k", throttle=cloud.Throttle(0))
    for path in ("/device/relay/control", "/device/light/control", "/interface/device/save", "/v2/devices/api/set/switch"):
        with pytest.raises(ValueError, match="not a read-only endpoint"):
            c._post(path)
