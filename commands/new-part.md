---
description: Start a new part or assembly from a brief, datasheets or reference drawings
argument-hint: <what you need — attach datasheets/drawings if you have them>
---

Use the freecad-design skill to start a new design for this request: $ARGUMENTS

1. Call `freecad_status` (check FreeCAD is found and runs on this computer — if not, tell me its setup_hint), then `create_project` with the right process.
2. Intake: read every attachment (copy vendor STEP/PDF files into the project's refs/ with `write_file` encoding base64); write `requirements.md` with a source for every number, `params.json`, and `checks.json`.
3. Before detailed modelling, show me the key dimensions and acceptance checks as a short table and wait for my OK.
