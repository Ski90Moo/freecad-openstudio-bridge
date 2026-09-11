# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Detect filled regions in the rendered elevations and report them in
building feet.

The drawing is 1-bit line art with three flat fill tones (192 glazing,
178 and 134 darker), rendered at 75 px/ft. Components are found by
run-length connected components -- deterministic, no thresholding
judgement beyond "which flat tone is this".
"""
import numpy as np
from PIL import Image

import elev
import render

Image.MAX_IMAGE_PIXELS = None

FILLS = (192, 178, 134)
TOL = 3

# crop windows in page points: (x0, x1, y0, y1)
REGION = {"NORTH": (400, 1990, 1180, 1420),
          "WEST": (460, 1025, 690, 1010),
          "EAST": (1400, 1975, 690, 1010),
          "SOUTH": (420, 2030, 190, 460)}


def components(mask):
    """Bounding boxes of 4-connected components, via row runs + union-find."""
    H, W = mask.shape
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    runs_prev = []
    all_runs = []
    for r in range(H):
        row = mask[r]
        if not row.any():
            runs_prev = []
            continue
        d = np.diff(row.astype(np.int8))
        starts = list(np.flatnonzero(d == 1) + 1)
        ends = list(np.flatnonzero(d == -1) + 1)
        if row[0]:
            starts.insert(0, 0)
        if row[-1]:
            ends.append(W)
        runs = []
        for s, e in zip(starts, ends):
            idx = len(all_runs)
            all_runs.append((r, s, e))
            parent[idx] = idx
            runs.append((s, e, idx))
        i = j = 0
        while i < len(runs) and j < len(runs_prev):
            s1, e1, i1 = runs[i]
            s2, e2, i2 = runs_prev[j]
            if s1 < e2 and s2 < e1:
                union(i1, i2)
            if e1 < e2:
                i += 1
            else:
                j += 1
        runs_prev = runs

    boxes = {}
    members = {}
    for idx, (r, s, e) in enumerate(all_runs):
        k = find(idx)
        if k not in boxes:
            boxes[k] = [s, r, e, r + 1, 0]
            members[k] = []
        b = boxes[k]
        b[0] = min(b[0], s)
        b[1] = min(b[1], r)
        b[2] = max(b[2], e)
        b[3] = max(b[3], r + 1)
        b[4] += e - s
        members[k].append((r, s, e))
    out = []
    for k, b in boxes.items():
        out.append(b + [members[k]])
    return out


def detect(name, min_ft=0.8):
    x0, x1, y0, y1 = REGION[name]
    c0, r1 = render.px_of(x0, y0)
    c1, r0 = render.px_of(x1, y1)
    c0, r0, c1, r1 = int(c0), int(r0), int(c1), int(r1)
    im = Image.open("draw.png").crop((c0, r0, c1, r1))
    a = np.asarray(im)

    u, z = elev.mappings()[name]

    def U(col):
        return u((c0 + col) / render.SCALE)

    def Z(row):
        return z(render.PAGE_H - (r0 + row) / render.SCALE)

    out = []
    for fv in FILLS:
        mask = np.abs(a.astype(np.int16) - fv) <= TOL
        for bx0, by0, bx1, by1, npx, runs in components(mask):
            wft = (bx1 - bx0) / render.SCALE / elev.PT
            hft = (by1 - by0) / render.SCALE / elev.PT
            if wft < min_ft or hft < min_ft:
                continue
            ua, ub = sorted([U(bx0), U(bx1)])
            za, zb = sorted([Z(by0), Z(by1)])
            # border coverage tells a rectangular frame ring (all four
            # sides fully inked) apart from a diagonal shadow, which has a
            # big bounding box and low solidity but no complete border.
            top = sum(e - s for r, s, e in runs if r == by0)
            bot = sum(e - s for r, s, e in runs if r == by1 - 1)
            rows = {}
            for r, s, e in runs:
                rows.setdefault(r, []).append((s, e))
            nrow = by1 - by0
            left = sum(1 for r, sp in rows.items() if any(s <= bx0 for s, e in sp))
            right = sum(1 for r, sp in rows.items() if any(e >= bx1 for s, e in sp))
            wpx_ = max(1, bx1 - bx0)
            border = min(top / wpx_, bot / wpx_, left / nrow, right / nrow)
            out.append({"fill": fv, "u0": ua, "u1": ub, "z0": za, "z1": zb,
                        "w": ub - ua, "h": zb - za, "border": border,
                        "solidity": npx / max(1, (bx1 - bx0) * (by1 - by0))})
    out.sort(key=lambda d: (d["u0"], d["z0"]))
    return out


if __name__ == "__main__":
    import sys
    for name in (sys.argv[1:] or ["NORTH", "WEST", "EAST", "SOUTH"]):
        rs = detect(name)
        print("=" * 78)
        print("%s : %d filled regions >= 0.8 ft" % (name, len(rs)))
        print("  %-8s %-8s %-8s %-8s %-6s %-6s %-4s %s"
              % ("u0_ft", "u1_ft", "z0_ft", "z1_ft", "w", "h", "fill", "sol/bord"))
        for d in rs:
            print("  %8.3f %8.3f %8.3f %8.3f %6.2f %6.2f %4d  %.2f %.2f"
                  % (d["u0"], d["u1"], d["z0"], d["z1"], d["w"], d["h"],
                     d["fill"], d["solidity"], d["border"]))
        print()
