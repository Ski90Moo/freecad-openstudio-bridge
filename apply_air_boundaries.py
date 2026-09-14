# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Apply air-boundary constructions to an .osm, sized by find_air_boundaries.

Run under the bridge venv:

    osvenv/Scripts/python.exe find_air_boundaries.py model.osm plan.json \
        --apply-json > boundaries.json
    osvenv/Scripts/python.exe apply_air_boundaries.py model.osm boundaries.json \
        --out model.osm

or in one pass, reading the report straight off the pipe:

    osvenv/Scripts/python.exe find_air_boundaries.py model.osm plan.json \
        --apply-json \
      | osvenv/Scripts/python.exe apply_air_boundaries.py model.osm - \
        --out model.osm

An air boundary is the deliberate absence of a wall: two spaces open to each
other, which the bridge is forced to model as two spaces because the floor plan
draws them as two rooms.  `find_air_boundaries.py` decides *which* surfaces
those are and *how fast* air moves through them; this script is only the hand
that writes the answer into the model.

This is a port of the `assign_air_boundary_construction` Ruby measure, which
had to run through the OpenStudio App or an MCP server.  Doing it here keeps
the whole geometry pipeline in one interpreter with one dependency, and means
CI can build a model with air boundaries in it -- the Ruby measure lives
outside the repo, so nothing automated could reach it.

Two things it is stricter about than the measure was, because both produce a
model that simulates happily and is wrong:

  * **Both faces or neither.**  An interzone surface pair is two objects.  The
    measure warned when only one was named; here it is an error, because half
    an air boundary is a wall with a hole in the accounting.

  * **One construction per zone pair.**  EnergyPlus creates mixing per
    `Construction:AirBoundary`, so two constructions spanning the same pair of
    zones mix that pair twice -- double the intended flow, no warning
    anywhere.
"""

import argparse
import json
import os
import sys

import openstudio

# Written into each construction's comment so a re-run can tell its own work
# apart from air boundaries added by hand or by an MCP tool, and clear only
# what it is responsible for.
MARKER = "created-by:freecad-bridge"

# Air at about 20 C.  Only used to report what a rate means, never to size it.
RHO = 1.2
CP = 1006.0


def load_model(path):
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.path(path))
    if not loaded.is_initialized():
        raise SystemExit("could not load %s" % path)
    return loaded.get()


def read_specs(path):
    """The --apply-json report, from a file or from stdin when path is '-'."""
    if path == "-":
        text = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    try:
        specs = json.loads(text)
    except ValueError as exc:
        raise SystemExit(
            "%s is not valid JSON: %s\n"
            "find_air_boundaries.py --apply-json writes the report to stdout; "
            "if you piped it, check nothing else is writing there too."
            % ("stdin" if path == "-" else path, exc))
    if not isinstance(specs, list):
        raise SystemExit("expected a list of constructions, got %s"
                         % type(specs).__name__)
    return specs


def names_in(spec):
    return [n.strip() for n in spec.get("surface_names", "").split(",")
            if n.strip()]


def ours(model):
    """The air-boundary constructions this script created previously."""
    return [c for c in model.getConstructionAirBoundarys()
            if MARKER in (c.comment() or "")]


def space_pair(surface):
    """The two space names a surface separates, or None if it is not interior."""
    adjacent = surface.adjacentSurface()
    if not (adjacent.is_initialized() and surface.space().is_initialized()
            and adjacent.get().space().is_initialized()):
        return None
    return frozenset((surface.space().get().nameString(),
                      adjacent.get().space().get().nameString()))


def check(model, specs):
    """Everything wrong with applying these specs to this model.

    All of it, not the first one -- a stale report usually names several
    surfaces that have moved on, and finding them one run at a time is a poor
    use of anyone's afternoon.
    """
    problems = []
    claimed = {}          # zone pair -> construction that already spans it
    assigned = {}         # surface name -> construction name

    for spec in specs:
        construction_name = spec.get("construction_name")
        if not construction_name:
            problems.append("a construction in the report has no name")
            continue

        method = spec.get("air_exchange_method", "SimpleMixing")
        if method not in ("SimpleMixing", "None"):
            problems.append("%s: air_exchange_method is %r, expected "
                            "'SimpleMixing' or 'None'"
                            % (construction_name, method))
        if method == "SimpleMixing":
            ach = spec.get("simple_mixing_ach")
            # OpenStudio's constructor writes 0.0 here rather than leaving the
            # IDD's 0.5 default, so SimpleMixing at an unset rate mixes
            # nothing at all and the model looks fine.
            if not isinstance(ach, (int, float)) or ach <= 0:
                problems.append(
                    "%s: simple_mixing_ach is %r; SimpleMixing at zero mixes "
                    "nothing, which is not what an air boundary is for"
                    % (construction_name, ach))

        names = names_in(spec)
        if not names:
            problems.append("%s: no surfaces" % construction_name)
            continue

        for name in names:
            found = model.getSurfaceByName(name)
            if not found.is_initialized():
                problems.append("%s: no surface named %r in the model -- the "
                                "report is for a different build"
                                % (construction_name, name))
                continue
            surface = found.get()

            if surface.outsideBoundaryCondition() != "Surface":
                problems.append(
                    "%s: %s is %r, not interior; an air boundary has to "
                    "separate two zones"
                    % (construction_name, name,
                       surface.outsideBoundaryCondition()))
                continue

            if len(surface.subSurfaces()) > 0:
                problems.append(
                    "%s: %s carries %d sub-surface(s); EnergyPlus does not "
                    "allow a window or door on an air boundary"
                    % (construction_name, name, len(surface.subSurfaces())))
                continue

            if name in assigned and assigned[name] != construction_name:
                problems.append("%s is claimed by both %s and %s"
                                % (name, assigned[name], construction_name))
                continue
            assigned[name] = construction_name

        # Both faces, or neither.
        for name in names:
            found = model.getSurfaceByName(name)
            if not found.is_initialized():
                continue
            adjacent = found.get().adjacentSurface()
            if not adjacent.is_initialized():
                continue
            partner = adjacent.get().nameString()
            if partner not in names:
                problems.append(
                    "%s: %s is named but its other face %s is not; both sides "
                    "of the pair must carry the construction"
                    % (construction_name, name, partner))

        # One construction per zone pair, or EnergyPlus mixes it twice.
        for name in names:
            found = model.getSurfaceByName(name)
            if not found.is_initialized():
                continue
            pair = space_pair(found.get())
            if pair is None:
                continue
            other = claimed.get(pair)
            if other is not None and other != construction_name:
                problems.append(
                    "%s and %s both span %s -- EnergyPlus creates mixing per "
                    "construction, so that pair would mix twice"
                    % (other, construction_name, " / ".join(sorted(pair))))
            else:
                claimed[pair] = construction_name

    return problems, assigned


def apply(model, specs):
    """Write the constructions in, and clear anything we wrote before.

    Returns (created, reused, surfaces assigned, surfaces cleared, removed).
    """
    wanted = set()
    for spec in specs:
        wanted.update(names_in(spec))

    # A surface that was an air boundary last run and is not in this report --
    # a corridor the plan no longer cuts in two, say -- keeps its construction
    # forever unless it is taken off.  Only ever our own; a construction
    # someone assigned by hand is left exactly where it is.
    cleared = 0
    mine = ours(model)
    mine_names = {c.nameString() for c in mine}
    for surface in model.getSurfaces():
        construction = surface.construction()
        if not construction.is_initialized():
            continue
        if (construction.get().nameString() in mine_names
                and surface.nameString() not in wanted):
            surface.resetConstruction()
            cleared += 1

    created = reused = 0
    assigned = 0
    for spec in specs:
        name = spec["construction_name"]
        existing = model.getConstructionAirBoundaryByName(name)
        if existing.is_initialized():
            boundary = existing.get()
            reused += 1
        else:
            boundary = openstudio.model.ConstructionAirBoundary(model)
            boundary.setName(name)
            created += 1
        boundary.setComment(MARKER)

        # The report is authoritative: a re-run with a different rate updates
        # a construction that already exists rather than leaving the old one.
        method = spec.get("air_exchange_method", "SimpleMixing")
        boundary.setAirExchangeMethod(method)
        if method == "SimpleMixing":
            boundary.setSimpleMixingAirChangesPerHour(
                float(spec["simple_mixing_ach"]))

        for surface_name in names_in(spec):
            surface = model.getSurfaceByName(surface_name).get()
            surface.setConstruction(boundary)
            assigned += 1

    # A construction left holding nothing is clutter in the object list and
    # shows up in reports as a construction that does nothing.
    removed = 0
    in_use = set()
    for surface in model.getSurfaces():
        construction = surface.construction()
        if construction.is_initialized():
            in_use.add(construction.get().nameString())
    for boundary in ours(model):
        if boundary.nameString() not in in_use:
            boundary.remove()
            removed += 1

    return created, reused, assigned, cleared, removed


def report(model, specs):
    """What each rate actually means, which is the part worth reading.

    ZoneMixing is linear in dT where the buoyancy it stands in for goes as
    dT**1.5, so a fixed rate sets a ceiling on how far the two zones may
    drift.  That ceiling is the number to argue with, not the ACH.
    """
    print("%-44s %6s %8s %9s" % ("construction", "ACH", "V_small", "dT@1kW"))
    print("-" * 70)
    for spec in specs:
        name = spec["construction_name"]
        found = model.getConstructionAirBoundaryByName(name)
        if not found.is_initialized():
            continue
        method = spec.get("air_exchange_method", "SimpleMixing")
        if method != "SimpleMixing":
            print("%-44s %6s %8s %9s" % (name[:44], "none", "-", "-"))
            continue

        ach = float(spec["simple_mixing_ach"])
        volumes = []
        for surface_name in names_in(spec):
            surface = model.getSurfaceByName(surface_name).get()
            if surface.space().is_initialized():
                volumes.append(surface.space().get().volume())
        # EnergyPlus applies the rate to the SMALLER of the two connected
        # zones (Energy+.idd, Construction:AirBoundary field N1).
        smaller = min(volumes) if volumes else 0.0
        flow = ach * smaller / 3600.0
        dt = (1000.0 / (RHO * CP * flow)) if flow > 0 else float("inf")
        print("%-44s %6.2f %8.1f %8.2fK" % (name[:44], ach, smaller, dt))
    print()
    print("  dT@1kW is where the two zones settle under a sustained 1 kW")
    print("  imbalance.  A large split in the RESULTS is a finding about the")
    print("  design -- stratification, or a zone wanting its own equipment --")
    print("  rather than an artefact of this number.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("osm")
    ap.add_argument("boundaries",
                    help="find_air_boundaries.py --apply-json output, "
                         "or '-' to read it from stdin")
    ap.add_argument("--out", help="defaults to overwriting <osm>")
    args = ap.parse_args()

    specs = read_specs(args.boundaries)
    model = load_model(os.path.abspath(args.osm))

    problems, assigned = check(model, specs)
    if problems:
        print("PROBLEMS (%d):" % len(problems))
        for problem in problems:
            print("  %s" % problem)
        sys.exit("\nRefusing to write the model.")

    created, reused, count, cleared, removed = apply(model, specs)
    print("%d construction(s): %d created, %d updated in place"
          % (len(specs), created, reused))
    print("%d surface(s) assigned, %d cleared, %d empty construction(s) removed"
          % (count, cleared, removed))
    print()
    report(model, specs)

    out = os.path.abspath(args.out or args.osm)
    model.save(openstudio.path(out), True)
    print("\nwrote %s" % out)
    print("  %d air-boundary construction(s) now in the model"
          % len(model.getConstructionAirBoundarys()))


if __name__ == "__main__":
    main()
