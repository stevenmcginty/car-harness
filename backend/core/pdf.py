"""A minimal PDF writer -- stdlib only, no dependencies.

Why this exists: the harness needs to hand a customer a printed battery report,
and adding reportlab/weasyprint for a one-page document of text, rules and
filled rectangles is a poor trade -- especially with the project moving to
Android, where those wheels do not follow. A PDF of this shape is a small,
well-specified format, so we write it directly (same reasoning as the pure-
stdlib PNG encoder used for the launcher icons).

Scope, deliberately narrow:
  * the 14 standard "base" fonts (Helvetica / Helvetica-Bold / Courier), which
    every PDF reader is required to have, so nothing is embedded;
  * text with left / right / centre alignment, using the real Adobe AFM metrics
    so right-aligned numbers actually line up;
  * filled rectangles and straight lines, for rules, table banding and bars;
  * multiple pages.

Everything is written uncompressed. A report is a few kilobytes; compression
would only trade clarity for nothing.

Coordinates: this module puts the origin at the TOP-LEFT and measures y
downwards, because that is how a page is laid out when you are reading it. The
flip to PDF's bottom-left origin happens once, in ``_ypdf``.
"""

from typing import List, Optional, Tuple

# A4 in PostScript points (1/72").
A4_WIDTH = 595.28
A4_HEIGHT = 841.89

HELVETICA = "Helvetica"
HELVETICA_BOLD = "Helvetica-Bold"
COURIER = "Courier"

_FONT_RESOURCE = {HELVETICA: "F1", HELVETICA_BOLD: "F2", COURIER: "F3"}

# Adobe AFM character widths, per 1000 units of em, for ASCII 32..126.
# Without these, right-aligned columns drift and centred headings sit off-centre.
_HELV = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELV_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)


def text_width(text: str, font: str, size: float) -> float:
    """Width of ``text`` in points. Courier is monospaced at 600/1000 em."""
    if font == COURIER:
        return len(text) * size * 0.6
    table = _HELV_BOLD if font == HELVETICA_BOLD else _HELV
    total = 0
    for ch in text:
        code = ord(ch)
        total += table[code - 32] if 32 <= code <= 126 else table[0]
    return total * size / 1000.0


def _escape(text: str) -> str:
    """PDF string escaping, and drop anything outside WinAnsi's safe ASCII.

    A stray non-ASCII byte in a customer's name or a module label would corrupt
    the stream, so unsupported characters are replaced rather than emitted.
    """
    out = []
    for ch in text:
        if ch in "\\()":
            out.append("\\" + ch)
        elif 32 <= ord(ch) <= 126:
            out.append(ch)
        elif ch in "’‘":
            out.append("'")
        elif ch in "“”":
            out.append('"')
        elif ch in "–—":
            out.append("-")
        elif ch == "°":
            out.append(chr(176))  # degree sign exists in WinAnsi
        else:
            out.append("?")
    return "".join(out)


class Page:
    """One page's content stream, built up as PDF operators."""

    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height
        self._ops: List[str] = []

    def _ypdf(self, y: float) -> float:
        """Top-left origin (ours) -> bottom-left origin (PDF's)."""
        return self.height - y

    def text(
        self,
        x: float,
        y: float,
        content: str,
        font: str = HELVETICA,
        size: float = 10.0,
        color: Tuple[float, float, float] = (0, 0, 0),
        align: str = "left",
    ) -> None:
        """Draw one line of text. ``y`` is the text baseline, from the top."""
        if content is None:
            return
        content = str(content)
        if align == "right":
            x -= text_width(content, font, size)
        elif align == "center":
            x -= text_width(content, font, size) / 2.0
        r, g, b = color
        self._ops.append(
            f"BT /{_FONT_RESOURCE[font]} {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
            f"1 0 0 1 {x:.2f} {self._ypdf(y):.2f} Tm ({_escape(content)}) Tj ET"
        )

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        color: Tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        """Filled rectangle; ``y`` is its TOP edge."""
        r, g, b = color
        self._ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg "
            f"{x:.2f} {self._ypdf(y + height):.2f} {width:.2f} {height:.2f} re f"
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        width: float = 0.5,
        color: Tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        r, g, b = color
        self._ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w "
            f"{x1:.2f} {self._ypdf(y1):.2f} m {x2:.2f} {self._ypdf(y2):.2f} l S"
        )

    def stream(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1", "replace")


class Pdf:
    """A whole document. ``add_page()`` then draw, then ``to_bytes()``."""

    def __init__(self, title: str = "", author: str = ""):
        self.pages: List[Page] = []
        self.title = title
        self.author = author

    def add_page(self, width: float = A4_WIDTH, height: float = A4_HEIGHT) -> Page:
        page = Page(width, height)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        if not self.pages:
            self.add_page()

        objects: List[bytes] = []

        def add(body: bytes) -> int:
            """Append an object; returns its 1-based object number."""
            objects.append(body)
            return len(objects)

        # 1 catalog, 2 pages tree, 3..5 fonts -- fixed numbers so the page
        # objects can reference them before they are written.
        catalog_num = add(b"<< /Type /Catalog /Pages 2 0 R >>")
        pages_num = add(b"")  # filled in once the kids are known
        font_nums = {
            HELVETICA: add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>"
            ),
            HELVETICA_BOLD: add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                b"/Encoding /WinAnsiEncoding >>"
            ),
            COURIER: add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
                b"/Encoding /WinAnsiEncoding >>"
            ),
        }
        resources = (
            "<< /Font << "
            + " ".join(
                f"/{_FONT_RESOURCE[name]} {num} 0 R" for name, num in font_nums.items()
            )
            + " >> >>"
        ).encode("latin-1")

        kids: List[int] = []
        for page in self.pages:
            data = page.stream()
            content_num = add(
                b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n"
                + data + b"\nendstream"
            )
            page_num = add(
                f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox "
                f"[0 0 {page.width:.2f} {page.height:.2f}] ".encode("latin-1")
                + b"/Resources " + resources
                + f" /Contents {content_num} 0 R >>".encode("latin-1")
            )
            kids.append(page_num)

        objects[pages_num - 1] = (
            "<< /Type /Pages /Count "
            + str(len(kids))
            + " /Kids ["
            + " ".join(f"{n} 0 R" for n in kids)
            + "] >>"
        ).encode("latin-1")

        info_num = add(
            b"<< /Title (" + _escape(self.title).encode("latin-1", "replace")
            + b") /Author (" + _escape(self.author).encode("latin-1", "replace")
            + b") /Producer (car-harness) >>"
        )

        # -- serialise, recording byte offsets for the xref table ------------ #
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(objects) + 1)
        for i, body in enumerate(objects, start=1):
            offsets[i] = len(out)
            out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"

        xref_at = len(out)
        out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
        out += b"0000000000 65535 f \n"
        for i in range(1, len(objects) + 1):
            out += f"{offsets[i]:010d} 00000 n \n".encode()
        out += (
            b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + str(catalog_num).encode() + b" 0 R"
            + b" /Info " + str(info_num).encode() + b" 0 R >>\n"
            b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
        )
        return bytes(out)


def wrap(text: str, font: str, size: float, max_width: float) -> List[str]:
    """Greedy word wrap to ``max_width`` points."""
    words = str(text).split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        trial = current + " " + word
        if text_width(trial, font, size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
