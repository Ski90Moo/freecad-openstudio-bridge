# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for resolving OS_AirBoundaryOverride tags on the floor-plan sketch.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

The tag lives on a wall-centerline edge, not a closed outline, so the thing
worth pinning down is different from the opening-tagging tests: an interior
edge must border exactly two rooms (planar-graph duality) for a declaration
to mean anything.  One face (the exterior boundary), the same face twice (a
dangling stub) and an unrecognised value are all refused rather than guessed
at.

A fourth case -- one real room and one SKIP-ed "Open to Below" face -- is not
a rejection at all: it is deferred to resolve_cross_story_overrides, which
matches the SKIP face's own footprint against a taller room on another
story.  That is the export-time version of what find_air_boundaries.py's
mezzanine-edge rule already does on the built model, and it is what lets the
sample's own rated wall (205 Mezzanine meeting the double-height Lobby) be
tagged at all -- the Lobby has no face on 205's own sketch to name.

fcbridge imports FreeCAD/Part/BOPTools at module scope and the venv has none
of them, so they are stubbed -- none of the functions under test touch them.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))


class StringExtension(object):
    """Stands in for Part.GeometryStringExtension, which minting creates."""

    def __init__(self):
        self.Name = ""
        self.Value = ""


_part = sys.modules.setdefault("Part", types.ModuleType("Part"))
_part.GeometryStringExtension = StringExtension
_bop = sys.modules.setdefault("BOPTools", types.ModuleType("BOPTools"))
if not hasattr(_bop, "SplitAPI"):
    _bop.SplitAPI = types.SimpleNamespace()

import fcbridge as fb  # noqa: E402

EXT = fb.AIR_BOUNDARY_EXT_NAME


class Point(object):
    def __init__(self, x, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Vertex(object):
    def __init__(self, point):
        self.Point = point


class Edge(object):
    """A curve evaluated at its middle, plus the two endpoints of a line."""

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


class Extension(object):
    def __init__(self, value):
        self.Value = value


class Facade(object):
    def __init__(self, tag=None, construction=False):
        self._tag = tag
        self.Construction = construction

    def hasExtensionOfName(self, name):
        return self._tag is not None and name == EXT

    def getExtensionOfName(self, name):
        return Extension(self._tag)


class Placement(object):
    """Identity, so midpoints (and polygon vertices) come through unchanged."""

    def multVec(self, point):
        return point

    def inverse(self):
        return self


class Sketch(object):
    def __init__(self, elements, label="Level 1"):
        self.Label = label
        self.Placement = Placement()
        self.Geometry = [Geometry(mid, p0, p1) for mid, _, p0, p1 in elements]
        self.GeometryFacadeList = [facade for _, facade, _p0, _p1 in elements]


def element(mid, tag=None, p0=None, p1=None, construction=False):
    return (mid, Facade(tag, construction), p0, p1)


class Wire(object):
    def __init__(self, edges, polygon=None):
        self.Edges = edges
        self.OrderedVertexes = [Vertex(p) for p in (polygon or [])]


class Face(object):
    def __init__(self, edges, polygon=None):
        self.OuterWire = Wire(edges, polygon)


# A closed 4 m x 4 m square, in millimetres (FreeCAD's internal unit), for
# faces whose own footprint face_polygon_m needs to read -- only the SKIP
# side of a deferred override actually exercises this.
SQUARE_MM = [Point(0, 0), Point(4000, 0), Point(4000, 4000), Point(0, 4000)]


class AirBoundaryOverridesTests(unittest.TestCase):

    def test_a_tagged_edge_between_two_rooms_resolves(self):
        # Geometry lives in FreeCAD's internal unit, millimetres -- a 4 m
        # wall segment from (0, 0) to (4000, 0) mm.
        mid = Point(2000, 0)
        p0, p1 = Point(0, 0), Point(4000, 0)
        sketch = Sketch([element(mid, "OPEN", p0, p1)])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-beta", face_b: "space-alpha"})

        self.assertEqual(deferred, [])
        self.assertEqual(problems, [])
        self.assertEqual(len(overrides), 1)
        entry = overrides[0]
        self.assertEqual(entry["kind"], "OPEN")
        self.assertEqual({entry["space_a"], entry["space_b"]},
                         {"space-alpha", "space-beta"})
        self.assertEqual(entry["vertices"], [[0.0, 0.0], [4.0, 0.0]])

    def test_an_edge_on_the_exterior_boundary_is_refused(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "OPEN", Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a], {face_a: "space-alpha"})

        self.assertEqual((overrides, deferred), ([], []))
        self.assertEqual(len(problems), 1)
        self.assertIn("exterior boundary", problems[0])

    def test_a_dangling_stub_borders_the_same_room_twice(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "OPEN", Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid), Edge(mid)])   # the stub's two sides
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a], {face_a: "space-alpha"})

        self.assertEqual((overrides, deferred), ([], []))
        self.assertEqual(len(problems), 1)
        self.assertIn("same room on both sides", problems[0])

    def test_solid_is_never_objected_to_only_open_is(self):
        """A SOLID declaration is "never an air boundary here" -- true
        regardless of how many rooms the edge borders, since no rule would
        ever turn a non-two-room edge into an air boundary anyway. Every
        refusal reason OPEN gets is silently accepted (resolves to
        nothing) for SOLID instead."""
        # Exterior boundary (one face only).
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "SOLID", Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a], {face_a: "space-alpha"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

        # Dangling stub (same room on both sides).
        sketch = Sketch([element(mid, "SOLID", Point(0, 0), Point(4, 0))])
        face_a = Face([Edge(mid), Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a], {face_a: "space-alpha"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

        # Unlabelled on both sides.
        sketch = Sketch([element(mid, "SOLID", Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b], {})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

        # Shared by more than two rooms.
        p0, p1 = Point(0, 0), Point(4000, 0)
        sketch = Sketch([element(Point(2000, 0), "SOLID", p0, p1)])
        edges = [Edge(Point(2000, 0)), Edge(Point(2000, 0)), Edge(Point(2000, 0))]
        face_a, face_b, face_c = Face([edges[0]]), Face([edges[1]]), Face([edges[2]])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b, face_c],
            {face_a: "space-alpha", face_b: "space-beta", face_c: "space-gamma"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

        # More than two rooms via pooling across multiple *clean* fragments
        # (no single fragment is ambiguous) -- a structurally different
        # code path from the single-fragment case above.
        p0, p1 = Point(0, 0), Point(8000, 0)
        sketch = Sketch([element(Point(4000, 0), "SOLID", p0, p1)])
        mid1, mid2 = Point(2000, 0), Point(6000, 0)
        face_alpha = Face([Edge(mid1)])
        face_beta = Face([Edge(mid1), Edge(mid2)])
        face_gamma = Face([Edge(mid2)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_alpha, face_beta, face_gamma],
            {face_alpha: "space-alpha", face_beta: "space-beta",
             face_gamma: "space-gamma"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

    def test_an_edge_bordering_a_skipped_region_is_deferred(self):
        """The sample's own rated wall is exactly this: 205 Mezzanine's
        edge borders "101 Open to Below", not a real room on that sketch --
        the room it really meets is a taller one on another story."""
        mid = Point(2000, 0)
        p0, p1 = Point(0, 0), Point(4000, 0)
        sketch = Sketch([element(mid, "SOLID", p0, p1)])
        face_a = Face([Edge(mid)], polygon=SQUARE_MM)
        face_b = Face([Edge(mid)])
        # face_a has no entry in face_space_id, the same as a SKIP-ed region.
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b], {face_b: "space-alpha"})

        self.assertEqual(overrides, [])
        self.assertEqual(problems, [])
        self.assertEqual(len(deferred), 1)
        entry = deferred[0]
        self.assertEqual(entry["kind"], "SOLID")
        self.assertEqual(entry["space_a"], "space-alpha")
        self.assertEqual(entry["vertices"], [[0.0, 0.0], [4.0, 0.0]])
        self.assertEqual(entry["skip_vertices"],
                         [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])

    def test_an_edge_bordering_only_unlabelled_faces_is_refused(self):
        """Both sides missing is not a mezzanine edge -- an unlabelled
        region is an export error the caller has already refused on, and
        pretending it is a SKIP void would resolve against nothing."""
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "OPEN", Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b], {})

        self.assertEqual((overrides, deferred), ([], []))
        self.assertEqual(len(problems), 1)
        self.assertIn("no labelled room on either side", problems[0])

    def test_an_unrecognized_value_is_reported_not_guessed(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "MAYBE", Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        self.assertEqual((overrides, deferred), ([], []))
        self.assertEqual(len(problems), 1)
        self.assertIn("MAYBE", problems[0])
        self.assertIn("SOLID", problems[0])
        self.assertIn("OPEN", problems[0])

    def test_no_tags_at_all_is_a_silent_no_op(self):
        mid = Point(2, 0)
        sketch = Sketch([element(mid, None, Point(0, 0), Point(4, 0))])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))

    def test_construction_geometry_is_never_tagged(self):
        """The tag lives on GeometryFacadeList, which construction geometry
        never reaches via Shape -- consistent with every other reader in
        fcbridge (element_values already skips it building the lookup)."""
        mid = Point(2, 0)
        sketch = Sketch([element(mid, "OPEN", Point(0, 0), Point(4, 0),
                                 construction=True)])
        face_a, face_b = Face([Edge(mid)]), Face([Edge(mid)])
        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})
        self.assertEqual((overrides, deferred, problems), ([], [], []))


class MultiFragmentTaggedEdgeTests(unittest.TestCase):
    """A wall long enough to cross a T-junction has its face boundary split
    into more than one fragment -- none of which necessarily shares the
    tagged edge's own midpoint.  This is the gap _on_tagged_edge/
    _classify_fragment exist to close: aggregate per-fragment
    classifications along the whole tagged run, rather than requiring one
    fragment's midpoint to equal the whole edge's midpoint exactly.
    """

    def test_a_tagged_edge_split_by_t_junctions_resolves_as_one_pair(self):
        # A 12 m run from (0,0) to (12000,0) mm, crossed by two T-junctions
        # at x=4000 and x=8000 -- three face-boundary fragments, none of
        # which is the whole edge's own midpoint (6000, 0) by itself... it
        # happens to be one of the three here, but the other two are not,
        # and all three must count.
        p0, p1 = Point(0, 0), Point(12000, 0)
        sketch = Sketch([element(Point(6000, 0), "SOLID", p0, p1)])
        frag_mids = [Point(2000, 0), Point(6000, 0), Point(10000, 0)]
        face_a = Face([Edge(m) for m in frag_mids])
        face_b = Face([Edge(m) for m in frag_mids])

        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        self.assertEqual(problems, [])
        self.assertEqual(deferred, [])
        self.assertEqual(len(overrides), 1)
        entry = overrides[0]
        self.assertEqual(entry["kind"], "SOLID")
        self.assertEqual({entry["space_a"], entry["space_b"]},
                         {"space-alpha", "space-beta"})
        self.assertEqual(entry["vertices"], [[0.0, 0.0], [12.0, 0.0]])

    def test_a_dangling_fragment_refuses_the_whole_run_even_if_others_are_clean(self):
        # Fragment at x=2000 is clean (alpha/beta); fragment at x=6000 is a
        # dangling stub -- face_a touches it on both sides.  One bad
        # fragment anywhere along the tagged run refuses the whole tag,
        # rather than silently resolving from the clean fragment alone.
        p0, p1 = Point(0, 0), Point(8000, 0)
        sketch = Sketch([element(Point(4000, 0), "OPEN", p0, p1)])
        clean_mid, stub_mid = Point(2000, 0), Point(6000, 0)
        face_a = Face([Edge(clean_mid), Edge(stub_mid), Edge(stub_mid)])
        face_b = Face([Edge(clean_mid)])

        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_b],
            {face_a: "space-alpha", face_b: "space-beta"})

        self.assertEqual((overrides, deferred), ([], []))
        self.assertEqual(len(problems), 1)
        self.assertIn("same room on both sides", problems[0])

    def test_a_multi_fragment_skip_edge_still_defers(self):
        # A mezzanine guardrail against an "Open to Below" void, split into
        # two fragments by a T-junction -- both fragments name the same
        # real room (space-alpha) against the same unlabelled SKIP face, so
        # this pools to exactly one room and defers, same as a
        # single-fragment SKIP edge would.
        p0, p1 = Point(0, 0), Point(8000, 0)
        sketch = Sketch([element(Point(4000, 0), "SOLID", p0, p1)])
        mid1, mid2 = Point(2000, 0), Point(6000, 0)
        face_a = Face([Edge(mid1), Edge(mid2)])
        face_skip = Face([Edge(mid1), Edge(mid2)], polygon=SQUARE_MM)

        overrides, deferred, problems = fb.air_boundary_overrides(
            sketch, [face_a, face_skip], {face_a: "space-alpha"})

        self.assertEqual(overrides, [])
        self.assertEqual(problems, [])
        self.assertEqual(len(deferred), 1)
        entry = deferred[0]
        self.assertEqual(entry["space_a"], "space-alpha")
        self.assertEqual(entry["skip_vertices"],
                         [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])


# fcbridge used to carry shape_edge_to_geometry_index -- an Edge21-ordinal
# -> Geometry-index mapping via Shape.Edges, on the assumption that a GUI
# selection made *while editing* a sketch names an edge the same way
# Shape.Edges (which excludes construction geometry) does. It doesn't:
# confirmed directly against the Sketcher Elements panel (which prints a
# row's raw GeoId alongside its Edge name, e.g. "Edge59#ID58" -- always
# exactly one apart, construction geometry included in the count same as
# real). Shape.Edges is what a *closed* sketch's pickable edges resolve
# against in the 3D view; it was never the right lookup for an edit-mode
# selection. tag_airboundary.FCMacro and tag_opening.FCMacro (the only
# callers, both edit-mode-only per their own header comments) now use the
# raw GeoId directly and no longer need this function at all -- removed
# rather than kept around unused, consistent with this project's own
# preference for a clean cutover over a parallel path.


def story(name, floor_to_floor_m, spaces):
    return {"name": name, "floor_to_floor_m": floor_to_floor_m,
            "spaces": spaces}


def space(space_id, name, vertices, height_m=None):
    out = {"id": space_id, "name": name, "vertices": vertices}
    if height_m is not None:
        out["height_m"] = height_m
    return out


class ResolveCrossStoryOverridesTests(unittest.TestCase):
    """The SKIP side of a deferred override, matched against whichever
    other story's room reaches up through it -- pure data, no FreeCAD."""

    SQUARE = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]

    def deferred_entry(self, kind="SOLID", space_a="mezz-id"):
        return {"kind": kind, "space_a": space_a,
                "skip_vertices": self.SQUARE, "vertices": [[0.0, 0.0],
                                                           [4.0, 0.0]]}

    def test_a_tall_room_below_resolves_the_skip_void(self):
        stories = [
            story("Level 1", 3.0, [
                space("lobby-id", "Lobby", self.SQUARE, height_m=6.0)]),
            story("Level 2", 3.0, [
                space("mezz-id", "205 Mezzanine",
                     [[4.0, 0.0], [8.0, 0.0], [8.0, 4.0], [4.0, 4.0]])]),
        ]
        resolved, problems = fb.resolve_cross_story_overrides(
            stories, [("Level 2", self.deferred_entry())])

        self.assertEqual(problems, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0], {
            "kind": "SOLID", "space_a": "mezz-id", "space_b": "lobby-id",
            "vertices": [[0.0, 0.0], [4.0, 0.0]],
        })

    def test_a_room_at_its_own_story_height_does_not_reach_through(self):
        """height_m absent (or equal to the story height) means an ordinary
        room, not one tall enough to stand in for the SKIP void."""
        stories = [
            story("Level 1", 3.0, [space("lobby-id", "Lobby", self.SQUARE)]),
            story("Level 2", 3.0, [
                space("mezz-id", "205 Mezzanine",
                     [[4.0, 0.0], [8.0, 0.0], [8.0, 4.0], [4.0, 4.0]])]),
        ]
        resolved, problems = fb.resolve_cross_story_overrides(
            stories, [("Level 2", self.deferred_entry())])

        self.assertEqual(resolved, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("no matching tall room", problems[0])

    def test_a_differently_shaped_room_is_not_matched(self):
        stories = [
            story("Level 1", 3.0, [
                space("lobby-id", "Lobby",
                     [[0.0, 0.0], [3.0, 0.0], [3.0, 4.0], [0.0, 4.0]],
                     height_m=6.0)]),
        ]
        resolved, problems = fb.resolve_cross_story_overrides(
            stories, [("Level 2", self.deferred_entry())])

        self.assertEqual(resolved, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("no matching tall room", problems[0])

    def test_two_tall_rooms_with_the_same_footprint_are_ambiguous(self):
        stories = [
            story("Level 1", 3.0, [
                space("lobby-id", "Lobby", self.SQUARE, height_m=6.0)]),
            story("Level 0", 3.0, [
                space("basement-id", "Basement Void", self.SQUARE,
                     height_m=9.0)]),
        ]
        resolved, problems = fb.resolve_cross_story_overrides(
            stories, [("Level 2", self.deferred_entry())])

        self.assertEqual(resolved, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("more than one tall room", problems[0])

    def test_the_footprint_match_ignores_winding_and_start_point(self):
        """The same square, traced from a different corner and the other
        way around -- shaft_pairs and cross_story_matches already rely on
        this, so the override resolver has to agree with them."""
        rotated = [[4.0, 4.0], [0.0, 4.0], [0.0, 0.0], [4.0, 0.0]]
        stories = [
            story("Level 1", 3.0, [
                space("lobby-id", "Lobby", rotated, height_m=6.0)]),
        ]
        resolved, _problems = fb.resolve_cross_story_overrides(
            stories, [("Level 2", self.deferred_entry())])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["space_b"], "lobby-id")


if __name__ == "__main__":
    unittest.main()
