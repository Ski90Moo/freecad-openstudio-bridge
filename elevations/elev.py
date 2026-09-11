# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Elevation geometry: page 3 pixels/points -> building feet.

Everything is anchored on the *column grid lines*, which are vector on every
sheet and therefore exact. Anchoring on the grid bubble text instead costs a
constant glyph-width error (+2.64 pt on digits, +3.36 pt on letters) that does
not cancel on a mirrored elevation -- it showed up as a 0.60 ft shift between
the elevation doors and the plan's door openings.

Vertical anchoring is the elevation datum lines, which land on the labelled
2'/10'/20' to better than 0.01 ft.
"""
import os

import pdfvec

# The drawing set these coordinates were read off.  Override with the ELEV_PDF
# environment variable; the constants below are specific to that sheet set and
# are the thing to re-measure for a different one.
SRC = os.environ.get("ELEV_PDF", "FloorplanTest-01.pdf")
PT = 9.0

# --- page 2 (plan): grid lines, vector ---
PLAN_COL = {"8": 402.00, "7": 536.88, "6": 748.32, "5": 883.20, "4": 1132.80,
            "3": 1382.64, "2": 1634.40, "1": 1886.40, "0": 1940.40}
PLAN_ROW = {"A": 609.36, "B": 739.92, "C": 789.36, "D": 969.12, "E": 1149.12}

# grid position in "plan feet", grid 8 / grid A line = 0
PLAN_X = {k: (v - PLAN_COL["8"]) / PT for k, v in PLAN_COL.items()}
PLAN_Y = {k: (v - PLAN_ROW["A"]) / PT for k, v in PLAN_ROW.items()}

# model_ft = plan_ft.  The traced envelope is the exterior wall OUTER face,
# and that face sits exactly on the column grid on three sides:
#   south wall outer 0.000 = grid A      west wall outer 0.000 = grid 8
#   north wall outer 59.973 = grid E     model span 59.994 (0.25 in over)
# so the two frames coincide. (A nearest-line fit cannot see this: the wall
# faces are 0.667 ft apart, so every offset scores near zero.)
OFF_X = 0.0
OFF_Y = 0.0

# --- page 3 (elevations): grid lines, vector ---
ELEV_GRID = {
    "NORTH": ({"0": 420.96, "1": 474.96, "2": 726.72, "3": 978.72,
               "4": 1228.32, "5": 1477.92, "6": 1613.04, "7": 1824.48,
               "8": 1959.36}, "X"),
    "SOUTH": ({"8": 474.96, "7": 609.84, "6": 821.28, "5": 956.16,
               "4": 1205.76, "3": 1455.60, "2": 1707.36, "1": 1959.36,
               "0": 2013.36}, "X"),
    "WEST": ({"E": 474.96, "D": 654.72, "C": 834.72, "B": 884.16,
              "A": 1014.72}, "Y"),
    "EAST": ({"A": 1419.60, "B": 1549.92, "C": 1599.36, "D": 1779.36,
              "E": 1959.36}, "Y"),
}

# pdf y of the 2-Finish Floor datum line for each elevation
DATUM = {"NORTH": 1207.40, "SOUTH": 223.20, "WEST": 736.10, "EAST": 736.10}


def _lsq(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    m = sxy / sxx
    return m, my - m * mx


def fits():
    """name -> (m, c, off, z0, axis).

    The same least-squares fit mappings() uses, handed back as coefficients so
    it can be inverted -- "which pdf x is model foot 175?" is the question a
    crop window asks, and a lambda cannot answer it.
    """
    out = {}
    for name, (grid, axis) in ELEV_GRID.items():
        ref = PLAN_X if axis == "X" else PLAN_Y
        off = OFF_X if axis == "X" else OFF_Y
        keys = list(grid)
        m, c = _lsq([grid[k] for k in keys], [ref[k] for k in keys])
        out[name] = (m, c, off, DATUM[name], axis)
    return out


def mappings(report=False):
    """name -> (u_of_pdfx, z_of_pdfy), both returning model feet."""
    out = {}
    for name, (grid, axis) in ELEV_GRID.items():
        ref = PLAN_X if axis == "X" else PLAN_Y
        off = OFF_X if axis == "X" else OFF_Y
        xs = [grid[k] for k in grid]
        ys = [ref[k] for k in grid]
        m, c = _lsq(xs, ys)
        if report:
            res = max(abs(m * x + c - y) for x, y in zip(xs, ys))
            print("%-6s slope %+.6f (1/9=%.6f)  max grid resid %.4f ft (%.2f in)"
                  % (name, m, 1 / PT, res, res * 12))
        z0 = DATUM[name]
        out[name] = (
            (lambda m, c, off: (lambda x: m * x + c + off))(m, c, off),
            (lambda z0: (lambda y: (y - z0) / PT))(z0),
        )
    return out


def all_paths():
    return pdfvec.page(SRC, 3)


if __name__ == "__main__":
    mappings(report=True)
