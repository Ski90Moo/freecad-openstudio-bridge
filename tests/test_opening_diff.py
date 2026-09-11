# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the change report on openings and shades.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

`update` tells you which spaces an architectural revision moved before you
apply it.  `apply` told you only how many subsurfaces it touched -- so six
moved windows, a widened door and a deleted storefront all arrived as
"updated 28 in place".  Preserving handles keeps a window's construction and
shading control attached; it says nothing about the window having moved.

The distinctions here are the ones an engineer acts on differently: a **moved**
or **resized** opening is a drawing revision to check against the architect's
markup, a **retyped** one is a decision somebody made in the document, and a
**rehosted** one means it landed on a different wall than before -- which is
either a real change or a projection going wrong.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opening_diff as od  # noqa: E402


def rect(x0=0.0, z0=0.0, w=1.5, h=2.0, y=0.0):
    return [(x0, y, z0), (x0 + w, y, z0), (x0 + w, y, z0 + h), (x0, y, z0 + h)]


def record(name="FixedWindow West 01", kind="FixedWindow",
           host="Surface 54", vertices=None):
    return {"name": name, "type": kind, "host": host,
            "vertices": rect() if vertices is None else vertices}


class GeometryTests(unittest.TestCase):

    def test_area_of_a_vertical_rectangle(self):
        self.assertAlmostEqual(od.area_m2(rect(w=1.5, h=2.0)), 3.0, 9)

    def test_area_is_independent_of_winding(self):
        self.assertAlmostEqual(od.area_m2(list(reversed(rect()))),
                               od.area_m2(rect()), 9)

    def test_area_of_a_horizontal_plate(self):
        plate = [(0, 0, 3), (2, 0, 3), (2, -1.5, 3), (0, -1.5, 3)]
        self.assertAlmostEqual(od.area_m2(plate), 3.0, 9)

    def test_centroid(self):
        self.assertEqual(od.centroid(rect(w=2.0, h=4.0)), (1.0, 0.0, 2.0))


class CompareTests(unittest.TestCase):

    def test_identical_is_no_change(self):
        changes, _ = od.compare(record(), record())
        self.assertEqual(changes, [])

    def test_a_moved_window(self):
        changes, detail = od.compare(record(), record(vertices=rect(x0=0.4)))
        self.assertEqual(changes, ["moved"])
        self.assertAlmostEqual(detail["moved_m"], 0.4, 6)

    def test_a_widened_window_is_resized_and_moved(self):
        """Widening on one side shifts the centroid too -- report both."""
        changes, detail = od.compare(record(),
                                     record(vertices=rect(w=1.8)))
        self.assertEqual(sorted(changes), ["moved", "resized"])
        self.assertAlmostEqual(detail["area_delta_m2"], 0.6, 6)
        self.assertAlmostEqual(detail["moved_m"], 0.15, 6)

    def test_a_window_grown_about_its_centre_is_only_resized(self):
        changes, detail = od.compare(
            record(), record(vertices=rect(x0=-0.15, w=1.8)))
        self.assertEqual(changes, ["resized"])
        self.assertAlmostEqual(detail["area_delta_m2"], 0.6, 6)

    def test_a_shrunk_window_reports_a_negative_delta(self):
        changes, detail = od.compare(
            record(), record(vertices=rect(x0=0.25, w=1.0)))
        self.assertIn("resized", changes)
        self.assertLess(detail["area_delta_m2"], 0)

    def test_a_retyped_opening(self):
        changes, detail = od.compare(record(), record(kind="GlassDoor"))
        self.assertEqual(changes, ["retyped"])
        self.assertEqual(detail["type"], ("FixedWindow", "GlassDoor"))

    def test_a_rehosted_opening(self):
        changes, detail = od.compare(record(), record(host="Surface 189"))
        self.assertEqual(changes, ["rehosted"])
        self.assertEqual(detail["host"], ("Surface 54", "Surface 189"))

    def test_everything_at_once(self):
        changes, _ = od.compare(
            record(),
            record(kind="Door", host="Surface 189", vertices=rect(x0=1, w=3)))
        self.assertEqual(sorted(changes),
                         ["moved", "rehosted", "resized", "retyped"])

    def test_json_rounding_is_not_a_change(self):
        """Vertices round-trip at six decimals; that must not read as moved."""
        nudged = [(x + 4e-7, y, z) for x, y, z in rect()]
        self.assertEqual(od.compare(record(), record(vertices=nudged))[0], [])

    def test_a_vertex_count_change_is_reshaped(self):
        five = rect() + [(0.75, 0.0, 2.5)]
        self.assertIn("reshaped", od.compare(record(),
                                             record(vertices=five))[0])


class DiffTests(unittest.TestCase):

    def setUp(self):
        self.before = {
            "a": record("FixedWindow West 01"),
            "b": record("FixedWindow West 03"),
            "c": record("Door North 04", kind="Door"),
        }

    def test_nothing_changed(self):
        result = od.diff(self.before, dict(self.before))
        self.assertEqual(len(result["unchanged"]), 3)
        self.assertEqual(result["changed"], [])
        self.assertEqual((result["added"], result["removed"]), ([], []))

    def test_an_added_and_a_removed_opening(self):
        after = dict(self.before)
        del after["b"]
        after["d"] = record("FixedWindow South 09")
        result = od.diff(self.before, after)
        self.assertEqual(result["added"], ["FixedWindow South 09"])
        self.assertEqual(result["removed"], ["FixedWindow West 03"])

    def test_a_retype_is_one_change_not_a_delete_and_an_add(self):
        """Keyed on the id, so the rename that comes with it is not a churn."""
        after = dict(self.before)
        after["c"] = record("GlassDoor North 04", kind="GlassDoor")
        result = od.diff(self.before, after)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0][0], "GlassDoor North 04")

    def test_the_report_names_what_changed(self):
        after = dict(self.before)
        after["a"] = record("FixedWindow West 01", vertices=rect(x0=0.4))
        lines = "\n".join(od.report(od.diff(self.before, after)))
        self.assertIn("changed    1", lines)
        self.assertIn("moved 400 mm", lines)

    def test_the_report_lists_removals_by_name(self):
        after = dict(self.before)
        del after["b"]
        lines = "\n".join(od.report(od.diff(self.before, after)))
        self.assertIn("removed: FixedWindow West 03", lines)


if __name__ == "__main__":
    unittest.main()
