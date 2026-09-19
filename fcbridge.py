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
