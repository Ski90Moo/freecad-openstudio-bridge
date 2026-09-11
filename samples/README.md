# Sample exchange files

Real output from a full round trip, kept so the exchange format can be read
without owning FreeCAD or OpenStudio. Neither file is an input to anything —
both are regenerated every time the pipeline runs.

| File | Written by | Consumed by |
|---|---|---|
| `surfaces.json` | `dump_osm_geometry.py` | `fc_import_surfaces.py`, `verify_roundtrip.py` |
| `openings.json` | `fc_export_openings.py` | `apply_openings.py` |

`schema/` holds the formal schemas; these are what they look like filled in.

## What is in them

A two-storey light-industrial building of 37 spaces, drawn on a 45° north
axis — the worked example the numbers throughout the docs belong to.

`surfaces.json` — 321 surfaces, 28 subsurfaces, 4 shading surfaces. Each
surface carries its name, OpenStudio handle, space, story, thermal zone,
surface type, boundary condition, construction, gross and net area, azimuth,
tilt, outward normal, and its full vertex loop in metres.

`openings.json` — the 28 windows and doors, each with its host surface, host
space, subsurface type and vertices.

Two things worth noticing, because both are load-bearing elsewhere:

- **Handles are present but are not identity.** They are regenerated on every
  rebuild, which is the whole reason [IDENTITY.md](../IDENTITY.md) exists.
- **`construction` is mostly `null`.** Geometry is all the bridge claims to
  produce; constructions, weather and HVAC are assigned downstream.

Room names are generic (`006-102-Office`, `027-403-Warehouse`) and no drawing
or client information travels with the coordinates.
