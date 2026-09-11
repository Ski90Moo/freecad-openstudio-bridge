# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for re-seating room labels after the walls move.

Run under either interpreter:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

What is being defended is narrow and important: a label *is* the room's
identity, so putting one in the wrong room does not produce an error, it
produces a model where two rooms have quietly swapped names, areas, heights
and thermal zones.  Nothing downstream can tell.

So the rules are deliberately conservative, and these tests pin the
conservatism rather than the cleverness:

  * a label already in a room of its own is never touched, however far from
    the middle it sits;
  * an area match only counts when exactly one free region has that area --
    two candidates is a guess, and a guess is worse than a question;
  * elimination fires only when precisely one label and one region are left;
  * anything else comes back as unresolved, by name.

The scenario throughout is the one that prompted this: moving the tenant
separation on FloorplanTest-02 left two regions with no label, two regions
with two, and every one of the four labels involved needing to move.
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

import fc_relabel as fr  # noqa: E402


class ResolveTests(unittest.TestCase):

    def test_a_settled_plan_moves_nothing(self):
        inside = {"a": "R1", "b": "R2"}
        areas = {"R1": 10.0, "R2": 20.0}
        assignment, homeless, empty, _why = fr.resolve(inside, areas,
                                                       {"a": 10.0, "b": 20.0})
        self.assertEqual(assignment, {"a": "R1", "b": "R2"})
        self.assertEqual((homeless, empty), ([], []))

    def test_a_label_in_its_own_room_is_kept_even_if_the_area_changed(self):
        """A room that was extended still belongs to the label standing in
        it.  Only containment can settle that, and it already has."""
        inside = {"a": "R1"}
        assignment, homeless, empty, _why = fr.resolve(
            inside, {"R1": 28.4}, {"a": 15.11})
        self.assertEqual(assignment, {"a": "R1"})
        self.assertEqual((homeless, empty), ([], []))

    def test_the_firewall_case(self):
        """Two rooms doubled up, two regions empty, all four labels adrift.

        Storage and RR slid sideways and their labels stayed put, landing in
        the Stair; the Stair's own label landed in the Hallway, which had
        grown.  Areas settle every one of them.
        """
        inside = {"hallway": "big", "stair": "big",
                  "storage": "small", "rr": "small"}
        areas = {"big": 28.40, "small": 8.62,
                 "new_storage": 9.59, "new_rr": 5.83}
        prior = {"hallway": 15.11, "stair": 8.62,
                 "storage": 9.59, "rr": 5.83}

        assignment, homeless, empty, why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["stair"], "small",
                         "the stair kept its 8.62 m2 room")
        self.assertEqual(assignment["storage"], "new_storage")
        self.assertEqual(assignment["rr"], "new_rr")
        self.assertEqual(assignment["hallway"], "big",
                         "the hallway is what is left, and it grew")
        self.assertEqual((homeless, empty), ([], []))
        self.assertIn("area", why["storage"])

    def test_two_rooms_of_the_same_area_are_not_guessed_at(self):
        """Identical areas cannot distinguish two rooms, so neither moves."""
        inside = {"a": None, "b": None}
        areas = {"R1": 12.0, "R2": 12.0}
        assignment, homeless, empty, _why = fr.resolve(
            inside, areas, {"a": 12.0, "b": 12.0})
        self.assertEqual(assignment, {})
        self.assertEqual(sorted(homeless), ["a", "b"])
        self.assertEqual(sorted(empty), ["R1", "R2"])

    def test_elimination_needs_exactly_one_of_each(self):
        inside = {"a": None}
        assignment, homeless, empty, why = fr.resolve(inside, {"R1": 99.0}, {})
        self.assertEqual(assignment, {"a": "R1"})
        self.assertIn("only", why["a"])

    def test_elimination_does_not_fire_on_two_and_two(self):
        inside = {"a": None, "b": None}
        areas = {"R1": 5.0, "R2": 7.0}
        assignment, homeless, empty, _why = fr.resolve(inside, areas, {})
        self.assertEqual(assignment, {})
        self.assertEqual(len(homeless), 2)
        self.assertEqual(len(empty), 2)

    def test_a_brand_new_room_is_reported_not_filled(self):
        inside = {"a": "R1"}
        areas = {"R1": 10.0, "R2": 6.0}
        assignment, homeless, empty, _why = fr.resolve(inside, areas,
                                                       {"a": 10.0})
        self.assertEqual(assignment, {"a": "R1"})
        self.assertEqual(homeless, [])
        self.assertEqual(empty, ["R2"])

    def test_a_label_whose_room_is_gone_is_reported(self):
        inside = {"a": "R1", "b": None}
        areas = {"R1": 10.0}
        assignment, homeless, empty, _why = fr.resolve(
            inside, areas, {"a": 10.0, "b": 4.0})
        self.assertEqual(assignment, {"a": "R1"})
        self.assertEqual(homeless, ["b"])
        self.assertEqual(empty, [])

    def test_without_prior_areas_only_containment_and_elimination_work(self):
        inside = {"a": "R1", "b": None, "c": None}
        areas = {"R1": 10.0, "R2": 6.0, "R3": 4.0}
        assignment, homeless, empty, _why = fr.resolve(inside, areas, {})
        self.assertEqual(assignment, {"a": "R1"})
        self.assertEqual(sorted(homeless), ["b", "c"])
        self.assertEqual(sorted(empty), ["R2", "R3"])

    def test_area_matching_respects_the_tolerance(self):
        """Two free regions, so elimination cannot rescue a failed match and
        the tolerance is what actually decides."""
        inside = {"a": None, "b": None}
        areas = {"R1": 9.59, "R2": 40.0}

        near, _h, _e, why = fr.resolve(inside, areas, {"a": 9.60})
        self.assertEqual(near.get("a"), "R1", "10 mm2 apart is one room")
        self.assertIn("area", why["a"])

        far, homeless, _e, _why = fr.resolve(inside, areas, {"a": 9.20})
        self.assertNotIn("a", far, "0.39 m2 apart is not evidence")
        self.assertIn("a", homeless)

    def test_a_doubled_room_keeps_the_label_its_area_vouches_for(self):
        """Both labels are inside it; only one has ever been that size."""
        inside = {"stair": "R", "storage": "R"}
        areas = {"R": 8.62, "other": 9.59}
        prior = {"stair": 8.62, "storage": 9.59}
        assignment, homeless, empty, _why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["stair"], "R")
        self.assertEqual(assignment["storage"], "other")
        self.assertEqual((homeless, empty), ([], []))

    def test_a_doubled_room_with_no_area_evidence_frees_itself(self):
        inside = {"a": "R", "b": "R"}
        areas = {"R": 8.62, "other": 9.59}
        assignment, homeless, empty, _why = fr.resolve(inside, areas, {})
        self.assertEqual(assignment, {})
        self.assertEqual(sorted(homeless), ["a", "b"])
        self.assertEqual(sorted(empty), ["R", "other"])

    def test_one_pass_is_not_enough(self):
        """FloorplanTest-02 as measured, which needs two rounds.

        Three labels had piled into the Shop, so the Shop looked doubled and
        could not be settled, and the Stair and Hallway had each landed alone
        in the other's room.  One pass places three of the five.  Once those
        three are counted where they are going rather than where they stand,
        the Shop holds one label and the Hallway falls out by elimination.
        """
        inside = {"shop": "R262", "storage": "R262", "rr": "R262",
                  "stair": "R959", "hallway": "R862"}
        areas = {"R262": 262.37, "R959": 9.59, "R862": 8.62,
                 "R583": 5.83, "R1511": 15.11}
        prior = {"shop": 235.74, "storage": 9.59, "rr": 5.83,
                 "stair": 8.62, "hallway": 28.40}

        one, _l, _f, _w = fr._one_pass(inside, areas, prior, fr.AREA_TOL_M2)
        self.assertEqual(len(one), 3, "a single pass gets three of five")

        assignment, homeless, empty, why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment,
                         {"shop": "R262", "storage": "R959", "rr": "R583",
                          "stair": "R862", "hallway": "R1511"})
        self.assertEqual((homeless, empty), ([], []))
        self.assertIn("only", why["hallway"])
        self.assertIn("area", why["storage"],
                      "the reason it was first placed for, not 'already "
                      "inside' from the round after")

    def test_a_label_alone_in_the_wrong_room_does_not_keep_it(self):
        """The measured failure on FloorplanTest-02.

        A wall moved and each label ended up alone in its neighbour's room,
        so every one of them looked settled.  Standing there is not evidence
        when the room is exactly the size the *other* label's room was.
        """
        inside = {"stair": "R959", "storage": None}
        areas = {"R959": 9.59, "R862": 8.62}
        prior = {"stair": 8.62, "storage": 9.59}
        assignment, homeless, empty, why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["storage"], "R959",
                         "the 9.59 m2 room goes to the label that was 9.59")
        self.assertEqual(assignment["stair"], "R862")
        self.assertEqual((homeless, empty), ([], []))
        self.assertIn("area", why["storage"])

    def test_a_deferred_label_takes_its_room_back(self):
        """Deferring must be able to delay a decision but never lose one.

        A rival exists, so the sitting label is set aside -- and then the
        rival's own claim turns out to be ambiguous (two free rooms are the
        size it was), so it settles nothing.  The room goes back to the label
        that was standing in it all along rather than being left empty.
        """
        inside = {"a": "R1", "b": None}
        areas = {"R1": 10.0, "R2": 10.0}
        prior = {"a": 42.0, "b": 10.0}
        assignment, homeless, empty, why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["a"], "R1", "a got its own room back")
        self.assertEqual(assignment["b"], "R2", "and b took what was left")
        self.assertEqual((homeless, empty), ([], []))
        self.assertIn("nothing else claimed it", why["a"])

    def test_two_rivals_are_no_evidence_at_all(self):
        """One rival is a swap; two is a guess, so the sitting label stays."""
        inside = {"a": "R", "b": None, "c": None}
        areas = {"R": 12.0, "R2": 30.0, "R3": 40.0}
        prior = {"a": 99.0, "b": 12.0, "c": 12.0}
        assignment, _homeless, _empty, why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["a"], "R")
        self.assertEqual(why["a"], "already inside")

    def test_a_new_label_with_no_history_is_never_bumped(self):
        """No prior area is not the same as a contradicted one.  A label the
        user just placed has no history to argue with and keeps its room."""
        inside = {"fresh": "R", "old": None}
        areas = {"R": 12.0, "R2": 30.0}
        prior = {"old": 12.0}
        assignment, homeless, _empty, _why = fr.resolve(inside, areas, prior)
        self.assertEqual(assignment["fresh"], "R")
        self.assertEqual(assignment["old"], "R2", "left over, by elimination")
        self.assertEqual(homeless, [])


if __name__ == "__main__":
    unittest.main()
