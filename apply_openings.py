# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Apply hand-drawn openings to an .osm as OpenStudio subsurfaces.

Run under the bridge venv:

    osvenv/Scripts/python.exe apply_openings.py model.osm openings.json \
        --out model.osm

Each opening becomes a SubSurface on its named host surface.  Vertices arrive
in building coordinates and are converted back into the host space's frame.

Each opening carries an id minted into the FreeCAD sketch, and that is what
this matches on: an opening it has seen before is **updated in place** --
vertices, name, type, even its host wall -- so its OpenStudio handle survives.
That matters because a handle is what a shading control, a frame and divider,
or an interzone pairing references; rebuilding a subsurface silently breaks
every one of them.  Only openings the drawing no longer contains are removed,
and only ones it has never seen are created.
"""

import argparse
import json
import os
import sys

import openstudio

import opening_diff

# Marker written into each subsurface's comment so a re-run can tell its own
# work apart from subsurfaces added by hand or by an MCP tool.  The outline's
# id follows it, and that is what keeps a handle alive.
MARKER = "created-by:freecad-bridge"
ID_PREFIX = "id:"


def comment_for(opening_id):
    return "%s %s%s" % (MARKER, ID_PREFIX, opening_id) if opening_id else MARKER


def id_of(subsurface):
    """The outline id recorded on a subsurface this script created, or None."""
    text = subsurface.comment() or ""
    if MARKER not in text:
        return None
    at = text.find(ID_PREFIX)
    return text[at + len(ID_PREFIX):].strip() or None if at >= 0 else None


def load_model(path):
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.path(path))
    if not loaded.is_initialized():
        raise SystemExit("could not load %s" % path)
    return loaded.get()


def to_space_frame(space, vertices):
    """Building coordinates -> the space's own frame."""
    pts = openstudio.Point3dVector()
    for x, y, z in vertices:
        pts.append(openstudio.Point3d(x, y, z))
    return space.transformation().inverse() * pts


# Same tolerances fc_export_openings.py's own find_host uses, so apply
# recognises exactly what export would have drawn the outline onto.
COPLANAR_TOL_M = 0.001
INSIDE_TOL_M = 0.001


def signed_area_2d(pts):
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i].x(), pts[i].y()
        x2, y2 = pts[(i + 1) % n].x(), pts[(i + 1) % n].y()
        total += x1 * y2 - x2 * y1
    return total / 2.0


def fits_host(vertices_building, surface):
    """True when this outline is coplanar with, and inside, this surface.

    Tested in the surface's own space frame -- coordinates only line up with
    a surface's plane and polygon there, not in building coordinates.

    pointInPolygon needs both flattened onto z = 0 (alignFace does that; a
    surface's own frame is planar but not z = 0) and wound clockwise, which
    alignFace does not guarantee -- it mirrors whatever winding the surface
    already has, and a surface's winding is whichever way its outward
    normal makes it, not a fixed convention.  Reversed here rather than
    trusted, since taking that on faith once already put an opening
    outside its wall with no error from anything.
    """
    space = surface.space()
    if not space.is_initialized():
        return False
    pts = to_space_frame(space.get(), vertices_building)
    plane = surface.plane()
    if not all(plane.pointOnPlane(p, COPLANAR_TOL_M) for p in pts):
        return False

    align = openstudio.Transformation.alignFace(surface.vertices())
    flat = align.inverse()
    host_poly = list(flat * surface.vertices())
    if signed_area_2d(host_poly) > 0:
        host_poly = list(reversed(host_poly))
    host_poly = openstudio.Point3dVector(host_poly)

    return all(openstudio.pointInPolygon(p, host_poly, INSIDE_TOL_M)
              for p in flat * pts)


def find_host(vertices_building, named_host, surfaces):
    """The host this outline actually belongs to, geometrically.

    Mirrors fc_export_openings.py's own find_host: an outline belongs to
    whichever surface it is coplanar with and inside, never to a name.  The
    saved host_surface is tried first -- when it still fits, nothing about
    the model has to change to confirm it -- but a stale name (surface
    numbers are not stable across a full rebuild) is not trusted just
    because it resolves; the same geometric test is what decides.

    Returns (host, is_fallback, problem).  problem is set, and host is
    None, when nothing fits or more than one surface does -- an opening
    floating off its wall is a silent-wrong-answer risk, so this fails
    loudly rather than guessing.
    """
    if named_host is not None and fits_host(vertices_building, named_host):
        return named_host, False, None

    matches = [s for s in surfaces if fits_host(vertices_building, s)]
    if len(matches) == 1:
        return matches[0], True, None
    if not matches:
        return None, True, "no surface in the model is coplanar with and contains it"
    names = ", ".join(s.nameString() for s in matches[:4])
    return None, True, "sits inside %d surfaces (%s)" % (len(matches), names)


def orient_like(pts, host_normal):
    """Match the host surface's facing, reversing the loop if needed.

    A subsurface wound against its parent is rejected by OpenStudio with an
    unhelpful message, so fix it here rather than surfacing it later.
    """
    normal = openstudio.getOutwardNormal(pts)
    if not normal.is_initialized():
        return pts
    dot = (normal.get().x() * host_normal.x()
           + normal.get().y() * host_normal.y()
           + normal.get().z() * host_normal.z())
    if dot < 0:
        return openstudio.Point3dVector(list(reversed(list(pts))))
    return pts


# Rounding for recognising an existing subsurface as the same opening.  The
# vertices went out through this pipeline and came back, so this only absorbs
# JSON's six decimal places, not real disagreement.
ADOPT_TOL_M = 1e-4


def outline_key(host_name, vertices):
    """A subsurface's identity by shape: its host and its rounded outline."""
    places = 4
    return (host_name,
            tuple(sorted(tuple(round(c, places) for c in v)
                         for v in vertices)))


def adoptable(model):
    """{(host, outline): subsurface} for subsurfaces this script does not own.

    A comment is the only place the model records which drawn outline a
    subsurface came from, and it is one stray edit away from being gone.  When
    that happens the id matches nothing, and creating a fresh subsurface leaves
    the orphan sitting on the same wall -- two openings in one hole, double the
    glazing area, no error anywhere.  Measured: 28 subsurfaces became 29.

    So an unowned subsurface occupying exactly the outline we are about to
    create is adopted instead: updated in place, re-stamped with the id, handle
    intact.  Matching on shape rather than name because the name is the part
    that changes.
    """
    found = {}
    for sub in model.getSubSurfaces():
        if MARKER in (sub.comment() or ""):
            continue
        surface = sub.surface()
        if not surface.is_initialized():
            continue
        space = surface.get().space()
        if not space.is_initialized():
            continue
        pts = space.get().transformation() * sub.vertices()
        key = outline_key(surface.get().nameString(),
                          [(p.x(), p.y(), p.z()) for p in pts])
        found.setdefault(key, sub)
    return found


def snapshot(keyed):
    """{id: record} of the model as it stands, for diffing against the drawing."""
    state = {}
    for opening_id, sub in keyed.items():
        surface = sub.surface()
        host = surface.get().nameString() if surface.is_initialized() else ""
        pts = []
        if surface.is_initialized():
            space = surface.get().space()
            if space.is_initialized():
                pts = [(p.x(), p.y(), p.z())
                       for p in space.get().transformation() * sub.vertices()]
        state[opening_id] = {"name": sub.nameString(),
                             "type": sub.subSurfaceType(),
                             "host": host, "vertices": pts}
    return state


def ours(model):
    """Subsurfaces this script created, as {outline id: subsurface}.

    Anything of ours without an id goes in under a None key and is treated as
    unmatchable -- it predates the ids and has to be rebuilt once.
    """
    keyed, unkeyed = {}, []
    for sub in model.getSubSurfaces():
        if MARKER not in (sub.comment() or ""):
            continue
        found = id_of(sub)
        if found:
            keyed[found] = sub
        else:
            unkeyed.append(sub)
    return keyed, unkeyed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("osm")
    ap.add_argument("openings")
    ap.add_argument("--out", help="defaults to overwriting the input .osm")
    ap.add_argument("--keep-existing", action="store_true",
                    help="do not remove subsurfaces from a previous run")
    ap.add_argument("--report", action="store_true",
                    help="show what would change and write nothing")
    args = ap.parse_args()

    model = load_model(os.path.abspath(args.osm))
    with open(args.openings, encoding="utf-8") as fh:
        data = json.load(fh)

    keyed, unkeyed = ours(model)
    if args.keep_existing:
        keyed, unkeyed = {}, []

    # Resolved once, up front, so the diff below previews the same host the
    # apply loop will actually use -- not the saved name, which a full
    # rebuild can leave pointing at an unrelated surface.  See find_host.
    by_name = {s.nameString(): s for s in model.getSurfaces()}
    all_surfaces = list(model.getSurfaces())
    resolved, refound = [], []
    for spec in data["openings"]:
        named_host = by_name.get(spec["host_surface"])
        host, is_fallback, problem = find_host(
            spec["vertices"], named_host, all_surfaces)
        resolved.append((host, problem))
        if is_fallback and host is not None:
            refound.append((spec["name"], spec["host_surface"],
                            host.nameString()))

    # The diff, before anything is touched.  An architectural revision moves
    # windows and widens doors as surely as it moves walls, and "updated 28 in
    # place" says nothing about which ones.
    before_state = snapshot(keyed)
    after_state = {}
    for spec, (host, problem) in zip(data["openings"], resolved):
        if spec.get("id") and host is not None:
            after_state[spec["id"]] = {
                "name": spec["name"], "type": spec["subsurface_type"],
                "host": host.nameString(),
                "vertices": [tuple(v) for v in spec["vertices"]]}
    changes = opening_diff.diff(before_state, after_state)
    for line in opening_diff.report(changes):
        print(line)
    if refound:
        print("\n%d opening(s) had a stale host name and were re-matched "
              "geometrically:" % len(refound))
        for name, was, now in refound:
            print("  %-24s %s -> %s" % (name, was, now))
    print("")
    if args.report:
        # A report is a success, not a failure: exit 0 like update does.
        print("report only -- re-run without --report to write the changes.")
        return

    # Removal comes first, and survivors are parked under a name nothing else
    # can hold.  OpenStudio makes names unique by suffixing, so creating
    # "Door North 01" while last run's "Door North 01" still exists silently
    # yields "Door North 8" -- and renaming survivors in place collides just
    # as easily, because deleting one opening shifts every later number on
    # that facade.  Parking first makes every final name free when it is set.
    incoming = {spec.get("id") for spec in data["openings"] if spec.get("id")}
    removed = 0
    for opening_id in [k for k in keyed if k not in incoming]:
        keyed.pop(opening_id).remove()
        removed += 1
    for sub in unkeyed:
        sub.remove()
        removed += 1
    for opening_id, sub in keyed.items():
        sub.setName("freecad-bridge-parked-%s" % opening_id)

    orphans = adoptable(model)
    added = updated = 0
    adopted = []
    problems = []
    seen = set()

    for spec, (host, problem) in zip(data["openings"], resolved):
        if problem:
            problems.append("%s: %s" % (spec["name"], problem))
            continue

        space = host.space()
        if not space.is_initialized():
            problems.append("%s: host surface has no space" % spec["name"])
            continue

        pts = to_space_frame(space.get(), spec["vertices"])
        pts = orient_like(pts, host.outwardNormal())

        opening_id = spec.get("id") or ""
        sub = keyed.get(opening_id) if opening_id else None

        adopted_this = False
        if sub is None:
            key = outline_key(host.nameString(), spec["vertices"])
            sub = orphans.pop(key, None)
            if sub is not None:
                adopted_this = True
                if opening_id:
                    keyed[opening_id] = sub

        if sub is not None:
            # Update in place.  The handle survives, so a shading control, a
            # frame and divider or an interzone pairing that references this
            # subsurface still references it after a retype.  Order matters:
            # set the host before the vertices, or OpenStudio validates the
            # new outline against the old wall.
            seen.add(opening_id)
            if not sub.setSurface(host):
                problems.append(
                    "%s: OpenStudio rejected moving it to %s"
                    % (spec["name"], host.nameString()))
                continue
            if not sub.setVertices(pts):
                problems.append("%s: OpenStudio rejected the new outline"
                                % spec["name"])
                continue
            sub.setName(spec["name"])
            if not sub.setSubSurfaceType(spec["subsurface_type"]):
                problems.append("%s: could not set type %r"
                                % (spec["name"], spec["subsurface_type"]))
            sub.setComment(comment_for(opening_id))
            if adopted_this:
                adopted.append(spec["name"])
            else:
                updated += 1
            continue

        sub = openstudio.model.SubSurface(pts, model)
        sub.setName(spec["name"])
        if not sub.setSurface(host):
            sub.remove()
            problems.append(
                "%s: OpenStudio rejected it on %s -- check it is coplanar with "
                "and inside the wall" % (spec["name"], host.nameString()))
            continue
        if not sub.setSubSurfaceType(spec["subsurface_type"]):
            problems.append("%s: could not set type %r"
                            % (spec["name"], spec["subsurface_type"]))
        sub.setComment(comment_for(opening_id))
        if opening_id:
            seen.add(opening_id)
        added += 1

    # A parked survivor the loop never reached -- its host went missing, say.
    # Leaving it parked would put a name nothing recognises into the model.
    orphaned = [(k, s) for k, s in keyed.items() if k not in seen]
    for opening_id, sub in orphaned:
        sub.remove()
        removed += 1

    print("added %d, updated %d in place, adopted %d, removed %d"
          % (added, updated, len(adopted), removed))
    if adopted:
        print("  adopted rather than duplicated -- their id was missing from "
              "the model, so\n  they were matched on shape and re-stamped: "
              "%s" % ", ".join(adopted[:6]))
    if unkeyed:
        print("  %d had no id and were rebuilt -- they predate outline ids, "
              "so their\n  handles changed this once and will be stable from "
              "now on" % len(unkeyed))
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)

    out = os.path.abspath(args.out or args.osm)
    model.save(openstudio.path(out), True)
    print("\nwrote %s" % out)
    print("  %d subsurfaces now in the model" % len(model.getSubSurfaces()))

    if problems:
        sys.exit("\nModel written, but some openings were not applied.")


if __name__ == "__main__":
    main()
