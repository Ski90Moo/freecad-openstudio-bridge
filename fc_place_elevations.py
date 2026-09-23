# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Put each elevation drawing on its own facade, at exact scale.

Run under FreeCAD's bundled Python, after elevations/facade_images.py has cut
the crops:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_place_elevations.py \
        FloorplanTest-05.FCStd elevations/facades/facade_images.json

An image plane placed by hand carries whatever scale and offset error the hand
had.  Measured against the vector plans, the one placed by hand for this
building was 0.25% out and 81 mm adrift -- invisible by eye, 0.2 m at the far
end of the facade, and it went straight into every opening traced over it.

Here the crop window was chosen in model feet and converted through the same
grid-line calibration the plans use, so the manifest says exactly which model
coordinates the image's own edges land on.  Placing it is then arithmetic: the
image goes in the facade's elevation frame -- the same frame its opening sketch
uses -- so an opening traced on the image is already in the right place.

The plane goes *exactly* on the wall, which is exactly the opening sketch's own
plane -- the same arrangement the floorplan rasters already use, where FirstFP
and the storey sketch both sit at z = 0.  Coplanar is what you want to trace
on: no parallax between the drawing and the line you are snapping to, however
you orbit.  FreeCAD draws edges with a polygon offset, so sketch lines read
cleanly on top of a coplanar image.

Faces are the exception -- two coplanar *faces* z-fight, so the wall surface
itself has to be hidden while tracing (trace_mode.FCMacro).  That costs
nothing: the wall outline is already in the sketch as external geometry.  Use
--standoff-mm to float the drawing clear of the wall instead, if you would
rather leave the geometry shown.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402

import fcbridge  # noqa: E402
from fc_seed_openings import (backup, elevation_placement, group_planes,
                              name_planes, wall_faces)  # noqa: E402

MM_PER_M = 1000.0
GROUP = "OS_Elevations"

# On the wall plane, so the drawing and its opening sketch are coplanar.
# Positive floats it outward, negative behind -- see the module docstring.
STANDOFF_MM = 0.0

PROPS = [
    ("OS_ElevationImage", "App::PropertyBool"),
    ("OS_Facade", "App::PropertyString"),
    ("OS_SourceSheet", "App::PropertyString"),
]


def point_on(group, u_mm, z_mm):
    """A point on the facade plane at building-axis coordinate u, height z."""
    n = group["normal"]
    along = FreeCAD.Vector(1, 0, 0) if abs(n.y) > 0.5 \
        else FreeCAD.Vector(0, 1, 0)
    return n * group["offset"] + along * u_mm + FreeCAD.Vector(0, 0, z_mm)


def get_group(doc):
    existing = doc.getObject(GROUP)
    if existing is not None and existing.TypeId == "App::DocumentObjectGroup":
        return existing
    group = doc.addObject("App::DocumentObjectGroup", GROUP)
    group.Label = "Elevation drawings"
    return group


def find_plane(doc, existing, facade):
    for obj in doc.Objects:
        if getattr(obj, "OS_ElevationImage", False) and \
                getattr(obj, "OS_Facade", "") == facade:
            return obj
    return existing.get(facade)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("manifest")
    ap.add_argument("--facade", action="append",
                    help="only this facade (repeatable); default is all")
    ap.add_argument("--standoff-mm", type=float, default=STANDOFF_MM,
                    help="offset from the wall plane, positive outward "
                         "(default %(default)s = coplanar with the wall and "
                         "its opening sketch); float it clear if you would "
                         "rather leave the wall faces shown")
    args = ap.parse_args()

    manifest_path = os.path.abspath(args.manifest)
    with open(manifest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    image_dir = os.path.dirname(manifest_path)

    path = os.path.abspath(args.fcstd)
    doc = fcbridge.open_document(path)
    pairs = wall_faces(doc)
    if not pairs:
        raise SystemExit(
            "no exterior wall faces in %s.\nRun  .\\bridge.ps1 import "
            "<surfaces.json> --into %s  first."
            % (os.path.basename(path), os.path.basename(path)))

    groups = {g["facade"]: g for g in name_planes(group_planes(pairs))}
    wanted = set(args.facade or [])

    print("backup: %s" % os.path.basename(backup(path)))
    if args.standoff_mm:
        print("standoff: %+.1f mm (%s the wall)"
              % (args.standoff_mm,
                 "outside" if args.standoff_mm > 0 else "behind"))
    else:
        print("standoff: none -- coplanar with the wall and its sketch")
    print()
    print("  %-8s %9s %9s %9s %9s  %s"
          % ("facade", "width m", "height m", "left m", "bottom m", "image"))

    container = get_group(doc)
    existing = {}
    made = refreshed = 0
    problems = []

    for entry in data["images"]:
        facade = entry["facade"]
        if wanted and facade not in wanted:
            continue
        group = groups.get(facade)
        if group is None:
            problems.append("%s: the document has no facade by that name (%s)"
                            % (facade, ", ".join(sorted(groups))))
            continue
        frame = elevation_placement(group)
        if frame is None:
            problems.append("%s: not a vertical facade" % facade)
            continue

        image = os.path.join(image_dir, entry["image"])
        if not os.path.exists(image):
            problems.append("%s: %s is missing" % (facade, entry["image"]))
            continue

        inverse = frame.inverse()
        corners = [
            inverse.multVec(point_on(group, entry["left_m"] * MM_PER_M,
                                     entry["bottom_m"] * MM_PER_M)),
            inverse.multVec(point_on(group, entry["right_m"] * MM_PER_M,
                                     entry["top_m"] * MM_PER_M)),
        ]
        if corners[0].x >= corners[1].x:
            problems.append(
                "%s: the crop reads right-to-left in the elevation frame "
                "(left edge at local x %.1f, right at %.1f).  The image would "
                "be mirrored; the manifest's left_m/right_m and the facade "
                "frame disagree." % (facade, corners[0].x, corners[1].x))
            continue

        width = corners[1].x - corners[0].x
        height = corners[1].y - corners[0].y
        mid = FreeCAD.Vector((corners[0].x + corners[1].x) / 2.0,
                             (corners[0].y + corners[1].y) / 2.0,
                             args.standoff_mm)

        obj = find_plane(doc, existing, facade)
        if obj is None:
            obj = doc.addObject("Image::ImagePlane",
                                "OS_Elev_Image_%s" % facade.replace(" ", "_"))
            obj.Label = "Elevation %s" % facade
            made += 1
        else:
            refreshed += 1
        for name, ptype in PROPS:
            if not hasattr(obj, name):
                obj.addProperty(ptype, name, "OpenStudio",
                                "FreeCAD to OpenStudio bridge")
        obj.OS_ElevationImage = True
        obj.OS_Facade = facade
        obj.OS_SourceSheet = "%s sheet %s" % (data.get("source_pdf", ""),
                                              data.get("sheet", ""))
        obj.ImageFile = image
        obj.XSize = width
        obj.YSize = height
        obj.Placement = FreeCAD.Placement(frame.multVec(mid), frame.Rotation)
        if obj not in container.Group:
            container.addObject(obj)

        print("  %-8s %9.3f %9.3f %9.3f %9.3f  %s"
              % (facade, width / MM_PER_M, height / MM_PER_M,
                 entry["left_m"], entry["bottom_m"], entry["image"]))

    placed = [o.Name for o in doc.Objects
              if getattr(o, "OS_ElevationImage", False)]
    doc.recompute()
    restored = fcbridge.save_document(doc)
    seeded = fcbridge.seed_view_defaults(path, placed)
    print("\nsaved %s%s" % (os.path.basename(path),
                            " (%d view entries restored)" % restored
                            if restored else ""))
    print("  %d image plane(s) created, %d refreshed" % (made, refreshed))
    if seeded:
        print("  %d given the default %s%% transparency"
              % (len(seeded),
                 dict((p[0], p[3]) for p in fcbridge.IMAGE_VIEW_DEFAULTS)
                 .get("Transparency", "?")))
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)
        sys.exit(1)
    print("\nThe images are embedded in the document, so they travel with it.\n"
          "Each one shares its facade's elevation frame, so an opening traced\n"
          "on the drawing lands on the wall without any further alignment.")


if __name__ == "__main__":
    main()
