# FreeCAD Design (plugin)

Design mechanical parts with Claude and FreeCAD, then send them off for manufacture. Claude turns a brief, datasheets and reference drawings into a parametric FreeCAD model. It verifies fits, clearances and alignment with measured numbers. It makes ISO drawings with every dimension checked against the model. Finally it produces a release package: verified STEP, 3MF, STL and DXF files, PDF drawings, a bill of materials, and manufacturing notes saying what to send to a print service, machine shop, turning shop or laser cutter.

## Requirements

- macOS with **FreeCAD 1.x** in `/Applications` (set `FREECAD_APP` for another location).
- The MCP server is local. It starts FreeCAD on this computer and needs no network access.
- Projects are kept in `~/CAD` (set `FREECAD_MCP_HOME` to change it).

## Use it

- **Cowork:** Customize ▸ Plugins ▸ Add ▸ Upload plugin, then choose `freecad-design.zip`. Start a task and run `/freecad-design:new-part`, or just describe the part.
- **Claude Code:** `claude --plugin-dir <this folder>`.
- **First run:** ask Claude to run `freecad_status`. It should report `host.platform: darwin` and FreeCAD found.

Commands:
- `/freecad-design:new-part` starts a design.
- `/freecad-design:check` verifies the design.
- `/freecad-design:drawing` makes or edits drawings.
- `/freecad-design:release` builds the supplier package.

**Live view.** With your OK, Claude can install the *MCP Reload* helper into FreeCAD (`open_in_gui` with `install_reload_helper`). After that, documents open in FreeCAD reload whenever Claude rebuilds them. Your camera position is kept, and a document with unsaved edits is never reloaded.

## Data

Everything stays on this computer: models, drawings and exports are written under your projects folder. The plugin sends nothing over the network.

## Evals

`evals/` is a `claude plugin eval` suite generated from `evals_harness/cases.json`. It needs FreeCAD on this Mac, the plugin's real MCP server, and each case's scaffold (which seeds the run's working folder with fixture projects and attachments). Run it from this folder:

```bash
claude plugin eval . --scaffold --allow-real-servers --allow-tools "mcp__plugin_freecad-design_freecad__*"
```

Add `--ablation none` to skip the no-plugin baseline (without the plugin there are no FreeCAD tools), or `--case <name> --runs 1` to try one case. Reports are written to `evals/results/`; rebuilding the plugin keeps them, and they are not included in the zip.

This suite is a smoke test: it checks tool use, key report lines and the files produced, and a judge model scores the rubric. The authoritative grader is the harness, which also measures the geometry through the server:

```bash
python3 evals_harness/run_case.py setup <case> <empty folder>
python3 evals_harness/run_case.py brief <case> <folder>    # instructions for the model under test
python3 evals_harness/run_case.py grade <case> <folder>    # JSON verdict
```

`setup` creates an empty `.mcp_calls.jsonl` in the folder; every tool call made through `evals_harness/mcp_cli.py` is appended to it, so a run that made no calls can be told apart from one whose logging failed. The model under test saves its final reply to `.final_message.md` there (the brief tells it to keep that bookkeeping out of the reply).

To add a case to a folder that already holds your own files, pass `--keep` to `setup`: it refuses, changing nothing, when a file it would write is already there (it names it), records every file it creates with its sha1 in `.eval_keep.json`, and a later `--keep` setup removes only those files that are still unchanged, plus the folders it made once they are empty. The scaffolds use `--keep`.
