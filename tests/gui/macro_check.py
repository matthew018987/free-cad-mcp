# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the FreeCAD GUI: the generated MCP_Reload.FCMacro toggles the watcher
and prints the new state each time."""
import json
import os

import FreeCAD as App
import FreeCADGui as Gui

res = {"messages": []}
orig = App.Console.PrintMessage
try:
    os.environ.pop("FCMCP_REQ", None)
    App.Console.PrintMessage = lambda m: res["messages"].append(m.rstrip("\n")) \
        if m.startswith("MCP Reload:") else orig(m)
    src = open(os.environ["MACRO"]).read()
    exec(compile(src, "MCP_Reload.FCMacro", "exec"), {"__name__": "__main__"})
    res["after_first_run"] = getattr(Gui, "_mcp_reload_watcher", None) is not None
    exec(compile(src, "MCP_Reload.FCMacro", "exec"), {"__name__": "__main__"})
    res["after_second_run"] = getattr(Gui, "_mcp_reload_watcher", None) is not None
except Exception:  # noqa: BLE001
    import traceback
    res["error"] = traceback.format_exc()
finally:
    App.Console.PrintMessage = orig
    with open(os.environ["OUT"], "w") as fh:
        json.dump(res, fh)
    os._exit(0)
