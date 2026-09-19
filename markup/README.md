# Seeding a document from calibration markup

**v1 / prototype.** Reads a minimal calibration layer off a marked-up
drawing set -- a crosshair and a dimension, per sheet -- and produces a new
FreeCAD document with each story's floor plan placed as a calibrated
background image, sitting under an empty sketch at the right elevation --
ready for the engineer to trace wall centerlines into, the same as any other
plan (see the top-level README's "Drawing conventions"). Written for the
request "read the plans and set up the document, instead of a person placing
the reference image by hand."

This does not relax the rule that geometry comes from CAD. Nothing here
traces a wall or reads a room; it places a reference image at exact scale
and position, and an empty sketch on top of it, exactly the part of the
existing `elevations/` workflow that already isn't judgment (see its
README's discussion of hand-placement error).

## The markup convention

Two objects, drawn as PDF annotations (not page content -- see
`read_markup.py`'s docstring for why that matters):

* **a crosshair** -- two short plain line annotations, crossing at one
  point. That point is the sheet's origin, and becomes the story sketch's
  local `(0, 0)`. Bluebeam's own crosshair tool links its two segments by
  `/IRT` (in-reply-to), which `read_markup.py` tries first; a person
  drawing two independent strokes with a plain line tool (Adobe Acrobat's
  "Line" annotation has no such grouping) produces two segments with no
  relationship in the PDF at all, so whatever `/IRT` doesn't account for is
  paired geometrically instead -- one roughly-vertical and one
  roughly-horizontal segment that actually cross, which is the actual
  definition of a crosshair regardless of which tool drew it. **The mark
  has to be a genuine annotation of a distinguishable colour, not black
  ink flattened into the page.** A stamp or freehand tool that bakes
  straight into the raster image leaves nothing in `/Annots` to read at
  all, and if it's drawn in the same black as the rest of the CAD linework
  there is no colour signal left to find it by either -- that combination
  was tried once and correctly produced zero crosshairs, not a wrong one.
* **a dimension** -- a "Length Measurement" / "Distance" line annotation
  (Bluebeam and Adobe Acrobat both produce these, structurally alike) with a
  known real-world length. Gives the sheet's scale, and is read two
  independent ways (the annotation's own typed text, and its `/Measure`
  scale ratio) that are required to agree.

**Colour is not the signal.** The first drawing set used a deliberate
purple (`#8000FF`) convention, drawn in Bluebeam; a second, drawn in Adobe
Acrobat, used Acrobat's own defaults instead (a reddish orange for
dimensions, a violet for sticky-note labels) and inches instead of
feet-and-inches text -- `read_markup.py` reads both without being told which
colour or which unit to expect, because an ordinary architectural dimension
is page *content*, and only a deliberately-run measuring tool produces an
annotation at all. Pass `--color` (or `read_page(..., color=...)`) to
additionally require a specific one, for a drawing set busy enough with
unrelated annotations that the structural signal alone would not be enough.

One crosshair and at least one dimension per sheet is enough to calibrate
the whole sheet -- a flat vector CAD plot is drawn at one uniform scale, so
knowing the origin and the scale on one axis gives every point on the page.
A sheet with no crosshair of its own can still be calibrated -- see
"Borrowing an origin" below.

On the elevation sheet specifically, a *vertical* dimension next to each
story's own drawing, labelled `FIRST FLOOR` / `SECOND FLOOR` in a text
annotation, gives that story's floor-to-floor height directly -- useful
because `fc_seed_labels.py`'s stacking-derivation can only ever supply every
story's height *except* the topmost one (there's no story above it to
measure the gap against).

## Running it

A companion raster (a TIFF verified pixel-register-identical to the PDF):

```powershell
$V = "osvenv\Scripts\python.exe"
$FC = "C:\Program Files\FreeCAD 1.1\bin\python.exe"

& $V markup\crop_plan.py FloorplanTest-01.pdf --tif FloorplanTest-02.tif `
    --out-dir markup\plans
& $FC fc_seed_floorplan.py FloorplanTest-04.FCStd markup\plans\manifest.json
```

Or a "raster PDF" -- one whose drawing is already a flattened image
embedded in the page, with no separate TIFF -- rendered directly, borrowing
an origin from a companion file when this one has no crosshair of its own
(see "Borrowing an origin" below):

```powershell
& $V markup\crop_plan.py FloorplanTest-02-Raster.pdf `
    --origin-from FloorplanTest-01.pdf --out-dir markup\plans
& $FC fc_seed_floorplan.py FloorplanTest-05.FCStd markup\plans\manifest.json
```

or, via the wrapper:

```powershell
.\bridge.ps1 markup-crop  ..\FloorplanTest-01.pdf --tif ..\FloorplanTest-02.tif --out-dir markup\plans
.\bridge.ps1 markup-place ..\FloorplanTest-04.FCStd markup\plans\manifest.json
```

`crop_plan.py` (venv) reads the PDF's `/Annots` via `read_markup.py`, crops
the matching page from a raster source at exact scale, and writes one PNG
per story plus `manifest.json`. `fc_seed_floorplan.py` (FreeCAD) creates or
updates the target document from that manifest and re-derives every `OS_*`
story property through `fc_seed_labels.init_stories()` rather than
duplicating that logic.

Once the plan above is traced and the model exists to place things against,
the elevation sheet's four facade drawings crop and place the same way --
`crop_elevations.py` is `crop_plan.py`'s counterpart for that sheet, and the
existing `fc_place_elevations.py` (top-level README §6a) already consumes a
manifest in this shape, matched by facade name:

```powershell
& $V markup\crop_elevations.py FloorplanTest-01.pdf --tif FloorplanTest-02.tif `
    --out-dir markup\elevations
& $FC fc_place_elevations.py FloorplanTest-04.FCStd markup\elevations\facade_images.json
```

Each of the four elevations carries its own crosshair and dimension on the
sheet, so each is calibrated independently in its own local (distance along
the facade, height above its own finish-floor datum) -- there is no shared
origin to derive the way the two plan pages have one, and no grid-line
correspondence to work out between sheets. Which crosshair belongs to which
compass elevation is read off this page's own vector title text (`NORTH
ELEVATION`, …), matched to the nearest one by position.

`read_markup.py` also runs standalone, for inspecting one page without
building anything:

```powershell
& $V markup\read_markup.py FloorplanTest-01.pdf --page 1
```

## Where the raster comes from

"Two sources, each for what it's good at" -- the same split
`elevations/README.md` uses. Calibration always comes from `--pdf`'s own
`/Annots`, never from anything measured in a raster image -- neither a TIFF
nor a flattened PDF page carries an annotation layer, so the crosshair and
dimension only exist there as pixels, unreadable without OCR (see "Limits").
Where the *pixels* come from is a separate choice:

* **`--tif`**, a companion raster already verified pixel-register-identical
  to the PDF. Verified this session, `FloorplanTest-02.tif` is a 120 dpi
  flattened export of the same 2448x1584 pt PDF pages, pixel-register-
  identical (`4080/2448 == 2640/1584 == 5/3 == 120/72` exactly, and the
  purple pixels in each frame land exactly where that ratio predicts from
  the matching page's `/Annots`). `TIF_PX_PER_PT` in `crop_plan.py` is that
  one verified constant for that one file pair, not a general TIFF
  property -- a different companion raster needs its own relationship
  re-verified before it can be trusted the same way.
* **no `--tif`** renders `--pdf`'s own pages directly at `--render-scale`
  px/pt, which is what a "raster PDF" needs -- there is nothing else to crop
  from, since the drawing is already a flattened image sitting in the page.

## Borrowing an origin

A sheet can carry a dimension with no crosshair -- the second drawing set
this was tried against did, on every page. `--origin-from <pdf>` covers it:
reuse the crosshair from the *same page number* in a companion PDF that has
one, scaled by the ratio of the two files' own independently-measured
drawing scales (`page_ft_per_pt` in `crop_plan.py`). This only works when
the two pages are the same drawing, uniformly scaled from the same page
corner -- true of a page simply re-exported or re-printed at a different
size, not true of an independently re-drawn sheet.

That assumption is checked twice, not once:

* **Automatically, every run.** A dimension line's own endpoint sits close
  to the true wall corner even without a crosshair -- offset only by
  whatever leader/witness distance the person drew it with -- so
  `resolve_origin` refuses when the borrowed point lands more than
  `ORIGIN_BORROW_TOL_FT` (2 ft) from the nearest one on the page it was
  borrowed onto. This does not *prove* the assumption, but it catches a
  gross mismatch (wrong page, a non-uniformly-scaled export) on every
  future run without anyone looking at pixels.
* **By eye, once, when this was first written.** The real proof: rendering
  `FloorplanTest-02-Raster.pdf` and looking for the *original* file's purple
  crosshair, baked into this file's own pixels as ink, at the position the
  borrow predicts -- found there, both pages, within a couple of millimetres
  once anti-aliasing is accounted for. That is what licenses trusting the
  automatic check above at all; it is not something every run re-derives.

## Limits

* **TIFF-only input isn't supported.** A raster image with no paired vector
  PDF carries no `/Annots` to read at all, so calibrating one would need
  colour-thresholding the purple pixels into a crosshair and a dimension,
  plus OCR to read the dimension's printed length -- neither is built. If
  that's ever needed, `elevations/render.py`'s bilevel-tile handling is the
  closest precedent for reading a scanned drawing in this bridge.
* **The north axis is read by eye, not derived.** `crop_plan.py`'s
  `NORTH_AXIS_DEG` is confirmed against this drawing's own north arrow (it
  points straight up), the same way a person would read it -- it is not
  extracted from the markup or any other automated signal. A different
  drawing set needs this re-checked.
* **The page-to-story and page-to-elevation tables are specific to this
  drawing set.** `PAGES` and `LABEL_TO_STORY` in `crop_plan.py` encode which
  PDF page is which story and how the elevation sheet labels its floor
  heights for *this* building (see `elevations/README.md`'s "What the sheets
  actually are" table, which this mirrors) -- a different sheet order or
  label wording needs the table edited, same as `elevations/elev.py`'s
  per-facade constants already are.
* **A brand-new document's image transparency cannot be pre-seeded.**
  `fcbridge.seed_view_defaults` only fills in objects with no view entry of
  their own, and a document that has never been through a GUI save has no
  `GuiDocument.xml` at all -- there is no gap to fill. Opening the document
  and saving is what *creates* that file, and doing so stamps every object,
  images included, with FreeCAD's own default (0%) before this script gets
  another chance to run; re-running it afterwards changes nothing, since the
  images already have an entry by then. Verified, not hypothetical: this is
  the sequence that was tried first. Set it by hand instead -- select each
  image plane, View tab, Transparency -> 70 -- there is no scripted fix for
  a document that started out entirely headless.
* **A plan page needs exactly one crosshair, or exactly one to borrow.**
  `resolve_origin` refuses on zero or more than one, whichever source it
  came from. The elevation sheet is different: its four crosshairs are
  matched to their elevations by title text, not counted against a single
  expected total.
