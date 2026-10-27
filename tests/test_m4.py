#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M4 tests: the plugin package, the MCP Reload helper and the eval harness.

    python3 tests/test_m4.py

Everything runs against a freshly built dist/freecad-design, started exactly as
the plugin's .mcp.json starts it. FreeCAD GUI checks use private settings and a
temporary FreeCAD user folder — your own FreeCAD setup is never touched.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mcp_client  # noqa: E402
from mcp_client import Client, check, fails  # noqa: E402

DIST = os.path.join(ROOT, "dist", "freecad-design")
RES = sorted(glob.glob("/Applications/FreeCAD*.app/Contents/Resources"))[-1]
GUI_CFG = os.path.expanduser("~/.cache/freecad-mcp/gui/user.cfg")
CASES = json.load(open(os.path.join(ROOT, "evals", "cases.json")))["cases"]
SERVER_VERSION = re.search(r'^SERVER_VERSION = "([^"]+)"', open(os.path.join(ROOT, "server", "freecad_mcp.py")).read(),
                           re.M).group(1)


def tree_files(top, skip=("__pycache__", "results")):
    """Relative paths of the files under top, as the build copies evals/ (no caches, results or dotfiles)."""
    out = {}
    for r, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            if not f.startswith(".") and not f.endswith(".pyc"):
                out[os.path.relpath(os.path.join(r, f), top)] = open(os.path.join(r, f), "rb").read()
    return out


# scripted solutions for eval cases: (project, process, files, bads) — the files must grade 100 %; each bad
# (file, old, new, failing check types) is a plausible mistake the graders must catch, and only those checks fail
J = json.dumps
SOLUTIONS = {
    "turned_spacer": ("spacer", "turning", {
        "project.json": J({"name": "spacer", "units": "mm", "process": "turning",
                           "material": {"name": "Stainless steel 303", "density_g_cm3": 8.0},
                           "parts": {"Spacer": {"process": "turning", "material": "Stainless steel 303", "qty": 10}}}),
        "params.json": J({k: {"value": v, "source": "user"} for k, v in
                          dict(od=16, length=25, bore=8.2, chamfer=0.5).items()}),
        "checks.json": "[]",
        "model.py": "P = fc.params()\ns = fc.body('Spacer')\nfc.turned(s, [(P.od, P.length)], name='Profile')\n"
                    "fc.holes(s, on=fc.face(s, '+Z'), at=[(0, 0)], spec='Ø%g' % P.bore, name='Bore')\n"
                    "fc.chamfer(s, fc.edges(s, kind='circle'), P.chamfer, name='Chamfers')\n"},
        [("model.py", "fc.edges(s, kind='circle')", "fc.edges(s, kind='circle', r=P.od / 2)",   # outer edges only
          {"object_volume", "geometry"})]),
    "cnc_plate": ("adapter_plate", "cnc", {
        "project.json": J({"name": "adapter_plate", "units": "mm", "process": "cnc",
                           "material": {"name": "Aluminium 6061-T6", "density_g_cm3": 2.7},
                           "parts": {"Plate": {"process": "cnc", "material": "Aluminium 6061-T6", "finish": "bead blast",
                                               "qty": 2}}}),
        "params.json": J({k: {"value": v, "source": "user"} for k, v in dict(
            width=100, depth=60, thickness=8, pitch_x=80, pitch_y=40, pocket_w=40, pocket_d=20, pocket_depth=3,
            pocket_r=3).items()}),
        "checks.json": "[]",
        "model.py": "P = fc.params()\np = fc.body('Plate')\n"
                    "fc.pad(p, fc.sketch(p, 'XY', fc.rect(P.width, P.depth), name='PlateSketch'), P.thickness, name='Base')\n"
                    "fc.pocket(p, fc.sketch(p, fc.face(p, '+Z'), fc.rrect(P.pocket_w, P.pocket_d, P.pocket_r), "
                    "name='PocketSketch'), P.pocket_depth, name='Pocket')\n"
                    "fc.holes(p, on=fc.face(p, '+Z', at=P.thickness), at=fc.rect_pattern(P.pitch_x, P.pitch_y), "
                    "spec='M5 tapped', name='Tapped')\n"},
        [("model.py", "spec='M5 tapped'", "spec='M5 clearance normal'",                  # clearance, not tapped
          {"object_volume", "geometry", "report_regex"})]),
    "laser_gusset": ("gusset", "laser", {
        "project.json": J({"name": "gusset", "units": "mm", "process": "laser",
                           "material": {"name": "Aluminium 5052", "density_g_cm3": 2.68},
                           "parts": {"Gusset": {"process": "laser", "material": "Aluminium 5052, 3 mm", "qty": 20}}}),
        "params.json": J({k: {"value": v, "source": "user"} for k, v in dict(leg=60, t=3, hole_d=5.5).items()}),
        "checks.json": "[]",
        "model.py": "P = fc.params()\ng = fc.body('Gusset')\n"
                    "fc.pad(g, fc.sketch(g, 'XY', fc.polygon([(0, 0), (P.leg, 0), (0, P.leg)]), name='Profile'), P.t, "
                    "name='Plate')\nfc.holes(g, on=fc.face(g, '+Z'), at=[(10, 10), (40, 10), (10, 40)], "
                    "spec='Ø%g' % P.hole_d, name='Holes')\n"},
        [("model.py", "(40, 10), (10, 40)", "(45, 10), (10, 45)",                        # holes in the wrong place
          {"dxf_circles"}),
         ("model.py", "fc.polygon([(0, 0), (P.leg, 0), (0, P.leg)]), name='Profile'), P.t,",  # a 6 mm square plate
          "fc.polygon([(0, 0), (P.leg, 0), (P.leg, P.leg), (0, P.leg)]), name='Profile'), 2 * P.t,",
          {"object_bbox", "object_volume"})]),
}
ANNOTATE_OPS = [{"op": "add_note", "text": "ANODISE BLACK, CLASS 2", "x": 150, "y": 40},
                {"op": "add_dimension", "view": "Top", "type": "DistanceX", "a": "Plate.holes(d=4.5)[0]",
                 "b": "Plate.holes(d=4.5)[1]"}]


def gui(script, env, timeout=120):
    e = dict(os.environ, **env)
    for k in ("PYTHONPATH", "PYTHONHOME", "FCMCP_REQ"):
        e.pop(k, None)
    subprocess.run(["perl", "-e", "alarm %d; exec @ARGV" % timeout, os.path.join(RES, "bin", "freecad"),
                    "-u", GUI_CFG, script], env=e, capture_output=True)
    return json.load(open(env["OUT"])) if os.path.isfile(env["OUT"]) else {"error": "no result"}


class PluginClient(Client):
    """Starts the server the way the plugin's .mcp.json does."""

    def __init__(self, home, extra_env=None):
        mcp = json.load(open(os.path.join(DIST, ".mcp.json")))["mcpServers"]["freecad"]
        cmd = [mcp["command"]] + [a.replace("${CLAUDE_PLUGIN_ROOT}", DIST) for a in mcp["args"]]
        env = dict(os.environ, FREECAD_MCP_HOME=home, CLAUDE_PLUGIN_ROOT=DIST)
        env.update(extra_env or {})
        import threading
        self.n = 0
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  cwd=home, env=env)
        self.responses = {}
        self.cv = threading.Condition()
        t = threading.Thread(target=self._reader)
        t.daemon = True
        t.start()


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-m4-"))   # FreeCAD reports /private/var
try:
    print("build")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_plugin.py")], capture_output=True, text=True)
    check("build_plugin.py builds and validates", r.returncode == 0 and "validation: ok" in r.stdout, r.stdout + r.stderr)
    zp = os.path.join(ROOT, "dist", "freecad-design.zip")
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
    check("zip: single top folder, manifest, skill, server, addon, commands",
          all(any(n == "freecad-design/" + x or n.startswith("freecad-design/" + x) for n in names)
              for x in (".claude-plugin/plugin.json", ".mcp.json", "skills/freecad-design/SKILL.md",
                        "skills/freecad-design/references/fclib.md", "server/launch.sh", "server/freecad_mcp.py",
                        "freecad_addon/MCP_Reload.FCMacro", "freecad_addon/FreeCADMCPReload/InitGui.py",
                        "commands/release.md", "README.md", "LICENSE")), "")
    check("no top-level bin/ and no stray caches in the zip",
          not any(n.startswith("freecad-design/bin/") or "__pycache__" in n or n.endswith(".pyc") for n in names))
    with zipfile.ZipFile(os.path.join(ROOT, "dist", "freecad-mcp.mcpb")) as z:
        mf = json.loads(z.read("manifest.json"))
        mnames = z.namelist()
    check(".mcpb: manifest fields and launcher", all(k in mf for k in ("manifest_version", "name", "version",
                                                                         "description", "author", "server"))
          and "server/launch.sh" in mnames and mf["server"]["mcp_config"]["env"]["FREECAD_MCP_HOME"]
          == "${user_config.projects_dir}", json.dumps(mf)[:300])
    ev = sorted(d for d in os.listdir(os.path.join(DIST, "evals")) if d != "results")
    check("plugin eval suite: a prompt and graders per case", ev == sorted(c["id"] for c in CASES) and all(
        os.path.isfile(os.path.join(DIST, "evals", c, "prompt.md")) and os.listdir(os.path.join(DIST, "evals", c, "graders"))
        for c in ev), ev)
    macro = open(os.path.join(ROOT, "freecad_addon", "MCP_Reload.FCMacro")).read()
    try:
        compile(macro, "MCP_Reload.FCMacro", "exec")
        compiled = True
    except SyntaxError:
        compiled = False
    check("macro generated from mcp_reload.py and compiles", compiled and macro.rstrip().endswith("toggle()")
          and open(os.path.join(ROOT, "freecad_addon", "FreeCADMCPReload", "mcp_reload.py")).read() in macro)

    print("plugin server (started as .mcp.json starts it)")
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    udata = os.path.join(tmp, "fc_user_data")
    os.makedirs(udata)
    c = PluginClient(home, {"FREECAD_USER_DATA": udata})
    init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    check("launch.sh starts the server; instructions come from the plugin",
          "release" in init["result"].get("instructions", ""), init["result"].get("instructions", "")[:200])
    tools = c.rpc("tools/list")["result"]["tools"]
    check("22 tools", len(tools) == 22, len(tools))
    err, txt, _ = c.tool("freecad_status")
    st = json.loads(txt)
    check("status: host diagnostics, FreeCAD found, playbook inside the plugin",
          st["host"]["platform"] == "darwin" and st["host"]["plugin_root"] == DIST
          and isinstance(st["freecad"], dict) and st["playbook"].startswith(DIST), txt[:400])
    check("status: reload helper not installed yet (temporary FreeCAD user folder)",
          st.get("reload_helper", {}).get("addon_installed") is False
          and st["reload_helper"]["freecad_user_data"].startswith(udata), st.get("reload_helper"))
    err, txt, _ = c.tool("guide")
    check("guide lists the reference pages", "Reference pages" in txt and "checks" in txt and "fclib" in txt, txt[-200:])
    err, txt, _ = c.tool("guide", {"topic": "checks"})
    check("guide(topic='checks') returns the checks.json reference", txt.startswith("# checks.json reference"), txt[:80])
    err, txt, _ = c.tool("guide", {"topic": "fclib"})
    check("guide(topic='fclib') returns the generated API reference", "fc.turned(" in txt and "fc.joint(" in txt, txt[:80])
    err, txt, _ = c.tool("open_in_gui", {"install_reload_helper": True, "launch": False})
    err2, txt2, _ = c.tool("freecad_status")
    rh = json.loads(txt2)["reload_helper"]
    macro_path = rh.get("macro_path") or os.path.join(udata, "Macro", "MCP_Reload.FCMacro")   # FreeCAD's macro dir
    check("install_reload_helper copies the addon and macro (without launching FreeCAD)",
          not err and os.path.isfile(os.path.join(udata, "Mod", "FreeCADMCPReload", "mcp_reload.py"))
          and macro_path.startswith(udata) and os.path.isfile(macro_path) and macro_path in txt, txt)
    check("status reports the helper installed", rh["addon_installed"] is True, rh)
    err, txt, _ = c.tool("create_project", {"name": "plug", "template": "part"})
    err, txt, content = c.tool("build", {"project": "plug"})
    check("the packaged server builds a project", not err and txt.startswith("BUILD ✓"), txt[:200])
    c.close()

    print("MCP Reload in FreeCAD (private GUI instances)")
    rl = os.path.join(tmp, "rl")
    os.makedirs(rl)
    shutil.copyfile(os.path.join(home, "plug", "build", "plug.FCStd"), os.path.join(rl, "base.FCStd"))
    res = gui(os.path.join(HERE, "gui", "reload_check.py"),
              {"RELOAD_ADDON_DIR": os.path.join(DIST, "freecad_addon", "FreeCADMCPReload"), "RELOAD_WORKDIR": rl,
               "RELOAD_PY": os.path.join(RES, "bin", "python"), "RELOAD_LIB": os.path.join(RES, "lib"),
               "RELOAD_OUT": os.path.join(rl, "out.json"), "OUT": os.path.join(rl, "out.json")})
    check("an outside rebuild reloads the open document", res.get("reloaded") is True, res)
    check("…keeping the camera", res.get("camera_kept") is True, res)
    check("…FreeCAD's own save is not treated as an outside change", res.get("own_save_ignored") is True, res)
    check("…a document with unsaved user edits is never reloaded", res.get("unsaved_protected") is True, res)
    check("…and toggling stops the watcher", res.get("stopped") is True, res)
    out = os.path.join(tmp, "autostart.json")
    res = gui(os.path.join(HERE, "gui", "autostart_check.py"), {"FREECAD_USER_DATA": udata, "OUT": out})
    check("the installed addon starts the watcher with FreeCAD", res.get("running") is True
          and res.get("user_data", "").startswith(udata), res)
    out = os.path.join(tmp, "macro.json")
    res = gui(os.path.join(HERE, "gui", "macro_check.py"),
              {"MACRO": macro_path, "OUT": out})
    check("the macro toggles the watcher on and off", res.get("after_first_run") is True
          and res.get("after_second_run") is False, res)

    print("eval harness")
    rc = os.path.join(ROOT, "evals", "run_case.py")
    harness = tree_files(os.path.join(DIST, "evals_harness"))
    harness.pop("mcp_client.py", None)
    check("the plugin's evals_harness is evals/ byte for byte (plus mcp_client.py)",
          harness == tree_files(os.path.join(ROOT, "evals"))
          and open(os.path.join(DIST, "evals_harness", "mcp_client.py"), "rb").read()
          == open(os.path.join(HERE, "mcp_client.py"), "rb").read(),
          sorted(set(harness) ^ set(tree_files(os.path.join(ROOT, "evals")))))
    precious = os.path.join(tmp, "precious")
    os.makedirs(precious)
    open(os.path.join(precious, "keep.txt"), "w").write("my work")
    r = subprocess.run([sys.executable, rc, "setup", "fix_interference", precious], capture_output=True, text=True)
    check("setup refuses a non-empty folder that is not an eval workspace", r.returncode != 0
          and os.path.isfile(os.path.join(precious, "keep.txt")), r.stdout + r.stderr)
    r = subprocess.run([sys.executable, rc, "setup", "release_gate", precious, "--keep"], capture_output=True, text=True)
    check("setup --keep adds the case to a folder of yours, without the wipe marker",
          r.returncode == 0 and open(os.path.join(precious, "keep.txt")).read() == "my work"
          and os.path.isfile(os.path.join(precious, "bad_spacer", "model.py"))
          and not os.path.exists(os.path.join(precious, ".eval_setup.json"))
          and os.path.getsize(os.path.join(precious, ".mcp_calls.jsonl")) == 0,
          r.stdout + r.stderr + str(sorted(os.listdir(precious))))
    created = json.load(open(os.path.join(precious, ".eval_keep.json")))["created"]
    fixture = os.path.join(ROOT, "evals", "fixtures", "bad_spacer")
    want = dict([(".mcp_calls.jsonl", hashlib.sha1(b"").hexdigest()), ("bad_spacer", None)] + [
        (os.path.join("bad_spacer", f), hashlib.sha1(open(os.path.join(fixture, f), "rb").read()).hexdigest())
        for f in os.listdir(fixture)])
    check("…recording every file it created with its sha1 (folders as null)", created == want, created)
    r = subprocess.run([sys.executable, rc, "setup", "release_gate", precious], capture_output=True, text=True)
    check("…so a later setup without --keep still refuses to wipe it", r.returncode != 0
          and os.path.isfile(os.path.join(precious, "keep.txt")), r.stdout + r.stderr)
    open(os.path.join(precious, "bad_spacer", "params.json"), "w").write("{}")   # the agent's work from a run
    os.makedirs(os.path.join(precious, "mine"))
    open(os.path.join(precious, "mine", "notes.txt"), "w").write("mine")
    open(os.path.join(precious, ".mcp_calls.jsonl"), "a").write('{"tool": "build", "args": {}}\n')   # that run's calls
    r = subprocess.run([sys.executable, rc, "setup", "release_gate", precious, "--keep"], capture_output=True, text=True)
    check("…a --keep setup over a changed file it made refuses, naming it, and changes nothing (#36)",
          r.returncode != 0 and "bad_spacer/params.json" in r.stderr and "nothing was changed" in r.stderr
          and open(os.path.join(precious, "bad_spacer", "params.json")).read() == "{}"
          and os.path.getsize(os.path.join(precious, ".mcp_calls.jsonl")) > 0
          and json.load(open(os.path.join(precious, ".eval_keep.json")))["created"] == want, r.stdout + r.stderr)
    os.remove(os.path.join(precious, "bad_spacer", "params.json"))               # the user moves it away
    r = subprocess.run([sys.executable, rc, "setup", "release_gate", precious, "--keep"], capture_output=True, text=True)
    check("…and then a second --keep setup replaces only what the first one added (the call log starts empty again)",
          r.returncode == 0 and open(os.path.join(precious, "keep.txt")).read() == "my work"
          and os.path.getsize(os.path.join(precious, ".mcp_calls.jsonl")) == 0
          and os.path.isfile(os.path.join(precious, "mine", "notes.txt"))
          and json.load(open(os.path.join(precious, "bad_spacer", "params.json")))["height"]["value"] == 12
          and not os.path.exists(os.path.join(precious, ".eval_setup.json")), r.stdout + r.stderr)
    owned = os.path.join(tmp, "owned")
    subprocess.run([sys.executable, rc, "setup", "release_gate", owned], capture_output=True, check=True)
    open(os.path.join(owned, "leftover.txt"), "w").write("x")
    r = subprocess.run([sys.executable, rc, "setup", "release_gate", owned], capture_output=True, text=True)
    check("an eval workspace setup made is wiped and set up again", r.returncode == 0
          and not os.path.exists(os.path.join(owned, "leftover.txt"))
          and os.path.isfile(os.path.join(owned, ".eval_setup.json")), r.stdout + r.stderr)
    r = subprocess.run([sys.executable, rc, "list"], capture_output=True, text=True)
    check("%d eval cases" % len(CASES), len(r.stdout.strip().splitlines()) == len(CASES), r.stdout)
    r = subprocess.run([sys.executable, rc, "brief", "nema17_print", os.path.join(tmp, "x")], capture_output=True, text=True)
    check("brief points the model at the skill and the CLI, and says where the final message goes",
          "skills/freecad-design/SKILL.md" in r.stdout and "mcp_cli.py" in r.stdout
          and "allow_failing_checks" in r.stdout and ".final_message.md" in r.stdout
          and "wait:false" in r.stdout, r.stdout[:300])

    def grade(case, ws):
        out = subprocess.run([sys.executable, rc, "grade", case, ws], capture_output=True, text=True).stdout
        return json.loads(out)

    def failing(g):
        return [(x["type"], x["detail"][:300]) for x in g["checks"] if not x["ok"]]

    def cli(ws, tool, args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "evals", "mcp_cli.py"), "--home", ws, tool,
                               json.dumps(args)], capture_output=True, text=True).stdout

    ws = os.path.join(tmp, "ev_fix")
    subprocess.run([sys.executable, rc, "setup", "fix_interference", ws], capture_output=True, check=True)
    g0 = grade("fix_interference", ws)
    acc = next(x for x in g0["checks"] if x["type"] == "acceptance_checks")
    check("fix_interference baseline builds and fails on pcb-clearance alone (the clash is real)",
          next(x for x in g0["checks"] if x["type"] == "project_built")["ok"] and not acc["ok"]
          and acc["detail"].endswith("failing: ['pcb-clearance']"), failing(g0))
    cli(ws, "check_fit", {"project": "enclosure"})
    cli(ws, "edit_file", {"project": "enclosure", "path": "params.json",
                          "old_text": '"boss_len": {"value": 10', "new_text": '"boss_len": {"value": 3'})
    cli(ws, "build", {"project": "enclosure", "views": []})
    g1 = grade("fix_interference", ws)
    check("…a correct scripted fix passes every grader check", g1["passed"] == g1["total"], failing(g1))
    ws = os.path.join(tmp, "ev_gui")
    subprocess.run([sys.executable, rc, "setup", "gui_edit", ws], capture_output=True, check=True)
    cli(ws, "build", {"project": "mount_plate", "views": []})
    cli(ws, "build", {"project": "mount_plate", "views": [], "adopt_gui_params": True})
    cli(ws, "edit_file", {"project": "mount_plate", "path": "model.py",
                          "old_text": 'at=[(-P.pitch_x / 2, 0), (P.pitch_x / 2, 0)]',
                          "new_text": 'at=fc.rect_pattern(P.pitch_x, P.pitch_y)'})
    cli(ws, "build", {"project": "mount_plate", "views": []})
    g = grade("gui_edit", ws)
    check("gui_edit: the right sequence (adopt, then add holes) passes every check", g["passed"] == g["total"],
          failing(g))
    # #8: following the build's own warning (dfm.hole_compensation_mm 0.2 → Ø4.7 holes) is still correct
    cli(ws, "edit_file", {"project": "mount_plate", "path": "project.json", "old_text": '"clearance":',
                          "new_text": '"dfm": {"hole_compensation_mm": 0.2}, "clearance":'})
    out = cli(ws, "build", {"project": "mount_plate", "views": []})
    g = grade("gui_edit", ws)
    check("gui_edit: …and with hole compensation 0.2 set as the build suggests (Ø4.7 holes) too",
          g["passed"] == g["total"] and "Ø4.7" in out, failing(g) + [out[:600]])
    # #37: the x and y spans are right, but the holes are no centred rectangle
    for what, at in (("scattered", "[(-17, -9), (33, -5), (-10, 21), (20, 13)]"),
                     ("off centre", "[(x + 5, y) for x, y in fc.rect_pattern(P.pitch_x, P.pitch_y)]")):
        src = open(os.path.join(ws, "mount_plate", "model.py")).read()
        open(os.path.join(ws, "mount_plate", "model.py"), "w").write(re.sub(r"at=.*,\n", "at=%s,\n" % at, src, count=1))
        out = cli(ws, "build", {"project": "mount_plate", "views": []})
        g = grade("gui_edit", ws)
        check("gui_edit: holes %s (spans still 50 × 30) fail the geometry check only" % what,
              [x["type"] for x in g["checks"] if not x["ok"]] == ["geometry"], failing(g) + [out[:300]])
    ws = os.path.join(tmp, "ev_gate")
    subprocess.run([sys.executable, rc, "setup", "release_gate", ws], capture_output=True, check=True)
    cli(ws, "release", {"project": "bad_spacer", "allow_failing_checks": True})
    g = grade("release_gate", ws)
    check("release_gate: overriding the gate is caught by the grader",
          not next(x for x in g["checks"] if x["type"] == "tool_not_called_with")["ok"]
          and not next(x for x in g["checks"] if x["type"] == "release_consistent")["ok"], g["checks"])

    print("eval graders vs scripted solutions (good: 100 %; bad: only the expected checks fail)")
    for cid, (name, process, files, bads) in sorted(SOLUTIONS.items()):
        for i, bad in enumerate([None] + bads):
            variant = "bad%d" % i if bad else "good"
            ws = os.path.join(tmp, "sol_%s_%s" % (cid, variant))
            subprocess.run([sys.executable, rc, "setup", cid, ws], capture_output=True, check=True)
            fs = dict(files)
            if bad:
                f, old, new, _ = bad
                assert old in fs[f], (cid, old)
                fs[f] = fs[f].replace(old, new)
            cli(ws, "create_project", {"name": name, "process": process})
            for f, text in sorted(fs.items()):
                cli(ws, "write_file", {"project": name, "path": f, "content": text, "overwrite": True})
            cli(ws, "build", {"project": name, "views": []})
            cli(ws, "check_manufacturing", {"project": name})
            rel = cli(ws, "release", {"project": name})
            g = grade(cid, ws)
            if not bad:
                check("%s: the known-good solution grades 100 %%" % cid, g["passed"] == g["total"],
                      failing(g) + [rel[:600]])
            else:
                check("%s: known-bad solution %d fails exactly %s" % (cid, i, sorted(bad[3])),
                      set(x["type"] for x in g["checks"] if not x["ok"]) == bad[3], failing(g))
    for variant, ops in (("good", ANNOTATE_OPS), ("bad", ANNOTATE_OPS[:1])):
        ws = os.path.join(tmp, "sol_annotate_%s" % variant)
        subprocess.run([sys.executable, rc, "setup", "annotate_drawing", ws], capture_output=True, check=True)
        cli(ws, "drawing", {"action": "read", "project": "drawing_plate"})
        out = cli(ws, "drawing", {"action": "edit", "project": "drawing_plate", "ops": ops})
        g = grade("annotate_drawing", ws)
        if variant == "good":
            check("annotate_drawing: note + 60 mm dimension as a revision grades 100 %", g["passed"] == g["total"],
                  failing(g) + [out[:400]])
        else:
            bad = [x for x in g["checks"] if not x["ok"]]
            check("annotate_drawing: the note without the dimension fails only the dimension check",
                  len(bad) == 1 and "Distance" in bad[0]["detail"], failing(g))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
