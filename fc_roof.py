# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Roof geometry: give the top of the building its real shape.

Imported by fc_export_floorplan.py under FreeCAD's Python.  Everything here
reads the document; nothing writes to the model.

Two methods, chosen per roof object with OS_RoofMethod:

  Extend   The roof describes where the top of the building is.  Every space
           that currently tops out at the roof's lowest point is carved
           against it, so its ceiling follows the slope and its walls grow
           into trapezoids.  The building gains no space, only volume.

  Attic    The roof is a solid sitting on top of the building.  It becomes a
           new space in its own right, and the spaces below keep their flat
           ceilings -- which matchSurfaces then pairs with the attic floor,
           turning them from exterior roofs into interior ceilings.

Which one is right is an energy question, not a geometry one.  Extend puts the
roof construction directly against conditioned air, so the sloped area is the
loss surface.  Attic inserts an unconditioned buffer, which is what you want
when there is a real ventilated cavity, a plenum, or ducts running above the
ceiling.  For a low-slope roof over an open warehouse Extend is usually the
honest model; for a residential-style truss space Attic is.

The carve is done with an OCC boolean rather than by lifting each vertex to
the roof plane, because the boolean is the only version that survives a roof
with more than one plane.  A hip, a gable whose ridge crosses a room, a
clerestory step: all of them produce a space with several roof faces and
walls that gain a vertex where the planes meet, and none of that needs special
handling here.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import Part  # noqa: E402
import fcbridge as fb  # noqa: E402

METHOD_PROP = "OS_RoofMethod"
STORY_PROP = "OS_StoryName"
SPACE_NAME_PROP = "OS_SpaceName"

IGNORE, EXTEND, ATTIC = "Ignore", "Extend", "Attic"
METHODS = [IGNORE, EXTEND, ATTIC]

ROOF_PROPS = (
    (METHOD_PROP, "App::PropertyEnumeration", METHODS,
     "How this roof shape becomes model geometry: Extend raises the spaces "
     "below up to it, Attic makes it a space of its own"),
    (STORY_PROP, "App::PropertyString", "Attic",
     "Building story the attic space is filed under (Attic method only)"),
    (SPACE_NAME_PROP, "App::PropertyString", "",
     "Attic space name as 'NUMBER | Name'; blank derives it from the object "
     "label (Attic method only)"),
)

# How far below the roof the carving solid reaches.  Only has to clear the
# lowest floor in the building; 40 m does that for anything this bridge is
# aimed at, and an over-long extrusion costs nothing.
DROP_MM = 40000.0

# A space is a candidate for Extend when its ceiling sits this close to the
# roof's lowest point.  Loose enough to absorb a plan drawn to the nearest
# millimetre, tight enough that a storey below is never caught by accident.
BASE_TOL_M = 0.05

# Matches how OpenStudio classifies a surface from its outward normal, so the
# type recorded here and the type the model ends up with agree.
TILT_LIMIT = 0.7071

# Below this a carved face is a sliver from the boolean, not a real surface.
MIN_FACE_AREA_MM2 = 100.0

# Fraction of its own footprint a space may lose to the roof outline before
# the result is called partial cover rather than rounding.
COVER_TOL = 1e-4


class RoofError(Exception):
    """A roof the exporter will not guess about."""


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def has_shape(obj):
    """True for an object carrying real geometry we can carve with.

    A group also reports a Shape -- a compound of its children -- so testing
    for the attribute alone would let a folder masquerade as a roof, which is
    exactly how four canopies once became eight.
    """
    if obj.TypeId in ("App::DocumentObjectGroup",
                      "App::DocumentObjectGroupPython"):
        return False
    if not obj.isDerivedFrom("Part::Feature"):
        return False
    shape = getattr(obj, "Shape", None)
    return shape is not None and not shape.isNull()


def method_of(obj):
    """The roof method this object declares, or None if it declares none."""
    value = getattr(obj, METHOD_PROP, None)
    if value is None:
        return None
    value = str(value)
    return value if value in METHODS else None


def roof_objects(doc):
    """Active roof objects, in document order.

    Objects inside a PartDesign Body are skipped in favour of the Body's own
    tip shape when the Body itself is tagged, so tagging either the Body or
    the feature works and tagging both does not count the roof twice.
    """
    tagged = [o for o in doc.Objects
              if has_shape(o) and method_of(o) not in (None, IGNORE)]
    inside_tagged = set()
    for obj in tagged:
        for child in getattr(obj, "Group", []) or []:
            inside_tagged.add(child.Name)
    return [o for o in tagged if o.Name not in inside_tagged]


def ensure_roof_props(obj):
    """Add the OS_* roof properties to an object if they are missing.

    Returns True when anything was added; the caller decides whether to save.
    """
    added = False
    for name, ptype, default, doc in ROOF_PROPS:
        if hasattr(obj, name):
            continue
        obj.addProperty(ptype, name, "OpenStudio", doc)
        setattr(obj, name, default)
        if name == METHOD_PROP:
            # An enumeration needs its list before a value will stick.
            setattr(obj, name, IGNORE)
        added = True
    if added:
        # addProperty marks the object for recompute.  Nothing geometric
        # changed, and a PartDesign Body showing a touch mark after being
        # offered a dropdown reads as "this shape is stale", which it is not.
        try:
            obj.purgeTouched()
        except AttributeError:              # pragma: no cover - old FreeCAD
            pass
    return added


def derived_space_name(obj):
    """'Attic-001' -> 'Attic'.  FreeCAD's duplicate suffix is not a name."""
    label = (obj.Label or obj.Name).strip()
    while label and label[-1].isdigit():
        label = label[:-1]
    return label.rstrip("-_ ") or obj.Name


def space_identity(obj):
    """(room_number, name) for an attic space."""
    raw = (getattr(obj, SPACE_NAME_PROP, "") or "").strip()
    if not raw:
        return "", derived_space_name(obj)
    if "|" in raw:
        num, _, name = raw.partition("|")
        return num.strip(), name.strip() or derived_space_name(obj)
    return "", raw


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------

def base_story(sketches):
    """The story the roof is measured against: the lowest one exported.

    Model coordinates are each story sketch's own frame plus its declared
    elevation, and the stories are assumed to share that frame in plan.  The
    roof is a single object spanning all of them, so it needs one frame to be
    read in, and the lowest story is the one whose origin the building sits
    on.
    """
    if not sketches:
        raise RoofError("no story sketches, so there is no frame to read the "
                        "roof in")
    return min(sketches, key=fb.story_elevation_m)


def to_model_mm(shape, sketch):
    """A copy of a global shape in model coordinates, still in millimetres.

    Model z is the story's declared elevation, which need not equal where the
    sketch happens to sit in the document -- a plan stacked at 3378.2 mm may
    declare 3.38 m.  The declaration wins.
    """
    out = fb.local_shape(shape, sketch)
    out.translate(FreeCAD.Vector(
        0.0, 0.0, fb.story_elevation_m(sketch) * fb.MM_PER_M))
    return out


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def upward_faces(shape):
    """The faces of a roof shape that actually shed water.

    A solid attic carries a floor and gable ends as well as roof, and only
    the roof is loss surface -- counting the floor too would report an attic
    as having twice the roof it has.  A bare extents face has no inside for
    an outward normal to point away from, so it is roof by definition.
    """
    if shape.Solids:
        entries, _volume = oriented_faces(shape.Solids[0])
        return [(f, n) for f, n, _pts in entries if n.z > TILT_LIMIT]
    return [(f, f.normalAt(0, 0)) for f in shape.Faces
            if abs(f.normalAt(0, 0).z) > TILT_LIMIT]


def below_solid(shapes):
    """Everything under the roof, as one solid to intersect spaces with.

    Each roof face is dropped straight down and the results fused, so a roof
    made of several planes -- hips, a gable, a clerestory step -- gives a
    single carving tool with no seams in it.
    """
    solid = None
    for shape in shapes:
        for face, _normal in upward_faces(shape):
            piece = face.extrude(FreeCAD.Vector(0.0, 0.0, -DROP_MM))
            solid = piece if solid is None else solid.fuse(piece)
    if solid is None:
        raise RoofError(
            "this roof has no upward-facing surface to carve with -- it is "
            "edge-on or upside down")
    return solid.removeSplitter()


def surface_normal(face):
    """Unit normal of a face, read where the parameters are always valid.

    (0, 0) need not lie in a trimmed face's parameter range, so the middle of
    that range is used instead.

    FreeCAD's normalAt already accounts for the face's orientation within its
    shell, so this is the outward normal for a face of a solid and must not be
    corrected by the Orientation flag a second time.  Measured, because the
    two disagree often enough to matter: on a carved space every face reports
    Reversed while normalAt is already outward, and on a PartDesign Body the
    flags come back mixed and correcting by them makes the set incoherent.
    """
    u0, u1, v0, v1 = face.ParameterRange
    normal = FreeCAD.Vector(face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0))
    normal.normalize()
    return normal


def polyhedron_volume(faces):
    """Volume enclosed by wound polygons, by the divergence theorem.

    Positive when they wind outward, negative when they wind inward, and
    neither when a face is missing or flipped on its own.

    Measured from the middle of the point cloud rather than from the model
    origin, and that is not cosmetic.  A face lying in a plane through the
    reference point contributes nothing, so with the origin as reference the
    sum cannot see a missing wall at x = 0 or a missing ground floor at
    z = 0 -- and the model origin is a corner of the building, so those are
    the commonest faces there are.
    """
    points = [p for face in faces for p in face]
    if not points:
        return 0.0
    count = float(len(points))
    middle = FreeCAD.Vector(sum(p.x for p in points) / count,
                            sum(p.y for p in points) / count,
                            sum(p.z for p in points) / count)
    total = 0.0
    for face in faces:
        origin = face[0] - middle
        for i in range(1, len(face) - 1):
            total += origin.dot((face[i] - face[0])
                                .cross(face[i + 1] - face[0]))
    return total / 6.0


def newell(points):
    """Normal of a polygon, from its winding."""
    normal = FreeCAD.Vector(0.0, 0.0, 0.0)
    count = len(points)
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        normal.x += (a.y - b.y) * (a.z + b.z)
        normal.y += (a.z - b.z) * (a.x + b.x)
        normal.z += (a.x - b.x) * (a.y + b.y)
    return normal


def classify(normal):
    if normal.z < -TILT_LIMIT:
        return "Floor"
    if normal.z > TILT_LIMIT:
        return "RoofCeiling"
    return "Wall"


def face_vertices(face, normal):
    """Outer boundary as points wound to match `normal`."""
    points = _weld3([v.Point for v in face.OuterWire.OrderedVertexes])
    if len(points) < 3:
        return []
    if newell(points).dot(normal) < 0:
        points.reverse()
    return points


def _weld3(points, tol=fb.WELD_TOL_MM):
    out = []
    for point in points:
        if out and (out[-1] - point).Length < tol:
            continue
        out.append(point)
    while len(out) > 1 and (out[0] - out[-1]).Length < tol:
        out.pop()
    return out


def oriented_faces(solid):
    """[(face, outward normal, outward-wound points)] plus the volume they
    enclose.

    normalAt gives the outward direction for each face on its own; the volume
    is what confirms the set as a whole.  A shell is coherent -- every face
    agreeing with every other -- so the polygons are either all outward or all
    inward, and the sign of the volume they enclose settles which in one
    number.  Anything the sign cannot explain is a real defect, and the caller
    treats it as one.

    Getting this wrong is silent: a flipped face gives OpenStudio a space with
    a negative volume and no complaint.  It typed the floor and ceiling of
    three rooms as walls before the volume check was here to catch it.
    """
    entries = []
    for face in solid.Faces:
        normal = surface_normal(face)
        entries.append([face, normal, face_vertices(face, normal)])

    volume = polyhedron_volume([e[2] for e in entries if len(e[2]) >= 3])
    if volume < 0:
        for entry in entries:
            entry[1] = entry[1] * -1.0
            entry[2] = list(reversed(entry[2]))
        volume = -volume
    return entries, volume


def solid_surfaces(solid):
    """[{type, vertices}] for every face of a solid, in metres.

    Vertices come out in model coordinates, wound so the normal points out of
    the space -- which is what OpenStudio means by a surface's outward normal
    and what it derives Floor / Wall / RoofCeiling from.

    Checked before returning by comparing the volume the polygons enclose
    with the solid's own.  A flipped or dropped face changes that number and
    nothing else does, so this is the one assertion that covers the whole
    conversion.
    """
    entries, volume = oriented_faces(solid)
    if abs(volume - solid.Volume) > max(1.0, solid.Volume * 1e-6):
        raise RoofError(
            "the faces of this solid enclose %.3f m3 but the solid is %.3f "
            "m3 -- a face is inside out or missing"
            % (volume / 1e9, solid.Volume / 1e9))

    surfaces, dropped = [], 0
    for face, normal, points in entries:
        if face.Area < MIN_FACE_AREA_MM2 or len(points) < 3:
            dropped += 1
            continue
        surfaces.append({
            "type": classify(normal),
            "vertices": [[round(p.x / fb.MM_PER_M, 6),
                          round(p.y / fb.MM_PER_M, 6),
                          round(p.z / fb.MM_PER_M, 6)] for p in points],
        })
    return surfaces, dropped


def prism(polygon_m, floor_m, top_m):
    """A space's flat-topped extrusion, in model millimetres."""
    base = [FreeCAD.Vector(x * fb.MM_PER_M, y * fb.MM_PER_M,
                           floor_m * fb.MM_PER_M) for x, y in polygon_m]
    face = Part.Face(Part.makePolygon(base + [base[0]]))
    return face.extrude(FreeCAD.Vector(0.0, 0.0,
                                       (top_m - floor_m) * fb.MM_PER_M))


def polygon_area_m2(polygon_m):
    return abs(fb.signed_area(polygon_m))


def carve(polygon_m, floor_m, ceiling_m, below, footprint_area_m2):
    """Intersect one space with the underside of the roof.

    Returns (surfaces, note).  note is None on success and a human sentence
    when the space was left alone, which happens when it turns out not to be
    under the roof at all -- a wing with its own separate roof, say.
    """
    solid = prism(polygon_m, floor_m, ceiling_m).common(below)
    solid = solid.removeSplitter()

    if not solid.Solids:
        return None, "not under the roof extents"

    if len(solid.Solids) > 1:
        raise RoofError(
            "the roof cuts this space into %d separate volumes; a space has "
            "to be one enclosed room, so split it in the plan first"
            % len(solid.Solids))

    solid = solid.Solids[0]
    entries, _volume = oriented_faces(solid)
    covered = sum(f.Area for f, n, _pts in entries if n.z < -TILT_LIMIT)
    covered_m2 = covered / (fb.MM_PER_M ** 2)
    if footprint_area_m2 and (
            footprint_area_m2 - covered_m2) / footprint_area_m2 > COVER_TOL:
        raise RoofError(
            "only %.1f m2 of this space's %.1f m2 footprint is under the "
            "roof -- extend the roof to cover it, or the part left out has "
            "no ceiling" % (covered_m2, footprint_area_m2))

    surfaces, dropped = solid_surfaces(solid)
    if dropped:
        raise RoofError("the carve left %d sliver face(s); the roof probably "
                        "grazes a wall" % dropped)
    return surfaces, None


# --------------------------------------------------------------------------
# the two methods
# --------------------------------------------------------------------------

def roof_extent(shapes):
    """(min_z_m, max_z_m, sloped_area_m2, plan_area_m2) of the roof itself."""
    zs, area, plan = [], 0.0, 0.0
    for shape in shapes:
        zs.append(shape.BoundBox.ZMin / fb.MM_PER_M)
        zs.append(shape.BoundBox.ZMax / fb.MM_PER_M)
        for face, normal in upward_faces(shape):
            area += face.Area / (fb.MM_PER_M ** 2)
            plan += face.Area / (fb.MM_PER_M ** 2) * abs(normal.z)
    return min(zs), max(zs), area, plan


def pitch_deg(min_z, max_z, plan_area_m2):
    """Representative slope, for the report only."""
    if plan_area_m2 <= 0 or max_z <= min_z:
        return 0.0
    run = math.sqrt(plan_area_m2)
    return math.degrees(math.atan2(max_z - min_z, run))


def attic_footprint(solid_shape):
    """(polygon_m, area_m2) of an attic's floor.

    The floor is what the spaces below have to meet, so it is read off the
    solid rather than assumed: the largest downward face at the bottom of the
    shape.
    """
    solids = solid_shape.Solids
    if not solids:
        raise RoofError("an Attic roof has to be a solid; this one is a %s. "
                        "Pad the profile, or tag it Extend instead"
                        % solid_shape.ShapeType)
    if len(solids) > 1:
        raise RoofError("an Attic roof has to be one solid; this one is %d"
                        % len(solids))
    solid = solids[0]
    entries, _volume = oriented_faces(solid)
    floors = [f for f, n, _pts in entries if n.z < -TILT_LIMIT]
    if not floors:
        raise RoofError("this attic has no floor -- no face of it points "
                        "downwards")
    floor = max(floors, key=lambda f: f.Area)
    points = _weld3([v.Point for v in floor.OuterWire.OrderedVertexes])
    plan = [(p.x, p.y) for p in points]
    plan = fb._drop_collinear(plan, fb.COLLINEAR_TOL_MM)
    if fb.signed_area(plan) < 0:
        plan.reverse()
    polygon = [[round(x / fb.MM_PER_M, 6), round(y / fb.MM_PER_M, 6)]
               for x, y in plan]
    return polygon, floor.Area / (fb.MM_PER_M ** 2)


def attic_space(obj, shape):
    """The space spec for one Attic roof, minus its identity."""
    solid = shape.Solids[0] if shape.Solids else None
    if solid is None:
        raise RoofError("an Attic roof has to be a solid; this one is a %s"
                        % shape.ShapeType)
    polygon, area = attic_footprint(shape)
    surfaces, dropped = solid_surfaces(solid)
    if dropped:
        raise RoofError("the attic solid has %d sliver face(s)" % dropped)
    room_number, name = space_identity(obj)
    return {
        "room_number": room_number,
        "name": name,
        "area_m2": round(area, 6),
        "vertices": polygon,
        "elevation_m": round(shape.BoundBox.ZMin / fb.MM_PER_M, 6),
        "height_m": round(shape.BoundBox.ZLength / fb.MM_PER_M, 6),
        "solid": {"source": "roof-attic",
                  "roof_object": obj.Name,
                  "surfaces": surfaces},
    }


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def space_top_m(space_spec, story_spec):
    height = space_spec.get("height_m") or story_spec["floor_to_floor_m"]
    return round(story_spec["elevation_m"] + float(height), 6)


def attic_base(floor_m, tops, tol_m=BASE_TOL_M):
    """Where an attic's floor belongs, given the storey tops beneath it.

    An attic only becomes part of the building if its floor lands on the
    ceilings below it.  Miss by a millimetre and matchSurfaces pairs nothing:
    OpenStudio defaults the unmatched attic floor to Ground -- 923 m2 of it,
    six metres above grade -- and leaves every room below still facing
    Outdoors, so the envelope is counted twice and nothing anywhere objects.

    Returns (target_m, delta_m, reaches).  `delta_m` is what to add to the
    attic to meet the ceilings, and `reaches` is False when the nearest storey
    top is further off than tol_m -- drafting slack is worth absorbing, a real
    gap is not.
    """
    if not tops:
        return None, 0.0, False
    target = min(tops, key=lambda t: abs(t - floor_m))
    delta = round(target - floor_m, 9)
    return target, delta, abs(delta) <= tol_m


def plan_overlap(shape_a, shape_b):
    """Do two roof shapes cover any of the same ground?

    Compared as shadows rather than as solids, because an attic sitting on
    top of an extend roof would never touch it in 3D yet still means two
    contradictory answers for what is above the same room.
    """
    flat = []
    for shape in (shape_a, shape_b):
        box = shape.BoundBox
        squashed = shape.copy()
        squashed.translate(FreeCAD.Vector(0.0, 0.0, -box.ZMin))
        flat.append(squashed.Faces[0].extrude(
            FreeCAD.Vector(0.0, 0.0, 1.0)) if squashed.Faces else None)
    if flat[0] is None or flat[1] is None:
        return False
    return flat[0].common(flat[1]).Volume > MIN_FACE_AREA_MM2


def apply(doc, sketches, stories, tol_m=BASE_TOL_M, mint=True):
    """Give the plan its roof.

    Mutates the space specs in `stories` in place, adding a `solid` to every
    space the roof reshapes.  Returns (roof_report, attic_stories, problems,
    notes); roof_report is None when the document declares no roof, which is
    the state every plan was in before this existed.

    `mint=False` leaves an attic without an id rather than writing one onto
    the roof object.  The review macro passes it: minting is a write, and a
    preview that quietly modifies the document is not a preview.
    """
    objects = roof_objects(doc)
    if not objects:
        return None, [], [], []

    base = base_story(sketches)
    shapes = {obj.Name: to_model_mm(obj.Shape, base) for obj in objects}
    extend = [o for o in objects if method_of(o) == EXTEND]
    attics = [o for o in objects if method_of(o) == ATTIC]

    problems, notes = [], []

    for roof in extend:
        for attic in attics:
            if plan_overlap(shapes[roof.Name], shapes[attic.Name]):
                problems.append(
                    "  %r (Extend) and %r (Attic) cover the same ground -- "
                    "the spaces underneath would be told two different "
                    "things about what is above them"
                    % (roof.Label, attic.Label))

    report = {
        "objects": [{"object": o.Name, "label": o.Label, "method": method_of(o)}
                    for o in objects],
        "extended": [],
        "attics": [],
    }

    if extend:
        below = below_solid([shapes[o.Name] for o in extend])
        min_z, max_z, area, plan_area = roof_extent(
            [shapes[o.Name] for o in extend])
        report.update({
            "method": EXTEND,
            "base_elevation_m": round(min_z, 6),
            "ridge_elevation_m": round(max_z, 6),
            "area_m2": round(area, 6),
            "plan_area_m2": round(plan_area, 6),
            "pitch_deg": round(pitch_deg(min_z, max_z, plan_area), 3),
        })

        for story in stories:
            for space in story["spaces"]:
                top = space_top_m(space, story)
                if abs(top - min_z) > tol_m:
                    continue
                try:
                    surfaces, note = carve(
                        space["vertices"], story["elevation_m"], max_z + 1.0,
                        below, space["area_m2"])
                except RoofError as exc:
                    problems.append("  %s/%s: %s"
                                    % (story["name"], space["name"], exc))
                    continue
                if note:
                    notes.append("  %s/%s left flat: %s"
                                 % (story["name"], space["name"], note))
                    continue
                space["solid"] = {"source": "roof-extend",
                                  "roof_object": extend[0].Name,
                                  "surfaces": surfaces}
                new_top = max(v[2] for s in surfaces for v in s["vertices"])
                report["extended"].append({
                    "story": story["name"],
                    "space": space["name"],
                    "was_top_m": top,
                    "now_top_m": round(new_top, 6),
                    "area_m2": space["area_m2"],
                })

    # Snap each attic onto the ceilings below before reading its geometry --
    # see attic_base for why an unsnapped one fails silently.  The whole solid
    # moves, so its footprint, its surfaces and its ridge stay consistent.
    tops = sorted({space_top_m(space, story)
                   for story in stories for space in story["spaces"]})

    attic_stories = {}
    for obj in attics:
        shape = shapes[obj.Name]
        floor_m = shape.BoundBox.ZMin / fb.MM_PER_M
        target, delta, reaches = attic_base(floor_m, tops, tol_m)
        if target is None:
            pass
        elif not reaches:
            notes.append(
                "  %r floor is %.3f m and the nearest storey top is %.3f m, "
                "%.0f mm away" % (obj.Label, floor_m, target,
                                  abs(delta) * fb.MM_PER_M))
            notes.append(
                "    nothing below reaches it, so its floor will come out "
                "Ground and the rooms under it stay Outdoors")
        elif delta:
            shape = shape.copy()
            shape.translate(FreeCAD.Vector(0.0, 0.0, delta * fb.MM_PER_M))
            shapes[obj.Name] = shape
            notes.append("  %r floor snapped %.1f mm to meet the ceilings "
                         "below at %.3f m"
                         % (obj.Label, delta * fb.MM_PER_M, target))

        try:
            spec = attic_space(obj, shapes[obj.Name])
        except RoofError as exc:
            problems.append("  %r: %s" % (obj.Label, exc))
            continue
        if mint:
            space_id, minted = fb.get_or_mint_space_id(obj)
        else:
            space_id = getattr(obj, "OS_SpaceId", "") or ""
            minted = not space_id
        spec["id"] = space_id
        spec["minted"] = minted
        story_name = (getattr(obj, STORY_PROP, "") or "Attic").strip()
        story = attic_stories.setdefault(story_name, {
            "name": story_name,
            "elevation_m": spec["elevation_m"],
            "floor_to_floor_m": spec["height_m"],
            "source_object": obj.Name,
            "spaces": [],
        })
        story["elevation_m"] = min(story["elevation_m"], spec["elevation_m"])
        story["floor_to_floor_m"] = max(story["floor_to_floor_m"],
                                        spec["height_m"])
        story["spaces"].append(spec)
        min_z, max_z, area, plan_area = roof_extent([shapes[obj.Name]])
        report["attics"].append({
            "object": obj.Name,
            "story": story_name,
            "space": spec["name"],
            "floor_elevation_m": spec["elevation_m"],
            "ridge_elevation_m": round(max_z, 6),
            "floor_area_m2": spec["area_m2"],
            "area_m2": round(area, 6),
            "pitch_deg": round(pitch_deg(min_z, max_z, plan_area), 3),
            "minted": minted,
        })
    if attics and "method" not in report:
        report["method"] = ATTIC
    elif attics and report.get("method") == EXTEND:
        report["method"] = "Extend+Attic"

    return report, list(attic_stories.values()), problems, notes
