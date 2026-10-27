"""Minimal PDF 1.4 writer (stdlib only).

Core fonts (Helvetica, Helvetica-Bold, Courier) with WinAnsi encoding, A4
portrait pages, zlib-compressed content streams, a correct xref table. The
primitives are deliberately few: text, lines, filled rectangles, and a flowing
document layer (`Document`) with headings, paragraphs, tables whose header is
repeated on every page, simple bar charts and a page-number footer.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

A4 = (595.28, 841.89)
MARGIN = 42.0

FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier"}

# Glyph widths (1/1000 em) of the standard Type 1 metrics for ASCII 32..126.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
         556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
         500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
         556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
         278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
              556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
              722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
              278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_SPECIAL = {"—": 1000, "–": 556, "…": 1000, "°": 400, "€": 556, "·": 278, "à": 556, "è": 556, "é": 556,
            "ì": 278, "ò": 556, "ù": 556, "À": 667, "È": 667, "É": 667, "Ì": 278, "Ò": 778, "Ù": 722, "×": 584}


def text_width(text: str, size: float, font: str = "F1") -> float:
    if font == "F3":
        return len(text) * 0.6 * size
    table = _HELV_BOLD if font == "F2" else _HELV
    total = 0
    for ch in text:
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else _SPECIAL.get(ch, 556)
    return total * size / 1000.0


def fit(text: str, width: float, size: float, font: str = "F1") -> str:
    """Truncate `text` with an ellipsis so that it fits in `width` points."""
    if text_width(text, size, font) <= width:
        return text
    while text and text_width(text + "…", size, font) > width:
        text = text[:-1]
    return text + "…" if text else ""


def _encode(text: str) -> bytes:
    """PDF string literal content in WinAnsi (cp1252); other characters become '?'."""
    raw = text.encode("cp1252", errors="replace")
    out = bytearray()
    for b in raw:
        if b in (0x28, 0x29, 0x5C):  # ( ) \
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out)


def _utf16(text: str) -> bytes:
    return (b"\xfe\xff" + text.encode("utf-16-be")).hex().upper().encode()


def _num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


Color = tuple[float, float, float]


class Canvas:
    """Low-level page operations, coordinates in points from the bottom-left corner."""

    def __init__(self, size=A4):
        self.size = size
        self.pages: list[list[bytes]] = []

    def add_page(self) -> None:
        self.pages.append([])

    def _op(self, s: bytes) -> None:
        if not self.pages:
            self.add_page()
        self.pages[-1].append(s)

    def text(self, x: float, y: float, text: str, size: float = 10, font: str = "F1",
             color: Color = (0, 0, 0), align: str = "left") -> None:
        if align != "left":
            w = text_width(text, size, font)
            x -= w if align == "right" else w / 2
        r, g, b = color
        self._op(b"BT /%s %s Tf %s %s %s rg %s %s Td (%s) Tj ET" % (
            font.encode(), _num(size).encode(), _num(r).encode(), _num(g).encode(), _num(b).encode(),
            _num(x).encode(), _num(y).encode(), _encode(text)))

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5,
             color: Color = (0, 0, 0)) -> None:
        r, g, b = color
        self._op(b"%s %s %s RG %s w %s %s m %s %s l S" % tuple(
            _num(v).encode() for v in (r, g, b, width, x1, y1, x2, y2)))

    def rect(self, x: float, y: float, w: float, h: float, fill: Color = (0, 0, 0)) -> None:
        r, g, b = fill
        self._op(b"%s %s %s rg %s %s %s %s re f" % tuple(_num(v).encode() for v in (r, g, b, x, y, w, h)))

    def to_bytes(self, title: str = "", author: str = "shelly-monitor") -> bytes:
        """Serialise: catalog, page tree, fonts, info, then one page + content stream per page."""
        pages = self.pages or [[]]
        n_fonts = len(FONTS)
        first_page = 3 + n_fonts + 1  # 1 catalog, 2 pages, fonts, info
        objects: list[bytes] = []
        kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(len(pages)))
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
        font_refs = []
        for i, (key, base) in enumerate(FONTS.items()):
            objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode())
            font_refs.append(f"/{key} {3 + i} 0 R")
        # document info strings are not WinAnsi: UTF-16BE with BOM as hex strings
        objects.append(b"<< /Title <" + _utf16(title) + b"> /Producer <" + _utf16(author) + b"> >>")
        resources = f"<< /Font << {' '.join(font_refs)} >> >>"
        w, h = self.size
        for i, ops in enumerate(pages):
            content = zlib.compress(b"\n".join(ops))
            objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(w)} {_num(h)}] "
                           f"/Resources {resources} /Contents {first_page + 2 * i + 1} 0 R >>".encode())
            objects.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content) + content
                           + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        info = 3 + n_fonts
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, info, xref)
        return bytes(out)


# --- flowing document layer ----------------------------------------------------------------

INK: Color = (0.11, 0.13, 0.16)
MUTED: Color = (0.40, 0.44, 0.50)
RULE: Color = (0.82, 0.85, 0.88)
ACCENT: Color = (0.05, 0.53, 0.78)
SHADE: Color = (0.95, 0.96, 0.97)


@dataclass
class Column:
    title: str
    width: float  # points
    align: str = "left"
    font: str = "F1"


class Document:
    """Top-down flow on A4 pages with automatic page breaks and a footer."""

    def __init__(self, title: str, footer: str = ""):
        self.c = Canvas()
        self.title = title
        self.footer = footer
        self.width = A4[0] - 2 * MARGIN
        self.y = 0.0
        self.new_page()

    @property
    def page_count(self) -> int:
        return len(self.c.pages)

    def new_page(self) -> None:
        self.c.add_page()
        self.y = A4[1] - MARGIN

    def ensure(self, height: float) -> bool:
        """Start a new page if `height` does not fit; True if a page was added."""
        if self.y - height < MARGIN + 24:
            self.new_page()
            return True
        return False

    def space(self, h: float) -> None:
        self.y -= h

    def heading(self, text: str, size: float = 13, keep: float = 60) -> None:
        """Section title; `keep` points of following content stay on the same page."""
        self.ensure(size + 14 + keep)
        self.space(size + 6)
        self.c.text(MARGIN, self.y, text, size, "F2", INK)
        self.space(8)

    def paragraph(self, text: str, size: float = 9.5, color: Color = INK, font: str = "F1",
                  leading: float = 1.35) -> None:
        """Word-wrapped text."""
        for raw in text.split("\n"):
            words, line = raw.split(" "), ""
            for word in words:
                cand = (line + " " + word).strip()
                if text_width(cand, size, font) > self.width and line:
                    self._line(line, size, color, font, leading)
                    line = word
                else:
                    line = cand
            self._line(line, size, color, font, leading)
            self.space(size * 0.5)  # paragraph spacing

    def _line(self, text: str, size: float, color: Color, font: str, leading: float) -> None:
        self.ensure(size * leading)
        self.space(size * leading)
        self.c.text(MARGIN, self.y, text, size, font, color)

    def key_values(self, items: list[tuple[str, str]], columns: int = 3, size: float = 9.5) -> None:
        """Grid of label/value pairs."""
        col_w = self.width / columns
        for i in range(0, len(items), columns):
            self.ensure(30)
            self.space(12)
            for j, (k, v) in enumerate(items[i:i + columns]):
                x = MARGIN + j * col_w
                self.c.text(x, self.y, k.upper(), 7, "F1", MUTED)
                self.c.text(x, self.y - 12, fit(v, col_w - 8, size, "F3"), size, "F3", INK)
            self.space(18)

    def tiles(self, items: list[tuple[str, str, str]]) -> None:
        """Summary boxes: (label, value, sub)."""
        h, gap = 54, 8
        self.ensure(h + 10)
        self.space(h + 4)
        w = (self.width - gap * (len(items) - 1)) / max(1, len(items))
        for i, (label, value, sub) in enumerate(items):
            x = MARGIN + i * (w + gap)
            self.c.rect(x, self.y, w, h, SHADE)
            self.c.text(x + 8, self.y + h - 13, label.upper(), 7, "F1", MUTED)
            self.c.text(x + 8, self.y + h - 32, fit(value, w - 16, 15, "F2"), 15, "F2", ACCENT if i == 0 else INK)
            self.c.text(x + 8, self.y + 8, fit(sub, w - 16, 7.5), 7.5, "F1", MUTED)
        self.space(6)

    def table(self, columns: list[Column], rows: list[list[str]], size: float = 8, empty: str = "") -> None:
        """Table that flows across pages, repeating the header row after every break."""
        scale = self.width / sum(c.width for c in columns)
        widths = [c.width * scale for c in columns]
        row_h = size + 6

        def header() -> None:
            self.space(row_h)
            self.c.rect(MARGIN, self.y - 4, self.width, row_h, SHADE)
            x = MARGIN
            for col, w in zip(columns, widths):
                self._cell(x, w, col.title, col.align, size, "F2", MUTED)
                x += w
            self.c.line(MARGIN, self.y - 4, MARGIN + self.width, self.y - 4, 0.6, RULE)

        self.ensure(row_h * 3)
        header()
        if not rows and empty:
            self.space(row_h)
            self.c.text(MARGIN + 4, self.y, empty, size, "F1", MUTED)
        for row in rows:
            if self.ensure(row_h):
                header()
            self.space(row_h)
            x = MARGIN
            for col, w, value in zip(columns, widths, row):
                self._cell(x, w, value, col.align, size, col.font, INK)
                x += w
            self.c.line(MARGIN, self.y - 4, MARGIN + self.width, self.y - 4, 0.3, RULE)
        self.space(6)

    def _cell(self, x: float, w: float, text: str, align: str, size: float, font: str, color: Color) -> None:
        text = fit(str(text), w - 8, size, font)
        if align == "right":
            self.c.text(x + w - 4, self.y, text, size, font, color, "right")
        else:
            self.c.text(x + 4, self.y, text, size, font, color)

    def bar_chart(self, labels: list[str], values: list[float], height: float = 150,
                  value_fmt=lambda v: f"{v:.1f}", empty: str = "") -> None:
        """Vertical bars drawn with rectangles; a few x labels and a max value line."""
        self.ensure(height + 40)
        self.space(height + 8)
        base, left = self.y, MARGIN + 30
        width = self.width - 30
        top = max(values) if values else 0
        self.c.line(left, base, left + width, base, 0.6, RULE)
        if not values or top <= 0:
            self.c.text(left + 4, base + 10, empty, 8, "F1", MUTED)
            self.space(18)
            return
        self.c.line(left, base + height, left + width, base + height, 0.3, RULE)
        self.c.text(left - 4, base + height - 3, value_fmt(top), 7, "F1", MUTED, "right")
        self.c.text(left - 4, base - 3, "0", 7, "F1", MUTED, "right")
        n = len(values)
        slot = width / n
        bar = max(0.8, slot * 0.72)
        for i, v in enumerate(values):
            hgt = height * v / top if v > 0 else 0
            if hgt > 0:
                self.c.rect(left + i * slot + (slot - bar) / 2, base, bar, max(0.6, hgt), ACCENT)
        picks = sorted({0, n // 2, n - 1}) if n > 2 else range(n)
        for i in picks:
            anchor = "left" if i == 0 else "right" if i == n - 1 else "center"
            x = left + i * slot + (0 if anchor == "left" else slot if anchor == "right" else slot / 2)
            self.c.text(x, base - 11, labels[i], 7, "F1", MUTED, anchor)
        self.space(18)

    def to_bytes(self, page_label: str = "Pagina {n} di {total}") -> bytes:
        total = self.page_count
        for i, ops in enumerate(self.c.pages):
            saved = self.c.pages
            self.c.pages = [ops]
            y = MARGIN - 14
            self.c.line(MARGIN, y + 10, A4[0] - MARGIN, y + 10, 0.3, RULE)
            if self.footer:
                self.c.text(MARGIN, y, fit(self.footer, self.width * 0.7, 7), 7, "F1", MUTED)
            self.c.text(A4[0] - MARGIN, y, page_label.format(n=i + 1, total=total), 7, "F1", MUTED, "right")
            self.c.pages = saved
        return self.c.to_bytes(self.title)
