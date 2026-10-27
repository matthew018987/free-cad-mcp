#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""server/freecad_mcp.py and server/launch.sh.

Part 1 runs the server module in-process with the FreeCAD workers replaced by fakes,
so argument validation, guide topics, setup hints, GUI detection, drawing options,
sheet bookkeeping and every release branch are checked exactly and quickly. Part 2
drives the real server over stdio with real FreeCAD (the drawing steps run the
FreeCAD GUI in the background for a few seconds each).

    python3 tests/test_server.py            # both parts
    python3 tests/test_server.py --unit     # part 1 only
"""
import base64
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
sys.path.insert(0, os.path.join(ROOT, "server"))
from mcp_client import Client, check, fails, images  # noqa: E402
import freecad_mcp as fm  # noqa: E402
import standards  # noqa: E402

ORIG = {k: getattr(fm, k) for k in ("run_worker", "run_gui_worker", "gui_pids", "find_freecad",
                                     "install_reload_helper", "fc_user_dirs", "gui_reload_hint")}


def restore():
    for k, v in ORIG.items():
        setattr(fm, k, v)


def raises(fn, *a, **kw):
    """The ToolError text fn raises, or None."""
    try:
        fn(*a, **kw)
    except fm.ToolError as e:
        return str(e)
    return None


def text_of(res):
    return res if isinstance(res, str) else "".join(c.get("text", "") for c in res if c.get("type") == "text")


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixE-"))
os.environ["FREECAD_MCP_HOME"] = tmp

# ======================================================================= part 1
try:
    print("argument validation")
    v = fm.TOOLS_BY_NAME
    e = raises(fm.validate_args, "render", v["render"]["inputSchema"], {"project": "p", "hidden_objects": ["A"]})
    check("unknown argument named, with the valid ones", e and "unknown argument 'hidden_objects' for render" in e
          and "valid: doc, project, views" in e and "Nothing was run" in e, e)
    e = raises(fm.validate_args, "drawing", v["drawing"]["inputSchema"], {"project": "p", "paper": "A0"})
    check("enum violation lists the allowed values", e and "paper must be one of auto, A4, A3, A2, A1" in e, e)
    e = raises(fm.validate_args, "release", v["release"]["inputSchema"], {"project": "p", "drawings": {"Plate": "x"}})
    check("wrong JSON type (release drawings={…})", e and "drawings must be string, got object" in e, e)
    e = raises(fm.validate_args, "write_file", v["write_file"]["inputSchema"], {"path": "a.md"})
    check("missing required argument", e and "missing required argument 'content' for write_file" in e, e)
    e = raises(fm.validate_args, "check_fit", v["check_fit"]["inputSchema"], {"contact_ok": [["A", 3]]})
    check("nested items checked", e and "contact_ok[0][1] must be string, got integer" in e, e)
    ok = fm.validate_args("measure", v["measure"]["inputSchema"], {"a": "X", "tol": 1, "timeout": 30.0, "b": None})
    check("integers accepted for number, integral floats for integer; null = not given",
          ok == {"a": "X", "tol": 1, "timeout": 30.0}, ok)
    ok = fm.validate_args("drawing", v["drawing"]["inputSchema"], {"scale": "1:2"}) and \
        fm.validate_args("drawing", v["drawing"]["inputSchema"], {"scale": 0.5})
    check("type lists: drawing scale accepts number or string", bool(ok))
    e = raises(fm.validate_args, "render", v["render"]["inputSchema"], {"views": ["sideways"]})
    check("anyOf: only the form of the value's own type is reported", e and
          "views[0] must be one of iso, front" in e and "must be object" not in e, e)
    e = raises(fm.validate_args, "render", v["render"]["inputSchema"], {"hiden": True})
    check("near misses get a suggestion", e and "did you mean 'hidden'?" in e, e)
    r = fm.call_tool("drawing", {"project": "p", "action": "create", "opps": []})
    check("call_tool returns the validation error before running anything", r.get("isError")
          and "unknown argument 'opps'" in r["content"][0]["text"], r)

    print("guide topics")
    g = fm.t_guide({"topic": "drawing"})
    check("'drawing' → the Document section (not the whole loop)", g.startswith("### ⑧ Document") and "② Requirements" not in g, g[:80])
    check("'release' → the Release section", fm.t_guide({"topic": "release"}).startswith("### ⑨ Release"))
    check("'verify' → the Verify section", fm.t_guide({"topic": "Verify"}).startswith("### ⑥ Verify"))
    check("section number '4' → §4", fm.t_guide({"topic": "4"}).startswith("## 4. Holes"))
    check("'1.6' → step ⑥ of the loop", fm.t_guide({"topic": "1.6"}).startswith("### ⑥ Verify"))
    check("reference page 'checks'", fm.t_guide({"topic": "checks"}).startswith("# checks.json reference"))
    e = raises(fm.t_guide, {"topic": "wibble"})
    check("unknown topic is an error listing the valid topics", e and "no guide topic 'wibble'" in e
          and "reference pages: checks, drawings, fclib, fea, processes, troubleshooting" in e and "⑧ Document" in e, e)

    print("FreeCAD-not-found guidance")
    real_platform = sys.platform
    try:
        fm.find_freecad = lambda: None
        sys.platform = "linux"
        st = json.loads(fm.t_freecad_status({}))
        hint = st.get("setup_hint", "")
        check("status on linux: says the server is not on the Mac and gives the config snippet",
              st["freecad"] == "NOT FOUND — see setup_hint" and "running on linux" in hint and "Cowork" in hint
              and '"command": "/bin/sh"' in hint and os.path.join(fm.HERE, "launch.sh") in hint
              and "FREECAD_MCP_HOME" in hint and "claude_desktop_config.json" in hint, hint)
        e = raises(fm.require_freecad)
        check("require_freecad gives the same guidance", e and "running on linux" in e and "launch.sh" in e, e)
        sys.platform = "darwin"
        st = json.loads(fm.t_freecad_status({}))
        check("status on macOS: install into /Applications or set FREECAD_APP",
              "/Applications" in st["setup_hint"] and "FREECAD_APP" in st["setup_hint"], st.get("setup_hint"))
    finally:
        sys.platform = real_platform
        restore()

    print("GUI detection")
    ps = ("  101 /Applications/FreeCAD.app/Contents/Resources/bin/freecad\n"
          "  102 /Applications/FreeCAD 1.1.app/Contents/MacOS/FreeCAD -psn_0_99\n"
          "  103 /Applications/FreeCAD.app/Contents/Resources/bin/freecad -u /Users/u/.cache/freecad-mcp/gui/user.cfg "
          "/x/server/drawing_worker.py\n"
          "  104 /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd -c pass\n"
          "  105 /Applications/FreeCAD.app/Contents/Resources/bin/python -I /x/server/freecad_mcp.py\n"
          "  106 /usr/bin/vim /Users/u/freecad\n"
          "  107 /usr/bin/freecad part.FCStd\n")
    check("ps parsing: FreeCAD 1.1 lower-case GUI, app launcher, linux binary; not our workers, cmd, editors",
          fm.gui_pids(ps) == ["101", "102", "107"], fm.gui_pids(ps))

    print("reload helper location and open_in_gui notes")
    ud = os.path.join(tmp, "ud")
    md = os.path.join(tmp, "macros")
    os.makedirs(ud)
    st = fm.reload_helper_state(ud, md)
    check("state looks for the macro in the macro folder", st["macro_path"] == os.path.join(md, "MCP_Reload.FCMacro")
          and st["macro_installed"] is False, st)
    try:
        fm.fc_user_dirs = lambda: {"user_data": ud, "macro_dir": md}
        dst, macro = fm.install_reload_helper()
        check("install puts the macro into macro_dir and the addon into Mod",
              macro == os.path.join(md, "MCP_Reload.FCMacro") and os.path.isfile(macro)
              and os.path.isfile(os.path.join(ud, "Mod", "FreeCADMCPReload", "mcp_reload.py")), (dst, macro))
        fm.install_reload_helper = lambda: ("ADDON", "MACRO")
        fm.find_freecad = lambda: {"kind": "python", "exe": "x", "lib": "", "app": None, "gui": None}
        fm.gui_pids = lambda ps_text=None: ["1"]
        t = fm.t_open_in_gui({"install_reload_helper": True, "launch": False})
        check("already installed + FreeCAD running → check the Report view for 'MCP Reload: watching'; the toggle "
              "macro only if it is absent (round 4 #51)", "Report view" in t and "'MCP Reload: watching'" in t
              and "do nothing" in t and "toggle" in t and "Only if it is absent" in t and "updated" in t, t)
        fm.fc_user_dirs = lambda: {"user_data": os.path.join(tmp, "ud_fresh"), "macro_dir": md}
        t = fm.t_open_in_gui({"install_reload_helper": True, "launch": False})
        check("FreeCAD already running, helper new → run the macro once (it prints 'MCP Reload: watching') or restart",
              "Macro ▸ Macros… ▸ MCP_Reload once" in t and "MCP Reload: watching" in t and "Report view" not in t
              and "installed:" in t, t)
        fm.gui_pids = lambda ps_text=None: []
        t = fm.t_open_in_gui({"install_reload_helper": True, "launch": False})
        check("not running → starts with FreeCAD, no macro step", "starts with FreeCAD" in t and "Macros…" not in t, t)
    finally:
        restore()

    print("home_dir")
    for raw, want in (("", "~/CAD"), ("${user_config.projects_dir}", "~/CAD"), ("  ", "~/CAD"),
                      ("$HOME/CAD2", "~/CAD2"), ("~/X", "~/X")):
        os.environ["FREECAD_MCP_HOME"] = raw
        check("FREECAD_MCP_HOME=%r → %s" % (raw, want), fm.home_dir() == os.path.expanduser(want), fm.home_dir())
    os.environ["FREECAD_MCP_HOME"] = tmp

    print("launch.sh")
    T = os.path.join(tmp, "launch")
    for app, ver in (("FreeCAD 0.21.app", "0.21.2"), ("FreeCAD_1.1.app", "1.1.4"), ("FreeCAD 1.0.app", "1.0.2")):
        b = os.path.join(T, "A", app, "Contents", "Resources", "bin")
        os.makedirs(b)
        with open(os.path.join(T, "A", app, "Contents", "Info.plist"), "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>CFBundleVersion</key>'
                     '<string>%s</string></dict></plist>\n' % ver)
        with open(os.path.join(b, "python"), "w") as fh:
            fh.write('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho "PY=$0|APP=$FREECAD_APP|ARGS=$*"\n')
        os.chmod(os.path.join(b, "python"), 0o755)
    os.makedirs(os.path.join(T, "H", "Applications"))
    os.makedirs(os.path.join(T, "bin"))
    src = open(os.path.join(ROOT, "server", "launch.sh")).read()
    check("launch.sh searches /Applications then ~/Applications", "bundles /Applications;" in src
          and '"$HOME/Applications"' in src)
    src = src.replace("bundles /Applications;", "bundles '%s/A';" % T).replace('"$HOME/Applications"', '"%s/H/Applications"' % T)
    ls = os.path.join(T, "bin", "launch.sh")
    with open(ls, "w") as fh:
        fh.write(src)
    r = subprocess.run(["/bin/sh", ls, "a1"], env={"PATH": "/usr/bin:/bin", "HOME": T + "/H",
                                                   "FREECAD_PYTHON": "/no/such/python"}, capture_output=True, text=True)
    check("a non-working FREECAD_PYTHON is skipped; the newest bundle wins; FREECAD_APP exported",
          r.returncode == 0 and "FreeCAD_1.1.app/Contents/Resources/bin/python|APP=%s/A/FreeCAD_1.1.app|" % T in r.stdout
          and r.stdout.strip().endswith("freecad_mcp.py a1"), r.stdout + r.stderr)
    print("launch.sh keeps a user-set FREECAD_APP (round 4 #35)")
    b10 = os.path.join(T, "A", "FreeCAD 1.0.app")
    cmd_out = os.path.join(T, "opt", "bin", "freecadcmd")
    os.makedirs(os.path.dirname(cmd_out))
    for f in (cmd_out, os.path.join(b10, "Contents", "Resources", "bin", "freecadcmd")):
        open(f, "w").write("#!/bin/sh\nexit 0\n")
        os.chmod(f, 0o755)

    def launch(app):
        r = subprocess.run(["/bin/sh", ls], env={"PATH": "/usr/bin:/bin", "HOME": T + "/H", "FREECAD_APP": app},
                           capture_output=True, text=True)
        m = re.search(r"PY=(.*?)\|APP=(.*?)\|", r.stdout)
        return (m.group(1), m.group(2)) if m else (None, r.stdout + r.stderr)

    py10 = os.path.join(b10, "Contents", "Resources", "bin", "python")
    got = launch(py10)
    check("FREECAD_APP = an older bundle's python: that python runs the server and FREECAD_APP is kept",
          got == (py10, py10), got)
    got = launch(os.path.join(b10, "Contents", "Resources", "bin", "freecadcmd"))
    check("FREECAD_APP = a bundle's freecadcmd: the bundle's python runs the server, FREECAD_APP is kept",
          got == (py10, os.path.join(b10, "Contents", "Resources", "bin", "freecadcmd")), got)
    got = launch(cmd_out)
    check("FREECAD_APP = a freecadcmd outside a bundle: kept for the workers (a bundle's python runs the server)",
          got[1] == cmd_out and got[0] and got[0].endswith("FreeCAD_1.1.app/Contents/Resources/bin/python"), got)
    got = launch(b10 + "/")
    check("FREECAD_APP with a trailing '/' (tab completion): that bundle, exported without the slash",
          got == (py10, b10), got)
    shutil.rmtree(os.path.join(T, "A"))
    r = subprocess.run(["/bin/sh", ls], env={"PATH": "/usr/bin:/bin", "HOME": T + "/H", "FREECAD_PYTHON": "/no/such/python"},
                       capture_output=True, text=True)
    check("nothing works → clear message, exit 1, /usr/bin/python3 never tried on macOS", r.returncode == 1
          and "install FreeCAD 1.x" in r.stderr.replace("Install", "install") and "FREECAD_PYTHON" in r.stderr
          and "/no/such/python (not found)" in r.stderr and "/usr/bin/python3" not in r.stderr, r.stderr)
    py = shutil.which("python3", path="/opt/homebrew/bin:/usr/local/bin")
    if py and sys.platform == "darwin":
        with open(os.path.join(T, "bin", "freecad_mcp.py"), "w") as fh:
            fh.write("import sys\nprint('RAN', sys.executable)\n")
        r = subprocess.run(["/bin/sh", ls], env={"PATH": "/usr/bin:%s" % os.path.dirname(py), "HOME": T + "/H"},
                           capture_output=True, text=True, timeout=60)
        check("no FreeCAD: a python3 later on PATH is used (macOS's /usr/bin/python3 skipped)",
              r.returncode == 0 and r.stdout.startswith("RAN ") and "/usr/bin/python" not in r.stdout,
              r.stdout + r.stderr[-300:])
    r = subprocess.run(["/bin/sh", os.path.join(ROOT, "server", "launch.sh")], input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                       env=dict(os.environ, FREECAD_PYTHON="/no/such/python"), capture_output=True, text=True, timeout=60)
    check("the real launch.sh starts the server with a bad FREECAD_PYTHON", '"id": 1, "result": {}' in r.stdout, r.stdout + r.stderr[-300:])

    print("find_freecad order")
    H = os.path.join(tmp, "fakehome")
    for app, ver in (("FreeCAD 0.21.app", "0.21.2"), ("FreeCAD_1.1.app", "1.1.4"), ("FreeCAD 1.0.app", "1.0.2")):
        c = os.path.join(H, "Applications", app, "Contents")
        os.makedirs(os.path.join(c, "Resources", "bin"))
        os.makedirs(os.path.join(c, "Resources", "lib"))
        with open(os.path.join(c, "Info.plist"), "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>'
                     'CFBundleShortVersionString</key><string>%s</string></dict></plist>\n' % ver)
        for f in (("Resources", "bin", "python"), ("Resources", "lib", "FreeCAD.so")):
            open(os.path.join(c, *f), "w").close()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = H
    try:
        bl = fm.freecad_bundles()
        mine = [os.path.basename(b) for b in bl if b.startswith(H)]
        check("~/Applications bundles newest first (1.x before 0.x)",
              mine == ["FreeCAD_1.1.app", "FreeCAD 1.0.app", "FreeCAD 0.21.app"], bl)
        if os.path.isdir("/Applications/FreeCAD.app"):
            check("/Applications/FreeCAD.app comes first", bl[0] == "/Applications/FreeCAD.app", bl)
        os.environ["FREECAD_APP"] = os.path.join(H, "Applications", "FreeCAD 0.21.app")
        check("FREECAD_APP is honoured first", fm.find_freecad()["app"] == os.environ["FREECAD_APP"])
        b21 = os.path.join(H, "Applications", "FreeCAD 0.21.app")
        os.environ["FREECAD_APP"] = b21 + "/"
        check("FREECAD_APP with a trailing '/' is honoured (round 4 #35)", fm.find_freecad()["app"] == b21,
              fm.find_freecad())
        os.environ["FREECAD_APP"] = os.path.join(b21, "Contents", "Resources", "bin", "python")
        fi = fm.find_freecad()
        check("FREECAD_APP = a bundle's python: that python, and the same bundle's GUI (not another FreeCAD)",
              fi["exe"] == os.environ["FREECAD_APP"] and fi["app"] == b21
              and fi["gui"] == os.path.join(b21, "Contents", "Resources", "bin", "freecad"), fi)
    finally:
        os.environ.pop("FREECAD_APP", None)
        os.environ["HOME"] = old_home

    print("drawing options: merge order and persistence")
    P = os.path.join(tmp, "dproj")
    os.makedirs(os.path.join(P, "drawings"))
    json.dump({"name": "dproj", "process": "fdm", "drawing": {"paper": "auto"},
               "parts": {"Plate": {"process": "cnc", "material": "6061", "finish": "anodised"}}},
              open(os.path.join(P, "project.json"), "w"))
    json.dump({"*": {"notes": ["ALL"], "scale": 2, "ops": [{"op": "x"}]},
               "Plate": {"notes": ["PLATE"], "ops": [{"op": "add_note", "text": "N"}]}},
              open(os.path.join(P, "drawings.json"), "w"))
    sh = fm.drawing_sheets(P, ["Plate"], {"paper": "A3"})
    check("project.json parts → \"*\" → part → overrides", sh == [{"process": "cnc", "material": "6061",
                                                                 "finish": "anodised", "notes": ["PLATE"], "scale": 2,
                                                                 "ops": [{"op": "add_note", "text": "N"}],
                                                                 "paper": "A3", "part": "Plate"}], sh)
    sh = fm.drawing_sheets(P, ["Plate"], {"scale": "auto"})
    check("\"auto\" scale/paper leave the choice to the worker", "scale" not in sh[0] and "paper" not in sh[0], sh)
    sh = fm.drawing_sheets(P, None, None)
    check("without a part: \"*\" options, never ops", sh == [{"notes": ["ALL"], "scale": 2}], sh)
    sh = fm.drawing_sheets(P, ["Plate"], {"notes": ["NEW"]}, reset=True)
    check("reset skips the part's saved options (\"*\" still applies)", sh[0].get("notes") == ["NEW"]
          and "ops" not in sh[0] and sh[0].get("scale") == 2, sh)
    before = open(os.path.join(P, "drawings.json")).read()
    fm.save_drawing_options(P, ["Plate"], {"paper": "A3"}, False)
    saved = json.load(open(os.path.join(P, "drawings.json")))
    check("only the keys the caller passed are persisted (no project.json defaults)",
          saved["Plate"] == {"notes": ["PLATE"], "ops": [{"op": "add_note", "text": "N"}], "paper": "A3"}
          and "process" not in saved["Plate"], saved)
    fm.save_drawing_options(P, ["Plate"], {"notes": ["ONLY"]}, True)
    check("reset clears the part's saved options first", json.load(open(os.path.join(P, "drawings.json")))["Plate"]
          == {"notes": ["ONLY"]})
    fm.save_drawing_options(P, None, {"title_fields": {"creator": "Q"}}, False)
    check("options without a part go under \"*\"", json.load(open(os.path.join(P, "drawings.json")))["*"]["title_fields"]
          == {"creator": "Q"})
    open(os.path.join(P, "drawings.json"), "w").write(before)
    json.dump({"objects": [{"label": "Plate"}]}, open(os.path.join(P, "x.json"), "w"))
    e = raises(fm.t_drawing, {"project": P, "ops": [{"op": "add_note", "text": "x"}]})
    check("create ops without exactly one part → error", e and "ops belong to one part's sheet" in e, e)
    calls = []

    def fake_gui(op, args, timeout=300, request_id=None):
        calls.append((op, json.loads(json.dumps(args))))
        if op == "create":
            out = []
            for s in args["sheets"]:
                stem = fm._sheet_stem(s["part"])
                files = {}
                for k in ("pdf", "png"):
                    files[k] = os.path.join(args["out_dir"], "%s.%s" % (stem, k))
                    os.makedirs(args["out_dir"], exist_ok=True)
                    open(files[k], "w").write("%s %s" % (k, s))
                rec = {"part": s["part"], "files": files, "dims": [{"ok": True}], "paper": "A4", "scale": "1:1",
                       "warnings": []}
                if args.get("save_dir"):
                    os.makedirs(args["save_dir"], exist_ok=True)
                    rec["document"] = os.path.join(args["save_dir"], stem + ".FCStd")
                    open(rec["document"], "w").write("sheet %s %s" % (s["part"], time.time()))
                out.append(rec)
            return {"ok": True, "text": "SHEETS", "sheets": out, "images": []}
        if op == "edit":
            open(args["save_as"], "w").write("edited " + json.dumps(args["ops"]))
            return {"ok": True, "text": "EDIT", "images": []}
        if op == "read":
            return {"ok": True, "text": "PAGE", "images": []}
        if op == "sheet_status":
            return {"ok": True, "text": "", "sheets": [{"part": s["part"], "doc": s["doc"],
                                                        "current": STATUS.get(s["part"], True), "detail": "volume changed",
                                                        "revision": "r0001"} for s in args["sheets"]]}
        if op == "export":
            os.makedirs(args["out_dir"], exist_ok=True)
            files = {k: os.path.join(args["out_dir"], "%s.%s" % (args["name"], k)) for k in ("pdf", "png", "svg", "dxf")}
            for f in files.values():
                open(f, "w").write("exported")
            return {"ok": True, "text": "EXPORT", "files": files}
        return {"ok": False, "error": "unknown op %s" % op}

    STATUS = {}
    fm.run_gui_worker = fake_gui
    try:
        os.makedirs(os.path.join(P, "build"))
        open(os.path.join(P, "build", "dproj.FCStd"), "w").write("model")
        os.makedirs(os.path.join(P, ".history", "r0002"))
        json.dump({"variant": None, "objects": [{"label": "Plate", "role": "part", "solids": 1},
                                                {"label": "Motor", "role": "ref", "solids": 1}]},
                  open(os.path.join(P, ".history", "r0002", "report.json"), "w"))
        res = fm.t_drawing({"project": P, "notes": ["DEBURR"]})
        op, a = calls[-1]
        check("create without part draws each built part (refs skipped) so its saved options apply",
              op == "create" and [s.get("part") for s in a["sheets"]] == ["Plate"] and a["sheets"][0]["notes"] == ["DEBURR"]
              and a["sheets"][0]["ops"] == [{"op": "add_note", "text": "N"}], a["sheets"])
        dj = json.load(open(os.path.join(P, "drawings.json")))
        check("after success: \"*\" gets notes; the sheet document is recorded in _sheets (I9)",
              dj["*"]["notes"] == ["DEBURR"] and dj["_sheets"]["Plate"]["file"] == "drawings/Plate.FCStd"
              and dj["_sheets"]["Plate"]["rev"] == "r0002" and len(dj["_sheets"]["Plate"]["sha1"]) == 40, dj)
        check("…and the reply says what was saved", "saved    drawings.json \"*\" (every part): notes" in text_of(res), text_of(res))
        n = len(calls)
        fm.run_gui_worker = lambda *a, **k: {"ok": False, "error": "boom"}
        e = raises(fm.t_drawing, {"project": P, "part": "Plate", "paper": "A2"})
        check("a failed create persists nothing", e and "boom" in e and "paper" not in
              json.load(open(os.path.join(P, "drawings.json"))).get("Plate", {}), e)
        fm.run_gui_worker = fake_gui

        print("drawing read/edit defaults, revisions")
        res = fm.t_drawing({"action": "read", "project": P})
        op, a = calls[-1]
        check("read with project only → the newest sheet; previews in .history/previews",
              op == "read" and a["doc"] == os.path.join(P, "drawings", "Plate.FCStd")
              and a["out_dir"] == os.path.join(P, ".history", "previews") and "drawings/Plate.FCStd (newest" in text_of(res),
              (a, text_of(res)))
        res = fm.t_drawing({"action": "edit", "project": P, "ops": [{"op": "add_note", "text": "A"}]})
        op, a = calls[-1]
        check("edit → reserves drawings/Plate.r01.FCStd and exports under that name",
              a["save_as"] == os.path.join(P, "drawings", "Plate.r01.FCStd") and a["export_name"] == "Plate.r01"
              and "saved    drawings/Plate.r01.FCStd (new revision" in text_of(res), (a, text_of(res)))
        e = raises(fm.t_drawing, {"action": "edit", "project": P, "doc": "drawings/Plate.FCStd",
                                  "ops": [{"op": "add_note", "text": "B"}]})
        check("editing an older revision is refused, naming the newest", e and "not the newest revision" in e
              and "drawings/Plate.r01.FCStd" in e and "from_older=true" in e, e)
        fm.t_drawing({"action": "edit", "project": P, "doc": "drawings/Plate.FCStd", "from_older": True,
                      "ops": [{"op": "add_note", "text": "B"}]})
        check("from_older forks a new revision after the newest", calls[-1][1]["save_as"].endswith("Plate.r02.FCStd"))
        r1 = fm.reserve_revision(os.path.join(P, "drawings", "Plate.FCStd"))
        r2 = fm.reserve_revision(os.path.join(P, "drawings", "Plate.r02.FCStd"))
        check("revision names are reserved atomically (two reservations never collide)",
              r1.endswith("Plate.r03.FCStd") and r2.endswith("Plate.r04.FCStd") and os.path.isfile(r1), (r1, r2))
        os.remove(r1)
        os.remove(r2)
        fm.run_gui_worker = lambda *a, **k: {"ok": False, "error": "edit failed"}
        raises(fm.t_drawing, {"action": "edit", "project": P, "ops": [{"op": "add_note", "text": "C"}]})
        check("a failed edit removes its placeholder", not os.path.exists(os.path.join(P, "drawings", "Plate.r03.FCStd")))
        fm.run_gui_worker = fake_gui
        res = fm.t_drawing({"action": "edit", "project": P, "save": "inplace", "ops": [{"op": "add_note", "text": "D"}]})
        bk = os.listdir(os.path.join(P, ".history", "backups", "drawings"))
        check("save='inplace' backs up into .history/backups/drawings", any(re.match(r"Plate\.r02\.FCStd\.\d{8}-\d{6}\.bak", b)
                                                                          for b in bk) and "backed up to .history/backups/drawings/" in text_of(res), (bk, text_of(res)))
        os.makedirs(os.path.join(P, "drawings2"))
        e = raises(fm.t_drawing, {"action": "read", "project": P, "part": "Nope"})
        check("read part without sheets → error saying how to make one", e and "no drawing of Nope" in e
              and "action=\"create\"" in e, e)
        open(os.path.join(P, "drawings", "Bracket.FCStd"), "w").write("x")
        e = raises(fm.t_drawing, {"action": "read", "project": P})
        check("several parts with sheets → error listing them", e and "Bracket" in e and "Plate (newest Plate.r02.FCStd)" in e, e)
        os.remove(os.path.join(P, "drawings", "Bracket.FCStd"))
        res = fm.t_drawing({"project": P, "part": "Plate"})
        moved = [b for b in os.listdir(os.path.join(P, ".history", "backups", "drawings")) if b.startswith("Plate.r0")]
        check("create regenerates and sets the hand-edited revisions aside (newest = the new sheet)",
              fm.newest_sheet(os.path.join(P, "drawings"), "Plate") == os.path.join(P, "drawings", "Plate.FCStd")
              and "moved    drawings/Plate.r01.FCStd" in text_of(res) and len(moved) >= 3, (moved, text_of(res)))
        check("…and the regenerated sheet counts as not edited",
              not fm.sheet_edited(P, "Plate", os.path.join(P, "drawings", "Plate.FCStd")))
    finally:
        restore()

    print("release (fake workers)")
    R = os.path.join(tmp, "rproj")
    os.makedirs(os.path.join(R, "build"))
    json.dump({"name": "rproj", "process": "cnc", "parts": {"Plate": {"process": "cnc", "qty": 2}}},
              open(os.path.join(R, "project.json"), "w"))
    json.dump([{"id": "a", "type": "valid", "object": "Plate"}], open(os.path.join(R, "checks.json"), "w"))
    open(os.path.join(R, "model.py"), "w").write("# model\n")
    open(os.path.join(R, "build", "rproj.FCStd"), "w").write("model")
    W = {"rev": "r0003", "inputs": "in-1", "unknown": {}, "export_ok": True, "finish_args": None}

    def fake_worker(op, args, timeout=300, request_id=None, job_key=None):
        if op == "build":
            return {"ok": True, "text": "BUILD ✓  rproj  %s\nchecks   (checks.json)\n  ✓ a        valid   ok" % W["rev"],
                    "data": {"ok": True, "rev": W["rev"], "fcstd": os.path.join(R, "build", "rproj.FCStd"), "checks": 1,
                             "checks_passed": 1, "inputs_sha1": W["inputs"]}}
        if op == "release_prepare":
            d = os.path.join(args["release_dir"], "Plate")
            os.makedirs(d)
            f = os.path.join(d, "Plate.step")
            open(f, "w").write("step")
            return {"ok": True, "text": "", "data": {
                "ok": W["export_ok"], "labels": ["Plate", "Motor"], "unknown_keys": W["unknown"],
                "parts": [{"part": "Plate", "process": "cnc", "material": "6061", "finish": "", "qty": 2,
                           "files": [f], "dfm": {"findings": []}, "drawing": None}],
                "refs": [{"part": "Motor", "bom": "buy", "qty": 2, "spec": "NEMA17", "size": [42, 42, 40]},
                         {"part": "Frame", "bom": "context", "qty": 1, "spec": None, "size": [1, 1, 1]}],
                "exports": [{"path": f, "format": "step", "ok": W["export_ok"], "check": "volume Δ 0.0000 %"
                             if W["export_ok"] else "volume Δ 3.2 % ✗"}]}}
        if op == "release_finish":
            W["finish_args"] = args
            rel = args["release_dir"]
            open(os.path.join(rel, "README.md"), "w").write("readme")
            z = rel + ".zip"
            with zipfile.ZipFile(z, "w") as zf:
                for root, _, files in os.walk(rel):
                    for fn in files:
                        zf.write(os.path.join(root, fn), os.path.join(os.path.basename(rel),
                                                                      os.path.relpath(os.path.join(root, fn), rel)))
            return {"ok": True, "text": z, "data": {"zip": z}}
        return {"ok": False, "error": "unexpected op %s" % op}

    fm.run_worker = fake_worker
    fm.run_gui_worker = fake_gui
    try:
        del calls[:]
        res = fm.t_release({"project": R})
        t = text_of(res)
        rel = os.path.join(R, "release", "rproj_r0003")
        names = zipfile.ZipFile(rel + ".zip").namelist()
        check("no sheet yet → generated, saved to drawings/, recorded; PDF shipped (no DXF)",
              "drawing Plate          generated and saved drawings/Plate.FCStd (no sheet yet)" in t
              and os.path.isfile(os.path.join(R, "drawings", "Plate.FCStd"))
              and "Plate" in json.load(open(os.path.join(R, "drawings.json")))["_sheets"]
              and "rproj_r0003/Plate/Plate_drawing.pdf" in names and not any(n.endswith(".dxf") for n in names), (t, names))
        check("the drawing preview image is returned", len(images(res)) == 1)
        check("part record carries the drawing path and [verified, total] dimensions for the notes",
              W["finish_args"]["parts"][0]["drawing"].endswith("Plate/Plate_drawing.pdf")
              and W["finish_args"]["parts"][0]["drawing_dims"] == [1, 1], W["finish_args"]["parts"][0])
        check("…and _sheets records the verified count with the sheet", json.load(open(os.path.join(
            R, "drawings.json")))["_sheets"]["Plate"]["dims"] == 1)
        meta = json.load(open(os.path.join(rel, ".release.json")))
        check(".release.json written (rev, inputs, checks) and kept out of the zip",
              meta["rev"] == "r0003" and meta["inputs_sha1"] == "in-1" and meta["checks"][0]["id"] == "a"
              and not any(n.endswith(".release.json") for n in names), meta)
        check("purchased vs reference parts listed", "purchased: Motor ×2" in t
              and "reference parts (used for checks, not supplied): Frame" in t, t)
        check("staging cleaned up", not [d for d in os.listdir(os.path.join(R, "release")) if d.startswith(".staging")])

        del calls[:]
        res = fm.t_release({"project": R})
        t = text_of(res)
        ex = [a for op, a in calls if op == "export"]
        check("same inputs → the earlier package is replaced, and says so", "replaced the earlier package rproj_r0003" in t
              and not os.path.exists(os.path.join(R, "release", "rproj_r0003-2")), t)
        check("current saved sheet → shipped AS IS (no restamping), its sheet revision reported",
              "shipped drawings/Plate.FCStd (sheet rev r0001, current)" in t and ex and "title_fields" not in ex[0]
              and not [1 for op, _ in calls if op == "create"], (t, calls))
        check("…with the dimension counts recorded when it was created",
              W["finish_args"]["parts"][0]["drawing_dims"] == [1, 1], W["finish_args"]["parts"][0])

        W["inputs"] = "in-2"
        json.dump([{"id": "a", "type": "valid", "object": "Plate"}, {"id": "b", "type": "mass", "object": "Plate"}],
                  open(os.path.join(R, "checks.json"), "w"))
        res = fm.t_release({"project": R})
        t = text_of(res)
        check("same rev, different inputs → <name>_<rev>-2 with a warning; the old package untouched",
              "rproj_r0003-2" in t and ".history was probably reset" in t
              and os.path.isfile(os.path.join(R, "release", "rproj_r0003-2.zip"))
              and json.load(open(os.path.join(rel, ".release.json")))["inputs_sha1"] == "in-1", t)
        check("checks changed since the previous release: in the output and passed to the notes",
              "checks changed since r0003: added b" in t and W["finish_args"]["checks_changes"] ==
              {"since": "r0003", "added": ["b"], "removed": [], "changed": []}, (t, W["finish_args"].get("checks_changes")))

        W["rev"] = "r0004"
        STATUS["Plate"] = False
        del calls[:]
        res = fm.t_release({"project": R})
        t = text_of(res)
        check("stale, not edited → regenerated with saved options, saved back to drawings/",
              "regenerated — drawings/Plate.FCStd was out of date (volume changed); saved drawings/Plate.FCStd" in t
              and [a for op, a in calls if op == "create"][0].get("save_dir") == os.path.join(R, "drawings"), t)

        W["rev"] = "r0005"
        open(os.path.join(R, "drawings", "Plate.r01.FCStd"), "w").write("hand edited")
        e = raises(fm.t_release, {"project": R})
        check("stale and edited by hand → STOP naming the sheet and both ways forward", e and "RELEASE STOPPED" in e
              and "Plate: drawings/Plate.r01.FCStd (a revision saved by drawing edit)" in e and "drawing(action=\"create\"" in e and "drawings=\"generate\"" in e, e)
        check("…and nothing is left behind", not os.path.exists(os.path.join(R, "release", "rproj_r0005"))
              and not [d for d in os.listdir(os.path.join(R, "release")) if d.startswith(".staging")])
        del calls[:]
        res = fm.t_release({"project": R, "drawings": "generate"})
        t = text_of(res)
        cr = [a for op, a in calls if op == "create"]
        check("drawings=generate ships a fresh sheet and leaves the edited one untouched",
              "generated for this release (drawings=\"generate\"); drawings/Plate.r01.FCStd has hand edits" in t
              and cr and "save_dir" not in cr[0] and open(os.path.join(R, "drawings", "Plate.r01.FCStd")).read() == "hand edited", t)
        STATUS["Plate"] = True
        W["rev"] = "r0006"
        res = fm.t_release({"project": R})
        check("current hand-edited revision → shipped as is",
              "shipped drawings/Plate.r01.FCStd (sheet rev r0001, current; a revision saved by drawing edit)"
              in text_of(res) and W["finish_args"]["parts"][0]["drawing_dims"] is None, text_of(res))
        res = fm.t_release({"project": R, "drawings": "none"})
        check("drawings=none → no drawing", "drawing Plate" not in text_of(res), text_of(res))

        W["rev"], W["export_ok"] = "r0007", False
        e = raises(fm.t_release, {"project": R, "drawings": "none"})
        check("an export that fails verification stops the release", e and "failed verification" in e
              and "allow_failed_exports=true" in e and "Plate.step — volume Δ 3.2 % ✗" in e, e)
        res = fm.t_release({"project": R, "drawings": "none", "allow_failed_exports": True})
        check("…unless allow_failed_exports, which is reported", "⚠ shipped with 1 export(s) that failed verification"
              in text_of(res), text_of(res))
        W["rev"], W["export_ok"], W["unknown"] = "r0008", True, {"parts": ["Bracket2"], "qty": [], "processes": []}
        e = raises(fm.t_release, {"project": R, "drawings": "none"})
        check("parts keys matching no object stop the release, listing the valid labels", e
              and "project.json \"parts\": Bracket2" in e and "Valid part labels: Plate, Motor" in e, e)
    finally:
        restore()

    print("files")
    os.makedirs(os.path.join(tmp, "fproj", "refs"))
    json.dump({"name": "fproj"}, open(os.path.join(tmp, "fproj", "project.json"), "w"))
    blob = bytes(range(256)) * 10
    out = fm.t_write_file({"project": "fproj", "path": "refs/vendor.step", "encoding": "base64",
                           "content": base64.b64encode(blob).decode()})
    check("base64 write of a binary reference file", open(os.path.join(tmp, "fproj", "refs", "vendor.step"), "rb").read() == blob, out)
    e = raises(fm.t_write_file, {"project": "fproj", "path": "refs/x.step", "content": "ISO-10303"})
    check("a binary type without encoding=base64 is refused with the fix", e and "refusing" in e and "base64" in e, e)
    e = raises(fm.t_write_file, {"project": "fproj", "path": "refs/x.pdf", "encoding": "base64", "content": "@@@"})
    check("invalid base64 is refused", e and "not valid base64" in e, e)
    e = raises(fm.t_write_file, {"project": "fproj", "path": "refs/x.exe", "encoding": "base64", "content": "AAAA"})
    check("other binary types are refused", e and "reference files only" in e, e)
    e = raises(fm.t_write_file, {"project": "fproj", "path": "build/fproj.FCStd", "encoding": "base64", "content": "AAAA"})
    check("base64 writes into generated folders (build/) are refused, pointing at refs/", e
          and "refusing to write into build/" in e and "refs/fproj.FCStd" in e
          and not os.path.exists(os.path.join(tmp, "fproj", "build")), e)
    fm.MAX_BINARY, mb = 100, fm.MAX_BINARY
    e = raises(fm.t_write_file, {"project": "fproj", "path": "refs/big.stl", "encoding": "base64",
                                 "content": base64.b64encode(b"x" * 200).decode()})
    fm.MAX_BINARY = mb
    check("size limit", e and "the limit is" in e, e)
    fm.t_write_file({"project": "fproj", "path": "notes.md", "content": "v1"})
    o1 = fm.t_write_file({"project": "fproj", "path": "notes.md", "content": "v2", "overwrite": True})
    o2 = fm.t_edit_file({"project": "fproj", "path": "notes.md", "old_text": "v2", "new_text": "v3"})
    bk = sorted(os.listdir(os.path.join(tmp, "fproj", ".history", "backups")))
    check("backups: every version kept in .history/backups/<relpath>.<timestamp>.bak, path reported",
          len(bk) == 2 and all(re.match(r"notes\.md\.\d{8}-\d{6}(-\d+)?\.bak$", b) for b in bk)
          and "backed up to .history/backups/notes.md." in o1 and "backup .history/backups/notes.md." in o2
          and not os.path.exists(os.path.join(tmp, "fproj", "notes.md.bak")), (bk, o1, o2))
    contents = sorted(open(os.path.join(tmp, "fproj", ".history", "backups", b)).read() for b in bk)
    check("…with the previous contents", contents == ["v1", "v2"], contents)
    loose = os.path.join(tmp, "loose.md")
    open(loose, "w").write("a")
    fm.t_write_file({"path": loose, "content": "b", "overwrite": True})
    check("outside a project: <file>.bak as before", open(loose + ".bak").read() == "a")
    t = fm.t_read_file({"project": "fproj", "path": "fproj/notes.md"})
    check("project-prefixed path is resolved inside the project", t.startswith(os.path.join(tmp, "fproj", "notes.md")), t)
    fm.t_write_file({"project": "fproj", "path": "fproj/refs/n.txt", "content": "x"})
    check("…for writes too", os.path.isfile(os.path.join(tmp, "fproj", "refs", "n.txt"))
          and not os.path.exists(os.path.join(tmp, "fproj", "fproj")))

    print("descriptions and hints")
    check("lookup_standard accepts process='turning'", fm.validate_args(
        "lookup_standard", v["lookup_standard"]["inputSchema"], {"process": "turning"}) == {"process": "turning"})
    check("check_fit default clearance comes from project.json", "clearance.default_min_mm" in v["check_fit"]["description"])
    try:
        fm.gui_pids = lambda ps_text=None: ["1"]
        fm.fc_user_dirs = lambda: {"user_data": os.path.join(tmp, "ud_none"), "macro_dir": None}
        h = fm.gui_reload_hint()
        check("build hint when FreeCAD is open without the reload helper", h and "File ▸ Revert" in h, h)
        fm.fc_user_dirs = lambda: {"user_data": ud, "macro_dir": md}
        check("…none when the helper is installed", fm.gui_reload_hint() is None)
        fm.gui_pids = lambda ps_text=None: []
        check("…none when FreeCAD is not open", fm.gui_reload_hint() is None)
    finally:
        restore()

    print("create_project: material density (round 3 E1)")
    C3 = os.path.join(tmp, "cp3")
    os.makedirs(C3)

    def new_proj(name, **kw):
        out = fm.t_create_project(dict({"name": name, "parent_dir": C3}, **kw))
        pj = json.load(open(os.path.join(C3, name, "project.json")))
        return out, pj, list(pj["parts"])[0] if pj.get("parts") else None

    out, pj, lb = new_proj("gusset", process="laser", material="5052 aluminium")
    want = standards.material_density("5052 aluminium")[0]
    check("laser + '5052 aluminium' → the aluminium density from the table, not the acrylic default",
          pj["material"]["density_g_cm3"] == want and want > 2.6 and "material table" in pj["material"]["density_source"]
          and ("Density: %g g/cm³ for 5052 aluminium" % want) in out, (pj["material"], out))
    check("…and the build's per-part lookup gives the same number", standards.density_for(pj, lb) == want)
    out, pj, lb = new_proj("shaft3", process="turning", material="1045 steel (C45), as supplied")
    check("turning + '1045 steel' → 7.85 in project.json, the same as the build uses",
          pj["material"]["density_g_cm3"] == 7.85 and standards.density_for(pj, lb) == 7.85, pj["material"])
    out, pj, lb = new_proj("odd", process="laser", material="Unobtainium 9000")
    check("unknown material → process default kept, marked assumed in project.json and in the reply",
          pj["material"]["density_g_cm3"] == fm.MATERIALS["laser"]["density_g_cm3"]
          and pj["material"]["density_source"].startswith("assumed (laser default")
          and "ASSUMED" in out and "Unobtainium 9000" in out and "density_g_cm3" in out, (pj["material"], out))
    out, pj, lb = new_proj("given", process="fdm", material="PETG", density_g_cm3=1.31)
    check("explicit density wins over the table — also on the part, so the build agrees",
          pj["material"]["density_g_cm3"] == 1.31 and pj["parts"][lb]["density_g_cm3"] == 1.31
          and standards.density_for(pj, lb) == 1.31, pj)
    e = raises(fm.t_create_project, {"name": "neg", "parent_dir": C3, "density_g_cm3": -2})
    check("a non-positive density is refused before anything is written", e and "must be > 0" in e
          and not os.path.exists(os.path.join(C3, "neg")), e)
    for proc in sorted(fm.MATERIALS):
        out, pj, lb = new_proj("def_" + proc, process=proc)
        check("%s default material: table density = the process default (%g)" % (proc, fm.MATERIALS[proc]["density_g_cm3"]),
              standards.density_for(pj, lb) == pj["material"]["density_g_cm3"] == fm.MATERIALS[proc]["density_g_cm3"], pj["material"])

    print("create_project: scaffolds (round 3 E6)")
    out, pj, lb = new_proj("asm3", template="assembly")
    model = open(os.path.join(C3, "asm3", "model.py")).read()
    check("assembly template: Assembly workbench pattern (assembly, ground, joint between named frames, solve), no mate",
          all(k in model for k in ("fc.assembly(", "fc.add(asm", "fc.ground(asm", "fc.joint(asm, \"fixed\", (body, \"seat\"), (ref, \"mount\")",
                                   "fc.solve(asm)", "fc.set_frames(body")) and "fc.mate(" not in model, model)
    for proc in sorted(fm.SCAFFOLDS):
        for tpl, src in (("part", fm.SCAFFOLDS[proc][0]), ("assembly", fm.MODEL_ASSEMBLY)):
            names = re.findall(r'name="(\w+)"', src)
            check("%s/%s scaffold: no two features share a name (no silent 'Base001')" % (proc, tpl),
                  len(names) == len(set(names)), names)
    out, pj, lb = new_proj("plate", process="laser")
    check("a body label that would collide with a template feature gets 'Part' appended",
          lb == "PlatePart" and 'fc.body("PlatePart")' in open(os.path.join(C3, "plate", "model.py")).read()
          and 'Body label: "PlatePart"' in out, (lb, out))

    print("lookup_standard (round 3 E3)")
    ls = fm.t_lookup_standard
    t = ls({"fit": "k6", "d": 8})
    lo, hi = standards.tolerance(8.0, "k6")
    check("fit='k6' with d → that class's limits (no crash)", "Ø8 k6" in t and ("%.4f … %.4f" % (lo, hi)) in t, t)
    check("fit='H11', d=8.2 → hole class limits (turned_spacer crashed on it)", "Ø8.2 H11" in ls({"fit": "H11", "d": 8.2}))
    t = ls({"fit": "H7/g6", "d": 8})
    check("fit pair unchanged", "H7/g6" in t and "clearance fit" in t, t)
    e = raises(ls, {"fit": "k", "d": 8})
    check("a malformed class is a tool error with examples (not a raw exception)", e and "'k6'" in e and "H7/g6" in e, e)
    e = raises(ls, {"fit": "k6"})
    check("fit without d → error with an example", e and "fit needs d" in e and "d=8" in e, e)
    for spec in ("ISO 2768-m", "m", "2768-mK"):
        t = ls({"tolerance": spec, "d": 80})
        check("tolerance=%r, d=80 → ±0.3" % spec, "80 mm → ±0.3 mm" in t and "ISO 2768-m" in t, t)
    check("tolerance='f', d=5 → ±0.05", "±0.05 mm" in ls({"tolerance": "f", "d": 5}))
    t = ls({"tolerance": "ISO 2768-m"})
    check("tolerance without d → the class's table", "linear" in t and "±0.3" in t and "→" not in t.split("\n")[1], t)
    e = raises(ls, {"tolerance": "ISO 2768-x", "d": 10})
    check("an unknown ISO 2768 class is explained", e and "'f' (fine), 'm' (medium)" in e, e)
    check("tolerance='k6' (an ISO 286 class) works like fit='k6'", "Ø8 k6" in ls({"tolerance": "k6", "d": 8}))
    e = raises(ls, {"tolerance": "H7"})
    check("an ISO 286 class as tolerance without d → error", e and "needs d" in e, e)
    t = ls({"bearing": "608"})
    check("bearing='608' → d 8 · D 22 · B 7 · r_s min 0.3", "bore d 8 · outside D 22 · width B 7 · chamfer r_s min 0.3" in t, t)
    check("bearing='6204-2RS' (suffix ignored)", "bore d 20 · outside D 47" in ls({"bearing": "6204-2RS"}))
    e = raises(ls, {"bearing": "999999"})
    check("unknown bearing → error naming the table", e and "no data for bearing" in e, e)
    check("size='608' (a bearing asked as a size) answers with the bearing", "outside D 22" in ls({"size": "608"}))
    t = ls({"size": "M3", "fit": "k6", "d": 8})
    check("several keys in one call", "M3" in t and "k6" in t, t)
    check("the overview lists the new lookups", "tolerance='ISO 2768-m'" in ls({}) and "bearing='608'" in ls({}))
    ok = fm.validate_args("lookup_standard", v["lookup_standard"]["inputSchema"], {"tolerance": "m", "bearing": "608", "d": 80})
    check("schema has tolerance and bearing", ok == {"tolerance": "m", "bearing": "608", "d": 80}, ok)
    dsc = v["lookup_standard"]["description"]
    check("description documents fit='k6', tolerance and bearing", "fit='k6'" in dsc and "tolerance='ISO 2768-m'" in dsc
          and "bearing='608'" in dsc, dsc)

    print("image paths, pass-through arguments (round 3 E4, E5)")
    inbox = os.path.join(tmp, "colleague", "inbox")
    os.makedirs(inbox)
    loose_doc = os.path.join(inbox, "bracket.FCStd")
    open(loose_doc, "w").write("x")
    cache = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "images")
    got = []

    def fake_w(op, args, timeout=300, request_id=None, job_key=None):
        got.append((op, args))
        return {"ok": True, "text": op.upper(), "images": [], "data": {}}

    fm.run_worker = fake_w
    try:
        fm.t_render({"doc": loose_doc})
        fm.t_run_script({"doc": loose_doc, "code": "pass", "views": ["iso"]})
        fm.t_section({"doc": loose_doc, "plane": {"z": 1}, "interference_ok": [["A", "B"]], "contact_ok": [["A", "C"]]})
        fm.t_check_fit({"doc": loose_doc, "pair_min": {"A|B": 0.1}})
        outs = {op: a.get("out") or a.get("image_path") for op, a in got}
        check("a document outside a project: render/run_script/section/check_fit images go to ~/.cache/freecad-mcp/images",
              all(o and o.startswith(cache + os.sep) for o in outs.values()), outs)
        check("…and nothing is written next to the user's file", not os.path.exists(os.path.join(inbox, "out")))
        sec = [a for op, a in got if op == "section"][0]
        fit = [a for op, a in got if op == "check_fit"][0]
        check("section passes interference_ok / contact_ok; check_fit passes pair_min",
              sec["interference_ok"] == [["A", "B"]] and sec["contact_ok"] == [["A", "C"]]
              and fit["pair_min"] == {"A|B": 0.1}, (sec, fit))
        del got[:]
        fm.t_section({"project": P, "plane": {"z": 1}})
        fm.t_run_script({"project": P, "code": "pass", "views": ["iso"]})
        fm.t_render({"project": P})
        outs = {op: a.get("out") or a.get("image_path") for op, a in got}
        check("inside a project: the project's out/ as before", all(o.startswith(os.path.join(P, "out") + os.sep)
                                                                    for o in outs.values()), outs)
        del got[:]
        fm.t_run_script({"doc": loose_doc, "code": "pass", "save": "revision", "from_older": True})
        check("run_script passes from_older (the worker's refusal names it)", got[-1][1]["from_older"] is True, got[-1][1])
    finally:
        restore()
    ok = fm.validate_args("check_fit", v["check_fit"]["inputSchema"], {"pair_min": {"A|B": 0.1}})
    check("check_fit schema: pair_min", ok == {"pair_min": {"A|B": 0.1}}, ok)
    e = raises(fm.validate_args, "check_fit", v["check_fit"]["inputSchema"], {"pair_min": {"A|B": "0.1"}})
    check("pair_min values must be numbers", e and 'pair_min["A|B"] must be number' in e, e)
    ok = fm.validate_args("section", v["section"]["inputSchema"], {"plane": {"z": 0}, "interference_ok": [["A", "B"]],
                                                                   "contact_ok": [["A", "C"]]})
    check("section schema: interference_ok, contact_ok", "interference_ok" in ok and "contact_ok" in ok, ok)
    check("run_script schema: from_older", "from_older" in v["run_script"]["inputSchema"]["properties"])

    print("build hint (round 3 E7)")
    BR = {"reused": False}

    def fake_build(op, args, timeout=300, request_id=None, job_key=None):
        return {"ok": True, "text": "BUILD ✓", "images": [], "data": {"ok": True, "rev": "r0001", "reused": BR["reused"]}}

    fm.run_worker = fake_build
    fm.gui_reload_hint = lambda: "FreeCAD is open — File ▸ Revert"
    try:
        fm._reload_hinted.clear()
        BR["reused"] = True
        check("no hint when the build reused the revision (inputs unchanged)", "Revert" not in text_of(fm.t_build({"project": P})))
        BR["reused"] = False
        check("hint on the first new revision", "Revert" in text_of(fm.t_build({"project": P})))
        check("…but only once per project per session", "Revert" not in text_of(fm.t_build({"project": P})))
        check("another project gets its own hint", "Revert" in text_of(fm.t_build({"project": R})))
    finally:
        restore()
        fm._reload_hinted.clear()

    print("drawing options and descriptions (round 3 E8)")
    ok = fm.validate_args("drawing", v["drawing"]["inputSchema"], {"hidden": True, "hole_dims": "pitch"})
    check("drawing schema: hidden, hole_dims", ok == {"hidden": True, "hole_dims": "pitch"}, ok)
    e = raises(fm.validate_args, "drawing", v["drawing"]["inputSchema"], {"hole_dims": "chain"})
    check("hole_dims enum", e and "hole_dims must be one of baseline, pitch" in e, e)
    sh = fm.drawing_sheets(P, ["Plate"], {"hidden": True, "hole_dims": "pitch"})
    check("hidden and hole_dims reach the sheet spec (and are saved like other options)",
          sh[0].get("hidden") is True and sh[0].get("hole_dims") == "pitch" and "hidden" in fm.DRAWING_KEYS, sh)
    dd = v["drawing"]["description"]
    check("drawing description: hidden, hole_dims, remove by match, align, append_note, notes kept as written",
          all(k in dd for k in ("hidden=true", "hole_dims=\"pitch\"", "remove {match: {view, value, type}}",
                                "align=\"left\"", "append_note {text}", "kept exactly as written")), dd)
    md = v["measure"]["description"]
    check("measure description: frames act as axes, bosses selector, order-independent",
          "acts as its +Z axis" in md and "bosses(d=22)" in md and "does not matter" in md, md)
    check("build description lists check types incl. hole_diameter", "hole_diameter" in v["build"]["description"])
    check("create_project description: assembly template uses fc.joint", "fc.joint" in json.dumps(v["create_project"]["inputSchema"]))
    print("measure points as arrays (round 4 #50)")
    ms = v["measure"]["inputSchema"]
    ok = fm.validate_args("measure", ms, {"project": "two", "a": "Two", "b": [0, 0, 0]})
    check("measure b=[x,y,z] passes validation", ok == {"project": "two", "a": "Two", "b": [0, 0, 0]}, ok)
    check("…and a string still does", fm.validate_args("measure", ms, {"a": "Two.holes", "b": "Plate"})["b"] == "Plate")
    e = raises(fm.validate_args, "measure", ms, {"a": "Two", "b": [0, 0]})
    check("a 2-number point is refused, saying 3 numbers", e and "b must have 3 items, got 2" in e, e)
    e = raises(fm.validate_args, "measure", ms, {"a": "Two", "b": [0, "y", 0]})
    check("a point with a non-number is refused", e and "b[1] must be number" in e, e)
    ok = fm.validate_args("measure", ms, {"queries": [{"a": "Two", "b": [6, 6, 13], "tol": 0.1}]})
    check("queries items accept array points too", ok["queries"][0]["b"] == [6, 6, 13], ok)
    e = raises(fm.validate_args, "measure", ms, {"queries": [{"a": 3}]})
    check("…and are checked", e and "queries[0].a must be string or array" in e, e)
    got = []
    fm.run_worker = lambda op, a, **k: got.append(a) or {"ok": True, "text": "MEASURE", "data": {}}
    try:
        fm.t_measure({"project": P, "a": [1, 2, 3], "b": "Plate"})
        check("an array a reaches the worker as a point", got and got[-1]["queries"] == [{"a": [1, 2, 3], "b": "Plate"}],
              got)
    finally:
        restore()

    print("drawing create stamps the drawn build's rev (round 4 #49)")
    V = os.path.join(tmp, "vproj")
    os.makedirs(os.path.join(V, "build"))
    json.dump({"name": "vproj", "process": "cnc"}, open(os.path.join(V, "project.json"), "w"))
    for f in ("vproj.FCStd", "vproj__thick.FCStd"):
        open(os.path.join(V, "build", f), "w").write("model")
    for r, var in (("r0001", None), ("r0002", "thick")):
        os.makedirs(os.path.join(V, ".history", r))
        json.dump({"variant": var, "objects": [{"label": "Plate", "role": "part", "solids": 1}]},
                  open(os.path.join(V, ".history", r, "report.json"), "w"))
    lbj = os.path.join(V, ".history", "last_build.json")
    json.dump({os.path.join(V, "build", "vproj.FCStd"): {"sha1": "x", "rev": "r0001"},
               os.path.join(V, "build", "vproj__thick.FCStd"): {"sha1": "y", "rev": "r0002"}}, open(lbj, "w"))
    vcalls = []

    def v_gui(op, args, timeout=300, request_id=None):
        vcalls.append((op, args))
        return {"ok": True, "text": "SHEETS", "sheets": [], "images": []}

    fm.run_gui_worker = v_gui
    try:
        fm.t_drawing({"project": V})
        check("after a variant build (r0002) the main build is drawn as r0001 (last_build.json, absolute key)",
              vcalls[-1][1]["rev"] == "r0001", vcalls[-1][1]["rev"])
        fm.t_drawing({"project": V, "doc": "build/vproj__thick.FCStd"})
        check("…and the variant file as r0002", vcalls[-1][1]["rev"] == "r0002", vcalls[-1][1]["rev"])
        json.dump({"build/vproj.FCStd": {"sha1": "x", "rev": "r0001"}}, open(lbj, "w"))
        fm.t_drawing({"project": V})
        check("last_build.json keyed by the path relative to the project works too", vcalls[-1][1]["rev"] == "r0001")
        os.remove(lbj)
        fm.t_drawing({"project": V})
        check("without last_build.json: the newest report of the same output (main, not the variant)",
              vcalls[-1][1]["rev"] == "r0001", vcalls[-1][1]["rev"])
    finally:
        restore()

    print("drawing currency, failed ops, package signature, reset, dimension counts (round 4 E)")
    K = os.path.join(tmp, "kproj")
    os.makedirs(os.path.join(K, "build"))
    os.makedirs(os.path.join(K, ".history", "r0001"))
    json.dump({"variant": None, "objects": [{"label": "Plate", "role": "part", "solids": 1},
                                            {"label": "Lid", "role": "part", "solids": 1}]},
              open(os.path.join(K, ".history", "r0001", "report.json"), "w"))
    kpj = {"name": "kproj", "process": "cnc", "material": {"name": "6061-T6", "density_g_cm3": 2.7},
           "drawing": {"paper": "auto"}}
    json.dump(kpj, open(os.path.join(K, "project.json"), "w"))
    json.dump([{"id": "a", "type": "valid", "object": "Plate"}], open(os.path.join(K, "checks.json"), "w"))
    open(os.path.join(K, "model.py"), "w").write("# model\n")
    open(os.path.join(K, "build", "kproj.FCStd"), "w").write("model")
    KW = {"rev": "r0001", "inputs": "k-1", "ops_failed": {}, "dims": (5, 7), "old_worker": False, "finish": None}
    kcalls = []

    def k_gui(op, args, timeout=300, request_id=None):
        kcalls.append((op, json.loads(json.dumps(args))))
        if op == "create":
            out = []
            for s in args["sheets"]:
                for part in ([s["part"]] if s.get("part") else ["Plate", "Lid"]):
                    stem = fm._sheet_stem(part)
                    os.makedirs(args["out_dir"], exist_ok=True)
                    files = {k: os.path.join(args["out_dir"], "%s.%s" % (stem, k)) for k in ("pdf", "png")}
                    for f in files.values():
                        open(f, "w").write("x")
                    rec = {"part": part, "files": files, "dims": [{"ok": True}] * 7, "dims_verified": KW["dims"][0],
                           "dims_total": KW["dims"][1], "paper": "A4", "scale": "1:1", "warnings": [],
                           "ops_done": [], "ops_failed": list(KW["ops_failed"].get(part, []))}
                    if args.get("save_dir"):
                        os.makedirs(args["save_dir"], exist_ok=True)
                        rec["document"] = os.path.join(args["save_dir"], stem + ".FCStd")
                        json.dump({"spec_hash": s.get("_spec_hash"), "rev": args["rev"], "t": time.time()},
                                  open(rec["document"], "w"))
                    out.append(rec)
            return {"ok": True, "text": "SHEETS", "sheets": out, "images": []}
        if op == "edit":                        # FcSource (spec hash) travels with the edited revision
            fs = json.load(open(args["doc"]))
            fs["edited"] = args["ops"]
            json.dump(fs, open(args["save_as"], "w"))
            return {"ok": True, "text": "EDIT", "images": []}
        if op == "sheet_status":                # like the K1 worker: the stored spec hash must match
            res = []
            for s in args["sheets"]:
                fs = json.load(open(s["doc"]))
                cur = KW["old_worker"] or fs.get("spec_hash") == s.get("spec_hash")
                res.append({"part": s["part"], "doc": s["doc"], "current": cur, "revision": fs.get("rev", ""),
                            "detail": "current" if cur else "material/finish/notes changed", "dims_verified": None})
            return {"ok": True, "text": "", "sheets": res}
        if op == "export":
            os.makedirs(args["out_dir"], exist_ok=True)
            files = {k: os.path.join(args["out_dir"], "%s.%s" % (args["name"], k)) for k in ("pdf", "png")}
            for f in files.values():
                open(f, "w").write("exported")
            return {"ok": True, "text": "EXPORT", "files": files}
        return {"ok": False, "error": "unknown op %s" % op}

    def k_worker(op, args, timeout=300, request_id=None, job_key=None):
        if op == "build":
            return {"ok": True, "text": "BUILD ✓  kproj  %s\nchecks   (checks.json)\n  ✓ a        valid   ok" % KW["rev"],
                    "data": {"ok": True, "rev": KW["rev"], "fcstd": os.path.join(K, "build", "kproj.FCStd"),
                             "checks": 1, "checks_passed": 1, "inputs_sha1": KW["inputs"]}}
        if op == "release_prepare":
            pj = json.load(open(os.path.join(K, "project.json")))
            recs = []
            for lb in args.get("objects") or ["Plate", "Lid"]:
                d = os.path.join(args["release_dir"], lb)
                os.makedirs(d)
                f = os.path.join(d, lb + ".step")
                open(f, "w").write("step %s" % time.time())        # exports embed their time, like STEP
                pc = (pj.get("parts") or {}).get(lb) or {}
                recs.append({"part": lb, "process": "cnc", "material": pc.get("material") or pj["material"]["name"],
                             "finish": pc.get("finish", ""), "qty": (args.get("qty") or {}).get(lb, 1), "files": [f],
                             "dfm": {"findings": []}, "drawing": None, "notes": []})
            return {"ok": True, "text": "", "data": {"ok": True, "labels": ["Plate", "Lid"], "unknown_keys": {},
                                                     "parts": recs, "refs": [], "exports": []}}
        if op == "release_finish":
            KW["finish"] = args
            rel = args["release_dir"]
            open(os.path.join(rel, "README.md"), "w").write("\n".join(
                "%s: %s" % (p["part"], " | ".join(p.get("notes") or [])) for p in args["parts"]))
            z = rel + ".zip"
            with zipfile.ZipFile(z, "w") as zf:
                for root, _, files in os.walk(rel):
                    for fn in files:
                        zf.write(os.path.join(root, fn), os.path.relpath(os.path.join(root, fn), os.path.dirname(rel)))
            return {"ok": True, "text": z, "data": {"zip": z}}
        return {"ok": False, "error": "unexpected op %s" % op}

    fm.run_gui_worker, fm.run_worker = k_gui, k_worker
    kdj = lambda: json.load(open(os.path.join(K, "drawings.json")))          # noqa: E731
    try:
        res = fm.t_drawing({"project": K})
        cr = [a for op, a in kcalls if op == "create"][-1]
        hs = [s.get("_spec_hash") for s in cr["sheets"]]
        check("K1: drawing create passes _spec_hash (sha1) in every sheet spec",
              len(hs) == 2 and all(h and re.match(r"^[0-9a-f]{40}$", h) for h in hs), cr["sheets"])
        rec = kdj()["_sheets"]["Plate"]
        check("…recorded in _sheets with the worker's dims_verified/dims_total (#34)",
              rec.get("spec_hash") == hs[0] and rec["dims"] == 5 and rec["dims_total"] == 7, rec)

        del kcalls[:]
        res = fm.t_release({"project": K})
        t = text_of(res)
        st = [a for op, a in kcalls if op == "sheet_status"][0]
        check("K1: release passes spec_hash per sheet to sheet_status — equal to create's when nothing changed",
              [s.get("spec_hash") for s in st["sheets"]] == hs, (st["sheets"], hs))
        check("…so both saved sheets ship as is (project-level process/material = release's explicit ones)",
              t.count("current)") == 2 and not [1 for op, _ in kcalls if op == "create"], t)
        dd = {p["part"]: p["drawing_dims"] for p in KW["finish"]["parts"]}
        check("current sheet: drawing_dims = [verified, total] from the create record (#34/#53)",
              dd == {"Plate": [5, 7], "Lid": [5, 7]}, dd)
        meta = json.load(open(os.path.join(K, "release", "kproj_r0001", ".release.json")))
        check(".release.json: package signature, arguments, sheets and the sha1 of every shipped file (#26)",
              re.match(r"^[0-9a-f]{40}$", meta.get("package_sha1", "")) and set(meta["sheets"]) == {"Plate.FCStd", "Lid.FCStd"}
              and "Plate/Plate.step" in meta["files"] and "Plate/Plate_drawing.pdf" in meta["files"]
              and "README.md" in meta["files"], meta)

        print("  K1 stale by spec")
        kpj["material"]["name"] = "5052 aluminium"
        json.dump(kpj, open(os.path.join(K, "project.json"), "w"))
        KW.update(rev="r0002", inputs="k-2")
        del kcalls[:]
        t = text_of(fm.t_release({"project": K}))
        cr = [a for op, a in kcalls if op == "create"]
        check("project material changed, shape unchanged → regenerated (not 'current'), naming why",
              t.count("regenerated — drawings/") == 2 and "material/finish/notes changed" in t
              and cr and all(s.get("_spec_hash") for s in cr[0]["sheets"]), t)
        check("…regenerated dims '5/7 dimensions verified' from the worker; drawing_dims [5, 7] (#34)",
              "5/7 dimensions verified" in t and KW["finish"]["parts"][0]["drawing_dims"] == [5, 7], t)
        check("…and the new spec hash is recorded", kdj()["_sheets"]["Plate"]["spec_hash"] == cr[0]["sheets"][0]["_spec_hash"]
              != hs[0])

        fm.t_drawing({"action": "edit", "project": K, "part": "Plate", "ops": [{"op": "add_note", "text": "H"}]})
        kpj["parts"] = {"Plate": {"finish": "anodised black"}}
        json.dump(kpj, open(os.path.join(K, "project.json"), "w"))
        KW.update(rev="r0003", inputs="k-3")
        e = raises(fm.t_release, {"project": K})
        check("stale by spec + hand edited → STOP naming the sheet and what changed (K1)", e and "RELEASE STOPPED" in e
              and "Plate: drawings/Plate.r01.FCStd (a revision saved by drawing edit) — material/finish/notes changed" in e
              and "drawing(action=\"create\"" in e and "drawings=\"generate\"" in e, e)
        KW["old_worker"] = True                 # a worker that only compares shapes: the _sheets record still catches it
        e = raises(fm.t_release, {"project": K})
        check("…also when the worker reports it current: the create record's spec hash differs",
              e and "Plate: drawings/Plate.r01.FCStd" in e and "material/finish/notes, title block or drawing options changed" in e, e)
        KW["old_worker"] = False
        fm.t_drawing({"project": K, "part": "Plate"})

        print("  failed create ops (#20)")
        KW.update(rev="r0004", inputs="k-4", ops_failed={"Lid": ["add_dimension Top Lid.holes(d=5.5)[5]: no hole [5]"]})
        json.dump(dict(kdj(), Lid={"ops": [{"op": "add_dimension", "view": "Top"}]}),
                  open(os.path.join(K, "drawings.json"), "w"))          # ops changed → Lid stale by spec
        e = raises(fm.t_release, {"project": K})
        check("regenerated sheet with failed create ops → STOP naming the op, the fix and allow_failed_ops",
              e and "RELEASE STOPPED" in e and "Lid: ✗ add_dimension Top Lid.holes(d=5.5)[5]: no hole [5]" in e
              and "drawing(action=\"create\", part='Lid', ops=[…])" in e and "allow_failed_ops=true" in e
              and "Regenerated and saved before stopping: drawings/Lid.FCStd" in e
              and not os.path.exists(os.path.join(K, "release", "kproj_r0004")), e)
        KW["ops_failed"] = {}
        e = raises(fm.t_release, {"project": K})
        check("…the saved sheet is now current but still lacks them: the create record keeps stopping the release",
              e and "Lid: ✗ add_dimension" in e and kdj()["_sheets"]["Lid"]["ops_failed"], e)
        t = text_of(fm.t_release({"project": K, "allow_failed_ops": True}))
        lid = [p for p in KW["finish"]["parts"] if p["part"] == "Lid"][0]
        readme = open(os.path.join(K, "release", "kproj_r0004", "README.md")).read()
        check("allow_failed_ops → shipped with a ✗ line in the output and in the README notes",
              "✗ drawing Lid: 1 create op(s) failed — shipped without them (allow_failed_ops)" in t
              and any("✗ The drawing lacks 1 requested addition" in n for n in lid["notes"])
              and "✗ The drawing lacks 1 requested addition" in readme, (t, lid["notes"], readme))

        print("  package signature (#26)")
        KW.update(rev="r0005", inputs="k-5")
        fm.t_drawing({"project": K, "part": "Lid", "ops": []})          # clean ops
        t1 = text_of(fm.t_release({"project": K, "parts": ["Plate"], "drawings": "none"}))
        t2 = text_of(fm.t_release({"project": K, "parts": ["Lid"], "drawings": "none"}))
        base = os.path.join(K, "release", "kproj_r0005")
        check("release(parts=[Plate]) then release(parts=[Lid]) → the Lid package is -2; the Plate package is kept",
              "kproj_r0005-2" in t2 and "holds a different package of this revision" in t2
              and os.path.isfile(base + ".zip") and os.path.isfile(os.path.join(base, "Plate", "Plate.step"))
              and os.path.isfile(os.path.join(base + "-2", "Lid", "Lid.step")), t2)
        t3 = text_of(fm.t_release({"project": K, "parts": ["Lid"], "drawings": "none"}))
        check("the same release again (exports differ only by their timestamps) replaces its own package",
              "replaced the earlier package kproj_r0005-2" in t3 and not os.path.exists(base + "-3"), t3)
        t4 = text_of(fm.t_release({"project": K, "parts": ["Lid"], "drawings": "none", "qty": {"Lid": 100}}))
        check("…a different qty is a different package → -3", "kproj_r0005-3" in t4 and os.path.isfile(base + "-2.zip"), t4)
        t5 = text_of(fm.t_release({"project": K}))
        t6 = text_of(fm.t_release({"project": K}))
        check("full release with drawings → -4; repeated → replaced", "kproj_r0005-4" in t5
              and "replaced the earlier package kproj_r0005-4" in t6, (t5, t6))
        fm.t_drawing({"action": "edit", "project": K, "part": "Lid", "ops": [{"op": "add_note", "text": "SEE"}]})
        t7 = text_of(fm.t_release({"project": K}))
        check("a hand edit to a shipped sheet (inputs unchanged) is a different package → -5, -4 kept",
              "kproj_r0005-5" in t7 and os.path.isfile(base + "-4.zip"), t7)

        print("  reset without part (#27)")
        dj = kdj()
        dj.update({"*": {"scale": "1:2", "notes": ["GLOBAL NOTE"]}, "Plate": {"paper": "A3"}, "Lid": {"notes": ["L"]}})
        json.dump(dj, open(os.path.join(K, "drawings.json"), "w"))
        del kcalls[:]
        res = fm.t_drawing({"project": K, "reset": True})
        cr = [a for op, a in kcalls if op == "create"][-1]
        check("reset=true without part draws with none of the saved options (no '*' scale/notes, no part paper)",
              all(not any(k in s for k in ("scale", "notes", "paper", "ops")) for s in cr["sheets"]), cr["sheets"])
        dj2 = kdj()
        check("…clears '*' and every part's options and keeps _sheets",
              set(dj2) == {"_sheets"} and set(dj2["_sheets"]) == {"Plate", "Lid"}, dj2)
        check("…and says so", "cleared \"*\" and every part's saved options" in text_of(res), text_of(res))
        st = [a for op, a in kcalls if op == "create"]
        sh = fm.drawing_sheets(K, ["Plate"])
        check("…so a later regeneration uses the same spec (same K1 hash as the sheet drawn)",
              fm.drawing_spec_hash(dict(sh[0]), fm._project_json(K)) == st[-1]["sheets"][0]["_spec_hash"], sh)
        json.dump(dict(kdj(), Lid={"notes": ["L"]}), open(os.path.join(K, "drawings.json"), "w"))
        fm.t_drawing({"project": K, "reset": True, "notes": ["N"]})
        dj3 = kdj()
        check("reset with options and no part: only the new '*' options remain", dj3.get("*") == {"notes": ["N"]}
              and "Lid" not in dj3 and "_sheets" in dj3, dj3)
        rs = v["drawing"]["inputSchema"]["properties"]["reset"]["description"]
        check("reset schema and description say '*' and every part", "of \"*\" and of every part" in rs
              and "\"*\" and every part's" in v["drawing"]["description"], rs)
    finally:
        restore()
except Exception:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("part 1 ran to the end", False)
finally:
    restore()

# ======================================================================= part 2
if "--unit" not in sys.argv:
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    udata = os.path.join(tmp, "fc_user_data")
    os.makedirs(udata)
    c = Client(home, {"FREECAD_USER_DATA": udata})
    try:
        c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        print("stdio: validation, guide, status")
        err, txt, _ = c.tool("render", {"project": "nope", "hidden_objects": ["A"]})
        check("render with an unknown argument is an error, not ignored", err and "unknown argument 'hidden_objects'" in txt, txt)
        err, txt, _ = c.tool("drawing", {"project": "nope", "paper": "A0"})
        check("drawing with a bad enum is an error", err and "paper must be one of" in txt, txt)
        err, txt, _ = c.tool("guide", {"topic": "drawing"})
        check("guide(topic='drawing') → ⑧ Document", not err and txt.startswith("### ⑧ Document"), txt[:80])
        err, txt, _ = c.tool("guide", {"topic": "nonsense"})
        check("guide with an unknown topic is an error", err and "no guide topic" in txt, txt[:200])
        err, txt, _ = c.tool("freecad_status")
        st = json.loads(txt)
        rh = st.get("reload_helper") or {}
        check("status reports where the macro goes (FreeCAD's macro folder)", rh.get("macro_path", "").endswith(
            "MCP_Reload.FCMacro") and "setup_hint" not in st, rh)
        err, txt, _ = c.tool("open_in_gui", {"install_reload_helper": True, "launch": False})
        check("install_reload_helper puts the macro where status says", not err and os.path.isfile(rh.get("macro_path", "")), txt)

        print("stdio: process scaffolds build")
        for proc in ("fdm", "sla", "cnc", "turning", "laser", "sheet"):
            err, txt, _ = c.tool("create_project", {"name": "s_%s" % proc, "process": proc})
            pj = json.load(open(os.path.join(home, "s_%s" % proc, "project.json")))
            label = "S%s" % proc.capitalize()
            check("%s scaffold: label in the reply and in project.json parts; paper auto" % proc, not err
                  and ('Body label: "%s"' % label) in txt and label in pj["parts"] and pj["drawing"]["paper"] == "auto", txt)
            comp = {"fdm": 0.2, "sla": 0.1}.get(proc)
            check("%s: dfm.hole_compensation_mm = %s" % (proc, comp), (pj.get("dfm") or {}).get("hole_compensation_mm") == comp, pj)
            err, txt, _ = c.tool("build", {"project": "s_%s" % proc, "views": []})
            check("%s scaffold builds clean" % proc, not err and txt.startswith("BUILD ✓") and "warning" in txt
                  and " 0 warnings" in txt, txt[:1500])

        err, txt, _ = c.tool("create_project", {"name": "s_asm", "template": "assembly"})
        err, txt, _ = c.tool("build", {"project": "s_asm", "views": []})
        check("assembly scaffold builds clean: grounded plate, reference part joined by named frames, no '001' names",
              not err and txt.startswith("BUILD ✓") and " 0 warnings" in txt and "grounded: SAsm" in txt
              and "RefMount" in txt and "holds ✓" in txt and not re.search(r"[A-Za-z]00\d\b", txt), txt[:1500])
        err, txt, _ = c.tool("create_project", {"name": "s_al", "process": "laser", "material": "5052 aluminium"})
        err, txt, _ = c.tool("build", {"project": "s_al", "views": []})
        check("laser plate in 5052 aluminium: the build's mass uses the aluminium density",
              not err and re.search(r"mass: 5052 aluminium, 2\.\d+ g/cm³", txt), txt[:600])

        print("stdio: files")
        err, txt, _ = c.tool("write_file", {"project": "s_fdm", "path": "refs/vendor.stl", "encoding": "base64",
                                            "content": base64.b64encode(b"solid x\nendsolid x\n").decode()})
        check("base64 reference file over stdio", not err and os.path.isfile(os.path.join(home, "s_fdm", "refs", "vendor.stl")), txt)
        err, txt, _ = c.tool("read_file", {"project": "s_fdm", "path": "s_fdm/params.json"})
        check("project-prefixed path over stdio", not err and "width" in txt, txt[:200])
        err, txt, _ = c.tool("measure", {"project": "s_fdm", "a": "SFdm", "b": [0, 0, 100]})
        check("measure with a point given as an array runs (round 4 #50)", not err and "Nothing was run" not in txt, txt[:300])

        print("stdio: drawings + release on a turned part (real FreeCAD GUI)")
        err, txt, _ = c.tool("drawing", {"project": "s_turning", "part": "STurning", "ops": [
            {"op": "add_note", "text": "POLISH Ø12", "x": 60, "y": 40}]})
        dj = json.load(open(os.path.join(home, "s_turning", "drawings.json"))) if os.path.isfile(
            os.path.join(home, "s_turning", "drawings.json")) else {}
        check("create with ops: sheet saved, ops persisted for the part, sheet recorded", not err
              and dj.get("STurning", {}).get("ops") and dj.get("_sheets", {}).get("STurning", {}).get("file")
              == "drawings/STurning.FCStd", txt[-1500:])
        err, txt, content = c.tool("drawing", {"action": "read", "project": "s_turning"})
        check("read with only the project reads the newest sheet; preview in .history/previews", not err
              and "drawings/STurning.FCStd (newest revision)" in txt and len(images(content)) == 1
              and os.listdir(os.path.join(home, "s_turning", ".history", "previews")), txt[-600:])
        err, txt, _ = c.tool("release", {"project": "s_turning"})
        rel = os.path.join(home, "s_turning", "release")
        zips = sorted(f for f in os.listdir(rel) if f.endswith(".zip")) if os.path.isdir(rel) else []
        names = zipfile.ZipFile(os.path.join(rel, zips[-1])).namelist() if zips else []
        check("release ships the saved current sheet as is (sheet rev reported), PDF only", not err
              and re.search(r"shipped drawings/STurning\.FCStd \(sheet rev r\d{4}, current\)", txt)
              and any(n.endswith("STurning_drawing.pdf") for n in names) and not any(n.endswith(".dxf") for n in names),
              txt[-1500:])
        err, txt, _ = c.tool("drawing", {"action": "edit", "project": "s_turning",
                                         "ops": [{"op": "add_note", "text": "HAND EDIT", "x": 60, "y": 60}]})
        check("edit saves drawings/STurning.r01.FCStd", not err and os.path.isfile(
            os.path.join(home, "s_turning", "drawings", "STurning.r01.FCStd")), txt[-800:])
        c.tool("edit_file", {"project": "s_turning", "path": "params.json", "old_text": '"value": 25,',
                             "new_text": '"value": 30,'})
        err, txt, _ = c.tool("release", {"project": "s_turning"})
        check("model changed + hand-edited sheet → release stops and says what to do", err and "RELEASE STOPPED" in txt
              and "STurning.r01.FCStd" in txt and "drawings=\"generate\"" in txt, txt[-1200:])
        err, txt, _ = c.tool("drawing", {"project": "s_turning", "part": "STurning"})
        check("create regenerates (ops replayed) and moves the edited revision to backups", not err
              and "moved    drawings/STurning.r01.FCStd" in txt and not os.path.exists(
                  os.path.join(home, "s_turning", "drawings", "STurning.r01.FCStd")), txt[-1200:])
        err, txt, content = c.tool("release", {"project": "s_turning"})
        check("…after which release ships the regenerated sheet with a preview", not err
              and re.search(r"shipped drawings/STurning\.FCStd \(sheet rev r\d{4}, current\)", txt)
              and len(images(content)) == 1, txt[-1200:])
        pjp = os.path.join(home, "s_turning", "project.json")
        pj = json.load(open(pjp))
        pj["parts"]["STurning"]["material"] = "303 stainless steel"
        json.dump(pj, open(pjp, "w"), indent=2)
        err, txt, _ = c.tool("release", {"project": "s_turning"})
        check("K1 (real worker): material changed in project.json, model unchanged → the saved sheet is regenerated, "
              "not shipped as current", not err and "regenerated — drawings/STurning.FCStd was out of date" in txt
              and "current)" not in txt, txt[-1200:])
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("part 2 ran to the end", False)
    finally:
        c.close()

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
