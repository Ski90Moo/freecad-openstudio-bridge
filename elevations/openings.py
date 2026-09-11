# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Consolidate the detected elevation fills into openings.

Two sources, each used for what it is good at:

  * the PLANS are vector, so an opening's width and position along the wall
    are exact. They are read as gaps in the exterior wall line.
  * the ELEVATIONS are raster (1-bit line art at 75 px/ft), and are the only
    source for sill and head heights.

Where both see the same opening they agree to about half an inch, which is
what licenses snapping the horizontal extent to the plan. Two kinds of opening
have no plan gap and come from the elevation alone: the north clerestory
strips, which sit above the plan's cut plane, and the overhead doors, which
the plan draws closed so the wall line runs through them.

Filtering rules, all read off the drawing:

  * keep a component if it is at least half solid (glass or a door panel) or
    its bounding-box border is at least 80% inked (a frame ring). That drops
    the diagonal canopy shadows, which span a whole storefront at 1-6%
    solidity with no complete border;
  * the canopies are one repeated CAD component -- every one sits at
    z 7.17-8.77 ft. They are projections, not openings, and apply_openings.py
    makes subsurfaces only, so they are removed by that signature and counted;
  * merge components that overlap, or that share 60% of the narrower one's
    width and are within 0.45 ft vertically. That stacks an overhead door's
    panel bands and its vision lights into one door.
"""
import detect
import plangaps

MF = 0.3048
L2_FLOOR_FT = 3.38 / MF          # storey 2 floor elevation, 11.089 ft

# Model plane each elevation looks at, and the axis running along it.
# Every one confirmed against wall gaps in the vector plan (vgaps.py).
FACADE = {
    "SOUTH": {"axis": "x", "at": "y", "value": 0.0, "outward": (0, -1)},
    "NORTH": {"axis": "x", "at": "y", "value": 59.994, "outward": (0, 1)},
    "WEST": {"axis": "y", "at": "x", "value": 0.0, "outward": (-1, 0)},
    "EAST": {"axis": "y", "at": "x", "value": 165.677, "outward": (1, 0)},
}
ENV = {"x": (0.0, 165.677), "y": (0.0, 59.994)}

CANOPY_Z = (7.00, 7.35)
CANOPY_H = (1.20, 1.90)


def keep(r):
    return r["solidity"] >= 0.5 or r["border"] >= 0.8


def is_canopy(r):
    return (CANOPY_Z[0] <= r["z0"] <= CANOPY_Z[1]
            and CANOPY_H[0] <= r["h"] <= CANOPY_H[1])


def merges(a, b, zgap=0.45, frac=0.60):
    if a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]:
        return True
    ov = min(a[1], b[1]) - max(a[0], b[0])
    narrow = min(a[1] - a[0], b[1] - b[0])
    if narrow <= 0 or ov < frac * narrow:
        return False
    return max(a[2], b[2]) - min(a[3], b[3]) <= zgap


def cluster(regions):
    # index 4 keeps the extent of the fill-192 members only. The darker 134
    # tone is the jamb reveal of a recessed opening -- wall depth seen in
    # elevation, not opening -- so where both are present the lighter fill
    # gives the true width. All four overhead doors have a 0.75 ft reveal on
    # one side; without this only the one whose reveal happened to pass the
    # solidity filter came out wider than its three identical siblings.
    boxes = []
    for r in regions:
        lit = [r["u0"], r["u1"]] if r["fill"] == 192 else None
        boxes.append([r["u0"], r["u1"], r["z0"], r["z1"], lit, 1])
    changed = True
    while changed:
        changed = False
        out = []
        for b in boxes:
            for o in out:
                if merges(b, o):
                    o[0], o[1] = min(o[0], b[0]), max(o[1], b[1])
                    o[2], o[3] = min(o[2], b[2]), max(o[3], b[3])
                    if b[4]:
                        o[4] = ([min(o[4][0], b[4][0]), max(o[4][1], b[4][1])]
                                if o[4] else list(b[4]))
                    o[5] += b[5]
                    changed = True
                    break
            else:
                out.append(b)
        boxes = out
    for b in boxes:
        if b[4]:
            b[0], b[1] = b[4]
    return sorted(boxes)


def snap(facade, boxes):
    """Replace the horizontal extent with the plan's, where the plan sees it."""
    allg = []
    for storey in (1, 2):
        for a, b, w in plangaps.gaps(facade, storey):
            allg.append((a, b, storey))
    bygap, loose = {}, []
    for bx in boxes:
        storey = 2 if bx[2] >= L2_FLOOR_FT - 1.0 else 1
        hit = None
        for a, b, s in allg:
            if s != storey:
                continue
            ov = min(bx[1], b) - max(bx[0], a)
            if ov > 0.5 * min(bx[1] - bx[0], b - a):
                hit = (a, b, s)
                break
        if hit is None:
            loose.append(bx)
        else:
            g = bygap.setdefault(hit, [1e9, -1e9, 0])
            g[0] = min(g[0], bx[2])
            g[1] = max(g[1], bx[3])
            g[2] += 1
    out = []
    for (a, b, s), (z0, z1, n) in bygap.items():
        out.append({"u0": a, "u1": b, "z0": z0, "z1": z1, "w": b - a,
                    "h": z1 - z0, "source": "plan+elevation", "storey": s})
    for bx in loose:
        out.append({"u0": bx[0], "u1": bx[1], "z0": bx[2], "z1": bx[3],
                    "w": bx[1] - bx[0], "h": bx[3] - bx[2],
                    "source": "elevation", "storey":
                        2 if bx[2] >= L2_FLOOR_FT - 1.0 else 1})
    return sorted(out, key=lambda d: (d["u0"], d["z0"]))


def classify(c):
    w, h, z0 = c["w"], c["h"], c["z0"]
    if w < 1.6 or h < 1.0:
        return None, "too small (%.2f x %.2f ft)" % (w, h)
    if z0 < 0.6:
        if h >= 10.0:
            return "OverheadDoor", ""
        if w >= 5.6:
            return "GlassDoor", ""
        if 2.4 <= w <= 5.6 and 5.8 <= h <= 8.2:
            return "Door", ""
        return None, "on the floor but %.2f x %.2f ft" % (w, h)
    if z0 >= 1.5:
        return "FixedWindow", ""
    return None, "sill at %.2f ft (%.2f x %.2f)" % (z0, w, h)


def run(report=True):
    result = {}
    for name in ("SOUTH", "NORTH", "WEST", "EAST"):
        lo, hi = ENV[FACADE[name]["axis"]]
        raw = detect.detect(name)
        canopies = [r for r in raw if keep(r) and is_canopy(r)]
        usable = [r for r in raw if keep(r) and not is_canopy(r)]
        kept, dropped = [], []
        for c in snap(name, cluster(usable)):
            if c["u1"] < lo - 0.5 or c["u0"] > hi + 0.5:
                dropped.append((c, "outside the envelope"))
                continue
            if c["z0"] > 24.5:
                dropped.append((c, "above the parapet"))
                continue
            kind, why = classify(c)
            if kind is None:
                dropped.append((c, why))
            else:
                c["type"] = kind
                kept.append(c)
        result[name] = {"kept": kept, "dropped": dropped,
                        "canopies": len(canopies)}
        if report:
            print("=" * 80)
            print("%s facade  (%s = %.3f ft, runs along %s) -- %d openings"
                  % (name, FACADE[name]["at"], FACADE[name]["value"],
                     FACADE[name]["axis"], len(kept)))
            print("  %-13s %9s %9s %8s %8s %6s %6s  L  %s"
                  % ("type", "u0_ft", "u1_ft", "sill", "head", "w", "h",
                     "source"))
            for c in kept:
                print("  %-13s %9.3f %9.3f %8.3f %8.3f %6.2f %6.2f  %d  %s"
                      % (c["type"], c["u0"], c["u1"], c["z0"], c["z1"],
                         c["w"], c["h"], c["storey"], c["source"]))
            if canopies:
                print("  -- %d canopy segment(s) at z 7.17-8.77 ft, "
                      "not modelled (subsurfaces only)" % len(canopies))
            for c, why in dropped:
                print("  -- skipped %8.2f..%-8.2f z %6.2f..%-6.2f  %s"
                      % (c["u0"], c["u1"], c["z0"], c["z1"], why))
            print()
    tot = sum(len(v["kept"]) for v in result.values())
    if report:
        print("TOTAL: %d openings" % tot)
    return result


if __name__ == "__main__":
    run()
