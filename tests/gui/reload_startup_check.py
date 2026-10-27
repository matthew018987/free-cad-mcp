# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE a FreeCAD GUI whose user folder holds the addon (FREECAD_USER_DATA):
files opened right after start-up, before the watcher starts (2 s later), are
covered — a console edit made then is protected, and a document FreeCAD marks
modified by itself (the first file of a session) still reloads.

env: RELOAD_WORKDIR (with a.FCStd and b.FCStd), RELOAD_PY (FreeCAD's bundled
     python), RELOAD_LIB (its lib dir), OUT (result JSON)
"""
import json
import os
import subprocess
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtWidgets

res = {}
msgs = []
WORK = os.environ["RELOAD_WORKDIR"]


def pump(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)


def until(pred, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        pump(0.1)
        if pred():
            return True
    return False


def external_edit(path, label):
    """Simulate the MCP server rebuilding the file: another process adds an object and saves."""
    code = ("import sys; sys.path.append(%r)\nimport FreeCAD as App\nd = App.openDocument(%r)\n"
            "d.addObject('Part::Box', %r)\nd.recompute()\nd.save()\n" % (os.environ["RELOAD_LIB"], path, label))
    r = subprocess.run([os.environ["RELOAD_PY"], "-I", "-c", code], capture_output=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if not k.startswith(("FCMCP_", "PYTHON"))})
    if r.returncode:
        res.setdefault("edit_errors", []).append(r.stderr.decode("utf-8", "replace")[-400:])


def doc_of(path):
    for d in App.listDocuments().values():
        if d.FileName and os.path.abspath(d.FileName) == os.path.abspath(path):
            return d
    return None


def has(path, label):
    d = doc_of(path)
    return d is not None and d.getObject(label) is not None


try:
    res["tracking_at_script_start"] = getattr(Gui, "_mcp_reload_edits", None) is not None
    res["watcher_running_at_script_start"] = getattr(Gui, "_mcp_reload_watcher", None) is not None
    os.environ.pop("FCMCP_REQ", None)
    import mcp_reload               # the addon's module (InitGui imported it)
    mcp_reload._say = msgs.append
    a, b = os.path.join(WORK, "a.FCStd"), os.path.join(WORK, "b.FCStd")
    da = App.openDocument(a)
    db = App.openDocument(b)
    db.addObject("Part::Sphere", "ConsoleWork")   # a console edit: no undo step
    db.recompute()
    pump(1.2)                        # LastModifiedDate has 1 s resolution
    res["a_modified_by_freecad"] = bool(Gui.getDocument(da.Name).Modified)
    res["b_undo_count"] = db.UndoCount
    external_edit(a, "Rebuild")
    external_edit(b, "Rebuild")
    res["watcher_started"] = until(mcp_reload.running, 10)
    res["a_reloaded"] = until(lambda: has(a, "Rebuild"), 10)
    res["b_warned"] = until(lambda: any("b.FCStd" in m and "you have unsaved changes" in m for m in msgs), 8)
    res["b_protected"] = has(b, "ConsoleWork") and not has(b, "Rebuild")
    res["messages"] = msgs
except Exception:  # noqa: BLE001
    import traceback
    res["error"] = traceback.format_exc()
    res["messages"] = msgs
finally:
    with open(os.environ["OUT"], "w") as fh:
        json.dump(res, fh)
    os._exit(0)
