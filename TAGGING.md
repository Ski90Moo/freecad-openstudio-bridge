# What to tag in FreeCAD

Everything the bridge needs is carried by **five** things. Nothing else in the
document is read — images, layers, construction lines, dimensions and scratch
sketches are all ignored.

| # | Carrier | How many | Tags it carries |
|---|---|---|---|
| 1 | the **Document** | one | `OS_NorthAxis_deg` |
| 2 | a **Sketch**, one per story | one per story | `OS_StoryName`, `OS_Elevation_m`, `OS_FloorToFloor_m`, `OS_Include` |
| 3 | a **Draft Text**, one per enclosed region | 43 on the test file | first text line = `NUM \| Name`; hidden `OS_SpaceId`; optional `OS_Height_m` |
| 4 | a **drawn outline**, one per window/door | later, Phase 3 | the object's **Label**, prefixed `WINDOW` / `DOOR` / … |
| 5 | a **roof shape**, if the roof is not flat | zero or one | `OS_RoofMethod`; for an attic, `OS_StoryName` and `OS_SpaceName` |

Items 1 and 2 are custom properties you type once per project. Item 3 is the
one with real work in it — that is where your judgment about the building
goes. Item 4 does not apply until you re-open the built geometry, and item 5
should be settled before you get there.

**You do not create any of these properties by hand.** Run

```powershell
& "C:\Program Files\FreeCAD 1.1\bin\python.exe" fc_seed_labels.py YourPlan.FCStd --init-stories
```

and the `OS_*` properties appear on the document and on every sketch, ready to
fill in from the Data tab. Doing it manually via right-click → *Add property*
works too, but the types have to match exactly.

---

## 1. The document — north

Select nothing, click the document name at the top of the model tree; the Data
tab shows document properties.

| Property | Type | Meaning |
|---|---|---|
| `OS_NorthAxis_deg` | Float | Degrees the building is rotated from true north |

Set this **once per project**. Geometry stays drawn to plan north — you do not
rotate the sketch. `0` means plan-up is true north; the value goes straight
into `OS:Building` North Axis, so shading and solar gains land correctly.

**Set it as soon as you know it, and check it before you simulate.** Nothing in
the geometry pipeline reads it — `setNorthAxis` moves no vertex, normal,
azimuth or area, so there is no reason to defer it. What it changes is where
the sun is at simulation time. Get it wrong and solar gains, shading and
daylighting are all wrong, with no error, no warning and a model that still
validates cleanly.

Read it off the drawing's north arrow: **degrees clockwise from true north to
plan-up**, i.e. to the sketch's +Y. A plan drawn with north up is `0`; a
building whose plan-up points 30° east of north is `30`.

---

## 2. Each story sketch — elevation, height, include

Select the sketch (`Sketch`, `Sketch001`, …) → Data tab → **OpenStudio** group.

| Property | Type | Example | What it does |
|---|---|---|---|
| `OS_StoryName` | String | `Level 1` | Names the `OS:BuildingStory`; also groups labels and the imported geometry tree |
| `OS_Elevation_m` | Float | `0.0` | Floor elevation in **metres**. Negative = below grade, which flips that story's exterior walls to `Ground` |
| `OS_FloorToFloor_m` | Float | `4.2` | **Metres.** Height the spaces are extruded to, unless a room overrides it with `OS_Height_m` |
| `OS_Include` | Bool | ✔ | Unticked sketches are ignored entirely |

Three things worth knowing:

**`OS_Include` is the on/off switch.** A sketch without it ticked is invisible
to the bridge, so reference traces, site outlines and abandoned attempts can
stay in the document without being cleaned up.

**Elevation is the floor, not the slab or the ceiling.** Stack them: `0.0`,
then `4.2` if Level 1 is 4.2 m floor-to-floor. A basement is `-3.0`.

**`OS_FloorToFloor_m` is used as the space height directly.** The bridge
extrudes each room the full floor-to-floor distance rather than modelling a
separate plenum — normal practice for a first-pass model, and the same thing
the OpenStudio App does from a floor diagram. If you want a real plenum, draw
it as its own story. A single room that is *not* this height — a
double-height lobby, a warehouse bay open to the roof — overrides it with
`OS_Height_m` on its own label; see section 3.

Sketch **Placement** is not something you tag — it is read, and each sketch's
own placement is its origin, rotation included. Lay the stories out side by
side on the sheet or stack them at their real elevations; both work.

Stacking makes the stories share a footprint, so a Level 1 label also falls
inside a Level 2 room. Each label is therefore assigned to the story plane it
is **nearest** to before any in-plane test runs. Keep a story's labels in that
story's plane — which is where `fc_seed_labels.py` puts them.

---

## 3. Each enclosed region — a Draft Text label

This is the important one. **Every enclosed region gets exactly one label — no
exceptions.** The exporter refuses to run otherwise.

### Getting the labels in place

```powershell
& "C:\Program Files\FreeCAD 1.1\bin\python.exe" fc_seed_labels.py YourPlan.FCStd
```

drops a placeholder into every unlabeled region, largest room first:

```
?? | Room 1 (262.4 m2)
```

You then edit each one in the GUI. Renaming 43 placeholders is a very
different job from creating 43 labels and positioning them.

**Then run `label_style.FCMacro`** (Macro → Execute) with the document open.
A headless session has no ViewObjects, so the seeder cannot set text size —
the new labels carry Draft's default height of a few millimetres, which
against a 50 m building is about one part in sixteen thousand. They are there,
they are placed correctly, and they are far too small to see. The macro sets
them to `OS_LabelFontSize_mm` (400 mm by default, adjustable with
`--font-mm`) and colours the ones still named `??` red so you can see what is
left to do.

### The text

First line only. Everything after line 1 is free for your own notes.

```
105 | Office
RR-203 | Restroom
HALL-1 | Corridor
Warehouse
```

`NUMBER | Name`. The number is optional — a bare `Warehouse` works, and
`102 Office` without the pipe is understood too — but a number is what makes
the OpenStudio names line up with the drawing set:

```
105 | Office    ->    space "007-105-Office" , zone "Zone 007-105-Office"
```

Only characters OpenStudio tolerates survive (`A-Z a-z 0-9 space _ . -`), so
avoid `#`, `/`, `&` and quotes in room names.

**The name is read, not just carried.** Two rules in
`find_air_boundaries.py` key off it, so spelling matters where it would
otherwise be free:

| name matches | consequence |
|---|---|
| `mezzanine`, `balcony`, `gallery`, `catwalk` | its walls onto a taller space become air boundaries |
| `corridor`, `hallway`, `hall`, `passage`, `circulation`, `breezeway` | walls slicing square across its run become air boundaries |
| `open stair` | its long sides onto a taller space or a mezzanine become air boundaries |

All are case-insensitive and match anywhere in the name, so `Hallway 6`,
`Service Corridor` and `Open Stair 2` are all found. None fires on its own:
each has geometric conditions on top (see README §5a), so naming a room
`Corridor` cannot by itself dissolve a wall. Conversely a corridor labelled
`Circulation Spine` is invisible to the rule — pass `--corridor-pattern` if
your naming differs.

`Open Stair` is the one that is purely a declaration. A plain `Stair` is
assumed **enclosed** and gets nothing, because a fire-rated stair with a
self-closing door is the common case and an air boundary there would be wrong
in a way the results would not obviously show. Even once declared open, only
the stair's *sides* are taken — its ends depend on which way the flight runs,
which is drawn as an arrow the bridge cannot read.

### Where the label goes

Matching is **point-in-polygon on `Placement.Base`**, the text insertion point.
Put it clearly inside the room, away from walls. Height above the story plane
is ignored, so it does not matter what Z the text floats at.

`label_style.FCMacro` sets Justification to Center, so the text is drawn
centred on that point: **what you see is what gets tested**, exactly so
horizontally. Vertically the anchor is on the baseline, so a line of text
rides a little above it — Draft Text exposes no vertical-alignment property.

A label you create by hand and do not run the macro over uses Draft's default
**Left** justification, where the anchor is at the start of line 1 and the text
runs off to the right. In that state the visual middle is *not* the tested
point. Re-running the macro re-centres everything, which is the simplest way to
keep the two in agreement.

Seeded labels start at the room's interior point — its centroid where the room
is convex, otherwise the point furthest from any wall — so they are already
correct and you only need to worry about this if you move one.

Two failure modes, both reported by name rather than guessed at:

- **no label inside a region** → export stops. This is deliberate: a corridor
  is often the *absence* of a drawn room, and that is exactly what got
  silently dropped when geometry came from a PDF.
- **two labels inside one region** → export stops. Usually a label that drifted
  through a wall, or a leftover placeholder next to its replacement.

### Rooms that are not the story's height

`OS_Height_m` on a room's label overrides `OS_FloorToFloor_m` for that room
alone. **`0.0` means defer to the story**, which is what it is set to on every
label — so the field is already sitting in the Data tab's OpenStudio group
when you want it, with no "add a property" dialog to go through.

Select the label → Data → OpenStudio → `OS_Height_m` → type the height in
metres. Use it for a room whose ceiling is not where its story's is:

| Situation | Value |
|---|---|
| double-height lobby; Level 1 f2f 3.38 + Level 2 f2f 2.71 | `6.09` |
| warehouse bay open to the roof | its real clear height |
| ordinary room | `0.0` — leave it alone |

Overrides are listed by name on every export and every build, so a room that is
not its story's height can never be one quietly.

Two things follow from raising a room, both of them wanted:

- **Its upper walls stop being exterior.** `intersectSurfaces` splits them at
  the story line, so the part above now bounds whichever upstairs rooms
  overlook the void. On `FloorplanTest-02`, raising three rooms moved 28 wall
  surfaces from `Outdoors` to matched interior — they had been wrongly
  exterior before.
- **Only that room rebuilds.** The override joins the change-propagation hash
  only when it is set, so setting one re-exports as exactly one changed room
  and leaves every other space, zone and HVAC assignment untouched. A document
  that predates the property hashes exactly as it did before.

The region *above* the void is a different question: it gets `SKIP` (next
section), not a height. Height and skip are the two independent answers —
*how tall is this space* and *is this a space at all*.

### Regions that are not rooms

Not every enclosed region is a space — the floor area outside a partial
mezzanine, an open-to-below void, a courtyard inside the envelope. Mark those
instead of deleting the label:

```
SKIP | open to below
SKIP | mezzanine void
- | courtyard
OPEN TO BELOW
```

`SKIP`, `-`, `OPEN TO BELOW` and `NOTASPACE` all work, in either field, in any
case. The region is excluded from the model and its area is reported, so the
decision is written down instead of leaving a hole nobody can see.

### `OS_SpaceId` — do not touch it

The exporter adds a hidden `OS_SpaceId` string property to each label the
first time it sees it, mints a UUID, and saves the document. **That UUID is the
identity of the room.** It is what lets you move a wall six months from now and
rebuild two spaces instead of regenerating all 43 and losing every thermostat,
terminal and schedule.

Rules:

- Never edit or clear it on a room you want to keep.
- **After copy-pasting a label, clear `OS_SpaceId` to empty** on the copy. A
  duplicated Draft Text carries the original's id, which would collapse two
  rooms onto one identity. The exporter detects this and stops, naming both
  rooms — but clearing the field is the fix.
- Renaming a room is free. `105 | Office` → `105 | Conference` keeps the same
  id, so the space is renamed rather than replaced.
- Deleting a label deletes the space. Its thermal zone is reported as orphaned
  rather than silently dropped.

---

## 4. Windows and doors — later, on the 3D geometry

Not on the floor plan. After the model is built you re-open it in FreeCAD
(`dump_osm_geometry.py` → `fc_import_surfaces.py`), draw a closed outline on
the actual wall face, and name it by **prefixing the object Label**:

| Label starts with | Becomes |
|---|---|
| `WINDOW` | FixedWindow |
| `OPWINDOW` | OperableWindow |
| `DOOR` | Door |
| `GLASSDOOR` | GlassDoor |
| `OVERHEAD` | OverheadDoor |
| `SKYLIGHT` | Skylight |

Anything after the keyword is free text and becomes part of the subsurface
name — `WINDOW north bay 3`. The host wall is found geometrically (the outline
must be coplanar with, and inside, exactly one surface), so there is no surface
name to keep in sync and no way to attach an opening to the wrong wall without
being told.

### `OS_OpeningId` — the identity that keeps handles alive

Every drawn outline gets a UUID, minted into the sketch geometry on its first
export and never changed after. It is the fenestration equivalent of
`OS_SpaceId`: the anchor that says *this is the same opening as last time*.

It matters because of what OpenStudio does with handles. Without an id,
`apply` had to delete every subsurface it had made and rebuild them, so every
run minted new handles — and anything referencing one (a shading control, a
frame and divider, an interzone pairing) pointed at an object that no longer
existed, with no error anywhere. With an id, an opening `apply` has seen before
is **updated in place** — vertices, name, type, even its host wall — and its
handle survives. Only openings the drawing no longer contains are removed.

The id rides on the geometry as a `Part::GeometryStringExtension` named
`OS_OpeningId`, alongside any type tag, and comes back on the imported object
as an `OS_OpeningId` property. Nothing to set by hand; the first export after
you draw an outline writes it, and says so:

```
minted an id for 28 outline(s) and wrote them into FloorplanTest-05.FCStd
  Revert in FreeCAD before editing it further.
```

That is the one time the export writes to your document. Later exports find
the ids already there and change nothing. Canopies work identically, under
`OS_ShadingId` — a photovoltaic generator attached to a canopy references it by
handle, so the same protection applies.

**If the id in the model is lost** — a stray edit strips the comment it lives
in — the opening is *adopted*, not duplicated: an unowned subsurface sitting on
exactly the outline about to be created is matched on shape, updated in place
and re-stamped, so its handle survives. Without that, the orphan stayed on the
wall and a second subsurface was created beside it, doubling the glazing area
with no error at all. Measured: 28 became 29. Adoptions are reported by name.

### `OS_PendingType` — the one field that answers back

Tagging an outline leaves the model's own object showing the old type, with no
sign anything happened. So `tag_opening.FCMacro` writes a **pending marker** on
it: an `OS_PendingType` property, and a suffix on the tree label.

```
Label                GlassDoor South 10  [-> Door]
OS_SubSurfaceType    GlassDoor        <- what the model holds. untouched.
OS_PendingType       Door             <- what your outline now says.
```

The real fields are deliberately left alone. They are what `verify` compares
against the model, and overwriting them would put a second source of truth on
the object whose whole job is to be a reflection. The marker is a *third*
thing: not the model, not the drawing, but the difference between them.

It clears itself. Retag to match, or apply the change, and both the property
and the label suffix disappear — so an object still carrying one always has
something outstanding. Clearing a tag entirely does not clear the marker; it
shows whatever the shape classifier then decides, which may still differ.

### The other `OS_*` fields on imported geometry are read-only

Everything under **Open Studio** in the property panel of a `Surface N`,
`GlassDoor South 10` or `Shading South 01` object is **written from the model,
never read back**. Those objects are the model's reflection in FreeCAD, not
your drawing:

```
your sketch  ->  openings  ->  .osm  ->  dump  ->  import  ->  the object
                                                               you can click
```

So editing `OS_SubSurfaceType` from `Door` to `GlassDoor` in the panel changes
nothing: `fc_export_openings.py` skips every object carrying that property (it
is the guard that stops the model's own subsurfaces being re-applied as if
freshly drawn), and the next `import` overwrites the field anyway. Retype the
**outline**, not the reflection — either of the two ways below.

`verify` compares these labels against the model as well as the vertices, so
an edit like that is reported by name rather than quietly discarded:

```
GlassDoor South 10: OS_SubSurfaceType reads 'Door' but the model says
'GlassDoor' -- these are written from the model, not read back; re-run import
```

### Overruling one outline: `tag_opening.FCMacro`

The Label rule types a whole sketch. To retype **one outline among many**,
select it and run the macro, then pick a type from the list. Two ways to
select, for the two moments you would want to:

| when | what you select |
|---|---|
| tagging as you draw | the outline's edges, while editing the sketch |
| after seeing the import | the subsurface in the tree — `GlassDoor South 10` |

The second is the normal one. Most of the time you let the classifier do the
work, look at the result, and fix the two it got wrong — and at that moment
the thing in front of you is the imported object. The macro walks back from it
to the outline you drew and tags that, since the imported object is only a
reflection.

Nothing is capped: one sketch can hard-specify a glass door, an overhead door
and an operable window at once. Everything left untagged is still classified by
shape, and picking `(classify by shape)` removes a tag. The export's `typed by`
column reads `tag` for the ones you decided and `shape` for the rest, so it is
visible at review time which is which.

The type rides on the geometry itself, as a `Part::GeometryStringExtension`
named `OS_SubSurfaceType` — document data, so it survives a save, a drag and a
new constraint, and it follows its element when other geometry is deleted. One
outline tagged with two different types is refused rather than guessed at.

**There is no UI for it anywhere in FreeCAD.** A geometry extension is not a
property, and the Elements panel lists `34-Line` with nowhere to put a tag, so
a tagged outline looks exactly like an untagged one. Two ways to see them:

* **run `tag_opening.FCMacro` with nothing selected** — it lists every tagged
  outline in the document and changes nothing;
* **run `review_openings.FCMacro`** — the full table of what each outline
  would become, with a `typed by` column reading `tag`, `label` or `shape`.
  This is the one to trust, because it runs the export's own classifier: a tag
  can be written and still decide nothing (it is on construction geometry, or
  its outline is not closed, or it lands in no wall). Nothing is written and
  nothing is saved, and it reads the **open** document, so you see the tag you
  just applied before saving.

Every tagging run also prints the sketch's tally before and after, and warns if
they are the same, so a run that changed nothing says so.

**Tag whole outlines, never some of their edges.** The tag goes on each edge,
so selecting three sides of a rectangle and tagging it leaves the fourth
saying what it said before, and the outline then means two things at once. The
export refuses such an outline rather than taking a majority vote — a tag is a
decision, and three-to-one is still two decisions. The macro now names any
outline that disagrees with itself, and where it is:

```
1 outline(s) disagree with themselves.  The export refuses these:
  Openings South wire 6: 1.69 x 2.73 m at 18.07 m along, 0.00 m up
      -- tagged Door and GlassDoor
```

That is worth having because the tally alone cannot show it: three of four
edges retagged reads as `Door 1, GlassDoor 11`, which looks like a document
with one door in it. The fix is to select **all** the outline's edges and tag
it once.

Tags are ordinary document edits: **Ctrl+S** before exporting, or the export
reads the file as it was.

---

## 5. Air boundaries — a wall edge, not a face

`find_air_boundaries.py` derives most air boundaries from the plan — shafts,
mezzanine edges, corridor cross-sections, open-stair sides — but two things a
floor plan never states outright: that a wall the rules would call a
guardrail is in fact **rated construction**, or that an edge no rule reaches
is **deliberately open**. Those used to live only as `--solid-surface` /
`--open-surface`, naming an OpenStudio surface by name — and surface names
are positional, reassigned on every full rebuild (see IDENTITY.md). A
hand-made coupling was lost that way once, silently: the wall read solid, the
model still built, and the two zones simply stopped exchanging air.

`tag_airboundary.FCMacro` tags the declaration onto the drawing instead, the
same way `tag_opening.FCMacro` overrides a window's type:

1. **Edit the floor-plan sketch** the wall belongs to, and select its
   wall-centerline **edge** — one edge, the segment between the two rooms it
   separates (or between a room and an `Open to Below` void — see below).
2. **Run the macro.** Pick `SOLID` (never an air boundary here, whatever a
   rule says) or `OPEN` (always one), or `(no override)` to clear it.
3. It prints what the edge resolves to right now — which two rooms, or why
   not — before you have exported anything.

| when | what you select |
|---|---|
| a rated wall a rule would open | its edge, `SOLID` |
| an opening no rule reaches (a stair end, a doorway held open by design) | its edge, `OPEN` |

The declaration rides on the edge itself as an `OS_AirBoundaryOverride`
`Part::GeometryStringExtension` — document data, so it survives a save, a
drag and a new constraint. `fc_export_floorplan.py` resolves it to the pair
of rooms it separates (by their `OS_SpaceId`, never by name) and the edge's
own line, and writes both into the floorplan JSON. `find_air_boundaries.py`
then finds the built wall(s) for it **by that position**, not by name, which
is what makes it survive a full rebuild with nothing to retype.

An edge that touches the building's exterior, or the same room on both
sides, or more than two rooms, is refused rather than guessed at — exactly
like an opening outline that disagrees with itself — **but only for
`OPEN`**. An air boundary is a construct between exactly two real zones,
so `OPEN` demands that pairing exist; there is no other way to physically
create one. `SOLID` makes no such demand — it declares "never an air
boundary here," which is already true no matter how many rooms the edge
borders, since no rule anywhere in this codebase would ever turn a
non-two-room edge into an air boundary in the first place. A `SOLID` tag
on an edge that does not cleanly separate two rooms (or one room and an
`Open to Below` void) is silently accepted and resolves to nothing, rather
than reported — it has nothing to protect against there, so refusing it
would be an objection to a situation with no actual failure mode.

**Run `normalize_walls.FCMacro` once per sketch before extensive tagging**
(README §1a). A long wall crossing a T-junction resolves correctly either
way — the tag lookup aggregates the fragments a T-junction splits its face
boundary into — but if that run genuinely touches **three or more rooms**
along its length (a wall separating a real corridor from several distinct
offices, say), it is still refused *by design*: there is no single pair of
rooms to write for it. Normalizing first turns that one long edge into
several independently-taggable ones at the sketch's own T-junctions, so the
fix is to tag each resulting edge on its own, not to change what the
resolver accepts. Normalizing also fixes the dead end an official wall
drawn as External Geometry used to be: its own edge had nowhere to carry a
tag at all, until promoted to a real, local line.

### Tagging against an `Open to Below` void

A mezzanine's guardrail is routinely drawn against exactly this: the room it
really meets is a taller room on **another story**, reaching up through a
deliberate `Open to Below` hole (§3, "Rooms that are not the story's
height") rather than being drawn a second time. There is nothing on the
mezzanine's own sketch to name for that room, so this case is resolved in
two steps instead of one:

1. At export, the tagged edge is matched to the `Open to Below` face it
   borders, and that face's own footprint is recorded.
2. Once every story has been read, that footprint is matched against every
   room tall enough to reach through it (`OS_Height_m` past its own story's
   `floor_to_floor_m`) — the same reasoning `find_air_boundaries.py`'s
   mezzanine-edge rule already applies to the *built* model, run here at
   export time instead. A prism is extruded straight up, so the room really
   reaching through a hole has that hole's exact footprint; no match, or
   more than one, is reported by name rather than guessed at.

The macro's own preview cannot show this part — it would have to read every
story sketch in the document to do it, which is more than tagging one edge
should touch — so it reports a count instead ("N edge(s) border an Open to
Below region") and the real answer shows up in `fc_export_floorplan.py`'s
own report on the next export.

Tags are ordinary document edits: **Ctrl+S** before exporting.

---

## 6. Canopies and fins — a plan sketch, not an elevation one

A window lives *in* a wall, so it is drawn in elevation. A canopy sticks *out*
of one, so it is drawn in **plan**, on a sketch whose z is the height of the
plate. The drawing is then the shading surface itself: true footprint, true
height, nothing to project and no host to find.

Tag the sketch by **giving its Label a keyword** — as the first word
(`CANOPY South entry`) or anywhere else in it (`Parapet Shading`) — or by
ticking an `OS_ShadingSketch` boolean:

| Label contains | Named |
|---|---|
| `CANOPY` | Canopy |
| `AWNING` | Awning |
| `OVERHANG` | Overhang |
| `FIN` | Fin |
| `SHADING` / `SHADE` | Shading |

```
.\bridge.ps1 seed YourPlan.FCStd --init-shading
```

adds the `OS_ShadingSketch` checkbox to every sketch that could plausibly
hold a shade, the same way `--init-roof` offers `OS_RoofMethod` on every roof
candidate: nothing is decided for you, but the checkbox is there to tick.
It starts **ticked** on a sketch whose label already reads as a shade (it was
already being picked up; the box just says so) and **unticked** everywhere
else. Story plan and opening-tracing sketches are left alone — ticking either
would turn every room or opening outline inside it into a phantom shade.

Every **closed** wire in the sketch is one shading surface. The rest of the
name comes from the nearest exterior wall — `Canopy South 01` — by the same
compass rule that named the door beneath it, so there is nothing to type.

Two optional properties on the sketch, for when the default is wrong:

| Property | Type | Effect |
|---|---|---|
| `OS_ShadingKind` | String | Overrides the name the keyword would give |
| `OS_ShadingGroupType` | String | `Building` (default, rotates with the north axis) or `Site` (does not) |

An **open** wire is a datum line, not a shade. It is listed and skipped, never
guessed at — toggle it to construction geometry (**G, N**) and it stops being
reported at all. Any planar sketch is read, so a vertical fin drawn on an
elevation sketch works the same way.

---

## 7. The roof — one dropdown on the shape you drew

Without this, every space is a flat-topped prism and the building's top is
wherever the storey heights put it. To give it the real roof, draw the roof and
tell the bridge what it is.

Anything with a shape can be the roof: a `PartDesign::Body`, the `Pad` inside
it, or a bare face traced over the plan. Run

```
.\bridge.ps1 seed samples\FloorplanTest-05.FCStd --init-roof
```

and every candidate gets the properties below, all set to `Ignore` — the state
the document was already in. Nothing is chosen for you, because which object is
the roof is a judgement.

| Property | Type | Example | Effect |
|---|---|---|---|
| `OS_RoofMethod` | Enumeration | `Extend` | `Ignore` / `Extend` / `Attic`. A dropdown in the Data tab |
| `OS_StoryName` | String | `Attic` | Story the attic space is filed under. **Attic only** |
| `OS_SpaceName` | String | `601 \| Attic` | Names the attic space, same `NUMBER \| Name` form as a room label. Blank derives it from the object's Label, minus FreeCAD's `-001` suffix. **Attic only** |

### `Extend` — the rooms grow up to the roof

Every space whose ceiling is within 50 mm of the roof's **lowest** point is
carved against the roof, so its ceiling follows the slope and its walls become
trapezoids. Same spaces, same footprints, same floor area; more volume, and a
roof surface that is now tilted.

A space that turns out not to be under the roof is left flat and said so. One
that is only *partly* under it stops the export — the part left out would have
no ceiling, and there is no honest guess to make there.

### `Attic` — the roof becomes a space

The roof solid becomes a space of its own on its own story. The rooms below
keep their flat ceilings, and matching pairs them with the attic floor, so
what was exterior roof becomes interior ceiling and the loss path now runs
through the attic.

The object gets an `OS_SpaceId` minted onto it, exactly like a room label, so
an attic is the same kind of thing to change propagation as a room is. Do not
touch it — the same rules as §3 apply.

An `Attic` object has to be a **solid**, and it has to have a floor. A bare
face is an `Extend` roof; pad it if you meant an attic.

### Which to choose

An energy question, not a geometry one. `Extend` puts the roof construction
directly against conditioned air. `Attic` inserts an unconditioned buffer, and
is right when there is a real ventilated cavity, a plenum, or ducts above the
ceiling. **An attic needs a construction set and a decision about whether it is
conditioned**; nothing else in the model will remind you.

Preview either with **`review_roof.FCMacro`** before exporting. It reads the
open document, writes nothing, and prints what the export would do.

### Roof first, openings after — but it is no longer a one-way door

The elevation sketches stand on the traced plan, so a roof change moves no
outline in the drawing. What it does cost is the model's own subsurfaces:
`update` rebuilds every space the roof reshapes, and that removes the openings
on them. It counts them first, and they come back from the drawing with
`dump` → `import` → `planes` → `openings` → `apply` — as new objects with new
handles.

So settling the roof first is still cheaper, but adding one later is a
re-apply, not a redraw.

---

## Order of work on a new plan

1. Trace wall **centerlines**, one sketch per story, as a connected network.
2. `fc_seed_labels.py --init-stories` → fill in `OS_StoryName`,
   `OS_Elevation_m`, `OS_FloorToFloor_m`, tick `OS_Include`; set
   `OS_NorthAxis_deg` on the document.
3. `fc_seed_labels.py`, then **`label_style.FCMacro` in the GUI** to make the
   labels visible → rename every placeholder; mark the non-rooms `SKIP`; set
   `OS_Height_m` on any room that is not its story's height.
4. `fc_export_floorplan.py` → fix anything it complains about, re-run.
5. `build_osm_geometry.py` → fails if any room's area disagrees with FreeCAD
   by more than 0.1%.
6. If the roof is not flat: `fc_seed_labels.py --init-roof`, set `OS_RoofMethod`
   on the one object that is the roof, preview with `review_roof.FCMacro`,
   re-run 4 and 5. Do this **before** drawing any openings (§7).

Steps 2 and 3 are once per project. Step 4 onward is re-run every time the
architecture changes, and only what moved gets rebuilt.
