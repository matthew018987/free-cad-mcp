# Troubleshooting

## Setup (Cowork / Claude Code)
- **`freecad_status` says FreeCAD NOT FOUND, or `host.platform` isn't
  `darwin`.** The MCP server isn't running on the Mac that has FreeCAD.app
  (Cowork may start plugin servers in its VM). `freecad_status` →
  `setup_hint` gives the exact fix. The manual setup: quit Claude (⌘Q) and add
  to `~/Library/Application Support/Claude/claude_desktop_config.json`
  ```json
  {"mcpServers": {"freecad": {"command": "/bin/sh",
    "args": ["<plugin folder>/server/launch.sh"],
    "env": {"FREECAD_MCP_HOME": "/Users/<you>/CAD"}}}}
  ```
  then upload the `freecad-design` skill on its own.
- **FreeCAD isn't in /Applications.** Set `FREECAD_APP` (Claude Code from a
  shell, or the `env` block above) to the FreeCAD.app path.
- **Drawings fail with "drawings need the FreeCAD GUI binary".** FreeCAD.app
  is incomplete or `FREECAD_APP` points at a console-only build.
- **An argument error ("unknown argument …, valid: …").** Arguments are
  checked against each tool's schema; use one of the names listed.

## Build errors
- **`Fillet ✗ "BRep_API: command not done"`.** The radius is too large for
  an adjacent face, or the edges include tangent seams.
  - Reduce the radius, or select fewer edges (`fc.edges(..., kind="line",
    parallel="Z")`).
  - Fillet last.
- **"Body result is NOT the full feature list".** A later feature failed and
  FreeCAD shows the last good shape. Fix the failed feature; don't delete it.
- **`FcError: no planar face of X faces +Z`.** The face selector found
  nothing.
  - `inspect(level="object")` lists faces with normals and offsets.
  - Use `at=` or `contains=`.
- **Sketch under-constrained / conflicting.** Use fclib profiles (fully
  constrained). For hand-made sketches, `inspect(level="sketch")`.
- **Coplanar boolean slivers.** Overlap the bodies by ≥ 0.01 mm and call
  `removeSplitter()`.
- **"hole … removed no material", or a cross hole only goes halfway.** The
  hole drills against its sketch normal. For a cross hole in a round part use
  `offset=+(r + 1)` so it starts outside the part and drills back through it;
  check `on`, `at` and `reverse`.

## Scripting (FreeCAD 1.x details)
- `AttachmentSupport` (not `Support`) and `MapMode = "FlatFace"`.
- Hole `ThreadSize` values look like `"M3x0.5"`. `ThreadClass` defaults to 4G,
  so set 6H (fclib does this for you).
- Call `doc.recompute()` and then check `obj.State` and `obj.getStatusString()`.
- Sketch constraint indices shift when geometry is deleted. Re-query them
  instead of caching.
- Booleans with exactly coplanar faces fail or leave slivers. Overlap by
  ≥ 0.01 mm and use `removeSplitter()`.
- Fillets fail when the radius is ≥ an adjacent face's width. Do fillets last
  and in small groups.
- `App.Rotation(App.Vector(axis), angle_deg)`. Placement order is base, then
  rotation.

## Build warnings
- **"… is fdm but project.json has no dfm.hole_compensation_mm".** Its holes
  are not compensated and will print undersize. Set 0.2 (FDM) / 0.1 (SLA), or
  0 to confirm none; checks written with `holes(d=<design Ø>)` keep working.

## Build refused
- **"was edited outside the build loop"** means the user changed the build
  file in FreeCAD.
  - Parameter edits: `build(adopt_gui_params=true)`.
  - Structural edits: port them into `model.py`, then `force=true`.
  - `inspect(level="changes")` lists them.

## Live view (MCP Reload helper)
- FreeCAD's report view says "MCP Reload: watching N open document(s)" when
  the helper runs. The `MCP_Reload` macro switches it off and on.
- **"… was rebuilt by Claude, but you have unsaved changes here."** Close the
  document **without saving** (or File ▸ Revert) to load the rebuild. Saving
  would overwrite it. A variant says the copy "was already marked modified
  when MCP Reload started" — same fix.
- It never reloads while a document is being edited (task panel, edit mode or
  a dialog open); it waits and reloads afterwards.

## Assemblies
- **"joint … does not hold".** The frames don't meet after solving.
  - Check each frame's normal (`measure(a="Part.frame(name)")`) and the joint
    `mode` (`against` for face-to-face).
  - Ground exactly one part.
- **A part vanished into the assembly as "Pad".** Pass the body, not a
  feature (fclib now maps a feature to its Body).

## Drawings
- **"dimension removed".** TechDraw measured something other than the model,
  so the dimension was dropped rather than shown wrong. Add it with an op if
  it is needed.
- **Crowded or tiny sheets.** Pass `paper` or `scale` (`2`, `"1:2"`,
  `"2:1"`), or limit `views`. These are remembered in `drawings.json`; go back
  with `paper="auto"`, `scale="auto"` or `reset=true`.
- **Additions disappear when the sheet is regenerated.** Put them in
  `create`'s `ops` (with `part`): they are replayed on every regeneration,
  release included. `edit` changes one sheet file.
- **A replayed op removed the wrong dimension.** Remove generated
  dimensions by `match: {view, type, value}`, not by name — names change when
  the model changes. A remove that matches nothing is reported as failed.
- **Adding a line to the NOTES list.** Use `append_note {text}`; `add_note`
  with `align: "left"` places free text by its baseline start.
- **"edit the newest revision".** Edits go on top of the newest `.rNN`; pass
  that file (or `from_older=true` to fork an older one on purpose).
- **A pocket is not dimensioned.** It is cut from a side no view looks at
  (add that view: `views=[…, "bottom"]`), or it is a deep slot/step that reads
  as a leg or rib — dimension it with an `add_dimension` op.
- **The flat-pattern view is missing** (bent sheet): no room — try
  `paper="A3"`; the bend table and "SEE DXF" note are there either way.
- **A section view you don't want** on a turned part: `section="none"`.
- **Never regenerate (`create`) a sheet the user edited by hand.** Use
  `edit`.

## Bent sheet metal (`fc.sheet_metal`)
- **"flat pattern overlaps itself" / "flanges collide when folded".**
  Adjacent flanges at a corner need an `inset` on one of them plus a `relief`.
- **"flange too short for its bends"** — the flat part must survive the
  setbacks (r + t)·tan(θ/2) of the bends at both ends: lengthen it or reduce
  the radius.
- **"hole off its face's flat part"** — holes must sit on a face's flat part,
  clear of the bends; the message gives the axes and the allowed range.
- **"the solid changed after fc.sheet_metal"** — don't cut or fillet a sheet
  part afterwards; put holes in the spec.

## Release refused
- **Failing checks, manufacturability "blocked", or GUI edits.** Fix the
  cause and tell the user. Use the `allow_*` overrides only with the user's
  explicit consent.
- **"not a part label" / unknown keys in project.json.** `parts`, `qty` and
  `processes` keys must be body labels; the message lists the valid ones.
- **An export failed verification.** Read the check (STEP re-import volume,
  mesh watertightness) and fix the model; `allow_failed_exports` only with
  the user's consent.
- **A drawing was edited by hand but the model changed since.** Re-apply the
  edits on a fresh sheet (`drawing create`, then the edits), or
  `drawings="generate"` to ship a fresh sheet without them (ask first).
- **A package `…_rNNNN-2` appeared.** A release with the same revision but
  different inputs already existed (usually `.history` was reset); the old
  one is kept.
