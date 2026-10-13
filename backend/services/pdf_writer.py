"""
ClassVision AI - Minimal PDF writer (Step 11)
----------------------------------------------
Enough PDF to print a register, and no more.

Why this exists instead of reportlab

    A school office wants a signed piece of paper, so Step 11 owes them a PDF.
    It does not follow that this project owes itself a PDF library. ReportLab
    is around 15 MB installed and brings its own C extension; this module is
    one file, has no dependency beyond the standard library, and produces the
    only document this project actually needs - a paginated table with a
    heading. A classroom appliance that has to be installed offline on a
    school's spare laptop is the deployment this project keeps designing for,
    and every megabyte of wheel is a reason that install fails.

    The trade is deliberate and narrow: no images, no vector graphics, no
    Unicode beyond Latin-1, one font family. If this project ever needs a
    chart in a PDF, the honest move is to add a real library then, not to
    grow this file until it is a bad one.

What a reader must know about the encoding

    Text is drawn with Helvetica, one of the 14 fonts every PDF reader is
    required to have built in, so nothing is embedded and the file stays small.
    The price is WinAnsiEncoding: Helvetica addresses one byte per glyph and
    can express Latin-1 and no more.

    A name in Devanagari, Tamil or Han CANNOT be drawn by this module. It is
    replaced character by character with "?" and the substitution is COUNTED
    and reported back to the caller, which is why write_table returns a report
    rather than only bytes. The route puts that count in a response header and
    the dashboard says so out loud, because a register that silently prints a
    student's name as "???" is worse than one that admits it cannot.

    The CSV export has no such limit - it is UTF-8 and lossless. Where names
    are not Latin-1, the CSV is the correct export and the dashboard says that
    too. Fixing this properly means embedding a subset of a Unicode TrueType
    font, which is a real piece of work and not a line of code; pretending
    otherwise in a docstring would be the actual failure here.

Coordinates

    PDF's origin is the BOTTOM-left corner and y grows upward, which is the
    opposite of every screen coordinate in this project. Rows are therefore
    laid out by subtracting from a running cursor that starts at the top
    margin. Sizes are in points: 1 pt = 1/72 inch, and A4 is 595.276 x 841.89.
"""

import zlib

# A4 in points. Portrait is the shape a register is filed in; landscape is
# offered because a twelve-column table does not fit in 595 points and a
# table squeezed to illegibility is not a report.
A4_PORTRAIT = (595.276, 841.89)
A4_LANDSCAPE = (841.89, 595.276)

MARGIN = 36.0                 # half an inch on every side
TITLE_SIZE = 16.0
SUBTITLE_SIZE = 9.0
HEADER_SIZE = 8.5
BODY_SIZE = 8.0
LINE_HEIGHT = 12.0
HEADER_GAP = 6.0

# Helvetica and Helvetica-Bold advance widths, in 1/1000 em, for the printable
# ASCII range. Measuring text is what lets a cell be truncated at the column
# boundary instead of running into its neighbour.
#
# Characters outside this table - the rest of Latin-1 - fall back to 556, the
# width of a digit. That is an approximation and it is only ever used to decide
# where to cut a string, so being a few points out shifts an ellipsis, not a
# column.
_REGULAR = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
)

FALLBACK_WIDTH = 556

# WinAnsiEncoding is NOT Latin-1, and this is where they differ.
#
# Bytes 0x80-0x9F are control codes in Latin-1 and printable punctuation in
# WinAnsi. Mapping these seven-odd characters instead of replacing them is not
# a nicety: U+2019 is the apostrophe every word processor produces, so a name
# pasted in as "O’Brien" would otherwise print as "O?Brien". The ellipsis
# matters for the same reason - truncate() needs a real one-glyph ellipsis, and
# U+2026 is only reachable through byte 0x85.
#
# A string that has been through encode_text holds one character per PDF BYTE,
# so ord() of every character in it is the byte to write. That invariant is
# what lets the width tables, truncate() and _escape() all work on the same
# string without a second encoding step.
WINANSI = {
    0x20AC: 0x80,   # euro
    0x201A: 0x82,   # single low quote
    0x0192: 0x83,   # florin
    0x201E: 0x84,   # double low quote
    0x2026: 0x85,   # ellipsis
    0x2020: 0x86,   # dagger
    0x2021: 0x87,   # double dagger
    0x02C6: 0x88,   # circumflex
    0x2030: 0x89,   # per mille
    0x0160: 0x8A,   # S caron
    0x2039: 0x8B,   # single left angle quote
    0x0152: 0x8C,   # OE
    0x017D: 0x8E,   # Z caron
    0x2018: 0x91,   # left single quote
    0x2019: 0x92,   # right single quote - the common apostrophe
    0x201C: 0x93,   # left double quote
    0x201D: 0x94,   # right double quote
    0x2022: 0x95,   # bullet
    0x2013: 0x96,   # en dash
    0x2014: 0x97,   # em dash
    0x02DC: 0x98,   # small tilde
    0x2122: 0x99,   # trademark
    0x0161: 0x9A,   # s caron
    0x203A: 0x9B,   # single right angle quote
    0x0153: 0x9C,   # oe
    0x017E: 0x9E,   # z caron
    0x0178: 0x9F,   # Y diaeresis
}

# Advance widths for those same positions, which the ASCII table above does not
# reach. Without these an ellipsis would be measured as a digit and a cell
# holding one could overrun its column by a few points.
_HIGH_WIDTHS = {
    0x80: 556, 0x82: 222, 0x83: 556, 0x84: 333, 0x85: 1000, 0x86: 556,
    0x87: 556, 0x88: 333, 0x89: 1000, 0x8A: 667, 0x8B: 333, 0x8C: 1000,
    0x8E: 611, 0x91: 222, 0x92: 222, 0x93: 333, 0x94: 333, 0x95: 350,
    0x96: 556, 0x97: 1000, 0x98: 333, 0x99: 1000, 0x9A: 500, 0x9B: 333,
    0x9C: 944, 0x9E: 500, 0x9F: 667,
}

# The ellipsis as the single WinAnsi byte that draws it. Used by truncate.
ELLIPSIS = chr(0x85)


def _widths(table):
    """Map a space-separated width list onto code points 32 upward."""
    widths = {32 + index: int(value)
              for index, value in enumerate(table.split())}
    widths.update(_HIGH_WIDTHS)
    return widths


REGULAR_WIDTHS = _widths(_REGULAR)
BOLD_WIDTHS = _widths(_BOLD)


class Encoded:
    """
    One string, flattened to what Helvetica can actually draw.

    `text` is the WinAnsi-safe string and `replaced` is how many characters
    did not survive. Keeping the count on the value rather than raising means
    a register still prints; the caller decides what to say about it.
    """

    __slots__ = ("text", "replaced")

    def __init__(self, text, replaced):
        self.text = text
        self.replaced = replaced


def encode_text(value):
    """
    Coerce any value to a WinAnsi string, counting what could not be encoded.

    The result holds one character per PDF BYTE: ord() of every character in
    it is in 0-255 and is exactly the byte the content stream will carry. That
    is the invariant everything downstream relies on.

    Three cases, in order:

        ASCII and the printable Latin-1 upper half pass straight through, because
        WinAnsi agrees with Latin-1 there - byte 0xE9 is "e-acute" to both, so
        "José Muñoz" needs no translation.

        The punctuation in WINANSI is TRANSLATED to the byte that draws it.
        U+2019 is the apostrophe every word processor produces; replacing it
        would print "O?Brien" for a name that Helvetica can draw perfectly.

        Everything else - Devanagari, Han, emoji - becomes "?" and is COUNTED.
        See the module header: the count is reported to the caller and ends up
        in a response header, because the alternative is a printed register
        that silently renames a student to "???".
    """
    if value is None:
        return Encoded("", 0)

    if isinstance(value, bool):
        value = "yes" if value else "no"

    text = str(value)
    out = []
    replaced = 0

    for character in text:
        code = ord(character)
        # Tabs and newlines would break the text operator they are written
        # into, so they become spaces rather than corrupting the page.
        if character in "\t\n\r":
            out.append(" ")
        elif 32 <= code <= 126 or 160 <= code <= 255:
            out.append(character)
        elif code in WINANSI:
            out.append(chr(WINANSI[code]))
        else:
            out.append("?")
            replaced += 1

    return Encoded("".join(out), replaced)


def text_width(text, size, bold=False):
    """Advance width of an already-encoded string, in points."""
    widths = BOLD_WIDTHS if bold else REGULAR_WIDTHS
    total = 0
    for character in text:
        total += widths.get(ord(character), FALLBACK_WIDTH)
    return total * size / 1000.0


def truncate(text, limit, size, bold=False):
    """
    The longest prefix of `text` that fits in `limit` points, ellipsised.

    A cell that does not fit is cut with a real one-glyph ellipsis rather than
    three dots, because "..." is three glyphs of width and the whole point here
    is to stop overrunning the column. ELLIPSIS is byte 0x85 - the WinAnsi
    position - not U+2026, for the reason encode_text explains: this string is
    already in byte space.
    """
    if limit <= 0:
        return ""
    if text_width(text, size, bold) <= limit:
        return text

    ellipsis = ELLIPSIS
    room = limit - text_width(ellipsis, size, bold)
    if room <= 0:
        return ""

    # Built forward one character at a time: the widths are additive, so
    # there is no need to re-measure the whole prefix each step.
    kept = []
    used = 0.0
    widths = BOLD_WIDTHS if bold else REGULAR_WIDTHS
    for character in text:
        advance = widths.get(ord(character), FALLBACK_WIDTH) * size / 1000.0
        if used + advance > room:
            break
        kept.append(character)
        used += advance

    return "".join(kept) + ellipsis


def _escape(text):
    r"""
    Escape a string for a PDF literal.

    Backslash first - escaping it after the parentheses would double the
    backslashes this function itself inserted.
    """
    return (text.replace("\\", r"\\")
                .replace("(", r"\(")
                .replace(")", r"\)"))


class _Page:
    """Drawing operators for one page, accumulated as text."""

    def __init__(self):
        self.parts = []

    def text(self, x, y, value, size, bold=False, grey=None):
        font = "/F2" if bold else "/F1"
        pieces = ["BT"]
        if grey is not None:
            pieces.append("%.3f g" % grey)
        pieces.append("%s %.2f Tf" % (font, size))
        pieces.append("1 0 0 1 %.2f %.2f Tm" % (x, y))
        pieces.append("(%s) Tj" % _escape(value))
        pieces.append("ET")
        if grey is not None:
            pieces.append("0 g")
        self.parts.append(" ".join(pieces))

    def line(self, x1, y1, x2, y2, grey=0.75, width=0.5):
        self.parts.append(
            "%.3f G %.2f w %.2f %.2f m %.2f %.2f l S 0 G"
            % (grey, width, x1, y1, x2, y2))

    def rect(self, x, y, width, height, grey=0.93):
        self.parts.append("%.3f g %.2f %.2f %.2f %.2f re f 0 g"
                          % (grey, x, y, width, height))

    def stream(self):
        return "\n".join(self.parts).encode("latin-1", "replace")


def _build(pages, size):
    """
    Assemble finished pages into a PDF file.

    Object numbering is fixed so the xref table can be written without a
    second pass: 1 catalogue, 2 page tree, 3 and 4 the two fonts, then a
    (page, content) pair per page. Every content stream is Flate-compressed,
    which is the one thing that keeps a 40-page register from being a megabyte
    of uncompressed text operators.
    """
    width, height = size
    objects = {}

    kids = " ".join("%d 0 R" % (5 + index * 2) for index in range(len(pages)))

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = ("<< /Type /Pages /Count %d /Kids [%s] >>"
                  % (len(pages), kids)).encode("latin-1")
    objects[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                  b"/Encoding /WinAnsiEncoding >>")
    objects[4] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                  b"/Encoding /WinAnsiEncoding >>")

    for index, page in enumerate(pages):
        page_number = 5 + index * 2
        content_number = page_number + 1

        objects[page_number] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            "/Contents %d 0 R >>" % (width, height, content_number)
        ).encode("latin-1")

        body = zlib.compress(page.stream(), 9)
        objects[content_number] = (
            ("<< /Length %d /Filter /FlateDecode >>\nstream\n"
             % len(body)).encode("latin-1")
            + body
            + b"\nendstream")

    out = bytearray()
    # %PDF-1.4 is the oldest version with everything used here, and the
    # binary comment on line two is what tells a transfer tool this is not
    # text and must not be newline-translated.
    out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"

    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += ("%d 0 obj\n" % number).encode("latin-1")
        out += objects[number]
        out += b"\nendobj\n"

    start = len(out)
    count = max(objects) + 1

    out += ("xref\n0 %d\n" % count).encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, count):
        out += ("%010d 00000 n \n" % offsets[number]).encode("latin-1")

    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (count, start)).encode("latin-1")

    return bytes(out)


def write_table(title, subtitles, columns, rows, landscape=True,
                footer=None):
    """
    Paginate a table into a PDF and return (bytes, report).

    `columns` is a sequence of (heading, width_share, align) triples.
    width_share is RELATIVE - the shares are normalised to whatever printable
    width the page has - so a caller describes a table's proportions once and
    does not have to re-do the arithmetic when the page turns sideways.

    `rows` is a sequence of sequences of values, already in column order.
    Values are stringified and encoded here rather than by the caller, so the
    "?" substitution happens in exactly one place and can be counted.

    The report says how many pages were produced and how many characters could
    not be encoded, which is the number the dashboard warns about. See the
    encoding note at the top of this module.
    """
    size = A4_LANDSCAPE if landscape else A4_PORTRAIT
    page_width, page_height = size
    printable = page_width - 2 * MARGIN

    shares = [max(0.0, float(share)) for _, share, _ in columns]
    total_share = sum(shares) or 1.0
    widths = [printable * share / total_share for share in shares]

    headings = []
    replaced = 0
    for heading, _, _ in columns:
        encoded = encode_text(heading)
        replaced += encoded.replaced
        headings.append(encoded.text)

    aligns = [align for _, _, align in columns]

    title_encoded = encode_text(title)
    replaced += title_encoded.replaced
    encoded_title = title_encoded.text

    encoded_subtitles = []
    for line in (subtitles or []):
        encoded = encode_text(line)
        replaced += encoded.replaced
        encoded_subtitles.append(encoded.text)

    encoded_footer = None
    if footer:
        encoded = encode_text(footer)
        replaced += encoded.replaced
        encoded_footer = encoded.text

    # Where the table starts on a first page (under the title block) and on
    # every page after it. A continued table does not repeat the title, but it
    # does repeat the column headings - a page of numbers whose columns are
    # named on a different sheet is not a report.
    first_top = (page_height - MARGIN - TITLE_SIZE
                 - len(encoded_subtitles) * (SUBTITLE_SIZE + 2) - 14.0)
    later_top = page_height - MARGIN - 4.0
    bottom = MARGIN + (LINE_HEIGHT if encoded_footer else 0.0)

    pages = []
    page = None
    cursor = 0.0
    page_index = 0

    def start_page():
        """Open a page, draw its furniture, and leave the cursor under it."""
        nonlocal page, cursor, page_index

        page = _Page()
        pages.append(page)
        page_index += 1

        if page_index == 1:
            y = page_height - MARGIN - TITLE_SIZE
            page.text(MARGIN, y, encoded_title, TITLE_SIZE, bold=True)
            y -= 4.0
            for line in encoded_subtitles:
                y -= SUBTITLE_SIZE + 2
                page.text(MARGIN, y, line, SUBTITLE_SIZE, grey=0.35)
            cursor = first_top
        else:
            cursor = later_top

        draw_headings()

    def draw_headings():
        """Column headings plus the rule under them."""
        nonlocal cursor

        page.rect(MARGIN, cursor - 3.0, printable, HEADER_SIZE + 6.0)

        x = MARGIN
        for index, heading in enumerate(headings):
            cell = truncate(heading, widths[index] - 4.0, HEADER_SIZE,
                            bold=True)
            page.text(_x(x, widths[index], cell, HEADER_SIZE, True,
                         aligns[index]),
                      cursor, cell, HEADER_SIZE, bold=True)
            x += widths[index]

        cursor -= HEADER_GAP
        page.line(MARGIN, cursor, MARGIN + printable, cursor, grey=0.55)
        cursor -= LINE_HEIGHT

    def _x(left, width, text, size, bold, align):
        """Left edge of a cell's text, honouring its alignment."""
        if align == "right":
            return left + width - 2.0 - text_width(text, size, bold)
        if align == "center":
            return left + (width - text_width(text, size, bold)) / 2.0
        return left + 2.0

    start_page()

    for row in rows:
        if cursor < bottom:
            start_page()

        x = MARGIN
        for index in range(len(columns)):
            value = row[index] if index < len(row) else None
            encoded = encode_text(value)
            replaced += encoded.replaced

            cell = truncate(encoded.text, widths[index] - 4.0, BODY_SIZE)
            page.text(_x(x, widths[index], cell, BODY_SIZE, False,
                         aligns[index]),
                      cursor, cell, BODY_SIZE)
            x += widths[index]

        cursor -= LINE_HEIGHT

    # Footers last, when the page count is finally known.
    if encoded_footer:
        for index, each in enumerate(pages):
            label = "%s  -  page %d of %d" % (encoded_footer, index + 1,
                                              len(pages))
            each.text(MARGIN, MARGIN - 4.0, label, SUBTITLE_SIZE, grey=0.45)

    return _build(pages, size), {
        "pages": len(pages),
        "rows": len(rows),
        "unencodable_characters": replaced,
    }
