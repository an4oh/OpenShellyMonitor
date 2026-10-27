"""ON-time aggregation.

For each device/channel, consecutive samples (a, b) form an interval of
min(b.ts - a.ts, max_gap) seconds, credited when sample `a` is ON and online.
The interval is attributed to the period (local time) of a.ts.

Energy: when both samples carry the device's cumulative counter (energy_wh,
from aenergy.total) the delta b - a is used (a negative delta means a counter
reset and is ignored). Otherwise, for credited intervals, it is estimated as
a.power_w * interval.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TextIO

from .cloud import ON_KINDS
from .store import Store


@dataclass
class Row:
    period: str
    home: str
    device_id: str | None  # None in home/room rollups
    name: str
    channel: int | None  # None in home/room rollups
    room: str | None
    on_minutes: float
    wh: float | None
    wh_source: str | None = None  # "counter", "estimate", "mixed" (rollups) or None
    channels: int = 1  # number of channels summed into this row


BY = ("channel", "room", "home", "group")
NO_ROOM = "(no room)"


def parse_range(date_from: str | None, date_to: str | None) -> tuple[int | None, int | None]:
    """YYYY-MM-DD bounds, both inclusive, in local time -> [lo, hi) Unix seconds.

    Raises ValueError on malformed dates or when from > to.
    """
    lo = hi = None
    if date_from:
        lo = int(datetime.strptime(date_from, "%Y-%m-%d").timestamp())
    if date_to:
        hi = int((datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).timestamp())
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError("'from' is after 'to'")
    return lo, hi


def period_key(ts: int, group: str) -> str:
    if group == "total":
        return "total"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d" if group == "day" else "%Y-%m")


def aggregate(store: Store, from_ts: int | None, to_ts: int | None, group: str, max_gap: int,
              include_zero: bool = False, home: str | None = None, room: str | None = None,
              by: str = "channel", kinds=ON_KINDS, device: tuple[str, int] | None = None,
              channels: set | None = None) -> list[Row]:
    """ON time per (period, home, device, channel), optionally rolled up by room or home.

    include_zero=True adds 0-minute rows for every channel known in `devices`
    that has no row, in every period of the range that has at least one sample
    (any channel); with group="total" that is the single "total" period.
    `home` and `room` filter the channels. Only channels of `kinds` count (by
    default switches and lights; covers and sensors are excluded from ON time).
    `device` = (device_id, channel) restricts to one channel; `channels` to a set of
    (home, device_id, channel). by="group" sums the channels of each custom group (a channel in
    several groups counts in each of them).
    """
    # Fetch up to max_gap past the end so the last interval in range can be closed.
    fetch_to = to_ts + max_gap if to_ts is not None else None
    acc: dict[tuple, list] = {}
    # Memoise period keys per 15-minute bucket (every UTC offset is a multiple of 15 min,
    # so a bucket never straddles a local day/month boundary).
    period_cache: dict[int, str] = {}

    def period_of(ts: int) -> str:
        bucket = ts // 900
        key = period_cache.get(bucket)
        if key is None:
            key = period_cache[bucket] = period_key(bucket * 900, group)
        return key

    prev = None
    seen_periods: set[str] = set()
    for s in store.samples(from_ts, fetch_to, home=home, room=room, kinds=kinds, device=device, channels=channels):
        if include_zero and (to_ts is None or s["ts"] < to_ts):
            seen_periods.add(period_of(s["ts"]))
        if prev is not None and (prev["home"], prev["device_id"], prev["channel"]) == \
                (s["home"], s["device_id"], s["channel"]):
            if to_ts is None or prev["ts"] < to_ts:
                credited = bool(prev["is_on"] and prev["online"])
                dt = min(s["ts"] - prev["ts"], max_gap)
                wh = source = None
                if prev["energy_wh"] is not None and s["energy_wh"] is not None:
                    delta = s["energy_wh"] - prev["energy_wh"]
                    wh = delta if delta >= 0 else 0.0  # counter reset: ignore the interval
                    source = "counter"
                elif credited and prev["power_w"] is not None:
                    wh = prev["power_w"] * dt / 3600.0
                    source = "estimate"
                if credited or wh:
                    key = (period_of(prev["ts"]), prev["home"], prev["device_id"], prev["channel"])
                    slot = acc.setdefault(key, [0, None, set()])
                    if credited:
                        slot[0] += dt
                    if wh is not None:
                        slot[1] = (slot[1] or 0.0) + wh
                        slot[2].add(source)
        prev = s
    info = store.channel_info()

    def make(period, h, dev, ch, secs, wh, sources) -> Row:
        d = info.get((h, dev, ch))
        return Row(period, h, dev, (d["name"] if d is not None and d["name"] else dev), ch,
                   d["room"] if d is not None else None, round(secs / 60.0, 2),
                   round(wh, 2) if wh is not None else None,
                   "counter" if "counter" in sources else ("estimate" if sources else None))

    rows = [make(p, h, dev, ch, *v) for (p, h, dev, ch), v in acc.items()]
    if include_zero:
        known = {(d["home"], d["device_id"], d["channel"]) for d in store.devices(home, room, kinds, channels)
                 if device is None or (d["device_id"], d["channel"]) == device}
        if group == "total" and known:
            seen_periods.add("total")
        for period in seen_periods:
            for h, dev, ch in known:
                if (period, h, dev, ch) not in acc:
                    rows.append(make(period, h, dev, ch, 0, None, set()))
    if by == "group":
        rows = rollup_groups(rows, store.custom_group_channels())
    elif by != "channel":
        rows = rollup(rows, by)
    rows.sort(key=lambda r: (r.period, r.home, r.name, r.channel if r.channel is not None else -1))
    return rows


def rollup_groups(rows: list[Row], groups: dict[str, set]) -> list[Row]:
    """Sum channel rows per (period, custom group). Rows have home "" and the group name."""
    acc: dict[tuple, Row] = {}
    for r in rows:
        for gname, members in groups.items():
            if (r.home, r.device_id, r.channel) not in members:
                continue
            key = (r.period, gname)
            g = acc.get(key)
            if g is None:
                acc[key] = Row(r.period, "", None, gname, None, None, r.on_minutes, r.wh, r.wh_source, 1)
                continue
            g.on_minutes = round(g.on_minutes + r.on_minutes, 2)
            if r.wh is not None:
                g.wh = round((g.wh or 0.0) + r.wh, 2)
            if r.wh_source and g.wh_source and r.wh_source != g.wh_source:
                g.wh_source = "mixed"
            else:
                g.wh_source = g.wh_source or r.wh_source
            g.channels += 1
    return list(acc.values())


def rollup(rows: list[Row], by: str) -> list[Row]:
    """Sum channel rows per (period, home[, room])."""
    if by not in ("room", "home"):
        raise ValueError(f"unknown grouping {by!r}")
    acc: dict[tuple, Row] = {}
    for r in rows:
        room = r.room if by == "room" else None
        key = (r.period, r.home, room)
        g = acc.get(key)
        if g is None:
            label = (room or NO_ROOM) if by == "room" else r.home
            acc[key] = Row(r.period, r.home, None, label, None, room, r.on_minutes, r.wh, r.wh_source, 1)
            continue
        g.on_minutes = round(g.on_minutes + r.on_minutes, 2)
        if r.wh is not None:
            g.wh = round((g.wh or 0.0) + r.wh, 2)
        if r.wh_source and g.wh_source and r.wh_source != g.wh_source:
            g.wh_source = "mixed"
        else:
            g.wh_source = g.wh_source or r.wh_source
        g.channels += 1
    return list(acc.values())


FIELDS = ["period", "home", "device_id", "name", "channel", "room", "on_minutes", "wh", "wh_source", "channels"]


def _cell(v) -> str:
    return "" if v is None else str(v)


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(v) -> str:
    """CSV cell safe to open in a spreadsheet: text starting like a formula gets a leading quote.

    Only `str` values are touched, so negative numbers stay numbers."""
    if isinstance(v, str):
        return "'" + v if v.startswith(_FORMULA_START) else v
    return _cell(v)


def render(rows: list[Row], fmt: str, by: str = "channel") -> str:
    if fmt == "json":
        return json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False) + "\n"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(FIELDS)
        for r in rows:
            w.writerow([_safe_cell(getattr(r, f)) for f in FIELDS])
        return buf.getvalue()
    multi_home = len({r.home for r in rows}) > 1
    cols: list[tuple[str, callable, bool]] = [("period", lambda r: r.period, False)]
    if (multi_home or by == "home") and by != "group":
        cols.append(("home", lambda r: r.home, False))
    if by == "group":
        cols.append(("group", lambda r: r.name, False))
    if by == "channel":
        cols.append(("device", lambda r: r.name, False))
        if any(r.room for r in rows):
            cols.append(("room", lambda r: r.room or "-", False))
        cols.append(("ch", lambda r: str(r.channel), True))
    elif by == "room":
        cols.append(("room", lambda r: r.name, False))
    if by != "channel":
        cols.append(("channels", lambda r: str(r.channels), True))
    cols += [
        ("on_min", lambda r: f"{r.on_minutes:.1f}", True),
        ("on_h", lambda r: f"{r.on_minutes / 60:.2f}", True),
        ("wh", lambda r: "-" if r.wh is None else f"{r.wh:.1f}", True),
    ]
    table = [[c[0] for c in cols]] + [[c[1](r) for c in cols] for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(cols))]
    out = []
    for n, line in enumerate(table):
        out.append("  ".join(cell.rjust(w) if cols[i][2] else cell.ljust(w)
                             for i, (cell, w) in enumerate(zip(line, widths))).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    if not rows:
        out.append("(no data)")
    return "\n".join(out) + "\n"


SAMPLE_FIELDS = ["ts", "iso_time", "home", "device_id", "channel", "room", "kind", "is_on", "value", "power_w",
                 "online", "energy_wh"]


def write_samples_csv(store: Store, lo: int | None, hi: int | None, out: TextIO,
                      home: str | None = None, room: str | None = None, channels: set | None = None) -> None:
    info = store.channel_info()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(SAMPLE_FIELDS)
    for s in store.samples(lo, hi, home=home, room=room, channels=channels):
        d = info.get((s["home"], s["device_id"], s["channel"]))
        w.writerow([s["ts"], datetime.fromtimestamp(s["ts"]).isoformat(), _safe_cell(s["home"]),
                    _safe_cell(s["device_id"]), s["channel"], _safe_cell(d["room"] if d is not None else None),
                    _safe_cell(s["kind"] or "switch"), _cell(s["is_on"]),
                    _cell(s["value"]), _cell(s["power_w"]), s["online"], _cell(s["energy_wh"])])
