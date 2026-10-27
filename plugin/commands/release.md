---
description: Prepare the verified package to send to a print service, machine shop, turning shop or laser cutter
argument-hint: [project name]
---

Use the freecad-design skill to release the project $ARGUMENTS (the current one if none is given):

1. Confirm each part's process, material, finish and quantity with me if they are not already in `project.json` `parts`.
2. `check_manufacturing`, then `drawing` for machined/cut parts — look at the previews.
3. `release` (drawings "auto": current sheets ship as they are, stale generated ones are regenerated). If it refuses — failing checks, manufacturability, an export that failed verification, an unknown part name in project.json, or a hand-edited drawing the model has moved past — tell me why and how to fix it; never pass an `allow_*` override or `drawings="generate"` over my edits without my explicit OK.
4. Tell me the zip path and exactly what to send to which supplier, with the verification summary.
