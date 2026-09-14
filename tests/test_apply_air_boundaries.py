# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""apply_air_boundaries.py -- the port of the assign_air_boundary_construction
Ruby measure.

Most of these are about what it REFUSES.  An air boundary applied wrongly does
not raise anything: the model builds, EnergyPlus runs, and the answer is quietly
different.  So every guard here is a failure that would otherwise be invisible
until someone questioned a result.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ApplyAirBoundaryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import openstudio
            import apply_air_boundaries
        except ImportError as exc:      # pragma: no cover
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.os = openstudio
        cls.aab = apply_air_boundaries

    def two_rooms(self):
        """Two rooms side by side, matched, so one wall is interior.

        Returns (model, [the two names of that shared wall]).
        """
        model = self.os.model.Model()
        spaces = self.os.model.SpaceVector()
        for index, x0 in enumerate((0.0, 5.0)):
            points = self.os.Point3dVector()
            for x, y in ((x0, 0.0), (x0, 4.0), (x0 + 5.0, 4.0),
                         (x0 + 5.0, 0.0)):
                points.append(self.os.Point3d(x, y, 0.0))
            space = self.os.model.Space.fromFloorPrint(points, 3.0,
                                                       model).get()
            space.setName("Room %d" % (index + 1))
            spaces.append(space)
        self.os.model.matchSurfaces(spaces)

        shared = [s.nameString() for space in spaces
                  for s in space.surfaces()
                  if s.adjacentSurface().is_initialized()]
        self.assertEqual(len(shared), 2, "fixture did not match the two rooms")
        return model, shared

    def spec(self, names, name="Air Boundary - 1 to 2", ach=5.0,
             method="SimpleMixing"):
        return {"construction_name": name,
                "surface_names": ",".join(names),
                "air_exchange_method": method,
                "simple_mixing_ach": ach}

    # ---- the happy path ------------------------------------------------

    def test_both_faces_get_the_construction(self):
        model, shared = self.two_rooms()
        specs = [self.spec(shared)]
        self.assertEqual(self.aab.check(model, specs)[0], [])
        self.aab.apply(model, specs)

        boundaries = model.getConstructionAirBoundarys()
        self.assertEqual(len(boundaries), 1)
        for name in shared:
            surface = model.getSurfaceByName(name).get()
            self.assertTrue(surface.construction().is_initialized())
            self.assertEqual(surface.construction().get().nameString(),
                             "Air Boundary - 1 to 2")

    def test_the_rate_is_written_not_left_defaulted(self):
        """OpenStudio's constructor writes 0.0, where the IDD default is 0.5.

        So SimpleMixing on a construction nobody set a rate on mixes nothing
        at all, and looks entirely healthy while doing it.
        """
        model, shared = self.two_rooms()
        fresh = self.os.model.ConstructionAirBoundary(model)
        self.assertEqual(fresh.simpleMixingAirChangesPerHour(), 0.0)
        fresh.remove()

        self.aab.apply(model, [self.spec(shared, ach=3.25)])
        boundary = model.getConstructionAirBoundarys()[0]
        self.assertEqual(boundary.airExchangeMethod(), "SimpleMixing")
        self.assertEqual(boundary.simpleMixingAirChangesPerHour(), 3.25)

    def test_reapplying_updates_rather_than_duplicating(self):
        model, shared = self.two_rooms()
        self.aab.apply(model, [self.spec(shared, ach=5.0)])
        created, reused, _, _, _ = self.aab.apply(
            model, [self.spec(shared, ach=2.0)])

        self.assertEqual((created, reused), (0, 1))
        self.assertEqual(len(model.getConstructionAirBoundarys()), 1)
        self.assertEqual(
            model.getConstructionAirBoundarys()[0]
            .simpleMixingAirChangesPerHour(), 2.0,
            "the report is authoritative; a changed rate must land")

    # ---- what it refuses -----------------------------------------------

    def test_a_zero_rate_is_refused(self):
        model, shared = self.two_rooms()
        problems, _ = self.aab.check(model, [self.spec(shared, ach=0)])
        self.assertTrue(any("mixes nothing" in p for p in problems), problems)

    def test_one_face_of_a_pair_is_refused(self):
        """Half an air boundary is a wall with a hole in the accounting."""
        model, shared = self.two_rooms()
        problems, _ = self.aab.check(model, [self.spec(shared[:1])])
        self.assertTrue(any("other face" in p for p in problems), problems)

    def test_two_constructions_on_one_zone_pair_are_refused(self):
        """EnergyPlus mixes per construction, so this doubles the flow."""
        model, shared = self.two_rooms()
        problems, _ = self.aab.check(model, [
            self.spec(shared[:1], name="A"),
            self.spec(shared[1:], name="B"),
        ])
        self.assertTrue(any("mix twice" in p for p in problems), problems)

    def test_an_exterior_surface_is_refused(self):
        model, _ = self.two_rooms()
        exterior = [s.nameString() for s in model.getSurfaces()
                    if s.outsideBoundaryCondition() == "Outdoors"][0]
        problems, _ = self.aab.check(model, [self.spec([exterior])])
        self.assertTrue(any("not interior" in p for p in problems), problems)

    def test_a_surface_carrying_a_window_is_refused(self):
        """EnergyPlus does not allow a sub-surface on an air boundary."""
        model, shared = self.two_rooms()
        host = model.getSurfaceByName(shared[0]).get()
        points = self.os.Point3dVector()
        for x, y, z in ((5.0, 1.0, 1.0), (5.0, 3.0, 1.0),
                        (5.0, 3.0, 2.0), (5.0, 1.0, 2.0)):
            points.append(self.os.Point3d(x, y, z))
        window = self.os.model.SubSurface(points, model)
        window.setSurface(host)

        problems, _ = self.aab.check(model, [self.spec(shared)])
        self.assertTrue(any("sub-surface" in p for p in problems), problems)

    def test_a_name_the_model_does_not_have_is_refused(self):
        """A stale report names surfaces from a build that no longer exists."""
        model, shared = self.two_rooms()
        problems, _ = self.aab.check(
            model, [self.spec(shared + ["Surface 9999"])])
        self.assertTrue(any("different build" in p for p in problems),
                        problems)

    def test_every_problem_is_reported_not_just_the_first(self):
        """A stale report usually names several, and one run per fix is slow."""
        model, shared = self.two_rooms()
        problems, _ = self.aab.check(model, [
            self.spec(["Surface 9998"], name="A"),
            self.spec(["Surface 9999"], name="B", ach=0),
        ])
        self.assertGreaterEqual(len(problems), 3, problems)

    # ---- not leaving yesterday's answer behind --------------------------

    def test_a_boundary_no_longer_reported_is_cleared(self):
        """A corridor the plan stopped cutting in two keeps its construction
        forever unless somebody takes it off."""
        model, shared = self.two_rooms()
        self.aab.apply(model, [self.spec(shared)])

        _, _, _, cleared, removed = self.aab.apply(model, [])
        self.assertEqual(cleared, 2)
        self.assertEqual(removed, 1)
        self.assertEqual(len(model.getConstructionAirBoundarys()), 0)
        for name in shared:
            self.assertFalse(
                model.getSurfaceByName(name).get()
                .construction().is_initialized())

    def test_an_air_boundary_added_by_hand_is_left_alone(self):
        """Only our own work is cleared.  Someone else's is theirs."""
        model, shared = self.two_rooms()
        mine = self.os.model.ConstructionAirBoundary(model)
        mine.setName("Hand-made")
        mine.setSimpleMixingAirChangesPerHour(1.0)
        for name in shared:
            model.getSurfaceByName(name).get().setConstruction(mine)

        self.aab.apply(model, [])
        self.assertEqual(len(model.getConstructionAirBoundarys()), 1)
        for name in shared:
            self.assertTrue(model.getSurfaceByName(name).get()
                            .construction().is_initialized())


if __name__ == "__main__":
    unittest.main()
