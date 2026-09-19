# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Propagate a floorplan change into an existing .osm.

Run under the bridge venv:

    # report only -- nothing is written
    osvenv/Scripts/python.exe update_osm_geometry.py model.osm floorplan.json

    # apply it
    osvenv/Scripts/python.exe update_osm_geometry.py model.osm floorplan.json \
        --apply --out model.osm

Spaces are matched by the FreeCAD OS_SpaceId recorded in <model>.fcmap.json,
never by geometry, so renaming a room or nudging a wall cannot cause a
mismatch.  Only spaces whose vertex hash changed are rebuilt; a rebuilt space
is reattached to its existing ThermalZone, so the HVAC, thermostat and
schedules hanging off that zone survive the update.

A rename is its own category, and not a cosmetic one: the room name is what
find_air_boundaries.py reads to decide that a space is a mezzanine or a
corridor.  A renamed space keeps its geometry, its surfaces and their names,
and its thermal zone -- only the Space and ThermalZone labels change -- so
construction assignments made against surface names survive it.

Surface intersection and matching is re-run over the changed spaces plus their
geometric neighbours only, rather than the whole model, to keep the blast
radius of an edit small.

A rebuilt space gets new surfaces, but not new identities: surface_identity
records every surface's name, construction and openings before the rebuild and
puts them back afterwards, recognising each surface by the line it stands on
in plan and the height it starts at.  So a roof change no longer costs the
openings, the air boundaries or the surface names, and anything that genuinely
could not be carried is named rather than left to be discovered by diffing
against a backup.
"""

import argparse
import json
import os
import re
import sys

import openstudio

import surface_identity

from build_osm_geometry import (FCMAP_VERSION, SUPPORTED_SCHEMAS,
                                apply_known_adjacencies, create_space,
                                drop_duplicate_surfaces,
                                resync_matched_surfaces, sanitize,
                                space_height, vertex_hash)


def load_model(path):
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.path(path))
    if not loaded.is_initialized():
        raise SystemExit("could not load %s" % path)
    return loaded.get()


def flatten(plan):
    """{space_id: (story_spec, space_spec)} for the whole plan."""
    out = {}
    for story in plan["stories"]:
        for space in story["spaces"]:
            out[space["id"]] = (story, space)
    return out


def sequence_of(space_name):
    """The leading NNN- of a generated space name, or None.

    That prefix is the space's stable ordinal, assigned once at build time, so
    a rename has to keep it rather than renumber.
    """
    match = re.match(r"(\d{3})-", space_name or "")
    return int(match.group(1)) if match else None


def display_name(sequence, space_spec):
    """The OpenStudio space name for a plan room -- same rule as the build."""
    return "%03d-%s-%s" % (sequence,
                           sanitize(space_spec["room_number"]) or "000",
                           sanitize(space_spec["name"]) or "Space")


def renamed_to(prior, space_spec):
    """The new space name if the label changed, else None.

    Compared on the plan fields rather than on the built name, so a space
    whose name OpenStudio had to uniquify is not re-flagged every run.  An
    fcmap written before those fields existed reports no rename rather than
    guessing one.
    """
    if (prior.get("room_name", space_spec["name"]) == space_spec["name"]
            and prior.get("room_number", space_spec["room_number"])
            == space_spec["room_number"]):
        return None
    sequence = sequence_of(prior.get("space_name"))
    if sequence is None:
        return None
    proposed = display_name(sequence, space_spec)
    return proposed if proposed != prior.get("space_name") else None


def classify(plan, fcmap):
    incoming = flatten(plan)
    added, changed, unchanged, removed, renamed = [], [], [], [], []

    for space_id, (story, space) in incoming.items():
        prior = fcmap.get(space_id)
        if prior is None:
            added.append(space_id)
        elif prior.get("vertex_hash") != vertex_hash(space, story):
            changed.append(space_id)
        else:
            unchanged.append(space_id)
        # A rename is orthogonal to the geometry, so a space can be both
        # rebuilt and relabelled in one edit.
        if prior is not None and renamed_to(prior, space):
            renamed.append(space_id)

    for space_id in fcmap:
        if space_id not in incoming:
            removed.append(space_id)

    return incoming, added, changed, unchanged, removed, renamed


def bbox_of(space):
    xs, ys, zs = [], [], []
    transformation = space.transformation()
    for surface in space.surfaces():
        for p in transformation * surface.vertices():
            xs.append(p.x())
            ys.append(p.y())
            zs.append(p.z())
    if not xs:
        return None
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def overlaps(a, b, pad=0.5):
    if a is None or b is None:
        return False
    return not (a[3] + pad < b[0] or b[3] + pad < a[0]
                or a[4] + pad < b[1] or b[4] + pad < a[1]
                or a[5] + pad < b[2] or b[5] + pad < a[2])


def story_for(model, name):
    for story in model.getBuildingStorys():
        if story.nameString() == name:
            return story
    story = openstudio.model.BuildingStory(model)
    story.setName(name)
    return story


def repair_exposed(surfaces, model, repaired):
    """Put surfaces back on the outside after their partner was deleted.

    Removing a space deletes its surfaces, and OpenStudio clears the adjacent
    pointer on whatever they were matched to -- but leaves the boundary
    condition reading "Surface".  The result is a surface that claims to face
    another surface and names none: EnergyPlus has no wall to put there, and
    nothing in OpenStudio objects.  Deleting the attic left 33 ceilings and
    923 m2 of roof in exactly that state.

    assignDefault* is the SDK's own answer -- a floor becomes Ground, anything
    else Outdoors, with sun and wind exposure to match -- so a roof that stops
    being an attic floor goes back to being a roof.
    """
    live = {s.handle() for s in model.getSurfaces()}
    for surface in surfaces:
        if surface.handle() not in live:
            continue                      # deleted too, nothing to repair
        if surface.adjacentSurface().is_initialized():
            continue                      # still matched to something real
        if surface.outsideBoundaryCondition() != "Surface":
            continue
        surface.assignDefaultBoundaryCondition()
        surface.assignDefaultSunExposure()
        surface.assignDefaultWindExposure()
        repaired.append((surface.nameString(),
                         surface.outsideBoundaryCondition(),
                         surface.grossArea()))
    return repaired


def rebuild_space(model, old_space, story_spec, space_spec, name, zone,
                  registry=None):
    """Replace a space's geometry, keeping its name and thermal zone.

    The ThermalZone object itself is never touched, so everything connected to
    it -- air loop, terminal, thermostat, sizing -- is still connected when the
    new space is attached.
    """
    space_type = None
    if old_space is not None:
        if old_space.spaceType().is_initialized():
            space_type = old_space.spaceType().get()
        old_space.remove()

    space, _mismatches = create_space(model, space_spec, story_spec, registry)
    if space is None:
        return None
    space.setName(name)
    space.setBuildingStory(story_for(model, sanitize(story_spec["name"])
                                     or "Story"))
    if zone is not None:
        space.setThermalZone(zone)
    if space_type is not None:
        space.setSpaceType(space_type)
    return space


def subsurfaces_at_risk(model, space_names):
    """The windows and doors a rebuild is about to take with it.

    Rebuilding a space removes its surfaces, and removing a surface removes
    the subsurfaces on it.  That is easy to miss because the update reports
    spaces, not openings -- a roof change marks every room under it as
    changed, and a model with fenestration loses every opening in those rooms
    without a word.

    Returns {space name: [(subsurface name, type, host surface name)]}.
    """
    wanted = set(space_names)
    at_risk = {}
    for space in model.getSpaces():
        if space.nameString() not in wanted:
            continue
        for surface in space.surfaces():
            for sub in surface.subSurfaces():
                at_risk.setdefault(space.nameString(), []).append(
                    (sub.nameString(), sub.subSurfaceType(),
                     surface.nameString()))
    return at_risk


def air_boundaries_at_risk(model, space_names):
    """The air-boundary couplings a rebuild is about to drop.

    The same mechanism that takes the openings takes these: a rebuilt space
    loses its surfaces, and the construction assignment goes with them.  It
    is far easier to miss, because an air boundary leaves no geometry behind
    to look wrong -- the wall is simply solid again, the model still builds,
    and the zones quietly stop exchanging air.

    Worse, find_air_boundaries.py cannot always put them back: its rules
    re-derive the ones a floor plan implies, but any coupling assigned by
    hand is outside them.  Exactly that happened to 033-204-Stair 2 <->
    034-205-Mezzanine, which was hand-made, was lost in a roof rebuild, and
    was only found by diffing against a backup.

    Returns {"space a <-> space b": (construction name, area m2, ach)}.
    """
    wanted = set(space_names)
    at_risk = {}
    for surface in model.getSurfaces():
        construction = surface.construction()
        if not construction.is_initialized():
            continue
        boundary = construction.get().to_ConstructionAirBoundary()
        if not boundary.is_initialized():
            continue
        here, adjacent = surface.space(), surface.adjacentSurface()
        if not (here.is_initialized() and adjacent.is_initialized()):
            continue
        there = adjacent.get().space()
        if not there.is_initialized():
            continue
        names = (here.get().nameString(), there.get().nameString())
        if not wanted.intersection(names):
            continue
        key = " <-> ".join(sorted(names))
        name, area, ach = at_risk.get(
            key, (construction.get().nameString(), 0.0,
                  boundary.get().simpleMixingAirChangesPerHour()))
        at_risk[key] = (name, area + surface.grossArea() / 2.0, ach)
    return at_risk


def report_air_boundary_risk(at_risk):
    if not at_risk:
        return
    print()
    print("  %d air-boundary coupling(s) touch spaces about to be rebuilt."
          % len(at_risk))
    print("  surface_identity carries these across, hand-made ones included,")
    print("  by recognising each wall from the line it stands on.")
    for key in sorted(at_risk):
        name, area, ach = at_risk[key]
        print("    %-56s %7.2f m2  %s ACH" % (key, area, ach))
    print()
    print("  Any it cannot place is named after the apply. Check that list --")
    print("  an air boundary leaves nothing behind to look wrong when it goes")
    print("  missing: the wall simply reads solid, the model still builds, and")
    print("  the zones quietly stop exchanging air.  There is no need to")
    print("  re-run find_air_boundaries.py; doing so re-proposes couplings you")
    print("  may have deliberately excluded, which is how a rated fire wall")
    print("  was once reopened.")


def report_subsurface_risk(at_risk):
    total = sum(len(v) for v in at_risk.values())
    print()
    print("  %d subsurface(s) sit on spaces about to be rebuilt, in %d room(s)."
          % (total, len(at_risk)))
    print("  surface_identity re-cuts these onto the rebuilt walls, keeping")
    print("  their names, types and OS_OpeningId comments.")
    for space_name in sorted(at_risk):
        kinds = {}
        for _name, kind, _host in at_risk[space_name]:
            kinds[kind] = kinds.get(kind, 0) + 1
        print("    %-38s %s"
              % (space_name, ", ".join("%s %d" % kv
                                       for kv in sorted(kinds.items()))))
    print()
    print("  One case it will not guess at: an opening whose host changes")
    print("  pitch, a skylight in a roof going from flat to sloped.  That")
    print("  outline genuinely has to be re-cut from the drawing, so it is")
    print("  reported rather than reprojected.  Put those back with")
    print("  dump -> import -> planes -> openings -> apply; the elevation")
    print("  sketches stand on the traced plan, not on the walls being")
    print("  rebuilt, so no outline moves.")
    print()
    print("  They are re-created, so their handles change even when the name")
    print("  and id do not: a shading control or frame-and-divider that")
    print("  references a subsurface still has to be reattached.  See")
    print("  IDENTITY.md.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("osm")
    ap.add_argument("floorplan")
    ap.add_argument("--fcmap", help="defaults to <osm>.fcmap.json")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (otherwise report only)")
    ap.add_argument("--out", help="defaults to overwriting the input .osm")
    args = ap.parse_args()

    osm_path = os.path.abspath(args.osm)
    fcmap_path = args.fcmap or (os.path.splitext(osm_path)[0] + ".fcmap.json")
    if not os.path.exists(fcmap_path):
        raise SystemExit(
            "No %s -- this model was not built by the bridge, so there is no\n"
            "id mapping to diff against.  Build it with build_osm_geometry.py "
            "first." % os.path.basename(fcmap_path))

    with open(fcmap_path, encoding="utf-8") as fh:
        sidecar = json.load(fh)
    fcmap = sidecar.get("spaces", {})
    with open(args.floorplan, encoding="utf-8") as fh:
        plan = json.load(fh)

    version = plan.get("schema_version", 1)
    if version not in SUPPORTED_SCHEMAS:
        raise SystemExit(
            "%s is schema version %s; this updater understands %s."
            % (os.path.basename(args.floorplan), version,
               " and ".join(str(v) for v in SUPPORTED_SCHEMAS)))

    incoming, added, changed, unchanged, removed, renamed = classify(plan,
                                                                     fcmap)

    print("unchanged : %d" % len(unchanged))
    print("changed   : %d" % len(changed))
    print("added     : %d" % len(added))
    print("removed   : %d" % len(removed))
    print("renamed   : %d" % len(renamed))

    roofed = 0
    for space_id in changed:
        _story, space = incoming[space_id]
        prior = fcmap[space_id]
        # A space reshaped by the roof keeps its footprint exactly, so the
        # area columns are identical and say nothing.  What moved is its top.
        note = ""
        if space.get("solid"):
            roofed += 1
            tops = [v[2] for s in space["solid"]["surfaces"]
                    for v in s["vertices"]]
            note = ("   roof: top %s to %.3f m"
                    % ("now" if prior.get("roof") else "flat, now",
                       max(tops)))
        elif prior.get("roof"):
            note = "   roof: no longer shaped by it, back to a flat top"
        print("  CHANGED  %-38s %.2f -> %.2f m2%s"
              % (prior["space_name"], prior["source_area_m2"],
                 space["area_m2"], note))
    if roofed:
        print()
        print("  %d of those changed only at the top, where the roof is. The"
              % roofed)
        print("  footprint is the same, which is why the areas match; the")
        print("  volume and the roof surface are what moved.")
    for space_id in added:
        _story, space = incoming[space_id]
        print("  ADDED    %s / %s (%.2f m2)"
              % (space["room_number"], space["name"], space["area_m2"]))
    for space_id in removed:
        print("  REMOVED  %-38s (zone %s)"
              % (fcmap[space_id]["space_name"], fcmap[space_id]["zone_name"]))
    for space_id in renamed:
        _story, space = incoming[space_id]
        prior = fcmap[space_id]
        print("  RENAMED  %-38s -> %s"
              % (prior["space_name"], renamed_to(prior, space)))
    if renamed:
        print()
        print("  A rename carries meaning: find_air_boundaries.py reads the")
        print("  room name to identify mezzanines and corridors, so re-run it")
        print("  after applying. Geometry, surfaces and thermal zones are")
        print("  untouched, so existing surface assignments survive.")

    model = load_model(osm_path)
    doomed_spaces = [fcmap[i]["space_name"] for i in changed + removed]
    doomed = subsurfaces_at_risk(model, doomed_spaces)
    if doomed:
        report_subsurface_risk(doomed)
    report_air_boundary_risk(air_boundaries_at_risk(model, doomed_spaces))

    if not args.apply:
        print("\nreport only -- re-run with --apply to write the changes")
        return

    if not (changed or added or removed or renamed):
        print("\nnothing to apply")
        return

    spaces_by_name = {s.nameString(): s for s in model.getSpaces()}
    zones_by_name = {z.nameString(): z for z in model.getThermalZones()}

    # Before anything is removed.  The names are read now because a space can
    # be reshaped and relabelled in one edit, and the capture has to be found
    # again under whichever name it ends up with.
    was_named = {i: fcmap[i]["space_name"] for i in changed}
    captured, ambiguous = surface_identity.capture(
        model, list(was_named.values()))

    touched, problems, orphans = [], [], []
    repaired, dropped = [], []
    adjacency_registry = {}

    for space_id in removed:
        entry = fcmap[space_id]
        space = spaces_by_name.get(entry["space_name"])
        if space is None:
            continue
        zone = space.thermalZone()
        if zone.is_initialized() and zone.get().equipment():
            orphans.append((entry["space_name"], zone.get().nameString()))
        # Everything this space was matched to has to be told it is now on
        # the outside.  Collect the partners first: once the space is gone
        # there is nothing left to ask.
        exposed = [s.adjacentSurface().get() for s in space.surfaces()
                   if s.adjacentSurface().is_initialized()]
        empty_zone = zone.get() if (zone.is_initialized()
                                    and not zone.get().equipment()) else None
        story = space.buildingStory()
        empty_story = story.get() if story.is_initialized() else None
        space.remove()
        del fcmap[space_id]
        repair_exposed(exposed, model, repaired)
        if empty_zone is not None and not empty_zone.spaces():
            dropped.append(("zone", empty_zone.nameString()))
            empty_zone.remove()
        if empty_story is not None and not empty_story.spaces():
            dropped.append(("story", empty_story.nameString()))
            empty_story.remove()

    for space_id in changed:
        story_spec, space_spec = incoming[space_id]
        entry = fcmap[space_id]
        old = spaces_by_name.get(entry["space_name"])
        zone = zones_by_name.get(entry["zone_name"])
        space = rebuild_space(model, old, story_spec, space_spec,
                              entry["space_name"], zone, adjacency_registry)
        if space is None:
            problems.append("rebuild failed for %s" % entry["space_name"])
            continue
        # The old space object is gone; anything that looks a space up by name
        # after this -- the rename pass, three loops down -- has to find the
        # new one.  A removed ModelObject does not complain when you set its
        # name, it just does nothing, so a stale entry here reads as a rename
        # that silently did not happen.
        spaces_by_name[entry["space_name"]] = space
        entry["vertex_hash"] = vertex_hash(space_spec, story_spec)
        entry["source_area_m2"] = space_spec["area_m2"]
        entry["height_m"] = round(space_height(space_spec, story_spec), 6)
        entry["room_number"] = space_spec["room_number"]
        entry["room_name"] = space_spec["name"]
        if space_spec.get("solid"):
            entry["roof"] = space_spec["solid"]["source"]
        else:
            entry.pop("roof", None)
        touched.append(space)

    # After the rebuilds, so a space that was both reshaped and relabelled
    # gets its new geometry under its old name and is then renamed once.
    for space_id in renamed:
        _story_spec, space_spec = incoming[space_id]
        entry = fcmap[space_id]
        wanted = renamed_to(entry, space_spec)
        if wanted is None:
            continue
        space = spaces_by_name.get(entry["space_name"])
        if space is None:
            problems.append("cannot rename missing space %s"
                            % entry["space_name"])
            continue
        space.setName(wanted)
        # setName uniquifies on collision, so read back what it actually took.
        actual = space.nameString()
        if actual != wanted:
            problems.append("renamed %s to %s, not %s -- name was taken"
                            % (entry["space_name"], actual, wanted))
        zone = space.thermalZone()
        if zone.is_initialized():
            zone.get().setName("Zone %s" % actual)
            entry["zone_name"] = zone.get().nameString()
        entry["space_name"] = actual
        entry["room_number"] = space_spec["room_number"]
        entry["room_name"] = space_spec["name"]

    next_seq = len(fcmap) + 1
    for space_id in added:
        story_spec, space_spec = incoming[space_id]
        name = "%03d-%s-%s" % (next_seq,
                               sanitize(space_spec["room_number"]) or "000",
                               sanitize(space_spec["name"]) or "Space")
        next_seq += 1
        zone = openstudio.model.ThermalZone(model)
        zone.setName("Zone %s" % name)
        space = rebuild_space(model, None, story_spec, space_spec, name, zone,
                              adjacency_registry)
        if space is None:
            problems.append("could not create %s" % name)
            continue
        fcmap[space_id] = {
            "space_name": space.nameString(),
            "zone_name": zone.nameString(),
            "story": sanitize(story_spec["name"]) or "Story",
            "room_number": space_spec["room_number"],
            "room_name": space_spec["name"],
            "vertex_hash": vertex_hash(space_spec, story_spec),
            "source_area_m2": space_spec["area_m2"],
            "height_m": round(space_height(space_spec, story_spec), 6),
        }
        if space_spec.get("solid"):
            fcmap[space_id]["roof"] = space_spec["solid"]["source"]
        touched.append(space)

    # Intersect and match the touched spaces together with anything whose
    # bounding box they reach, rather than the whole model.
    affected = openstudio.model.SpaceVector()
    touched_boxes = [bbox_of(s) for s in touched]
    touched_names = {s.nameString() for s in touched}
    for space in model.getSpaces():
        if space.nameString() in touched_names:
            affected.append(space)
            continue
        box = bbox_of(space)
        if any(overlaps(box, tb) for tb in touched_boxes):
            affected.append(space)

    paired = apply_known_adjacencies(adjacency_registry)
    resynced = 0
    if len(affected) > 1:
        openstudio.model.intersectSurfaces(affected)
        openstudio.model.matchSurfaces(affected)
        resynced = resync_matched_surfaces(affected)
    if paired:
        print("\npaired %d surface(s) the plan already knew were the same "
              "boundary, before intersectSurfaces ran" % paired)
    print("\nre-matched %d space(s) (%d rebuilt + neighbours)"
          % (len(affected), len(touched)))
    if resynced:
        print("  resynced %d matched surface pair(s) intersectSurfaces "
              "left a hair apart" % resynced)

    for space_name, surface_name, area, subs in drop_duplicate_surfaces(model):
        print("  removed duplicate %-14s from %-30s %8.3f m2%s"
              % (surface_name, space_name, area,
                 "  (had %d subsurface(s))" % subs if subs else ""))

    # After intersect, match and the duplicate sweep: the capture is split the
    # way the old model was, and only now is the new model split the same way.
    if captured:
        now = {was_named[i]: fcmap[i]["space_name"]
               for i in changed if i in fcmap}
        surface_identity.report_restored(
            surface_identity.restore(model, captured, now), ambiguous)

    if repaired:
        by_bc = {}
        for _name, bc, area in repaired:
            count, total = by_bc.get(bc, (0, 0.0))
            by_bc[bc] = (count + 1, total + area)
        print("  put back on the outside after their partner was removed:")
        for bc, (count, total) in sorted(by_bc.items()):
            print("    %-9s %3d surface(s), %8.1f m2" % (bc, count, total))

    if dropped:
        print("  removed with it: %s"
              % ", ".join("%s %s" % (kind, name) for kind, name in dropped))

    if orphans:
        print("\nZONES LEFT WITHOUT SPACES (%d) -- review their HVAC:"
              % len(orphans))
        for space_name, zone_name in orphans:
            print("  %s had zone %s" % (space_name, zone_name))

    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)

    out = os.path.abspath(args.out or osm_path)
    model.save(openstudio.path(out), True)
    sidecar["spaces"] = fcmap
    sidecar["schema_version"] = FCMAP_VERSION
    with open(fcmap_path, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2)
        fh.write("\n")

    print("\nwrote %s" % out)
    print("wrote %s" % fcmap_path)
    if problems:
        sys.exit("\nModel written, but some updates failed.")


if __name__ == "__main__":
    main()
