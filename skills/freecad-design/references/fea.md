# Strength and stiffness — `analyze`

`analyze` runs a finite element analysis — linear static (default), natural
frequencies or linear buckling — of one part or of several parts bonded
together: gmsh meshes them with second-order tetrahedra and CalculiX solves
them (both ship with FreeCAD). It rebuilds first, so the result is always for
the current model.

## A load case
```
analyze(project="bracket", case="motor_load",
        fixed=["Bracket.holes(d=5.5)"],                       # what is held
        loads=[{"ref": "Bracket.holes(d=22.4)", "force": 25, "direction": "-Z"},
               {"ref": "Bracket.face(+X)", "pressure": 0.05}],  # MPa, pushes on the face
        gravity=False, material=None, mesh_size=None, refine=True)
```
- **Supports** (`fixed`): faces or holes that are held rigidly — bolted holes
  (`Part.holes(d=5.5)`), a clamped or bonded face (`Part.face(-Z)`). Rigid
  supports are stiffer than real bolts and clamps, so results are slightly
  optimistic on deflection and pessimistic on stress right at the support.
- **Loads**: `force` in N with a `direction` (`"-Z"`, `"+X+Y"`, `[x,y,z]`) or
  as a vector `[fx, fy, fz]`, spread over the referenced faces; `pressure` in
  MPa pushing on the face; `gravity=true` adds self weight. Loads from mating
  parts go in as forces on the faces or holes they act on (a motor's weight
  on its pilot bore and screw holes, a belt pull on a pulley seat).
- **Material**: from `project.json` (`parts.<Part>.material`, else the
  project material) via the material table (E, ν, yield — see them first with
  `lookup_standard(material="PETG")`), or
  `material="6061-T6"` / `{"E": 2100, "nu": 0.38, "yield": 50}` (MPa).
  Part-level overrides: `parts.<Part>.youngs_modulus_mpa`, `poisson`,
  `yield_mpa`.
- **Mesh**: a size from the part (at least two elements through its thinnest
  wall, coarsened for large parts); `mesh_size` overrides. The refined solve
  (size/1.5, to show convergence) runs by default only when the first mesh
  has ≤ 50 000 nodes — the reply says when it was skipped; `refine=true`
  forces it (minutes and several GB on big meshes), `refine=false` skips it.
- The case is saved in `fea.json` under its name; `analyze(case="motor_load")`
  re-runs it. Failed cases are not saved; `save=false` keeps an exploratory
  case out of `fea.json`.
- **What-if**: `analyze(case="motor_load", params={"rib_t": 2.5})` builds the
  change as a variant and solves it without touching `params.json`, the saved
  case or its result. When a value wins, write it into `params.json` (with its
  source) and re-run the case.

## Other analyses
- **Natural frequencies** — `analysis="frequency"`, `modes` (default 6): the
  supports as mounted, loads ignored, needs a density (from the material or
  `material={…, "density": g/cm³}`). Reports the first frequencies and how
  mode 1 moves; notes an equal pair (a symmetric section) and 0 Hz rigid-body
  modes (the part is not held). Keep the first frequency well above any
  forcing frequency (motor speed, step rate, vibration source).
- **Buckling** — `analysis="buckling"`, `modes` (default 3): the load factor
  at which the given loads buckle the part ("buckles at 35.4 × the load").
  Negative factors mean the reversed load would buckle it. Linear buckling is
  unconservative (imperfections): keep a factor ≥ 3, and also run a static
  case — stocky parts yield before they buckle.
- **Several parts** — `parts=["Bracket", "Plate"]` and `bonded=[["Bracket.face(-Z)",
  "Plate.face(+Z)"], …]`: the touching faces (or holes/bosses) are glued
  together; supports and loads can be on any part; each part uses its own
  material from `project.json`; results are per part (stress, safety factor
  against its own yield) with the governing part. Every part must be held or
  bonded to a held one. Bonded is glued — stiffer than a real bolted joint and
  no sliding or separation, so bracket it as for supports.

## Reading the result
- **reaction**: the supports must carry the applied load (✓ in equilibrium).
  If not, a load or support is not where you meant.
- **stress**: the largest von Mises stress and where, with the **safety
  factor** against yield. Also the largest stress **away from the supports**:
  a rigid support's edge concentrates stress that a real bolt or clamp
  spreads, so judge the part there.
- **converge**: how much a finer mesh changes each result — displacement
  (should change < 3 %), the peak stress and the stress away from the
  supports, each marked ✓/✗ (frequency: f1 < 2 %; buckling: the factor
  < 3 %). A `fea_*` check only carries a "not converged"
  note for the quantity it checks. A peak stress that keeps rising is a
  **singularity** —
  a sharp internal corner, the edge of a rigid support, or a point-like
  load. Add a fillet (and re-check), or judge the stress a little way off.
- **deflect**: the largest displacement and its direction — compare with
  what the function can tolerate (a motor mount's misalignment, a shelf's
  sag).

## Requirements and checks
Put the requirement in `checks.json` against a saved case:
```json
[{"id": "strength", "type": "fea_safety", "case": "motor_load", "min": 3},
 {"id": "sag", "type": "fea_displacement", "case": "motor_load", "max": 0.2},
 {"id": "stress", "type": "fea_stress", "case": "motor_load", "max": 20, "away_from_supports": true},
 {"id": "no-resonance", "type": "fea_frequency", "case": "modes", "min": 150},
 {"id": "no-buckling", "type": "fea_buckling", "case": "column", "min": 3}]
```
A result counts for the build only while the part's geometry and the case
(and material) are unchanged; otherwise the check fails as **stale** and
names the case to re-run. `release` re-solves stale cases itself.

**Bolted holes as supports**: a hole held as a rigid ring always shows a
peak at its rim that grows with every mesh refinement — a modelling artefact
(a real screw and washer spread the load). Decide up front how the part is
judged and write the check that way: `fea_safety` with `"away_from_supports":
true` judges the part a couple of elements away from the supports, and say
so in the report, with the rim peak. Bearing stress under a screw head in a
plastic part is real — keep a washer in the design and the margin generous.
Bracket between two models when it matters: holes only (conservative) vs the
back face bonded to the wall (stiff bound).

**Print orientation**: prints are weaker across their layers. If the
strongest orientation is not the one `check_manufacturing` picks for
overhang and bed contact, set `project.json` → `parts.<Part>.print_up`
(the model direction that points up on the printer, e.g. `"+Y"`); the check
and the released 3MF/STL follow it.

Safety factors (rules of thumb): metals under static load ≥ 1.5–2;
3D prints ≥ 3 — they are weaker across layers (often half), creep under
constant load and vary with print settings; brittle materials (resin,
acrylic) ≥ 3 against the breaking stress. Say which factor you used and why.

## Limits
Linear elastic, small deflections, isotropic material (prints are not);
bonded parts are glued (no contact, sliding, separation or bolt preload);
linear buckling only (no post-buckling or imperfections); no fatigue or
transient dynamics. Results are evidence for a decision —
state the load case and these limits when you report them.
