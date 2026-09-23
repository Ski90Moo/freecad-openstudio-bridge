# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Unit tests for corridor_split.py -- pure geometry, no FreeCAD/openstudio."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corridor_split as cs


def area_of(poly):
    return abs(cs._signed_area(poly))


class RectangleTests(unittest.TestCase):
    def test_plain_rectangle_is_a_no_op(self):
        rect = [(0, 0), (10, 0), (10, 4), (0, 4)]
        kind, cuts = cs.classify_and_cut(rect)
        self.assertEqual(kind, cs.RECTANGLE)
        self.assertIsNone(cuts)

    def test_rotated_rectangle_is_also_a_no_op(self):
        # A rectangle drawn on a non-zero angle -- the north-axis case.
        theta = math.radians(37.0)
        ux, uy = math.cos(theta), math.sin(theta)
        base = [(0, 0), (10, 0), (10, 4), (0, 4)]
        rotated = [(x * ux - y * uy, x * uy + y * ux) for x, y in base]
        kind, cuts = cs.classify_and_cut(rotated)
        self.assertEqual(kind, cs.RECTANGLE)
        self.assertIsNone(cuts)


class LShapeTests(unittest.TestCase):
    L = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)]

    def test_l_shape_splits_into_two_rectangles(self):
        kind, cuts = cs.classify_and_cut(self.L)
        self.assertEqual(kind, "l")
        self.assertEqual(len(cuts), 1)
        p, q = cuts[0]
        # Both endpoints must lie on the original boundary.
        self.assertIsNotNone(cs._locate_on_boundary(self.L, p))
        self.assertIsNotNone(cs._locate_on_boundary(self.L, q))
        pieces = cs._split_polygon_by_chord(self.L, p, q)
        self.assertIsNotNone(pieces)
        a, b = pieces
        self.assertTrue(cs._is_rectangle(a))
        self.assertTrue(cs._is_rectangle(b))
        self.assertAlmostEqual(area_of(a) + area_of(b), area_of(self.L), places=6)

    def test_l_shape_is_idempotent(self):
        # A plain rectangle leg, once split off, must not be re-split.
        kind, cuts = cs.classify_and_cut(self.L)
        p, q = cuts[0]
        a, b = cs._split_polygon_by_chord(self.L, p, q)
        for piece in (a, b):
            piece_kind, piece_cuts = cs.classify_and_cut(piece)
            self.assertEqual(piece_kind, cs.RECTANGLE)
            self.assertIsNone(piece_cuts)


class TShapeTests(unittest.TestCase):
    # The actual 001-HALL-1-Corridor footprint from the reported bug --
    # a bar with a stem hanging off its underside.
    T = [(14.589, 14.114), (0, 14.114), (0, 12.408), (4.385, 12.408),
         (4.385, 3.823), (6.029, 3.823), (6.029, 12.408), (14.589, 12.408)]

    def test_t_shape_splits_into_bar_and_stem(self):
        kind, cuts = cs.classify_and_cut(self.T)
        self.assertEqual(kind, "t")
        self.assertEqual(len(cuts), 1)
        p, q = cuts[0]
        pieces = cs._split_polygon_by_chord(self.T, p, q)
        self.assertIsNotNone(pieces)
        a, b = pieces
        self.assertTrue(cs._is_rectangle(a))
        self.assertTrue(cs._is_rectangle(b))
        self.assertAlmostEqual(area_of(a) + area_of(b), area_of(self.T), places=6)
        # The stem is the narrow piece: exactly the reflex-vertex run,
        # 6.029 - 4.385 = 1.644 m, matching the bug report's Surface 9.
        widths = sorted(max(x for x, _ in piece) - min(x for x, _ in piece)
                        for piece in (a, b))
        self.assertAlmostEqual(widths[0], 6.029 - 4.385, places=3)

    def test_t_shape_rotated_on_a_north_axis_still_splits(self):
        theta = math.radians(45.0)
        ux, uy = math.cos(theta), math.sin(theta)
        rotated = [(x * ux - y * uy, x * uy + y * ux) for x, y in self.T]
        kind, cuts = cs.classify_and_cut(rotated)
        self.assertEqual(kind, "t")


class CrossShapeTests(unittest.TestCase):
    # A symmetric plus: 2-wide arms, extending to +-7, center square +-1.
    CROSS = [(1, -1), (7, -1), (7, 1), (1, 1), (1, 7), (-1, 7), (-1, 1),
             (-7, 1), (-7, -1), (-1, -1), (-1, -7), (1, -7)]

    def test_cross_shape_splits_into_three_rectangles(self):
        kind, cuts = cs.classify_and_cut(self.CROSS)
        self.assertEqual(kind, "cross")
        self.assertEqual(len(cuts), 2)
        pieces = cs._apply_cuts(self.CROSS, cuts)
        self.assertIsNotNone(pieces)
        self.assertEqual(len(pieces), 3)
        for piece in pieces:
            self.assertTrue(cs._is_rectangle(piece))
        self.assertAlmostEqual(sum(area_of(p) for p in pieces),
                               area_of(self.CROSS), places=6)


class UnsupportedShapeTests(unittest.TestCase):
    def test_non_rectilinear_polygon_is_unsupported(self):
        triangle_ish = [(0, 0), (10, 0), (5, 8)]
        kind, reason = cs.classify_and_cut(triangle_ish)
        self.assertEqual(kind, cs.UNSUPPORTED)
        self.assertTrue(reason)

    def test_z_shape_two_unmatched_reflex_corners_is_unsupported(self):
        # A Z/S-shaped footprint: two reflex corners that do not share an
        # axis, unlike a T's matched pair.
        z = [(0, 0), (4, 0), (4, 6), (10, 6), (10, 10), (6, 10), (6, 4), (0, 4)]
        kind, reason = cs.classify_and_cut(z)
        self.assertEqual(kind, cs.UNSUPPORTED)
        self.assertTrue(reason)

    def test_five_reflex_corners_is_unsupported(self):
        # A comb-like shape well outside T/L/cross scope.
        comb = [(0, 0), (12, 0), (12, 10), (10, 10), (10, 4), (8, 4), (8, 10),
                (6, 10), (6, 4), (4, 4), (4, 10), (2, 10), (2, 4), (0, 4)]
        kind, reason = cs.classify_and_cut(comb)
        self.assertEqual(kind, cs.UNSUPPORTED)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
