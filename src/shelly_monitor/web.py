"""Web dashboard (stdlib http.server, no authentication): reads the DB read-only; custom groups are
writable unless SHELLY_WEB_READONLY=1."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import base64
import hashlib
import re
import signal
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .cloud import EVENT_KINDS, ON_KINDS, event_details
from .report import BY, SAMPLE_FIELDS, _safe_cell, aggregate, parse_range, render, write_samples_csv
from .store import Store

log = logging.getLogger(__name__)

MAX_RANGE_DAYS = 1100  # from/to span (inclusive) accepted by every endpoint
MAX_EXPORT_ROWS = 100_000  # raw samples in one /api/export.csv
MAX_EVENT_ROWS = 1000  # activity log rows in one /api/device/events (newest first)


def _sha256_sources(html: str, tag: str) -> str:
    return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(m.encode()).digest()).decode() + "'"
                    for m in re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S))


def content_security_policy(index: bytes) -> str:
    """CSP allowing only the inline <script>/<style> blocks of `index`, by hash (no 'unsafe-inline')."""
    html = index.decode()
    return (f"default-src 'none'; script-src 'self' {_sha256_sources(html, 'script')}; "
            f"style-src 'self' {_sha256_sources(html, 'style')}; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


SECURITY_HEADERS = {  # Content-Security-Policy is added per server by make_server (hashes of index.html)
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
GROUPS = ("day", "month", "total")


class BadRequest(ValueError):
    pass


class NotFound(LookupError):
    pass


class Unsupported(ValueError):
    pass


def _index_html() -> bytes:
    return resources.files("shelly_monitor").joinpath("web/index.html").read_bytes()


class Dashboard:
    """Query logic, independent of HTTP. One read-only connection per call."""

    def __init__(self, db: str, interval: int, readonly: bool = False):
        self.db = db
        self.interval = interval
        self.readonly = readonly

    def _store(self) -> Store | None:
        if not Path(self.db).is_file():
            return None
        return Store(self.db, readonly=True)

    @staticmethod
    def _range(q: dict) -> tuple[int | None, int | None]:
        try:
            lo, hi = parse_range(q.get("from"), q.get("to"))
        except (ValueError, OverflowError, OSError) as e:
            raise BadRequest(f"invalid date range: {e}; expected from/to as YYYY-MM-DD") from None
        if lo is not None and hi is not None and round((hi - lo) / 86400) > MAX_RANGE_DAYS:
            raise BadRequest(f"date range too long: at most {MAX_RANGE_DAYS} days")
        return lo, hi

    @staticmethod
    def _where(q: dict, store: Store | None = None) -> dict:
        """home= / room= / grp= filters (empty string = no filter).

        grp=c:<id> is a custom group, grp=d:<home>/<group_id> a cloud group; both become a
        set of channels. Unknown groups raise NotFound."""
        where = {"home": q.get("home") or None, "room": q.get("room") or None, "channels": None}
        grp = q.get("grp") or ""
        if grp and store is not None:
            kind, _, ref = grp.partition(":")
            if kind == "c":
                g = store.custom_group(ref) if ref.isdigit() else None
                if g is None:
                    raise NotFound("custom group not found")
                where["channels"] = {(m["home"], m["device_id"], m["channel"]) for m in g["members"] if m["home"]}
            elif kind == "d" and "/" in ref:
                home, _, gid = ref.rpartition("/")
                where["channels"] = store.cloud_group_channels(home, gid)
            else:
                raise BadRequest("grp must be c:<id> or d:<home>/<group_id>")
        return where

    def _rows(self, store: Store, lo, hi, group: str, q: dict, **kw) -> list:
        return aggregate(store, lo, hi, group, max_gap=2 * self.interval, **self._where(q, store), **kw)

    def summary(self, q: dict) -> dict:
        lo, hi = self._range(q)
        out = {
            "version": __version__, "interval_s": self.interval, "from": q.get("from"), "to": q.get("to"),
            "home": q.get("home") or None, "room": q.get("room") or None, "grp": q.get("grp") or None,
            "days": round((hi - lo) / 86400) if lo is not None and hi is not None else None,
            "on_hours": 0.0, "wh": None, "wh_counter_channels": 0, "wh_estimate_channels": 0,
            "channels": 0, "channels_on": 0, "devices": 0, "devices_offline": 0, "homes": 0,
            "last_sample_ts": None, "samples": 0,
        }
        store = self._store()
        if store is None:
            return out
        with store:
            where = self._where(q, store)
            rows = self._rows(store, lo, hi, "total", q)
            latest = store.latest(**where)
            out["samples"] = store.count_samples(lo, hi, **where)
        out["on_hours"] = round(sum(r.on_minutes for r in rows) / 60, 2)
        whs = [r.wh for r in rows if r.wh is not None]
        out["wh"] = round(sum(whs), 2) if whs else None
        out["wh_counter_channels"] = sum(r.wh_source == "counter" for r in rows)
        out["wh_estimate_channels"] = sum(r.wh_source == "estimate" for r in rows)
        out["channels"] = len(latest)
        out["channels_on"] = sum(bool(s["is_on"] and s["online"]) for s in latest.values())
        out["devices"] = len({(h, dev) for h, dev, _ in latest})
        out["devices_offline"] = len({(h, dev) for (h, dev, _), s in latest.items() if not s["online"]})
        out["homes"] = len({h for h, _, _ in latest})
        out["last_sample_ts"] = max((s["ts"] for s in latest.values()), default=None)
        return out

    def report(self, q: dict) -> list[dict]:
        lo, hi = self._range(q)
        group = q.get("group", "total")
        if group not in GROUPS:
            raise BadRequest("group must be day, month or total")
        by = q.get("by", "channel")
        if by not in BY:
            raise BadRequest("by must be channel, room, home or group")
        store = self._store()
        if store is None:
            return []
        with store:
            rows = self._rows(store, lo, hi, group, q, include_zero=True, by=by)
            latest = store.latest(**self._where(q, store)) if by == "channel" else {}
            info = store.channel_info()
        result = []
        for r in rows:
            item = {
                "period": r.period, "home": r.home, "room": r.room, "name": r.name,
                "on_min": r.on_minutes, "on_h": round(r.on_minutes / 60, 2),
                "wh": r.wh, "wh_source": r.wh_source, "channels": r.channels,
            }
            if by == "channel":
                key = (r.home, r.device_id, r.channel)
                last, d = latest.get(key), info.get(key)
                item.update({
                    "device_id": r.device_id, "channel": r.channel,
                    "model": d["model"] if d is not None else None,
                    "state": None if last is None else ("on" if last["is_on"] else "off"),
                    "online": None if last is None else bool(last["online"]),
                    "last_ts": None if last is None else last["ts"],
                })
            result.append(item)
        return result

    def daily(self, q: dict) -> list[dict]:
        lo, hi = self._range(q)
        per_day: dict[str, float] = {}
        store = self._store()
        if store is not None:
            with store:
                for r in self._rows(store, lo, hi, "day", q):
                    per_day[r.period] = per_day.get(r.period, 0.0) + r.on_minutes
        if lo is not None and hi is not None:
            day, end = date.fromtimestamp(lo), date.fromtimestamp(hi - 1)
            while day <= end:
                per_day.setdefault(day.isoformat(), 0.0)
                day += timedelta(days=1)
        return [{"date": d, "on_h": round(m / 60, 2)} for d, m in sorted(per_day.items())]

    @staticmethod
    def _state(kind: str | None, last) -> tuple[str | None, float | None]:
        if last is None:
            return None, None
        kind = kind or "switch"
        if kind == "sensor":
            return None, last["value"]
        if kind == "cover":
            return ("moving" if last["is_on"] else "stopped"), last["value"]
        if last["is_on"] is None:
            return None, last["value"]
        return ("on" if last["is_on"] else "off"), last["value"]

    def devices(self, q: dict) -> list[dict]:
        """Every registered channel, with current state and, with from/to, ON hours."""
        lo, hi = self._range(q)
        store = self._store()
        if store is None:
            return []
        with store:
            where = self._where(q, store)
            rows = store.devices(**where)
            latest = store.latest(**where, kinds=None)
            groups = store.channel_groups(where["home"])
            on = {}
            if lo is not None and hi is not None:
                for r in aggregate(store, lo, hi, "total", 2 * self.interval, kinds=ON_KINDS + ("cover",),
                                   **where):
                    on[(r.home, r.device_id, r.channel)] = r
        group, text = q.get("group") or None, (q.get("q") or "").strip().casefold()
        out = []
        for d in rows:
            key = (d["home"], d["device_id"], d["channel"])
            g = groups.get(key, [])
            if group is not None and group not in g:
                continue
            item = self._device_item(d, latest.get(key), g, on.get(key))
            if text and not any(text in str(v).casefold() for v in
                                (item["name"], item["device_id"], item["room"], item["type"], item["kind"], *g)
                                if v):
                continue
            out.append(item)
        return out

    def _device_item(self, d, last, groups, period=None) -> dict:
        state, value = self._state(d["kind"], last)
        return {
            "home": d["home"], "device_id": d["device_id"], "channel": d["channel"],
            "name": d["name"], "room": d["room"],
            "model": d["model"] or d["type"], "type": d["type"] or d["model"], "gen": d["gen"],
            "kind": d["kind"] or ("switch" if d["last_seen"] is not None else "unknown"),
            "channels_count": d["channels_count"],
            "online": None if d["online"] is None else bool(d["online"]),
            "state": state, "value": value, "last_seen": d["last_seen"], "groups": groups,
            "on_h": round(period.on_minutes / 60, 2) if period is not None else (0.0 if d["last_seen"] else None),
            "wh": period.wh if period is not None else None,
        }

    def groups(self, q: dict) -> list[dict]:
        store = self._store()
        if store is None:
            return []
        with store:
            rooms = {(d["home"], d["room_id"]): d["room"] for d in store.devices() if d["room_id"] is not None}
            return [{"home": g["home"], "group_id": g["group_id"], "name": g["name"], "type": g["type"],
                     "room": rooms.get((g["home"], g["room_id"])), "members": g["members"]}
                    for g in store.groups(q.get("home") or None)]

    MAX_TRANSITIONS = 5000

    def device(self, q: dict) -> dict:
        """One channel: info, compressed transitions and daily ON hours in the period."""
        home, device_id = q.get("home"), q.get("id")
        if not home or not device_id or not (q.get("ch") or "").isdigit():
            raise BadRequest("home, id and ch (integer) are required")
        channel = int(q["ch"])
        lo, hi = self._range(q)
        store = self._store()
        if store is None:
            raise NotFound("device not found")
        max_gap = 2 * self.interval
        with store:
            row = next((d for d in store.devices(home) if (d["device_id"], d["channel"]) == (device_id, channel)), None)
            if row is None:
                raise NotFound("device not found")
            last = store.latest(home, kinds=None).get((home, device_id, channel))
            groups = store.channel_groups(home).get((home, device_id, channel), [])
            custom = [g for g, members in store.custom_group_channels().items()
                      if (home, device_id, channel) in members]
            transitions, prev_ts, prev_key, samples, first_ts = [], None, None, 0, None
            for smp in store.samples(lo, hi, home=home, device=(device_id, channel)):
                samples += 1
                first_ts = smp["ts"] if first_ts is None else first_ts
                if prev_ts is not None and smp["ts"] - prev_ts > max_gap:
                    transitions.append({"ts": prev_ts + self.interval, "gap": True})
                    prev_key = None
                key = (smp["is_on"], smp["value"], smp["online"])
                if key != prev_key:
                    transitions.append({"ts": smp["ts"], "is_on": None if smp["is_on"] is None else bool(smp["is_on"]),
                                        "value": smp["value"], "online": bool(smp["online"])})
                    prev_key = key
                prev_ts = smp["ts"]
            daily = {r.period: r for r in aggregate(store, lo, hi, "day", max_gap, kinds=None, home=home,
                                                    device=(device_id, channel))}
        truncated = len(transitions) > self.MAX_TRANSITIONS
        if truncated:  # keep an even subset, always with the first and the last point
            step = len(transitions) / self.MAX_TRANSITIONS
            transitions = [transitions[int(i * step)] for i in range(self.MAX_TRANSITIONS - 1)] + [transitions[-1]]
        days = []
        if lo is not None and hi is not None:
            day, end = date.fromtimestamp(lo), date.fromtimestamp(hi - 1)
            while day <= end:
                days.append(day.isoformat())
                day += timedelta(days=1)
        else:
            days = sorted(daily)
        info = self._device_item(row, last, groups)
        info.pop("on_h"); info.pop("wh")
        return {
            **info, "interval_s": self.interval, "from": q.get("from"), "to": q.get("to"),
            "custom_groups": custom,
            "samples": samples, "first_ts": first_ts, "last_ts": prev_ts,
            "transitions": transitions, "truncated": truncated,
            "last_sample": None if last is None else {"ts": last["ts"], "is_on": last["is_on"], "value": last["value"]},
            "daily": [{"day": d, "on_h": round(daily[d].on_minutes / 60, 2) if d in daily else 0.0,
                       "wh": daily[d].wh if d in daily else None} for d in days],
        }


    @staticmethod
    def _channel_ref(q: dict) -> tuple[str, str, int]:
        home, device_id = q.get("home"), q.get("id")
        if not home or not device_id or not (q.get("ch") or "").isdigit():
            raise BadRequest("home, id and ch (integer) are required")
        return home, device_id, int(q["ch"])

    def _events(self, q: dict, limit: int) -> tuple[str, str, int, list[dict], int]:
        home, device_id, channel = self._channel_ref(q)
        lo, hi = self._range(q)
        store = self._store()
        if store is None:
            raise NotFound("device not found")
        with store:
            if not any((d["device_id"], d["channel"]) == (device_id, channel) for d in store.devices(home)):
                raise NotFound("device not found")
            return (home, device_id, channel, store.events_for(home, device_id, channel, lo, hi, limit),
                    store.count_events(home, device_id, channel, lo, hi))

    @staticmethod
    def _event_item(e: dict) -> dict:
        arg, source = event_details(e["payload"])
        return {"ts_ms": e["ts_ms"], "event": e["event"], "kind": EVENT_KINDS.get(e["event"], "other"),
                "is_on": e["is_on"], "arg": arg, "source": source, "rowid": e["rowid"]}

    def device_events(self, q: dict) -> dict:
        """Activity log of one channel in the period, newest first (at most MAX_EVENT_ROWS)."""
        _, _, _, rows, total = self._events(q, MAX_EVENT_ROWS)
        return {"events": [self._event_item(e) for e in rows], "total": total, "truncated": total > len(rows),
                "limit": MAX_EVENT_ROWS}

    def device_events_csv(self, q: dict) -> bytes:
        home, device_id, channel, rows, _ = self._events(q, MAX_EXPORT_ROWS)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["ts_ms", "iso_time", "home", "device_id", "channel", "event", "kind", "is_on", "arg", "source",
                    "payload"])
        for e in rows:
            it = self._event_item(e)
            arg = it["arg"]
            w.writerow([e["ts_ms"], datetime.fromtimestamp(e["ts_ms"] / 1000).isoformat(timespec="milliseconds"),
                        _safe_cell(home), _safe_cell(device_id), channel, e["event"], it["kind"],
                        "" if e["is_on"] is None else int(e["is_on"]),
                        "" if arg is None else int(arg) if isinstance(arg, bool) else _safe_cell(arg),
                        _safe_cell(it["source"] or ""), _safe_cell(e["payload"])])
        return buf.getvalue().encode()

    def homes(self, q: dict) -> list[dict]:
        store = self._store()
        if store is None:
            return []
        with store:
            return [{k: v for k, v in h.items() if k != "server"} | {"room_count": len(h["rooms"])}
                    for h in store.homes()]

    def export_csv(self, q: dict) -> bytes:
        lo, hi = self._range(q)
        buf = io.StringIO()
        store = self._store()
        if store is None:
            buf.write(",".join(SAMPLE_FIELDS) + "\n")
        else:
            with store:
                where = self._where(q, store)
                if store.count_samples(lo, hi, **where) > MAX_EXPORT_ROWS:
                    raise BadRequest(f"too many samples: at most {MAX_EXPORT_ROWS} rows per export, "
                                     "narrow the date range or the filters")
                write_samples_csv(store, lo, hi, buf, **where)
        return buf.getvalue().encode()

    def report_csv(self, q: dict) -> bytes:
        """The aggregated report, same columns as `report --format csv`."""
        lo, hi = self._range(q)
        group, by = q.get("group", "total"), q.get("by", "channel")
        if group not in GROUPS:
            raise BadRequest("group must be day, month or total")
        if by not in BY:
            raise BadRequest("by must be channel, room or home")
        store = self._store()
        if store is None:
            return render([], "csv", by).encode()
        with store:
            rows = self._rows(store, lo, hi, group, q, include_zero=True, by=by)
        return render(rows, "csv", by).encode()

    def device_csv(self, q: dict) -> bytes:
        """Transitions of one channel with their duration."""
        d = self.device(q)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["ts", "iso_time", "home", "device_id", "channel", "kind", "state", "is_on", "value", "online",
                    "duration_s"])
        tr = d["transitions"]
        end = (d["last_ts"] + self.interval) if d["last_ts"] else None
        for i, t in enumerate(tr):
            nxt = tr[i + 1]["ts"] if i + 1 < len(tr) else end
            if t.get("gap"):
                state = "no_data"
            elif not t["online"]:
                state = "offline"
            elif d["kind"] == "sensor":
                state = "value"
            elif d["kind"] == "cover":
                state = "moving" if t["is_on"] else "stopped"
            else:
                state = "on" if t["is_on"] else "off"
            w.writerow([t["ts"], datetime.fromtimestamp(t["ts"]).isoformat(), _safe_cell(d["home"]),
                        _safe_cell(d["device_id"]), d["channel"], _safe_cell(d["kind"]), state, "" if t.get("is_on") is None else int(t["is_on"]),
                        "" if t.get("value") is None else t["value"], "" if t.get("gap") else int(t["online"]),
                        "" if nxt is None else nxt - t["ts"]])
        return buf.getvalue().encode()

    def report_pdf(self, q: dict) -> bytes:
        from .pdfreport import report_pdf
        self._range(q)  # validate before building
        return report_pdf(self, q)

    def device_pdf(self, q: dict) -> bytes:
        from .pdfreport import device_pdf
        return device_pdf(self, q)


def _filename(prefix: str, q: dict, ext: str) -> str:
    parts = [prefix, q.get("home") or "", q.get("id") or "", q.get("from") or "start", q.get("to") or "end"]
    name = "_".join(p for p in parts if p) + "." + ext
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


class Forbidden(PermissionError):
    pass


def _parse_ref(m: dict) -> tuple[str, str, int]:
    if not isinstance(m, dict):
        raise BadRequest("each member must be an object")
    home, device_id, channel = m.get("key_id") or m.get("home"), m.get("device_id"), m.get("channel")
    if not isinstance(home, str) or not isinstance(device_id, str) or not isinstance(channel, int) \
            or isinstance(channel, bool):
        raise BadRequest("member needs key_id (or home), device_id and integer channel")
    return home, device_id, channel


class Groups:
    """Custom groups: read through the dashboard, written with a read-write store."""

    def __init__(self, dash: Dashboard):
        self.dash = dash

    def config(self, q: dict) -> dict:
        return {"version": __version__, "readonly": self.dash.readonly, "interval_s": self.dash.interval}

    def list(self, q: dict) -> list[dict]:
        lo, hi = self.dash._range(q)
        store = self.dash._store()
        if store is None:
            return []
        with store:
            groups = store.custom_groups()
            totals = {}
            if lo is not None and hi is not None and groups:
                totals = {r.name: r for r in aggregate(store, lo, hi, "total", 2 * self.dash.interval,
                                                       include_zero=True, by="group")}
        for g in groups:
            t = totals.get(g["name"])
            g["on_h"] = round(t.on_minutes / 60, 2) if t else (0.0 if lo is not None else None)
            g["wh"] = t.wh if t else None
            g["wh_source"] = t.wh_source if t else None
        return groups

    def _write(self):
        if self.dash.readonly:
            raise Forbidden("the dashboard is read-only (SHELLY_WEB_READONLY=1)")
        return Store(self.dash.db)

    def _one(self, store: Store, group_id: int) -> dict:
        g = store.custom_group(group_id)
        if g is None:
            raise NotFound("custom group not found")
        return g

    def save(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise BadRequest("expected a JSON object")
        gid, name, color = body.get("id"), body.get("name"), body.get("color")
        if gid is not None and (not isinstance(gid, int) or isinstance(gid, bool)):
            raise BadRequest("id must be an integer")
        if not isinstance(name, str):
            raise BadRequest("name is required")
        if color is not None and not isinstance(color, str):
            raise BadRequest("color must be #rrggbb")
        with self._write() as store:
            try:
                gid = store.save_custom_group(name, color, gid)
            except ValueError as e:
                raise BadRequest(str(e)) from None
            except KeyError:
                raise NotFound("custom group not found") from None
            return self._one(store, gid)

    def members(self, group_id: int, body: dict) -> dict:
        members = body.get("members") if isinstance(body, dict) else None
        if not isinstance(members, list) or len(members) > 5000:
            raise BadRequest("members must be a list (at most 5000)")
        refs = [_parse_ref(m) for m in members]
        with self._write() as store:
            try:
                store.set_custom_group_members(group_id, refs)
            except ValueError as e:
                raise BadRequest(str(e)) from None
            except KeyError:
                raise NotFound("custom group not found") from None
            return self._one(store, group_id)

    def delete(self, group_id: int) -> dict:
        with self._write() as store:
            try:
                store.delete_custom_group(group_id)
            except KeyError:
                raise NotFound("custom group not found") from None
        return {"deleted": group_id}


# Queries iterate many SQLite rows; the sqlite3 module releases the GIL around every step, so
# concurrent queries in threads contend for the GIL (convoy effect) and run several times slower
# than one after the other. Database work is therefore serialised; the server stays threaded so
# that static responses and slow clients do not block each other.
_DB_LOCK = threading.Lock()

_GROUP_PATH = re.compile(r"^/api/custom-groups/(\d+)(/members)?$")
MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    server_version = f"shelly-monitor/{__version__}"
    sys_version = ""
    timeout = 30  # seconds of socket inactivity before a (slow or idle) client is dropped
    dashboard: Dashboard  # set by make_server
    index: bytes
    security_headers: dict = SECURITY_HEADERS  # with the CSP, set by make_server

    groups: Groups  # set by make_server
    GROUP_ROUTES = {"/api/custom-groups": "list", "/api/config": "config"}

    JSON_ROUTES = {
        "/api/summary": "summary",
        "/api/report": "report",
        "/api/daily": "daily",
        "/api/devices": "devices",
        "/api/homes": "homes",
        "/api/groups": "groups",
        "/api/device": "device",
        "/api/device/events": "device_events",
    }

    FILE_ROUTES = {  # path -> (Dashboard method, file name prefix, extension, content type)
        "/api/export.csv": ("export_csv", "shelly-samples", "csv", "text/csv; charset=utf-8"),
        "/api/report.csv": ("report_csv", "shelly-report", "csv", "text/csv; charset=utf-8"),
        "/api/report.pdf": ("report_pdf", "shelly-report", "pdf", "application/pdf"),
        "/api/device.csv": ("device_csv", "shelly-device", "csv", "text/csv; charset=utf-8"),
        "/api/device.pdf": ("device_pdf", "shelly-device", "pdf", "application/pdf"),
        "/api/device-events.csv": ("device_events_csv", "shelly-device-events", "csv", "text/csv; charset=utf-8"),
    }

    def log_request(self, code="-", size="-"):
        level = logging.INFO if isinstance(code, int) and code >= 400 else logging.DEBUG
        log.log(level, '%s "%s" %s', self.address_string(), self.requestline, code)

    def log_message(self, format, *args):
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**self.security_headers, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def send_error(self, code, message=None, explain=None):
        """Errors raised by http.server itself (400 bad request line, 414, 431, 501 unknown method,
        505...): JSON with the same security headers as every other response."""
        self.log_error("code %d, message %s", code, message)
        self.close_connection = True
        self._json(code, {"error": message or self.responses.get(code, ("error",))[0]}, {"Connection": "close"})

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            q = {k: v[-1] for k, v in parse_qs(url.query, strict_parsing=False).items()}
            if url.path in ("/", "/index.html"):
                self._send(200, self.index, "text/html; charset=utf-8")
            elif url.path in self.JSON_ROUTES:
                with _DB_LOCK:
                    result = getattr(self.dashboard, self.JSON_ROUTES[url.path])(q)
                self._json(200, result)
            elif url.path in self.GROUP_ROUTES:
                with _DB_LOCK:
                    result = getattr(self.groups, self.GROUP_ROUTES[url.path])(q)
                self._json(200, result)
            elif url.path in self.FILE_ROUTES:
                method, prefix, ext, ctype = self.FILE_ROUTES[url.path]
                with _DB_LOCK:
                    body = getattr(self.dashboard, method)(q)
                self._send(200, body, ctype,
                           {"Content-Disposition": f'attachment; filename="{_filename(prefix, q, ext)}"'})
            else:
                self._json(404, {"error": "not found"})
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except NotFound as e:
            self._json(404, {"error": str(e)})
        except sqlite3.Error as e:
            log.error("database error on %s: %s", url.path, e)
            self._json(500, {"error": "database error"})
        except Exception:
            log.exception("unexpected error on %s", url.path)
            self._json(500, {"error": "internal error"})

    do_HEAD = do_GET

    def _body(self):
        """JSON body of a write. CSRF guard: a custom header (not settable by plain HTML forms or
        cross-site requests without a CORS preflight) and a JSON content type are required."""
        if self.headers.get("X-Requested-With") != "shelly-monitor":
            raise Forbidden("missing X-Requested-With: shelly-monitor header")
        if self.command == "DELETE":
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Unsupported("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if length <= 0 or length > MAX_BODY:
            raise BadRequest("missing or too large body")
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            raise BadRequest("invalid JSON") from None

    def _write(self):
        url = urlsplit(self.path)
        try:
            if self.groups.dash.readonly:
                raise Forbidden("the dashboard is read-only (SHELLY_WEB_READONLY=1)")
            m = _GROUP_PATH.match(url.path)
            if self.command == "POST" and url.path == "/api/custom-groups":
                body = self._body()
                with _DB_LOCK:
                    result = self.groups.save(body)
                self._json(200, result)
            elif self.command == "PUT" and m and m.group(2):
                body = self._body()
                with _DB_LOCK:
                    result = self.groups.members(int(m.group(1)), body)
                self._json(200, result)
            elif self.command == "DELETE" and m and not m.group(2):
                self._body()
                with _DB_LOCK:
                    result = self.groups.delete(int(m.group(1)))
                self._json(200, result)
            else:
                self._json(404 if not url.path.startswith("/api/custom-groups") else 405,
                           {"error": "not found" if not url.path.startswith("/api/custom-groups")
                            else "method not allowed"})
        except Forbidden as e:
            self._json(403, {"error": str(e)})
        except Unsupported as e:
            self._json(415, {"error": str(e)})
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except NotFound as e:
            self._json(404, {"error": str(e)})
        except sqlite3.Error as e:
            log.error("database error on %s %s: %s", self.command, url.path, e)
            self._json(500, {"error": "database error"})
        except Exception:
            log.exception("unexpected error on %s %s", self.command, url.path)
            self._json(500, {"error": "internal error"})

    do_POST = do_PUT = do_DELETE = _write


def make_server(db: str, interval: int, host: str, port: int, readonly: bool | None = None) -> ThreadingHTTPServer:
    if readonly is None:
        readonly = os.environ.get("SHELLY_WEB_READONLY", "").strip() == "1"
    dash = Dashboard(db, interval, readonly)
    index = _index_html()
    headers = {**SECURITY_HEADERS, "Content-Security-Policy": content_security_policy(index)}
    handler = type("BoundHandler", (Handler,), {"dashboard": dash, "groups": Groups(dash), "index": index,
                                                "security_headers": headers})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(db: str, interval: int, host: str, port: int, migrate_home: str = "home") -> int:
    if Path(db).is_file():
        try:  # apply schema migrations once, so read-only connections see the current schema
            Store(db, home=migrate_home).close()
        except sqlite3.Error as e:
            log.warning("could not open %s for migration (%s); serving read-only as is", db, e)
    else:
        log.warning("database %s not found yet: the dashboard will be empty until the poller writes it", db)
    try:
        server = make_server(db, interval, host, port)
    except OSError as e:
        log.error("cannot listen on %s:%d: %s", host, port, e)
        return 1

    def _stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    log.info("dashboard on http://%s:%d/ (no authentication: keep it on localhost or behind a proxy; %s)",
             host, server.server_address[1],
             "read-only" if server.RequestHandlerClass.groups.dash.readonly else "custom groups editable")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0
