---
description: Verify the current design end to end — build, checks, fits, alignment, manufacturability
argument-hint: [project name]
---

Use the freecad-design skill to verify the project $ARGUMENTS (the current one if none is given):

1. `build` and read the whole report — every feature, sketch and `checks.json` check.
2. `check_fit` across all parts; `section` through each interface (hole and shaft axes, mating faces); `measure` mating hole patterns and axes.
3. `check_manufacturing` for each part's process.
4. Report a table: requirement → evidence → ✓/✗, then anything I need to decide. Don't change the design unless I ask.
