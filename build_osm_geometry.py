# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Build OpenStudio geometry from the bridge floorplan JSON.

Run under the bridge venv (system Python 3.12 + openstudio 3.11.0):

    osvenv/Scripts/python.exe build_osm_geometry.py floorplan.json \
        --out runs/mybuilding.osm

Writes the .osm plus a sidecar <model>.fcmap.json mapping each FreeCAD
OS_SpaceId to the OpenStudio space and zone it produced, with a hash of the
source vertices.  That sidecar is the identity anchor for change propagation
-- OpenStudio handles are regenerated on every rebuild, so they cannot serve.

Doing this here rather than through MCP tool calls sidesteps five documented
gaps: no BuildingStory creation tool, spaces silently created on a failed
call, story assignment not positioning geometry, cross-type name
auto-suffixing, and the unreachable Building singleton.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import openstudio

FCMAP_VERSION = 1

# 1 is a flat-topped plan; 2 may also carry a roof and the per-space
# solids it produces; 3 may also carry air_boundary_overrides; 4 may also
# give an ordinary (non-roofed) room a `solid` of its own, pre-split and
# pre-paired from the wall-centerline network (fc_walls.py).  All four
# build correctly here -- this module never reads air_boundary_overrides
# itself, only find_air_boundaries.py does.
SUPPORTED_SCHEMAS = (1, 2, 3, 4)

# A `solid` used to mean "roofed" unconditionally -- fc_roof.py was the only
# source of one.  Schema 4 also gives an ordinary room a `solid` (see
# fc_walls.py), so anything that used spec.get("solid") as a stand-in for
# "this room's height came from the roof, not a story override" has to
# check the source instead.
ROOF_SOLID_SOURCES = {"roof-extend", "roof-attic"}


def sanitize(text):
    """OpenStudio names are global across object types; keep them tame.

    Returns "" for input that is entirely punctuation, so callers can apply
    their own fallback rather than having one silently baked in here.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 _.-]", "", (text or "").strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def space_height(space, story):
    """Extrusion height for one space, in metres.

    A room may override its story's floor-to-floor -- a double-height lobby,
    a warehouse bay open to the roof.  Absent or zero means defer to the
    story, which is what every room does unless told otherwise.
    """
    override = space.get("height_m")
    if override:
        override = float(override)
        if override <= 0:
            # The JSON is documented as hand-authorable, so the exporter is
            # not the only thing that can put a number here.
            raise SystemExit(
                "space %r has height_m %.3f -- must be positive, or omitted "
                "to use the story height" % (space.get("name"), override))
        return override
    return float(story["floor_to_floor_m"])


def vertex_hash(space, story):
    """Stable fingerprint of everything that affects this space's geometry.

    The height override joins the payload only when it is set, so a room
    without one hashes exactly as it did before the override existed.  That
    keeps an existing fcmap valid: adding this feature must not make every
    space look changed and trigger a full rebuild.  The roof solid is folded
    in on the same terms: a flat-topped room hashes as it always did, and
    re-pitching the roof marks exactly the rooms under it as changed.
    """
    payload = {
        "v": [[round(x, 6), round(y, 6)] for x, y in space["vertices"]],
        "e": round(story["elevation_m"], 6),
        "h": round(story["floor_to_floor_m"], 6),
    }
    if space.get("height_m"):
        payload["oh"] = round(float(space["height_m"]), 6)
    solid = space.get("solid")
    if solid:
        payload["s"] = [
            [surface["type"]] + [[round(c, 6) for c in v]
                                 for v in surface["vertices"]]
            for surface in solid["surfaces"]]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def load_fcmap(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("spaces", {})
    return {}


def make_floor_print(vertices, z):
    """Point3dVector oriented so fromFloorPrint yields a downward floor.

    The exporter emits counter-clockwise polygons (positive signed area in
    plan), which gives an upward normal.  OpenStudio wants the floor facing
    down, so reverse when the outward normal points up -- the same correction
    create_space_from_floor_print applies.
    """
    pts = openstudio.Point3dVector()
    for x, y in vertices:
        pts.append(openstudio.Point3d(x, y, z))

    normal = openstudio.getOutwardNormal(pts)
    if normal.is_initialized() and normal.get().z() > 0:
        pts = openstudio.Point3dVector(list(reversed(list(pts))))
    return pts


def point3d_vector(vertices):
    pts = openstudio.Point3dVector()
    for x, y, z in vertices:
        pts.append(openstudio.Point3d(x, y, z))
    return pts


def space_from_solid(model, solid, registry=None):
    """A space built surface by surface, for anything a prism cannot say.

    fromFloorPrint can only make a flat-topped box.  A space whose ceiling
    follows a pitched roof, and an attic, are both given to us as an explicit
    set of faces instead, already wound so each outward normal points out of
    the room.

    OpenStudio derives Floor / Wall / RoofCeiling from that normal itself, so
    the type FreeCAD recorded is not imposed here -- it is compared, and any
    disagreement handed back, because the two disagreeing means one of them
    has the winding wrong and that is not something to paper over.

    A surface carrying a match_id -- an attic floor piece and the room
    ceiling it mirrors, minted together by fc_roof.py's mirror_ceiling --
    is recorded into `registry` under that id, so apply_known_adjacencies
    can pair the two once every space exists.  See that function for why
    this has to happen before intersectSurfaces runs, not after.
    """
    space = openstudio.model.Space(model)
    mismatches = []
    for spec in solid["surfaces"]:
        surface = openstudio.model.Surface(
            point3d_vector(spec["vertices"]), model)
        surface.setSpace(space)
        if surface.surfaceType() != spec["type"]:
            mismatches.append((surface.nameString(), spec["type"],
                               surface.surfaceType()))
        match_id = spec.get("match_id")
        if match_id and registry is not None:
            registry.setdefault(match_id, []).append(surface)
    return space, mismatches


def create_space(model, spec, story_spec, registry=None):
    """(space, mismatches) -- the one place a space comes into existence."""
    solid = spec.get("solid")
    if solid:
        return space_from_solid(model, solid, registry)

    pts = make_floor_print(spec["vertices"], story_spec["elevation_m"])
    built = openstudio.model.Space.fromFloorPrint(
        pts, space_height(spec, story_spec), model)
    return (built.get() if built.is_initialized() else None), []


def apply_known_adjacencies(registry):
    """Pair surfaces the plan already knows are the same physical boundary.

    Set before intersectSurfaces/matchSurfaces run, not after: those calls
    still refragment an already-perfectly-mirrored pair when reconciling
    many spaces at once.  Measured on this bridge's own test building: 13
    clean attic-floor pieces, each already the exact mirror of the room
    ceiling it belongs to, came back as 31 pieces -- 30 of them with a
    spurious diagonal edge nothing in the plan drew -- purely from being
    present in a 36-space intersectSurfaces call.  The same 13 pairs, told
    to OpenStudio directly first, survive that call untouched: it does not
    re-examine a surface that already has an adjacent one.

    Returns how many pairs were set.
    """
    paired = 0
    for surfaces in registry.values():
        if len(surfaces) != 2:
            continue
        a, b = surfaces
        a.setAdjacentSurface(b)
        paired += 1
    return paired


def build(plan, existing_map):
    model = openstudio.model.Model()
    building = model.getBuilding()
    building.setNorthAxis(float(plan.get("north_axis_deg", 0.0)))
    building.setName(sanitize(os.path.splitext(
        plan.get("source_document", "Building"))[0]) or "Building")

    fcmap, problems, below_grade, overrides = {}, [], [], []
    zone_seq = 0
    adjacency_registry = {}

    for story_spec in plan["stories"]:
        story = openstudio.model.BuildingStory(model)
        story.setName(sanitize(story_spec["name"]) or "Story")
        story.setNominalZCoordinate(story_spec["elevation_m"])
        story.setNominalFloortoFloorHeight(story_spec["floor_to_floor_m"])

        elevation = story_spec["elevation_m"]

        for spec in story_spec["spaces"]:
            zone_seq += 1
            height = space_height(spec, story_spec)
            prior = existing_map.get(spec["id"], {})

            # Reuse the name a previous build gave this id, so re-exports do
            # not rename spaces out from under HVAC assignments.
            space_name = prior.get("space_name") or "%03d-%s-%s" % (
                zone_seq, sanitize(spec["room_number"]) or "000",
                sanitize(spec["name"]) or "Space")
            zone_name = prior.get("zone_name") or "Zone %s" % space_name

            space, mismatches = create_space(model, spec, story_spec,
                                             adjacency_registry)
            if space is None:
                problems.append("fromFloorPrint failed for %r (%d vertices)"
                                % (spec["name"], len(spec["vertices"])))
                continue
            for surface_name, wanted, got in mismatches:
                problems.append(
                    "%s/%s: FreeCAD called %s a %s, OpenStudio made it a %s "
                    "-- the vertex winding disagrees with the normal"
                    % (story_spec["name"], spec["name"], surface_name,
                       wanted, got))

            space.setName(space_name)
            space.setBuildingStory(story)

            zone = openstudio.model.ThermalZone(model)
            zone.setName(zone_name)
            space.setThermalZone(zone)

            if elevation < 0:
                below_grade.append(space.nameString())
            # A roofed space has no single top to report, and its height came
            # from the roof rather than from anyone overriding a story.  An
            # ordinary room's own wall-network solid (schema 4) is not a
            # roof, so it must not be excluded here the same way.
            solid_source = (spec.get("solid") or {}).get("source")
            if spec.get("height_m") and solid_source not in ROOF_SOLID_SOURCES:
                overrides.append((space.nameString(),
                                  story_spec["floor_to_floor_m"], height,
                                  elevation + height))

            fcmap[spec["id"]] = {
                "space_name": space.nameString(),
                "zone_name": zone.nameString(),
                "story": story.nameString(),
                "room_number": spec["room_number"],
                "room_name": spec["name"],
                "vertex_hash": vertex_hash(spec, story_spec),
                "source_area_m2": spec["area_m2"],
                "height_m": round(height, 6),
            }
            if solid_source:
                fcmap[spec["id"]]["solid_source"] = solid_source

    paired = apply_known_adjacencies(adjacency_registry)

    # getSpaces() hands back a tuple; these two want a real SpaceVector.
    spaces = openstudio.model.SpaceVector()
    for space in model.getSpaces():
        spaces.append(space)
    openstudio.model.intersectSurfaces(spaces)
    openstudio.model.matchSurfaces(spaces)
    resynced = resync_matched_surfaces(spaces)
    duplicates = drop_duplicate_surfaces(model)

    return (model, fcmap, problems, below_grade, overrides, duplicates,
            resynced, paired)


def drop_duplicate_surfaces(model):
    """Remove surfaces intersectSurfaces left doubled up.

    Splitting one large surface against many small ones can emit the same
    piece twice: once matched to the neighbour it was split against, and once
    orphaned.  Measured on an attic floor split against 34 ceilings -- one
    2.863 m2 patch came out twice, which put the space 0.31% over its true
    floor area and left four edges used three times, so OpenStudio declared it
    unenclosed and fell back to an approximate volume.

    Two surfaces of one space with the same outline are never both real, so
    the orphan goes and the matched copy stays.  Returns what was removed;
    silently dropping model objects is not something to do without saying so.
    """
    removed = []
    for space in model.getSpaces():
        groups = {}
        for surface in space.surfaces():
            key = tuple(sorted((round(v.x(), 4), round(v.y(), 4),
                                round(v.z(), 4)) for v in surface.vertices()))
            groups.setdefault(key, []).append(surface)
        for group in groups.values():
            if len(group) < 2:
                continue
            # Keep whichever is load-bearing: one that is matched to a
            # neighbour, or failing that one that carries openings.
            keep = max(group, key=lambda s: (
                s.adjacentSurface().is_initialized(), len(s.subSurfaces())))
            for surface in group:
                if surface.handle() == keep.handle():
                    continue
                removed.append((space.nameString(), surface.nameString(),
                                surface.grossArea(),
                                len(surface.subSurfaces())))
                surface.remove()
    return removed


def resync_matched_surfaces(spaces):
    """Force a matched surface pair to share bit-identical vertices.

    intersectSurfaces splits a large surface -- an attic floor spanning many
    rooms -- against each smaller one it borders, and matchSurfaces then
    pairs the resulting pieces up.  Both are OpenStudio's own geometry
    kernel, and neither guarantees reproducing the same point twice to the
    last bit: measured on an attic floor whose input was already
    bit-identical on the room side, the matched attic-floor piece still came
    back 0.08 mm off.  FreeCAD's own drawability check fails below that --
    0.005 mm -- so a room that genuinely reaches its attic can still come
    back as an outline that will not draw a face, with no error from
    OpenStudio anywhere.

    One side's vertices are copied onto the other, reversed to match the
    winding a matched interior surface always has; which side is arbitrary,
    since they are only ever supposed to be the same polygon seen from
    opposite spaces.  Returns how many pairs were resynced.
    """
    seen, resynced = set(), 0
    for space in spaces:
        for surface in space.surfaces():
            partner = surface.adjacentSurface()
            if not partner.is_initialized():
                continue
            partner = partner.get()
            pair_key = tuple(sorted((surface.nameString(),
                                     partner.nameString())))
            if pair_key in seen:
                continue
            seen.add(pair_key)

            mine = [(round(v.x(), 9), round(v.y(), 9), round(v.z(), 9))
                   for v in surface.vertices()]
            theirs = [(round(v.x(), 9), round(v.y(), 9), round(v.z(), 9))
                     for v in partner.vertices()]
            if theirs == list(reversed(mine)):
                continue

            mirrored = openstudio.Point3dVector(
                [openstudio.Point3d(x, y, z) for x, y, z in reversed(mine)])
            if partner.setVertices(mirrored):
                resynced += 1
    return resynced


def apply_ground_boundaries(model, space_names):
    """Below-grade walls are not auto-flagged as ground contact.

    fromFloorPrint sets the floor to Ground where appropriate but leaves the
    walls Outdoors.  Flip them, and report the count so a foundation
    construction gets assigned -- changing the boundary condition does not
    pull a construction across with it.
    """
    flipped = 0
    wanted = set(space_names)
    for space in model.getSpaces():
        if space.nameString() not in wanted:
            continue
        for surface in space.surfaces():
            if (surface.surfaceType() == "Wall"
                    and surface.outsideBoundaryCondition() == "Outdoors"):
                surface.setOutsideBoundaryCondition("Ground")
                flipped += 1
    return flipped


def verify(model, plan, fcmap):
    """Per-space floor area, FreeCAD vs OpenStudio.

    This is the check that replaces eyeballing a rendered plan against the
    architect's sheet: any disagreement is a translation bug, not a judgement
    call.
    """
    by_name = {s.nameString(): s for s in model.getSpaces()}
    worst, rows = 0.0, []
    for entry in fcmap.values():
        space = by_name.get(entry["space_name"])
        if space is None:
            rows.append((entry["space_name"], entry["source_area_m2"], None,
                         None))
            continue
        built = space.floorArea()
        want = entry["source_area_m2"]
        pct = abs(built - want) / want * 100.0 if want else 0.0
        worst = max(worst, pct)
        rows.append((entry["space_name"], want, built, pct))
    return worst, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("floorplan")
    ap.add_argument("--out", required=True, help="path of the .osm to write")
    ap.add_argument("--fcmap", help="sidecar path (default <out>.fcmap.json)")
    ap.add_argument("--tolerance-pct", type=float, default=0.1,
                    help="max allowed floor-area disagreement (default 0.1%%)")
    args = ap.parse_args()

    with open(args.floorplan, encoding="utf-8") as fh:
        plan = json.load(fh)

    version = plan.get("schema_version", 1)
    if version not in SUPPORTED_SCHEMAS:
        sys.exit(
            "%s is schema version %s; this builder understands %s.\n"
            "A newer plan may carry geometry this build would drop silently, "
            "so it is refused rather than half-read."
            % (os.path.basename(args.floorplan), version,
               " and ".join(str(v) for v in SUPPORTED_SCHEMAS)))

    out = os.path.abspath(args.out)
    fcmap_path = args.fcmap or (os.path.splitext(out)[0] + ".fcmap.json")
    existing = load_fcmap(fcmap_path)
    if existing:
        print("reusing %d name(s) from %s"
              % (len(existing), os.path.basename(fcmap_path)))

    model, fcmap, problems, below_grade, overrides, duplicates, resynced, \
        paired = build(plan, existing)

    if paired:
        print("paired %d surface(s) the plan already knew were the same "
              "boundary, before intersectSurfaces ran" % paired)
    if resynced:
        print("resynced %d matched surface pair(s) intersectSurfaces left "
              "a hair apart" % resynced)

    if duplicates:
        print("removed %d surface(s) intersectSurfaces emitted twice:"
              % len(duplicates))
        for space_name, surface_name, area, subs in duplicates:
            print("  %-32s %-14s %8.3f m2%s"
                  % (space_name, surface_name, area,
                     "  (had %d subsurface(s))" % subs if subs else ""))

    if overrides:
        print("per-room height overrides: %d" % len(overrides))
        for name, story_h, height, top in overrides:
            print("  %-28s %5.3f m -> %5.3f m   top at %6.3f m"
                  % (name, story_h, height, top))

    if below_grade:
        flipped = apply_ground_boundaries(model, below_grade)
        print("below grade: %d space(s), %d wall(s) set to Ground "
              "-- assign a foundation construction to these"
              % (len(below_grade), flipped))

    worst, rows = verify(model, plan, fcmap)
    bad = [r for r in rows if r[3] is None or r[3] > args.tolerance_pct]

    roof = plan.get("roof")
    if roof:
        roofed = sum(1 for st in plan["stories"] for sp in st["spaces"]
                     if (sp.get("solid") or {}).get("source")
                     in ROOF_SOLID_SOURCES)
        print("roof        : %s, %d space(s) shaped by it"
              % (roof.get("method", "-"), roofed))
        for attic in roof.get("attics", []):
            print("  attic %r -> its own space on story %r"
                  % (attic["space"], attic["story"]))

    print("\nspaces      : %d" % len(model.getSpaces()))
    print("zones       : %d" % len(model.getThermalZones()))
    print("stories     : %d" % len(model.getBuildingStorys()))
    print("surfaces    : %d" % len(model.getSurfaces()))
    print("north axis  : %.1f deg" % model.getBuilding().northAxis())
    print("floor area  : %.1f m2" % model.getBuilding().floorArea())
    print("area check  : worst disagreement %.4f%% (tolerance %.2f%%)"
          % (worst, args.tolerance_pct))

    if problems:
        print("\nBUILD PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  %s" % p)

    if bad:
        print("\nAREA MISMATCHES (%d):" % len(bad))
        for name, want, built, pct in bad[:20]:
            if built is None:
                print("  %-40s MISSING from model" % name)
            else:
                print("  %-40s FreeCAD %9.3f  OpenStudio %9.3f  %+.3f%%"
                      % (name, want, built, pct))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    model.save(openstudio.path(out), True)

    with open(fcmap_path, "w", encoding="utf-8") as fh:
        json.dump({
            "schema_version": FCMAP_VERSION,
            "model": os.path.basename(out),
            "source_document": plan.get("source_document"),
            "generated_utc": datetime.now(timezone.utc)
                                     .replace(microsecond=0).isoformat(),
            "spaces": fcmap,
        }, fh, indent=2)
        fh.write("\n")

    print("\nwrote %s" % out)
    print("wrote %s" % fcmap_path)

    if problems or bad:
        sys.exit("\nModel written, but the checks above did not pass clean.")


if __name__ == "__main__":
    main()
