# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Read a sheet's calibration markup off its PDF annotations.

Two things get drawn on a sheet before it comes anywhere near this bridge:

* a **dimension** -- a "Distance"/"Length Measurement" line annotation, with
  a known real-world length -- and
* a **crosshair** -- two short plain line annotations, grouped, crossing at
  one point -- marking the sheet's origin.

Both are *annotations* (PDF ``/Annots``), not content-stream drawing, so
``pdfvec.py``'s path parser cannot see them at all -- it only reads what is
painted, not what is overlaid.  This module reads the annotation dictionaries
directly instead of rendering anything.

Every dimension's length is read two independent ways -- the human-typed
``/Contents`` string, and the endpoint distance run through the annotation's
own ``/Measure`` scale ratio -- and they are required to agree.  That is the
one place a hand-typed number enters this pipeline, so it is checked rather
than trusted.

**Colour is not the signal, the annotation type is.** The first drawing set
this was built against used a deliberate purple (#8000FF) convention, drawn
in Bluebeam. A second set, annotated in Adobe Acrobat instead, used Acrobat's
own default colours -- a reddish orange for its "Distance" tool, a violet for
its sticky notes -- and inches instead of feet-and-inches text. Neither
matters here: an ordinary architectural dimension is page *content* (drawn
once, in ink, by the plotter), never an interactive ``/Subtype /Line`` with
``/IT /LineDimension`` -- that only exists because someone ran a measuring
tool on purpose. So `read_page` accepts any annotation of the right
structural shape by default, regardless of colour; pass `color` to also
require a specific one, for a drawing set busy enough with unrelated
annotations that the structural signal alone would not be enough.

Run standalone to inspect a page:

    osvenv/Scripts/python.exe markup/read_markup.py FloorplanTest-01.pdf --page 1
"""
import argparse
import json
import re
import sys

from pypdf import PdfReader

PURPLE_RGB = (0.5019608, 0.0, 1.0)     # the first drawing set's convention;
                                        # not a default -- pass explicitly.
COLOR_TOL = 0.05
LENGTH_TOL_FT = 1.0 / 96.0          # 1/8 inch -- text vs. Measure must agree
CROSSHAIR_TOL_PT = 1.0              # how far apart two segment endpoints may
                                     # read before they are not "the same x/y"

# NumberFormat /U strings this has actually been seen to carry, -> feet.
_UNIT_TO_FT = {
    "'": 1.0, "ft": 1.0, "feet": 1.0, "foot": 1.0,
    '"': 1.0 / 12.0, "in": 1.0 / 12.0, "inch": 1.0 / 12.0, "inches": 1.0 / 12.0,
}

# Architectural feet-inches: 164'-11 1/4"
_DIM_RE = re.compile(
    r"(\d+)'-(\d+)(?:\s+(\d+)/(\d+))?\"")
# A bare decimal with a unit: 1978.97 in / 164.9 ft
_DECIMAL_RE = re.compile(r"([\d.]+)\s*(in|ft|'|\")", re.IGNORECASE)


class MarkupError(Exception):
    """A calibration annotation does not check out -- refuse rather than
    guess."""


def parse_length_feet(text):
    """'164\'-11 1/4"' or '1978.97 in' -> feet, or None if neither matches."""
    if not text:
        return None
    m = _DIM_RE.search(text)
    if m:
        feet, inches, num, den = m.groups()
        total_in = float(inches)
        if num and den:
            total_in += float(num) / float(den)
        return float(feet) + total_in / 12.0
    m = _DECIMAL_RE.search(text)
    if m:
        value, unit = m.groups()
        factor = _UNIT_TO_FT.get(unit.lower())
        return float(value) * factor if factor is not None else None
    return None


def _get(obj, key, default=None):
    return obj.get(key, default)


def _matches_color(obj, target):
    c = _get(obj, "/C")
    if c is not None and len(c) == 3:
        try:
            if all(abs(float(a) - float(b)) <= COLOR_TOL
                   for a, b in zip(c, target)):
                return True
        except (TypeError, ValueError):
            pass
    target_hex = "%02x%02x%02x" % tuple(round(v * 255) for v in target)
    for key in ("/DS", "/RC"):
        style = _get(obj, key)
        if style and target_hex in str(style).lower():
            return True
    return False


def _measure_ft_per_pt(measure_obj):
    """The /Measure dictionary's own ratio, converted to feet per point
    regardless of which unit it was expressed in (seen: feet, inches)."""
    if measure_obj is None:
        return None
    x_fmt = _get(measure_obj, "/X")
    if not x_fmt:
        return None
    entry = _resolve(x_fmt[0])
    value = entry.get("/C")
    unit = str(entry.get("/U", ""))
    factor = _UNIT_TO_FT.get(unit.lower())
    if value is None or factor is None:
        return None
    return float(value) * factor


def _endpoints(obj):
    L = obj["/L"]
    return (float(L[0]), float(L[1])), (float(L[2]), float(L[3]))


def _resolve(ref):
    return ref.get_object() if hasattr(ref, "get_object") else ref


def read_page(pdf_path, page_no, color=None):
    """Calibration markup on one 1-based page.

    `color` restricts to annotations matching that RGB triple (0-1 floats);
    the default, None, accepts any annotation of the right structural shape
    regardless of colour -- see the module docstring for why that is safe.

    Returns {"page": n, "crosshairs": [...], "dimensions": [...],
             "texts": [...]}.
    """
    reader = PdfReader(pdf_path)
    page = reader.pages[page_no - 1]
    annots = [_resolve(a) for a in page.get("/Annots", [])]
    if color is not None:
        annots = [a for a in annots if _matches_color(a, color)]

    dimensions = []
    plain_lines = []
    texts = []

    for a in annots:
        subtype = str(_get(a, "/Subtype"))
        if subtype in ("/FreeText", "/Text"):
            rect = [float(v) for v in a["/Rect"]]
            texts.append({"text": str(_get(a, "/Contents", "")),
                          "rect": rect})
            continue
        if subtype != "/Line":
            continue
        if _get(a, "/IT") == "/LineDimension":
            p0, p1 = _endpoints(a)
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            axis = "x" if abs(dx) >= abs(dy) else "y"
            length_pt = (dx * dx + dy * dy) ** 0.5

            ft_per_pt = _measure_ft_per_pt(_resolve(_get(a, "/Measure")))
            length_ft_measure = length_pt * ft_per_pt if ft_per_pt else None

            contents = str(_get(a, "/Contents", ""))
            length_ft_text = parse_length_feet(contents)

            if length_ft_text is not None and length_ft_measure is not None:
                if abs(length_ft_text - length_ft_measure) > LENGTH_TOL_FT:
                    raise MarkupError(
                        "page %d: dimension %r disagrees with itself -- "
                        "text says %.4f ft, Measure scale says %.4f ft "
                        "(tolerance %.4f ft)"
                        % (page_no, contents, length_ft_text,
                           length_ft_measure, LENGTH_TOL_FT))
                length_ft = length_ft_text
            else:
                length_ft = length_ft_text or length_ft_measure
            if length_ft is None:
                raise MarkupError(
                    "page %d: dimension at %s has neither a readable "
                    "/Contents string nor a /Measure scale -- cannot "
                    "calibrate from it" % (page_no, (p0, p1)))

            dimensions.append({
                "p0": p0, "p1": p1, "axis": axis,
                "length_ft": length_ft,
                "length_ft_text": length_ft_text,
                "length_ft_measure": length_ft_measure,
                "ft_per_pt": ft_per_pt,
                "text": contents,
            })
        elif _get(a, "/IT") is None:
            plain_lines.append(a)

    crosshairs, unpaired_nm = _find_crosshairs(plain_lines)
    if unpaired_nm:
        print("page %d: %d line annotation(s) did not pair into a "
              "crosshair (ignored): %s" % (page_no, len(unpaired_nm), unpaired_nm),
              file=sys.stderr)

    return {"page": page_no, "crosshairs": crosshairs,
            "dimensions": dimensions, "texts": texts}


def _is_vertical(seg):
    (x0, y0), (x1, y1) = seg
    return abs(y1 - y0) > abs(x1 - x0)


def _origin_of(seg_v, seg_h):
    vx = (seg_v[0][0] + seg_v[1][0]) / 2.0
    hy = (seg_h[0][1] + seg_h[1][1]) / 2.0
    return (vx, hy)


def _crossing_within(seg_v, seg_h, tol):
    """Do a roughly-vertical and a roughly-horizontal segment actually cross
    (within `tol` pt of each other's own extent), rather than merely sit
    near each other?"""
    vx = (seg_v[0][0] + seg_v[1][0]) / 2.0
    hy = (seg_h[0][1] + seg_h[1][1]) / 2.0
    v_ys = sorted((seg_v[0][1], seg_v[1][1]))
    h_xs = sorted((seg_h[0][0], seg_h[1][0]))
    return (h_xs[0] - tol <= vx <= h_xs[1] + tol
            and v_ys[0] - tol <= hy <= v_ys[1] + tol)


def _find_crosshairs(plain_lines):
    """Pair up plain Line annotations into crosshairs two ways:

    1. via /IRT (in-reply-to) -- what Bluebeam's own crosshair tool emits,
       a pair of annotations one of which names the other; or, for
       whichever lines that doesn't account for,
    2. geometrically -- one roughly-vertical and one roughly-horizontal
       segment that actually cross within CROSSHAIR_TOL_PT of each other's
       own extent. This is what a person drawing two independent strokes
       with a plain line tool produces (no grouping metadata at all), and
       it is the more general test in any case: a crosshair *is* two
       perpendicular segments crossing at a point, whichever tool drew them.

    Returns (crosshairs, unpaired_nm).
    """
    by_nm = {str(_get(a, "/NM")): a for a in plain_lines}
    paired_nm = set()
    crosshairs = []

    for a in plain_lines:
        nm = str(_get(a, "/NM"))
        if nm in paired_nm:
            continue
        irt = _get(a, "/IRT")
        if irt is None:
            continue
        target = _resolve(irt)
        target_nm = str(_get(target, "/NM"))
        if target_nm not in by_nm or target_nm in paired_nm:
            continue
        paired_nm.add(nm)
        paired_nm.add(target_nm)
        segs = [_endpoints(a), _endpoints(target)]
        vert = [s for s in segs if _is_vertical(s)]
        horiz = [s for s in segs if not _is_vertical(s)]
        if len(vert) == 1 and len(horiz) == 1:
            crosshairs.append(_origin_of(vert[0], horiz[0]))
        else:
            mids = [((s[0][0] + s[1][0]) / 2.0, (s[0][1] + s[1][1]) / 2.0)
                    for s in segs]
            crosshairs.append((sum(m[0] for m in mids) / len(mids),
                               sum(m[1] for m in mids) / len(mids)))

    # Whatever /IRT left unpaired, try pairing geometrically instead: a
    # roughly-vertical and a roughly-horizontal segment that actually cross.
    # First match wins -- two candidate crossings this close together on one
    # page would be a stranger drawing than this is built to handle.
    remaining = [a for a in plain_lines if str(_get(a, "/NM")) not in paired_nm]
    vert_candidates = [a for a in remaining if _is_vertical(_endpoints(a))]
    horiz_candidates = [a for a in remaining if not _is_vertical(_endpoints(a))]
    for v in vert_candidates:
        v_nm = str(_get(v, "/NM"))
        if v_nm in paired_nm:
            continue
        seg_v = _endpoints(v)
        for h in horiz_candidates:
            h_nm = str(_get(h, "/NM"))
            if h_nm in paired_nm:
                continue
            seg_h = _endpoints(h)
            if not _crossing_within(seg_v, seg_h, CROSSHAIR_TOL_PT):
                continue
            paired_nm.add(v_nm)
            paired_nm.add(h_nm)
            crosshairs.append(_origin_of(seg_v, seg_h))
            break

    unpaired_nm = [nm for nm in by_nm if nm not in paired_nm]
    return [{"x": o[0], "y": o[1]} for o in crosshairs], unpaired_nm


def _parse_color(text):
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected 'r,g,b' as 0-1 floats")
    return tuple(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--color", type=_parse_color,
                    help="restrict to annotations near this 'r,g,b' colour "
                         "(0-1 floats, e.g. 0.5019608,0,1 for the Bluebeam "
                         "purple convention); default accepts any annotation "
                         "of the right structural shape regardless of colour")
    ap.add_argument("--out", help="write JSON here instead of printing")
    args = ap.parse_args()

    data = read_page(args.pdf, args.page, color=args.color)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
            fh.write("\n")
        print("wrote %s" % args.out)

    print("page %d: %d crosshair(s), %d dimension(s), %d text(s)"
          % (data["page"], len(data["crosshairs"]), len(data["dimensions"]),
             len(data["texts"])))
    for c in data["crosshairs"]:
        print("  crosshair at (%.3f, %.3f) pt" % (c["x"], c["y"]))
    for d in data["dimensions"]:
        agree = ("text=%.4f measure=%.4f" % (d["length_ft_text"],
                                              d["length_ft_measure"])
                 if d["length_ft_text"] is not None
                 and d["length_ft_measure"] is not None
                 else "length=%.4f" % d["length_ft"])
        print("  dimension %-14s axis=%s  %s ft  (%s)"
              % (repr(d["text"]), d["axis"], "%.4f" % d["length_ft"], agree))
    for t in data["texts"]:
        print("  text %r at %s" % (t["text"], t["rect"]))


if __name__ == "__main__":
    main()
