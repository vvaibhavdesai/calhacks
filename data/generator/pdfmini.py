"""Minimal deterministic PDF writer (stdlib only) for synthetic Encore documents.

Produces small, readable, uncompressed PDF 1.4 files: Helvetica text, US Letter,
automatic word-wrap and pagination. No timestamps, IDs or other varying metadata,
so the same input always yields the same bytes (needed for manifest hashes).

Only printable ASCII is supported; callers must not pass other characters.
"""
from __future__ import annotations

import textwrap

PAGE_W, PAGE_H = 612, 792
LEFT, TOP = 54, 744
LEADING = 13
FONT_SIZE = 10
LINES_PER_PAGE = 54
WRAP = 92
PAGE_BREAK = "\f"


def _check_ascii(s: str) -> None:
    for ch in s:
        if not (32 <= ord(ch) < 127):
            raise ValueError(f"pdfmini supports printable ASCII only, got {ch!r} in {s!r}")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def layout(paragraphs: list[str]) -> list[list[str]]:
    """Wrap paragraphs into lines and split into pages.

    A paragraph equal to PAGE_BREAK forces a new page. An empty string is a blank line.
    """
    pages: list[list[str]] = [[]]
    for para in paragraphs:
        if para == PAGE_BREAK:
            if pages[-1]:
                pages.append([])
            continue
        lines = textwrap.wrap(para, WRAP, break_long_words=True, break_on_hyphens=False) if para else [""]
        for ln in lines:
            _check_ascii(ln)
            if len(pages[-1]) >= LINES_PER_PAGE:
                pages.append([])
            pages[-1].append(ln)
    if not pages[-1] and len(pages) > 1:
        pages.pop()
    return pages


def build_pdf(title: str, paragraphs: list[str]) -> tuple[bytes, int]:
    """Return (pdf_bytes, page_count)."""
    _check_ascii(title)
    pages = layout(paragraphs)
    n = len(pages)
    # object numbers: 1 catalog, 2 pages, 3 font, 4 info, then (page, content) pairs
    objs: dict[int, bytes] = {}
    kids = []
    for i, lines in enumerate(pages):
        page_no, content_no = 5 + 2 * i, 6 + 2 * i
        kids.append(f"{page_no} 0 R")
        body = [f"BT\n/F1 {FONT_SIZE} Tf\n{LEADING} TL\n{LEFT} {TOP} Td"]
        for j, ln in enumerate(lines):
            if j:
                body.append("T*")
            body.append(f"({_esc(ln)}) Tj")
        body.append("ET")
        stream = ("\n".join(body) + "\n").encode("ascii")
        objs[content_no] = (f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream")
        objs[page_no] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                         f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>").encode("ascii")
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n} >>".encode("ascii")
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[4] = f"<< /Title ({_esc(title)}) /Producer (Encore synthetic generator - fictional data) >>".encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii") + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {size} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
    return bytes(out), n
