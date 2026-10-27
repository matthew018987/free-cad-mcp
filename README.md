# FreeCAD MCP server

Lets Claude design mechanical parts in FreeCAD and send them off for
manufacture: 3D printing, CNC milling, turning, laser cutting, sheet-metal bending. Claude writes
FreeCAD Python (`model.py`); the server runs it in a separate FreeCAD process
and returns evidence it can act on:
- a build report: every feature's status, hole specs, sketch constraint
  status, mass, acceptance checks, and changes since the last build
- fit, section and alignment measurements
- manufacturability checks per process
- strength, stiffness, natural frequencies and buckling by FEA (CalculiX), for
  one part or bonded parts: stress, safety factor, deflection,
  with equilibrium and mesh-convergence checks
- ISO drawings with every dimension verified against the model
- a release package: verified STEP/3MF/STL/DXF files, drawings, BOM and
  manufacturing notes, zipped

```
brief + datasheets → requirements → model.py → build ⇄ verify (checks, fit, section, measure)
                  → check_manufacturing → drawing → release → zip for the supplier
```

**Zero dependencies.** The server is stdlib-only Python and runs on the Python
bundled inside FreeCAD.app, so FreeCAD is the only thing to install. There is
no "start RPC server" button, and an OpenCascade crash only ends a worker
process. An optional FreeCAD addon, *MCP Reload*, keeps open documents in step
with Claude's rebuilds.

Status: M1 (core loop), M2 (verification), M2b (Assembly workbench), M3
(drawings, export, manufacturability, release) and M4 (Cowork / Claude Code
plugin, reload helper, evals) built, evaluated end to end and hardened, and M5
(FEA with CalculiX) built — see [DESIGN.md](DESIGN.md) §11.

## Requirements

- FreeCAD 1.x — tested with 1.1.4 on macOS (`/Applications/FreeCAD.app`).
  Elsewhere, set `FREECAD_APP` to FreeCAD.app, its bundled `python`, or a
  `freecadcmd` binary.

## Install

**As a plugin (Cowork or Claude Code).** Build it, then install the zip:

```bash
python3 tools/build_plugin.py
```

This writes `dist/freecad-design.zip` (the plugin: MCP server, the
`freecad-design` skill, four commands, the reload addon and evals),
`dist/freecad-design/` (the same, unzipped) and `dist/freecad-mcp.mcpb` (the
server alone, as an MCP bundle). In Cowork: Customize ▸ Plugins ▸ Add ▸ Upload
plugin ▸ `freecad-design.zip`. In Claude Code:

```bash
claude --plugin-dir dist/freecad-design
```

Then ask Claude to run `freecad_status`: it should report `host.platform:
darwin` and FreeCAD found. If it reports `linux` or no FreeCAD, the app is
running plugin servers away from your Mac; use the manual setup below.

**Manually, Claude Desktop / Cowork.** Quit Claude completely (⌘Q), then add
this to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/Applications/FreeCAD.app/Contents/Resources/bin/python",
      "args": ["-I", "/Users/matthew/Development/Projects/freeCAD MCP/server/freecad_mcp.py"],
      "env": { "FREECAD_MCP_HOME": "/Users/matthew/CAD" }
    }
  }
}
```

Then upload `skills/freecad-design/` as a skill (or paste `SKILL.md` into the
project instructions) so Claude follows the design playbook. Without it,
Claude can still read the playbook and its reference pages through the `guide`
tool.

**Manually, Claude Code.**

```bash
claude mcp add freecad -e FREECAD_MCP_HOME=$HOME/CAD -- /Applications/FreeCAD.app/Contents/Resources/bin/python -I "/Users/matthew/Development/Projects/freeCAD MCP/server/freecad_mcp.py"
```

## Tools

| Tool | What it does |
|---|---|
| `freecad_status` | FreeCAD location and version (runs a worker), home, roots, GUI, jobs, host, reload helper, and a `setup_hint` when FreeCAD can't be reached |
| `guide` | The design playbook, one section of it (by number or keyword), or a reference page |
| `create_project` | Scaffold `project.json`, `params.json`, `model.py` (suited to the process: plate, pocketed block, turned part, flat blank), `requirements.md`, `checks.json`, `refs/ build/ out/` |
| `list_projects` | Projects and loose `.FCStd` files under the home |
| `read_file` / `write_file` / `edit_file` | Text files, every overwrite backed up in `.history/backups/`; `write_file(encoding="base64")` copies vendor STEP/STL/PDF/images into `refs/` |
| `build` | Run `model.py` → `build/<name>.FCStd` plus the **build report** (features, holes, sketches, mass, Δ, `checks.json` ✓/✗) and a render. `params` overrides, `variant` builds, `adopt_gui_params`, `wait:false`. The revision moves on only when the inputs change |
| `run_script` | FreeCAD Python against any document; `save="revision"` writes the next `.rNN` and never touches the original |
| `inspect` | Summary, object geometry index + detected holes, sketches, parameters, **edits made in the FreeCAD GUI** — FCStd, STEP, IGES, BREP, STL, OBJ |
| `render` | Labelled grid of views; `shaded` / `xray` / `drawing` styles; sections; highlighting |
| `measure` | Distances, angles (axis ↔ plane too), coaxiality and radial gaps, hole spacing, the wall around a hole, hole-pattern alignment, frames — by geometry reference |
| `check_fit` | Interference / contact / clearance for every pair of parts (per-pair minimums with `pair_min`), with the overlap rendered in red |
| `section` | 2D cross-section: areas, exact extents, in-plane gaps or overlaps (intended ones allowed), the gap on each side of a cut hole axis, hatched plot with zoomed insets |
| `lookup_standard` | ISO clearance holes, threads, heads and counterbores, nuts, washers, inserts, ISO 286 fits (pairs or one class), ISO 2768 general tolerances, common ball bearings, process rules |
| `analyze` | FEA of one part or several bonded parts (gmsh + CalculiX, both bundled with FreeCAD): linear static (default), natural frequencies or linear buckling; supports, loads and bonded faces on faces/holes, materials from the project; max von Mises stress and where (also away from the supports), safety factor, max deflection, reactions vs applied load, finer-mesh convergence, stress/deflection image; load cases saved in `fea.json`; `fea_*` checks |
| `check_manufacturing` | Per process: print orientation in words, overhangs and walls (FDM/SLA); access directions, internal corner radii vs tool size, unreachable corners (CNC); bar stock, L/D and secondary ops (turning); uniform plate (laser) |
| `drawing` | ISO part drawings: `create` (style, views, paper and scale, verified dimensions incl. pockets, hole/thread/fit callouts, section through turned parts with bores, bend table and flat pattern for bent sheet, notes, title block, `ops` replayed on every regeneration → PDF/SVG/DXF + preview), `read`, `edit` (verified added dimensions, saved as the next revision) |
| `export` | STEP/IGES (re-import checked), 3MF/STL/OBJ (watertight check), BREP, DXF/SVG flat profiles |
| `release` | Build → checks → manufacturability → per-process exports (print files pre-oriented) + drawings (current sheets shipped as they are) → BOM.csv + README.md → zip; refuses to ship a failing design, a failed export, an unknown part name or a stale hand-edited drawing |
| `job_status` | Collect background work |
| `open_in_gui` | Open a file in the FreeCAD GUI, starting it if needed; `install_reload_helper` (with your OK) installs MCP Reload |

Inside scripts, `fc` (`server/fclib.py`) provides parameters bound to a
spreadsheet, fully constrained sketch profiles, standard holes (ISO 273
clearance, tapped 6H, ISO 4762/10642 counterbores and countersinks), reference
parts with named frames, mates, heat-set insert holes, DIN 974 counterbores, fit
bores (`"Ø8 H7"`), nut traps, the standards tables (`fc.std`), FreeCAD
Assembly workbench helpers (`fc.assembly`, `fc.ground`, `fc.joint` between
named frames, `fc.solve` with joint verification), and turned parts
(`fc.turned` with per-step fits, `fc.external_thread`).

Geometry references used by `measure`, `check_fit`, `section` and
`checks.json`: `Bracket`, `Bracket.face(+Z)`, `Bracket.holes(d=3.4)`,
`Bracket.holes(d=3.4)[0]`, `Motor.bosses(d=22)`, `Motor.frame(mount)`, `[x, y, z]`, `plane(z=8)`,
`axis([p], [d])`. Everything is measured in global coordinates, including
parts inside assemblies and links.

## Live view in FreeCAD

`open_in_gui` with `install_reload_helper: true` (Claude asks you first)
copies the *MCP Reload* addon into FreeCAD's `Mod/` folder and
`MCP_Reload.FCMacro` into your macro folder. From then on FreeCAD reloads an
open document when Claude rebuilds its file, keeping the camera, the active
document and the view in front. The report view says "MCP Reload: watching N
open document(s)" when it starts; run the macro to switch it off or on.

- A document with unsaved edits of yours (including Python-console and macro
  edits) is never reloaded. The report view says so: close it **without
  saving** (or File ▸ Revert) to load the rebuild — saving would overwrite it.
- It never reloads in the middle of an edit (edit mode, a task panel or a
  dialog open); it waits and reloads afterwards.
- Documents that were already out of date when the watcher started are
  reloaded too, unless FreeCAD already marked them modified: those count as
  unsaved until you save or revert them.

## Configuration

| Env var | Effect |
|---|---|
| `FREECAD_APP` | FreeCAD.app, its `bin/python`, or `freecadcmd` (default: `/Applications/FreeCAD.app`, then other FreeCAD bundles newest first, then `~/Applications`) |
| `FREECAD_PYTHON` | Interpreter `launch.sh` starts the server with (default: FreeCAD's bundled Python) |
| `FREECAD_MCP_HOME` | Projects directory; relative paths resolve here (default `~/CAD`; `$VARS` and `~` expand) |
| `FREECAD_MCP_ROOTS` | `:`-separated allow-list of directories the server may touch |

## Layout

```
server/freecad_mcp.py   MCP server (stdlib only)
server/worker.py        runs one request inside FreeCAD
server/fclib.py         `fc` helper library for model scripts
server/render.py        headless renderer
server/analysis.py      measure, check_fit, section, checks, GUI-edit diffs (inside FreeCAD)
server/standards.py     ISO tables: holes, threads, fasteners, fits
server/manufacture.py   verified exports, DFM checks, release package (inside FreeCAD)
server/drawing_worker.py  TechDraw drawings — runs in the FreeCAD GUI process
server/drawing_support.py shared drawing geometry helpers
server/fea.py           FEA load cases: gmsh mesh, CalculiX solve, results (inside FreeCAD)
skills/freecad-design/  the playbook skill (SKILL.md) and its reference pages
prompt/                 server instructions, project-instructions template
plugin/                 plugin manifest, .mcp.json, commands, plugin README
freecad_addon/          MCP Reload addon and macro
tools/build_plugin.py   builds and validates dist/ (plugin zip, .mcpb)
evals/                  eval cases, fixtures, mcp_cli.py and run_case.py
examples/nema17_bracket worked example
tests/smoke_test.py     M1 end-to-end tests over stdio with real FreeCAD
tests/test_m2.py        M2 verification tests against tests/fixtures/fitlab
tests/test_m3.py        assemblies, export, drawings, DFM, release
tests/test_m4.py        plugin build, packaged server, reload helper, eval harness
tests/test_analysis.py  hole detection, measure semantics, check types
tests/test_drawings.py  drawing read/edit/ops, paper and scale, sheet status
tests/test_manufacture.py  release docs, oriented exports, DFM wording, standards
tests/test_worker_fclib.py hole compensation, ref-part roles, revisions, build lock
tests/test_server.py    argument validation, guide, setup hints, release flows
tests/test_reload_addon.py MCP Reload in real FreeCAD GUIs
tests/test_evals_plugin.py generated plugin eval suite, graders, new cases
tests/test_m5.py        FEA against theory (beam deflection and stress, natural frequency,
                        Euler buckling, bonded vs solid), equilibrium, load cases, stale checks
tests/test_sheet_metal.py bent sheet metal: folding, flat pattern, DFM, flat DXF, eval case
examples/turned_shaft   lathe part: fits, thread, cross hole → drawing + release
DESIGN.md               design, spike results, roadmap
```

## Tests

```bash
python3 tests/smoke_test.py
```

```bash
python3 tests/test_m2.py
```

```bash
python3 tests/test_m3.py
```

```bash
python3 tests/test_m4.py
```

```bash
python3 tests/test_m5.py
```

Then the hardening suites: `test_analysis.py`, `test_drawings.py`,
`test_manufacture.py`, `test_worker_fclib.py`, `test_server.py`,
`test_reload_addon.py`, `test_evals_plugin.py`.

1,205 checks over real stdio JSON-RPC with real FreeCAD, about 20 minutes in
total. Drawings run FreeCAD in the background (on macOS nothing appears on
screen); the reload and plugin suites open FreeCAD windows for a few seconds
at a time.

`test_m4.py` (49 checks) covers:
- the plugin build: layout, manifest, `.mcp.json`, skill and commands, zip and
  `.mcpb`, and the generated `claude plugin eval` suite
- the packaged server started exactly as `.mcp.json` says, its reference
  pages and host diagnostics
- the reload helper: install, reload on rebuild, never reloading a document
  with unsaved edits, autostart, and the macro toggle
- the eval harness: setup safety, briefs, and scripted known-good and
  known-bad solutions that the graders must pass or fail

## Evals

`evals/cases.json` holds 15 end-to-end design tasks: a printed NEMA 17
bracket, a turned spacer, a CNC plate, a laser gusset, fixing an
interference, adopting a GUI edit, annotating an existing drawing, a release
that must be refused, a turned shaft with a k6 bearing seat and an M8 thread,
a two-part assembly, patching a supplied model as a revision, a section
request, re-making a part from a PNG reference drawing, sizing a printed shelf
bracket by FEA (safety factor and sag), and an off-topic request that must
not touch the CAD tools. Each has a prompt, fixtures,
automatic checks and a rubric. Run one by hand with an agent:

```bash
python3 evals/run_case.py setup cnc_plate /tmp/eval_cnc
```

```bash
python3 evals/run_case.py brief cnc_plate /tmp/eval_cnc
```

Give the brief to an agent (it ends by writing its final message to
`.final_message.md`, which some graders read), then grade the workspace:

```bash
python3 evals/run_case.py grade cnc_plate /tmp/eval_cnc
```

`setup --keep` adds a case's files to an existing folder and records them in
`.eval_keep.json`, so a later setup removes only what it added.

The built plugin also contains the cases as a `claude plugin eval` suite.
It is a smoke test (tool-use, trace and judge graders); `run_case.py grade`
is the authoritative check. From `dist/freecad-design`:

```bash
claude plugin eval . --scaffold --allow-real-servers --allow-tools "mcp__plugin_freecad-design_freecad__*"
```

Results land in `dist/freecad-design/evals/results/`, which rebuilds keep and
the zip leaves out.

`test_m3.py` (37 checks) covers:
- assemblies: joints hold; a wrong joint mode is caught by the checks
- verified exports, including DXF with true circles at the origin
- drawings: every dimension verified, callouts, fits with limits, threads,
  per-part documents, read and edit
- manufacturability verdicts for every process
- release packages, and the three refusal gates (failing checks, not
  manufacturable, unported GUI edits)

`test_m2.py` (56 checks) runs against a fixture whose answers are known in
advance:
- a 500 mm³ overlap, a 2.0 mm gap
- a shaft with a 0.1 mm radial gap in its bore
- a hole pattern offset by 0.03 mm
- a contact pair whose nearest real gap is 0.42 mm
- a part inside a moved container, and a link
- standards lookups, `checks.json` with three designed failures
- detecting, adopting and refusing GUI edits
- the NEMA17 example's acceptance checks

`smoke_test.py` (50 checks) covers:
- the protocol and every tool
- build reports, including a failed fillet and a script error
- change tracking, and the overwrite guard
- inspect on FCStd and STEP, and renders
- revision-safe patching
- crash isolation (a SIGSEGV in a worker), background jobs, concurrency
  and cancellation
- the folder allow-list
