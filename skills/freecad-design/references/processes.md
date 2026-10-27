# Designing for the process

Set each part's process in `project.json` → `parts.<Part>.process` (keys are
body labels). It drives three things: `check_manufacturing`, the drawing
style, and what `release` produces. Use `lookup_standard(process=…)` for the
rules of thumb (fdm, sla, cnc, turning, laser, sheet).

Per-part settings in `project.json` → `parts.<Part>`: `process`, `material`,
`finish`, `qty`, `notes` (a string or a list — they go into the README and
BOM), `density_g_cm3` (only if the material isn't a common one; mass uses the
part's material otherwise). Bought-out parts come from `fc.ref_part(…,
bom="buy", qty=…, spec=…)`; `bom="context"` parts are listed as reference only
and `bom="none"` keep-outs are left out. The README gives one item's size and
the count found ("each 10 × 10 × 4; 2 in model").

## 3D printing — `fdm`, `sla`
- `check_manufacturing` reports:
  - the best print orientation, in words: "print with the face that points
    −X in the model down on the bed"
  - the overhang area and bed contact
  - the thinnest wall (FDM ≥ 1.2 mm, SLA ≥ 0.8)
  - whether the part fits the build volume (`project.json` `printer.volume`)
  - holes under 2 mm
  - horizontal holes over Ø6, which sag
  - threads tapped into plastic
- Print orientation: chosen for least overhang and most bed contact, or set
  `parts.<Part>.print_up` (e.g. `"+Y"`) when strength across layers matters;
  the released 3MF/STL follow it.
- Hole compensation: `project.json` → `dfm.hole_compensation_mm` (0.2 FDM,
  0.1 SLA from `create_project`) is added by `fc.holes` to clearance and plain
  holes (not tapped, insert or fit holes). The drawing and README say "print
  as modelled, no further compensation".
- Design for it:
  - give the part a flat base face
  - keep overhangs ≤ 45° (chamfer instead of fillet on downward edges)
  - use teardrop or flat-topped horizontal holes
  - use heat-set inserts (`"M3 insert"`) or nut traps (`fc.nut_trap`) instead of tapped plastic
- Release: **3MF + STL already rotated into the print orientation** (place as
  imported) **+ STEP** in the model orientation, the general tolerance
  (typ. ±0.2 mm FDM / ±0.1 mm SLA) and any part notes (print settings,
  infill). Drawings only with `drawings="all"`.

## CNC milling — `cnc`
- `check_manufacturing` reports:
  - which directions the features need access from (setups), separately from
    the outer faces (facing and profiling)
  - the smallest internal corner radius and the largest end mill that fits
    ("internal corners R3 → end mill ≤ Ø6"), and walls deeper than 4 × that
    tool
  - sharp internal corners a cutter cannot reach as floor corners (these
    need a fillet ≥ 1 mm, ideally ≥ ⅓ of the pocket depth)
  - faces tilted to all three axes (4/5-axis work)
  - thin walls
  - blind holes deeper than 10 × D
- Design for it:
  - radius every vertical internal corner
  - prefer through holes
  - use standard tapped holes (`"M5 tapped"` → 6H with the tap drill from the
    standard) and ISO 286 fits for bearing and pin bores (`"Ø8 H7"`)
- Release: **STEP + PDF drawing**. The drawing governs tolerances and threads.
  The general tolerance comes from `project.json` (ISO 2768-m by default).

## Turning — `turning`
- Model with `fc.turned(body, [(Ø, length, fit?), …])` along Z, then:
  - chamfers
  - `fc.external_thread` for threads
  - cross holes with `fc.holes(on="XZ", offset=+(r + 1), …)`
- `check_manufacturing` reports:
  - bar stock: the next standard bar above the OD plus allowance ("Ø18 bar for
    Ø16 finished OD") and the cut length (≥ L + 3 mm)
  - length/diameter ratio (above 3 needs a tailstock; above 6, a steady)
  - non-turned features as secondary operations
  - threads
- Design for it:
  - chamfer the ends
  - put a small radius or undercut at each shoulder, and thread relief
  - put fits only where needed (`(12, 28, "k6")`)
- Release: **STEP + PDF drawing**, in a lathe layout:
  - one Ø dimension per step, on that step, with fit limits where the step
    carries a fit (upper deviation first: `⌀8 k6 (+0.010/+0.001)`)
  - thread steps called out as threads (`M8×1.25-6g`) with their length
  - lengths from the end face, a centreline, hidden lines for bores
  - `lookup_standard(bearing="608")` gives bearing seats; `fit="k6", d=8`
    one class; `tolerance="ISO 2768-m", d=…` the general band

## Laser / waterjet / router — `laser`
- Flat blanks only: `check_manufacturing` requires a uniform plate (no
  pockets, steps, edge chamfers or bends). It warns about holes smaller than
  the sheet thickness in metal.
- Rules: holes ≥ thickness, edge distance ≥ 1–1.5 × thickness, webs ≥
  thickness, draw nominal (the shop applies kerf).
- Release: **DXF** cut profile (R12, true circles and arcs, origin at the
  lower-left corner, mm units in the header) **+ SVG + STEP + PDF drawing**.
  The README states material and thickness.

## Bent sheet metal — `sheet`
- Build it with `fc.sheet_metal(name, thickness, base, flanges=[…], holes=[…],
  radius=None, k_factor=None)` so the folded part and the flat pattern come
  from the same numbers (see `references/fclib.md`):
  - `base` is the **outside footprint** of the base face (a flange's outer face
    is flush with its edge); the sheet lies on z = 0 … t and +Z is "up".
  - Flange `length` is the **outside** length; `edge` is a base edge index
    (fc.rect: 0 = −Y, 1 = +X, 2 = +Y, 3 = −X), a direction ("+X") or
    `"flangeN"` for a lip or Z-bend on an earlier flange; `angle` (< 180°),
    `up`, `radius`, `inset` (a narrower tab) and `relief` (a slot where a bend
    ends inside an edge).
  - Holes: clearance or plain, in that face's 2D coordinates.
- Flat pattern maths: each bend removes a setback (r + t)·tan(θ/2) from both
  sides and adds a bend allowance BA = π/180·θ·(r + K·t); the bend line is the
  middle of the BA strip. K defaults to 0.33 (air bending) — confirm with the
  shop's press brake and tooling.
- `check_manufacturing` (sheet): inside radius ≥ t, flange flat ≥ 3t + r,
  bends ending inside an edge have a relief, holes ≥ t, hole edge ≥ 2t + r from
  a bend line and ≥ t from the outline; it reports thickness, bends, K and the
  flat size. A bent part with process `laser` is an error (set `sheet`).
- Release: **flat-pattern DXF** (layer CUT: outline and holes; layer BEND:
  dashed bend lines with "B1 90° UP R2" notes; checked against FcSheet) **+
  STEP of the folded part + PDF drawing** (with a bend table). README: material,
  thickness, K-factor, radius, flat size and the bend table. `export(dxf|svg)`
  of a sheet part writes the flat pattern.
- Limits: one thickness, no hems (180°), no rounded base outline, flanges on
  base edges or a flange's far edge only, no automatic corner relief (adjacent
  flanges need an inset plus relief), clearance/plain holes only. The part is
  not parametric in the FreeCAD GUI — change the spec in model.py.

## Drawing style and layout
The style follows the process: laser/sheet → plate, turning → turned, others
from the shape (a thin uniform part → plate, else prismatic); `style=`
overrides. `hole_dims="pitch"` dimensions a hole pattern by its pitch plus one
location (default: baselines from a datum edge); `hidden=true` adds hidden
lines. Notes print exactly as written. Long title-block values are shortened
to fit (the MATERIAL note keeps the full text). The result explains a paper
or scale change, or a view that was left out for lack of room.

## Drawings in a release
With `drawings="auto"` (the default) a part that needs a drawing gets its
newest sheet from `drawings/` if that sheet still matches the model (edits
included, shipped with its own sheet revision); a stale generated sheet is regenerated with
its saved options and ops; a stale sheet that was edited by hand **stops** the
release (re-apply the edits on a fresh sheet, or `drawings="generate"` to ship
a fresh sheet without them). Only the PDF is shipped — the drawing DXF drops
notes and the title block, and a sheet DXF next to a cut profile could get
cut. When a part that needs a drawing ships without one, the README says so.
