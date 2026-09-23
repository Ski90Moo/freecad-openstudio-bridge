# Thin wrapper that picks the right interpreter for each bridge script.
#
#   .\bridge.ps1 export  ..\FloorplanTest-01.FCStd --out plan.json
#   .\bridge.ps1 build   plan.json --out runs\mybuilding.osm
#   .\bridge.ps1 dump    runs\mybuilding.osm --out surfaces.json
#
# Two runtimes are involved and they are not interchangeable: the fc_* scripts
# need FreeCAD's bundled Python (it is the only one that can import FreeCAD),
# and the model scripts need the venv (the only one with openstudio).
#
# `roundtrip` chains export -> build -> openings (export+apply) -> shading
# (export+apply) into one call, so a full build can no longer be done in
# pieces and quietly left short -- confirmed directly, twice, that stopping
# after `build` (which never touches openings or shading at all) reads as a
# complete model until someone checks getSubSurfaces()/getShadingSurfaces()
# and finds them empty.
#
#   .\bridge.ps1 roundtrip ..\FloorplanTest-05.FCStd --out runs\mybuilding.osm

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('seed', 'relabel', 'export', 'build', 'dump', 'import',
                 'verify', 'planes', 'crop', 'elevations', 'openings',
                 'apply', 'shading', 'apply-shading', 'air-boundaries',
                 'apply-air-boundaries', 'update', 'markup-crop', 'markup-place',
                 'roundtrip')]
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

if ($Command -eq 'roundtrip') {
    if ($Rest.Count -lt 3 -or $Rest[0] -like '-*' -or $Rest[1] -ne '--out') {
        throw "Usage: .\bridge.ps1 roundtrip <fcstd> --out <output.osm>"
    }
    if (-not (Test-Path $freecad)) { throw "FreeCAD Python not found at $freecad." }
    if (-not (Test-Path $venv)) {
        throw "Bridge venv missing at $venv. Create it with:`n" +
              "  python -m venv osvenv`n" +
              "  osvenv\Scripts\pip install openstudio==3.11.0"
    }

    $fcstd = $Rest[0]
    $osm = $Rest[2]
    $outDir = Split-Path -Parent $osm
    if (-not $outDir) { $outDir = '.' }
    if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
    $base = [System.IO.Path]::GetFileNameWithoutExtension($osm)
    $plan = Join-Path $outDir "$base.plan.json"
    $openingsJson = Join-Path $outDir "$base.openings.json"
    $shadingJson = Join-Path $outDir "$base.shading.json"
    $surfacesJson = Join-Path $outDir "$base.surfaces.json"
    $bareSurfacesJson = Join-Path $outDir "$base.bare-surfaces.json"
    $boundariesJson = Join-Path $outDir "$base.boundaries.json"

    function Step {
        param([string]$Title, [string]$Interpreter, [string]$File, [string[]]$StepArgs)
        Write-Host "`n== $Title =="
        & $Interpreter (Join-Path $root $File) @StepArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host "`nroundtrip stopped at '$Title' (exit $LASTEXITCODE)."
            exit $LASTEXITCODE
        }
    }

    Step -Title 'export'            -Interpreter $freecad -File 'fc_export_floorplan.py' -StepArgs @($fcstd, '--out', $plan)
    Step -Title 'build'             -Interpreter $venv    -File 'build_osm_geometry.py'   -StepArgs @($plan, '--out', $osm)
    # Refresh the FCStd's own OS_Geometry preview against the just-built
    # (bare) wall network BEFORE exporting openings: fc_export_openings.py
    # stamps its host walls against whatever OS_Geometry the document last
    # held, and refuses if the wall network moved since -- which it will
    # have, any time normalize_walls.FCMacro (or any other geometry change)
    # ran more recently than the last import. Confirmed directly against
    # doc05: without this step, roundtrip fails here on a freshly
    # normalized document with "imported OS_Geometry was taken from a
    # different plan than the one drawn now".
    Step -Title 'refresh (bare)'    -Interpreter $venv    -File 'dump_osm_geometry.py'    -StepArgs @($osm, '--out', $bareSurfacesJson)
    Step -Title 'refresh (--into)'  -Interpreter $freecad -File 'fc_import_surfaces.py'   -StepArgs @($bareSurfacesJson, '--into', $fcstd)
    Step -Title 'openings (export)' -Interpreter $freecad -File 'fc_export_openings.py'   -StepArgs @($fcstd, '--out', $openingsJson)
    Step -Title 'openings (apply)'  -Interpreter $venv    -File 'apply_openings.py'       -StepArgs @($osm, $openingsJson)
    Step -Title 'shading (export)'  -Interpreter $freecad -File 'fc_export_shading.py'    -StepArgs @($fcstd, '--out', $shadingJson)
    Step -Title 'shading (apply)'   -Interpreter $venv    -File 'apply_shading.py'        -StepArgs @($osm, $shadingJson)
    # find_air_boundaries.py reads $plan (not the FCStd), so it picks up any
    # OS_AirBoundaryOverride tag automatically -- no --solid-surface/
    # --open-surface flag needed here. This step, and the final dump+import
    # below, were both missing from the first version of this command:
    # confirmed directly that omitting them leaves apply_air_boundaries.py's
    # own changes invisible to the FreeCAD document's own OS_Geometry
    # preview -- "colorize" (or any other in-document check) kept reporting
    # 0 air boundaries even though the .osm this same roundtrip had just
    # built was correct, the same class of gap the dump+import steps above
    # already close for openings and shading.
    Step -Title 'air boundaries (find)'  -Interpreter $venv -File 'find_air_boundaries.py'    -StepArgs @($osm, $plan, '--apply-json', '--out', $boundariesJson)
    Step -Title 'air boundaries (apply)' -Interpreter $venv -File 'apply_air_boundaries.py'   -StepArgs @($osm, $boundariesJson, '--out', $osm)
    # Close the loop back into the FreeCAD document itself: apply_openings/
    # apply_shading/apply_air_boundaries only ever touch the .osm. Without
    # this, the document's own OS_Geometry preview (Subsurfaces/Shading
    # "from the model" groups, and each wall's own OS_Air_Boundary
    # property) keeps showing whatever bare geometry the last dump+import
    # happened to carry -- confirmed directly reporting "0 subsurfaces, 0
    # shading" and separately "0 air boundaries" in the GUI's own Report
    # View, even though the .osm this same roundtrip had just built was
    # correct both times.
    Step -Title 'dump (complete)'   -Interpreter $venv    -File 'dump_osm_geometry.py'    -StepArgs @($osm, '--out', $surfacesJson)
    Step -Title 'import (--into)'   -Interpreter $freecad -File 'fc_import_surfaces.py'   -StepArgs @($surfacesJson, '--into', $fcstd)

    Write-Host "`nroundtrip complete: $osm ($fcstd's own OS_Geometry preview refreshed too)"
    exit 0
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
