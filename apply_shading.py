# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Apply hand-drawn canopies and fins to an .osm as OpenStudio shading surfaces.

Run under the bridge venv:

    osvenv/Scripts/python.exe apply_shading.py model.osm shading.json \
        --out model.osm

Each outline becomes a ShadingSurface in a ShadingSurfaceGroup named after its
type.  A **Building** group rotates with the building's north axis, which is
what an attached canopy must do; a **Site** group does not, which is what a
neighbouring building or a tree needs.  Both groups are left at the origin
with no rotation of their own, so the vertices written here are the same
building coordinates the FreeCAD document holds -- and the script proves that
by reading them back through the group's transformation before it saves.

Shading surfaces created by a previous run are removed first, so redrawing a
canopy in FreeCAD and re-running replaces it rather than stacking a second one
on top.
"""

import argparse
import json
import os
import sys

import openstudio

import opening_diff

# Marker written into each shading surface's comment so a re-run can tell its
# own work apart from shading added by hand or by an MCP tool.
MARKER = "created-by:freecad-bridge"
ID_PREFIX = "id:"


def comment_for(shading_id):
    return "%s %s%s" % (MARKER, ID_PREFIX, shading_id) if shading_id else MARKER


def id_of(shade):
    """The outline id recorded on a shade this script created, or None."""
    text = shade.comment() or ""
    if MARKER not in text:
        return None
    at = text.find(ID_PREFIX)
    return text[at + len(ID_PREFIX):].strip() or None if at >= 0 else None


def outline_key(group_type, vertices):
    """A shade's identity by shape, for adopting one whose id went missing."""
    return (group_type,
            tuple(sorted(tuple(round(c, 4) for c in v) for v in vertices)))


GROUP_NAMES = {
    "Building": "Building Shading Surfaces",
    "Site": "Site Shading Surfaces",
}


def load_model(path):
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.path(path))
    if not loaded.is_initialized():
        raise SystemExit("could not load %s" % path)
    return loaded.get()


def ours(model):
    """Shades this script created, as ({id: shade}, [shades with no id])."""
    keyed, unkeyed = {}, []
    for shade in model.getShadingSurfaces():
        if MARKER not in (shade.comment() or ""):
            continue
        found = id_of(shade)
        if found:
            keyed[found] = shade
        else:
            unkeyed.append(shade)
    return keyed, unkeyed


def adoptable(model):
    """{(group type, outline): shade} for shades this script does not own.

    The comment is the only record of which drawn outline a shade came from,
    and one stray edit removes it.  Without this, the id would match nothing,
    a fresh shade would be created, and the orphan would stay where it is --
    two canopies in the same place, double the shading, no error.
    """
    found = {}
    for group in model.getShadingSurfaceGroups():
        transformation = group.buildingTransformation()
        for shade in group.shadingSurfaces():
            if MARKER in (shade.comment() or ""):
                continue
            pts = transformation * shade.vertices()
            found.setdefault(
                outline_key(group.shadingSurfaceType(),
                            [(p.x(), p.y(), p.z()) for p in pts]), shade)
    return found


def drop_empty_groups(model):
    """A group of ours left with nothing in it is clutter in the tree.

    Only this script's own group names are considered -- a group somebody else
    made, empty or not, is not ours to remove.
    """
    dropped = 0
    for group in model.getShadingSurfaceGroups():
        if group.nameString() in GROUP_NAMES.values() and \
                not group.shadingSurfaces():
            group.remove()
            dropped += 1
    return dropped


def get_group(model, group_type, groups):
    """The group for this type, reused across runs so nothing accumulates."""
    if group_type in groups:
        return groups[group_type]
    name = GROUP_NAMES[group_type]
    for existing in model.getShadingSurfaceGroups():
        if existing.nameString() == name:
            groups[group_type] = existing
            return existing
    group = openstudio.model.ShadingSurfaceGroup(model)
    group.setName(name)
    if not group.setShadingSurfaceType(group_type):
        raise SystemExit("OpenStudio rejected shading surface type %r"
                         % group_type)
    groups[group_type] = group
    return group


def to_group_frame(group, vertices):
    pts = openstudio.Point3dVector()
    for x, y, z in vertices:
        pts.append(openstudio.Point3d(x, y, z))
    return group.transformation().inverse() * pts


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("osm")
    ap.add_argument("shading")
    ap.add_argument("--out", help="defaults to overwriting the input .osm")
    ap.add_argument("--keep-existing", action="store_true",
                    help="do not remove shading from a previous run")
    ap.add_argument("--report", action="store_true",
                    help="show what would change and write nothing")
    ap.add_argument("--tolerance-m", type=float, default=1e-6,
                    help="worst allowed read-back deviation (default "
                         "%(default)s)")
    args = ap.parse_args()

    model = load_model(os.path.abspath(args.osm))
    with open(args.shading, encoding="utf-8") as fh:
        data = json.load(fh)

    keyed, unkeyed = ours(model)
    if args.keep_existing:
        keyed, unkeyed = {}, []

    before_state = {}
    for shading_id, shade in keyed.items():
        group = shade.shadingSurfaceGroup()
        pts, gtype = [], ""
        if group.is_initialized():
            gtype = group.get().shadingSurfaceType()
            pts = [(p.x(), p.y(), p.z())
                   for p in group.get().buildingTransformation()
                   * shade.vertices()]
        before_state[shading_id] = {"name": shade.nameString(),
                                    "type": gtype, "host": gtype,
                                    "vertices": pts}
    after_state = {}
    for spec in data["shading"]:
        if spec.get("id"):
            after_state[spec["id"]] = {
                "name": spec["name"],
                "type": spec.get("group_type", "Building"),
                "host": spec.get("group_type", "Building"),
                "vertices": [tuple(v) for v in spec["vertices"]]}
    for line in opening_diff.report(opening_diff.diff(before_state,
                                                      after_state),
                                    noun="shade"):
        print(line)
    print("")
    if args.report:
        # A report is a success, not a failure: exit 0 like update does.
        print("report only -- re-run without --report to write the changes.")
        return

    # Remove first, then park survivors: OpenStudio makes names unique by
    # suffixing, so setting a final name while last run's copy still holds it
    # silently yields "Shading South 5".
    incoming = {spec.get("id") for spec in data["shading"] if spec.get("id")}
    removed = 0
    for shading_id in [k for k in keyed if k not in incoming]:
        keyed.pop(shading_id).remove()
        removed += 1
    for shade in unkeyed:
        shade.remove()
        removed += 1
    for shading_id, shade in keyed.items():
        shade.setName("freecad-bridge-parked-%s" % shading_id)

    orphans = adoptable(model)
    groups, added, updated, problems = {}, 0, 0, []
    adopted = []
    worst = 0.0

    for spec in data["shading"]:
        group_type = spec.get("group_type", "Building")
        if group_type not in GROUP_NAMES:
            problems.append("%s: group type %r is not Building or Site"
                            % (spec["name"], group_type))
            continue
        group = get_group(model, group_type, groups)

        pts = to_group_frame(group, spec["vertices"])
        shading_id = spec.get("id") or ""
        shade = keyed.get(shading_id) if shading_id else None
        was_adopted = False
        if shade is None:
            shade = orphans.pop(outline_key(group_type, spec["vertices"]), None)
            was_adopted = shade is not None

        if shade is None:
            shade = openstudio.model.ShadingSurface(pts, model)
            added += 1
        elif not shade.setVertices(pts):
            problems.append("%s: OpenStudio rejected the new outline"
                            % spec["name"])
            continue
        elif was_adopted:
            adopted.append(spec["name"])
        else:
            updated += 1

        shade.setName(spec["name"])
        if not shade.setShadingSurfaceGroup(group):
            problems.append("%s: OpenStudio rejected it on group %s"
                            % (spec["name"], group.nameString()))
            continue
        shade.setComment(comment_for(shading_id))

        # Read it back the way dump_osm_geometry.py will: whatever frame the
        # group turns out to be in, the vertices must land where they were
        # drawn.  A silent frame error here would be invisible until somebody
        # noticed the shadows were in the wrong place.
        back = group.transformation() * shade.vertices()
        want = spec["vertices"]
        if len(back) != len(want):
            problems.append("%s: %d vertices went in, %d came back"
                            % (spec["name"], len(want), len(back)))
            continue
        for got, expected in zip(back, want):
            worst = max(worst,
                        abs(got.x() - expected[0]),
                        abs(got.y() - expected[1]),
                        abs(got.z() - expected[2]))

    print("added %d, updated %d in place, adopted %d, removed %d"
          % (added, updated, len(adopted), removed))
    if adopted:
        print("  adopted rather than duplicated: %s" % ", ".join(adopted[:6]))
    for group_type in sorted(groups):
        surfaces = groups[group_type].shadingSurfaces()
        print("  %-8s %-26s %2d surface(s), %.3f m2"
              % (group_type, GROUP_NAMES[group_type], len(surfaces),
                 sum(s.grossArea() for s in surfaces)))
    if added or updated or adopted:
        print("  worst read-back deviation: %.9f m" % worst)

    dropped = drop_empty_groups(model)
    if dropped:
        print("removed %d empty shading group(s)" % dropped)

    if worst > args.tolerance_m:
        problems.append(
            "vertices read back %.9f m from where they were drawn, over the "
            "%.9f m tolerance" % (worst, args.tolerance_m))

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)
        sys.exit("\nRefusing to write the model.")

    out = os.path.abspath(args.out or args.osm)
    model.save(openstudio.path(out), True)
    print("\nwrote %s" % out)
    print("  %d shading surface(s) now in the model"
          % len(model.getShadingSurfaces()))


if __name__ == "__main__":
    main()
