# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for pre-splitting and pre-pairing an ordinary room's walls.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

room_solid() is pure data -- no FreeCAD, no openstudio -- so these tests
build its inputs (segments, adjacency, heights) by hand rather than through
fcbridge.wall_adjacencies/room_wall_segments, which are tested separately in
tests/test_fcbridge_wall_adjacencies.py against real FreeCAD stubs. What
matters here is the arithmetic: which segment gets a match_id, where the
height-mismatch split lands, and that Floor/Ceiling still come out exactly
as fromFloorPrint would have made them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fc_walls as fw  # noqa: E402

SQUARE = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]

# One segment, the shared east wall between two 4x4 rooms side by side.
SHARED_EDGE = ("k1", (4.0, 0.0), (4.0, 4.0))


def interior(space_a, space_b):
    return {"k1": {"kind": "interior", "space_a": space_a,
                  "space_b": space_b, "vertices": [[4.0, 0.0], [4.0, 4.0]]}}


class RoomSolidTests(unittest.TestCase):

    def test_equal_height_neighbor_gets_one_paired_wall(self):
        heights = {"left": 3.0, "right": 3.0}
        solid = fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                              heights, interior("left", "right"), {})

        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1)
        self.assertIn("match_id", walls[0])
        self.assertEqual(walls[0]["match_space_id"], "right")
        self.assertEqual(walls[0]["vertices"],
                         [[4.0, 0.0, 0.0], [4.0, 4.0, 0.0],
                          [4.0, 4.0, 3.0], [4.0, 0.0, 3.0]])

    def test_this_room_shorter_gets_one_wall_at_its_own_height(self):
        heights = {"left": 2.5, "right": 3.0}
        solid = fw.room_solid("left", 0.0, 2.5, SQUARE, [SHARED_EDGE],
                              heights, interior("left", "right"), {})

        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1)
        self.assertIn("match_id", walls[0])
        self.assertEqual(walls[0]["vertices"][2][2], 2.5)   # top z

    def test_this_room_taller_gets_a_paired_and_an_exposed_wall(self):
        heights = {"left": 4.0, "right": 3.0}
        solid = fw.room_solid("left", 0.0, 4.0, SQUARE, [SHARED_EDGE],
                              heights, interior("left", "right"), {})

        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 2)
        paired = [w for w in walls if "match_id" in w]
        exposed = [w for w in walls if "match_id" not in w]
        self.assertEqual(len(paired), 1)
        self.assertEqual(len(exposed), 1)
        # paired: 0.0 to 3.0 (the shorter neighbour's height)
        self.assertEqual(paired[0]["vertices"][0][2], 0.0)
        self.assertEqual(paired[0]["vertices"][2][2], 3.0)
        # exposed: 3.0 to 4.0, this room's own extra height, unpaired
        self.assertEqual(exposed[0]["vertices"][0][2], 3.0)
        self.assertEqual(exposed[0]["vertices"][2][2], 4.0)

    def test_exterior_edge_is_one_plain_unpaired_wall(self):
        adjacency = {"k1": {"kind": "exterior",
                            "vertices": [[4.0, 0.0], [4.0, 4.0]]}}
        solid = fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                              {"left": 3.0}, adjacency, {})

        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1)
        self.assertNotIn("match_id", walls[0])
        self.assertEqual(walls[0]["vertices"],
                         [[4.0, 0.0, 0.0], [4.0, 4.0, 0.0],
                          [4.0, 4.0, 3.0], [4.0, 0.0, 3.0]])

    def test_refused_edge_is_built_the_same_as_exterior(self):
        adjacency = {"k1": {"kind": "refused", "reason": "shared by 3 rooms",
                            "vertices": [[4.0, 0.0], [4.0, 4.0]]}}
        solid = fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                              {"left": 3.0}, adjacency, {})
        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1)
        self.assertNotIn("match_id", walls[0])

    def test_a_missing_adjacency_entry_falls_back_to_unpaired(self):
        solid = fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                              {"left": 3.0}, {}, {})
        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1)
        self.assertNotIn("match_id", walls[0])

    def test_two_rooms_sharing_a_match_ids_cache_get_the_same_id(self):
        match_ids = {}
        left = fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                             {"left": 3.0, "right": 3.0},
                             interior("left", "right"), match_ids)
        right = fw.room_solid("right", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                              {"left": 3.0, "right": 3.0},
                              interior("left", "right"), match_ids)

        left_id = [s for s in left["surfaces"]
                  if s["type"] == "Wall"][0]["match_id"]
        right_id = [s for s in right["surfaces"]
                   if s["type"] == "Wall"][0]["match_id"]
        self.assertEqual(left_id, right_id)
        self.assertEqual(len(match_ids), 1)

    def test_order_independence_right_processed_first(self):
        """Whichever room is processed first mints the id; the second must
        reuse it, not mint its own."""
        match_ids = {}
        fw.room_solid("right", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                     {"left": 3.0, "right": 3.0},
                     interior("left", "right"), match_ids)
        fw.room_solid("left", 0.0, 3.0, SQUARE, [SHARED_EDGE],
                     {"left": 3.0, "right": 3.0},
                     interior("left", "right"), match_ids)
        self.assertEqual(len(match_ids), 1)

    def test_floor_is_reversed_footprint_at_elevation(self):
        solid = fw.room_solid("left", 2.5, 3.0, SQUARE, [],
                              {"left": 3.0}, {}, {})
        floor = next(s for s in solid["surfaces"] if s["type"] == "Floor")
        self.assertEqual(floor["vertices"],
                         [[x, y, 2.5] for x, y in reversed(SQUARE)])

    def test_ceiling_is_footprint_at_top(self):
        solid = fw.room_solid("left", 2.5, 3.0, SQUARE, [],
                              {"left": 3.0}, {}, {})
        ceiling = next(s for s in solid["surfaces"]
                      if s["type"] == "RoofCeiling")
        self.assertEqual(ceiling["vertices"],
                         [[x, y, 5.5] for x, y in SQUARE])

    def test_source_is_wall_network(self):
        solid = fw.room_solid("left", 0.0, 3.0, SQUARE, [], {"left": 3.0},
                              {}, {})
        self.assertEqual(solid["source"], "wall-network")

    def test_height_equal_tolerance_boundary(self):
        # Queried from the taller room's own perspective, since only the
        # taller side ever grows a second, exposed piece -- the shorter
        # side always reports one wall regardless of how different heights
        # get.
        within = {"left": 3.0 + fw.HEIGHT_EQUAL_TOL_M / 2, "right": 3.0}
        solid = fw.room_solid("left", 0.0, within["left"], SQUARE,
                              [SHARED_EDGE], within,
                              interior("left", "right"), {})
        walls = [s for s in solid["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls), 1, "within tolerance must not split")

        over = {"left": 3.0 + fw.HEIGHT_EQUAL_TOL_M * 2, "right": 3.0}
        solid_over = fw.room_solid("left", 0.0, over["left"], SQUARE,
                                   [SHARED_EDGE], over,
                                   interior("left", "right"), {})
        walls_over = [s for s in solid_over["surfaces"] if s["type"] == "Wall"]
        self.assertEqual(len(walls_over), 2,
                         "past tolerance, the taller room must split")


if __name__ == "__main__":
    unittest.main()
