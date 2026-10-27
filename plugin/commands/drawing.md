---
description: Make or update ISO drawings for the project's parts, or annotate an existing drawing
argument-hint: [part name or drawing file] [what to add]
---

Use the freecad-design skill for drawings: $ARGUMENTS

- New or regenerated sheets: `drawing` with action `create`. Options (notes, finish, title fields, paper, scale) are remembered in drawings.json; additions that must survive regeneration go in `ops` with `part` — they are replayed on every regeneration, release included. Look at the preview and report anything a machinist would still need.
- An existing drawing: `drawing` action `read` first (the newest sheet in drawings/ by default), then action `edit` with add/move/remove ops on the newest revision — never regenerate someone's own sheet. Each added dimension is verified against the model. Edits save as the next revision (.r01, .r02 …), and release ships that sheet while it still matches the model.
