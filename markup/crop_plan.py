# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Crop each story's floor plan off the raster sheet, calibrated by the
sheet's own markup, and write fc_seed_floorplan.py's manifest.

Run under the bridge venv:

    osvenv/Scripts/python.exe markup/crop_plan.py FloorplanTest-01.pdf \
        --tif FloorplanTest-02.tif --out-dir markup/plans

or, for a PDF whose raster art lives in the PDF itself rather than a
companion TIFF:

    osvenv/Scripts/python.exe markup/crop_plan.py FloorplanTest-02-Raster.pdf \
        --origin-from FloorplanTest-01.pdf --out-dir markup/plans

Two files, one job each -- read_markup.py knows nothing about which page is
which story or what building this is; this script is the one place that
convention lives, the same split ``elevations/elev.py`` (per-facade grid
constants) and ``elevations/facade_images.py`` (cropping policy) already use.

Calibration always comes from ``--pdf``'s own annotations, not from anything
measured in a raster image -- a TIFF or a flattened PDF page carries no
``/Annots`` to read. Where the pixels come from is a separate choice:
``--tif``, when given, is a companion raster verified pixel-register-identical
to the PDF (see ``TIF_PX_PER_PT``'s docstring); otherwise this renders the
PDF's own pages directly at ``--render-scale`` px/pt, which is exactly what a
"raster PDF" (a flattened image already embedded in the page) needs -- there
is nothing else to crop from.

``--origin-from`` covers the case a page has no crosshair of its own: it
borrows one from the same page number in a companion PDF that does, scaled by
the ratio of the two files' own independently-measured drawing scales. See
markup/README.md for when that is trustworthy and how it is checked.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pypdfium2 as pdfium                                       # noqa: E402
from PIL import Image                                            # noqa: E402
from pypdf import PdfReader                                      # noqa: E402

import read_markup as rm                                         # noqa: E402

Image.MAX_IMAGE_PIXELS = None

FT_TO_M = 0.3048

# Verified this session: FloorplanTest-02.tif is 4080x2640 px at 120 dpi
# against a 2448x1584 pt (34x22 in) PDF page -- 4080/2448 == 2640/1584 ==
# 5/3 == 120/72 exactly, and the purple pixels in each TIFF frame land where
# that ratio predicts from the matching PDF page's /Annots. This is specific
# to that one TIFF/PDF pair, not a general TIFF property -- a different
# companion raster would need its own DPI relationship verified before
# trusting it here, see markup/README.md's limits.
TIF_PX_PER_PT = 5.0 / 3.0

# px/pt when rendering a PDF's own pages directly (no --tif). Arbitrary
# within reason -- higher only costs file size, since the underlying art in
# a "raster PDF" is already fixed-resolution.
DEFAULT_RENDER_SCALE = 6.0

MARGIN_FT = 10.0

# Which PDF page is which story. This is the one place that is specific to
# this drawing set's sheet order -- see elevations/README.md's "What the
# sheets actually are" table, which this mirrors.
PAGES = {1: "Level 2", 2: "Level 1"}
HEIGHTS_PAGE = 3

# The two height dimensions on the elevation sheet are labelled by which
# story they belong to; this is the only place that labelling is turned into
# a story name. A building with more than two stories would need this table
# extended, and a differently-labelled sheet would need it edited.
LABEL_TO_STORY = {"FIRST FLOOR": "Level 1", "SECOND FLOOR": "Level 2"}

# Read directly off both plan pages' own north arrow (points straight up --
# plan-up is true north for this building). Not derived from the markup:
# TAGGING.md's convention is that a north arrow is read by eye, the same as
# the FLOORPLAN/ELEVATION sheet labels this whole scheme assumes an AI can
# already read without help.
NORTH_AXIS_DEG = 0.0

# How far a borrowed origin (see --origin-from) may sit from this page's own
# nearest dimension-line endpoint before it is refused rather than trusted.
# Not zero: a dimension line's own end floats off the true wall corner by
# whatever leader/witness offset the person drew it with, so some slack is
# expected even when the borrowed origin is exactly right.
ORIGIN_BORROW_TOL_FT = 2.0


# How far pages 1-2 may disagree on the building's own width/depth before
# that is a real problem rather than hand-drawing noise.
ENVELOPE_TOL_FT = 0.1


def envelope(markup_by_page):
    """(width_ft, depth_ft, width_src_pages, depth_src_pages).

    Building width/depth in feet, gathered from whichever of pages 1-2 carry
    an x-axis / y-axis dimension, and averaged when more than one page has
    one. Refuses if two pages disagree by more than ENVELOPE_TOL_FT (a
    vector dimension agrees exactly across pages; a hand-drawn one on a
    raster image does not, so some slack is expected there), or if either
    axis has no source at all.
    """
    width = {}
    depth = {}
    for page, m in markup_by_page.items():
        for d in m["dimensions"]:
            (width if d["axis"] == "x" else depth).setdefault(page, d["length_ft"])

    def resolve(values, axis_name):
        if not values:
            raise SystemExit("no %s-axis dimension on pages %s"
                             % (axis_name, sorted(markup_by_page)))
        spread = max(values.values()) - min(values.values())
        if spread > ENVELOPE_TOL_FT:
            raise SystemExit("pages disagree on building %s by %.3f ft: %s"
                             % (axis_name, spread, values))
        return sum(values.values()) / len(values)

    return (resolve(width, "x (width)"), resolve(depth, "y (depth)"),
            sorted(width), sorted(depth))


def page_ft_per_pt(markup):
    """This one page's own scale, averaged over whatever dimensions it has."""
    vals = [d["ft_per_pt"] for d in markup["dimensions"] if d["ft_per_pt"]]
    if not vals:
        raise SystemExit("page %d: no dimension carries a /Measure scale"
                         % markup["page"])
    return sum(vals) / len(vals)


def global_pt_per_ft(markup_by_page):
    """One scale for the whole sheet set, cross-checked across every page.

    Every dimension already agreed with its own /Measure scale in
    read_markup.py; this additionally checks that every dimension on every
    page implies the *same* scale, which a mixed-scale drawing set would
    violate. Rounded to 1% before comparing -- a hand-drawn dimension's own
    scale is not exact the way a vector one's is (see markup/README.md).
    """
    vals = [d["ft_per_pt"] for m in markup_by_page.values()
            for d in m["dimensions"] if d["ft_per_pt"]]
    if not vals:
        raise SystemExit("no dimension on any page carries a /Measure scale")
    mean = sum(vals) / len(vals)
    if max(abs(v - mean) for v in vals) > 0.01 * mean:
        raise SystemExit("sheets disagree on drawing scale by more than 1%%: %s"
                         % sorted(set(round(v, 6) for v in vals)))
    return 1.0 / mean


def floor_heights(page3_markup):
    """{"Level 1": floor_to_floor_m, "Level 2": floor_to_floor_m}.

    Matches each vertical (page-space) dimension on the elevation sheet to
    the FIRST FLOOR / SECOND FLOOR label whose text sits inside that
    dimension's own extent -- proximity, not position, since both are drawn
    along the same vertical run.
    """
    heights = {}
    for d in page3_markup["dimensions"]:
        if d["axis"] != "y":
            continue
        y_lo, y_hi = sorted((d["p0"][1], d["p1"][1]))
        for t in page3_markup["texts"]:
            _, ry0, _, ry1 = t["rect"]
            ty = (ry0 + ry1) / 2.0
            if not (y_lo - 1.0 <= ty <= y_hi + 1.0):
                continue
            label = t["text"].strip().upper()
            story = LABEL_TO_STORY.get(label)
            if story is None:
                continue
            heights[story] = d["length_ft"] * FT_TO_M
            print("  %-8s <- %r %.4f ft = %.4f m"
                  % (story, d["text"], d["length_ft"], heights[story]))
    return heights


def resolve_origin(page, story, markup, this_ft_per_pt, args):
    """(x, y) in this page's own pt-space, and a one-line source note."""
    crosshairs = markup["crosshairs"]
    if len(crosshairs) == 1:
        return (crosshairs[0]["x"], crosshairs[0]["y"]), "own crosshair"
    if len(crosshairs) > 1:
        raise SystemExit("page %d (%s): %d crosshairs found -- ambiguous"
                         % (page, story, len(crosshairs)))
    if not args.origin_from:
        raise SystemExit(
            "page %d (%s) has no crosshair, and no --origin-from was given "
            "to borrow one from a companion PDF that has this same page "
            "calibrated" % (page, story))

    companion = rm.read_page(args.origin_from, page, color=args.origin_from_color)
    cch = companion["crosshairs"]
    if len(cch) != 1:
        raise SystemExit(
            "--origin-from %s page %d: expected exactly 1 crosshair, found %d"
            % (args.origin_from, page, len(cch)))
    companion_ft_per_pt = page_ft_per_pt(companion)
    k = companion_ft_per_pt / this_ft_per_pt
    origin = (cch[0]["x"] * k, cch[0]["y"] * k)

    # Plausibility check: a dimension line's own endpoint sits close to the
    # true wall corner even without a crosshair (offset only by whatever
    # leader/witness distance the person drew it with), so the borrowed
    # origin should land near at least one. This does not prove the "same
    # page corner, uniform scale" assumption behind the borrow -- that was
    # confirmed by eye this session, against the actual pixels -- but it
    # catches a gross mismatch (wrong page, a non-uniformly-scaled export)
    # automatically on every future run.
    candidates = [p for d in markup["dimensions"] for p in (d["p0"], d["p1"])]
    if not candidates:
        raise SystemExit(
            "page %d (%s): borrowed an origin but this page has no "
            "dimension of its own to sanity-check it against" % (page, story))
    nearest = min(candidates, key=lambda p: (p[0] - origin[0]) ** 2
                                            + (p[1] - origin[1]) ** 2)
    dist_ft = (((nearest[0] - origin[0]) ** 2 + (nearest[1] - origin[1]) ** 2)
              ** 0.5) * this_ft_per_pt
    if dist_ft > ORIGIN_BORROW_TOL_FT:
        raise SystemExit(
            "page %d (%s): origin borrowed from %s (scaled %.4fx) sits "
            "%.2f ft from the nearest dimension-line endpoint on this page "
            "-- too far to trust; check --origin-from and the page numbers "
            "line up" % (page, story, os.path.basename(args.origin_from), k,
                        dist_ft))
    return origin, ("borrowed from %s page %d (scaled %.4fx), %.2f ft from "
                    "this page's own nearest dimension endpoint"
                    % (os.path.basename(args.origin_from), page, k, dist_ft))


def load_raster(args, page):
    """(PIL.Image RGB, px_per_pt, page_height_pt) for this story's source."""
    if args.tif:
        im = Image.open(args.tif)
        im.seek(page - 1)
        h_pt = float(PdfReader(args.pdf).pages[page - 1].mediabox.height)
        return im.convert("RGB"), TIF_PX_PER_PT, h_pt
    pdf = pdfium.PdfDocument(args.pdf)
    pdf_page = pdf[page - 1]
    h_pt = pdf_page.get_size()[1]
    img = pdf_page.render(scale=args.render_scale).to_pil().convert("RGB")
    return img, args.render_scale, h_pt


def crop_story(args, page, origin_pt, pt_per_ft, width_ft, depth_ft,
               margin_ft, out_path):
    frame, px_per_pt, page_h_pt = load_raster(args, page)
    ox, oy = origin_pt

    def ft_to_px(x_ft, y_ft):
        x_pt, y_pt = ox + x_ft * pt_per_ft, oy + y_ft * pt_per_ft
        return x_pt * px_per_pt, (page_h_pt - y_pt) * px_per_pt

    px0, py1 = ft_to_px(-margin_ft, -margin_ft)
    px1, py0 = ft_to_px(width_ft + margin_ft, depth_ft + margin_ft)
    box = (int(round(px0)), int(round(py0)), int(round(px1)), int(round(py1)))
    box = (max(0, box[0]), max(0, box[1]),
           min(frame.width, box[2]), min(frame.height, box[3]))
    crop = frame.crop(box)
    crop.save(out_path, optimize=True)

    def px_to_ft(px, py):
        x_pt, y_pt = px / px_per_pt, page_h_pt - py / px_per_pt
        return (x_pt - ox) / pt_per_ft, (y_pt - oy) / pt_per_ft

    left_ft, bottom_ft = px_to_ft(box[0], box[3])
    right_ft, top_ft = px_to_ft(box[2], box[1])
    rect_m = {"left_m": left_ft * FT_TO_M, "right_m": right_ft * FT_TO_M,
              "bottom_m": bottom_ft * FT_TO_M, "top_m": top_ft * FT_TO_M}
    return rect_m, crop.size


def _parse_color(text):
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected 'r,g,b' as 0-1 floats")
    return tuple(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf")
    ap.add_argument("--tif", help="companion raster to crop from; default "
                    "renders --pdf's own pages directly (for a 'raster PDF' "
                    "whose art is already a flattened image in the page)")
    ap.add_argument("--render-scale", type=float, default=DEFAULT_RENDER_SCALE,
                    help="px/pt when rendering --pdf directly (default "
                         "%(default)s; ignored when --tif is given)")
    ap.add_argument("--origin-from",
                    help="a companion PDF to borrow a page's crosshair from "
                         "(scaled by the ratio of the two files' own "
                         "measured drawing scale) when --pdf's own page has "
                         "none")
    ap.add_argument("--origin-from-color", type=_parse_color,
                    help="restrict --origin-from's crosshair search to this "
                         "'r,g,b' colour (0-1 floats); default accepts any")
    ap.add_argument("--color", type=_parse_color,
                    help="restrict --pdf's own markup to this 'r,g,b' "
                         "colour (0-1 floats); default accepts any "
                         "annotation of the right structural shape")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "plans"))
    ap.add_argument("--margin-ft", type=float, default=MARGIN_FT)
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    pages_needed = list(PAGES) + [HEIGHTS_PAGE]
    markup = {p: rm.read_page(args.pdf, p, color=args.color) for p in pages_needed}

    width_ft, depth_ft, width_src, depth_src = envelope(
        {p: markup[p] for p in PAGES})
    print("envelope: %.4f x %.4f ft  (width from page%s %s, depth from page%s %s)"
          % (width_ft, depth_ft, "s" if len(width_src) > 1 else "", width_src,
             "s" if len(depth_src) > 1 else "", depth_src))

    pt_per_ft = global_pt_per_ft(markup)
    ft_per_pt = 1.0 / pt_per_ft
    print("scale: %.4f pt/ft, cross-checked across %d page(s)"
          % (pt_per_ft, len(pages_needed)))

    print("\nfloor-to-floor heights, from page %d:" % HEIGHTS_PAGE)
    heights = floor_heights(markup[HEIGHTS_PAGE])
    missing = [s for s in ("Level 1", "Level 2") if s not in heights]
    if missing:
        raise SystemExit(
            "could not derive a floor-to-floor height for %s from page %d's "
            "markup -- check it carries a FIRST FLOOR / SECOND FLOOR "
            "labelled height dimension" % (missing, HEIGHTS_PAGE))

    elevation_m = {"Level 1": 0.0, "Level 2": heights["Level 1"]}
    print("\nLevel 2 stacks at z = %.4f m (Level 1's derived floor-to-floor)"
          % elevation_m["Level 2"])

    source = args.tif if args.tif else "%s (rendered)" % args.pdf
    print("\ncropping stories from %s:" % os.path.basename(source))
    stories = []
    for page, story in sorted(PAGES.items()):
        m = markup[page]
        origin_pt, origin_note = resolve_origin(page, story, m, ft_per_pt, args)
        if origin_note != "own crosshair":
            print("  %-8s origin: %s" % (story, origin_note))
        image_name = "%s.png" % story.replace(" ", "")
        out_path = os.path.join(out_dir, image_name)
        rect_m, px_size = crop_story(args, page, origin_pt, pt_per_ft,
                                     width_ft, depth_ft, args.margin_ft,
                                     out_path)
        print("  %-8s page %d  origin=(%.2f, %.2f) pt  %dx%d px -> %s"
              % (story, page, origin_pt[0], origin_pt[1], px_size[0],
                 px_size[1], image_name))
        entry = {"name": story, "page": page, "image": image_name,
                 "elevation_m": round(elevation_m[story], 6)}
        entry.update({k: round(v, 6) for k, v in rect_m.items()})
        stories.append(entry)

    # Elevation order, not page order, so the object FreeCAD mints "Sketch"
    # for is the ground floor and "Sketch001" is the one stacked above it --
    # matching the existing sample's own convention (README section 3).
    stories.sort(key=lambda s: s["elevation_m"])

    manifest = {
        "schema_version": 1,
        "source_pdf": os.path.basename(args.pdf),
        "source_tif": os.path.basename(args.tif) if args.tif else None,
        "units": "meters",
        "north_axis_deg": NORTH_AXIS_DEG,
        "envelope_ft": {"width": width_ft, "depth": depth_ft},
        "top_story_name": "Level 2",
        "top_story_floor_to_floor_m": round(heights["Level 2"], 6),
        "stories": stories,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    print("\nwrote %s" % manifest_path)


if __name__ == "__main__":
    main()
