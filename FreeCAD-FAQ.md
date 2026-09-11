# FreeCAD FAQ

Running log of FreeCAD 1.1.3 usage questions from working on the FloorplanTest
model / FreeCAD-Bridge pipeline. Consolidated on request, before each context
compaction, so the answers survive in one place.

---

## Draft workbench

**Q: How do I reposition a Draft Text label without manually editing XYZ position values?**
Use the Draft **Move** tool (`M`) and drag it in the 3D view, rather than typing
numbers into the Placement/Position property.

**Q: Move is greyed out and no drag anchor appears when I try to move a text label — why?**
Another Draft command's Task panel is still open (e.g. a "Create Text" panel
left mid-flow). Only one Draft command can be active at a time. Close/finish
that task first, then select the object, then Move becomes available.

---

## Expression Editor

**Q: Scaling an imported image with `1' 4" + 3/8" * 144.59` in the Expression
Editor gives "Unit mismatch in plus operation" — why?**
`3/8"` parses as `3 / (8")` — division by an 8-inch quantity — not `0.375"`.
That produces an inverse-length unit, which can't be added to a plain length.
Also, as written, `* 144.59` only scales that `3/8"` term, not the whole sum,
because `*` binds tighter than the implicit `+` between the compound length
terms.

**Q: I tried `(1' 4" + (3/8)") * 144.59` instead and got "Failed to parse
expression" — why?**
FreeCAD's expression grammar won't accept a unit symbol (`"`) directly after a
closing parenthesis — `)"` is invalid syntax, full stop. Use a decimal
instead: `(1' 4" + 0.375") * 144.59`.

---

## Attachment (planes, sketches, images)

**Q: Can a new Sketch be created on the same plane as an existing imported image?**
Yes — set the new Sketch's **Map Mode** (Data tab → Attachment) to the same
plane/face the image uses.

**Q: While editing a sketch on the XZ plane, can I add constraints that
reference geometry in a different sketch on the XY plane?**
Yes, via **External Geometry** — see the External Geometry section below.

**Q: Can an image be imported onto the same plane as an existing sketch?**
Not the same way a Sketch can. **Draft ImagePlane objects do not have the
Attachment extension in FreeCAD 1.1.3** — no Map Mode, no Attachment Offset.
Their only positioning properties are `Base` (Placement: Position/Angle/Axis)
and the `Image Plane` group (Image File, XSize, YSize). To put an image on
the same plane as an existing sketch, copy the sketch's Placement values onto
the image manually — either by hand in the Property editor, or via the Python
console:
```python
img = App.ActiveDocument.getObjectsByLabel("MyImage")[0]
sk  = App.ActiveDocument.getObjectsByLabel("MySketch")[0]
img.Placement = sk.Placement
```
This is a one-time copy, not a live parametric link.

**Q: How do I reverse a sketch plane's normal?**
- If the sketch has an active **Map Mode** (attached to a plane/face): Data
  tab → **Attachment Offset** → set **Angle** to `180` and **Axis** to
  `(1, 0, 0)` (or `(0, 1, 0)`). This rotates 180° about an in-plane axis,
  flipping the local Z (normal) while keeping the sketch on the same plane.
  Do this via Attachment Offset, not Placement directly — Placement gets
  recomputed from the attachment on every recompute.
- If the sketch has **no** Map Mode (e.g. a plain sketch with a manual
  Placement): edit **Placement → Angle/Axis** directly, same 180° trick.

---

## Sketcher metadata

**Q: While working in Sketcher, what options exist for attaching external
metadata to a sketch or its geometry?**
What this project already uses (see `TAGGING.md`): custom `OS_*` properties
on Sketch objects (`App::PropertyString`/`Float`/`Bool`/`Map`, added via
right-click → Add property in the Property editor — type must match exactly
for scripted readers), the `OS_LayerKinds` property paired with the
Elements-panel Layer assignment, and Draft Text labels for per-region data.

**Q: Are there other FreeCAD-native metadata options beyond what the project
already uses?** (None of these are currently read by the bridge pipeline.)
- **Named constraints** — rename a Sketcher constraint (double-click, or
  right-click → Rename) to carry a human-readable tag.
- **Document-level built-in fields** — `Comment`, `Author`, `Company`,
  `License`, `Id`, and a `Meta` `PropertyMap` on the `App::Document` itself.
- **Spreadsheet + Expression Engine** — structured/tabular metadata in a
  Spreadsheet object, linked into other objects' properties via expressions.

---

## External Geometry

**Q: How do I reference geometry from another object/sketch while editing a
sketch?**
Sketch menu → Sketcher geometries → **External geometry** (toolbar icon,
shortcut `G, X`). Pulls edges/points from another object into the sketch
being edited as non-editable reference geometry, projected onto the current
sketch's plane along its normal.

**Q: I'm editing a sketch inside a Part Design Body and want to reference an
edge on a surface, but External Geometry (`G, X`) won't let me pick it — why?**
Checklist, most to least likely:
1. **The surface isn't visible in the 3D view** during sketch edit — select
   it in the tree and hit Space to toggle visibility.
2. **Circular dependency** — the edge belongs to a feature that comes
   *after* this sketch in the tree (depends on the sketch to exist).
   FreeCAD silently refuses these.
3. **You're clicking a silhouette/outline edge of a curved surface**, not a
   real modeled edge — that needs a different mode (dropdown next to the
   External Geometry toolbar button). A flat face's boundary edges are
   always real edges and don't need this.
4. Imprecise click — zoom in tighter than feels necessary.

**Q: Status bar says "This object belongs to another body, can't link" — what does that mean and how do I get the reference anyway?**
Part Design deliberately blocks a sketch's External Geometry from linking
directly to a face/edge owned by a **different Body** than the sketch's own.
Fix — use a **Sub-Shape Binder**:
1. Make the target Body active (or in a plain-Part context, just have your
   Body active).
2. **Select the target edge/face in the 3D view FIRST.**
3. *Then* run Part Design → **Create a sub-object(s) shape binder**.
4. Re-enter your sketch's edit mode and run External Geometry (`G, X`) again,
   referencing the edge on the new Binder object instead of the original —
   it now belongs to your own Body, so it's selectable.

   ⚠️ **Gotcha:** the Sub-Shape Binder tool does **not** open a picker dialog
   if invoked with nothing selected — it silently creates a blank/empty
   Binder with no linked geometry. Selection order matters: select the
   edge/face *before* clicking the tool, not after.

**Q: The Binder object is showing in the 3D view but tiny/hard to click — is
that a zoom problem?**
Usually not the real problem (see the Sub-Shape Binder gotcha above first —
check whether the Binder is actually empty). If it does turn out to be a
scale issue: select the Binder in the tree, then **View → Standard views →
Fit selection**, or scroll-zoom directly onto it before invoking External
Geometry again.

---

## Surfaces vs. solids

**Q: How do I create a surface instead of a solid extrusion?**
- **Part workbench:** select the sketch → **Part → Extrude** → uncheck
  **Solid** in the task panel (or toggle the `Solid` property off afterward).
  Gives a face/shell instead of a solid block.
- **Surface workbench:** switch workbench → **Surface → Filling** (fills a
  closed wire/sketch boundary flat, good for non-extruded "fill in place"
  surfaces), or **Sections**/**GeomFillSurface** for lofted/multi-profile
  surfaces.
- **Part Design (Pad) cannot do this at all** — see below.

**Q: I don't see "Surface → Extrude" in the Surface workbench menu.**
Correct — it doesn't exist. The Surface workbench's actual toolset is
**Filling, GeomFillSurface, Sections, Blend, Reverse Orientation** — no
Extrude command. Extrude only exists in the **Part** workbench.

**Q: I want to extrude a sketch up to a target Face1 — how do I set that as
a reference, or measure the distance to type in?**
Part workbench's **Extrude** has no "up to face" mode — it only takes a
fixed numeric length (that's a Part Design Pad feature, not Part's). Two
ways around it:
- If Face1 is parallel to the relevant plane: use FreeCAD's **Measure** tool
  (Tools menu, or its toolbar icon) to get the exact perpendicular distance,
  then type that number into the Length field.
- If Face1 isn't parallel (angled/sloped target): overshoot the extrusion on
  purpose (with **Create solid** checked), build a second solid representing
  "everything beyond Face1," then **Part → Boolean → Common** (or **Cut**) the
  two together. This is the manual version of what Pad's "Up to Face" does
  internally.

**Q: I switched to Part Design's Pad instead, since it has a native "Up to
face" Type — how do I untoggle the solid property to get a surface?**
You can't — **Part Design Pad has no solid/surface toggle at all.** Part
Design Bodies are solid-modeling only; every feature must resolve to a valid
solid. Workflow to get both "up to face" targeting *and* a surface:
1. Let Pad create the solid (`Type: Up to face`, targeting Face1).
2. Select the specific face you want off the resulting solid.
3. Extract it — see "Extracting a face" below.
4. Hide/delete the solid Pad if you don't need it, keeping just the
   extracted face.

---

## Extracting a face from a solid

**Q: How do I pull a single face off a solid as its own surface object?**
Select the face, then **Part → Copy → Shape Element Copy**. This extracts
just the selected sub-element (face/edge) as a standalone object, and stays
parametrically linked — it updates if the source solid's geometry changes.

⚠️ **Gotchas, in the order I hit them:**
- **"Downgrade" is not a Part workbench command** — it's in the Draft
  workbench, and isn't the right tool for this anyway.
- **Part → Copy → Simple Copy is also the wrong one** — it copies the whole
  selected *object* (e.g. the entire Pad solid), ignoring which sub-element
  you had highlighted. Use **Shape Element Copy** instead, one item below it
  in the same submenu.

---

## Settings worth turning on

**Report View auto-open for macros:** Edit → Preferences → General → Report
View → tick "Show report view on normal message" (and the warning/error
equivalents). The Report View panel then opens itself whenever a macro
prints something, instead of staying hidden with output easy to miss.
