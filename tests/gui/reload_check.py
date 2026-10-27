# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the FreeCAD GUI (private user.cfg) to test the MCP Reload watcher.

env: RELOAD_ADDON_DIR (addon folder), RELOAD_WORKDIR (scratch dir with base.FCStd),
     RELOAD_PY (FreeCAD's bundled python), RELOAD_LIB (its lib dir), RELOAD_OUT (result JSON)
"""
import json
import os
import subprocess
import sys
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtWidgets


def cam_pose(cam):
    """Position and orientation of an Inventor camera string (near/far planes follow rendering, not the user)."""
    pose = []
    for key in ("position", "orientation"):
        line = cam.split(key, 1)[1].splitlines()[0] if key in cam else ""
        pose.append([round(float(v), 4) for v in line.split()])
    return pose


res = {}


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)


def external_edit(path, label):
    """Simulate the MCP server rebuilding the file: another process adds an object and saves."""
    code = ("import sys; sys.path.append(%r)\nimport FreeCAD as App\nd = App.openDocument(%r)\n"
            "d.addObject('Part::Box', %r)\nd.recompute()\nd.save()\n" % (os.environ["RELOAD_LIB"], path, label))
    r = subprocess.run([os.environ["RELOAD_PY"], "-I", "-c", code], capture_output=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if not k.startswith(("FCMCP_", "PYTHON"))})
    res.setdefault("edits", []).append({"label": label, "rc": r.returncode,
                                        "err": r.stderr.decode("utf-8", "replace")[-400:]})


def until(pred, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        pump(0.1)
        if pred():
            return True
    return False


try:
    mw = Gui.getMainWindow()
    if mw is not None:
        mw.showMinimized()
    sys.path.insert(0, os.environ["RELOAD_ADDON_DIR"])
    os.environ.pop("FCMCP_REQ", None)
    import mcp_reload
    # the addon follows edits from FreeCAD start-up, before any file opens (a file opened before that and
    # marked modified — FreeCAD marks the first file of a session itself — would be kept, not reloaded)
    getattr(mcp_reload, "track", lambda: None)()     # (an addon from before track() needs no call)
    work = os.environ["RELOAD_WORKDIR"]
    path = os.path.join(work, "base.FCStd")
    doc = App.openDocument(path)
    name = doc.Name
    view = Gui.getDocument(name).ActiveView
    view.viewTop()
    view.setCameraType("Orthographic")
    pump(0.5)
    cam_before = view.getCamera()
    res["modified_after_open"] = bool(Gui.getDocument(name).Modified)
    res["undo_after_open"] = doc.UndoCount
    res["undo_mode"] = doc.UndoMode
    w = mcp_reload.start(poll_ms=150)
    res["started"] = mcp_reload.running()

    # 1. an outside rebuild is reloaded, camera kept
    res["stamp_before"] = list(mcp_reload._stamp(path) or [])
    res["seen_keys"] = list(w.seen.keys())
    external_edit(path, "AddedByMCP")
    res["stamp_after"] = list(mcp_reload._stamp(path) or [])
    res["reloaded"] = until(lambda: App.getDocument(name) is not None
                            and App.getDocument(name).getObject("AddedByMCP") is not None, 15)
    pump(0.5)
    try:
        cam_after = Gui.getDocument(name).ActiveView.getCamera()
        res["camera_kept"] = cam_pose(cam_after) == cam_pose(cam_before)
        if not res["camera_kept"]:      # rare: keep both cameras so a failure can be diagnosed
            res["cameras"] = [cam_before, cam_after]
    except Exception as e:  # noqa: BLE001
        res["camera_kept"] = "error %s" % e
    res["reload_count_1"] = w.reloads

    # 2. FreeCAD's own save is not treated as an outside change
    d = App.getDocument(name)
    d.save()
    pump(2.0)
    res["own_save_ignored"] = w.reloads == res["reload_count_1"]

    # 3. unsaved GUI changes are protected
    d = App.getDocument(name)
    d.openTransaction("user edit")          # what a GUI command does
    d.addObject("Part::Sphere", "UnsavedWork")
    d.recompute()
    d.commitTransaction()
    pump(0.3)
    res["modified_flag"] = bool(Gui.getDocument(name).Modified)
    res["undo_after_user_edit"] = d.UndoCount
    external_edit(path, "SecondRebuild")
    pump(3.0)
    d = App.getDocument(name)
    res["unsaved_protected"] = d is not None and d.getObject("UnsavedWork") is not None \
        and d.getObject("SecondRebuild") is None and w.reloads == res["reload_count_1"]

    # 4. toggling stops it
    mcp_reload.toggle()
    res["stopped"] = not mcp_reload.running()
except Exception as e:  # noqa: BLE001
    import traceback
    res["error"] = traceback.format_exc()
finally:
    with open(os.environ["RELOAD_OUT"], "w") as fh:
        json.dump(res, fh)
    os._exit(0)
