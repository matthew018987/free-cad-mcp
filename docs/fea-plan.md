# FEA: fixes, gaps and new features (plan)

Status: **phase 1 in progress** · 2026-11-17 · decisions in §6 · branch `feature/fea-fixes`, from `main` at `50e95a4` · FreeCAD 1.1.3 on Linux (cloud sessions), 1.1.4 on the Mac

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
| 1.1 | Results carry a method version and go stale when it changes | S | planned |
| 1.2 | Benchmarks beyond beams: a hole, a curved face under pressure, a lug | M | planned |
| 1.3 | Brittle materials judged on principal stress | S | planned |
| 1.4 | "Away from the supports" by distance, with a margin set by the part | S | planned |
| 1.5 | Bearing load for pins, screws and shafts in holes | M | planned |
| 1.6 | An unconverged result fails its checks | S | planned |
| 1.7 | Warnings outside the linear range | S | planned |
| 1.8 | Frequency results say what they leave out | S | planned |
| 1.9 | Loads that balance each other are not "out of balance" | S | planned |
| 2.1 | Supports: sliding, symmetry, pinned, bolt-like | M + L | planned |
| 2.2 | Loads: torque, remote force, gravity direction and g-loads, carried mass | M | planned |
| 2.3 | Results: reaction per support, values at a named face or hole, layer strength for prints | M | planned |
| 2.4 | Mesh: local refinement, quality numbers | M | planned |
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
PATH=/opt/freecad/bin:$PATH python3 tests/test_fea.py
```

The server finds `freecadcmd` on `PATH` (`find_freecad`), FreeCAD finds `ccx`
and `gmsh` beside it. That build has numpy and matplotlib but no scipy, so
`fea.py` stays numpy-only. Its gmsh mesh object defaults to curved second-order
elements (`SecondOrderLinear` off) and curvature sizing of 12 elements per
turn, so holes and fillets are not meshed as polygons.

## 2. Phase 1: wrong

"Wrong" means the tool gives, or lets a check pass on, a misleading answer.

### 1.1 Results carry a method version

A result is stale when the geometry (`geom_sig`), the case or the material
(`case_hash`) changes. Nothing ties it to the code that produced it, so after
any fix below an old `result.json` would still count as evidence in `release`.

- `fea.METHOD`, an integer raised whenever the same case on the same geometry
  would give different numbers. It goes into the result and the check: a
  result from another method is stale.
- The result records the CalculiX, gmsh and FreeCAD versions. A result solved
  with another CalculiX or gmsh version is stale too.

### 1.2 Benchmarks beyond beams

`tests/test_fea.py` proves bars with flat faces: a cantilever, a column, two
bonded layers. Real parts fail at holes and fillets. New cases with textbook
answers, in `tests/test_fea_benchmarks.py`:

- **Plate with a hole in tension**: the peak stress at the hole edge against
  the stress concentration factor for a finite-width plate (Heywood:
  Ktn = 2 + (1 - d/W)^3 on the net section).
- **Thick cylinder under internal pressure** (Lamé): hoop stress, von Mises
  and the largest principal stress at the bore, away from the held end. This
  also proves a pressure on a curved face.
- **Pin-loaded lug**: for the bearing load of 1.5.

### 1.3 Brittle materials judged on principal stress

The material table marks resin, acrylic and PMMA as brittle ("treat the yield
as the breaking stress"), but every safety factor is yield / von Mises. Von
Mises is a yield criterion for ductile materials. A brittle material breaks on
its largest principal stress, which von Mises understates where tension acts
in more than one direction (a notch root, a constrained face).

- `fea.py` computes the principal stresses at every node and reports the
  largest tensile and the largest compressive one, with where.
- A material has a `brittle` flag (the table sets it; `material={…, "brittle":
  true}` or `parts.<Part>.brittle` override it). A brittle part is judged on
  its largest principal stress by size, tension or compression, against the
  breaking stress. That is conservative in compression, where brittle
  materials are stronger.
- The report and the checks say which criterion each part was judged on.

### 1.4 "Away from the supports" by distance

A rigid support inflates the stress at its edge, so the result also gives the
peak "away from the supports", and `away_from_supports` checks judge the part
there. Today that zone is each fixed face's bounding box grown by two element
sizes. For a slanted face or a large bore the box takes in material far from
the support and can hide a real peak. The zone also shrinks with a smaller
`mesh_size`, so the judged number depends on the mesh.

- The zone is every node within the margin of a fixed face, by distance to
  the face itself.
- The margin comes from the part, not the mesh: twice the automatic mesh
  size, which is the thinnest wall (at most 2/3 of the part's thinnest
  dimension). `support_margin` in the case overrides it. The result states it.

### 1.5 Bearing load

A `force` on a hole is spread over the whole wall as one traction. Half of it
pulls on the far side, which a pin, screw or shaft cannot do. The contact
pressure comes out four times lower than the usual cosine bearing distribution
(peak 4F / (π d t)), and the material beside the hole carries too little.
Stress and deflection far from the hole are right.

- A new load: `{"ref": "Lug.holes(d=10)", "bearing": 500, "direction": "+X"}`.
  The pressure only pushes, on the side the load presses on, in proportion to
  the cosine of the angle to the load, scaled so the resultant is the force.
  The same rule serves a boss or shaft pushed by a hub.
- A plain `force` across a hole axis still works and adds a note that names
  `bearing`. `fea.md` describes loads from pins, screws and shafts as bearing
  loads.

### 1.6 An unconverged result fails its checks

A check on a quantity that did not converge passed with a note. A result that
was never solved on a second mesh said nothing at all. The release README
printed only the tick and the value.

- A `fea_*` check fails when its quantity did not converge, or when no
  second mesh was solved, unless the check sets `"accept_unconverged": true`.
  An accepted check says so in its value.
- No more skipped convergence checks: when a finer mesh would be too large,
  the second solve uses a coarser mesh (size × 1.5) and the first mesh stays
  the result. Only `refine=false` solves once.
- The release README's verification table carries each check's basis and
  notes.

### 1.7 Warnings outside the linear range

The analysis is linear: small deflections, stress below yield. Outside that
range the numbers are not physical, and nothing said so.

- **Beyond yield**: when the judged stress away from the supports exceeds the
  strength, the result says the part yields (or breaks) and that the
  deflection is larger than shown. A `fea_displacement` check on such a
  result fails.
- **Large deflection**: when the largest displacement exceeds 5 % of the
  part's longest dimension, the result says small-deflection theory no longer
  holds. Checks on it carry the note.

### 1.8 Frequency results say what they leave out

`fea.md` says to keep the first natural frequency above the motor speed. The
analysis has only the part's own mass and rigid supports: the mass of what
the part carries lowers the real frequency, and real supports lower it again.
Until 2.2 adds carried mass, every frequency result and the docs say that the
computed frequency is an upper bound.

### 1.9 Loads that balance each other

The equilibrium line compares the support reactions with the applied
resultant. When the loads balance each other (pressure inside a bore, two
opposing forces) the resultant is zero and the report says "out of balance by
0.0 %". The balance is now measured against the size of the loads, not their
resultant.

Every saved case is re-solved after this phase (1.1), and a check that passed
on an unconverged or a support-edge stress may now fail.

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

Local refinement at small fillets and holes, and mesh quality numbers (worst
element, count of poor elements) in the result.

### 2.5 NAFEMS benchmarks

LE1 (elliptic membrane) and LE10 (thick plate) need symmetry supports (2.1)
and a stress at a point (2.3). They join the benchmark suite here.

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
