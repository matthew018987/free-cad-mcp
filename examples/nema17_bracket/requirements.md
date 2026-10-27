# nema17_bracket — requirements

Every number carries a source: [file p.N], [user], [standard], or [assumption].

## Function
- Hold a NEMA 17 stepper (17HS4401) with its shaft horizontal, on a flat frame. [user]

## Interfaces
| Interface | Feature | Value | Tolerance | Source |
|---|---|---|---|---|
| Motor flange | square | 42.3 mm | — | [17HS4401 datasheet p1] |
| Motor screws | 4× M3, **31.0 mm square** pattern | 31.0 | ±0.1 | [datasheet p1] — square spacing, not a PCD (PCD would be 43.8) |
| Motor pilot | boss Ø22 × 2 | Ø22 | −0.05 | [datasheet p1] |
| Shaft | Ø5 × 24 | | | [datasheet p1] |
| Frame | 2× M4, 34 mm apart | 34 | ±0.2 | [user] |

## Envelope and keep-outs
- Motor body ≥ 1 mm above the frame surface (z = 0). [user]

## Process and material
- FDM, PETG. Min wall 2 mm. [project.json]

## Acceptance checks (checks.json)
- No interference with the motor; pilot radial gap ≥ 0.2 mm; screw pattern offset ≤ 0.05 mm;
  motor ≥ 1 mm above the frame; bracket ≤ 40 g.

## Assumptions
- Pilot bore Ø22.5 (0.5 diametral clearance for FDM). Wall 5 mm. Gussets 20 × 4.

## Open questions
- None for this example.
