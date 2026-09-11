# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the roof: pitched ceilings, attics, and what they must not break.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

Two halves, because the roof spans both interpreters.

The FreeCAD half is stubbed the way the other fc_* tests are stubbed.  What is
worth pinning down there is the winding, and only the winding: OpenStudio
derives Floor / Wall / RoofCeiling from a surface's outward normal, and takes a
face that points the wrong way without a word -- the space simply comes out
with a negative volume.  Nothing downstream notices.  So the check that
matters is that the polygons about to be emitted enclose the volume the solid
says they should, and that is what polyhedron_volume is for.  Trusting OCC's
per-face Orientation flag instead is not hypothetical: it typed the floor and
the ceiling of three rooms as walls, and FreeCAD's normalAt turns out to have
applied it already.

The model half is real openstudio.  The point there is that adding the roof
must not disturb anything that was already working: a flat room has to hash
exactly as it did before this existed, or the first update after upgrading
would rebuild all 37 spaces and take their HVAC with it.
"""

import math
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------
# FreeCAD stubs -- fc_roof and fcbridge both import FreeCAD at module scope
# --------------------------------------------------------------------------

class Vector(object):
    """Enough of FreeCAD.Vector for the geometry under test."""

    def __init__(self, x=0.0, y=0.0, z=0.0):
        if isinstance(x, Vector):
            x, y, z = x.x, x.y, x.z
        self.x, self.y, self.z = float(x), float(y), float(z)

    @property
    def Length(self):
        return math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2)

    def __sub__(self, other):
        return Vector(self.x - other.x, self.y - other.y, self.z - other.z)

    def __add__(self, other):
        return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def __mul__(self, scale):
        return Vector(self.x * scale, self.y * scale, self.z * scale)

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return Vector(self.y * other.z - self.z * other.y,
                      self.z * other.x - self.x * other.z,
                      self.x * other.y - self.y * other.x)

    def normalize(self):
        length = self.Length
        if length:
            self.x, self.y, self.z = (self.x / length, self.y / length,
                                      self.z / length)
        return self

    def __repr__(self):
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)


_freecad = sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))
_freecad.Vector = Vector
sys.modules.setdefault("Part", types.ModuleType("Part"))
_bop = sys.modules.setdefault("BOPTools", types.ModuleType("BOPTools"))
if not hasattr(_bop, "SplitAPI"):
    _bop.SplitAPI = types.SimpleNamespace()

import fc_roof  # noqa: E402


def box(x0=0.0, x1=2.0, y0=0.0, y1=3.0, z0=0.0, z1=4.0):
    """The six faces of a box, each wound so its normal points outward."""
    def face(*points):
        return [Vector(*p) for p in points]

    return [
        face((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)),   # -z
        face((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),   # +z
        face((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),   # -y
        face((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),   # +y
        face((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),   # +x
        face((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),   # -x
    ]


class VolumeTests(unittest.TestCase):
    """The one check that covers the whole FreeCAD-to-model conversion."""

    def test_outward_faces_enclose_a_positive_volume(self):
        self.assertAlmostEqual(fc_roof.polyhedron_volume(box()), 24.0, 9)

    def test_inward_faces_enclose_a_negative_one(self):
        flipped = [list(reversed(face)) for face in box()]
        self.assertAlmostEqual(fc_roof.polyhedron_volume(flipped), -24.0, 9)

    def test_one_flipped_face_is_neither(self):
        """The failure the check exists for: a single face pointing in.

        A global sign error is recoverable -- reverse everything.  One face
        out of step is a defect, and it shows up as a volume that is not the
        solid's and not minus the solid's either.
        """
        faces = box()
        faces[1] = list(reversed(faces[1]))
        volume = fc_roof.polyhedron_volume(faces)
        self.assertNotAlmostEqual(volume, 24.0, 6)
        self.assertNotAlmostEqual(volume, -24.0, 6)

    def test_a_missing_face_is_caught_too(self):
        for dropped in range(6):
            faces = box()
            del faces[dropped]
            self.assertNotAlmostEqual(fc_roof.polyhedron_volume(faces),
                                      24.0, 6,
                                      "dropping face %d went unnoticed"
                                      % dropped)

    def test_a_face_through_the_model_origin_is_not_a_blind_spot(self):
        """The regression that made the reference point matter.

        Summed from the origin, a face lying in a plane through it adds
        nothing, so a box cornered on (0, 0, 0) could lose its x = 0 wall,
        its y = 0 wall or its ground floor and still measure full.  The
        building's own origin is exactly such a corner.
        """
        for dropped in (0, 2, 5):       # the -z, -y and -x faces
            faces = box(x0=0.0, y0=0.0, z0=0.0)
            del faces[dropped]
            self.assertNotAlmostEqual(fc_roof.polyhedron_volume(faces),
                                      24.0, 6)

    def test_a_wedge(self):
        """A shed roof: the shape the Attic method actually produces.

        4 m x 2 m on plan, 1 m tall at y = 0 and coming to an edge at y = 2,
        so 4 m3 -- and with a gable end that is a triangle, not a quad.
        """
        def face(*points):
            return [Vector(*p) for p in points]

        wedge = [
            face((0, 0, 0), (0, 2, 0), (4, 2, 0), (4, 0, 0)),        # floor
            face((0, 0, 0), (4, 0, 0), (4, 0, 1), (0, 0, 1)),        # -y
            face((0, 0, 1), (4, 0, 1), (4, 2, 0), (0, 2, 0)),        # roof
            face((4, 0, 0), (4, 2, 0), (4, 0, 1)),                   # +x gable
            face((0, 0, 0), (0, 0, 1), (0, 2, 0)),                   # -x gable
        ]
        self.assertAlmostEqual(fc_roof.polyhedron_volume(wedge), 4.0, 9)


class ClassifyTests(unittest.TestCase):

    def test_a_flat_ceiling(self):
        self.assertEqual(fc_roof.classify(Vector(0, 0, 1)), "RoofCeiling")

    def test_a_flat_floor(self):
        self.assertEqual(fc_roof.classify(Vector(0, 0, -1)), "Floor")

    def test_a_wall(self):
        self.assertEqual(fc_roof.classify(Vector(1, 0, 0)), "Wall")

    def test_a_shallow_roof_is_still_a_roof(self):
        """1.4 degrees, the pitch on the test building."""
        self.assertEqual(fc_roof.classify(Vector(0, 0.0416, 0.9991)),
                         "RoofCeiling")

    def test_a_steep_roof_is_still_a_roof(self):
        """A 12:12 gable sits at 45 degrees, right on the boundary."""
        root = math.sqrt(0.5)
        self.assertEqual(fc_roof.classify(Vector(0, -root, root + 1e-6)),
                         "RoofCeiling")

    def test_a_near_vertical_face_is_a_wall(self):
        self.assertEqual(fc_roof.classify(Vector(0, 0.99, 0.1)), "Wall")

    def test_newell_matches_the_winding(self):
        floor, ceiling = box()[0], box()[1]
        self.assertLess(fc_roof.newell(floor).z, 0)
        self.assertGreater(fc_roof.newell(ceiling).z, 0)


class NamingTests(unittest.TestCase):
    """An attic is a room, so it needs a room's identity."""

    class Obj(object):
        def __init__(self, label, name="Body", space_name=None):
            self.Label, self.Name = label, name
            if space_name is not None:
                self.OS_SpaceName = space_name

    def test_the_freecad_copy_suffix_is_not_part_of_the_name(self):
        self.assertEqual(
            fc_roof.derived_space_name(self.Obj("Attic-001")), "Attic")

    def test_a_label_that_ends_in_a_digit_on_purpose_survives(self):
        self.assertEqual(
            fc_roof.derived_space_name(self.Obj("Roof")), "Roof")

    def test_an_all_digit_label_falls_back_to_the_object_name(self):
        self.assertEqual(
            fc_roof.derived_space_name(self.Obj("001", "Pad")), "Pad")

    def test_identity_defaults_to_the_label(self):
        self.assertEqual(fc_roof.space_identity(self.Obj("Attic-001")),
                         ("", "Attic"))

    def test_identity_parses_number_and_name(self):
        self.assertEqual(
            fc_roof.space_identity(self.Obj("Attic-001",
                                            space_name="601 | Attic")),
            ("601", "Attic"))

    def test_a_name_with_no_separator_is_all_name(self):
        self.assertEqual(
            fc_roof.space_identity(self.Obj("Attic-001",
                                            space_name="Roof Plenum")),
            ("", "Roof Plenum"))


class CandidateTests(unittest.TestCase):
    """Which spaces the Extend method reaches for."""

    def test_a_space_takes_the_story_height(self):
        self.assertAlmostEqual(
            fc_roof.space_top_m({}, {"elevation_m": 3.38,
                                     "floor_to_floor_m": 2.71}), 6.09, 9)

    def test_an_override_wins(self):
        self.assertAlmostEqual(
            fc_roof.space_top_m({"height_m": 6.09},
                                {"elevation_m": 0.0,
                                 "floor_to_floor_m": 3.38}), 6.09, 9)

    def test_a_zero_override_defers(self):
        self.assertAlmostEqual(
            fc_roof.space_top_m({"height_m": 0},
                                {"elevation_m": 0.0,
                                 "floor_to_floor_m": 3.38}), 3.38, 9)


class AtticBaseTests(unittest.TestCase):
    """Where an attic's floor has to sit for the building to close.

    The failure this prevents is silent and expensive: the attic was traced
    2 mm below the ceilings it covers, matchSurfaces paired nothing, and
    OpenStudio quietly typed 923 m2 of attic floor as Ground six metres up
    while every room below kept its Outdoors roof -- the envelope counted
    twice, with no warning from anything.
    """

    TOPS = [3.38, 6.09]

    def test_a_floor_already_on_a_ceiling_is_left_alone(self):
        target, delta, reaches = fc_roof.attic_base(6.09, self.TOPS)
        self.assertEqual(target, 6.09)
        self.assertEqual(delta, 0.0)
        self.assertTrue(reaches)

    def test_the_two_millimetre_miss_is_snapped_up(self):
        target, delta, reaches = fc_roof.attic_base(6.088, self.TOPS)
        self.assertEqual(target, 6.09)
        self.assertAlmostEqual(delta, 0.002, 9)
        self.assertTrue(reaches)

    def test_an_attic_drawn_high_is_snapped_down(self):
        _target, delta, reaches = fc_roof.attic_base(6.11, self.TOPS)
        self.assertAlmostEqual(delta, -0.02, 9)
        self.assertTrue(reaches)

    def test_it_snaps_to_the_nearest_storey_not_the_highest(self):
        target, _delta, reaches = fc_roof.attic_base(3.4, self.TOPS)
        self.assertEqual(target, 3.38)
        self.assertTrue(reaches)

    def test_a_real_gap_is_reported_rather_than_snapped(self):
        """Half a metre is a modelling decision, not a drafting slip."""
        target, delta, reaches = fc_roof.attic_base(6.6, self.TOPS)
        self.assertEqual(target, 6.09)
        self.assertAlmostEqual(delta, -0.51, 9)
        self.assertFalse(reaches)

    def test_the_tolerance_boundary_is_inclusive(self):
        _t, _d, reaches = fc_roof.attic_base(6.09 + fc_roof.BASE_TOL_M,
                                             self.TOPS)
        self.assertTrue(reaches)

    def test_a_plan_with_no_storeys_snaps_to_nothing(self):
        target, delta, reaches = fc_roof.attic_base(6.09, [])
        self.assertIsNone(target)
        self.assertEqual(delta, 0.0)
        self.assertFalse(reaches)


class OutlineDriftTests(unittest.TestCase):
    """The guard on re-importing walls whose shape changed.

    An opening is traced by snapping to the external geometry of an imported
    wall, so it is constrained to it.  Change the wall -- which is exactly
    what a roof does -- and re-import, and the constraints drag the outlines
    with them.  Measured on the test building: 20 of 27 outlines moved, four
    of them by more than 80 m.  Nothing downstream would have questioned it;
    the openings would simply have been applied where they had been dragged.
    """

    @classmethod
    def setUpClass(cls):
        import fc_import_surfaces
        cls.fis = fc_import_surfaces

    @staticmethod
    def at(x, y, z, extent=(1500.0, 0.0, 2000.0), label="Openings South"):
        """Positions are in millimetres, as they are in the document."""
        return (Vector(x, y, z), extent, label)

    def test_an_untouched_import_reports_nothing(self):
        before = {"a": self.at(1000, 0, 1000), "b": self.at(5000, 0, 1000)}
        self.assertEqual(self.fis.outline_drift(before, dict(before)), [])

    def test_a_moved_outline_is_reported_with_the_distance(self):
        before = {"a": self.at(1000, 0, 1000)}
        after = {"a": self.at(1000, 0, 4300)}
        drift = self.fis.outline_drift(before, after)
        self.assertEqual(len(drift), 1)
        self.assertAlmostEqual(drift[0][2], 3300.0, 6)

    def test_a_resized_outline_is_reported_even_if_it_did_not_move(self):
        before = {"a": self.at(1000, 0, 1000, extent=(1500.0, 0.0, 2000.0))}
        after = {"a": self.at(1000, 0, 1000, extent=(1500.0, 0.0, 2400.0))}
        drift = self.fis.outline_drift(before, after)
        self.assertEqual(len(drift), 1)
        self.assertAlmostEqual(drift[0][3], 400.0, 6)

    def test_a_sub_millimetre_shift_is_not_drift(self):
        """Rebuilding a face moves a vertex in the last decimal place."""
        before = {"a": self.at(1000, 0, 1000)}
        after = {"a": self.at(1000.0004, 0, 1000)}
        self.assertEqual(self.fis.outline_drift(before, after), [])

    def test_an_outline_with_no_prior_position_is_not_reported(self):
        """A newly drawn opening has nothing to have moved from."""
        self.assertEqual(
            self.fis.outline_drift({}, {"a": self.at(1000, 0, 1000)}), [])


# --------------------------------------------------------------------------
# the model side -- real openstudio
# --------------------------------------------------------------------------

def rect(x0=0.0, x1=4.0, y0=0.0, y1=3.0, z=0.0):
    return [[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]]


class BuilderTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import build_osm_geometry
            import openstudio
        except ImportError as exc:      # pragma: no cover - venv not set up
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.bog = build_osm_geometry
        cls.os = openstudio

    def wedge_solid(self):
        """A 4 x 3 room, 2 m at y=0 sloping to 3 m at y=3."""
        return {"source": "roof-extend", "surfaces": [
            {"type": "Floor",
             "vertices": [[0, 0, 0], [0, 3, 0], [4, 3, 0], [4, 0, 0]]},
            {"type": "RoofCeiling",
             "vertices": [[0, 0, 2], [4, 0, 2], [4, 3, 3], [0, 3, 3]]},
            {"type": "Wall",
             "vertices": [[0, 0, 0], [4, 0, 0], [4, 0, 2], [0, 0, 2]]},
            {"type": "Wall",
             "vertices": [[4, 3, 0], [0, 3, 0], [0, 3, 3], [4, 3, 3]]},
            {"type": "Wall",
             "vertices": [[4, 0, 0], [4, 3, 0], [4, 3, 3], [4, 0, 2]]},
            {"type": "Wall",
             "vertices": [[0, 3, 0], [0, 0, 0], [0, 0, 2], [0, 3, 3]]},
        ]}

    def test_a_space_from_a_solid_has_the_volume_it_was_given(self):
        model = self.os.model.Model()
        space, mismatches = self.bog.space_from_solid(model, self.wedge_solid())
        self.assertEqual(mismatches, [])
        self.assertAlmostEqual(space.volume(), 4 * 3 * 2.5, 6)
        self.assertAlmostEqual(space.floorArea(), 12.0, 6)

    def test_openstudio_agrees_with_the_types_freecad_recorded(self):
        model = self.os.model.Model()
        space, _ = self.bog.space_from_solid(model, self.wedge_solid())
        kinds = sorted(s.surfaceType() for s in space.surfaces())
        self.assertEqual(kinds, ["Floor", "RoofCeiling", "Wall", "Wall",
                                 "Wall", "Wall"])

    def test_a_flipped_face_is_reported_not_absorbed(self):
        """The silent failure, made loud.

        Reversing one face leaves geometry OpenStudio accepts; only the type
        it derives gives it away.  Without the comparison this reaches the
        model as a room with a floor on the ceiling.
        """
        solid = self.wedge_solid()
        solid["surfaces"][0]["vertices"].reverse()
        model = self.os.model.Model()
        _space, mismatches = self.bog.space_from_solid(model, solid)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0][1:], ("Floor", "RoofCeiling"))

    def test_without_a_solid_a_space_is_still_a_prism(self):
        model = self.os.model.Model()
        space, mismatches = self.bog.create_space(
            model, {"vertices": [v[:2] for v in rect()], "name": "Office"},
            {"elevation_m": 0.0, "floor_to_floor_m": 3.0})
        self.assertEqual(mismatches, [])
        self.assertAlmostEqual(space.volume(), 36.0, 6)
        self.assertEqual(len(space.surfaces()), 6)


class RemovalTests(unittest.TestCase):
    """Deleting a space must not leave its neighbours facing nothing.

    OpenStudio clears the adjacent pointer when the partner surface goes, but
    leaves the boundary condition reading "Surface" -- a surface that claims
    to face another surface and names none.  EnergyPlus has no wall to put
    there and OpenStudio never objects.  Removing the attic left 33 ceilings
    and 923 m2 in that state, and the run reported "re-matched 0 space(s)"
    because the re-match set is built from rebuilt spaces and a removal
    contributes none.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import openstudio
            import update_osm_geometry
        except ImportError as exc:      # pragma: no cover
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.os = openstudio
        cls.uog = update_osm_geometry

    def stacked(self):
        """A room with a second one sitting on its ceiling, matched."""
        model = self.os.model.Model()
        points = self.os.Point3dVector()
        for x, y in ((0.0, 0.0), (0.0, 4.0), (6.0, 4.0), (6.0, 0.0)):
            points.append(self.os.Point3d(x, y, 0.0))
        lower = self.os.model.Space.fromFloorPrint(points, 3.0, model).get()
        lower.setName("Lower")
        upper = self.os.model.Space.fromFloorPrint(points, 2.0, model).get()
        upper.setName("Upper")
        upper.setZOrigin(3.0)
        spaces = self.os.model.SpaceVector()
        spaces.append(lower)
        spaces.append(upper)
        self.os.model.matchSurfaces(spaces)
        return model, lower, upper

    def ceiling_of(self, space):
        return next(s for s in space.surfaces()
                    if s.surfaceType() == "RoofCeiling")

    def test_the_fixture_really_is_matched(self):
        _model, lower, _upper = self.stacked()
        ceiling = self.ceiling_of(lower)
        self.assertEqual(ceiling.outsideBoundaryCondition(), "Surface")
        self.assertTrue(ceiling.adjacentSurface().is_initialized())

    def test_removing_the_space_above_leaves_a_dangling_ceiling(self):
        """The defect itself, so the repair is not testing a no-op."""
        _model, lower, upper = self.stacked()
        upper.remove()
        ceiling = self.ceiling_of(lower)
        self.assertEqual(ceiling.outsideBoundaryCondition(), "Surface")
        self.assertFalse(ceiling.adjacentSurface().is_initialized())

    def test_the_repair_puts_the_ceiling_back_outdoors(self):
        model, lower, upper = self.stacked()
        exposed = [s.adjacentSurface().get() for s in upper.surfaces()
                   if s.adjacentSurface().is_initialized()]
        upper.remove()
        repaired = self.uog.repair_exposed(exposed, model, [])
        self.assertEqual(len(repaired), 1)
        ceiling = self.ceiling_of(lower)
        self.assertEqual(ceiling.outsideBoundaryCondition(), "Outdoors")
        self.assertEqual(ceiling.sunExposure(), "SunExposed")
        self.assertEqual(ceiling.windExposure(), "WindExposed")

    def test_a_still_matched_surface_is_left_alone(self):
        model, lower, _upper = self.stacked()
        ceiling = self.ceiling_of(lower)
        self.assertEqual(self.uog.repair_exposed([ceiling], model, []), [])
        self.assertEqual(ceiling.outsideBoundaryCondition(), "Surface")

    def test_a_surface_deleted_alongside_is_skipped(self):
        """Both sides can go at once; the repair must not touch a dead handle."""
        model, lower, upper = self.stacked()
        exposed = [s.adjacentSurface().get() for s in upper.surfaces()
                   if s.adjacentSurface().is_initialized()]
        upper.remove()
        lower.remove()
        self.assertEqual(self.uog.repair_exposed(exposed, model, []), [])

    def test_a_floor_goes_back_to_ground_not_outdoors(self):
        """The upper space keeps a floor; it must not become Outdoors."""
        model, lower, upper = self.stacked()
        exposed = [s.adjacentSurface().get() for s in lower.surfaces()
                   if s.adjacentSurface().is_initialized()]
        lower.remove()
        self.uog.repair_exposed(exposed, model, [])
        floor = next(s for s in upper.surfaces()
                     if s.surfaceType() == "Floor")
        self.assertEqual(floor.outsideBoundaryCondition(), "Ground")


class HashTests(unittest.TestCase):
    """Adding the roof must not make every existing space look changed."""

    @classmethod
    def setUpClass(cls):
        try:
            import build_osm_geometry
        except ImportError as exc:      # pragma: no cover
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.bog = build_osm_geometry
        cls.story = {"elevation_m": 3.38, "floor_to_floor_m": 2.71}
        cls.space = {"vertices": [v[:2] for v in rect()]}

    def test_a_flat_space_hashes_as_it_did_before_the_roof_existed(self):
        """Pinned to the literal value the previous version produced.

        If this changes, the next `update` reports all 37 spaces as changed
        and rebuilds them -- which is how a thermal zone loses its HVAC.
        """
        self.assertEqual(self.bog.vertex_hash(self.space, self.story),
                         "594bf7cba22f4cc7")

    def test_a_roofed_space_hashes_differently(self):
        roofed = dict(self.space, solid={"source": "roof-extend", "surfaces": [
            {"type": "Floor", "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0]]}]})
        self.assertNotEqual(self.bog.vertex_hash(roofed, self.story),
                            self.bog.vertex_hash(self.space, self.story))

    def test_repitching_the_roof_changes_the_hash(self):
        low = dict(self.space, solid={"source": "roof-extend", "surfaces": [
            {"type": "RoofCeiling",
             "vertices": [[0, 0, 6.0], [1, 0, 6.0], [1, 1, 6.2]]}]})
        high = dict(self.space, solid={"source": "roof-extend", "surfaces": [
            {"type": "RoofCeiling",
             "vertices": [[0, 0, 6.0], [1, 0, 6.0], [1, 1, 6.8]]}]})
        self.assertNotEqual(self.bog.vertex_hash(low, self.story),
                            self.bog.vertex_hash(high, self.story))


class DuplicateTests(unittest.TestCase):
    """intersectSurfaces can emit the same patch twice."""

    @classmethod
    def setUpClass(cls):
        try:
            import build_osm_geometry
            import openstudio
        except ImportError as exc:      # pragma: no cover
            raise unittest.SkipTest("openstudio not importable: %s" % exc)
        cls.bog = build_osm_geometry
        cls.os = openstudio

    def prism(self, model, name, z0=0.0, z1=3.0):
        pts = self.os.Point3dVector()
        for x, y, _ in reversed(rect()):
            pts.append(self.os.Point3d(x, y, z0))
        space = self.os.model.Space.fromFloorPrint(pts, z1 - z0, model).get()
        space.setName(name)
        return space

    def test_a_clean_model_loses_nothing(self):
        model = self.os.model.Model()
        self.prism(model, "Office")
        self.assertEqual(self.bog.drop_duplicate_surfaces(model), [])
        self.assertEqual(len(model.getSurfaces()), 6)

    def test_the_orphan_copy_goes_and_the_matched_one_stays(self):
        model = self.os.model.Model()
        lower = self.prism(model, "Office")
        upper = self.prism(model, "Attic", 3.0, 5.0)

        spaces = self.os.model.SpaceVector()
        for space in (lower, upper):
            spaces.append(space)
        self.os.model.matchSurfaces(spaces)

        floor = next(s for s in upper.surfaces()
                     if s.surfaceType() == "Floor")
        self.assertTrue(floor.adjacentSurface().is_initialized())

        twin = self.os.model.Surface(floor.vertices(), model)
        twin.setSpace(upper)
        self.assertEqual(len(upper.surfaces()), 7)

        removed = self.bog.drop_duplicate_surfaces(model)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0][0], "Attic")
        self.assertEqual(len(upper.surfaces()), 6)
        survivor = next(s for s in upper.surfaces()
                        if s.surfaceType() == "Floor")
        self.assertTrue(survivor.adjacentSurface().is_initialized())

    def test_two_spaces_sharing_an_outline_are_left_alone(self):
        """Coincident surfaces in *different* spaces are how matching works."""
        model = self.os.model.Model()
        self.prism(model, "Office")
        self.prism(model, "Attic", 3.0, 5.0)
        self.assertEqual(self.bog.drop_duplicate_surfaces(model), [])


if __name__ == "__main__":
    unittest.main()
