# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the canopy path: fc_export_shading's geometry, apply_shading's model.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

Two things here are worth pinning down because neither fails loudly.

**Which way a shade faces.** A canopy wound the wrong way still shades, so a
sign error would survive every area check and only show up as a plate drawn
from underneath in the OpenStudio App.

**Which objects count as drawn.** A FreeCAD group reports a `Shape` that is a
Compound of its children, so the group `import` puts the model's own canopies
in -- "Shading from the model" -- matched the SHADING keyword and handed all
four back a second time.  The model went from 4 shades to 8 with no error
anywhere.  That is what is_drawable exists to stop.

fc_export_shading imports FreeCAD at module scope and the venv has no FreeCAD,
so FreeCAD.Vector is stubbed with a real one -- the arithmetic under test is
its own, not OCC's.
"""

import math
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Vector(object):
    """Just enough of FreeCAD.Vector for the polygon maths under test."""

    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    @property
    def Length(self):
        return math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2)

    def normalize(self):
        length = self.Length
        if length:
            self.x, self.y, self.z = (self.x / length, self.y / length,
                                      self.z / length)
        return self

    def negative(self):
        return Vector(-self.x, -self.y, -self.z)

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return Vector(self.y * other.z - self.z * other.y,
                      self.z * other.x - self.x * other.z,
                      self.x * other.y - self.y * other.x)

    def __add__(self, other):
        return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def __iadd__(self, other):
        return self.__add__(other)

    def __eq__(self, other):
        return (self.x, self.y, self.z) == (other.x, other.y, other.z)

    def __repr__(self):                       # pragma: no cover - debugging
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)


# setdefault, then attach: another test module in this run may already have
# registered a bare FreeCAD stub, and whichever of us goes first, the module
# that ends up in sys.modules has to carry Vector.
_freecad = sys.modules.setdefault("FreeCAD", types.ModuleType("FreeCAD"))
_freecad.Vector = Vector
sys.modules.setdefault("Part", types.ModuleType("Part"))
_bop = types.ModuleType("BOPTools")
_bop.SplitAPI = types.SimpleNamespace()
sys.modules.setdefault("BOPTools", _bop)

import fcbridge  # noqa: E402
import fc_export_shading as fx  # noqa: E402

# A 4 m x 1.5 m canopy at 2.9 m, wound counter-clockwise seen from above.
CANOPY = [Vector(0, 0, 2900), Vector(0, -1500, 2900),
          Vector(4000, -1500, 2900), Vector(4000, 0, 2900)]

SOUTH = Vector(0, -1, 0)          # outward normal of a south-facing wall


class Stub(object):
    """A document object with only the attributes the exporter reads."""

    def __init__(self, label, type_id="Sketcher::SketchObject", **props):
        self.Label = label
        self.TypeId = type_id
        self.__dict__.update(props)

    def isDerivedFrom(self, kind):
        return kind == "Part::Feature" and self.TypeId.startswith("Part::")


class LabelTests(unittest.TestCase):

    def test_keyword_gives_kind_and_suffix(self):
        self.assertEqual(fx.classify_label("CANOPY South entry"),
                         ("Canopy", "South entry"))

    def test_matching_is_case_insensitive(self):
        self.assertEqual(fx.classify_label("Shading Sketch")[0], "Shading")

    def test_shade_and_shading_agree(self):
        self.assertEqual(fx.classify_label("SHADE x")[0],
                         fx.classify_label("SHADING x")[0])

    def test_overhang_wins_over_nothing_else(self):
        self.assertEqual(fx.classify_label("OVERHANG 1")[0], "Overhang")

    def test_unrelated_label_is_not_a_shade(self):
        self.assertEqual(fx.classify_label("Openings South"), (None, ""))

    def test_keyword_anywhere_in_the_label_still_matches(self):
        self.assertEqual(fx.classify_label("Parapet Shading"),
                         ("Shading", ""))

    def test_keyword_as_a_substring_of_another_word_does_not_match(self):
        # The anywhere-in-the-label fallback is whole-word: FIN must not fire
        # on FINISH, nor SHADE on SHADED, when the keyword is not the prefix
        # (a prefix match, e.g. 'Shaded Courtyard', is a separate, pre-existing
        # rule this change does not touch).
        self.assertEqual(fx.classify_label("Roof Finish Detail"), (None, ""))
        self.assertEqual(fx.classify_label("South Shaded Zone"), (None, ""))

    def test_empty_label_is_safe(self):
        self.assertEqual(fx.classify_label(""), (None, ""))


class DrawableTests(unittest.TestCase):
    """The regression that doubled the canopies."""

    def test_a_group_is_not_drawable(self):
        group = Stub("Shading from the model", "App::DocumentObjectGroup")
        self.assertTrue(fx.classify_label(group.Label)[0])
        self.assertFalse(fx.is_drawable(group))

    def test_a_sketch_is_drawable(self):
        self.assertTrue(fx.is_drawable(Stub("Shading Sketch")))

    def test_a_part_feature_is_drawable(self):
        self.assertTrue(fx.is_drawable(Stub("CANOPY 1", "Part::Feature")))

    def test_imported_shading_is_reference(self):
        obj = Stub("Shading South 01", "Part::Feature",
                   OS_SurfaceName="Shading South 01", OS_ShadingSurface=True)
        self.assertTrue(fx.is_reference(obj))

    def test_imported_surface_is_reference(self):
        self.assertTrue(fx.is_reference(
            Stub("Surface 5", "Part::Feature", OS_SurfaceName="Surface 5")))

    def test_a_drawn_sketch_is_not_reference(self):
        self.assertFalse(fx.is_reference(Stub("Shading Sketch")))


class GeometryTests(unittest.TestCase):

    def test_normal_of_a_ccw_canopy_points_up(self):
        self.assertAlmostEqual(fx.polygon_normal(CANOPY).z, 1.0)

    def test_normal_of_a_cw_canopy_points_down(self):
        self.assertAlmostEqual(
            fx.polygon_normal(list(reversed(CANOPY))).z, -1.0)

    def test_degenerate_loop_has_no_normal(self):
        line = [Vector(0, 0, 0), Vector(1000, 0, 0), Vector(2000, 0, 0)]
        self.assertIsNone(fx.polygon_normal(line))

    def test_area_is_in_square_metres(self):
        normal = fx.polygon_normal(CANOPY)
        self.assertAlmostEqual(fx.polygon_area_m2(CANOPY, normal), 6.0, 6)

    def test_area_does_not_depend_on_winding(self):
        loop = list(reversed(CANOPY))
        self.assertAlmostEqual(
            fx.polygon_area_m2(loop, fx.polygon_normal(loop)), 6.0, 6)

    def test_area_does_not_depend_on_distance_from_the_origin(self):
        moved = [Vector(p.x + 90000, p.y + 40000, p.z) for p in CANOPY]
        normal = fx.polygon_normal(moved)
        self.assertAlmostEqual(fx.polygon_area_m2(moved, normal), 6.0, 6)


class OrientTests(unittest.TestCase):

    def test_an_upward_canopy_is_left_alone(self):
        normal = fx.polygon_normal(CANOPY)
        points, out = fx.orient(CANOPY, normal, SOUTH)
        self.assertEqual(points, CANOPY)
        self.assertAlmostEqual(out.z, 1.0)

    def test_a_downward_canopy_is_reversed(self):
        loop = list(reversed(CANOPY))
        points, out = fx.orient(loop, fx.polygon_normal(loop), SOUTH)
        self.assertEqual(points, list(reversed(loop)))
        self.assertAlmostEqual(out.z, 1.0)

    def test_a_fin_faces_away_from_its_wall(self):
        # Vertical plate in the x = 0 plane, wound so its normal is +x.
        fin = [Vector(0, 0, 0), Vector(0, 0, 3000),
               Vector(0, -1000, 3000), Vector(0, -1000, 0)]
        normal = fx.polygon_normal(fin)
        self.assertAlmostEqual(abs(normal.z), 0.0)
        west = Vector(-1, 0, 0)
        _, out = fx.orient(fin, normal, west)
        self.assertGreater(out.dot(west), 0.0)

    def test_a_fin_with_no_wall_keeps_its_drawn_winding(self):
        fin = [Vector(0, 0, 0), Vector(0, 0, 3000),
               Vector(0, -1000, 3000), Vector(0, -1000, 0)]
        normal = fx.polygon_normal(fin)
        points, out = fx.orient(fin, normal, None)
        self.assertEqual(points, fin)
        self.assertAlmostEqual(out.dot(normal), 1.0)


class FacadeNameTests(unittest.TestCase):
    """Shared with the openings exporter, so a canopy and the door beneath
    it are named by one rule."""

    def test_south_wall_names_a_south_canopy(self):
        self.assertEqual(fcbridge.compass_of(SOUTH), "South")

    def test_every_cardinal_direction(self):
        self.assertEqual(
            [fcbridge.compass_of(v) for v in
             (Vector(0, 1, 0), Vector(1, 0, 0),
              Vector(0, -1, 0), Vector(-1, 0, 0))],
            ["North", "East", "South", "West"])

    def test_tilt_does_not_change_the_bearing(self):
        self.assertEqual(fcbridge.compass_of(Vector(0, -1, -0.3)), "South")


class _Stub(object):
    def __init__(self, text):
        self._text = text

    def comment(self):
        return self._text


class ApplyShadingTests(unittest.TestCase):
    """The model half, against the real OpenStudio SDK."""

    @classmethod
    def setUpClass(cls):
        try:
            import openstudio  # noqa: F401
            import apply_shading  # noqa: F401
        except ImportError as exc:       # pragma: no cover - venv not set up
            raise unittest.SkipTest("openstudio not importable: %s" % exc)

    def setUp(self):
        import openstudio
        import apply_shading
        self.os = openstudio
        self.mod = apply_shading
        self.model = openstudio.model.Model()

    def add(self, name, vertices, group_type="Building", groups=None,
            shading_id=""):
        group = self.mod.get_group(self.model, group_type,
                                   groups if groups is not None else {})
        pts = self.mod.to_group_frame(group, vertices)
        shade = self.os.model.ShadingSurface(pts, self.model)
        shade.setName(name)
        shade.setShadingSurfaceGroup(group)
        shade.setComment(self.mod.comment_for(shading_id))
        return group, shade

    def sweep(self):
        """What apply_shading does to shades the drawing no longer has."""
        keyed, unkeyed = self.mod.ours(self.model)
        for shade in list(keyed.values()) + unkeyed:
            shade.remove()
        return len(keyed) + len(unkeyed)

    def test_vertices_survive_the_group_frame(self):
        want = [[0.0, 0.0, 2.9], [0.0, -1.5, 2.9],
                [4.0, -1.5, 2.9], [4.0, 0.0, 2.9]]
        group, shade = self.add("Canopy South 01", want)
        got = group.transformation() * shade.vertices()
        for point, expected in zip(got, want):
            self.assertAlmostEqual(point.x(), expected[0], 9)
            self.assertAlmostEqual(point.y(), expected[1], 9)
            self.assertAlmostEqual(point.z(), expected[2], 9)

    def test_area_matches_what_was_drawn(self):
        _, shade = self.add("Canopy South 01",
                            [[0.0, 0.0, 2.9], [0.0, -1.5, 2.9],
                             [4.0, -1.5, 2.9], [4.0, 0.0, 2.9]])
        self.assertAlmostEqual(shade.grossArea(), 6.0, 6)

    def test_building_group_rotates_with_the_north_axis(self):
        """The reason an attached canopy is Building and not Site."""
        self.model.getBuilding().setNorthAxis(45.0)
        group, shade = self.add("Canopy South 01",
                                [[0.0, 0.0, 2.9], [0.0, -1.5, 2.9],
                                 [4.0, -1.5, 2.9], [4.0, 0.0, 2.9]])
        building = group.buildingTransformation() * shade.vertices()
        site = group.siteTransformation() * shade.vertices()
        self.assertAlmostEqual(building[0].x(), 0.0, 9)
        self.assertNotAlmostEqual(site[2].x(), building[2].x(), 3)

    def test_group_is_reused_not_duplicated(self):
        groups = {}
        self.add("A", [[0, 0, 3], [0, -1, 3], [1, -1, 3]], groups=groups)
        self.add("B", [[2, 0, 3], [2, -1, 3], [3, -1, 3]], groups=groups)
        self.assertEqual(len(self.model.getShadingSurfaceGroups()), 1)
        self.assertEqual(
            len(self.model.getShadingSurfaceGroups()[0].shadingSurfaces()), 2)

    def test_ours_takes_only_our_own(self):
        groups = {}
        self.add("ours", [[0, 0, 3], [0, -1, 3], [1, -1, 3]], groups=groups)
        group = self.mod.get_group(self.model, "Building", groups)
        theirs = self.os.model.ShadingSurface(
            self.mod.to_group_frame(group, [[5, 0, 3], [5, -1, 3], [6, -1, 3]]),
            self.model)
        theirs.setName("added by hand")
        theirs.setShadingSurfaceGroup(group)

        self.assertEqual(self.sweep(), 1)
        left = [s.nameString() for s in self.model.getShadingSurfaces()]
        self.assertEqual(left, ["added by hand"])

    def test_reapplying_does_not_stack(self):
        for _ in range(3):
            self.sweep()
            self.add("Canopy South 01",
                     [[0, 0, 3], [0, -1, 3], [1, -1, 3]], groups={})
        self.assertEqual(len(self.model.getShadingSurfaces()), 1)

    def test_empty_group_is_dropped_but_a_stranger_is_not(self):
        groups = {}
        self.add("ours", [[0, 0, 3], [0, -1, 3], [1, -1, 3]], groups=groups)
        stranger = self.os.model.ShadingSurfaceGroup(self.model)
        stranger.setName("Somebody Else's Shading")

        self.sweep()
        self.assertEqual(self.mod.drop_empty_groups(self.model), 1)
        names = [g.nameString() for g in self.model.getShadingSurfaceGroups()]
        self.assertEqual(names, ["Somebody Else's Shading"])

    def test_a_shade_keeps_its_handle_when_reshaped_in_place(self):
        """Why shades carry an id: a PV generator references the handle."""
        _, shade = self.add("Canopy South 01",
                            [[0, 0, 3], [0, -1, 3], [1, -1, 3]],
                            shading_id="sid-a")
        handle = str(shade.handle())
        pts = self.os.Point3dVector()
        for x, y in ((0, 0), (0, -1.5), (2, -1.5), (2, 0)):
            pts.append(self.os.Point3d(x, y, 3))
        self.assertTrue(shade.setVertices(pts))
        self.assertEqual(str(shade.handle()), handle)

    def test_ours_keys_by_id(self):
        self.add("Canopy South 01", [[0, 0, 3], [0, -1, 3], [1, -1, 3]],
                 shading_id="sid-a")
        keyed, unkeyed = self.mod.ours(self.model)
        self.assertEqual(sorted(keyed), ["sid-a"])
        self.assertEqual(unkeyed, [])

    def test_a_shade_whose_comment_was_stripped_is_adoptable(self):
        """Otherwise it would be duplicated: two canopies, double shading."""
        verts = [[0, 0, 3], [0, -1, 3], [1, -1, 3]]
        _, shade = self.add("Canopy South 01", verts, shading_id="sid-a")
        shade.setComment("")
        self.assertEqual(self.mod.ours(self.model), ({}, []))
        orphans = self.mod.adoptable(self.model)
        self.assertIn(self.mod.outline_key("Building", verts), orphans)

    def test_comment_round_trip(self):
        text = self.mod.comment_for("sid-a")
        self.assertEqual(self.mod.id_of(_Stub(text)), "sid-a")
        self.assertEqual(self.mod.id_of(_Stub("! " + text)), "sid-a")
        self.assertIsNone(self.mod.id_of(_Stub("placed by hand")))

    def test_site_and_building_get_separate_groups(self):
        groups = {}
        self.add("b", [[0, 0, 3], [0, -1, 3], [1, -1, 3]],
                 "Building", groups)
        self.add("s", [[9, 0, 3], [9, -1, 3], [10, -1, 3]], "Site", groups)
        types_ = sorted(g.shadingSurfaceType()
                        for g in self.model.getShadingSurfaceGroups())
        self.assertEqual(types_, ["Building", "Site"])


if __name__ == "__main__":
    unittest.main()
