# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Export a traced FreeCAD floor plan to the bridge JSON schema.

Run under FreeCAD's bundled Python:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" fc_export_floorplan.py \
        FloorplanTest-01.FCStd --out floorplan.json

Reads every sketch flagged OS_Include, recovers each enclosed room from the
wall-centerline network, matches it to its Draft Text label, and writes exact
vertices in metres.  Newly minted OS_SpaceId values are written back into the
FCStd unless --no-write-ids is given.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD  # noqa: E402
import fc_roof  # noqa: E402
import fcbridge as fb  # noqa: E402

# 2 adds the roof: an optional `roof` block, an optional `solid` on any space
# whose shape the roof changed, and the attic story that the Attic method
# invents.  A version 1 plan is a valid version 2 plan -- the fields are all
# additions -- but the reverse is not true, and a builder that ignored `solid`
# would quietly produce the flat-roofed model instead of the one asked for.
SCHEMA_VERSION = 2

# A storey outside this range is a units mistake, not a building.  The story
# properties are App::PropertyLength now, so a bound expression carries its own
# unit and cannot land a millimetre value in a metre field the way the old
# App::PropertyFloat did -- twice.  This stays as the backstop for the one
# route still open: a number typed straight into the property by hand.  Wide
# enough for an aircraft hangar, narrow enough that a factor of a thousand
# cannot hide in it.
MIN_STORY_M = 1.5
MAX_STORY_M = 30.0


NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
ALL_NUMBERS = re.compile(
    r"\[\s*\n\s*(" + NUMBER + r"(?:\s*,\s*" + NUMBER + r")*)\s*\n\s*\]")


def dumps_compact_vertices(payload):
    """Indented JSON, but with each coordinate tuple kept on one line.

    A 43-space plan is ~700 vertices; one coordinate per line makes the file
    unreadable and useless to diff, which defeats the point of a reviewable
    exchange format.  Roof surfaces carry [x, y, z] rather than [x, y], so
    this collapses a run of numbers of any length -- it cannot swallow
    anything else, because a nested array would put a bracket in the middle.
    """
    text = json.dumps(payload, indent=2)
    return ALL_NUMBERS.sub(
        lambda m: "[" + re.sub(r"\s+", " ", m.group(1)) + "]", text)


def natural_key(space):
    """Sort rooms so zone numbering is stable across re-exports."""
    num = space["room_number"]
    digits = "".join(ch for ch in num if ch.isdigit())
    return (0 if digits else 1, int(digits) if digits else 0, num, space["name"])


def build_story(sketch, labels, sketches):
    faces = fb.extract_room_faces(sketch)
    mine = fb.labels_for_sketch(sketch, labels, sketches)
    pairs, unlabeled, orphans, multi = fb.match_labels_to_faces(faces, mine,
                                                                sketch)

    story_name = getattr(sketch, "OS_StoryName", "") or sketch.Label
    problems = []
    for face in unlabeled:
        problems.append(
            "  UNLABELED room in %r: %.1f m2 at centroid %s"
            % (story_name, fb.area_m2(face), fb.centroid_m(face, sketch))
        )
    for face, found in multi:
        names = ", ".join(repr(x.Label) for x in found)
        problems.append(
            "  %d labels inside one room in %r (%.1f m2 at %s): %s"
            % (len(found), story_name, fb.area_m2(face),
               fb.centroid_m(face, sketch),
               names)
        )

    story_height = round(fb.story_height_m(sketch), 6)
    if not MIN_STORY_M <= story_height <= MAX_STORY_M:
        hint = ""
        if MIN_STORY_M <= story_height / 1000.0 <= MAX_STORY_M:
            hint = (" -- that is %.3f m in millimetres. OS_FloorToFloor is a "
                    "length: give it a unit (%.0f mm, or 8ft 10-11/16in) "
                    "rather than a bare number"
                    % (story_height / 1000.0, story_height))
        problems.append(
            "  %r has a floor-to-floor of %g m%s"
            % (story_name, story_height, hint))

    spaces, minted, added_props, relabeled, skipped, overrides = \
        [], 0, 0, 0, [], []
    for face, label in pairs:
        relabeled += 1 if fb.sync_label_display(label) else 0
        if fb.is_skip_label(label):
            skipped.append((label.Label, fb.area_m2(face)))
            continue
        space_id, was_minted = fb.get_or_mint_space_id(label)
        minted += 1 if was_minted else 0
        added_props += 1 if fb.ensure_height_prop(label) else 0
        room_number, name = fb.parse_label_text(label)
        if not name:
            problems.append("  EMPTY label text on %r" % label.Label)
            name = label.Label
        space = {
            "id": space_id,
            "room_number": room_number,
            "name": name,
            "area_m2": fb.area_m2(face),
            "vertices": fb.face_polygon_m(face, sketch),
        }

        height = fb.label_height_m(label)
        if height is not None and height <= 0:
            problems.append(
                "  NEGATIVE %s (%.3f) on %r -- clear it to 0 to use the "
                "story height" % (fb.HEIGHT_PROP, height, label.Label))
        elif height is not None:
            # Only emitted when it differs, so the JSON stays a diff of
            # decisions rather than restating the story height 34 times.
            space["height_m"] = round(height, 6)
            overrides.append((story_name, name, story_height, height))
        spaces.append(space)

    spaces.sort(key=natural_key)

    story = {
        "name": story_name,
        "elevation_m": round(fb.story_elevation_m(sketch), 6),
        "floor_to_floor_m": story_height,
        "source_sketch": sketch.Name,
        "spaces": spaces,
    }
    return (story, problems, orphans, minted, added_props, relabeled,
            skipped, overrides)


def report_roof(roof, notes):
    """Print what the roof did, in the terms an engineer checks it in.

    The numbers worth looking at are the added volume and the extra roof
    area: those are what change the loads.  A roof this shallow changes
    almost nothing, and the report should make that obvious rather than
    implying the pitch mattered.
    """
    if roof is None:
        return

    print("\nroof: %s" % roof.get("method", "-"))
    for entry in roof["objects"]:
        print("  %-24s %s" % (entry["label"], entry["method"]))

    if roof.get("extended"):
        rows = roof["extended"]
        print("  base %.3f m, ridge %.3f m, pitch %.2f deg"
              % (roof["base_elevation_m"], roof["ridge_elevation_m"],
                 roof["pitch_deg"]))
        print("  roof area %.1f m2 over %.1f m2 of plan (+%.2f%%)"
              % (roof["area_m2"], roof["plan_area_m2"],
                 (roof["area_m2"] / roof["plan_area_m2"] - 1.0) * 100.0
                 if roof["plan_area_m2"] else 0.0))
        added = sum(r["area_m2"] * (r["now_top_m"] - r["was_top_m"]) / 2.0
                    for r in rows)
        print("  %d space(s) raised to it, roughly +%.0f m3 of volume"
              % (len(rows), added))
        for row in sorted(rows, key=lambda r: -r["area_m2"]):
            print("    %-12s %-28s %6.3f -> %6.3f m   %8.1f m2"
                  % (row["story"], row["space"][:28], row["was_top_m"],
                     row["now_top_m"], row["area_m2"]))

    for attic in roof.get("attics", []):
        print("  attic %r on story %r: floor %.3f m, ridge %.3f m, "
              "%.1f m2 floor, %.1f m2 roof"
              % (attic["space"], attic["story"], attic["floor_elevation_m"],
                 attic["ridge_elevation_m"], attic["floor_area_m2"],
                 attic["area_m2"]))
        print("    the ceilings below it become interior surfaces -- give "
              "the attic a construction set and decide whether it is "
              "conditioned")

    if notes:
        # Not all of these are spaces left flat -- an attic reports its snap
        # onto the ceilings below here too -- so the heading stays neutral
        # and each note says what it is.
        print("  notes:")
        for note in notes:
            print("  %s" % note)


def cross_story_matches(stories):
    """Footprints appearing identically on more than one story.

    These are the stairwells, shafts and other common features of the manual
    workflow.  Two adjacent stories sharing none is a strong hint that one of
    them is misaligned, so it is reported as a warning.
    """
    seen = {}
    for story in stories:
        for space in story["spaces"]:
            key = tuple(sorted(tuple(v) for v in space["vertices"]))
            seen.setdefault(key, []).append((story["name"], space["name"],
                                             space["area_m2"]))
    return [v for v in seen.values() if len(v) > 1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fcstd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-write-ids", action="store_true",
                    help="do not persist newly minted OS_SpaceId values")
    ap.add_argument("--allow-problems", action="store_true",
                    help="write the JSON even when rooms are unlabeled")
    ap.add_argument("--no-roof", action="store_true",
                    help="ignore OS_RoofMethod and give every space a flat "
                         "top, for comparing against the pitched result")
    args = ap.parse_args()

    doc = fb.open_document(os.path.abspath(args.fcstd))
    sketches = fb.story_sketches(doc)
    if not sketches:
        sys.exit(
            "No sketch has OS_Include set.\n"
            "Run fc_seed_labels.py --init-stories first, or set the OS_* "
            "properties on each story sketch."
        )

    labels = fb.room_labels(doc)

    print("document : %s" % doc.Name)
    print("stories  : %d   labels: %d" % (len(sketches), len(labels)))

    stories, problems, skipped_all, overrides_all = [], [], [], []
    minted_total = props_total = relabeled_total = 0
    for sketch in sketches:
        (story, probs, _orphans, minted, added_props, relabeled, skipped,
         overrides) = build_story(sketch, labels, sketches)
        stories.append(story)
        problems.extend(probs)
        minted_total += minted
        props_total += added_props
        relabeled_total += relabeled
        skipped_all.extend(skipped)
        overrides_all.extend(overrides)
        print("  %-20s %3d rooms   elev %7.3f m   f2f %5.3f m%s"
              % (story["name"], len(story["spaces"]),
                 story["elevation_m"], story["floor_to_floor_m"],
                 "   (%d skipped)" % len(skipped) if skipped else ""))

    if skipped_all:
        print("\nregions deliberately not modelled as spaces: %d (%.1f m2)"
              % (len(skipped_all), sum(a for _, a in skipped_all)))
        for name, area in skipped_all[:10]:
            print("  %-40s %8.1f m2" % (name, area))

    if overrides_all:
        print("\nper-room height overrides (%s): %d"
              % (fb.HEIGHT_PROP, len(overrides_all)))
        for story_name, name, story_h, height in overrides_all:
            print("  %-12s %-28s %5.3f m -> %5.3f m   (top of space %+.3f m)"
                  % (story_name, name, story_h, height, height - story_h))

    roof = None
    if args.no_roof:
        if fc_roof.roof_objects(doc):
            print("\nroof: ignored (--no-roof); every space keeps a flat top")
    else:
        roof, attic_stories, roof_problems, roof_notes = fc_roof.apply(
            doc, sketches, stories)
        problems.extend(roof_problems)
        stories.extend(attic_stories)
        minted_total += sum(1 for st in attic_stories
                            for sp in st["spaces"] if sp.pop("minted", False))
        report_roof(roof, roof_notes)

    # Copy-pasting a Draft Text in the GUI copies its OS_SpaceId too, which
    # would silently collapse two rooms onto one identity in the fcmap.
    seen_ids = {}
    for story in stories:
        for space in story["spaces"]:
            seen_ids.setdefault(space["id"], []).append(
                "%s/%s" % (story["name"], space["name"]))
    for space_id, where in sorted(seen_ids.items()):
        if len(where) > 1:
            problems.append(
                "  DUPLICATE OS_SpaceId %s.. shared by %d rooms (%s) -- a "
                "label was copied; clear OS_SpaceId on all but one so a fresh "
                "id is minted" % (space_id[:8], len(where), ", ".join(where)))

    # Only the stories that came from sketches have labels to account for; an
    # attic is declared on the roof object, so counting it here would mask a
    # label that landed outside every room.
    placed = sum(len(s["spaces"]) for s in stories
                 if "source_sketch" in s) + len(skipped_all)
    if len(labels) > placed:
        problems.append(
            "  %d label(s) are not inside any room -- check placement"
            % (len(labels) - placed))

    if minted_total or props_total or relabeled_total:
        written = []
        if minted_total:
            written.append("minted %d new OS_SpaceId value(s)" % minted_total)
        if props_total:
            written.append("added %s to %d label(s)"
                           % (fb.HEIGHT_PROP, props_total))
        if relabeled_total:
            written.append("relabeled %d object(s) to match their drawn text"
                           % relabeled_total)
        if args.no_write_ids:
            print("%s -- NOT saved (--no-write-ids)" % "; ".join(written))
        else:
            backup = args.fcstd + ".bak"
            if not os.path.exists(backup):
                shutil.copy2(args.fcstd, backup)
            fb.save_document(doc)
            print("%s; saved document (backup: %s)"
                  % ("; ".join(written), os.path.basename(backup)))

    matches = cross_story_matches(stories)
    if matches:
        print("\ncross-story footprint matches (common features): %d"
              % len(matches))
        for group in sorted(matches, key=lambda g: -g[0][2])[:12]:
            where = " = ".join("%s/%s" % (s, n) for s, n, _ in group)
            print("  %8.1f m2  %s" % (group[0][2], where))
    elif len(stories) > 1:
        problems.append(
            "  NO footprint is shared between stories -- stories are probably "
            "misaligned (check each sketch Placement)")

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print(p)
        if not args.allow_problems:
            sys.exit(
                "\nRefusing to write %s.\n"
                "Fix whichever of the problems above apply, or re-run with "
                "--allow-problems\nto export anyway.\n"
                "\nFor an unlabeled region: every enclosed region needs "
                "exactly one room\nlabel -- an unlabeled region is usually a "
                "corridor or a room that would\notherwise be silently "
                "dropped.  If the walls just moved, the labels have\nnot: "
                "run fc_relabel.py, which puts each one back in its own room "
                "and says\nso before it writes.  For a room that is "
                "genuinely new, fc_seed_labels.py\nplaces a placeholder."
                % args.out
            )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_document": os.path.basename(args.fcstd),
        "exported_utc": datetime.now(timezone.utc)
                                .replace(microsecond=0).isoformat(),
        "units": "meters",
        "north_axis_deg": fb.north_axis_deg(doc),
        "stories": stories,
    }
    if roof is not None:
        payload["roof"] = roof

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dumps_compact_vertices(payload))
        fh.write("\n")

    modelled = sum(len(st["spaces"]) for st in stories)
    total_area = sum(s["area_m2"] for st in stories for s in st["spaces"])
    print("\nwrote %s" % out)
    print("  %d stories, %d spaces, %.1f m2 total floor area"
          % (len(stories), modelled, total_area))


if __name__ == "__main__":
    main()
