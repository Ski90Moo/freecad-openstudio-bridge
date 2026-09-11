# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Export drawn window and door outlines from a geometry FCStd.

Run under FreeCAD's bundled Python:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_export_openings.py \
        FloorplanTest-02.FCStd --out openings.json

Two ways to draw an opening, both read here:

**In an elevation sketch** (what fc_seed_openings.py creates).  Every closed
outline in a sketch flagged OS_OpeningSketch is an opening, so a whole facade
is one sketch -- which is how a person actually draws an elevation.  Type comes
from the size and sill height and is printed for review.

Two ways to overrule that.  For a whole sketch, start its Label with the
keyword.  For **one outline among many** -- the storefront in a row of doors --
select it in the sketch editor and run tag_opening.FCMacro, which pins the type
to that geometry as a named string extension.  There is no limit: one sketch
can hard-specify a glass door, an overhead door and an operable window at once,
and everything left untagged is still classified by shape.

**As its own object**, with the keyword at the front of its Label:

    WINDOW ...      -> FixedWindow        DOOR ...       -> Door
    OPWINDOW ...    -> OperableWindow     GLASSDOOR ...  -> GlassDoor
    SKYLIGHT ...    -> Skylight           OVERHEAD ...   -> OverheadDoor

The host surface is found geometrically either way -- the outline must be
coplanar with, and inside, exactly one imported surface -- so there is nothing
to keep in sync by hand.

**Walls set back from the facade** are handled by projection, so they can be
drawn on the facade's sketch rather than needing one of their own.  An
elevation is a parallel projection: a lean-to, a reveal or a recessed bay
appears on the sheet in the same place as the facade in front of it, and a
person tracing it naturally draws it there.  When an outline is coplanar with
no surface, it is projected back along the drawing's own normal -- the exact
inverse of how the elevation was made -- onto the nearest wall facing the same
way.  Every projected opening is reported with the distance it moved, because
that is the one thing about it a person needs to check.
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import Part  # noqa: E402

import fcbridge  # noqa: E402

MM_PER_M = 1000.0

KINDS = [
    ("OPWINDOW", "OperableWindow"),
    ("GLASSDOOR", "GlassDoor"),
    ("OVERHEAD", "OverheadDoor"),
    ("SKYLIGHT", "Skylight"),
    ("WINDOW", "FixedWindow"),
    ("DOOR", "Door"),
]

# How far out of the host plane a drawn vertex may sit.  Sketches attached to
# a face land exactly on it; free-drawn geometry needs a little slack.
COPLANAR_TOL_MM = 1.0
INSIDE_TOL_MM = 1.0

# Projection onto a wall set back from the drawing plane.  The candidate must
# face the same way as the drawing to within NORMAL_TOL -- a wall facing the
# other way is on the far side of the building, not behind this one -- and the
# nearest hit wins.  Two hits within TIE_MM of each other are ambiguous and get
# reported rather than guessed.
PROJECT_NORMAL_TOL = 1e-4
MAX_PROJECT_MM = 10000.0
PROJECT_TIE_MM = 1.0

# Classification, for outlines whose sketch does not name a type.  Every
# threshold is a shape distinction a person would make at a glance, and all
# of them are measured against the host wall's own floor line -- not global
# z -- so a door on the second storey is still a door.
SILL_ON_FLOOR_M = 0.20
OVERHEAD_MIN_H_M = 3.00
GLASSDOOR_MIN_W_M = 1.70
WINDOW_MIN_SILL_M = 0.45
MIN_W_M = 0.30
MIN_H_M = 0.30


def classify_label(label):
    upper = (label or "").strip().upper()
    for keyword, kind in KINDS:
        if upper.startswith(keyword):
            return kind, (label or "").strip()[len(keyword):].strip(" -_:")
    return None, None


def classify_shape(width, height, sill):
    """Opening kind from its size and its sill above the host's floor line."""
    if width < MIN_W_M or height < MIN_H_M:
        return None, "only %.2f x %.2f m" % (width, height)
    if sill <= SILL_ON_FLOOR_M:
        if height >= OVERHEAD_MIN_H_M:
            return "OverheadDoor", ""
        if width >= GLASSDOOR_MIN_W_M:
            return "GlassDoor", ""
        return "Door", ""
    if sill >= WINDOW_MIN_SILL_M:
        return "FixedWindow", ""
    return None, ("sill %.2f m above the floor is neither a door (<= %.2f) "
                  "nor a window (>= %.2f)"
                  % (sill, SILL_ON_FLOOR_M, WINDOW_MIN_SILL_M))


def dumps_compact_vertices(payload):
    text = json.dumps(payload, indent=2)
    return re.sub(
        r"\[\s*\n\s*(-?\d+\.?\d*),\s*\n\s*(-?\d+\.?\d*),\s*\n\s*"
        r"(-?\d+\.?\d*)\s*\n\s*\]",
        r"[\1, \2, \3]",
        text,
    )


def host_surfaces(doc):
    """Imported OpenStudio surfaces, as (object, planar face) pairs."""
    out = []
    for obj in doc.Objects:
        if not getattr(obj, "OS_SurfaceName", None):
            continue
        if getattr(obj, "OS_SubSurfaceType", None):
            continue
        faces = getattr(obj.Shape, "Faces", [])
        if faces:
            out.append((obj, faces[0]))
    return out


# Name of the string extension that hard-specifies one outline's type.  It is
# the same name as the property on the imported subsurface objects on purpose:
# that is the field a person reaches for, and this is the place it belongs --
# on the drawn geometry, which is the input, rather than on the model's
# reflection of it, which is not.
ELEMENT_KIND_EXT = "OS_SubSurfaceType"

# Text accepted as a type, either the keyword or the OpenStudio name:
# "GLASSDOOR" and "GlassDoor" both work.
KIND_BY_TEXT = {}
for _keyword, _kind in KINDS:
    KIND_BY_TEXT[_keyword] = _kind
    KIND_BY_TEXT[_kind.upper()] = _kind

# Name of the string extension carrying an outline's stable identity.  It is
# what keeps an OpenStudio handle alive across a retype: apply matches on it
# and updates the subsurface in place instead of deleting and rebuilding, so
# anything referencing that handle -- a shading control, a frame and divider,
# an interzone pairing -- still points at the same object afterwards.
OPENING_ID_EXT = "OS_OpeningId"

# The join between Shape.Wires and the geometry list, and the id minting on
# top of it, live in fcbridge so the shading exporter anchors the same way.
MID_KEY_PLACES = fcbridge.MID_KEY_PLACES
_mid_key = fcbridge.mid_key
_edge_mid = fcbridge.edge_mid
_element_index = fcbridge.element_index
_element_values = fcbridge.element_values
_wire_values = fcbridge.wire_values


def element_kinds(sketch):
    """{edge midpoint: type} for every element carrying a hard-specified type.

    FreeCAD lets a named string ride along on one piece of sketch geometry
    (Part::GeometryStringExtension), and it is document data: it survives a
    save, a drag, a new constraint, and it follows its element when something
    else in the sketch is deleted.  So a type can be pinned to one outline and
    a single sketch can hard-specify as many different types as it has
    outlines.  tag_opening.FCMacro is what writes them.

    Shape.Wires is what an opening is; the tag lives on the geometry list.
    Midpoints join the two -- they are unique per edge, unlike the vertices a
    closed outline shares with its neighbours.
    """
    lookup, problems = {}, []
    for midpoint, text in _element_values(sketch, ELEMENT_KIND_EXT).items():
        resolved = KIND_BY_TEXT.get(text.upper())
        if resolved is None:
            problems.append("%s: %s reads %r, which is no known type -- one "
                            "of %s"
                            % (sketch.Label, ELEMENT_KIND_EXT, text,
                               ", ".join(sorted({k for _, k in KINDS}))))
            continue
        lookup[midpoint] = resolved
    return lookup, problems


def opening_sketches(doc):
    for obj in doc.Objects:
        if obj.TypeId != "Sketcher::SketchObject":
            continue
        if getattr(obj, "OS_OpeningSketch", False) or classify_label(obj.Label)[0]:
            yield obj


def ensure_opening_ids(doc):
    """Give every drawn opening outline a stable id.  See fcbridge.ensure_ids."""
    return fcbridge.ensure_ids(opening_sketches(doc), OPENING_ID_EXT)


def wire_kind(wire, lookup):
    """(kind_or_None, None), or (None, [kinds]) when one outline has two.

    A tag on some edges and not others is not ambiguous -- a person tags an
    outline by selecting it, and a rectangle drawn with the rectangle tool is
    four elements.  Only two *different* types on one outline is a real
    conflict.
    """
    kinds = set()
    for edge in wire.Edges:
        found = lookup.get(_mid_key(_edge_mid(edge)))
        if found is not None:
            kinds.add(found)
    if len(kinds) > 1:
        return None, sorted(kinds)
    return (kinds.pop() if kinds else None), None


def outlines(doc):
    """Each outline: (source, tag, kind_or_None, typed_by, id, points).

    A sketch contributes one entry per closed wire, so an elevation drawn as
    a single sketch comes back as the openings it contains.  Anything else
    contributes its first wire, as before.
    """
    found, problems = [], []
    for obj in doc.Objects:
        # Imported geometry is reference, not input.  Subsurfaces already in
        # the model arrive labelled with their OpenStudio names -- "Door West
        # 26", "OverheadDoor South 07" -- which would otherwise read as
        # freshly drawn openings and get applied a second time.
        if getattr(obj, "OS_SurfaceName", None) or \
                getattr(obj, "OS_SubSurfaceType", None):
            continue
        # A group is not a drawing surface, even though FreeCAD hands it a
        # Shape -- a Compound of its children.  A group labelled with a
        # keyword ("DOOR Storage", say) would otherwise re-offer everything
        # inside it as freshly drawn outlines.
        if obj.TypeId != "Sketcher::SketchObject" and \
                not obj.isDerivedFrom("Part::Feature"):
            continue
        label_kind, suffix = classify_label(obj.Label)
        is_sketch = obj.TypeId == "Sketcher::SketchObject"
        opening_sketch = bool(getattr(obj, "OS_OpeningSketch", False))

        if is_sketch and (opening_sketch or label_kind):
            tag = suffix or getattr(obj, "OS_Facade", "") or obj.Label
            wires = getattr(obj.Shape, "Wires", [])
            if not wires:
                continue
            lookup, complaints = element_kinds(obj)
            problems.extend(complaints)
            ids = _element_values(obj, OPENING_ID_EXT)
            for i, wire in enumerate(wires, start=1):
                if not wire.isClosed():
                    problems.append(
                        "%s wire %d: not a closed outline" % (obj.Label, i))
                    continue
                kind, source = label_kind, "label" if label_kind else ""
                if lookup:
                    pinned, conflicting = wire_kind(wire, lookup)
                    if conflicting:
                        problems.append(
                            "%s wire %d: its edges are tagged %s -- one "
                            "outline cannot be two types"
                            % (obj.Label, i, " and ".join(conflicting)))
                        continue
                    if pinned:
                        kind, source = pinned, "tag"
                marks = _wire_values(wire, ids)
                found.append((obj, tag, kind, source,
                              marks[0] if len(marks) == 1 else "",
                              [v.Point for v in wire.OrderedVertexes]))
        elif label_kind and not is_sketch:
            shape = getattr(obj, "Shape", None)
            if shape is None:
                continue
            wires = getattr(shape, "Wires", [])
            points = ([v.Point for v in wires[0].OrderedVertexes] if wires
                      else [v.Point for v in getattr(shape, "Vertexes", [])])
            found.append((obj, suffix or obj.Name, label_kind, "label",
                          str(getattr(obj, "OS_OpeningId", "") or ""), points))
    return found, problems


def find_host(points, hosts):
    """The single imported surface this outline belongs to.

    Returns ((obj, face), None) or (None, why) -- an opening floating off its
    wall is a silent-wrong-answer risk, so it fails loudly rather than
    snapping to the nearest candidate.
    """
    matches = []
    for obj, face in hosts:
        surface = face.Surface
        normal = getattr(surface, "Axis", None)
        if normal is None:
            continue
        origin = face.Vertexes[0].Point
        planar = all(
            abs((p - origin).dot(normal)) <= COPLANAR_TOL_MM for p in points)
        if not planar:
            continue
        if all(face.isInside(p, INSIDE_TOL_MM, True) for p in points):
            matches.append((obj, face))

    if not matches:
        return None, "no imported surface is coplanar with and contains it"
    if len(matches) > 1:
        names = ", ".join(o.OS_SurfaceName for o, _ in matches[:4])
        return None, "sits inside %d surfaces (%s)" % (len(matches), names)
    return matches[0], None


def face_normal(face):
    """The face's outward normal, honouring a reversed orientation.

    Surface.Axis alone points whichever way the underlying plane was built;
    the imported surfaces carry OpenStudio's outward sense in Orientation.
    """
    normal = FreeCAD.Vector(face.Surface.Axis)
    if face.Orientation == "Reversed":
        normal = normal.negative()
    return normal


def drawing_normal(obj, points):
    """The outward normal of the plane an outline was drawn on.

    For an attached opening sketch that is the facade's own outward normal,
    because fc_seed_openings.py builds the sketch frame with local +Z outward.
    Returns (normal, certain); a free-drawn outline's winding does not say
    which side is out, so the caller tries both directions for those.
    """
    if obj.TypeId == "Sketcher::SketchObject":
        normal = obj.Placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        if normal.Length > 1e-9:
            normal.normalize()
            return normal, True

    normal = FreeCAD.Vector(0, 0, 0)  # Newell, so it works on any polygon
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        normal.x += (point.y - nxt.y) * (point.z + nxt.z)
        normal.y += (point.z - nxt.z) * (point.x + nxt.x)
        normal.z += (point.x - nxt.x) * (point.y + nxt.y)
    if normal.Length < 1e-9:
        return None, False
    normal.normalize()
    return normal, False


def project_host(points, hosts, normal, certain):
    """Host an outline drawn on the elevation plane of a wall set back from it.

    Returns ((obj, face), moved_points, offset_mm, None) or
    (None, None, 0.0, why).  Offset is signed along `normal`, so a wall behind
    the drawing gives a negative one.
    """
    directions = [normal] if certain else [normal, normal.negative()]
    candidates = []
    for direction in directions:
        for obj, face in hosts:
            if face_normal(face).dot(direction) < 1.0 - PROJECT_NORMAL_TOL:
                continue
            offset = (face.Vertexes[0].Point - points[0]).dot(direction)
            if abs(offset) > MAX_PROJECT_MM:
                continue
            moved = [p + direction * offset for p in points]
            if all(face.isInside(p, INSIDE_TOL_MM, True) for p in moved):
                candidates.append((abs(offset), obj, face, moved, offset))

    if not candidates:
        return None, None, 0.0, \
            "and nothing it projects onto along the drawing's normal"
    candidates.sort(key=lambda c: c[0])
    if len(candidates) > 1 and \
            candidates[1][0] - candidates[0][0] < PROJECT_TIE_MM:
        names = ", ".join(c[1].OS_SurfaceName for c in candidates[:4])
        return None, None, 0.0, \
            ("and it projects onto %d surfaces the same distance away (%s)"
             % (len(candidates), names))
    _, obj, face, moved, offset = candidates[0]
    return (obj, face), moved, offset, None


def measure(points, face):
    """(width, height, sill) in metres, in the host wall's own elevation.

    Height and sill are measured from the bottom of the host face rather than
    from global zero, so the same rule reads a door on the second storey the
    same way it reads one at grade.
    """
    normal = face.Surface.Axis
    horizontal = FreeCAD.Vector(normal.x, normal.y, normal.z).cross(
        FreeCAD.Vector(0, 0, 1))
    if horizontal.Length < 1e-9:
        horizontal = FreeCAD.Vector(1, 0, 0)
    horizontal.normalize()
    along = [p.dot(horizontal) for p in points]
    zs = [p.z for p in points]
    floor = min(v.Point.z for v in face.Vertexes)
    return ((max(along) - min(along)) / MM_PER_M,
            (max(zs) - min(zs)) / MM_PER_M,
            (min(zs) - floor) / MM_PER_M)


MATCH_TOL_M = 1e-5


def host_normal(doc, name):
    """Outward normal of an imported surface, for undoing a display offset."""
    for obj in doc.Objects:
        if getattr(obj, "OS_SurfaceName", None) != name:
            continue
        faces = getattr(obj.Shape, "Faces", [])
        if not faces:
            return None
        normal = FreeCAD.Vector(faces[0].Surface.Axis)
        if faces[0].Orientation == "Reversed":
            normal = normal.negative()
        return normal
    return None


def _same_points(got, want, tol):
    """True when two vertex lists are the same points, in any order."""
    if len(got) != len(want):
        return False
    left = list(want)
    for point in got:
        for i, other in enumerate(left):
            if all(abs(a - b) <= tol for a, b in zip(point, other)):
                del left[i]
                break
        else:
            return False
    return True


def subsurface_outcomes(doc, openings, tol=MATCH_TOL_M):
    """{imported subsurface: the opening it would become on the next apply}.

    The objects under "Subsurfaces from the model" are the model's reflection,
    written by import and read by nothing -- so retyping an outline leaves them
    showing the old type with no sign anything happened.  This is the join that
    lets a macro mark them pending: match each one back to the outline that
    produced it, by vertices, once the display offset is off.

    Vertices are compared rather than names because the name is what changes:
    a retyped `Door South 10` comes back as `GlassDoor South 10`.  The stored
    opening vertices are post-projection, which is what apply wrote, so an
    opening projected onto a setback wall matches here like any other.
    """
    normals = {}
    outcomes = {}
    for obj in doc.Objects:
        if not getattr(obj, "OS_SubSurfaceType", None):
            continue
        host = getattr(obj, "OS_HostSurface", "")
        if host not in normals:
            normals[host] = host_normal(doc, host)
        normal = normals[host]
        offset = getattr(obj, "OS_DisplayOffset_mm", 0.0)
        shift = normal * offset if (offset and normal is not None) \
            else FreeCAD.Vector(0, 0, 0)
        got = [((v.Point.x - shift.x) / MM_PER_M,
                (v.Point.y - shift.y) / MM_PER_M,
                (v.Point.z - shift.z) / MM_PER_M)
               for v in obj.Shape.Vertexes]
        for opening in openings:
            if _same_points(got, [tuple(v) for v in opening["vertices"]], tol):
                outcomes[obj] = opening
                break
    return outcomes


def classify_document(doc):
    """(openings, rows, projected, problems, hosts, drawn) for one document.

    Split out of main() so review_openings.FCMacro can show the same table
    inside FreeCAD, against the open document, without writing anything.  The
    review is the step that matters -- "human decides, machine measures" only
    works if the human can see the measurements -- and requiring a terminal to
    see them put that behind a door.  One implementation, so the preview and
    the export can never disagree about what a thing is.
    """
    hosts = host_surfaces(doc)
    drawn, problems = outlines(doc)

    openings = []
    rows = []
    projected = []
    counter = {}
    for obj, tag, label_kind, typed_by, opening_id, points in drawn:
        if len(points) < 3:
            problems.append("%s: only %d vertices" % (obj.Label, len(points)))
            continue

        found, why = find_host(points, hosts)
        offset = 0.0
        if found is None:
            normal, certain = drawing_normal(obj, points)
            if normal is not None:
                found, moved, offset, gone = project_host(
                    points, hosts, normal, certain)
                if found is None:
                    why = "%s, %s" % (why, gone)
                else:
                    points = moved
        if found is None:
            # The sketch name alone is not enough to find it again: a facade
            # holds a dozen outlines and they all report the same label.  Say
            # where on the wall it is and how big, which is what you look for
            # when you open the sketch.
            xs = [p.x / MM_PER_M for p in points]
            zs = [p.z / MM_PER_M for p in points]
            problems.append(
                "%s: an outline %.2f x %.2f m at %.2f m along, %.2f m up: %s"
                % (obj.Label, max(xs) - min(xs), max(zs) - min(zs),
                   min(xs), min(zs), why))
            continue
        host_obj, face = found

        width, height, sill = measure(points, face)
        if label_kind:
            kind, why = label_kind, ""
        else:
            kind, why = classify_shape(width, height, sill)
        if kind is None:
            problems.append(
                "%s at %.2f m along, sill %.2f m: %s -- put it in a sketch "
                "labelled WINDOW/DOOR/... to say what it is"
                % (obj.Label, min(p.x for p in points) / MM_PER_M, sill, why))
            continue

        counter[tag] = counter.get(tag, 0) + 1
        name = "%s %s %02d" % (kind, tag, counter[tag])
        rows.append((name, kind, width, height, sill,
                     host_obj.OS_SurfaceName,
                     getattr(host_obj, "OS_SpaceName", ""),
                     typed_by or "shape",
                     offset))
        if offset:
            projected.append((name, offset / MM_PER_M,
                              host_obj.OS_SurfaceName,
                              getattr(host_obj, "OS_SpaceName", "")))
        openings.append({
            "name": name,
            "id": opening_id,
            "subsurface_type": kind,
            "host_surface": host_obj.OS_SurfaceName,
            "host_space": getattr(host_obj, "OS_SpaceName", ""),
            "source_object": obj.Name,
            "width_m": round(width, 4),
            "height_m": round(height, 4),
            "sill_above_floor_m": round(sill, 4),
            "projected_m": round(abs(offset) / MM_PER_M, 4) if offset else 0.0,
            "vertices": [[round(p.x / MM_PER_M, 6),
                          round(p.y / MM_PER_M, 6),
                          round(p.z / MM_PER_M, 6)] for p in points],
        })

    return openings, rows, projected, problems, hosts, drawn


def stale_geometry(doc):
    """Complain if OS_Geometry predates the plan as it is drawn now.

    An opening is hosted by testing its outline against the imported
    surfaces, and those are a copy of the model.  Move a wall, rebuild the
    model, and that copy shows walls where they used to be: an outline then
    finds no host, or -- the quiet one -- finds a wall whose name has since
    been given to a different piece of the building, and the apply attaches
    the opening to it without complaint.

    So this is checked before the outlines are, because otherwise the error
    you get is "no imported surface is coplanar with and contains it", which
    is true, unhelpful, and sends you looking at the drawing.
    """
    sketches = fcbridge.story_sketches(doc)
    if not sketches:
        return []                 # no plan here to compare against
    stamped = fcbridge.stored_plan_digest(doc)
    if not stamped:
        print("\nthe imported geometry carries no plan stamp, so whether it "
              "is current\ncannot be checked -- run dump + import once and it "
              "will be from then on.")
        return []
    if stamped == fcbridge.plan_digest(sketches):
        return []
    return ["the imported OS_Geometry was taken from a different plan than "
            "the one drawn now (stamp %s, plan %s) -- run dump + import to "
            "refresh it before exporting openings, or the hosts these "
            "outlines name may no longer be the walls they sit on"
            % (stamped, fcbridge.plan_digest(sketches))]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-problems", action="store_true")
    ap.add_argument("--no-mint-ids", action="store_true",
                    help="do not give un-identified outlines an id, and do "
                         "not write to the document.  Their subsurfaces get "
                         "rebuilt rather than updated, so their OpenStudio "
                         "handles change on every apply.")
    args = ap.parse_args()

    doc = FreeCAD.openDocument(os.path.abspath(args.fcstd))

    # Before anything is classified: every outline needs an identity, and it
    # has to live in the document, so it survives to the next export.
    minted, id_problems = (0, []) if args.no_mint_ids else ensure_opening_ids(doc)
    if minted:
        doc.recompute()
        fcbridge.save_document(doc)
        print("minted an id for %d outline(s) and wrote them into %s"
              % (minted, os.path.basename(args.fcstd)))
        print("  Revert in FreeCAD before editing it further.")

    openings, rows, projected, problems, hosts, drawn = \
        classify_document(doc)
    problems = id_problems + problems + stale_geometry(doc)
    print("imported surfaces available as hosts: %d" % len(hosts))
    print("outlines drawn: %d" % len(drawn))
    without = [o["name"] for o in openings if not o["id"]]
    if without:
        print("  %d without an id (handles will not be stable for these): %s"
              % (len(without), ", ".join(without[:6])))

    if rows:
        print("\n%-28s %-14s %6s %6s %6s  %-11s %-24s %s"
              % ("name", "type", "w m", "h m", "sill", "host", "space",
                 "typed by"))
        for row in rows:
            print("%-28s %-14s %6.2f %6.2f %6.2f  %-11s %-24s %s%s"
                  % (row[0], row[1], row[2], row[3], row[4], row[5],
                     row[6][:24], row[7], "  projected" if row[8] else ""))

    if projected:
        print("\nprojected onto a wall set back from the drawing (%d) -- an\n"
              "elevation is a parallel projection, so these were traced on the\n"
              "facade in front of them.  Check the distance is the setback you\n"
              "expect:" % len(projected))
        for name, distance, host, space in projected:
            print("  %-28s %6.3f m %-8s %-11s %s"
                  % (name, abs(distance),
                     "back" if distance < 0 else "forward", host, space))

    by_kind = {}
    for o in openings:
        by_kind[o["subsurface_type"]] = by_kind.get(o["subsurface_type"], 0) + 1
    print("\nopenings found: %d%s"
          % (len(openings),
             (" (" + ", ".join("%s %d" % kv
                               for kv in sorted(by_kind.items())) + ")")
             if by_kind else ""))

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)
        if not args.allow_problems:
            sys.exit(
                "\nRefusing to write %s.\nEvery opening outline must be closed "
                "and lie flat on exactly one wall\nface, and the wall it lands "
                "on has to be the one the model holds now.\nFix the listed "
                "outlines, refresh the geometry if it is out of date, or\n"
                "re-run with --allow-problems." % args.out)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dumps_compact_vertices({
            "schema_version": 1,
            "source_document": os.path.basename(args.fcstd),
            "generated_utc": datetime.now(timezone.utc)
                                     .replace(microsecond=0).isoformat(),
            "units": "meters",
            "openings": openings,
        }))
        fh.write("\n")
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main()
