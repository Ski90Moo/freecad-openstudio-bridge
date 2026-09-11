# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Compare an imported geometry FCStd against the surfaces JSON it came from.

Run under FreeCAD's bundled Python:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" verify_roundtrip.py \
        model_geometry.FCStd surfaces.json

Every surface must match vertex for vertex.  This is what proves the FreeCAD
document you are about to draw windows onto is the model, not an
approximation of it.

Subsurfaces are the one deliberate exception: the importer pulls them a couple
of millimetres out of their host wall so they can be seen and clicked, and
records how far on OS_DisplayOffset_mm.  That offset is subtracted here before
comparing, so the check still holds to the micron -- a display offset that
drifted from what the object claims would fail like any other error.

The OS_* labels are checked as well as the vertices, because they are output,
not input: the importer writes them from the model and nothing ever reads them
back.  Editing OS_SubSurfaceType in the property panel to retype an opening is
a natural thing to try and does nothing at all -- retyping happens on the
drawn outline, via a sketch Label or tag_opening.FCMacro.  Comparing them here
turns that silent no-op into a named failure.
"""

import argparse
import json
import sys

import FreeCAD

MM_PER_M = 1000.0

# (property, JSON key) pairs the importer writes verbatim.  Only checked when
# the key is in that record, so a surface is not asked for a subsurface's
# fields.  Deliberately the identity labels a person might try to edit -- not
# construction, which is legitimately blank all over this model.
LABELS = [
    ("OS_SubSurfaceType", "subsurface_type"),
    ("OS_HostSurface", "host_surface"),
    ("OS_SurfaceType", "surface_type"),
    ("OS_BoundaryCondition", "outside_boundary_condition"),
    ("OS_ShadingGroupType", "group_type"),
    ("OS_SpaceName", "space"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("surfaces_json")
    ap.add_argument("--tolerance-mm", type=float, default=0.001)
    args = ap.parse_args()

    doc = FreeCAD.openDocument(args.fcstd)
    with open(args.surfaces_json, encoding="utf-8") as fh:
        data = json.load(fh)

    expected = {r["name"]: r for r in data["surfaces"]}
    expected.update({r["name"]: r for r in data.get("subsurfaces", [])})
    expected.update({r["name"]: r for r in data.get("shading", [])})
    normals = {r["name"]: r.get("outward_normal")
               for r in data.get("surfaces", [])}

    worst_vertex, worst_area, checked = 0.0, 0.0, 0
    problems = []

    for obj in doc.Objects:
        name = getattr(obj, "OS_SurfaceName", None)
        if not name:
            continue
        record = expected.get(name)
        if record is None:
            problems.append("%s: not present in %s"
                            % (name, args.surfaces_json))
            continue

        for prop, key in LABELS:
            if key not in record:
                continue
            want = str(record.get(key) or "")
            got = str(getattr(obj, prop, "") or "")
            if got != want:
                problems.append(
                    "%s: %s reads %r but the model says %r -- these are "
                    "written from the model, not read back; re-run import"
                    % (name, prop, got, want))

        # Undo the display offset the importer applied, so what is compared is
        # where the model says the subsurface is.
        shift = FreeCAD.Vector(0, 0, 0)
        offset = getattr(obj, "OS_DisplayOffset_mm", 0.0)
        if offset:
            normal = normals.get(getattr(obj, "OS_HostSurface", None))
            if normal is None:
                problems.append(
                    "%s: offset %.3f mm but its host %r is not in %s"
                    % (name, offset, getattr(obj, "OS_HostSurface", ""),
                       args.surfaces_json))
                continue
            shift = FreeCAD.Vector(*normal) * offset

        got = sorted(
            tuple(round(c / MM_PER_M, 6)
                  for c in (v.Point.x - shift.x, v.Point.y - shift.y,
                            v.Point.z - shift.z))
            for v in obj.Shape.Vertexes)
        want = sorted(tuple(round(c, 6) for c in v)
                      for v in record["vertices"])

        if len(got) != len(want):
            problems.append("%s: %d vertices in FreeCAD vs %d in the model"
                            % (name, len(got), len(want)))
            continue

        for g, w in zip(got, want):
            worst_vertex = max(worst_vertex,
                               max(abs(a - b) for a, b in zip(g, w)))
        if obj.Shape.Faces:
            worst_area = max(
                worst_area,
                abs(obj.Shape.Area / (MM_PER_M ** 2) - record["gross_area_m2"]))
        checked += 1

    missing = set(expected) - {getattr(o, "OS_SurfaceName", None)
                               for o in doc.Objects}
    for name in sorted(missing):
        problems.append("%s: in the model but not in the FreeCAD document"
                        % name)

    print("surfaces checked       : %d / %d" % (checked, len(expected)))
    print("worst vertex deviation : %.6f mm" % (worst_vertex * MM_PER_M))
    print("worst area deviation   : %.9f m2" % worst_area)

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems[:20]:
            print("  %s" % p)

    if problems or worst_vertex * MM_PER_M > args.tolerance_mm:
        sys.exit("\nround trip FAILED")
    print("\nround trip OK")


if __name__ == "__main__":
    main()
