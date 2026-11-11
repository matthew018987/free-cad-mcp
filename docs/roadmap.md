# Roadmap: manufacturing evidence and LLM-driven iteration

Status: **plan** (nothing below is built yet) · 2026-11-09 · follows M0-M5 in [design.md](design.md) and M6, tolerancing
([tolerancing-plan.md](tolerancing-plan.md))

Two areas:

1. **Closing the loop to manufacture**: slicer integration, cost and lead
   time, more processes (injection moulding, casting, welded frames,
   extrusion-profile frames) and a CAM toolpath preview.
2. **LLM-driven iteration and project management**: parameter sweeps and
   optimisation, a visual diff between revisions, checking against a
   reference drawing, revision letters with change notes and git, and
   quick-turn intake of bought-out parts.

The principles of design.md §1 hold: code in, evidence out; numbers before
pictures; a stdlib-only server with the numpy/PIL work in FreeCAD workers; few
tools and a rich `fc` library. On top of that, for everything below: every
estimate states its basis, every new result goes stale with the geometry it was
made for (as FEA results do now), and external programs (slicers, git) are
optional and discovered, never needed by an existing flow.

---

## 0. Summary

| # | Feature | What Claude gets back | Surface | Needs | Size | Milestone |
|---|---|---|---|---|---|---|
| 1.1 | Slicer integration | print time, filament, support material, and the features the slicer drops, per orientation | `check_manufacturing(simulate=true)`; checks `print_time`, `filament`, `support`, `print_coverage` | F1 F5 | M | M7 |
| 1.2 | Cost and lead time | unit price and NRE per part, process and quantity, a lead-time range, every rate and its basis | new tool `estimate`; check `cost` | F1 F6 | M | M7 |
| 1.3a | Injection moulding | draft map, undercuts and the side actions that free them, wall uniformity, parting line, clamp force, cycle time | process `moulding`; `fc.draft`, `fc.shell`, `fc.rib`, `fc.boss` | F1 F4 | L | M9 |
| 1.3b | Casting | 1.3a plus cores, hot spots and machining allowance; an as-cast and a machined model | process `casting`; `fc.cast_blank` | 1.3a | M | M9 |
| 1.3c | Extrusion-profile frames | T-slot members, connectors, cut list with end machining, member deflection | process `profile_frame`; `fc.extrusion` | F1, 2.5 | M | M10 |
| 1.3d | Welded frames | mitred and coped members, cut list nested into stock bars, fit-up, weld access, weld table | process `weldment`; `fc.member`, `fc.weldment` | 1.3c | L | M10 |
| 1.4 | CAM toolpath preview | 2.5D toolpaths per setup, simulated stock removal, uncut and gouged volume with locations, cycle time | `check_manufacturing(simulate=true)` for cnc; check `uncut` | S1 F4 F5 | XL | M11 |
| 2.1 | Sweeps and optimisation | table of evaluated points, best feasible point, binding constraint, sensitivities, Pareto front | new tool `sweep` | F3 F6 | M | M8 |
| 2.2 | Visual diff | rN laid over rM: added, removed and moved material per part, with volumes and hole changes | `render(compare=…)` | F2 F4 | S-M | M8 |
| 2.3 | Reference drawing check | calibrated overlay of drawing and model, per-dimension mismatches in mm, unmatched features | new tool `compare_reference`; check `drawing_dims` | F4 | L | M10 |
| 2.4 | Revisions and change notes | Rev A/B/C per part, a drafted change note, a revision table on drawings, a git commit and tag per release | `release(revision=…, change_note=…)`; `inspect(level="revisions")` | F2 | M | M8 |
| 2.5 | Quick-turn intake | datasheet → part spec → reference part with frames and automatic interface checks; STEP import with proposed frames; a shared library | new tool `reference_part`; `fc.ref_part(spec=…)`, `fc.ref_part(lib=…)` | F4 | L | M9 |

F1-F6 are shared foundations (§2) and S1 is a spike. The server goes from 22
tools to 26 (`estimate`, `sweep`, `compare_reference`, `reference_part`);
everything else extends an existing tool, and details live in `guide`
reference pages rather than in tool descriptions. Sizes are relative,
S < M < L < XL, with M5 (fea.py, `analyze`, the `fea_*` checks and
test_fea.py) as an L.

---

## 1. What this builds on, and gaps found in the code

| Existing piece | Where | Used by |
|---|---|---|
| Six axis-aligned print orientations scored by overhang area, bed contact and height | `manufacture.print_orientation` | 1.1 candidate orientations, 1.2 |
| Setup directions by ray casting, internal corner radii, largest end mill, wall depth per corner | `manufacture.machining_access`, `corner_problems` | 1.2 setups and tool Ø, 1.4 one CAM job per setup |
| Ray tests against faces, inward-ray wall thickness | `manufacture._ray_tester`, `wall_thickness` | 1.3a/b undercuts and wall distribution |
| Bar stock and cut length, plate profile, flat pattern | `turned_size`, `standards.bar_for`, `flat_profile`, `sheet_flat_pattern` | 1.2 |
| The release's oriented mesh export | `manufacture._orient_mesh`, `export_parts(print_up=…)` | 1.1 slices exactly what ships |
| Build revisions, input hash, inputs and report per revision, Δ against the previous build of the same output | `worker._build`, `_new_rev`, `inputs_sha1`, `SNAPSHOT_FILES`, `delta_lines` | F2, F3, 2.2, 2.4 |
| What-if FEA on parameter overrides | `worker.op_analyze(params=…)` (variant `fea`) | F3, 2.1 |
| Geometry fingerprint that makes FEA results stale | `fea.geom_sig` | F5 evidence cache |
| Headless renderer with a drawing style, returning px/mm per view | `render.render` | F4, 1.4, 2.2, 2.3 |
| Release staging, `.release.json` (inputs, files, checks), checks changed since the previous release, title-block `revision_index` | `t_release`, `release_target`, `checks_changes`, `drawing_worker.edit_revision` | 2.4 |
| Reference parts from STEP/IGES/BREP/STL/OBJ or a shape, named frames, BOM roles; files read through fclib are recorded | `fc.ref_part`, `fc.set_frames`, `fc._ctx.reads` → `report.reads` | 2.5 |
| Hole and boss detection | `analysis.detect_holes`, `detect_bosses` | 2.2 hole diff, 2.3 hole matching, 2.5 proposed frames |
| Eval cases with a PNG reference drawing, a datasheet intake and an FEA sizing task | `evals/cases.json`: `reference_drawing`, `nema17_print`, `shelf_bracket` | 2.3, 2.5, 2.1 |

Gaps to close first:

1. **Process names are hard-coded in nearly thirty places across six files**:
   `freecad_mcp.MATERIALS`, `PROCESS_LABEL`, `NEEDS_DRAWING`, the
   `create_project` defaults and the `process` enums of four tool schemas;
   `manufacture.PROCESS_ALIASES`, `NOT_PRINTED`, `RELEASE_FORMATS`,
   `NEEDS_DRAWING`, `PROCESS_NAME`, `GENERAL_TOL_PRINT` and several
   `process in (…)` tests; `standards.MIN_WALL` and `PROCESS_RULES`;
   `fclib._norm_process`; the drawing worker and `op_release_prepare`. Four new
   processes would multiply that. → F1
2. **`.history/rNNNN/` keeps the inputs and the report, not the geometry, and
   not the files in `refs/`** (only their hash, inside `inputs_sha1`). An old
   revision can't be rendered, and can't be rebuilt exactly once a reference
   file has changed. → F2
3. **Variant builds take main revision numbers** (`_new_rev` runs for variants
   too), so a 40-point sweep would add 40 revisions; and every
   `analyze(params=…)` what-if builds into the one file
   `build/<name>__fea.FCStd`, so two what-ifs can't run at once. → F3
4. **`render` always fits the view** (`scale = min(…)` over the cells), so it
   can't draw at a known px/mm around a known point, which an overlay on a
   drawing, or one fixed camera for two revisions, needs. → F4
5. **Cancelling kills the worker but not its children.** `cancel_request`
   calls `proc.kill()` on the worker, and fea.py runs CalculiX with
   `subprocess.run`, which is why `op_analyze` keeps a deadline. Slicers, CAM
   simulation and git would add more grandchildren. → F5
6. **`SERVER_FILES` in `scripts/build_plugin.py` names every server module**,
   and data folders are copied one by one (`server/viewer/`). Each new module
   and folder below must be added there and to the packaging test.

---

## 2. Foundations

### F1. Process registry: `server/processes.py` (S)
One stdlib table that the server, the workers and fclib import, like
`standards.py`. Per process: `id`, aliases, label, family (print, subtractive,
sheet, formative, fabricated), default material and density, minimum wall,
rules (moved from `standards.PROCESS_RULES`), release formats, whether a
drawing is needed, general tolerance text, drawing style, the `create_project`
template, and the names of its DFM, cost and `simulate` handlers.
- Tool-schema enums are generated from it, so a new process is one entry plus
  its handlers.
- It lands first as a pure refactor: no behaviour change, every existing suite
  passes unchanged, plus a test that every entry has every field.

### F2. Revision store: geometry and inputs per revision (S-M)
- `_build` also writes `.history/rNNNN/geometry.zip`: one BREP per result
  object in global coordinates, and `geometry.json` (label, role, colour, BOM
  role, `geom_sig`). BREP text compresses well; a simple part is tens of KB
  zipped.
- Content-addressed inputs: the first time a file under `refs/` or in
  `report.reads` is seen, it is copied to `.history/objects/<sha1>`, and
  `report.json` gets `inputs: {path: sha1}`. Any revision can then be rebuilt
  exactly in a temporary folder: 2.2 uses that for revisions whose geometry was
  pruned, and 2.4 to reproduce a released revision.
- Retention: geometry for the newest 30 revisions, every released revision and
  every revision a sweep or change note refers to; objects are deduplicated;
  `project.json → history.keep_geometry` changes the count.

### F3. Scratch evaluation: `worker.op_evaluate` (M)
- Runs model.py with overrides in a fresh document and evaluates the geometric
  checks and the metrics asked for (F6), plus FEA cases, DFM, cost or slicing
  when asked, all in one FreeCAD process. It allocates no revision and writes
  no `last_build.json`, viewer scene or history snapshot; files go under
  `build/.scratch/` only when a step needs one.
- `_build` is split into "run the model" and "report on it", so `build` and
  `evaluate` share the code rather than copy it.
- Memoised by `inputs_sha1(overrides)` and the metric list, in
  `.history/evals/<sha1>.json`: a repeated sweep point or what-if costs
  nothing.
- `analyze(params=…)` moves onto it, so each what-if has its own scratch file
  and two can run in parallel.

### F4. Renderer: fixed scale, registration, overlays (S-M)
- `render(…, scale_px_per_mm=…, center=…)` skips the fit and draws at that
  scale around that model point. The result also returns each view's
  model-mm → pixel transform, so other tools can map pixels back to the model.
- Overlays: an image underlay with an affine or homography transform (2.3); 3D
  polylines with colour and width (toolpaths, parting lines, weld seams,
  displacement arrows); per-face colour overrides (draft map, deviation map,
  diff colours).

### F5. External programs and an evidence cache (S)
- `server/externals.py` (stdlib) finds OrcaSlicer, PrusaSlicer and git (and
  Bambu Studio, whose CLI is close to Orca's: Orca is a fork of it): an
  environment variable first (`FREECAD_MCP_SLICER`, `FREECAD_MCP_GIT`), then
  the macOS app bundles (`/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`,
  `/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer`), then `PATH`,
  each with a version probe. `freecad_status` reports them with a `setup_hint`.
  `/usr/bin/git` on macOS is an Xcode shim that fails until the Xcode licence
  is accepted, the trap design.md §2.3 found with `/usr/bin/python3`, so git is
  probed, never assumed.
- Workers start in their own process group (`start_new_session=True`) and a
  cancel kills the group (`os.killpg`), which stops CalculiX, slicers and
  simulations with their worker. The FEA deadline stays as a second guard.
- Evidence cache: `out/evidence/<kind>/<part>/<key>.json`, keyed by
  `fea.geom_sig`, a settings hash and the program's version. Each result
  records the `geom_sig` it was made for, so checks that use it go stale as
  `fea_*` checks do, and `release` re-runs stale evidence (as it re-solves
  stale FEA cases) or stops.

### F6. Metrics (S)
One resolver for sweep objectives and constraints, `estimate` drivers and the
new checks, parsed like geometry references (`analysis._parse_args`), with
errors that list the valid names:

```
mass(Bracket)   volume(Bracket)   bbox(Bracket).z   check(wall-min)   result(key)   (an fc.result value)
fea(default).safety_factor | .max_disp | .freq1 | .buckling
dfm(Bracket).overhang_mm2 | .setups | .min_corner_radius | .min_wall
slice(Bracket).time_min | .filament_g | .support_g | .coverage
cost(Bracket, qty=10).unit | .nre | .lead_days
cam(Bracket).uncut_mm3 | .cycle_min
```

---

## 3. Closing the loop to manufacture

### 1.1 Slicer integration

**Goal.** Replace "≤ 45° overhangs" with the slicer's own answer: real print
time, filament and support material, and the features it can't print.

**Which slicer.** Compared on licence, support and developer activity (GitHub
search API, 9 Nov 2026; commits on the default branch):

| Slicer | Licence | Backed by | Stars | Commits, 12 months | Merged PRs, 12 months | CLI |
|---|---|---|---|---|---|---|
| OrcaSlicer | AGPL-3.0 | its community | 16.4k | ~5,000 | ~2,300 | documented in its wiki; flags move between releases |
| PrusaSlicer | AGPL-3.0 | Prusa Research | 9.5k | ~1,700 | none on GitHub (in-house team) | stable for years; FDM and SLA |
| Bambu Studio | AGPL-3.0 | Bambu Lab | 5.3k | ~4,200 | not measured | close to Orca's; centred on Bambu printers; 8.3k open issues |
| UltiMaker Cura | LGPL-3.0 (engine AGPL-3.0) | UltiMaker | 7.1k | ~1,200 (engine ~960) | not measured | the engine needs fully resolved machine definitions: the hardest to drive |

- **Licence** doesn't separate them. The server runs the user's own installed
  slicer, unmodified, as a separate program, which puts no AGPL obligations on
  the Apache-2.0 server. The plugin must not bundle a slicer binary or copy a
  slicer's profile files.
- **OrcaSlicer first**: by far the largest and most active developer
  community (about three times PrusaSlicer's commits, and the only one merging
  outside contributions at scale) and very wide printer coverage. The
  trade-off is no company behind it and a CLI that moves, handled by version
  pinning.
- **PrusaSlicer second**: company-backed, the most stable CLI, and the one that
  slices SLA. Bambu Studio comes nearly free after Orca; Cura is not planned.

**Surface.**
- `check_manufacturing(simulate=true)` slices each fdm part (and sla, where
  the slicer handles it) in its print orientation: `parts.<Part>.print_up`,
  else the best from `print_orientation`. `orientations=3` also slices the next
  two candidates and ranks them by support material, then time. Claude sets
  `print_up` from that evidence; `release` re-slices when the orientation it
  ships has no current evidence, so the README's numbers always describe the
  shipped file.
- `project.json → printer`: `{"slicer": "orca" | "prusaslicer", "profile":
  {"printer": …, "print": …, "filament": …}, "volume": […]}`. Profile names
  resolve in the user's slicer configuration folder, or to files in
  `refs/slicer/`. The plugin ships generic profiles (0.4 mm nozzle, 0.2 mm
  layers; PLA, PETG, ASA) so slicing works with no setup, written for this
  project rather than copied from a slicer.
- Checks: `print_time` (max minutes), `filament` (max g), `support` (max g, or
  max % of the part), `print_coverage` (min % of the model's cross-section that
  the slicer extrudes).
- `release`: the README gives each printed part's time, filament and support,
  naming the slicer and profile. `gcode=true` adds G-code for users with their
  own printer (printer-specific, never by default).

**How it works.**
1. Worker: export the part oriented exactly as the release would
   (`_orient_mesh`).
2. Server: run the slicer as a cancellable child (F5), with supports switched
   on where the profile leaves them off, so "needs supports" is measured
   rather than assumed.
   - OrcaSlicer (first): `--load-settings "printer.json;process.json"
     --load-filaments filament.json --slice 0 --export-3mf out.3mf`, reading
     the G-code inside the 3MF. Flags have moved between Orca versions
     (`--load-filaments` against `--load-filament`, for one), so the driver
     reads `--help`, keeps an argument builder per version range, and
     `freecad_status` names the versions the tests cover. User presets inherit
     from Orca's system presets, so the driver resolves the `inherits` chain;
     where Orca keeps both on macOS is the first thing to confirm.
   - PrusaSlicer (second): `--export-gcode --load <printer.ini> --load
     <print.ini> --load <filament.ini> --output <file> <part.stl>`.
3. Server: parse the G-code (plain text, stdlib). Totals from the header and
   footer comments: time, filament length, volume and mass. Extrusion per
   feature type from the feature markers (`;TYPE:` in PrusaSlicer,
   `; FEATURE:` in Orca): support and support interface, bridges, the first
   layer's area on the bed. The layer list. The slicer's warnings and exit
   status: empty layers, outside the bed, stability.
4. Worker post-pass, the "real printability" check: on sampled layers,
   rasterise the extrusion paths (segment × line width) and the model's section
   at that height (`shape.slice`) with numpy, and report the model area left
   unextruded, grouped into regions with locations. This catches walls, fins,
   pins and lettering thinner than the nozzle lays down, which no overhang rule
   sees.

**Example.**
```
✓ Bracket - fdm: ready
    · sliced with OrcaSlicer 2.x, "Generic PETG 0.4 / 0.20mm", print_up +Z
    · 1 h 42 min · 31.4 g PETG · supports 2.1 g (7 %) under the arm, from z 18
    · orientations: +Z 1 h 42 min, 2.1 g support · -X 1 h 55 min, no support · +Y 2 h 10 min, 4.0 g
    ⚠ the 0.3 mm fin at x 12, z 12-30 is not extruded (narrower than one 0.45 mm line):
      make it ≥ 0.9 mm (two lines) or remove it
```

**Tests** (`tests/test_slicer.py`). Parser tests on stored header and feature
blocks from both slicers. A fake slicer script in `tests/fixtures/` that writes
canned G-code, so the whole path runs where no slicer is installed. A
real-slicer part, skipped when none is found: a 20 mm cube (no support, mass
close to volume × density × fill), a T-shape with a 15 mm overhang (support in
one orientation, none when flipped), and a 0.3 mm fin that coverage reports
while a 1.2 mm wall passes.

**Risks.** Profiles are the main friction (Orca wants fully resolved JSON,
PrusaSlicer ini files): ship generic ones and read the user's. Orca's CLI
changes between releases: pin and test a version range, and outside it fail
with a message that names the tested versions. Slicing takes seconds per part
and orientation: cache by `geom_sig` and profile hash. SLA: Orca slices FDM
only, so SLA evidence arrives with the PrusaSlicer driver; until then SLA
keeps the geometry estimates.

### 1.2 Cost and lead-time estimate

**Goal.** The playbook tells Claude to raise anything that affects cost, but
nothing computes it. With an estimate, "CNC $48 vs FDM $6 at qty 4" comes from
numbers, with the rates in view.

**Surface.** A new tool:
```
estimate(project, parts?, processes?: ["fdm", "cnc"], qty?: [1, 10, 100], rates?: "default" | <supplier>)
```
- Per part × process × quantity: unit price, NRE (setup, programming,
  tooling), lead time as a range, the basis (geometry heuristic, slicer or
  toolpath) and the three biggest cost drivers. Quantities default to the
  part's `qty` in project.json plus 1, 10 and 100.
- A process the part can't be made by says why, from the same DFM verdicts
  ("sheet: not a uniform plate or an fc.sheet_metal part").
- A `cost` check (`{"type": "cost", "part": "Bracket", "qty": 10, "max_unit":
  15}`) makes a budget an acceptance criterion, and a sweep constraint.

**Model** (every term is printed with its rate and where the rate came from):

| Process | Material | Time | Setup and other |
|---|---|---|---|
| fdm | filament g (slicer; else volume × shell and infill factor) × $/kg | print h (slicer; else a volumetric rate) × $/h; support removal by support g | per order; parts per plate from the bed area |
| sla | resin with supports × $/L | layers × layer time, shared by the parts on a plate | wash and cure per batch |
| cnc | stock: bbox + allowance, up to standard plate or bar × $/kg | roughing: removed volume ÷ MRR(material, tool Ø from the smallest corner radius); finishing: feature area ÷ rate; holes × per-hole time, more when tapped; toolpath time once 1.4 exists | setups (from `machining_access`) × setup time; CAM programming NRE; finish per area with a batch minimum |
| turning | bar (`bar_for`, cut length L + 3) × $/kg | removed volume ÷ MRR, plus facing, parting and threads | a secondary setup for cross holes |
| laser | sheet area (profile bbox × nesting factor) × $/m² by material and thickness | cut length ÷ speed(material, t) + pierces × pierce time | per-order minimum |
| sheet | the laser blank above | bends × per-bend time | press-brake setup per distinct bend set |
| moulding, casting | shot or pour weight with runners × $/kg | cycle time (cooling from the thickest wall) × machine rate for the clamp-force class | tooling NRE from envelope, cavities, side actions and finish class |

**Rates.** `server/costing.py` ships a default table in USD labelled "typical
US small-batch online-service rates, 2026: replace with your supplier's".
Users override it in `~/CAD/rates.json` (named suppliers) and per project in
`project.json → costing`. Users elsewhere supply a table in their own
currency: shop rates differ by region, not just by exchange rate. Calibration: `costing.quotes` holds real quotes
(part, process, qty, unit price, supplier, date); the estimate prints its ratio
to them and can scale that process ("calibrated ×1.3 against 2 quotes").
**Lead time** = queue days + machine hours ÷ capacity + finishing days +
shipping, per supplier, as a range.

**Example.**
```
ESTIMATE bracket r0042  (USD, default rates: estimates, not quotes)
part     process  qty 1             qty 10    qty 100   lead time   basis
Bracket  fdm      $9.80             $6.10/u   $4.90/u   2-4 days    slicer: 1 h 42 min, 31 g
Bracket  cnc      $118 ($70 NRE)    $48/u     $29/u     7-12 days   2 setups, 41 cm³ removed, R3 → Ø6 tool
drivers  cnc: setups 38 % · roughing 31 % · stock 12 %    fdm: machine time 64 % · material 21 %
```

**Where it shows up.** `release` prints the estimate in its reply and stores it
in `.release.json`, but keeps it out of the supplier package: your cost
estimate in a README you send a supplier only helps their side of the
negotiation. In the playbook, "ask before you change the process, material or
anything that affects cost" gains "and show the `estimate` difference".

**Tests** (`tests/test_costing.py`, against a pinned test rate table, exact
answers). The `cnc_plate` eval part: 100 × 60 × 8 stock plus allowance, the
pocket volume, 4 tapped holes, one setup for the features. The `laser_gusset`:
cut length = triangle perimeter + 3 × π × 5.5, 4 pierces. The `turned_spacer`:
Ø18 bar, 28 mm cut. Unit price falls with quantity; one more setup raises it.
Eval `quote_tradeoff`: "what's the cheapest way to make 4, and 400, of these
brackets?", graded on `estimate` being called for at least two processes and
the answer quoting its numbers and basis.

**Risks.** Absolute numbers will be off for many suppliers. The defences are
transparency (basis and rates shown, ranges rather than points), calibration
from real quotes, and wording (an estimate, never a quote). Live supplier
quotes are out of scope unless a supplier offers a documented API the user has
access to.

### 1.3 More processes

Each process lands as an F1 entry with: DFM in a new `server/dfm_<family>.py`
(manufacture.py is already over 1,500 lines), a drawing style, release
formats, rules for `lookup_standard(process=…)`, a `create_project` scaffold, a
cost model (1.2), a section in `references/processes.md`, standards tables
with their sources, and an eval case.

**Shared engine for moulding and casting: `server/pull.py`.**
- Pull direction: `parts.<Part>.pull`, else the axis direction with the least
  undercut area and the simplest parting line (later: candidates from the
  normals of large planar faces).
- Draft per face: the angle between the face and the pull direction, sampled
  across curved faces (reusing `_mesh_arrays`).
- Undercuts: a point on a face is released by the cavity or the core when a ray
  along +pull or -pull leaves the part (`_ray_tester`). Faces released by
  neither are undercuts, grouped, each with the lateral direction that frees it
  ("side action from +X").
- Parting line: the edges between faces released by different halves,
  reported as planar or stepped (a stepped parting line costs tooling).
- Wall thickness: `wall_thickness` extended to return the distribution and
  its locations, not only the minimum.

**1.3a Injection moulding: `moulding` (L)**
- fclib: `fc.draft(body, faces, angle, pull="+Z")` (PartDesign::Draft with
  readable errors, checking that every face got its draft), `fc.shell(body,
  open_faces, t)` (PartDesign::Thickness), `fc.rib(body, on, path, t, h)` and
  `fc.boss(body, on, at, screw="M3")` (boss OD, core hole and gussets from the
  usual ratios).
- DFM:
  - faces below the minimum draft (default 1°, about 1° more per 0.025 mm of
    texture depth as a rule of thumb; texture suppliers publish their own),
    with their area
  - undercuts, with the side action or lifter that frees each
  - nominal wall and deviations: zones over 1.5 × nominal (sink, voids),
    abrupt steps; ribs over 0.6 × the wall they stand on (sink); sharp
    internal corners (want r ≥ 0.5 × wall)
  - projected area along the pull → clamp force at the material's cavity
    pressure → machine class
  - cooling time from the thickest wall s, t ≈ s² ÷ (π²α) · ln(4(Tm - Tw) ÷
    (π(Te - Tw))) (α thermal diffusivity; melt, mould and ejection
    temperatures) → cycle time, which feeds 1.2
- Render: a draft map with F4 face colours (fine, low, none or negative,
  undercut) in colours that stay distinct for colour-blind viewers.
- Release: drafted STEP and a drawing noting as-moulded dimensions, shrinkage,
  material grade, texture, parting line and ejector marks; the README says a
  tooling quote is needed. No 3MF or STL.
- Tests: a 50 × 50 × 20 box shelled to 2 mm with 1° drafts passes, and the same
  box undrafted flags four walls of known area; a side hole gives one undercut
  freed from ±X; a 1.8 mm rib on a 2 mm wall warns of sink; the cooling time of
  a 2 mm ABS plate matches the formula. Eval `moulded_enclosure`: a PC/ABS
  enclosure lid with screw bosses.

**1.3b Casting: `casting`, sand, investment or die (M, after 1.3a)**
- The pull engine with per-type defaults: draft (sand 1-3°, die 1-2°,
  investment 0-0.5°), minimum wall (sand about 4 mm, die and investment about
  1.5 mm), and general tolerances and machining allowances from ISO 8062-3
  grades (a new standards table).
- Extra findings: cores (internal cavities neither half releases, with count
  and size); hot spots (thick sections fed only through thinner ones, a
  porosity risk); sharp junctions (hot tears).
- Machining allowance: `fc.cast_blank(body, machined=[faces], allowance=…)`
  makes the as-cast body from the finished one (functional faces offset
  outward, small holes filled to be drilled later), so one model.py gives
  both.
- Release: as-cast STEP and drawing, machined STEP and drawing, alloy and
  temper.
- Tests: a block with an internal blind cavity has one core; a thick boss on a
  thin plate is a hot spot; the allowance adds the expected volume.

**1.3c Extrusion-profile frames: `profile_frame` (M)**
- Tables: 2020, 2040, 3030 and 4040 T-slot profiles (slot width, centre bore
  and tap, area, Ix, Iy, mass per metre), flagged vendor-typical as the
  heat-set insert table is.
- fclib: `fc.extrusion(name, series, length, at, axis, rotate=0,
  end_taps=("M5", "M5"))` builds a Body with the real slot section, so T-nut
  and slot checks are honest, with frames at both ends and on each slot face.
  Brackets, T-nuts and screws come from the 2.5 library as `bom="buy"`
  reference parts.
- Checks: a cut list (length with cut tolerance, end taps, access holes); the
  connector BOM; fastener access; T-nuts in their slots; member deflection
  from E·I (simply supported or cantilever, by joint), or `analyze` with
  bonded members when the frame is statically indeterminate.
- Release: cut-list CSV (member, series, length, qty, end machining), BOM,
  assembly drawing with balloons, STEP.
- Tests: a 500 × 300 frame with corner brackets has exact butt-joint cut
  lengths; a 500 mm 2020 member with 100 N at mid-span deflects F·L³ ÷ (48·E·I)
  with I from the table.

**1.3d Welded frames: `weldment` (L)**
- Tables: EN 10219 square, rectangular and circular hollow sections, EN 10056
  angles, EN 10058 flat bar (sizes, A, I, W, mass per metre).
- fclib: `fc.member(name, profile="SHS 40x40x3", start, end, rotate=0,
  align="center")` and `fc.weldment(name, members, ends="mitre" | "butt",
  gap=0)`, which trims members where they meet (mitres at two-member corners,
  butt or cope at T-joints) and records each joint's seams with type and
  length. Plates and gussets are ordinary bodies.
- Checks: a cut list with end angles; nesting into stock lengths (6 m, with
  kerf) for the bars to buy and the offcut; fit-up gaps over 1 mm; weld access
  (a torch keep-out at each seam against the other members); vent and drain
  holes in closed sections when the finish is hot-dip galvanising; weld length
  per joint.
- FEA: bonded members (the CalculiX `*TIE` that fea.py already writes) stand in
  for the welds; beam elements for large frames come later.
- Drawing: ISO 2553 weld symbols and a cut-list table. TechDraw has a weld
  symbol object; whether it scripts reliably is a spike, with a note and leader
  as the fallback.
- Release: STEP of the frame, a STEP per member for tube laser cutting,
  cut-list CSV, drawing, BOM.
- Tests: a 1 m square SHS frame with mitred corners (four 1000 mm members with
  45° ends, nested in one 6 m bar), a coped T-joint, the vent-hole warning.

**Candidates, for prioritising.** The four above, plus others common at
online services. Pairs that share an engine are cheaper built together:
moulding then casting, profile frames then weldments, weldments with tube
bending.

| # | Process | What it adds | Reuses | Size |
|---|---|---|---|---|
| 1 | Injection moulding | draft, wall uniformity, parting line, undercuts and side actions, clamp force, cycle time, tooling cost | ray tests, wall thickness | L |
| 2 | Casting (sand, investment, die) | 1 plus cores, hot spots, machining allowance, as-cast and machined models | 1's pull engine | M |
| 3 | Extrusion-profile frames | T-slot members, brackets and T-nuts, cut list with end taps, deflection | the 2.5 library, bonded FEA | M |
| 4 | Welded frames | mitres and copes, cut list nested into stock bars, fit-up, weld access, weld symbols | 3's members and cut list | L |
| 5 | SLS / MJF nylon printing | no supports; trapped powder (enclosed voids need escape holes), minimum wall and gap, warping of large flats; cost by packed volume | the print checks | S |
| 6 | Metal printing (DMLS / SLM) | supports and their removal, residual stress on large flats, machining allowance on functional faces | the print checks, 2's allowance | M |
| 7 | Urethane (vacuum) casting | 10-50 parts from a silicone mould: wall and rib rules, tolerances, master and mould cost | 1's wall checks | S |
| 8 | Vacuum forming | draft, draw ratio, corner webbing, sheet thinning, mould cost | 1's draft map | M |
| 9 | Tube bending | bend radius against OD, straight length between bends, a bend table for the bender | 4's members | M |
| 10 | Wire EDM | through profiles in thick or hard plate, near-sharp internal corners, start holes | the laser plate profile and DXF | S |
| 11 | Sheet-metal gaps (not a new process) | hems, rounded outlines, automatic corner reliefs: the limits design.md lists today | `fc.sheet_metal` | S-M |
| 12 | 3+2-axis CNC | indexed setups for the faces today's check says need 4/5-axis | the 1.4 CAM preview | M |

**Order.** Awaiting the owner's ranking of this list; M9 and M10 hold 1-4 until
then.

### 1.4 CAM toolpath preview (FreeCAD's CAM workbench)

**Goal.** Evidence that a CNC part can be cut: toolpaths per setup, the
material they leave or gouge, and a cycle time, beyond the ray-based access
check.

**Spike S1, a gate run early in M7.** In FreeCAD 1.1 console mode:
- import the CAM modules (FreeCAD's headless notes warn that not every
  workbench module imports by default)
- create a job, `Path.Main.Job.Create("Job", [body], None)`, as FreeCAD's own
  CAM tests do
- add a tool controller through 1.1's reworked tool library
- add facing, profile, pocket or adaptive, and drilling operations; recompute;
  read each operation's `Path.Commands`; post-process one to G-code
- try the `PathSimulator` stock-removal module headless

Outcome: use FreeCAD's operations, or our own 2.5D path generator (planar
sections and `makeOffset2D`) if the operations prove fragile headless. The
simulation and the evidence are ours either way.

**Surface.** `check_manufacturing(simulate=true)` for cnc parts, configured by
`project.json → cnc: {"machine": "3-axis", "tools": "default" | <file>,
"stock": "auto" | [x, y, z]}`, and an `uncut` check (max mm³).

**Pipeline.**
1. Setups from `machining_access` (feature directions plus outer sides): one
   job per setup, with the part turned so the setup's access direction is the
   spindle's +Z. Stock is
   the bbox plus allowance, rounded to standard plate, or what the previous
   setup left.
2. Tools from a default library (flat end mills Ø1-Ø12, ball and chamfer
   mills; drills and taps from the ISO tap-drill and clearance tables): for
   each pocket, the largest end mill its smallest corner radius allows, then a
   finishing pass.
3. Operations: face, adaptive clearing, outer profile, pocket finishing,
   drilling and tapping (from detected holes and Hole features), chamfers.
4. Simulation: a voxel or tri-dexel stock at 0.1-0.25 mm (adapted to the part
   size, with a memory cap) carved by each move's swept tool in numpy; or
   `PathSimulator` if S1 shows it works headless.
5. Compare with the part: uncut = simulated stock outside the part beyond a
   tolerance (tool-radius corners, unreachable pockets); gouge = part material
   the tool removed. Both as volumes with locations.
6. Time: path length ÷ feed per tool from a small material × tool table (chip
   load × flutes × rpm, vendor-typical and flagged), plus tool changes and
   setups. It replaces the geometry estimate in 1.2.

**Example.**
```
✓ Plate - cnc: cuttable as modelled (toolpath preview, 3-axis)
    · 2 setups: +Z (face, adaptive Ø6, profile Ø6, drill Ø4.2 ×4, tap M5 ×4) · -Z (face)
    · cycle ≈ 14 min + 2 setups · 18.4 m of toolpath · 4 tools
    · uncut 0.00 mm³ beyond 0.05 mm · no gouges
    ⚠ preview only: check the toolpaths in your own CAM before cutting
```
With sharp pocket corners the same part reports "✗ 23.2 mm³ left in 4
internal corners of the 40 × 20 pocket: the Ø6 tool leaves R3; add R3 fillets
or accept R3 on the drawing". The image draws each tool's paths in its own
colour over the part (F4 polylines) and the uncut material in red.

**Tests** (`tests/test_cam.py`, known answers). A sharp-cornered 40 × 20 × 3
pocket cut with a Ø6 tool leaves (1 - π/4) · r² · depth = 5.79 mm³ per corner,
23.2 mm³ in all, and nothing once the corners are R3. A drilled Ø4.2 through
hole leaves nothing. A T-slot is reported as unreachable. A simple profile's
cycle time matches a hand calculation.

**Out of scope.** 4/5-axis, 3D surfacing (OpenCamLib), fixtures and
production G-code. A G-code export exists only as a labelled preview and never
goes into a release by default.

**Risks.** FreeCAD's CAM API moves between versions (Path became CAM in 1.0,
and 1.1 reworked the tool library): pin the versions S1 tested and report
them in `freecad_status`. Simulation time: bounded by resolution and a time
budget, and run as a background job. Arbitrary geometry: v1 covers 2.5D
prismatic parts and reports what the preview doesn't cover rather than
guessing.

---

## 4. LLM-driven iteration and project management

### 2.1 Optimisation and parameter sweeps

**Goal.** Claude searches instead of guessing: "minimise mass, with every
check passing and a safety factor of at least 3".

**Surface.** A new tool:
```json
{"project": "shelf_bracket",
 "vary": {"t": [4, 5, 6, 7, 8], "rib_h": {"min": 6, "max": 20, "step": 2}},
 "objective": {"min": "mass(Bracket)"},
 "subject_to": ["checks", "fea(default).safety_factor >= 3", "fea(default).max_disp <= 2"],
 "method": "grid",
 "budget": {"evaluations": 40, "minutes": 20}, "parallel": 3, "wait": false}
```
- `vary`: parameters from params.json (unknown names are an error), as lists
  or ranges; lists carry discrete choices such as a screw size or rib count.
- `objective`: `min` or `max` of a metric (F6), or `objectives` for a Pareto
  front (mass against cost, say).
- `subject_to`: `"checks"` means every checks.json check; the rest are metric
  comparisons.
- Methods: `grid` (up to three parameters); `lhs` (space-filling); `bisect`
  (one parameter against a monotone constraint: "the thinnest t with SF ≥ 3"
  in about 7 evaluations for 0.05 mm over 3-10 mm); `nelder_mead` (two to four
  continuous parameters).
- Returns: a compact table (the best ten and the Pareto front); the best
  feasible point with all its metrics; which constraint binds and by how much;
  sensitivities at the best point (finite differences); points that failed to
  build (infeasible, with the error; the sweep carries on); and one plot
  (objective against the main parameter, coloured by feasibility).
- It never writes params.json. It ends with what to adopt ("write t = 5.5 into
  params.json, source: sweep s03, then build"), in line with the playbook rule
  that overrides are temporary.

**How.** The server (stdlib) runs F3 `evaluate` per point on up to `parallel`
workers. FEA points are CPU-heavy, so the default is the CPU count divided by
CalculiX's threads. It runs as a background job with progress in `job_status`
("17/40 · best t = 5.5, 41.2 g, SF 3.1"), and as MCP progress notifications
when the client sends a progress token. Points append to
`.history/sweeps/sNN/points.jsonl`, so `resume: "s03"` carries on after a
cancel without re-solving any point. A point solves only the FEA cases its
constraints name, and computes DFM, cost or slicing only when referenced; cost
uses the geometry basis inside a sweep, and the chosen point is confirmed with
full evidence afterwards.

**Tests** (`tests/test_sweep.py`). test_fea's cantilever: the thinnest section
that meets a tip-deflection limit has the closed form t = (4·F·L³ ÷
(E·b·δ))^(1/3), and `bisect` must land within one step of it. A grid on a
plate (mass exactly linear in t; the feasibility edge from a `bbox` check). A
point that fails to build mid-range. A cancelled sweep, resumed, solves no
point twice. Eval `optimise_bracket`: "as light as possible with SF ≥ 3",
graded on `sweep` being called, the released mass staying under a limit, and a
rubric line for naming the binding constraint.

### 2.2 Visual diff between revisions

**Goal.** Show what moved between revisions, not just the Δ numbers.

**Surface.** `render(compare="previous" | "r0041" | "rev:A",
compare_style="overlay" | "side_by_side")`, with the usual views, section and
objects. When the geometry changed, the build report's Δ line ends with a
pointer to it. Later, the live viewer gets a "compare with previous" toggle (a
ghost of the previous GLB).

**How.**
- The other revision's geometry comes from F2, or from a rebuild of its
  snapshot when it was pruned.
- Parts match by label (renamed ones by geometric similarity). Unchanged
  material is grey, added material (new.cut(old)) blue, removed material
  (old.cut(new)) orange and translucent so it never hides the new part; the
  colours stay distinct for colour-blind viewers. The booleans use a fuzzy
  tolerance; where OpenCascade fails on coincident faces, faces are coloured
  by their deviation from the other shape instead (points sampled on each
  face, distance to the other solid).
- Same shape, new placement: the old outline ghosted, an arrow, and "moved
  +2.0 Z, turned 90° about Z".
- Both revisions use one fixed camera (F4), so `side_by_side` is a true
  flip-book.
- Text: per part, volume added and removed, bbox change, and holes added,
  removed, moved or resized (a `detect_holes` diff), alongside the report's
  feature Δ.

```
COMPARE r0041 → r0042 (bracket)
  Bracket   +1 240 mm³ / -310 mm³ · bbox z 24 → 26
            holes: 4× Ø3.4 → Ø4.5, same positions · new Ø5.6 insert hole at (0, 15)
  Motor     moved +2.0 mm Z
  image     out/bracket_compare_r0041_r0042.png
```

**Tests** (`tests/test_compare.py`). A 5 mm plate whose Ø10 hole opens to
Ø12: removed π(6² - 5²) · 5 = 172.79 mm³, nothing added. A moved reference
part: the exact displacement. An unchanged rebuild: "no geometry change". A
pruned revision rebuilt from its snapshot gives the same answer as the stored
one.

### 2.3 Checking against a reference drawing or photo

**Goal.** Today the playbook has Claude compare a drawing-style render with
the reference by eye. Instead: a calibrated overlay, and each of the drawing's
dimensions checked against the model in mm.

**Surface.** A new tool:
```json
{"project": "flange_plate", "image": "refs/plate_drawing.png", "view": "top",
 "calibrate": {"points": [{"px": [112, 640], "model": [0, 0]}, {"px": [1175, 640], "model": [90, 0]}]},
 "dims": [{"id": "bore-x", "value": 35, "type": "distance_x", "a": {"px": [112, 400]}, "b": {"px": [525, 395]}},
          {"id": "bore", "value": 20, "type": "diameter", "at": {"px": [525, 395]}}]}
```
- References: PNG and JPG (PIL); PDF pages, rendered with QtPdf if FreeCAD's
  PySide6 includes it (a spike; otherwise ask for a PNG); DXF and SVG read as
  vectors and compared exactly (a small reader for lines, circles, arcs and
  polylines).
- Calibration: two points give scale, rotation and offset; three an affine
  map; four or more a homography (a photo of a part lying flat); `"auto"` fits
  the outline's bounding box and refines by matching edge points; `{"scale":
  "1:1", "dpi": 300, "origin_px": […]}` suits scans at a known scale. The
  residual is reported ("0.06 mm RMS at 11.8 px/mm").
- Overlay: the view rendered in drawing style at that transform (F4) and
  compared both ways by edge distance (a numpy chamfer-distance transform): the
  share of model edges within 0.2 mm of a drawing line, plus drawing lines with
  no model counterpart and the reverse, located in model mm. Holes: around each
  model hole's projected centre, a local circle search in the drawing gives the
  centre offset and the Ø difference.
- Dimensions: Claude reads the dimension values with vision and passes them as
  `dims`, each with the pixel positions of the features it dimensions. The tool
  snaps each position through the calibration to a model feature (hole centre,
  edge, face) and measures the model. Vision supplies the meaning; geometry
  supplies the number.
- Persist: the dims are saved in `refs/<drawing>.dims.json`, and a
  `drawing_dims` check re-verifies the model against the drawing on every
  build.

```
COMPARE plate_drawing.png (top) ↔ flange_plate r0007
  calibration  2 points, 11.81 px/mm, residual 0.06 mm
  outline      98 % of model edges within 0.2 mm of a drawing line
  dimensions   ✓ width 90 → 90.00 · ✓ height 50 → 50.00 · ✓ 4× Ø4.5 → 4.50
               ✗ bore-x 35 → 37.00 (Δ +2.00) · ✗ corner R5 → R3 (Δ -2.0)
  unmatched    a Ø6 circle in the drawing at (45, 40) has no hole in the model
  image        out/compare_plate_drawing_top.png (drawing grey, model red, agreement black)
```

**Tests** (`tests/test_reference.py`). `evals/fixtures/reference_drawing_png.py`
already draws the plate: generate copies with known errors (the bore moved
2 mm, R5 made R3, a hole missing) and require each to be found within 0.2 mm at
300 dpi. A scan turned 5° and scaled 0.97 still calibrates within 0.1 mm. A DXF
reference matches to 0.001 mm. A synthetic photo (a homography-warped render)
within 0.5 mm. Eval `drawing_mismatch`: a model with two deliberate errors and
the drawing it should match; the agent has to find and fix both.

**Risks.** Noisy scans and hand drawings: dimension lines, text and hatching
confuse edge matching, so comparison stays in a band around the model's own
edges and holes, and Claude can mask regions. First or third angle: the tool
takes the view by name, so reading the projection stays with Claude, as the
playbook already asks.

### 2.4 Revision control and change notes

**Goal.** Released revisions (A, B, C) with change notes, a revision table on
the drawings, and a git commit and tag per release. The build counter rNNNN
stays as the work-in-progress number underneath.

**Surface.**
- `release(revision="next" | "B", change_note="…")`. Letters follow ASME
  Y14.35: they skip I, O, Q, S, X and Z, and after Y come AA, AB …. Companies
  differ on the first release (A, or a dash with A as the first change), so
  `project.json → revision.first` sets it (default A); `revision.scheme:
  "numbers"` gives 01, 02 … instead.
- Revisions are per part, which is the industry norm: a revision belongs to the
  drawing that defines the part (ASME Y14.35; the ISO 7200 title block has one
  revision field per document), and PLM systems keep one revision per item with
  iterations inside it (Windchill's A.1, A.2), which is how the build revisions
  rNNNN relate to letters here. A release in which only the Plate changed moves
  the Plate to B and leaves the Bracket at A; an assembly has its own letter,
  changed when its own drawing or BOM changes. A one-part project reads as one
  letter.
- The same inputs as the released revision re-issue the same letter (today's
  `release_target` logic). Changed inputs without a note stop the release with
  a **drafted note**, which Claude confirms with the user and passes back.
- The draft comes from data that exists or is built here: parameter changes
  (`t 5 → 6`), feature and hole changes (the report's Δ, the 2.2 hole diff),
  checks added, removed or changed (`checks_changes`), mass and bbox, DFM
  verdicts, drawing options and changed refs.
- Form, fit and function: when an interface moved (a hole pattern an `align`
  or `positions` check measures, a frame, a larger outer envelope), the draft
  warns that the new part isn't interchangeable with the old one and suggests a
  new part number rather than a new revision, as ASME practice does.
- `inspect(level="revisions")` lists the history, and `render(compare="rev:A")`
  shows a change.

**Records and outputs.**
- `revisions.json` at the project root: per part, each letter with its build
  rev, date, note, package and git commit.
- Drawings: a released sheet's title-block `revision_index` is the part's
  letter. Drafts keep today's build-rev numbering from `edit_revision`, so a
  draft can't pass for a release. A revision table (letter, description, date)
  sits above the title block, probably a Spreadsheet placed with TechDraw's
  spreadsheet view (a spike).
- BOM.csv gets a revision column, the README a revision history with the note,
  and exported file names carry the letter (`Bracket_B.step`), configurable.
- The build report's header says what is being worked on: "Bracket rev B
  released (r0042) · 3 inputs changed since".

**git**, opt-in per project (`project.json → revision.git: "commit"`):
- On release, commit the design inputs only (model.py, params.json,
  checks.json, project.json, drawings.json, fea.json, requirements.md,
  revisions.json, refs/, drawings/*.FCStd, and the release folder's
  `.release.json`, README.md and BOM.csv) with `git commit -- <paths>`, so the
  user's other staged work is never swept in. Message `<project> Bracket rev B:
  <note>`, tag `<project>/Bracket-B`. It never pushes.
- No repository yet: offer `git init` with a `.gitignore` for build/, out/,
  .history/ and the release zips, only with the user's OK. Large binaries go
  through git LFS when the repository is set up for it.
- git missing or blocked (the Xcode shim): the release still succeeds and says
  the commit was skipped.

**Tests** (`tests/test_revisions.py`). Release A; change t; the next release
stops with a draft containing `t 5 → 6`; with the note it gives B, with B in
the title block, A and B in the revision table and B in the BOM and README. An
unchanged re-release stays B. Changing only the Plate moves only the Plate. In
a temporary git repository: the commit holds only project paths, the tag
exists, nothing is pushed; with git missing the release succeeds with a note.
Eval `revision_b`: "the customer wants 6 mm instead of 5: issue the next
revision", graded from `revisions.json` and the title block.

### 2.5 Quick-turn intake: datasheet to reference part with frames

**Goal.** One prompt from "here's the datasheet, the STEP file or the part" to
a verified reference part with named frames, interface checks and sources,
instead of hand-modelling an envelope.

**(a) Part spec, `refs/<name>.part.json`.** What Claude writes from a
datasheet. Reading the PDF stays with Claude, citing pages:
```json
{"name": "Motor", "bom": "context",
 "body": [{"box": [42.3, 42.3, 48], "at": [0, 0, -48], "chamfer": 3, "src": "nema17.pdf p2"}],
 "features": [{"boss": {"d": 22, "h": 2}, "src": "p2"},
              {"shaft": {"d": 5, "length": 24, "flat": 0.5, "from": 2}, "src": "p2"},
              {"holes": {"pattern": "rect", "w": 31, "h": 31, "thread": "M3", "depth": 4.5}, "src": "p2"},
              {"keepout": {"box": [16, 12, 10], "at": [0, -27, -12]}, "why": "connector and cable bend"}],
 "frames": {"mount": {"at": [0, 0, 0], "normal": "+Z"}, "shaft": {"at": [0, 0, 26], "normal": "+Z"}},
 "checks": "auto"}
```
`fc.ref_part("Motor", spec="refs/motor.part.json")` builds it (boxes,
cylinders, extruded profiles, hole patterns with cosmetic threads, shafts with
flats, keep-outs as a companion `bom="none"` part), sets the frames and BOM
data, and keeps each number's source. On build it cross-checks the spec (hole
spacing against PCD, features inside the envelope, frames on faces), and
`"checks": "auto"` measures the built interfaces against the spec values in the
build report. That is the playbook's gate ③ ("measure its interface features
against the datasheet before you use it"), done automatically.

**(b) The `reference_part` tool.**
```
reference_part(project, name, source={"file": "refs/motor.step"} | {"spec": "refs/motor.part.json"}
                                     | {"library": "nema17", "length": 48} | {"url": "…"})
```
- For STEP, IGES or STL: a units check (a file modelled in inches is 25.4 ×
  off the datasheet envelope, so it is flagged); interface detection with
  `detect_holes`, `detect_bosses`, hole patterns and the large planar faces;
  **proposed frames** ("mount: the face with the 4 × M3 31 × 31 pattern, +Z out
  of it; shaft: the Ø5 boss axis"); and optional defeaturing for speed
  (fillets, text and small features dropped, interface faces kept).
- Returns the measured interface table, the proposed frames drawn on a render
  (F4) and a model.py snippet, and writes `refs/<name>.part.json` with the
  frames, source and BOM role.
- `url`: the server downloads the file itself (a datasheet PDF, a STEP, or a
  zip of them) into `refs/`, from a link the user gave or one Claude found for
  the part, and saves the source (URL, date, sha1, size, licence or terms note)
  in `refs/<name>.source.json`. Guards: HTTPS only; a size cap (200 MB); an
  allow-list of file types (STEP, IGES, STL, OBJ, 3MF, PDF, PNG, JPG, ZIP);
  zips unpacked with path checks; and the file is parsed in a worker, as every
  model file is. Whether FreeCAD's bundled Python finds a CA bundle for HTTPS
  is checked by `freecad_status`, and a failure names the fix. Pages behind a
  login stay with the user: they download the file and
  `write_file(encoding="base64")` copies it in, as now.

**(c) A library.** `server/partlib/*.part.json` ships specs for common
bought-out parts, every number with its source, and vendor-specific ones
flagged "typical: confirm with your part" as the heat-set insert table is:
- NEMA 8 to 23 stepper mounting faces (hole pattern, pilot and shaft by frame
  size; body length a parameter)
- bearings from the existing bearing table
- ISO 4762, 7380 and 10642 screws, ISO 4032 nuts and ISO 7089 washers, with
  BOM spec strings; heat-set inserts
- the 1.3c T-slot profiles and their brackets and T-nuts
- MGN9 and MGN12 rails, LM8UU bushings, SG90 and MG996R servos, 40-120 mm fans
- Raspberry Pi and Arduino mounting patterns with connector keep-outs

`fc.ref_part("Motor", lib="nema17", length=48)` uses it, and a user library in
`~/CAD/_library/` (same format) overrides the shipped one. Library reads go
through `fc._ctx.reads`, so editing a library part makes a new revision, as
other files that model.py reads already do. `server/partlib/` is copied by
`build_plugin.py` like `server/viewer/`.

**Tests** (`tests/test_intake.py`). The library NEMA 17 measures a 31.0 pattern,
a Ø22 pilot and a Ø5 shaft, and its auto checks pass. A spec whose PCD
contradicts its hole spacing is refused. An inch STEP is flagged. Frames
proposed on a generated vendor-like STEP (a box with four holes and a boss)
land on the right face. Downloads from a local test server: the size cap, the
type allow-list, a zip with `../` paths refused, provenance written.
`nema17_print` and `two_part_assembly` rerun with the
library: the same graders, fewer tool calls. New eval `datasheet_intake`: two
datasheets and one plate holding both parts, graded on frames and interface
checks.

---

## 5. Playbook and command changes

| Where | Change |
|---|---|
| SKILL.md ① Intake, ③ Reference parts | `reference_part` and the library first; gate ③ is the auto interface checks |
| SKILL.md ⑥ Verify, evidence table | "prints as intended" → `check_manufacturing(simulate=true)` slicer evidence · "can be cut" → toolpath preview · "matches the drawing" → `compare_reference` per-dimension results (replacing the render-by-eye row) · "costs about X" → `estimate`, basis shown |
| SKILL.md ⑦ Iterate | sizing against a target → `sweep`, reporting the binding constraint and margin; `render(compare="previous")` after a structural change |
| SKILL.md ⑨ Release | revision letter and change note; git commit when the project opts in |
| SKILL.md §3 Communicating | changing the process, material or quantity comes with the `estimate` difference |
| SKILL.md §8 Definition of done | an estimate when process or quantity was a choice; a change note on every release after the first |
| references/ | new pages `costing`, `slicing`, `cam`, `sweeps`, `revisions`, `intake`; `processes` gains moulding, casting, profile frames and weldments |
| commands/ | `/freecad-design:estimate` (compare processes and quantities) and `/freecad-design:revise` (next revision with a change note) |

---

## 6. Roadmap

Each milestone ends as M4 and M5 did: stdio suites with known answers against
real FreeCAD, playbook and reference-page updates, new eval cases, a hardening
round from the eval runs, and a `SERVER_VERSION` bump (the plugin version
follows it).

| Milestone | Scope | Why here |
|---|---|---|
| **M7 Cost and real printability** | F1, F5, F6, F2; 1.2 estimate on the geometry basis; 1.1 slicer; spike S1 | The most value for the effort: the estimate only needs metrics DFM already computes, and the slicer both sharpens it and closes the most common printability gap. S1 runs early because CAM is the biggest risk |
| **M8 Iteration** | F3, F4 fixed scale; 2.1 sweeps; 2.2 visual diff; 2.4 revisions and git | Reuses F2 and F3, and makes everything after it more useful: a sweep can minimise cost, and a change note can carry its diff |
| **M9 Intake and formative processes** | 2.5 intake and library; 1.3a moulding; 1.3b casting | The library feeds 1.3c; moulding and casting share the pull engine |
| **M10 Frames and the reference check** | 1.3c profile frames; 1.3d weldments; 2.3 `compare_reference` | Frames use the library; the reference check uses F4's overlays |
| **M11 CAM** | 1.4 toolpath preview; estimate v2 with toolpath times | Built on S1's outcome and every foundation |

```
F1 process registry ──┬──► 1.2 estimate ◄──── 1.1 slicer ◄──── F5 externals
                      │          ▲
                      │          └─────────── 1.4 CAM ◄── S1 spike, F4, F5
                      ├──► 1.3a moulding ──► 1.3b casting
                      └──► 1.3c profile frames ──► 1.3d weldments
2.5 intake + library ─────► 1.3c (T-slot profiles, connectors)
F2 revision store ────┬──► 2.2 visual diff ──► 2.4 (diff in change notes)
                      └──► 2.4 revisions + git
F3 evaluate ──────────────► 2.1 sweeps ◄── F6 metrics ◄── 1.1, 1.2, 1.4 add metrics
F4 renderer ──────────────► 2.2, 2.3, 1.3a draft map, 1.4 toolpaths, 2.5 frames
```

---

## 7. Decisions

| Question | Decision | Affects |
|---|---|---|
| Currency of the default cost table | **USD** (decided 2026-11-09), with a user rates file and calibration from real quotes | 1.2 |
| Which slicer first | **OrcaSlicer, then PrusaSlicer**: recommended on licence, support and developer activity (§1.1), to confirm | 1.1 |
| Order of the new processes | **open**: rank the candidate list in §1.3 | 1.3, M9-M10 |
| Revision scheme | **letters per part** (decided 2026-11-09; the industry norm, §2.4). Open: first release A or a dash (default A); git opt-in per project (recommended) | 2.4 |
| May the server download vendor files itself? | **yes** (decided 2026-11-09), with the guards in §2.5 | 2.5 |
| CAM output | **open**: recommended a labelled preview G-code export, never in a release by default | 1.4 |

## 8. Not planned

- Live supplier quotes, unless a supplier offers a documented API the user has
  access to.
- Mould-flow simulation (gates, weld lines, warpage).
- Production CAM: 4/5-axis, 3D surfacing, fixtures, validated post-processors.
- Weld strength and fatigue beyond the bonded linear FEA.
- A hosted parts library.

## 9. External references checked while planning

- OrcaSlicer CLI: <https://www.orcaslicer.com/wiki/cli/cli_mode>
- Headless FreeCAD (workbench modules in console mode):
  <https://wiki.freecad.org/Headless_FreeCAD>
- Slicer licences and activity: the GitHub search API, 2026-11-09
  (`prusa3d/PrusaSlicer`, `OrcaSlicer/OrcaSlicer`, `bambulab/BambuStudio`,
  `Ultimaker/Cura`, `Ultimaker/CuraEngine`)
- ASME Y14.35 revision letters, as summarised in:
  <https://ndia.dtic.mil/wp-content/uploads/2008/technical/GastonEngineeringDrawingsY14_35.pdf>
