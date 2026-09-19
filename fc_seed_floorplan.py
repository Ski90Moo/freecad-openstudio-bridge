# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Seed a new (or existing) FreeCAD document from markup/crop_plan.py's
manifest: one calibrated background image and one empty story sketch per
story, stacked at their real elevations, ready to trace.

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_seed_floorplan.py \
        FloorplanTest-04.FCStd markup/plans/manifest.json

This is the step before the bridge's own contract begins: geometry still
comes from CAD, drawn by a person (README "Drawing conventions"). What this
script does is exactly the part that is not judgment -- getting a
correctly-scaled, correctly-positioned reference image and an empty sketch
into the document, the same way fc_place_elevations.py places a facade
drawing by arithmetic instead of by hand, because a hand-placed image is
where scale error enters the model.

The image plane and the sketch share one convention: the manifest's
crosshair-relative (0, 0) *is* the sketch's local origin, at
Placement.Base = (0, 0, elevation_mm) with no rotation. So the image is
placed by the same left/right/bottom/top arithmetic fc_place_elevations.py
already uses, just on a flat z-plane instead of a vertical facade frame --
there is no wall-plane transform to invert here.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402

import fcbridge as fb  # noqa: E402
import fc_seed_labels as fsl  # noqa: E402
from fc_seed_openings import backup  # noqa: E402

MM_PER_M = 1000.0
GROUP = "OS_FloorPlans"

PROPS = [
    ("OS_PlanImage", "App::PropertyBool"),
    ("OS_StoryName", "App::PropertyString"),
]

VERIFY_TOL_MM = 0.001


def get_or_create_document(path):
    if os.path.exists(path):
        print("backup: %s" % os.path.basename(backup(path)))
        return FreeCAD.openDocument(path), False
    name = os.path.splitext(os.path.basename(path))[0]
    doc = FreeCAD.newDocument(name)
    doc.saveAs(path)
    return doc, True


def get_group(doc):
    existing = doc.getObject(GROUP)
    if existing is not None and existing.TypeId == "App::DocumentObjectGroup":
        return existing
    group = doc.addObject("App::DocumentObjectGroup", GROUP)
    group.Label = "Floor plan drawings"
    return group


def find_by_story(doc, type_id, prop, story_name):
    for obj in doc.Objects:
        if obj.TypeId == type_id and getattr(obj, prop, None) == story_name:
            return obj
    return None


def place_sketch(doc, story_name, z_mm):
    sketch = find_by_story(doc, "Sketcher::SketchObject", "OS_StoryName",
                           story_name)
    made = sketch is None
    if made:
        sketch = doc.addObject("Sketcher::SketchObject", "Sketch")
        sketch.Label = story_name
    fb.ensure_story_props(sketch)
    sketch.OS_StoryName = story_name
    sketch.OS_Include = True
    sketch.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, z_mm),
                                         FreeCAD.Rotation())
    return sketch, made


def place_image(doc, group, story, image_dir):
    story_name = story["name"]
    obj = find_by_story(doc, "Image::ImagePlane", "OS_StoryName", story_name)
    made = obj is None
    if made:
        obj = doc.addObject("Image::ImagePlane",
                            "OS_Plan_%s" % story_name.replace(" ", ""))
        obj.Label = "%s floor plan" % story_name
    for name, ptype in PROPS:
        if not hasattr(obj, name):
            obj.addProperty(ptype, name, "OpenStudio",
                            "FreeCAD to OpenStudio bridge")
    obj.OS_PlanImage = True
    obj.OS_StoryName = story_name
    obj.ImageFile = os.path.join(image_dir, story["image"])

    left_mm = story["left_m"] * MM_PER_M
    right_mm = story["right_m"] * MM_PER_M
    bottom_mm = story["bottom_m"] * MM_PER_M
    top_mm = story["top_m"] * MM_PER_M
    z_mm = story["elevation_m"] * MM_PER_M
    obj.XSize = right_mm - left_mm
    obj.YSize = top_mm - bottom_mm
    obj.Placement = FreeCAD.Placement(
        FreeCAD.Vector((left_mm + right_mm) / 2.0,
                       (bottom_mm + top_mm) / 2.0, z_mm),
        FreeCAD.Rotation())
    if obj not in group.Group:
        group.addObject(obj)
    return obj, made


def verify(images):
    """The manifest's own left/right/bottom/top, read back off the placed
    Image::ImagePlane's Placement + XSize/YSize -- catches a sign or
    half-vs-whole-size mistake in this script, not a data problem."""
    worst = 0.0
    for story, obj in images:
        left_mm = story["left_m"] * MM_PER_M
        right_mm = story["right_m"] * MM_PER_M
        bottom_mm = story["bottom_m"] * MM_PER_M
        top_mm = story["top_m"] * MM_PER_M
        actual_left = obj.Placement.Base.x - obj.XSize.Value / 2.0
        actual_right = obj.Placement.Base.x + obj.XSize.Value / 2.0
        actual_bottom = obj.Placement.Base.y - obj.YSize.Value / 2.0
        actual_top = obj.Placement.Base.y + obj.YSize.Value / 2.0
        dev = max(abs(actual_left - left_mm), abs(actual_right - right_mm),
                  abs(actual_bottom - bottom_mm), abs(actual_top - top_mm))
        worst = max(worst, dev)
        status = "OK" if dev <= VERIFY_TOL_MM else "MISMATCH"
        print("  %-8s worst deviation %.9f mm  [%s]"
              % (story["name"], dev, status))
    if worst > VERIFY_TOL_MM:
        raise SystemExit(
            "placement verification failed (worst %.9f mm > %.3f mm "
            "tolerance) -- refusing to trust this document" % (worst, VERIFY_TOL_MM))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("manifest")
    ap.add_argument("--images-dir", help="default: the manifest's own directory")
    args = ap.parse_args()

    manifest_path = os.path.abspath(args.manifest)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    image_dir = os.path.abspath(args.images_dir or os.path.dirname(manifest_path))

    path = os.path.abspath(args.fcstd)
    doc, created = get_or_create_document(path)
    print("%s %s" % ("created" if created else "opened", os.path.basename(path)))

    group = get_group(doc)
    images = []
    for story in manifest["stories"]:
        z_mm = story["elevation_m"] * MM_PER_M
        sketch, sk_made = place_sketch(doc, story["name"], z_mm)
        img, img_made = place_image(doc, group, story, image_dir)
        images.append((story, img))
        print("  %-8s sketch %s (%s)  image %s (%s)  z=%.4f m"
              % (story["name"], sketch.Name, "new" if sk_made else "kept",
                 img.Name, "new" if img_made else "refreshed",
                 story["elevation_m"]))

    print()
    changed, stacked = fsl.init_stories(doc)
    if not stacked:
        raise SystemExit(
            "expected a stacked layout (stories at different Placement.Base.z) "
            "-- got side-by-side instead; check the manifest's elevation_m values")

    top_name = manifest["top_story_name"]
    top_sketch = find_by_story(doc, "Sketcher::SketchObject", "OS_StoryName",
                               top_name)
    top_f2f_m = manifest["top_story_floor_to_floor_m"]
    top_sketch.OS_FloorToFloor = top_f2f_m * MM_PER_M
    print("%s (topmost): OS_FloorToFloor set to %.4f m from the purple "
          "markup on the elevation sheet -- confirm against the drawing"
          % (top_name, top_f2f_m))

    north = float(manifest.get("north_axis_deg", 0.0))
    doc.OS_NorthAxis_deg = north
    print("OS_NorthAxis_deg = %.1f" % north)

    restored = fb.save_document(doc)
    seeded = fb.seed_view_defaults(path, [img.Name for _, img in images])
    print("\nsaved %s%s" % (os.path.basename(path),
                            " (%d view entries restored)" % restored
                            if restored else ""))
    if seeded:
        print("  %d image(s) given the default 70%% transparency" % len(seeded))
    elif created:
        # There is no window to seed into after the fact: opening this
        # document in the GUI and saving is what *creates* GuiDocument.xml
        # in the first place, and doing that stamps every object -- these
        # two images included -- with FreeCAD's own default (0%) before
        # seed_view_defaults ever gets another chance to run. Re-running
        # this script afterwards changes nothing, because by then the
        # images already have an entry of their own, and seeding never
        # overwrites one. Verified: that is not a hypothetical, it is what
        # happened the first time this was tried.
        print("  no view data to seed into yet (brand-new document) -- this "
              "cannot be pre-seeded for a document that starts out entirely "
              "headless. Set it by hand once you open it: select each image "
              "plane, View tab (not Data), Transparency -> 70")

    print("\nverification -- placed image bounds vs. the manifest:")
    verify(images)
    print("\n%s is ready: %d stor%s, each with a calibrated background image "
          "and an empty sketch at the right elevation. Trace wall "
          "centerlines into each story's sketch, same as any other plan."
          % (os.path.basename(path), len(manifest["stories"]),
             "y" if len(manifest["stories"]) == 1 else "ies"))


if __name__ == "__main__":
    main()
