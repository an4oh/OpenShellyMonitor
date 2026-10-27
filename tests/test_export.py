"""CSV and PDF exports (stdlib PDF writer)."""

import re
import shutil
import subprocess
import zlib

import pytest

from shelly_monitor import cli
from shelly_monitor.pdf import Canvas, Column, Document, fit, text_width


def check_pdf(data: bytes) -> int:
    """Structural validation: header, every xref offset points to its object, trailer. Returns pages."""
    assert data.startswith(b"%PDF-1.4\n")
    assert data.rstrip().endswith(b"%%EOF")
    startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
    assert data[startxref:startxref + 4] == b"xref"
    m = re.match(rb"xref\n0 (\d+)\n", data[startxref:])
    count = int(m.group(1))
    entries = data[startxref + m.end():].split(b"\n")[:count]
    assert entries[0].startswith(b"0000000000 65535 f")
    for n, entry in enumerate(entries[1:], 1):
        off, gen, kind = entry.split()
        assert kind == b"n" and gen == b"00000"
        assert data[int(off):].startswith(b"%d 0 obj\n" % n), f"object {n} not at offset {int(off)}"
    assert re.search(rb"/Size %d /Root 1 0 R" % count, data)
    pages = int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data).group(1))
    assert len(re.findall(rb"/Type /Page /Parent", data)) == pages
    for stream in re.findall(rb"stream\n(.*?)\nendstream", data, re.S):
        zlib.decompress(stream)  # every content stream is valid Flate data
    return pages


def page_streams(data: bytes) -> list[bytes]:
    return [zlib.decompress(s) for s in re.findall(rb"stream\n(.*?)\nendstream", data, re.S)]


def test_canvas_minimal_and_escaping():
    c = Canvas()
    c.add_page()
    c.text(50, 800, "Perché (prova) \\ città — 5 €", 12, "F2")
    c.line(50, 790, 300, 790)
    c.rect(50, 700, 100, 20, (0.2, 0.4, 0.6))
    data = c.to_bytes("Titolo — è")
    assert check_pdf(data) == 1
    (content,) = page_streams(data)
    assert b"(Perch\\351 \\(prova\\) \\\\ citt\\340 \\227 5 \\200) Tj" in content
    assert b"re f" in content and b" l S" in content
    assert b"/Encoding /WinAnsiEncoding" in data and b"/BaseFont /Courier" in data
    assert b"/Title <FEFF" in data


def test_text_metrics_and_fit():
    assert text_width("ii", 10) < text_width("MM", 10)
    assert text_width("abc", 10, "F3") == pytest.approx(18.0)
    s = fit("Termostato zona giorno molto lungo", 60, 8)
    assert s.endswith("…") and text_width(s, 8) <= 60


def test_document_table_repeats_header_and_numbers_pages():
    doc = Document("Test", "footer")
    doc.heading("Tabella")
    doc.table([Column("Colonna A", 2), Column("Num", 1, "right")], [[f"riga {i}", str(i)] for i in range(200)])
    data = doc.to_bytes()
    pages = check_pdf(data)
    assert pages >= 3
    streams = page_streams(data)
    assert all(b"(Colonna A) Tj" in s for s in streams)  # header on every page
    assert b"(Pagina 1 di %d) Tj" % pages in streams[0] and b"(Pagina %d di %d) Tj" % (pages, pages) in streams[-1]


# --- API and CLI ------------------------------------------------------------------------

from tests.test_devices import api, get as get_json, registry  # noqa: E402,F401  (fixtures)
import http.client  # noqa: E402


def fetch(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


Q = "from=2026-10-01&to=2026-10-31"


def test_report_csv_matches_cli_columns(api):
    r, body = fetch(api, "/api/report.csv?" + Q + "&group=total&by=channel&home=H2")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/csv")
    assert 'filename="shelly-report_H2_2026-10-01_2026-10-31.csv"' in r.getheader("Content-Disposition")
    lines = body.decode().splitlines()
    assert lines[0] == "period,home,device_id,name,channel,room,on_minutes,wh,wh_source,channels"
    assert any(l.startswith("total,H2,b2b2b2000003,Pump,0,Garage,5.0,") for l in lines)
    r, body = fetch(api, "/api/report.csv?" + Q + "&by=home&group=day")
    assert body.decode().splitlines()[1].startswith("2026-10-01,H2,,H2,")
    assert fetch(api, "/api/report.csv?" + Q + "&by=floor")[0].status == 400


def test_device_csv(api):
    r, body = fetch(api, "/api/device.csv?home=H2&id=b2b2b2000003&ch=0&from=2026-10-01&to=2026-10-01")
    assert r.status == 200
    head, *rows = body.decode().splitlines()
    assert head == "ts,iso_time,home,device_id,channel,kind,state,is_on,value,online,duration_s"
    assert [r.split(",")[6] for r in rows] == ["on", "off", "no_data", "on"]
    assert [r.split(",")[-1] for r in rows][:3] == ["120", "120", "660"]  # off lasts until the data gap


def test_report_pdf(api):
    r, body = fetch(api, "/api/report.pdf?" + Q)
    assert r.status == 200 and r.getheader("Content-Type") == "application/pdf"
    assert r.getheader("Content-Disposition").endswith('.pdf"')
    pages = check_pdf(body)
    assert pages >= 2
    text = b"".join(page_streams(body))
    for needle in (b"Report accensioni", b"(Riepilogo)", b"(Ore per casa)", b"(Ore per stanza)",
                   b"(Ore accese al giorno)", b"(Note)", b"(Pump)", b"(Garage)"):
        assert needle in text
    assert fetch(api, "/api/report.pdf?from=bad")[0].status == 400


def test_device_pdf(api):
    r, body = fetch(api, "/api/device.pdf?home=H2&id=b2b2b2000003&ch=0&" + Q)
    assert r.status == 200
    assert check_pdf(body) >= 1
    text = b"".join(page_streams(body))
    assert b"(Pump)" in text and re.search(rb"\(Transizioni \\\(ultime \d+\\\)\) Tj", text)
    assert b"(Ore accese al giorno)" in text
    r, body = fetch(api, "/api/device.pdf?home=H2&id=b2b2b2000003&ch=100&" + Q)  # sensor: no daily bars
    assert check_pdf(body) == 1 and b"(Ore accese al giorno)" not in b"".join(page_streams(body))
    assert fetch(api, "/api/device.pdf?home=H2&id=nope&ch=0")[0].status == 404


def test_cli_pdf_and_csv_out(registry, monkeypatch, tmp_path, capsys):
    db, _ = registry
    monkeypatch.setenv("SHELLY_DB", db)
    env = ["--env-file", str(tmp_path / "none")]
    assert cli.main(env + ["report", "--format", "pdf"]) == 2
    assert "--out FILE is required" in capsys.readouterr().err
    out = tmp_path / "r.pdf"
    assert cli.main(env + ["report", "--format", "pdf", "--out", str(out), "--home", "H2"]) == 0
    assert check_pdf(out.read_bytes()) >= 2
    assert cli.main(env + ["export", "--format", "pdf"]) == 2
    sp = tmp_path / "s.pdf"
    assert cli.main(env + ["export", "--format", "pdf", "--out", str(sp)]) == 0
    assert b"Campioni" in b"".join(page_streams(sp.read_bytes()))
    csv_out = tmp_path / "r.csv"
    assert cli.main(env + ["report", "--group", "total", "--format", "csv", "--out", str(csv_out)]) == 0
    assert csv_out.read_text().startswith("period,home,")
    assert cli.main(env + ["export", "--out", str(tmp_path / "s.csv")]) == 0
    assert (tmp_path / "s.csv").read_text().startswith("ts,iso_time,")


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext not installed")
def test_pdftotext_reads_title(api, tmp_path):
    _, body = fetch(api, "/api/report.pdf?" + Q)
    path = tmp_path / "r.pdf"
    path.write_bytes(body)
    text = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, check=True).stdout.decode()
    assert "Shelly Monitor — Report accensioni" in text and "Ore per stanza" in text and "città" not in text
