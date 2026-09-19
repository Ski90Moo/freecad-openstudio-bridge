# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Crop the four elevation drawings off the elevation sheet, calibrated by
each one's own crosshair + dimension, and write a manifest in the same
shape fc_place_elevations.py already consumes.

    osvenv/Scripts/python.exe markup/crop_elevations.py FloorplanTest-01.pdf \
        --tif FloorplanTest-02.tif --out-dir markup/elevations

This is the elevation-sheet counterpart of crop_plan.py -- same calibration
source (this page's own /Annots, via read_markup.py), same
render-or-use-a-companion-raster choice, but the frame is a facade's own
local (u, z) -- distance along the elevation, height above its own finish
floor datum -- instead of a flat plan. Each of the four elevations gets its
own crosshair on this sheet, so each is calibrated independently; nothing
here assumes they share an origin the way the two plan pages do.

Which crosshair belongs to which compass elevation is read from this page's
own title text (`NORTH ELEVATION` etc, vector per elevations/README.md's
"What the sheets actually are" table) matched by proximity -- weighted
toward matching row first, since North/South's titles sit at a very
different *x* than their own crosshair (the title callout is at the left of
the drawing block, the crosshair near the height dimensions at the right),
while East and West share a row and are told apart by *x* alone.

Placing the crops onto the model's real facades is a separate, existing
step: fc_place_elevations.py already does that correctly from a manifest in
this shape, matched by facade name, and refuses rather than guess if a crop
reads mirrored -- see its own docstring.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "elevations"))

import pypdfium2 as pdfium                                       # noqa: E402
from PIL import Image                                            # noqa: E402
from pypdf import PdfReader                                      # noqa: E402

import pdfvec                                                    # noqa: E402
import read_markup as rm                                         # noqa: E402

Image.MAX_IMAGE_PIXELS = None

FT_TO_M = 0.3048
PAGE = 3

# Verified for FloorplanTest-02.tif against FloorplanTest-01.pdf -- see
# markup/crop_plan.py's own note. Not a general TIFF property.
TIF_PX_PER_PT = 5.0 / 3.0
DEFAULT_RENDER_SCALE = 6.0

TITLE_RE = re.compile(r"[A-Z][A-Z ]*ELEVATION")
COMPASS = ("NORTH", "SOUTH", "EAST", "WEST")

# Which end of each elevation's own width dimension is real u=0 (the same
# global origin the floor plan's crosshair marks) -- verified this session
# by cross-referencing each elevation's grid-bubble row (vector text) against
# the floor plan's own grid8/gridA position: North and West read backwards
# (their crosshair sits at grid8/gridE, the *far* end from the plan's
# origin), South and East read forwards (crosshair at grid8/gridA, the near
# end). This is specific to this drawing set's convention -- like PAGES and
# LABEL_TO_STORY in crop_plan.py, a different one needs this re-derived, not
# assumed. "u0_at_crosshair": True means the crosshair *is* real u=0;
# False means the crosshair is real u=width (the facade's own measured
# width, so this still self-scales to whatever is actually measured).
FACADE_U0_AT_CROSSHAIR = {
    "North": False, "South": True, "East": True, "West": False,
}

# Generous, uniform height crop for every elevation -- covers footing to
# parapet on this building with margin. Same reasoning as
# elevations/facade_images.py's Z_LO_FT/Z_HI_FT, in metres.
Z_LO_M = -1.5
Z_HI_M = 9.0
MARGIN_M = 1.0

# Weighted match: elevations on the same sheet row (East/West) have almost
# identical title-y and crosshair-y, so y alone cannot tell them apart from
# each other -- but it separates every row from every other row, which x
# cannot, since a title sits far left of its own crosshair. So y dominates
# the match and x only breaks a same-row tie.
Y_WEIGHT = 1.0
X_WEIGHT = 0.1


def match_titles_to_crosshairs(titles, crosshairs):
    pairs = []
    used_t = set()
    candidates = []
    for ci, c in enumerate(crosshairs):
        for ti, t in enumerate(titles):
            d = (Y_WEIGHT * (c["y"] - t["y"])) ** 2 + (X_WEIGHT * (c["x"] - t["x"])) ** 2
            candidates.append((d, ci, ti))
    candidates.sort(key=lambda c: c[0])
    used_c = set()
    for d, ci, ti in candidates:
        if ci in used_c or ti in used_t:
            continue
        used_c.add(ci)
        used_t.add(ti)
        compass = next(w for w in COMPASS if w in titles[ti]["text"])
        pairs.append((compass.title(), crosshairs[ci]))
    return pairs


def load_raster(pdf_path, tif_path, render_scale):
    if tif_path:
        im = Image.open(tif_path)
        im.seek(PAGE - 1)
        h_pt = float(PdfReader(pdf_path).pages[PAGE - 1].mediabox.height)
        return im.convert("RGB"), TIF_PX_PER_PT, h_pt
    pdf = pdfium.PdfDocument(pdf_path)
    page = pdf[PAGE - 1]
    h_pt = page.get_size()[1]
    img = page.render(scale=render_scale).to_pil().convert("RGB")
    return img, render_scale, h_pt


def crop_elevation(frame, px_per_pt, page_h_pt, origin_pt, far_x_pt, pt_per_ft,
                   width_ft, u0_at_crosshair, out_path):
    """`origin_pt` is this facade's crosshair (page pt); `far_x_pt` is the
    other end of its own width dimension. Real u is found by linear
    interpolation between those two page-points and {0, width_ft} in
    whichever order `u0_at_crosshair` (see FACADE_U0_AT_CROSSHAIR) says --
    not by assuming the crosshair is always u=0, which only holds for two
    of these four elevations.
    """
    ox, oy = origin_pt
    width_m = width_ft * FT_TO_M
    span_x = far_x_pt - ox   # page-pt distance crosshair -> far end (signed)

    def u_ft_of(x_pt):
        t = (x_pt - ox) / span_x  # 0 at crosshair, 1 at far end
        return t * width_ft if u0_at_crosshair else (1.0 - t) * width_ft

    def x_pt_of(u_ft):
        t = (u_ft / width_ft) if u0_at_crosshair else (1.0 - u_ft / width_ft)
        return ox + t * span_x

    def m_to_px(u_m, z_m):
        x_pt = x_pt_of(u_m / FT_TO_M)
        y_pt = oy + (z_m / FT_TO_M) * pt_per_ft
        return x_pt * px_per_pt, (page_h_pt - y_pt) * px_per_pt

    u_lo, u_hi = -MARGIN_M, width_m + MARGIN_M
    px0, py1 = m_to_px(u_lo, Z_LO_M)
    px1, py0 = m_to_px(u_hi, Z_HI_M)
    box = (int(round(min(px0, px1))), int(round(min(py0, py1))),
          int(round(max(px0, px1))), int(round(max(py0, py1))))
    box = (max(0, box[0]), max(0, box[1]),
          min(frame.width, box[2]), min(frame.height, box[3]))
    crop = frame.crop(box)
    crop.save(out_path, optimize=True)

    def px_to_m(px, py):
        x_pt, y_pt = px / px_per_pt, page_h_pt - py / px_per_pt
        u_ft = u_ft_of(x_pt)
        z_ft = (y_pt - oy) / pt_per_ft
        return u_ft * FT_TO_M, z_ft * FT_TO_M

    # box[0] (pixel-left) and box[2] (pixel-right) each map to a real u --
    # do NOT force left_m < right_m here. Whichever is numerically smaller
    # depends on FACADE_U0_AT_CROSSHAIR: when the page and the real u run the
    # same way, pixel-left is the smaller u; when this elevation reads
    # backwards (u0_at_crosshair=False), pixel-left is the *larger* u, and
    # forcing an ascending swap would relabel the crop's own left edge as its
    # right, mismatching every pixel against fc_place_elevations.py's frame.
    # That is exactly the "reads right-to-left" refusal it exists to catch.
    left_m, bottom_m = px_to_m(box[0], box[3])
    right_m, top_m = px_to_m(box[2], box[1])
    rect_m = {"left_m": left_m, "right_m": right_m,
              "bottom_m": bottom_m, "top_m": top_m}
    return rect_m, crop.size


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf")
    ap.add_argument("--tif", help="companion raster to crop from; default "
                    "renders --pdf's own page 3 directly")
    ap.add_argument("--render-scale", type=float, default=DEFAULT_RENDER_SCALE)
    ap.add_argument("--color", help="restrict markup search to this "
                    "'r,g,b' colour (0-1 floats); default accepts any "
                    "annotation of the right structural shape")
    ap.add_argument("--titles-from",
                    help="a companion PDF to read this page's "
                         "'<COMPASS> ELEVATION' title positions from, "
                         "scaled by the ratio of the two files' own "
                         "measured drawing scale -- for a 'raster PDF' "
                         "whose own page 3 has no vector text to read")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "elevations"))
    args = ap.parse_args()
    color = tuple(float(v) for v in args.color.split(",")) if args.color else None

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    markup = rm.read_page(args.pdf, PAGE, color=color)
    if len(markup["crosshairs"]) != 4:
        raise SystemExit("page %d: expected 4 crosshairs (one per elevation), "
                         "found %d" % (PAGE, len(markup["crosshairs"])))

    if args.titles_from:
        _, texts = pdfvec.page(args.titles_from, PAGE)
        this_ft_per_pt = sum(d["ft_per_pt"] for d in markup["dimensions"]
                             if d["ft_per_pt"]) / len(
                                 [d for d in markup["dimensions"] if d["ft_per_pt"]])
        companion_markup = rm.read_page(args.titles_from, PAGE)
        companion_ft_per_pt = sum(d["ft_per_pt"] for d in companion_markup["dimensions"]
                                  if d["ft_per_pt"]) / len(
                                      [d for d in companion_markup["dimensions"] if d["ft_per_pt"]])
        k = companion_ft_per_pt / this_ft_per_pt
        titles = [{"text": t["text"].strip(), "x": t["x"] * k, "y": t["y"] * k}
                 for t in texts if TITLE_RE.fullmatch(t["text"].strip())]
        print("titles borrowed from %s, scaled %.4fx" % (args.titles_from, k))
    else:
        _, texts = pdfvec.page(args.pdf, PAGE)
        titles = [{"text": t["text"].strip(), "x": t["x"], "y": t["y"]}
                 for t in texts if TITLE_RE.fullmatch(t["text"].strip())]
    if len(titles) != 4:
        raise SystemExit("page %d: expected 4 '<COMPASS> ELEVATION' vector "
                         "text labels, found %d -- %s (pass --titles-from a "
                         "companion PDF if this page's art is raster)"
                         % (PAGE, len(titles), [t["text"] for t in titles]))

    pairs = match_titles_to_crosshairs(titles, markup["crosshairs"])
    print("crosshair -> facade match:")
    for facade, c in pairs:
        print("  %-6s (%.2f, %.2f) pt" % (facade, c["x"], c["y"]))

    ft_per_pt_vals = [d["ft_per_pt"] for d in markup["dimensions"] if d["ft_per_pt"]]
    if not ft_per_pt_vals:
        raise SystemExit("page %d: no dimension carries a /Measure scale" % PAGE)
    pt_per_ft = 1.0 / (sum(ft_per_pt_vals) / len(ft_per_pt_vals))

    # each facade's own width dimension: the "x"-axis (page-space) dimension
    # whose midpoint is closest to that facade's own crosshair
    frame, px_per_pt, page_h_pt = load_raster(args.pdf, args.tif, args.render_scale)
    entries = []
    for facade, c in pairs:
        if facade not in FACADE_U0_AT_CROSSHAIR:
            raise SystemExit("no FACADE_U0_AT_CROSSHAIR entry for %r -- add "
                             "one (derived from this sheet's own grid-bubble "
                             "row, see the module docstring)" % facade)
        # Match this facade's own width dimension by *y* (its row on the
        # sheet) with *x* required to fall inside (or very near) the
        # dimension's own span -- y alone is not enough: East and West's
        # dimensions sit only 0.36 pt apart in y (same row, different x
        # range), so a crosshair can read as numerically closer to its
        # neighbour's dimension than its own.
        def dim_distance(d):
            # A sum, not a lexicographic (x_penalty, y_diff) tuple: a
            # crosshair sitting a fraction of a point outside its own
            # dimension's span (rounding, not a real ambiguity) must not
            # lose to a neighbour's dimension merely for reading exactly 0 --
            # that neighbour can still be hundreds of points off in y.
            y_mid = (d["p0"][1] + d["p1"][1]) / 2.0
            x_lo, x_hi = sorted((d["p0"][0], d["p1"][0]))
            x_penalty = max(0.0, x_lo - c["x"], c["x"] - x_hi)
            return x_penalty + abs(y_mid - c["y"])

        x_dims = [d for d in markup["dimensions"] if d["axis"] == "x"]
        nearest = min(x_dims, key=dim_distance)
        width_ft = nearest["length_ft"]
        # the dimension's own far endpoint -- whichever of its two ends is
        # not (approximately) this facade's crosshair
        far_x_pt = (nearest["p0"][0]
                   if abs(nearest["p0"][0] - c["x"]) > abs(nearest["p1"][0] - c["x"])
                   else nearest["p1"][0])

        image_name = "%s.png" % facade
        out_path = os.path.join(out_dir, image_name)
        rect_m, px_size = crop_elevation(frame, px_per_pt, page_h_pt,
                                         (c["x"], c["y"]), far_x_pt, pt_per_ft,
                                         width_ft, FACADE_U0_AT_CROSSHAIR[facade],
                                         out_path)
        print("  %-6s width=%.3f ft (%r)  u0_at_crosshair=%s  %dx%d px -> %s"
              % (facade, width_ft, nearest["text"], FACADE_U0_AT_CROSSHAIR[facade],
                 px_size[0], px_size[1], image_name))
        entry = {"facade": facade, "image": image_name}
        entry.update({k: round(v, 6) for k, v in rect_m.items()})
        entries.append(entry)

    manifest = {
        "schema_version": 1,
        "source_pdf": os.path.basename(args.pdf),
        "source_tif": os.path.basename(args.tif) if args.tif else None,
        "sheet": PAGE,
        "units": "meters",
        "images": entries,
    }
    manifest_path = os.path.join(out_dir, "facade_images.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    print("\nwrote %s" % manifest_path)


if __name__ == "__main__":
    main()
