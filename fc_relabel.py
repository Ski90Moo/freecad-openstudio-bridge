# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Re-seat room labels after the walls have moved.

Run under FreeCAD's bundled Python:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_relabel.py \
        FloorplanTest-05.FCStd --previous runs/floorplans/fptest05.json

Reports what it would do; --apply writes it.

A label is how a region becomes a space: point-in-face is the whole of the
matching, and a label that is no longer inside its own room breaks the export
outright.  Move a wall and that happens to every label the wall passed --
rooms slide, labels do not.  Moving the tenant separation on FloorplanTest-02
left two regions with no label, two regions with two, and the export refused
until all four were placed by hand.

The obvious fix -- constrain each label to the sketch edges around it -- does
not survive contact with FreeCAD.  Draft Text has no attachment extension, and
adding Part::AttachExtensionPython is inert because Draft's execute() never
calls positionBySupport().  Expressions on Placement.Base do work and do track
the geometry, but they have to name an edge, and Shape.EdgeN is positional:
measured, adding one line to a sketch swapped which edge was Edge1, and a
label bound to it would have silently re-anchored to a different wall.  That
is a worse failure than the one being fixed, because nothing looks wrong.

So the label is not tied to the geometry.  It is re-derived from it, here,
after the fact -- which needs no constraints, survives a room changing shape
as well as position, and can say plainly when it is not sure.

How a label is matched to its room, in falling order of confidence:

  1. It is already inside exactly one region, and that region holds only it.
     Nothing to do; the label is left exactly where it is.  A label that is
     merely off-centre is not a problem to fix, and moving it would churn
     placements the user chose.  The one exception is a room whose area says
     it belongs to someone else: a wall moving through a row of small rooms
     leaves every label alone in its neighbour's room, so all of them look
     settled and none of them is.  Such a label is set aside for step 2, and
     step 2b hands the room straight back if nothing better turns up.
  2. Its room's area is unchanged from the last export.  A room that moved
     without resizing is the same room, and an exact area match against a
     region nothing else claims is about as strong as evidence gets in a
     floor plan.
  3. One label and one region are left over.  By elimination.

Anything still unresolved is reported by name and left alone.  A region that
genuinely did not exist before needs a label the user writes, and guessing a
room number is not a service.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fcbridge as fb  # noqa: E402

# How close two areas must be to call them the same room.  The vertices went
# out through JSON's six decimal places and came back, so this absorbs the
# round trip and nothing that would matter on a drawing.
AREA_TOL_M2 = 0.05


# --------------------------------------------------------------------------
# the matching, kept free of FreeCAD so it can be tested on its own
# --------------------------------------------------------------------------

def resolve(inside, areas, prior, tol=AREA_TOL_M2):
    """Which label belongs to which region.

    inside  {label: region or None}  where each label currently sits
    areas   {region: area_m2}        every enclosed region in the story
    prior   {label: area_m2}         what that label's room was last export

    Returns (assignment, homeless, empty, why) where assignment maps every
    label that could be placed to its region, homeless lists the labels that
    could not, empty lists regions left without one, and why records the rule
    that placed each label so the report can show its own reasoning.

    The rules below are run to a fixed point rather than once, because a
    label that is about to move is not really in the way.  Measured on
    FloorplanTest-02: three labels had piled into the Shop, so the Shop
    looked doubled and neither it nor the room they came from could be
    settled; with those three placed, the second round found the Shop held
    one label, and the last hallway fell out by elimination.  Deciding to
    move a label and then still counting it where it stood is the only thing
    that had stopped it.

    Rounds only ever add: an assignment that came out smaller than the one
    before it is discarded, so the fixed point cannot be worse than one pass.
    """
    assignment, why = {}, {}
    loose, free = list(inside), list(areas)

    for _round in range(len(areas) + 1):
        placed = dict(inside)
        placed.update(assignment)       # decided labels stand where they go
        found, still_loose, still_free, reasons = _one_pass(
            placed, areas, prior, tol)
        if len(found) <= len(assignment):
            break
        for label in found:
            # keep the reason it was first placed for: on a later round every
            # settled label would just say "already inside", which is true
            # and useless.
            if label not in assignment:
                why[label] = reasons[label]
        assignment, loose, free = found, still_loose, still_free

    return assignment, loose, free, why


def _one_pass(inside, areas, prior, tol):
    """One application of the rules.  Same signature as resolve()."""
    holders = {}
    for label, region in inside.items():
        if region is not None:
            holders.setdefault(region, []).append(label)

    def fits(label, region):
        was = prior.get(label)
        return was is not None and abs(was - areas[region]) <= tol

    # A label alone in a room the right size is not in dispute, and cannot be
    # a rival for anything else.
    settled = set()
    for region, found in holders.items():
        if len(found) == 1 and fits(found[0], region):
            settled.add(found[0])

    assignment, why = {}, {}

    # 1. Already in a region of its own.
    #
    # Standing in a room by yourself is normally proof enough.  It stops
    # being proof when the areas contradict it: if this room is not the size
    # this label's room was, and exactly one label that is *not* settled has
    # a room that was this size, the two have swapped places and granting the
    # sitting tenant the room would strand the one that belongs in it.  That
    # is what a wall moving through a row of small rooms does -- each label
    # ends up alone in its neighbour's room, so every one of them looks
    # settled and none of them is.  Deferring costs nothing: a deferred label
    # that nothing better turns up for takes its room back below.
    for region, found in holders.items():
        if len(found) != 1:
            continue
        label = found[0]
        rivals = [other for other in inside
                  if other is not label and other not in settled
                  and fits(other, region)]
        contradicted = prior.get(label) is not None and not fits(label, region)
        if contradicted and len(rivals) == 1:
            continue
        assignment[label] = region
        why[label] = "already inside"

    # A region holding several labels keeps whichever one its area vouches
    # for; the rest are adrift and the region is not free unless none does.
    for region, found in holders.items():
        if len(found) < 2:
            continue
        keeper = None
        for label in found:
            was = prior.get(label)
            if was is not None and abs(was - areas[region]) <= tol:
                keeper = label
                break
        if keeper is not None:
            assignment[keeper] = region
            why[keeper] = "already inside, and its area matches"

    loose = [label for label in inside if label not in assignment]
    taken = set(assignment.values())
    free = [region for region in areas if region not in taken]

    # 2. Unchanged area: the room moved but did not resize.
    for label in list(loose):
        was = prior.get(label)
        if was is None:
            continue
        matches = [r for r in free if abs(areas[r] - was) <= tol]
        if len(matches) != 1:
            continue
        assignment[label] = matches[0]
        why[label] = "area unchanged at %.2f m2" % was
        free.remove(matches[0])
        loose.remove(label)

    # 2b. Nothing better turned up.  A label deferred at step 1 because its
    # area pointed elsewhere, still standing alone in a room nobody claimed,
    # keeps it -- so deferring can only ever delay a decision, never lose it.
    for label in list(loose):
        region = inside.get(label)
        if region in free and len(holders.get(region, ())) == 1:
            assignment[label] = region
            why[label] = "already inside, and nothing else claimed it"
            free.remove(region)
            loose.remove(label)

    # 3. One of each left over.
    if len(loose) == 1 and len(free) == 1:
        assignment[loose[0]] = free[0]
        why[loose[0]] = "the only label and the only room left"
        loose, free = [], []

    return assignment, loose, free, why


# --------------------------------------------------------------------------
# document side
# --------------------------------------------------------------------------

def load_prior(path):
    """{OS_SpaceId: area_m2} from before the edit.

    Takes either an exported floorplan JSON or a model's .fcmap.json, because
    which one is to hand depends on what you were doing.  After a plan edit
    the floorplan JSON has usually already been overwritten by the export that
    failed, while the fcmap beside the .osm still holds what the model was
    actually built from -- which is the record you want anyway.
    """
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    out = {}
    for story in data.get("stories", []):
        for space in story.get("spaces", []):
            if space.get("id"):
                out[space["id"]] = space["area_m2"]
    for space_id, entry in (data.get("spaces") or {}).items():
        if "source_area_m2" in entry:
            out.setdefault(space_id, entry["source_area_m2"])
    return out


def label_text(label):
    number, name = fb.parse_label_text(label)
    return ("%s %s" % (number, name)).strip() or label.Label


def story_moves(sketch, labels, sketches, prior_by_id, recentre):
    """What this story needs, as (moves, homeless, empty, why).

    A move is (label, region, current point, wanted point) in the sketch's own
    frame for reporting and in global millimetres for writing.
    """
    faces = fb.extract_room_faces(sketch)
    mine = fb.labels_for_sketch(sketch, labels, sketches)
    pairs, unlabeled, orphans, multi = fb.match_labels_to_faces(
        faces, mine, sketch)

    index = {id(face): face for face in faces}
    inside = {}
    for label in mine:
        inside[label] = None
    for face, label in pairs:
        inside[label] = id(face)
    for face, found in multi:
        for label in found:
            inside[label] = id(face)

    areas = {id(face): fb.area_m2(face) for face in faces}
    prior = {}
    for label in mine:
        space_id = getattr(label, fb.ID_PROP, "") or ""
        if space_id in prior_by_id:
            prior[label] = prior_by_id[space_id]

    assignment, homeless, empty, why = resolve(inside, areas, prior)

    # A label that could not be placed is often sitting in a room that has
    # just been given to another label, so the plan still has two labels in
    # one room after the move and the export still refuses.  Saying whose
    # room it is standing in turns "could not be matched" into something the
    # user can act on without hunting for it on the drawing.
    blocking = {}
    for label in homeless:
        region = inside.get(label)
        if region is None:
            continue
        for other, target in assignment.items():
            if target == region and other is not label:
                blocking[label] = other
                break

    moves = []
    for label, region in assignment.items():
        if inside.get(label) == region and not recentre:
            continue                      # already home; leave it alone
        face = index[region]
        wanted = fb.interior_point(face, sketch)
        current = label.Placement.Base
        moves.append((label, face, current, wanted, why.get(label, "")))
    stuck = [(label, blocking.get(label)) for label in homeless]
    return moves, stuck, [index[r] for r in empty], areas


def running_as_macro():
    """True when this file is being run from inside the FreeCAD GUI.

    A macro is run in the GUI process, with FreeCAD's own argv, so there is no
    file name for argparse to find and it stops with "the following arguments
    are required: fcstd" -- which reads as a broken script rather than as the
    wrong entry point.  There is nothing to parse and nothing to fix; the GUI
    half is relabel.FCMacro, which works on the open document.

    GuiUp is the whole test.  Counting argv would look like a tighter check
    and is a worse one: FreeCAD launched on a document has that document's
    path in argv, so the guard would fall through and argparse would happily
    take it as the file to relabel -- silently working on the copy on disk
    rather than the one on screen.  The CLI is always run under the bundled
    python, where GuiUp is 0 (measured).
    """
    return bool(getattr(fb.FreeCAD, "GuiUp", False))


def main():
    if running_as_macro():
        fb.FreeCAD.Console.PrintError(
            "fc_relabel.py is the command-line half; it needs a file name and "
            "a macro is run without one.\n"
            "Run relabel.FCMacro instead -- same matching, but on the open "
            "document, undoable with Ctrl+Z, and it finds the previous room "
            "areas itself.\n")
        return

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("--previous",
                    help="the last exported floorplan JSON, for room areas; "
                         "without it only labels already inside a room of "
                         "their own can be settled")
    ap.add_argument("--apply", action="store_true",
                    help="write the moves (otherwise report only)")
    ap.add_argument("--recentre", action="store_true",
                    help="also re-seat labels that are already in the right "
                         "room")
    args = ap.parse_args()

    path = os.path.abspath(args.fcstd)
    doc = fb.open_document(path)
    sketches = fb.story_sketches(doc)
    if not sketches:
        sys.exit("No sketch has OS_Include set.")
    labels = fb.room_labels(doc)
    prior_by_id = load_prior(args.previous)

    print("document : %s" % doc.Name)
    print("labels   : %d   prior areas: %d" % (len(labels), len(prior_by_id)))
    if not prior_by_id:
        print("  no --previous given, so a label that left its room can only "
              "be placed by elimination")

    total_moves = total_homeless = total_empty = 0
    for sketch in sketches:
        story = str(getattr(sketch, "OS_StoryName", "") or sketch.Label)
        moves, homeless, empty, _areas = story_moves(
            sketch, labels, sketches, prior_by_id, args.recentre)
        total_moves += len(moves)
        total_homeless += len(homeless)
        total_empty += len(empty)

        if not (moves or homeless or empty):
            print("\n%s: every label is in a room of its own." % story)
            continue

        print("\n%s:" % story)
        for label, face, current, wanted, reason in moves:
            here = fb.local_point(current, sketch)
            there = fb.local_point(wanted, sketch)
            print("  move  %-24s [%8.3f, %8.3f] -> [%8.3f, %8.3f]"
                  % (label_text(label), here.x / 1000.0, here.y / 1000.0,
                     there.x / 1000.0, there.y / 1000.0))
            print("        %-24s %.2f m2 room; %s"
                  % ("", fb.area_m2(face), reason))
        for label, blocker in homeless:
            print("  ??    %-24s could not be matched to a room"
                  % label_text(label))
            if blocker is not None:
                print("        %-24s and it is standing in the one now given "
                      "to %s -- move or delete it"
                      % ("", label_text(blocker)))
        for face in empty:
            centre = fb.centroid_m(face, sketch)
            print("  ??    %8.2f m2 region at [%.3f, %.3f] has no label"
                  % (fb.area_m2(face), centre[0], centre[1]))

    print()
    if total_homeless or total_empty:
        print("UNRESOLVED: %d label(s) and %d region(s). These need a "
              "decision:" % (total_homeless, total_empty))
        print("  a region with no label is a room that did not exist before "
              "-- give it a Draft Text, or run fc_seed_labels.py to place a")
        print("  placeholder; a label with no region is one whose room is "
              "gone, so delete it or say where it went.")

    if not total_moves:
        print("nothing to move.")
        return

    if not args.apply:
        print("%d label(s) would move -- re-run with --apply to write it."
              % total_moves)
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.prerelabel-%s.FCStd" % (path[:-len(".FCStd")], stamp)
    shutil.copy2(path, backup)

    written = 0
    for sketch in sketches:
        moves, _homeless, _empty, _areas = story_moves(
            sketch, labels, sketches, prior_by_id, args.recentre)
        for label, _face, current, wanted, _reason in moves:
            placement = label.Placement
            # z is left exactly as it was: which story a label belongs to is
            # decided by which story plane it is nearest, so moving it in z
            # could hand the label to the floor above.
            placement.Base = fb.FreeCAD.Vector(wanted.x, wanted.y, current.z)
            label.Placement = placement
            written += 1

    doc.recompute()
    fb.save_document(doc)
    print("moved %d label(s); saved (backup: %s)"
          % (written, os.path.basename(backup)))


if __name__ == "__main__":
    main()
