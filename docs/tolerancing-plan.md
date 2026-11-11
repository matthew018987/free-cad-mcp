# Tolerancing: GD&T and tolerance stack-up (M6 plan)

Status: **M6.1 built and tested** (every suite passed on the Mac, 2026-11-11), the rest planned; decisions in §10 · branch `feature/tolerancing`, from `main` at `6aec6c5` · FreeCAD 1.1.4

**Goal.** A CNC or turning shop can quote, make and inspect a part from the
release zip alone: a datum reference frame, geometric tolerances on the
functional features, toleranced functional sizes, surface texture where it
matters, a general tolerance and a general surface texture for the rest, and a
ballooned drawing with the matching list of characteristics to inspect. No
tolerance is a guess:
each one traces to a requirement, a fit, the bolted-joint rule or a stack-up, and
stack-ups are re-verified on every build like any other check.

---

## 1. What a shop needs, what exists, what is missing

| Need | Today | Missing |
|---|---|---|
| Toleranced functional sizes | ISO 286 fits on holes (`"Ø8 H7"` → `FcFit`) and turned steps (`FcSteps`, `FcFits`), drawn with their deviations; `add_dimension` ops accept `±0.05` or `+0.1/-0.05` | Toleranced lengths outside a drawing op. A tolerance typed into an op is invisible to analysis, the README and inspection |
| General tolerance | `project.json` `drawing.general_tolerance` (default `ISO 2768-m`) in the notes and title block; `lookup_standard(tolerance=…)` gives ISO 2768-1 bands | General *geometrical* tolerances: ISO 2768-2 (H/K/L, withdrawn in 2021) or ISO 22081, which replaced it |
| Datum reference frame | Hole positions are dimensioned from the outline extremes (`datum_vertex`) | Datums A/B/C and datum feature symbols |
| Geometric tolerances | `checks.json` `parallel`, `perpendicular`, `coaxial`, `coplanar` check the **nominal** model | A way to *specify* flatness, perpendicularity, position, run-out … for the shop; feature control frames; basic dimensions |
| Stack-up | `measure`, `check_fit`, `section`, `radial_gap` give **nominal** gaps | Worst-case and statistical stacks, contribution ranking, allocation, and "does the bolt pattern still assemble at the limits" |
| Cost of tolerances | `check_manufacturing` checks geometry (corner radii, access, depths) | Whether a tolerance suits the process, and which tolerances drive the price |
| Surface texture | One fixed note on cnc/turning sheets: `SURFACE ROUGHNESS Ra 3.2 UNLESS STATED` | Per-surface requirements (bearing seats, sealing faces) with ISO 21920-1 symbols, the general requirement as a setting, and a check that the process reaches it |
| Inspection | The README lists holes, fits, threads | Balloon numbers on the sheet and the matching characteristic list for first-article inspection |
| STEP | Geometry only | Semantic PMI (STEP AP242) stays out of scope: FreeCAD has no Python API for it. The PDF drawing governs, as now |

## 2. Principles

1. **Specify once, with geometry references.** A part's tolerancing scheme
   lives in `tolerances.json`, beside `checks.json` and `fea.json`, written
   with the references `measure` already uses (`Housing.face(-Z)`,
   `Housing.holes(d=22)`, `Shaft.bosses(d=12)`). The drawing tool can apply
   it to any model it draws, including an existing FCStd or STEP. Fits stay
   where they are (hole specs, turned steps) and join the scheme
   automatically.
2. **Numbers before pictures.** Every datum and tolerance resolves against
   the build on every build (✓/✗). A stack-up measures its nominals from the
   model, takes its signs from the geometry, and must **close**: its links add
   up to the gap measured in the assembly.
3. **The analysis drives the drawing.** A stack link never owns a tolerance.
   It points at an entry of the part's scheme, and the drawing dimensions that
   feature pair with that tolerance. What the shop inspects is what the
   analysis assumed.
4. **ISO GPS** (ISO 1101, 5459, 2692, 8015, 14405-1, 286, 2768-1, 21920-1),
   following the ISO decision of 2026-10-25 (`design.md` §12), confirmed for
   M6. ASME Y14.5 stays out of scope.
5. **A stdlib core.** Parsing, validation rules, stack maths and the
   frame/datum SVGs go in a new `server/tolerancing.py`, unit-tested anywhere
   (a cloud container without FreeCAD included), like `standards.py`.
6. **Small surface.** One new tool (`stackup`), two check types, new
   `lookup_standard` keys, four new drawing ops and one `inspect` level.
   Without `tolerances.json` nothing changes: same notes, same drawings, no
   balloons, no sheet goes stale.

## 3. `tolerances.json`

The running example: a CNC housing with a 608 bearing on a shoulder, a cover
whose spigot holds the bearing down, four M5 screws from the cover into the
housing, and an end-play requirement of 0.05 … 0.30 mm. z = 0 is the plane
where cover and housing meet.

```json
{
  "parts": {
    "Housing": {
      "datums": {"A": "Housing.face(-Z)", "B": "Housing.holes(d=22)"},
      "tolerances": [
        {"id": "A-flat", "type": "flatness", "feature": "Housing.face(-Z)", "tol": 0.02},
        {"id": "seat", "type": "size", "feature": "Housing.holes(d=22)", "class": "H7", "envelope": true},
        {"id": "seat-perp", "type": "perpendicularity", "feature": "Housing.holes(d=22)", "tol": 0.02,
         "datums": "A"},
        {"id": "seat-Ra", "type": "surface", "feature": "Housing.holes(d=22)", "ra": 1.6,
         "source": "bearing housing seat, maker's recommendation"},
        {"id": "depth", "type": "size", "between": ["Housing.face(+Z)", "Housing.face(+Z, at=-10.12)"],
         "tol": "±0.035", "source": "stack end-play"},
        {"id": "m5-tapped", "type": "position", "feature": "Housing.holes(d=4.2)", "tol": 0.25, "projected": 6,
         "datums": "A B", "source": "fixed M5 with Cover (bolted-joint rule)"}
      ]
    },
    "Cover": {
      "datums": {"A": "Cover.face(-Z, contains=[25, 0, 0])"},
      "tolerances": [
        {"id": "A-flat", "type": "flatness", "feature": "Cover.datum(A)", "tol": 0.05},
        {"id": "spigot", "type": "size", "between": ["Cover.datum(A)", "Cover.face(-Z)"], "tol": "±0.02",
         "source": "stack end-play"},
        {"id": "m5-clear", "type": "position", "feature": "Cover.holes(d=5.5)", "tol": 0.25, "material": "M",
         "datums": "A"}
      ]
    },
    "Bearing": {
      "tolerances": [
        {"id": "width", "type": "size", "between": ["Bearing.face(-Z)", "Bearing.face(+Z)"], "tol": "0/-0.12",
         "source": "ISO 492 normal class (608)"}
      ]
    }
  },
  "stacks": {
    "end-play": {"direction": "+Z", "gap": ["Bearing.face(+Z)", "Cover.face(-Z)"],
                 "links": ["Bearing.width", "Housing.depth", "Cover.spigot"]}
  }
}
```

| Field | Meaning |
|---|---|
| `datums` | Letter → datum feature: a planar face (datum plane), a hole or boss (datum axis). Letters A … Z without I, O, Q. `Part.datum(A)` then works as a reference everywhere (`measure`, checks, stacks, drawing ops) |
| `type` | `size`, `surface` (texture), or one of the 14 ISO 1101 characteristics: `straightness`, `flatness`, `roundness`, `cylindricity`, `line_profile`, `surface_profile`, `parallelism`, `perpendicularity`, `angularity`, `position`, `coaxiality`, `symmetry`, `circular_runout`, `total_runout` |
| `feature` / `between` | The toleranced feature (face, holes, bosses; a pattern gives the `n×` count), or for a size the two parallel faces it lies between |
| `tol` | Geometric: zone width in mm (the ⌀ is added for axis zones). Size: `±0.05`, `+0.1/0`, `0/-0.12` or limits `10.00/10.05` |
| `class` | ISO 286 class on a feature of size (`H7`, `h9`), from the same tables as `lookup_standard` |
| `datums` | `"A"`, `"A B C"`, `"A B(M)"`, common datum `"A-B"` |
| `material`, `projected`, `envelope`, `all_around` | Ⓜ / Ⓛ, Ⓟ height in mm, Ⓔ on a size, profile all around |
| `ra` / `rz`, `removal`, `lay`, `method` | `surface` entries: the upper limit in µm (ISO 21920-1), whether material removal is required (default yes on cnc/turning parts), the lay symbol (`=`, `⊥`, `X`, `M`, `C`, `R`, `P`) and a process note such as "ground" |
| `source` | Why this value (requirement, fit, bolted-joint rule, stack): shown in the README and the inspection list |

- Ids are unique per part and written `Part.id` from outside it (stack links).
- The general surface texture sits beside the general tolerance:
  `project.json` `drawing.general_surface` (default `Ra 3.2` on cnc/turning
  parts, what today's note says), overridden per part by
  `parts.<Part>.general_surface`.
- Bought-out parts (`fc.ref_part`) can carry size tolerances from their
  datasheet or `lookup_standard(bearing=…)`; stacks use them, drawings don't.
- Nominals always come from the model; the scheme holds only tolerances.
- `tolerances.json` joins `model.py`, `params.json`, `checks.json`,
  `project.json` and `drawings.json` in `inputs_sha1` (worker and server),
  `SNAPSHOT_FILES` and so the release package signature: changing a tolerance
  moves the revision.

## 4. The scheme in the build report

Each entry resolves against the build and is checked. A bad entry is ✗ with
the reason and never stops the build (like a malformed check); `release`
refuses while any entry is ✗. To keep the report short it shows one line per
part plus every problem; `inspect(project, level="tolerances")` lists every
entry resolved.

```
tolerances  (tolerances.json · general ISO 2768-mK)
  Housing  datums A face(-Z) · B Ø22 bore          6 ✓
  Cover    datums A face(-Z) at z 0                3 ✓ · 1 ✗
    ✗ top-par  ∥ 0.05 | C      datum C is not defined on Cover (has A)
  Bearing  bought-out                              1 ✓
```

```
inspect level=tolerances
Housing  datums A face(-Z) 60 × 60 at z −30 · B Ø22 bore, axis Z at (0, 0)
  A-flat     ⏥ 0.02                 face(-Z)
  seat       ⌀22 H7 Ⓔ (+0.021/0)    1 bore
  seat-perp  ⟂ ⌀0.02 | A            Ø22 bore
  seat-Ra    √ Ra 1.6               Ø22 bore                           bearing housing seat
  depth      10.12 ±0.035           face(+Z) ↔ face(+Z, at=-10.12)    stack end-play
  m5-tapped  ⌖ ⌀0.25 Ⓟ6 | A | B     4× M5 tapped                       fixed M5 with Cover
```

Rules (a data table in `tolerancing.py`, so each finding names its rule):
- The characteristic suits the feature: flatness on a plane, cylindricity on
  a cylinder, run-out on a surface of revolution; Ⓜ / Ⓛ only on features of
  size (holes, bosses, slots); ⌀ zones only for axes.
- Datums: form tolerances take none; orientation, run-out, coaxiality and
  symmetry need at least one; run-out needs a datum axis; position without
  datums warns. Every letter is defined on that part and used once per frame.
- Hierarchy on one feature: form ≤ orientation ≤ location (a flatness looser
  than the same face's parallelism is redundant: ⚠).
- A datum feature without a form tolerance of its own: ⚠ advisory.
- An ISO 286 fit under ISO 8015 needs the envelope requirement Ⓔ
  (ISO 14405-1) to act as a fit: ⚠ when `envelope` is missing.
- A position with Ⓜ on standard clearance holes is compared with the
  bolted-joint rule (§5.5).
- Surface texture: a value outside the preferred series (Ra 0.025 … 50 µm)
  warns; a fit surface coarser than the bearing makers' seat recommendation
  (Ra 1.6 for IT7, Ra 0.8 for IT6 and finer: rule) warns.
- Fits recorded by `model.py` (`FcFit`, `FcFits`, `FcSteps`) appear as
  implicit `size` entries, so the report and the inspection list carry them
  too; a `class` in `tolerances.json` that disagrees with the model's fit is ✗.

## 5. Tolerance stack-up

### 5.1 The chain
A 1D stack runs along a `direction` from one gap face to the other. Each
**link** is a dimension on one part between two features (planar faces
perpendicular to the direction, axes, points) and names an entry of that
part's scheme. Inline links `{"part", "from", "to", "tol"}` are allowed while
exploring; saving the stack moves their tolerances into the parts' schemes.

From the model, per link: the nominal along the direction, its **sign** (from
the traversal, so no sign errors) and **continuity** (each link ends on the
plane where the next one starts: the parts touch there). The links are
ordered by continuity from the first gap face. For the whole chain:
**closure**, Σ signed nominals = the gap measured in the model. A chain that
does not close is an error that names where it breaks: "Bearing.face(-Z) at
z −10.120 ≠ Housing.face(+Z, at=-10.2) at z −10.200: something sits between
them, or the bearing is not seated".

Coordinate references (`at=`, `contains=`) move when a size changes. M6.3
adds a `floor` selector for blind bores and counterbores
(`Housing.holes(d=22).floor`), the most common stack link, so chains stay
semantic.

### 5.2 Where each link's tolerance comes from
1. The scheme entry it names: `size` limits or an ISO 286 class; a geometric
   zone (position, profile) contributes ±t/2 about zero.
2. A fit recorded in the model, for diameter links, halved for radial stacks
   (`"radius": true`).
3. Standard tables for bought-out parts (bearing width, ISO 492 normal class).
4. The part's general tolerance (ISO 2768-1 at that length), marked
   `⚠ not specified` so it can't hide.

### 5.3 Methods
- **Worst case**, always: limit arithmetic, asymmetric tolerances included.
- **RSS**: band centres for the mean, σᵢ = Tᵢ / 6 (process at ±3σ, Cp 1;
  `cp` per link or per stack); result mean ± 3σ and the share out of spec
  (normal model, `math.erf`).
- **Monte Carlo**, on request: normal or uniform per link, fixed seed,
  100 000 samples (numpy when present, pure Python otherwise).
- **Contribution**: each link's share of the worst-case range and of the
  variance, i.e. which tolerance to tighten first.

### 5.4 Allocation
With a target band, fixed links (bought-out parts, `"fixed": true`) keep
their tolerance; the others get one ISO 286 grade (equal precision, from
`standards.IT`), the coarsest that meets the band, plus the nominal shift
that centres the mean, stated as the parameter changes to make.

### 5.5 Bolted joints (position at MMC)
- Floating fastener (clearance holes in both parts): T = H − F for each part.
- Fixed fastener (one part tapped or pressed): T₁ + T₂ = H − F, with Ⓟ on
  the tapped holes.
- H = the smallest clearance hole (ISO 273 fine / normal / coarse are H12 /
  H13 / H14, so H = the nominal), F = the largest screw (nominal major Ø,
  conservative).

`lookup_standard(joint="M5", clearance="normal", mount="floating")` →
"Ø5.5 H13 (5.500 … 5.680) · M5: ⌖ ⌀0.5 Ⓜ per part; fixed: ⌀0.5 shared".
Holes left to the general tolerance are converted to the equivalent position
zone (±t on two axes → ⌀2√2·t), so a pattern located by ISO 2768-m ±0.2 shows
as ⌀0.57 > ⌀0.5 ✗.

### 5.6 Checks
```json
[{"id": "end-play", "type": "stackup", "stack": "end-play", "method": "worst_case", "min": 0.05, "max": 0.30},
 {"id": "end-play-rss", "type": "stackup", "stack": "end-play", "method": "rss", "max_out_pct": 0.27},
 {"id": "cover-screws", "type": "joint_fit", "a": "Cover.holes(d=5.5)", "b": "Housing.holes(d=4.2)",
  "screw": "M5", "mount": "fixed"}]
```
They need no solver, so they run on every build, `release` gates on them and
the README lists them under "checks changed since the last release", as for
any check. `joint_fit` also checks that the patterns align nominally.

### 5.7 The `stackup` tool
```
stackup(project, stack | {direction, gap, links}, method?, target?, allocate?, auto?, params?, save?)
```
It rebuilds first (cheap when nothing changed), like `analyze`. `params` are
what-if overrides on a variant; `save` (default true for a named stack)
writes the stack. The first run of the example, before allocation, with the
bore 10.20 deep and the spigot left to the general tolerance:

```
STACKUP end-play  along +Z  r0012                worst case ✗ · RSS ✗  (0.05 … 0.30)
 #  part     link                                   nominal  tolerance  from
 1  Bearing  face(+Z) → face(-Z)                     −7.000  0/−0.12    Bearing.width (ISO 492 normal)
 2  Housing  face(+Z, at=-10.2) → face(+Z)          +10.200  +0.05/0    Housing.depth
 3  Cover    datum(A) → face(-Z)                     −3.000  ±0.1       ISO 2768-m at 3 mm  ⚠ not specified
 gap         0.200 · chain closes ✓ (model gap 0.200)
 worst case  0.100 … 0.470   ✗ above 0.30
 RSS ±3σ     0.166 … 0.404   ✗ ≈ 35 % of assemblies above 0.30 (normal, Cp 1)
 share       WC  Cover 54 % · Bearing 32 % · Housing 14 %     RSS  Cover 70 % · Bearing 25 % · Housing 4 %
 allocate    bearing fixed (0.12 of the 0.25 band) · IT10: Housing.depth ±0.035, Cover.spigot ±0.020 → 0.23 ✓
             (IT11 0.29 ✗) · centre the mean: bore depth 10.20 → 10.12 → worst case 0.065 … 0.295 ✓
```

`auto=true` with only `gap` and `direction` proposes the chain: the shortest
path through the contacts along the direction (the contact pairs `check_fit`
already finds). It lists ambiguous paths instead of guessing.

## 6. Drawings

For a part with a scheme, `drawing create` adds:
- **Datum feature symbols**: a boxed letter with a filled triangle on the
  face's edge-on line, or on the Ø dimension of a datum axis.
- **Feature control frames**: under the size callout for holes, bosses and
  patterns (`4× M5-6H THRU` with `⌖ ⌀0.25 Ⓟ6 | A | B` beneath); with a leader
  to the edge-on face for surfaces, clear of any dimension line (on a
  dimension line it would mean the median plane).
- **Basic dimensions** (`TheoreticalExact`, framed) locating positioned
  features from their datum features, in place of today's outline-extreme
  baselines.
- **Size tolerances** from `size` entries, on the face pair they name,
  verified like every other dimension.
- **Surface texture symbols** (ISO 21920-1) from `surface` entries: the
  graphical symbol (material removal required on machined parts) with its
  requirement, on the face's edge-on line or an extension line, or on the Ø
  dimension of a bore or shaft.
- **Notes**: `TOLERANCING ISO 8015`, the general tolerance (`ISO 2768-mK`)
  and the general surface texture, as a note and as the symbol with the
  exceptions mark beside the title block.
- **Balloons**: a numbered balloon beside every characteristic on the sheet
  (each dimension, callout, frame and surface symbol, and the notes that set
  requirements: material, general tolerance, general surface, finish). The
  numbers are the rows of the inspection list (§7).

**Rendering.** TechDraw has no feature control frame or datum objects (its
wiki suggests balloons as a workaround; to confirm on 1.1.4 in M6.0).
Planned approach: a `TechDraw::DrawViewSymbol` per frame, datum and surface
symbol, its SVG generated by `tolerancing.py` (ISO 7083 proportions; symbols
drawn as paths so no font glyph is needed; text in osifont), leader
included, placed in paper coordinates from the view mapping (model × scale +
offset) and **verified**: the leader tip, triangle or symbol apex must lie on
the target edge within 0.1 mm, or the symbol is removed and reported, as
unverifiable dimensions are today. FreeCAD's own surface-finish command
inserts its symbols as `DrawViewSymbol` too (to confirm in M6.0). Each symbol
stores its entry as `FcGdt` JSON, so `drawing read` can list it and edits
can find it. Balloons are native `TechDraw::DrawViewBalloon` objects
(circular bubble, the number as text), which is what TechDraw has them for.
Fallbacks if the spike rejects symbols: `DrawViewBalloon` (square bubble,
filled-triangle end) for datums, `DrawLeaderLine` + `DrawRichAnno` for
frames.

**Stable balloon numbers.** Shops file inspection reports by balloon number,
so regenerating a sheet must not renumber it. Each part keeps a register,
`drawings.json` `_balloons` (bookkeeping like `_sheets`, not a design
input), mapping a characteristic key (a scheme id, or a generated item's
key such as `overall-width` or `holes:4×M5-6H`) to its number: a
regenerated sheet reuses the numbers, a new characteristic takes the next
free one, and a removed one retires its number (never reused). Option
`balloons` = `"all"` (default for parts with a scheme), `"toleranced"` (only
items tighter than the general tolerance, and the notes) or `"none"`;
parts without a scheme opt in with `balloons="all"`.

**Sheet currency.** `content_parts` gains a `tolerances` key **only for
parts that have a scheme**, so existing sheets keep their hashes; a changed
tolerance or surface requirement makes the sheet stale and `release`
regenerates it.

**Edit ops** for existing drawings (add, never move): `add_datum {letter, a,
view?}`, `add_fcf {a, type, tol, datums?, material?, view?}`, `add_surface
{a, ra | rz, view?}`, `add_dimension {…, basic: true}` and `balloons {}`,
which numbers every characteristic on a sheet the user brings that has no
balloon yet and returns the list.

## 7. Manufacturability, release, playbook

**`check_manufacturing`** (cnc and turning; fdm/sla as warnings) gets
tolerance-capability findings from a process table in `standards.py`
(typical values marked "rule", overridable in `project.json`
`dfm.capability`): "⌖ ⌀0.01 on 4× Ø5.5 is tighter than typical CNC milling
(⌀0.05): jig boring and CMM inspection, expect a price premium", "IT6 bore:
reaming or fine boring", "flatness 0.005: grinding or lapping", "Ra 0.4 on
a milled face: grinding or lapping", plus a count of characteristics tighter
than the general tolerance or finer than the general surface, which is what
quoting systems price.

**`release`**:
- refuses while a scheme entry is ✗, a `stackup` or `joint_fit` check
  fails, or a sheet lacks an item of its part's scheme;
- README per part: the datums in words ("A = bottom face, B = Ø22 bore"),
  the frames and toleranced sizes, and every stack with its method and result
  as evidence;
- `<Part>_inspection.csv` in each part's folder, one row per balloon: the
  number, the characteristic, the requirement as drawn, nominal, upper and
  lower limit (a general-tolerance dimension gets its ISO 2768 limits, a
  frame its zone, a surface its Ra limit), datums, feature, view and source,
  then empty columns for the shop (measured, gauge, result): the
  first-article list it fills in. UTF-8 with a byte-order mark so
  spreadsheet apps show the symbols; every symbol is also spelled out
  ("position").

**Playbook** (`SKILL.md`, a new `references/tolerancing.md`, `checks.md`):
- ② Requirements: write functional gaps with their limits (end play,
  clearance at the limits) and turn each into a `stackup` check.
- A new step before ⑧ for cnc and turning parts and any part with fits:
  1. datums from the function: A = what it sits on, B = what locates it (a
     bore, a pin hole, an edge), C = what stops rotation;
  2. fits from `lookup_standard`, with Ⓔ;
  3. bolt patterns from the bolted-joint rule, one `joint_fit` check per
     bolted joint;
  4. a stack for every functional gap, worst case first, then allocate;
  5. surface texture on the surfaces that do work (bearing seats, seal
     faces, sliding faces), from `lookup_standard(surface=…)`;
  6. everything else stays on the general tolerance and surface: tight
     requirements cost money, so specify only what the function needs;
  7. `check_manufacturing`, and tell the user the cost drivers.
- Definition of done: the scheme is all ✓, the stack checks pass, and the
  sheet's balloons match its inspection list.

## 8. Phases

All work goes on `feature/tolerancing` (from `main` at `6aec6c5`), one
commit series per phase; `main` is untouched until the FreeCAD suites pass
on the Mac.

| Phase | Scope | Runs on | Done when |
|---|---|---|---|
| **M6.0 spike** | TechDraw on FreeCAD 1.1.4: list the `TechDraw::` types (confirm no native frame or datum); `TheoreticalExact` in PDF, SVG and the PNG preview; `DrawViewSymbol` with the frame, datum and surface SVGs `tolerancing.py` generates (anchor and scale conventions, osifont text, export fidelity); numbered `DrawViewBalloon`s made from Python (origin and bubble coordinates, export); time per object; save and reopen | Mac (GUI): `/Applications/FreeCAD.app/Contents/Resources/bin/python docs/prototypes/gdt_prototype.py` (any Python 3 launches it) | Its `findings.json`, `spike.pdf` and `crop_*.png` reviewed and recorded as a table like `design.md` §2.3 |
| **M6.1 foundation** | `server/tolerancing.py`: parsing, characteristic table, validation rules, frame/datum/surface SVG and text, worst case / RSS / Monte Carlo / contribution / allocation, the bolted-joint rule, inspection rows. `standards.py`: ISO 2768-2 H/K/L (marked withdrawn, with the ISO 22081 note), ISO 273 H12/H13/H14, ISO 492 widths, IT ranges to 500 mm, process capability (IT grades, geometric, Ra), the preferred Ra series. `lookup_standard`: `joint`, `gdt` (a reference card per characteristic), `surface`, `tolerance="ISO 2768-mK"` | Anywhere (stdlib) | `tests/test_tolerancing.py`: textbook stacks, asymmetric limits, allocation, the bolted-joint rule, table edges, every validation rule, well-formed SVG with the right compartments |
| **M6.2 scheme in the loop** | Load and resolve `tolerances.json` in the build; the report section; `inspect(level="tolerances")`; `Part.datum(A)`; implicit fits; revision inputs; capability findings in `check_manufacturing`; playbook pages | Mac | Fixture `tests/fixtures/gdtlab/`: every rule triggered once; the revision moves when only `tolerances.json` changes |
| **M6.3 stack-up** | `stackup` tool and worker op, continuity and closure, tolerance sources, `stackup` and `joint_fit` checks, allocation, `save`, the `floor` selector; then `auto` | Mac (maths anywhere) | Fixture `tests/fixtures/stacklab/` (housing, 608 envelope, cover): the numbers in §5.7, broken and non-closing chains, checks pass and fail, the release gate |
| **M6.4 drawings** | Datums, frames, basic dimensions, size tolerances, surface symbols, balloons and their register, notes, `content_parts`, `read`, edit ops, layout room for symbols and balloons | Mac (GUI) | `tests/test_drawings.py`: every scheme item drawn and verified, balloon numbers stable across a regeneration (a new item takes the next number, a removed one retires its own), stale on a tolerance change, sheets without a scheme unchanged |
| **M6.5 release** | README section, the inspection CSV numbered like the balloons, gates | Mac | `tests/test_assembly_release.py` additions: CSV rows = balloons on the shipped sheet |
| **M6.6 evals and docs** | Eval cases `gdt_plate` (CNC adapter plate: datums from the mounting interface, flatness, floating M5 positions, H7 pilot ⟂ A with Ra 1.6, a ballooned sheet and its inspection CSV) and `bearing_stack` (end play by stack-up, tolerances allocated and on the drawings); README tool table; `design.md` status and implementation map | Mac + agents | Both cases pass their graders and rubric |

M6.1 comes first because the spike draws the SVGs it generates; M6.4 waits
for the spike's findings. M6.1 merges on its own, ahead of M6.2: its lookups mention only what the
server does today, and M6.2 brings the `tolerances.json` guidance with the build
support.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Custom symbols misplace, or drop out of the PDF export | Spike first; every symbol verified after placement or removed and reported; balloon and rich-annotation fallbacks |
| Frames, surface symbols and balloons crowd small sheets (pockets already do) | Reserve room in the layout; balloons sit beside their item with no leader where they fit; the reply names what didn't fit and suggests `paper="A3"` or `balloons="toleranced"`, as it does for views |
| Balloon numbers change between revisions and break the shop's records | The per-part register: numbers are reused, new items take the next free number, removed numbers are retired |
| GD&T is easy to get wrong (datum order, Ⓜ on a plane, form with datums) | The validation table with reasons; `lookup_standard(gdt=…)`; the playbook's datums-from-function rule |
| Sign mistakes and missing links in stacks | Signs from geometry; continuity and closure against the model |
| Statistics read as guarantees | Worst case always reported; RSS and Monte Carlo labelled with their assumptions; the README states the method |
| Over-tolerancing raises the price | Capability findings; the tolerance count in the README |
| Schema tokens | One tool with a compact schema; ops and check types documented in the reference page, not in tool descriptions |
| No FreeCAD in cloud sessions | The stdlib core is fully testable there; the FreeCAD suites run on the Mac before each merge |

## 10. Decisions (taken 2026-11-09)

| # | Question | Decision |
|---|---|---|
| 1 | Standard | **ISO GPS.** ASME Y14.5 out of scope |
| 2 | Where the scheme lives | **`tolerances.json`**: one place for drawing, stack-up and inspection, and it works for existing models too (not `fc.datum` / `fc.tolerance` calls in `model.py`) |
| 3 | General geometrical tolerance on CNC and turning parts | **`ISO 2768-mK` on parts with a scheme**, ISO 22081 (a general profile tolerance to the part's datums) as the option. ISO 2768-2 was withdrawn in 2021 in favour of ISO 22081, but "mK" is what most shops expect. Parts without a scheme keep `ISO 2768-m` |
| 4 | Ⓔ on fits | Added automatically on parts with a scheme (ISO 14405-1); existing sheets unchanged (default adopted) |
| 5 | Inspection | **Balloon numbers on the sheet and `<Part>_inspection.csv` in v1** |
| 6 | Statistical default | Checks are worst case unless they say `rss`; RSS at ±3σ, Cp 1; Monte Carlo on request (default adopted) |
| 7 | Scope of v1 | 1D stacks (axial and radial), the bolted-joint rule, the 14 characteristics, single-line frames, **surface texture symbols (ISO 21920-1)**, balloons. Later: composite frames, 2D/3D vector loops, thermal expansion, STEP AP242 PMI, datum flags in the live viewer |

## References

- TechDraw dimensions: the `TheoreticalExact` property frames a basic
  dimension (wiki.freecad.org, TechDraw LengthDimension).
- TechDraw GD&T (wiki.freecad.org, "TechDraw Geometric dimensioning and
  tolerancing", written for 0.20): frames and datums are made with balloons,
  symbols are typed into format labels.
- ISO 2768-2:1989 withdrawn on 2021-02-04, replaced by ISO 22081:2021
  (SIS, Genorma catalogue entries); ISO 2768-1 remains valid.
- ISO 1302:2002 withdrawn, replaced by ISO 21920-1:2021 (published
  2021-12-20; a revision is in draft as ISO/CD 21920-1). The graphical
  symbol shops know is unchanged.
