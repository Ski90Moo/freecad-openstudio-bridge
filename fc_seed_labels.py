# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Prepare a traced FreeCAD document for export.

Two jobs, both idempotent:

  --init-stories   add the OS_StoryName / OS_Elevation / OS_FloorToFloor /
                   OS_Include properties to every sketch, and OS_NorthAxis_deg
                   to the document, so they can be filled in from the GUI.
                   When the sketches are stacked at real Z (Placement.Base.z
                   differs between them), OS_Elevation and OS_FloorToFloor
                   are derived from that spacing instead -- the topmost
                   story's OS_FloorToFloor still has to be entered by hand,
                   since there is no story above it to measure against.

  --init-roof      add OS_RoofMethod to every solid or face that could be a
                   roof, so one can be picked from the dropdown in the Data
                   tab.  Nothing is chosen for you; every candidate starts at
                   Ignore.

  --init-shading   add the OS_ShadingSketch checkbox to every sketch that
                   could plausibly hold a shade, so a sketch whose Label
                   does not already read as one can still be included.
                   Starts ticked where the label already reads as a shade,
                   unticked everywhere else.

  (default)        drop a placeholder Draft Text into every enclosed region
                   that has no label yet, so the rooms can be named in the GUI
                   instead of created from scratch.

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_seed_labels.py \
        FloorplanTest-01.FCStd --init-stories

The document is backed up to <name>.FCStd.bak before the first write.
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import Draft  # noqa: E402
import fc_roof  # noqa: E402
import fc_export_shading as fc_shading  # noqa: E402
import fcbridge as fb  # noqa: E402

PLACEHOLDER_PREFIX = "??"

# Draft's own default text height is a few millimetres, which is invisible
# against a 50 m building.  This is the size label_style.FCMacro applies.
DEFAULT_FONT_MM = 400.0


def backup_once(path):
    dest = path + ".bak"
    if not os.path.exists(dest):
        shutil.copy2(path, dest)
        print("backup: %s" % os.path.basename(dest))


def ensure_doc_props(doc, font_mm=None):
    """Document-level properties other than north.

    FontSize lives on a ViewObject, and a headless session has none, so the
    size cannot be applied from here.  Recording the intent on the document
    lets label_style.FCMacro apply it in one click from the GUI.
    """
    changed = False
    if not hasattr(doc, "OS_LabelFontSize_mm"):
        doc.addProperty("App::PropertyFloat", "OS_LabelFontSize_mm",
                        "OpenStudio", "Room label text height in mm")
        doc.OS_LabelFontSize_mm = DEFAULT_FONT_MM
        changed = True
    if font_mm is not None and doc.OS_LabelFontSize_mm != font_mm:
        doc.OS_LabelFontSize_mm = font_mm
        changed = True
    return changed


def init_stories(doc):
    changed = ensure_doc_props(doc)
    if not hasattr(doc, "OS_NorthAxis_deg"):
        doc.addProperty("App::PropertyFloat", "OS_NorthAxis_deg", "OpenStudio",
                        "Building north axis in degrees")
        doc.OS_NorthAxis_deg = 0.0
        changed = True
        print("document: added OS_NorthAxis_deg (currently 0.0)")

    sketches = fb.all_sketches(doc)
    for sketch in sketches:
        if fb.ensure_story_props(sketch):
            changed = True
            if not sketch.OS_StoryName:
                sketch.OS_StoryName = sketch.Label
            print("  %-12s -> OS_StoryName=%r  OS_Elevation=%.3f m  "
                  "OS_FloorToFloor=%.3f m  OS_Include=%s"
                  % (sketch.Name, sketch.OS_StoryName,
                     fb.story_elevation_m(sketch),
                     fb.story_height_m(sketch), sketch.OS_Include))
        else:
            print("  %-12s already has OS_* properties (OS_Include=%s)"
                  % (sketch.Name, sketch.OS_Include))

    derived = fb.stack_elevations_m(
        {sketch.Name: sketch.Placement.Base.z for sketch in sketches})
    stacked = bool(derived)
    if derived:
        print("\nstacked layout detected -- deriving OS_Elevation and "
              "OS_FloorToFloor from Placement.Base.z:")
        for sketch in sketches:
            elevation_m, floor_to_floor_m = derived[sketch.Name]
            if fb.story_elevation_m(sketch) != elevation_m:
                sketch.OS_Elevation = elevation_m * fb.MM_PER_M
                changed = True
            line = "  %-12s Placement.z=%9.1f mm -> OS_Elevation=%.3f m" % (
                sketch.Name, sketch.Placement.Base.z, elevation_m)
            if floor_to_floor_m is None:
                print(line + "  (topmost -- set OS_FloorToFloor by hand)")
                continue
            if fb.story_height_m(sketch) != floor_to_floor_m:
                sketch.OS_FloorToFloor = floor_to_floor_m * fb.MM_PER_M
                changed = True
            print(line + "  OS_FloorToFloor=%.3f m" % floor_to_floor_m)
    return changed, stacked


def init_roof(doc):
    """Offer OS_RoofMethod on everything that could plausibly be a roof.

    Which object is the roof is a judgement -- a Body, its Pad, a bare face
    traced over the plan -- so this does not guess.  It puts the dropdown on
    every candidate and leaves them all at Ignore, which is the state the
    document was already in.
    """
    candidates = [o for o in doc.Objects
                  if fc_roof.has_shape(o)
                  and o.Shape.Faces
                  and o.TypeId != "Sketcher::SketchObject"
                  and not getattr(o, "OS_SurfaceName", None)]
    if not candidates:
        print("no solids or faces in this document to offer as a roof.")
        return False

    changed = False
    for obj in candidates:
        added = fc_roof.ensure_roof_props(obj)
        changed = changed or added
        shape = obj.Shape
        print("  %-24s %-22s %s  %d face(s)%s"
              % (obj.Label[:24], obj.TypeId.split("::")[-1],
                 "solid" if shape.Solids else shape.ShapeType.lower(),
                 len(shape.Faces),
                 "   (added)" if added else "   OS_RoofMethod=%s"
                 % getattr(obj, fc_roof.METHOD_PROP, "?")))
    return changed


def init_shading(doc):
    """Offer OS_ShadingSketch on every sketch that could plausibly be a shade.

    Skipped: story plan sketches (OS_Include) and opening-tracing sketches
    (OS_OpeningSketch) -- ticking either would turn every room or opening
    outline it holds into a phantom shading surface -- and any sketch used as
    a solid's own profile (a roof body's sketch, say), found the same way
    fc_export_shading's own identity join does, via InList.
    """
    candidates = []
    for obj in doc.Objects:
        if obj.TypeId != "Sketcher::SketchObject":
            continue
        if hasattr(obj, fc_shading.SHADING_SKETCH_PROP):
            candidates.append(obj)
            continue
        if hasattr(obj, "OS_Include") or getattr(obj, "OS_OpeningSketch", False):
            continue
        if any(o.isDerivedFrom("PartDesign::Feature") for o in obj.InList):
            continue
        candidates.append(obj)

    if not candidates:
        print("no sketches in this document to offer as shading.")
        return False

    changed = False
    for obj in candidates:
        added = fc_shading.ensure_shading_props(obj)
        changed = changed or added
        if added:
            state = "(added, ticked)" if obj.OS_ShadingSketch else "(added)"
        else:
            state = "OS_ShadingSketch=%s" % obj.OS_ShadingSketch
        print("  %-24s %s" % (obj.Label[:24], state))
    return changed


def group_for(doc, story_name):
    name = "Labels_%s" % "".join(
        ch if ch.isalnum() else "_" for ch in story_name)
    existing = doc.getObject(name)
    if existing is not None:
        return existing
    group = doc.addObject("App::DocumentObjectGroup", name)
    group.Label = "Labels - %s" % story_name
    return group


def seed(doc, height_mm):
    sketches = fb.story_sketches(doc)
    if not sketches:
        sys.exit("No sketch has OS_Include set -- run --init-stories first, "
                 "then tick OS_Include on each story sketch.")

    labels = fb.room_labels(doc)
    created = 0
    for sketch in sketches:
        story_name = getattr(sketch, "OS_StoryName", "") or sketch.Label
        faces = fb.extract_room_faces(sketch)
        mine = fb.labels_for_sketch(sketch, labels, sketches)
        _, unlabeled, _, _ = fb.match_labels_to_faces(faces, mine, sketch)
        print("%-20s %3d rooms, %3d already labeled, %3d to seed"
              % (story_name, len(faces), len(faces) - len(unlabeled),
                 len(unlabeled)))
        if not unlabeled:
            continue

        group = group_for(doc, story_name)
        # Largest first, so the placeholder numbering is stable and the big
        # rooms are the easy ones to name.
        for n, face in enumerate(sorted(unlabeled, key=lambda f: -f.Area), 1):
            pt = fb.interior_point(face, sketch)
            text = Draft.make_text(
                ["%s | Room %d (%.1f m2)"
                 % (PLACEHOLDER_PREFIX, n, fb.area_m2(face))],
                pt)
            fb.sync_label_display(text)
            # text.ViewObject is None in a headless session, so the font size
            # is recorded on the document instead -- see ensure_doc_props.
            group.addObject(text)
            created += 1

    doc.recompute()
    return created


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("--init-stories", action="store_true",
                    help="add the OS_* story properties and exit")
    ap.add_argument("--init-roof", action="store_true",
                    help="add OS_RoofMethod to every roof candidate and exit")
    ap.add_argument("--init-shading", action="store_true",
                    help="add OS_ShadingSketch to every shading candidate "
                         "and exit")
    ap.add_argument("--font-mm", type=float, default=DEFAULT_FONT_MM,
                    help="label text height in mm, recorded on the document "
                         "for label_style.FCMacro (default %d)"
                         % DEFAULT_FONT_MM)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without saving")
    args = ap.parse_args()

    path = os.path.abspath(args.fcstd)
    doc = fb.open_document(path)

    if args.init_roof:
        changed = init_roof(doc)
        if changed and not args.dry_run:
            backup_once(path)
            fb.save_document(doc)
            print("\nsaved.  Now set OS_RoofMethod on the one object that "
                  "is the roof\n(Data tab, OpenStudio group):\n"
                  "  Extend -- the spaces below grow up to meet it\n"
                  "  Attic  -- it becomes a space of its own\n"
                  "Then re-run the export.")
        elif not changed:
            print("\nevery candidate already has OS_RoofMethod.")
        return

    if args.init_shading:
        changed = init_shading(doc)
        if changed and not args.dry_run:
            backup_once(path)
            fb.save_document(doc)
            print("\nsaved.  Tick OS_ShadingSketch (Data tab, OpenStudio "
                  "group) on anything\nthat should be a shade but is not "
                  "already ticked, then re-run the export.")
        elif not changed:
            print("\nevery candidate already has OS_ShadingSketch.")
        return

    if args.init_stories:
        changed, stacked = init_stories(doc)
        if changed and not args.dry_run:
            backup_once(path)
            fb.save_document(doc)
            if stacked:
                print("\nsaved.  OS_Elevation and OS_FloorToFloor were "
                      "derived from Placement.Base.z above.\nCheck the "
                      "topmost story's OS_FloorToFloor by hand, tick "
                      "OS_Include on each story\nsketch (Data tab, "
                      "OpenStudio group), then re-run without --init-stories "
                      "to\nseed room labels.")
            else:
                print("\nsaved.  Now set OS_Elevation, OS_FloorToFloor and "
                      "tick OS_Include\non each story sketch (Data tab, "
                      "OpenStudio group), then re-run without\n--init-stories "
                      "to seed room labels.\nBoth are lengths: type them "
                      "with a unit (8ft 10-11/16in, 2710 mm), or bind\nthem "
                      "to a spreadsheet cell that has one.")
        elif not changed:
            print("\nnothing to add.")
        return

    ensure_doc_props(doc, args.font_mm)
    created = seed(doc, args.font_mm)
    if created and not args.dry_run:
        backup_once(path)
        kept = fb.save_document(doc)
        print("\nseeded %d placeholder label(s) and saved." % created)
        if kept:
            print("preserved %d view-data entries the headless save would "
                  "otherwise have dropped." % kept)
        print("\nNEXT: open the document and run label_style.FCMacro "
              "(Macro -> Execute).")
        print("New labels carry FreeCAD's default text height, a few mm, "
              "which is invisible\nagainst a building tens of metres across. "
              "The macro sets them to %.0f mm.\nFont size is a ViewObject "
              "property and cannot be set headless."
              % doc.OS_LabelFontSize_mm)
        print("\nThen replace each '%s | Room N' with the real room number "
              "and name,\ne.g. '105 | Office', and run fc_export_floorplan.py."
              % PLACEHOLDER_PREFIX)
    elif created:
        print("\nwould seed %d placeholder label(s) (--dry-run)." % created)
    else:
        print("\nevery room already has a label.")


if __name__ == "__main__":
    main()
