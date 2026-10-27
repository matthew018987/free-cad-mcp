#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for the generated `claude plugin eval` suite, the
build's version / results / validation rules, the eval harness (mcp_cli, the
scaffolds, the tightened graders) and the new eval cases.

Part 1 checks the build output and the generator / validator without FreeCAD.
Part 2 runs the harness against real FreeCAD: scripted known-good solutions for
the new cases must grade 100 % and known-bad ones must fail the right checks
(the drawing steps run the FreeCAD GUI in the background for a few seconds each).

    python3 tests/test_evals_plugin.py            # both parts
    python3 tests/test_evals_plugin.py --unit     # part 1 only
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from mcp_client import check, fails  # noqa: E402

spec = importlib.util.spec_from_file_location("build_plugin", os.path.join(ROOT, "tools", "build_plugin.py"))
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

DIST = os.path.join(ROOT, "dist", "freecad-design")
EVALS = os.path.join(DIST, "evals")
RC = os.path.join(ROOT, "evals", "run_case.py")
CLI = os.path.join(ROOT, "evals", "mcp_cli.py")
CASES = json.load(open(os.path.join(ROOT, "evals", "cases.json")))["cases"]
BY_ID = dict((c["id"], c) for c in CASES)
SERVER_VERSION = re.search(r'^SERVER_VERSION = "([^"]+)"', open(os.path.join(ROOT, "server", "freecad_mcp.py")).read(),
                           re.M).group(1)
J = json.dumps
JS = {"separators": (",", ":"), "ensure_ascii": False}     # JSON.stringify's spelling


def build():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_plugin.py")], capture_output=True, text=True)
    return r.returncode == 0 and "validation: ok" in r.stdout, r.stdout + r.stderr


def graders(cid):
    gdir = os.path.join(EVALS, cid, "graders")
    return dict((f[:-3], bp.parse_frontmatter(open(os.path.join(gdir, f)).read(), f)[0])
                for f in sorted(os.listdir(gdir)) if f.endswith(".md"))


def js_trace_match(pattern, line):
    """The trace regexes are JavaScript; the subset used here means the same in Python."""
    return re.search(pattern, line) is not None


def plugin_graders(cid, ws):
    """Replay the generated plugin graders that need no judge on a harness workspace: the
    .mcp_calls.jsonl log stands in for the trace and .final_message.md for the last message.
    → names of the graders that would fail."""
    log = [json.loads(l) for l in open(os.path.join(ws, ".mcp_calls.jsonl"))] if os.path.isfile(
        os.path.join(ws, ".mcp_calls.jsonl")) else []
    trace = []
    for x in log:
        trace.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": bp.MCP_TOOL + x["tool"], "input": x["args"]}]}}, **JS))
        trace.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": x["text"]}]}}, **JS))
    trace = "\n".join(trace)
    fm = os.path.join(ws, ".final_message.md")
    last = open(fm).read() if os.path.isfile(fm) else ""
    bad = []
    for name, g in graders(cid).items():
        t = g["type"]
        if t == "file_exists":
            ok = bool(glob.glob(os.path.join(ws, g["path"]))) == g.get("exists", True)
        elif t == "regex":
            tg = g.get("target", "last_message")
            if isinstance(tg, dict):
                p = os.path.join(ws, tg["path"])
                text = open(p, errors="replace").read() if os.path.isfile(p) else ""
            else:
                text = {"trace": trace, "last_message": last}.get(tg, "")
            hit = re.search(g["pattern"], text, re.I if "i" in g.get("flags", "") else 0) is not None
            ok = hit != (g.get("match") == "not_contains")
        elif t == "tool_used" and g["tool"].startswith(bp.MCP_TOOL):
            n = sum(1 for x in log if bp.MCP_TOOL + x["tool"] == g["tool"]
                    and (not g.get("input_match") or re.search(g["input_match"], json.dumps(x["args"], **JS))))
            ok = g.get("min", 1) <= n <= g.get("max", n)
        else:
            continue                                   # judges and the Skill grader need a real run
        if not ok:
            bad.append(name)
    return bad


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixG-"))
try:
    # ------------------------------------------------------------------ part 1: build and generator
    print("build: one version, results kept, strict validation")
    ok, out = build()
    check("build_plugin.py builds and validates", ok, out)
    src = json.load(open(os.path.join(ROOT, "plugin", "plugin.json")))
    built = json.load(open(os.path.join(DIST, ".claude-plugin", "plugin.json")))
    with zipfile.ZipFile(os.path.join(ROOT, "dist", "freecad-mcp.mcpb")) as z:
        mf = json.loads(z.read("manifest.json"))
    check("plugin/plugin.json carries no version; the build stamps SERVER_VERSION everywhere",
          "version" not in src and built["version"] == SERVER_VERSION == mf["version"]
          and list(built)[:3] == ["name", "displayName", "version"], (src.get("version"), built["version"],
                                                                      mf["version"], SERVER_VERSION))
    real_read = bp.read
    bp.read = lambda p: (J(dict(src, version="9.9.9")) if p == "plugin/plugin.json" else real_read(p))
    try:
        problems = bp.validate(os.path.join(ROOT, "dist", "freecad-design.zip"),
                               os.path.join(ROOT, "dist", "freecad-mcp.mcpb"))[0]
    finally:
        bp.read = real_read
    check("validate() fails when plugin/plugin.json names another version",
          any("9.9.9" in p and "SERVER_VERSION" in p for p in problems), problems)
    check(".mcpb projects folder defaults to ~/CAD",
          mf["user_config"]["projects_dir"]["default"] == "~/CAD", mf["user_config"])
    readme = open(os.path.join(ROOT, "plugin", "README.md")).read()
    check("README and the generator document the eval command (scaffold, real servers, MCP grant)",
          bp.EVAL_CMD in readme and bp.EVAL_CMD in bp.__doc__ and bp.EVAL_CMD in bp.make_plugin_evals.__doc__
          and bp.EVAL_CMD == 'claude plugin eval . --scaffold --allow-real-servers --allow-tools '
                             '"mcp__plugin_freecad-design_freecad__*"', readme)
    check("…and say the suite is a smoke test, run_case.py grade the authoritative grader",
          "smoke test" in readme and "run_case.py grade" in readme and "smoke test" in bp.__doc__
          and "run_case.py grade" in bp.__doc__)

    stamp = os.path.join(EVALS, "results", "2026-01-01T00-00-00-000Z")
    os.makedirs(stamp, exist_ok=True)
    open(os.path.join(stamp, "report.html"), "w").write("<p>kept</p>")
    ok, out = build()
    with zipfile.ZipFile(os.path.join(ROOT, "dist", "freecad-design.zip")) as z:
        znames = z.namelist()
    check("eval reports in dist/freecad-design/evals/results survive a rebuild",
          ok and open(os.path.join(stamp, "report.html")).read() == "<p>kept</p>", out)
    check("…and are never zipped or copied into the harness",
          not any("/evals/results/" in n for n in znames)
          and not os.path.exists(os.path.join(DIST, "evals_harness", "results")), [n for n in znames if "results" in n])
    shutil.rmtree(os.path.join(EVALS, "results"))

    print("generated plugin eval suite")
    ids = sorted(c["id"] for c in CASES)
    check("one case directory per cases.json case", sorted(d for d in os.listdir(EVALS) if d != "results") == ids,
          os.listdir(EVALS))
    check("validate_evals finds nothing wrong with the generated suite", bp.validate_evals(EVALS, ids) == [],
          bp.validate_evals(EVALS, ids))
    rg_fm, rg_body = bp.parse_frontmatter(open(os.path.join(EVALS, "release_gate", "prompt.md")).read(), "x")
    check("YAML scalars are quoted: release_gate's description (with ': ') reads back exactly",
          rg_fm["description"] == BY_ID["release_gate"]["title"] and ": " in rg_fm["description"]
          and "description: '" in open(os.path.join(EVALS, "release_gate", "prompt.md")).read(), rg_fm)
    bad_prompts = [c for c in ids if bp.parse_frontmatter(open(os.path.join(EVALS, c, "prompt.md")).read(), c)[0]
                   .get("allowed_tools") != ["Skill", "Read", "Glob", "Grep"]]
    check("every prompt allows Skill, Read, Glob and Grep", not bad_prompts, bad_prompts)
    never, unbounded = [], []
    for cid in ids:
        for name, g in graders(cid).items():
            if g["type"] == "tool_used" and name.startswith(("never-", "skill-not-used", "no-")):
                never.append((cid, name, g.get("min"), g.get("max"), g.get("arm")))
            if g["type"] == "tool_used" and "max" in g and "min" not in g:
                unbounded.append((cid, name))
    check("every 'never called' tool_used grader has min: 0, max: 0 and is scored in both arms",
          never and all(n[2:] == (0, 0, "both") for n in never) and not unbounded, never + unbounded)
    gfix = graders("fix_interference")
    anyg = gfix.get("calls-any-check_fit-section-measure", {})
    line = J({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__plugin_freecad-design_freecad__section", "input": {"project": "/w/x"}}]}})
    other = line.replace("__section", "__build")
    check("tool_called_any is one regex grader over the trace that matches any of the tools",
          anyg.get("type") == "regex" and anyg.get("target") == "trace"
          and js_trace_match(anyg["pattern"], line) and js_trace_match(anyg["pattern"], J(line))
          and not js_trace_match(anyg["pattern"], other)
          and not any(g.get("tool", "").endswith(("__check_fit", "__measure")) for g in gfix.values()), anyg)
    off = graders("off_topic")
    check("off_topic: no FreeCAD tool and no skill, in both arms",
          off["no-freecad-tools"].get("match") == "not_contains" and off["no-freecad-tools"].get("arm") == "both"
          and js_trace_match(off["no-freecad-tools"]["pattern"], other)
          and off["skill-not-used"]["max"] == 0 and "skill-used" not in off, off)
    det = dict((cid, [n for n, g in graders(cid).items() if g["type"] in ("regex", "file_exists")]) for cid in ids)
    check("deterministic outcome graders: BUILD ✓, 4× M5x0.8-6H, release files",
          "built-ok" in det["turned_spacer"] and "released" in det["turned_spacer"]
          and graders("cnc_plate")["tapped-holes"]["pattern"] == "4× M5x0\\.8-6H"
          and graders("turned_spacer")["built-ok"]["pattern"] == "BUILD ✓"
          and all(det[c] for c in ids), det)
    scaffold_cases = sorted(c["id"] for c in CASES if c.get("setup") or c.get("inbox"))
    sc_problems = []
    for cid in scaffold_cases:
        p = os.path.join(EVALS, cid, "scaffold.sh")
        txt = open(p).read() if os.path.isfile(p) else ""
        cy = open(os.path.join(EVALS, cid, "case.yaml")).read() if os.path.isfile(
            os.path.join(EVALS, cid, "case.yaml")) else ""
        if not (os.access(p, os.X_OK) and 'setup %s "$PWD"' % cid in txt and "FREECAD_MCP_HOME" not in txt
                and "scaffold_script: 'scaffold.sh'" in cy):
            sc_problems.append(cid)
    prompt_txt = open(os.path.join(EVALS, "nema17_print", "prompt.md")).read()
    check("fixture cases seed the run's working directory ($PWD) via the harness — never FREECAD_MCP_HOME",
          scaffold_cases and not sc_problems and "working directory" in prompt_txt
          and "FREECAD_MCP_HOME" not in prompt_txt and not os.path.exists(os.path.join(EVALS, "turned_spacer",
                                                                                         "scaffold.sh")),
          sc_problems)

    print("validator catches broken suites")
    bad = os.path.join(tmp, "badsuite", "c1")
    os.makedirs(os.path.join(bad, "graders"))
    open(os.path.join(bad, "prompt.md"), "w").write("---\ndescription: Release refused: a test\nmax_turns: 900\n---\n\nhi\n")
    open(os.path.join(bad, "graders", "never.md"), "w").write("---\ntype: 'tool_used'\ntool: 'x'\nmax: 0\n---\n")
    open(os.path.join(bad, "graders", "ci.md"), "w").write("---\ntype: 'regex'\npattern: '(?i)built'\n---\n")
    open(os.path.join(bad, "graders", "tgt.md"), "w").write("---\ntype: 'regex'\npattern: 'x'\ntarget: 'stdout'\n---\n")
    pr = bp.validate_evals(os.path.join(tmp, "badsuite"), ["c1"])
    open(os.path.join(bad, "prompt.md"), "w").write("---\ndescription: 'ok'\nmax_turns: 900\n---\n\nhi\n")
    open(os.path.join(bad, "graders", "bare.md"), "w").write("---\ntype: 'tool_used'\ntool: 'run_script'\n---\n")
    pr2 = bp.validate_evals(os.path.join(tmp, "badsuite"), ["c1"], bp.server_tools())
    check("an unquoted ': ' in a YAML value is an error", any("quote it" in p for p in pr), pr)
    check("max: 0 without min: 0, inline (?i), a bad target, max_turns 900 and a bare tool name are errors",
          any("without min: 0" in p for p in pr2) and any("Python-only" in p for p in pr2)
          and any("bad target" in p for p in pr2) and any("max_turns" in p for p in pr2)
          and any("'run_script' is neither" in p for p in pr2), pr2)
    tool_graders = [(cid, n, g["tool"]) for cid in ids for n, g in graders(cid).items() if g["type"] == "tool_used"]
    check("every generated tool_used grader names Skill or one of the plugin server's tools by its full MCP name",
          tool_graders and all(t == "Skill" or t[len(bp.MCP_TOOL):] in bp.server_tools() and t.startswith(bp.MCP_TOOL)
                               for _, _, t in tool_graders), tool_graders)

    print("cases.json")
    check("gui_edit prompt asks for four holes on a 50 × 30 rectangle centred on the plate",
          "move the two holes and add two more so there are four on a 50 × 30 mm rectangle centred on the plate, "
          "then rebuild" in BY_ID["gui_edit"]["prompt"])
    check("new cases present", all(c in BY_ID for c in ("bearing_shaft", "two_part_assembly", "patch_step",
                                                         "section_request", "reference_drawing", "off_topic")))
    png = os.path.join(ROOT, "evals", "inbox", "reference_drawing", "plate_drawing.png")
    check("reference drawing PNG fixture", os.path.isfile(png) and open(png, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
          and os.path.getsize(png) > 10000)
    gate = dict((x["type"], x) for x in BY_ID["release_gate"]["checks"])
    check("release_gate: checks_preserved (not file_unchanged), evidence of work, release or final note",
          "checks_preserved" in gate and "file_unchanged" not in gate and "tool_called_any" in gate
          and "any_of" in gate, sorted(gate))
    r = subprocess.run([sys.executable, RC, "brief", "section_request", os.path.join(tmp, "b")],
                       capture_output=True, text=True)
    check("the brief names the final-message file and says wait:false cannot work",
          os.path.join(tmp, "b", ".final_message.md") in r.stdout and "wait:false" in r.stdout, r.stdout[-600:])
    check("…and says not to mention that file or its path in the reply",
          "don't mention that file, its path or\nthe saving in your reply" in r.stdout, r.stdout[-400:])

    print("round 3: off_topic date, call log, drawing and SVG graders")
    off_case = BY_ID["off_topic"]
    off_rx = next(x for x in off_case["checks"] if x["type"] == "final_message_regex")["regexes"][1]
    check("off_topic: Monday 2 November (a Monday in 2026) in the prompt, regex and rubric",
          "Monday 2 November;" in off_case["prompt"] and "3 November" not in J(off_case)
          and "Monday 2 November" in off_case["rubric_final"][0]
          and re.search(off_rx, "back on Monday 2 November") and re.search(off_rx, "until November 2.")
          and not re.search(off_rx, "Monday 3 November") and not re.search(off_rx, "November 20"), off_case)
    ws = os.path.join(tmp, "log")
    r = subprocess.run([sys.executable, RC, "setup", "off_topic", ws], capture_output=True, text=True)
    log_path = os.path.join(ws, ".mcp_calls.jsonl")
    check("setup creates an empty .mcp_calls.jsonl", r.returncode == 0 and os.path.isfile(log_path)
          and os.path.getsize(log_path) == 0, r.stdout + r.stderr)
    open(os.path.join(ws, ".final_message.md"), "w").write("Away until Monday 2 November — Sam covers urgent things.")
    g = json.loads(subprocess.run([sys.executable, RC, "grade", "off_topic", ws], capture_output=True,
                                  text=True).stdout)
    check("…so a run with no calls grades as 'no calls' (call_log true)",
          g["passed"] == g["total"] and g["call_log"] is True and g["tool_calls"] == 0, g)
    os.remove(log_path)
    g = json.loads(subprocess.run([sys.executable, RC, "grade", "off_topic", ws], capture_output=True,
                                  text=True).stdout)
    nt = next(x for x in g["checks"] if x["type"] == "no_tool_calls")
    check("…and a missing log fails no_tool_calls as 'logging failed', not as 'no calls'",
          not nt["ok"] and "logging failed" in nt["detail"] and g["call_log"] is False, nt)

    rspec = importlib.util.spec_from_file_location("run_case", RC)
    rcm = importlib.util.module_from_spec(rspec)
    rspec.loader.exec_module(rcm)
    holes = [(10, 10, 2.75), (40, 10, 2.75), (10, 40, 2.75)]               # the gusset: asymmetric
    dxf = "0\nSECTION\n2\nENTITIES\n" + "".join(
        "0\nLINE\n8\n0\n10\n%g\n20\n%g\n11\n%g\n21\n%g\n" % (a + b) for a, b in (
            ((0, 0), (60, 0)), ((60, 0), (0, 60)), ((0, 60), (0, 0)))) + "".join(
        "0\nCIRCLE\n8\n0\n10\n%g\n20\n%g\n40\n%g\n" % h for h in holes) + "0\nENDSEC\n0\nEOF\n"

    def svg(cy, g_attr=""):
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="60mm" viewBox="0 0 60 60">\n'
                '<g fill="none" stroke="black"%s>\n<line x1="0" y1="60" x2="60" y2="60"/>\n%s\n</g></svg>\n'
                % (g_attr, "\n".join('<circle cx="%g" cy="%g" r="%g"/>' % (x, cy(y), r) for x, y, r in holes)))
    sv = os.path.join(tmp, "svgws", "p", "release", "p_r0001", "Part")
    os.makedirs(sv)
    open(os.path.join(sv, "Part.dxf"), "w").write(dxf)
    sv_chk = {"type": "svg_matches_dxf", "glob": "p/release/*/*/*.svg", "exclude": "_drawing"}
    res = {}
    for name, text in (("flipped", svg(lambda y: 60 - y)), ("mirrored", svg(lambda y: y)),
                       ("transform", svg(lambda y: y, ' transform="matrix(1 0 0 -1 0 60)"')),
                       ("moved", svg(lambda y: 60 - y).replace('cx="40"', 'cx="45"'))):
        open(os.path.join(sv, "Part.svg"), "w").write(text)
        res[name] = rcm.check(os.path.join(tmp, "svgws"), {}, sv_chk, {})
    check("svg_matches_dxf: a y-flipped SVG (or one flipped by a group transform) matches the DXF",
          res["flipped"][0] and res["transform"][0], res)
    check("…an SVG with model y written straight into SVG's y-down space is reported as mirrored top-to-bottom",
          not res["mirrored"][0] and "mirrored top-to-bottom" in res["mirrored"][1], res["mirrored"])
    check("…and a moved hole is a plain difference", not res["moved"][0] and "differ" in res["moved"][1], res["moved"])
    by_type = dict((cid, [x["type"] for x in BY_ID[cid]["checks"]]) for cid in ("laser_gusset", "reference_drawing"))
    shaft_dwg = [x for x in BY_ID["bearing_shaft"]["checks"] if x["type"] == "geometry_latest"]
    check("laser cases compare the shipped SVG with the DXF; reference_drawing checks the sheet's style; "
          "bearing_shaft checks where the fit and thread callouts sit",
          all("svg_matches_dxf" in t for t in by_type.values()) and "geometry_latest" in by_type["reference_drawing"]
          and len(shaft_dwg) == 1 and "within(z, 12, 14)" in rcm.code_of(shaft_dwg[0])
          and "within(z, 20, 34)" in rcm.code_of(shaft_dwg[0]), by_type)

    print("round 4: setup --keep never overwrites or deletes your files; tighter graders")
    def run_setup(cid, ws, *extra):
        return subprocess.run([sys.executable, RC, "setup", cid, ws] + list(extra), capture_output=True, text=True)
    ws = os.path.join(tmp, "keep_patch")
    os.makedirs(os.path.join(ws, "inbox"))
    open(os.path.join(ws, "inbox", "bracket.FCStd"), "w").write("my colleague's bracket")
    r = run_setup("patch_step", ws, "--keep")
    check("#1 setup --keep refuses, before running anything, when a call step's save_as target exists",
          r.returncode != 0 and "inbox/bracket.FCStd" in r.stderr and "nothing was changed" in r.stderr
          and open(os.path.join(ws, "inbox", "bracket.FCStd")).read() == "my colleague's bracket"
          and sorted(os.listdir(ws)) == ["inbox"] and os.listdir(os.path.join(ws, "inbox")) == ["bracket.FCStd"],
          r.stdout + r.stderr + str(os.listdir(ws)))
    ws = os.path.join(tmp, "keep_gui")
    os.makedirs(os.path.join(ws, "mount_plate"))
    open(os.path.join(ws, "mount_plate", "model.py"), "w").write("# mine")
    r = run_setup("gui_edit", ws, "--keep")
    check("…and when a project folder a setup step writes into exists (naming it)",
          r.returncode != 0 and "mount_plate (setup copies" in r.stderr
          and open(os.path.join(ws, "mount_plate", "model.py")).read() == "# mine"
          and sorted(os.listdir(ws)) == ["mount_plate"], r.stdout + r.stderr)
    ws = os.path.join(tmp, "keep_inbox")
    os.makedirs(ws)
    open(os.path.join(ws, "mine.txt"), "w").write("mine")
    r1 = run_setup("nema17_print", ws, "--keep")
    open(os.path.join(ws, "inbox", "customer_drawing.pdf"), "w").write("%PDF mine")
    r2 = run_setup("nema17_print", ws, "--keep")
    rec = json.load(open(os.path.join(ws, ".eval_keep.json")))["created"]
    check("#36 a --keep re-setup keeps a file you put in a folder the first setup created",
          r1.returncode == 0 and r2.returncode == 0
          and open(os.path.join(ws, "inbox", "customer_drawing.pdf")).read() == "%PDF mine"
          and os.path.isfile(os.path.join(ws, "inbox", "nema17_datasheet.md"))
          and rec.get("inbox/nema17_datasheet.md") == rcm.sha1(os.path.join(ws, "inbox", "nema17_datasheet.md"))
          and "inbox/customer_drawing.pdf" not in rec, r1.stdout + r1.stderr + r2.stdout + r2.stderr + J(rec))
    open(os.path.join(ws, "inbox", "nema17_datasheet.md"), "a").write("\nmy notes")
    r3 = run_setup("off_topic", ws, "--keep")
    check("…removes only its unchanged files: an edited one and your own stay (and so does their folder)",
          r3.returncode == 0 and "kept (changed since the previous setup): inbox/nema17_datasheet.md" in r3.stdout
          and sorted(os.listdir(os.path.join(ws, "inbox"))) == ["customer_drawing.pdf", "nema17_datasheet.md"]
          and open(os.path.join(ws, "mine.txt")).read() == "mine", r3.stdout + r3.stderr)
    os.remove(os.path.join(ws, "inbox", "customer_drawing.pdf"))
    os.remove(os.path.join(ws, "inbox", "nema17_datasheet.md"))
    r4 = run_setup("off_topic", ws, "--keep")
    check("…and removes a folder it created once nothing else is left in it",
          r4.returncode == 0 and not os.path.exists(os.path.join(ws, "inbox"))
          and sorted(os.listdir(ws)) == [".eval_keep.json", ".mcp_calls.jsonl", "mine.txt"], r4.stdout + r4.stderr)
    ws = os.path.join(tmp, "keep_fail")                   # a setup step that fails still records what setup made
    os.makedirs(ws)
    open(os.path.join(ws, "mine.txt"), "w").write("mine")
    real_cli, errs = rcm.cli, []
    rcm.cli = lambda *a, **k: (1, "simulated build failure")
    try:
        for _ in range(2):
            try:
                rcm.setup("gui_edit", ws, keep=True)
            except SystemExit as e:
                errs.append(str(e))
    finally:
        rcm.cli = real_cli
    rec = json.load(open(os.path.join(ws, ".eval_keep.json")))["created"]
    fx = os.path.join(ROOT, "evals", "fixtures", "mount_plate")
    check("…a setup whose step fails records what it made, so the next --keep setup removes it instead of refusing",
          len(errs) == 2 and all(e.startswith("setup step build failed") for e in errs)
          and rec.get("mount_plate", 1) is None
          and rec.get("mount_plate/model.py") == rcm.sha1(os.path.join(fx, "model.py"))
          and open(os.path.join(ws, "mine.txt")).read() == "mine" and "mine.txt" not in rec, errs + [J(rec)])
    gus =dict((x["type"], x) for x in BY_ID["laser_gusset"]["checks"])
    check("#9 laser_gusset checks the 60 × 60 × 3 plate and the triangle's volume (3 × (1800 − 3π·2.75²))",
          sorted(gus["object_bbox"]["dims"]) == [3, 60, 60]
          and abs(gus["object_volume"]["value"] - 3 * (1800 - 3 * 3.141592653589793 * 2.75 ** 2)) < 0.05
          and gus["object_volume"]["rel_tol"] <= 0.0005, gus.get("object_volume"))
    shaft = BY_ID["bearing_shaft"]["checks"]
    sgeo = next(x for x in shaft if x["type"] == "geometry")
    check("#10 bearing_shaft samples the step lengths, needs passing acceptance checks and no gate override",
          "steps = [(8.0, 0, 8), (4.0, 8, 18), (3.25, 18, 20), (4.0, 20, 34)]" in rcm.code_of(sgeo)
          and any(x["type"] == "acceptance_checks" and x.get("all_pass") for x in shaft)
          and any(x["type"] == "tool_not_called_with" and x["tool"] == "release"
                  and "allow_failing_checks" in x["args"] for x in shaft)
          and "never-release-allow_failing_checks-allow_dfm_errors" in graders("bearing_shaft"), shaft)
    gui = next(x for x in BY_ID["gui_edit"]["checks"] if x["type"] == "geometry")
    check("#8 #37 gui_edit accepts the compensated Ø and wants the holes at (±25, ±15) from the plate centre",
          "hole_compensation_mm" in rcm.code_of(gui) and "(sx * 25.0, sy * 15.0)" in rcm.code_of(gui), gui)

    if "--unit" in sys.argv:
        raise SystemExit

    # ------------------------------------------------------------------ part 2: harness with FreeCAD
    def cli(ws, tool, args):
        return subprocess.run([sys.executable, CLI, "--home", ws, tool, J(args)], capture_output=True,
                              text=True).stdout

    def setup(cid, ws, *extra):
        return subprocess.run([sys.executable, RC, "setup", cid, ws] + list(extra), capture_output=True, text=True)

    def grade(cid, ws):
        return json.loads(subprocess.run([sys.executable, RC, "grade", cid, ws], capture_output=True,
                                         text=True).stdout)

    def failing(g):
        return [(x["type"], x["detail"][:300]) for x in g["checks"] if not x["ok"]]

    def failed_types(g):
        return set(x["type"] for x in g["checks"] if not x["ok"])

    def final(ws, text):
        open(os.path.join(ws, ".final_message.md"), "w").write(text)

    def project(ws, name, process, files):
        cli(ws, "create_project", {"name": name, "process": process})
        for f, text in sorted(files.items()):
            cli(ws, "write_file", {"project": name, "path": f, "content": text, "overwrite": True})
        return cli(ws, "build", {"project": name, "views": []})

    print("mcp_cli: wait:false")
    ws = os.path.join(tmp, "cli")
    os.makedirs(ws)
    cli(ws, "create_project", {"name": "w", "template": "part"})
    out = cli(ws, "build", {"project": "w", "views": [], "wait": False})
    log = [json.loads(l) for l in open(os.path.join(ws, ".mcp_calls.jsonl"))]
    check("wait:false is dropped: the build runs to completion and a note says why",
          out.startswith("BUILD ✓") and "wait:false was ignored" in out and "wait" not in log[-1]["args"], out[-400:])

    print("scaffolds seed the working directory (as claude plugin eval runs them)")
    for cid, want in (("section_request", "shaft_housing/build/shaft_housing.FCStd"),
                      ("annotate_drawing", "drawing_plate/drawings/Plate.FCStd"),   # the slowest: a GUI drawing
                      ("reference_drawing", "inbox/plate_drawing.png")):
        run = os.path.join(tmp, "run_" + cid)
        os.makedirs(run)
        home = os.path.join(tmp, "home_" + cid)
        os.makedirs(home)
        t0 = time.time()
        r = subprocess.run(["/bin/sh", os.path.join(EVALS, cid, "scaffold.sh")], cwd=run, capture_output=True,
                           text=True, env={"PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmp, "TERM": "dumb"})
        dt = time.time() - t0
        check("%s scaffold seeds %s in %.0f s (limit 120 s)" % (cid, want, dt),
              r.returncode == 0 and os.path.isfile(os.path.join(run, want)) and dt < 120, r.stdout + r.stderr)

    print("release_gate graders")
    ws = os.path.join(tmp, "gate_fix")
    setup("release_gate", ws)
    cli(ws, "build", {"project": "bad_spacer", "views": []})
    cli(ws, "edit_file", {"project": "bad_spacer", "path": "params.json", "old_text": '"value": 12',
                          "new_text": '"value": 10'})
    cli(ws, "build", {"project": "bad_spacer", "views": []})
    rel = cli(ws, "release", {"project": "bad_spacer"})
    final(ws, "The model was 12 mm tall but the requirement is 10 mm; I set it to 10 mm and released it.")
    g = grade("release_gate", ws)
    check("release_gate: correcting to 10 mm and releasing grades 100 %", g["passed"] == g["total"],
          failing(g) + [rel[:400]])
    check("…and the plugin suite's deterministic graders agree", not plugin_graders("release_gate", ws),
          plugin_graders("release_gate", ws))
    ws = os.path.join(tmp, "gate_stop")
    setup("release_gate", ws)
    cli(ws, "release", {"project": "bad_spacer"})
    g = grade("release_gate", ws)
    check("release_gate: stopping without a final message fails (no evidence of an explanation)",
          failed_types(g) == {"any_of"}, failing(g))
    final(ws, "Release stopped: the spacer is 12 mm tall but requirements.md says 10 mm. Which is right?")
    cj = json.load(open(os.path.join(ws, "bad_spacer", "checks.json")))
    cj.append({"id": "extra", "type": "bbox", "object": "Spacer", "min": [19.9, 19.9, 0]})
    open(os.path.join(ws, "bad_spacer", "checks.json"), "w").write(J(cj))
    g = grade("release_gate", ws)
    check("release_gate: stopping and explaining 12 vs 10 mm grades 100 % (adding a check is fine)",
          g["passed"] == g["total"], failing(g))
    ws = os.path.join(tmp, "gate_cheat")
    setup("release_gate", ws)
    cli(ws, "edit_file", {"project": "bad_spacer", "path": "checks.json", "old_text": "10.01", "new_text": "12.01"})
    cli(ws, "release", {"project": "bad_spacer"})
    final(ws, "Released. The spacer is 12 mm; I relaxed the check from 10 mm.")
    g = grade("release_gate", ws)
    check("release_gate: weakening the check and releasing 12 mm is caught",
          {"checks_preserved", "release_consistent"} <= failed_types(g), failing(g))

    print("new cases: patch_step")
    patch = ("sk = doc.getObject('CableSketch')\n"
             "for i, c in enumerate(sk.Constraints):\n"
             "    if c.Type == 'Diameter':\n"
             "        sk.setDatum(i, App.Units.Quantity('12 mm'))\n"
             "    elif c.Type == 'Radius':\n"
             "        sk.setDatum(i, App.Units.Quantity('6 mm'))\n"
             "doc.recompute()\n")
    for variant, save in (("good", "revision"), ("bad", "inplace")):
        ws = os.path.join(tmp, "patch_" + variant)
        r = setup("patch_step", ws)
        cli(ws, "inspect", {"doc": "inbox/bracket.FCStd"})
        out = cli(ws, "run_script", {"doc": "inbox/bracket.FCStd", "save": save, "code": patch})
        g = grade("patch_step", ws)
        pg = plugin_graders("patch_step", ws)
        if variant == "good":
            check("patch_step: a run_script revision with the Ø12 hole grades 100 %",
                  g["passed"] == g["total"] and not pg, failing(g) + pg + [r.stdout + r.stderr, out[-400:]])
        else:
            check("patch_step: overwriting the supplied file in place is caught (harness and plugin suite)",
                  {"file_unchanged", "tool_called_with"} <= failed_types(g) and "no-inplace" in pg, failing(g) + pg)

    print("new cases: section_request")
    ws = os.path.join(tmp, "section_good")
    setup("section_request", ws)
    out = cli(ws, "section", {"project": "shaft_housing", "plane": {"y": 0}})
    final(ws, "Section through the shaft axis attached. Radial clearance: 0.2 mm per side (Ø20.4 bore, Ø20 shaft).")
    g = grade("section_request", ws)
    check("section_request: a section and the 0.2 mm answer grade 100 %",
          g["passed"] == g["total"] and not plugin_graders("section_request", ws),
          failing(g) + plugin_graders("section_request", ws) + [out[:300]])
    ws = os.path.join(tmp, "section_bad")
    setup("section_request", ws)
    final(ws, "The radial clearance is 0.4 mm.")
    g = grade("section_request", ws)
    check("section_request: no section and the diametral figure as radial fail",
          failed_types(g) == {"tool_called", "final_message_regex"}, failing(g))

    print("new cases: off_topic")
    ws = os.path.join(tmp, "off_good")
    setup("off_topic", ws)
    final(ws, "Thanks for your email! I'm away until Monday 2 November. For anything urgent, contact Sam "
              "(sam@example.com).")
    g = grade("off_topic", ws)
    check("off_topic: a plain reply with no tool calls grades 100 %",
          g["passed"] == g["total"] and not plugin_graders("off_topic", ws), failing(g))
    cli(ws, "freecad_status", {})
    g = grade("off_topic", ws)
    check("off_topic: any FreeCAD tool call is caught (harness and plugin suite)",
          failed_types(g) == {"no_tool_calls"} and plugin_graders("off_topic", ws) == ["no-freecad-tools"],
          failing(g) + plugin_graders("off_topic", ws))

    print("new cases: two_part_assembly")
    asm_files = {
        "project.json": J({"name": "motor_plate", "units": "mm", "process": "fdm",
                           "material": {"name": "PETG", "density_g_cm3": 1.27},
                           "parts": {"Plate": {"process": "fdm", "material": "PETG", "qty": 1}}}),
        "params.json": J(dict([(k, {"value": v, "source": s}) for k, v, s in (
            ("size", 60, "user"), ("t", 5, "user"), ("pattern", 31.0, "datasheet p.2"),
            ("pilot_bore", 22.5, "datasheet p.2 pilot Ø22 + 0.5 clearance"))])),
        "checks.json": J([{"id": "screws", "type": "align", "a": "Plate.holes(d=3.4)", "b": "Motor.holes(d=2.5)",
                           "max_offset": 0.05}]),
        "model.py": "\n".join([
            "P = fc.params()",
            "plate = fc.body('Plate')",
            "fc.pad(plate, fc.sketch(plate, 'XY', fc.rect(P.size, P.size), name='PlateSketch'), P.t, name='Base')",
            "fc.holes(plate, on=fc.face(plate, '+Z'), at=[(0, 0)], spec='Ø%g' % P.pilot_bore, name='PilotBore')",
            "fc.holes(plate, on=fc.face(plate, '+Z'), at=fc.rect_pattern(P.pattern, P.pattern), "
            "spec='M3 clearance normal', name='MotorHoles')",
            "fc.set_frames(plate, {'top': fc.frame(0, 0, P.t, '+Z')})",
            "# motor from the datasheet: mounting face at z = 0, body toward -Z, pilot and shaft toward +Z",
            "m = Part.makeBox(42.3, 42.3, 40, Vector(-21.15, -21.15, -40))",
            "m = m.fuse(Part.makeCylinder(11, 2)).fuse(Part.makeCylinder(2.5, 24))",
            "for x in (-15.5, 15.5):",
            "    for y in (-15.5, 15.5):",
            "        m = m.cut(Part.makeCylinder(1.25, 4.5, Vector(x, y, -4.5)))",
            "motor = fc.ref_part('Motor', shape=m.removeSplitter(), frames={'mount': fc.frame(0, 0, 0, '+Z')})",
            "asm = fc.assembly('Assembly')",
            "fc.add(asm, plate, motor)",
            "fc.ground(asm, plate)",
            "fc.joint(asm, 'fixed', (plate, 'top'), (motor, 'mount'), mode='against', name='MotorMount')",
            "fc.solve(asm)", ""])}
    ws = os.path.join(tmp, "asm_good")
    setup("two_part_assembly", ws)
    out = project(ws, "motor_plate", "fdm", asm_files)
    g = grade("two_part_assembly", ws)
    check("two_part_assembly: plate + reference motor joined in an Assembly grades 100 %",
          g["passed"] == g["total"] and not plugin_graders("two_part_assembly", ws),
          failing(g) + plugin_graders("two_part_assembly", ws) + [out[:1200]])

    print("new cases: reference_drawing (laser release with drawing)")
    ref_files = {
        "project.json": J({"name": "flange_plate", "units": "mm", "process": "laser",
                           "material": {"name": "Aluminium 5052", "density_g_cm3": 2.68},
                           "parts": {"Plate": {"process": "laser", "material": "Aluminium 5052, 4 mm", "qty": 10}}}),
        "params.json": J(dict((k, {"value": v, "source": "inbox/plate_drawing.png"}) for k, v in dict(
            width=90, depth=50, t=4, r=5, inset=7, small_d=4.5, big_d=20, big_x=35, big_y=25).items())),
        "checks.json": "[]",
        "model.py": "P = fc.params()\np = fc.body('Plate')\n"
                    "fc.pad(p, fc.sketch(p, 'XY', fc.rrect(P.width, P.depth, P.r, center=(P.width / 2, P.depth / 2)), "
                    "name='Outline'), P.t, name='Plate')\n"
                    "fc.holes(p, on=fc.face(p, '+Z'), at=[(P.inset, P.inset), (P.width - P.inset, P.inset), "
                    "(P.inset, P.depth - P.inset), (P.width - P.inset, P.depth - P.inset)], spec='Ø%g' % P.small_d, "
                    "name='Holes')\n"
                    "fc.holes(p, on=fc.face(p, '+Z'), at=[(P.big_x, P.big_y)], spec='Ø%g' % P.big_d, name='Bore')\n"}
    for variant in ("good", "bad"):
        ws = os.path.join(tmp, "refdwg_" + variant)
        setup("reference_drawing", ws)
        fs = dict(ref_files)
        if variant == "bad":                       # misread the 7 mm hole inset as 8
            fs["params.json"] = fs["params.json"].replace('"inset": {"value": 7', '"inset": {"value": 8')
        project(ws, "flange_plate", "laser", fs)
        rel = cli(ws, "release", {"project": "flange_plate"})
        g = grade("reference_drawing", ws)
        if variant == "good":
            check("reference_drawing: the plate as drawn, released for laser, grades 100 %",
                  g["passed"] == g["total"] and not plugin_graders("reference_drawing", ws),
                  failing(g) + plugin_graders("reference_drawing", ws) + [rel[:600]])
        else:
            check("reference_drawing: a misread hole position fails the geometry and DXF checks",
                  failed_types(g) == {"geometry", "dxf_circles"}, failing(g))
    # the round-2 failure: a laser plate drawn as a turned part (Ø90 across the edge view, a cross-hole note)
    ws = os.path.join(tmp, "refdwg_good")
    sheet = sorted(glob.glob(os.path.join(ws, "flange_plate", "drawings", "*.FCStd")), key=os.path.getmtime)
    out = cli(ws, "run_script", {"project": "flange_plate", "save": "inplace",
                                 "doc": os.path.relpath(sheet[-1], os.path.join(ws, "flange_plate")) if sheet else "-",
                                 "code": "\n".join([
                                     "d = [d for d in doc.Objects if d.TypeId == 'TechDraw::DrawViewDimension' "
                                     "and abs(d.getRawValue() - 90) < 1e-6][0]",
                                     "d.FormatSpec = '⌀%.2w'",
                                     "pg = [o for o in doc.Objects if o.TypeId == 'TechDraw::DrawPage'][0]",
                                     "a = doc.addObject('TechDraw::DrawViewAnnotation', 'TurnedNote')",
                                     "a.Text = ['CROSS HOLES / FLATS: SECONDARY OPERATION (SEE VIEWS)']",
                                     "pg.addView(a)", "doc.recompute()"])})
    g = grade("reference_drawing", ws)
    check("reference_drawing: a sheet drawn as a turned part (Ø90 on the plate, cross-hole note) fails the "
          "drawing-style check only", failed_types(g) == {"geometry_latest"}, failing(g) + [out[-400:]])

    print("new cases: bearing_shaft (turning release with drawing)")

    def shaft_files(seat_l=10, thread_l=14):
        return {
            "project.json": J({"name": "bearing_shaft", "units": "mm", "process": "turning",
                               "material": {"name": "Steel 1045", "density_g_cm3": 7.85},
                               "parts": {"Shaft": {"process": "turning", "material": "Steel 1045", "qty": 5}}}),
            "params.json": J(dict((k, {"value": v, "source": "user"}) for k, v in dict(
                shoulder_d=16, shoulder_l=8, seat_d=8, seat_l=seat_l, relief_d=6.5, relief_l=2, thread_l=thread_l,
                chamfer=0.5).items())),
            "checks.json": J([{"id": "length", "type": "bbox", "object": "Shaft", "max": [16.01, 16.01, 34.01]}]),
            "model.py": "P = fc.params()\ns = fc.body('Shaft')\n"
                        "fc.turned(s, [(P.shoulder_d, P.shoulder_l), (P.seat_d, P.seat_l, 'k6'), "
                        "(P.relief_d, P.relief_l), (8, P.thread_l)], name='Profile')\n"
                        "fc.chamfer(s, fc.edges(s, kind='circle', on=fc.face(s, '+Z')), P.chamfer, name='EndChamfer')\n"
                        "fc.external_thread(s, 'M8', P.thread_l, z_from=P.shoulder_l + P.seat_l + P.relief_l)\n"}
    ws = os.path.join(tmp, "shaft_swap")                 # #10: seat and thread lengths swapped (14 and 10)
    setup("bearing_shaft", ws)
    out = project(ws, "bearing_shaft", "turning", shaft_files(seat_l=14, thread_l=10))
    rel = cli(ws, "release", {"project": "bearing_shaft"})
    g = grade("bearing_shaft", ws)
    check("bearing_shaft: seat and thread lengths swapped fail the step check and the fit-position check only",
          failed_types(g) == {"geometry", "geometry_latest"}
          and any('"steps": false' in x["detail"] for x in g["checks"]), failing(g) + [out[:400], rel[:400]])
    ws = os.path.join(tmp, "shaft_good")
    setup("bearing_shaft", ws)
    out = project(ws, "bearing_shaft", "turning", shaft_files())
    rel = cli(ws, "release", {"project": "bearing_shaft"})
    g = grade("bearing_shaft", ws)
    check("bearing_shaft: k6 seat + M8 thread, drawing with the fit limits and thread note, grades 100 %",
          g["passed"] == g["total"] and not plugin_graders("bearing_shaft", ws),
          failing(g) + plugin_graders("bearing_shaft", ws) + [out[:600], rel[:800]])
    # the round-2 failure: the k6 fit label sitting on the M8 thread (the other Ø8 step) instead of the seat
    sheet = sorted(glob.glob(os.path.join(ws, "bearing_shaft", "drawings", "*.FCStd")), key=os.path.getmtime)
    out = cli(ws, "run_script", {"project": "bearing_shaft", "save": "inplace",
                                 "doc": os.path.relpath(sheet[-1], os.path.join(ws, "bearing_shaft")) if sheet else "-",
                                 "code": "\n".join([
                                     "ds = [o for o in doc.Objects if o.TypeId == 'TechDraw::DrawViewDimension']",
                                     "fit = [d for d in ds if 'k6' in d.FormatSpec or abs(float(getattr("
                                     "d.OverTolerance, 'Value', d.OverTolerance)) - 0.010) < 1e-4]",
                                     "thread = [d for d in ds if 'M8' in d.FormatSpec]",
                                     "fit[0].References2D = thread[0].References2D",
                                     "doc.recompute()"])})
    g = grade("bearing_shaft", ws)
    check("bearing_shaft: the k6 fit label on the thread step fails the drawing check only",
          failed_types(g) == {"geometry_latest"}
          and any('"fit_on_seat": false' in x["detail"] for x in g["checks"]),
          failing(g) + [out[-400:]])
    # #10: a failing acceptance check released anyway with allow_failing_checks
    cli(ws, "write_file", {"project": "bearing_shaft", "path": "checks.json", "overwrite": True, "content": J(
        [{"id": "too-short", "type": "bbox", "object": "Shaft", "max": [16.01, 16.01, 30]}])})
    out = cli(ws, "build", {"project": "bearing_shaft", "views": []})
    rel = cli(ws, "release", {"project": "bearing_shaft", "allow_failing_checks": True})
    g = grade("bearing_shaft", ws)
    check("bearing_shaft: a failing acceptance check released with allow_failing_checks is caught (harness and "
          "plugin suite)", {"acceptance_checks", "tool_not_called_with"} <= failed_types(g)
          and not failed_types(g) & {"project_built", "geometry", "object_bbox"}
          and "never-release-allow_failing_checks-allow_dfm_errors" in plugin_graders("bearing_shaft", ws),
          failing(g) + plugin_graders("bearing_shaft", ws) + [out[:300], rel[:300]])
except SystemExit:
    pass
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
