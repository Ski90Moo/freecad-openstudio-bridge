# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the per-outline type override in fc_export_openings.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

The shape classifier decides per outline; the sketch Label overrides a whole
sketch.  Nothing in between existed, so three office entrances measured 1.69 m
against a 1.70 m threshold and came back as `Door`.

A geometry layer was tried first and was only a partial answer -- FreeCAD 1.1
offers two layers, so one override kind per sketch, and a facade wanting a
glass door *and* an overhead door *and* an operable window had nowhere to go.
The tag is a Part::GeometryStringExtension named OS_SubSurfaceType, riding on
the geometry itself: no limit, and measured to survive a save, a drag, a new
constraint, and the index shift when other geometry is deleted.

What is worth pinning down: an unreadable tag must be reported rather than
ignored, since silently dropping one puts the wrong subsurface type in the
model; and one outline carrying two different types must be refused, because
there is no honest answer for it.

fc_export_openings imports FreeCAD at module scope and the venv has no
FreeCAD, so it is stubbed -- none of the functions under test touch it.
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

import fc_export_openings as fo  # noqa: E402


class Point(object):
    def __init__(self, x, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Edge(object):
    """Just enough for _edge_mid: a curve evaluated at its middle."""

    def __init__(self, mid):
        self.FirstParameter, self.LastParameter = 0.0, 2.0
        self._mid = mid

    def valueAt(self, parameter):
        assert parameter == 1.0
        return self._mid


class Geometry(object):
    def __init__(self, mid):
        self._edge = Edge(mid)

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
        return self._tag is not None and name == fo.ELEMENT_KIND_EXT

    def getExtensionOfName(self, name):
        return Extension(self._tag)


class Placement(object):
    """Identity, so midpoints come through unchanged."""

    def multVec(self, point):
        return point


class Sketch(object):
    def __init__(self, elements, label="Openings South"):
        self.Label = label
        self.Placement = Placement()
        self.Geometry = [Geometry(mid) for mid, _ in elements]
        self.GeometryFacadeList = [facade for _, facade in elements]


class Wire(object):
    def __init__(self, mids):
        self.Edges = [Edge(m) for m in mids]


def rect(x, tag, construction=False):
    """Four elements at x, x+1, x+2, x+3, all carrying the same tag."""
    return [(Point(x + n), Facade(tag, construction)) for n in range(4)]


def wire_at(x):
    return Wire([Point(x + n) for n in range(4)])


class ElementKindsTests(unittest.TestCase):

    def test_a_tagged_rectangle(self):
        lookup, problems = fo.element_kinds(Sketch(rect(0, "GlassDoor")))
        self.assertEqual(problems, [])
        self.assertEqual(set(lookup.values()), {"GlassDoor"})
        self.assertEqual(len(lookup), 4)

    def test_the_keyword_spelling_works_too(self):
        lookup, _ = fo.element_kinds(Sketch(rect(0, "GLASSDOOR")))
        self.assertEqual(set(lookup.values()), {"GlassDoor"})

    def test_case_and_spacing_are_forgiven(self):
        lookup, _ = fo.element_kinds(Sketch(rect(0, "  glassdoor ")))
        self.assertEqual(set(lookup.values()), {"GlassDoor"})

    def test_every_type_is_reachable(self):
        for kind in ("FixedWindow", "OperableWindow", "Door", "GlassDoor",
                     "OverheadDoor", "Skylight"):
            lookup, problems = fo.element_kinds(Sketch(rect(0, kind)))
            self.assertEqual(set(lookup.values()), {kind}, kind)
            self.assertEqual(problems, [], kind)

    def test_the_whole_point_three_types_in_one_sketch(self):
        """What the layer mechanism could not do."""
        sketch = Sketch(rect(0, "GlassDoor") + rect(10, "OverheadDoor")
                        + rect(20, "OperableWindow") + rect(30, None))
        lookup, problems = fo.element_kinds(sketch)
        self.assertEqual(problems, [])
        self.assertEqual(
            [fo.wire_kind(wire_at(x), lookup)[0] for x in (0, 10, 20, 30)],
            ["GlassDoor", "OverheadDoor", "OperableWindow", None])

    def test_untagged_elements_are_absent(self):
        lookup, _ = fo.element_kinds(Sketch(rect(0, None)))
        self.assertEqual(lookup, {})

    def test_an_empty_tag_is_not_an_override(self):
        lookup, problems = fo.element_kinds(Sketch(rect(0, "   ")))
        self.assertEqual((lookup, problems), ({}, []))

    def test_construction_geometry_is_never_read(self):
        sketch = Sketch(rect(0, "GlassDoor", construction=True))
        self.assertEqual(fo.element_kinds(sketch)[0], {})

    def test_an_unknown_type_is_reported_with_the_choices(self):
        lookup, problems = fo.element_kinds(Sketch(rect(0, "PatioDoor")))
        self.assertEqual(lookup, {})
        self.assertEqual(len(problems), 4)
        self.assertIn("PatioDoor", problems[0])
        self.assertIn("GlassDoor", problems[0])

    def test_a_bad_tag_does_not_lose_a_good_one(self):
        sketch = Sketch(rect(0, "GlassDoor") + rect(10, "Nonsense"))
        lookup, problems = fo.element_kinds(sketch)
        self.assertEqual(set(lookup.values()), {"GlassDoor"})
        self.assertEqual(len(problems), 4)


class WireKindTests(unittest.TestCase):

    def setUp(self):
        sketch = Sketch(rect(0, None) + rect(10, "GlassDoor")
                        + rect(20, "OverheadDoor"))
        self.lookup = fo.element_kinds(sketch)[0]

    def test_an_untagged_outline_falls_through(self):
        self.assertEqual(fo.wire_kind(wire_at(0), self.lookup), (None, None))

    def test_a_tagged_outline(self):
        self.assertEqual(fo.wire_kind(wire_at(10), self.lookup),
                         ("GlassDoor", None))

    def test_a_second_outline_keeps_its_own_type(self):
        self.assertEqual(fo.wire_kind(wire_at(20), self.lookup),
                         ("OverheadDoor", None))

    def test_one_outline_with_two_types_is_refused(self):
        wire = Wire([Point(10), Point(11), Point(20), Point(21)])
        kind, conflicting = fo.wire_kind(wire, self.lookup)
        self.assertIsNone(kind)
        self.assertEqual(conflicting, ["GlassDoor", "OverheadDoor"])

    def test_a_partly_tagged_outline_is_not_a_conflict(self):
        """Tagging one edge of a rectangle is a person tagging the rectangle."""
        wire = Wire([Point(10), Point(999), Point(998), Point(997)])
        self.assertEqual(fo.wire_kind(wire, self.lookup), ("GlassDoor", None))

    def test_rounding_absorbs_float_noise(self):
        wire = Wire([Point(10.000000001)])
        self.assertEqual(fo.wire_kind(wire, self.lookup), ("GlassDoor", None))

    def test_a_shifted_midpoint_does_not_match(self):
        """The join has to be tight enough that a different edge misses."""
        wire = Wire([Point(10.5)])
        self.assertEqual(fo.wire_kind(wire, self.lookup), (None, None))


class ClassifyShapeTests(unittest.TestCase):
    """The thresholds the override exists to overrule."""

    def test_a_tall_opening_at_the_floor_is_an_overhead_door(self):
        self.assertEqual(fo.classify_shape(3.65, 4.24, 0.0)[0], "OverheadDoor")

    def test_height_beats_width(self):
        """A wide *and* tall opening is a roll-up, not a storefront."""
        self.assertEqual(fo.classify_shape(4.35, 3.20, 0.0)[0], "OverheadDoor")

    def test_a_wide_opening_at_the_floor_is_a_glass_door(self):
        self.assertEqual(fo.classify_shape(4.35, 2.73, 0.0)[0], "GlassDoor")

    def test_the_glass_door_threshold_is_inclusive(self):
        self.assertEqual(fo.classify_shape(1.70, 2.73, 0.0)[0], "GlassDoor")

    def test_the_case_that_prompted_the_override(self):
        """1.69 m is 10 mm short, and no shape rule can know better."""
        self.assertEqual(fo.classify_shape(1.69, 2.73, 0.0)[0], "Door")

    def test_a_raised_sill_is_a_window(self):
        self.assertEqual(fo.classify_shape(1.20, 1.50, 0.93)[0], "FixedWindow")

    def test_sill_is_measured_from_the_hosts_floor(self):
        """Same opening, second storey: still a door, not a window."""
        self.assertEqual(fo.classify_shape(1.02, 2.18, 0.0)[0], "Door")

    def test_the_ambiguous_sill_band_is_refused(self):
        kind, why = fo.classify_shape(1.20, 1.50, 0.30)
        self.assertIsNone(kind)
        self.assertIn("neither a door", why)

    def test_something_too_small_is_refused(self):
        kind, why = fo.classify_shape(0.20, 0.20, 0.0)
        self.assertIsNone(kind)
        self.assertIn("0.20 x 0.20", why)

    def test_only_the_classifier_cannot_reach_these(self):
        """No shape produces them, which is why a tag is the only route."""
        reachable = {fo.classify_shape(w, h, s)[0]
                     for w in (0.6, 1.2, 1.69, 1.7, 4.35)
                     for h in (0.6, 1.5, 2.73, 4.24)
                     for s in (0.0, 0.93, 2.0)}
        self.assertNotIn("OperableWindow", reachable)
        self.assertNotIn("Skylight", reachable)


if __name__ == "__main__":
    unittest.main()
