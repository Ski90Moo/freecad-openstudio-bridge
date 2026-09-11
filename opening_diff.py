# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""What changed about an opening or a shade, between the model and the drawing.

Pure Python -- no FreeCAD, no openstudio -- so both sides of the bridge can
import it: the FreeCAD-side review macro, which diffs the open document against
the geometry imported from the model, and the venv-side apply scripts, which
diff the model itself.  One implementation, so the preview and the thing it
previews cannot disagree.

This exists because fenestration changes when the plan does.  An architectural
revision moves windows, widens doors, deletes a storefront and adds a canopy,
and `update` reports exactly that for spaces -- unchanged, changed, added,
removed -- while `apply` used to report only how many subsurfaces it touched.
Preserving an opening's handle keeps its construction and its shading control
attached; it does not tell anyone the window moved 400 mm.
"""

# Below these, a difference is arithmetic noise rather than a change.  Vertices
# make a round trip through JSON at six decimal places of a metre, so the floor
# is 1e-6 m; these sit an order of magnitude above it.
MOVE_TOL_M = 1e-4
AREA_TOL_M2 = 1e-5


def centroid(vertices):
    n = float(len(vertices))
    return tuple(sum(v[i] for v in vertices) / n for i in range(3))


def area_m2(vertices):
    """Area of a planar loop in 3D, by the Newell vector."""
    total = [0.0, 0.0, 0.0]
    count = len(vertices)
    for i in range(count):
        a, b = vertices[i], vertices[(i + 1) % count]
        total[0] += a[1] * b[2] - a[2] * b[1]
        total[1] += a[2] * b[0] - a[0] * b[2]
        total[2] += a[0] * b[1] - a[1] * b[0]
    return 0.5 * sum(c * c for c in total) ** 0.5


def distance(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


def compare(before, after, move_tol=MOVE_TOL_M, area_tol=AREA_TOL_M2):
    """(changes, detail) for one opening seen in both the model and the drawing.

    `changes` is a list of words -- retyped, rehosted, moved, resized -- empty
    when nothing differs.  Reporting them separately matters: a window that
    moved is a drawing revision to check against the architect's markup, while
    one that was retyped is a decision somebody made in the document.
    """
    changes, detail = [], {}

    if before.get("type") != after.get("type"):
        changes.append("retyped")
        detail["type"] = (before.get("type"), after.get("type"))

    if before.get("host") != after.get("host"):
        changes.append("rehosted")
        detail["host"] = (before.get("host"), after.get("host"))

    old, new = before.get("vertices") or [], after.get("vertices") or []
    if old and new:
        shift = distance(centroid(old), centroid(new))
        if shift > move_tol:
            changes.append("moved")
            detail["moved_m"] = shift

        growth = area_m2(new) - area_m2(old)
        if abs(growth) > area_tol:
            changes.append("resized")
            detail["area_m2"] = (area_m2(old), area_m2(new))
            detail["area_delta_m2"] = growth
    elif old or new:
        changes.append("reshaped")

    if len(old) != len(new) and "reshaped" not in changes:
        changes.append("reshaped")
        detail["vertices"] = (len(old), len(new))

    return changes, detail


def describe(name, changes, detail):
    """One review line for a changed opening, with the numbers that matter."""
    parts = []
    for change in changes:
        if change == "retyped":
            parts.append("%s -> %s" % detail["type"])
        elif change == "rehosted":
            parts.append("host %s -> %s" % detail["host"])
        elif change == "moved":
            parts.append("moved %.0f mm" % (detail["moved_m"] * 1000.0))
        elif change == "resized":
            parts.append("%.2f -> %.2f m2 (%+.2f)"
                         % (detail["area_m2"][0], detail["area_m2"][1],
                            detail["area_delta_m2"]))
        else:
            parts.append(change)
    return "%-26s %s" % (name, ", ".join(parts))


def diff(before, after):
    """Diff two {id: record} maps into changed / added / removed.

    Keyed on the drawn id, so an opening that was renamed by a retype is one
    change rather than a delete and an add.
    """
    changed, unchanged = [], []
    for key, new in after.items():
        old = before.get(key)
        if old is None:
            continue
        changes, detail = compare(old, new)
        if changes:
            changed.append((new.get("name") or old.get("name"),
                            changes, detail))
        else:
            unchanged.append(new.get("name") or old.get("name"))
    added = [v.get("name") for k, v in after.items() if k not in before]
    removed = [v.get("name") for k, v in before.items() if k not in after]
    return {
        "changed": sorted(changed, key=lambda row: row[0] or ""),
        "unchanged": sorted(n for n in unchanged if n),
        "added": sorted(n for n in added if n),
        "removed": sorted(n for n in removed if n),
    }


def report(result, noun="opening"):
    """The review block both the macro and the apply scripts print."""
    lines = ["%-10s %d" % ("unchanged", len(result["unchanged"])),
             "%-10s %d" % ("changed", len(result["changed"])),
             "%-10s %d" % ("added", len(result["added"])),
             "%-10s %d" % ("removed", len(result["removed"]))]
    if result["changed"]:
        lines.append("")
        lines.append("changed %ss:" % noun)
        for name, changes, detail in result["changed"]:
            lines.append("  " + describe(name, changes, detail))
    for label in ("added", "removed"):
        if result[label]:
            lines.append("")
            lines.append("%s: %s" % (label, ", ".join(result[label])))
    return lines
