# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Carry a surface's identity across a rebuild.

Rebuilding a space deletes every surface in it and builds new ones, and
everything hanging off the old surfaces goes with them: the windows and doors,
the construction assignments -- air boundaries included -- and the names.
Recovering that afterwards has cost a full export/apply cycle for the openings,
a re-derivation for the air boundaries, and a hand-written matcher for the two
couplings no rule can re-derive.  It also quietly reopened a rated fire wall
once, because the re-derivation proposed it and the surface names it had to be
excluded by had all moved.

None of that is necessary.  What a rebuild changes is where the top of a wall
is; what it does not change is where the wall stands.  So a surface can be
recognised on the far side of a rebuild by the line it stands on in plan
together with the height it starts at -- (plan line, base z).  Measured across
the Extend round trip that motivated this: 147 of 147 walls and floors in the
14 rebuilt spaces matched on that key, none ambiguous, none left without a
successor, and not one changed its vertex count.

This deliberately does *not* match the incoming face list from FreeCAD against
the live surfaces.  Those two do not correspond.  FreeCAD hands over the raw
carve -- one face per wall -- and intersectSurfaces then splits each of those
into as many pieces as it has neighbours: 8 faces became 19 surfaces in one
room here, and only 73 of 180 lined up.  The capture is taken from the model
before the rebuild and applied to the model after intersect has run, so both
sides are split the same way and the question never arises.

What is carried:

  name           so a --solid-surface list, a test, or a construction
                 assigned by surface name still points at the same wall
  construction   which is where an air boundary lives
  subsurfaces    outline, type, construction, and the OS_OpeningId comment
                 that apply_openings.py keys on
  Adiabatic      the one boundary condition matchSurfaces cannot re-derive,
                 because nothing in the geometry implies it

What is not carried: sun and wind exposure, and every other boundary
condition.  matchSurfaces derives those from the new geometry and is right to;
forcing the old ones back would be how an attic floor ends up claiming to be
Ground six metres in the air.

A surface with no successor is reported, never guessed at.  Its construction
is inherited only when the face that replaced it covers the same ground and
nothing else claims it -- which is what happens when a flat roof already
fragmented by intersectSurfaces comes back as one pitched face.  Its
subsurfaces move only when the replacement is coplanar, because a skylight in
a roof that has just changed pitch genuinely needs re-cutting from the
drawing, and silently reprojecting it would be a worse answer than saying so.
"""

import math

# openstudio is imported where it is used, not here: everything above
# the restore -- the key, the containment tests -- is plain geometry,
# and keeping it importable under FreeCAD's interpreter means those
# tests run in both of the bridge's two pythons rather than one.

# 0.1 mm.  The vertices went out to FreeCAD and came back through JSON's six
# decimal places, so this absorbs the round trip and nothing wider.
KEY_DP = 4

# How far a subsurface may sit off a candidate host's plane and still be
# re-homed onto it.  Same order as the outlines themselves.
COPLANAR_TOL_M = 1e-3

# Slack for point-in-polygon, so a window whose edge lies exactly on the wall
# it belongs to is inside it.
CONTAIN_TOL_M = 1e-4

# How far off the line between its neighbours a plan point may sit and still
# be treated as a split rather than a corner.  A millimetre: rounding to
# KEY_DP can move a genuinely collinear point by a few tens of microns over a
# long wall, while the shallowest real corner in a floor plan is orders of
# magnitude further out than this.
PLAN_COLLINEAR_TOL_M = 1e-3

# Above this the face is flat enough for its plan outline to mean something.
# Matches the tilt OpenStudio itself uses to call a surface a roof or a floor.
HORIZONTAL = 0.7071


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def model_points(space, obj):
    """A surface or subsurface's vertices in building coordinates.

    Surfaces are stored in their space's own frame.  Every key and every
    containment test here is between objects that may end up in different
    spaces, so all of it happens in building coordinates.
    """
    return [(p.x(), p.y(), p.z())
            for p in space.transformation() * obj.vertices()]


def _weld(plan):
    """Drop consecutive repeats, and close the loop.

    A wall's outline projects onto its own plan line twice -- once going along
    the bottom edge and once coming back along the top -- so welding leaves
    the line itself.
    """
    out = []
    for point in plan:
        if out and out[-1] == point:
            continue
        out.append(point)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _off_line(a, b, c):
    """How far b sits off the line through a and c, in metres."""
    span = math.hypot(c[0] - a[0], c[1] - a[1])
    if span < 1e-12:
        return float("inf")
    cross = ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    return abs(cross) / span


def plan_shape(points, tol=PLAN_COLLINEAR_TOL_M):
    """The ground a face stands on, with split points removed.

    Points that only sit on a line between their neighbours are dropped,
    because they are not part of the shape -- they are where something else
    met it.  That matters: a hip or a valley crossing a room puts an extra
    vertex in the top edge of every wall it lands on, and keying on the raw
    point set would make those walls unrecognisable on the far side of the
    rebuild for no reason.  Their footprint has not moved.
    """
    plan = _weld([(round(x, KEY_DP), round(y, KEY_DP)) for x, y, _z in points])
    if len(plan) < 3:
        return tuple(sorted(plan))

    corners = [p for i, p in enumerate(plan)
               if _off_line(plan[i - 1], p, plan[(i + 1) % len(plan)]) > tol]
    if len(corners) < 3:
        # Every point was on one line: a vertical face.  It is a line segment
        # in plan, and its two ends are the whole of its identity.
        return (min(plan), max(plan))
    return tuple(sorted(corners))


def surface_key(points):
    """What a surface keeps across a rebuild.

    For a wall: the line it stands on, plus the height it starts at.  Plan
    alone is not enough -- a wall along one line is usually several surfaces
    stacked up the height of the room, split where each neighbour reaches only
    part of it, and a ten-sided room came back with fifteen walls.  A rebuild
    moves tops, never bases, so the base separates them.

    For a floor or a ceiling: the ground it covers and which way it faces.
    Base z would be wrong here, because raising a room moves its ceiling by
    definition -- and then a ceiling would never be recognised, losing its
    name and its construction over a change that did not alter its outline
    at all.  Two horizontal faces of one space cannot share an outline and a
    facing without overlapping, so this stays unambiguous.
    """
    plan = plan_shape(points)
    normal = newell(points)
    if normal is not None and abs(normal[2]) > HORIZONTAL:
        return ("up" if normal[2] > 0 else "down"), plan, 0.0
    return "wall", plan, round(min(z for _x, _y, z in points), KEY_DP)


def newell(points):
    """Unit normal of a polygon, or None if it has no area."""
    nx = ny = nz = 0.0
    count = len(points)
    for i in range(count):
        x0, y0, z0 = points[i]
        x1, y1, z1 = points[(i + 1) % count]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return None
    return (nx / length, ny / length, nz / length)


def plane_of(points):
    """(unit normal, offset) of the plane through a polygon."""
    normal = newell(points)
    if normal is None:
        return None
    offset = sum(normal[i] * points[0][i] for i in range(3))
    return normal, offset


def off_plane(points, plane):
    """The furthest any point sits from a plane."""
    normal, offset = plane
    return max(abs(sum(normal[i] * p[i] for i in range(3)) - offset)
               for p in points)


def flatten(points, normal):
    """Drop the polygon's dominant axis, so it can be tested in 2D."""
    axis = max(range(3), key=lambda i: abs(normal[i]))
    keep = [i for i in range(3) if i != axis]
    return [(p[keep[0]], p[keep[1]]) for p in points]


def _near_segment(point, a, b, tol):
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span < 1e-18:
        return math.hypot(px - ax, py - ay) <= tol
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) <= tol


def point_inside(point, polygon, tol=CONTAIN_TOL_M):
    """Ray cast, counting a point on the boundary as inside."""
    x, y = point
    count = len(polygon)
    inside = False
    for i in range(count):
        a = polygon[i]
        b = polygon[(i + 1) % count]
        if _near_segment(point, a, b, tol):
            return True
        if (a[1] > y) != (b[1] > y):
            crossing = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < crossing:
                inside = not inside
    return inside


def contains(host_points, points, plane_tol=COPLANAR_TOL_M,
             edge_tol=CONTAIN_TOL_M):
    """Does this face hold that polygon -- in its plane, not just its shadow?

    The plane test is not a formality.  Without it a skylight in a flat roof
    would count as contained by the pitched face that replaced it, because
    from above it plainly is, and it would be re-attached at the old height:
    an opening floating below its own roof, which OpenStudio accepts.
    """
    plane = plane_of(host_points)
    if plane is None:
        return False
    if off_plane(points, plane) > plane_tol:
        return False
    normal, _offset = plane
    outline = flatten(host_points, normal)
    return all(point_inside(p, outline, edge_tol)
               for p in flatten(points, normal))


def plan_contains(host_points, points, tol=CONTAIN_TOL_M):
    """Same test, but as shadows -- for a roof that changed pitch."""
    outline = [(p[0], p[1]) for p in host_points]
    return all(point_inside((p[0], p[1]), outline, tol) for p in points)


def is_horizontal(points):
    normal = newell(points)
    return normal is not None and abs(normal[2]) > HORIZONTAL


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

def _construction_name(obj):
    con = obj.construction()
    return con.get().nameString() if con.is_initialized() else None


def capture_subsurface(space, sub):
    return {
        "name": sub.nameString(),
        "type": sub.subSurfaceType(),
        "vertices": model_points(space, sub),
        "construction": _construction_name(sub),
        "comment": sub.comment() or "",
    }


def capture(model, space_names):
    """Everything worth keeping from the spaces about to be rebuilt.

    Returns (captured, ambiguous):

      captured   {space_name: {key: record}}
      ambiguous  [(space_name, [surface name, ...])] for keys held by more
                 than one surface in the same space

    An ambiguous key is dropped rather than guessed at.  Restoring a window
    onto whichever of two identically-keyed walls happened to be indexed
    first is exactly the kind of silent mistake this module exists to stop.
    """
    wanted = set(space_names)
    captured, ambiguous = {}, []

    for space in model.getSpaces():
        name = space.nameString()
        if name not in wanted:
            continue
        by_key = {}
        for surface in space.surfaces():
            points = model_points(space, surface)
            by_key.setdefault(surface_key(points), []).append(
                (surface, points))

        records = {}
        for key, entries in by_key.items():
            if len(entries) > 1:
                ambiguous.append(
                    (name, sorted(s.nameString() for s, _p in entries)))
                continue
            surface, points = entries[0]
            records[key] = {
                "name": surface.nameString(),
                "surface_type": surface.surfaceType(),
                "construction": _construction_name(surface),
                "adiabatic": surface.outsideBoundaryCondition() == "Adiabatic",
                "area_m2": surface.grossArea(),
                "points": points,
                "subsurfaces": [capture_subsurface(space, sub)
                                for sub in surface.subSurfaces()],
            }
        captured[name] = records

    return captured, ambiguous


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def _constructions_by_name(model):
    out = {}
    for con in model.getConstructionBases():
        out[con.nameString()] = con
    return out


PARK_PREFIX = "__rebuild_"


def _park_names(model, plans, wanted):
    """Move every surface that could be in the way onto a temporary name.

    The names being restored are all free -- the surfaces that held them were
    deleted -- which is exactly why they have to be cleared first.  OpenStudio
    hands a freed name straight back to the next surface it creates, so by the
    time the restore runs, some other new surface is often sitting on the name
    a wall is about to ask for.  setName does not fail in that case; it
    uniquifies to 'Surface 274 1', and the wall quietly ends up somewhere
    else in the numbering.

    This has to be global rather than per space, and both halves matter.
    Parking only the space being restored lets the space after it lose 18
    names to siblings already processed -- measured, on this building.  And
    parking only the rebuilt spaces misses the surfaces intersectSurfaces
    creates in untouched neighbours, which are new objects that may also have
    picked up a freed name.
    """
    parked = []
    seen = set()
    for index, (space, _records, _matched, _orphans, _live) in enumerate(
            plans):
        for position, surface in enumerate(space.surfaces()):
            surface.setName("%s%d_%d" % (PARK_PREFIX, index, position))
            parked.append(surface)
            seen.add(surface.handle())

    for surface in model.getSurfaces():
        if surface.handle() in seen or surface.nameString() not in wanted:
            continue
        surface.setName("%s_outside_%d" % (PARK_PREFIX, len(parked)))
        parked.append(surface)
    return parked


def _assign_names(space, records, matched, report):
    for key, record in records.items():
        surface = matched.get(key)
        if surface is None:
            continue
        surface.setName(record["name"])
        if surface.nameString() != record["name"]:
            report["notes"].append(
                "  %s could not take back the name %r (it became %r)"
                % (space.nameString(), record["name"], surface.nameString()))
        else:
            report["renamed"] += 1


def _unpark(model, parked):
    """Give anything still on a temporary name a clean, free one."""
    taken = {s.nameString() for s in model.getSurfaces()}
    counter = 0
    for surface in parked:
        if not surface.nameString().startswith(PARK_PREFIX):
            continue
        while True:
            counter += 1
            candidate = "Surface %d" % counter
            if candidate not in taken:
                break
        taken.add(candidate)
        surface.setName(candidate)


def _restore_subsurfaces(model, space, surface, record, constructions,
                         report):
    import openstudio

    host_points = model_points(space, surface)
    for spec in record["subsurfaces"]:
        if not contains(host_points, spec["vertices"]):
            report["lost"].append(
                (record["name"], spec["name"],
                 "its outline no longer fits the wall"))
            continue
        pts = openstudio.Point3dVector()
        for x, y, z in spec["vertices"]:
            pts.append(openstudio.Point3d(x, y, z))
        pts = space.transformation().inverse() * pts

        sub = openstudio.model.SubSurface(pts, model)
        if not sub.setSurface(surface):
            sub.remove()
            report["lost"].append(
                (record["name"], spec["name"],
                 "OpenStudio rejected it on the rebuilt wall"))
            continue
        sub.setName(spec["name"])
        sub.setSubSurfaceType(spec["type"])
        if spec["comment"]:
            sub.setComment(spec["comment"])
        if spec["construction"] in constructions:
            sub.setConstruction(constructions[spec["construction"]])
        report["subsurfaces"] += 1


def _rehome_opening(spec, live):
    """Where one opening can still go when its wall has no successor.

    Asking whether some new surface contains the whole of the old one is the
    wrong question for an opening, and it is the question that loses windows.
    A wall that was *shortened* -- because the partition at its far end moved,
    which is most of what a plan edit does -- keeps its line, its base and
    everything on it, but no longer covers its old extent, so the whole-face
    test rejects it and seven openings on unmoved walls go in the bin.

    The question that matters is narrower: is this particular opening inside
    that face, in its plane?  Only an unambiguous single answer is accepted;
    two candidates mean the wall was split under the opening, and picking one
    would be a guess.
    """
    hits = [surface for surface, points in live
            if contains(points, spec["vertices"])]
    return hits[0] if len(hits) == 1 else None


def _rehome(record, live, used):
    """Where an unmatched surface's belongings should go, if anywhere.

    Returns (surface, mode).  "coplanar" means the replacement lies in the
    same plane and covers the old outline, so a subsurface on it is still in
    the right place.  "plan" means it covers the same ground but at a
    different pitch -- good enough to inherit a construction, not good enough
    to move a window.
    """
    for surface, points in live:
        if contains(points, record["points"], COPLANAR_TOL_M, COPLANAR_TOL_M):
            return surface, "coplanar"

    if is_horizontal(record["points"]):
        for surface, points in live:
            if surface.surfaceType() != record["surface_type"]:
                continue
            if surface.handle() in used:
                continue
            if plan_contains(points, record["points"], COPLANAR_TOL_M):
                return surface, "plan"
    return None, None


def restore(model, captured, space_names_now):
    """Put names, constructions and openings back on the rebuilt surfaces.

    `space_names_now` maps the name a space had when it was captured to the
    name it has now, so a space that was reshaped and relabelled in one edit
    is still found.

    Must run after intersectSurfaces and matchSurfaces: the capture is split
    the way the old model was, and only after intersect is the new model split
    the same way.
    """
    report = {"surfaces": 0, "renamed": 0, "constructions": 0,
              "subsurfaces": 0, "inherited": 0, "adiabatic": 0,
              "reopened": 0, "lost": [], "notes": []}
    constructions = _constructions_by_name(model)
    spaces = {s.nameString(): s for s in model.getSpaces()}

    # Work out every match first, while the names still mean what they did.
    plans = []
    for old_name, records in captured.items():
        space = spaces.get(space_names_now.get(old_name, old_name))
        if space is None:
            report["notes"].append(
                "  %s is gone, so %d surface(s) could not be restored"
                % (old_name, len(records)))
            continue

        live = [(s, model_points(space, s)) for s in space.surfaces()]
        by_key = {}
        for surface, points in live:
            by_key.setdefault(surface_key(points), []).append(surface)

        matched, orphans = {}, []
        for key, record in records.items():
            found = by_key.get(key, [])
            if len(found) == 1:
                matched[key] = found[0]
            else:
                orphans.append(record)
        plans.append((space, records, matched, orphans, live))

    wanted = {record["name"] for records in captured.values()
              for record in records.values()}
    parked = _park_names(model, plans, wanted)
    for space, records, matched, _orphans, _live in plans:
        _assign_names(space, records, matched, report)
    _unpark(model, parked)

    for space, records, matched, orphans, live in plans:
        for key, record in records.items():
            surface = matched.get(key)
            if surface is None:
                continue
            report["surfaces"] += 1
            if record["construction"] in constructions:
                surface.setConstruction(constructions[record["construction"]])
                report["constructions"] += 1
            if (record["adiabatic"]
                    and not surface.adjacentSurface().is_initialized()):
                surface.setOutsideBoundaryCondition("Adiabatic")
                report["adiabatic"] += 1
            if record["subsurfaces"]:
                _restore_subsurfaces(model, space, surface, record,
                                     constructions, report)

        used = {s.handle() for s in matched.values()}
        for record in orphans:
            target, mode = _rehome(record, live, used)
            if target is None:
                for spec in record["subsurfaces"]:
                    host = _rehome_opening(spec, live)
                    if host is None:
                        report["lost"].append(
                            (record["name"], spec["name"],
                             "nothing replaced that surface"))
                        continue
                    _restore_subsurfaces(model, space, host,
                                         dict(record, subsurfaces=[spec]),
                                         constructions, report)
                    report["reopened"] += 1
                if record["construction"]:
                    report["notes"].append(
                        "  %s/%s carried %r and has no successor"
                        % (space.nameString(), record["name"],
                           record["construction"]))
                continue
            if (record["construction"] in constructions
                    and not target.construction().is_initialized()):
                target.setConstruction(constructions[record["construction"]])
                report["inherited"] += 1
            if mode == "coplanar" and record["subsurfaces"]:
                _restore_subsurfaces(model, space, target, record,
                                     constructions, report)
            elif record["subsurfaces"]:
                for spec in record["subsurfaces"]:
                    report["lost"].append(
                        (record["name"], spec["name"],
                         "the face that replaced it has a different pitch"))
    return report


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def report_restored(report, ambiguous):
    """Say what survived the rebuild, and be loud about what did not."""
    print("\ncarried across the rebuild:")
    print("  %d surface(s) recognised by their plan line and base"
          % report["surfaces"])
    print("  %d name(s), %d construction(s), %d opening(s)"
          % (report["renamed"], report["constructions"],
             report["subsurfaces"]))
    if report["inherited"]:
        print("  %d construction(s) inherited by a face that replaced one"
              % report["inherited"])
    if report["reopened"]:
        print("  %d opening(s) re-cut onto a wall that was shortened rather "
              "than moved" % report["reopened"])
    if report["adiabatic"]:
        print("  %d surface(s) put back to Adiabatic" % report["adiabatic"])

    if ambiguous:
        print("\n  %d surface(s) shared a key with a sibling and were left "
              "alone:" % sum(len(names) for _s, names in ambiguous))
        for space_name, names in ambiguous:
            print("    %-34s %s" % (space_name, ", ".join(names)))

    if report["lost"]:
        print("\nOPENINGS NOT CARRIED (%d) -- re-run fc_export_openings.py "
              "and apply_openings.py:" % len(report["lost"]))
        for surface_name, sub_name, why in report["lost"]:
            print("  %-22s %-24s %s" % (surface_name, sub_name, why))

    for note in report["notes"]:
        print(note)
