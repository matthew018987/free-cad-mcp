#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M2 verification tests: standards, measure, check_fit, section, checks.json,
GUI-edit detection — over real stdio with real FreeCAD, against geometry whose
answers are known in advance (tests/fixtures/fitlab/model.py):

  BlockA ∩ BlockB            500 mm³ interference
  GapA ↔ GapB                2.0 mm gap
  Plate bore Ø10 ↔ Shaft Ø9.8  coaxial, 0.1 mm radial gap
  Plate Ø3.4 ↔ Flange Ø2.5   31 mm square patterns, 0.03 mm offset
  Plate ↔ Flange             contact at z = 0; no real gap (the 0.42 / 1.0 between the hole rims lie
                             along the contact plane, not across open space)
  Lifted (in a container at z+50) ↔ Floor   90 mm (40 if placements were ignored)
  LinkedBox (link z+100 in the container)   z from 150

    python3 tests/test_m2.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import Client, check, fails, images  # noqa: E402

ROOT = os.path.dirname(HERE)


def num(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def near(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


tmp = tempfile.mkdtemp(prefix="fcmcp-m2-")
try:
    shutil.copytree(os.path.join(HERE, "fixtures", "fitlab"), os.path.join(tmp, "fitlab"))
    shutil.copytree(os.path.join(ROOT, "examples", "nema17_bracket"), os.path.join(tmp, "nema17_bracket"),
                    ignore=shutil.ignore_patterns("build", "out", ".history"))
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    print("lookup_standard")
    err, txt, _ = c.tool("lookup_standard", {"size": "M3"})
    check("M3: ISO 273 normal clearance Ø3.4", "normal Ø3.4" in txt, txt)
    check("M3: tap drill Ø2.5, pitch 0.5", "tap drill             Ø2.5" in txt and "pitch                 0.5" in txt, txt)
    check("M3: DIN 974 counterbore Ø6.5", "counterbore Ø6.5" in txt, txt)
    check("M3: nut AF 5.5 and insert hole Ø4 flagged VERIFY", "AF 5.5" in txt and "VERIFY" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "H7/g6", "d": 8})
    check("Ø8 H7/g6 → +0.005 … +0.029 clearance fit", "+0.0050 … +0.0290" in txt and "clearance fit" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "H7/k6", "d": 22})
    check("Ø22 H7/k6 → transition fit", "transition fit" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "H7/p6", "d": 10})
    check("Ø10 H7/p6 → interference fit", "interference fit" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"process": "fdm"})
    check("FDM process rules", not err and "undersize" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"size": "M7"})
    check("unknown size is an error", err and "unknown metric size" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "H7/g6"})
    check("fit without d is an error", err and "needs d" in txt, txt)

    print("build + checks.json (fitlab)")
    err, txt, _ = c.tool("build", {"project": "fitlab", "views": []})
    check("fitlab builds", not err and txt.startswith("BUILD ✓"), txt)
    check("checks 9/12 with exactly the three designed failures", "checks 9/12 ✗" in txt
          and "✗ no-overlap-AB" in txt and "✗ gap-too-small" in txt and "✗ pattern-tight" in txt, txt)
    check("check value: 500 mm³ interference", "500.000 mm³" in txt, txt)
    check("check value: radial gap 0.100", re.search(r"✓ shaft-radial\s+radial_gap\s+0\.100", txt), txt)
    check("check value: coaxial offset 0", re.search(r"✓ shaft-coaxial\s+coaxial\s+offset 0\.0000", txt), txt)
    check("check value: link composed to z 150", re.search(r"✓ link-height\s+min_z\s+150\.000", txt), txt)
    check("check value: contact, no pseudo-gap between the hole rims across the contact plane",
          re.search(r"✓ flange-seat\s+clearance\s+contact\s+no other gap within range", txt), txt)
    check("new hole specs: insert, DIN974 cbore, H7 bore, nut trap", "Insert ✓ (1× Ø4.00 ↧6.7" in txt
          and "Counterbore Ø8.0↧4.4" in txt and "Ø8.00 THRU H7" in txt and "NutTrap ✓" in txt, txt)
    check("insert warns to verify vendor data", "confirm with the insert's datasheet" in txt, txt)

    print("measure")
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "GapA", "b": "GapB"})
    check("box ↔ box distance 2.0", near(num(r"minimum distance ([\d.]+)", txt), 2.0), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Plate.holes(d=10)", "b": "Shaft"})
    check("bore ↔ shaft: coaxial Ø9.8 in Ø10, radial gap 0.1", "coaxial Ø9.800 inside Ø10.000" in txt
          and near(num(r"minimum radial gap ([\d.]+)", txt), 0.1), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Plate.holes(d=3.4)", "b": "Flange.holes(d=2.5)"})
    check("pattern ↔ pattern: 4 pairs, max offset 0.03", "4 matched pair(s)" in txt
          and near(num(r"max offset ([\d.]+)", txt), 0.03, 1e-4), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Plate.face(-Z)", "b": "Flange.face(+Z)"})
    check("plane ↔ plane: coplanar, facing each other", "COPLANAR ✓" in txt and "facing each other" in txt, txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Plate.frame(bottom)", "b": "Flange.frame(top)"})
    check("frame ↔ frame: same origin, rotated 180°", near(num(r"\|([\d.]+)\| mm", txt), 0.0)
          and near(num(r"rotation ([\d.]+)°", txt), 180.0), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "[220, 20, 100]", "b": "plane(z=0)"})
    check("point ↔ plane: signed distance 100", near(num(r"signed distance ([\d.-]+)", txt), 100.0), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "LinkedBox"})
    check("link inside a container: global bbox from z 150", "from (400.00, 0.00, 150.00)" in txt, txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Floor", "b": "Lifted"})
    check("container placement composed: Floor ↔ Lifted 90", near(num(r"minimum distance ([\d.]+)", txt), 90.0), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "Plate.holes(d=10)", "b": "Shaft.Face1"})
    check("hole ↔ cylindrical face: coaxial with radial gap", "COAXIAL ✓" in txt
          and near(num(r"minimum radial gap ([\d.]+)", txt), 0.1), txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "queries": [{"a": "Nope"}, {"a": "Plate.holes(d=3.4)[9]"},
                                                                     {"a": "Plate.wibble"}]})
    check("bad references explain themselves", "no object 'Nope' — objects:" in txt
          and "index 9 is out of range" in txt and "unknown selector" in txt, txt)
    err, txt, _ = c.tool("measure", {"project": "fitlab", "a": "BlockA", "b": "BlockB"})
    check("overlapping objects report interference", "INTERFERENCE 500.000" in txt, txt)

    print("check_fit")
    err, txt, content = c.tool("check_fit", {"project": "fitlab"})
    check("whole document: one problem, the 500 mm³ overlap", "1 problem(s) ✗" in txt
          and re.search(r"✗ BlockA ↔ BlockB\s+INTERFERENCE 500\.000", txt), txt)
    check("…touching plate/flange reported as contact, no 0.42 pseudo-gap between the hole rims",
          re.search(r"Plate ↔ Flange\s+contact \(touching, no overlap\)\n", txt) and "0.4200" not in txt, txt)
    check("…shaft gap 0.1 and far pairs collapsed", re.search(r"Plate ↔ Shaft\s+gap 0\.1000", txt)
          and "other pair(s) clear" in txt, txt)
    check("…and an image of the overlap", len(images(content)) == 1)
    err, txt, _ = c.tool("check_fit", {"project": "fitlab", "objects": ["GapA", "GapB"], "min_clearance": 1.5})
    check("2.0 gap passes min_clearance 1.5", "all OK ✓" in txt, txt)
    err, txt, _ = c.tool("check_fit", {"project": "fitlab", "objects": ["GapA", "GapB"], "min_clearance": 2.5})
    check("2.0 gap fails min_clearance 2.5", "1 problem(s) ✗" in txt, txt)
    err, txt, _ = c.tool("check_fit", {"project": "fitlab", "objects": ["Plate", "Flange"], "min_clearance": 0.3})
    check("unexpected contact fails when clearance is required", "not listed as an expected contact" in txt, txt)
    err, txt, _ = c.tool("check_fit", {"project": "fitlab", "objects": ["Plate", "Flange"], "min_clearance": 0.3,
                                       "contact_ok": [["Plate", "Flange"]]})
    check("listed contact passes", "all OK ✓" in txt, txt)
    err, txt, _ = c.tool("check_fit", {"project": "fitlab", "objects": ["BlockA", "BlockB"],
                                       "interference_ok": [["BlockB", "BlockA"]]})
    check("allowed interference (press fit) passes", "all OK ✓" in txt and "allowed" in txt, txt)

    print("section")
    err, txt, content = c.tool("section", {"project": "fitlab", "plane": {"y": 20},
                                           "objects": ["Plate", "Shaft", "Flange"]})
    check("y=20 section: shaft ↔ plate gap 0.1 in plane", near(num(r"Plate ↔ Shaft gap ([\d.]+)", txt), 0.1), txt)
    check("…plate area 2 × 15 × 5 = 150 mm²", near(num(r"Plate\s+area ([\d.]+)", txt), 150.0), txt)
    check("…touching flange: no 1.0 'nearest other gap' between the Ø10 / Ø12 bore rims on the contact line",
          re.search(r"Plate ↔ Flange touching\n", txt) and "nearest other gap" not in txt, txt)
    check("…plot returned", len(images(content)) == 1)
    err, txt, _ = c.tool("section", {"project": "fitlab", "plane": {"z": 5}, "objects": ["BlockA", "BlockB"]})
    check("z=5 section: 5 × 10 = 50 mm² overlap", near(num(r"OVERLAP ([\d.]+) mm²", txt), 50.0), txt)
    err, txt, _ = c.tool("section", {"project": "fitlab", "plane": {"z": 999}})
    check("plane that misses everything is an error", err and "does not cut" in txt, txt)
    err, txt, _ = c.tool("section", {"project": "fitlab", "plane": {"point": [220, 20, 2.5], "normal": [0, 0, 1]},
                                     "objects": ["Plate", "Shaft"]})
    check("arbitrary plane via point + normal", not err and near(num(r"Plate ↔ Shaft gap ([\d.]+)", txt), 0.1), txt)

    print("GUI edits")
    err, txt, _ = c.tool("inspect", {"project": "fitlab", "level": "changes"})
    check("untouched build reports no changes", "none — the file matches the last build" in txt, txt)
    c.tool("run_script", {"project": "fitlab", "doc": "build/fitlab.FCStd", "save": "inplace",
                          "code": "sh = doc.getObject('Params')\nsh.set(sh.getCellFromAlias('plate_t'), '6')\n"
                                  "doc.recompute()"})
    err, txt, _ = c.tool("build", {"project": "fitlab", "views": [], "skip_checks": True})
    check("parameter edit blocks the build and is named exactly", err and "plate_t: 5 → 6" in txt
          and "added" not in txt and "changed" not in txt, txt)
    err, txt, _ = c.tool("build", {"project": "fitlab", "views": [], "skip_checks": True, "adopt_gui_params": True})
    pj = json.load(open(os.path.join(tmp, "fitlab", "params.json")))
    check("adopt_gui_params copies it into params.json and builds", not err and "adopted" in txt
          and pj["plate_t"]["value"] == 6.0 and "GUI" in pj["plate_t"].get("note", ""), txt)
    c.tool("run_script", {"project": "fitlab", "doc": "build/fitlab.FCStd", "save": "inplace",
                          "code": "doc.addObject('Part::Box', 'Extra')\ndoc.getObject('Pad001').Length = 12\n"
                                  "doc.recompute()"})
    err, txt, _ = c.tool("inspect", {"project": "fitlab", "level": "changes"})
    check("structural edits listed exactly", "added    Extra" in txt and "Pad001" in txt and "10.0 mm → 12.0 mm" in txt
          and txt.count("\n  ") == 2, txt)
    err, txt, _ = c.tool("build", {"project": "fitlab", "views": [], "skip_checks": True, "adopt_gui_params": True})
    check("structural edits still block, even with adopt_gui_params", err and "Port structural edits" in txt, txt)
    err, txt, _ = c.tool("build", {"project": "fitlab", "views": [], "skip_checks": True, "force": True})
    check("force rebuilds from model.py", not err and txt.startswith("BUILD ✓"), txt)

    print("worked example")
    err, txt, _ = c.tool("build", {"project": "nema17_bracket", "views": []})
    check("NEMA17 bracket: all 5 acceptance checks pass", not err and "checks 5/5" in txt and "✗" not in txt, txt)
    # designed gap 0.25 radial (Ø22.5 bore on a Ø22 pilot); the FDM print compensation (+0.2 on Ø) models
    # the bore at Ø22.7, so the modelled gap is 0.35 and prints back to about 0.25
    check("…pilot radial gap 0.35 as modelled (0.25 designed + 0.1 print compensation)",
          re.search(r"✓ pilot-gap\s+radial_gap\s+0\.350", txt) and "Ø22.5 +0.2 comp → Ø22.7" in txt, txt)
    check("…motor seats with nearest gap 0.35", re.search(r"✓ motor-seat\s+clearance\s+0\.3500", txt), txt)
    check("…screw pattern offset 0", re.search(r"✓ screw-pattern\s+align\s+max offset 0\.0000", txt), txt)
    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
