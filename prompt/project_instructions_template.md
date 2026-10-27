<!-- Paste into a Cowork project's instructions and fill in. Only project-specific
     facts belong here; the workflow lives in the freecad-design skill. -->

# CAD project: <name>

- **Use the freecad-design skill and the freecad MCP server for all CAD work.**
  Project folder: `~/CAD/<name>`.
- **Process:** <FDM on Prusa MK4, 0.4 mm nozzle | CNC 3-axis aluminium | laser-cut 3 mm acrylic | …>
- **Material:** <PETG, density 1.27 g/cm³ | 6061-T6 | …>
- **Default clearances:**
  - sliding fit <0.3> mm
  - bolt clearance ISO 273 <normal>
  - FDM hole compensation <+0.2> mm (project.json dfm.hole_compensation_mm; applied automatically)
- **Minimum wall:** <2.0> mm. **Max overhang without support:** <45>°.
- **Fasteners in stock:** <M3 socket head ISO 4762 6/8/10/12 mm; M3 heat-set inserts 4.2 × 5.7 mm; …>
- **Drawings:** ISO first-angle (the only projection the drawing tool makes). **General tolerance:** <ISO 2768-m>.
- **Title block:** company <…>, author <…>, drawing number prefix <…>.
- **Parts and how they're made** (copied into project.json `parts`):
  <Bracket: CNC, 6061-T6, bead blast, qty 2 · Shaft: turning, 303 stainless, qty 4 · Cover: FDM PETG, qty 1>.
- **Suppliers:** <e.g. online CNC/turning service; local print farm; laser shop> — `release` writes what to send each.
- **Ask before:** changing any interface dimension taken from a datasheet,
  changing material or process.
