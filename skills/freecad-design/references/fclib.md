# fclib reference

Generated from `server/fclib.py` — the `fc` module available inside `model.py` and `run_script`.
Also predefined there: `App` / `FreeCAD`, `Part`, `Sketcher`, `Vector`, `Placement`, `Rotation`, `doc`.
Coordinates are global millimetres; 2D points on a plane or face are read in that plane's dominant
global axes ((x, y) on ±Z, (x, z) on ±Y, (y, z) on ±X).

`fc.std` is the standards module (`fc.std.clearance('M3', 'normal')`, `fc.std.fit(8, 'H7/g6')`, `fc.std.counterbore('M4')`, `fc.std.nut_trap('M3')`, `fc.std.heat_set_insert('M3')`).

## Parameters and context

### `fc.params()`

Load params.json (+ build overrides) into a Params spreadsheet and return
attribute access to the values. Numeric values come back as Param.

### `fc.path(rel)`

Resolve a path relative to the project directory. Files read this way
(and by ref_part) are part of the build's inputs: when one changes, the
next build is a new revision — read data files through fc.path.

### `fc.doc()`

The FreeCAD document being built (also available as `doc`).

### `fc.result(**kw)`

Return structured data: from run_script it is shown in the tool result,
from model.py on a 'result' line of the build report (and in report.json).

### `fc.warn(msg)`

Add a warning to the build report (shown under 'warnings').

## Bodies and sketches

### `fc.body(name)`

A new PartDesign Body labelled `name` — one per manufactured part.

### `fc.sketch(bd, on, *profiles, name=None, offset=0.0)`

A fully constrained sketch in body `bd` on an origin plane ('XY', 'XZ',
'YZ'), a FaceRef from fc.face, or a Placement. Profiles are fc.rect,
fc.rrect, fc.circle, fc.polygon, fc.points.

### `fc.rect(w, h, center=(0, 0))`

Axis-aligned rectangle w x h (global axes of the plane) centred at `center`.

### `fc.rrect(w, h, r, center=(0, 0))`

Rounded rectangle w x h with corner radius r, centred at `center`.

### `fc.circle(d=None, at=(0, 0), r=None)`

Circle of diameter d (or radius r) centred at `at`.

### `fc.polygon(pts)`

Closed polygon through points (each vertex locked in place).

### `fc.points(pts, d=1.0)`

Small circles used only as positions (e.g. hole centres).

## Features

### `fc.pad(bd, sk, length, reverse=False, symmetric=False, name=None)`

Extrude a sketch by `length` (a Param stays bound to the spreadsheet);
reverse flips the direction, symmetric pads both ways.

### `fc.pocket(bd, sk, depth='through', reverse=False, name=None)`

Cut a sketch into the body: depth 'through' or mm.

### `fc.revolve(bd, sk, axis='V_Axis', angle=360.0, name=None)`

PartDesign Revolution of a sketch about one of its own axes
('V_Axis' | 'H_Axis') — the basis of every turned part.

### `fc.fillet(bd, edge_ref, r, name=None)`

Round the edges selected with fc.edges(...) — do fillets last.

### `fc.chamfer(bd, edge_ref, size, name=None)`

Chamfer the edges selected with fc.edges(...) by `size` mm.

## Turned parts

### `fc.turned(bd, sections, axis='Z', start=0.0, name='Turned')`

A turned (lathe) part from its diameter steps, as one fully constrained
half-profile revolved about a global axis ('Z' only for now).

sections: [(diameter, length) or (diameter, length, fit), ...] from the
start face outward, e.g. [(20, 6), (12, 28, "k6"), (8, 14)] → a Ø20×6
collar, a Ø12×28 k6 bearing seat, a Ø8×14 spigot. Fits go on the drawing
with their ISO 286 limits, on the step that carries them. The body records
each step in FcSteps (JSON list of {"d", "z0", "z1", "fit"}; z along the
turning axis in the body's coordinates, the first step starting at
`start`) — the same z as fc.external_thread's z_from. Returns the
Revolution feature.

### `fc.external_thread(obj, size, length, z_from, fit='6g', name=None)`

Record an external thread (cosmetic): spec, length and where it starts,
for the drawing callout and the release notes. The model keeps the major
diameter — model the actual thread only when 3D printing it.

## Holes

### `fc.holes(bd, on, at, spec, depth=None, thread='cosmetic', name=None, reverse=False, offset=0.0, compensation=None)`

Standard holes drilled into `on` (a FaceRef or 'XY'|'XZ'|'YZ') at the
given points. spec examples:
  'M3 tapped'                         threaded 6H, cosmetic unless thread='modelled'
  'M3 clearance normal'               ISO 273 fine | normal | coarse
  'M3 clearance normal + cbore ISO4762'  FreeCAD's counterbore for a socket head
  'M3 clearance + cbore DIN974'       DIN 974-1 counterbore (standards table)
  'M3 clearance + csk ISO10642'       countersink for a flat head
  'M3 insert'                         heat-set insert hole (vendor-typical; blind)
  'Ø8 H7'                             fit bore, nominal Ø8, class recorded for drawings
  'Ø4.2'  ·  'Ø6 + cbore 10x4'        plain / custom
depth: 'through' (default; inserts default to insert length + 1) or mm.
offset moves the sketch plane along its normal — e.g. start a cross hole
from outside a round part: on="XZ", offset=+(radius + 1).
compensation (mm, diametral) enlarges clearance and plain Ø holes and their
counterbore/countersink for printing; None takes project.json
dfm.hole_compensation_mm when the body's process is fdm or sla (a printed
part whose project.json has no such key gets a build warning — set it, or
0 to confirm none). Tapped, insert and fit (H7) holes are never
compensated.

### `fc.nut_trap(bd, on, at, size, depth=None, clearance=0.2, angle=0.0, name=None)`

Hexagonal pockets for ISO 4032 nuts at the given points: across-flats
+ clearance, depth = nut height + clearance unless given. Add the bolt's
clearance hole separately with fc.holes.

## Selecting geometry

### `fc.face(obj, direction, at=None, contains=None, tol=1e-06)`

The planar face of obj facing `direction`. With several candidates the
outermost one wins; `at` selects the plane offset along the direction and
`contains` a point that must lie on the face.

### `fc.edges(obj, kind=None, parallel=None, on=None, r=None, at=None, axis='Z', tol=1e-06)`

Edges of obj, filtered (all filters combine):
  kind      'line' | 'circle'
  parallel  lines along 'X' | 'Y' | 'Z' (or a vector)
  on        a FaceRef (fc.face(...)): edges lying on that face
  r         circles of radius r
  at        edges lying entirely in the plane at coordinate `at` along
            `axis` (default 'Z'): at=34 → edges in the plane z = 34, e.g.
            the end circles of a part turned along Z; axis='X', at=-20 →
            edges in the plane x = -20.
Selections combine with +: fc.edges(b, kind="circle", at=0) +
fc.edges(b, kind="circle", at=34) chamfers both ends in one feature.

## Reference parts, frames and mates

### `fc.ref_part(name, step=None, shape=None, placement=None, frames=None, color=None, bom='context', qty=1, spec=None)`

A bought-out / reference part used for fit and alignment checks and
excluded from mass. Give a vendor file (`step`: STEP, IGES, BREP, STL or
OBJ, path relative to the project) or a Part shape built from primitives;
`frames` names its interfaces.
bom: 'buy' — purchased with this order (qty = total to buy, spec = order
text, e.g. 'NEMA17 42x40 1.8°') · 'context' (default) — customer-owned or
environment, listed as reference only · 'none' — keep-outs and envelopes,
listed nowhere.

### `fc.set_frames(obj, frames)`

Attach named local frames (Placements) to an object.

### `fc.get_frame(obj, name, local=False)`

The named frame of obj (set with set_frames or ref_part(frames=…)) as a
Placement: global by default, in obj's own coordinates with local=True.

### `fc.set_color(obj, rgb)`

Colour obj in renders and build images: rgb = (r, g, b), each 0..1.

### `fc.frame(x=0.0, y=0.0, z=0.0, normal='+Z', xdir=None)`

A Placement at (x, y, z) whose local +Z points along `normal`.

### `fc.frame_on(face_ref, at=None)`

A frame on a face: origin at its centre (or `at`), +Z along the outward
normal. `at` is a 3D point, or a 2D point in the face's dominant global
axes like every fclib 2D point — (x, y) on ±Z faces, (x, z) on ±Y faces,
(y, z) on ±X faces — projected onto the face's plane. Coordinates are
those of the face's feature (global for bodies at the origin, which is how
fclib creates them).

### `fc.mate(part, part_frame, target, target_frame=None, mode='against', offset=0.0, rotate=0.0)`

Place `part` so its named frame meets a target frame.

target: a Placement (global), or an object with target_frame naming one of
its frames. mode 'against' puts the frames face to face (Z axes opposed,
as when a motor's mounting face sits on a plate); 'aligned' makes them
coincide. offset moves along the target's +Z; rotate spins about it (deg).

## Assemblies (FreeCAD Assembly workbench)

### `fc.assembly(name='Assembly')`

An Assembly workbench assembly with its joint group.

### `fc.add(asm, *objs)`

Move parts (bodies, reference parts) into the assembly. A PartDesign
feature (Pad, Pocket …) stands for its Body.

### `fc.ground(asm, obj)`

Fix one part in place; every other part is positioned by joints.

### `fc.joint(asm, kind, a, b, mode='aligned', offset=0.0, angle=0.0, name=None)`

Join two parts by frames. kind: fixed | revolute (about the frames' Z) |
cylindrical | slider (along Z) | ball | distance | parallel | perpendicular |
angle. mode 'against' puts the frames face to face (Z axes opposed, e.g. a
motor face on a plate); 'aligned' makes them coincide. offset shifts b
along a's Z; angle turns b about it (degrees).

Unless b is grounded, joint() PLACES part b where the joint puts it (any
placement given to b before is replaced): FreeCAD's solver accepts
parallel or anti-parallel axes and keeps the solution nearest the current
position, so starting b in place is what makes the result exact. fc.solve
then verifies every joint; the build report says "holds ✓ (b placed by
the joint and verified)" — the solver did not have to move it.

### `fc.solve(asm)`

Solve the assembly, then check each joint actually holds: its two frames
must coincide (fixed/revolute/ball), share an axis (cylindrical) or a
direction (slider). Problems become build warnings.

### `fc.joint_error(j)`

(position error mm, angle error deg) for a joint, measured between its
two frames in global coordinates; None for grounded/unsupported joints.

## Patterns

### `fc.bolt_circle(pcd, n, start=0.0, center=(0, 0))`

n points equally spaced on a circle of diameter pcd, first at `start` deg.

### `fc.grid(nx, ny, px, py, center=(0, 0))`

nx x ny points at pitch px, py, centred on `center`.

### `fc.rect_pattern(w, h, center=(0, 0))`

The four corners of a w x h rectangle (e.g. a square bolt pattern).

## Sheet metal

### `fc.sheet_metal(name, thickness, base, flanges=(), holes=(), radius=None, k_factor=None, placement=None)`

A bent sheet-metal part of one thickness, built from a spec so the folded
solid and its flat pattern come from the same numbers. Returns a
Part::Feature labelled `name` (process "sheet" in project.json) whose FcSheet
(JSON) records the spec, every bend and the flat size; fc.sheet_flat(part)
gives the flat pattern, and export/release write it as the DXF.
  base      the base face in the XY plane: fc.rect(w, h, center), fc.polygon([...])
            or a list of (x, y) points — its OUTSIDE footprint: a flange's outer
            face is flush with the edge it hangs on. The sheet lies on z = 0 … t;
            its top side (+Z) is "up".
  flanges   [{"edge": 1, "length": 25, "angle": 90, "up": True}, …] — flange N is
            "flangeN" (1, 2 … in this order). Keys:
            edge    a base edge: index i runs from point i to point i + 1 (fc.rect:
                    0 = −Y, 1 = +X, 2 = +Y, 3 = −X), or the way it faces ("+X"); or
                    "flangeN" — the far edge of an earlier flange (lips, Z-bends)
            length  outside length: from the outside of its bend (the parent's bottom
                    face for an up bend, its top face for a down bend) to the free edge
                    or to the outside of the next bend — a 90° flange on the base is
                    as tall as the part
            angle   bend angle in degrees (default 90; 0 < angle < 180)
            up      True (default) folds toward the top side, False away from it
            radius  inside bend radius (default `radius`)
            inset   [from the edge's first point, from its last] (mm): a flange
                    narrower than its edge, e.g. a tab (a flange's far edge runs
                    the same way as the edge that flange hangs on)
            relief  where a bend ends inside an edge, cut a slot this wide (mm;
                    True = thickness) beside it, past the bend, so it does not tear
  holes     [{"on": "base" | "flange1" …, "at": [(a, b), …], "spec": "M4 clearance
            normal" | "Ø5"}] — clearance or plain holes through the sheet; points in
            that face's 2D coordinates like every fclib 2D point: the dominant global
            axes of its plane in the part's own coordinates ((x, y) on the base, (y, z)
            on a flange facing ±X, (x, z) facing ±Y), or 3D points. A hole must lie on
            its face's flat part, clear of the bends.
  radius    default inside bend radius (default = thickness)
  k_factor  where the neutral axis lies in the bend (default 0.33, air bending)
  placement where the finished part goes (fc.frame(...)); the coordinates above
            are the part's own, before it.
Raises FcError when a hole is off its face, a flange is too short for its bends,
the flanges collide when folded or the flat pattern overlaps itself.

### `fc.sheet_flat(obj)`

The flat pattern of a part made by fc.sheet_metal, rebuilt from its FcSheet:
(a Part.Face at z = 0 — the blank with its holes, seen from the sheet's top side,
lower-left corner at the origin — and the bend lines as Part.Edges in the same
coordinates, in order B1, B2 …). Up bends fold toward the viewer. obj may also be
the FcSheet data (dict or JSON text).

### `fc.sheet_data(obj)`

FcSheet of obj as a dict (None for a part not made by fc.sheet_metal).
