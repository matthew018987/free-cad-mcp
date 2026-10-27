#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""the MCP Reload helper (freecad_addon/) in real FreeCAD GUIs.

    python3 tests/test_reload_addon.py

Runs the addon from the source tree (no plugin build needed). Every FreeCAD GUI
gets a temporary user.cfg and user folder — your own FreeCAD setup is never
touched — and runs under the server's GUI lock, so drawing workers started by
other test suites are not disturbed.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from mcp_client import check, fails  # noqa: E402

ADDON = os.path.join(ROOT, "freecad_addon")
MOD = os.path.join(ADDON, "FreeCADMCPReload")
RES = sorted(glob.glob("/Applications/FreeCAD*.app/Contents/Resources"))[-1]
LOCK = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "gui", "gui.lock")


def read(p):
    with open(p) as fh:
        return fh.read()


def gui(script, env, timeout=300):
    """Run a script inside a private FreeCAD GUI; returns the JSON it writes to $OUT."""
    run = tempfile.mkdtemp(prefix="gui-", dir=tmp)
    e = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "FCMCP_REQ"):
        e.pop(k, None)
    e["FREECAD_USER_DATA"] = os.path.join(run, "user_data")   # no user addons in the test GUI
    os.makedirs(e["FREECAD_USER_DATA"])
    e.update(env)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        r = subprocess.run(["perl", "-e", "alarm %d; exec @ARGV" % timeout, os.path.join(RES, "bin", "freecad"),
                            "-u", os.path.join(run, "user.cfg"), script], env=e, capture_output=True)
    if os.path.isfile(env["OUT"]):
        return json.load(open(env["OUT"]))
    return {"error": "no result (exit %s)" % r.returncode, "log": r.stdout.decode("utf-8", "replace")[-1500:]}


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixF-"))   # FreeCAD reports /private/var
try:
    print("addon sources")
    src = read(os.path.join(MOD, "mcp_reload.py"))
    init_gui = read(os.path.join(MOD, "InitGui.py"))
    macro = read(os.path.join(ADDON, "MCP_Reload.FCMacro"))
    expected = ("# MCP_Reload.FCMacro — generated from freecad_addon/FreeCADMCPReload/mcp_reload.py; do not edit.\n"
                "# Run it to toggle the watcher: open documents reload when the FreeCAD MCP server rebuilds them.\n\n"
                + src + "\n\n# --- macro entry point\ntoggle()\n")
    check("the macro is regenerated from mcp_reload.py (as tools/build_plugin.py writes it)", macro == expected)
    try:
        compile(macro, "MCP_Reload.FCMacro", "exec")
        compile(src, "mcp_reload.py", "exec")
        compile(init_gui, "InitGui.py", "exec")
        compiled = True
    except SyntaxError as e:
        compiled = str(e)
    check("addon files compile", compiled is True, compiled)
    check("no hard PySide6 import — FreeCAD's PySide shim works on Qt5 and Qt6 builds",
          not re.search(r"(?m)^\s*(from|import) PySide6", src + init_gui)
          and "from PySide import QtCore" in src and "from PySide import QtCore" in init_gui)
    check("the addon starts the watcher with its start-up line (no quiet start)",
          "mcp_reload.start()" in init_gui and "quiet" not in src + init_gui)
    check("the addon follows edits from FreeCAD start-up, before the watcher starts",
          init_gui.index("mcp_reload.track()") < init_gui.index("QTimer.singleShot"))
    check("module docstring gives the close-without-saving advice",
          "close it WITHOUT saving (or File ▸ Revert)" in src and "saving would overwrite Claude's rebuild" in src
          and "Save or close it" not in src)
    check("still inert inside the server's workers (FCMCP_REQ)", 'os.environ.get("FCMCP_REQ")' in src)

    print("protection rules (FreeCAD GUI, ~1 min)")
    work = os.path.join(tmp, "fix")
    os.makedirs(work)
    out = os.path.join(tmp, "fix.json")
    res = gui(os.path.join(HERE, "gui", "reload_fix_check.py"),
              {"RELOAD_ADDON_DIR": MOD, "RELOAD_WORKDIR": work, "RELOAD_PY": os.path.join(RES, "bin", "python"),
               "RELOAD_LIB": os.path.join(RES, "lib"), "OUT": out})
    short = {k: v for k, v in res.items() if k != "messages"}
    check("GUI script ran without errors", "error" not in res and not res.get("edit_errors"),
          res.get("error") or res.get("edit_errors") or res.get("log"))
    check("start() does nothing when FCMCP_REQ is set", res.get("inert_in_worker") is True, short)
    check("start prints 'watching N open document(s) — run the MCP_Reload macro to stop'",
          res.get("start_counts_documents") is True, res.get("start_message"))
    check("a document already stale when the watcher starts is reloaded",
          res.get("stale_open_copy_is_old") is True and res.get("stale_at_start_reloaded") is True, short)
    check("…an up-to-date one is left alone", res.get("fresh_not_reloaded") is True, short)
    check("…one with a console edit made before the watcher ever started is kept, with a warning that says why",
          res.get("early_modified_kept") is True and res.get("early_warned") is True,
          res.get("early_message") or short)
    check("…until it is saved in FreeCAD: the next rebuild then reloads it",
          res.get("early_saved_then_reloaded") is True, short)
    check("a console edit made right after opening a document is protected",
          res.get("quick_warned") is True and res.get("quick_protected") is True, short)
    check("File ▸ Save a Copy keeps the open document's console edit protected",
          res.get("save_copy_warned") is True and res.get("save_copy_protected") is True, short)
    check("edits made while the watcher is stopped are protected when it starts again; a clean document "
          "rebuilt meanwhile reloads", res.get("paused_stopped") is True and res.get("paused_warned") is True
          and res.get("paused_protected") is True and res.get("paused_clean_reloaded") is True, short)
    check("a Python-console edit (no undo step) is protected",
          res.get("console_edit_undo_count") == 0 and res.get("console_warned") is True
          and res.get("console_protected") is True, short)
    check("the warning says close WITHOUT saving / Revert, and that saving would overwrite the rebuild",
          res.get("conflict_advice") is True, res.get("conflict_message"))
    check("each further rebuild of an edited document gets one more line, still protected",
          res.get("console_rewarned") is True and res.get("console_still_protected") is True, res.get("messages"))
    check("close without saving + reopen loads the rebuild, with no second reload",
          res.get("console_reopened_fresh") is True, short)
    check("a console property change is protected too", res.get("property_edit_protected") is True, short)
    check("saving in FreeCAD clears the edit state — the next rebuild reloads",
          res.get("saved_then_reloaded") is True, short)
    check("load-time recompute, recompute-only changes and visibility toggles are not edits",
          res.get("noise_not_an_edit") is True, short)
    check("edit mode defers the reload (and says why)", res.get("in_edit") is True
          and res.get("edit_mode_deferred") is True, res.get("edit_mode_message") or short)
    check("…and it reloads once edit mode ends", res.get("edit_mode_then_reloaded") is True, short)
    check("an open task panel defers the reload, then it reloads",
          res.get("task_panel_deferred") is True and res.get("task_panel_then_reloaded") is True, short)
    check("a modal dialog defers the reload, then it reloads",
          res.get("modal_deferred") is True and res.get("modal_then_reloaded") is True, short)
    check("reloading a background document keeps the active document and view",
          res.get("background_reloaded") is True and res.get("background_kept_focus") is True,
          [res.get("focus_before"), res.get("focus_after")])
    check("the active document stays active when it is the one reloaded",
          res.get("foreground_reloaded") is True and res.get("foreground_still_active") is True, short)
    check("close, rebuild, reopen is not reloaded a second time", res.get("reopen_no_double_reload") is True,
          short)
    check("a cancelled command (aborted transaction, e.g. task panel Cancel) is not an edit",
          res.get("aborted_command_not_an_edit") is True, short)
    check("a committed command is protected (undo step)", res.get("committed_command_protected") is True, short)
    check("opening a drawing sheet is not an edit", res.get("sheet_open_not_an_edit") is True, short)
    check("TechDraw re-matching dimension references after load is not an edit",
          res.get("techdraw_rematch_not_an_edit") is True, short)
    check("a drawing whose 3D view is in front keeps it in front, with its camera",
          res.get("drawing_3d_kept") is True and res.get("drawing_sheet_opened") is True,
          [res.get("drawing_front_before"), res.get("drawing_front_3d"), res.get("drawing_sheet_opened"),
           res.get("drawing_camera"), res.get("drawing_camera_after")])
    check("a drawing whose sheet is in front keeps the sheet in front, and the 3D camera",
          res.get("drawing_sheet_kept") is True, res.get("drawing_sheet_detail"))
    check("stopping prints the new state", res.get("stopped") is True
          and (res.get("stop_message") or "").startswith("stopped — ")
          and "run the MCP_Reload macro to start it again" in (res.get("stop_message") or ""), res.get("stop_message"))

    print("existing GUI checks against the source addon")
    rl = os.path.join(tmp, "rl")
    os.makedirs(rl)
    seed = os.path.join(tmp, "seed.py")
    with open(seed, "w") as fh:
        fh.write("import sys; sys.path.append(%r)\nimport FreeCAD as App\nd = App.newDocument('base')\n"
                 "d.addObject('Part::Box', 'Box')\nd.recompute()\nd.saveAs(%r)\n"
                 % (os.path.join(RES, "lib"), os.path.join(rl, "base.FCStd")))
    subprocess.run([os.path.join(RES, "bin", "python"), "-I", seed], capture_output=True, timeout=120)
    out = os.path.join(rl, "out.json")
    res = gui(os.path.join(HERE, "gui", "reload_check.py"),
              {"RELOAD_ADDON_DIR": MOD, "RELOAD_WORKDIR": rl, "RELOAD_PY": os.path.join(RES, "bin", "python"),
               "RELOAD_LIB": os.path.join(RES, "lib"), "RELOAD_OUT": out, "OUT": out})
    check("reload_check: an outside rebuild reloads the open document, keeping the camera",
          res.get("reloaded") is True and res.get("camera_kept") is True, res)
    check("reload_check: own save ignored, unsaved transaction edit protected, toggle stops",
          res.get("own_save_ignored") is True and res.get("unsaved_protected") is True
          and res.get("stopped") is True, res)
    udata = os.path.join(tmp, "udata")
    shutil.copytree(MOD, os.path.join(udata, "Mod", "FreeCADMCPReload"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = os.path.join(tmp, "autostart.json")
    res = gui(os.path.join(HERE, "gui", "autostart_check.py"), {"FREECAD_USER_DATA": udata, "OUT": out})
    check("the installed addon starts the watcher with FreeCAD (PySide shim)", res.get("running") is True
          and res.get("user_data", "").startswith(udata), res)
    st = os.path.join(tmp, "startup")
    os.makedirs(st)
    with open(seed, "w") as fh:
        fh.write("import sys; sys.path.append(%r)\nimport FreeCAD as App\n" % os.path.join(RES, "lib")
                 + "".join("d = App.newDocument(%r)\nd.addObject('Part::Box', 'Box')\nd.recompute()\nd.saveAs(%r)\n"
                           % (n, os.path.join(st, n + ".FCStd")) for n in ("a", "b")))
    subprocess.run([os.path.join(RES, "bin", "python"), "-I", seed], capture_output=True, timeout=120)
    out = os.path.join(tmp, "startup.json")
    res = gui(os.path.join(HERE, "gui", "reload_startup_check.py"),
              {"FREECAD_USER_DATA": udata, "RELOAD_WORKDIR": st, "RELOAD_PY": os.path.join(RES, "bin", "python"),
               "RELOAD_LIB": os.path.join(RES, "lib"), "OUT": out})
    short = {k: v for k, v in res.items() if k != "messages"}
    check("startup check ran without errors", "error" not in res and not res.get("edit_errors"),
          res.get("error") or res.get("edit_errors") or res.get("log"))
    check("the installed addon follows edits from start-up: a console edit to a file opened before the watcher "
          "started is protected", res.get("tracking_at_script_start") is True and res.get("watcher_started") is True
          and res.get("b_undo_count") == 0 and res.get("b_warned") is True and res.get("b_protected") is True, short)
    check("…while the untouched file reloads (FreeCAD marks the first file of a session modified by itself)",
          res.get("a_reloaded") is True, short)
    out = os.path.join(tmp, "macro.json")
    res = gui(os.path.join(HERE, "gui", "macro_check.py"),
              {"MACRO": os.path.join(ADDON, "MCP_Reload.FCMacro"), "OUT": out})
    check("the macro toggles the watcher on and off", res.get("after_first_run") is True
          and res.get("after_second_run") is False, res)
    m = res.get("messages") or []
    check("the macro prints the new state each time",
          len(m) == 2 and m[0].startswith("MCP Reload: watching ") and "run the MCP_Reload macro to stop" in m[0]
          and m[1].startswith("MCP Reload: stopped — "), m)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
