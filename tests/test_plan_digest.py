# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the plan stamp that dates the imported geometry.

The OS_Geometry group in a document is a copy of the model, and an opening is
hosted by testing its outline against it.  Edit the plan, rebuild the model,
and that copy shows walls where they used to be -- so the export either finds
no host for an outline or, worse, finds a wall whose name has since been given
to a different piece of the building.

Measured: after one wall move the opening export failed with "no imported
surface is coplanar with and contains it", which is true and tells you nothing
about the cause.  The import now stamps the document with a fingerprint of the
plan it was taken from, and the export compares.

Timestamps cannot do this job -- the .osm is rewritten by the openings apply
itself, so the model is routinely newer than the import for reasons that move
no wall.  The plan is the thing to watch, because nothing else moves a wall.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))
sys.modules.setdefault("Part", types.ModuleType("Part"))
_bop = sys.modules.setdefault("BOPTools", types.ModuleType("BOPTools"))
if not hasattr(_bop, "SplitAPI"):
    _bop.SplitAPI = types.SimpleNamespace()

import fcbridge as fb  # noqa: E402


class Vertex(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class Edge(object):
    def __init__(self, a, b):
        self.Vertexes = [Vertex(*a), Vertex(*b)]


class Quantity(object):
    """Enough of a FreeCAD Quantity for story_elevation_m/story_height_m."""

    def __init__(self, mm):
        self.mm = float(mm)

    def getValueAs(self, unit):
        assert unit == "m"
        return self.mm / 1000.0


class Sketch(object):
    def __init__(self, edges, elevation_mm=0.0, height_mm=3000.0):
        self.Shape = types.SimpleNamespace(
            Edges=[Edge(a, b) for a, b in edges])
        self.OS_Elevation = Quantity(elevation_mm)
        self.OS_FloorToFloor = Quantity(height_mm)


def square(x=0.0, y=0.0, size=1000.0):
    a = (x, y, 0.0)
    b = (x + size, y, 0.0)
    c = (x + size, y + size, 0.0)
    d = (x, y + size, 0.0)
    return [(a, b), (b, c), (c, d), (d, a)]


class DigestTests(unittest.TestCase):

    def test_the_same_plan_gives_the_same_digest(self):
        self.assertEqual(fb.plan_digest([Sketch(square())]),
                         fb.plan_digest([Sketch(square())]))

    def test_order_does_not_matter(self):
        """Re-drawing an edge the other way round, or adding elements in a
        different order, is not a change to the building."""
        forward = square()
        backward = [(b, a) for a, b in reversed(forward)]
        self.assertEqual(fb.plan_digest([Sketch(forward)]),
                         fb.plan_digest([Sketch(backward)]))

    def test_a_moved_wall_changes_the_digest(self):
        moved = square()
        moved[1] = ((1000.0, 0.0, 0.0), (1000.0, 900.0, 0.0))
        self.assertNotEqual(fb.plan_digest([Sketch(square())]),
                            fb.plan_digest([Sketch(moved)]))

    def test_a_moved_story_changes_the_digest(self):
        """Points are global, so sliding a whole story counts."""
        self.assertNotEqual(fb.plan_digest([Sketch(square())]),
                            fb.plan_digest([Sketch(square(x=5000.0))]))

    def test_a_changed_storey_height_changes_the_digest(self):
        """Not geometry in the sketch, but it decides where the walls stop --
        the ×1000 units bug reached the model exactly this way."""
        self.assertNotEqual(
            fb.plan_digest([Sketch(square(), height_mm=3000.0)]),
            fb.plan_digest([Sketch(square(), height_mm=2710.0)]))

    def test_a_changed_elevation_changes_the_digest(self):
        self.assertNotEqual(
            fb.plan_digest([Sketch(square(), elevation_mm=0.0)]),
            fb.plan_digest([Sketch(square(), elevation_mm=3378.0)]))

    def test_microns_are_not_a_change(self):
        """A vertex that came back through JSON's six decimal places must not
        read as a moved wall."""
        nudged = [((0.0, 0.0, 0.0), (1000.0000001, 0.0, 0.0))]
        exact = [((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0))]
        self.assertEqual(fb.plan_digest([Sketch(exact)]),
                         fb.plan_digest([Sketch(nudged)]))

    def test_a_millimetre_is_a_change(self):
        near = [((0.0, 0.0, 0.0), (1001.0, 0.0, 0.0))]
        exact = [((0.0, 0.0, 0.0), (1000.0, 0.0, 0.0))]
        self.assertNotEqual(fb.plan_digest([Sketch(exact)]),
                            fb.plan_digest([Sketch(near)]))

    def test_two_stories_are_not_interchangeable(self):
        """Both stories go into one digest, so a change to either dates the
        import -- and swapping which is which is a change."""
        a = Sketch(square(), elevation_mm=0.0)
        b = Sketch(square(x=60000.0), elevation_mm=3378.0)
        swapped_b = Sketch(square(x=60000.0), elevation_mm=0.0)
        swapped_a = Sketch(square(), elevation_mm=3378.0)
        self.assertNotEqual(fb.plan_digest([a, b]),
                            fb.plan_digest([swapped_a, swapped_b]))

    def test_the_digest_is_short_and_printable(self):
        """It goes in a report line and a document property, so it has to be
        something a person can compare by eye."""
        digest = fb.plan_digest([Sketch(square())])
        self.assertEqual(len(digest), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))

    def test_an_empty_plan_still_digests(self):
        self.assertEqual(len(fb.plan_digest([])), 16)


class StampTests(unittest.TestCase):

    class Doc(object):
        def __init__(self):
            self._props = {}

        def addProperty(self, ptype, name, group, doc):
            self._props[name] = ""
            setattr(self, name, "")

    def test_a_document_with_no_stamp_reads_empty(self):
        self.assertEqual(fb.stored_plan_digest(self.Doc()), "")

    def test_stamping_then_reading_round_trips(self):
        doc, sketches = self.Doc(), [Sketch(square())]
        written = fb.stamp_plan_digest(doc, sketches)
        self.assertEqual(fb.stored_plan_digest(doc), written)
        self.assertEqual(written, fb.plan_digest(sketches))

    def test_re_stamping_overwrites(self):
        doc = self.Doc()
        fb.stamp_plan_digest(doc, [Sketch(square())])
        second = fb.stamp_plan_digest(doc, [Sketch(square(x=5000.0))])
        self.assertEqual(fb.stored_plan_digest(doc), second)


if __name__ == "__main__":
    unittest.main()
