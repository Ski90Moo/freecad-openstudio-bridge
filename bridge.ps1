# Thin wrapper that picks the right interpreter for each bridge script.
#
#   .\bridge.ps1 export  ..\FloorplanTest-01.FCStd --out plan.json
#   .\bridge.ps1 build   plan.json --out runs\mybuilding.osm
#   .\bridge.ps1 dump    runs\mybuilding.osm --out surfaces.json
#
# Two runtimes are involved and they are not interchangeable: the fc_* scripts
# need FreeCAD's bundled Python (it is the only one that can import FreeCAD),
# and the model scripts need the venv (the only one with openstudio).

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('seed', 'relabel', 'export', 'build', 'dump', 'import',
                 'verify', 'planes', 'crop', 'elevations', 'openings',
                 'apply', 'shading', 'apply-shading', 'air-boundaries',
                 'apply-air-boundaries', 'update', 'markup-crop', 'markup-place')]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$freecad = 'C:\Program Files\FreeCAD 1.1\bin\python.exe'
$venv = Join-Path $root 'osvenv\Scripts\python.exe'

$scripts = @{
    seed     = @{ py = 'freecad'; file = 'fc_seed_labels.py' }
    relabel  = @{ py = 'freecad'; file = 'fc_relabel.py' }
    export   = @{ py = 'freecad'; file = 'fc_export_floorplan.py' }
    build    = @{ py = 'venv';    file = 'build_osm_geometry.py' }
    dump     = @{ py = 'venv';    file = 'dump_osm_geometry.py' }
    import   = @{ py = 'freecad'; file = 'fc_import_surfaces.py' }
    verify   = @{ py = 'freecad'; file = 'verify_roundtrip.py' }
    planes   = @{ py = 'freecad'; file = 'fc_seed_openings.py' }
    crop     = @{ py = 'venv';    file = 'elevations\facade_images.py' }
    elevations = @{ py = 'freecad'; file = 'fc_place_elevations.py' }
    openings = @{ py = 'freecad'; file = 'fc_export_openings.py' }
    apply    = @{ py = 'venv';    file = 'apply_openings.py' }
    shading  = @{ py = 'freecad'; file = 'fc_export_shading.py' }
    'apply-shading' = @{ py = 'venv'; file = 'apply_shading.py' }
    'air-boundaries' = @{ py = 'venv'; file = 'find_air_boundaries.py' }
    'apply-air-boundaries' = @{ py = 'venv'; file = 'apply_air_boundaries.py' }
    update   = @{ py = 'venv';    file = 'update_osm_geometry.py' }
    'markup-crop'  = @{ py = 'venv';    file = 'markup\crop_plan.py' }
    'markup-place' = @{ py = 'freecad'; file = 'fc_seed_floorplan.py' }
}

$spec = $scripts[$Command]
if ($spec.py -eq 'freecad') { $interpreter = $freecad } else { $interpreter = $venv }

if (-not (Test-Path $interpreter)) {
    if ($spec.py -eq 'venv') {
        throw "Bridge venv missing at $interpreter. Create it with:`n" +
              "  python -m venv osvenv`n" +
              "  osvenv\Scripts\pip install openstudio==3.11.0"
    }
    throw "FreeCAD Python not found at $interpreter."
}

& $interpreter (Join-Path $root $spec.file) @Rest
exit $LASTEXITCODE
