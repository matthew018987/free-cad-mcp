#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""manufacture.py and standards.py: standards (turning/laser rules, densities, bar
stock), manufacture (oriented print exports, DXF units, DFM wording and
metrics, part records, release README/BOM) — standards directly, manufacture
inside FreeCAD's python on shapes whose answers are known, and lookup_standard
plus a print release over real stdio.

    python3 tests/test_manufacture.py
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, HERE)
sys.path.insert(0, SERVER)
from mcp_client import FC_PY, Client, check, fails  # noqa: E402
import standards  # noqa: E402

# ------------------------------------------------------------------------------------------------------------
# runs inside FreeCAD's python: builds the fixtures, calls manufacture, writes JSON results
FC_SCRIPT = r'''
import json, os, sys
SERVER, OUT, RES = sys.argv[1], sys.argv[2], sys.argv[3]
LIB = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "lib")
sys.path.insert(0, SERVER)
sys.path.append(LIB)
for d in ("Part", "PartDesign", "Mesh", "MeshPart", "Import", "Sketcher"):
    sys.path.append(os.path.join(os.path.dirname(LIB), "Mod", d))
import FreeCAD as App, Part, Mesh
import manufacture as mf
import fclib as fc
V = App.Vector
r = {}
doc = App.newDocument("FixC")


def feat(name, shape):
    o = doc.addObject("Part::Feature", name)
    o.Shape = shape
    return o


def vfillet(box, r_):
    return box.makeFillet(r_, [e for e in box.Edges if abs(e.Vertexes[0].Point.x - e.Vertexes[1].Point.x) < 1e-9
                                and abs(e.Vertexes[0].Point.y - e.Vertexes[1].Point.y) < 1e-9])


def lines(o, proc, project=None):
    d = mf.dfm(o, proc, project or {})
    return {"verdict": d["verdict"], "text": "\n".join(mf.dfm_lines(d)), "metrics": {
        k: v for k, v in d["metrics"].items() if isinstance(v, (int, float, str, list, type(None)))}}


# 1. oriented print exports: 20x20x10 block with a Ø4 boss sticking out 5 mm along +X
blk = Part.makeBox(20, 20, 10, V(5, 5, 5)).fuse(
    Part.makeCylinder(2, 5, V(25, 15, 10), V(1, 0, 0))).removeSplitter()
ob = feat("Block", blk)
for up in ("+X", "-X", "+Z", "-Y"):
    d = os.path.join(OUT, "exp" + up.replace("+", "p").replace("-", "m"))
    recs = mf.export_parts(doc, [ob], ["stl", "3mf", "obj", "step"], d, print_up=up)
    res = {"recs": [{k: x[k] for k in ("format", "ok", "check", "oriented")} for x in recs]}
    for f in ("stl", "3mf", "obj"):
        m = Mesh.Mesh(os.path.join(d, "Block." + f))
        bb = m.BoundBox
        pts = [p for p in m.Points]
        top = [p for p in pts if p.z > bb.ZMax - 4]
        bot = [p for p in pts if p.z < bb.ZMin + 4]
        res[f] = {"bb": [bb.XMin, bb.YMin, bb.ZMin, bb.XLength, bb.YLength, bb.ZLength],
                  "top_xy": [max(p.x for p in top) - min(p.x for p in top), max(p.y for p in top) - min(p.y for p in top)],
                  "bot_xy": [max(p.x for p in bot) - min(p.x for p in bot), max(p.y for p in bot) - min(p.y for p in bot)]}
    sb = Part.read(os.path.join(d, "Block.step")).BoundBox
    res["step_bb"] = [sb.XMin, sb.YMin, sb.ZMin, sb.XLength, sb.YLength, sb.ZLength]
    r["export" + up] = res
try:
    mf.export_parts(doc, [ob], ["stl"], os.path.join(OUT, "bad"), print_up="up")
    r["bad_up"] = "no error"
except mf.MfgError as e:
    r["bad_up"] = str(e)

# 2. DXF header
plain = feat("Plain", Part.makeBox(50, 30, 5))
mf.export_parts(doc, [plain], ["dxf"], os.path.join(OUT, "dxf"))
r["dxf"] = open(os.path.join(OUT, "dxf", "Plain.dxf")).read()

# 2b. SVG vs DXF on an asymmetric profile placed off the origin: bulged right edge (arc R36.3 — its full circle
# reaches far outside the part), R8 fillet only at the top-left corner, Ø6 hole upper left, Ø4 lower right
ws = Part.Wire([Part.LineSegment(V(0, 0, 0), V(60, 0, 0)).toShape(),
                Part.Arc(V(60, 0, 0), V(66, 20, 0), V(60, 40, 0)).toShape(),
                Part.LineSegment(V(60, 40, 0), V(0, 40, 0)).toShape(), Part.LineSegment(V(0, 40, 0), V(0, 0, 0)).toShape()])
asym = Part.Face(ws).extrude(V(0, 0, 3))
asym = asym.makeFillet(8, [e for e in asym.Edges if all(abs(v.Point.x) < 1e-6 and abs(v.Point.y - 40) < 1e-6
                                                      for v in e.Vertexes)])
asym = asym.cut(Part.makeCylinder(3, 3, V(15, 30, 0))).cut(Part.makeCylinder(2, 3, V(45, 10, 0)))
asym.translate(V(100, 50, 7))
r["asym_recs"] = [{k: x[k] for k in ("format", "ok", "check")} for x in
                  mf.export_parts(doc, [feat("Asym", asym)], ["dxf", "svg"], os.path.join(OUT, "asym"))]
r["asym_dxf"] = open(os.path.join(OUT, "asym", "Asym.dxf")).read()
r["asym_svg"] = open(os.path.join(OUT, "asym", "Asym.svg")).read()
r["minwall_same"] = mf.MIN_WALL is mf.standards.MIN_WALL

# 2c. item_size: one item of a reference part holding several (washers, screws of head + shank solids)
two_w = Part.makeCompound([Part.makeBox(10, 10, 1), Part.makeBox(10, 10, 1, V(20, 0, 0))])
screw = lambda x: [Part.makeCylinder(2.75, 3, V(x, 0, 10)), Part.makeCylinder(1.5, 10, V(x, 0, 0))]  # noqa: E731
mixed = Part.makeCompound([Part.makeBox(10, 10, 4), Part.makeBox(5, 5, 4, V(30, 0, 0))])
r["item_size"] = {"washers": mf.item_size(two_w), "screws": mf.item_size(Part.makeCompound(screw(0) + screw(30))),
                  "single": mf.item_size(Part.makeBox(3, 4, 5)), "mixed": mf.item_size(mixed)}

# 3. CNC: plate 100x60x8, pocket 40x20x3 R3 from +Z, four Ø4.2 through holes
plate = Part.makeBox(100, 60, 8, V(-50, -30, 0)).cut(vfillet(Part.makeBox(40, 20, 3, V(-20, -10, 5)), 3))
for x in (-40, 40):
    for y in (-20, 20):
        plate = plate.cut(Part.makeCylinder(2.1, 8, V(x, y, 0)))
r["cnc_plate"] = lines(feat("CncPlate", plate), "cnc")
r["cnc_plain"] = lines(plain, "cnc")
deep = Part.makeBox(100, 60, 30, V(-50, -30, 0)).cut(vfillet(Part.makeBox(40, 20, 28, V(-20, -10, 2)), 2))
r["cnc_deep"] = lines(feat("Deep", deep), "cnc")
# pocket from +Z and a blind Ø5 hole from the bottom (-Z)
two = Part.makeBox(60, 40, 12).cut(vfillet(Part.makeBox(20, 20, 4, V(5, 10, 8)), 4)).cut(
    Part.makeCylinder(2.5, 6, V(45, 20, 0)))
r["cnc_two"] = lines(feat("TwoSided", two), "cnc")
# rounded-corner plate with a through hole: rounded outline = profiling, hole = feature
rp = vfillet(Part.makeBox(60, 40, 6), 5).cut(Part.makeCylinder(3, 6, V(30, 20, 0)))
r["cnc_round"] = lines(feat("RoundPlate", rp), "cnc")

# 4. turning: Ø16 x 25 spacer, Ø8.2 bore, 0.5 chamfers
sp = Part.makeCylinder(8, 25).cut(Part.makeCylinder(4.1, 25))
sp = sp.makeChamfer(0.5, [e for e in sp.Edges if e.Curve.__class__.__name__ == "Circle"])
r["turn"] = lines(feat("Spacer", sp), "turning")

# 4b. round 4 (#24): concave cylinders across the cutting direction are ball/bull-nose features, not end-mill
# corners — slot along X with R1.5 floor fillets, half-round groove R3, clamp half-bore R10; still corners: an
# open-ended slot's vertical R2 corners, a through window's R3 corners
sl = Part.makeBox(120, 40, 20).cut(Part.makeBox(120, 12, 10, V(0, 14, 10)))
sl = sl.makeFillet(1.5, [e for e in sl.Edges if e.Curve.__class__.__name__ == "Line"
                         and all(abs(v.Point.z - 10) < 1e-6 and 13 < v.Point.y < 27 for v in e.Vertexes)
                         and abs(e.Vertexes[0].Point.x - e.Vertexes[1].Point.x) > 100])
r["cnc_slot_fillet"] = lines(feat("SlotFillet", sl), "cnc")
r["cnc_groove"] = lines(feat("Groove", Part.makeBox(100, 30, 15).cut(
    Part.makeCylinder(3, 100, V(0, 15, 15), V(1, 0, 0)))), "cnc")
r["cnc_halfbore"] = lines(feat("HalfBore", Part.makeBox(60, 40, 30).cut(
    Part.makeCylinder(10, 60, V(0, 20, 30), V(1, 0, 0)))), "cnc")
r["cnc_open_slot"] = lines(feat("OpenSlot", Part.makeBox(80, 40, 20).cut(
    vfillet(Part.makeBox(60, 10, 8, V(30, 15, 12)), 2))), "cnc")
r["cnc_window"] = lines(feat("Window", Part.makeBox(100, 60, 10).cut(
    vfillet(Part.makeBox(40, 20, 10, V(30, 20, 0)), 3))), "cnc")
# …the filleted slot plus a blind Ø5 hole from the +X end: the cover now picks +X alone (it reaches the slot through
# its open ends and the hole), yet the fillets run through the slot and +Z reaches them — still not end-mill corners
r["cnc_slot_side"] = lines(feat("SlotSide", sl.cut(Part.makeCylinder(2.5, 15, V(105, 33, 5), V(1, 0, 0)))), "cnc")
# round 4 (#25): bar and L/D from the largest diameter of the whole part — cone head, ball, torus bead
r["turn_cone"] = lines(feat("Poppet", Part.makeCylinder(5, 30).fuse(
    Part.makeCone(5, 10, 5, V(0, 0, 30))).removeSplitter()), "turning")
r["turn_ball"] = lines(feat("Knob", Part.makeCylinder(4, 28).fuse(
    Part.makeSphere(12, V(0, 0, 28))).removeSplitter()), "turning")
r["turn_bead"] = lines(feat("Bead", Part.makeCylinder(5, 40).fuse(
    Part.makeTorus(8, 3, V(0, 0, 20))).removeSplitter()), "turning")
r["turn_x"] = lines(feat("ShaftX", Part.makeCylinder(10, 40, V(5, 7, 9), V(1, 0, 0)).fuse(
    Part.makeCylinder(6, 20, V(45, 7, 9), V(1, 0, 0))).removeSplitter()), "turning")

# 5. FDM orientation in words: an L lying as modelled, a thin plate standing on its edge
L = Part.makeBox(40, 30, 4).fuse(Part.makeBox(40, 4, 30)).removeSplitter()
r["fdm_L"] = lines(feat("Lpart", L), "fdm")
r["fdm_fin"] = lines(feat("Fin", Part.makeBox(3, 40, 30)), "fdm")

# 6. part_record: a PartDesign body with M3 clearance holes compensated +0.2
fc._setup(doc)
bd = fc.body("Bracket")
fc.pad(bd, fc.sketch(bd, "XY", fc.rect(40, 30)), 5, name="Base")
# a plain Ø3.4 hole made Ø3.6 with FcHoleComp 0.2 by hand — what fc.holes(compensation=0.2) records (I1),
# without depending on fclib's implementation
hf = fc.holes(bd, on=fc.face(bd, "+Z"), at=[(-10, 0), (10, 0)], spec="Ø3.4", depth="through", name="Screws")
h = hf if getattr(hf, "TypeId", "") == "PartDesign::Hole" else \
    [o for o in bd.Group if o.TypeId == "PartDesign::Hole"][0]
if not hasattr(h, "FcHoleComp"):
    h.addProperty("App::PropertyFloat", "FcHoleComp")
h.FcHoleComp = 0.2
h.Diameter = 3.6
doc.recompute()
proj = {"name": "demo", "material": {"name": "PETG", "density_g_cm3": 1.27},
        "parts": {"Bracket": {"process": "fdm", "material": "Aluminium 6061-T6", "qty": 3,
                              "notes": "Deburr all edges"}}}
rec = mf.part_record(bd, "fdm", proj)
r["rec"] = {k: rec[k] for k in ("holes", "notes", "hole_compensation", "density", "density_source", "density_note",
                                "mass_g", "qty", "material")}
r["volume"] = bd.Shape.Volume
proj2 = {"material": {"name": "PETG", "density_g_cm3": 1.27},
         "parts": {"Bracket": {"material": "Unobtainium", "notes": ["one", "", "two"]}}}
rec2 = mf.part_record(bd, "fdm", proj2)
r["rec2"] = {k: rec2[k] for k in ("notes", "density", "density_note", "mass_g")}
fitted = feat("Fitted", Part.makeCylinder(4, 10).fuse(Part.makeCylinder(11, 5, V(0, 0, 10))))
fitted.addProperty("App::PropertyString", "FcFits")
fitted.FcFits = json.dumps({"8": "k6", "22": "h6"})
r["fits"] = mf.part_record(fitted, "turning", {})["fits"]

# 7. write_release_docs on hand-made records
rel = os.path.join(OUT, "rel")
os.makedirs(rel)


def f(*p):
    return os.path.join(rel, *p)


def dfm_of(o, proc):
    return mf.dfm(o, proc, {})


printed = dict(mf.part_record(bd, "fdm", proj), part="Bracket", dfm=dfm_of(bd, "fdm"),
               files=[f("Bracket", "Bracket.3mf"), f("Bracket", "Bracket.stl"), f("Bracket", "Bracket.step")],
               exports=[{"file": "Bracket.3mf", "format": "3mf", "ok": True, "check": "120 triangles, watertight",
                         "oriented": "-X"},
                        {"file": "Bracket.stl", "format": "stl", "ok": False, "check": "NOT WATERTIGHT",
                         "oriented": "-X"},
                        {"file": "Bracket.step", "format": "step", "ok": True, "check": "re-imported",
                         "oriented": None}], drawing=None)
mill = dict(mf.part_record(feat("Mill", plate), "cnc", {}), dfm=dfm_of(doc.getObject("Mill"), "cnc"),
            files=[f("Mill", "Mill.step")], exports=[{"file": "Mill.step", "format": "step", "ok": True,
                                                     "check": "re-imported", "oriented": None}], drawing=None)
shaft = Part.makeCylinder(10, 40).fuse(Part.makeCylinder(6, 20, V(0, 0, 40))).removeSplitter()
for sub in ("Shaft", "Gusset"):
    os.makedirs(f(sub), exist_ok=True)
    open(f(sub, sub + "_drawing.pdf"), "w").write("%PDF-1.4 stub")
turned = dict(mf.part_record(feat("Shaft", shaft), "turning", {}), dfm=dfm_of(doc.getObject("Shaft"), "turning"),
              files=[f("Shaft", "Shaft.step")], drawing=f("Shaft", "Shaft_drawing.pdf"), drawing_dims=[7, 7],
              exports=[{"file": "Shaft.step", "format": "step", "ok": True, "check": "re-imported", "oriented": None}])
tri = Part.Face(Part.makePolygon([V(0, 0, 0), V(60, 0, 0), V(0, 60, 0), V(0, 0, 0)])).extrude(V(0, 0, 3))
laser = dict(mf.part_record(feat("Gusset", tri), "laser", {}), dfm=dfm_of(doc.getObject("Gusset"), "laser"),
             files=[f("Gusset", "Gusset.dxf"), f("Gusset", "Gusset.svg"), f("Gusset", "Gusset.step"),
                    f("Gusset", "Gusset_drawing.pdf")], drawing=f("Gusset", "Gusset_drawing.pdf"), drawing_dims="5/6",
             exports=[{"file": "Gusset.dxf", "format": "dxf", "ok": True, "check": "4 entities", "oriented": None}])
refs = [{"part": "Screw_M3x10", "bom": "buy", "qty": 8, "spec": "ISO 4762 M3x10 A2", "size": [5.5, 5.5, 13.0]},
        {"part": "Motor", "bom": "context", "qty": 1, "spec": None, "size": [42.3, 42.3, 48.0]},
        {"part": "KeepOut", "bom": "none", "qty": 1, "spec": None, "size": [10, 10, 10]},
        {"part": "Legacy", "qty": 1, "size": [1, 2, 3]},
        dict({"part": "M5Washers", "bom": "buy", "qty": 4, "spec": "ISO 7089 M5"}, **mf.item_size(two_w)),
        dict({"part": "Spares", "bom": "context", "qty": 1, "spec": None}, **mf.item_size(mixed))]
report = "\n".join(["BUILD ✓  demo  r0009  0.1 s   (0 errors · 0 warnings · checks 1/1)",
                    "note     inputs unchanged — still r0009", "params   a=1 · b=2",
                    "objects   (mass: PETG, 1.27 g/cm³)", "  Bracket  Body     solid ✓ valid ✓",
                    "checks   (checks.json)", "  ✓ fit                clearance   0.30 mm",
                    "warnings", "  Plate is fdm but has no hole compensation", "stdout", "  debug 42",
                    "Δ r0008   no geometric change", "files    build/demo.FCStd  ·  out/demo_build.png"])
checks = [{"id": "fit", "ok": True, "value": "0.30 mm"}]
mf.write_release_docs(rel, {"name": "demo", "drawing": {"general_tolerance": "ISO 2768-m"}}, "r0009",
                      [printed, mill, turned, laser], refs, checks, report,
                      {"since": "r0007", "added": ["fit"], "removed": ["old-gap"], "changed": []})
r["readme"] = open(f("README.md")).read()
r["bom"] = open(f("BOM.csv")).read()
mf.write_release_docs(rel, {"name": "demo"}, "r0010", [dict(mill, exports=[])], [], [], "", None)
r["readme_plain"] = open(f("README.md")).read()
mf.write_release_docs(rel, {"name": "demo"}, "r0011", [dict(mill, exports=[], drawing=f("Mill", "Mill_drawing.pdf"))],
                      [], [], "", None)
r["readme_missing"] = open(f("README.md")).read()
mf.write_release_docs(rel, {"name": "demo"}, "r0012", [dict(turned, exports=[], drawing_dims=None)], [], [], "", None)
r["readme_nodims"] = open(f("README.md")).read()

# round 4: #53 drawing dimension status; #46 print-compensated part released as CNC; #47 bought ref with fewer
# than the items in the model; #48 BOM wording for mixed-size refs
r["dims"] = [[repr(v), list(mf._dims_status(v))] for v in
             ([7, 7], [5, 7], [0, 0], [0, 5], 7, 0, "5/6", "7", None, {"verified": 3, "total": 3}, "n/a")]
r["dfm_comp_cnc"] = mf.dfm(bd, "cnc", proj)
r["dfm_comp_fdm"] = mf.dfm(bd, "fdm", proj)
r["dfm_comp_laser"] = [x["message"] for x in mf.dfm(bd, "laser", proj)["findings"]]
# already built for CNC (an explicit compensation= in model.py): the fix is removing it, not changing the process
r["dfm_comp_built_cnc"] = [x["message"] for x in mf.dfm(bd, "cnc", {"parts": {"Bracket": {"process": "cnc"}}})[
    "findings"] if x.get("code") == "print_comp"]
# the part's material spelled with other spacing/case than the project's: the same material, no density note
rec3 = mf.part_record(bd, "fdm", {"material": {"name": "Black PETG", "density_g_cm3": 1.3},
                                  "parts": {"Bracket": {"material": "black  petg"}}})
r["rec3"] = {k: rec3[k] for k in ("density", "density_source", "density_note")}
rel2 = os.path.join(OUT, "rel2")
os.makedirs(os.path.join(rel2, "Bracket"))
open(os.path.join(rel2, "Bracket", "Bracket.step"), "w").write("stub")
milled = dict(mf.part_record(bd, "cnc", proj), dfm=r["dfm_comp_cnc"], files=[os.path.join(rel2, "Bracket", "Bracket.step")],
              exports=[{"file": "Bracket.step", "format": "step", "ok": True, "check": "re-imported", "oriented": None}],
              drawing=None)
screws4 = Part.makeCompound(screw(0) + screw(20) + screw(40) + screw(60))
refs2 = [dict({"part": "M3Screws", "bom": "buy", "qty": 1, "spec": "ISO 4762 M3x12"}, **mf.item_size(screws4)),
         dict({"part": "M3Enough", "bom": "buy", "qty": 4, "spec": "ISO 4762 M3x12"}, **mf.item_size(screws4)),
         dict({"part": "Hardware", "bom": "buy", "qty": 2, "spec": "kit"}, **mf.item_size(mixed)),
         dict({"part": "Fixture", "bom": "context", "qty": 1, "spec": None}, **mf.item_size(screws4))]
mf.write_release_docs(rel2, {"name": "demo"}, "r0013", [milled], refs2, [], "", None)
r["readme_comp"] = open(os.path.join(rel2, "README.md")).read()
r["bom_comp"] = open(os.path.join(rel2, "BOM.csv")).read()
json.dump(r, open(RES, "w"), default=str)
'''


def run_fc(tmp):
    script = os.path.join(tmp, "fix_c_fc.py")
    with open(script, "w") as fh:
        fh.write(FC_SCRIPT)
    res = os.path.join(tmp, "fix_c.json")
    p = subprocess.run(["perl", "-e", "alarm 600; exec @ARGV", FC_PY, "-I", script, SERVER, tmp, res],
                       capture_output=True, text=True, cwd=tmp)
    if not os.path.isfile(res):
        print(p.stdout[-3000:], p.stderr[-3000:])
        return None
    return json.load(open(res))


def near(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixC-"))
try:
    print("standards (C1)")
    tr = " ".join(standards.PROCESS_RULES.get("turning", []))
    check("turning rules: L/D 3 / 6, tool nose radius, thread relief 1.5 × pitch, chamfers, bar stock, "
          "secondary ops", all(k in tr for k in ("≤ 3", "≤ 6", "nose radius", "0.2–0.8", "1.5 × pitch", "chamfer",
                                                 "bar", "OD + ~1 mm", "secondary")), tr)
    la = " ".join(standards.PROCESS_RULES["laser"])
    check("laser rules: min hole ≥ t, edge distance 1–1.5 t, webs ≥ t, corner relief, kerf, material + thickness",
          all(k in la for k in ("min hole Ø ≥ material thickness", "edge distance ≥ 1–1.5", "webs",
                                "relief", "kerf ≈ 0.1–0.2", "state material and thickness")), la)
    check("fdm rules still mention undersize holes and now the automatic compensation",
          "undersize" in " ".join(standards.PROCESS_RULES["fdm"])
          and "hole_compensation_mm" in " ".join(standards.PROCESS_RULES["fdm"]))
    D = standards.density_for
    check("density: parts.<label>.density_g_cm3 wins",
          D({"parts": {"A": {"density_g_cm3": 2.0, "material": "Steel"}}, "material": {"density_g_cm3": 1.27}},
            "A") == 2.0)
    check("density: part material from the table, case-insensitive substring, longest key wins",
          D({"parts": {"A": {"material": "Aluminium 6061-T6"}}, "material": {"density_g_cm3": 1.27}}, "A") == 2.70
          and D({"parts": {"A": {"material": "303 STAINLESS steel"}}}, "A") == 8.0
          and D({"parts": {"A": {"material": "PETG black"}}}, "A") == 1.27
          and D({"parts": {"A": {"material": "Aluminium bronze"}}}, "A") == 7.6)
    check("density: unknown part material → project density; nothing → None",
          D({"parts": {"A": {"material": "Unobtainium"}}, "material": {"density_g_cm3": 1.27}}, "A") == 1.27
          and D({"material": {"density_g_cm3": "8"}}, "B") == 8.0 and D({}, "A") is None
          and D({"material": {"density_g_cm3": "x"}}, "A") is None)
    check("MATERIAL_DENSITY keys are lower case", all(k == k.lower() for k in standards.MATERIAL_DENSITY))

    print("standards, round 4 (#22 #28 #23)")
    MD = standards.material_density
    want = {"ABS plastic": "abs", "black ABS plastic": "abs", "POM plastic": "pom", "ASA plastic": "asa",
            "PC plastic": "pc", "chrome plated ABS": "abs", "PETG-CF": "petg", "PLA+": "pla", "PLA": "pla",
            "stainless 316L": "316", "AISI304": "304", "S235JR": "s235", "Aluminium 6061-T6": "6061",
            "plated steel": "steel", "aluminum plate": "aluminum", "PA66": "pa66", "Nylon12": "nylon",
            "1045 steel (C45), as supplied": "c45", "ASA/ABS blend": "asa", "ABS/ASA blend": "abs",
            "PC/ABS": "pc/abs", "PC-ABS": "pc-abs", "PET-G": "pet-g", "Polypropylene (PP)": "polypropylene"}
    got = {n: MD(n)[1] for n in want}
    check("material_density matches whole tokens: 'pla' is not in plastic/plate/plated; alloy numbers still match "
          "next to letters (316L, AISI304, S235JR); ties go to the earliest word", got == want,
          {n: (got[n], want[n]) for n in want if got[n] != want[n]})
    check("…'Plastic' / 'plastics' alone match nothing (→ the caller's default, marked assumed)",
          MD("Plastic") == (None, None) and MD("plastics") == (None, None), (MD("Plastic"), MD("plastics")))
    check("…densities: ABS plastic 1.04, POM plastic 1.41, ASA plastic 1.07",
          (MD("ABS plastic")[0], MD("POM plastic")[0], MD("ASA plastic")[0]) == (1.04, 1.41, 1.07))
    DI = standards.density_info
    pj = {"material": {"name": "POM plastic", "density_g_cm3": 1.5},
          "parts": {"A": {"material": "pom  Plastic"}, "B": {"material": "ABS"}, "C": {},
                    "E": {"material": "Unobtainium"}, "F": {"material": "ABS", "density_g_cm3": 1.1}}}
    check("density: the part's material = the project's (case/space-insensitive) → the explicit project density "
          "beats the table guess", DI(pj, "A") == (1.5, "project material"), DI(pj, "A"))
    check("…no part material (or no part entry) → project density", DI(pj, "C") == (1.5, "project material")
          and DI(pj, "Nope") == (1.5, "project material"), (DI(pj, "C"), DI(pj, "Nope")))
    d, src = DI(pj, "B")
    check("…a different part material → its table density, and the source says the project density was not used",
          d == 1.04 and "'abs' in the material table" in src and "not the project density 1.5" in src
          and "parts.B.density_g_cm3" in src, src)
    check("…unknown part material → project density; parts.<label>.density_g_cm3 still wins over everything",
          DI(pj, "E")[0] == 1.5 and DI(pj, "F") == (1.1, "parts.F.density_g_cm3"), (DI(pj, "E"), DI(pj, "F")))
    pj2 = {"material": {"name": "PETG"}, "parts": {"A": {"material": "PETG"}}}
    check("…same material but no project density → the table", DI(pj2, "A")[0] == 1.27, DI(pj2, "A"))
    check("bar_for: next standard bar ≥ OD + 1 (Ø16 → 18, Ø20 → 22, Ø8 → 10, Ø17 → 18), None above the table",
          standards.bar_for(16) == 18 and standards.bar_for(20) == 22 and standards.bar_for(8) == 10
          and standards.bar_for(17) == 18 and standards.bar_for(400) is None,
          [standards.bar_for(x) for x in (16, 20, 8, 17, 400)])

    print("standards, round 3 (C2, C5, C6)")
    T = standards.tolerance
    check("single ISO 286 classes: Ø8 k6, Ø8.2 H11, Ø10 JS7, Ø8 k8 (ei 0), Ø8 g6",
          T(8, "k6") == (8.001, 8.01) and T(8.2, "H11") == (8.2, 8.29) and T(10, "JS7") == (9.993, 10.007)
          and T(8, "k8") == (8.0, 8.022) and T(8, "g6") == (7.986, 7.995),
          [T(8, "k6"), T(8.2, "H11"), T(10, "JS7"), T(8, "k8"), T(8, "g6")])
    check("hole classes with Δ (Ø22 K7 M7 N7 P7) and keyway classes (6 P9 N9 JS9), D/E/F from d/e/f",
          T(22, "K7") == (21.985, 22.006) and T(22, "M7") == (21.979, 22.0) and T(22, "N7") == (21.972, 21.993)
          and T(22, "P7") == (21.965, 21.986) and T(6, "P9") == (5.958, 5.988) and T(6, "N9") == (5.97, 6.0)
          and T(6, "JS9") == (5.985, 6.015) and T(20, "F7") == (20.02, 20.041) and T(20, "e8") == (19.927, 19.96)
          and T(20, "D10") == (20.065, 20.149))
    check("deviation text upper first, half microns kept", standards.deviation_text(8, "k6") == "+0.010/+0.001"
          and standards.deviation_text(22, "h6") == "+0.000/-0.013"
          and standards.deviation_text(12, "js6") == "+0.0055/-0.0055",
          [standards.deviation_text(*a) for a in ((8, "k6"), (22, "h6"), (12, "js6"))])
    t = standards.fit_report(8, "k6")
    check("fit_report with a single class gives its limits (no 'not enough values to unpack')",
          "upper +0.010  lower +0.001" in t and "8.0010 … 8.0100" in t and "Ø8 k6 (+0.010/+0.001)" in t, t)
    check("fit_report pairs unchanged", "+0.0050 … +0.0290" in standards.fit_report(8, "H7/g6"))

    def err_of(fn, *a):
        try:
            fn(*a)
        except ValueError as e:
            return str(e)
        return ""
    e = err_of(standards.fit_report, 8, "k")
    check("bad class: clear message with examples", "'k6'" in e and "'H7/g6'" in e and "IT5–IT14" in e, e)
    e = err_of(standards.fit_report, 8, "g6/H7")
    check("shaft-first pair: says to write the hole class first", "hole class (upper case) first" in e
          and "'H7/g6'" in e, e)
    check("unsupported grade / letter named", "IT5–IT14" in err_of(T, 8, "H17") and "js/JS" in err_of(T, 8, "j6")
          and "250 mm" in err_of(T, 300, "H7"), [err_of(T, 8, "H17"), err_of(T, 8, "j6")])
    G = standards.general_tolerance
    check("ISO 2768-1: m at 80 → ±0.3, ISO 2768-mK at 3 → ±0.1, f at 6 → ±0.05, c at 30.1 → ±0.8",
          G("m", 80) == 0.3 and G("ISO 2768-mK", 3) == 0.1 and G("f", 6) == 0.05 and G("c", 30.1) == 0.8
          and G("medium", 400) == 0.5)
    check("ISO 2768: undefined bands and bad classes are clear errors",
          "no general tolerance" in err_of(G, "v", 2) and "0.5 mm" in err_of(G, "m", 0.3)
          and "'m'" in err_of(G, "x", 3), [err_of(G, "v", 2), err_of(G, "x", 3)])
    t = standards.general_tolerance_report("m", 80)
    check("ISO 2768 report: the value at d, all bands, edges and angles; fit='ISO 2768-m' dispatches to it",
          "80 mm → ±0.3 mm" in t and "120–400 ±0.5" in t and "radii and chamfer heights" in t and "±0°30'" in t
          and "±0.3 mm" in standards.fit_report(80, "ISO 2768-m"), t)
    B = standards.bearing
    b = B("608")
    check("bearing 608: 8 × 22 × 7, r_s min 0.3", (b["d"], b["D"], b["B"], b["r_s_min"]) == (8, 22, 7, 0.3), b)
    need = ["623", "624", "625", "626", "627", "628", "629"] + ["600%d" % i for i in range(7)] + \
        ["620%d" % i for i in range(7)] + ["680%d" % i for i in range(6)]
    check("bearing table covers 623–629, 6000–6006, 6200–6206, 6800–6805, 608",
          all(k in standards.BEARINGS for k in need + ["608"]), [k for k in need if k not in standards.BEARINGS])
    check("bearing designations with brand/suffix and ISO 618xx names",
          (B("SKF 6204-2RS")["designation"], B("SKF 6204-2RS")["suffix"]) == ("6204", "2RS")
          and B("61804")["designation"] == "6804" and B("608ZZ")["D"] == 22 and B("6006")["r_s_min"] == 1.0
          and (B("6205")["d"], B("6205")["D"], B("6205")["B"]) == (25, 52, 15))
    e = err_of(B, "6010")
    check("unknown bearing names the table", "6200–6206" in e and "catalogue" in e, e)
    t = standards.metric_report("608")
    check("size='608' (lookup_standard's size path) gives the bearing, M7 still an unknown metric size",
          "bore d 8 · outside D 22 · width B 7 · chamfer r_s min 0.3" in t and "ISO 492" in t
          and "unknown metric size" in err_of(standards.metric_report, "M7")
          and "bearing='608'" in err_of(standards.metric_report, "M7"), t)
    fdm = " ".join(standards.PROCESS_RULES["fdm"])
    sla = " ".join(standards.PROCESS_RULES["sla"])
    check("fdm/sla min wall rules cite check_manufacturing's thresholds and dfm.min_wall_mm",
          "min wall ≥ %g mm" % standards.MIN_WALL["fdm"] in fdm and "dfm.min_wall_mm" in fdm
          and "min wall ≥ %g mm" % standards.MIN_WALL["sla"] in sla and "dfm.min_wall_mm" in sla
          and "0.9 mm" not in fdm and (standards.MIN_WALL["fdm"], standards.MIN_WALL["sla"]) == (1.2, 0.8), fdm + sla)
    check("compensation rule says where the value comes from and that the build warns when it is missing",
          "create_project sets 0.2; the build warns when it is missing" in fdm
          and "create_project sets 0.1" in sla and "(0.2) to" not in fdm, fdm)

    print("manufacture inside FreeCAD (C2–C6)")
    r = run_fc(tmp)
    check("FreeCAD unit script ran", r is not None)
    r = r or {}

    def ex(up, fmt_):
        return (r.get("export" + up) or {}).get(fmt_) or {}

    # block 20x20x10 + boss Ø4 x5 along +X: +X up → 25 tall, boss (Ø4 footprint) on top
    ok = all(near(ex("+X", f)["bb"][2], 0) and near(ex("+X", f)["bb"][5], 25, 0.01)
             and max(ex("+X", f)["top_xy"]) <= 4.01 and max(ex("+X", f)["bot_xy"]) >= 19.9
             for f in ("stl", "3mf", "obj")) if ex("+X", "stl") else False
    check("print_up +X: STL/3MF/OBJ rotated so model +X points up, z min 0, boss on top", ok,
          json.dumps(r.get("export+X"))[:600])
    ok = all(near(ex("-X", f)["bb"][2], 0) and near(ex("-X", f)["bb"][5], 25, 0.01)
             and max(ex("-X", f)["bot_xy"]) <= 4.01 for f in ("stl", "3mf")) if ex("-X", "stl") else False
    check("print_up -X: boss at the bottom", ok, json.dumps(r.get("export-X"))[:600])
    ok = ex("+Z", "stl") and near(ex("+Z", "stl")["bb"][2], 0) and near(ex("+Z", "stl")["bb"][5], 10, 0.01) \
        and near(ex("+Z", "stl")["bb"][0], 5, 0.01)
    check("print_up +Z: model orientation kept, dropped onto z = 0", bool(ok), json.dumps(r.get("export+Z"))[:400])
    ok = ex("-Y", "stl") and near(ex("-Y", "stl")["bb"][2], 0) and near(ex("-Y", "stl")["bb"][5], 20, 0.01) \
        and near(ex("-Y", "stl")["bb"][3], 25, 0.01)
    check("print_up -Y: 20 mm Y extent becomes the height", bool(ok), json.dumps(r.get("export-Y"))[:400])
    recs = (r.get("export+X") or {}).get("recs") or []
    check("records: mesh files oriented '+X' and verified, STEP not oriented and in model position",
          [x["oriented"] for x in recs] == ["+X", "+X", "+X", None] and all(x["ok"] for x in recs)
          and "oriented for printing" in recs[0]["check"]
          and near((r.get("export+X") or {}).get("step_bb", [0] * 6)[3], 25, 0.01)
          and near((r.get("export+X") or {}).get("step_bb", [0] * 6)[0], 5, 0.01), recs)
    check("bad print_up is an error naming the valid directions", "+X" in r.get("bad_up", "")
          and "-Z" in r.get("bad_up", ""), r.get("bad_up"))
    dxf = r.get("dxf", "")
    check("DXF R12 header declares millimetres ($INSUNITS 4, $MEASUREMENT 1)",
          "$ACADVER\n1\nAC1009" in dxf and "$INSUNITS\n70\n4\n" in dxf and "$MEASUREMENT\n70\n1\n" in dxf, dxf[:300])

    # SVG vs DXF on the asymmetric profile (C1)
    def dxf_entities(txt):
        ln = txt.splitlines()
        pairs = list(zip(ln[0::2], ln[1::2]))
        ents, cur = [], None
        for code, val in pairs:
            if code.strip() == "0":
                cur = {"type": val.strip()}
                ents.append(cur)
            elif cur is not None:
                cur.setdefault(code.strip(), val.strip())
        return ents
    ents = dxf_entities(r.get("asym_dxf", ""))
    dcirc = sorted((float(x["10"]), float(x["20"]), float(x["40"])) for x in ents if x["type"] == "CIRCLE")
    darcs = [(float(x["10"]), float(x["20"]), float(x["40"])) for x in ents if x["type"] == "ARC"]
    svg = r.get("asym_svg", "")
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    H = float(vb.group(2)) if vb else 0
    scirc = sorted((float(a), H - float(b), float(c)) for a, b, c in
                   re.findall(r'<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"/>', svg))
    check("asymmetric profile: DXF hole centres at the model positions (lower-left corner at 0,0)",
          len(dcirc) == 2 and all(near(a, b) for p_, q in zip(dcirc, [(15, 30, 3), (45, 10, 2)]) for a, b in zip(p_, q)),
          dcirc)
    check("SVG extents are the real profile 66 × 40 (an arc's full circle does not count)",
          vb and near(float(vb.group(1)), 66, 0.01) and near(H, 40, 0.01), vb and vb.groups())
    check("SVG circles = DXF circles with y flipped (not mirrored): the upper-left Ø6 is in the SVG's top half",
          len(scirc) == 2 and all(near(a, b) for p_, q in zip(scirc, dcirc) for a, b in zip(p_, q))
          and re.search(r'<circle cx="15" cy="10" r="3"/>', svg), (scirc, dcirc))
    sarcs = []
    for x1, y1, rx, _ry, fa, fs, x2, y2 in re.findall(
            r'<path d="M (\S+) (\S+) A (\S+) (\S+) 0 (\d) (\d) (\S+) (\S+)"/>', svg):
        x1, y1, rr, x2, y2 = map(float, (x1, y1, rx, x2, y2))
        hx, hy = (x1 - x2) / 2, (y1 - y2) / 2
        k = (max(rr * rr - hx * hx - hy * hy, 0) / (hx * hx + hy * hy)) ** 0.5 * (1 if fa != fs else -1)
        sarcs.append((k * hy + (x1 + x2) / 2, H - (-k * hx + (y1 + y2) / 2), rr))
    check("SVG arcs (endpoint form, centre from the flags) land on the DXF arc centres: R8 corner and the bulge",
          len(sarcs) == len(darcs) == 2 and all(any(all(near(a, b, 0.01) for a, b in zip(sa, da)) for da in darcs)
                                                for sa in sarcs)
          and any(near(da[2], 8) and near(da[0], 8) and near(da[1], 32) for da in darcs), (sarcs, darcs))
    check("SVG states its coordinate relation to the DXF", "DXF y = 40 - svg y" in svg and "seen from +Z" in svg
          and "not mirrored" in svg, svg[:400])
    check("DXF/SVG exports verified", [x["ok"] for x in r.get("asym_recs", [])] == [True, True], r.get("asym_recs"))
    check("manufacture's MIN_WALL is the standards table", r.get("minwall_same") is True)
    it = r.get("item_size") or {}
    check("item_size: two washers → each 10 × 10 × 1, 2 found; head+shank screws count as one item each",
          it.get("washers", {}).get("count") == 2 and [round(v, 3) for v in it["washers"]["size"]] == [10, 10, 1]
          and it["washers"]["identical"] and it.get("screws", {}).get("count") == 2
          and [round(v, 2) for v in it["screws"]["size"]] == [5.5, 5.5, 13]
          and it.get("single", {}).get("count") == 1 and it.get("mixed", {}).get("identical") is False
          and [round(v, 3) for v in it["mixed"]["size"]] == [10, 10, 4], it)
    check("part_record fits: upper deviation first, like the drawing",
          r.get("fits") == ["Ø8 k6 (+0.010/+0.001)", "Ø22 h6 (+0.000/-0.013)"], r.get("fits"))

    t = (r.get("cnc_plate") or {}).get("text", "")
    check("CNC plate: features need access from +Z; outer faces from 6 sides", "features need access from +Z;" in t
          and "outer faces from 6 side(s) (facing and profiling)" in t and "machined from" not in t, t)
    check("CNC plate: internal corners R3 → end mill ≤ Ø6, pocket depth 3 OK, ready",
          "internal corners R3 → end mill ≤ Ø6 (pocket depth 3 ≤ 4 × Ø6 ✓)" in t
          and (r.get("cnc_plate") or {}).get("verdict") == "ok"
          and (r.get("cnc_plate") or {}).get("metrics", {}).get("max_tool_d") == 6, t)
    t = (r.get("cnc_plain") or {}).get("text", "")
    check("CNC plain plate: no features beyond the outer faces", "no features beyond the outer faces" in t
          and "outer faces from 6 side(s)" in t, t)
    t = (r.get("cnc_deep") or {}).get("text", "")
    check("CNC deep pocket: 28 deep at R2 > 4 × Ø4 warned once with the radius to use",
          "4 internal corner(s) R2 on walls up to 28 mm deep" in t and "≥ R3.5" in t and t.count("⚠ 4 internal") == 1
          and (r.get("cnc_deep") or {}).get("verdict") == "check", t)
    t = (r.get("cnc_two") or {}).get("text", "")
    check("CNC two-sided part: features need access from +Z and -Z; R4 corners",
          "features need access from +Z and -Z" in t and "R4 → end mill ≤ Ø8" in t, t)
    t = (r.get("cnc_round") or {}).get("text", "")
    check("CNC rounded plate: rounded outline is profiling, hole from +Z, no corner line",
          "features need access from +Z;" in t and "outer faces from 6 side(s)" in t and "internal corners" not in t, t)

    t = (r.get("turn") or {}).get("text", "")
    m = (r.get("turn") or {}).get("metrics", {})
    check("turning stock: Ø18 bar for Ø16 finished OD, cut length ≥ 28", "turned from Ø18 bar for Ø16 finished OD"
          in t and "cut length ≥ 28" in t and m.get("bar_d") == 18, t)

    for k, rr in (("cnc_slot_fillet", "R1.5"), ("cnc_groove", "R3"), ("cnc_halfbore", "R10")):
        t = (r.get(k) or {}).get("text", "")
        check("CNC %s: no end-mill corner / 'deeper than 4 × D' warning; ball or bull-nose radius %s, from +Z, ready"
              % (k[4:], rr), "internal corner" not in t and "deeper than" not in t and "end mill ≤" not in t
              and ("concave radii %s across the cutting direction" % rr) in t and "ball or bull-nose" in t
              and "features need access from +Z;" in t and (r.get(k) or {}).get("verdict") == "ok"
              and "max_tool_d" not in (r.get(k) or {}).get("metrics", {}), t)
    t = (r.get("cnc_open_slot") or {}).get("text", "")
    check("CNC open-ended slot: its vertical R2 corners are still end-mill corners",
          "internal corners R2 → end mill ≤ Ø4 (pocket depth 8 ≤ 4 × Ø4 ✓)" in t and "across" not in t, t)
    t = (r.get("cnc_window") or {}).get("text", "")
    check("CNC through window: R3 corners through the 10 mm plate are end-mill corners",
          "internal corners R3 → end mill ≤ Ø6 (pocket depth 10 ≤ 4 × Ø6 ✓)" in t, t)
    t = (r.get("cnc_slot_side") or {}).get("text", "")
    check("CNC through slot + a side hole (the cover picks +X first): the floor fillets are still not end-mill "
          "corners, whatever setups were picked", "internal corner" not in t and "deeper than" not in t
          and "end mill ≤" not in t and "concave radii R1.5 across the cutting direction" in t
          and "features need access from +X" in t, t)
    for k, d, bar, ld in (("turn_cone", 20, 22, "1.8"), ("turn_ball", 24, 25, "1.7"), ("turn_bead", 22, 25, "1.8"),
                          ("turn_x", 20, 22, "3.0")):
        t = (r.get(k) or {}).get("text", "")
        m = (r.get(k) or {}).get("metrics", {})
        check("turning %s: bar and L/D from the largest diameter of the whole part (Ø%d → Ø%d bar, L/D %s)"
              % (k[5:], d, bar, ld), ("turned from Ø%d bar for Ø%d finished OD" % (bar, d)) in t
              and ("(L/D %s)" % ld) in t and near(m.get("max_d"), d) and m.get("bar_d") == bar
              and "tailstock" not in t, t)
    check("turning cone head: min Ø still from the cylinders (Ø10 stem)",
          near(((r.get("turn_cone") or {}).get("metrics") or {}).get("min_d"), 10), r.get("turn_cone"))

    t = (r.get("fdm_L") or {}).get("text", "")
    check("FDM orientation in plain words (as modelled)",
          "print with the face that points -Z in the model down on the bed (as modelled)" in t, t)
    t = (r.get("fdm_fin") or {}).get("text", "")
    check("FDM: a 3 mm fin is laid flat — words name the bed face, never 'X up'",
          re.search(r"print with the face that points [+-]X in the model down on the bed:", t)
          and not re.search(r"[+-][XYZ] up", t), t)

    rec = r.get("rec") or {}
    check("part_record: hole compensation, nominal in the hole text", near(rec.get("hole_compensation"), 0.2)
          and any("2× Ø3.6 THRU (Ø3.4 nominal + 0.2 print compensation)" in h for h in rec.get("holes", [])), rec)
    check("part_record: notes from a string, density from the part's material (Aluminium 2.70), mass from it",
          rec.get("notes") == ["Deburr all edges"] and rec.get("density") == 2.7
          and near(rec.get("mass_g"), r.get("volume", 0) * 2.7 / 1000.0, 1e-6) and rec.get("density_note") is None,
          rec)
    rec2 = r.get("rec2") or {}
    check("part_record: notes list cleaned; unknown material → project density with a note saying what to set",
          rec2.get("notes") == ["one", "two"] and rec2.get("density") == 1.27
          and "parts.Bracket.density_g_cm3" in (rec2.get("density_note") or ""), rec2)
    rec3 = r.get("rec3") or {}
    check("part_record: the part's material = the project's but spelled with other spacing/case → the project "
          "density, no density note (same rule as density_info)", rec3.get("density") == 1.3
          and rec3.get("density_source") == "project material" and rec3.get("density_note") is None, rec3)

    rd = r.get("readme", "")
    bom = list(csv.reader(io.StringIO(r.get("bom", ""))))
    check("BOM: header has spec and notes columns", bom and bom[0][-2:] == ["spec", "notes"], bom[:1])
    names = [row[1] for row in bom[1:]]
    check("BOM: made parts and bought refs only (no context / none / legacy refs)",
          names == ["Bracket", "Mill", "Shaft", "Gusset", "Screw_M3x10", "M5Washers"], names)
    check("BOM: bought ref with qty and spec; made part notes",
          bom[5][2:4] == ["8", "buy"] and bom[5][-2] == "ISO 4762 M3x10 A2" and bom[1][-1] == "Deburr all edges"
          and bom[1][2:4] == ["3", "make"], bom)
    check("BOM: the drawing PDF is listed with its part", "Shaft_drawing.pdf" in bom[3][9], bom[3])
    check("README: printed part — oriented files, plain-word orientation, tolerance, compensation, notes",
          "already oriented for printing, place as imported" in rd
          and "3MF/STL are already oriented for printing — place as imported" in rd
          and "goes down on the bed" in rd and "General tolerance: typ. ±0.2 mm (FDM)." in rd
          and "Holes are modelled 0.2 mm larger in diameter to compensate for printing (in the 3MF/STL and the "
              "STEP) — print as modelled, no further hole compensation." in rd
          and "- Notes:\n  - Deburr all edges" in rd, rd[:3000])
    check("README: CNC part without a drawing says so plainly", "**Send:** the STEP model — ⚠ no drawing included: "
          "tolerances, threads and fits are not communicated" in rd and "General tolerances ISO 2768-m." in rd, rd)
    check("README: turned part — stock via bar_for and cut length, drawing governs",
          "Stock: Ø22 bar for Ø20 finished OD; cut length ≥ 63 mm (finished length 60 + 3 mm" in rd
          and "the STEP model and the PDF drawing (the drawing governs diameters, fits and threads)" in rd
          and "General tolerances ISO 2768-m unless stated on the drawing." in rd, rd)
    check("README: laser part — material thickness line, DXF profile + PDF drawing",
          "Material thickness: 3 mm." in rd and "the DXF profile (1:1, mm) and the PDF drawing; state material and "
          "thickness" in rd, rd)
    check("README: no drawing DXF mentioned", "drawing.dxf" not in rd and "DXF drawing" not in rd
          and "PDF/DXF" not in rd)
    check("README: purchased and reference parts separated; keep-outs and legacy refs placed right",
          "## Purchased parts (buy)" in rd and "| Screw_M3x10 | 8 | ISO 4762 M3x10 A2 |" in rd
          and "## Reference parts (used for checks — not supplied)" in rd and "| Motor |" in rd
          and "| Legacy |" in rd and "KeepOut" not in rd, rd)
    check("README: per-file verification table with ✓/✗ and 'oriented for printing'",
          "| `Bracket/Bracket.3mf` | 3mf | ✓ | 120 triangles, watertight; oriented for printing (model -X up) |" in rd
          and "| `Bracket/Bracket.stl` | stl | ✗ |" in rd and "| `Shaft/Shaft.step` | step | ✓ |" in rd, rd)
    check("README: failed export warned at the top", "⚠ 1 exported file(s) failed verification" in rd, rd[:600])
    check("README: checks changed since r0007", "## Checks changed since r0007" in rd and "- added: `fit`" in rd
          and "- removed: `old-gap`" in rd and "- changed" not in rd, rd)
    check("README: purchased hardware shows ONE item's size and the count found (not the bbox of all)",
          "| M5Washers | 4 | ISO 7089 M5 | each 10.0 × 10.0 × 1.0; 2 in model |" in rd
          and "| Spares | — | largest 10.0 × 10.0 × 4.0; 2 in model |" in rd and "Envelope" not in rd, rd)
    row = next((x for x in bom if x[1] == "M5Washers"), [])
    check("BOM: bought washers sized per item with the count in notes",
          row[8:9] == ["10.0 × 10.0 × 1.0"] and row[-1] == "size of one item; 2 in model", row)
    br = rd.split("## Build report", 1)[-1]
    check("README build report: no internal note/Δ/files/stdout lines, facts kept",
          "BUILD ✓  demo  r0009" in br and "params   a=1" in br and "✓ fit" in br and "warnings" in br
          and "no hole compensation" in br and "inputs unchanged" not in br and "Δ" not in br
          and "files    build" not in br and "debug 42" not in br and "\nnote" not in br, br)
    check("README Files: drawing PDFs listed with their dimension check; STEP of a compensated print says so",
          "| `Shaft/Shaft_drawing.pdf` | pdf | ✓ | drawing sheet; 7/7 dimensions verified against the model |" in rd
          and "| `Gusset/Gusset_drawing.pdf` | pdf | ⚠ | drawing sheet; 5/6 dimensions verified against the model |"
          in rd and "| `Bracket/Bracket.step` | step | ✓ | re-imported; holes include +0.2 print compensation |" in rd
          and "| `Mill/Mill.step` | step | ✓ | re-imported |" in rd, rd)
    rm = r.get("readme_missing", "")
    check("README Files: a drawing PDF missing from the package is ✗", "| `Mill/Mill_drawing.pdf` | pdf | ✗ | "
          "drawing sheet; missing from the package |" in rm, rm)
    rn = r.get("readme_nodims", "")
    check("README Files: a drawing without a reported dimension check is not marked verified",
          "| `Shaft/Shaft_drawing.pdf` | pdf | — | drawing sheet; dimension check not reported |" in rn, rn)
    rp = r.get("readme_plain", "")
    check("README without changes/refs/exports: no empty sections", "Checks changed" not in rp
          and "Purchased" not in rp and "Reference parts" not in rp and "## Files" not in rp, rp)

    print("release docs, round 4 (#53 #46 #47 #48)")
    dims = {k: tuple(v) for k, v in r.get("dims") or []}
    check("drawing dims: ✓ only when all verified and > 0; ⚠ for some/none verified or nothing to verify; — when "
          "the total is unknown (a bare count) or unreadable",
          dims.get("[7, 7]") == ("7/7 dimensions verified against the model", "✓")
          and dims.get("[5, 7]", ("", ""))[1] == "⚠" and dims.get("[0, 0]", ("", ""))[1] == "⚠"
          and dims.get("[0, 5]", ("", ""))[1] == "⚠" and dims.get("{'verified': 3, 'total': 3}", ("", ""))[1] == "✓"
          and dims.get("7") == ("7 dimensions verified against the model (total not reported)", "—")
          and dims.get("0") == ("no dimensions verified against the model", "⚠")
          and dims.get("'5/6'", ("", ""))[1] == "⚠" and dims.get("'7'", ("", ""))[1] == "—"
          and dims.get("None") == ("dimension check not reported", "—") and dims.get("'n/a'", ("", ""))[1] == "—",
          dims)
    dc = r.get("dfm_comp_cnc") or {}
    cf = [x for x in dc.get("findings", []) if x.get("code") == "print_comp"]
    check("dfm: a print-compensated part checked for CNC gets a warning to rebuild with compensation 0",
          len(cf) == 1 and cf[0]["severity"] == "warn" and "0.2 mm larger" in cf[0]["message"]
          and "rebuild with compensation 0 before machining" in cf[0]["message"]
          and 'parts.Bracket.process to "cnc"' in cf[0]["message"] and dc.get("verdict") in ("check", "blocked"), cf)
    check("…not for the print process it was compensated for; laser says 'before cutting'",
          not any(x.get("code") == "print_comp" for x in (r.get("dfm_comp_fdm") or {}).get("findings", []))
          and any("rebuild with compensation 0 before cutting" in m for m in r.get("dfm_comp_laser") or []))
    cb = r.get("dfm_comp_built_cnc") or []
    check("…a part already built for CNC (explicit compensation= in model.py): the fix says remove it, not 'set the "
          "process'", len(cb) == 1 and "remove compensation=… from its fc.holes(...) calls" in cb[0]
          and "parts.Bracket.process" not in cb[0], cb)
    rc = r.get("readme_comp", "")
    check("README: CNC release of a compensated part — ⚠ line naming the fix, once (not repeated in the "
          "manufacturability notes)", "- ⚠ The holes in the exported files are modelled 0.2 mm larger" in rc
          and "rebuild with compensation 0 before machining" in rc
          and rc.count("rebuild with compensation 0 before machining: set") == 1
          and "print as modelled, no further hole compensation" not in rc, rc)
    check("README Files: the CNC STEP keeps the compensation note with ⚠ rebuild",
          "| `Bracket/Bracket.step` | step | ✓ | re-imported; holes include +0.2 print compensation ⚠ rebuild with "
          "compensation 0 before machining |" in rc, rc)
    check("README purchased: qty 1 but 4 in the model is flagged in the table and below it; qty 4 is not",
          "| M3Screws | 1 ⚠ | ISO 4762 M3x12 | each 5.5 × 5.5 × 13.0; 4 in model |" in rc
          and "- M3Screws: ⚠ qty 1 but 4 in the model — set fc.ref_part(..., qty=4)" in rc
          and "| M3Enough | 4 | ISO 4762 M3x12 |" in rc and "M3Enough: ⚠" not in rc
          and "Fixture: ⚠" not in rc, rc)
    bom2 = {row[1]: row for row in csv.reader(io.StringIO(r.get("bom_comp", "")))}
    check("BOM: short qty flagged in the notes; mixed sizes say 'largest' like the README",
          bom2.get("M3Screws", [""])[-1] == "size of one item; 4 in model; ⚠ qty 1 but 4 in the model — set "
          "fc.ref_part(..., qty=4) (the total to buy) in model.py if all of them are bought"
          and bom2.get("M3Enough", [""])[-1] == "size of one item; 4 in model"
          and bom2.get("Hardware", [""])[-1] == "size of the largest item; 2 in model (sizes differ)"
          and "| Hardware | 2 | kit | largest 10.0 × 10.0 × 4.0; 2 in model |" in rc, bom2)

    print("lookup_standard and release over stdio")
    for name in ("turned_shaft", "nema17_bracket"):
        shutil.copytree(os.path.join(ROOT, "examples", name), os.path.join(tmp, name),
                        ignore=shutil.ignore_patterns("build", "out", ".history", "release", "drawings",
                                                      "drawings.json"))
    pj = os.path.join(tmp, "nema17_bracket", "project.json")
    proj = json.load(open(pj))
    proj["parts"]["Bracket"].update(material="PLA", notes=["Print in black", "0.2 mm layers"], qty=2)
    json.dump(proj, open(pj, "w"), indent=2)
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    err, txt, _ = c.tool("lookup_standard", {"process": "turning"})
    check("lookup_standard(process='turning') returns the turning rules", not err and "tailstock" in txt
          and "1.5 × pitch" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"process": "laser"})
    check("lookup_standard(process='laser') has the richer rules", not err and "edge distance" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "k6", "d": 8})
    check("lookup_standard(fit='k6', d=8) gives the shaft limits", not err and "Ø8 k6 (+0.010/+0.001)" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "H11", "d": 8.2})
    check("lookup_standard(fit='H11', d=8.2) gives the hole limits", not err and "8.2000 … 8.2900" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"fit": "k", "d": 8})
    check("lookup_standard bad class: error with examples, not a Python unpack error",
          err and "'k6'" in txt and "unpack" not in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"size": "608"})
    check("lookup_standard(size='608') answers with the bearing", not err and "outside D 22" in txt, txt)
    for name in ("turned_shaft", "nema17_bracket"):
        c.tool("build", {"project": name, "views": []})
    err, txt, _ = c.tool("check_manufacturing", {"project": "turned_shaft"})
    check("check_manufacturing turning: Ø22 bar for the Ø20 shaft, cut length", not err
          and "turned from Ø22 bar for Ø20 finished OD" in txt and "cut length ≥ 51" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "nema17_bracket"})
    check("check_manufacturing FDM: orientation in plain words", not err
          and re.search(r"print with the face that points [+-][XYZ] in the model down on the bed", txt), txt)
    err, txt, _ = c.tool("release", {"project": "nema17_bracket"})
    rel = os.path.join(tmp, "nema17_bracket", "release")
    zips = sorted(f for f in os.listdir(rel) if f.endswith(".zip")) if os.path.isdir(rel) else []
    check("FDM release written", not err and zips, txt)
    if zips:
        z = zipfile.ZipFile(os.path.join(rel, zips[-1]))
        readme = z.read([n for n in z.namelist() if n.endswith("README.md")][0]).decode()
        bomtxt = z.read([n for n in z.namelist() if n.endswith("BOM.csv")][0]).decode()
        check("…README: notes, plain-word orientation, FDM tolerance, PLA density for the mass",
              "- Notes:\n  - Print in black\n  - 0.2 mm layers" in readme and "goes down on the bed" in readme
              and "General tolerance: typ. ±0.2 mm (FDM)" in readme and "(1.24 g/cm³)" in readme
              and "quantity 2" in readme, readme[:2500])
        check("…the motor is a reference part, not bought", "## Reference parts (used for checks — not supplied)"
              in readme and "| Motor |" in readme and "Purchased parts" not in readme, readme)
        br = readme.split("## Build report", 1)[-1]
        check("…README build report without internal lines", "BUILD ✓" in br and "\nΔ " not in br
              and "\nnote " not in br and "\nfiles " not in br, br)
        rows = list(csv.reader(io.StringIO(bomtxt)))
        check("…BOM: Bracket only, notes column filled", [x[1] for x in rows[1:]] == ["Bracket"]
              and rows[1][-1] == "Print in black; 0.2 mm layers" and rows[1][2:4] == ["2", "make"], rows)
    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
