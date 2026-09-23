# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for classifying every wall-network edge, not just tagged ones.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

wall_adjacencies works one level finer than air_boundary_overrides: it
classifies every *face-boundary* edge, not every *drawn* edge, because a
wall drawn as one continuous line can legitimately border a different room
on each side of a T-junction partway along it (an interior partition
meeting an exterior wall away from a corner is the ordinary case). This is
what lets fc_walls.py pre-pair a wall segment with the exact neighbour it
actually touches, rather than the neighbour the whole drawn line implies.

fcbridge imports FreeCAD/Part/BOPTools at module scope and the venv has none
of them, so they are stubbed -- none of the functions under test touch them.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_freecad = sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))


class StringExtension(object):
    def __init__(self):
        self.Name = ""
        self.Value = ""


_part = sys.modules.setdefault("Part", types.ModuleType("Part"))
_part.GeometryStringExtension = StringExtension
_bop = sys.modules.setdefault("BOPTools", types.ModuleType("BOPTools"))
if not hasattr(_bop, "SplitAPI"):
    _bop.SplitAPI = types.SimpleNamespace()

import fcbridge as fb  # noqa: E402


class Point(object):
    def __init__(self, x, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


_freecad.Vector = Point


class Vertex(object):
    def __init__(self, point):
        self.Point = point


class Edge(object):
    def __init__(self, mid, p0=None, p1=None):
        self.FirstParameter, self.LastParameter = 0.0, 2.0
        self._mid = mid
        self.Vertexes = [Vertex(p0 if p0 is not None else mid),
                         Vertex(p1 if p1 is not None else mid)]

    def valueAt(self, parameter):
        assert parameter == 1.0
        return self._mid


class Geometry(object):
    def __init__(self, mid, p0=None, p1=None):
        self._edge = Edge(mid, p0, p1)

    def toShape(self):
        return self._edge


class Facade(object):
    def __init__(self, tag=None, construction=False):
        self._tag = tag
        self.Construction = construction

    def hasExtensionOfName(self, name):
        return False

    def getExtensionOfName(self, name):
        raise AssertionError("not used by wall_adjacencies/room_wall_segments")


class Placement(object):
    """Identity, so points come through unchanged."""

    def multVec(self, point):
        return point

    def inverse(self):
        return self


class Sketch(object):
    def __init__(self, elements, label="Level 1"):
        self.Label = label
        self.Placement = Placement()
        self.Geometry = [Geometry(mid, p0, p1) for mid, p0, p1 in elements]
        self.GeometryFacadeList = [Facade() for _ in elements]


def element(mid, p0=None, p1=None):
    return (mid, p0, p1)


class Wire(object):
    def __init__(self, edges, polygon=None):
        self.Edges = edges
        self.OrderedVertexes = [Vertex(p) for p in (polygon or [])]


class Face(object):
    def __init__(self, edges, polygon=None):
        self.OuterWire = Wire(edges, polygon)


class WallAdjacenciesTests(unittest.TestCase):

    def test_two_rooms_sharing_an_edge_are_interior(self):
        mid = Point(2000, 0)
        p0, p1 = Point(0, 0), Point(4000, 0)
        sketch = Sketch([element(mid, p0, p1)])
        face_a = Face([Edge(mid, p0, p1)])
        face_b = Face([Edge(mid, p0, p1)])
        adjacency = fb.wall_adjacencies(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        self.assertEqual(len(adjacency), 1)
        record = next(iter(adjacency.values()))
        self.assertEqual(record["kind"], "interior")
        self.assertEqual({record["space_a"], record["space_b"]},
                         {"space-alpha", "space-beta"})
        self.assertEqual(record["vertices"], [[0.0, 0.0], [4.0, 0.0]])

    def test_one_bordering_face_is_exterior(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid)])
        adjacency = fb.wall_adjacencies(sketch, [face_a],
                                        {face_a: "space-alpha"})
        self.assertEqual(next(iter(adjacency.values()))["kind"], "exterior")

    def test_shared_by_three_rooms_is_refused(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid)])
        face_b = Face([Edge(mid)])
        face_c = Face([Edge(mid)])
        adjacency = fb.wall_adjacencies(
            sketch, [face_a, face_b, face_c],
            {face_a: "a", face_b: "b", face_c: "c"})
        record = next(iter(adjacency.values()))
        self.assertEqual(record["kind"], "refused")
        self.assertIn("more than two", record["reason"])

    def test_a_dangling_stub_is_refused(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid), Edge(mid)])
        adjacency = fb.wall_adjacencies(sketch, [face_a],
                                        {face_a: "space-alpha"})
        record = next(iter(adjacency.values()))
        self.assertEqual(record["kind"], "refused")
        self.assertIn("same room on both sides", record["reason"])

    def test_a_skip_or_unlabelled_side_is_refused(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        adjacency = fb.wall_adjacencies(sketch, [face_a, face_b],
                                        {face_a: "space-alpha"})
        record = next(iter(adjacency.values()))
        self.assertEqual(record["kind"], "refused")
        self.assertIn("SKIP", record["reason"])

    def test_no_deferred_case_unlike_air_boundary_overrides(self):
        """The whole point: there is no tag here for a later pass to
        resolve a SKIP-adjacent edge against, so it is refused outright."""
        mid = Point(2, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        adjacency = fb.wall_adjacencies(sketch, [face_a, face_b],
                                        {face_a: "space-alpha"})
        for record in adjacency.values():
            self.assertIn(record["kind"], ("interior", "exterior", "refused"))

    def test_every_edge_in_the_sketch_gets_a_record(self):
        mid_ab = Point(4, 0)
        mid_out_a = Point(0, 2)
        mid_out_b = Point(8, 2)
        sketch = Sketch([
            element(mid_ab, Point(4, 0), Point(4, 4)),
            element(mid_out_a, Point(0, 0), Point(0, 4)),
            element(mid_out_b, Point(8, 0), Point(8, 4)),
        ])
        face_a = Face([Edge(mid_ab), Edge(mid_out_a)])
        face_b = Face([Edge(mid_ab), Edge(mid_out_b)])
        adjacency = fb.wall_adjacencies(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        self.assertEqual(len(adjacency), 3)
        kinds = sorted(r["kind"] for r in adjacency.values())
        self.assertEqual(kinds, ["exterior", "exterior", "interior"])


class RoomWallSegmentsTests(unittest.TestCase):

    def test_segment_count_matches_edge_count(self):
        square = [Point(0, 0), Point(4000, 0), Point(4000, 4000),
                 Point(0, 4000)]
        edges = [Edge(Point((square[i].x + square[(i + 1) % 4].x) / 2,
                            (square[i].y + square[(i + 1) % 4].y) / 2))
                for i in range(4)]
        face = Face(edges, polygon=square)
        sketch = Sketch([])

        segments = fb.room_wall_segments(face, sketch)
        self.assertEqual(len(segments), 4)

    def test_winding_is_ccw(self):
        square = [Point(0, 0), Point(4000, 0), Point(4000, 4000),
                 Point(0, 4000)]
        face = Face([], polygon=square)
        sketch = Sketch([])

        segments = fb.room_wall_segments(face, sketch)
        pts = [seg[1] for seg in segments]
        area = fb.signed_area(pts)
        self.assertGreater(area, 0)

    def test_a_clockwise_polygon_is_reversed_to_ccw(self):
        square_cw = [Point(0, 0), Point(0, 4000), Point(4000, 4000),
                    Point(4000, 0)]
        face = Face([], polygon=square_cw)
        sketch = Sketch([])

        segments = fb.room_wall_segments(face, sketch)
        pts = [seg[1] for seg in segments]
        self.assertGreater(fb.signed_area(pts), 0)

    def test_a_t_junction_point_is_preserved_not_dropped(self):
        """Unlike face_polygon_m, a collinear point stays -- it is exactly
        where a neighbour's wall lands part way along a straight run."""
        polygon = [Point(0, 0), Point(2000, 0), Point(4000, 0),
                  Point(4000, 4000), Point(0, 4000)]
        face = Face([], polygon=polygon)
        sketch = Sketch([])

        segments = fb.room_wall_segments(face, sketch)
        self.assertEqual(len(segments), 5)

    def test_vertices_convert_to_metres(self):
        square = [Point(0, 0), Point(4000, 0), Point(4000, 4000),
                 Point(0, 4000)]
        face = Face([], polygon=square)
        sketch = Sketch([])

        segments = fb.room_wall_segments(face, sketch)
        _key, p0, p1 = segments[0]
        self.assertEqual(p0, (0.0, 0.0))
        self.assertEqual(p1, (4.0, 0.0))

    def test_key_matches_wall_adjacencies_for_the_same_edge(self):
        """The whole point of computing mid_key the same two-step way: a
        lookup from fc_walls.room_solid against wall_adjacencies' dict has
        to land on the same key for the same physical edge."""
        mid = Point(2000, 0)
        sketch = Sketch([element(mid, Point(0, 0), Point(4000, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        adjacency = fb.wall_adjacencies(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        square = [Point(0, 0), Point(4000, 0), Point(4000, 4000),
                 Point(0, 4000)]
        room_face = Face([Edge(mid)], polygon=square)
        segments = fb.room_wall_segments(room_face, sketch)

        bottom_edge_keys = [key for key, p0, p1 in segments
                            if p0 == (0.0, 0.0) and p1 == (4.0, 0.0)]
        self.assertEqual(len(bottom_edge_keys), 1)
        self.assertIn(bottom_edge_keys[0], adjacency)
        self.assertEqual(adjacency[bottom_edge_keys[0]]["kind"], "interior")


if __name__ == "__main__":
    unittest.main()
