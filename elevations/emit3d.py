# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Turn the consolidated openings into building-coordinate rectangles and
check each one lands inside exactly one exterior wall of the model.

Writes openings_3d.json for fc_place_openings.py to draw into the geometry
document. The host is found here only as a pre-flight; the authoritative
host-matching is fc_export_openings.py's, which does it geometrically against
the real faces.
"""
import json
import os

import openings as O

MF = 0.3048
# A surfaces.json from dump_osm_geometry.py; see samples/ for one.
SURF = os.environ.get("BRIDGE_SURFACES", "../samples/surfaces.json")
OUT = "openings_3d.json"

# model wall planes, metres
PLANE = {"SOUTH": ("y", 0.0), "NORTH": ("y", 18.286),
         "WEST": ("x", 0.0), "EAST": ("x", 50.498)}

LABEL = {"FixedWindow": "WINDOW", "OperableWindow": "OPWINDOW",
         "Door": "DOOR", "GlassDoor": "GLASSDOOR",
         "OverheadDoor": "OVERHEAD", "Skylight": "SKYLIGHT"}


def rect(facade, u0, u1, z0, z1):
    """Rectangle in building coordinates (metres), CCW seen from outside."""
    axis, at = PLANE[facade]
    a0, a1 = u0 * MF, u1 * MF
    b0, b1 = z0 * MF, z1 * MF
    if axis == "y":
        return [[a0, at, b0], [a1, at, b0], [a1, at, b1], [a0, at, b1]]
    return [[at, a0, b0], [at, a1, b0], [at, a1, b1], [at, a0, b1]]


def load_walls():
    d = json.load(open(SURF, encoding="utf-8"))
    out = []
    for s in d["surfaces"]:
        if s["surface_type"] != "Wall":
            continue
        if s["outside_boundary_condition"] != "Outdoors":
            continue
        xs = [v[0] for v in s["vertices"]]
        ys = [v[1] for v in s["vertices"]]
        zs = [v[2] for v in s["vertices"]]
        out.append({"name": s["name"], "space": s["space"],
                    "x": (min(xs), max(xs)), "y": (min(ys), max(ys)),
                    "z": (min(zs), max(zs))})
    return out


def host_for(facade, verts, walls, tol=0.02):
    axis, at = PLANE[facade]
    other = "x" if axis == "y" else "y"
    oi = 0 if other == "x" else 1
    ai = 0 if axis == "x" else 1
    lo = min(v[oi] for v in verts)
    hi = max(v[oi] for v in verts)
    zlo = min(v[2] for v in verts)
    zhi = max(v[2] for v in verts)
    hits = []
    for w in walls:
        if abs(w[axis][0] - at) > tol or abs(w[axis][1] - at) > tol:
            continue
        if w[other][0] - tol <= lo and hi <= w[other][1] + tol \
                and w["z"][0] - tol <= zlo and zhi <= w["z"][1] + tol:
            hits.append(w)
    return hits


def main():
    res = O.run(report=False)
    walls = load_walls()
    out = []
    problems = []
    print("%-6s %-13s %-9s %-9s %-8s %-8s  %s"
          % ("facade", "type", "u0_m", "u1_m", "z0_m", "z1_m", "host wall"))
    n = 0
    for facade in ("SOUTH", "NORTH", "WEST", "EAST"):
        for c in res[facade]["kept"]:
            n += 1
            v = rect(facade, c["u0"], c["u1"], c["z0"], c["z1"])
            hits = host_for(facade, v, walls)
            if len(hits) == 1:
                where = "%s  %s" % (hits[0]["name"], hits[0]["space"])
            elif not hits:
                where = "** NO WALL CONTAINS IT **"
                problems.append((facade, c, "no containing wall"))
            else:
                where = "** %d walls: %s **" % (
                    len(hits), ", ".join(h["name"] for h in hits))
                problems.append((facade, c, "ambiguous"))
            print("%-6s %-13s %9.3f %9.3f %8.3f %8.3f  %s"
                  % (facade, c["type"], min(x[0] if facade in ("SOUTH", "NORTH")
                                            else x[1] for x in v),
                     max(x[0] if facade in ("SOUTH", "NORTH") else x[1]
                         for x in v),
                     min(x[2] for x in v), max(x[2] for x in v), where))
            out.append({
                "label": "%s %s %02d" % (LABEL[c["type"]], facade.title(), n),
                "subsurface_type": c["type"],
                "facade": facade,
                "storey": c["storey"],
                "source": c["source"],
                "expected_host": hits[0]["name"] if len(hits) == 1 else None,
                "expected_space": hits[0]["space"] if len(hits) == 1 else None,
                "vertices": [[round(a, 6) for a in p] for p in v],
            })
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"units": "meters", "count": len(out), "openings": out},
                  fh, indent=1)
    print()
    print("%d openings written to %s" % (len(out), OUT))
    if problems:
        print("\n%d PROBLEM(S):" % len(problems))
        for f, c, why in problems:
            print("   %s %s %.2f-%.2f ft z %.2f-%.2f : %s"
                  % (f, c["type"], c["u0"], c["u1"], c["z0"], c["z1"], why))
    else:
        print("every opening lies inside exactly one exterior wall surface")


if __name__ == "__main__":
    main()
