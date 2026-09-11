# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Dump every surface's vertex loop out of an .osm.

Run under the bridge venv:

    osvenv/Scripts/python.exe dump_osm_geometry.py model.osm --out surfaces.json

This is the read-back openstudio-mcp does not provide: get_surface_details
advertises vertices but only ever reports len(surface.vertices()), so there is
no way to get a coordinate out through the MCP today.

Vertices are converted from space-local to building coordinates via the
space's own transformation, so they can be drawn directly.
"""

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone

import openstudio


def dumps_compact_vertices(payload):
    text = json.dumps(payload, indent=2)
    return re.sub(
        r"\[\s*\n\s*(-?\d+\.?\d*),\s*\n\s*(-?\d+\.?\d*),\s*\n\s*"
        r"(-?\d+\.?\d*)\s*\n\s*\]",
        r"[\1, \2, \3]",
        text,
    )


def opt(value, attr="nameString"):
    """Unwrap an OpenStudio boost-optional into a name or None."""
    if value is None or not value.is_initialized():
        return None
    return getattr(value.get(), attr)()


def is_air_boundary(surface):
    """True when the surface's construction is an OS:Construction:AirBoundary.

    Keyed on the object type, not on the construction name, so a construction
    renamed by hand still reports correctly.
    """
    construction = surface.construction()
    if construction is None or not construction.is_initialized():
        return False
    return construction.get().to_ConstructionAirBoundary().is_initialized()


def opening_id(subsurface):
    """The FreeCAD outline id apply_* recorded in the comment.

    Carrying it back out is what lets the FreeCAD document show which drawn
    outline a subsurface came from without matching coordinates, and what
    proves a handle survived a retype rather than being rebuilt.
    """
    text = subsurface.comment() or ""
    at = text.find("id:")
    return text[at + 3:].strip() or None if at >= 0 else None


def global_vertices(surface, transformation):
    pts = transformation * surface.vertices()
    return [[round(p.x(), 6), round(p.y(), 6), round(p.z(), 6)] for p in pts]


def dump(model):
    surfaces, subsurfaces = [], []

    for space in model.getSpaces():
        transformation = space.transformation()
        story = opt(space.buildingStory())
        zone = opt(space.thermalZone())

        for surface in space.surfaces():
            normal = surface.outwardNormal()
            surfaces.append({
                "name": surface.nameString(),
                "handle": str(surface.handle()),
                "space": space.nameString(),
                "story": story,
                "thermal_zone": zone,
                "surface_type": surface.surfaceType(),
                "outside_boundary_condition":
                    surface.outsideBoundaryCondition(),
                "sun_exposure": surface.sunExposure(),
                "wind_exposure": surface.windExposure(),
                "construction": opt(surface.construction()),
                "is_air_boundary": is_air_boundary(surface),
                "adjacent_surface": opt(surface.adjacentSurface()),
                "gross_area_m2": round(surface.grossArea(), 6),
                "net_area_m2": round(surface.netArea(), 6),
                "azimuth_deg": round(surface.azimuth() * 180.0 / 3.141592653589793, 3),
                "tilt_deg": round(surface.tilt() * 180.0 / 3.141592653589793, 3),
                "outward_normal": [round(normal.x(), 6), round(normal.y(), 6),
                                   round(normal.z(), 6)],
                "vertices": global_vertices(surface, transformation),
            })

            for sub in surface.subSurfaces():
                subsurfaces.append({
                    "name": sub.nameString(),
                    "handle": str(sub.handle()),
                    "opening_id": opening_id(sub),
                    "host_surface": surface.nameString(),
                    "space": space.nameString(),
                    "subsurface_type": sub.subSurfaceType(),
                    "construction": opt(sub.construction()),
                    "gross_area_m2": round(sub.grossArea(), 6),
                    "vertices": global_vertices(sub, transformation),
                })

    return surfaces, subsurfaces


def dump_shading(model):
    """Canopies, fins and site shades, in building coordinates.

    buildingTransformation() is the one to use rather than the group's own:
    it folds in the space for a space-attached group and undoes the north axis
    for a site group, so every shade lands in the same frame as the surfaces
    above and can be drawn straight into the FreeCAD document.
    """
    shading = []
    for group in model.getShadingSurfaceGroups():
        transformation = group.buildingTransformation()
        space = group.space()
        for shade in group.shadingSurfaces():
            pts = transformation * shade.vertices()
            normal = shade.outwardNormal()
            shading.append({
                "name": shade.nameString(),
                "handle": str(shade.handle()),
                "shading_id": opening_id(shade),
                "shading_group": group.nameString(),
                "group_type": group.shadingSurfaceType(),
                "space": space.get().nameString()
                         if space.is_initialized() else None,
                "construction": opt(shade.construction()),
                "gross_area_m2": round(shade.grossArea(), 6),
                "tilt_deg": round(shade.tilt() * 180.0 / math.pi, 3),
                "azimuth_deg": round(shade.azimuth() * 180.0 / math.pi, 3),
                "outward_normal": [round(normal.x(), 6), round(normal.y(), 6),
                                   round(normal.z(), 6)],
                "vertices": [[round(p.x(), 6), round(p.y(), 6),
                              round(p.z(), 6)] for p in pts],
            })
    return shading


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("osm")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    path = os.path.abspath(args.osm)
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.path(path))
    if not loaded.is_initialized():
        raise SystemExit("could not load %s" % path)
    model = loaded.get()

    surfaces, subsurfaces = dump(model)
    shading = dump_shading(model)

    payload = {
        "schema_version": 1,
        "source_model": os.path.basename(path),
        "generated_utc": datetime.now(timezone.utc)
                                 .replace(microsecond=0).isoformat(),
        "units": "meters",
        "north_axis_deg": model.getBuilding().northAxis(),
        "surfaces": surfaces,
        "subsurfaces": subsurfaces,
        "shading": shading,
    }

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dumps_compact_vertices(payload))
        fh.write("\n")

    by_type = {}
    for s in surfaces:
        by_type[s["surface_type"]] = by_type.get(s["surface_type"], 0) + 1

    air = [s for s in surfaces if s["is_air_boundary"]]

    print("wrote %s" % out)
    print("  %d surfaces (%s), %d subsurfaces, %d spaces"
          % (len(surfaces),
             ", ".join("%s %d" % kv for kv in sorted(by_type.items())),
             len(subsurfaces), len(model.getSpaces())))
    if shading:
        by_group = {}
        for s_ in shading:
            by_group[s_["group_type"]] = by_group.get(s_["group_type"], 0) + 1
        print("  %d shading surface(s) (%s), %.3f m2"
              % (len(shading),
                 ", ".join("%s %d" % kv for kv in sorted(by_group.items())),
                 sum(s_["gross_area_m2"] for s_ in shading)))
    if air:
        print("  %d air boundary surface(s) across %d construction(s)"
              % (len(air), len(set(s["construction"] for s in air))))


if __name__ == "__main__":
    main()
