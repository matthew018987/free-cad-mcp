# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the FreeCAD GUI started with FREECAD_USER_DATA pointing at a folder
holding the addon: the watcher must start by itself."""
import json
import os
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtWidgets

t0 = time.time()
while time.time() - t0 < 6:
    QtWidgets.QApplication.processEvents()
    time.sleep(0.05)
with open(os.environ["OUT"], "w") as fh:
    json.dump({"user_data": App.getUserAppDataDir(),
               "running": getattr(Gui, "_mcp_reload_watcher", None) is not None}, fh)
os._exit(0)
