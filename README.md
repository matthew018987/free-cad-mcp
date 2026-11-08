# FreeCAD MCP server

Lets Claude design mechanical parts in FreeCAD and send them off for
manufacture: 3D printing, CNC milling, turning, laser cutting and sheet-metal
bending. Claude writes FreeCAD Python (`model.py`); the server runs it in a
separate FreeCAD process and returns evidence it can act on:
- a build report: every feature's status, hole specs, sketch constraint
  status, mass, acceptance checks, and changes since the last build
- fit, section and alignment measurements
- manufacturability checks per process
- strength, stiffness, natural frequencies and buckling by FEA (CalculiX)
- ISO drawings with every dimension verified against the model
- a release package: verified STEP/3MF/STL/DXF files, drawings, BOM and
  manufacturing notes, zipped

```
brief + datasheets → requirements → model.py → build ⇄ verify (checks, fit, section, measure)
                  → check_manufacturing → drawing → release → zip for the supplier
```

The server is stdlib-only Python and runs on the Python bundled inside
FreeCAD.app, so FreeCAD is the only thing to install. Design notes are in
[docs/design.md](docs/design.md).

## Requirements

FreeCAD 1.x, tested with 1.1.4 on macOS (`/Applications/FreeCAD.app`).
Elsewhere, set `FREECAD_APP` to FreeCAD.app, its bundled `python`, or a
`freecadcmd` binary.

## Install

**Claude Code, from GitHub.**

```
/plugin marketplace add matthew018987/free-cad-mcp
```

```
/plugin install freecad-design@freecad-mcp
```

**Cowork.** Build the plugin zip, then Customize ▸ Plugins ▸ Add ▸ Upload
plugin ▸ `dist/freecad-design.zip`:

```bash
python3 scripts/build_plugin.py
```

The build also writes `dist/freecad-mcp.mcpb`, the server alone as an MCP
bundle.

Then ask Claude to run `freecad_status`. It should report
`host.platform: darwin` and FreeCAD found; if not, the app is running plugin
servers away from your Mac, so use the manual setup.

**Manual setup.** Run `server/freecad_mcp.py` from your checkout with
FreeCAD's Python. Claude Code:

```bash
claude mcp add freecad -- /Applications/FreeCAD.app/Contents/Resources/bin/python -I "<checkout>/server/freecad_mcp.py"
```

Claude Desktop: add this to
`~/Library/Application Support/Claude/claude_desktop_config.json` and restart
Claude:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/Applications/FreeCAD.app/Contents/Resources/bin/python",
      "args": ["-I", "<checkout>/server/freecad_mcp.py"]
    }
  }
}
```

Without the plugin, add `skills/freecad-design/` as a skill so Claude follows
the design playbook (the `guide` tool serves the same text).

## Tools

| Tool | What it does |
|---|---|
| `freecad_status` | FreeCAD location and version, projects home, GUI, jobs, host, and a `setup_hint` when FreeCAD can't be reached |
| `guide` | The design playbook, one section of it, or a reference page |
| `create_project` | Scaffold `project.json`, `params.json`, `model.py` (suited to the process), `requirements.md`, `checks.json` |
| `list_projects` | Projects and loose `.FCStd` files under the home |
| `read_file` / `write_file` / `edit_file` | Text files, every overwrite backed up; `write_file(encoding="base64")` copies vendor STEP/STL/PDF/images into `refs/` |
| `build` | Run `model.py` → `build/<name>.FCStd` plus the build report (features, holes, sketches, mass, Δ, `checks.json` ✓/✗) and a render |
| `run_script` | FreeCAD Python against any document; `save="revision"` never touches the original |
| `inspect` | Summary, geometry index, detected holes, sketches, parameters and edits made in the FreeCAD GUI (FCStd, STEP, IGES, BREP, STL, OBJ) |
| `render` | Labelled grid of views; shaded, x-ray or drawing style; sections; highlighting |
| `measure` | Distances, angles, coaxiality and radial gaps, hole spacing, wall thickness, pattern alignment, frames |
| `check_fit` | Interference, contact and clearance for every pair of parts |
| `section` | 2D cross-section: areas, extents, in-plane gaps or overlaps, with a hatched plot |
| `lookup_standard` | ISO clearance holes, threads, fasteners, inserts, ISO 286 fits, ISO 2768 tolerances, bearings, process rules |
| `analyze` | FEA of one part or bonded parts (gmsh + CalculiX, bundled with FreeCAD): stress, safety factor, deflection, natural frequencies, buckling |
| `check_manufacturing` | Per-process checks: print orientation and overhangs, tool access and corner radii, bar stock, plate uniformity |
| `drawing` | ISO part drawings: `create` (views, scale, verified dimensions, callouts, title block → PDF/SVG/DXF), `read`, `edit` |
| `export` | STEP/IGES, 3MF/STL/OBJ, BREP and DXF/SVG profiles, each verified |
| `release` | Build, checks, manufacturability, exports, drawings, BOM and notes → zip; refuses to ship a failing design |
| `job_status` | Collect background work |
| `open_in_gui` | Open a file in the FreeCAD GUI; `install_reload_helper` (with your OK) installs MCP Reload; `viewer: true` opens the live 3D viewer in your browser instead |

Inside `model.py`, `fc` (`server/fclib.py`) provides parameters bound to a
spreadsheet, fully constrained sketch profiles, standard holes, reference
parts with named frames, mates, Assembly workbench joints and turned parts.
Geometry references such as `Bracket.face(+Z)`, `Bracket.holes(d=3.4)[0]`,
`Motor.frame(mount)` or `plane(z=8)` work in `measure`, `check_fit`, `section`
and `checks.json`, in global coordinates.

## Live 3D viewer

The server runs a small web page on `http://127.0.0.1:7787/` (`freecad_status`
and `create_project` give the link, and `open_in_gui(viewer=true)` opens it).
Keep it open for the whole design: after every build it reloads the model and
keeps your camera where you left it. With no `?project=` in the address it
follows whichever project was built last.

- Rotate (drag), pan (right-drag), zoom (scroll), view presets matching the
  server's renders (Front looks toward +Y), orthographic or perspective.
- Section along X, Y or Z with a slider; cut faces are capped.
- Show, hide, isolate (double-click) or x-ray each part.
- Click a part for a point's coordinates; shift-click a second point for the
  distance and ΔX/ΔY/ΔZ.
- Revision, build status, `checks.json` results and parameters in the side
  panel. A failed build shows its error; if it made no geometry the last model
  stays on screen.

Each build writes `build/viewer/scene.glb` (FreeCAD's own glTF export, one
node per part) and `scene.json`. The page is plain three.js, loaded from
jsDelivr, so it needs internet access the first time. The server listens
on 127.0.0.1 only and serves nothing but the page and those two files. Any
running freecad-mcp server for the same home can serve the page, so it keeps
working when Claude restarts the server.

## Live view in FreeCAD

`open_in_gui` with `install_reload_helper: true` (Claude asks you first)
installs the *MCP Reload* addon and macro. FreeCAD then reloads an open
document whenever Claude rebuilds it, keeping the camera. A document with
unsaved edits of yours is never reloaded, and nothing reloads mid-edit. Run
the `MCP_Reload` macro to switch it off or on.

## Configuration

| Env var | Effect |
|---|---|
| `FREECAD_APP` | FreeCAD.app, its `bin/python`, or `freecadcmd` (default: `/Applications/FreeCAD.app`, then other FreeCAD bundles) |
| `FREECAD_PYTHON` | Interpreter `launch.sh` starts the server with (default: FreeCAD's bundled Python) |
| `FREECAD_MCP_HOME` | Projects directory (default `~/CAD`) |
| `FREECAD_MCP_ROOTS` | `:`-separated allow-list of directories the server may touch |
| `FREECAD_MCP_VIEWER` | `0` turns the live 3D viewer and its scene export off |
| `FREECAD_MCP_VIEWER_PORT` | Viewer port (default 7787; if another program holds it, a free port is used) |

## Layout

```
.claude-plugin/          plugin.json (with the MCP server) and marketplace.json
commands/                the plugin's slash commands
skills/freecad-design/   the design playbook skill and its reference pages
server/                  the MCP server and the FreeCAD workers
  freecad_mcp.py         MCP server (stdlib only); launch.sh starts it
  worker.py              runs one request inside FreeCAD
  fclib.py               `fc` helper library for model scripts
  analysis.py            measure, check_fit, section, checks
  render.py              headless renderer
  scene.py               live viewer scene: glTF + scene.json per build
  viewer.py              live viewer web server (stdlib only)
  viewer/                the viewer page (three.js)
  manufacture.py         exports, DFM checks, release package
  drawing_worker.py      TechDraw drawings (in the FreeCAD GUI process)
  fea.py                 FEA: gmsh mesh, CalculiX solve, results
  standards.py           ISO tables
freecad-addon/           MCP Reload addon and macro
docs/                    design notes, packaged plugin README, images
examples/                worked examples
evals/                   end-to-end eval cases and harness
scripts/build_plugin.py  builds and validates dist/
tests/                   test suites (real FreeCAD over stdio)
```

## Tests

Each suite drives the real server over stdio with a real FreeCAD. Run one
with, for example:

```bash
python3 tests/test_core_loop.py
```

The drawing suite runs FreeCAD in the background; the reload and packaging
suites open FreeCAD windows for a few seconds.

## Evals

`evals/cases.json` holds end-to-end design tasks, each with fixtures,
automatic checks and a rubric. Set one up, give the brief to an agent, then
grade the workspace:

```bash
python3 evals/run_case.py setup cnc_plate /tmp/eval_cnc
```

```bash
python3 evals/run_case.py brief cnc_plate /tmp/eval_cnc
```

```bash
python3 evals/run_case.py grade cnc_plate /tmp/eval_cnc
```
