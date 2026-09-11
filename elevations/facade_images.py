# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Crop each elevation off sheet 3 and say exactly where it belongs in 3D.

Run under the bridge venv:

    osvenv/Scripts/python.exe elevations/facade_images.py

Writes one PNG per elevation plus facade_images.json, which fc_place_elevations
turns into image planes in the FreeCAD document.

The point is registration.  A hand-placed elevation image carries whatever
scale and offset error the placing hand had -- measured against the vector
plans, the one placed by hand for this building was 0.25% out and 81 mm
adrift, growing to 0.2 m at the far end of the facade.  Here the crop window
is chosen in *model feet* and converted to pdf points through the same
grid-line calibration the openings came from, so the image lands where the
drawing says, to the width of a drawn line.

Unlike render.py this keeps the vector annotation.  Grid bubbles, datum lines
and dimension strings were noise for automated rectangle detection; for a
person tracing an elevation they are the most useful thing on the sheet.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pypdfium2 as pdfium                                       # noqa: E402
from PIL import Image                                            # noqa: E402

import elev                                                      # noqa: E402

Image.MAX_IMAGE_PIXELS = None

FT_TO_M = 0.3048
PAGE = 2                       # zero-based; sheet 3 is the elevations
PAGE_H_PT = 1584.0

# Render at twice the drawing's own 4.167 px/pt and box-filter back down: the
# source tiles are 1-bit, so this is what turns a hard-edged bilevel crop into
# readable greys instead of aliased speckle.
NATIVE_PX_PER_PT = 927.0 / 222.48
OVERSAMPLE = 2

# Crop window, model feet.  Horizontally the column grid's own span plus a
# margin, so it adapts to the building rather than to this one.  Vertically a
# fixed band about the finish-floor datum -- wide enough for footing to parapet
# and narrow enough not to reach the elevation stacked above.
MARGIN_FT = 4.0
Z_LO_FT = -4.0
Z_HI_FT = 30.0

FACADES = {"NORTH": "North", "SOUTH": "South",
           "WEST": "West", "EAST": "East"}


def crop_window(axis):
    """(u_lo, u_hi) in model feet for an elevation running along `axis`."""
    ref = elev.PLAN_X if axis == "X" else elev.PLAN_Y
    return min(ref.values()) - MARGIN_FT, max(ref.values()) + MARGIN_FT


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", default=elev.SRC)
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "facades"))
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    manifest = args.manifest or os.path.join(out_dir, "facade_images.json")

    scale = NATIVE_PX_PER_PT * OVERSAMPLE
    pdf = pdfium.PdfDocument(args.pdf)
    page = pdf[PAGE]
    sheet = page.render(scale=scale, grayscale=True).to_pil().convert("L")
    print("sheet %d rendered %dx%d px (%.3f px/pt, %.1f px/ft)"
          % (PAGE + 1, sheet.width, sheet.height, scale, scale * elev.PT))

    entries = []
    for name, label in FACADES.items():
        m, c, off, z0, axis = elev.fits()[name]
        u_lo, u_hi = crop_window(axis)

        # model feet -> pdf points, the inverse of the grid-line fit
        x_of_u = lambda u: (u - c - off) / m                      # noqa: E731
        y_of_z = lambda z: z0 + z * elev.PT                       # noqa: E731

        px_lo, px_hi = sorted((x_of_u(u_lo), x_of_u(u_hi)))
        py_top, py_bot = y_of_z(Z_HI_FT), y_of_z(Z_LO_FT)

        box = (int(round(px_lo * scale)),
               int(round((PAGE_H_PT - py_top) * scale)),
               int(round(px_hi * scale)),
               int(round((PAGE_H_PT - py_bot) * scale)))
        crop = sheet.crop(box)
        crop = crop.resize((crop.width // OVERSAMPLE, crop.height // OVERSAMPLE),
                           Image.LANCZOS)
        path = os.path.join(out_dir, "Elevation-%s.png" % label)
        crop.save(path, optimize=True)

        # What the crop's own left and right edges are in model coordinates.
        # The elevations are mirrored in pairs -- north and west read right to
        # left along the building axis -- so the left edge is not always u_lo.
        u_left = m * (box[0] / scale) + c + off
        u_right = m * (box[2] / scale) + c + off
        z_top = (PAGE_H_PT - box[1] / scale - z0) / elev.PT
        z_bot = (PAGE_H_PT - box[3] / scale - z0) / elev.PT

        entries.append({
            "facade": label,
            "image": os.path.basename(path),
            "axis": axis.lower(),
            "left_m": round(u_left * FT_TO_M, 6),
            "right_m": round(u_right * FT_TO_M, 6),
            "bottom_m": round(z_bot * FT_TO_M, 6),
            "top_m": round(z_top * FT_TO_M, 6),
        })
        print("  %-6s %5dx%-5d px  along %s %8.3f -> %8.3f m   z %6.3f -> "
              "%6.3f m  %s"
              % (label, crop.width, crop.height, axis.lower(),
                 u_left * FT_TO_M, u_right * FT_TO_M,
                 z_bot * FT_TO_M, z_top * FT_TO_M,
                 "%.0f kB" % (os.path.getsize(path) / 1024.0)))

    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1,
                   "source_pdf": os.path.basename(args.pdf),
                   "sheet": PAGE + 1,
                   "units": "meters",
                   "px_per_ft": round(scale * elev.PT / OVERSAMPLE, 4),
                   "images": entries}, fh, indent=1)
        fh.write("\n")
    print("\nwrote %s" % manifest)


if __name__ == "__main__":
    main()
