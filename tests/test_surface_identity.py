# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for carrying a surface's identity across a rebuild.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

The thing being defended here is not geometry -- the rebuild already got that
right -- but everything hanging off the geometry.  Before this existed, a roof
change cost 18 openings and 9 air-boundary couplings, and put every surface
name somewhere new; recovering that took an export/apply cycle, a
re-derivation, and a hand-written matcher, and one attempt at it silently
reopened a rated fire wall.

Two failure modes are worth naming, because both are silent:

  * a key that matches the wrong wall moves a window into a different room,
    and nothing in OpenStudio objects;
  * a name that fails to come back leaves a --solid-surface list, a test, or a
    construction assignment pointing at whatever wall now holds the number.

So the tests below check that the key separates walls it must separate
(stacked pieces on one plan line), refuses to guess when it cannot, and that
names come back even when a sibling space has already taken them -- which is
the case that actually failed first, on 18 surfaces.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class KeyTests(unittest.TestCase):
    """The key itself, which needs no model."""

    @classmethod
    def setUpClass(cls):
        import surface_identity
        cls.si = surface_identity

    def test_plan_alone_would_confuse_stacked_walls(self):
        # The lower and upper halves of one wall: same line, different base.
        lower = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                 (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        upper = [(0.0, 0.0, 3.0), (4.0, 0.0, 3.0),
                 (4.0, 0.0, 6.0), (0.0, 0.0, 6.0)]
        a, b = self.si.surface_key(lower), self.si.surface_key(upper)
        self.assertEqual(a[0], b[0], "same plan line")
        self.assertNotEqual(a, b, "but they must not share a key")

    def test_raising_a_top_does_not_change_the_key(self):
        before = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                  (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        after = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                 (4.0, 0.0, 5.5), (0.0, 0.0, 4.2)]
        self.assertEqual(self.si.surface_key(before),
                         self.si.surface_key(after))

    def test_a_wall_gaining_a_vertex_keeps_its_key(self):
        """A hip or valley crossing a room splits the top edge, not the base."""
        plain = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                 (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        ridged = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 0.0, 3.0),
                  (2.0, 0.0, 4.0), (0.0, 0.0, 3.0)]
        self.assertEqual(self.si.surface_key(plain),
                         self.si.surface_key(ridged))

    def test_moving_a_wall_changes_the_key(self):
        here = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        there = [(0.0, 0.5, 0.0), (4.0, 0.5, 0.0),
                 (4.0, 0.5, 3.0), (0.0, 0.5, 3.0)]
        self.assertNotEqual(self.si.surface_key(here),
                            self.si.surface_key(there))

    def test_winding_does_not_affect_a_walls_key(self):
        loop = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        self.assertEqual(self.si.surface_key(loop),
                         self.si.surface_key(list(reversed(loop))))


class ContainmentTests(unittest.TestCase):
    """Whether a window still fits, and whether a face covers the same ground."""

    @classmethod
    def setUpClass(cls):
        import surface_identity
        cls.si = surface_identity

    def wall(self, top=3.0):
        return [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                (4.0, 0.0, top), (0.0, 0.0, top)]

    def test_a_window_inside_its_wall_is_contained(self):
        window = [(1.0, 0.0, 1.0), (3.0, 0.0, 1.0),
                  (3.0, 0.0, 2.0), (1.0, 0.0, 2.0)]
        self.assertTrue(self.si.contains(self.wall(), window))

    def test_a_window_above_a_shortened_wall_is_not(self):
        window = [(1.0, 0.0, 2.5), (3.0, 0.0, 2.5),
                  (3.0, 0.0, 3.5), (1.0, 0.0, 3.5)]
        self.assertFalse(self.si.contains(self.wall(3.0), window))

    def test_an_edge_flush_with_the_wall_counts_as_inside(self):
        flush = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                 (4.0, 0.0, 1.0), (0.0, 0.0, 1.0)]
        self.assertTrue(self.si.contains(self.wall(), flush))

    def test_plan_containment_ignores_pitch(self):
        """A flat roof fragment sits under the pitched face that replaced it."""
        pitched = [(0.0, 0.0, 6.0), (10.0, 0.0, 7.0),
                   (10.0, 8.0, 7.0), (0.0, 8.0, 6.0)]
        fragment = [(1.0, 1.0, 6.0), (4.0, 1.0, 6.0),
                    (4.0, 4.0, 6.0), (1.0, 4.0, 6.0)]
        self.assertTrue(self.si.plan_contains(pitched, fragment))
        self.assertFalse(self.si.contains(pitched, fragment),
                         "not coplanar, so a skylight must not be moved")


class RoundTripTests(unittest.TestCase):
    """Capture, rebuild for real, restore -- on a live model."""

    @classmethod
    def setUpClass(cls):
        try:
            import openstudio
            import surface_identity
        except ImportError as exc:      # pragma: no cover
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.os = openstudio
        cls.si = surface_identity

    def room(self, model, x0, name, height=3.0):
        pts = self.os.Point3dVector()
        for x, y in ((x0, 0.0), (x0, 4.0), (x0 + 4.0, 4.0), (x0 + 4.0, 0.0)):
            pts.append(self.os.Point3d(x, y, 0.0))
        space = self.os.model.Space.fromFloorPrint(pts, height, model).get()
        space.setName(name)
        return space

    def pair(self):
        """Two rooms side by side, matched, with a window and an air boundary."""
        model = self.os.model.Model()
        left = self.room(model, 0.0, "Left")
        right = self.room(model, 4.0, "Right")
        spaces = self.os.model.SpaceVector()
        spaces.append(left)
        spaces.append(right)
        self.os.model.intersectSurfaces(spaces)
        self.os.model.matchSurfaces(spaces)

        party = next(s for s in left.surfaces()
                     if s.adjacentSurface().is_initialized())
        boundary = self.os.model.ConstructionAirBoundary(model)
        boundary.setName("Air Boundary - test")
        party.setConstruction(boundary)
        party.setName("Party Wall")

        # Every wall of this room touches y = 0 at one end, so the test has to
        # ask for the one that lies *in* that plane.  OpenStudio will attach a
        # subsurface to a wall it is not coplanar with and say nothing, so
        # getting this wrong builds a fixture that looks fine and is not.
        outer = next(s for s in left.surfaces()
                     if s.surfaceType() == "Wall"
                     and s.outsideBoundaryCondition() == "Outdoors"
                     and all(abs(v.y()) < 1e-6
                             for v in left.transformation() * s.vertices()))
        outer.setName("Front Wall")
        window = self.os.Point3dVector()
        for x, z in ((1.0, 1.0), (3.0, 1.0), (3.0, 2.0), (1.0, 2.0)):
            window.append(self.os.Point3d(x, 0.0, z))
        sub = self.os.model.SubSurface(
            left.transformation().inverse() * window, model)
        sub.setSurface(outer)
        sub.setName("Front Window")
        sub.setSubSurfaceType("FixedWindow")
        sub.setComment("OS_OpeningId=abc123")
        return model, left, right

    def rebuild(self, model, space, height):
        """Delete a space and build it again taller, the way an update does."""
        x0 = min(v.x() for s in space.surfaces()
                 for v in space.transformation() * s.vertices())
        old_name = space.nameString()
        space.remove()
        fresh = self.room(model, x0, old_name, height)
        return fresh

    def test_a_window_and_an_air_boundary_survive_a_rebuild(self):
        model, left, right = self.pair()
        captured, ambiguous = self.si.capture(model, ["Left"])
        self.assertEqual(ambiguous, [])
        self.assertEqual(len(captured["Left"]), len(left.surfaces()))

        fresh = self.rebuild(model, left, 4.5)
        spaces = self.os.model.SpaceVector()
        spaces.append(fresh)
        spaces.append(right)
        self.os.model.intersectSurfaces(spaces)
        self.os.model.matchSurfaces(spaces)
        self.assertEqual(len(model.getSubSurfaces()), 0,
                         "the rebuild really did take the window")

        report = self.si.restore(model, captured, {"Left": "Left"})

        self.assertEqual(report["subsurfaces"], 1)
        sub = model.getSubSurfaces()[0]
        self.assertEqual(sub.nameString(), "Front Window")
        self.assertEqual(sub.subSurfaceType(), "FixedWindow")
        self.assertIn("abc123", sub.comment())
        self.assertAlmostEqual(sub.grossArea(), 2.0, places=6)

        party = next(s for s in fresh.surfaces()
                     if s.nameString() == "Party Wall")
        self.assertTrue(party.construction().is_initialized())
        self.assertEqual(party.construction().get().nameString(),
                         "Air Boundary - test")

    def test_names_come_back_even_when_a_sibling_took_them(self):
        """The case that failed first, on 18 surfaces.

        The old names are all free after the rebuild, so OpenStudio hands them
        straight back to whichever surface it creates next -- often one in a
        different room.  Parking has to be global for this to work.
        """
        model, left, right = self.pair()
        captured, _ambiguous = self.si.capture(model, ["Left", "Right"])
        wanted = {r["name"] for recs in captured.values()
                  for r in recs.values()}

        fresh_left = self.rebuild(model, left, 4.5)
        fresh_right = self.rebuild(model, right, 4.5)
        spaces = self.os.model.SpaceVector()
        spaces.append(fresh_left)
        spaces.append(fresh_right)
        self.os.model.intersectSurfaces(spaces)
        self.os.model.matchSurfaces(spaces)

        self.si.restore(model, captured,
                        {"Left": "Left", "Right": "Right"})

        got = {s.nameString() for s in model.getSurfaces()}
        self.assertTrue(wanted <= got,
                        "missing: %s" % sorted(wanted - got))
        self.assertEqual(len(got), len(model.getSurfaces()),
                         "names must stay unique")

    def test_an_ambiguous_key_is_reported_not_guessed(self):
        model = self.os.model.Model()
        space = self.os.model.Space(model)
        space.setName("Odd")
        loop = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0),
                (4.0, 0.0, 3.0), (0.0, 0.0, 3.0)]
        for index in range(2):
            pts = self.os.Point3dVector()
            for x, y, z in loop:
                pts.append(self.os.Point3d(x, y, z))
            surface = self.os.model.Surface(pts, model)
            surface.setSpace(space)
            surface.setName("Twin %d" % index)

        captured, ambiguous = self.si.capture(model, ["Odd"])
        self.assertEqual(captured["Odd"], {})
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0][0], "Odd")
        self.assertEqual(ambiguous[0][1], ["Twin 0", "Twin 1"])

    def test_a_window_that_no_longer_fits_is_reported(self):
        """Shortening a wall must not silently drop or reproject its window."""
        model, left, right = self.pair()
        captured, _ambiguous = self.si.capture(model, ["Left"])
        self.rebuild(model, left, 1.5)          # window sat at 1.0 to 2.0 m

        report = self.si.restore(model, captured, {"Left": "Left"})
        self.assertEqual(report["subsurfaces"], 0)
        self.assertTrue(any(name == "Front Window"
                            for _host, name, _why in report["lost"]),
                        "expected the window in the lost list, got %r"
                        % report["lost"])

    def test_nothing_is_restored_into_a_space_that_went_away(self):
        model, left, _right = self.pair()
        captured, _ambiguous = self.si.capture(model, ["Left"])
        left.remove()
        report = self.si.restore(model, captured, {"Left": "Left"})
        self.assertEqual(report["surfaces"], 0)
        self.assertTrue(report["notes"])

    def test_an_inherited_construction_survives_a_defaulted_target(self):
        """restore()'s orphan-rehome path checked
        `not target.construction().is_initialized()` to decide a replacement
        was unclaimed -- but on any model with a building-level
        DefaultConstructionSet (every real model, once constructions are
        assigned, unlike this module's own bare fixtures) that is True as
        soon as ANY default resolves, whether or not something was
        hard-assigned.  So an orphan's construction was never inherited on
        a real model.  isConstructionDefaulted() is the right question:
        True whenever nothing is hard-set, False once something genuinely
        claims the surface -- both verified against a live model before
        this test was written.
        """
        model = self.os.model.Model()
        space = self.os.model.Space(model)
        space.setName("Attic")

        # A small flat fragment, as if intersectSurfaces had already split a
        # flat roof into pieces -- the same shape ContainmentTests'
        # test_plan_containment_ignores_pitch uses for the "plan" rehome
        # mode, just captured through the real capture()/restore() path
        # this time.
        fragment = self.os.Point3dVector()
        for x, y in ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)):
            fragment.append(self.os.Point3d(x, y, 3.0))
        roof = self.os.model.Surface(fragment, model)
        roof.setSpace(space)
        roof.setSurfaceType("RoofCeiling")
        roof.setName("Flat Roof Fragment")
        boundary = self.os.model.ConstructionAirBoundary(model)
        boundary.setName("Air Boundary - roof test")
        roof.setConstruction(boundary)

        captured, ambiguous = self.si.capture(model, ["Attic"])
        self.assertEqual(ambiguous, [])
        roof.remove()

        default_construction = self.os.model.Construction(model)
        default_construction.setName("Default Roof")
        surface_defaults = self.os.model.DefaultSurfaceConstructions(model)
        surface_defaults.setRoofCeilingConstruction(default_construction)
        construction_set = self.os.model.DefaultConstructionSet(model)
        construction_set.setDefaultExteriorSurfaceConstructions(
            surface_defaults)
        model.getBuilding().setDefaultConstructionSet(construction_set)

        # The replacement: one pitched face covering the whole 4x4
        # footprint, not just the fragment's 2x2 corner -- not coplanar
        # with the flat fragment, so this is the orphan/"plan" path, not a
        # key match.
        pitched = self.os.Point3dVector()
        for x, y, z in ((0.0, 0.0, 3.0), (4.0, 0.0, 3.5),
                        (4.0, 4.0, 3.5), (0.0, 4.0, 3.0)):
            pitched.append(self.os.Point3d(x, y, z))
        new_roof = self.os.model.Surface(pitched, model)
        new_roof.setSpace(space)
        new_roof.setSurfaceType("RoofCeiling")
        new_roof.setName("Pitched Roof")

        self.assertTrue(
            new_roof.construction().is_initialized(),
            "fixture must resolve a defaulted construction, or this test "
            "does not exercise the bug")
        self.assertTrue(new_roof.isConstructionDefaulted())

        report = self.si.restore(model, captured, {"Attic": "Attic"})

        self.assertEqual(report["inherited"], 1)
        self.assertTrue(new_roof.construction().is_initialized())
        self.assertEqual(new_roof.construction().get().nameString(),
                         "Air Boundary - roof test")


if __name__ == "__main__":
    unittest.main()
