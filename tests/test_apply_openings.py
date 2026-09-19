# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for apply_openings.py's host resolution.

find_host and fits_host only touch the openstudio module -- no FreeCAD, no
Part -- so unlike most of the bridge's geometry these are testable against
real openstudio.model.Surface objects, not stubs.

The regression this guards: apply_openings.py used to trust the host_surface
name a .json carried verbatim.  A full rebuild reassigns Surface numbers
essentially arbitrarily, so that name can end up pointing at a wall the
outline was never drawn on -- and OpenStudio's own setSurface/setVertices
did not reject the mismatch.  Measured on FloorplanTest-05: all 28 openings
in the model, silently placed on the wrong wall, no error anywhere.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openstudio  # noqa: E402

import apply_openings as ao  # noqa: E402


def make_wall(model, space, x0, x1, z1=3.0, y=0.0):
    """A simple rectangular Wall surface in the XZ plane at this y."""
    pts = openstudio.Point3dVector([
        openstudio.Point3d(x0, y, 0.0), openstudio.Point3d(x1, y, 0.0),
        openstudio.Point3d(x1, y, z1), openstudio.Point3d(x0, y, z1)])
    surface = openstudio.model.Surface(pts, model)
    surface.setSpace(space)
    return surface


class FitsHostTests(unittest.TestCase):

    def setUp(self):
        self.model = openstudio.model.Model()
        self.space = openstudio.model.Space(self.model)
        self.wall = make_wall(self.model, self.space, 0.0, 10.0)

    def test_an_outline_inside_the_wall_fits(self):
        pts = [(2.0, 0.0, 1.0), (3.0, 0.0, 1.0), (3.0, 0.0, 2.0),
              (2.0, 0.0, 2.0)]
        self.assertTrue(ao.fits_host(pts, self.wall))

    def test_an_outline_outside_the_walls_extent_does_not_fit(self):
        pts = [(15.0, 0.0, 1.0), (16.0, 0.0, 1.0), (16.0, 0.0, 2.0),
              (15.0, 0.0, 2.0)]
        self.assertFalse(ao.fits_host(pts, self.wall))

    def test_an_outline_off_the_walls_plane_does_not_fit(self):
        pts = [(2.0, 0.5, 1.0), (3.0, 0.5, 1.0), (3.0, 0.5, 2.0),
              (2.0, 0.5, 2.0)]
        self.assertFalse(ao.fits_host(pts, self.wall))


class FindHostTests(unittest.TestCase):

    def setUp(self):
        self.model = openstudio.model.Model()
        self.space = openstudio.model.Space(self.model)
        # Two disjoint walls a real fresh-build renumbering could confuse
        # for each other -- neither overlaps the other's extent.
        self.near = make_wall(self.model, self.space, 0.0, 10.0)
        self.near.setName("Surface 1")
        self.far = make_wall(self.model, self.space, 100.0, 110.0)
        self.far.setName("Surface 2")
        self.outline = [(102.0, 0.0, 1.0), (103.0, 0.0, 1.0),
                        (103.0, 0.0, 2.0), (102.0, 0.0, 2.0)]

    def test_a_still_valid_named_host_is_kept_without_a_fallback_search(self):
        host, is_fallback, problem = ao.find_host(
            self.outline, self.far, [self.near, self.far])
        self.assertIs(host, self.far)
        self.assertFalse(is_fallback)
        self.assertIsNone(problem)

    def test_a_stale_name_is_replaced_by_the_geometric_match(self):
        # The saved name now resolves to the WRONG wall -- exactly the
        # fresh-rebuild scenario: named_host is valid but does not fit.
        host, is_fallback, problem = ao.find_host(
            self.outline, self.near, [self.near, self.far])
        self.assertIs(host, self.far)
        self.assertTrue(is_fallback)
        self.assertIsNone(problem)

    def test_a_missing_named_host_falls_back_too(self):
        host, is_fallback, problem = ao.find_host(
            self.outline, None, [self.near, self.far])
        self.assertIs(host, self.far)
        self.assertTrue(is_fallback)

    def test_nothing_fitting_is_reported_not_guessed(self):
        outline = [(500.0, 0.0, 1.0), (501.0, 0.0, 1.0),
                  (501.0, 0.0, 2.0), (500.0, 0.0, 2.0)]
        host, is_fallback, problem = ao.find_host(
            outline, None, [self.near, self.far])
        self.assertIsNone(host)
        self.assertIn("no surface", problem)

    def test_two_fits_is_ambiguous_not_guessed(self):
        # A second wall on the same plane, overlapping self.near's extent --
        # any outline that fits one fits both.
        overlap = make_wall(self.model, self.space, 0.0, 10.0)
        outline = [(2.0, 0.0, 1.0), (3.0, 0.0, 1.0), (3.0, 0.0, 2.0),
                  (2.0, 0.0, 2.0)]
        host, is_fallback, problem = ao.find_host(
            outline, None, [self.near, self.far, overlap])
        self.assertIsNone(host)
        self.assertIn("2 surfaces", problem)


if __name__ == "__main__":
    unittest.main()
