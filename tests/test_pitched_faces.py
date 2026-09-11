# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""A pitched roof face has to survive the OSM's six decimal places.

Run under FreeCAD's own Python -- these are the only tests in the suite that
need real OCC rather than the stubs the other fc_* tests use, because what is
being pinned down is precisely what OCC will and will not accept:

    "C:/Program Files/FreeCAD 1.1/bin/python.exe" -m unittest discover \
        -s tests -p "test_pitched_faces.py" -v

Under the bridge venv they skip, and `discover` over the whole tests directory
stays green in both interpreters.

The regression, found on screen: eight roof surfaces drew nothing in FreeCAD
after the roof was applied.  They were not broken -- OpenStudio was perfectly
happy with them, and the areas were right -- but the OSM stores metres to six
decimal places, so a sloped loop touching three or more elevations has each of
those elevations rounded independently and no longer lies on one exact plane.
OCC's Precision::Confusion is 1e-7 mm, so Part.Face refused the wire and
make_face fell back to the bare outline, which draws no face at all.  Loops
spanning only two elevations are parallelograms and stay exactly planar either
way, which is why part of the same roof came in fine and the flat roof never
showed this at all.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import FreeCAD
    import Part
    # Another test module may have installed the stubs into sys.modules first;
    # those have no OCC behind them and would prove nothing.
    REAL_OCC = hasattr(Part, "OCCError") and hasattr(FreeCAD, "Vector")
except ImportError:
    REAL_OCC = False


@unittest.skipUnless(REAL_OCC, "needs FreeCAD's OCC -- run under FreeCAD python")
class PitchedFaceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import fc_import_surfaces
        cls.fis = fc_import_surfaces

    def setUp(self):
        del self.fis.repaired[:]
        del self.fis.outlines[:]

    # Surface 120 of fptest02.osm, verbatim -- 020-204-Shop's roof, the one
    # selected in the screenshot that started this.
    SHOP_ROOF = [
        (16.38331, 18.286096, 6.09),
        (16.38331, 12.166092, 6.344954),
        (20.322842, 12.166092, 6.344954),
        (20.322842, 0, 6.851784),
        (33.352711, 0, 6.851784),
        (33.352711, 18.286096, 6.09),
    ]

    def test_occ_really_does_refuse_this_loop(self):
        """The premise.  Without the repair there is no face at all."""
        pts = [FreeCAD.Vector(x * 1000.0, y * 1000.0, z * 1000.0)
               for x, y, z in self.SHOP_ROOF]
        wire = Part.makePolygon(pts + [pts[0]])
        self.assertTrue(wire.isClosed())
        self.assertTrue(wire.isValid())
        with self.assertRaises(Part.OCCError):
            Part.Face(wire)

    def test_the_pitched_roof_comes_in_as_a_face(self):
        shape = self.fis.make_face(self.SHOP_ROOF, "Surface 120")
        self.assertEqual(len(shape.Faces), 1)
        self.assertAlmostEqual(shape.Faces[0].Area / 1e6, 262.60, 2)
        self.assertEqual([n for n, _ in self.fis.repaired], ["Surface 120"])
        self.assertEqual(self.fis.outlines, [])

    def test_the_repair_moves_nothing_that_matters(self):
        self.fis.make_face(self.SHOP_ROOF, "Surface 120")
        self.assertLess(self.fis.repaired[0][1], 0.001)     # under a micron

    def test_a_flat_roof_needs_no_repair(self):
        flat = [(x, y, 6.09) for x, y, _ in self.SHOP_ROOF]
        self.assertEqual(len(self.fis.make_face(flat, "flat").Faces), 1)
        self.assertEqual(self.fis.repaired, [])

    def test_two_elevations_need_no_repair(self):
        """The other half of the same roof: a sloped parallelogram is exact."""
        sloped = [(0.0, 0.0, 6.851784), (10.0, 0.0, 6.851784),
                  (10.0, 18.286096, 6.09), (0.0, 18.286096, 6.09)]
        self.assertEqual(len(self.fis.make_face(sloped, "sloped").Faces), 1)
        self.assertEqual(self.fis.repaired, [])

    def test_a_genuinely_bent_loop_stays_an_outline(self):
        """A real error is millimetres out, and must not be quietly flattened."""
        bent = list(self.SHOP_ROOF)
        bent[2] = (20.322842, 12.166092, 6.40)      # 55 mm off the plane
        shape = self.fis.make_face(bent, "bent")
        self.assertEqual(len(shape.Faces), 0)
        self.assertEqual(self.fis.outlines, ["bent"])
        self.assertEqual(self.fis.repaired, [])

    def test_a_degenerate_loop_is_still_rejected(self):
        self.assertIsNone(self.fis.make_face([(0, 0, 0), (1, 0, 0)], "line"))


if __name__ == "__main__":
    unittest.main()
