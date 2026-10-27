"""PDF reports (Italian labels by default) built on the dashboard queries."""

from __future__ import annotations

from datetime import date, datetime

from . import __version__
from .pdf import Column, Document

LABELS_IT = {
    "title": "Shelly Monitor — Report accensioni",
    "device_title": "Shelly Monitor — Scheda dispositivo",
    "period": "Periodo",
    "generated": "Generato il",
    "homes": "Case incluse",
    "room_filter": "Stanza",
    "all": "tutte",
    "summary": "Riepilogo",
    "on_hours": "Ore accese",
    "moving_hours": "Ore in movimento",
    "energy": "Energia",
    "channels": "Canali",
    "samples": "Campioni",
    "starts": "Accensioni",
    "moves": "Movimenti",
    "last_value": "Ultimo valore",
    "min": "Minimo",
    "max": "Massimo",
    "by_home": "Ore per casa",
    "by_room": "Ore per stanza",
    "by_group": "Ore per gruppo",
    "by_group_note": "Gruppi personalizzati: un canale può appartenere a più gruppi e viene contato in ciascuno.",
    "group": "Gruppo",
    "per_day": "Ore accese al giorno",
    "per_day_moving": "Ore in movimento al giorno",
    "channel_table": "Canali",
    "home": "Casa",
    "room": "Stanza",
    "device": "Dispositivo",
    "channel": "Can.",
    "channel_long": "Canale",
    "model": "Modello",
    "on_min": "Min ON",
    "hours": "Ore",
    "wh": "Wh",
    "source": "Fonte",
    "counter": "contatore",
    "wh_sources": "contatore su {c} canali, stima su {e}",
    "estimate": "stima",
    "mixed": "misto",
    "no_room": "(nessuna stanza)",
    "no_data": "(nessun dato nel periodo)",
    "no_power": "nessun dato di potenza",
    "interval": "intervallo {s} s",
    "days": "{n} giorni",
    "notes": "Note",
    "notes_text": (
        "Metodo di calcolo. Lo stato di ogni canale viene letto da Shelly Cloud ogni {interval} secondi. "
        "Il tempo fra due campioni consecutivi dello stesso canale viene accreditato come acceso quando il "
        "primo campione è acceso e il dispositivo è online; ogni intervallo è limitato a {cap} secondi "
        "(2 × intervallo), così i buchi di dati non gonfiano i totali. L'intervallo è attribuito al giorno "
        "(ora locale) in cui inizia.\n"
        "Energia. Per i canali con contatore cumulativo (aenergy.total dei Gen2, meters.total dei Gen1) si usa "
        "la differenza del contatore fra campioni consecutivi; un contatore che torna indietro (riavvio) conta 0. "
        "Per gli altri canali l'energia è stimata come potenza istantanea × durata, solo mentre è acceso. "
        "La colonna Fonte indica il metodo: i valori sono stime, non letture fiscali.\n"
        "Le ore accese contano interruttori e luci; tapparelle e sensori sono esclusi (il tempo di movimento "
        "delle tapparelle è nella scheda del singolo dispositivo)."
    ),
    "info": "Informazioni",
    "state": "Stato",
    "online": "Online",
    "last_sample": "Ultimo campione",
    "gen": "Gen",
    "id": "ID",
    "kind": "Tipo",
    "groups": "Gruppi",
    "yes": "sì",
    "no": "no",
    "transitions": "Transizioni (ultime {n})",
    "when": "Data e ora",
    "duration": "Durata",
    "ongoing": "in corso",
    "offline": "offline",
    "gap": "nessun dato",
    "moving": "in movimento",
    "stopped": "ferma",
    "page": "Pagina {n} di {total}",
    "footer": "shelly-monitor {version} · {period}",
    "samples_title": "Shelly Monitor — Campioni",
    "samples_note": "Mostrati i primi {n} campioni su {total}: per i dati completi usare l'esportazione CSV.",
    "value": "Valore",
    "power": "W",
    "kinds": {"switch": "Interruttore", "light": "Luce", "rgbw": "Luce RGBW", "cover": "Tapparella",
              "sensor": "Sensore", "input": "Ingressi", "unknown": "Sconosciuto"},
}


def it_num(value: float | None, decimals: int = 1) -> str:
    """Italian number format: 1.234,5"""
    if value is None:
        return "–"
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_wh(wh: float | None) -> str:
    if wh is None:
        return "–"
    return f"{it_num(wh / 1000, 2)} kWh" if abs(wh) >= 1000 else f"{it_num(wh, 1)} Wh"


def fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "–"


def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return d.strftime("%d/%m")


def fmt_period(q: dict) -> str:
    def one(v):
        return date.fromisoformat(v).strftime("%d/%m/%Y") if v else "…"
    return f"{one(q.get('from'))} – {one(q.get('to'))}"


def fmt_duration(sec: int | None, L: dict) -> str:
    if sec is None:
        return L["ongoing"]
    if sec < 60:
        return f"{sec} s"
    m = round(sec / 60)
    if m < 60:
        return f"{m} min"
    h = m // 60
    return f"{h} h {m % 60:02d} min" if h < 48 else f"{it_num(h / 24, 1)} g"


def report_pdf(dash, q: dict, labels: dict = LABELS_IT) -> bytes:
    """Report of ON time for the period and filters in `q` (from, to, home, room)."""
    L = labels
    base = {k: q[k] for k in ("from", "to", "home", "room", "grp") if q.get(k)}
    summary = dash.summary(base)
    channels = dash.report({**base, "group": "total", "by": "channel"})
    by_home = dash.report({**base, "group": "total", "by": "home"})
    by_room = dash.report({**base, "group": "total", "by": "room"})
    by_group = dash.report({**{k: v for k, v in base.items() if k in ("from", "to")}, "group": "total", "by": "group"})
    daily = dash.daily(base)
    homes = [h["home"] for h in dash.homes({})]
    if base.get("home"):
        homes = [base["home"]]
    period = fmt_period(base)

    doc = Document(L["title"], L["footer"].format(version=__version__, period=period))
    doc.space(4)
    doc.c.text(42, doc.y - 14, L["title"], 18, "F2")
    doc.space(30)
    doc.key_values([
        (L["period"], period),
        (L["generated"], datetime.now().strftime("%d/%m/%Y %H:%M")),
        (L["homes"], ", ".join(homes) or "–"),
    ] + ([(L["room_filter"], base["room"])] if base.get("room") else []))

    doc.heading(L["summary"])
    c, e = summary["wh_counter_channels"], summary["wh_estimate_channels"]
    wh_sub = L["no_power"] if summary["wh"] is None else L["wh_sources"].format(c=c, e=e)
    doc.tiles([
        (L["on_hours"], it_num(summary["on_hours"], 1), L["days"].format(n=summary["days"] or "–")),
        (L["energy"], fmt_wh(summary["wh"]), wh_sub),
        (L["channels"], it_num(summary["channels"], 0), f"{summary['channels_on']} ON"),
        (L["samples"], it_num(summary["samples"], 0), L["interval"].format(s=summary["interval_s"])),
    ])

    def src(r):
        return L.get(r["wh_source"], "") if r.get("wh_source") else ""

    doc.heading(L["by_home"])
    doc.table([Column(L["home"], 4), Column(L["channels"], 1.3, "right"), Column(L["hours"], 1.5, "right"),
               Column(L["wh"], 1.6, "right"), Column(L["source"], 1.5)],
              [[r["home"], str(r["channels"]), it_num(r["on_h"], 2), it_num(r["wh"], 1), src(r)] for r in by_home],
              empty=L["no_data"])

    doc.heading(L["by_room"])
    rooms = sorted(by_room, key=lambda r: (r["home"], r["room"] is None, r["room"] or ""))
    doc.table([Column(L["home"], 2.5), Column(L["room"], 3), Column(L["channels"], 1.3, "right"),
               Column(L["hours"], 1.5, "right"), Column(L["wh"], 1.6, "right")],
              [[r["home"], r["room"] or L["no_room"], str(r["channels"]), it_num(r["on_h"], 2), it_num(r["wh"], 1)]
               for r in rooms], empty=L["no_data"])

    if by_group:
        doc.heading(L["by_group"])
        doc.paragraph(L["by_group_note"], 8, (0.4, 0.44, 0.5))
        doc.table([Column(L["group"], 4), Column(L["channels"], 1.3, "right"), Column(L["hours"], 1.5, "right"),
                   Column(L["wh"], 1.6, "right"), Column(L["source"], 1.5)],
                  [[r["name"], str(r["channels"]), it_num(r["on_h"], 2), it_num(r["wh"], 1), src(r)]
                   for r in sorted(by_group, key=lambda r: r["name"])])

    doc.heading(L["per_day"])
    doc.bar_chart([fmt_day(d["date"]) for d in daily], [d["on_h"] for d in daily],
                  value_fmt=lambda v: f"{it_num(v, 1)} h", empty=L["no_data"])

    doc.heading(L["channel_table"])
    rows = sorted(channels, key=lambda r: (-r["on_min"], r["home"], r["name"] or ""))
    doc.table([Column(L["home"], 1.6), Column(L["room"], 1.7), Column(L["device"], 2.9), Column(L["channel"], 0.75, "right"),
               Column(L["model"], 1.9, "left", "F3"), Column(L["on_min"], 1.2, "right"), Column(L["hours"], 1, "right"),
               Column(L["wh"], 1.1, "right"), Column(L["source"], 1.1)],
              [[r["home"], r["room"] or "–", r["name"] or f"{r['device_id']}:{r['channel']}", str(r["channel"]),
                r.get("model") or "–", it_num(r["on_min"], 0), it_num(r["on_h"], 2), it_num(r["wh"], 1), src(r)]
               for r in rows], size=7.5, empty=L["no_data"])

    doc.new_page()
    doc.heading(L["notes"])
    interval = summary["interval_s"]
    doc.paragraph(L["notes_text"].format(interval=interval, cap=2 * interval))
    return doc.to_bytes(L["page"])


def device_pdf(dash, q: dict, labels: dict = LABELS_IT, max_transitions: int = 300) -> bytes:
    """One channel: info, tiles, hours per day, last transitions."""
    L = labels
    d = dash.device(q)
    kind = d["kind"]
    sensor, cover = kind == "sensor", kind == "cover"
    period = fmt_period(q)
    name = d["name"] or f"{d['device_id']}:{d['channel']}"
    doc = Document(f"{L['device_title']}: {name}", L["footer"].format(version=__version__, period=period))
    doc.space(4)
    doc.c.text(42, doc.y - 14, name, 18, "F2")
    doc.c.text(42, doc.y - 30, f"{L['device_title']} · {L['period']} {period}", 9, "F1", (0.4, 0.44, 0.5))
    doc.space(40)

    tr = d["transitions"]
    state = _state_text(d, L)
    doc.heading(L["info"])
    doc.key_values([
        (L["home"], d["home"]), (L["room"], d["room"] or L["no_room"]), (L["kind"], L["kinds"].get(kind, kind)),
        (L["state"], state), (L["online"], "–" if d["online"] is None else (L["yes"] if d["online"] else L["no"])),
        (L["last_sample"], fmt_ts(d["last_seen"])),
        (L["model"], d["type"] or d["model"] or "–"), (L["gen"], d["gen"] or "–"), (L["id"], d["device_id"]),
        (L["channel_long"], str(d["channel"])), (L["groups"], ", ".join(d["groups"]) or "–"),
        (L["generated"], datetime.now().strftime("%d/%m/%Y %H:%M")),
    ])

    on_h = sum(x["on_h"] for x in d["daily"])
    whs = [x["wh"] for x in d["daily"] if x["wh"] is not None]
    starts = sum(1 for t in tr if not t.get("gap") and t.get("online") and t.get("is_on"))
    vals = [t["value"] for t in tr if not t.get("gap") and t.get("value") is not None]
    doc.heading(L["summary"])
    if sensor:
        doc.tiles([(L["last_value"], _value_text(d, d["value"]), fmt_ts(d["last_seen"])),
                   (L["min"], _value_text(d, min(vals)) if vals else "–", period),
                   (L["max"], _value_text(d, max(vals)) if vals else "–", period),
                   (L["samples"], it_num(d["samples"], 0), L["interval"].format(s=d["interval_s"]))])
    else:
        doc.tiles([(L["moving_hours"] if cover else L["on_hours"], it_num(on_h, 1), L["days"].format(n=len(d["daily"]))),
                   (L["energy"], fmt_wh(sum(whs)) if whs else "–", "" if whs else L["no_power"]),
                   (L["moves"] if cover else L["starts"], it_num(starts, 0), period),
                   (L["samples"], it_num(d["samples"], 0), L["interval"].format(s=d["interval_s"]))])
        doc.heading(L["per_day_moving"] if cover else L["per_day"])
        doc.bar_chart([fmt_day(x["day"]) for x in d["daily"]], [x["on_h"] for x in d["daily"]],
                      value_fmt=lambda v: f"{it_num(v, 1)} h", empty=L["no_data"])

    last = tr[-max_transitions:][::-1]
    doc.heading(L["transitions"].format(n=len(last)))
    rows = []
    for t in last:
        i = tr.index(t)
        nxt = tr[i + 1]["ts"] if i + 1 < len(tr) else None
        rows.append([fmt_ts(t["ts"]), _transition_text(d, t, L), fmt_duration(nxt - t["ts"] if nxt else None, L)])
    doc.table([Column(L["when"], 2.2, "left", "F3"), Column(L["state"], 3), Column(L["duration"], 1.6, "right")],
              rows, empty=L["no_data"])
    return doc.to_bytes(L["page"])


def _value_text(d: dict, value) -> str:
    if value is None:
        return "–"
    t = str(d.get("type") or d.get("model") or "")
    if t.startswith(("S4SN", "SHWT")):
        return "allarme" if value else "asciutto"
    if d["channel"] >= 1000:
        return f"{it_num(value, 0)} %"
    return f"{it_num(value, 1)} °C"


def _state_text(d: dict, L: dict) -> str:
    if d["online"] is False:
        return L["offline"]
    if d["kind"] == "sensor":
        return _value_text(d, d["value"])
    if d["kind"] == "cover":
        pos = f"{it_num(d['value'], 0)} %" if d["value"] is not None else ""
        return f"{L['moving']} {pos}".strip() if d["state"] == "moving" else (pos or L["stopped"])
    return {"on": "ON", "off": "OFF"}.get(d["state"] or "", "–")


def _transition_text(d: dict, t: dict, L: dict) -> str:
    if t.get("gap"):
        return L["gap"]
    if not t.get("online"):
        return L["offline"]
    if d["kind"] == "sensor":
        return _value_text(d, t.get("value"))
    if d["kind"] == "cover":
        pos = f" · {it_num(t['value'], 0)} %" if t.get("value") is not None else ""
        return (L["moving"] if t.get("is_on") else L["stopped"]) + pos
    extra = f" · {it_num(t['value'], 0)} %" if t.get("is_on") and t.get("value") is not None and d["kind"] != "switch" else ""
    return ("ON" if t.get("is_on") else "OFF") + extra


def samples_pdf(dash, q: dict, labels: dict = LABELS_IT, max_rows: int = 5000) -> bytes:
    """Raw samples as a table (capped: the CSV export is the complete one)."""

    L = labels
    lo, hi = dash._range(q)
    period = fmt_period(q)
    doc = Document(L["samples_title"], L["footer"].format(version=__version__, period=period))
    doc.space(4)
    doc.c.text(42, doc.y - 14, L["samples_title"], 18, "F2")
    doc.space(30)
    rows, total = [], 0
    store = dash._store()
    if store is not None:
        with store:
            info = store.channel_info()
            for s in store.samples(lo, hi, home=q.get("home") or None, room=q.get("room") or None):
                total += 1
                if len(rows) >= max_rows:
                    continue
                d = info.get((s["home"], s["device_id"], s["channel"]))
                kind = s["kind"] or "switch"
                state = "–" if s["is_on"] is None else ("ON" if s["is_on"] else "OFF")
                rows.append([fmt_ts(s["ts"]), s["home"], (d["name"] if d is not None and d["name"] else s["device_id"]),
                             str(s["channel"]), L["kinds"].get(kind, kind), state, it_num(s["value"], 1),
                             it_num(s["power_w"], 1), L["yes"] if s["online"] else L["no"]])
    doc.key_values([(L["period"], period), (L["generated"], datetime.now().strftime("%d/%m/%Y %H:%M")),
                    (L["samples"], it_num(total, 0))])
    if total > max_rows:
        doc.paragraph(L["samples_note"].format(n=it_num(max_rows, 0), total=it_num(total, 0)), 8.5)
    doc.table([Column(L["when"], 2.1, "left", "F3"), Column(L["home"], 1.5), Column(L["device"], 2.8),
               Column(L["channel"], 0.6, "right"), Column(L["kind"], 1.4), Column(L["state"], 0.8),
               Column(L["value"], 0.9, "right"), Column(L["power"], 0.9, "right"), Column(L["online"], 0.8)],
              rows, size=7, empty=L["no_data"])
    return doc.to_bytes(L["page"])
