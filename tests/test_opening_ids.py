# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the outline identity that keeps OpenStudio handles alive.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

apply_openings used to delete every subsurface it had made and rebuild them,
so **every** run minted new handles -- not just a retype.  Anything holding a
handle (a shading control, a frame and divider, an interzone pairing) pointed
at an object that no longer existed, silently.  An id minted into the sketch
lets apply match an opening it has seen before and update it in place instead.

Two things here bite quietly and so are pinned down:

**Name collisions.** OpenStudio makes names unique by suffixing.  Creating
"Door North 01" while last run's "Door North 01" is still in the model yields
"Door North 8" with no error at all -- which is exactly what happened on the
first run of this, across all 28.  Renaming survivors collides the same way,
because deleting one opening shifts every later number on its facade.  Hence
remove-then-park-then-name.

**Comment parsing**, since the comment is where the id lives.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CommentTests(unittest.TestCase):
    """apply_openings.id_of / comment_for, the model-side identity."""

    @classmethod
    def setUpClass(cls):
        try:
            import apply_openings  # noqa: F401
        except ImportError as exc:      # pragma: no cover - venv not set up
            raise unittest.SkipTest("openstudio not importable: %s" % exc)

    def setUp(self):
        import apply_openings
        self.mod = apply_openings

    class Stub(object):
        def __init__(self, text):
            self._text = text

        def comment(self):
            return self._text

    def test_round_trip(self):
        text = self.mod.comment_for("abc-123")
        self.assertEqual(self.mod.id_of(self.Stub(text)), "abc-123")

    def test_openstudio_prefixes_a_bang_on_load(self):
        """Comments come back from a saved model with '! ' in front."""
        text = "! " + self.mod.comment_for("abc-123")
        self.assertEqual(self.mod.id_of(self.Stub(text)), "abc-123")

    def test_no_id_yet(self):
        self.assertIsNone(self.mod.id_of(self.Stub(self.mod.MARKER)))

    def test_not_ours_at_all(self):
        self.assertIsNone(self.mod.id_of(self.Stub("added by hand")))
        self.assertIsNone(self.mod.id_of(self.Stub("")))

    def test_a_foreign_comment_mentioning_id_is_not_ours(self):
        self.assertIsNone(self.mod.id_of(self.Stub("some id:whatever")))

    def test_an_empty_id_reads_as_none(self):
        self.assertIsNone(self.mod.id_of(self.Stub(self.mod.MARKER + " id:")))

    def test_comment_without_an_id_is_just_the_marker(self):
        self.assertEqual(self.mod.comment_for(""), self.mod.MARKER)


class ApplyTests(unittest.TestCase):
    """The model half, against the real OpenStudio SDK."""

    @classmethod
    def setUpClass(cls):
        try:
            import openstudio  # noqa: F401
            import apply_openings  # noqa: F401
        except ImportError as exc:      # pragma: no cover - venv not set up
            raise unittest.SkipTest("openstudio not importable: %s" % exc)

    def setUp(self):
        import openstudio
        import apply_openings
        self.os = openstudio
        self.mod = apply_openings
        self.model = openstudio.model.Model()
        self.space = openstudio.model.Space(self.model)
        self.wall = self.make_wall(0)

    def make_wall(self, x0):
        pts = self.os.Point3dVector()
        for x, z in ((x0, 0), (x0 + 6, 0), (x0 + 6, 3), (x0, 3)):
            pts.append(self.os.Point3d(x, 0, z))
        surface = self.os.model.Surface(pts, self.model)
        surface.setSurfaceType("Wall")
        surface.setOutsideBoundaryCondition("Outdoors")
        surface.setSpace(self.space)
        return surface

    def add(self, name, opening_id, x0=1.0, kind="Door"):
        pts = self.os.Point3dVector()
        for x, z in ((x0, 0), (x0 + 1, 0), (x0 + 1, 2), (x0, 2)):
            pts.append(self.os.Point3d(x, 0, z))
        sub = self.os.model.SubSurface(pts, self.model)
        sub.setName(name)
        sub.setSurface(self.wall)
        sub.setSubSurfaceType(kind)
        sub.setComment(self.mod.comment_for(opening_id))
        return sub

    def test_ours_keys_by_id(self):
        self.add("Door South 01", "id-a")
        keyed, unkeyed = self.mod.ours(self.model)
        self.assertEqual(sorted(keyed), ["id-a"])
        self.assertEqual(unkeyed, [])

    def test_ours_separates_the_unidentified(self):
        sub = self.add("Door South 01", "")
        sub.setComment(self.mod.MARKER)
        keyed, unkeyed = self.mod.ours(self.model)
        self.assertEqual(keyed, {})
        self.assertEqual(len(unkeyed), 1)

    def test_ours_ignores_subsurfaces_it_did_not_make(self):
        sub = self.add("Door South 01", "id-a")
        sub.setComment("placed by hand")
        keyed, unkeyed = self.mod.ours(self.model)
        self.assertEqual((keyed, unkeyed), ({}, []))

    def test_the_collision_that_mangled_28_names(self):
        """Creating over a live name silently suffixes rather than failing."""
        self.add("Door South 01", "id-a")
        second = self.add("Door South 01", "id-b", x0=3.0)
        self.assertNotEqual(second.nameString(), "Door South 01")

    def test_parking_frees_the_name(self):
        """Which is why survivors are parked before final names are set."""
        first = self.add("Door South 01", "id-a")
        first.setName("freecad-bridge-parked-id-a")
        second = self.add("Door South 01", "id-b", x0=3.0)
        self.assertEqual(second.nameString(), "Door South 01")

    def test_a_handle_survives_retyping_in_place(self):
        sub = self.add("Door South 01", "id-a")
        handle = str(sub.handle())
        self.assertTrue(sub.setSubSurfaceType("GlassDoor"))
        sub.setName("GlassDoor South 01")
        self.assertEqual(str(sub.handle()), handle)
        self.assertEqual(sub.subSurfaceType(), "GlassDoor")

    def test_a_handle_survives_reshaping_in_place(self):
        sub = self.add("Door South 01", "id-a")
        handle = str(sub.handle())
        pts = self.os.Point3dVector()
        for x, z in ((1, 0), (2.5, 0), (2.5, 2.4), (1, 2.4)):
            pts.append(self.os.Point3d(x, 0, z))
        self.assertTrue(sub.setVertices(pts))
        self.assertEqual(str(sub.handle()), handle)
        self.assertAlmostEqual(sub.grossArea(), 3.6, 6)

    def test_a_handle_survives_moving_to_another_wall(self):
        other = self.make_wall(10)
        sub = self.add("Door South 01", "id-a")
        handle = str(sub.handle())
        pts = self.os.Point3dVector()
        for x, z in ((11, 0), (12, 0), (12, 2), (11, 2)):
            pts.append(self.os.Point3d(x, 0, z))
        self.assertTrue(sub.setSurface(other))
        self.assertTrue(sub.setVertices(pts))
        self.assertEqual(str(sub.handle()), handle)
        self.assertEqual(sub.surface().get().nameString(), other.nameString())

    def test_a_reference_to_the_handle_survives(self):
        """The reason any of this matters."""
        sub = self.add("Door South 01", "id-a")
        construction = self.os.model.Construction(self.model)
        construction.setLayers([self.os.model.StandardGlazing(self.model)])
        control = self.os.model.ShadingControl(construction)
        self.assertTrue(control.addSubSurface(sub))
        sub.setSubSurfaceType("GlassDoor")
        sub.setName("GlassDoor South 01")
        self.assertEqual(len(control.subSurfaces()), 1)
        self.assertEqual(control.subSurfaces()[0].nameString(),
                         "GlassDoor South 01")


if __name__ == "__main__":
    unittest.main()
