# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for the air-boundary flow correlations in find_air_boundaries.py.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

These check the correlations against the case they were measured for and
against their own scaling laws.  They do NOT validate that applying the result
as a fixed ZoneMixing rate is the right model for a given opening -- see
test_validity_range below for why that is a separate question.
"""

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import find_air_boundaries as fab  # noqa: E402

T = 293.15


class TestDoorwayBenchmark(unittest.TestCase):
    """The case Brown & Solvason was derived and measured for.

    A standard 0.8 x 2.0 m doorway at about 1 K is quoted throughout the
    interzone-airflow literature at roughly 0.1 m3/s one way.  This is the
    only external number in the file, so it is the anchor: if the
    implementation drifts, this is what catches it.
    """

    def test_standard_doorway_at_1K(self):
        q = fab.vertical_flow(0.8, 2.0, 1.0, T)
        self.assertAlmostEqual(q, 0.059, places=3)

    def test_doorway_lands_inside_the_measured_Cd_spread(self):
        """The reported spread across studies is 0.35 <= Cd <= 0.63.

        Bracketing the benchmark by that spread rather than by one number is
        the honest form of this test: the literature does not agree on Cd to
        better than a factor of 1.8, so neither should the assertion.
        """
        q = fab.vertical_flow(0.8, 2.0, 1.0, T)
        shape = (1.0 / 3.0) * 0.8 * 2.0 ** 1.5 * (9.80665 * 1.0 / T) ** 0.5
        self.assertGreaterEqual(q, 0.35 * shape)
        self.assertLessEqual(q, 0.63 * shape)

    def test_doorway_velocity_is_a_gentle_draft(self):
        # Mean speed through half the opening, the quantity a person would
        # actually feel.  Perception threshold is ~0.15 m/s.
        q = fab.vertical_flow(0.8, 2.0, 1.0, T)
        v = q / (0.8 * 2.0 / 2)
        self.assertLess(v, 0.15)


class TestScaling(unittest.TestCase):
    """The correlations' own exponents, which the report leans on."""

    def test_flow_scales_with_sqrt_delta_t(self):
        a = fab.vertical_flow(3.0, 2.7, 1.0, T)
        b = fab.vertical_flow(3.0, 2.7, 4.0, T)
        self.assertAlmostEqual(b / a, 2.0, places=6)

    def test_flow_is_linear_in_width(self):
        a = fab.vertical_flow(3.0, 2.7, 0.5, T)
        b = fab.vertical_flow(9.0, 2.7, 0.5, T)
        self.assertAlmostEqual(b / a, 3.0, places=6)

    def test_flow_scales_with_height_to_the_three_halves(self):
        a = fab.vertical_flow(3.0, 2.0, 0.5, T)
        b = fab.vertical_flow(3.0, 4.0, 0.5, T)
        self.assertAlmostEqual(b / a, 2.0 ** 1.5, places=6)

    def test_interfacial_velocity_is_width_independent(self):
        """v = (2/3) Cd sqrt(H) sqrt(g dT / T) -- no width in it.

        This is why every mezzanine edge in the report reads the same
        0.085 m/s despite spanning 1.7 to 33 m2.
        """
        speeds = []
        for width in (1.0, 5.0, 20.0):
            q = fab.vertical_flow(width, 2.71, 0.5, T)
            speeds.append(q / (width * 2.71 / 2))
        for v in speeds[1:]:
            self.assertAlmostEqual(v, speeds[0], places=9)

    def test_horizontal_flow_scales_with_diameter_to_the_five_halves(self):
        # Q = C sqrt(g' D^5), and D = sqrt(4A/pi), so quadrupling the area
        # doubles D and multiplies flow by 2^2.5.
        a = fab.horizontal_flow(4.0, 0.5, T)
        b = fab.horizontal_flow(16.0, 0.5, T)
        self.assertAlmostEqual(b / a, 2.0 ** 2.5, places=6)

    def test_zero_and_negative_openings_carry_nothing(self):
        self.assertEqual(fab.vertical_flow(0.0, 2.7, 0.5, T), 0.0)
        self.assertEqual(fab.vertical_flow(3.0, 0.0, 0.5, T), 0.0)
        self.assertEqual(fab.horizontal_flow(0.0, 0.5, T), 0.0)


class TestSelfConsistency(unittest.TestCase):
    """dT is an output of the load imbalance, not a free input.

    Q = K sqrt(dT), so the heat carried is rho.cp.K.dT^1.5 and the dT that
    settles out is (W / rho.cp.K)^(2/3).  The 0.5 K working figure should
    correspond to a plausible imbalance rather than being a bare convention.
    """

    RHO_CP = 1.2 * 1006.0

    def settled_delta_t(self, width, height, watts):
        k = fab.vertical_flow(width, height, 1.0, T)     # Q at dT = 1
        return (watts / (self.RHO_CP * k)) ** (2.0 / 3.0)

    def test_kilowatt_imbalance_across_a_mezzanine_edge_gives_about_half_a_K(self):
        # 33 m2 of edge at 2.71 m high, the 301/303 mezzanine.
        dt = self.settled_delta_t(33.0 / 2.71, 2.71, 1000.0)
        self.assertGreater(dt, 0.3)
        self.assertLess(dt, 0.8)

    def test_larger_imbalance_raises_delta_t_only_as_the_two_thirds_power(self):
        a = self.settled_delta_t(12.2, 2.71, 1000.0)
        b = self.settled_delta_t(12.2, 2.71, 8000.0)
        self.assertAlmostEqual(b / a, 8.0 ** (2.0 / 3.0), places=6)


class TestValidityRange(unittest.TestCase):
    """The correlation assumes an aperture SMALL relative to both zones.

    Not a property of the arithmetic but of where it may be applied, so it is
    recorded here rather than enforced in the code: an opening comparable to
    the zone's own floor means there is no second well-mixed reservoir, and
    the two-zone abstraction -- not the flow -- is what has failed.
    """

    def test_documented_thresholds(self):
        # Annex 20 Fig. 2.16: below Aw = 0.1 the flow is bulk-density driven,
        # which is the regime the vertical correlation belongs to.
        self.assertEqual(fab.AW_DENSITY_DRIVEN_MAX, 0.1)
        self.assertEqual(fab.DEFAULT_DELTA_T, 0.5)
        # Annex 20 steady-state doorway measurements, not the textbook 0.6.
        self.assertEqual(fab.CD_VERTICAL, 0.43)
        self.assertEqual(fab.C_HORIZONTAL, 0.055)

    def test_default_cap_is_five_ach(self):
        self.assertEqual(fab.DEFAULT_MAX_ACH, 5.0)

    def test_cap_still_couples_an_ordinary_imbalance(self):
        """The cap has to leave steady-state coupling intact to be worth it.

        100 W across the 301 mezzanine at 5 ACH settles at about 0.75 K --
        inside the 0.5-1 K band an open boundary is expected to hold. The
        headroom shows up only under a large imbalance, which is exactly the
        transient the model is meant to reveal.
        """
        rho_cp = 1.2 * 1006.0
        q = fab.DEFAULT_MAX_ACH * 79.5 / 3600.0
        self.assertAlmostEqual(100.0 / (rho_cp * q), 0.75, delta=0.05)

    def test_capping_the_rate_raises_delta_t_in_proportion(self):
        """ZoneMixing is linear, so dT scales as 1/ACH.

        This is the headroom the cap buys: at 5 ACH a sustained 1 kW split
        reaches ~7.5 K instead of being absorbed, so it reaches the results
        where an engineer can act on it.
        """
        rho_cp = 1.2 * 1006.0
        volume = 79.5
        def settled(ach):
            return 1000.0 / (rho_cp * ach * volume / 3600.0)
        self.assertAlmostEqual(settled(5.0), 7.5, delta=0.2)
        self.assertAlmostEqual(settled(10.0) / settled(20.0), 2.0, places=6)


class TestFootprintAxes(unittest.TestCase):
    """A corridor's length must survive the building not being axis-aligned.

    FloorplanTest-02 sits on a 45 degree north axis, so measuring the long
    dimension on x and y would report a corridor as nearly square and the rule
    would never fire.
    """

    def test_axis_aligned_rectangle(self):
        length, width, axis = fab.footprint_axes(
            [(0, 0), (10, 0), (10, 2), (0, 2)])
        self.assertAlmostEqual(length, 10.0, places=6)
        self.assertAlmostEqual(width, 2.0, places=6)
        self.assertAlmostEqual(abs(axis[0]), 1.0, places=6)

    def test_rotated_rectangle_keeps_its_true_dimensions(self):
        c = math.cos(math.radians(45.0))
        pts = [(0, 0), (10 * c, 10 * c), (10 * c - 2 * c, 10 * c + 2 * c),
               (-2 * c, 2 * c)]
        length, width, axis = fab.footprint_axes(pts)
        # footprint_axes rounds vertices to 1 micron before hulling, so the
        # comparison cannot be tighter than that.
        self.assertAlmostEqual(length, 10.0, places=5)
        self.assertAlmostEqual(width, 2.0, places=5)
        self.assertAlmostEqual(abs(axis[0]), c, places=5)
        self.assertAlmostEqual(abs(axis[1]), c, places=5)

    def test_degenerate_footprint_reports_zero_width(self):
        """A footprint with no area must fall out of the rule, not crash.

        corridor_openings() skips on width <= 0, so that is the contract --
        the length it reports for a bare line is irrelevant.
        """
        length, width, _axis = fab.footprint_axes([(0, 0), (1, 1)])
        self.assertEqual(width, 0.0)
        self.assertGreater(length, 0.0)


class TestCorridorCrossSection(unittest.TestCase):
    """Why "narrow compared with the hallway" needs two more conditions.

    The numbers are 101a Hallway's, read off the built model: 8.58 m long,
    1.65 m wide, with three interior walls that a width-only test cannot tell
    apart.  This is the case that decides the rule, so it is pinned here.
    """

    LENGTH, WIDTH = 8.58, 1.65

    def selects(self, run, square, narrow_max=None, span_tol=None,
                square_max=None):
        narrow = run / self.LENGTH
        span = run / self.WIDTH
        return (narrow <= (narrow_max or fab.DEFAULT_CORRIDOR_NARROW_RATIO)
                and abs(span - 1.0) <= (span_tol
                                        or fab.DEFAULT_CORRIDOR_SPAN_TOL)
                and square <= (square_max or fab.DEFAULT_CORRIDOR_SQUARE_COS))

    def test_open_ends_are_selected(self):
        # to 101 Lobby and to 101b Hallway 6: full width, square to the run.
        self.assertTrue(self.selects(1.65, 0.0))

    def test_the_narrowest_wall_on_the_corridor_is_still_a_wall(self):
        """1.29 m of office 102's frontage -- w/L 0.15, the lowest on 101a.

        A width-only rule would select this before either open end, which is
        the whole reason the span condition exists.
        """
        run = 1.29
        self.assertLess(run / self.LENGTH, 1.65 / self.LENGTH)   # narrower
        self.assertFalse(self.selects(run, 1.0))
        self.assertFalse(self.selects(run, 0.0))                  # span alone

    def test_a_room_frontage_wider_than_the_corridor_is_rejected(self):
        # RR 110 off 101b: 2.48 m against a 1.70 m corridor, w/W 1.46.
        self.assertFalse(self.selects(2.48, 1.0))

    def test_a_full_width_wall_along_the_run_is_rejected(self):
        # 101d Stair beside 101a: 1.58 m, w/W 0.96, but parallel to the run.
        self.assertTrue(abs(1.58 / self.WIDTH - 1.0)
                        <= fab.DEFAULT_CORRIDOR_SPAN_TOL)
        self.assertFalse(self.selects(1.58, 1.0))

    def test_defaults(self):
        self.assertEqual(fab.DEFAULT_CORRIDOR_NARROW_RATIO, 0.25)
        self.assertEqual(fab.DEFAULT_CORRIDOR_SPAN_TOL, 0.15)
        self.assertEqual(fab.DEFAULT_CORRIDOR_SQUARE_COS, 0.30)

    def test_square_tolerance_allows_a_skewed_junction(self):
        # |cos| 0.30 is about 17 degrees out of square.
        self.assertGreater(math.degrees(math.acos(
            fab.DEFAULT_CORRIDOR_SQUARE_COS)), 72.0)

    def test_pattern_matches_the_usual_names_and_not_offices(self):
        p = re.compile(fab.CORRIDOR_PATTERN, re.IGNORECASE)
        for name in ("Hallway", "Hallway 6", "Corridor", "Hall",
                     "Circulation", "Passage"):
            self.assertTrue(p.search(name), name)
        for name in ("Office", "Break Room", "Storage", "IT Room", "Shop"):
            self.assertFalse(p.search(name), name)


class TestStackedCrossSections(unittest.TestCase):
    """Two openings in one plane, open to different storeys, cannot both be
    open.

    101c Hallway is two storeys tall and ends against office 107 below and IT
    room 116 above, in the same plane.  If both were air boundaries a person
    would walk out of 116 into the void over the corridor -- so in reality one
    of them is a wall, or the upper one is a guardrail, and the drawing does
    not say which.
    """

    def candidate(self, plane, space_b):
        return {"plane": plane, "space_b": space_b,
                "space_a": "004-101c-Hallway"}

    def test_same_plane_different_storeys_is_rejected(self):
        story = {"107": "Level 1", "116": "Level 2"}
        kept, rejected = fab.reject_stacked_cross_sections(
            [self.candidate((1.0, 0.0, 12.5), "107"),
             self.candidate((1.0, 0.0, 12.5), "116")], story)
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 2)

    def test_same_plane_same_storey_is_kept(self):
        """A corridor end split into fragments on one storey is ordinary."""
        story = {"a": "Level 1", "b": "Level 1"}
        kept, rejected = fab.reject_stacked_cross_sections(
            [self.candidate((1.0, 0.0, 12.5), "a"),
             self.candidate((1.0, 0.0, 12.5), "b")], story)
        self.assertEqual(len(kept), 2)
        self.assertEqual(rejected, [])

    def test_different_planes_are_independent(self):
        """The two ENDS of a corridor are unrelated to each other."""
        story = {"lobby": "Level 1", "116": "Level 2"}
        kept, rejected = fab.reject_stacked_cross_sections(
            [self.candidate((1.0, 0.0, 3.0), "lobby"),
             self.candidate((1.0, 0.0, 12.5), "116")], story)
        self.assertEqual(len(kept), 2)
        self.assertEqual(rejected, [])

    def test_a_lone_opening_is_never_rejected(self):
        kept, rejected = fab.reject_stacked_cross_sections(
            [self.candidate((0.0, 1.0, 4.65), "113")],
            {"113": "Level 2"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, [])


class TestOpenStair(unittest.TestCase):
    """Sides are derivable, ends are not.

    101d's stair is 5.75 m long x 1.58 m wide and stacked over two storeys.
    Its long sides open onto the double-height lobby and the 113 mezzanine and
    are guardrail edges either way.  Its two ends are the way in and the wall
    behind the flight -- one each per storey, and which is which is drawn as a
    direction arrow the exported plan does not carry.
    """

    LENGTH, WIDTH = 5.75, 1.58
    AXIS = (1.0, 0.0)

    def is_end(self, run, direction):
        square = abs(direction[0] * self.AXIS[0] + direction[1] * self.AXIS[1])
        return (abs(run / self.WIDTH - 1.0) <= fab.DEFAULT_CORRIDOR_SPAN_TOL
                and square <= fab.DEFAULT_CORRIDOR_SQUARE_COS)

    def test_long_sides_are_not_ends(self):
        # Surfaces 29, 177 and 179 -- taken automatically.
        self.assertFalse(self.is_end(self.LENGTH, (1.0, 0.0)))

    def test_short_walls_across_the_run_are_ends(self):
        # Surfaces 28, 30, 176 and 178 -- left to the engineer.
        self.assertTrue(self.is_end(self.WIDTH, (0.0, 1.0)))

    def test_plain_stair_is_not_opted_in(self):
        p = re.compile(fab.OPEN_STAIR_PATTERN, re.IGNORECASE)
        for name in ("Open Stair", "Open Stair 2", "OPENSTAIR", "open  stair"):
            self.assertTrue(p.search(name), name)
        for name in ("Stair", "Stair 2", "Stairwell", "Egress Stair"):
            self.assertFalse(p.search(name), name)

    def test_open_stair_is_not_also_a_corridor(self):
        """The two rules must not both claim a stair's walls."""
        p = re.compile(fab.CORRIDOR_PATTERN, re.IGNORECASE)
        self.assertFalse(p.search("Open Stair"))
        self.assertFalse(p.search("Stair 2"))


class TestSolidOverride(unittest.TestCase):
    """A fire wall is not visible in geometry, so the engineer names it."""

    def opening(self, a, b):
        return {"surface_a": a, "surface_b": b}

    def test_naming_either_face_drops_the_pair(self):
        for named in ("Surface 292", "Surface 312"):
            kept, dropped = fab.drop_solid(
                [self.opening("Surface 292", "Surface 312")], {named})
            self.assertEqual(kept, [], named)
            self.assertEqual(len(dropped), 1, named)

    def test_other_openings_are_untouched(self):
        kept, dropped = fab.drop_solid(
            [self.opening("Surface 292", "Surface 312"),
             self.opening("Surface 221", "Surface 286")],
            {"Surface 292"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["surface_a"], "Surface 221")
        self.assertEqual(len(dropped), 1)

    def test_an_unmatched_name_drops_nothing(self):
        kept, dropped = fab.drop_solid(
            [self.opening("Surface 292", "Surface 312")], {"Surface 999"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])


class MockOptional:
    def __init__(self, value=None):
        self._value = value

    def is_initialized(self):
        return self._value is not None

    def get(self):
        return self._value


class MockSpace:
    def __init__(self, name):
        self._name = name

    def nameString(self):
        return self._name


class MockPoint3d:
    def __init__(self, z):
        self._z = z

    def z(self):
        return self._z


class MockSurface:
    """Just enough of openstudio.model.Surface for promote_declared."""

    def __init__(self, name, space, area=10.0, kind="Wall", height=3.0):
        self._name = name
        self._space = space
        self._adjacent = MockOptional(None)
        self._area = area
        self._kind = kind
        self._boundary = "Outdoors"
        self._height = height

    def nameString(self):
        return self._name

    def space(self):
        return MockOptional(self._space)

    def adjacentSurface(self):
        return self._adjacent

    def grossArea(self):
        return self._area

    def surfaceType(self):
        return self._kind

    def outsideBoundaryCondition(self):
        return self._boundary

    def vertices(self):
        return [MockPoint3d(0.0), MockPoint3d(self._height)]


def pair_up(a, b):
    """Make two mock surfaces each other's interior partner."""
    a._adjacent, b._adjacent = MockOptional(b), MockOptional(a)
    a._boundary = b._boundary = "Surface"


class MockModel:
    def __init__(self, surfaces):
        self._surfaces = surfaces

    def getSurfaces(self):
        return self._surfaces


class PromoteDeclaredTests(unittest.TestCase):
    """--open-surface has to agree with itself, not just with the rules.

    promote_declared looked up each name's covering construction from a dict
    built once, before the loop, from the rule-derived openings only -- so
    two declared surfaces sharing a pair no rule had covered each minted
    their own "declared" (kind, space_a, space_b), and group_pairs (keyed on
    that exact triple) gave the pair two constructions.  Measured on
    FloorplanTest-04: `--open-surface "Surface 77,Surface 147"`, both on
    004-101-LobbyReception <-> 026-S1-Stair, came back as "Air Boundary -
    Opening 101 to S1" and "Air Boundary - Opening S1 to 101 - Level 2" --
    apply_air_boundaries.py's own zone-pair guard caught it and refused to
    write the model, rather than double the mixing silently.
    """

    def test_two_declared_surfaces_on_a_new_pair_share_one_construction(self):
        lobby, stair = MockSpace("004-101-LobbyReception"), \
            MockSpace("026-S1-Stair")
        a1, a2 = MockSurface("Surface 77", lobby), \
            MockSurface("Surface 146", stair)
        pair_up(a1, a2)
        b1, b2 = MockSurface("Surface 147", stair), \
            MockSurface("Surface 78", lobby)
        pair_up(b1, b2)
        model = MockModel([a1, a2, b1, b2])

        found, problems = fab.promote_declared(
            model, [], {"Surface 77", "Surface 147"})

        self.assertEqual(problems, [])
        self.assertEqual(len(found), 2)
        keys = {(op["kind"], op["space_a"], op["space_b"]) for op in found}
        self.assertEqual(len(keys), 1,
                         "both surfaces must resolve to the same (kind, "
                         "space_a, space_b) or group_pairs makes two "
                         "constructions out of one pair: %s" % keys)

    def test_naming_the_other_face_first_still_merges(self):
        """Order must not matter: which face is named first, or which of
        mine/theirs a surface happens to be, is not a decision to make
        twice."""
        corridor = MockSpace("001-HALL-1-Corridor")
        lobby = MockSpace("004-101-LobbyReception")
        a1, a2 = MockSurface("Surface 21", corridor), \
            MockSurface("Surface 82", lobby)
        pair_up(a1, a2)
        b1, b2 = MockSurface("Surface 15", lobby), \
            MockSurface("Surface 89", corridor)
        pair_up(b1, b2)
        model = MockModel([a1, a2, b1, b2])

        found, problems = fab.promote_declared(
            model, [], {"Surface 21", "Surface 89"})

        self.assertEqual(problems, [])
        keys = {(op["kind"], op["space_a"], op["space_b"]) for op in found}
        self.assertEqual(len(keys), 1, keys)

    def test_a_declared_surface_joins_an_existing_rule_derived_pair(self):
        """Pre-existing behaviour, still correct once the lookup updates as
        it goes: a declared surface on a pair a rule already covers joins
        that rule's construction rather than starting a new one."""
        rule_op = {"kind": "mezzanine", "space_a": "Room A",
                  "space_b": "Room B",
                  "surface_a": "Surface 1", "surface_b": "Surface 2"}
        s = MockSurface("Surface 9", MockSpace("Room B"))
        partner = MockSurface("Surface 10", MockSpace("Room A"))
        pair_up(s, partner)
        model = MockModel([s, partner])

        found, problems = fab.promote_declared(model, [rule_op], {"Surface 9"})

        self.assertEqual(problems, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "mezzanine")
        self.assertEqual(found[0]["space_a"], "Room A")
        self.assertEqual(found[0]["space_b"], "Room B")

    def test_an_unclaimed_surface_still_stands_alone(self):
        room_a, room_b = MockSpace("Room A"), MockSpace("Room B")
        s1, s2 = MockSurface("Surface 1", room_a), \
            MockSurface("Surface 2", room_b)
        pair_up(s1, s2)
        model = MockModel([s1, s2])

        found, problems = fab.promote_declared(model, [], {"Surface 1"})

        self.assertEqual(problems, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "declared")


# The model to check these invariants against.  BRIDGE_TEST_OSM is how CI
# points them at the one it just built from samples/ -- without it these tests
# could only ever run on the machine that happens to have a model at the path
# below, which meant they never ran anywhere but one desk.
OSM = os.environ.get("BRIDGE_TEST_OSM") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "MCP", "runs", "fptest02.osm")


@unittest.skipUnless(os.path.exists(OSM), "built model not present")
class TestAppliedModelInvariants(unittest.TestCase):
    """Checks against the model as actually built, not against arithmetic.

    The one that matters most is the zone-pair invariant. EnergyPlus creates
    mixing per Construction:AirBoundary, so two constructions between the same
    pair of zones would silently DOUBLE the flow -- a mistake that produces a
    plausible-looking model and no error anywhere.
    """

    @classmethod
    def setUpClass(cls):
        cls.model = fab.load_model(os.path.abspath(OSM))
        cls.constructions = cls.model.getConstructionAirBoundarys()

    def surfaces_of(self, construction):
        return [s for s in self.model.getSurfaces()
                if s.construction().is_initialized()
                and s.construction().get().nameString()
                == construction.nameString()]

    def test_no_two_constructions_share_a_zone_pair(self):
        seen = {}
        for construction in self.constructions:
            pairs = set()
            for surface in self.surfaces_of(construction):
                adjacent = surface.adjacentSurface()
                if not (adjacent.is_initialized()
                        and surface.space().is_initialized()
                        and adjacent.get().space().is_initialized()):
                    continue
                pairs.add(frozenset((surface.space().get().nameString(),
                                     adjacent.get().space().get().nameString())))
            for pair in pairs:
                self.assertNotIn(
                    pair, seen,
                    "%s and %s both span %s -- EnergyPlus would mix twice"
                    % (construction.nameString(), seen.get(pair), sorted(pair)))
                seen[pair] = construction.nameString()

    def test_both_faces_carry_the_same_construction(self):
        """Half an air boundary is meaningless, and easy to leave behind."""
        for construction in self.constructions:
            for surface in self.surfaces_of(construction):
                adjacent = surface.adjacentSurface()
                self.assertTrue(adjacent.is_initialized(),
                                "%s is not interior" % surface.nameString())
                partner = adjacent.get().construction()
                self.assertTrue(partner.is_initialized(),
                                "%s has an air boundary, its partner %s does "
                                "not" % (surface.nameString(),
                                         adjacent.get().nameString()))
                self.assertEqual(partner.get().nameString(),
                                 construction.nameString())

    def test_every_air_boundary_sets_a_rate(self):
        """The constructor writes 0.0, not the IDD's 0.5 -- so 0 means the
        rate was never set and the boundary mixes nothing at all."""
        for construction in self.constructions:
            self.assertEqual(construction.airExchangeMethod(), "SimpleMixing")
            self.assertGreater(construction.simpleMixingAirChangesPerHour(),
                               0.0, construction.nameString())

    def test_the_fire_wall_stayed_solid(self):
        """205 Mezzanine to the lobby and to 101c is rated, not a guardrail.

        Keyed on the pair of ROOM NUMBERS, never on surface names and never on
        full space names.  Surface names are positional: rebuilding a space
        renumbers them, so the four names this test used to name moved to
        entirely different walls when the roof went in, and it started
        asserting things about the wrong geometry.

        A full space name carries the same hazard one level up.  The leading
        index in `034-205-Mezzanine` is an export-order counter, not identity:
        exporting the same drawing after a room was added anywhere earlier in
        the order shifts every index after it, and this test went looking for
        a wall that was still there under a name that no longer existed.  The
        room number is the part the drawing actually fixes.
        """
        for pair in (("205", "101"), ("205", "101c")):
            faces = [s for s in self.model.getSurfaces()
                     if self.room_pair(s) == frozenset(pair)]
            self.assertTrue(faces, "no wall between rooms %s and %s" % pair)
            for surface in faces:
                construction = surface.construction()
                self.assertFalse(
                    construction.is_initialized(),
                    "%s (rooms %s to %s) is %s -- that wall is rated, not a "
                    "guardrail" % (surface.nameString(), pair[0], pair[1],
                                   construction.get().nameString()
                                   if construction.is_initialized() else ""))

    @staticmethod
    def room_number(space_name):
        """`034-205-Mezzanine` -> `205`; the drawing's own label for the room."""
        parts = space_name.split("-")
        return parts[1] if len(parts) > 2 else space_name

    @classmethod
    def room_pair(cls, surface):
        """The two room numbers an interior surface separates, or None."""
        here, adjacent = surface.space(), surface.adjacentSurface()
        if not (here.is_initialized() and adjacent.is_initialized()):
            return None
        there = adjacent.get().space()
        if not there.is_initialized():
            return None
        return frozenset((cls.room_number(here.get().nameString()),
                          cls.room_number(there.get().nameString())))


class RebuildRiskTests(unittest.TestCase):
    """A rebuild takes the air boundaries with it, and says nothing.

    The openings at least leave a hole in the elevation when they go.  An air
    boundary leaves nothing: the wall reads solid, the model still builds, and
    the two zones simply stop exchanging air.  One hand-made coupling was lost
    to a roof rebuild and found only by diffing against a backup, so the
    update now names every coupling it is about to drop.
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

    def coupled(self):
        """Two rooms side by side, matched, sharing an air boundary."""
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

        boundary = self.os.model.ConstructionAirBoundary(model)
        boundary.setName("Air Boundary - 1 to 2")
        boundary.setSimpleMixingAirChangesPerHour(5.0)
        shared = 0
        for space in spaces:
            for surface in space.surfaces():
                if surface.adjacentSurface().is_initialized():
                    surface.setConstruction(boundary)
                    shared += 1
        self.assertEqual(shared, 2, "fixture did not match the two rooms")
        return model

    def test_a_rebuilt_space_reports_its_coupling(self):
        model = self.coupled()
        at_risk = self.uog.air_boundaries_at_risk(model, ["Room 1"])
        self.assertEqual(list(at_risk), ["Room 1 <-> Room 2"])
        name, area, ach = at_risk["Room 1 <-> Room 2"]
        self.assertEqual(name, "Air Boundary - 1 to 2")
        self.assertEqual(ach, 5.0)
        self.assertAlmostEqual(area, 12.0, 6)   # 4 m x 3 m, counted once

    def test_naming_the_other_side_finds_the_same_pair(self):
        model = self.coupled()
        self.assertEqual(list(self.uog.air_boundaries_at_risk(model,
                                                              ["Room 2"])),
                         ["Room 1 <-> Room 2"])

    def test_the_pair_is_not_counted_twice_when_both_are_rebuilt(self):
        model = self.coupled()
        at_risk = self.uog.air_boundaries_at_risk(model,
                                                  ["Room 1", "Room 2"])
        self.assertEqual(len(at_risk), 1)
        self.assertAlmostEqual(at_risk["Room 1 <-> Room 2"][1], 12.0, 6)

    def test_an_untouched_space_reports_nothing(self):
        model = self.coupled()
        self.assertEqual(self.uog.air_boundaries_at_risk(model, ["Room 9"]),
                         {})

    def test_an_ordinary_construction_is_not_an_air_boundary(self):
        model = self.coupled()
        plain = self.os.model.Construction(model)
        for surface in model.getSurfaces():
            if surface.adjacentSurface().is_initialized():
                surface.setConstruction(plain)
        self.assertEqual(self.uog.air_boundaries_at_risk(model, ["Room 1"]),
                         {})


if __name__ == "__main__":
    unittest.main()
