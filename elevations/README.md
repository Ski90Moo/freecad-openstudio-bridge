# Openings from the drawing set

Reads windows and doors out of `FloorplanTest-01.pdf` and feeds them into the
normal `openings` → `apply` path. Written for the request "place the windows
and doors using the elevation views".

This does **not** relax the rule that geometry comes from CAD. Wall geometry
still comes from the traced FreeCAD sketch; only the openings come from the
drawing set, and they come from its *coordinates*, not from looking at it.

> **This is the one path you cannot run from a clone.** It starts from a PDF
> drawing set, and drawing sets carry title blocks and client identification,
> so none is shipped here — the commands below name the author's files and you
> will need to substitute your own. The *result* of this path is shipped: the
> 28 windows and doors in [`samples/`](../samples/) were placed by it, and the
> four elevation images it worked from are in `facades/`. Everything
> downstream of `openings.json` runs normally against the sample.

## What the sheets actually are

| sheet | content | form |
|---|---|---|
| page 1 | 2nd floor plan | **vector** |
| page 2 | 1st floor plan | **vector** |
| page 3 | four elevations | **raster** line art + vector annotation |

Page 3 is the awkward one: the drawing itself is fifty 1-bit CCITT-G4 tiles at
4.167 px/pt (75 px/ft at the 2x render, 6.25 px/inch), while the grid bubbles,
datum lines, dimension strings and titles are vector on top. So the
*annotation* is exact and the *linework* is measured.

That split is what makes this tractable. The vector annotation pins the
coordinate frame exactly; the raster only ever has to answer "where is the
edge of this filled rectangle", to about a third of an inch.

## Calibration

Anchored on the **column grid lines**, which are vector on every sheet.

Anchoring on the grid *bubbles* instead costs a constant glyph-width error
(+2.64 pt on a digit, +3.36 pt on a letter, the text origin sitting left of
the centred glyph). It cancels on the plan, where only differences are used,
but **not on a mirrored elevation**, where it doubles. It showed up as a
0.60 ft shift between the elevation's doors and the plan's door openings —
consistent to 0.2 in across all three doors, which is what gave it away.

Vertical anchoring is the elevation datum lines. They check out exactly:

| datum | measured | expected |
|---|---|---|
| 0-Top of Footing → 2-Finish Floor | 18.00 pt | 2 ft |
| 2-Finish Floor → 3-Eave | 90.00 pt | 10 ft |
| 2-Finish Floor → 5-Eave | 180.00 pt | 20 ft |

at 9.000 pt/ft, which is 1/8" = 1'-0" at 72 pt/in.

**Plan feet = model feet, no offset.** The traced envelope is the exterior wall
*outer* face, and that face sits on the column grid on three sides: south wall
0.000 = grid A, west wall 0.000 = grid 8, north wall 59.973 = grid E against a
model span of 59.994 (0.25 in over). Note a nearest-line fit cannot establish
this — the two faces of an 8" wall are 0.667 ft apart, so every candidate
offset scores near zero. It has to be done on identified features.

## Two sources, each for what it is good at

* the **plans** are vector, so an opening's width and position along the wall
  are exact. They are read as gaps in the exterior wall line.
* the **elevations** are the only source for sill and head heights.

Where both see the same opening they agree to about half an inch, which is
what licenses snapping the horizontal extent to the plan. Two kinds of opening
have no plan gap and come from the elevation alone: the north clerestory
strips, which sit above the plan's cut plane, and the overhead doors, which
the plan draws closed so the wall line runs through them.

## Running it

```powershell
$V = "..\osvenv\Scripts\python.exe"
$FC = "C:\Program Files\FreeCAD 1.1\bin\python.exe"

& $V render.py                 # strip page 3 to its image layer, rasterise
& $V openings.py               # detect, consolidate, classify -- review this
& $V emit3d.py                 # -> openings_3d.json, pre-checks the host wall
& $FC fc_place_openings.py ..\..\FloorplanTest-02-Openings.FCStd openings_3d.json

cd ..
.\bridge.ps1 openings ..\FloorplanTest-02-Openings.FCStd --out elevations\openings.json
.\bridge.ps1 apply runs\fptest02.osm elevations\openings.json
```

`overlay.py SOUTH ov_south.png` draws the result back onto the elevation it
came from — the quickest check that nothing drifted.

Extra venv packages this needs beyond `openstudio`: `pypdf`, `pypdfium2`,
`pillow`, `numpy`.

## Limits

* **Canopies are not modelled.** Every one is the same CAD component at
  z 7.17–8.77 ft; they are removed by that signature and counted. They are
  real shading and `apply_openings.py` makes subsurfaces only, so if they
  matter they need shading surfaces.
* **A recessed opening is taken at the door leaf**, not the outer face of the
  reveal. All four overhead doors have a 0.75 ft jamb reveal; the leaf is the
  consistent choice and the one three of the four gave anyway.
* **Openings on the lean-to beyond the envelope are skipped** — one door at
  x 167.0–169.9 ft on the south, past the model's 165.677 ft east wall.
* Classification is by size and sill height, so an unusual opening may need
  its type corrected. The label in the FCStd is what decides it.
