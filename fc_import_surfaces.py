# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Rebuild an OpenStudio model's surfaces as FreeCAD geometry.

Run under FreeCAD's bundled Python, either into a new document:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_import_surfaces.py \
        surfaces.json --out model_geometry.FCStd

or -- preferred -- back into the source document the plan was traced in:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_import_surfaces.py \
        surfaces.json --into FloorplanTest-02.FCStd

Every OpenStudio surface becomes its own selectable Part::Feature face
carrying OS_SurfaceName / OS_Handle / OS_SpaceName / OS_SurfaceType /
OS_BoundaryCondition, grouped story -> space so the tree mirrors the model.

That is deliberate: the point is not just to look at the geometry, it is to
draw window and door rectangles onto real wall faces and push them back with
fc_export_openings.py.  A merged mesh could not support that.

--into is what makes the fenestration workflow work at all.  A sketch can only
attach to, and take external geometry from, objects in its *own* document, so
the generated walls have to live beside the plan sketches for fc_seed_openings
to hang elevation sketches off them.  Surfaces are matched by OS_SurfaceName
and updated in place, so those attachments survive a regeneration.
"""

import argparse
import time
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import Part  # noqa: E402

import fcbridge  # noqa: E402

M_TO_MM = 1000.0

# Everything this script generates hangs off one top-level group, so in a
# document that also holds the traced plan it is one thing to collapse, hide
# or delete.
ROOT_GROUP = "OS_Geometry"

# Colour by what the modeller actually needs to spot: a wall that should be
# interior but reads as Outdoors, a floor that never got matched.
COLOURS = {
    ("Wall", "Outdoors"): (0.80, 0.75, 0.55),
    ("Wall", "Surface"): (0.55, 0.65, 0.80),
    ("Wall", "Ground"): (0.45, 0.35, 0.25),
    ("Wall", "Adiabatic"): (0.70, 0.45, 0.70),
    ("RoofCeiling", "Outdoors"): (0.65, 0.30, 0.25),
    ("RoofCeiling", "Surface"): (0.60, 0.70, 0.70),
    ("Floor", "Ground"): (0.40, 0.40, 0.40),
    ("Floor", "Surface"): (0.50, 0.60, 0.55),
    ("Floor", "Outdoors"): (0.85, 0.55, 0.30),
}
DEFAULT_COLOUR = (0.75, 0.75, 0.75)

# An air boundary is not a wall, it is the deliberate absence of one, so it
# gets a colour no real construction uses and enough transparency to read as
# an opening.  Without this it renders as an ordinary interior wall and the
# whole point of looking at the model is lost.
AIR_BOUNDARY_COLOUR = (0.90, 0.20, 0.80)
AIR_BOUNDARY_TRANSPARENCY = 75


def rgb(value):
    """0xRRGGBB -> the 0..1 triple FreeCAD wants."""
    return ((value >> 16 & 255) / 255.0, (value >> 8 & 255) / 255.0,
            (value & 255) / 255.0)


# Straight from the OpenStudio App's own palette, so a canopy is the same
# colour in both tools and the group type is readable at a glance.  These are
# the ThreeJSForwardTranslator material colours the App's 3D View renders --
# read out of the SDK, not sampled off a screen.  The App lights them at about
# 73%, so BuildingShading arrives on screen as #533870 rather than #714C99;
# the material colour is the one to store.
SHADING_COLOURS = {
    "Building": rgb(0x714C99),      # purple
    "Site": rgb(0x4B7C95),          # teal
    "Space": rgb(0x4C6EB2),         # dark blue
}
DEFAULT_SHADING_COLOUR = SHADING_COLOURS["Building"]

SUBSURFACE_COLOURS = {
    "FixedWindow": (0.35, 0.70, 0.90),
    "OperableWindow": (0.30, 0.60, 0.85),
    "GlassDoor": (0.40, 0.75, 0.85),
    "Door": (0.60, 0.40, 0.20),
    "OverheadDoor": (0.55, 0.35, 0.20),
    "Skylight": (0.50, 0.80, 0.95),
}

PROPS = [
    ("OS_SurfaceName", "App::PropertyString", "name"),
    ("OS_Handle", "App::PropertyString", "handle"),
    ("OS_SpaceName", "App::PropertyString", "space"),
    ("OS_SurfaceType", "App::PropertyString", "surface_type"),
    ("OS_BoundaryCondition", "App::PropertyString",
     "outside_boundary_condition"),
    ("OS_Construction", "App::PropertyString", "construction"),
]


def packed(colour):
    """A 0..1 RGB triple as the integer App::PropertyColor serialises.

    (r << 24) | (g << 16) | (b << 8) | alpha, matching the PropertyColor
    values already in this document's GuiDocument.xml.
    """
    r, g, b = (max(0, min(255, int(round(c * 255.0)))) for c in colour)
    return (r << 24) | (g << 16) | (b << 8) | 255


def view_defaults(colour, transparency):
    """Seedable display settings for one object, in seed_view_defaults form.

    Colour has to be seeded rather than set, because a scripted run has no
    ViewObject to set it on.  It matters most for an object that did not
    exist before: retyping an opening renames it, so `import` deletes the old
    object and builds a new one, and a new object with no entry comes up in
    FreeCAD's default grey -- a retyped door that still looks like the window
    it used to be.  Only objects with no entry are seeded, so a colour the
    user or colorize.FCMacro chose is never overwritten.
    """
    return (
        ("ShapeColor", "App::PropertyColor", "PropertyColor",
         str(packed(colour))),
        ("Transparency", "App::PropertyPercent", "Integer",
         str(int(transparency))),
    )


def style(obj, colour, transparency):
    """Apply display colour when a ViewObject exists, and return it for seeding.

    Headless FreeCAD creates no ViewObjects at all, so setting is a no-op for
    the normal scripted run -- which is why the returned settings get written
    into GuiDocument.xml afterwards, and why colorize.FCMacro exists to
    re-apply them from inside the GUI, reading the same OS_* properties.
    """
    view = getattr(obj, "ViewObject", None)
    if view is not None:
        view.ShapeColor = colour
        view.Transparency = transparency
    return view_defaults(colour, transparency)


def safe_name(text, prefix):
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text)
    return ("%s_%s" % (prefix, cleaned))[:60]


# An OpenStudio subsurface is exactly coplanar with its host wall, which is
# right in the model and unusable on screen: coplanar faces z-fight, so the
# window flickers through the wall, and a pick ray hits both at the same depth
# so clicking a window as often selects the wall behind it.  Pulling the
# subsurface a couple of millimetres out of the wall settles both -- invisible
# at building scale, and recorded on the object as OS_DisplayOffset_mm so
# verify_roundtrip.py takes it back off before comparing.
SUBSURFACE_OFFSET_MM = 2.0


# A sloped roof polygon that touches three or more distinct elevations does not
# lie on one exact plane once it has been through the OSM file: every vertex is
# written to finite precision, so each elevation rounds independently.  The
# error is well under a micron -- nothing the energy model can see, and
# OpenStudio itself is happy -- but OCC's Precision::Confusion is 1e-7 mm, so
# Part.Face refuses the wire and the surface arrives as a bare outline that
# draws no face at all.  Projecting onto the best-fit plane costs less than a
# micron and gives back a real face.  Loops spanning only two elevations are
# parallelograms and stay exactly planar, which is why flat roofs never hit
# this and only some of a pitched roof's faces do.
# The OSM stores 6 decimal places of a metre, so each vertex carries up to
# 0.0005 mm of rounding and a fitted plane can sit ~0.001 mm off the worst
# point.  5 um leaves room for that and is still far smaller than any real
# geometry error, which would be millimetres at least.  Measured on this
# building's pitched roof: 0.00008 to 0.0004 mm.
PLANARITY_TOL_MM = 0.005

# Populated by make_face, reported at the end of the run.
repaired = []   # (name, how far each point moved, mm)
outlines = []   # names that stayed wires -- genuinely suspect


def best_fit_plane(pts):
    """Centroid and unit normal of the plane that best fits a closed loop."""
    normal = FreeCAD.Vector(0, 0, 0)
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        normal += FreeCAD.Vector((p.y - q.y) * (p.z + q.z),
                                 (p.z - q.z) * (p.x + q.x),
                                 (p.x - q.x) * (p.y + q.y))
    if normal.Length < 1e-12:
        return None, None
    centre = FreeCAD.Vector(0, 0, 0)
    for p in pts:
        centre = centre.add(p)
    return centre.multiply(1.0 / len(pts)), normal.normalize()


def make_face(vertices, name=None):
    pts = [FreeCAD.Vector(x * M_TO_MM, y * M_TO_MM, z * M_TO_MM)
           for x, y, z in vertices]
    if len(pts) < 3:
        return None
    try:
        return Part.Face(Part.makePolygon(pts + [pts[0]]))
    except Part.OCCError:
        pass

    centre, normal = best_fit_plane(pts)
    if centre is not None:
        plane = Part.Plane(centre, normal)
        flat = [plane.value(*plane.parameter(p)) for p in pts]
        moved = max((a - b).Length for a, b in zip(pts, flat))
        if moved <= PLANARITY_TOL_MM:
            try:
                face = Part.Face(Part.makePolygon(flat + [flat[0]]))
                repaired.append((name, moved))
                return face
            except Part.OCCError:
                pass

    # Really non-planar, or self-intersecting -- keep the outline so it is still
    # visible and obviously suspect, rather than dropping it silently.
    outlines.append(name)
    return Part.makePolygon(pts + [pts[0]])


def set_prop(obj, ptype, name, value):
    """Set an OS_* property, creating it only if it is not already there.

    Re-adding an existing property is an error, and in --into mode most
    objects already have every property from the previous run.
    """
    if not hasattr(obj, name):
        obj.addProperty(ptype, name, "OpenStudio", "from the OpenStudio model")
    setattr(obj, name, value)


def add_props(obj, record, extra=None):
    for prop, ptype, key in PROPS:
        if key not in record:
            continue
        set_prop(obj, ptype, prop, str(record.get(key) or ""))
    for prop, ptype, value in (extra or []):
        set_prop(obj, ptype, prop, value)


def get_group(doc, cache, name, label, parent=None):
    if name in cache:
        return cache[name]
    existing = doc.getObject(name)
    if existing is not None and \
            existing.TypeId == "App::DocumentObjectGroup":
        group = existing
    else:
        group = doc.addObject("App::DocumentObjectGroup", name)
    group.Label = label
    if parent is not None and group not in parent.Group:
        parent.addObject(group)
    cache[name] = group
    return group


def reparent(obj, group):
    """Put obj in group and in no other, without disturbing what is correct."""
    parent = None
    getter = getattr(obj, "getParentGroup", None)
    if getter is not None:
        parent = getter()
    if parent is group:
        return
    if parent is not None:
        parent.removeObject(obj)
    group.addObject(obj)


def backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem, ext = os.path.splitext(path)
    dest = "%s.bak-%s%s" % (stem, stamp, ext)
    shutil.copy2(path, dest)
    return dest


def open_target(args):
    """(document, is_existing).  Backs up first when writing into one."""
    if not args.into:
        return FreeCAD.newDocument("osgeometry"), False
    path = os.path.abspath(args.into)
    if not os.path.exists(path):
        raise SystemExit("no such document: %s" % path)
    print("backup: %s" % os.path.basename(backup(path)))
    return fcbridge.open_document(path), True


OUTLINE_MOVE_TOL_MM = 1.0


def outline_positions(doc):
    """{opening id: (centroid, extent)} for every drawn opening outline.

    An outline that moves because of an import is always wrong -- an import
    brings geometry *in*, it does not edit the drawing.  It happened: the
    elevation sketches used to attach to the wall faces with FlatFace, and
    re-importing after a roof change turned three of them through 180 degrees
    and slid them up to 39 m, taking 20 of 27 outlines with them.  They now
    stand on the traced plan instead (see fc_seed_openings), which removes the
    cause, and this stays as the check that it stayed removed.

    Returns {} if the openings exporter is unavailable, since this is a check
    on the side and must never be what stops an import.
    """
    try:
        import fc_export_openings
    except Exception:                       # pragma: no cover - defensive
        return {}
    try:
        drawn, _problems = fc_export_openings.outlines(doc)
    except Exception:                       # pragma: no cover - defensive
        return {}

    positions = {}
    for entry in drawn:
        points, opening_id = entry[5], entry[4]
        if not opening_id or len(points) < 3:
            continue
        count = float(len(points))
        centre = FreeCAD.Vector(sum(p.x for p in points) / count,
                                sum(p.y for p in points) / count,
                                sum(p.z for p in points) / count)
        extent = (max(p.x for p in points) - min(p.x for p in points),
                  max(p.y for p in points) - min(p.y for p in points),
                  max(p.z for p in points) - min(p.z for p in points))
        positions[opening_id] = (centre, extent, entry[0].Label)
    return positions


def outline_drift(before, after, tol=OUTLINE_MOVE_TOL_MM):
    """[(label, id, moved_mm, resized)] for outlines the import disturbed."""
    moved = []
    for opening_id, (centre, extent, label) in sorted(after.items()):
        was = before.get(opening_id)
        if was is None:
            continue
        distance = (centre - was[0]).Length
        resized = max(abs(a - b) for a, b in zip(extent, was[1]))
        if distance > tol or resized > tol:
            moved.append((label, opening_id, distance, resized))
    return moved


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("surfaces_json")
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument("--out", help="path of a new .FCStd to write")
    out.add_argument("--into", help="existing .FCStd to write the geometry "
                                    "into, beside the traced plan")
    ap.add_argument("--skip-interior", action="store_true",
                    help="omit surfaces whose boundary condition is Surface")
    ap.add_argument("--subsurface-offset-mm", type=float,
                    default=SUBSURFACE_OFFSET_MM,
                    help="pull subsurfaces this far out of their host wall so "
                         "they shade and pick cleanly (default %(default)s); "
                         "0 leaves them exactly coplanar")
    args = ap.parse_args()

    with open(args.surfaces_json, encoding="utf-8") as fh:
        data = json.load(fh)

    doc, existing_doc = open_target(args)
    outlines_before = outline_positions(doc) if existing_doc else {}

    # Surfaces are matched by OpenStudio name so an object keeps its identity
    # across regenerations -- which is what keeps a sketch attached to it, and
    # that sketch's external geometry, from breaking.
    known = {}
    by_id = {}
    for obj in doc.Objects:
        name = getattr(obj, "OS_SurfaceName", None)
        if name:
            known[name] = obj
        # An opening's id outlives its name: retyping renames it.  Matching on
        # the id keeps the same FreeCAD object -- and therefore its colour,
        # and anything attached to it -- instead of deleting and rebuilding.
        opening_id = getattr(obj, "OS_OpeningId", None)
        if opening_id:
            by_id[opening_id] = obj
    seen = set()
    kept = set()

    cache = {}
    root = get_group(doc, cache, ROOT_GROUP, ROOT_GROUP) if existing_doc \
        else None
    made = reused = air_count = 0
    seeds = {}
    failed = []

    for record in data["surfaces"]:
        if args.skip_interior and \
                record["outside_boundary_condition"] == "Surface":
            continue

        shape = make_face(record["vertices"], record["name"])
        if shape is None:
            failed.append(record["name"])
            continue

        story = record.get("story") or "No Story"
        space = record.get("space") or "No Space"
        story_group = get_group(doc, cache, safe_name(story, "Story"), story,
                                root)
        space_group = get_group(doc, cache,
                                safe_name("%s_%s" % (story, space), "Space"),
                                space, story_group)

        obj = known.get(record["name"])
        if obj is None:
            obj = doc.addObject("Part::Feature",
                                safe_name(record["name"], "S"))
            made += 1
        else:
            reused += 1
        seen.add(record["name"])
        obj.Label = record["name"]
        obj.Shape = shape
        air = bool(record.get("is_air_boundary"))
        add_props(obj, record, [
            ("OS_Story", "App::PropertyString", str(story)),
            ("OS_AdjacentSurface", "App::PropertyString",
             str(record.get("adjacent_surface") or "")),
            ("OS_AirBoundary", "App::PropertyBool", air),
        ])
        if air:
            seeds[obj.Name] = style(obj, AIR_BOUNDARY_COLOUR,
                                    AIR_BOUNDARY_TRANSPARENCY)
            air_count += 1
        else:
            seeds[obj.Name] = style(obj, COLOURS.get(
                (record["surface_type"],
                 record["outside_boundary_condition"]), DEFAULT_COLOUR),
                60 if record["outside_boundary_condition"] == "Surface" else 0)
        reparent(obj, space_group)

    subs = 0
    sub_group = None
    normals = {r["name"]: r.get("outward_normal")
               for r in data.get("surfaces", [])}
    for record in data.get("subsurfaces", []):
        shape = make_face(record["vertices"], record["name"])
        if shape is None:
            failed.append(record["name"])
            continue
        normal = normals.get(record.get("host_surface"))
        offset = args.subsurface_offset_mm if normal else 0.0
        if offset:
            shape.translate(FreeCAD.Vector(*normal) * offset)
        obj = by_id.get(record.get("opening_id")) or known.get(record["name"])
        if obj is None:
            obj = doc.addObject("Part::Feature",
                                safe_name(record["name"], "SS"))
        seen.add(record["name"])
        kept.add(obj.Name)
        obj.Label = record["name"]
        obj.Shape = shape
        add_props(obj, record, [
            ("OS_HostSurface", "App::PropertyString",
             str(record.get("host_surface") or "")),
            ("OS_SubSurfaceType", "App::PropertyString",
             str(record.get("subsurface_type") or "")),
            ("OS_DisplayOffset_mm", "App::PropertyFloat", offset),
            ("OS_OpeningId", "App::PropertyString",
             str(record.get("opening_id") or "")),
        ])
        seeds[obj.Name] = style(obj, SUBSURFACE_COLOURS.get(
            record.get("subsurface_type"), (0.35, 0.70, 0.90)), 0)
        if existing_doc:
            # Created on the first subsurface, not up front: a model with no
            # openings yet should not grow an empty group in the tree.
            if sub_group is None:
                sub_group = get_group(doc, cache, safe_name("Subsurfaces",
                                                            "Group"),
                                      "Subsurfaces from the model", root)
            reparent(obj, sub_group)
        subs += 1

    # Shading is not part of any space, so it hangs off the root rather than
    # the story tree.  No display offset: a canopy is coplanar with nothing,
    # so there is nothing for it to z-fight with.
    shades = 0
    shade_group = None
    for record in data.get("shading", []):
        shape = make_face(record["vertices"], record["name"])
        if shape is None:
            failed.append(record["name"])
            continue
        obj = known.get(record["name"])
        if obj is None:
            obj = doc.addObject("Part::Feature",
                                safe_name(record["name"], "SH"))
        seen.add(record["name"])
        obj.Label = record["name"]
        obj.Shape = shape
        add_props(obj, record, [
            ("OS_ShadingSurface", "App::PropertyBool", True),
            ("OS_ShadingGroup", "App::PropertyString",
             str(record.get("shading_group") or "")),
            ("OS_ShadingGroupType", "App::PropertyString",
             str(record.get("group_type") or "")),
            ("OS_ShadingId", "App::PropertyString",
             str(record.get("shading_id") or "")),
        ])
        seeds[obj.Name] = style(obj, SHADING_COLOURS.get(record.get("group_type"),
                                       DEFAULT_SHADING_COLOUR), 0)
        if existing_doc:
            if shade_group is None:
                shade_group = get_group(doc, cache,
                                        safe_name("Shading", "Group"),
                                        "Shading from the model", root)
            reparent(obj, shade_group)
        shades += 1

    # By object, not by name: an opening matched on its id has kept the object
    # under a new name, and its old name is still sitting in `known`.
    stale = [name for name, obj in known.items()
             if name not in seen and obj.Name not in kept]
    for name in stale:
        doc.removeObject(known[name].Name)

    # A group left empty by this run -- the subsurface group after the last
    # opening is removed, a space group after its space is deleted -- is
    # clutter with nothing in it to explain itself.
    emptied = [g for g in cache.values() if not g.Group and g is not root]
    for group in emptied:
        del cache[group.Name]
        doc.removeObject(group.Name)

    # Record which plan this geometry is a picture of, so anything that hosts
    # against it -- fc_export_openings.py above all -- can tell when the plan
    # has moved on and the picture is out of date.  Only meaningful when the
    # document holds the plan as well as the geometry; a standalone geometry
    # document has nothing to compare against later.
    plan_sketches = fcbridge.story_sketches(doc)
    stamped = fcbridge.stamp_plan_digest(doc, plan_sketches) \
        if plan_sketches else ""

    doc.recompute()
    drift = outline_drift(outlines_before, outline_positions(doc)) \
        if outlines_before else []
    if existing_doc:
        restored = fcbridge.save_document(doc)
        target = doc.FileName
    else:
        target = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        doc.saveAs(target)
        restored = 0

    # Colours, into the saved file, for objects FreeCAD has never built a
    # ViewProvider for.  A retype renames an opening, so the old object is
    # removed and a new one built; without this it comes up in default grey,
    # still looking like the type it used to be.  Objects that already have an
    # entry are left alone, so nothing the user or colorize.FCMacro set is lost.
    seeded = fcbridge.seed_view_defaults(target, seeds)

    print("wrote %s" % target)
    print("  %d surfaces (%d new, %d updated in place), %d subsurfaces, "
          "%d shading, %d groups"
          % (made + reused, made, reused, subs, shades, len(cache)))
    if seeded:
        print("  seeded a display colour for %d object(s) that had none"
              % len(seeded))
    if stamped:
        print("  stamped the plan it was taken from (%s), so the opening "
              "export can tell if the plan moves on" % stamped)
    if subs and args.subsurface_offset_mm:
        print("  subsurfaces pulled %.1f mm out of their host wall so they "
              "shade and pick cleanly" % args.subsurface_offset_mm)
    if stale:
        print("  %d surface(s) no longer in the model were removed: %s"
              % (len(stale), ", ".join(sorted(stale)[:5])))
    if restored:
        print("  restored %d view-data entry/entries" % restored)
    if air_count:
        print("  %d of them air boundaries" % air_count)
    elif any(r.get("construction") for r in data["surfaces"]):
        print("  no air boundaries found. If you expected some, "
              "re-run dump_osm_geometry.py -- is_air_boundary is "
              "newer than this JSON may be.")
    if failed:
        print("  %d shape(s) could not be built: %s"
              % (len(failed), ", ".join(failed[:5])))

    if repaired:
        print("  %d surface(s) projected onto their best-fit plane to face "
              "(worst %.1e mm): %s"
              % (len(repaired), max(m for _, m in repaired),
                 ", ".join(n for n, _ in repaired[:6])))

    if outlines:
        print("  WARNING: %d surface(s) are outlines, not faces, and will "
              "draw nothing: %s" % (len(outlines), ", ".join(outlines[:6])))
        print("  Their loops are more than %.3f mm off any single plane, so "
              "this is a real" % PLANARITY_TOL_MM)
        print("  geometry problem, not rounding -- check them in the model.")

    if drift:
        print()
        print("  WARNING: %d drawn opening outline(s) MOVED during this "
              "import." % len(drift))
        print("  An import brings geometry in; it must not edit the drawing.")
        print("  Something in the sketch is tied to what was just replaced --")
        print("  check the sketch's attachment (it should be on the traced")
        print("  plan, not on a model surface) and any external geometry.")
        print()
        print("  %-14s %-10s %10s %10s" % ("sketch", "id", "moved mm",
                                           "resized mm"))
        for label, opening_id, distance, resized in sorted(
                drift, key=lambda d: -d[2]):
            print("  %-14s %-10s %10.1f %10.1f"
                  % (label[:14], opening_id[:8], distance, resized))
        print()
        print("  Check these in the sketch before exporting openings again.")
        print("  Revert this document to undo the import, or fix each outline")
        print("  against the wall it belongs to.")

    if existing_doc:
        print("\nNext: .\\bridge.ps1 planes to hang an elevation sketch off "
              "each\nfacade, then draw openings into it -- see README.")
    else:
        print("\nNext: open it in FreeCAD and run colorize.FCMacro to shade "
              "the\nsurfaces by type and boundary condition, then draw a "
              "rectangle on\na wall face for each window or door -- see "
              "README.")


if __name__ == "__main__":
    main()
