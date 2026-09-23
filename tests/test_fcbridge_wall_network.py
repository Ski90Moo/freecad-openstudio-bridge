# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for pre-splitting a wall-centerline sketch's T-junctions and
keeping External Geometry out of the "official wall" role.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

find_t_junctions/t_junctions_on_sketch/external_wall_geometry/
wall_network_problems are pure or stub-driven and covered here in full.
split_t_junctions/promote_external_edges make real SketchObject.split()/
ExternalGeo/addExternal/delExternal/addConstraint calls and are verified
manually against real FreeCAD files instead (see the plan file and
normalize_walls.FCMacro) -- a stub for those would only test the stub.

fcbridge imports FreeCAD/Part/BOPTools at module scope and the venv has none
of them, so they are stubbed -- none of the functions under test touch them.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))

_part = sys.modules.setdefault("Part", types.ModuleType("Part"))
_bop = sys.modules.setdefault("BOPTools", types.ModuleType("BOPTools"))
if not hasattr(_bop, "SplitAPI"):
    _bop.SplitAPI = types.SimpleNamespace()

import fcbridge as fb  # noqa: E402


class Point(object):
    def __init__(self, x, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Vertex(object):
    def __init__(self, point):
        self.Point = point


class Edge(object):
    """A curve evaluated at its middle, plus the two endpoints of a line --
    what _touching_map/_on_tagged_edge need from a face's own boundary."""

    def __init__(self, mid, p0=None, p1=None):
        self.FirstParameter, self.LastParameter = 0.0, 2.0
        self._mid = mid
        self.Vertexes = [Vertex(p0 if p0 is not None else mid),
                         Vertex(p1 if p1 is not None else mid)]

    def valueAt(self, parameter):
        assert parameter == 1.0
        return self._mid


class _PointShape(object):
    def __init__(self, point):
        self.Vertexes = [Vertex(point)]


class ExternalPoint(object):
    """Stands in for a Vertex-type ExternalGeo entry (Part.Point) --
    promote_external_edges' own anchor references, or one added by hand.
    Its resolved shape has exactly one Vertex, unlike a LineSegment's two,
    which is how external_wall_geometry tells the two apart."""

    def __init__(self, point):
        self.point = point

    def toShape(self):
        return _PointShape(self.point)


class LineSegment(object):
    """Stands in for Part.LineSegment -- t_junctions_on_sketch keys off
    this exact class name (type(geometry).__name__), matching what a real
    FreeCAD sketch reports."""

    def __init__(self, p0, p1):
        self.p0, self.p1 = p0, p1

    def toShape(self):
        return Edge(self.p0, self.p0, self.p1)


class ArcOfCircle(object):
    """A non-line geometry element -- t_junctions_on_sketch must skip it
    without raising."""

    def __init__(self, p0, p1):
        self.p0, self.p1 = p0, p1

    def toShape(self):
        return Edge(self.p0, self.p0, self.p1)


class Facade(object):
    def __init__(self, construction=False):
        self.Construction = construction


class Placement(object):
    """Identity, so points come through unchanged."""

    def multVec(self, point):
        return point

    def inverse(self):
        return self


class Shape(object):
    def __init__(self, edges):
        self.Edges = edges


class Sketch(object):
    def __init__(self, geoms, constructions=None, ext_geoms=(),
                ext_already_promoted=(), ext_non_defining=(),
                label="Level 2"):
        self.Label = label
        geoms = list(geoms)
        flags = list(constructions) if constructions is not None else \
            [False] * len(geoms)
        # Index 0/1 are always the sketch's own H/V axis in a real sketch;
        # external_wall_geometry skips them without dereferencing, so a
        # placeholder is enough here.
        self.ExternalGeo = [None, None] + list(ext_geoms)
        # promote_external_edges' own signature: a real local edge whose
        # own endpoints coincide with the external one's -- what
        # _promoted_external_geo_ids actually looks for now (a geometric
        # match, not a Constraint scan: getConstruction proved unreliable
        # for a negative GeoId, measured directly against a real file, and
        # a Constraint scan stopped recognizing a promoted edge that a
        # later split_t_junctions() run then construction-demoted, also
        # measured directly).
        for geo_id in ext_already_promoted:
            ext_edge = self.ExternalGeo[-geo_id - 1]
            geoms.append(LineSegment(ext_edge.p0, ext_edge.p1))
            flags.append(False)
        self.Geometry = geoms
        self.GeometryFacadeList = [Facade(construction=c) for c in flags]
        self.Placement = Placement()

        # sketch.Shape.Edges: every non-construction local edge, plus every
        # External Geometry edge added as addExternal(..., defining=True)
        # -- everything in ext_geoms by default, since that is what the
        # existing tests below assume, except any geo_id named in
        # ext_non_defining, which stands in for FreeCAD's own default
        # (defining=False): a snapping/reference guide _defining_external_
        # geo_ids must never report as defining, whatever it is connected
        # to (see that function's own docstring for why Shape.Edges
        # membership, not any Construction-style flag, is the only signal
        # that actually distinguishes the two).
        shape_edges = []
        for geo, is_constr in zip(self.Geometry, flags):
            if is_constr:
                continue
            shape_edges.append(geo.toShape())
        for index, geometry in enumerate(self.ExternalGeo):
            if index < 2 or geometry is None:
                continue
            if -(index + 1) in ext_non_defining:
                continue
            shape = geometry.toShape()
            if len(shape.Vertexes) != 2:
                continue               # a point, never an edge
            shape_edges.append(shape)
        self.Shape = Shape(shape_edges)


class FindTJunctionsTests(unittest.TestCase):

    def test_no_nearby_edges_finds_nothing(self):
        edges = [("A", Point(0, 0), Point(4000, 0))]
        self.assertEqual(fb.find_t_junctions(edges), {})

    def test_a_proper_corner_is_not_a_t_junction(self):
        # B's endpoint lands exactly on A's own endpoint -- already a
        # shared vertex, nothing to split.
        edges = [("A", Point(0, 0), Point(4000, 0)),
                ("B", Point(4000, 0), Point(4000, 4000))]
        self.assertEqual(fb.find_t_junctions(edges), {})

    def test_one_interior_hit_is_found(self):
        edges = [("A", Point(0, 0), Point(4000, 0)),
                ("B", Point(2000, 0), Point(2000, 4000))]
        result = fb.find_t_junctions(edges)
        self.assertEqual(list(result.keys()), ["A"])
        self.assertEqual(len(result["A"]), 1)
        t, point, other_key, other_pos_id = result["A"][0]
        self.assertAlmostEqual(t, 0.5)
        self.assertEqual(point, (2000.0, 0.0, 0.0))
        # B's own p0 -- (2000, 0) -- is what landed on A, so PosId 1.
        self.assertEqual((other_key, other_pos_id), ("B", 1))
        # A's own endpoints sit 2 m off B's line -- no reverse hit on B.
        self.assertNotIn("B", result)

    def test_two_interior_hits_come_back_sorted_by_t(self):
        edges = [("A", Point(0, 0), Point(12000, 0)),
                ("B", Point(8000, 0), Point(8000, 4000)),
                ("C", Point(4000, 0), Point(4000, 4000))]
        result = fb.find_t_junctions(edges)
        ts = [t for t, _point, _ok, _op in result["A"]]
        self.assertEqual(ts, sorted(ts))
        self.assertAlmostEqual(ts[0], 4000.0 / 12000.0)
        self.assertAlmostEqual(ts[1], 8000.0 / 12000.0)
        causes = [(ok, op) for _t, _p, ok, op in result["A"]]
        self.assertEqual(causes, [("C", 1), ("B", 1)])

    def test_a_four_way_junction_dedupes_to_one_split_point(self):
        # B and C both land their endpoint at the same physical point on A.
        edges = [("A", Point(0, 0), Point(8000, 0)),
                ("B", Point(4000, 0), Point(4000, 4000)),
                ("C", Point(4000, 0), Point(4000, -4000))]
        result = fb.find_t_junctions(edges)
        self.assertEqual(len(result["A"]), 1)

    def test_a_point_near_the_edges_own_endpoint_is_excluded(self):
        # 0.5 mm from A's own end at x=4000 -- inside TAGGED_EDGE_TOL_MM's
        # own-endpoint exclusion band, not a T-junction.
        edges = [("A", Point(0, 0), Point(4000, 0)),
                ("B", Point(3999.5, 0), Point(3999.5, 4000))]
        self.assertEqual(fb.find_t_junctions(edges), {})

    def test_a_point_beyond_the_finite_span_is_excluded(self):
        edges = [("A", Point(0, 0), Point(4000, 0)),
                ("B", Point(6000, 0), Point(6000, 4000))]
        self.assertEqual(fb.find_t_junctions(edges), {})

    def test_a_point_off_the_line_is_excluded(self):
        # 5 mm perpendicular offset -- beyond TAGGED_EDGE_TOL_MM (1 mm).
        edges = [("A", Point(0, 0), Point(4000, 0)),
                ("B", Point(2000, 5), Point(2000, 10))]
        self.assertEqual(fb.find_t_junctions(edges), {})


class TJunctionsOnSketchTests(unittest.TestCase):

    def test_a_genuine_t_junction_is_found_through_the_sketch(self):
        geoms = [LineSegment(Point(0, 0), Point(4000, 0)),
                LineSegment(Point(2000, 0), Point(2000, 4000))]
        sketch = Sketch(geoms)
        result = fb.t_junctions_on_sketch(sketch)
        self.assertEqual(list(result.keys()), [0])
        self.assertEqual(len(result[0]), 1)

    def test_construction_geometry_is_skipped(self):
        # The crossing edge is construction -- excluded entirely, so the
        # wall it would have split against reports no junction at all.
        geoms = [LineSegment(Point(0, 0), Point(4000, 0)),
                LineSegment(Point(2000, 0), Point(2000, 4000))]
        sketch = Sketch(geoms, constructions=[False, True])
        self.assertEqual(fb.t_junctions_on_sketch(sketch), {})

    def test_non_line_geometry_is_skipped_without_raising(self):
        geoms = [LineSegment(Point(0, 0), Point(4000, 0)),
                ArcOfCircle(Point(2000, 0), Point(2000, 4000))]
        sketch = Sketch(geoms)
        self.assertEqual(fb.t_junctions_on_sketch(sketch), {})


class ExternalWallGeometryTests(unittest.TestCase):

    def test_axis_placeholders_are_skipped(self):
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        sketch = Sketch([], ext_geoms=[ext_edge])
        result = fb.external_wall_geometry(sketch)
        self.assertEqual([geo_id for geo_id, _geo in result], [-3])

    def test_a_vertex_type_entry_is_skipped(self):
        # A lone point can never be "an official wall" needing promotion
        # -- and without this filter, promote_external_edges' own newly
        # added Vertex anchor references would show up as candidates on
        # its very next run, confirmed directly.
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        ext_point = ExternalPoint(Point(5000, 5000))
        sketch = Sketch([], ext_geoms=[ext_edge, ext_point])
        result = fb.external_wall_geometry(sketch)
        self.assertEqual([geo_id for geo_id, _geo in result], [-3])

    def test_an_already_promoted_entry_is_skipped(self):
        """Regression: promote_external_edges pins a real local line to the
        external reference with a Coincident constraint, but the
        reference's own endpoints are still exactly coincident with that
        new line -- so a naive "does this entry's line touch a face
        fragment" check would flag it forever.  Caught by running the real
        mechanism against samples/FloorplanTest-02.FCStd: SecondFP
        Sketch's 19 promoted edges were still reported as problems after
        normalizing.  (getConstruction(geo_id) looked like the obvious
        fix and was tried first, but measured directly against the same
        file it returns True unconditionally for every external GeoId
        regardless of whether toggleConstruction has ever touched it --
        scanning Constraints for what promote_external_edges itself
        creates is what actually reflects the sketch's real state.)"""
        edge_a = LineSegment(Point(0, 0), Point(0, 4000))
        edge_b = LineSegment(Point(4000, 0), Point(4000, 4000))
        sketch = Sketch([], ext_geoms=[edge_a, edge_b],
                        ext_already_promoted=[-3])
        result = fb.external_wall_geometry(sketch)
        self.assertEqual([geo_id for geo_id, _geo in result], [-4])

    def test_a_promoted_edge_later_construction_demoted_is_still_skipped(self):
        """Regression: split_t_junctions() demotes a split original to
        construction without ever touching its topology, so a promoted
        edge that a *later* T-junction split runs against still has
        endpoints exactly matching the external reference that seeded it
        -- but only its Construction flag changed, not its position.
        Caught by running the real mechanism against
        samples/FloorplanTest-02.FCStd: 5 of SecondFP Sketch's 19 promoted
        edges were reported as problems again after their own subsequent
        split, because an earlier version of this check only matched
        against non-construction geometry."""
        geoms = [LineSegment(Point(0, 0), Point(0, 4000))]
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        sketch = Sketch(geoms, constructions=[True], ext_geoms=[ext_edge])
        result = fb.external_wall_geometry(sketch)
        self.assertEqual(result, [])

    def test_a_non_defining_entry_is_never_a_candidate(self):
        """A non-defining edge (addExternal's own default -- a deliberate
        snapping/reference guide, not a wall) never appears in
        sketch.Shape.Edges regardless of what it is connected to, so it is
        excluded here entirely, not merely left un-taggable. Root-caused
        against FloorplanTest-06's own Level 2: an un-promoted, non-defining
        reference stub sat mid-room, invisible to extract_room_faces either
        way -- promoting it would have manufactured a room-split risk
        (real, local, defining geometry Shape.Edges DOES see) that never
        existed for the reference itself."""
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        sketch = Sketch([], ext_geoms=[ext_edge], ext_non_defining=[-3])
        self.assertEqual(fb.external_wall_geometry(sketch), [])

    def test_a_defining_and_a_non_defining_entry_are_told_apart(self):
        defining = LineSegment(Point(0, 0), Point(0, 4000))
        non_defining = LineSegment(Point(4000, 0), Point(4000, 4000))
        sketch = Sketch([], ext_geoms=[defining, non_defining],
                        ext_non_defining=[-4])
        result = fb.external_wall_geometry(sketch)
        self.assertEqual([geo_id for geo_id, _geo in result], [-3])


class WallNetworkProblemsTests(unittest.TestCase):

    def test_a_clean_sketch_reports_nothing(self):
        geoms = [LineSegment(Point(0, 0), Point(4000, 0))]
        sketch = Sketch(geoms)
        self.assertEqual(fb.wall_network_problems(sketch), [])

    def test_a_t_junction_and_an_external_edge_are_both_reported(self):
        geoms = [LineSegment(Point(0, 0), Point(4000, 0)),
                LineSegment(Point(2000, 0), Point(2000, 4000))]
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        sketch = Sketch(geoms, ext_geoms=[ext_edge], label="Level 2")

        problems = fb.wall_network_problems(sketch)
        self.assertEqual(len(problems), 2)
        self.assertIn("T-junction", problems[0])
        self.assertIn("Level 2", problems[0])
        self.assertIn("External Geometry", problems[1])
        self.assertIn("Level 2", problems[1])

    def test_an_external_edge_is_reported_even_if_it_borders_no_room(self):
        # A defining edge is promoted unconditionally regardless of room
        # adjacency -- there is no "purely decorative, safe to leave
        # alone" exemption for it, since t_junctions_on_sketch can't see an
        # un-promoted edge either way. (A non-defining edge gets a
        # different exemption entirely -- see
        # test_a_non_defining_external_edge_is_never_reported below.)
        geoms = [LineSegment(Point(0, 0), Point(4000, 0))]
        ext_edge = LineSegment(Point(9000, 0), Point(9000, 4000))
        sketch = Sketch(geoms, ext_geoms=[ext_edge])
        problems = fb.wall_network_problems(sketch)
        self.assertEqual(len(problems), 1)
        self.assertIn("External Geometry", problems[0])

    def test_a_non_defining_external_edge_is_never_reported(self):
        # Unlike the defining case above: a non-defining edge is a
        # deliberate snapping/reference guide, not an official wall
        # normalize_walls.FCMacro needs to promote -- so it is never a
        # problem, whether or not it borders a room, whether or not
        # normalize_walls.FCMacro has ever run on this sketch.
        geoms = [LineSegment(Point(0, 0), Point(4000, 0))]
        ext_edge = LineSegment(Point(9000, 0), Point(9000, 4000))
        sketch = Sketch(geoms, ext_geoms=[ext_edge], ext_non_defining=[-3])
        self.assertEqual(fb.wall_network_problems(sketch), [])

    def test_an_already_promoted_external_edge_is_not_reported_again(self):
        # Same geometry as the reported-problem case above, but the
        # reference is already flagged construction -- promote_external_edges
        # already ran, so this must not fire again.
        geoms = [LineSegment(Point(0, 0), Point(4000, 0))]
        ext_edge = LineSegment(Point(0, 0), Point(0, 4000))
        sketch = Sketch(geoms, ext_geoms=[ext_edge],
                        ext_already_promoted=[-3])
        self.assertEqual(fb.wall_network_problems(sketch), [])


if __name__ == "__main__":
    unittest.main()
