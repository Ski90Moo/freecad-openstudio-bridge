# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Export drawn shading surfaces -- canopies, awnings, fins -- from a FCStd.

Run under FreeCAD's bundled Python:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_export_shading.py \
        FloorplanTest-02.FCStd --out shading.json

**A canopy is traced in plan, not in elevation.**  That is the one place this
differs from openings, and it is not an arbitrary choice: a canopy is a
horizontal plate, so a plan trace over a roof-plan image gives its true
footprint directly, at the sketch's own z.  Draw the sketch at the height of
the canopy's underside and the shape needs no projection at all.  A vertical
fin works the same way from an elevation sketch -- any planar sketch is read,
whatever its orientation, because a sketch is planar and a shading surface is
planar.

A sketch is a shading sketch when its **Label starts with a keyword**, or when
it carries an `OS_ShadingSketch` boolean:

    CANOPY ...   -> Canopy      OVERHANG ... -> Overhang
    AWNING ...   -> Awning      FIN ...      -> Fin
    SHADING ...  -> Shading     SHADE ...    -> Shading

Every closed wire in such a sketch is one shading surface.  An **open** wire is
a datum line, not a shade, so it is listed and skipped rather than guessed at
-- the tidy way to keep one is to toggle it to construction geometry (G, N),
which takes it out of the sketch's Shape entirely.  See README section 6c.

Unlike an opening, a shading surface has no host: it is not coplanar with
anything and nothing contains it.  So the facade in its name is descriptive
only, taken from the nearest exterior wall, and a shade that is near no wall
is still exported -- just without one.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import Part  # noqa: E402

import fcbridge  # noqa: E402

MM_PER_M = 1000.0

KINDS = [
    ("OVERHANG", "Overhang"),
    ("CANOPY", "Canopy"),
    ("AWNING", "Awning"),
    ("SHADING", "Shading"),
    ("SHADE", "Shading"),
    ("FIN", "Fin"),
]

# Space-attached shading (Shading:Zone:Detailed) is deliberately not here:
# it needs a host space, and nothing in this workflow has asked for one yet.
GROUP_TYPES = ("Building", "Site")

# Identity for a drawn shade, minted into the sketch the same way an opening's
# is.  Without it apply_shading had to delete and rebuild every canopy on every
# run, minting new handles -- and a photovoltaic generator attached to a canopy
# references it by handle.
SHADING_ID_EXT = "OS_ShadingId"

# How far a shade may sit from a wall and still be named after that facade.
# A canopy touches its wall, so this only has to absorb a drawing that stops
# short of it; past this the name would be a guess.
FACADE_TOL_MM = 1000.0

# Below this a wire is a scribble, not a shade worth putting in the model.
MIN_AREA_M2 = 0.01

# A normal within this of horizontal is treated as vertical for the purpose of
# choosing which way it should face.
VERTICAL_TOL = 1e-3


def classify_label(label):
    """('Canopy', 'South entry') from 'CANOPY South entry', else (None, '')."""
    upper = (label or "").upper()
    for keyword, kind in KINDS:
        if upper.startswith(keyword):
            return kind, (label[len(keyword):] or "").strip(" -_:")
    return None, ""


def dumps_compact_vertices(payload):
    text = json.dumps(payload, indent=2)
    return re.sub(
        r"\[\s*\n\s*(-?\d+\.?\d*),\s*\n\s*(-?\d+\.?\d*),\s*\n\s*"
        r"(-?\d+\.?\d*)\s*\n\s*\]",
        r"[\1, \2, \3]",
        text,
    )


def exterior_walls(doc):
    """Imported exterior wall faces, for naming a shade after its facade."""
    walls = []
    for obj in doc.Objects:
        if getattr(obj, "OS_SurfaceType", "") != "Wall":
            continue
        if getattr(obj, "OS_BoundaryCondition", "") != "Outdoors":
            continue
        for face in getattr(obj.Shape, "Faces", []):
            walls.append((obj, face))
    return walls


def is_reference(obj):
    """True for geometry imported from the model, which is never input."""
    return bool(getattr(obj, "OS_SurfaceName", None)
                or getattr(obj, "OS_SubSurfaceType", None)
                or getattr(obj, "OS_ShadingSurface", False))


def is_drawable(obj):
    """True for an object that can hold a drawn outline of its own.

    A group is explicitly not one, even though FreeCAD gives it a `Shape`: it
    is a Compound of whatever is inside it.  "Shading from the model" -- the
    group `import` puts the model's own canopies in -- therefore matched the
    SHADING keyword and handed every canopy back a second time.  Nothing in
    the group carries a drawn outline that its children do not.
    """
    return obj.TypeId == "Sketcher::SketchObject" or \
        obj.isDerivedFrom("Part::Feature")


def shading_sketches(doc):
    for obj in doc.Objects:
        if obj.TypeId != "Sketcher::SketchObject":
            continue
        if getattr(obj, "OS_ShadingSketch", False) or classify_label(obj.Label)[0]:
            yield obj


def outlines(doc):
    """(source, kind, points) per closed wire, plus notes on what was skipped.

    A shading sketch contributes one shade per closed wire, the same way an
    opening sketch contributes one opening per closed wire.
    """
    found, skipped, problems = [], [], []
    for obj in doc.Objects:
        if is_reference(obj) or not is_drawable(obj):
            continue
        label_kind, _ = classify_label(obj.Label)
        is_sketch = obj.TypeId == "Sketcher::SketchObject"
        tagged = bool(getattr(obj, "OS_ShadingSketch", False))
        if not (label_kind or tagged):
            continue
        if tagged and not label_kind:
            label_kind = "Shading"
        kind = getattr(obj, "OS_ShadingKind", "") or label_kind

        shape = getattr(obj, "Shape", None)
        wires = getattr(shape, "Wires", []) if shape is not None else []
        if not wires and not is_sketch:
            continue
        ids = fcbridge.element_values(obj, SHADING_ID_EXT) if is_sketch else {}
        for i, wire in enumerate(wires, start=1):
            points = [v.Point for v in wire.OrderedVertexes]
            if not wire.isClosed():
                skipped.append((obj.Label, i, wire.Length / MM_PER_M,
                                len(points)))
                continue
            if len(points) < 3:
                problems.append("%s wire %d: only %d vertices"
                                % (obj.Label, i, len(points)))
                continue
            marks = fcbridge.wire_values(wire, ids) if ids else []
            found.append((obj, kind, marks[0] if len(marks) == 1 else "",
                          points))
    return found, skipped, problems


def polygon_normal(points):
    """Newell normal of a closed loop, unit length, or None if degenerate."""
    normal = FreeCAD.Vector(0, 0, 0)
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        normal.x += (point.y - nxt.y) * (point.z + nxt.z)
        normal.y += (point.z - nxt.z) * (point.x + nxt.x)
        normal.z += (point.x - nxt.x) * (point.y + nxt.y)
    if normal.Length < 1e-9:
        return None
    normal.normalize()
    return normal


def polygon_area_m2(points, normal):
    """Area of a planar loop, from the Newell vector before normalising."""
    total = FreeCAD.Vector(0, 0, 0)
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        total += point.cross(nxt)
    return abs(total.dot(normal)) / 2.0 / (MM_PER_M ** 2)


def nearest_wall(wire, walls):
    """(obj, face, distance_mm) of the closest exterior wall, or (None,)*2+inf.

    Distance is measured shape to shape, so a canopy whose back edge lies in
    the wall plane comes back at zero -- which is what an abutting canopy
    should read as, and what distinguishes it from a freestanding site shade.
    """
    best, best_dist = (None, None), float("inf")
    for obj, face in walls:
        try:
            dist = wire.distToShape(face)[0]
        except Exception:            # pragma: no cover - OCC edge cases
            continue
        if dist < best_dist:
            best, best_dist = (obj, face), dist
    return best[0], best[1], best_dist


def face_outward_normal(face):
    normal = FreeCAD.Vector(face.Surface.Axis)
    if face.Orientation == "Reversed":
        normal = normal.negative()
    return normal


def orient(points, normal, wall_normal):
    """Wind the loop so the shade faces the way a person would expect.

    A horizontal canopy faces up: that is the side the sun reaches, and it is
    what the OpenStudio App draws as the top of the plate.  A vertical fin has
    no up, so it faces the same way as the wall it stands off -- away from the
    building.  Neither changes what the surface shades; both stop the model
    from displaying a canopy as if seen from underneath.
    """
    if abs(normal.z) > VERTICAL_TOL:
        flip = normal.z < 0
    elif wall_normal is not None:
        flip = normal.dot(wall_normal) < 0
    else:
        flip = False
    if flip:
        return list(reversed(points)), normal.negative()
    return points, normal


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--group-type", choices=GROUP_TYPES, default="Building",
                    help="which frame the shades belong to (default "
                         "%(default)s).  Building rotates with the building's "
                         "north axis, Site does not.")
    ap.add_argument("--facade-tol-m", type=float,
                    default=FACADE_TOL_MM / MM_PER_M,
                    help="how close a shade must be to an exterior wall to be "
                         "named after that facade (default %(default)s)")
    ap.add_argument("--allow-problems", action="store_true")
    ap.add_argument("--no-mint-ids", action="store_true",
                    help="do not give un-identified shades an id, and do not "
                         "write to the document.  Their shading surfaces get "
                         "rebuilt rather than updated, so their OpenStudio "
                         "handles change on every apply.")
    args = ap.parse_args()

    doc = FreeCAD.openDocument(os.path.abspath(args.fcstd))

    minted, id_problems = (0, []) if args.no_mint_ids else         fcbridge.ensure_ids(shading_sketches(doc), SHADING_ID_EXT)
    if minted:
        doc.recompute()
        fcbridge.save_document(doc)
        print("minted an id for %d shade(s) and wrote them into %s"
              % (minted, os.path.basename(args.fcstd)))
        print("  Revert in FreeCAD before editing it further.")

    walls = exterior_walls(doc)
    north = fcbridge.north_axis_deg(doc)
    print("exterior wall faces available for naming: %d" % len(walls))

    drawn, skipped, problems = outlines(doc)
    problems = id_problems + problems
    print("closed outlines drawn: %d" % len(drawn))

    tol_mm = args.facade_tol_m * MM_PER_M
    shading, rows = [], []
    counter = {}

    for obj, kind, shading_id, points in drawn:
        normal = polygon_normal(points)
        if normal is None:
            problems.append("%s: outline is degenerate, no plane" % obj.Label)
            continue
        area = polygon_area_m2(points, normal)
        if area < MIN_AREA_M2:
            problems.append("%s: %.4f m2 is too small to be a shade"
                            % (obj.Label, area))
            continue

        wire = Part.makePolygon(list(points) + [points[0]])
        wall_obj, wall_face, dist = nearest_wall(wire, walls) if walls \
            else (None, None, float("inf"))
        wall_normal = face_outward_normal(wall_face) if wall_face else None
        facade = fcbridge.compass_of(wall_normal) \
            if wall_normal is not None and dist <= tol_mm else ""

        points, normal = orient(points, normal, wall_normal)
        group_type = getattr(obj, "OS_ShadingGroupType", "") or args.group_type
        if group_type not in GROUP_TYPES:
            problems.append("%s: OS_ShadingGroupType %r is not one of %s"
                            % (obj.Label, group_type, ", ".join(GROUP_TYPES)))
            continue

        tag = ("%s %s" % (kind, facade)).strip()
        counter[tag] = counter.get(tag, 0) + 1
        name = "%s %02d" % (tag, counter[tag])

        tilt = math.degrees(math.acos(max(-1.0, min(1.0, normal.z))))
        azimuth = fcbridge.plan_azimuth(normal) if tilt > 1.0 else 0.0
        zs = [p.z for p in points]
        rows.append((name, area, min(zs) / MM_PER_M, tilt,
                     facade or "-",
                     "-" if dist == float("inf") else dist / MM_PER_M,
                     getattr(wall_obj, "OS_SurfaceName", "") if wall_obj
                     else "", obj.Label))

        shading.append({
            "name": name,
            "id": shading_id,
            "kind": kind,
            "facade": facade,
            "group_type": group_type,
            "source_object": obj.Name,
            "source_label": obj.Label,
            "area_m2": round(area, 4),
            "tilt_deg": round(tilt, 3),
            "azimuth_deg": round(azimuth, 3),
            "nearest_wall": getattr(wall_obj, "OS_SurfaceName", "")
                            if wall_obj else "",
            "nearest_wall_m": (round(dist / MM_PER_M, 4)
                               if dist != float("inf") else None),
            "vertices": [[round(p.x / MM_PER_M, 6),
                          round(p.y / MM_PER_M, 6),
                          round(p.z / MM_PER_M, 6)] for p in points],
        })

    if rows:
        print("\n%-22s %8s %8s %6s  %-9s %8s  %-11s %s"
              % ("name", "area m2", "z m", "tilt", "facade", "gap m",
                 "nearest wall", "drawn on"))
        for row in rows:
            gap = row[5] if isinstance(row[5], str) else "%8.3f" % row[5]
            print("%-22s %8.3f %8.3f %6.1f  %-9s %8s  %-11s %s"
                  % (row[0], row[1], row[2], row[3], row[4], gap,
                     row[6] or "-", row[7]))

    if skipped:
        print("\nopen wires ignored (%d) -- a shade has to be a closed "
              "outline, so\nthese read as datum lines.  Toggle them to "
              "construction geometry (G, N)\nand they stop being reported:"
              % len(skipped))
        for label, index, length, count in skipped:
            print("  %-22s wire %d: %d vertices, %.3f m long"
                  % (label, index, count, length))

    by_kind = {}
    for s in shading:
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
    print("\nshading surfaces found: %d%s"
          % (len(shading),
             (" (" + ", ".join("%s %d" % kv
                               for kv in sorted(by_kind.items())) + ")")
             if by_kind else ""))
    if shading:
        print("  total area %.3f m2, group type %s"
              % (sum(s["area_m2"] for s in shading),
                 ", ".join(sorted({s["group_type"] for s in shading}))))
        print("  facade names are plan north, as on the drawings; the "
              "building's north\n  axis is %.1f deg, so true bearings differ "
              "by that much." % north)

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)
        if not args.allow_problems:
            sys.exit("\nRefusing to write %s.\nFix the listed outlines, or "
                     "re-run with --allow-problems." % args.out)

    out = os.path.abspath(args.out)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dumps_compact_vertices({
            "schema_version": 1,
            "source_document": os.path.basename(args.fcstd),
            "generated_utc": datetime.now(timezone.utc)
                                     .replace(microsecond=0).isoformat(),
            "units": "meters",
            "north_axis_deg": north,
            "shading": shading,
        }))
        fh.write("\n")
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main()
