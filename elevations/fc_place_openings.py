# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Draw the derived opening outlines into the geometry FCStd.

Run under FreeCAD's bundled Python. Creates one planar face per opening,
labelled with the keyword fc_export_openings.py looks for, so the normal
`bridge.ps1 openings` -> `bridge.ps1 apply` path takes over from here and
does its own geometric host-matching.

Outlines carry OS_FromElevations so a re-run replaces its own work and leaves
anything drawn by hand alone.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import FreeCAD  # noqa: E402
import Part  # noqa: E402
import fcbridge  # noqa: E402

MM = 1000.0
GROUP = "Openings_from_elevations"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fcstd")
    ap.add_argument("openings")
    args = ap.parse_args()

    with open(args.openings, encoding="utf-8") as fh:
        data = json.load(fh)

    doc = fcbridge.open_document(os.path.abspath(args.fcstd))

    removed = 0
    for obj in list(doc.Objects):
        if getattr(obj, "OS_FromElevations", False):
            doc.removeObject(obj.Name)
            removed += 1
    if removed:
        print("removed %d outline(s) from a previous run" % removed)

    grp = None
    for obj in doc.Objects:
        if obj.Name == GROUP or obj.Label == GROUP:
            grp = obj
            break
    if grp is None:
        grp = doc.addObject("App::DocumentObjectGroup", GROUP)
        grp.Label = GROUP

    made = 0
    for spec in data["openings"]:
        pts = [FreeCAD.Vector(x * MM, y * MM, z * MM)
               for x, y, z in spec["vertices"]]
        wire = Part.makePolygon(pts + [pts[0]])
        obj = doc.addObject("Part::Feature", "Opening")
        obj.Shape = Part.Face(wire)
        obj.Label = spec["label"]
        obj.addProperty("App::PropertyBool", "OS_FromElevations", "OpenStudio",
                        "drawn from the elevation sheet, not by hand")
        obj.OS_FromElevations = True
        for prop, key in (("OS_Facade", "facade"), ("OS_Source", "source"),
                          ("OS_ExpectedHost", "expected_host")):
            obj.addProperty("App::PropertyString", prop, "OpenStudio", "")
            setattr(obj, prop, str(spec.get(key) or ""))
        grp.addObject(obj)
        made += 1

    doc.recompute()
    fcbridge.save_document(doc)
    print("placed %d opening outline(s) into %s"
          % (made, os.path.basename(args.fcstd)))
    by = {}
    for spec in data["openings"]:
        by[spec["subsurface_type"]] = by.get(spec["subsurface_type"], 0) + 1
    for k in sorted(by):
        print("   %-14s %d" % (k, by[k]))


if __name__ == "__main__":
    main()
