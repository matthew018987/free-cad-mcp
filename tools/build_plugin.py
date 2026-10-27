#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the installable artifacts from this repository (stdlib only):

    python3 tools/build_plugin.py

  freecad_addon/MCP_Reload.FCMacro            generated from mcp_reload.py (single source)
  skills/freecad-design/references/fclib.md   generated from server/fclib.py docstrings
  dist/freecad-design/                        the plugin folder (Claude Code: --plugin-dir)
  dist/freecad-design.zip                     the same, for Cowork / claude.ai "Upload plugin"
  dist/freecad-mcp.mcpb                       Claude Desktop extension (server only)
  dist/freecad-design/evals/                  `claude plugin eval` suite generated from evals/cases.json
  dist/freecad-design/evals_harness/          the eval harness (evals/ as is) — the authoritative grader

and validates the plugin against the documented rules (manifest name, no
top-level bin/, component paths, zip layout, launcher, skill frontmatter, one
version = SERVER_VERSION in server/freecad_mcp.py, every eval file's frontmatter
and grader options). Eval reports in dist/freecad-design/evals/results/ survive
rebuilds and are never zipped. Run the plugin eval suite from dist/freecad-design:

    claude plugin eval . --scaffold --allow-real-servers --allow-tools "mcp__plugin_freecad-design_freecad__*"

It is a smoke test; `python3 evals/run_case.py grade <case> <workspace>` is authoritative.
"""
import ast
import json
import os
import re
import shutil
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
PLUGIN = os.path.join(DIST, "freecad-design")
SERVER_FILES = ["freecad_mcp.py", "worker.py", "fclib.py", "analysis.py", "render.py", "standards.py",
                "manufacture.py", "drawing_worker.py", "drawing_support.py", "fea.py", "launch.sh"]


def read(p):
    with open(os.path.join(ROOT, p)) as fh:
        return fh.read()


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


# ------------------------------------------------------------- generation

def make_macro():
    src = read("freecad_addon/FreeCADMCPReload/mcp_reload.py")
    macro = ("# MCP_Reload.FCMacro — generated from freecad_addon/FreeCADMCPReload/mcp_reload.py; do not edit.\n"
             "# Run it to toggle the watcher: open documents reload when the FreeCAD MCP server rebuilds them.\n\n"
             + src + "\n\n# --- macro entry point\ntoggle()\n")
    write(os.path.join(ROOT, "freecad_addon", "MCP_Reload.FCMacro"), macro)


def _sig(node):
    a = node.args
    parts = []
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(pos, defaults):
        if arg.arg == "self":
            continue
        parts.append(arg.arg + ("=" + ast.unparse(d) if d is not None else ""))
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(arg.arg + ("=" + ast.unparse(d) if d is not None else ""))
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


SECTIONS = [
    ("Parameters and context", ["params", "path", "doc", "result", "warn"]),
    ("Bodies and sketches", ["body", "sketch", "rect", "rrect", "circle", "polygon", "points"]),
    ("Features", ["pad", "pocket", "revolve", "fillet", "chamfer"]),
    ("Turned parts", ["turned", "external_thread"]),
    ("Holes", ["holes", "nut_trap"]),
    ("Selecting geometry", ["face", "edges"]),
    ("Reference parts, frames and mates", ["ref_part", "set_frames", "get_frame", "set_color", "frame", "frame_on",
                                           "mate"]),
    ("Assemblies (FreeCAD Assembly workbench)", ["assembly", "add", "ground", "joint", "solve", "joint_error"]),
    ("Patterns", ["bolt_circle", "grid", "rect_pattern"]),
    ("Sheet metal", ["sheet_metal", "sheet_flat", "sheet_data"]),
]


def make_fclib_reference():
    tree = ast.parse(read("server/fclib.py"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            found[node.name] = ("fc.%s%s" % (node.name, _sig(node)), ast.get_docstring(node) or "")
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_") and node.name not in (
                "FcError", "Param", "Params", "FaceRef", "EdgeRef"):
            init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
            found[node.name] = ("fc.%s%s" % (node.name, _sig(init) if init else "()"), ast.get_docstring(node) or "")
    out = ["# fclib reference", "",
           "Generated from `server/fclib.py` — the `fc` module available inside `model.py` and `run_script`.",
           "Also predefined there: `App` / `FreeCAD`, `Part`, `Sketcher`, `Vector`, `Placement`, `Rotation`, `doc`.",
           "Coordinates are global millimetres; 2D points on a plane or face are read in that plane's dominant",
           "global axes ((x, y) on ±Z, (x, z) on ±Y, (y, z) on ±X).", "",
           "`fc.std` is the standards module (`fc.std.clearance('M3', 'normal')`, `fc.std.fit(8, 'H7/g6')`, "
           "`fc.std.counterbore('M4')`, `fc.std.nut_trap('M3')`, `fc.std.heat_set_insert('M3')`).", ""]
    listed = set()
    for title, names in SECTIONS:
        out += ["## " + title, ""]
        for n in names:
            if n not in found:
                continue
            sig, doc = found[n]
            listed.add(n)
            out += ["### `%s`" % sig, "", doc.strip() or "_(no docstring)_", ""]
    rest = sorted(set(found) - listed)
    if rest:
        out += ["## Other", ""]
        for n in rest:
            sig, doc = found[n]
            out += ["### `%s`" % sig, "", doc.strip() or "_(no docstring)_", ""]
    write(os.path.join(ROOT, "skills", "freecad-design", "references", "fclib.md"), "\n".join(out))
    return len(found)


# --------------------------------------------------------- plugin evals

EVAL_CMD = ('claude plugin eval . --scaffold --allow-real-servers '
            '--allow-tools "mcp__plugin_freecad-design_freecad__*"')
MCP_TOOL = "mcp__plugin_freecad-design_freecad__"
SKILL_MATCH = r'"skill"\s*:\s*"(?:[\w-]+:)?freecad-design"'
WORKSPACE_NOTE = ("(Eval workspace: treat the current working directory as the projects folder. Pass projects and "
                  "files to the FreeCAD tools by absolute path inside it — e.g. project = \"<working directory>/name\""
                  " — and create new projects there with create_project(parent_dir = the working directory). "
                  "Files mentioned as inbox/… are in its inbox/ folder.)")
SCAFFOLD = """#!/bin/sh
# Seeds this run's working directory ($PWD) for the %(id)s eval: the fixture projects and attachments
# the prompt refers to, made by the eval harness shipped in evals_harness/. Runs only with --scaffold.
set -e
CASE_DIR=$(cd "$(dirname "$0")" && pwd)
RC="$CASE_DIR/../../evals_harness/run_case.py"
if [ ! -f "$RC" ]; then
  echo "scaffold: $RC not found - run the suite from the built plugin folder (dist/freecad-design)" >&2
  exit 1
fi
for PY in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Applications/FreeCAD.app/Contents/Resources/bin/python; do
  P=$(command -v "$PY" 2>/dev/null) || continue
  if [ "$(uname)" = Darwin ] && [ "$P" = /usr/bin/python3 ]; then continue; fi  # Apple's stub may pop a dialog
  if "$P" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    exec "$P" "$RC" setup %(id)s "$PWD" --keep
  fi
done
echo "scaffold: no working Python 3.8+ - install Python 3, or FreeCAD 1.x (its bundled python works)" >&2
exit 1
"""


def _yv(v):
    """A YAML value: strings single-quoted (backslashes stay literal — right for regexes)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yv(x) for x in v) + "]"
    return "'%s'" % str(v).replace("'", "''")


def yaml_lines(fields):
    out = []
    for k, v in fields.items():
        if isinstance(v, dict):
            out.append("%s:" % k)
            out += ["  %s: %s" % (k2, _yv(v2)) for k2, v2 in v.items()]
        else:
            out.append("%s: %s" % (k, _yv(v)))
    return out


def frontmatter(fields, body=""):
    return "\n".join(["---"] + yaml_lines(fields) + ["---", ""]) + ("\n" + body.strip() + "\n" if body.strip() else "")


def _rx(v):
    return re.sub(r"([\\.^$*+?{}\[\]|()])", r"\\\1", json.dumps(v))


def _args_match(args):
    parts = ['"%s"\\s*:\\s*%s' % (k, _rx(v)) for k, v in args.items()]
    return parts[0] if len(parts) == 1 else "^" + "".join("(?=.*%s)" % p for p in parts)


CC_TOOLS = {"Skill", "Read", "Glob", "Grep", "NotebookRead", "AskUserQuestion", "Agent", "TodoWrite", "Bash",
            "Write", "Edit", "WebFetch", "WebSearch"}


def _tool(name):
    """cases.json names the server's tools bare ("run_script"); a grader needs the plugin's full MCP name."""
    return name if name in CC_TOOLS or name.startswith("mcp__") else MCP_TOOL + name


def server_tools():
    return set(re.findall(r'\{"name": "(\w+)", "handler"', read("server/freecad_mcp.py")))


def _trace_tool(names=None):
    """Regex for a tool_use of one of our MCP tools in the trace (JSON lines; quotes may be escaped)."""
    q = '\\\\?"'
    return "%sname%s\\s*:\\s*%s%s%s" % (q, q, q, MCP_TOOL, "(?:%s)%s" % ("|".join(names), q) if names else "")


def _check_graders(c):
    """Deterministic graders translated from the harness checks that the trace can show."""
    out = []
    for chk in c["checks"]:
        t, tool = chk["type"], chk.get("tool")
        if t == "tool_called":
            out.append(("calls-%s" % tool, {"type": "tool_used", "tool": MCP_TOOL + tool, "min": chk.get("min", 1)}))
        elif t == "tool_called_any":        # tool_used names one tool: one regex over the trace instead
            out.append(("calls-any-%s" % "-".join(chk["tools"]), {
                "type": "regex", "target": "trace", "pattern": _trace_tool(chk["tools"])}))
        elif t == "no_tool_calls":          # none of our tools, in either arm
            out.append(("no-freecad-tools", {"type": "regex", "target": "trace", "pattern": _trace_tool(),
                                             "match": "not_contains", "arm": "both"}))
        elif t == "tool_called_with":
            out.append(("calls-%s-%s" % (tool, "-".join(chk["args"])), {
                "type": "tool_used", "tool": MCP_TOOL + tool, "input_match": _args_match(chk["args"])}))
        elif t == "tool_not_called_with":
            a = chk["args"]
            pat = _args_match(a) if isinstance(a, dict) else "|".join('"%s"\\s*:\\s*true' % k for k in a)
            what = "-".join("%s-%s" % kv for kv in a.items()) if isinstance(a, dict) else "-".join(a)
            out.append(("never-%s-%s" % (tool, what), {
                "type": "tool_used", "tool": MCP_TOOL + tool, "input_match": pat, "min": 0, "max": 0,
                "arm": "both"}))
    return out


def make_plugin_evals(dest):
    """The `claude plugin eval` suite, generated from evals/cases.json. It needs the
    real server and the scaffolds, so run it from the built plugin folder with

        claude plugin eval . --scaffold --allow-real-servers --allow-tools "mcp__plugin_freecad-design_freecad__*"

    Each run starts in an empty working directory; a case's scaffold.sh seeds it
    with that case's fixture projects and inbox files (evals_harness/run_case.py
    setup <case> "$PWD"), and the prompt tells Claude to use that folder as its
    projects folder (absolute paths, create_project parent_dir), so file graders
    can read what it produced. This suite is a smoke test — tool use, key report
    lines and files, plus judged rubrics; `evals_harness/run_case.py grade` (the
    harness, with geometry checks through the server) is the authoritative grader."""
    cases = json.loads(read("evals/cases.json"))
    gr, gf = cases["generic_rubric"], cases.get("generic_focus") or []
    for c in cases["cases"]:
        d = os.path.join(dest, c["id"])
        pl = c.get("plugin") or {}
        cad = c.get("cad", True)
        fixture = bool(c.get("setup") or c.get("inbox"))
        fm = {"description": c["title"], "tags": ["freecad", "fixture" if fixture else "scratch" if cad else "negative"],
              "runs": 1, "max_turns": pl.get("max_turns", 80), "timeout_seconds": pl.get("timeout_seconds", 1800),
              "allowed_tools": ["Skill", "Read", "Glob", "Grep"]}
        write(os.path.join(d, "prompt.md"), frontmatter(fm, c["prompt"] + ("\n\n" + WORKSPACE_NOTE if cad else "")))
        g = os.path.join(d, "graders")
        skill = {"type": "tool_used", "tool": "Skill", "input_match": SKILL_MATCH}
        if not cad:                          # must not trigger: counted in both arms
            skill.update({"min": 0, "max": 0, "arm": "both"})
        write(os.path.join(g, "skill-used.md" if cad else "skill-not-used.md"), frontmatter(skill))
        for name, spec in _check_graders(c):
            write(os.path.join(g, name + ".md"), frontmatter(spec))
        for spec in pl.get("graders") or []:
            spec = dict(spec)
            name, body = spec.pop("name"), spec.pop("criteria", "")
            if isinstance(spec.get("target"), dict):
                spec["target"] = {"source": "file", "path": spec["target"]["file"]}
            if spec.get("tool"):
                spec["tool"] = _tool(spec["tool"])
            write(os.path.join(g, name + ".md"), frontmatter(spec, body))
        judged = ([(x, "trace") for x in c.get("rubric", [])] + [(x, "last_message") for x in c.get("rubric_final", [])]
                  + [(gr[i], gf[i] if i < len(gf) else "trace") for i in c.get("generic", [0, 1])])
        for i, (crit, focus) in enumerate(judged, 1):
            write(os.path.join(g, "judge-%d.md" % i), frontmatter(
                {"type": "llm", "focus": focus},
                "PASS if: %s\nFAIL if: what you can see shows it was not done, or done differently." % crit))
        if fixture:
            write(os.path.join(d, "scaffold.sh"), SCAFFOLD % {"id": c["id"]})
            os.chmod(os.path.join(d, "scaffold.sh"), 0o755)
            write(os.path.join(d, "case.yaml"), "\n".join(yaml_lines(
                {"schema_version": "1.1", "name": c["id"], "context": {"scaffold_script": "scaffold.sh"}})) + "\n")


# --- a strict reader for the YAML subset written above (validate() parses every generated file)

_PLAIN_BAD = re.compile(r"^[-?:,\[\]{}#&*!|>'\"%@`]|: |\s#|:$")


def _yaml_scalar(s, where):
    s = s.strip()
    if s.startswith("'"):
        if len(s) < 2 or not s.endswith("'") or "'" in s[1:-1].replace("''", ""):
            raise ValueError("%s: bad single-quoted string %s" % (where, s))
        return s[1:-1].replace("''", "'")
    if s.startswith('"'):
        try:
            return json.loads(s)
        except ValueError:
            raise ValueError("%s: bad double-quoted string %s" % (where, s))
    if s in ("true", "false"):
        return s == "true"
    if s in ("", "~", "null"):
        return None
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d+\.\d*$", s):
        return float(s)
    if _PLAIN_BAD.search(s):
        raise ValueError("%s: plain value %r is not valid YAML here — quote it" % (where, s))
    return s


def _yaml_value(s, where):
    s = s.strip()
    if s.startswith("{"):
        raise ValueError("%s: flow mappings are not used here — write a block mapping" % where)
    if not s.startswith("["):
        return _yaml_scalar(s, where)
    if not s.endswith("]"):
        raise ValueError("%s: unterminated list %s" % (where, s))
    items, cur, q, i, inner = [], "", None, 0, s[1:-1]
    while i < len(inner):
        ch = inner[i]
        if q:
            cur += ch
            if q == '"' and ch == "\\":
                cur += inner[i + 1:i + 2]
                i += 1
            elif ch == q and q == "'" and inner[i + 1:i + 2] == "'":
                cur += "'"
                i += 1
            elif ch == q:
                q = None
        elif ch in "'\"":
            q, cur = ch, cur + ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        elif ch in "[]{}":
            raise ValueError("%s: nested collections are not used here: %s" % (where, s))
        else:
            cur += ch
        i += 1
    if q:
        raise ValueError("%s: unterminated quote in %s" % (where, s))
    if cur.strip() or items:
        items.append(cur)
    return [_yaml_scalar(x, where) for x in items]


def parse_frontmatter(text, where):
    """(fields, body) of a `---` frontmatter block: `key: value`, flow lists and one
    level of block mapping. Raises ValueError naming the offending line."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("%s: no --- frontmatter --- block" % where)
    data, cur = {}, None
    for n, line in enumerate(m.group(1).split("\n"), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line[:len(line) - len(line.lstrip())]:
            raise ValueError("%s line %d: tab indentation" % (where, n))
        mm = re.match(r"^( *)([A-Za-z_][\w-]*):(?: +(.*))?$", line)
        if not mm:
            raise ValueError("%s line %d: expected 'key: value', got %r" % (where, n, line))
        ind, key, val = len(mm.group(1)), mm.group(2), mm.group(3)
        at = "%s line %d" % (where, n)
        if ind == 0:
            if key in data:
                raise ValueError("%s: duplicate key %s" % (at, key))
            if val is None or not val.strip():
                data[key], cur = {}, key
            else:
                data[key], cur = _yaml_value(val, at), None
        elif cur is None:
            raise ValueError("%s: unexpected indentation" % at)
        elif val is None or not val.strip():
            raise ValueError("%s: only one level of nesting is used here" % at)
        else:
            data[cur][key] = _yaml_value(val, at)
    return data, m.group(2)


PROMPT_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome", "model",
               "max_turns", "timeout_seconds", "allowed_tools", "append_system_prompt", "env"}
GRADER_KEYS = {"regex": {"pattern", "flags", "match", "target"}, "tool_used": {"tool", "input_match", "min", "max"},
               "tool_order": {"before", "after"}, "file_exists": {"path", "exists"}, "llm": {"criteria", "focus"},
               "baseline": {"baseline_file", "criteria"}}
FOCUS = {"last_message", "trace", "files", "mock_calls"}


def _pattern_problem(p):
    """The pattern must compile, and avoid Python-only syntax JavaScript rejects."""
    if not isinstance(p, str) or not p:
        return "empty pattern"
    if re.search(r"\(\?[aiLmsux-]+[):]|\(\?P<|\\[AZz]", p):
        return "uses Python-only regex syntax (inline flags, (?P<…>), \\A/\\Z) — JavaScript rejects it"
    try:
        re.compile(p)
    except re.error as e:
        return "does not compile: %s" % e
    return None


def validate_evals(evals_dir, case_ids, tools=None):
    """Problems in a generated suite; `tools` (the server's tool names) also checks tool_used names."""
    problems = []
    dirs = sorted(d for d in os.listdir(evals_dir) if d != "results" and os.path.isdir(os.path.join(evals_dir, d)))
    if set(dirs) != set(case_ids):
        problems.append("eval suite cases %s differ from evals/cases.json %s" % (dirs, sorted(case_ids)))
    for cid in dirs:
        cdir = os.path.join(evals_dir, cid)
        rel = "evals/%s/" % cid
        try:
            fm, body = parse_frontmatter(open(os.path.join(cdir, "prompt.md")).read(), rel + "prompt.md")
        except (OSError, ValueError) as e:
            problems.append(str(e))
            continue
        if set(fm) - PROMPT_KEYS:
            problems.append("%sprompt.md: unknown keys %s" % (rel, sorted(set(fm) - PROMPT_KEYS)))
        for k, lo, hi in (("runs", 1, 50), ("max_turns", 1, 200), ("timeout_seconds", 1, 3600)):
            if k in fm and not (isinstance(fm[k], int) and lo <= fm[k] <= hi):
                problems.append("%sprompt.md: %s must be an integer %d–%d" % (rel, k, lo, hi))
        if not isinstance(fm.get("allowed_tools", []), list) or not body.strip():
            problems.append("%sprompt.md: allowed_tools must be a list and the prompt non-empty" % rel)
        if os.path.isfile(os.path.join(cdir, "case.yaml")):
            try:
                cy, _ = parse_frontmatter("---\n%s\n---\n" % open(os.path.join(cdir, "case.yaml")).read().strip(),
                                          rel + "case.yaml")
                sc = (cy.get("context") or {}).get("scaffold_script")
                if cy.get("schema_version") != "1.1" or cy.get("name") != cid:
                    problems.append("%scase.yaml: needs schema_version '1.1' and name %s" % (rel, cid))
                if sc and not os.access(os.path.join(cdir, sc), os.X_OK):
                    problems.append("%scase.yaml: scaffold_script %s missing or not executable" % (rel, sc))
            except (OSError, ValueError) as e:
                problems.append(str(e))
        gdir = os.path.join(cdir, "graders")
        files = sorted(f for f in os.listdir(gdir) if f.endswith(".md")) if os.path.isdir(gdir) else []
        if not files:
            problems.append("%s: no graders (the case would fail to load)" % rel)
        scored = 0
        for f in files:
            where = rel + "graders/" + f
            try:
                g, gbody = parse_frontmatter(open(os.path.join(gdir, f)).read(), where)
            except ValueError as e:
                problems.append(str(e))
                continue
            t = g.get("type")
            if t not in GRADER_KEYS:
                problems.append("%s: unknown grader type %r" % (where, t))
                continue
            extra = set(g) - GRADER_KEYS[t] - {"type", "weight", "arm"}
            if extra:
                problems.append("%s: keys %s are not %s options" % (where, sorted(extra), t))
            if g.get("arm") not in (None, "with-only", "both"):
                problems.append("%s: arm must be with-only or both" % where)
            if t == "tool_used":
                mn, mx = g.get("min", 1), g.get("max")
                if not g.get("tool"):
                    problems.append("%s: tool_used needs tool" % where)
                elif tools is not None and g["tool"] not in CC_TOOLS and g["tool"] not in set(
                        MCP_TOOL + x for x in tools):
                    problems.append("%s: tool %r is neither a Claude Code tool nor %s<one of the server's tools>"
                                    % (where, g["tool"], MCP_TOOL))
                if "max" in g and "min" not in g and mx < 1:
                    problems.append("%s: max: %s without min: 0 can never pass (min defaults to 1)" % (where, mx))
                elif mx is not None and mn > mx:
                    problems.append("%s: min %s > max %s" % (where, mn, mx))
                if g.get("input_match") and _pattern_problem(g["input_match"]):
                    problems.append("%s: input_match %s" % (where, _pattern_problem(g["input_match"])))
                scored += g.get("tool") != "Skill"
            elif t == "regex":
                if _pattern_problem(g.get("pattern")):
                    problems.append("%s: pattern %s" % (where, _pattern_problem(g.get("pattern"))))
                tg = g.get("target", "last_message")
                if not ((isinstance(tg, str) and tg in FOCUS)
                        or (isinstance(tg, dict) and tg.get("source") == "file" and tg.get("path"))):
                    problems.append("%s: bad target %r" % (where, tg))
                if not re.match(r"^[dgimsuyv]*$", str(g.get("flags", ""))):
                    problems.append("%s: flags must be JavaScript regex flags" % where)
                if not (g.get("match") is None or g["match"] == "not_contains" or re.match(r"^count:\d+$", str(g["match"]))):
                    problems.append("%s: match must be not_contains or count:N" % where)
                scored += 1
            elif t in ("file_exists", "tool_order"):
                scored += 1
            elif not (g.get("criteria") or gbody.strip()) or g.get("focus", "last_message") not in FOCUS:
                problems.append("%s: %s grader needs criteria (the body) and a valid focus" % (where, t))
        if files and not scored:
            problems.append("%s: only judge / Skill graders — add a deterministic grader" % rel)
    return problems


# ---------------------------------------------------------------- assemble

RESULTS = os.path.join(PLUGIN, "evals", "results")      # `claude plugin eval .` writes its reports here
RESULTS_KEEP = os.path.join(DIST, ".eval-results")       # kept here while the plugin folder is rebuilt


def server_version():
    """The one version number: SERVER_VERSION in server/freecad_mcp.py (the build stamps it everywhere)."""
    return re.search(r'^SERVER_VERSION = "([^"]+)"', read("server/freecad_mcp.py"), re.M).group(1)


def assemble():
    if os.path.isdir(RESULTS):                            # keep eval reports across rebuilds
        os.makedirs(RESULTS_KEEP, exist_ok=True)
        for n in os.listdir(RESULTS):
            if not os.path.exists(os.path.join(RESULTS_KEEP, n)):
                shutil.move(os.path.join(RESULTS, n), os.path.join(RESULTS_KEEP, n))
    try:
        return _assemble()
    finally:
        if os.path.isdir(RESULTS_KEEP):
            os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
            if os.path.isdir(RESULTS):
                shutil.rmtree(RESULTS)
            shutil.move(RESULTS_KEEP, RESULTS)


def _assemble():
    if os.path.isdir(PLUGIN):
        shutil.rmtree(PLUGIN)
    os.makedirs(os.path.join(PLUGIN, ".claude-plugin"))
    manifest = json.loads(read("plugin/plugin.json"))
    manifest.pop("version", None)                         # plugin/plugin.json carries no version: SERVER_VERSION
    manifest = dict(list(manifest.items())[:2] + [("version", server_version())] + list(manifest.items())[2:])
    write(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(os.path.join(ROOT, "plugin", "mcp.json"), os.path.join(PLUGIN, ".mcp.json"))
    shutil.copytree(os.path.join(ROOT, "plugin", "commands"), os.path.join(PLUGIN, "commands"))
    shutil.copyfile(os.path.join(ROOT, "plugin", "README.md"), os.path.join(PLUGIN, "README.md"))
    shutil.copyfile(os.path.join(ROOT, "LICENSE"), os.path.join(PLUGIN, "LICENSE"))
    shutil.copytree(os.path.join(ROOT, "skills"), os.path.join(PLUGIN, "skills"),
                    ignore=shutil.ignore_patterns(".*"))
    os.makedirs(os.path.join(PLUGIN, "server"))
    for f in SERVER_FILES:
        shutil.copyfile(os.path.join(ROOT, "server", f), os.path.join(PLUGIN, "server", f))
    shutil.copyfile(os.path.join(ROOT, "prompt", "server_instructions.md"),
                    os.path.join(PLUGIN, "server", "server_instructions.md"))
    os.chmod(os.path.join(PLUGIN, "server", "launch.sh"), 0o755)
    shutil.copytree(os.path.join(ROOT, "freecad_addon"), os.path.join(PLUGIN, "freecad_addon"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
    # eval harness (fixtures + runner) and the claude plugin eval suite
    h = os.path.join(PLUGIN, "evals_harness")
    shutil.copytree(os.path.join(ROOT, "evals"), h, ignore=shutil.ignore_patterns("__pycache__", "results", ".*"))
    shutil.copyfile(os.path.join(ROOT, "tests", "mcp_client.py"), os.path.join(h, "mcp_client.py"))
    make_plugin_evals(os.path.join(PLUGIN, "evals"))
    return manifest


def _walk(top):
    """os.walk over the plugin folder without the eval reports (evals/results is never shipped)."""
    for root, dirs, files in os.walk(top):
        dirs[:] = sorted(d for d in dirs if os.path.join(root, d) != RESULTS)
        yield root, dirs, files


def zip_plugin():
    zp = os.path.join(DIST, "freecad-design.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in _walk(PLUGIN):
            for f in sorted(files):
                full = os.path.join(root, f)
                arc = os.path.join("freecad-design", os.path.relpath(full, PLUGIN))
                info = zipfile.ZipInfo.from_file(full, arc)
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as fh:
                    z.writestr(info, fh.read())
    return zp


def make_mcpb(manifest):
    """Claude Desktop extension with the server and the reload helper (no skill:
    install the plugin zip for that). Packed by hand — `mcpb pack` is not needed
    for a bundle without dependencies."""
    mp = os.path.join(DIST, "freecad-mcp.mcpb")
    mf = {
        "manifest_version": "0.3",
        "name": "freecad-mcp",
        "display_name": "FreeCAD MCP",
        "version": manifest["version"],
        "description": "Local FreeCAD server: build, verify, draw and release mechanical parts.",
        "author": manifest["author"],
        "license": "Apache-2.0",
        "server": {
            "type": "binary",
            "entry_point": "server/launch.sh",
            "mcp_config": {"command": "/bin/sh", "args": ["${__dirname}/server/launch.sh"],
                           "env": {"FREECAD_MCP_HOME": "${user_config.projects_dir}"}},
        },
        "user_config": {"projects_dir": {"type": "directory", "title": "Projects folder",
                                         "description": "Where FreeCAD projects are created and read.",
                                         "default": "~/CAD", "required": False}},
        "compatibility": {"platforms": ["darwin", "linux"]},
    }
    with zipfile.ZipFile(mp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(mf, indent=2))
        for sub in ("server", "freecad_addon", "skills"):
            for root, dirs, files in os.walk(os.path.join(PLUGIN, sub)):
                for f in sorted(files):
                    full = os.path.join(root, f)
                    info = zipfile.ZipInfo.from_file(full, os.path.relpath(full, PLUGIN))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(full, "rb") as fh:
                        z.writestr(info, fh.read())
    return mp


# --------------------------------------------------------------- validate

def validate(zp, mp=None):
    problems = []
    m = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
    ver = server_version()
    src = json.loads(read("plugin/plugin.json"))
    if "version" in src and src["version"] != ver:
        problems.append("plugin/plugin.json version %s disagrees with SERVER_VERSION %s in server/freecad_mcp.py — "
                        "drop it from plugin.json (the build stamps SERVER_VERSION)" % (src["version"], ver))
    if m.get("version") != ver:
        problems.append("built plugin.json version %r is not SERVER_VERSION %s" % (m.get("version"), ver))
    if mp:
        with zipfile.ZipFile(mp) as z:
            mv = json.loads(z.read("manifest.json")).get("version")
        if mv != ver:
            problems.append(".mcpb manifest version %r is not SERVER_VERSION %s" % (mv, ver))
    name = m.get("name", "")
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        problems.append("plugin name %r is not kebab-case" % name)
    if re.match(r"^(claude-|anthropic-|anthropics-|cc-plugin-)", name):
        problems.append("plugin name uses a reserved prefix")
    if os.path.isdir(os.path.join(PLUGIN, "bin")):
        problems.append("top-level bin/ makes Cowork/claude.ai refuse the plugin")
    if os.listdir(os.path.join(PLUGIN, ".claude-plugin")) != ["plugin.json"]:
        problems.append(".claude-plugin/ must hold only plugin.json")
    mcp = json.load(open(os.path.join(PLUGIN, ".mcp.json")))
    for sname, cfg in mcp["mcpServers"].items():
        for a in cfg.get("args", []):
            p = a.replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN)
            if "${" in p or not os.path.exists(p):
                problems.append("server %s arg %r does not resolve inside the plugin" % (sname, a))
    if not os.access(os.path.join(PLUGIN, "server", "launch.sh"), os.X_OK):
        problems.append("server/launch.sh is not executable")
    sk = open(os.path.join(PLUGIN, "skills", "freecad-design", "SKILL.md")).read()
    fm = re.match(r"^---\n(.*?)\n---\n", sk, re.S)
    if not fm:
        problems.append("SKILL.md has no frontmatter")
    else:
        meta = dict(l.split(":", 1) for l in fm.group(1).splitlines() if ":" in l)
        if meta.get("name", "").strip() != "freecad-design":
            problems.append("SKILL.md name must match its folder")
        if len(meta.get("description", "").strip()) > 1024:
            problems.append("SKILL.md description is over 1024 characters")
    if sk.count("\n") > 500:
        problems.append("SKILL.md is over 500 lines")
    for ref in re.findall(r"`references/([\w-]+\.md)`", sk):
        if not os.path.isfile(os.path.join(PLUGIN, "skills", "freecad-design", "references", ref)):
            problems.append("SKILL.md mentions missing references/%s" % ref)
    for cmd in os.listdir(os.path.join(PLUGIN, "commands")):
        if not open(os.path.join(PLUGIN, "commands", cmd)).read().startswith("---\ndescription:"):
            problems.append("command %s lacks a description" % cmd)
    words = len(re.sub(r"```.*?```", "", open(os.path.join(PLUGIN, "README.md")).read(), flags=re.S).split())
    if words < 40:
        problems.append("README under 40 words")
    cases, tools = json.loads(read("evals/cases.json"))["cases"], server_tools()
    problems += validate_evals(os.path.join(PLUGIN, "evals"), [c["id"] for c in cases], tools)

    def named(chk):                                      # tools a harness check names (any_of included)
        return ([chk["tool"]] if chk.get("tool") else []) + list(chk.get("tools") or []) + [
            t for sub in chk.get("checks") or [] for t in named(sub)]
    for c in cases:
        for t in sorted(set(t for chk in c["checks"] for t in named(chk)) - tools):
            problems.append("evals/cases.json %s: no server tool %r" % (c["id"], t))
    nfiles = sum(len(f) for _, _, f in _walk(PLUGIN))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in _walk(PLUGIN) for f in fs)
    if nfiles > 5000 or size > 200 * 1024 * 1024:
        problems.append("plugin exceeds 5000 files / 200 MB")
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        tops = set(n.split("/")[0] for n in names)
        if tops != {"freecad-design"} or "freecad-design/.claude-plugin/plugin.json" not in names:
            problems.append("zip layout: plugin.json must be at <single folder>/.claude-plugin/plugin.json")
        info = z.getinfo("freecad-design/server/launch.sh")
        if not (info.external_attr >> 16) & stat.S_IXUSR:
            problems.append("launch.sh lost its executable bit in the zip")
    return problems, nfiles, size


def main():
    make_macro()
    n = make_fclib_reference()
    manifest = assemble()
    zp = zip_plugin()
    mp = make_mcpb(manifest)
    problems, nfiles, size = validate(zp, mp)
    print("fclib reference: %d entries" % n)
    print("plugin  %s  (%d files, %.0f KB)" % (PLUGIN, nfiles, size / 1024))
    print("zip     %s  (%.0f KB)" % (zp, os.path.getsize(zp) / 1024))
    print("mcpb    %s  (%.0f KB)" % (mp, os.path.getsize(mp) / 1024))
    print("version %s (SERVER_VERSION)" % manifest["version"])
    print("evals   %s  — run there: %s" % (os.path.join(PLUGIN, "evals"), EVAL_CMD))
    if problems:
        print("PROBLEMS:\n  " + "\n  ".join(problems))
        return 1
    print("validation: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
