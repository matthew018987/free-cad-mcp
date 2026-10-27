#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test: drives the server over real stdio JSON-RPC with a
real FreeCAD. Run with any Python 3.7+:

    python3 tests/smoke_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import Client, check, fails, images  # noqa: E402


tmp = tempfile.mkdtemp(prefix="fcmcp-test-")
try:
    c = Client(tmp)
    r = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "smoke", "version": "0"}})
    check("initialize", r["result"]["serverInfo"]["name"] == "freecad")
    check("initialize carries instructions", "build" in r["result"].get("instructions", ""))
    c.notify("notifications/initialized")

    tools = c.rpc("tools/list")["result"]["tools"]
    names = sorted(t["name"] for t in tools)
    check("tools/list returns 22 tools", len(names) == 22, ", ".join(names))

    err, txt, _ = c.tool("freecad_status")
    st = json.loads(txt)
    check("freecad_status finds FreeCAD and probes it", isinstance(st["freecad"], dict)
          and str(st.get("version", "")).startswith("FreeCAD 1."), txt)

    err, txt, _ = c.tool("guide")
    check("guide returns the playbook", not err and "Definition of done" in txt, txt[:200])
    err, txt, _ = c.tool("guide", {"topic": "holes"})
    check("guide topic returns one section", not err and "lookup_standard" in txt and len(txt) < 6000, txt[:200])

    # ---------------------------------------------------------- project + build
    err, txt, _ = c.tool("create_project", {"name": "plate", "template": "assembly",
                                            "description": "smoke test"})
    proj = os.path.join(tmp, "plate")
    check("create_project", not err and os.path.isfile(os.path.join(proj, "model.py")), txt)
    err, txt, _ = c.tool("create_project", {"name": "plate"})
    check("create_project refuses a non-empty dir", err, txt)

    err, txt, content = c.tool("build", {"project": "plate"})
    check("build ✓ on the template", not err and txt.startswith("BUILD ✓"), txt)
    check("build returns an image", len(images(content)) == 1)
    check("build writes the FCStd", os.path.isfile(os.path.join(proj, "build", "plate.FCStd")))
    check("build reports fully constrained sketches", "all fully constrained ✓" in txt, txt)
    check("build reports mass", " g  bbox" in txt, txt)
    check("build reports hole specs (fdm template: +0.2 print compensation)", "MountHoles ✓" in txt
          and "Ø3.4 +0.2 comp → Ø3.6" in txt, txt)

    err, txt, _ = c.tool("edit_file", {"project": "plate", "path": "params.json",
                                       "old_text": '"value": 5,', "new_text": '"value": 8,'})
    check("edit_file params.json", not err, txt)
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("rebuild reports Δ vs previous", "Δ r0001" in txt and "thickness 5" in txt, txt)

    err, txt, _ = c.tool("build", {"project": "plate", "params": {"thickness": 12}, "variant": "thick",
                                   "views": []})
    check("variant build with override", not err and "thickness=12*" in txt
          and os.path.isfile(os.path.join(proj, "build", "plate__thick.FCStd")), txt)

    # failing feature must be reported, not hidden
    with open(os.path.join(proj, "model.py"), "a") as fh:
        fh.write('\nfc.fillet(body, fc.edges(body, kind="circle"), 25)\n')
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("failed fillet → BUILD ✗ with FreeCAD's message", txt.startswith("BUILD ✗")
          and "Fillet ✗" in txt and "BRep_API" in txt, txt)
    check("…and warns the body is not the full feature list", "NOT the full feature list" in txt, txt)

    # script error points at the model.py line
    with open(os.path.join(proj, "model.py"), "a") as fh:
        fh.write("fc.face(body, '+Q')\n")
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("script error cites model.py line", "model.py:" in txt and "+Q" in txt, txt)
    shutil.copyfile(os.path.join(proj, ".history", "r0002", "model.py"), os.path.join(proj, "model.py"))

    # build refuses to clobber a file edited elsewhere (here: an object added to the build file)
    fcstd = os.path.join(proj, "build", "plate.FCStd")
    c.tool("build", {"project": "plate", "views": []})
    c.tool("run_script", {"project": "plate", "doc": "build/plate.FCStd", "save": "inplace",
                          "code": "doc.addObject('Part::Box', 'HandMade')"})
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("build refuses to overwrite an externally edited FCStd", err and "force=true" in txt
          and "HandMade" in txt, txt)
    err, txt, _ = c.tool("build", {"project": "plate", "views": [], "force": True})
    check("force=true overwrites", not err and txt.startswith("BUILD ✓"), txt)

    # ------------------------------------------------------------------ inspect
    err, txt, _ = c.tool("inspect", {"project": "plate"})
    check("inspect summary", not err and "DOCUMENT plate.FCStd" in txt and "Params" in txt, txt)
    err, txt, _ = c.tool("inspect", {"project": "plate", "object": "Plate"})
    check("inspect object detects 4 through holes", "holes    4 detected" in txt and txt.count("THRU") >= 4, txt)
    check("inspect object indexes faces", "Face1 " in txt and "normal" in txt, txt[:300])
    err, txt, _ = c.tool("inspect", {"project": "plate", "object": "Reference"})
    check("inspect ref part shows its frame", "mount" in txt and "frames" in txt, txt)
    err, txt, _ = c.tool("inspect", {"project": "plate", "level": "sketch", "object": "BaseSketch"})
    check("inspect sketch shows DoF and bound constraints", "DoF 0" in txt and "Params." in txt, txt)
    err, txt, _ = c.tool("inspect", {"project": "plate", "level": "params"})
    check("inspect params lists expressions", "expressions" in txt and "Params.thickness" in txt, txt)

    # ------------------------------------------------------------------- render
    err, txt, content = c.tool("render", {"project": "plate", "views": ["iso", "front", "top", "right"]})
    check("render grid", not err and len(images(content)) == 1 and "iso, front, top, right" in txt, txt)
    err, txt, content = c.tool("render", {"project": "plate", "views": ["front"], "section": {"y": 0},
                                          "style": "drawing"})
    check("render section in drawing style", not err and len(images(content)) == 1, txt)
    err, txt, _ = c.tool("render", {"project": "plate", "views": ["sideways"]})
    check("render rejects an unknown view", err and ("unknown view" in txt or "views[0] must be one of" in txt), txt)

    # --------------------------------------------------------------- run_script
    err, txt, _ = c.tool("run_script", {"project": "plate", "doc": "build/plate.FCStd",
                                        "code": "b = doc.getObjectsByLabel('Plate')[0]\n"
                                                "fc.result(volume=round(b.Shape.Volume, 1))\nprint('hi')"})
    check("run_script returns fc.result data and stdout", not err and '"volume"' in txt and "hi" in txt, txt)
    before = open(fcstd, "rb").read()
    err, txt, _ = c.tool("run_script", {"project": "plate", "doc": "build/plate.FCStd", "save": "revision",
                                        "code": "o = doc.addObject('Part::Box', 'Extra')\no.Length = 5"})
    check("run_script save=revision writes .r01", not err and os.path.isfile(os.path.join(proj, "build", "plate.r01.FCStd")), txt)
    check("…and leaves the original untouched", open(fcstd, "rb").read() == before)
    check("…and reports the change", "added: Extra" in txt, txt)
    err, txt, _ = c.tool("run_script", {"code": "raise ValueError('boom')"})
    check("run_script error is reported", "ValueError: boom" in txt, txt)
    step = os.path.join(tmp, "vendor.step")
    err, txt, _ = c.tool("run_script", {"code": "Part.makeCylinder(5, 20).exportStep(%r)" % step})
    check("run_script on a new document", not err and os.path.isfile(step), txt)
    err, txt, _ = c.tool("inspect", {"doc": step})
    check("inspect works on STEP", not err and "solid ✓" in txt, txt)
    err, txt, content = c.tool("render", {"doc": step})
    check("render works on STEP", not err and len(images(content)) == 1, txt)

    # crash isolation: a native crash ends the worker, not the server
    err, txt, _ = c.tool("run_script", {"code": "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)"})
    check("worker crash is reported", err and "signal" in txt, txt)
    err, txt, _ = c.tool("list_projects")
    check("server survives a worker crash", not err and "plate" in txt, txt)

    # ---------------------------------------------------------------- files
    err, txt, _ = c.tool("read_file", {"project": "plate", "path": "model.py", "start_line": 1, "end_line": 3})
    check("read_file with range", not err and "showing 1-3" in txt, txt)
    err, txt, _ = c.tool("write_file", {"project": "plate", "path": "build/x.FCStd", "content": "x"})
    check("write_file refuses binary types", err and "refusing" in txt, txt)
    err, txt, _ = c.tool("write_file", {"project": "plate", "path": "notes.md", "content": "a"})
    err2, txt2, _ = c.tool("write_file", {"project": "plate", "path": "notes.md", "content": "b"})
    check("write_file needs overwrite=true", not err and err2, txt2)
    err, txt, _ = c.tool("edit_file", {"project": "plate", "path": "model.py", "old_text": "fc", "new_text": "FC"})
    check("edit_file refuses ambiguous matches", err and "appears" in txt, txt)

    # ----------------------------------------------------------- background
    err, txt, _ = c.tool("build", {"project": "plate", "wait": False, "views": []})
    job = txt.split("job   ")[1].split()[0] if "job   " in txt else None
    check("build wait=false returns a job id", not err and job, txt)
    err, txt, _ = c.tool("job_status", {"job_id": job, "wait_seconds": 60})
    check("job_status collects the build", not err and txt.startswith("BUILD ✓"), txt)

    # concurrency: a fast call is answered while a slow worker runs
    slow = c.send("tools/call", {"name": "run_script", "arguments": {"code": "import time\ntime.sleep(6)"}})
    time.sleep(0.5)
    t0 = time.time()
    err, txt, _ = c.tool("list_projects")
    dt = time.time() - t0
    check("fast call answered during a slow run (%.2fs)" % dt, not err and dt < 2.0, txt)
    c.wait(slow, 60)

    # cancellation kills the worker
    mid = c.send("tools/call", {"name": "run_script", "arguments": {"code": "import time\ntime.sleep(60)"}})
    time.sleep(1.5)
    t0 = time.time()
    c.notify("notifications/cancelled", {"requestId": mid, "reason": "test"})
    resp = c.wait(mid, 30)
    check("cancel kills the worker promptly (%.1fs)" % (time.time() - t0),
          time.time() - t0 < 10 and Client.unpack(resp)[0], Client.unpack(resp)[1])
    c.close()

    # sandbox
    c2 = Client(tmp, {"FREECAD_MCP_ROOTS": proj})
    c2.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    err, txt, _ = c2.tool("read_file", {"path": "/etc/hosts"})
    check("FREECAD_MCP_ROOTS confines file access", err and "outside the allowed roots" in txt, txt)
    c2.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
