# SPDX-License-Identifier: Apache-2.0
"""
MCP Reload — keep the FreeCAD GUI in step with files rebuilt by the FreeCAD MCP
server.

When Claude rebuilds a model (build/<name>.FCStd) or regenerates a drawing
(drawings/<part>.FCStd), the copy open in FreeCAD goes stale: FreeCAD never
re-reads a file it already has open. This watcher polls the files of every
open document and reloads one when it changes on disk, keeping the camera.

  * A document with unsaved changes in the GUI is never reloaded. The Report
    view says so: close it WITHOUT saving (or File ▸ Revert) to load the new
    version — saving would overwrite Claude's rebuild.
  * Edits count from the moment a document is loaded or saved, whether made
    through a command, the Python console or a macro — also while the watcher
    is stopped. They are followed from FreeCAD start-up with the addon, or from
    the first run of the macro; a document that FreeCAD already marks modified
    then is treated as having unsaved changes until you save, revert or close it.
  * It waits while you work: no reload while the document is in edit mode, a
    task panel is open or a dialog is up — it reloads when you close them.
  * Reloading keeps the camera and the view in front (a drawing sheet stays in
    front); reloading a background document keeps your active document and view.
  * A document that was already stale when the watcher started (the file on
    disk is newer than the open copy) is reloaded too — unless it has unsaved
    changes.
  * Saves made by FreeCAD itself are recognised and ignored; File ▸ Save a Copy
    leaves the open document's unsaved changes protected.
  * Inside the MCP server's own FreeCAD workers it does nothing.

Run the MCP_Reload macro to toggle it, or install the FreeCADMCPReload addon to
start it with FreeCAD.
"""

import datetime
import os
import re
import zipfile

import FreeCAD as App

POLL_MS = 1000
_SETTLE_S = 0.6          # wait for a writer to finish before reading the file
_STALE = (0, -1)         # stamp for a document found stale at start: reload it on the next ticks
_EDITS_API = 1           # interface of the edit tracker shared by the addon and the macro (Gui._mcp_reload_edits)
# Property changes that are not the user's edits: Visibility & co. (NoModify in FreeCAD too), results
# and read-only state such as a drawing template's size, which TechDraw sets again when a sheet is shown
_NOT_EDITS = frozenset(("NoModify", "Output", "ReadOnly"))
# TechDraw rewrites these itself when it re-matches dimension references after its threaded
# hidden-line pass, which can finish seconds after a drawing is loaded
_TECHDRAW_AUTO = frozenset(("SavedGeometry", "BoxCorners", "References2D"))


def _say(msg):
    App.Console.PrintMessage("MCP Reload: %s\n" % msg)


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _when(text):
    """FreeCAD's LastModifiedDate ('2026-10-25T21:17:16+08:00') as a datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _file_date(path):
    """LastModifiedDate stored in an .FCStd file's Document.xml, or None."""
    try:
        with zipfile.ZipFile(path) as z, z.open("Document.xml") as fh:
            head = fh.read(1 << 20).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — not a zip, unreadable, being written
        return None
    m = re.search(r'name="LastModifiedDate"[^>]*>\s*<String value="([^"]*)"', head)
    return _when(m.group(1)) if m else None


def _views(gd, kind):
    """A GUI document's views of one type (Gui::View3DInventor, TechDrawGui::MDIViewPage)."""
    try:
        return list(gd.mdiViewsOfType(kind))
    except Exception:  # noqa: BLE001 — no GUI document, or that workbench is not loaded
        return []


def _sheet(gd, page):
    """The open view of the drawing sheet (DrawPage) called page, or None."""
    for v in _views(gd, "TechDrawGui::MDIViewPage"):
        if v.getPage().Name == page:
            return v
    return None


def _own_file(doc, filename):
    """True when a save wrote the document's own file — not File ▸ Save a Copy, which leaves the open
    document (and its unsaved changes) as it was."""
    try:
        return bool(doc.FileName) and os.path.realpath(filename) == os.path.realpath(doc.FileName)
    except Exception:  # noqa: BLE001
        return False


def _stale(doc, path):
    """True when the file on disk was saved after the open copy was loaded or saved."""
    disk, mine = _file_date(path), _when(getattr(doc, "LastModifiedDate", ""))
    try:
        return bool(disk and mine and disk > mine)
    except TypeError:        # one has a time zone, the other not
        return False


class _Edits:
    """Which open documents were changed since they were loaded or saved (method names are FreeCAD's
    document observer API). One per FreeCAD session, shared by the addon and the macro: it starts with the
    addon at FreeCAD start-up (or with the first start of the watcher) and keeps running while the watcher
    is stopped, so a restart still knows the edits made meanwhile."""

    api = _EDITS_API

    def __init__(self):
        import FreeCADGui as Gui
        self.edited = set()      # document names changed since they were loaded or saved
        self.known = set()       # document names followed since they were clean: every edit is in `edited`
        for d in App.listDocuments().values():
            try:                 # one already marked modified may hold edits nobody saw: not known
                gd = Gui.getDocument(d.Name)
                if gd is not None and not gd.Modified:
                    self.known.add(d.Name)
            except Exception:  # noqa: BLE001
                pass
        App.addDocumentObserver(self)

    # keep these cheap: slotChangedObject fires for every property

    def slotCreatedDocument(self, doc):  # noqa: N802 — opened, reopened (File ▸ Revert) or new
        self.edited.discard(doc.Name)
        self.known.add(doc.Name)

    def slotDeletedDocument(self, doc):  # noqa: N802
        self.edited.discard(doc.Name)
        self.known.discard(doc.Name)

    def slotCreatedObject(self, obj):  # noqa: N802
        self._touched(obj)

    def slotDeletedObject(self, obj):  # noqa: N802
        self._touched(obj)

    def slotChangedObject(self, obj, prop):  # noqa: N802
        self._touched(obj, prop)

    def slotFinishSaveDocument(self, doc, filename):  # noqa: N802
        if _own_file(doc, filename):
            self.edited.discard(doc.Name)
            self.known.add(doc.Name)

    def _touched(self, obj, prop=None):
        try:
            doc = obj.Document
            name = doc.Name
            if name in self.edited:
                return
            # opening a file (restore) and recompute only rewrite outputs. Commands, undo/redo and cancelled
            # task panels run in a transaction: what they leave behind shows in the undo count (see _unsaved)
            if getattr(doc, "Restoring", False) or getattr(doc, "Recomputing", False) \
                    or getattr(doc, "HasPendingTransaction", False):
                return
            if prop and ((prop in _TECHDRAW_AUTO and obj.TypeId.startswith("TechDraw::"))
                         or not _NOT_EDITS.isdisjoint(obj.getPropertyStatus(prop))):
                return
        except Exception:  # noqa: BLE001 — object half-deleted, property gone …
            return
        self.edited.add(name)


class _DocObserver:
    """Records FreeCAD's own saves and document closes for the watcher."""

    def __init__(self, watcher):
        self.w = watcher

    def slotDeletedDocument(self, doc):  # noqa: N802
        self.w._closed(doc)

    def slotFinishSaveDocument(self, doc, filename):  # noqa: N802
        self.w._saved(doc, filename)


class Watcher:
    def __init__(self, poll_ms=POLL_MS):
        from PySide import QtCore   # FreeCAD's shim: PySide6 or PySide2, whichever this build uses
        self.seen = {}               # path -> stamp FreeCAD last loaded or saved
        self.pending = {}            # path -> (stamp, first time seen changed)
        self.warned = {}             # path -> stamp we said "unsaved changes" about
        self.deferred = {}           # path -> stamp we said "waiting for the editor/dialog" about
        self.edits = track()         # which documents have unsaved edits
        self.reloads = 0
        self._ticking = False
        self.observer = _DocObserver(self)
        App.addDocumentObserver(self.observer)
        for d in App.listDocuments().values():
            if d.FileName:
                path = os.path.abspath(d.FileName)
                self.seen[path] = _STALE if _stale(d, path) else _stamp(path)
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(poll_ms))

    def stop(self):
        self.timer.stop()
        try:
            App.removeDocumentObserver(self.observer)
        except Exception:  # noqa: BLE001
            pass

    # --- document observer callbacks

    def _closed(self, doc):
        try:
            path = os.path.abspath(doc.FileName) if doc.FileName else None
        except Exception:  # noqa: BLE001
            path = None
        if path:      # reopening the file later starts afresh — no spurious reload
            for d in (self.seen, self.pending, self.warned, self.deferred):
                d.pop(path, None)

    def _saved(self, doc, filename):
        if not _own_file(doc, filename):     # File ▸ Save a Copy: the open document is unchanged
            return
        path = os.path.abspath(filename)
        self.seen[path] = _stamp(path)
        for d in (self.pending, self.warned, self.deferred):
            d.pop(path, None)

    # --- polling

    def tick(self):
        if self._ticking:            # openDocument can pump events: never re-enter a reload
            return
        self._ticking = True
        try:
            self._tick()
        finally:
            self._ticking = False

    def _tick(self):
        import time
        try:
            docs = list(App.listDocuments().values())
        except Exception:  # noqa: BLE001
            return
        for d in docs:
            try:
                path = os.path.abspath(d.FileName) if d.FileName else None
            except Exception:  # noqa: BLE001 — closed by an earlier reload in this tick
                continue
            if not path:
                continue
            now = _stamp(path)
            if path not in self.seen:
                self.seen[path] = now           # newly opened: remember, don't reload
                continue
            if now is None or now == self.seen[path]:
                self.pending.pop(path, None)
                if now is not None:
                    self.warned.pop(path, None)
                    self.deferred.pop(path, None)
                continue
            # changed on disk: wait until the writer has settled
            first = self.pending.get(path)
            if first is None or first[0] != now:
                self.pending[path] = (now, time.time())
                continue
            if time.time() - first[1] < _SETTLE_S:
                continue
            base = os.path.basename(path)
            unsaved = self._unsaved(d)
            if unsaved:
                if self.warned.get(path) != now:
                    self.warned[path] = now
                    _say("%s was rebuilt by Claude, but %s. Close it WITHOUT saving (or File ▸ Revert) to load "
                         "the new version — saving would overwrite Claude's rebuild."
                         % (base, "you have unsaved changes here" if unsaved == "edited" else
                            "this copy was already marked modified when MCP Reload started, so it may have "
                            "unsaved changes"))
                continue
            why = self._busy(d)
            if why:                         # keep it pending: retried on every tick
                if self.deferred.get(path) != now:
                    self.deferred[path] = now
                    _say("%s was rebuilt by Claude — it reloads as soon as %s." % (base, why))
                continue
            for m in (self.pending, self.warned, self.deferred):
                m.pop(path, None)
            self.reload(d, path, now)

    def _unsaved(self, doc):
        """Whether a reload would throw away edits in the GUI copy: "edited" for changes seen since it was
        loaded or saved, or FreeCAD's Modified flag together with undo steps (or with undo off); "unknown"
        when it was already marked modified before its edits were followed; None when it is safe.
        Modified alone is not enough otherwise: TechDraw pages, for one, flag themselves on load."""
        name = doc.Name
        if name in self.edits.edited:
            return "edited"
        try:
            import FreeCADGui as Gui
            gd = Gui.getDocument(name)
            if not (gd and gd.Modified):
                return None
            if doc.UndoMode == 0 or doc.UndoCount > 0:
                return "edited"
            return None if name in self.edits.known else "unknown"
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _busy(doc):
        """Why a reload has to wait (None when it can go ahead): closing a document under an
        open editor, task panel or dialog loses the user's input or crashes FreeCAD."""
        import FreeCADGui as Gui
        try:
            gd = Gui.getDocument(doc.Name)
            if gd is not None and gd.getInEdit() is not None:
                return "you leave edit mode (close its task panel)"
        except Exception:  # noqa: BLE001
            pass
        try:
            if Gui.Control.activeDialog():
                return "the open task panel is closed (OK or Cancel)"
        except Exception:  # noqa: BLE001
            pass
        try:
            from PySide import QtWidgets
            if QtWidgets.QApplication.activeModalWidget() is not None:
                return "the open dialog is closed"
        except Exception:  # noqa: BLE001
            pass
        return None

    def reload(self, doc, path, stamp):
        import FreeCADGui as Gui
        name = doc.Name
        mw = Gui.getMainWindow()
        active = App.ActiveDocument.Name if App.ActiveDocument is not None else None
        view = cam = page = None
        try:
            view = mw.getActiveWindow()
        except Exception:  # noqa: BLE001
            pass
        try:            # the 3D view's camera, even while a drawing sheet is in front
            cam = _views(Gui.getDocument(name), "Gui::View3DInventor")[0].getCamera()
        except Exception:  # noqa: BLE001
            pass
        if active == name:
            try:
                page = view.getPage().Name   # the user is looking at a drawing sheet
            except Exception:  # noqa: BLE001 — a 3D view
                pass
        try:
            App.closeDocument(name)
            nd = App.openDocument(path)
        except Exception as e:  # noqa: BLE001
            _say("could not reload %s: %s — close it and open it again (File ▸ Open)." % (os.path.basename(path), e))
            self.seen[path] = stamp
            return
        self.seen[path] = _stamp(path)
        self.reloads += 1
        try:
            ngd = Gui.getDocument(nd.Name)
        except Exception:  # noqa: BLE001
            ngd = None
        try:
            if cam:
                _views(ngd, "Gui::View3DInventor")[0].setCamera(cam)
        except Exception:  # noqa: BLE001
            pass
        if active == name:              # the same view in front: opening a drawing shows its sheet
            try:
                App.setActiveDocument(nd.Name)
                if page:
                    front = _sheet(ngd, page)
                    if front is None and nd.getObject(page) is not None:
                        nd.getObject(page).ViewObject.show()   # a file saved without the GUI keeps it closed
                        front = _sheet(ngd, page)
                else:
                    front = (_views(ngd, "Gui::View3DInventor") or [None])[0]
                if front is not None:
                    mw.setActiveWindow(front)
            except Exception:  # noqa: BLE001
                pass
        else:                           # a background document: give the focus back
            try:
                if view is not None:
                    mw.setActiveWindow(view)
            except Exception:  # noqa: BLE001 — that view was closed meanwhile
                pass
            try:
                cur = App.ActiveDocument
                if active in App.listDocuments() and (cur is None or cur.Name != active):
                    App.setActiveDocument(active)
                    Gui.setActiveDocument(active)
            except Exception:  # noqa: BLE001
                pass
        _say("reloaded %s" % os.path.basename(path))


def in_mcp_worker():
    return bool(os.environ.get("FCMCP_REQ"))


def track():
    """Start following edits to open documents — once per FreeCAD session (the addon calls this at
    start-up; start() does too). Returns the shared tracker, or None inside the server's workers."""
    import FreeCADGui as Gui
    if in_mcp_worker():
        return None
    e = getattr(Gui, "_mcp_reload_edits", None)
    if getattr(e, "api", None) != _EDITS_API:      # none yet, or one left by an older copy of this file
        if e is not None:
            try:
                App.removeDocumentObserver(e)
            except Exception:  # noqa: BLE001
                pass
        e = Gui._mcp_reload_edits = _Edits()
    return e


def start(poll_ms=POLL_MS):
    import FreeCADGui as Gui
    if in_mcp_worker():
        return None
    w = getattr(Gui, "_mcp_reload_watcher", None)
    if w is None:
        w = Watcher(poll_ms)
        Gui._mcp_reload_watcher = w
        _say("watching %d open document(s) — run the MCP_Reload macro to stop" % len(w.seen))
    return w


def stop():
    import FreeCADGui as Gui
    w = getattr(Gui, "_mcp_reload_watcher", None)
    if w is not None:
        w.stop()
        Gui._mcp_reload_watcher = None
        _say("stopped — open documents no longer reload when Claude rebuilds them; run the MCP_Reload macro "
             "to start it again")


def toggle():
    import FreeCADGui as Gui
    if getattr(Gui, "_mcp_reload_watcher", None) is None:
        start()
    else:
        stop()


def running():
    import FreeCADGui as Gui
    return getattr(Gui, "_mcp_reload_watcher", None) is not None
