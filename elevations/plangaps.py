# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Wall openings read as gaps in the plan's exterior wall lines.

The plans are vector, so these are exact. Page 2 is Level 1, page 1 is
Level 2, and both sheets carry the same column grid, so one calibration
serves both.

The elevations give an opening's sill and head; the plan gives its width and
position along the wall to the width of a drawn line. Where both see the same
opening they agree to about half an inch, which is what licenses using the
plan for the horizontal extent.
"""
import pdfvec
import elev

PAGE = {1: 1, 2: 2}          # storey index -> pdf page (L1 = page 2)
SHEET = {1: 2, 2: 1}

# inner face of each exterior wall, grid-line frame, feet
WALL = {
    "SOUTH": ("h", 0.667),
    "NORTH": ("h", 59.307),
    "WEST": ("v", 0.667),
    "EAST": ("v", 164.267),
}

_cache = {}


def segments(storey, orient, at, tol=0.05):
    key = (storey, orient, round(at, 3))
    if key in _cache:
        return _cache[key]
    paths, _ = pdfvec.page(elev.SRC, SHEET[storey])
    runs = []
    for p in paths:
        for a, b in zip(p["pts"], p["pts"][1:]):
            if orient == "h":
                if abs(a[1] - b[1]) > 0.06:
                    continue
                pos = ((a[1] + b[1]) / 2 - elev.PLAN_ROW["A"]) / elev.PT
                lo, hi = sorted([(a[0] - elev.PLAN_COL["8"]) / elev.PT,
                                 (b[0] - elev.PLAN_COL["8"]) / elev.PT])
            else:
                if abs(a[0] - b[0]) > 0.06:
                    continue
                pos = ((a[0] + b[0]) / 2 - elev.PLAN_COL["8"]) / elev.PT
                lo, hi = sorted([(a[1] - elev.PLAN_ROW["A"]) / elev.PT,
                                 (b[1] - elev.PLAN_ROW["A"]) / elev.PT])
            if abs(pos - at) > tol or hi - lo < 0.05:
                continue
            runs.append([lo, hi])
    runs.sort()
    merged = []
    for s, e in runs:
        if merged and s <= merged[-1][1] + 0.03:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    _cache[key] = merged
    return merged


def gaps(facade, storey, min_w=1.5, max_w=20.0):
    orient, at = WALL[facade]
    runs = segments(storey, orient, at)
    out = []
    for (s0, e0), (s1, e1) in zip(runs, runs[1:]):
        w = s1 - e0
        if min_w <= w <= max_w:
            out.append((e0, s1, w))
    return out


if __name__ == "__main__":
    for facade in ("SOUTH", "NORTH", "WEST", "EAST"):
        for storey in (1, 2):
            g = gaps(facade, storey)
            print("%-6s storey %d : %d opening(s)" % (facade, storey, len(g)))
            for a, b, w in g:
                print("      %8.3f .. %8.3f   width %6.3f ft" % (a, b, w))
        print()
