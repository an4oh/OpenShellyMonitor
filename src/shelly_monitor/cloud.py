"""Minimal Shelly Cloud client (stdlib only) and status payload parser."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"shelly-monitor/{__version__} (+contact-on-request)"
DEFAULT_REQUEST_GAP = 5.0  # seconds between two cloud requests (SHELLY_REQUEST_GAP)
MIN_REQUEST_GAP = 1.1  # the cloud allows at most 1 request/second per account
NO_ROOM_ID = -10

# shelly-monitor is read-only towards Shelly Cloud: these are the only endpoints it may call.
# Custom groups, names and every other edit live in the local database only.
READ_ONLY_ENDPOINTS = frozenset({
    "/device/all_status",
    "/interface/device/get_all_lists",
    "/user/get_units_info",
    "/user/get_settings",
    "/user/get_sub_units",
    "/user/get_user_key",
    "/statistics/event-log",  # activity log, undocumented (see docs/activity-log-discovery.md)
})
EVENT_LOG_MAX_PAGES = 5


class Throttle:
    """Minimum gap between requests, shared by every client of one account
    (several homes can belong to the same account and share its rate limit)."""

    def __init__(self, gap: float = DEFAULT_REQUEST_GAP, clock=time.monotonic, sleep=time.sleep):
        self.gap = gap
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def __enter__(self) -> "Throttle":
        self._lock.acquire()
        if self._last is not None:
            wait = self._last + self.gap - self._clock()
            if wait > 0:
                self._sleep(wait)
        return self

    def __exit__(self, *exc) -> None:
        self._last = self._clock()  # measured from the end of the previous request
        self._lock.release()


DEFAULT_THROTTLE = Throttle()
# Gen2+ components that have an on/off "output"
# Gen2+ components: key prefix -> kind
_OUTPUT_KEY = re.compile(r"^(switch|light|rgbw|rgb|cct|cover):(\d+)$")
_SENSOR_KEY = re.compile(r"^(temperature|humidity|flood):(\d+)$")
_COMPONENT_KEY = re.compile(r"^[a-z_0-9]+:\d+$")
_CHANNEL_ID = re.compile(r"^(.+)_(\d+)$")
_GEN2_KIND = {"switch": "switch", "light": "light", "cct": "light", "rgbw": "rgbw", "rgb": "rgbw", "cover": "cover"}

# Device kinds. ON time is counted for these; covers count "moving" time only in the device detail.
KINDS = ("switch", "light", "rgbw", "cover", "sensor", "input", "unknown")
ON_KINDS = ("switch", "light", "rgbw")
# Sensor channel numbering, so that several readings of one device never share a channel:
# temperature:N / flood:N -> N (as the cloud's "<id>_100" peripheral ids), humidity:N -> 1000 + N,
# Gen1 ext_temperature[k] -> 100 + k, ext_humidity[k] -> 1100 + k, Gen1 tmp -> 0 for sensor-only
# devices (H&T); the internal temperature of relays/dimmers (Gen1 tmp, Gen2 switch.temperature) is ignored.
HUMIDITY_OFFSET = 1000
GEN1_EXT_OFFSET = 100


class CloudError(Exception):
    """Any failure talking to Shelly Cloud. The caller skips the cycle."""


@dataclass(frozen=True)
class Sample:
    ts: int
    device_id: str
    channel: int
    is_on: bool | None  # None for sensors
    power_w: float | None
    online: bool
    energy_wh: float | None = None  # cumulative counter (aenergy.total / Gen1 meters.total), when reported
    home: str = "home"
    kind: str = "switch"
    value: float | None = None  # cover position %, sensor reading


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    model: str | None
    gen: str | None
    home: str = "home"
    online: bool = True


@dataclass(frozen=True)
class ChannelMeta:
    """Per-channel metadata from /interface/device/get_all_lists."""
    name: str | None
    room_id: int | None
    room: str | None
    type: str | None = None
    gen: str | None = None
    kind: str | None = None
    channels_count: int | None = None


@dataclass(frozen=True)
class Group:
    group_id: str
    name: str
    type: str | None
    room_id: int | None
    members: tuple[tuple[str, int], ...]


@dataclass
class DeviceLists:
    channels: dict[tuple[str, int], ChannelMeta] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    rooms: dict[int, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.channels or self.groups)


def normalize_server(server: str) -> str:
    server = server.strip()
    for prefix in ("https://", "http://"):
        if server.startswith(prefix):
            server = server[len(prefix):]
    return server.rstrip("/")


class ShellyCloud:
    def __init__(self, server: str, auth_key: str, timeout: float = 15.0, throttle: Throttle | None = None):
        if not server or not auth_key:
            raise ValueError("SHELLY_SERVER and SHELLY_AUTH_KEY are required")
        self.base = f"https://{normalize_server(server)}"
        self.auth_key = auth_key
        self.timeout = timeout
        self.throttle = throttle or DEFAULT_THROTTLE

    def _post(self, path: str, params: dict | None = None, any_data: bool = False, whole: bool = False):
        if path not in READ_ONLY_ENDPOINTS:  # the tool never changes anything in Shelly Cloud
            raise ValueError(f"{path} is not a read-only endpoint")
        with self.throttle:
            return self._request(path, params, any_data, whole)

    def _request(self, path: str, params: dict | None, any_data: bool = False, whole: bool = False):
        """POST a form. `whole` returns the whole JSON object for endpoints that answer
        {"errors": [...], "result": ...} instead of {"isok": ..., "data": ...}."""
        body = urllib.parse.urlencode({**(params or {}), "auth_key": self.auth_key}).encode()
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise CloudError(f"{path}: rate limited (HTTP 429)") from e
            if e.code >= 500:
                raise CloudError(f"{path}: server error (HTTP {e.code})") from e
            raise CloudError(f"{path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CloudError(f"{path}: network error: {e}") from e
        try:
            payload = json.loads(raw)
        except ValueError as e:
            raise CloudError(f"{path}: invalid JSON response") from e
        if whole:
            if not isinstance(payload, dict) or payload.get("isok") is False or payload.get("errors"):
                errors = payload.get("errors") if isinstance(payload, dict) else None
                raise CloudError(f"{path}: request rejected: {errors!r}")
            return payload
        if not isinstance(payload, dict) or not payload.get("isok"):
            errors = payload.get("errors") if isinstance(payload, dict) else None
            raise CloudError(f"{path}: request rejected: {errors!r}")
        data = payload.get("data")
        if any_data:
            return data
        if not isinstance(data, dict):
            raise CloudError(f"{path}: response has no 'data' object")
        return data

    def all_status(self) -> dict:
        """Return data from POST /device/all_status (one request)."""
        return self._post("/device/all_status")

    def event_log(self, tags: list[str], limit: int = 500, pointer: str | None = None) -> dict:
        """One page of the activity log (undocumented POST /statistics/event-log).

        `tags` are channel ids ("<id>" = channel 0, "<id>_<N>"), sent as qs-style
        tags[i]=...; the next page is asked with the response's `pointer` and no tags.
        Returns the whole response ({"result": {tag: [...]}, "pointer"?}). Raises CloudError.
        """
        params: dict = {"pointer": pointer} if pointer else {f"tags[{i}]": t for i, t in enumerate(tags)}
        params["limit"] = int(limit)
        data = self._post("/statistics/event-log", params, whole=True)
        if not isinstance(data.get("result"), dict):
            raise CloudError("/statistics/event-log: response has no 'result' object")
        return data

    def events(self, tags: list[str], home: str, limit: int = 500,
               max_pages: int = EVENT_LOG_MAX_PAGES) -> list[Event]:
        """Activity log events of `tags`, following `pointer` for at most `max_pages` pages.
        The cloud keeps only the last 5 events per tag. Raises CloudError."""
        if not tags:
            return []
        out: list[Event] = []
        pointer = None
        for _ in range(max_pages):
            data = self.event_log(tags, limit, pointer)
            out.extend(parse_event_log(data, home))
            pointer = data.get("pointer")
            if not isinstance(pointer, str) or not pointer:
                break
        else:
            if pointer:
                log.debug("home %r: activity log has more than %d pages, rest skipped", home, max_pages)
        return out

    def discover_home(self) -> HomeIdentity | None:
        """Name and unit id of this key's home, or None if it cannot be told apart.

        Uses the undocumented /user/get_units_info, /user/get_settings and, only
        when the avatar is ambiguous, /user/get_sub_units. Raises CloudError.
        """
        units = self._post("/user/get_units_info")
        settings = self._post("/user/get_settings")
        found = match_unit(units, settings)
        if found == "ambiguous":
            subs = self._post("/user/get_sub_units", any_data=True)  # may be [] when there are none
            found = match_unit(units, settings, subs if isinstance(subs, dict) else {})
        return found if isinstance(found, HomeIdentity) else None

    def device_lists(self) -> DeviceLists:
        """Best-effort registry: every channel (name, room, type, kind) and the groups.

        Uses /interface/device/get_all_lists, which is what the Shelly web app
        uses but is NOT part of the documented Cloud Control API. Ids are
        "<id>" for channel 0 and "<id>_<N>" for channel N of multi-channel
        devices. Any failure returns {} so that monitoring keeps working.
        """
        try:
            data = self._post("/interface/device/get_all_lists")
        except CloudError as e:
            log.info("device names not available from cloud (%s); use 'devices --name'", e)
            return DeviceLists()
        return parse_device_lists(data)


@dataclass(frozen=True)
class HomeIdentity:
    unit_id: int | None
    name: str


def match_unit(units_info, settings, sub_units=None) -> HomeIdentity | None | str:
    """Find which unit (home) of the account a key belongs to.

    `units_info` is the data of /user/get_units_info (all homes of the account),
    `settings` the data of /user/get_settings (its `avatar` is the avatar of the
    key's home). Candidates are units whose avatar equals settings.avatar; if
    several match and `sub_units` (data of /user/get_sub_units) is given, they
    are narrowed to units whose `subs` equals the number of `sub_*` entries.
    Returns the identity, None when nothing matches, or "ambiguous" when more
    than one unit is left (the caller may fetch sub_units and retry).
    """
    if not isinstance(units_info, dict) or not isinstance(settings, dict):
        return None
    avatar = settings.get("settings", {}).get("avatar") if isinstance(settings.get("settings"), dict) else None
    if avatar is None:
        return None
    units = [u for key in ("own_units", "sub_units") for u in (units_info.get(key) or [])
             if isinstance(u, dict)]
    candidates = [u for u in units if u.get("avatar") == avatar and u.get("name")]
    if len(candidates) > 1:
        if sub_units is None:
            return "ambiguous"
        n_subs = sum(1 for k in sub_units if str(k).startswith("sub_")) if isinstance(sub_units, dict) else 0
        candidates = [u for u in candidates if u.get("subs") == n_subs]
    if len(candidates) != 1:
        return "ambiguous" if candidates else None
    unit = candidates[0]
    unit_id = unit.get("unit_id")
    return HomeIdentity(unit_id if isinstance(unit_id, int) else None, str(unit["name"]))


@dataclass(frozen=True)
class Event:
    """One activity log row. `event` is the cloud's `e` (1 turn, 4 sensor, 5 cover,
    8 brightness, 13 input, ...); `is_on` is set only for e=1; `payload` is the raw `p`."""
    ts_ms: int  # epoch milliseconds, UTC
    home: str
    device_id: str
    channel: int
    event: int
    is_on: bool | None
    payload: str
    rowid: int  # increasing per cloud unit (home): the deduplication key


EVENT_TURN, EVENT_BRIGHTNESS, EVENT_INPUT, EVENT_COVER, EVENT_SENSOR = 1, 8, 13, 5, 4
EVENT_KINDS = {EVENT_TURN: "turn", EVENT_SENSOR: "sensor", EVENT_COVER: "cover",
               EVENT_BRIGHTNESS: "brightness", EVENT_INPUT: "input"}


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_event_log(data, home: str) -> list[Event]:
    """Events of an event-log response ({"result": {tag: [{tag, t, e, p, rowid}]}}).

    Device and channel come from the tag ("<id>" / "<id>_<N>"). Malformed rows
    (bad time/rowid/event, `p` not a JSON list, a turn event without a boolean)
    are skipped with a WARNING; never raises.
    """
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return []
    out: list[Event] = []
    for key, rows in result.items():
        if not isinstance(rows, list):
            log.warning("activity log: tag %s is not a list, skipped", key)
            continue
        for row in rows:
            if not isinstance(row, dict):
                log.warning("activity log: tag %s: row is not an object, skipped", key)
                continue
            ts_ms, event, rowid = _int(row.get("t")), _int(row.get("e")), _int(row.get("rowid"))
            raw = row.get("p")
            try:
                p = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                p = None
            if ts_ms is None or event is None or rowid is None or not isinstance(p, list):
                log.warning("activity log: tag %s: malformed row %r, skipped", key, row)
                continue
            is_on = None
            if event == EVENT_TURN:
                if len(p) < 3 or not isinstance(p[2], bool):
                    log.warning("activity log: tag %s: turn event without state %r, skipped", key, raw)
                    continue
                is_on = p[2]
            device_id, channel = split_channel_id(str(row.get("tag") or key))
            out.append(Event(ts_ms, home, device_id, channel, event, is_on, raw, rowid))
    return out


def event_details(payload: str):
    """(arg, source) of a raw `p`: p[2] (state, brightness, input "S"/"L", cover state)
    and p[3] when it is a string (input source "v_eve"/"input"). (None, None) if unreadable."""
    try:
        p = json.loads(payload)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(p, list):
        return None, None
    arg = p[2] if len(p) > 2 and isinstance(p[2], (str, int, float, bool)) else None
    source = p[3] if len(p) > 3 and isinstance(p[3], str) else None
    return arg, source


def split_channel_id(value: str) -> tuple[str, int]:
    """'abc' -> ('abc', 0); 'abc_2' -> ('abc', 2)."""
    m = _CHANNEL_ID.match(value)
    return (m.group(1), int(m.group(2))) if m else (value, 0)


_TYPE_KIND = (  # (type prefix, kind), first match wins; used when category/mode do not say
    ("SHSW", "switch"), ("SHPLG", "switch"), ("SHEM", "sensor"), ("SHDM", "light"), ("SHBLB", "light"),
    ("SHCB", "light"), ("SHVIN", "light"), ("SHBDUO", "light"), ("SHRGBW2", "light"), ("SHIX3", "input"),
    ("SHBTN", "input"), ("SHHT", "sensor"), ("SHWT", "sensor"), ("SHDW", "sensor"), ("SHMOS", "sensor"),
    ("SHTRV", "sensor"), ("SNSN", "input"), ("S3SN", "input"), ("S4SN", "sensor"), ("SNSW", "switch"),
    ("S3SW", "switch"), ("SPSW", "switch"), ("SNPL", "switch"), ("S3PL", "switch"), ("SNDM", "light"),
    ("S3DM", "light"), ("SNDC", "light"),
)
_CATEGORY_KIND = {"relay": "switch", "light": "light", "roller": "cover", "cover": "cover",
                  "inputs_reader": "input", "sensor": "sensor"}


def kind_for(type_: str | None, category: str | None = None, mode: str | None = None,
             channel: int | None = None) -> str:
    """Kind of a channel from its cloud type code, category and mode."""
    if channel is not None and channel >= 100:
        return "sensor"  # peripherals ("<id>_100"): add-on probes, BLU sensors
    mode = (mode or "").lower()
    if mode in ("roller", "cover"):
        return "cover"
    kind = _CATEGORY_KIND.get((category or "").lower())
    if kind is None:
        kind = next((k for prefix, k in _TYPE_KIND if (type_ or "").upper().startswith(prefix)), "unknown")
    if kind == "light" and mode in ("color", "rgbw", "rgb"):
        return "rgbw"
    return kind


def _room_id(value) -> int | None:
    # -10 = no room; other negative ids (e.g. -20) are not real rooms either
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_device_lists(data) -> DeviceLists:
    """Parse get_all_lists: 'devices' (dict or list), 'rooms' (id -> {name, ...}), 'groups'.

    The channel comes from the 'channel' field when present, else from the
    '_N' suffix of the id. Negative room ids (-10 = no room) mean "no room".
    """
    out = DeviceLists()
    if not isinstance(data, dict):
        return out
    rooms_raw = data.get("rooms")
    if isinstance(rooms_raw, dict):
        for rid, room in rooms_raw.items():
            if isinstance(room, dict) and room.get("name"):
                try:
                    out.rooms[int(rid)] = str(room["name"])
                except (TypeError, ValueError):
                    continue
    devices = data.get("devices")
    if isinstance(devices, dict):
        devices = list(devices.values())
    for dev in devices if isinstance(devices, list) else []:
        if not isinstance(dev, dict) or dev.get("id") is None:
            continue
        device_id, channel = split_channel_id(str(dev["id"]))
        if isinstance(dev.get("channel"), int) and not isinstance(dev.get("channel"), bool):
            channel = dev["channel"]
        room_id = _room_id(dev.get("room_id"))
        gen = dev.get("gen")
        type_ = str(dev["type"]) if dev.get("type") else None
        count = dev.get("channels_count")
        out.channels[(device_id, channel)] = ChannelMeta(
            str(dev["name"]) if dev.get("name") else None, room_id,
            out.rooms.get(room_id) if room_id is not None else None,
            type_, f"G{gen}" if isinstance(gen, int) and not isinstance(gen, bool) else None,
            kind_for(type_, dev.get("category"), dev.get("mode"), channel),
            count if isinstance(count, int) and not isinstance(count, bool) else None,
        )
    groups = data.get("groups")
    for gid, g in (groups.items() if isinstance(groups, dict) else []):
        if not isinstance(g, dict):
            continue
        members = tuple(split_channel_id(str(m)) for m in g.get("devices") or [] if m is not None)
        out.groups.append(Group(str(g.get("id", gid)), str(g.get("name") or gid),
                                str(g["type"]) if g.get("type") else None, _room_id(g.get("room_id")), members))
    return out


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} missing or not boolean")
    return value


def _gen1_energy(meter) -> float | None:
    # Gen1 meters[].total is in watt-minutes
    total = _number(meter.get("total")) if isinstance(meter, dict) else None
    return total / 60.0 if total is not None else None


def _online(entry: dict, info: dict, ts: int, stale_after: int | None) -> bool | None:
    """cloud.connected when present (Gen1 and Gen2), then _dev_info.online, then a recent
    `_updated` (UTC, "YYYY-MM-DD HH:MM:SS") when `stale_after` is given; None if unknown."""
    cloud = entry.get("cloud")
    if isinstance(cloud, dict) and isinstance(cloud.get("connected"), bool):
        return cloud["connected"]
    if isinstance(info.get("online"), bool):
        return info["online"]
    updated = entry.get("_updated")
    if stale_after and isinstance(updated, str):
        try:
            when = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
        return ts - when < stale_after
    return None


def _parse_device(key: str, entry: dict, ts: int, stale_after: int | None = None
                  ) -> tuple[DeviceInfo, list[Sample]] | None:
    info = entry.get("_dev_info")
    info = info if isinstance(info, dict) else {}
    if entry.get("family") == "thermostat" or "thermostat" in entry and "code" not in entry:
        log.debug("device %s: cloud virtual thermostat, ignored", key)
        return None
    # The docs say the keys of devices_status are not reliable: prefer an explicit id.
    # Gen1 entries have no id: the key is the device id.
    device_id = str(info.get("id") or entry.get("id") or key)
    model = info.get("code") or entry.get("code")
    gen = info.get("gen")
    online = _online(entry, info, ts, stale_after)
    samples: list[Sample] = []

    def add(channel, kind, is_on, power=None, energy=None, value=None):
        samples.append(Sample(ts, device_id, channel, is_on, power, True, energy, "home", kind, value))

    gen1 = any(k in entry for k in ("relays", "lights", "rollers", "meters", "wifi_sta")) and \
        not any(_COMPONENT_KEY.match(k) for k in entry)
    if gen1:
        gen = gen or "G1"
        meters = entry.get("meters") if isinstance(entry.get("meters"), list) else []
        rollers = entry.get("rollers")
        if isinstance(rollers, list) and rollers and entry.get("mode") != "relay":
            for i, r in enumerate(rollers):
                if not isinstance(r, dict) or not isinstance(r.get("state"), str):
                    raise ValueError(f"rollers[{i}].state missing")
                add(i, "cover", r["state"] in ("open", "close"), _number(r.get("power")), None,
                    _number(r.get("current_pos")))
        else:
            relays = entry.get("relays")
            if relays is not None and not isinstance(relays, list):
                raise ValueError("'relays' is not a list")
            for i, relay in enumerate(relays or []):
                meter = meters[i] if i < len(meters) else None
                add(i, "switch", _bool(relay.get("ison") if isinstance(relay, dict) else None, f"relays[{i}].ison"),
                    _number(meter.get("power")) if isinstance(meter, dict) else None, _gen1_energy(meter))
        lights = entry.get("lights")
        if lights is not None and not isinstance(lights, list):
            raise ValueError("'lights' is not a list")
        for i, light in enumerate(lights or []):
            if not isinstance(light, dict):
                raise ValueError(f"lights[{i}] is not an object")
            meter = meters[i] if i < len(meters) else None
            power = _number(light.get("power"))
            if power is None and isinstance(meter, dict):
                power = _number(meter.get("power"))
            kind = "rgbw" if light.get("mode") == "color" else "light"
            add(i, kind, _bool(light.get("ison"), f"lights[{i}].ison"), power, _gen1_energy(meter),
                _number(light.get("brightness")))
        has_outputs = bool(samples)
        ext_t = entry.get("ext_temperature")
        for k, v in (ext_t.items() if isinstance(ext_t, dict) else []):
            if isinstance(v, dict) and _number(v.get("tC")) is not None and str(k).isdigit():
                add(GEN1_EXT_OFFSET + int(k), "sensor", None, value=_number(v["tC"]))
        ext_h = entry.get("ext_humidity")
        for k, v in (ext_h.items() if isinstance(ext_h, dict) else []):
            if isinstance(v, dict) and _number(v.get("hum")) is not None and str(k).isdigit():
                add(HUMIDITY_OFFSET + GEN1_EXT_OFFSET + int(k), "sensor", None, value=_number(v["hum"]))
        tmp = entry.get("tmp")  # H&T reading; on dimmers/relays it is the internal temperature: skipped
        if not has_outputs and isinstance(tmp, dict) and _number(tmp.get("tC")) is not None \
                and tmp.get("is_valid", True) is not False:
            add(0, "sensor", None, value=_number(tmp["tC"]))
        hum = entry.get("hum")
        if not has_outputs and isinstance(hum, dict) and _number(hum.get("value")) is not None:
            add(HUMIDITY_OFFSET, "sensor", None, value=_number(hum["value"]))
        if "flood" in entry and isinstance(entry["flood"], bool):
            add(0, "sensor", None, value=1.0 if entry["flood"] else 0.0)
    else:  # Gen2+
        if gen is None and any(_COMPONENT_KEY.match(k) for k in entry):
            gen = "G2+"
        by_channel: dict[int, Sample] = {}
        for k, comp in entry.items():
            m = _OUTPUT_KEY.match(k)
            sm = _SENSOR_KEY.match(k)
            if not m and not sm:
                continue
            if not isinstance(comp, dict):
                raise ValueError(f"{k} is not an object")
            if m:
                kind, channel = _GEN2_KIND[m.group(1)], int(m.group(2))
                aenergy = comp.get("aenergy")
                energy = _number(aenergy.get("total")) if isinstance(aenergy, dict) else None
                if kind == "cover":
                    state = comp.get("state")
                    if not isinstance(state, str):
                        raise ValueError(f"{k}.state missing")
                    sample = Sample(ts, device_id, channel, state in ("opening", "closing"),
                                    _number(comp.get("apower")), True, energy, "home", "cover",
                                    _number(comp.get("current_pos")))
                else:
                    sample = Sample(ts, device_id, channel, _bool(comp.get("output"), f"{k}.output"),
                                    _number(comp.get("apower")), True, energy, "home", kind,
                                    _number(comp.get("brightness")))
            else:
                name, channel = sm.group(1), int(sm.group(2))
                if name == "temperature":
                    value = _number(comp.get("tC"))
                elif name == "humidity":
                    value, channel = _number(comp.get("rh")), HUMIDITY_OFFSET + channel
                else:
                    value = 1.0 if comp.get("alarm") is True else 0.0 if comp.get("alarm") is False else None
                if value is None:
                    continue
                sample = Sample(ts, device_id, channel, None, None, True, None, "home", "sensor", value)
            if channel in by_channel:
                log.warning("device %s: more than one component on channel %d, keeping the first",
                            device_id, channel)
                continue
            by_channel[channel] = sample
        samples = [by_channel[c] for c in sorted(by_channel)]

    if online is None:
        online = True  # no signal at all: the cloud reported a status, assume reachable
    samples = [replace(x, online=online) for x in samples]
    if not samples:
        log.debug("device %s (%s) has no output or sensor, registered without samples", device_id, model)
    return DeviceInfo(device_id, str(model) if model is not None else None,
                      str(gen) if gen is not None else None, "home", online), samples


def parse_all_status(data, ts: int, home: str = "home", stale_after: int | None = None
                     ) -> tuple[list[DeviceInfo], list[Sample]]:
    """Turn the 'data' object of /device/all_status into device infos and samples.

    Every device in the payload gets a DeviceInfo (it is in the cloud status, so
    its online flag is known), also those without samples (inputs only).
    `stale_after` (seconds) enables the `_updated` online fallback.
    Never raises on unexpected payloads: bad devices are skipped with a WARNING.
    """
    statuses = data.get("devices_status") if isinstance(data, dict) else None
    if not isinstance(statuses, dict):
        log.warning("all_status: 'devices_status' missing or not an object")
        return [], []
    devices: list[DeviceInfo] = []
    samples: list[Sample] = []
    for key, entry in statuses.items():
        if not isinstance(entry, dict):
            log.warning("device %s: status is not an object, skipped", key)
            continue
        try:
            parsed = _parse_device(str(key), entry, ts, stale_after)
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            log.warning("device %s: unrecognised payload (%s), skipped", key, e)
            continue
        if parsed:
            devices.append(replace(parsed[0], home=home))
            samples.extend(replace(x, home=home) for x in parsed[1])
    return devices, samples
