# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Shared geometry extraction for the FreeCAD -> OpenStudio bridge.

Runs under FreeCAD's bundled Python (3.11).  Imported by fc_export_floorplan.py
and fc_seed_labels.py.

The core trick: a traced floor plan is a *connected network* of wall-centerline
segments, so Shape.Wires does not give you rooms.  Slicing a padded bounding
rectangle by those edges does -- each enclosed region comes back as its own
face.  Verified on FloorplanTest-01.FCStd: 26 rooms on Sketch, 17 on Sketch001,
zero sliver faces.
"""

import hashlib
import math
import os
import re
import uuid
import zipfile
from xml.etree import ElementTree

import FreeCAD
import Part
from BOPTools import SplitAPI

MM_PER_M = 1000.0

# Padding around a sketch bounding box before slicing.  Only needs to be large
# enough that the outer region is unambiguously one face.
SLICE_PAD_MM = 1000.0

# A vertex is dropped when it sits closer than this to the line joining its
# neighbours.  T-junctions where an interior wall meets another wall split an
# otherwise straight edge; 35 of 151 vertices on the test file are these.
COLLINEAR_TOL_MM = 0.5

# Two vertices closer than this are treated as one.
WELD_TOL_MM = 0.01

# How much closer to one story plane than another a label must be before it is
# considered to belong to that story rather than tying between them.
PLANE_TOL_MM = 1.0

STORY_PROPS = {
    "OS_StoryName": ("App::PropertyString", ""),
    "OS_Elevation": ("App::PropertyLength", 0.0),
    "OS_FloorToFloor": ("App::PropertyLength", 3000.0),
    "OS_Include": ("App::PropertyBool", False),
}

# App::PropertyFloat carries no unit and converts nothing, so binding one to
# an expression -- a spreadsheet cell of building constants, say -- silently
# hands it the value in FreeCAD's internal unit, which is millimetres.  A
# storey typed as 8ft 10-11/16in arrives as 2709.86 and is read as metres: a
# storey 2.7 km tall, every area and volume downstream wrong, and no error
# anywhere.  That happened twice on this project before these became
# App::PropertyLength, which keeps the unit, displays in whatever schema the
# user has chosen, and reads back as a Quantity.  The unit is carried rather
# than assumed -- which is why the names lost their _m suffix too: a length
# that displays feet-inches has no business being called _m.

# Per-room height override, held on the room's own label.  0 means "use the
# story's floor-to-floor height", so the property can sit on every label
# without having to mean anything.
HEIGHT_PROP = "OS_Height"

# The room's durable identity, minted on the label because a label is an
# object and a region is not.  Everything downstream keys on this: the fcmap,
# the change diff, and re-seating a label that its walls moved out from under.
ID_PROP = "OS_SpaceId"


class BridgeError(Exception):
    """Raised for contract violations the user has to fix in the document."""


# --------------------------------------------------------------------------
# document / story discovery
# --------------------------------------------------------------------------

def open_document(path):
    return FreeCAD.openDocument(str(path))


def _view_entries(path):
    """The view-side members of an .FCStd, as {name: bytes}.

    An .FCStd is a zip.  Everything the geometry needs -- shape .brp files,
    image planes -- is named inside Document.xml; the view data (colours,
    visibility, camera, thumbnail) is not.  That split identifies it exactly,
    without hard-coding FreeCAD's blob names.
    """
    if not os.path.exists(path):
        return {}
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "Document.xml" not in names:
                return {}
            doc_xml = zf.read("Document.xml").decode("utf-8", "replace")
            return {n: zf.read(n) for n in names
                    if n != "Document.xml" and n not in doc_xml}
    except (zipfile.BadZipFile, OSError):
        return {}


def save_document(doc):
    """doc.save(), keeping the view data a headless save would throw away.

    FreeCAD builds no ViewProviders without a GUI, so a scripted save rewrites
    the .FCStd without GuiDocument.xml and its companion colour blobs.  The
    geometry survives, but the document loses every display setting the user
    had -- layer colours, per-object visibility, line widths, the saved
    camera -- and reopens looking wrong.

    So snapshot the view entries, let FreeCAD save, then put back whatever it
    dropped.  Objects this script created have no entry of their own and get
    FreeCAD's defaults on open, which is correct.

    Returns the number of entries restored.
    """
    path = doc.FileName
    preserved = _view_entries(path)
    doc.save()
    if not preserved:
        return 0

    with zipfile.ZipFile(path) as zf:
        present = set(zf.namelist())
    missing = [(n, d) for n, d in preserved.items() if n not in present]
    if not missing:
        return 0

    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as src, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        for name, data in missing:
            dst.writestr(name, data)
    os.replace(tmp, path)
    return len(missing)


# A scanned drawing is mostly white paper, so it wants to be faint enough to
# sketch over.  70% is what reads well against a dark viewport; over a light
# one it composites to within a few grey levels of the background and vanishes,
# which is a background choice, not a placement bug.  "Two side" lighting keeps
# it lit when you orbit past it.
IMAGE_VIEW_DEFAULTS = (
    ("Lighting", "App::PropertyEnumeration", "Integer", "1"),
    ("Transparency", "App::PropertyPercent", "Integer", "70"),
)

_VIEW_ENTRY = """        <ViewProvider name="%s">
            <Properties Count="%d" TransientCount="0">
%s
            </Properties>
        </ViewProvider>
"""
_VIEW_PROP = """                <Property name="%s" type="%s" status="1">
                    <%s value="%s"/>
                </Property>"""


def seed_view_defaults(path, names, props=IMAGE_VIEW_DEFAULTS):
    """Give objects a first-time display setting, in the saved .FCStd.

    Headless FreeCAD builds no ViewProviders, so a scripted run cannot set
    transparency or lighting through the API at all.  But the saved file is a
    zip and the display settings are plain XML inside GuiDocument.xml, so an
    object FreeCAD has never built a ViewProvider for can be handed one here.

    Only objects with no entry of their own are touched, so this can never
    overwrite a setting the user made -- once they have opened and saved the
    document, every object has an entry and this becomes a no-op.

    `names` may be a dict of {name: props} when the objects do not all want
    the same settings -- an imported wall and an imported door are coloured
    differently -- in which case the `props` argument is the fallback for any
    name whose entry is None.

    Returns the names actually seeded.  Call it after save_document().
    """
    per_name = names if isinstance(names, dict) else None
    if not os.path.exists(path):
        return []
    with zipfile.ZipFile(path) as zf:
        if "GuiDocument.xml" not in zf.namelist():
            return []
        gui = zf.read("GuiDocument.xml").decode("utf-8")

    match = re.search(r'<ViewProviderData Count="(\d+)">', gui)
    if not match:
        return []
    existing = set(re.findall(r'<ViewProvider name="([^"]+)"', gui))
    wanted = [n for n in names if n not in existing]
    if not wanted:
        return []

    def settings(name):
        return (per_name.get(name) or props) if per_name else props

    wanted = [n for n in wanted if settings(n)]
    if not wanted:
        return []

    body = "".join(
        _VIEW_ENTRY % (name, len(settings(name)),
                       "\n".join(_VIEW_PROP % p for p in settings(name)))
        for name in wanted)
    closing = gui.rindex("</ViewProviderData>")
    gui = gui[:closing] + body + gui[closing:]
    gui = gui.replace(match.group(0),
                      '<ViewProviderData Count="%d">'
                      % (int(match.group(1)) + len(wanted)), 1)

    # Parse before committing: a malformed GuiDocument.xml costs the user
    # every display setting in the document.
    ElementTree.fromstring(gui)

    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as src, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename == "GuiDocument.xml":
                dst.writestr(item, gui.encode("utf-8"))
            else:
                dst.writestr(item, src.read(item.filename))
    os.replace(tmp, path)
    return wanted


def length_m(obj, name):
    """A length property in metres, or None when the object has not got it.

    App::PropertyLength reads back as a Quantity, which knows its own unit,
    so the conversion is asked for rather than assumed.  Should a FreeCAD
    build hand back a bare float instead, that float is in the internal unit
    -- millimetres -- and never in metres, so the fallback divides.
    """
    if not hasattr(obj, name):
        return None
    value = getattr(obj, name)
    try:
        return float(value.getValueAs("m"))
    except AttributeError:
        return float(value) / MM_PER_M


def story_elevation_m(sketch):
    """The story's elevation in metres."""
    value = length_m(sketch, "OS_Elevation")
    return 0.0 if value is None else value


def story_height_m(sketch):
    """The story's floor-to-floor height in metres."""
    value = length_m(sketch, "OS_FloorToFloor")
    return 3.0 if value is None else value


def stack_elevations_m(placements_mm):
    """Elevation and floor-to-floor derived from real stacked placement.

    ``placements_mm`` maps an arbitrary key (a sketch name) to that sketch's
    ``Placement.Base.z`` in millimetres. Returns ``{key: (elevation_m,
    floor_to_floor_m)}`` for every key, sorted low to high, with
    ``floor_to_floor_m`` ``None`` for the topmost story -- there is no story
    above it to measure the spacing against, so that one still has to be
    entered by hand.

    Returns ``{}`` when every sketch sits at the same Z. That is the
    side-by-side layout (README "Stories may sit side by side or be
    stacked"), where Placement carries no elevation information at all.
    """
    if len(set(placements_mm.values())) <= 1:
        return {}
    ordered = sorted(placements_mm.items(), key=lambda kv: kv[1])
    result = {}
    for i, (key, z_mm) in enumerate(ordered):
        floor_to_floor_m = None
        if i + 1 < len(ordered):
            floor_to_floor_m = (ordered[i + 1][1] - z_mm) / MM_PER_M
        result[key] = (z_mm / MM_PER_M, floor_to_floor_m)
    return result


def ensure_story_props(sketch):
    """Add the OS_* story properties to a sketch if they are missing.

    Returns True when anything was added (caller decides whether to save).
    """
    added = False
    for name, (ptype, default) in STORY_PROPS.items():
        if not hasattr(sketch, name):
            sketch.addProperty(ptype, name, "OpenStudio",
                               "FreeCAD to OpenStudio bridge")
            setattr(sketch, name, default)
            added = True
    return added


def story_sketches(doc):
    """Sketches flagged for export, in document order.

    A sketch with no OS_Include property, or OS_Include False, is skipped --
    so scratch/reference sketches are safe to leave in the document.
    """
    return [o for o in doc.Objects
            if o.TypeId == "Sketcher::SketchObject"
            and getattr(o, "OS_Include", False)]


def all_sketches(doc):
    return [o for o in doc.Objects if o.TypeId == "Sketcher::SketchObject"]


def north_axis_deg(doc):
    """Building north axis, stored as a document property.  Defaults to 0."""
    return float(getattr(doc, "OS_NorthAxis_deg", 0.0) or 0.0)


# --------------------------------------------------------------------------
# is the imported geometry still the geometry this plan describes?
# --------------------------------------------------------------------------
#
# The OS_Geometry group in a document is a copy of the model, and openings are
# hosted by testing each drawn outline against it.  Edit the plan, rebuild the
# model, and that copy is a picture of walls that have moved -- so an outline
# either finds no host or, worse, finds a wall whose name now belongs to a
# different piece of the building.  Measured: after one wall move, the opening
# export failed with "no imported surface is coplanar with and contains it",
# which says nothing about the actual cause.
#
# Comparing timestamps does not work: the .osm is rewritten by the openings
# apply itself, so the model is routinely newer than the import for reasons
# that change no wall.  What matters is whether the *plan* has moved since the
# geometry was imported, because nothing else moves a wall.  So the import
# stamps the document with a fingerprint of the plan it was taken from, and
# anything that depends on the imported geometry can compare.

PLAN_DIGEST_PROP = "OS_PlanDigest"


def digest_points(points, dp=3):
    """A short fingerprint of a set of coordinates, independent of order.

    Sorted, so re-drawing an edge in the other direction or adding elements in
    a different order does not read as a change; rounded to microns, so the
    last bits of a float do not either.
    """
    rows = sorted("%.*f,%.*f,%.*f" % (dp, x, dp, y, dp, z)
                  for x, y, z in points)
    return hashlib.sha1("|".join(rows).encode("utf-8")).hexdigest()[:16]


def plan_points(sketches):
    """Every edge endpoint of the story sketches, in global millimetres.

    Global, so moving a whole story counts.  Story elevation and height go in
    as points too -- they are not geometry in the sketch, but they set where
    the walls start and stop, so a change to either dates the import exactly
    as a moved wall does.
    """
    points = []
    for i, sketch in enumerate(sketches):
        for edge in sketch.Shape.Edges:
            for vertex in edge.Vertexes:
                points.append((vertex.X, vertex.Y, vertex.Z))
        points.append((float(i), story_elevation_m(sketch) * MM_PER_M,
                       story_height_m(sketch) * MM_PER_M))
    return points


def plan_digest(sketches):
    return digest_points(plan_points(sketches))


def stored_plan_digest(doc):
    """What the plan looked like when OS_Geometry was last imported, or ""."""
    return str(getattr(doc, PLAN_DIGEST_PROP, "") or "")


def stamp_plan_digest(doc, sketches):
    """Record the current plan on the document.  Returns the digest."""
    if not hasattr(doc, PLAN_DIGEST_PROP):
        doc.addProperty("App::PropertyString", PLAN_DIGEST_PROP, "OpenStudio",
                        "The plan the imported OS_Geometry was built from")
    value = plan_digest(sketches)
    setattr(doc, PLAN_DIGEST_PROP, value)
    return value


# --------------------------------------------------------------------------
# facade names
# --------------------------------------------------------------------------

COMPASS = ["North", "Northeast", "East", "Southeast",
           "South", "Southwest", "West", "Northwest"]


def plan_azimuth(normal):
    """Bearing of an outward normal in the *building's* own frame."""
    return math.degrees(math.atan2(normal.x, normal.y)) % 360.0


def compass_of(normal):
    """Facade name from the outward normal, in plan north.

    Deliberately not rotated by the building's north axis.  This building sits
    at 45 degrees, so its "south" elevation faces true southwest -- but the
    drawing set, the elevation sheets and every name already in the model call
    that facade South, and a sketch labelled Southwest for the wall everyone
    points at as south is a trap.  True orientation is printed alongside and
    lives in the model's north axis, where it belongs.

    Shared with the shading exporter so a canopy on the south wall is named
    South by the same rule that named the door beneath it.
    """
    return COMPASS[int((plan_azimuth(normal) + 22.5) % 360.0 // 45.0)]


# --------------------------------------------------------------------------
# per-outline identity
# --------------------------------------------------------------------------

# Rounding used to match a wire edge back to the sketch geometry it came from.
# The two are the same curve, so this only has to absorb OCC's own float
# noise; measured on the real document, 112 of 112 edges match exactly.
MID_KEY_PLACES = 4


def mid_key(point):
    return tuple(round(c, MID_KEY_PLACES) for c in (point.x, point.y, point.z))


def edge_mid(edge):
    return edge.valueAt((edge.FirstParameter + edge.LastParameter) / 2.0)


def element_index(sketch):
    """{edge midpoint: geometry index} for a sketch's drawable geometry."""
    lookup = {}
    facades = sketch.GeometryFacadeList
    for index, geometry in enumerate(sketch.Geometry):
        if index >= len(facades) or facades[index].Construction:
            continue
        try:
            edge = geometry.toShape()
        except Exception:            # pragma: no cover - OCC edge cases
            continue
        lookup[mid_key(sketch.Placement.multVec(edge_mid(edge)))] = index
    return lookup


def element_values(sketch, ext_name):
    """{edge midpoint: value} for elements carrying this string extension.

    Shape.Wires is what a drawn outline is; the extension lives on the
    geometry list.  Midpoints join the two -- they are unique per edge, unlike
    the vertices a closed outline shares with its neighbours.
    """
    lookup = {}
    facades = sketch.GeometryFacadeList
    for midpoint, index in element_index(sketch).items():
        facade = facades[index]
        if not facade.hasExtensionOfName(ext_name):
            continue
        value = (facade.getExtensionOfName(ext_name).Value or "").strip()
        if value:
            lookup[midpoint] = value
    return lookup


def wire_values(wire, lookup):
    """The distinct values carried by this outline's edges."""
    return sorted({lookup[key] for key in
                   (mid_key(edge_mid(edge)) for edge in wire.Edges)
                   if key in lookup})


def ensure_ids(sketches, ext_name):
    """Give every closed outline a stable id, minting where there is none.

    The id is what lets apply match an outline it has seen before and update
    the OpenStudio object in place, keeping its handle.  Without one, apply has
    to delete and rebuild, which mints a new handle every run and silently
    breaks anything referencing the old one -- a shading control on a window,
    a photovoltaic generator on a canopy.

    Shared by the opening and shading exporters so both anchor the same way.
    Ids are minted once and then left alone.  Returns (minted, problems).
    """
    minted, problems = 0, []
    for sketch in sketches:
        wires = [w for w in getattr(sketch.Shape, "Wires", []) if w.isClosed()]
        if not wires:
            continue
        lookup = element_values(sketch, ext_name)
        index_of = element_index(sketch)
        facades = sketch.GeometryFacadeList
        changed = False

        for i, wire in enumerate(wires, start=1):
            found = wire_values(wire, lookup)
            if len(found) > 1:
                problems.append(
                    "%s wire %d: its edges carry %d different %s values (%s) "
                    "-- two outlines were probably merged; delete the ids and "
                    "re-export to mint a fresh one"
                    % (sketch.Label, i, len(found), ext_name,
                       ", ".join(found)))
                continue
            if found:
                continue

            indices = [index_of.get(mid_key(edge_mid(edge)))
                       for edge in wire.Edges]
            if any(index is None for index in indices):
                problems.append("%s wire %d: did not join back to the sketch "
                                "geometry, so it cannot be given an id"
                                % (sketch.Label, i))
                continue
            new_id = str(uuid.uuid4())
            for index in indices:
                extension = Part.GeometryStringExtension()
                extension.Name = ext_name
                extension.Value = new_id
                facades[index].setExtension(extension)
            changed = True
            minted += 1

        if changed:
            sketch.GeometryFacadeList = facades
    return minted, problems


# --------------------------------------------------------------------------
# air-boundary overrides
# --------------------------------------------------------------------------

AIR_BOUNDARY_EXT_NAME = "OS_AirBoundaryOverride"
AIR_BOUNDARY_KINDS = ("SOLID", "OPEN")


def _touching_map(faces):
    """{edge midpoint: [(face, edge), ...]} for every boundary edge of every
    room face.  Shared by air_boundary_overrides (resolves only tagged
    edges, needs the face for its room id) and wall_adjacencies (classifies
    every edge, needs the edge itself for its endpoints) -- one walk, two
    consumers.
    """
    touching = {}
    for face in faces:
        for edge in face.OuterWire.Edges:
            touching.setdefault(mid_key(edge_mid(edge)), []).append(
                (face, edge))
    return touching


def _classify_fragment(entries, face_space_id):
    """What one face-boundary fragment separates -- the one classification
    both wall_adjacencies (one fragment == one edge, the common case) and
    air_boundary_overrides (many fragments under one tagged run) need,
    factored out so they cannot silently disagree about what "the same
    room on both sides" or "a SKIP void" means.

    `entries` is one _touching_map value: [(face, edge), ...] for a single
    fragment. Returns (kind, a, b):

      ("interior", space_a, space_b)  two distinct, labelled rooms
      ("skip", space, skip_face)      one labelled room, one SKIP/unlabelled
                                       face -- skip_face is that face object
      ("exterior", None, None)        only one face touches it
      ("dangling", space, None)       the same face touches it twice
      ("unlabelled", None, None)      neither face is labelled
      ("ambiguous", None, None)       more than two faces (should not occur:
                                       planar-graph duality caps a boundary
                                       edge at two, this is a defensive case)
    """
    face_list = [f for f, _e in entries]
    with_id = [f for f in face_list if f in face_space_id]
    rooms = sorted({face_space_id[f] for f in with_id})

    if len(face_list) < 2:
        return ("exterior", None, None)
    if len(face_list) > 2:
        return ("ambiguous", None, None)
    if len(with_id) == 2 and len(rooms) < 2:
        return ("dangling", rooms[0] if rooms else None, None)
    if len(with_id) == 0:
        return ("unlabelled", None, None)
    if len(with_id) == 1:
        skip_face = next(f for f in face_list if f not in face_space_id)
        return ("skip", rooms[0], skip_face)
    return ("interior", rooms[0], rooms[1])


# How far a fragment's own midpoint may sit from the tagged edge's line and
# still count as "on it" -- 1 mm, the same order of tolerance surface_identity
# uses for plan geometry (PLAN_COLLINEAR_TOL_M).  A fragment's midpoint is
# not an independent measurement, it is the midpoint of a shorter piece of
# the exact same drawn line, so this only has to absorb FreeCAD's own float
# noise through the Placement transform, not real positional error.
TAGGED_EDGE_TOL_MM = 1.0


def _on_tagged_edge(point, p0, p1, tol=TAGGED_EDGE_TOL_MM):
    """Does `point` -- a plain (x, y, z) tuple -- lie on the segment from p0
    to p1 (anything with .x/.y/.z in the same frame: a real FreeCAD.Vector
    or a stand-in)?  Plain attribute arithmetic rather than Vector's own
    operators, so this has no FreeCAD dependency beyond the attributes
    themselves and is exercised directly in tests/test_fcbridge_air_boundary_overrides.py
    without stubbing more than that.  Perpendicular distance and parameter
    range are checked separately so a point exactly at either end still
    counts.
    """
    px, py, pz = point
    dx, dy, dz = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
    length2 = dx * dx + dy * dy + dz * dz
    if length2 < 1e-9:
        return False
    vx, vy, vz = px - p0.x, py - p0.y, pz - p0.z
    t = (vx * dx + vy * dy + vz * dz) / length2
    if t < -1e-6 or t > 1 + 1e-6:
        return False
    cx, cy, cz = p0.x + t * dx, p0.y + t * dy, p0.z + t * dz
    ddx, ddy, ddz = px - cx, py - cy, pz - cz
    return (ddx * ddx + ddy * ddy + ddz * ddz) ** 0.5 <= tol


def air_boundary_overrides(sketch, faces, face_space_id):
    """Resolve OS_AirBoundaryOverride tags to the room pair each edge separates.

    `faces` is extract_room_faces(sketch)'s output; `face_space_id` is
    {face: space_id}, built by the caller as it assigns labels -- a SKIP-ed
    face carries no entry, the same as it carries no space.

    The tag is drawn on a whole geometry edge -- whatever length the wall
    run has, a real fire wall included -- but a T-junction where another
    wall meets it partway along splits that run's *face* boundary into more
    than one fragment, each with its own midpoint.  So the tagged edge's own
    two endpoints are what a fragment is matched against here (every
    fragment whose own midpoint lies on that line, within its span), not
    the tagged edge's single midpoint against a single fragment's -- a wall
    tagged whole is not necessarily bounded whole.  Measured: a rated wall
    drawn as one ~50-foot run, crossing two T-junctions, resolved to zero
    fragments under exact-midpoint matching and three under this.

    An interior sketch edge borders exactly two faces (planar-graph duality);
    one bordering only one face sits on the building's exterior boundary, and
    one bordering the same face twice is a dangling stub inside one room.
    Neither can be an air boundary, so both are reported rather than
    silently dropped -- this is the export-time check that keeps a tagged
    edge from ever needing to be resolved by position later on, the way a
    surface name has to be.  The same refusal covers a tagged run that
    genuinely crosses more than two rooms along its length (drawn too long,
    or crossing a real corner) -- rooms found across every matched fragment
    are pooled before this check, so a run touching a third room anywhere
    along it is refused exactly as if one edge touched three faces at once.

    All of that only ever applies to OPEN.  An air boundary is a construct
    between exactly two real zones -- OPEN demands that pairing exist
    because there is no other way to physically create one.  SOLID makes no
    such demand: it declares "never an air boundary here," which is true
    whether the edge borders zero, one, two or three rooms, since no rule
    anywhere in this codebase would ever turn a non-two-room edge into an
    air boundary in the first place -- there is nothing for a SOLID
    declaration to protect against on such an edge, so refusing it would be
    an objection to a situation with no actual failure mode.  A SOLID tag
    on anything other than a clean two-room (or one-room-plus-SKIP) edge is
    silently accepted and resolves to nothing, rather than reported.

    A third case -- one real room and one SKIP-ed face -- is neither of
    those.  A mezzanine's guardrail is routinely tagged against exactly this:
    the room it really borders is a taller room on another story, reaching
    up through a deliberate "Open to Below" hole rather than being drawn a
    second time, and there is nothing on *this* sketch to name it.  That is
    resolved later, across every story at once
    (resolve_cross_story_overrides), so it is returned separately rather
    than as a problem or a finished override.

    Returns (overrides, deferred, problems).  `deferred` entries carry
    "skip_vertices" -- the SKIP face's own plan polygon -- in place of
    space_b, for that second pass to match against a taller room's
    footprint.
    """
    tags = element_values(sketch, AIR_BOUNDARY_EXT_NAME)
    if not tags:
        return [], [], []

    touching = _touching_map(faces)
    index_of = element_index(sketch)
    overrides, deferred, problems = [], [], []
    for midpoint, kind in tags.items():
        if kind not in AIR_BOUNDARY_KINDS:
            problems.append(
                "  %s: unrecognized %s value %r (want %s)"
                % (sketch.Label, AIR_BOUNDARY_EXT_NAME, kind,
                   " or ".join(AIR_BOUNDARY_KINDS)))
            continue

        geom_index = index_of.get(midpoint)
        if geom_index is None:
            problems.append(
                "  %s: %s=%s did not join back to the sketch geometry"
                % (sketch.Label, AIR_BOUNDARY_EXT_NAME, kind))
            continue
        edge = sketch.Geometry[geom_index].toShape()
        p0, p1 = edge.Vertexes[0].Point, edge.Vertexes[1].Point
        vertices = [[round(p0.x / MM_PER_M, 6), round(p0.y / MM_PER_M, 6)],
                   [round(p1.x / MM_PER_M, 6), round(p1.y / MM_PER_M, 6)]]

        # A wall long enough to cross a T-junction has its face boundary
        # split into several fragments -- classify each one on its own
        # terms first (so a genuine dangling stub at one point along the
        # run is not washed out by clean fragments elsewhere), then pool
        # the results.
        gp0 = sketch.Placement.multVec(p0)
        gp1 = sketch.Placement.multVec(p1)
        classified = []
        for frag_mid, entries in touching.items():
            if _on_tagged_edge(frag_mid, gp0, gp1):
                classified.append(_classify_fragment(entries, face_space_id))

        reason = None
        if not classified:
            reason = "touches the building's exterior boundary"
        elif any(k == "ambiguous" for k, _a, _b in classified):
            reason = "is shared by more than two rooms"
        elif any(k == "dangling" for k, _a, _b in classified):
            reason = "borders the same room on both sides"
        elif all(k in ("exterior", "unlabelled") for k, _a, _b in classified):
            reason = ("touches the building's exterior boundary"
                     if any(k == "exterior" for k, _a, _b in classified)
                     else "borders no labelled room on either side")
        if reason:
            if kind == "OPEN":
                problems.append(
                    "  %s: %s=%s on an edge that %s -- an air boundary needs "
                    "exactly two rooms"
                    % (sketch.Label, AIR_BOUNDARY_EXT_NAME, kind, reason))
            # SOLID: no objection -- see the docstring above. Nothing to
            # resolve, nothing to protect against, so nothing to report.
            continue

        # Only "interior" and "skip" fragments ever name a room; "exterior"
        # and "unlabelled" stretches along the same run contribute nothing
        # and are not an error once at least one fragment resolves cleanly.
        rooms, skip_face = set(), None
        for k, a, b in classified:
            if k == "interior":
                rooms.add(a)
                rooms.add(b)
            elif k == "skip":
                rooms.add(a)
                skip_face = skip_face or b

        if len(rooms) > 2:
            if kind == "OPEN":
                problems.append(
                    "  %s: %s=%s on an edge that is shared by more than two "
                    "rooms -- an air boundary needs exactly two rooms"
                    % (sketch.Label, AIR_BOUNDARY_EXT_NAME, kind))
            continue

        if len(rooms) == 1:
            # No "interior" fragment resolved this pair outright, only a
            # SKIP-adjacent one -- deferred to resolve_cross_story_overrides.
            deferred.append({
                "kind": kind, "space_a": next(iter(rooms)),
                "skip_vertices": face_polygon_m(skip_face, sketch),
                "vertices": vertices,
            })
            continue

        space_a, space_b = sorted(rooms)
        overrides.append({
            "kind": kind,
            "space_a": space_a, "space_b": space_b,
            "vertices": vertices,
        })
    return overrides, deferred, problems


def wall_adjacencies(sketch, faces, face_space_id):
    """Classify every wall-centerline segment on this story by what it
    separates.

    One entry per **face-boundary edge** -- the granularity a wall actually
    exists at, not the granularity it was drawn at.  An interior partition
    meeting an exterior wall away from a corner (a T-junction) splits that
    wall's face boundary into pieces that can each border a *different*
    neighbour, even though the wall was drawn as one continuous line; this
    works from the room faces' own post-slice boundaries for exactly that
    reason, the same source room_wall_segments reads.  (Contrast
    air_boundary_overrides' own tag lookup, which starts from a whole drawn
    edge's own two endpoints -- what a person selects and tags in the
    FreeCAD GUI -- then matches every fragment whose own midpoint lies on
    that line and pools their classifications, so a tag on a wall that
    spans a T-junction like this resolves too, not just the granularity
    used here.)

    Returns {edge midpoint: record}:

      {"vertices": [[x, y], [x, y]],      # metres, sketch-local frame
       "kind": "interior" | "exterior" | "refused",
       "space_a": id, "space_b": id,      # kind == "interior" only, sorted
       "reason": str}                     # kind == "refused" only

    "interior" is the only kind a caller may pre-pair (fc_walls.py).
    "exterior" and "refused" both mean: build this segment as an ordinary,
    unpaired surface and let intersectSurfaces/matchSurfaces settle it,
    exactly as fromFloorPrint would have produced there -- nothing changes
    for those edges.  "shared by more than two rooms" should not occur at
    this granularity (planar-graph duality caps a boundary edge at two
    faces); kept as a defensive check, not a case expected to fire.
    """
    inv = sketch.Placement.inverse()
    touching = _touching_map(faces)

    out = {}
    for midpoint, found in touching.items():
        _face0, edge0 = found[0]
        p0 = inv.multVec(edge0.Vertexes[0].Point)
        p1 = inv.multVec(edge0.Vertexes[1].Point)
        vertices = [[round(p0.x / MM_PER_M, 6), round(p0.y / MM_PER_M, 6)],
                   [round(p1.x / MM_PER_M, 6), round(p1.y / MM_PER_M, 6)]]

        kind, a, b = _classify_fragment(found, face_space_id)
        if kind == "interior":
            out[midpoint] = {"vertices": vertices, "kind": "interior",
                             "space_a": a, "space_b": b}
        elif kind == "exterior":
            out[midpoint] = {"vertices": vertices, "kind": "exterior"}
        elif kind == "dangling":
            out[midpoint] = {"vertices": vertices, "kind": "refused",
                             "reason": "borders the same room on both sides"}
        elif kind == "ambiguous":
            out[midpoint] = {"vertices": vertices, "kind": "refused",
                             "reason": "shared by more than two rooms"}
        else:               # "skip" or "unlabelled" -- no tag here to
                            # resolve a SKIP void against, unlike
                            # air_boundary_overrides
            out[midpoint] = {"vertices": vertices, "kind": "refused",
                             "reason": "borders a SKIP or unlabelled region"}
    return out


def room_wall_segments(face, sketch):
    """This room's full wall-centerline loop, one entry per edge.

    Unlike face_polygon_m, no collinear run is dropped: a T-junction where a
    neighbour's wall lands part way along an otherwise straight run is
    exactly the point where the two sides may need different treatment (a
    different neighbour, a different height), so it has to stay a separate
    segment here even though it is not a real corner of the room's shape.

    Returns [(mid_key, (x0_m, y0_m), (x1_m, y1_m)), ...], CCW in plan, in the
    sketch's own frame -- the same frame face_polygon_m returns, so a wall
    quad built from this lines up with the room's own Floor/Ceiling built
    from its vertices.  mid_key is computed the same two-step transform
    wall_adjacencies' own keys reduce to for a straight edge under a rigid
    Placement (local midpoint == image of the endpoint midpoint, since
    Placement has no scaling component), so a lookup against
    wall_adjacencies' dict is an exact match.
    """
    inv = sketch.Placement.inverse()
    raw = [inv.multVec(v.Point) for v in face.OuterWire.OrderedVertexes]
    pts = [(p.x, p.y) for p in raw]
    pts = _weld(pts, WELD_TOL_MM)
    if signed_area(pts) < 0:
        pts.reverse()

    out = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        mid_local = FreeCAD.Vector((x0 + x1) / 2.0, (y0 + y1) / 2.0, 0.0)
        key = mid_key(sketch.Placement.multVec(mid_local))
        out.append((key,
                    (round(x0 / MM_PER_M, 6), round(y0 / MM_PER_M, 6)),
                    (round(x1 / MM_PER_M, 6), round(y1 / MM_PER_M, 6))))
    return out


def resolve_cross_story_overrides(stories, deferred):
    """Resolve air-boundary overrides whose other side was a SKIP void.

    `deferred` is [(story_name, override), ...], each override carrying
    "skip_vertices" -- the SKIP face's own plan polygon -- in place of
    space_b (see air_boundary_overrides).

    A SKIP region stands in for a taller room on another story, reaching up
    past its own floor-to-floor by an OS_Height_m override -- exactly the
    situation find_air_boundaries.py's mezzanine-edge rule already
    recognises, on the built model, by whichever wall surface actually ends
    up touching which.  This is the export-time equivalent, run once every
    story is known: every room tall enough to reach through a gap is indexed
    by its own footprint, and a SKIP face is resolved against whichever one
    has the identical footprint.  Prisms are extruded straight up, so a room
    reaching through a hole directly above it has that hole's exact
    footprint -- the same assumption cross_story_matches already reports on.

    Returns (resolved, problems).  A resolved entry is a plain override,
    space_b filled in and skip_vertices dropped -- indistinguishable in the
    output from one air_boundary_overrides resolved outright.
    """
    by_footprint = {}
    for story in stories:
        for space in story["spaces"]:
            height = space.get("height_m") or story["floor_to_floor_m"]
            if height <= story["floor_to_floor_m"] + 1e-6:
                continue                # not tall enough to reach through
            key = tuple(sorted(tuple(v) for v in space["vertices"]))
            by_footprint.setdefault(key, []).append(
                (story["name"], space["name"], space["id"]))

    resolved, problems = [], []
    for story_name, override in deferred:
        key = tuple(sorted(tuple(v) for v in override["skip_vertices"]))
        hits = by_footprint.get(key, [])
        if not hits:
            problems.append(
                "  %s: %s=%s borders an Open to Below region with no "
                "matching tall room on another story -- draw the room, or "
                "give it OS_Height_m so it reaches through"
                % (story_name, AIR_BOUNDARY_EXT_NAME, override["kind"]))
            continue
        if len(hits) > 1:
            problems.append(
                "  %s: %s=%s matches more than one tall room by footprint "
                "(%s) -- too ambiguous to resolve"
                % (story_name, AIR_BOUNDARY_EXT_NAME, override["kind"],
                   ", ".join("%s/%s" % (s, n) for s, n, _id in hits)))
            continue
        _story, _name, space_id = hits[0]
        resolved.append({
            "kind": override["kind"],
            "space_a": override["space_a"], "space_b": space_id,
            "vertices": override["vertices"],
        })
    return resolved, problems


# --------------------------------------------------------------------------
# wall-network normalization -- pre-split T-junctions in the sketch itself,
# and keep External Geometry out of the "official wall" role entirely, so
# air_boundary_overrides/wall_adjacencies never have to reconstruct a wall's
# true segmentation from face boundaries after the fact.  normalize_walls.
# FCMacro is the entry point that actually runs these against a document;
# wall_network_problems is what fc_export_floorplan.py refuses on when a
# sketch has not been run through it.
# --------------------------------------------------------------------------

def _project_onto_segment(point, p0, p1):
    """(t, perpendicular_distance_mm) of `point` (plain (x, y, z)) against
    the infinite line through p0/p1 (anything with .x/.y/.z), or None if
    p0 == p1.  t=0 at p0, t=1 at p1 -- not clamped, so a point beyond
    either end still reports where it would project to; the caller decides
    what range of t counts.
    """
    px, py, pz = point
    dx, dy, dz = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
    length2 = dx * dx + dy * dy + dz * dz
    if length2 < 1e-9:
        return None
    vx, vy, vz = px - p0.x, py - p0.y, pz - p0.z
    t = (vx * dx + vy * dy + vz * dz) / length2
    cx, cy, cz = p0.x + t * dx, p0.y + t * dy, p0.z + t * dz
    ddx, ddy, ddz = px - cx, py - cy, pz - cz
    return t, (ddx * ddx + ddy * ddy + ddz * ddz) ** 0.5


def find_t_junctions(edges, tol=TAGGED_EDGE_TOL_MM):
    """Where another edge's own endpoint lands strictly inside this edge's
    span -- a T-junction, needing a split there before this network can be
    treated as a planar graph where every edge borders at most two rooms.

    `edges` is [(key, p0, p1)], p0/p1 anything with .x/.y/.z, all in one
    shared local frame -- the sketch's own, which is what sketch.split()'s
    own point argument expects, no Placement transform needed.

    Two edges crossing free-form through both interiors (an X) is not a
    T-junction and is not detected here -- a wall-centerline network should
    never contain one, and guessing how to split both would be worse than
    leaving it to be reported as a modelling mistake elsewhere.

    Returns {key: [(t, (x, y, z), other_key, other_pos_id), ...]}, sorted
    by t along that edge -- the ordered split points sketch.split() must be
    called with, in order, for a wall crossing more than one T-junction.
    `other_key`/`other_pos_id` (1 for that edge's own p0, 2 for its p1)
    identify exactly which other edge's endpoint landed here -- the actual
    cause of the T-junction, needed so the caller can pin the new split
    point to it with a Coincident constraint, not just cut at a coordinate
    snapshot nothing then holds in place.  Measured directly: without that
    constraint, later moving the other edge's endpoint leaves the split
    point behind, opening a gap where the walls used to meet exactly.
    Multiple other edges' endpoints landing at the same physical point (a
    4-way "+") are deduped to one entry -- pinning to any one of them
    still ties the point down, since they are themselves coincident.
    """
    hits = {}
    for key, p0, p1 in edges:
        dx, dy, dz = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        if length < 1e-6:
            continue
        end_tol_t = min(tol / length, 0.5)

        found = []
        for other_key, q0, q1 in edges:
            if other_key == key:
                continue
            for other_pos_id, endpoint in ((1, q0), (2, q1)):
                point = (endpoint.x, endpoint.y, endpoint.z)
                projected = _project_onto_segment(point, p0, p1)
                if projected is None:
                    continue
                t, dist = projected
                if dist > tol:
                    continue
                if t <= end_tol_t or t >= 1.0 - end_tol_t:
                    continue          # already a shared vertex, not a split
                found.append((t, point, other_key, other_pos_id))

        found.sort(key=lambda item: item[0])
        deduped = []
        for t, point, other_key, other_pos_id in found:
            if deduped:
                prev = deduped[-1][1]
                ddx, ddy, ddz = (point[0] - prev[0], point[1] - prev[1],
                                point[2] - prev[2])
                if (ddx * ddx + ddy * ddy + ddz * ddz) ** 0.5 <= WELD_TOL_MM:
                    continue
            deduped.append((t, point, other_key, other_pos_id))
        if deduped:
            hits[key] = deduped
    return hits


def t_junctions_on_sketch(sketch):
    """{geometry index: [(t, local_point), ...]} for every T-junction on
    this sketch's own drawable (non-construction) straight geometry.  Thin
    FreeCAD-reading wrapper around find_t_junctions -- stub-testable with
    the same Point/Vertex/Edge/Geometry/Facade/Sketch stubs
    test_fcbridge_air_boundary_overrides.py already has.

    A curved wall-centerline (arc, circle) is out of scope for this pass --
    skipped here, not silently: an edge report of "crosses a T-junction"
    only ever fires for LineSegment geometry, so a curved wall with a
    T-junction on it goes unnoticed by this check specifically and falls
    through to whatever intersectSurfaces/matchSurfaces would already have
    done with it.
    """
    facades = sketch.GeometryFacadeList
    edges = []
    for index, geometry in enumerate(sketch.Geometry):
        if index < len(facades) and facades[index].Construction:
            continue
        if type(geometry).__name__ != "LineSegment":
            continue
        try:
            edge = geometry.toShape()
        except Exception:            # pragma: no cover - OCC edge cases
            continue
        edges.append((index, edge.Vertexes[0].Point, edge.Vertexes[1].Point))
    return find_t_junctions(edges)


def split_t_junctions(sketch, junctions=None):
    """Split every T-junction, without ever modifying the *original* edge.

    Splitting an edge directly -- the first version of this function --
    measured to silently break anything else in the document holding a
    live External Geometry reference into it: a project's own elevation
    sketches and its own Level 2 story both reference Level 1's wall edges
    exactly this way, and splitting Level 1 broke both, cascading into
    "many walls disappeared" and Sketcher errors ("Failed to project
    external geometry", "Invalid shape name") on recompute. Splitting a
    line changes the underlying topology even when SketchObject.split()
    itself keeps the same GeoId for the "before" piece, and that is enough
    to invalidate a reference that expected the whole, original edge.

    So instead: **copy** the edge in place, **pin** the copy to the
    original with two Coincident constraints, **demote the original to
    construction** (its topology is untouched, only excluded from
    Shape.Edges/room-face extraction -- confirmed this alone does not
    disturb an external reference into it), and only then **split the
    copy**. Any external reference to the original edge keeps resolving,
    before and after the copy is split, because the original is never
    touched beyond a construction-flag toggle. Verified end to end against
    a real project's own Level 1/Level 2 external-geometry relationship.

Every `Part::GeometryStringExtension` on the original -- not just
    OS_AirBoundaryOverride specifically, generically, the same way
    attachment_edge_references() scans by property *type* rather than a
    hardcoded name -- is copied onto the new copy explicitly: unlike
    split() acting on the same element, addGeometry() creates a plain new
    element with no extensions of its own, so this cannot rely on split()'s
    own automatic inheritance until the copy already carries them.
    (getExtensions() also returns an internal SketchGeometryExtension with
    no Name/Value of its own -- deliberately not copied, filtered out by
    isinstance.) From the copy onward, split()'s own inheritance takes
    over: verified directly it propagates *every* string extension present,
    not just a single known one, onto both pieces of a further split, and a
    Coincident constraint joining them is added automatically.

    junctions defaults to t_junctions_on_sketch(sketch), read from the
    *original* geometry before any copying starts. For the points on one
    original edge, they are applied in increasing-t order to the copy,
    and after each split the next point targets len(sketch.Geometry) - 1
    -- the newest entry -- for the same reason split() always keeps a
    split target's own GeoId for the "before" piece and appends a new one
    for the "after" piece.

    Each split point is additionally pinned, with its own Coincident
    constraint, to the other edge's endpoint that caused it
    (junctions' own other_key/other_pos_id) -- split() only cuts at a
    coordinate snapshot, it does not know or care that the point is
    supposed to coincide with anything else, so without this the two
    would only coincide until something moves either one. Verified this
    is not redundant with anything split() already adds (a plain
    two-point solve, no over-constraint warning) and that it survives a
    *later* split elsewhere in the network: a Coincident constraint
    referencing an endpoint that a subsequent split relocates is itself
    migrated by split() onto whichever new piece now holds that
    endpoint, confirmed directly by moving the far end of an
    already-pinned, already-split copy and watching the correct (new)
    tail piece follow it, not the original near piece.

    Returns [(original_geo_index, copy_geo_index, [new_geo_index, ...])]
    for the caller's report.
    """
    import Sketcher

    if junctions is None:
        junctions = t_junctions_on_sketch(sketch)

    results = []
    for geo_index, points in junctions.items():
        geo = sketch.Geometry[geo_index]
        edge = geo.toShape()
        p0, p1 = edge.Vertexes[0].Point, edge.Vertexes[1].Point
        copy_id = sketch.addGeometry(Part.LineSegment(p0, p1), False)

        facades = sketch.GeometryFacadeList
        original = facades[geo_index]
        for ext in original.getExtensions():
            if isinstance(ext, Part.GeometryStringExtension):
                facades[copy_id].setExtension(ext)
        original.Construction = True
        sketch.GeometryFacadeList = facades

        sketch.addConstraint(
            Sketcher.Constraint('Coincident', copy_id, 1, geo_index, 1))
        sketch.addConstraint(
            Sketcher.Constraint('Coincident', copy_id, 2, geo_index, 2))

        current = copy_id
        new_indices = []
        for _t, point, other_key, other_pos_id in points:
            sketch.split(current, FreeCAD.Vector(*point))
            # `current`'s own PosId 2 is the split point just created, on
            # the "before" piece that this loop never splits again --
            # pin it now, once, to the edge that actually caused it.
            sketch.addConstraint(Sketcher.Constraint(
                'Coincident', current, 2, other_key, other_pos_id))
            current = len(sketch.Geometry) - 1
            new_indices.append(current)
        results.append((geo_index, copy_id, new_indices))
    return results


def _split_descendants(sketch, copy_id):
    """Every geo_id produced by one split_t_junctions() run for one
    original edge, starting from its copy_id -- copy_id itself plus every
    piece reachable by following split()'s own auto-added "Coincident
    (piece, 2, next_piece, 1)" joining constraint, in order.
    """
    chain = [copy_id]
    current = copy_id
    while True:
        next_id = None
        for c in sketch.Constraints:
            if c.Type != "Coincident":
                continue
            if c.First == current and c.FirstPos == 2 and c.SecondPos == 1:
                next_id = c.Second
                break
            if c.Second == current and c.SecondPos == 2 and c.FirstPos == 1:
                next_id = c.First
                break
        if next_id is None or next_id in chain:
            break
        chain.append(next_id)
        current = next_id
    return chain


def find_split_originals(sketch):
    """{original_geo_id: [copy_id, descendant_id, ...]} for every edge
    split_t_junctions() has already copy+demoted on this sketch, detected
    from the Coincident-constraint signature it leaves behind rather than
    any record kept elsewhere -- this is what makes undo_t_junction_splits
    possible even for a sketch normalized in an earlier session, with no
    history of what was done available.

    Detected from the *copy's* side, not the original's: the copy is a
    freshly addGeometry()'d element, so its own PosId 1 has exactly one
    Coincident constraint on it ever -- the original-to-copy pin
    split_t_junctions adds before any splitting of the copy -- whereas the
    *original*'s own PosId 1 may also carry a pre-existing constraint from
    whatever real neighbour it was already drawn touching there, measured
    directly on the sample, so requiring uniqueness on the original's side
    instead would pick up that neighbour at random. The matching PosId-2
    pin is deliberately not used to confirm the pair: split()'s own
    constraint migration (verified directly -- splitting an edge relocates
    any pre-existing constraint on its far endpoint onto whichever new
    piece now holds it) moves that pin onto the copy's final tail piece
    the moment the copy is split even once, so it no longer references the
    copy's own GeoId at all. Walking the split chain forward from the
    copy (_split_descendants) finds every piece regardless.
    """
    facades = sketch.GeometryFacadeList
    at_pos1 = {}
    for c in sketch.Constraints:
        if c.Type != "Coincident":
            continue
        for this_id, this_pos, other_id, other_pos in (
                (c.First, c.FirstPos, c.Second, c.SecondPos),
                (c.Second, c.SecondPos, c.First, c.FirstPos)):
            if this_pos != 1 or this_id < 0 or other_id < 0:
                continue
            at_pos1.setdefault(this_id, []).append((other_id, other_pos))

    originals = {}
    for copy_id, facade in enumerate(facades):
        if facade.Construction:
            continue                  # candidates are the copy, which is real
        links = at_pos1.get(copy_id, [])
        if len(links) != 1:
            continue                  # a fresh copy's own start ties exactly once
        original_id, other_pos = links[0]
        if other_pos != 1 or original_id >= len(facades):
            continue
        if not facades[original_id].Construction:
            continue
        originals[original_id] = _split_descendants(sketch, copy_id)
    return originals


def undo_t_junction_splits(sketch):
    """Reverse every split_t_junctions() transformation on this sketch:
    delete each copy and its split descendants, and restore the original
    edge's Construction flag to False.

    Safe because the original was never otherwise modified -- copy+pin+
    demote+split never touches the original's own topology or extensions,
    only its Construction flag, so undoing is exactly "delete what was
    added, flip the flag back", not a reconstruction of anything.  Any
    OS_AirBoundaryOverride tag the original carried was never removed from
    it either (only copied onto the new copy alongside), so it is already
    back in place the moment the flag flips.

    That safety claim covers only this sketch's own geometry. It does NOT
    cover another object's Attachment into this sketch: repair_edge_attachments
    converts an EdgeN reference to that edge's own endpoint VertexN names
    *before* splitting, specifically so FreeCAD's own vertex-identity
    tracking carries the reference forward correctly across the split --
    and that tracking is one-directional. Confirmed directly on doc05:
    undoing a split whose original had already been through
    repair_edge_attachments left three elevation sketches (OS_Elev_South/
    North/West) State=Invalid, because their AttachmentSupport still named
    the *post-split* vertex numbers, which no longer existed once the split
    pieces were deleted. Calling this on a sketch with such a consumer
    still attached will break it; use pin_existing_splits() instead when
    the actual goal is only to add crossing-wall pins split_t_junctions()
    didn't yet add, since that never changes the topology at all.

    Returns [(original_geo_index, [deleted_geo_id, ...])] for the caller's
    report.  Real FreeCAD only, not unit-tested -- verified manually.
    """
    originals = find_split_originals(sketch)
    to_delete = sorted(
        {gid for chain in originals.values() for gid in chain}, reverse=True)
    for gid in to_delete:
        sketch.delGeometry(gid)

    facades = sketch.GeometryFacadeList
    for geo_id in originals:
        facades[geo_id].Construction = False
    sketch.GeometryFacadeList = facades

    return [(geo_id, chain) for geo_id, chain in originals.items()]


def pin_existing_splits(sketch):
    """Add the crossing-wall Coincident pin split_t_junctions() adds today
    to every split point that was cut under an older, unpinned version of
    that function -- without touching the wall network's topology at all,
    so nothing downstream (an Attachment already re-pointed at this
    sketch's post-split vertex numbering, openings, shading, air
    boundaries) is disturbed. The alternative -- undo_t_junction_splits()
    then split_t_junctions() again -- changes GeoIds and vertex numbering
    and is only safe on a sketch nothing else has attached to; see that
    function's docstring for the regression this avoids.

    Reconstructs, for each already-split original edge
    (find_split_originals), exactly the same (t, point, other_key,
    other_pos_id) list split_t_junctions() would compute today, by calling
    find_t_junctions() against a synthetic edge list: every real
    (non-construction) edge that is not itself a split piece, plus each
    split original's own whole, never-modified span standing in for its
    fragments (safe to use directly -- copy+pin+demote+split never
    modifies the original's own topology). The i-th entry in that
    reconstructed list corresponds to the i-th piece in the original's
    split chain by construction: N split points always produce a chain of
    N+1 pieces, cut in increasing-t order, the same order
    find_t_junctions() returns -- so no matching by coordinate is needed,
    only zipping the two lists.

    A pin already present (for example because a piece was re-split more
    recently, after the crossing-wall fix shipped) is left alone -- this
    only ever adds a missing Coincident, never a duplicate.

    Returns (added, problems). added is
    [(original_geo_id, [(piece_geo_id, other_key, other_pos_id), ...])] for
    every original where at least one pin was newly added. problems is a
    list of human-readable strings for any original whose reconstructed
    T-junction count no longer matches its existing split-chain length --
    left untouched rather than guessed at, the same refuse-don't-guess
    posture as repair_edge_attachments.
    """
    import Sketcher

    originals = find_split_originals(sketch)
    descendant_ids = {gid for chain in originals.values() for gid in chain}

    facades = sketch.GeometryFacadeList
    edges = []
    for index, geometry in enumerate(sketch.Geometry):
        if type(geometry).__name__ != "LineSegment":
            continue
        if index in descendant_ids:
            continue
        if (index not in originals and index < len(facades)
                and facades[index].Construction):
            continue
        edge = geometry.toShape()
        edges.append((index, edge.Vertexes[0].Point, edge.Vertexes[1].Point))

    all_junctions = find_t_junctions(edges)

    existing = set()
    for c in sketch.Constraints:
        if c.Type != "Coincident":
            continue
        existing.add((c.First, c.FirstPos, c.Second, c.SecondPos))
        existing.add((c.Second, c.SecondPos, c.First, c.FirstPos))

    added = []
    problems = []
    for original_id, chain in originals.items():
        points = all_junctions.get(original_id, [])
        if len(points) != len(chain) - 1:
            problems.append(
                "geo %d: reconstructed %d T-junction point(s), expected %d "
                "for its %d-piece split chain -- left unpinned"
                % (original_id, len(points), len(chain) - 1, len(chain)))
            continue
        new_pins = []
        for piece_id, (_t, _point, other_key, other_pos_id) in zip(chain, points):
            if (piece_id, 2, other_key, other_pos_id) in existing:
                continue
            sketch.addConstraint(Sketcher.Constraint(
                'Coincident', piece_id, 2, other_key, other_pos_id))
            existing.add((piece_id, 2, other_key, other_pos_id))
            new_pins.append((piece_id, other_key, other_pos_id))
        if new_pins:
            added.append((original_id, new_pins))
    return added, problems


def _promoted_external_geo_ids(sketch):
    """Which negative GeoIds already have a local edge whose own two
    endpoints coincide with theirs -- promote_external_edges' own
    signature, checked geometrically rather than by scanning Constraints
    for a reference to the external edge's own geo_id.

    getConstruction(geo_id) looked like the obvious way to ask "has this
    external entry already been demoted to construction," but measured
    directly against a real file it returns True unconditionally for every
    external GeoId whether or not toggleConstruction has ever touched it
    (Shape.Edges still drops by one when toggled either way, so the actual
    per-entry state exists internally, just not through this getter).

    A first version scanned Constraints for a direct reference to the
    external edge's own geo_id instead -- correct for the single-Edge-
    reference pin an older version of promote_external_edges used, but
    wrong now that the new real edge is pinned to two *Vertex* references
    instead (a different geo_id entirely; nothing constrains the old Edge
    reference's own geo_id directly any more, only its demoted-but-kept
    presence). A geometric endpoint match catches a promotion done either
    way -- old direct-edge pin or new vertex-anchored one -- uniformly,
    without caring which mechanism did it.

    A second version only matched against *non*-construction local edges,
    on the theory that a promoted edge is real until something else flags
    it construction. Wrong for the specific case of a promoted edge that a
    later split_t_junctions() run then split: that demotes the *original*
    to construction without ever touching its topology, so its endpoints
    still coincide exactly with the external edge that seeded it -- but a
    non-construction-only match stopped recognizing it, confirmed directly
    (5 promoted-then-split entries on SecondFP Sketch kept getting
    reported as "not yet promoted" every subsequent call). Matching
    against every LineSegment regardless of Construction fixes this; the
    residual risk -- some unrelated construction edge happening to share
    both endpoints with an external reference by coincidence -- only ever
    causes that one entry to be skipped as "already handled" when it
    is not, the same benign failure mode the original bug already had.
    """
    def close(a, b):
        dx, dy, dz = a.x - b.x, a.y - b.y, a.z - b.z
        return (dx * dx + dy * dy + dz * dz) ** 0.5 <= WELD_TOL_MM

    real_edges = []
    for geo in sketch.Geometry:
        if type(geo).__name__ != "LineSegment":
            continue
        shape = geo.toShape()
        real_edges.append((shape.Vertexes[0].Point, shape.Vertexes[1].Point))

    promoted = set()
    for index, geometry in enumerate(sketch.ExternalGeo):
        if index < 2:
            continue
        try:
            shape = geometry.toShape()
            ep0, ep1 = shape.Vertexes[0].Point, shape.Vertexes[1].Point
        except Exception:             # pragma: no cover - OCC edge cases
            continue
        for rp0, rp1 in real_edges:
            if ((close(rp0, ep0) and close(rp1, ep1))
                    or (close(rp0, ep1) and close(rp1, ep0))):
                promoted.add(-(index + 1))
                break
    return promoted


def _defining_external_geo_ids(sketch):
    """geo_ids of every External Geometry edge entry FreeCAD is actually
    building this sketch's own Shape from -- addExternal(..., defining=True).

    defining=False is FreeCAD's own default, and the only value every
    addExternal call anywhere in this codebase ever passes (this function's
    own caller below, fc_seed_openings.py) -- so a non-defining entry is the
    ordinary kind, not a special case. It is a pure snapping/reference
    guide: measured directly against a real file, its endpoints never
    appear in sketch.Shape.Edges regardless of what it touches or how it is
    connected. README Sec. 6c documents the same behaviour for an opening
    sketch's own External Geometry: "adding four external edges moves
    neither [Shape.Wires nor edges] count."

    Nothing on the geometry entry itself carries this distinction where a
    script could just read it off: sketch.getConstruction(geo_id) returns
    True unconditionally for every External Geometry entry regardless of
    its defining status (measured directly, same dead end
    ExternalWallGeometryTests.test_an_already_promoted_entry_is_skipped
    hit trying to use it for a different check), and so does every
    SketchGeometryExtension/ExternalGeometryExtension property tried.
    Shape.Edges membership is the only signal found that actually varies.
    ExternalGeo's own toShape() returns points local to the sketch's own 2D
    plane (Z=0, no Placement applied -- confirmed directly, same as
    _external_geo_source_object's own p0_local/p1_local), while
    sketch.Shape.Edges is the sketch's real, global shape, Placement
    already baked in. A story sketch's Placement carries its elevation, so
    comparing the two coordinate spaces directly failed shut (every
    endpoint off by the whole story height, past WELD_TOL_MM, so nothing
    ever matched -- not just a non-defining entry, everything, measured
    directly the first time this was written without the transform below).
    sketch.Placement.multVec brings a local point into the same global
    frame Shape.Edges is already in, the same transform
    _external_geo_source_object uses to compare against a *different*
    object's own Placement.
    """
    def close(a, b):
        dx, dy, dz = a.x - b.x, a.y - b.y, a.z - b.z
        return (dx * dx + dy * dy + dz * dz) ** 0.5 <= WELD_TOL_MM

    edge_endpoints = []
    for edge in sketch.Shape.Edges:
        verts = edge.Vertexes
        if len(verts) != 2:
            continue
        edge_endpoints.append((verts[0].Point, verts[1].Point))

    defining = set()
    for index, geometry in enumerate(sketch.ExternalGeo):
        if index < 2:
            continue
        try:
            shape = geometry.toShape()
            ep0 = sketch.Placement.multVec(shape.Vertexes[0].Point)
            ep1 = sketch.Placement.multVec(shape.Vertexes[1].Point)
        except Exception:             # pragma: no cover - OCC edge cases
            continue
        for e0, e1 in edge_endpoints:
            if (close(e0, ep0) and close(e1, ep1)) \
                    or (close(e0, ep1) and close(e1, ep0)):
                defining.add(-(index + 1))
                break
    return defining


def external_wall_geometry(sketch):
    """(geo_id, geometry) for every real, still-official, DEFINING External
    Geometry *edge* entry on this sketch.  sketch.ExternalGeo index 0/1 are
    always the sketch's own H/V axis (geo_id -1/-2, confirmed empirically,
    not merely assumed from the classic Sketcher convention); real
    references start at index 2 (geo_id -3), one GeoId lower per entry.

    A Vertex-type entry (exactly one Vertexes -- promote_external_edges'
    own anchor references, or one added by hand) is skipped: a lone point
    can never be "an official wall" needing promotion in the first place,
    and counting one as a candidate here made promote_external_edges'
    every run add another (deduped, but still pointless) generation of
    vertex references on top of its own, confirmed directly.

    An entry promote_external_edges already pinned a real local line to
    (see _promoted_external_geo_ids) is skipped -- its own endpoints stay
    exactly coincident with that new line, so a naive "does this entry's
    line touch a face fragment" check would otherwise flag it forever.

    A non-defining entry (_defining_external_geo_ids) is skipped too, not
    merely left un-taggable. It was traced in as a snapping/reference guide
    on purpose -- addExternal's default -- and never appears in
    sketch.Shape.Edges regardless of promotion, so it can never itself
    border a room or sit at a T-junction. Promoting one would silently
    convert a reference someone deliberately did not want treated as a wall
    into real, local, defining geometry that Shape.Edges (and therefore
    extract_room_faces and every downstream room computation) then DOES
    see -- manufacturing exactly the room-split risk defining=False was
    meant to opt out of, for no benefit: there is no T-junction blind spot
    to close for an edge that structurally cannot border a room in the
    first place.
    """
    already = _promoted_external_geo_ids(sketch)
    defining = _defining_external_geo_ids(sketch)
    out = []
    for index, geometry in enumerate(sketch.ExternalGeo):
        if index < 2:
            continue                  # H axis, V axis
        try:
            if len(geometry.toShape().Vertexes) != 2:
                continue               # a point, not an edge
        except Exception:             # pragma: no cover - OCC edge cases
            continue
        geo_id = -(index + 1)
        if geo_id in already:
            continue
        if geo_id not in defining:
            continue                  # a snapping/reference guide, not
                                       # an official wall -- leave it alone
        out.append((geo_id, geometry))
    return out


def _known_external_vertex_geo_ids(sketch):
    """{(source_obj.Name, vertex_name): geo_id} for every Vertex-type
    External Geometry entry already on this sketch -- what
    promote_external_edges seeds its reuse cache with, so a vertex shared
    with an edge some *earlier* call already promoted (or that a user
    added by hand) is reused instead of re-added, which addExternal
    refuses as a duplicate.

    Resolved by coordinate match against each candidate source object, the
    same way _external_geo_source_object resolves an edge's source: a
    Vertex-type entry's own current SubElement name is exactly as prone to
    the ExternalGeometry name-collision problem described there, so this
    does not index into that flattened name list either. A Vertex-type
    entry is distinguished from an Edge-type one by its resolved shape
    having exactly one Vertex (confirmed directly: geometry type name
    'Point', shape type 'Vertex', shape.Vertexes has length 1 -- vs. 2 for
    an edge).
    """
    candidates = []
    for obj, _names in sketch.ExternalGeometry:
        if obj not in candidates:
            candidates.append(obj)

    known = {}
    for index, geometry in enumerate(sketch.ExternalGeo):
        if index < 2:
            continue
        try:
            shape = geometry.toShape()
            verts = shape.Vertexes
        except Exception:             # pragma: no cover - OCC edge cases
            continue
        if len(verts) != 1:
            continue                  # an edge (2 vertices), not a point
        point_local = verts[0].Point
        for obj in candidates:
            point_in_obj_frame = obj.Placement.multVec(point_local)
            vname = _vertex_name_at(obj, point_in_obj_frame)
            if vname is not None:
                known[(obj.Name, vname)] = -(index + 1)
                break
    return known


def _external_geo_source_object(sketch, ext_geo_index):
    """Which object sketch.ExternalGeo[ext_geo_index] (>= 2) was created
    from.

    Not found by counting through sketch.ExternalGeometry's own flattened
    subelement names: that property groups entries by source object with
    one name tuple per object, and measured directly after a 25-edge
    split on the source, an originally 19-entry tuple collapsed to 10
    names -- an extensive topology change on the source can make two
    originally-distinct external references resolve to the *same* current
    name, and FreeCAD's own display list merges them, so a position in
    that flattened list no longer corresponds 1:1 with sketch.ExternalGeo's
    own order. sketch.ExternalGeo[ext_geo_index] itself is unaffected by
    this (each entry keeps its own correctly-resolved geometry regardless
    of what name it currently displays under), so this instead resolves
    the object by testing which candidate object's own Shape the resolved
    edge's endpoints actually belong to.

    Returns None -- refuse, don't guess -- if sketch.ExternalGeometry lists
    more than one candidate object and more than one matches, or if none
    does; unambiguous for the overwhelmingly common case of a story sketch
    referencing exactly one other story.
    """
    candidates = []
    for obj, _names in sketch.ExternalGeometry:
        if obj not in candidates:
            candidates.append(obj)
    if len(candidates) == 1:
        return candidates[0]

    edge_shape = sketch.ExternalGeo[ext_geo_index].toShape()
    p0_local = edge_shape.Vertexes[0].Point
    p1_local = edge_shape.Vertexes[1].Point
    matches = []
    for obj in candidates:
        # External Geometry copies the source's own local 2D coordinates
        # as-is into this sketch's ExternalGeo, ignoring both sketches'
        # own Placement (their story elevation) entirely -- confirmed
        # directly: transforming through THIS sketch's Placement put a
        # point exactly this sketch's own Z-elevation away from every
        # candidate vertex, while the source's OWN Placement lines up
        # exactly. So p0_local/p1_local are treated as local to `obj`,
        # not to `sketch`.
        p0 = obj.Placement.multVec(p0_local)
        p1 = obj.Placement.multVec(p1_local)
        if _vertex_name_at(obj, p0) is not None and _vertex_name_at(obj, p1) is not None:
            matches.append(obj)
    return matches[0] if len(matches) == 1 else None


def promote_external_edges(sketch):
    """Promote every still-official External Geometry edge on this sketch
    (external_wall_geometry(sketch) -- unconditionally, no "does this
    border a labelled room" filter) into a real, local, taggable
    Part.LineSegment, anchored to that edge's own two endpoints via two
    Vertex-only external references, not the one Edge reference it started
    as. The old Edge reference is kept, demoted to construction (drops out
    of Shape.Edges, same as ever) rather than deleted -- delExternal's own
    0-based index has the same flattened-list fragility
    _external_geo_source_object's docstring describes, confirmed directly:
    deleting by an index computed before a later entry's own name collided
    would delete the wrong entry. A little inert cruft left behind is a
    small price for never deleting the wrong thing.

    Why unconditional for every DEFINING edge (external_wall_geometry
    already filters to just those -- see its own docstring and
    _defining_external_geo_ids for what "defining" means and why a
    non-defining edge is excluded from this reasoning entirely):
    t_junctions_on_sketch only ever iterates sketch.Geometry, never
    sketch.ExternalGeo, so any defining External Geometry edge left
    un-promoted is invisible to T-junction detection regardless of whether
    it borders a room -- confirmed directly (FloorplanTest-05, Level 2:
    un-promoted entries invisible to t_junctions_on_sketch regardless of
    room adjacency). extract_room_faces reads sketch.Shape.Edges, which
    includes a defining External Geometry edge whether or not it has been
    promoted (confirmed directly: Shape.Edges on that same sketch was
    longer than its real, non-construction Geometry count, by exactly the
    un-promoted defining entries) -- so promoting one that turns out not to
    border any labelled room changes nothing about room detection, only
    makes it visible to T-junction detection too, same as every other
    wall. A non-defining edge gets none of this: Shape.Edges never includes
    one regardless of promotion, so there is no room-detection blind spot
    promoting it could ever close, only one it could manufacture.

    Why Vertex-only, not Edge, anchoring: not required for the reference to
    survive an unrelated edge elsewhere in the source sketch being demoted
    to construction later -- verified directly that an Edge-based
    ExternalGeometry reference already re-resolves correctly across that
    ordinal shift for a single demotion (its stored SubElement name
    updates, State stays Up-to-date, endpoints unchanged), unlike
    AttachmentSupport's raw EdgeN addressing. Done anyway for consistency,
    and because it turns out to matter for a different reason at scale:
    resolving *this* promotion's own source object and vertex names by
    coordinate match, not by the source edge's current EdgeN name, is what
    keeps this working even where the name-collision problem above would
    otherwise make an Edge-based lookup ambiguous.

    A vertex shared by two adjacent walls (the common case -- every
    interior corner) is only ever added once and reused for both: measured
    directly, addExternal refuses a duplicate (object, subelement) pair
    outright ("Not able to add external shape element VertexN"), and
    promoting edge44 then edge45 of the same corner would otherwise ask
    for that shared VertexN twice.

    An entry whose source object can't be resolved
    (_external_geo_source_object), or whose edge's endpoints cannot be
    matched back to a 'VertexN' name on that object, is left untouched
    rather than guessed at -- refuse, don't guess, the same posture as
    repair_edge_attachments.

    Returns [(old_geo_id, new_geo_index), ...] for the caller's report.
    Real FreeCAD only, not unit-tested -- verified manually.
    """
    import Sketcher

    entries = external_wall_geometry(sketch)
    if not entries:
        return []

    # (source_obj.Name, vertex_name) -> geo_id, seeded from every
    # Vertex-type entry already present (an earlier call's own promotions,
    # or one added by hand) and kept up to date as new ones are added in
    # this call -- two adjacent walls share a corner vertex, so promoting
    # both wants the same VertexN twice; addExternal refuses a duplicate
    # (object, subelement) pair outright, measured directly, so each one
    # is only ever added once and reused.
    known_geo_id = _known_external_vertex_geo_ids(sketch)

    promotions = []
    for geo_id, geometry in entries:
        ext_geo_index = -geo_id - 1

        source_obj = _external_geo_source_object(sketch, ext_geo_index)
        if source_obj is None:
            continue                  # ambiguous source -- leave alone

        edge_shape = geometry.toShape()
        p0_local = edge_shape.Vertexes[0].Point
        p1_local = edge_shape.Vertexes[1].Point
        # p0_local/p1_local are local to source_obj, not to sketch -- see
        # _external_geo_source_object's own comment on this.
        p0_in_source_frame = source_obj.Placement.multVec(p0_local)
        p1_in_source_frame = source_obj.Placement.multVec(p1_local)

        v1_name = _vertex_name_at(source_obj, p0_in_source_frame)
        v2_name = _vertex_name_at(source_obj, p1_in_source_frame)
        if v1_name is None or v2_name is None:
            continue                  # endpoint unresolved -- leave alone

        vertex_ids = []
        for vname in (v1_name, v2_name):
            key = (source_obj.Name, vname)
            if key not in known_geo_id:
                before_count = len(sketch.ExternalGeo)
                sketch.addExternal(source_obj.Name, vname)
                known_geo_id[key] = -(before_count + 1)
            vertex_ids.append(known_geo_id[key])
        v1_geo_id, v2_geo_id = vertex_ids

        new_id = sketch.addGeometry(Part.LineSegment(p0_local, p1_local), False)
        sketch.addConstraint(
            Sketcher.Constraint('Coincident', new_id, 1, v1_geo_id, 1))
        sketch.addConstraint(
            Sketcher.Constraint('Coincident', new_id, 2, v2_geo_id, 1))
        sketch.toggleConstruction(geo_id)

        promotions.append((geo_id, new_id))

    return promotions


def wall_network_problems(sketch):
    """Human-readable problem strings for fc_export_floorplan.py's existing
    problems/--allow-problems refusal -- the export-time guard that keeps
    the wall network normalized (every edge borders at most two rooms,
    every official wall is real local geometry) from rotting once a plan
    is edited again after normalize_walls.FCMacro last ran.

    Combines t_junctions_on_sketch and external_wall_geometry -- the same
    two functions normalize_walls.FCMacro itself calls, so the preview and
    the export enforcement can never disagree. Any remaining DEFINING
    External Geometry edge is a problem now, not just one bordering a
    labelled room: promote_external_edges promotes every defining edge
    unconditionally (t_junctions_on_sketch can't see an un-promoted one
    regardless of whether it borders a room), so anything
    external_wall_geometry still finds means normalize_walls.FCMacro has
    not been run since, or was refused on it. A non-defining edge is never
    reported here at all -- external_wall_geometry excludes it, since it is
    a deliberate snapping/reference guide with nothing for
    normalize_walls.FCMacro to fix.
    """
    problems = []

    junctions = t_junctions_on_sketch(sketch)
    if junctions:
        problems.append(
            "  %s: %d wall edge(s) cross a T-junction without a matching "
            "sketch split -- run normalize_walls.FCMacro before exporting"
            % (sketch.Label, len(junctions)))

    remaining = external_wall_geometry(sketch)
    if remaining:
        problems.append(
            "  %s: %d External Geometry edge(s) not yet promoted to real, "
            "taggable geometry -- run normalize_walls.FCMacro before "
            "exporting"
            % (sketch.Label, len(remaining)))

    return problems


ORDINAL_NAME_RE = re.compile(r"^(Edge|Vertex|Face)(\d+)$")

LINKSUB_PROPERTY_TYPES = ("App::PropertyLinkSub", "App::PropertyLinkSubList")


def _edge_ordinal(name):
    """int ordinal from 'Edge21', or None if `name` isn't that shape."""
    match = ORDINAL_NAME_RE.match(name)
    if match and match.group(1) == "Edge":
        return int(match.group(2))
    return None


def _vertex_name_at(sketch, global_point, tol=WELD_TOL_MM):
    """The 'VertexN' name (1-based, matching sketch.Shape.Vertexes order)
    at global_point, or None if nothing is that close.

    This is the read side of the fact that makes repair_edge_attachments
    possible: FreeCAD's own Shape.Vertexes enumeration is exactly what
    AttachmentSupport's 'VertexN' names index into, so matching by
    position here is matching the same thing FreeCAD itself resolves the
    name against -- not a parallel, possibly-drifting numbering.
    """
    gx, gy, gz = global_point.x, global_point.y, global_point.z
    for index, vertex in enumerate(sketch.Shape.Vertexes):
        p = vertex.Point
        dx, dy, dz = p.x - gx, p.y - gy, p.z - gz
        if (dx * dx + dy * dy + dz * dz) ** 0.5 <= tol:
            return "Vertex%d" % (index + 1)
    return None


def edge_ordinal_endpoints_as_vertex_names(sketch, edge_ordinal):
    """('VertexN', 'VertexM') for Shape.Edge<edge_ordinal>'s two endpoints
    on `sketch` (1-based ordinal, matching the 'EdgeN' naming convention),
    or (None, None) if either endpoint can't be matched back to a vertex --
    should not happen for a real, closed wall network, but reported rather
    than assumed if it does.
    """
    edge = sketch.Shape.Edges[edge_ordinal - 1]
    p0, p1 = edge.Vertexes[0].Point, edge.Vertexes[1].Point
    return _vertex_name_at(sketch, p0), _vertex_name_at(sketch, p1)


def attachment_edge_references(doc):
    """[(consumer_obj, prop_name, target_sketch, subelement_names)] for
    every non-empty PropertyLinkSub/PropertyLinkSubList property anywhere
    in the document whose target is a Sketcher::SketchObject and whose
    subelement_names contains at least one 'EdgeN'-style name.

    Generic, type-driven scan (getTypeIdOfProperty) rather than a
    hardcoded property-name list -- this is what surfaces every consumer
    regardless of which property holds the reference (AttachmentSupport,
    but also Profile/ReferenceAxis or anything else with this same
    property type), the same way this project already refuses to guess at
    a fixed set of names elsewhere.
    """
    out = []
    for obj in doc.Objects:
        for prop_name in obj.PropertiesList:
            try:
                type_id = obj.getTypeIdOfProperty(prop_name)
            except Exception:            # pragma: no cover - stale property
                continue
            if type_id not in LINKSUB_PROPERTY_TYPES:
                continue
            value = getattr(obj, prop_name)
            if not value:
                continue
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if not entry or entry[0] is None:
                    continue
                target, subnames = entry
                if target.TypeId != "Sketcher::SketchObject":
                    continue
                if any(_edge_ordinal(n) is not None for n in subnames):
                    out.append((obj, prop_name, target, subnames))
    return out


REPAIRABLE_ATTACHMENT_PROPERTY = "AttachmentSupport"


def repair_edge_attachments(doc, sketch):
    """Rewrite every AttachmentSupport entry targeting `sketch` so its
    'EdgeN' subelements become that edge's own two endpoint 'VertexN'
    names instead -- any existing 'VertexN' entry is left as-is.

    Only AttachmentSupport is touched, even though attachment_edge_references()
    itself finds every LinkSub property generically (ExternalGeometry
    included). Measured directly: ExternalGeometry is a read-only property
    (FreeCAD manages it through addExternal/delExternal, not assignment --
    attempting setattr raises AttributeError), and it does not need this
    fix anyway -- its 'EdgeN' names are already resolved through a stable,
    self-updating mechanism verified to survive split_t_junctions on its
    own (unlike AttachmentSupport's raw ordinal addressing, which does
    not). Any other LinkSub property this scan turns up (Profile,
    ReferenceAxis, ...) is left alone too, for the same reason
    attachment_edge_references() itself refuses to guess: no evidence
    either way about how it behaves, so touching it would be a new,
    unverified assumption of exactly the kind that caused the last two
    regressions.

    Verified this survives split_t_junctions where a raw 'EdgeN'
    AttachmentSupport reference does not: FreeCAD's Attachment engine
    auto-renumbers a 'VertexN' reference across a topology change
    (confirmed directly -- splitting the edge a vertex-based
    AttachmentSupport referenced left the object's State 'Up-to-date' and
    its Placement bit-for-bit unchanged, the stored names simply updated
    to the vertices' new ordinals), but does not do the same for 'EdgeN'.
    Must run BEFORE split_t_junctions touches this sketch -- converting
    after the fact would be converting an already-broken reference.

    An entry that cannot be fully converted (an endpoint vertex not found)
    is left untouched and reported as a problem instead of guessed at.

    Returns (converted, problems):
      converted: [(consumer_label, prop_name, old_subelements, new_subelements)]
      problems: human-readable strings, sketch.Label-prefixed
    """
    converted, problems = [], []
    for obj, prop_name, target, subnames in attachment_edge_references(doc):
        if target is not sketch or prop_name != REPAIRABLE_ATTACHMENT_PROPERTY:
            continue
        new_names = []
        ok = True
        for name in subnames:
            ordinal = _edge_ordinal(name)
            if ordinal is None:
                new_names.append(name)
                continue
            v0, v1 = edge_ordinal_endpoints_as_vertex_names(sketch, ordinal)
            if v0 is None or v1 is None:
                ok = False
                break
            new_names.extend([v0, v1])
        if not ok:
            problems.append(
                "  %s: could not repair %r's %s (%s) onto vertices -- "
                "left as-is, and as an Edge-ordinal reference it will not "
                "survive a T-junction split here"
                % (sketch.Label, obj.Label, prop_name, ", ".join(subnames)))
            continue

        new_value = (target, tuple(new_names))
        current = getattr(obj, prop_name)
        if isinstance(current, list):
            updated = [new_value if entry[0] is target and
                      entry[1] == subnames else entry for entry in current]
            setattr(obj, prop_name, updated)
        else:
            setattr(obj, prop_name, new_value)
        converted.append((obj.Label, prop_name, subnames, tuple(new_names)))
    return converted, problems


def document_object_errors(doc):
    """{obj.Name: list(obj.State)} for every object whose State is not
    exactly ['Up-to-date'] after the caller's own doc.recompute().

    The whole-document safety gate normalize_walls.FCMacro checks after
    every mutating step -- obj.State is the same signal the GUI's own
    Report View and object-tree error icons are driven by, confirmed
    directly to catch what a narrower "did this one call raise" check
    missed (a stale/fallback shape can still be returned without raising
    even once FreeCAD's own recompute has already flagged the object
    Invalid).
    """
    out = {}
    for obj in doc.Objects:
        state = list(obj.State) if hasattr(obj, "State") else []
        if state and state != ["Up-to-date"]:
            out[obj.Name] = state
    return out


# --------------------------------------------------------------------------
# room extraction
# --------------------------------------------------------------------------

def local_shape(shape, sketch):
    """A copy of `shape` expressed in the sketch own frame, where the sketch
    lies in the z=0 plane."""
    out = shape.copy()
    out.transformShape(sketch.Placement.inverse().toMatrix())
    return out


def local_point(point, sketch):
    """A global point expressed in the sketch own frame."""
    return sketch.Placement.inverse().multVec(point)


def plane_distance(point, sketch):
    """Perpendicular distance from a global point to the sketch plane."""
    return abs(local_point(point, sketch).z)


def extract_room_faces(sketch):
    """Enclosed regions of a sketch line network, exterior region removed.

    Faces come back in the document *global* frame so label positions stay
    meaningful, but every position test happens in the sketch own frame.  A
    story sketch may sit anywhere: laid out beside its neighbours on the sheet,
    or stacked at its real elevation, in which case it is nowhere near the
    global z=0 plane.
    """
    edges = sketch.Shape.Edges
    if not edges:
        raise BridgeError("sketch %r has no edges" % sketch.Label)

    bb = local_shape(sketch.Shape, sketch).BoundBox
    pad = SLICE_PAD_MM
    rect = Part.makePlane(
        bb.XLength + 2 * pad,
        bb.YLength + 2 * pad,
        FreeCAD.Vector(bb.XMin - pad, bb.YMin - pad, 0),
    )
    # Lift the slicing rectangle out of the local frame into the sketch plane,
    # wherever that is.  transformShape moves the geometry itself, so this does
    # not depend on how makePlane distributes position between shape and
    # placement.
    rect.transformShape(sketch.Placement.toMatrix())
    pieces = SplitAPI.slice(rect, edges, "Split")

    x_lo, x_hi = bb.XMin - pad, bb.XMax + pad
    y_lo, y_hi = bb.YMin - pad, bb.YMax + pad

    rooms = []
    for face in pieces.Faces:
        lb = local_shape(face, sketch).BoundBox
        touches_boundary = (
            abs(lb.XMin - x_lo) < 1e-6 or abs(lb.XMax - x_hi) < 1e-6
            or abs(lb.YMin - y_lo) < 1e-6 or abs(lb.YMax - y_hi) < 1e-6
        )
        if not touches_boundary:
            rooms.append(face)

    if not rooms:
        raise BridgeError(
            "sketch %r yielded no enclosed regions from %d edges.\n"
            "The wall-centerline network is probably not closed -- check for "
            "gaps at\ncorners, or edges that only look joined."
            % (sketch.Label, len(edges)))
    return rooms


def _dist2(a, b):
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy


def _weld(points, tol):
    out = []
    for p in points:
        if out and _dist2(out[-1], p) < tol * tol:
            continue
        out.append(p)
    while len(out) > 1 and _dist2(out[0], out[-1]) < tol * tol:
        out.pop()
    return out


def _drop_collinear(points, tol):
    """Remove vertices lying within tol of the segment joining their
    neighbours.  Iterates until stable, since removing one can expose another.
    """
    pts = list(points)
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            abx, aby = b[0] - a[0], b[1] - a[1]
            acx, acy = c[0] - a[0], c[1] - a[1]
            span = math.hypot(acx, acy)
            if span < tol:
                continue
            # perpendicular distance from b to the line a->c
            if abs(abx * acy - aby * acx) / span < tol:
                del pts[i]
                changed = True
                break
    return pts


def signed_area(points):
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def face_polygon_m(face, sketch):
    """Outer boundary of face as CCW [x, y] pairs in metres.

    Coordinates are expressed in the sketch own frame -- every story Placement
    is its origin, so stories drawn side by side on the sheet stack correctly.
    Using the full inverse Placement (not just Base) means a rotated sketch
    works too.
    """
    inv = sketch.Placement.inverse()
    raw = [inv.multVec(v.Point) for v in face.OuterWire.OrderedVertexes]
    pts = [(p.x, p.y) for p in raw]

    pts = _weld(pts, WELD_TOL_MM)
    pts = _drop_collinear(pts, COLLINEAR_TOL_MM)

    if len(pts) < 3:
        raise BridgeError("degenerate polygon after cleanup (%d vertices)"
                          % len(pts))

    if signed_area(pts) < 0:
        pts.reverse()

    return [[round(x / MM_PER_M, 6), round(y / MM_PER_M, 6)] for x, y in pts]


# --------------------------------------------------------------------------
# room labels
# --------------------------------------------------------------------------

def is_room_label(obj):
    """A Draft Text carrying room identity.

    Draft Text objects are App::FeaturePython with a Text property (a list of
    strings) and a Placement.  Layers are also App::FeaturePython, hence the
    Text check rather than a bare type check.
    """
    if not hasattr(obj, "Text") or not hasattr(obj, "Placement"):
        return False
    proxy = getattr(obj, "Proxy", None)
    if proxy is not None and type(proxy).__name__ in ("Layer", "LayerContainer"):
        return False
    return True


def room_labels(doc):
    return [o for o in doc.Objects if is_room_label(o)]


def label_point(label):
    """The label anchor in global coordinates.

    This is Placement.Base -- the text insertion point at the start of line 1,
    not the visual centre of the text block.
    """
    b = label.Placement.Base
    return FreeCAD.Vector(b.x, b.y, b.z)


def labels_for_sketch(sketch, labels, sketches):
    """The labels belonging to this story.

    Stories may be laid out side by side on the sheet -- every sketch in the
    same plane -- or stacked at their real elevations.  Stacked, in-plane
    position alone is ambiguous: every story shares the same footprint, so a
    Level 1 label sits inside a Level 2 room too.  Assigning each label to the
    story plane it is *nearest* to resolves that.  Laid out side by side the
    planes coincide, every label ties, and in-plane containment does the
    disambiguation exactly as it always did.
    """
    if len(sketches) < 2:
        return list(labels)
    mine = []
    for label in labels:
        pt = label_point(label)
        nearest = min(plane_distance(pt, s) for s in sketches)
        if plane_distance(pt, sketch) <= nearest + PLANE_TOL_MM:
            mine.append(label)
    return mine


def parse_label_text(label):
    """Turn the first text line into (room_number, name).

    "105 | Office" -> ("105", "Office").  Without a separator the whole line
    is the name, unless it starts with something that looks like a room
    number, so the convention degrades gracefully.
    """
    lines = [s.strip() for s in (label.Text or []) if s and s.strip()]
    if not lines:
        return "", ""
    first = lines[0]
    if "|" in first:
        num, _, name = first.partition("|")
        return num.strip(), name.strip()
    m = re.match(r"^([A-Za-z]{0,3}[-_]?\d{1,4}[A-Za-z]?)\s+(.*)$", first)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", first


def sync_label_display(label):
    """Make the tree Label read the same as the drawn Text.

    A fresh label gets an auto-numbered Label ("Level 1 R06") because there
    is nothing else to call it yet.  Nothing downstream reads Label for room
    identity -- parse_label_text and is_room_label both work from Text -- so
    once a room is renamed, leaving the old auto-numbered Label behind is
    pure cost: the object becomes unfindable in the tree by anything but
    scrolling.  Mirroring Text here is free.  Returns True when it changed.
    """
    lines = [s.strip() for s in (label.Text or []) if s and s.strip()]
    if not lines or label.Label == lines[0]:
        return False
    label.Label = lines[0]
    return True


SKIP_TOKENS = ("SKIP", "-", "OPEN TO BELOW", "OPENTOBELOW", "NOTASPACE")


def is_skip_label(label):
    """True when a label deliberately marks a region as *not* a space.

    Every enclosed region has to be accounted for, but not every one is a
    room: the area under a partial mezzanine, an open-to-below void, or a
    courtyard inside the envelope all show up as enclosed faces.  Labelling
    them SKIP records the decision explicitly instead of leaving a silent
    hole, which is the whole point of refusing to export unlabeled regions.
    """
    lines = [s.strip() for s in (label.Text or []) if s and s.strip()]
    if not lines:
        return False
    first = lines[0].strip().upper()
    # The marker may sit in either field of "NUM | Name" -- "SKIP" alone,
    # "SKIP | mezzanine void", or "- | open to below" all mean the same thing.
    candidates = [first]
    if "|" in first:
        before, _, after = first.partition("|")
        candidates += [before.strip(), after.strip()]
    return any(c == token or c.startswith(token + " ")
               for c in candidates if c
               for token in SKIP_TOKENS)


def get_or_mint_space_id(label):
    """Stable UUID for this room, minted on first sight.

    Returns (space_id, was_minted).  The caller saves the document if any id
    was minted -- this is the identity anchor the whole change-propagation
    story hangs on, so it has to survive in the FCStd.
    """
    if not hasattr(label, ID_PROP):
        label.addProperty("App::PropertyString", ID_PROP, "OpenStudio",
                          "Stable OpenStudio space identity")
        setattr(label, ID_PROP, "")
    if not getattr(label, ID_PROP):
        setattr(label, ID_PROP, str(uuid.uuid4()))
        return getattr(label, ID_PROP), True
    return getattr(label, ID_PROP), False


def ensure_height_prop(label):
    """Add the OS_Height override property to a label if it is missing.

    Added to every room label rather than only the rooms that need it, so the
    field is already sitting in the property editor's OpenStudio group when
    you want it.  FreeCAD's "add a property" dialog is fiddly enough that a
    property you have to create by hand is one nobody uses.

    Returns True when the property was added (caller decides whether to save).
    """
    if hasattr(label, HEIGHT_PROP):
        return False
    label.addProperty("App::PropertyLength", HEIGHT_PROP, "OpenStudio",
                      "Space height; 0 = use the story floor-to-floor "
                      "height")
    setattr(label, HEIGHT_PROP, 0.0)
    return True


def label_height_m(label):
    """The room's height override in metres, or None when it defers.

    None and 0.0 are the same answer -- defer to the story -- so a label that
    predates the property behaves identically to one that has it untouched.
    A negative value is returned as-is for the caller to reject; silently
    treating it as "unset" would hide a typed minus sign.
    """
    value = length_m(label, HEIGHT_PROP)
    if value is None:
        return None
    return None if value == 0.0 else value


def match_labels_to_faces(faces, labels, sketch):
    """Point-in-face assignment, done in the sketch own frame.

    Returns (pairs, unlabeled_faces, orphan_labels, multi_labeled) where pairs
    is [(face, label)].  Every failure mode is reported rather than guessed
    at -- an unlabeled enclosed region is exactly the corridor that got
    silently skipped in the PDF-derived model.

    The label anchor is flattened onto the story plane before testing, so how
    high above (or below) the plan a label floats never matters.  Which story
    it belongs to is settled beforehand by labels_for_sketch.
    """
    hits = {i: [] for i in range(len(faces))}
    orphans = []
    local_faces = [local_shape(f, sketch) for f in faces]
    for label in labels:
        pt = local_point(label_point(label), sketch)
        pt.z = 0.0
        for i, face in enumerate(local_faces):
            if face.isInside(pt, 1e-3, True):
                hits[i].append(label)
                break
        else:
            orphans.append(label)

    pairs, unlabeled, multi = [], [], []
    for i, face in enumerate(faces):
        found = hits[i]
        if not found:
            unlabeled.append(face)
        elif len(found) > 1:
            multi.append((face, found))
        else:
            pairs.append((face, found[0]))
    return pairs, unlabeled, orphans, multi


def interior_point(face, sketch, grid=24):
    """A point guaranteed to lie inside `face`, as far from its walls as
    practical, returned in global coordinates.

    The centre of mass of an L-shaped room can fall outside the room, so a
    label placed there would match the wrong face (or none).  Fall back to a
    grid search that maximises clearance from the boundary.

    Searching happens in the sketch own frame and the result is mapped back
    out, so a seeded label lands in its own story plane -- which is what keeps
    stacked stories unambiguous on the next run.
    """
    local = local_shape(face, sketch)
    c = local.CenterOfMass
    candidate = FreeCAD.Vector(c.x, c.y, 0)
    if local.isInside(candidate, 1e-3, True):
        return sketch.Placement.multVec(candidate)

    bb = local.BoundBox
    best, best_clear = None, -1.0
    wire = local.OuterWire
    for i in range(1, grid):
        for j in range(1, grid):
            pt = FreeCAD.Vector(
                bb.XMin + bb.XLength * i / grid,
                bb.YMin + bb.YLength * j / grid,
                0,
            )
            if not local.isInside(pt, 1e-3, True):
                continue
            clear = Part.Vertex(pt).distToShape(wire)[0]
            if clear > best_clear:
                best, best_clear = pt, clear
    if best is None:
        raise BridgeError("could not find an interior point for a %.2f m2 face"
                          % area_m2(face))
    return sketch.Placement.multVec(best)


def centroid_m(face, sketch):
    """Face centre of mass in the sketch own frame, metres.

    Reported coordinates then match the plan itself rather than wherever the
    story happens to sit on the sheet.
    """
    c = local_shape(face, sketch).CenterOfMass
    return [round(c.x / MM_PER_M, 3), round(c.y / MM_PER_M, 3)]


def area_m2(face):
    return round(face.Area / (MM_PER_M * MM_PER_M), 4)
