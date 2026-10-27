# Drawings — `drawing`

## What `create` works out
One ISO sheet per manufactured part (`drawings/<Part>.FCStd`; PDF/SVG/PNG in
`out/drawings/`). Every dimension is measured against the model; any that
don't match are removed and reported.
- **Style and views**: from the process (laser → plate, turning → turned,
  bent sheet → three views) or the shape; first-angle projection plus an iso
  view when there is room (the result says when a view was left out and why).
- **Paper and scale**: the smallest paper (A4 first) that reaches 1:1 or
  better, else the largest standard scale up to A1.
- **Dimensions**: overall size; hole positions (from datum edges, or by
  pitch with `hole_dims="pitch"`); **pockets, slots, steps and cut-outs** —
  size, location from the same datums, depth (a dimension in a side view that
  shows the floor, or a "↧3" callout; `hidden=true` turns callouts into
  dimensions) and corner radii ("4× R3"); identical pockets share one callout
  ("2× 30 × 20 ↧4"). A pocket cut from a side no view looks at gets a ✗ naming
  the view to add (`views=[…, "bottom"]`). Deep slots and steps that look like
  legs or ribs are left to you.
- **Callouts**: hole and thread callouts from the Hole features, fits with
  their ISO 286 limits (upper deviation first).
- **Turned parts**: one Ø per step on that step, fit and thread callouts
  (`M8×1.25-6g`) with thread lengths; with a bore or internal groove the side
  view becomes **SECTION A-A** through the axis (`section="auto"`, the
  default; `"full"` forces it, `"none"` keeps the outside view), each bore
  with its own Ø and depth. TechDraw has no half sections.
- **Bent sheet metal** (`fc.sheet_metal`): thickness, a bend table ("B1 90° UP
  R2 · B2 90° DOWN R2", K-factor), "FLAT PATTERN: SEE DXF (w × h)" and a flat
  pattern view with numbered, dashed bend lines when there is room (else try
  `paper="A3"`).
- **Notes and title block**: material, general tolerance, edge note matching
  the model, finish and print notes; your notes exactly as written; long
  title-block values shortened to fit (identifiers never).

## Options (remembered in `drawings.json`)
Given to `create` they are reused by every later create and by `release`;
without `part` they apply to all parts; `reset=true` clears them; `"auto"`
restores automatic paper/scale/section:
`notes`, `finish`, `material`, `title_fields={creator, drawing_number…}`,
`views`, `scale` (`2`, `"1:2"`, `"2:1"`, `"auto"`), `paper`, `style`,
`hidden=true`, `hole_dims="pitch"`, `section`, and `ops=[…]` with `part` —
extra dimensions and notes added after the generated content and **replayed on
every regeneration**: the way to make additions to a generated sheet last.

## Ops (for `create` and `edit`)
- `add_dimension {a, b, view?, type?, tolerance?, x?, y?, text?}` — ends can be
  holes (`Part.holes(d=3.4)[0]`), `[x,y,z]` points, planar faces
  (`Part.face(+X)`) or view vertices as `read` prints them (`Top.Vertex16`);
  `view` defaults to the one looking along a hole's axis; `type` Distance,
  DistanceX/Y, Radius, Diameter. Each added dimension is verified against the
  true model distance ("verified ✓ 60.000") or removed; a dimension whose ends
  are not parallel to the view (iso) is refused unless you give `text`. It is
  placed outside the outline; `x`/`y` (offset from the view centre, paper mm)
  override.
- `add_hole_callout {a, view?, text?}`, `add_note {text, x, y, size, align}`
  (x, y = text centre in page mm; `align:"left"` = start of the baseline),
  `append_note {text}` (a numbered line in the NOTES block), `set_title
  {fields}`, `move {name, x, y}`, `remove {name}` or `remove {match: {view,
  type, value, text}}` — use `match` in replayed ops (generated names change
  when the model changes); a remove that matches nothing fails.
- An `edit` with any failing op saves nothing and lists every failure;
  `create` ops never abort (failures are reported, and `release` stops on a
  regenerated sheet whose replayed ops failed).

## Existing drawings
1. `drawing(action="read")` first (with only `project`, it opens the newest
   sheet in `drawings/`); it lists views, dimensions with their anchors, notes
   and the title block.
2. **Add** what was asked with `edit` ops. Never move, restyle or remove
   existing items unless told to.
3. Edits save as the next revision (`.r01`, `.r02` …; title block `r0001.1`,
   `.2` …); always edit the newest one (`from_older=true` forks an older one on
   purpose). Never regenerate (`create`) a sheet the user edited by hand.

`release` ships a sheet that still matches the model as it is (edits
included), regenerates a stale generated one with its options and ops, and
stops on a stale sheet edited by hand — see `references/processes.md`.
