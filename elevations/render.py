# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Render page 3's drawing layer (images only, annotation stripped) to a bitmap.

Keeping the vector annotation out matters: the grid lines, datum lines and
dimension strings cross the elevations and would otherwise be indistinguishable
from window frames during rectangle detection.
"""
import os
import re
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, ArrayObject

SRC = os.environ.get("ELEV_PDF", "FloorplanTest-01.pdf")
STRIPPED = "page3_images_only.pdf"
PAGE_W, PAGE_H = 2448.0, 1584.0
SCALE = 2 * 927.0 / 222.48       # px per pt = 8.3333 -> 75 px/ft, 6.25 px/in

PLACE = re.compile(
    rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+"
    rb"([-\d.]+)\s+cm\s*/(\w+)\s+Do")


def strip():
    reader = PdfReader(SRC)
    page = reader.pages[2]
    data = page.get_contents().get_data()
    parts = []
    for m in PLACE.finditer(data):
        a, b, c, d, e, f = (m.group(i).decode() for i in range(1, 7))
        parts.append("q %s %s %s %s %s %s cm /%s Do Q"
                     % (a, b, c, d, e, f, m.group(7).decode()))
    print("kept %d image placements" % len(parts))

    writer = PdfWriter()
    writer.add_page(page)
    newpage = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("latin-1"))
    newpage[NameObject("/Contents")] = writer._add_object(stream)
    if "/Annots" in newpage:
        newpage[NameObject("/Annots")] = ArrayObject()
    with open(STRIPPED, "wb") as fh:
        writer.write(fh)
    print("wrote %s" % STRIPPED)


def render(path=STRIPPED, out="draw.png"):
    pdf = pdfium.PdfDocument(path)
    page = pdf[0]
    bmp = page.render(scale=SCALE, grayscale=True)
    im = bmp.to_pil().convert("L")
    print("rendered %dx%d px  (%.4f px/pt, %.2f px/ft)"
          % (im.width, im.height, SCALE, SCALE * 9))
    im.save(out)
    h = im.histogram()
    dark = sum(h[:128])
    print("dark px: %d (%.2f%%)" % (dark, 100.0 * dark / (im.width * im.height)))
    return im


def px_of(x_pt, y_pt):
    "page points -> pixel (col, row)"
    return x_pt * SCALE, (PAGE_H - y_pt) * SCALE


def pt_of(col, row):
    return col / SCALE, PAGE_H - row / SCALE


if __name__ == "__main__":
    strip()
    render()
