# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Classify a corridor footprint as a rectangle, L, T or cross, and find the
straight cut(s) that split it into per-leg rectangles.

Pure Python -- no FreeCAD, no openstudio -- so it is importable and
unit-testable from both sides of the bridge: `split_corridors.FCMacro` (under
FreeCAD's own Python) calls it to find where to draw new wall-centerline
edges, before any of this project's 3D geometry exists; `find_air_boundaries.py`
(under the bridge venv) imports CORRIDOR_PATTERN from here so both sides
recognise a corridor by the same regex.

Why split at all: a T/L/cross-shaped corridor is a single non-convex room,
and `find_air_boundaries.py`'s corridor rule measures a wall against the
footprint's *one* minimum-area bounding rectangle -- which cannot represent
more than one leg's own width at a time.  On a real T-shaped corridor this
silently rejects genuine corridor-end openings.  Splitting the room into
rectangles before that rule ever runs removes the problem at the source
instead of teaching the rule about multi-leg footprints.  It also gives
EnergyPlus a convex zone instead of one it will warn about.

Only three topologies are handled -- T, L and cross (+) -- because those are
the shapes this is scoped to.  Anything else (U, Z, zigzag, a footprint whose
walls are not all parallel or perpendicular to one another, 3 or 5+ reflex
corners) is reported and left alone; it needs a person, not a guess.

The classification counts reflex (concave) corners, which is enough to tell
these shapes apart for a simple rectilinear polygon: a rectangle has none, an
L exactly one, a T exactly two (and they sit at the same coordinate, being
the two inner corners where a stem meets a bar), a cross exactly four (two
matched pairs).  Every candidate cut is *verified* by actually performing the
split and checking the pieces are rectangles -- a shape that merely has the
right reflex count but isn't really one of these three is refused rather
than guessed at.
"""

import math

CORRIDOR_PATTERN = r"corridor|hallway|\bhall\b|passage|circulation|breezeway"

# Real drawings are on the order of metres; both a "same point" and a
# "same line" test use this.  Loose enough to absorb ordinary floating-point
# noise from FreeCAD's own geometry kernel, tight enough that no real wall
# is ever this close to another without actually being coincident with it.
TOL_M = 1e-4

RECTANGLE = "rectangle"
UNSUPPORTED = "unsupported"


def _cross(o, a, b):
    """Signed turn at `a`, going o->a->b.  >0 left (convex on a CCW ring),
    <0 right (reflex), ~0 collinear/reversed."""
    ox, oy = a[0] - o[0], a[1] - o[1]
    nx, ny = b[0] - a[0], b[1] - a[1]
    return ox * ny - oy * nx


def _signed_area(poly):
    total = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _to_local(poly):
    """Rotate `poly` so every edge is horizontal or vertical in the
    returned frame, using the longest edge as the reference direction --
    the same reason `footprint_axes` in find_air_boundaries.py measures on
    a footprint's own principal axis rather than on x/y: the building (and
    so the corridor) need not be drawn on a 0-degree north axis.

    Returns (local_poly, to_world) or None if no single reference direction
    makes every edge axis-aligned -- i.e. the room's walls are not all
    parallel or perpendicular to one another, which puts it out of scope
    immediately.
    """
    n = len(poly)
    if n < 4:
        return None
    edges = []
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < TOL_M:
            return None
        edges.append((length, dx / length, dy / length))
    _, ux, uy = max(edges, key=lambda e: e[0])

    def to_local(pt):
        x, y = pt
        return (x * ux + y * uy, -x * uy + y * ux)

    local = [to_local(p) for p in poly]
    for i in range(n):
        u0, v0 = local[i]
        u1, v1 = local[(i + 1) % n]
        if abs(u1 - u0) > TOL_M and abs(v1 - v0) > TOL_M:
            return None

    def to_world(pt):
        u, v = pt
        return (u * ux - v * uy, u * uy + v * ux)

    return local, to_world


def _reflex_indices(poly):
    n = len(poly)
    out = []
    for i in range(n):
        turn = _cross(poly[i - 1], poly[i], poly[(i + 1) % n])
        if turn < -TOL_M:
            out.append(i)
    return out


def _collapse_collinear(poly):
    pts = list(poly)
    changed = True
    while changed and len(pts) > 3:
        changed = False
        n = len(pts)
        for i in range(n):
            if abs(_cross(pts[i - 1], pts[i], pts[(i + 1) % n])) < TOL_M:
                del pts[i]
                changed = True
                break
    return pts


def _is_rectangle(poly):
    """True when `poly`'s edges (already known axis-aligned, since every
    piece here is built from axis-aligned source material) collapse to
    exactly four convex corners."""
    pts = _collapse_collinear(poly)
    if len(pts) != 4:
        return False
    return all(_cross(pts[i - 1], pts[i], pts[(i + 1) % 4]) > TOL_M
              for i in range(4))


def _rect_area(poly):
    pts = _collapse_collinear(poly)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def _point_eq(a, b):
    return abs(a[0] - b[0]) <= TOL_M and abs(a[1] - b[1]) <= TOL_M


def _on_segment(a, b, pt):
    ax, ay = a
    bx, by = b
    px, py = pt
    seg_len = math.hypot(bx - ax, by - ay)
    if seg_len < TOL_M:
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) / seg_len > TOL_M:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    return -TOL_M <= dot <= seg_len * seg_len + TOL_M


def _locate_on_boundary(poly, pt):
    """('vertex', i) if pt is poly[i]; ('edge', i) if pt lies strictly
    inside the edge poly[i]->poly[i+1]; else None."""
    n = len(poly)
    for i in range(n):
        if _point_eq(poly[i], pt):
            return ("vertex", i)
    for i in range(n):
        if _on_segment(poly[i], poly[(i + 1) % n], pt):
            return ("edge", i)
    return None


def _insert_point(poly, pt):
    loc = _locate_on_boundary(poly, pt)
    if loc is None:
        return None
    kind, i = loc
    if kind == "vertex":
        return list(poly), i
    return poly[:i + 1] + [pt] + poly[i + 1:], i + 1


def _point_strictly_inside(poly, pt):
    """Even-odd ray cast.  Used only on chord midpoints, which are never
    meant to land on the boundary -- a chord whose midpoint is outside (or
    on) the boundary is cutting through empty space or retracing a wall,
    not slicing the room."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_int:
                inside = not inside
    return inside


def _split_polygon_by_chord(poly, p, q):
    """Split simple polygon `poly` along the straight chord p-q into two
    simple polygons.  Both p and q must already lie on the boundary (as an
    existing vertex or strictly inside an existing edge); the chord's own
    midpoint must lie strictly inside the room, so a chord that only
    grazes the boundary from outside is refused.  Returns (poly_a, poly_b)
    or None.
    """
    inserted = _insert_point(poly, p)
    if inserted is None:
        return None
    ring, pi = inserted
    inserted = _insert_point(ring, q)
    if inserted is None:
        return None
    ring, qi = inserted
    if pi == qi:
        return None

    mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
    if not _point_strictly_inside(poly, mid):
        return None

    lo, hi = sorted((pi, qi))
    arc_a = ring[lo:hi + 1]
    arc_b = ring[hi:] + ring[:lo + 1]
    return arc_a, arc_b


def _cast_ray(poly, origin, axis, sign):
    """Nearest point where an axis-aligned ray from `origin` (along `axis`,
    'u' or 'v', in direction `sign`) meets an edge of `poly` running the
    other way -- the first wall a straight cut from a reflex corner would
    hit.  None if the ray never meets the boundary going that way.
    """
    ox, ov = origin
    best = None
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if axis == "u":
            if abs(a[0] - b[0]) > TOL_M:
                continue
            edge_u = a[0]
            lo, hi = sorted((a[1], b[1]))
            if not (lo - TOL_M <= ov <= hi + TOL_M):
                continue
            delta = (edge_u - ox) * sign
        else:
            if abs(a[1] - b[1]) > TOL_M:
                continue
            edge_v = a[1]
            lo, hi = sorted((a[0], b[0]))
            if not (lo - TOL_M <= ox <= hi + TOL_M):
                continue
            delta = (edge_v - ov) * sign
        if delta <= TOL_M:
            continue
        if best is None or delta < best[0]:
            hit = (edge_u, ov) if axis == "u" else (ox, edge_v)
            best = (delta, hit)
    return best[1] if best else None


def _try_l(poly, reflex_idx):
    """Every valid rectangular split of a single-reflex-corner room,
    extending each of the two edges meeting at the reflex corner straight
    across the room.  0, 1 or 2 candidates come back verified; the caller
    picks among however many survive.
    """
    n = len(poly)
    r = poly[reflex_idx]
    prev_pt = poly[reflex_idx - 1]
    next_pt = poly[(reflex_idx + 1) % n]

    rays = []
    if abs(prev_pt[1] - r[1]) <= TOL_M:
        rays.append(("u", 1 if r[0] > prev_pt[0] else -1))
    else:
        rays.append(("v", 1 if r[1] > prev_pt[1] else -1))
    if abs(next_pt[1] - r[1]) <= TOL_M:
        sign = 1 if next_pt[0] > r[0] else -1
        rays.append(("u", -sign))
    else:
        sign = 1 if next_pt[1] > r[1] else -1
        rays.append(("v", -sign))

    valid = []
    for axis, sign in rays:
        hit = _cast_ray(poly, r, axis, sign)
        if hit is None:
            continue
        pieces = _split_polygon_by_chord(poly, r, hit)
        if pieces and all(_is_rectangle(p) for p in pieces):
            valid.append((r, hit))
    return valid


def _axis_aligned_pair(a, b):
    return abs(a[0] - b[0]) <= TOL_M or abs(a[1] - b[1]) <= TOL_M


def _try_t(poly, reflex_indices):
    i, j = reflex_indices
    a, b = poly[i], poly[j]
    if not _axis_aligned_pair(a, b):
        return None
    pieces = _split_polygon_by_chord(poly, a, b)
    if pieces and all(_is_rectangle(p) for p in pieces):
        return [(a, b)]
    return None


def _apply_cuts(poly, chords):
    """Apply each chord to whichever current piece it actually splits.
    Used for the cross case, where the two chords belong to two different
    pieces once the first one has been cut."""
    pieces = [poly]
    for p, q in chords:
        new_pieces = []
        used = False
        for piece in pieces:
            if not used:
                result = _split_polygon_by_chord(piece, p, q)
                if result:
                    new_pieces.extend(result)
                    used = True
                    continue
            new_pieces.append(piece)
        if not used:
            return None
        pieces = new_pieces
    return pieces


def _try_cross(poly, reflex_indices):
    """Search the three ways to pair up four reflex corners for two
    disjoint matched chords that cut the room into exactly three
    rectangles -- the minimum partition of a plus shape: one full bar
    spanning two opposite arms, plus the other two arms as caps."""
    a, b, c, d = reflex_indices
    pairings = (((a, b), (c, d)), ((a, c), (b, d)), ((a, d), (b, c)))
    for (i1, j1), (i2, j2) in pairings:
        p1, q1 = poly[i1], poly[j1]
        p2, q2 = poly[i2], poly[j2]
        if not (_axis_aligned_pair(p1, q1) and _axis_aligned_pair(p2, q2)):
            continue
        pieces = _apply_cuts(poly, [(p1, q1), (p2, q2)])
        if pieces and len(pieces) == 3 and all(_is_rectangle(p) for p in pieces):
            return [(p1, q1), (p2, q2)]
    return None


def classify_and_cut(vertices):
    """Classify a room footprint and find its rectangle-split cuts.

    `vertices` is a plain list of (x, y) in metres, matching a plan.json
    space's own `vertices` field (or `fcbridge.face_polygon_m`'s output).
    Winding does not matter -- it is normalised here.

    Returns (kind, cuts):
      ("rectangle", None)       -- already a rectangle, nothing to do.
      ("unsupported", reason)   -- not a clean L/T/cross; left alone.
      ("l" | "t" | "cross", [(p, q), ...]) -- cut endpoints, in the same
        (x, y) metres frame as `vertices`, one pair per straight cut to draw.
    """
    poly = [(float(x), float(y)) for x, y in vertices]
    if _signed_area(poly) < 0:
        poly = list(reversed(poly))

    local = _to_local(poly)
    if local is None:
        return (UNSUPPORTED,
                "walls are not all parallel or perpendicular to one another")
    local_poly, to_world = local

    reflex = _reflex_indices(local_poly)
    r = len(reflex)

    if r == 0:
        return (RECTANGLE, None)

    if r == 1:
        candidates = _try_l(local_poly, reflex[0])
        if not candidates:
            return (UNSUPPORTED,
                    "one reflex corner, but no clean rectangular split "
                    "found -- not a simple L")
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            scored = []
            for cand in candidates:
                pieces = _split_polygon_by_chord(local_poly, *cand)
                areas = sorted(_rect_area(p) for p in pieces)
                balance = areas[1] - areas[0]
                scored.append((balance, cand))
            scored.sort(key=lambda t: t[0])
            chosen = scored[0][1]
        p, q = chosen
        return ("l", [(to_world(p), to_world(q))])

    if r == 2:
        cuts = _try_t(local_poly, reflex)
        if not cuts:
            return (UNSUPPORTED,
                    "two reflex corners, but they are not a matched "
                    "T cross-section")
        return ("t", [(to_world(p), to_world(q)) for p, q in cuts])

    if r == 4:
        cuts = _try_cross(local_poly, reflex)
        if not cuts:
            return (UNSUPPORTED,
                    "four reflex corners, but not a clean symmetric "
                    "cross junction")
        return ("cross", [(to_world(p), to_world(q)) for p, q in cuts])

    return (UNSUPPORTED, "%d reflex corners -- not a T, L, or cross" % r)
