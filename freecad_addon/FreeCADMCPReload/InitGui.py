# SPDX-License-Identifier: Apache-2.0
# FreeCAD MCP Reload addon: start the watcher with the GUI (see mcp_reload.py).
# FreeCAD runs InitGui.py with exec(), so this file has no __file__; the addon
# directory is already on sys.path.


def _start_mcp_reload():
    try:
        import mcp_reload
        mcp_reload.start()
    except Exception as e:  # noqa: BLE001 — never break FreeCAD start-up
        import FreeCAD
        FreeCAD.Console.PrintWarning("MCP Reload: not started (%s) — run the MCP_Reload macro to start it\n" % e)


try:     # follow edits from now on: a file FreeCAD opens at start-up is covered before the watcher starts
    import mcp_reload
    mcp_reload.track()
except Exception:  # noqa: BLE001 — start() tries again
    pass
try:
    from PySide import QtCore     # FreeCAD's shim: works on Qt5 and Qt6 builds
    QtCore.QTimer.singleShot(2000, _start_mcp_reload)   # after the main window is up
except Exception:  # noqa: BLE001
    _start_mcp_reload()
