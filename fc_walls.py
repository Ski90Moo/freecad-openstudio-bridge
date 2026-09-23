# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Pre-split and pre-pair an ordinary room's walls, before OpenStudio exists.

Imported by fc_export_floorplan.py under FreeCAD's Python. Everything here
reads the document; nothing writes to the model.

For a plain, flat-topped room, the wall OpenStudio's own intersectSurfaces
would eventually derive is already fully determined by the 2D
wall-centerline network plus each room's height -- there is no boolean to
run, only arithmetic. This mints the same kind of `solid` block
build_osm_geometry.py's space_from_solid() already knows how to build
(today produced only by fc_roof.py, for a roofed room), so an ordinary room
takes the exact same path: create_space() cannot tell the difference.

Two rooms sharing a wall segment mint one match_id between them
(apply_known_adjacencies() pairs it with setAdjacentSurface before
intersectSurfaces runs, exactly the way fc_roof.mirror_ceiling's attic-floor
pairs already do) -- which is the actual point: a wall handed to OpenStudio
with its neighbour already known survives that call untouched, instead of
being refragmented the way 13 already-correct attic-floor pairs came back
as 31 pieces purely from being present in a 36-space intersectSurfaces call
(see apply_known_adjacencies' own docstring). A height mismatch (a
mezzanine wall shorter than its neighbour) is just as closed-form: the
shared wall splits at the shorter room's own ceiling, and the exposed piece
above it is an ordinary, unpaired wall on the taller room alone -- nothing
here decides whether that piece is real construction or an air boundary,
that is still a separate, later decision by a rule (find_air_boundaries.py)
or a hand tag (tag_airboundary.FCMacro).

What this deliberately does not touch: a wall edge wall_adjacencies()
refuses (a T-junction where 2D adjacency does not resolve to a clean pair,
a SKIP or unlabelled neighbour) is built as an ordinary, unpaired wall and
left to intersectSurfaces/matchSurfaces exactly as it is today -- nothing
regresses for those edges, they simply do not get the pre-pairing benefit.
Nor does this touch roofed rooms at all: fc_roof.apply() runs after every
room here already has a `solid`, and unconditionally overwrites it for any
room it carves -- the precedence falls out of call order in
fc_export_floorplan.py, not from anything checked here. See that module's
main() for the exact sequencing.
"""

import uuid

# Heights within this of each other count as "the same" -- 0.1 mm, well
# below anything a drawing or a story property could distinguish.
HEIGHT_EQUAL_TOL_M = 1e-4


def _wall_quad(p0, p1, z0, z1):
    """A vertical wall face standing on the line from p0 to p1, from z0 up
    to z1 -- the same [x, y, z] triple shape build_osm_geometry.py's
    space_from_solid() already expects for every surface in a `solid`."""
    (x0, y0), (x1, y1) = p0, p1
    return [[x0, y0, z0], [x1, y1, z0], [x1, y1, z1], [x0, y0, z1]]


def room_solid(space_id, elevation_m, own_height_m, footprint_m, segments,
               heights_by_space, adjacency, match_ids):
    """The `solid` block for one ordinary (non-roofed) room. Pure data --
    no FreeCAD import, so this is unit-testable without it.

    footprint_m: this room's *simplified* plan polygon (space["vertices"])
      -- used for Floor/Ceiling only, so those stay exactly as clean as
      fromFloorPrint would have made them.
    segments: this room's *full* boundary,
      fcbridge.room_wall_segments(face, sketch) -- used for walls, so a
      T-junction can split independently of the simplified footprint.
    heights_by_space: {space id: effective height_m}, every room on this
      story -- needed to look up a neighbour's height by id.
    adjacency: fcbridge.wall_adjacencies(...)'s {mid_key: record}.
    match_ids: {mid_key: uuid}, threaded across every room on the sketch so
      an interior edge's two rooms mint exactly one id between them --
      whichever room is processed first mints it, the second reuses it via
      match_ids.setdefault. Never persisted -- a same-process correlation
      key only, exactly like fc_roof.mirror_ceiling's match_id.
    """
    top_m = elevation_m + own_height_m
    surfaces = []

    for key, p0, p1 in segments:
        record = adjacency.get(key)
        if record is None or record["kind"] != "interior":
            # Exterior, refused, or this room's own footprint moved since
            # adjacency was computed (should not happen, but building an
            # ordinary unpaired wall here is exactly today's behaviour,
            # never a new failure mode).
            surfaces.append({"type": "Wall",
                             "vertices": _wall_quad(p0, p1, elevation_m, top_m)})
            continue

        other_id = (record["space_b"] if record["space_a"] == space_id
                   else record["space_a"])
        other_h = heights_by_space.get(other_id, own_height_m)

        if abs(own_height_m - other_h) < HEIGHT_EQUAL_TOL_M:
            match_id = match_ids.setdefault(key, uuid.uuid4().hex)
            surfaces.append({"type": "Wall", "match_id": match_id,
                             "match_space_id": other_id,
                             "vertices": _wall_quad(p0, p1, elevation_m, top_m)})
            continue

        split_z = elevation_m + min(own_height_m, other_h)
        match_id = match_ids.setdefault(key, uuid.uuid4().hex)
        surfaces.append({"type": "Wall", "match_id": match_id,
                         "match_space_id": other_id,
                         "vertices": _wall_quad(p0, p1, elevation_m, split_z)})
        if own_height_m > other_h:
            # The piece exposed above the shorter neighbour: an ordinary,
            # unpaired wall on this room alone. Nothing here decides
            # whether it stays solid or becomes an air boundary.
            surfaces.append({"type": "Wall",
                             "vertices": _wall_quad(p0, p1, split_z, top_m)})

    surfaces.append({"type": "Floor",
                     "vertices": [[x, y, elevation_m]
                                 for x, y in reversed(footprint_m)]})
    surfaces.append({"type": "RoofCeiling",
                     "vertices": [[x, y, top_m] for x, y in footprint_m]})
    return {"source": "wall-network", "surfaces": surfaces}


def synthesize_story_solids(fcbridge, sketch, faces, face_space_id, spaces,
                            space_faces, elevation_m, story_height_m):
    """Orchestrator: FreeCAD-touching, thin. Mutates sp["solid"] in place
    for every space in `spaces`.

    `fcbridge` is passed in rather than imported here so this module stays
    importable (for room_solid's own tests) without FreeCAD on the path --
    fcbridge itself needs FreeCAD/Part/BOPTools at import time.

    Returns (interior, exterior, refused) counts for the export report;
    `refused` is a list of reason strings, one per refused edge, not just a
    count -- each one means a wall that falls back to
    intersectSurfaces/matchSurfaces exactly as it would have before this
    module existed, which is worth being able to name, not just tally.
    """
    heights_by_space = {sp["id"]: (sp.get("height_m") or story_height_m)
                        for sp in spaces}
    adjacency = fcbridge.wall_adjacencies(sketch, faces, face_space_id)
    match_ids = {}

    for sp in spaces:
        face = space_faces[sp["id"]]
        segments = fcbridge.room_wall_segments(face, sketch)
        own_h = heights_by_space[sp["id"]]
        sp["solid"] = room_solid(sp["id"], elevation_m, own_h,
                                 sp["vertices"], segments, heights_by_space,
                                 adjacency, match_ids)

    interior = sum(1 for r in adjacency.values() if r["kind"] == "interior")
    exterior = sum(1 for r in adjacency.values() if r["kind"] == "exterior")
    refused = [r["reason"] for r in adjacency.values()
              if r["kind"] == "refused"]
    return interior, exterior, refused
