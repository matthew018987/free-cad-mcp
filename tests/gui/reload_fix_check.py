# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the FreeCAD GUI (private user.cfg and user folder) to test the
MCP Reload watcher's protection rules: console edits (also right after opening,
before the watcher starts, while it is stopped, and after File ▸ Save a Copy),
edit mode, task panels, dialogs, background documents, stale-at-start documents
and reopened files.

env: RELOAD_ADDON_DIR (addon folder), RELOAD_WORKDIR (empty scratch dir),
     RELOAD_PY (FreeCAD's bundled python), RELOAD_LIB (its lib dir), OUT (result JSON)
"""
import json
import os
import shutil
import subprocess
import sys
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


def make(stem):
    """A saved document with one box, closed again; returns its path."""
    path = os.path.join(WORK, stem + ".FCStd")
    d = App.newDocument(stem)
    d.addObject("Part::Box", "Box")
    d.recompute()
    d.saveAs(path)
    App.closeDocument(d.Name)
    return path


def make_drawing(stem, extra=None, folder=WORK):
    """A document saved by this GUI (so its sheet opens with it) with a box, a TechDraw sheet, one view
    and one dimension, plus a box labelled `extra`; returns its path."""
    path = os.path.join(folder, stem + ".FCStd")
    d = App.newDocument(stem)
    box = d.addObject("Part::Box", "Box")
    if extra:
        d.addObject("Part::Box", extra)
    page = d.addObject("TechDraw::DrawPage", "Page")
    tpl = d.addObject("TechDraw::DrawSVGTemplate", "Template")
    tpl.Template = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg")
    page.Template = tpl
    view = d.addObject("TechDraw::DrawViewPart", "View")
    page.addView(view)
    view.Source = [box]
    d.recompute()
    until(lambda: bool(view.getVisibleEdges()), 10)      # hidden-line removal runs in a thread
    dim = d.addObject("TechDraw::DrawViewDimension", "Dim")
    dim.Type = "DistanceX"
    dim.References2D = [(view, "Edge0")]
    page.addView(dim)
    d.recompute()
    pump(0.5)
    d.saveAs(path)
    App.closeDocument(d.Name)
    return path


def drawing_rebuilds(path, labels):
    """Rebuilt versions of a drawing, made up front (making one changes the active document)."""
    out = {}
    for label in labels:
        folder = os.path.join(WORK, "rebuilds", label)
        os.makedirs(folder)
        out[label] = make_drawing(os.path.splitext(os.path.basename(path))[0], extra=label, folder=folder)
    return out


def gui_rebuild(path, src):
    """Simulate the server's drawing worker (a FreeCAD GUI) rewriting a drawing: the new file opens
    its sheet when it is loaded."""
    shutil.copyfile(src, path + ".tmp")
    os.replace(path + ".tmp", path)


def front():
    """Which view is in front: 'page:<name>' for a drawing sheet, '3d' for a 3D view."""
    v = Gui.getMainWindow().getActiveWindow()
    try:
        return "page:" + v.getPage().Name
    except Exception:  # noqa: BLE001
        return "3d" if hasattr(v, "getCamera") else type(v).__name__


def camera(path):
    """The orientation of the document's 3D view."""
    v = Gui.getDocument(doc_of(path).Name).mdiViewsOfType("Gui::View3DInventor")[0]
    return [round(float(x), 4) for x in v.getCamera().split("orientation")[1].split("\n")[0].split()]


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


def said(path, text):
    base = os.path.basename(path)
    return [m for m in msgs if base in m and text in m]


def mdi_title():
    area = Gui.getMainWindow().findChild(QtWidgets.QMdiArea)
    sub = area.activeSubWindow() if area is not None else None
    return sub.windowTitle() if sub is not None else None


try:
    mw = Gui.getMainWindow()
    if mw is not None:
        mw.showMinimized()
    sys.path.insert(0, os.environ["RELOAD_ADDON_DIR"])
    import mcp_reload
    mcp_reload._say = msgs.append
    os.environ["FCMCP_REQ"] = "x"    # F9: inert inside the server's own FreeCAD workers
    res["inert_in_worker"] = mcp_reload.start(poll_ms=150) is None and not mcp_reload.running()
    os.environ.pop("FCMCP_REQ", None)

    names = ("warmup", "stale", "fresh", "early", "console", "quick", "copy", "prop", "saved", "inedit", "task",
             "modal", "bg", "fg", "reopen", "noise", "aborted", "committed", "paused", "pausedclean")
    P = {n: make(n) for n in names}
    # the first file opened in a session gets marked modified by FreeCAD itself (view colours set as its
    # workbench loads): open one first, so "stale" and "fresh" below are clean
    d = App.openDocument(P["warmup"])
    pump(1.5)
    App.closeDocument(d.Name)

    # open before the watcher starts: "stale" is rebuilt meanwhile, "fresh" is not; "early" gets a console
    # edit (no undo step) before edits are followed at all, and is rebuilt meanwhile too
    App.openDocument(P["stale"])
    App.openDocument(P["fresh"])
    d = App.openDocument(P["early"])
    pump(0.5)
    d.addObject("Part::Sphere", "EarlyWork")
    d.recompute()
    pump(1.2)                        # LastModifiedDate has 1 s resolution
    external_edit(P["stale"], "AddedWhileStopped")
    external_edit(P["early"], "AddedWhileStopped")
    res["stale_open_copy_is_old"] = not has(P["stale"], "AddedWhileStopped")
    res["stale_modified_before_start"] = bool(Gui.getDocument(doc_of(P["stale"]).Name).Modified)

    w = mcp_reload.start(poll_ms=150)
    res["start_message"] = msgs[-1] if msgs else None
    res["start_counts_documents"] = res["start_message"] == \
        "watching 3 open document(s) — run the MCP_Reload macro to stop"

    # F6: a document already stale at start is reloaded; the fresh one is not
    res["stale_at_start_reloaded"] = until(lambda: has(P["stale"], "AddedWhileStopped"), 10)
    pump(1.0)
    res["fresh_not_reloaded"] = w.reloads == 1
    # #0: …but not one already marked modified when edits started being followed: warned, kept
    res["early_modified_kept"] = has(P["early"], "EarlyWork") and not has(P["early"], "AddedWhileStopped")
    res["early_message"] = (said(P["early"], "unsaved changes") or [None])[0]
    res["early_warned"] = bool(res["early_message"]) and "already marked modified when MCP Reload started" \
        in res["early_message"] and "Close it WITHOUT saving (or File ▸ Revert)" in res["early_message"]
    # …until it is saved in FreeCAD: its edits are followed from then on, so the next rebuild reloads it
    doc_of(P["early"]).save()
    pump(0.5)
    n0 = w.reloads
    external_edit(P["early"], "Rebuild2")
    res["early_saved_then_reloaded"] = until(lambda: has(P["early"], "Rebuild2"), 10) and w.reloads == n0 + 1 \
        and has(P["early"], "EarlyWork")

    # F3: a Python-console edit (no transaction, no undo step) is protected
    d = App.openDocument(P["console"])
    pump(1.0)                        # the watcher has seen the file
    d.addObject("Part::Sphere", "ConsoleWork")
    d.recompute()
    res["console_edit_undo_count"] = d.UndoCount
    n0 = w.reloads
    external_edit(P["console"], "Rebuild1")
    res["console_warned"] = until(lambda: bool(said(P["console"], "unsaved changes")), 8)
    res["console_protected"] = has(P["console"], "ConsoleWork") and not has(P["console"], "Rebuild1") \
        and w.reloads == n0
    res["conflict_message"] = (said(P["console"], "unsaved changes") or [None])[0]
    res["conflict_advice"] = bool(res["conflict_message"]) and \
        "Close it WITHOUT saving (or File ▸ Revert) to load the new version" in res["conflict_message"] \
        and "saving would overwrite Claude's rebuild" in res["conflict_message"]
    external_edit(P["console"], "Rebuild2")      # another rebuild: one more line, still protected
    res["console_rewarned"] = until(lambda: len(said(P["console"], "unsaved changes")) == 2, 8)
    res["console_still_protected"] = has(P["console"], "ConsoleWork") and w.reloads == n0
    # F7: following the advice (close without saving, open again) loads the rebuild, with no second reload
    App.closeDocument(doc_of(P["console"]).Name)
    App.openDocument(P["console"])
    pump(2.0)
    res["console_reopened_fresh"] = has(P["console"], "Rebuild2") and not has(P["console"], "ConsoleWork") \
        and w.reloads == n0

    # #0: a console edit made right after opening, before the watcher's next ticks, is protected
    d = App.openDocument(P["quick"])
    d.addObject("Part::Sphere", "QuickWork")
    d.recompute()
    pump(0.2)                        # the watcher sees the file before the rebuild arrives
    n0 = w.reloads
    external_edit(P["quick"], "Rebuild")
    res["quick_warned"] = until(lambda: bool(said(P["quick"], "you have unsaved changes")), 8)
    res["quick_protected"] = has(P["quick"], "QuickWork") and not has(P["quick"], "Rebuild") and w.reloads == n0

    # #41: File ▸ Save a Copy keeps the open document's console edit protected
    d = App.openDocument(P["copy"])
    pump(1.0)
    d.addObject("Part::Sphere", "CopyWork")
    d.recompute()
    os.makedirs(os.path.join(WORK, "snapshots"))
    d.saveCopy(os.path.join(WORK, "snapshots", "copy.FCStd"))
    pump(0.5)
    n0 = w.reloads
    external_edit(P["copy"], "Rebuild")
    res["save_copy_warned"] = until(lambda: bool(said(P["copy"], "you have unsaved changes")), 8)
    res["save_copy_protected"] = has(P["copy"], "CopyWork") and not has(P["copy"], "Rebuild") and w.reloads == n0

    # F3: a console property change (no new object) counts too
    d = App.openDocument(P["prop"])
    pump(1.0)
    d.getObject("Box").Length = 33
    n0 = w.reloads
    external_edit(P["prop"], "Rebuild")
    pump(3.0)
    res["property_edit_protected"] = not has(P["prop"], "Rebuild") and w.reloads == n0 \
        and bool(said(P["prop"], "unsaved changes"))

    # F3: saving in FreeCAD clears the edit state — the next rebuild reloads
    d = App.openDocument(P["saved"])
    pump(1.0)
    d.addObject("Part::Sphere", "SavedWork")
    d.recompute()
    d.save()
    pump(0.5)
    n0 = w.reloads
    external_edit(P["saved"], "Rebuild")
    res["saved_then_reloaded"] = until(lambda: has(P["saved"], "Rebuild") and has(P["saved"], "SavedWork"), 10) \
        and w.reloads == n0 + 1

    # F3: load-time and recompute-only changes and view toggles are not edits
    d = App.openDocument(P["noise"])
    d.getObject("Box").touch()       # like FreeCAD recomputing a document on load
    d.recompute()
    pump(1.0)
    d.getObject("Box").Visibility = False
    d.getObject("Box").touch()
    d.recompute()
    pump(0.3)
    n0 = w.reloads
    external_edit(P["noise"], "Rebuild")
    res["noise_not_an_edit"] = until(lambda: has(P["noise"], "Rebuild"), 10) and w.reloads == n0 + 1

    # F4: edit mode defers the reload until the user leaves it
    d = App.openDocument(P["inedit"])
    pump(1.0)
    gd = Gui.getDocument(d.Name)
    gd.setEdit("Box")
    pump(0.5)
    res["in_edit"] = gd.getInEdit() is not None
    n0 = w.reloads
    external_edit(P["inedit"], "Rebuild")
    pump(2.5)
    res["edit_mode_deferred"] = not has(P["inedit"], "Rebuild") and w.reloads == n0 \
        and bool(said(P["inedit"], "reloads as soon as you leave edit mode"))
    res["edit_mode_message"] = (said(P["inedit"], "rebuilt") or [None])[0]
    Gui.getDocument(doc_of(P["inedit"]).Name).resetEdit()
    if Gui.Control.activeDialog():
        Gui.Control.closeDialog()
    res["edit_mode_then_reloaded"] = until(lambda: has(P["inedit"], "Rebuild"), 10)

    # F4: an open task panel defers the reload
    class Panel:
        def __init__(self):
            self.form = QtWidgets.QWidget()

    App.openDocument(P["task"])
    pump(1.0)
    Gui.Control.showDialog(Panel())
    pump(0.3)
    n0 = w.reloads
    external_edit(P["task"], "Rebuild")
    pump(2.5)
    res["task_panel_deferred"] = not has(P["task"], "Rebuild") and w.reloads == n0 \
        and bool(said(P["task"], "task panel"))
    Gui.Control.closeDialog()
    res["task_panel_then_reloaded"] = until(lambda: has(P["task"], "Rebuild"), 10)

    # F4: a modal dialog defers the reload
    App.openDocument(P["modal"])
    pump(1.0)
    dlg = QtWidgets.QDialog(mw)
    dlg.setModal(True)
    dlg.show()
    pump(0.3)
    n0 = w.reloads
    external_edit(P["modal"], "Rebuild")
    pump(2.5)
    res["modal_deferred"] = not has(P["modal"], "Rebuild") and w.reloads == n0 \
        and bool(said(P["modal"], "dialog is closed"))
    dlg.close()
    res["modal_then_reloaded"] = until(lambda: has(P["modal"], "Rebuild"), 10)

    # F5: reloading a background document keeps the active document and view
    bg = App.openDocument(P["bg"])
    fg = App.openDocument(P["fg"])
    pump(0.5)
    mw.setActiveWindow(Gui.getDocument(fg.Name).ActiveView)
    pump(0.5)
    before = (App.ActiveDocument.Name, Gui.ActiveDocument.Document.Name, mdi_title())
    res["focus_before"] = list(before)
    n0 = w.reloads
    external_edit(P["bg"], "Rebuild")
    res["background_reloaded"] = until(lambda: has(P["bg"], "Rebuild"), 10) and w.reloads == n0 + 1
    pump(0.5)
    after = (App.ActiveDocument.Name, Gui.ActiveDocument.Document.Name, mdi_title())
    res["focus_after"] = list(after)
    fg_name = fg.Name
    res["background_kept_focus"] = before == after and before[0] == fg_name
    # …and the active document itself stays active when it is reloaded
    n0 = w.reloads
    external_edit(P["fg"], "Rebuild")
    res["foreground_reloaded"] = until(lambda: has(P["fg"], "Rebuild"), 10) and w.reloads == n0 + 1
    pump(0.5)
    res["foreground_still_active"] = App.ActiveDocument is not None and App.ActiveDocument.Name == fg_name \
        and App.ActiveDocument.getObject("Rebuild") is not None

    # F7: closing a document, rebuilding it, and opening it again is not a reload
    d = App.openDocument(P["reopen"])
    pump(1.0)
    App.closeDocument(d.Name)
    pump(0.5)
    external_edit(P["reopen"], "Rebuild")
    n0 = w.reloads
    App.openDocument(P["reopen"])
    pump(2.5)
    res["reopen_no_double_reload"] = has(P["reopen"], "Rebuild") and w.reloads == n0 \
        and not any(os.path.basename(P["reopen"]) in m for m in msgs)

    # F3: a command that was cancelled (its transaction aborted, e.g. a task panel's Cancel) is not an edit…
    d = App.openDocument(P["aborted"])
    pump(1.0)
    App.setActiveTransaction("Edit")             # what a GUI command does
    d.getObject("Box").Length = 20
    d.recompute()
    App.closeActiveTransaction(True)
    n0 = w.reloads
    external_edit(P["aborted"], "Rebuild")
    res["aborted_command_not_an_edit"] = until(lambda: has(P["aborted"], "Rebuild"), 10) and w.reloads == n0 + 1 \
        and not said(P["aborted"], "unsaved changes")
    # …while a committed one is protected (it leaves an undo step)
    d = App.openDocument(P["committed"])
    pump(1.0)
    App.setActiveTransaction("Edit")
    d.getObject("Box").Length = 20
    d.recompute()
    App.closeActiveTransaction()
    n0 = w.reloads
    external_edit(P["committed"], "Rebuild")
    pump(3.0)
    res["committed_command_protected"] = not has(P["committed"], "Rebuild") and w.reloads == n0 \
        and bool(said(P["committed"], "unsaved changes"))

    # F3: TechDraw re-matching dimension references after its threaded hidden-line pass (seconds after a
    # drawing is loaded) rewrites SavedGeometry, BoxCorners and References2D: not the user's edits
    P["drawing"] = make_drawing("drawing")
    V = drawing_rebuilds(P["drawing"], ("Rebuild2", "Rebuild3"))
    d = App.openDocument(P["drawing"])
    pump(1.0)
    res["techdraw_modified_by_load"] = bool(Gui.getDocument(d.Name).Modified)   # FreeCAD flags it itself
    dim = d.getObject("Dim")
    dim.SavedGeometry = list(dim.SavedGeometry)
    dim.BoxCorners = list(dim.BoxCorners)
    dim.References2D = dim.References2D
    n0 = w.reloads
    external_edit(P["drawing"], "Rebuild1")
    res["techdraw_rematch_not_an_edit"] = until(lambda: has(P["drawing"], "Rebuild1"), 10) \
        and w.reloads == n0 + 1 and not said(P["drawing"], "unsaved changes")

    # F5: the view in front stays in front (reopening a drawing shows its sheet), the 3D camera is kept
    pump(1.0)
    gd = Gui.getDocument(doc_of(P["drawing"]).Name)
    v3 = gd.mdiViewsOfType("Gui::View3DInventor")[0]
    mw.setActiveWindow(v3)
    v3.setAnimationEnabled(False)    # read the final camera, not one mid-animation
    v3.viewRight()
    pump(0.3)
    cam = camera(P["drawing"])
    res["drawing_camera"] = cam
    res["drawing_front_before"] = front()
    n0 = w.reloads
    gui_rebuild(P["drawing"], V["Rebuild2"])
    reloaded = until(lambda: has(P["drawing"], "Rebuild2"), 10) and w.reloads == n0 + 1
    pump(0.5)
    res["drawing_front_3d"] = front()
    res["drawing_camera_after"] = camera(P["drawing"])
    res["drawing_3d_kept"] = reloaded and res["drawing_front_before"] == "3d" and front() == "3d" \
        and App.ActiveDocument.Name == doc_of(P["drawing"]).Name and camera(P["drawing"]) == cam
    res["drawing_sheet_opened"] = Gui.getDocument(doc_of(P["drawing"]).Name).mdiViewsOfType(
        "TechDrawGui::MDIViewPage") != []
    # closing and opening the sheet again, as a user would: TechDraw sets the template's size again
    vo = doc_of(P["drawing"]).getObject("Page").ViewObject
    vo.hide()
    pump(0.3)
    vo.show()
    pump(0.3)
    edited = w.edits.edited if hasattr(w, "edits") else w.edited     # (the watcher kept it before round 4)
    res["sheet_open_not_an_edit"] = doc_of(P["drawing"]).Name not in edited
    mw.setActiveWindow(Gui.getDocument(doc_of(P["drawing"]).Name).mdiViewsOfType("TechDrawGui::MDIViewPage")[0])
    pump(0.3)
    res["drawing_front_page_before"] = front()
    n0 = w.reloads
    gui_rebuild(P["drawing"], V["Rebuild3"])
    reloaded = until(lambda: has(P["drawing"], "Rebuild3"), 10) and w.reloads == n0 + 1
    pump(0.5)
    res["drawing_front_page"] = front()
    res["drawing_sheet_detail"] = [reloaded, res["drawing_front_page_before"], front(), App.ActiveDocument.Name,
                                   doc_of(P["drawing"]).Name, camera(P["drawing"]) == cam]
    res["drawing_sheet_kept"] = res["drawing_sheet_detail"][:3] == [True, "page:Page", "page:Page"] \
        and res["drawing_sheet_detail"][3] == res["drawing_sheet_detail"][4] and res["drawing_sheet_detail"][5]

    # #0: edits made while the watcher is stopped (macro) still count when it starts again; a clean document
    # rebuilt meanwhile reloads at the restart
    d = App.openDocument(P["paused"])
    App.openDocument(P["pausedclean"])
    pump(1.0)
    mcp_reload.toggle()
    res["paused_stopped"] = not mcp_reload.running()
    d.addObject("Part::Sphere", "PausedWork")
    d.recompute()
    pump(1.2)                        # LastModifiedDate has 1 s resolution
    external_edit(P["paused"], "Rebuild")
    external_edit(P["pausedclean"], "Rebuild")
    mcp_reload.toggle()
    w = mcp_reload.start()           # the running watcher
    res["paused_clean_reloaded"] = until(lambda: has(P["pausedclean"], "Rebuild"), 10)
    res["paused_warned"] = until(lambda: bool(said(P["paused"], "you have unsaved changes")), 8)
    res["paused_protected"] = has(P["paused"], "PausedWork") and not has(P["paused"], "Rebuild")

    # F8: stopping prints the new state
    mcp_reload.toggle()
    res["stopped"] = not mcp_reload.running()
    res["stop_message"] = msgs[-1] if msgs else None
    res["messages"] = msgs
except Exception:  # noqa: BLE001
    import traceback
    res["error"] = traceback.format_exc()
    res["messages"] = msgs
finally:
    with open(os.environ["OUT"], "w") as fh:
        json.dump(res, fh)
    os._exit(0)
