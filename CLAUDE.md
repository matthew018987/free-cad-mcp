# Working in this repository

## Git

- Name every branch `feature/<short-description>` in kebab-case, e.g. `feature/live-viewer`.
- Before pushing, rename any local branch that doesn't follow this, including a session's assigned branch: `git branch -m <old> feature/<name>`, then `git push -u origin feature/<name>`. `main` keeps its name.
- Commit messages are one line: short, clear and concise, in the imperative like the existing history ("Add live browser 3D viewer that follows every build"). No body.
- No Claude attribution in commits or pull requests: no `Co-Authored-By`, `Claude-Session` or "Generated with Claude Code" lines.
