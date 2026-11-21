# FEA: fixes, gaps and new features (plan)

Status: **phase 1 built** · 2026-11-17 · decisions in §6 · branch `feature/fea-fixes`, from `main` at `50e95a4` · tested on Linux with FreeCAD 1.1.3 (cloud session) and on the Mac with FreeCAD 1.1.4, CalculiX 2.23 and gmsh 4.15.0 (benchmarks within 1 % of Linux)

**Goal.** An `analyze` result is evidence a release can rest on. Three phases,
in this order: fix what gives or allows a misleading answer today, fill the
gaps in what the three analyses (static, frequency, buckling) can model, then
add new features, starting with an FEA report in the release package.

M5 (design.md §11) stays the base: gmsh meshes with second-order tetrahedra,
CalculiX solves, `server/fea.py` writes the input and reads the results with
numpy. No other solver or FEA library replaces them: they are the standard
open-source pair, they ship with FreeCAD, and FreeCAD's own FEM workbench is
built on them.

Sizes use the roadmap's scale (S < M < L, with M5 itself an L).

---

## 0. Summary

| # | Item | Size | State |
|---|---|---|---|
| 0 | A Linux test bed: FreeCAD, CalculiX and gmsh from conda-forge | S | done |
| 1.1 | Results carry a method version and go stale when it changes | S | done |
| 1.2 | Benchmarks beyond beams: a hole, a curved face under pressure, a lug | M | done |
| 1.3 | Brittle materials judged on principal stress | S | done |
| 1.4 | "Away from the supports" by distance, with a margin set by the part | S | done |
| 1.5 | Bearing load for pins, screws and shafts in holes | M | done |
| 1.6 | An unconverged result fails its checks | S | done |
| 1.7 | Warnings outside the linear range | S | done |
| 1.8 | Frequency results say what they leave out | S | done |
| 1.9 | Loads that balance each other are not "out of balance" | S | done (found by 1.2) |
| 1.10 | The same mesh on every run, and no element folded over | S | done (found by 1.2) |
| 2.1 | Supports: sliding, symmetry, pinned, bolt-like | M + L | planned |
| 2.2 | Loads: torque, remote force, gravity direction and g-loads, carried mass | M | planned |
| 2.3 | Results: reaction per support, values at a named face or hole, layer strength for prints | M | planned |
| 2.4 | Mesh: local refinement, quality numbers, less scatter in the peak | M | planned |
| 2.5 | NAFEMS benchmarks LE1 and LE10 | S | planned |
| 3.1 | FEA report in the release package | M | planned |
| 3.2 | ParaView export | S | planned |
| 3.3 | Screw check from the support reactions | M | planned |
| 3.4 | On demand: contact and preload, large deflection, creep and fatigue, thermal, shells | L each | not planned |

## 1. Step 0: a place to run the tests

The suites had only run on the Mac. A cloud session has no FreeCAD, so no fix
could be proven there. conda-forge carries FreeCAD, CalculiX and gmsh for
Linux:

```sh
micromamba create -y -p /opt/freecad -c conda-forge freecad=1.1.3 python=3.11 calculix gmsh matplotlib-base
for b in freecadcmd ccx gmsh; do ln -sf /opt/freecad/bin/$b /usr/local/bin/$b; done
python3 tests/test_fea.py
```

The server finds `freecadcmd` on `PATH` (`find_freecad`), and FreeCAD finds
`ccx` and `gmsh` there too. That build has numpy and matplotlib but no scipy,
so `fea.py` stays numpy-only.

At `main`, `tests/test_fea.py` passed on Linux as it does on the Mac. So did
`test_sheet_metal`, `test_tolerancing`, `test_verification` and `test_viewer`.
The other suites fail there for reasons that have nothing to do with FEA: they
look for `FreeCAD.app` bundles, or need the FreeCAD GUI for drawings. Making
them run on Linux is outside this plan.

## 2. Phase 1: wrong

"Wrong" means the tool gave, or let a check pass on, a misleading answer.

### 1.1 Results carry a method version

A result was stale when the geometry (`geom_sig`), the case or the material
(`case_hash`) changed. Nothing tied it to the code that produced it, so after
any fix below an old `result.json` would still have counted as evidence in
`release`.

- `fea.METHOD`, an integer raised whenever the same case on the same geometry
  would give different numbers. It is in every result, and a result from
  another method is stale (`fea.outdated`). This phase is method 2.
- The result records the CalculiX, gmsh and FreeCAD versions, and the report
  ends with a `solver` line. A result solved by another CalculiX or gmsh is
  stale too.

### 1.2 Benchmarks beyond beams

`tests/test_fea.py` proved bars with flat faces. Real parts fail at holes and
fillets. `tests/test_fea_benchmarks.py` adds cases with answers from theory,
solved at the default mesh:

| Case | Quantity | Theory | Result |
|---|---|---|---|
| Strip with a hole, pulled (Ø10 in 50 × 5, 5 kN) | peak stress at the hole | 62.8 MPa (Heywood, Ktn = 2 + (1 - d/W)³) | 61.7 MPa, -1.8 % |
| Thick tube, 10 MPa inside (Ø40 / Ø20) | von Mises at the bore | 23.33 MPa (Lamé) | 23.80 MPa, +2.0 % |
| | largest principal stress, acrylic | 16.67 MPa (Lamé) | 18.24 MPa, +9 %; 17.41 MPa on a 2 mm mesh |
| Lug pulled by a pin (Ø12 in 30 × 6, 1 kN) | peak contact pressure | 17.68 MPa (4F / (π d t)) | 17.66 MPa |
| | stress beside the hole | | 26.2 MPa with `bearing`, 13.6 MPa with `force` |

What the benchmarks showed:

- Curved faces were meshed well: FreeCAD's gmsh object defaults to curved
  second-order elements and to 12 elements per turn.
- Two defects nobody had seen: 1.9 and 1.10.
- The tube's held end stops it widening there. The disturbance overshoots
  Lamé's hoop stress by 4 % some 30 mm along and has died out by 45 mm, so
  the benchmark judges the tube beyond that with `support_margin`.
- A peak is the largest of the nodal values. In a uniform field these scatter
  about the true value, so the peak errs on the safe side: 2 % for von Mises,
  up to 9 % for a principal stress at ν 0.37. Taking mid-side values from
  their corner nodes cut that to 3 % but lowered the peak at the hole by
  3 % too, so the nodal values stay as CalculiX gives them (2.4).
- The strip's peak still rises with the mesh (60.0, 61.7, 63.6 MPa at 2.5,
  1.67 and 1.25 mm): the stress at mid-thickness is a little above the
  plane-stress value the formula gives.

### 1.3 Brittle materials judged on principal stress

The material table marked resin, acrylic and PMMA as brittle ("treat the yield
as the breaking stress"), but every safety factor was yield / von Mises. Von
Mises is a yield criterion for ductile materials. A brittle material breaks on
its largest principal stress, which von Mises understates where tension acts
in more than one direction (a notch root, a held face).

- `fea.py` computes the principal stresses at every node. The `principal`
  line reports the largest tension and the largest compression, with where.
- A material has a `brittle` flag (`standards.BRITTLE` sets it;
  `material={…, "brittle": true}` or `parts.<Part>.brittle` override it). A
  brittle part is judged on its largest principal stress by size, tension or
  compression, against the breaking stress. That is on the safe side in
  compression, where brittle materials are stronger.
- The report and the checks name the criterion each part was judged on.

On the acrylic tube of 1.2 the safety factor is 70 / 18.2 = 3.8, where
von Mises would have said 70 / 23.3 = 3.0. In a notch the difference goes the
other way.

### 1.4 "Away from the supports" by distance

A rigid support inflates the stress at its edge, so the result also gives the
peak "away from the supports", and `away_from_supports` checks judge the part
there. That zone was each fixed face's bounding box grown by two element
sizes. For a slanted face or a large bore the box took in material far from
the support and could hide a real peak. The zone also shrank with a smaller
`mesh_size`, so the judged number moved with the mesh.

- The zone is every node within the margin of a fixed face, by distance to
  the face itself (`fea._sample_faces`, `fea._within`).
- The margin comes from the part, not the mesh: twice the automatic mesh
  size, which is the thinnest wall (at most 2/3 of the part's thinnest
  dimension). `support_margin` in the case overrides it. The result states
  it.

The cantilever's stress away from its support is now 56.1 MPa at a 3.33 mm
and at a 1.5 mm mesh, while the peak at the support edge grows from 60.6 to
68.7 MPa.

### 1.5 Bearing load

A `force` on a hole is spread over the whole wall as one traction. Half of it
pulls on the far side, which a pin, screw or shaft cannot do.

- A new load: `{"ref": "Lug.holes(d=12)", "bearing": 1000, "direction": "+X"}`.
  The pressure only pushes, on the side of the wall the load presses
  against, in proportion to the cosine of the angle to the load, scaled so
  the resultant is the force exactly. The same rule serves a boss pushed by
  a hub.
- A stepped hole takes the load on its smallest diameter; `"d"` in the load
  picks another. The direction must be across the axis.
- A plain `force` across a hole axis still works and adds a note that names
  `bearing`. `fea.md` describes loads from pins, screws and shafts as
  bearing loads.

On the lug of 1.2 the stress beside the hole is 26.2 MPa with the bearing
load and 13.6 MPa with the force: the force gave about half.

### 1.6 An unconverged result fails its checks

A check on a quantity that did not converge passed with a note, and a result
that was never solved on a second mesh said nothing at all.

- A `fea_*` check fails when its quantity did not converge between the two
  meshes, or when the case was solved on one mesh, unless the check sets
  `"accept_unconverged": true`. An accepted check says so in its value.
- No more skipped convergence checks: when a finer mesh would be too large,
  the second solve uses a coarser mesh (size × 1.5) and the first mesh stays
  the result. Only `refine=false` solves once.

The plan also said the release README dropped the note. That was wrong: the
release takes each check's whole line from the build report, so the basis and
any note were always in the README's verification table.

A two-mesh comparison does not catch every singularity. The cantilever's peak
at its held edge changes 1 % between the default meshes and 14 % between a
1.5 and a 1 mm mesh. The note "the peak is at a support" and
`away_from_supports` remain the way to judge such a part.

### 1.7 Warnings outside the linear range

The analysis is linear: small deflections, stress below the strength. Outside
that range the numbers are not physical, and nothing said so.

- **Beyond yield**: when the judged stress away from the supports is above
  the strength, the result says the part yields (or breaks) and that the
  deflection is larger than shown. A `fea_displacement` check on such a
  result fails.
- **Large deflection**: when the largest displacement is over 5 % of the
  parts' longest dimension, the result says a linear analysis is only
  approximate. Checks on it carry the note.

### 1.8 Frequency results say what they leave out

`fea.md` said to keep the first natural frequency above the motor speed. The
analysis has only the part's own mass and rigid supports: the mass of what
the part carries lowers the real frequency, and real supports lower it again.
Until 2.2 adds carried mass, every frequency result and `fea.md` call the
computed frequencies upper bounds.

### 1.9 Loads that balance each other

The equilibrium line compared the support reactions with the applied
resultant. For loads that balance each other (pressure inside a bore, two
opposing forces) the resultant is zero, and the report said "✗ out of balance
by 0.0 %". The balance is now measured against the size of the loads.

### 1.10 The same mesh on every run, and no element folded over

Two defects in the mesh, found by the tube of 1.2:

- gmsh ran on every core and returned a different mesh on each run, so the
  same case gave different numbers each time (the peak at a support edge
  moved by over 1 %). It now runs on one thread, at no cost in time: 14 s
  either way for 300 000 nodes.
- On curved parts a few curved elements could fold over (a Jacobian
  determinant ≤ 0). CalculiX then stopped, and the tool blamed the supports.
  The tube failed this way at 3.33 mm, the size its own refined solve uses.
  gmsh's high-order optimiser now untangles them, any that remain get
  straight edges, the `mesh` line ends with the worst scaled Jacobian, and
  the message names the mesh.

Every saved case is re-solved after this phase (1.1), and a check that passed
on an unconverged stress may now fail.

## 3. Phase 2: incomplete

Things the three analyses should be able to model.

### 2.1 Supports

Every support holds all three directions (`*BOUNDARY FIXED, 1, 3`).

- **Sliding and symmetry**: a flat face held along its normal only.
- **Pinned**: a hole held radially and free to turn about its axis.
- **Bolt-like** (L): a hole held through a load-spreading coupling instead of
  a rigid ring, so the rim peak of 1.4 no longer appears and each bolt has a
  reaction.

### 2.2 Loads

- **Torque** about a hole or face axis (a motor's reaction torque).
- **Remote force**: a force acting at a point off the face (a belt pull at
  the pulley, an overhung load), applied as the equivalent force and moment.
- **Gravity** in any direction and in multiples of g (transport, shock).
- **Carried mass** on a face or hole, for frequency and gravity cases.

### 2.3 Results

- **Reaction per support**, so the load on each screw is known.
- **At a reference**: stress and displacement at a named face or hole, as
  results and as checks (`"at": "Mount.holes(d=22)"`), not only the global
  peak.
- **Layer strength for prints**: with `print_up`, the tension across the
  layers and the shear between them against a reduced strength. Prints lose
  far more strength than stiffness across layers, so this comes before any
  direction-dependent stiffness.

### 2.4 Mesh

- Local refinement at small fillets and holes.
- Mesh quality beyond the scaled Jacobian of 1.10: the shape of the worst
  element, and how many are poor.
- Less scatter in the peak (1.2): a stress recovery that does not lower real
  peaks, for example from the integration points.

### 2.5 NAFEMS benchmarks

LE1 (elliptic membrane) and LE10 (thick plate) need symmetry supports (2.1)
and a stress at a point (2.3). They join the benchmark suite here, with a
handbook value for the lug's stress concentration.

## 4. Phase 3: new features

### 3.1 FEA report in the release package

No single report format is mandatory for general mechanical parts. The layout
follows NAFEMS practice (its Engineering Simulation Quality Management
Standard and reporting guidance) and ASME V&V 10. Sector rules (ASME BPVC
VIII-2 Part 5, EN 1993-1-14, the FKM guideline) apply only when a project
names them.

One report per load case, `fea/<case>.md` with its images, linked from the
release README:

1. Summary: each requirement, its value, margin and verdict.
2. Scope: what was analysed and what was not.
3. Model: revision, geometry fingerprint, simplifications, units.
4. Materials: values, sources, the failure criterion used.
5. Mesh: element type, size, counts, quality, the convergence table.
6. Supports and loads, with a picture of where they act.
7. Solver: CalculiX, gmsh and FreeCAD versions, the method version.
8. Verification: equilibrium, convergence, and a hand calculation the case
   can carry (`"expect": {"max_disp": 0.58, "tol": 0.1, "source": "FL³/3EI"}`).
9. Results: stress and displacement plots, peaks with locations, per part,
   reactions per support.
10. Assessment against `checks.json`.
11. Assumptions, limits and notes.
12. Traceability: input hash, case hash, date.

Markdown with images first. A single PDF can follow.

### 3.2 ParaView export

A `.vtu` file beside each result, so anyone can inspect the full field.

### 3.3 Screw check

Tension and shear on each screw from the support reactions (2.3), against the
fastener tables in `standards.py`.

### 3.4 On demand

Contact and bolt preload, large deflection, creep and fatigue, thermal loads,
shell elements for sheet metal. Each is an L and none is planned until a
project needs it.

## 5. Tests and docs

Every item lands with its tests (`tests/test_fea.py`,
`tests/test_fea_benchmarks.py`), `skills/freecad-design/references/fea.md`,
the `analyze` tool description and design.md §11. Each phase is its own
branch: `feature/fea-fixes`, then one per phase 2 and 3 item or group.

## 6. Decisions

| Date | Decision |
|---|---|
| 2026-11-17 | Order: wrong, then incomplete, then new features |
| 2026-11-17 | Keep gmsh and CalculiX; no other FEA library |
| 2026-11-17 | An unconverged result fails its check (1.6); `accept_unconverged` is the explicit way out |
| 2026-11-17 | The cloud test bed installs FreeCAD from conda-forge |
| 2026-11-17 | The FEA report is Markdown first, in the NAFEMS layout |
| 2026-11-17 | gmsh runs on one thread: the same mesh on every run (1.10) |
| 2026-11-17 | Nodal stresses stay as CalculiX gives them: corner-averaged mid-side values lowered the peak at a hole (1.2) |
