import json
from datetime import datetime

import pytest

from shelly_monitor.cloud import Sample
from shelly_monitor.report import aggregate, render
from shelly_monitor.store import Store

BASE = int(datetime(2026, 10, 1, 12, 0).timestamp())


@pytest.fixture
def store(tmp_path):
    with Store(str(tmp_path / "r.db")) as st:
        yield st


def put(st, rows):
    st.save_poll([], [Sample(BASE + t, dev, ch, on, p, online) for t, dev, ch, on, p, online in rows], BASE)


def test_minutes_with_gap_capped(store):
    # ON at 0, 60, 120; then a 10-minute hole; ON at 720, OFF at 780
    put(store, [(0, "d", 0, True, None, True), (60, "d", 0, True, None, True),
                (120, "d", 0, True, None, True), (720, "d", 0, True, None, True),
                (780, "d", 0, False, None, True), (840, "d", 0, False, None, True)])
    (row,) = aggregate(store, None, None, "total", max_gap=120)
    # 60 + 60 + capped 120 + 60 = 300 s
    assert row.on_minutes == 5.0 and row.wh is None


def test_offline_and_off_not_counted(store):
    put(store, [(0, "d", 0, True, None, False), (60, "d", 0, False, None, True), (120, "d", 0, True, None, True)])
    assert aggregate(store, None, None, "total", max_gap=120) == []


def test_wh_and_channels(store):
    put(store, [(0, "d", 0, True, 1200.0, True), (60, "d", 0, True, 600.0, True), (120, "d", 0, False, 0, True),
                (0, "d", 1, True, None, True), (60, "d", 1, False, None, True)])
    rows = {r.channel: r for r in aggregate(store, None, None, "day", max_gap=120)}
    assert rows[0].on_minutes == 2.0 and rows[0].wh == 30.0  # 1200*60/3600 + 600*60/3600
    assert rows[1].on_minutes == 1.0 and rows[1].wh is None
    assert rows[0].period == "2026-10-01"


def test_range_and_grouping(store):
    day2 = int(datetime(2026, 10, 2, 12, 0).timestamp()) - BASE
    put(store, [(0, "d", 0, True, None, True), (60, "d", 0, False, None, True),
                (day2, "d", 0, True, None, True), (day2 + 60, "d", 0, False, None, True)])
    by_day = aggregate(store, None, None, "day", 120)
    assert [(r.period, r.on_minutes) for r in by_day] == [("2026-10-01", 1.0), ("2026-10-02", 1.0)]
    assert [r.on_minutes for r in aggregate(store, None, None, "month", 120)] == [2.0]
    lo = int(datetime(2026, 10, 2).timestamp())
    assert [r.period for r in aggregate(store, lo, None, "day", 120)] == ["2026-10-02"]
    # interval starting before `to` is closed by a sample just after it
    to = BASE + 30
    (r,) = aggregate(store, None, to, "day", 120)
    assert r.on_minutes == 1.0


def test_names_and_render(store):
    put(store, [(0, "d", 0, True, 100.0, True), (60, "d", 0, False, 0, True)])
    store.rename("home", "d", 0, "Flat A")
    rows = aggregate(store, None, None, "total", 120)
    assert rows[0].name == "Flat A"
    assert json.loads(render(rows, "json"))[0]["on_minutes"] == 1.0
    csv_out = render(rows, "csv").splitlines()
    assert csv_out[0] == "period,home,device_id,name,channel,room,on_minutes,wh,wh_source,channels"
    assert csv_out[1].startswith("total,home,d,Flat A,0,,1.0,")
    assert "Flat A" in render(rows, "table")
    assert "(no data)" in render([], "table")


def test_energy_counter_delta_preferred_and_reset_ignored(store):
    st = store
    st.save_poll([], [
        Sample(BASE, "e", 0, True, 9999.0, True, 100.0),
        Sample(BASE + 60, "e", 0, True, 9999.0, True, 110.0),
        Sample(BASE + 120, "e", 0, True, 9999.0, True, 2.0),     # counter reset
        Sample(BASE + 180, "e", 0, False, 0.0, True, 5.0),
        Sample(BASE + 240, "e", 0, False, 0.0, True, 5.0),
    ], BASE)
    (row,) = aggregate(st, None, None, "total", max_gap=120)
    assert row.on_minutes == 3.0
    assert row.wh == 13.0  # 10 + (reset ignored) + 3


def test_energy_estimate_when_counter_missing_on_one_side(store):
    store.save_poll([], [
        Sample(BASE, "e", 0, True, 600.0, True, None),
        Sample(BASE + 60, "e", 0, False, 0.0, True, 50.0),
    ], BASE)
    (row,) = aggregate(store, None, None, "total", max_gap=120)
    assert row.wh == 10.0


def test_include_zero(store):
    put(store, [(0, "d", 0, True, None, True), (60, "d", 0, False, None, True),
                (0, "off", 0, False, None, True), (60, "off", 0, False, None, True)])
    from shelly_monitor.cloud import DeviceInfo
    store.save_poll([DeviceInfo("off", "M", "G2+")], [Sample(BASE + 120, "off", 0, False, None, True)], BASE + 120)
    assert [r.device_id for r in aggregate(store, None, None, "total", 120)] == ["d"]
    rows = aggregate(store, None, None, "total", 120, include_zero=True)
    assert [(r.device_id, r.on_minutes, r.wh) for r in rows] == [("d", 1.0, None), ("off", 0.0, None)]
    lo = int(datetime(2026, 10, 1).timestamp())
    hi = int(datetime(2026, 10, 3).timestamp())
    days = aggregate(store, lo, hi, "day", 120, include_zero=True)
    assert [(r.period, r.device_id) for r in days] == [("2026-10-01", "d"), ("2026-10-01", "off")]
