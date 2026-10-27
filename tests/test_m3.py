#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M2b/M3 tests: assemblies, export, drawings, manufacturability and release —
over real stdio with real FreeCAD (the drawing tests run the FreeCAD GUI
in the background for a few seconds each).

    python3 tests/test_m3.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import Client, check, fails, images  # noqa: E402

ROOT = os.path.dirname(HERE)


def num(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


tmp = tempfile.mkdtemp(prefix="fcmcp-m3-")
try:
    for src, name in ((os.path.join(HERE, "fixtures", "fitlab"), "fitlab"),
                      (os.path.join(HERE, "fixtures", "asmlab"), "asmlab"),
                      (os.path.join(ROOT, "examples", "nema17_bracket"), "nema17_bracket"),
                      (os.path.join(ROOT, "examples", "turned_shaft"), "turned_shaft")):
        shutil.copytree(src, os.path.join(tmp, name),
                        ignore=shutil.ignore_patterns("build", "out", ".history", "release", "drawings",
                                                      "drawings.json"))
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    tools = sorted(t["name"] for t in c.rpc("tools/list")["result"]["tools"])
    check("22 tools", len(tools) == 22, ", ".join(tools))

    print("assemblies (Assembly workbench)")
    err, txt, _ = c.tool("build", {"project": "asmlab", "views": []})
    check("assembly builds, solves and every joint holds", not err and "BUILD ✓" in txt
          and "MotorMount     Fixed        Base ↔ Motor  holds ✓" in txt
          and "ShaftSpin      Revolute     Base ↔ Shaft  holds ✓" in txt, txt)
    check("…grounded part and part count reported", "3 part(s) · grounded: Base · 2 joint(s)" in txt, txt)
    check("…motor seated face-to-face at z = 5, shaft coaxial with 0.1 gap", "checks 4/4" in txt
          and re.search(r"✓ motor-on-top\s+min_z\s+5\.000", txt) and re.search(r"✓ shaft-gap\s+radial_gap\s+0\.100", txt), txt)
    err, txt, _ = c.tool("build", {"project": "asmlab", "views": [], "variant": "aligned",
                                   "params": {"mount_mode": "aligned"}})
    check("'aligned' instead of 'against' puts the motor inside the plate — checks catch it",
          "checks 2/4 ✗" in txt and "✗ motor-on-top" in txt and "interference" in txt, txt)
    err, txt, _ = c.tool("measure", {"project": "asmlab", "a": "Base.frame(top)", "b": "Motor.frame(mount)"})
    check("joint frames coincide (against → rotated 180°)", "|0.0000| mm" in txt and "rotation 180.000°" in txt, txt)
    err, txt, _ = c.tool("check_fit", {"project": "asmlab", "contact_ok": [["Base", "Motor"]]})
    check("check_fit works on assembly parts", "all OK ✓" in txt, txt)

    print("export")
    c.tool("build", {"project": "nema17_bracket", "views": []})
    err, txt, _ = c.tool("export", {"project": "nema17_bracket", "formats": ["step", "3mf", "stl", "dxf"]})
    check("STEP re-imported with identical volume", re.search(r"✓ Bracket\.step .*volume Δ 0\.0000 %", txt), txt)
    check("3MF and STL watertight", re.search(r"✓ Bracket\.3mf .*watertight", txt) and
          re.search(r"✓ Bracket\.stl .*watertight", txt), txt)
    check("DXF of an L-bracket is flagged as not a plate", re.search(r"✗ Bracket\.dxf .*NOT a uniform plate", txt), txt)
    c.tool("build", {"project": "fitlab", "views": [], "skip_checks": True})
    err, txt, _ = c.tool("export", {"project": "fitlab", "objects": ["Plate"], "formats": ["dxf", "svg"]})
    dxf = open(os.path.join(tmp, "fitlab", "out", "export", "Plate.dxf")).read()
    check("plate DXF verified: 4 lines + 5 true circles", "✓ Plate.dxf" in txt and dxf.count("\nCIRCLE\n") == 5
          and dxf.count("\nLINE\n") == 4, txt)
    xs = [float(v) for v in re.findall(r"\n10\n(-?[\d.]+)", dxf)]
    check("DXF origin at the profile's lower-left corner", xs and min(xs) >= -1e-6 and max(xs) <= 40.0001, str(xs))

    print("drawings")
    err, txt, content = c.tool("drawing", {"project": "fitlab", "part": "Plate"})
    out = os.path.join(tmp, "fitlab", "out", "drawings")
    check("plate drawing: every dimension verified", not err and "9/9 verified against the model" in txt, txt)
    check("…style, paper and scale chosen", "(plate, A4 ISO 5457 landscape, scale 1:1)" in txt, txt)
    check("…hole callouts", "4× ⌀3.4 THRU" in txt and "⌀10 THRU" in txt, txt)
    check("…PDF, SVG, DXF and PNG preview written",
          all(os.path.isfile(os.path.join(out, "Plate." + e)) for e in ("pdf", "svg", "dxf", "png"))
          and len(images(content)) == 1, os.listdir(out) if os.path.isdir(out) else "no dir")
    svg = open(os.path.join(out, "Plate.svg")).read()
    check("…title block filled (title, material, scale)", ">Plate<" in svg and "PETG" in svg and "1:1" in svg)
    err, txt, _ = c.tool("drawing", {"project": "fitlab", "part": "HoleLab", "notes": ["Anodise black"]})
    check("standards callouts: insert, DIN 974 counterbore, H7 bore",
          "⌀4 ↧6.7" in txt and "⌀4.5 THRU  ⌴ ⌀8 ↧4.4" in txt and "⌀8 H7 THRU" in txt, txt)
    check("…extra notes appended (as written) and remembered in drawings.json", "Anodise black" in txt and
          json.load(open(os.path.join(tmp, "fitlab", "drawings.json")))["HoleLab"]["notes"] == ["Anodise black"], txt)
    c.tool("build", {"project": "turned_shaft", "views": []})
    err, txt, _ = c.tool("drawing", {"project": "turned_shaft"})
    tsvg = open(os.path.join(tmp, "turned_shaft", "out", "drawings", "Shaft.svg")).read()
    check("turned part: lathe layout, all dims verified", "(turned, A4" in txt
          and re.search(r"dims\s+(\d+)/\1 verified", txt), txt)
    check("…fit with ISO 286 limits and thread note on the sheet", "k6 (+0.012/+0.001)" in tsvg
          and "M8×1.25-6g" in tsvg, "")
    check("…cross hole called out", "⌀3 THRU" in txt, txt)
    check("one drawing document per part, earlier sheets kept",
          os.path.isfile(os.path.join(tmp, "fitlab", "drawings", "Plate.FCStd"))
          and os.path.isfile(os.path.join(tmp, "fitlab", "drawings", "HoleLab.FCStd")))
    dwg = "drawings/Plate.FCStd"
    err, txt, content = c.tool("drawing", {"action": "read", "project": "fitlab", "doc": dwg})
    check("read: pages, views, dimensions and notes", "PAGE Plate" in txt and "dim " in txt and "note" in txt
          and len(images(content)) == 1, txt[:400])
    err, txt, content = c.tool("drawing", {"action": "edit", "project": "fitlab", "doc": dwg, "page": "Plate",
                                           "ops": [{"op": "add_note", "text": "DEBURR ALL HOLES", "x": 200, "y": 120},
                                                   {"op": "add_dimension", "view": "Top", "type": "DistanceX",
                                                    "a": "Plate.holes(d=3.4)[0]", "b": "Plate.holes(d=3.4)[1]",
                                                    "y": -60, "tolerance": "±0.05"},
                                                   {"op": "set_title", "fields": {"creator": "Test Bench"}}]})
    check("edit: note, toleranced dimension, title field; saved as a revision", not err
          and os.path.isfile(os.path.join(tmp, "fitlab", "drawings", "Plate.r01.FCStd"))
          and "DEBURR ALL HOLES" in txt and "creator=Test Bench" in txt and re.search(r"DistanceX\s+31\b", txt), txt)

    print("manufacturability")
    err, txt, _ = c.tool("check_manufacturing", {"project": "nema17_bracket"})
    check("FDM bracket ready, with a print orientation", "✓ Bracket — fdm: ready" in txt and "print with" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "fitlab", "objects": ["HoleLab"], "process": "cnc"})
    check("CNC: the hex nut trap's 6 vertical corners need a radius", "6 sharp internal corner(s) along +Z" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "nema17_bracket", "process": "cnc"})
    warns = [ln for ln in txt.splitlines() if ln.strip().startswith("⚠ ") and "Bracket — cnc" not in ln]
    check("CNC: bracket floor corners are fine — the only warning is the FDM hole compensation built into the "
          "model (round 4 #46)", "Bracket — cnc" in txt and "internal corner" not in txt and len(warns) == 1
          and "rebuild with compensation 0 before machining" in warns[0], txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "turned_shaft"})
    check("turning: bar size, L/D, secondary op, thread", "turned from Ø22 bar for Ø20 finished OD" in txt and "secondary" in txt
          and "M8×1.25-6g" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "fitlab", "objects": ["HoleLab", "Plate"], "process": "laser"})
    check("laser: stepped part blocked, flat plate allowed", "✗ HoleLab — laser: not manufacturable" in txt
          and "Plate — laser" in txt and "✗ Plate" not in txt, txt)

    print("release")
    err, txt, content = c.tool("release", {"project": "turned_shaft"})
    rel = os.path.join(tmp, "turned_shaft", "release")
    zips = [f for f in os.listdir(rel) if f.endswith(".zip")] if os.path.isdir(rel) else []
    check("turning release: zip with STEP, drawing PDF/DXF, BOM and notes", not err and zips, txt)
    if zips:
        names = zipfile.ZipFile(os.path.join(rel, zips[0])).namelist()
        check("…package contents", any(n.endswith("Shaft/Shaft.step") for n in names)
              and any(n.endswith("Shaft_drawing.pdf") for n in names) and any(n.endswith("BOM.csv") for n in names)
              and any(n.endswith("README.md") for n in names), names)
        readme = zipfile.ZipFile(os.path.join(rel, zips[0])).read([n for n in names if n.endswith("README.md")][0]).decode()
        check("…notes say what to send, stock, fit, thread, quantity and secondary op",
              "**Send:** the STEP model and the PDF drawing" in readme and "Ø22 bar for Ø20 finished OD" in readme
              and "Ø12 k6 (+0.012/+0.001)" in readme and "M8×1.25-6g" in readme and "quantity 4" in readme
              and "secondary" in readme, readme[:1500])
    err, txt, _ = c.tool("release", {"project": "nema17_bracket"})
    check("print release: 3MF + STL + STEP; the motor (ref_part without bom=) is a reference, not purchased",
          not err and "Bracket.3mf" in txt and "purchased: Motor" not in txt
          and re.search(r"reference parts .*: Motor", txt), txt)
    err, txt, _ = c.tool("release", {"project": "fitlab"})
    check("release refuses failing acceptance checks", err and "acceptance checks fail" in txt, txt[:300])
    err, txt, _ = c.tool("release", {"project": "fitlab", "allow_failing_checks": True, "process": "laser",
                                     "parts": ["HoleLab"]})
    check("release refuses a part that is not manufacturable for its process", err and "not manufacturable" in txt,
          txt[:300])
    c.tool("run_script", {"project": "nema17_bracket", "doc": "build/nema17_bracket.FCStd", "save": "inplace",
                          "code": "doc.addObject('Part::Box', 'Sneaky')"})
    err, txt, _ = c.tool("release", {"project": "nema17_bracket"})
    check("release refuses unported GUI edits", err and "Sneaky" in txt, txt[:300])
    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
