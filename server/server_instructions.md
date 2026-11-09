<!-- Sent as the MCP `instructions` field on initialize. Keep it under ~15 lines. -->
FreeCAD design server. Units mm and degrees; Z up. Code in, evidence out:

1. At the start of a design task, call `guide` once (unless the freecad-design
   skill is already loaded). Then `freecad_status`, and `create_project` or
   `inspect` the user's file.
2. Put every driving dimension in params.json with its source. Take hole,
   thread, fastener and fit dimensions from `lookup_standard`, never from
   memory.
3. Edit model.py one feature group at a time and `build` after each. Read the
   whole build report: a failed feature can be invisible in renders because
   FreeCAD keeps the last good shape.
4. Never claim fit, clearance or alignment from an image. Use `check_fit`,
   `section` and `measure`, quote the numbers, and record each requirement in
   checks.json so every build re-checks it.
5. Change existing user files only through `run_script(save="revision")`.
   Never overwrite them. If a build is refused because the user edited it in
   the FreeCAD GUI, adopt the parameter edits (`adopt_gui_params`) and port
   the structural ones into model.py.
6. Before anything is "done": `analyze` load-bearing parts (safety factor,
   deflection), `check_manufacturing` for each part's process,
   review the `drawing` preview, then `release` to produce the verified
   supplier package (exports, drawings, BOM, notes, zip). Never use an
   `allow_*` override or overwrite hand-edited drawings without the user's OK.
7. Keep the user posted with short updates: what changed, check status, one
   image.
