# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Minimal PDF content-stream parser: recovers paths and text in page space.

Enough of the operator set to read a CAD-plotted sheet exactly:
graphics state stack, CTM, path construction, painting ops, and text
placement. Curves are flattened to their control points (the elevations use
them only for door swings and bubbles).
"""
import re as _re
import zlib
from pypdf import PdfReader


def _mul(a, b):
    "3x2 affine matrix product: a then b."
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


TOKEN = _re.compile(rb"""
      (?P<num>[-+]?(?:\d+\.\d*|\.\d+|\d+))
    | (?P<name>/[^\s/\[\]()<>{}]*)
    | (?P<str>\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))
    | (?P<hex><[0-9A-Fa-f\s]*>)
    | (?P<arr>[\[\]])
    | (?P<dict><<|>>)
    | (?P<op>[A-Za-z'"*][A-Za-z0-9*'"]*)
""", _re.VERBOSE)


def _unescape(raw):
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            n = body[i + 1]
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            if n in mapping:
                out.append(mapping[n])
                i += 2
                continue
            if 0x30 <= n <= 0x37:
                j = i + 1
                digits = b""
                while j < len(body) and len(digits) < 3 and 0x30 <= body[j] <= 0x37:
                    digits += bytes([body[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(n)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def parse(data):
    """Return (paths, texts).

    paths: dicts with pts (page-space), op (painting operator), fill, stroke, width
    texts: dicts with text, x, y (page-space origin of the text run), size
    """
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    fill_g, stroke_g, lw = 0.0, 0.0, 1.0
    cur = []          # current subpath, page space
    subpaths = []
    start = None
    pos = (0.0, 0.0)
    paths, texts = [], []
    tm = trm = None
    fsize = 0.0
    operands = []

    def flush(op):
        nonlocal cur, subpaths
        if cur:
            subpaths.append(cur)
        for sp in subpaths:
            if len(sp) >= 2:
                paths.append({"pts": sp, "op": op, "fill": fill_g,
                              "stroke": stroke_g, "width": lw})
        cur, subpaths = [], []

    for mo in TOKEN.finditer(data):
        kind = mo.lastgroup
        tok = mo.group()
        if kind == "num":
            operands.append(float(tok))
            continue
        if kind in ("name", "str", "hex", "arr", "dict"):
            operands.append(tok)
            continue

        op = tok.decode("latin-1")
        n = operands

        if op == "q":
            stack.append((ctm, fill_g, stroke_g, lw))
        elif op == "Q":
            if stack:
                ctm, fill_g, stroke_g, lw = stack.pop()
        elif op == "cm" and len(n) >= 6:
            ctm = _mul(tuple(float(v) for v in n[-6:]), ctm)
        elif op == "w" and n:
            lw = float(n[-1])
        elif op == "g" and n:
            fill_g = float(n[-1])
        elif op == "G" and n:
            stroke_g = float(n[-1])
        elif op in ("rg", "sc", "scn") and len(n) >= 3:
            try:
                fill_g = sum(float(v) for v in n[-3:]) / 3.0
            except (TypeError, ValueError):
                pass
        elif op in ("RG", "SC", "SCN") and len(n) >= 3:
            try:
                stroke_g = sum(float(v) for v in n[-3:]) / 3.0
            except (TypeError, ValueError):
                pass
        elif op == "m" and len(n) >= 2:
            if cur:
                subpaths.append(cur)
            pos = (float(n[-2]), float(n[-1]))
            start = pos
            cur = [_apply(ctm, *pos)]
        elif op == "l" and len(n) >= 2:
            pos = (float(n[-2]), float(n[-1]))
            cur.append(_apply(ctm, *pos))
        elif op in ("c", "v", "y") and len(n) >= 2:
            # flatten: keep the endpoint (and control points for c)
            vals = [float(v) for v in n]
            for i in range(0, len(vals) - 1, 2):
                cur.append(_apply(ctm, vals[i], vals[i + 1]))
            pos = (vals[-2], vals[-1])
        elif op == "h":
            if cur and start is not None:
                cur.append(_apply(ctm, *start))
        elif op == "re" and len(n) >= 4:
            x, y, w, h = (float(v) for v in n[-4:])
            if cur:
                subpaths.append(cur)
                cur = []
            subpaths.append([_apply(ctm, x, y), _apply(ctm, x + w, y),
                             _apply(ctm, x + w, y + h), _apply(ctm, x, y + h),
                             _apply(ctm, x, y)])
        elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
            flush(op)
        elif op == "BT":
            tm = trm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        elif op == "ET":
            tm = trm = None
        elif op == "Tf" and len(n) >= 1:
            try:
                fsize = float(n[-1])
            except (TypeError, ValueError):
                pass
        elif op == "Tm" and len(n) >= 6:
            tm = trm = tuple(float(v) for v in n[-6:])
        elif op in ("Td", "TD") and len(n) >= 2:
            if tm is not None:
                tm = _mul((1, 0, 0, 1, float(n[-2]), float(n[-1])), tm)
                trm = tm
        elif op in ("T*",):
            pass
        elif op in ("Tj", "TJ", "'", '"'):
            if trm is not None:
                chunks = []
                for v in n:
                    if isinstance(v, bytes) and v.startswith(b"("):
                        chunks.append(_unescape(v).decode("latin-1"))
                s = "".join(chunks)
                if s.strip():
                    x, y = _apply(_mul(trm, ctm), 0.0, 0.0)
                    sc = (_mul(trm, ctm))
                    texts.append({"text": s, "x": x, "y": y,
                                  "size": fsize * abs(sc[3] or sc[1] or 1.0)})
        operands = []

    return paths, texts


def page(src, pageno):
    reader = PdfReader(src)
    pg = reader.pages[pageno - 1]
    return parse(pg.get_contents().get_data())
