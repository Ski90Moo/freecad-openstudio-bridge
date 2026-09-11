# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the story and room length properties.

Run under either interpreter:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

These were App::PropertyFloat, named OS_Elevation_m / OS_FloorToFloor_m /
OS_Height_m, and read as metres because the name said so.  That worked for as
long as the numbers were typed in by hand.  Bind one to an expression -- a
spreadsheet of building constants, which is a thoroughly reasonable thing to
want -- and FreeCAD hands the float the value in its own internal unit, which
is millimetres.  A storey entered as 8ft 10-11/16in arrives as 2709.86 and is
read as 2709.86 metres.

Nothing catches it.  The document is valid, the sketch is unchanged, the
number is plausible if you do not look at the units, and the model comes out
with a storey 2.7 km tall and every area and volume downstream wrong.  It
happened twice on this project.

App::PropertyLength ends it by carrying the unit instead of assuming it, and
these tests pin down the reading: a Quantity is asked what it is worth in
metres rather than being trusted to be metres already.
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


class Quantity(object):
    """Enough of FreeCAD's Quantity: it knows what unit it is in."""

    def __init__(self, millimetres):
        self.millimetres = float(millimetres)

    def getValueAs(self, unit):
        assert unit == "m", unit
        return self.millimetres / 1000.0


class Thing(object):
    def __init__(self, **props):
        for name, value in props.items():
            setattr(self, name, value)


class LengthReadingTests(unittest.TestCase):

    def test_a_quantity_is_asked_for_metres(self):
        obj = Thing(OS_FloorToFloor=Quantity(2710.0))
        self.assertAlmostEqual(fb.length_m(obj, "OS_FloorToFloor"), 2.710)

    def test_the_feet_inches_case_that_started_this(self):
        """8ft 10-11/16in.  As a float this read as 2709.86 metres."""
        obj = Thing(OS_FloorToFloor=Quantity(2709.8625))
        self.assertAlmostEqual(fb.length_m(obj, "OS_FloorToFloor"), 2.7098625)

    def test_a_bare_number_is_millimetres_not_metres(self):
        """FreeCAD's internal unit, which is the only thing a bare number
        coming off a length property can be."""
        obj = Thing(OS_Elevation=3380.0)
        self.assertAlmostEqual(fb.length_m(obj, "OS_Elevation"), 3.38)

    def test_a_property_that_is_not_there_reads_as_None(self):
        self.assertIsNone(fb.length_m(Thing(), "OS_Elevation"))


class StoryTests(unittest.TestCase):

    def test_elevation(self):
        sketch = Thing(OS_Elevation=Quantity(3380.0))
        self.assertAlmostEqual(fb.story_elevation_m(sketch), 3.38)

    def test_floor_to_floor(self):
        sketch = Thing(OS_FloorToFloor=Quantity(3380.0))
        self.assertAlmostEqual(fb.story_height_m(sketch), 3.38)

    def test_a_sketch_without_the_properties_falls_back(self):
        self.assertEqual(fb.story_elevation_m(Thing()), 0.0)
        self.assertEqual(fb.story_height_m(Thing()), 3.0)

    def test_ground_level_is_not_mistaken_for_missing(self):
        """0.0 m is a real elevation and must not read as 'unset'."""
        self.assertEqual(fb.story_elevation_m(Thing(OS_Elevation=Quantity(0.0))),
                         0.0)


class RoomHeightTests(unittest.TestCase):

    def test_an_override_reads_in_metres(self):
        label = Thing(OS_Height=Quantity(6090.0))
        self.assertAlmostEqual(fb.label_height_m(label), 6.09)

    def test_zero_means_defer_to_the_story(self):
        self.assertIsNone(fb.label_height_m(Thing(OS_Height=Quantity(0.0))))

    def test_a_label_without_the_property_defers_too(self):
        self.assertIsNone(fb.label_height_m(Thing()))

    def test_a_negative_override_is_handed_back_not_swallowed(self):
        """A typed minus sign is a mistake to report, not to treat as unset."""
        label = Thing(OS_Height=Quantity(-2000.0))
        self.assertAlmostEqual(fb.label_height_m(label), -2.0)


class PropertyTypeTests(unittest.TestCase):
    """The declarations themselves, so no float can creep back in."""

    def test_every_story_length_is_a_length(self):
        for name in ("OS_Elevation", "OS_FloorToFloor"):
            self.assertEqual(fb.STORY_PROPS[name][0], "App::PropertyLength",
                             "%s must carry its own unit" % name)

    def test_no_property_still_promises_metres_in_its_name(self):
        named = [n for n in fb.STORY_PROPS] + [fb.HEIGHT_PROP]
        self.assertEqual([n for n in named if n.endswith("_m")], [])


if __name__ == "__main__":
    unittest.main()
