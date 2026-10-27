# checks.json reference

`checks.json` is a list of acceptance checks. Every `build` evaluates them and
reports `✓ / ✗` with the measured value, and `release` refuses while any check
fails. Write one check per requirement in `requirements.md`. References use the
same syntax as `measure`: `Bracket`, `Bracket.face(+Z)`, `Bracket.face(+X+Y)`,
`Bracket.holes(d=3.4)`, `Bracket.holes(d=3.4)[0]`, `Motor.frame(mount)`,
`[x,y,z]`, `plane(z=8)`, `axis([x,y,z],[dx,dy,dz])`.

Every check takes `"id"` (unique) and `"type"`, and may take `"note"`.

| type | fields | passes when |
|---|---|---|
| `clearance` | `a`, `b` (objects), `min` (mm, default `project.json` clearance.default_min_mm), `contact_ok` | min distance ≥ `min`; with `contact_ok: true` the parts may touch (a motor face on a plate) and the **nearest other gap** must be ≥ `min` |
| `interference` | `a`, `b`, `max` (mm³, default 0) | overlap volume ≤ `max` (use `max > 0` only for deliberate press fits) |
| `radial_gap` | `a` (holes ref), `b` (object or cylinder), `min` / `max` | smallest **signed** radial gap (a shaft bigger than its hole is negative, ✗) between each hole and the coaxial cylinder inside it, measured only where they overlap along the axis (a head in a counterbore is checked against the counterbore Ø; a head beyond the hole's end is an error — select the shank) |
| `align` | `a`, `b` (hole patterns or axes), `max_offset` (default 0.05), `allow_unmatched` | every hole in `a` pairs with one in `b` within `max_offset`; extra holes in `b` fail unless `allow_unmatched: true` |
| `positions` | `holes` (holes or bosses ref), `points` ([[x,y,z], …], or [[x,y], …] with `axis`), `axis` (e.g. `"Z"`), `tol` (0.05), `allow_extra` | every point has a hole axis within `tol` (any order) and no extra holes unless allowed — use it when there is no mating part to `align` with |
| `coaxial` | `a`, `b` (holes/cylinders/frames, or a hole and a part), `max_offset` (0.01), `max_angle` (0.05°) | axes coincide |
| `parallel` / `perpendicular` | `a`, `b`, `max_angle` (0.05°) | plane↔plane and axis↔axis: the angle. **Axis↔plane** (a hole, cylinder, line edge, `axis()` or a frame's +Z against a planar face or `plane()`): `parallel` = the axis lies parallel to the plane, `perpendicular` = the axis is along the plane's normal |
| `coplanar` | `a`, `b` (planar faces/planes) | parallel and 0 apart |
| `distance` | `a`, `b`, `min` / `max` / `value` + `tol` | measured distance within limits. A hole against another part = wall-to-part gap; a hole against **its own part** = the minimum wall around the hole (edge distance; tapped holes from the thread major Ø); a point against an axis or frame = radial distance; a part against an axis or frame = distance from the axis line; a part against `plane(…)` = its distance from the plane |
| `bound` | `object`, `axis` (x/y/z), `side` (min/max), `min`/`max` | the object's extreme coordinate within limits |
| `min_x` … `max_z` | `object`, `min`/`max` | shorthand for `bound` |
| `bbox` | `object`, `max` / `min` ([x, y, z]) | overall size within limits (both may be given) |
| `mass` | `object`, `min_g` / `max_g` | mass from the part's density (`parts.<Part>.density_g_cm3`, its material, or the project material) |
| `volume` | `object`, `min` / `max` / `value` + `tol` | volume in mm³ |
| `hole_count` | `holes` (ref) or `object`, `count` | number of detected holes matches |
| `hole_diameter` | `holes` (ref) or `object`, `d`, `tol` (0.01), `nominal` | every matched hole's smallest Ø equals `d`; with `nominal: true` the print compensation is subtracted first, so the check states the design Ø (`"d": 5.5` for an M5 clearance hole modelled at Ø5.7) and survives a compensation change. Tapped holes compare at the tap drill |
| `face_count` | `object`, `surface` (plane, cylinder, cone, torus, sphere, any), `radius` + `tol` (0.01), `concave` (true/false), `count` or `min`/`max` | number of **distinct** surfaces (split faces of one cylinder count once): chamfers are cones, fillets and internal corner radii are concave cylinders, an OD is a convex cylinder |
| `valid` | `object` | valid, closed solid |
| `fea_safety` | `case` (an `analyze` case in fea.json), `min` (default 2), `away_from_supports` | yield ÷ max von Mises of the latest result ≥ min |
| `fea_stress` | `case`, `max` (MPa), `away_from_supports` | max von Mises ≤ max |
| `fea_displacement` | `case`, `max` (mm), `part` | max displacement ≤ max |
| `fea_frequency` | `case` (a frequency case), `min` (Hz) | first natural frequency ≥ min |
| `fea_buckling` | `case` (a buckling case), `min` | lowest positive buckling load factor ≥ min |

The `fea_*` checks read the latest `analyze` result for their case; on a
multi-part case the static ones judge every part (each against its own yield)
unless they name a `part`, and a check on the wrong kind of case errors. It counts
only while the part's geometry and the case (and material) are unchanged;
otherwise the check fails as stale and names the case to re-run (`release`
re-solves stale cases itself). See `references/fea.md`.

What counts as a hole: a concave cylinder that wraps at least half a turn,
with any concave cones on the same axis (countersinks, drill points). Fillets,
pocket corner radii and slot ends are not holes — check them with
`face_count`. Coaxial holes in separate walls (clevis lugs) are separate
holes; ↧ depth is the full-diameter depth (a drill-point tip is reported
separately). `holes(d=…)` matches the modelled Ø, but a print-compensated
hole only by its design Ø. `bosses(d=…)` selects convex full-round cylinders
(shafts, pilots, pins) and works wherever an axis does (`coaxial`,
`radial_gap` as `b`, `align`, `positions`).

Related tool options: `check_fit(pair_min={"Screws|Washers": 0.1})` sets one
pair's own minimum (designed hardware gaps); `contact_ok`/`interference_ok`
take pairs as `["Screws", "Bracket"]` or `"Screws|Bracket"`; `section(interference_ok=[…],
contact_ok=[…])` accepts intended overlaps (threads, press fits) and contacts
like `check_fit`, and a cut through a hole axis reports the gap on each side.
`measure` gives the same answer in either order. Frames work in `coaxial`,
`parallel`, `perpendicular` and `align` (their +Z axes). Pair names in
`contact_ok`/`interference_ok` may be a Name or a Label in any case. A
malformed check reports a ✗ error naming the field instead of failing the
build. "Nearest other gap" ignores hole-rim pseudo-gaps along a contact plane.

Examples:

```json
[
  {"id": "motor-seat", "type": "clearance", "a": "Bracket", "b": "Motor", "min": 0.2, "contact_ok": true},
  {"id": "pilot-gap", "type": "radial_gap", "a": "Bracket.holes(d=22.5)", "b": "Motor", "min": 0.2},
  {"id": "screws", "type": "align", "a": "Bracket.holes(d=3.4)", "b": "Motor.holes(d=2.5)", "max_offset": 0.05},
  {"id": "m4-pattern", "type": "positions", "holes": "Plate.holes(d=4.5)", "axis": "Z",
   "points": [[-25, -15], [25, -15], [-25, 15], [25, 15]]},
  {"id": "edge-margin", "type": "distance", "a": "Plate.holes(d=4.5)", "b": "Plate", "min": 4.0},
  {"id": "m5-clearance", "type": "hole_diameter", "holes": "Spacer.holes(axis=Z)", "d": 5.5, "nominal": true},
  {"id": "pilot-centred", "type": "coaxial", "a": "Plate.holes(d=22.5)", "b": "Motor.bosses(d=22)"},
  {"id": "shaft-horizontal", "type": "parallel", "a": "Motor.frame(shaft)", "b": "Extrusion.face(+Z)"},
  {"id": "frame-clear", "type": "min_z", "object": "Motor", "min": 2.0},
  {"id": "corner-r3", "type": "face_count", "object": "Plate", "surface": "cylinder", "radius": 3,
   "concave": true, "count": 4},
  {"id": "chamfers", "type": "face_count", "object": "Spacer", "surface": "cone", "count": 4},
  {"id": "envelope", "type": "bbox", "object": "Bracket", "max": [60, 40, 55]},
  {"id": "mass", "type": "mass", "object": "Bracket", "max_g": 40}
]
```

Never weaken a check to make a release pass. If the requirement itself was
wrong, change `requirements.md` with the user's agreement first, then the
check. If the model is the wrong side of a documented requirement, fix the
model (and say so). `release` lists the checks added, removed or changed
since the previous release in the package README.
