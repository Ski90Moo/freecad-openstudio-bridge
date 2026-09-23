# Identity — how the bridge knows what is what

**Status: current design documented, consolidation deferred.** See
[Deferred: consolidating the model side](#deferred-consolidating-the-model-side)
at the end. Nothing here needs changing to keep working; the open question is
whether three carriers should become one.

---

## The problem identity solves

The drawing is the source of truth and the model is derived from it. Every time
the bridge runs, it has to answer: *is this the same room / window / canopy as
last time, or a new one?*

Getting that wrong is expensive and silent:

- A space treated as new is **rebuilt**, and its thermal zone's HVAC,
  constructions and schedules go with it.
- A subsurface treated as new is **recreated**, so its OpenStudio handle
  changes — and a handle is what a shading control, a frame and divider, an
  interzone pairing or an airflow-network link references. All of them break,
  with no error.
- A canopy treated as new does the same to a photovoltaic generator attached
  to it.

None of that shows up in `validate_model`. It shows up as a simulation that
quietly stops matching the design.

**OpenStudio handles cannot be used for this.** There is no `setHandle` — not
on model objects, not on `IdfObject` (checked in 3.11). Handles are the SDK's
to mint, `build` mints a whole new set, and `intersectSurfaces` renames
surfaces non-deterministically on top. So identity has to be minted on the CAD
side and mapped onto the model, never the reverse.

---

## What exists today

| | id lives in FreeCAD on… | mapping to the model lives in… |
|---|---|---|
| **space** | `OS_SpaceId`, a property on the room's Draft Text label | `<model>.fcmap.json`, keyed by the id |
| **opening** | `OS_OpeningId`, a string extension on the sketch geometry | the subsurface's own comment |
| **shade** | `OS_ShadingId`, a string extension on the sketch geometry | the shading surface's own comment |
| **surface** | nothing — it is not drawn, it is derived | nothing stored; recovered geometrically, see below |

### A surface has no id, and does not need one

A surface is not something anyone draws, so there is nowhere on the CAD side to
mint an id onto. It does not follow that a surface has no identity: it has a
**position**, and `surface_identity.py` recovers identity from that instead —
a wall is recognised by the line it stands on in plan plus the height it starts
at, a floor or ceiling by the ground it covers and which way it faces.

That is enough to carry a surface's name, its construction (which is where an
air boundary lives) and its openings across a rebuild, because a rebuild moves
the top of a wall and never its footprint. Measured: 148 of 148 walls, floors
and ceilings in 14 rebuilt spaces, none ambiguous.

Where it stops is exactly where position stops meaning anything — a wall that
**moved** in the plan, or a face whose **pitch changed** under an opening. Both
are reported by name rather than guessed at. Two surfaces sharing a key inside
one space are reported too, and neither is touched.

This is why `intersectSurfaces` renaming surfaces non-deterministically stopped
mattering: the names are put back afterwards from the capture, so a
`--solid-surface` list or a construction assigned by surface name survives a
rebuild that would previously have moved it to an unrelated wall.

Two axes differ, and only the second is a design choice.

### The FreeCAD side is forced, not chosen

A room label **is an object** (`App::FeaturePython`), so it can carry a
property. A drawn opening is a **wire inside a sketch** — and FreeCAD gives
wires no properties at all. The only per-element handle FreeCAD offers is a
`Part::GeometryStringExtension` riding on the geometry, which is what
`tag_opening.FCMacro` and the exporters use.

Unifying this would mean making every opening its own object, which means
abandoning "trace a whole facade in one sketch" — the thing that makes
fenestration tracing bearable. Not worth it. The asymmetry is FreeCAD's, not
the bridge's.

Measured, so it can be relied on: a geometry extension survives a save and
reload, a drag, a new constraint, and it **follows its element** when other
geometry in the sketch is deleted and the indices shift.

### The model side is a real choice

Three options, all tested against openstudio 3.11:

| | sidecar (`fcmap.json`) | comment | `additionalProperties` |
|---|---|---|---|
| travels with a copied `.osm` | ✗ | ✓ | ✓ |
| carries more than an id | ✓ | ✗ | ✓ |
| readable without loading a model | ✓ | ✗ | ✗ |
| survives a full `build` (all-new objects) | ✓ | ✗ | ✗ |
| can go stale vs. the model | ✓ silently | ✗ | ✗ |
| survives a tool rewriting the object | ✓ | ✗ | partly |
| cost | one file to keep beside the `.osm` | none | one `OS:AdditionalProperties` object each |

`additionalProperties` was verified to work on `Space`, `SubSurface` **and**
`ShadingSurface`, and to survive a save/load round trip on all three. Comments
survive too, and come back prefixed `! ` — which is why `id_of` tolerates it.

---

## Failure modes, and what happens now

### A comment is stripped

Measured before it was fixed: stripping one subsurface's comment and
re-applying took the model from **28 subsurfaces to 29**. The orphan kept its
name and handle, a second subsurface was created on the same wall with the same
outline, and that wall had **double the glazing area** — no error anywhere.

Now both apply scripts **adopt** instead: an unowned object occupying exactly
the outline about to be created is matched on `(host, outline)`, updated in
place and re-stamped with the id, handle intact. Adoptions are reported by
name. Shape is the right key precisely because the name is the part that
changes.

The one thing to know: adoption will also take over a subsurface placed by hand
at exactly a drawn outline's position, bringing it under bridge management.
That is right for a recovered orphan and is never silent.

### The sidecar goes missing

`update` refuses to run:

```
No fptest05.fcmap.json -- this model was not built by the bridge, so there is
no id mapping to diff against.
```

A good error, but the recovery is "find the file". If it is genuinely gone, all
36 space identities are gone with it, and the next `build` makes new spaces —
taking their HVAC. **This is the weakest point in the current design**, and the
reason the deferred work below is worth doing.

Note the asymmetry it creates: hand someone `fptest05.osm` on its own and its
28 subsurfaces and 5 shading surfaces can each say which drawn outline they
came from. Its 36 spaces cannot.

### An outline is retyped

A retype renames the object (`Door South 10` → `GlassDoor South 10`), so name
matching would see a delete and an add. Id matching sees one change. Verified:
handle `{931d7eb5-…}` before and after, 28 of 28 handles unchanged across a
re-apply, with a `ShadingControl` still resolving to the retyped subsurface.

### Two objects want the same name

OpenStudio makes names unique by **suffixing, silently**. Creating
`Door North 01` while the previous run's still exists yields `Door North 8` —
which mangled all 28 names the first time this was built. Hence the order in
both apply scripts: **remove stale → park survivors under
`freecad-bridge-parked-<id>` → only then set final names.** Deleting one
opening shifts every later number on its facade, so renaming in place collides
just as easily. Do not reorder those three steps.

---

## Change detection

Identity says *which* object. Change detection says *what happened to it*, and
both paths now report it before writing:

| | how the diff is computed | reported by |
|---|---|---|
| spaces | stored `vertex_hash` in the sidecar vs. the current plan | `update` (report, then `--apply`) |
| openings, shades | live comparison against the model, keyed by id | `apply_*` (report always, `--report` to stop) and `review_openings.FCMacro` |

Spaces need a *stored* hash because `build` replaces every model object, so
there is nothing left to compare against. Openings are never destroyed, so the
model itself is the previous state.

`opening_diff.py` is pure Python — no FreeCAD, no openstudio — specifically so
the FreeCAD-side review macro and the venv-side apply scripts share one
implementation and cannot disagree about what changed.

---

## Deferred: consolidating the model side

Two separable jobs. Both are optional; neither is needed for correctness today.

### Job A — mirror the space identity onto the model

Write each space's `OS_SpaceId` **and** its `vertex_hash` onto the OpenStudio
space as `additionalProperties` features, keeping the sidecar as the authority.

- Fixes the weakest point above: a lost sidecar becomes **rebuildable from the
  model** rather than fatal. Everything else in a sidecar entry — `space_name`,
  `zone_name`, `story`, `source_area_m2`, `height_m` — is already in the model,
  and `room_number` / `room_name` are derivable from the space name.
- Mirroring the hash as well as the id matters. With only the id, recovery
  restores identity but loses the change history, so the first `update`
  afterwards would report everything unchanged whether or not it moved — the
  wrong kind of wrong.
- Cost: 37 extra objects, one new write in `build_osm_geometry.py`, one new
  fallback in `update_osm_geometry.py`. Nothing existing changes behaviour.

### Job B — one mechanism for everything

Put spaces, openings and shading all on `additionalProperties` and retire the
comments, and possibly the sidecar.

- **For:** one mechanism, one failure mode, one thing to document. Everything
  travels with the `.osm`. `additionalProperties` is a first-class model
  object rather than a comment, so it is less likely to be dropped by a tool
  that rewrites an object.
- **Against:** touches `build`, `update`, both apply scripts, `dump`, `import`
  and their tests, and re-opens the whole identity chain for verification.
  Adds ~69 objects that show up in `list_model_objects` and every enumeration
  of the model. The sidecar's remaining real advantage — being diffable and
  reviewable without loading a model — would be lost unless it is kept as a
  generated copy.

### Why this was deferred

The original argument for keeping them separate was that only spaces need
change detection. **That argument was wrong** — an architectural revision moves
windows and widens doors as readily as it moves walls, which is why openings
now have a change report of their own. With that reason gone, the two designs
are more alike than different, and Job B is more attractive than it first
looked.

What still genuinely differs is only *where the previous state lives*: a stored
hash for spaces, because `build` destroys the objects; the live model for
openings, because they are never destroyed. That is a smaller difference than a
whole second mechanism.

Revisit when any of these happen:

- a model needs to move without its sidecar and identity has to survive;
- shading controls, frame-and-dividers or PV get attached, making handle
  stability load-bearing rather than precautionary;
- a third kind of object needs identity, and the cost of choosing a mechanism
  has to be paid a third time.
