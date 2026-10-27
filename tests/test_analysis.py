#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""server/analysis.py: hole detection, measure
semantics, check_fit wording and the new checks — over real stdio with real
FreeCAD, on fixtures whose answers are known in advance:

  CncPlate   100 × 60 × 8 at (0, 200), R3 pocket 40 × 20 × 3, 4 × M5 tapped THRU on 80 × 40
  Spacer     Ø16 × 25 turned, Ø8.2 bore, 0.5 chamfers on all four circular edges
  Gusset     60 × 60 × 3 triangle at x = 100, Ø5.5 holes at (110, 10) (140, 10) (110, 40)
  Bracket    60 × 30 × 5 plate, two Ø3.6 holes 20 apart; Screws: Ø5.5 heads on top, Ø3 shanks
  Heads      a lone Ø5.5 screw head on Bracket hole 0 (nothing inside the hole)
  Lap        30 × 20 × 4 plate with two Ø6 holes 4 apart (they run into each other)
  Motor      box at x = 400 with a frame 'shaft' whose +Z points −X (a horizontal shaft)
  Housing    40 × 40 × 30 at (600, 0), Ø20.4 bore; Shaft Ø20 ref 0.05 off-centre in +X (gaps 0.15 / 0.25)
  Stud       Ø5 ref screwed into CncPlate's M5 tapped hole at (40, 220) (a thread overlap)
  Shelf      ref with two -Z faces, at z = 12 and z = 22
  Printed    plate with 2 × M3 clearance holes, compensation 0.2 (modelled Ø3.6)
  round 4 (x ≥ 1000): Housing2 Ø8 bore + BigShaft Ø8.02 (interference), StackA/StackB stacked Ø3.4 holes
  0.3 apart (StackC's 2 apart), PinA/PinB near-coaxial pins, Csk M4 countersink 5 from an edge (0.5 wall at
  the top), Drilled Ø6 ↧18 with a drill point in a 20 block (0.197 under the tip), Chamfered Ø4 hole 2 from
  an edge under a 1.5 chamfer (0.5 at the top), Clevis Ø8 through two lugs, Cbored Ø3.4/Ø6.5 + CapScrew
  seated on the floor, TapBlock M5 under Clamp Ø5.5 (touching), Motor2/Mount2 frames on one axis, Printed2
  4 × M3 clearance + 2 × Ø3.6 pins (compensation 0.2: modelled Ø3.6 and Ø3.8)

    python3 tests/test_analysis.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import FC_PY, Client, check, fails  # noqa: E402

ROOT = os.path.dirname(HERE)

MODEL = '''# fixtures for the analysis tests
P = fc.params()

cnc = fc.body("CncPlate")
fc.pad(cnc, fc.sketch(cnc, "XY", fc.rect(100, 60, center=(0, 200)), name="CncSketch"), 8, name="CncBase")
fc.pocket(cnc, fc.sketch(cnc, "XY", fc.rrect(40, 20, 3, center=(0, 200)), name="PocketSketch", offset=8), 3,
          name="Pocket")
fc.holes(cnc, on=fc.face(cnc, "+Z", at=8), at=fc.rect_pattern(80, 40, center=(0, 200)), spec="M5 tapped",
         depth="through", name="M5")

sp = fc.body("Spacer")
fc.turned(sp, [(16, 25)], name="Turned")
fc.holes(sp, on=fc.face(sp, "+Z"), at=[(0, 0)], spec="Ø8.2", depth="through", name="Bore")
fc.chamfer(sp, fc.edges(sp, kind="circle"), 0.5, name="Chamfers")

gu = fc.body("Gusset")
fc.pad(gu, fc.sketch(gu, "XY", fc.polygon([(100, 0), (160, 0), (100, 60)]), name="GussetSketch"), P.t,
       name="GussetPlate")
fc.holes(gu, on=fc.face(gu, "+Z"), at=[(110, 10), (140, 10), (110, 40)], spec="Ø5.5", name="GussetHoles")

br = fc.body("Bracket")
fc.pad(br, fc.sketch(br, "XY", fc.rect(60, 30, center=(300, 15)), name="BracketSketch"), 5, name="BracketPlate")
fc.holes(br, on=fc.face(br, "+Z"), at=[(290, 15), (310, 15)], spec="Ø3.6", name="BracketHoles")
screws = []
for x in (290, 310):
    head = Part.makeCylinder(2.75, 3, Vector(x, 15, 5))
    shank = Part.makeCylinder(1.5, 10, Vector(x, 15, -5))
    screws.append(head.fuse(shank).removeSplitter())
fc.ref_part("Screws", shape=Part.makeCompound(screws))
fc.ref_part("Heads", shape=Part.makeCylinder(2.75, 3, Vector(290, 15, 5)))
lap = fc.body("Lap")
fc.pad(lap, fc.sketch(lap, "XY", fc.rect(30, 20, center=(300, 100)), name="LapSketch"), 4, name="LapPlate")
fc.holes(lap, on=fc.face(lap, "+Z"), at=[(296, 100), (300, 100)], spec="Ø6", name="LapHoles")
fc.ref_part("Motor", shape=Part.makeBox(42, 42, 42, Vector(400, -21, -21)),
            frames={"shaft": fc.frame(400, 0, 0, normal="-X")})

ho = fc.body("Housing")
fc.pad(ho, fc.sketch(ho, "XY", fc.rect(40, 40, center=(600, 0)), name="HousingSketch"), 30, name="HousingBlock")
fc.holes(ho, on=fc.face(ho, "+Z"), at=[(600, 0)], spec="Ø20.4", depth="through", name="HousingBore")
fc.ref_part("Shaft", shape=Part.makeCylinder(10, 50, Vector(600.05, 0, -10)))
fc.ref_part("Stud", shape=Part.makeCylinder(2.5, 12, Vector(40, 220, -2)))
fc.ref_part("Shelf", shape=Part.makeBox(10, 10, 20, Vector(700, 0, 12)).fuse(
    Part.makeBox(20, 10, 10, Vector(700, 0, 22))).removeSplitter())
pr = fc.body("Printed")
fc.pad(pr, fc.sketch(pr, "XY", fc.rect(30, 20, center=(800, 0)), name="PrintedSketch"), 4, name="PrintedPlate")
fc.holes(pr, on=fc.face(pr, "+Z"), at=[(792, 0), (808, 0)], spec="M3 clearance normal", compensation=0.2,
         name="PrintedHoles")
# round 4 fixtures (x >= 1000, clear of the others)
fc.ref_part("BigShaft", shape=Part.makeCylinder(4.01, 40, Vector(1000, 0, -5)))
h2 = fc.body("Housing2")
fc.pad(h2, fc.sketch(h2, "XY", fc.rect(20, 20, center=(1000, 0)), name="Housing2Sketch"), 30, name="Housing2Block")
fc.holes(h2, on=fc.face(h2, "+Z"), at=[(1000, 0)], spec="Ø8", depth="through", compensation=0, name="Housing2Bore")
sa = fc.body("StackA")
fc.pad(sa, fc.sketch(sa, "XY", fc.rect(20, 20, center=(1100, 0)), name="StackASketch"), 3, name="StackAPlate")
fc.holes(sa, on=fc.face(sa, "+Z"), at=[(1100, 0)], spec="Ø3.4", compensation=0, name="StackAHole")
sb = fc.body("StackB")
fc.pad(sb, fc.sketch(sb, "XY", fc.rect(20, 20, center=(1100, 0)), name="StackBSketch", offset=3), 3,
       name="StackBPlate")
fc.holes(sb, on=fc.face(sb, "+Z"), at=[(1100.3, 0)], spec="Ø3.4", compensation=0, name="StackBHole")
sc = fc.body("StackC")
fc.pad(sc, fc.sketch(sc, "XY", fc.rect(20, 20, center=(1100, 0)), name="StackCSketch", offset=6), 3,
       name="StackCPlate")
fc.holes(sc, on=fc.face(sc, "+Z"), at=[(1102, 0)], spec="Ø3.4", compensation=0, name="StackCHole")
fc.ref_part("PinA", shape=Part.makeCylinder(1.5, 10, Vector(1100, 50, 0)))
fc.ref_part("PinB", shape=Part.makeCylinder(2.5, 10, Vector(1100.2, 50, 12)))
cs = fc.body("Csk")
fc.pad(cs, fc.sketch(cs, "XY", fc.rect(20, 20, center=(1210, 10)), name="CskSketch"), 6, name="CskPlate")
fc.holes(cs, on=fc.face(cs, "+Z"), at=[(1205, 10)], spec="M4 clearance + csk ISO10642", compensation=0,
         name="CskHole")
dp = fc.body("Drilled")
fc.pad(dp, fc.sketch(dp, "XY", fc.rect(20, 20, center=(1310, 10)), name="DrilledSketch"), 20, name="DrilledBlock")
fc.holes(dp, on=fc.face(dp, "+Z"), at=[(1310, 10)], spec="Ø6", depth=18, compensation=0, name="DrilledHole")
ch = Part.makeBox(30, 20, 10, Vector(1400, 0, 0))
ch = ch.makeChamfer(1.5, [e for e in ch.Edges if abs(e.BoundBox.XMax - 1400) < 1e-6 and e.BoundBox.ZMin > 9.9])
ch = ch.cut(Part.makeCylinder(2, 10, Vector(1404, 10, 0)))
fc.ref_part("Chamfered", shape=ch)
cl = Part.makeBox(40, 20, 5, Vector(1500, 0, 0)).fuse([Part.makeBox(5, 20, 25, Vector(1500, 0, 5)),
                                                       Part.makeBox(5, 20, 25, Vector(1535, 0, 5))]).removeSplitter()
cl = cl.cut(Part.makeCylinder(4, 50, Vector(1490, 10, 20), Vector(1, 0, 0)))
fc.ref_part("Clevis", shape=cl)
cb = fc.body("Cbored")
fc.pad(cb, fc.sketch(cb, "XY", fc.rect(20, 20, center=(1600, 0)), name="CboredSketch"), 8, name="CboredPlate")
fc.holes(cb, on=fc.face(cb, "+Z"), at=[(1600, 0)], spec="Ø3.4 + cbore 6.5x3", compensation=0, name="CboredHole")
fc.ref_part("CapScrew", shape=Part.makeCylinder(2.75, 3, Vector(1600, 0, 5)).fuse(
    Part.makeCylinder(1.5, 12, Vector(1600, 0, -4))).removeSplitter())
tb = fc.body("TapBlock")
fc.pad(tb, fc.sketch(tb, "XY", fc.rect(20, 20, center=(1700, 0)), name="TapBlockSketch"), 10, name="TapBlockPlate")
fc.holes(tb, on=fc.face(tb, "+Z"), at=[(1700, 0)], spec="M5 tapped", depth="through", name="TapBlockHole")
cp = fc.body("Clamp")
fc.pad(cp, fc.sketch(cp, "XY", fc.rect(20, 20, center=(1700, 0)), name="ClampSketch", offset=10), 5,
       name="ClampPlate")
fc.holes(cp, on=fc.face(cp, "+Z"), at=[(1700, 0)], spec="Ø5.5", compensation=0, name="ClampHole")
fc.ref_part("Motor2", shape=Part.makeBox(10, 10, 10, Vector(1795, -5, -10)),
            frames={"shaft": fc.frame(1800, 0, 0, normal="+Z")})
fc.ref_part("Mount2", shape=Part.makeBox(10, 10, 5, Vector(1795, -5, 20)),
            frames={"bore": fc.frame(1800, 0, 20, normal="-Z"), "side": fc.frame(1800, 0, 20, normal="+X")})
p2 = fc.body("Printed2")
fc.pad(p2, fc.sketch(p2, "XY", fc.rect(60, 30, center=(1900, 0)), name="Printed2Sketch"), 4, name="Printed2Plate")
fc.holes(p2, on=fc.face(p2, "+Z"), at=[(1880, -8), (1880, 8), (1920, -8), (1920, 8)], spec="M3 clearance normal",
         compensation=0.2, name="Printed2M3")
fc.holes(p2, on=fc.face(p2, "+Z"), at=[(1890, 0), (1910, 0)], spec="Ø3.6", compensation=0.2, name="Printed2Pins")
'''

CHECKS = [
    {"id": "cnc-holes", "type": "hole_count", "object": "CncPlate", "count": 4},
    {"id": "spacer-holes", "type": "hole_count", "object": "Spacer", "count": 1},
    {"id": "m5-positions", "type": "positions", "holes": "CncPlate.holes(d=4.2)",
     "points": [[40, 220, 0], [-40, 220, 0], [40, 180, 0], [-40, 180, 0]]},
    {"id": "m5-pos-2d", "type": "positions", "holes": "CncPlate.holes", "axis": "+Z",
     "points": [[-40, 180], [40, 180], [-40, 220], [40, 220]], "tol": 0.01},
    {"id": "pos-wrong", "type": "positions", "holes": "CncPlate.holes",
     "points": [[40, 220, 0], [-40, 220, 0], [40, 180.5, 0]]},
    {"id": "pos-extra-ok", "type": "positions", "holes": "CncPlate.holes", "allow_extra": True,
     "points": [[40, 220, 0], [-40, 220, 0]]},
    {"id": "pocket-r3", "type": "face_count", "object": "CncPlate", "surface": "cylinder", "radius": 3,
     "concave": True, "count": 4},
    {"id": "chamfers", "type": "face_count", "object": "Spacer", "surface": "cone", "count": 4},
    {"id": "bore-chamfers", "type": "face_count", "object": "Spacer", "surface": "cone", "concave": True, "count": 2},
    {"id": "od-split-once", "type": "face_count", "object": "Spacer", "surface": "cylinder", "concave": False,
     "radius": 8, "count": 1},
    {"id": "fillets-none", "type": "face_count", "object": "Gusset", "surface": "torus", "max": 0},
    {"id": "spacer-wall", "type": "distance", "a": "Spacer.holes[0]", "b": "Spacer", "min": 2.85},
    {"id": "web-hyp", "type": "distance", "a": "Gusset.holes(d=5.5)[1]", "b": "Gusset.face(+X+Y)",
     "value": 4.3211, "tol": 0.001},
    {"id": "hole-to-motor", "type": "distance", "a": "Bracket.holes[1]", "b": "Motor.face(-X)",
     "value": 88.2, "tol": 0.001},
    {"id": "shank-gap", "type": "radial_gap", "a": "Bracket.holes", "b": "Screws", "min": 0.25},
    {"id": "screws-seat", "type": "clearance", "a": "Screws", "b": "Bracket", "min": 0.3, "contact_ok": True},
    {"id": "shaft-horizontal", "type": "parallel", "a": "Motor.frame(shaft)", "b": "Bracket.face(-Z)"},
    {"id": "shaft-not-vert", "type": "perpendicular", "a": "Motor.frame(shaft)", "b": "Bracket.face(-Z)"},
    {"id": "gusset-env", "type": "bbox", "object": "Gusset", "min": [59.99, 59.99, 2.99], "max": [60.01, 60.01, 3.01]},
    {"id": "gusset-mass", "type": "mass", "object": "Gusset", "max_g": 20},
    {"id": "cnc-wall-major", "type": "distance", "a": "CncPlate.holes[0]", "b": "CncPlate", "value": 7.5,
     "tol": 0.001},
    {"id": "printed-nominal", "type": "hole_diameter", "holes": "Printed.holes", "d": 3.4, "nominal": True},
    {"id": "printed-modelled", "type": "hole_diameter", "holes": "Printed.holes", "d": 3.6},
    {"id": "printed-as-nominal", "type": "hole_diameter", "holes": "Printed.holes", "d": 3.4},
    {"id": "tap-drill", "type": "hole_diameter", "object": "CncPlate", "d": 4.2},
    {"id": "shank-pos", "type": "positions", "holes": "Screws.bosses(d=3)", "points": [[290, 15, 0], [310, 15, 0]]},
    {"id": "boss-gap", "type": "radial_gap", "a": "Bracket.holes[1]", "b": "Screws.bosses(d=3)[1]", "min": 0.29},
    {"id": "boss-coax", "type": "coaxial", "a": "Bracket.holes[1]", "b": "Screws.bosses(d=3)[1]"},
    {"id": "frame-point", "type": "distance", "a": "Motor.frame(shaft)", "b": [410, 3, 4], "value": 5},
    {"id": "axis-object", "type": "distance", "a": "axis([600,0,0],[0,0,1])", "b": "Housing", "value": 10.2},
    # round 4
    {"id": "big-gap", "type": "radial_gap", "a": "Housing2.holes[0]", "b": "BigShaft.bosses[0]", "min": 0},
    {"id": "big-gap-face", "type": "radial_gap", "a": "Housing2.holes[0]", "b": "BigShaft.Face1", "min": 0},
    {"id": "csk-wall", "type": "distance", "a": "Csk.holes[0]", "b": "Csk", "min": 1.5},
    {"id": "floor", "type": "distance", "a": "Drilled.holes[0]", "b": "Drilled.face(-Z)", "min": 1.5},
    {"id": "chamfer-wall", "type": "distance", "a": "Chamfered.holes[0]", "b": "Chamfered", "min": 1.5},
    {"id": "clevis-n", "type": "hole_count", "object": "Clevis", "count": 2},
    {"id": "clevis-pos", "type": "positions", "holes": "Clevis.holes", "axis": "+X",
     "points": [[10, 20], [10, 20]]},
    {"id": "frames-coax", "type": "coaxial", "a": "Motor2.frame(shaft)", "b": "Mount2.frame(bore)"},
    {"id": "frames-par", "type": "parallel", "a": "Motor2.frame(shaft)", "b": "Mount2.frame(bore)"},
    {"id": "frames-perp", "type": "perpendicular", "a": "Motor2.frame(shaft)", "b": "Mount2.frame(side)"},
    {"id": "frames-align", "type": "align", "a": "Motor2.frame(shaft)", "b": "Mount2.frame(bore)"},
    {"id": "pins-n", "type": "hole_count", "holes": "Printed2.holes(d=3.6)", "count": 2},
    {"id": "pins-pos", "type": "positions", "holes": "Printed2.holes(d=3.6)", "points": [[1890, 0, 0], [1910, 0, 0]]},
    {"id": "m3-design", "type": "hole_diameter", "holes": "Printed2.holes(d=3.4)", "d": 3.4, "nominal": True},
    {"id": "tap-clamp", "type": "clearance", "a": "TapBlock", "b": "Clamp", "min": 1.0, "contact_ok": True},
    {"id": "bad-points", "type": "positions", "holes": "Printed2.holes", "points": [30, 20, 0]},
    {"id": "bad-d", "type": "hole_diameter", "holes": "Printed2.holes", "d": None},
    {"id": "bad-radius", "type": "face_count", "object": "Clevis", "surface": "cylinder", "radius": None, "count": 1},
    {"id": "bad-missing", "type": "positions", "holes": "Printed2.holes"},
    {"id": "head-cbore", "type": "radial_gap", "a": "Cbored.holes[0]", "b": "CapScrew.bosses(d=5.5)", "min": 0.4},
    {"id": "head-face", "type": "radial_gap", "a": "Bracket.holes[0]", "b": "Screws.bosses(d=5.5)[0]", "min": 0},
]


def line(cid, txt):
    m = re.search(r"^\s*([✓✗]) %s\s.*$" % re.escape(cid), txt, re.M)
    return m.group(0) if m else ""


def num(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixA-"))
try:
    proj = os.path.join(tmp, "fixa")
    os.makedirs(proj)
    with open(os.path.join(proj, "model.py"), "w") as fh:
        fh.write(MODEL)
    with open(os.path.join(proj, "params.json"), "w") as fh:
        json.dump({"t": {"value": 3, "unit": "mm", "source": "test fixture"}}, fh)
    with open(os.path.join(proj, "checks.json"), "w") as fh:
        json.dump(CHECKS, fh, indent=1)
    with open(os.path.join(proj, "project.json"), "w") as fh:
        json.dump({"name": "fixa", "process": "fdm", "material": {"name": "PETG", "density_g_cm3": 1.27},
                   "clearance": {"default_min_mm": 0.0}}, fh)
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    print("build + checks.json")
    err, txt, _ = c.tool("build", {"project": "fixa", "views": []})
    check("fixtures build", not err and "BUILD ✓" in txt, txt)
    exp_fail = {"pos-wrong", "shaft-not-vert", "printed-as-nominal", "big-gap", "big-gap-face", "csk-wall", "floor",
                "chamfer-wall", "bad-points", "bad-d", "bad-radius", "bad-missing", "head-face"}
    bad = [k["id"] for k in CHECKS if line(k["id"], txt).lstrip().startswith("✗")]
    check("exactly the designed check failures", set(bad) == exp_fail, "\n".join(line(k["id"], txt)
                                                                                for k in CHECKS))

    print("hole detection")
    check("R3 pocket corners are not holes: 4 holes on the CNC plate", line("cnc-holes", txt).startswith("  ✓"),
          line("cnc-holes", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "CncPlate.holes(d=6)"})
    check("…holes(d=6) matches nothing", "no holes match" in t2, t2)
    check("turned spacer: one bore, the convex OD chamfers are not part of it",
          line("spacer-holes", txt).startswith("  ✓"), line("spacer-holes", txt))
    err, t2, _ = c.tool("inspect", {"project": "fixa", "object": "Spacer"})
    hl = [ln for ln in t2.splitlines() if "Ø8.200" in ln and "THRU" in ln]
    check("…the bore spans z 0…25, not −7.5…32.5 (cone apexes off the face ignored)",
          hl and "25.00" in hl[0] and "32.50" not in hl[0] and "-7.50" not in hl[0], "\n".join(hl) or t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "CncPlate.holes"})
    check("tapped holes stay through holes Ø4.2", t2.count("Ø4.200 axis +Z THRU") == 4, t2)

    print("two parallel holes")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Bracket.holes[1]"})
    check("axis spacing 20 and wall-to-wall 16.4, no OVERLAP", "axis spacing 20.0000 mm; wall-to-wall 16.4000 mm"
          in t2 and "OVERLAP" not in t2 and "radial gap" not in t2, t2)

    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Lap.holes[0]", "b": "Lap.holes[1]"})
    check("two Ø6 holes 4 apart (each wraps ~¾ turn, still holes): they intersect",
          "axis spacing 4.0000 mm; wall-to-wall -2.0000 mm  ✗ the holes intersect" in t2 and "OVERLAP" not in t2, t2)

    print("screw in a hole")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Screws"})
    rg = num(r"minimum radial gap ([-\d.]+)", t2)
    check("the shank inside the hole is measured, the head on the face is ignored",
          "coaxial Ø3.000 inside Ø3.600" in t2 and rg is not None and abs(rg - 0.3) < 1e-4
          and "OVERLAP" not in t2 and "Ø5.500 outside" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Heads"})
    check("a head on the face but nothing in the hole: no coaxial cylinder inside the hole",
          "no coaxial cylinder inside the hole (coaxial Ø5.500 lies outside its depth" in t2 and "OVERLAP" not in t2,
          t2)
    check("radial_gap check uses the shank: 0.300", re.search(r"✓ shank-gap\s+radial_gap\s+0\.300", txt),
          line("shank-gap", txt))

    print("material around a hole")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Spacer.holes[0]", "b": "Spacer"})
    check("hole ↔ its own part: minimum wall 2.9 at an end face (both 0.5 chamfers; 3.9 between them)",
          "minimum wall around the hole 2.9000 mm" in t2 and "OVERLAP" not in t2, t2)
    check("distance check on hole ↔ own part uses the minimum wall", re.search(r"✓ spacer-wall\s+distance\s+2\.900",
                                                                               txt), line("spacer-wall", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Gusset.holes", "b": "Gusset"})
    check("all holes ↔ own part: worst hole and one row per hole", "minimum wall around the holes (worst:" in t2
          and "4.3211 mm" in t2 and "7.2500 mm" in t2, t2)

    print("axis ↔ plane")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Motor.frame(shaft)", "b": "Bracket.face(-Z)"})
    check("a frame's +Z is an axis: lies parallel to the plane", "axis lies parallel to the plane (0.000°)" in t2, t2)
    check("parallel passes, perpendicular fails for a horizontal shaft",
          line("shaft-horizontal", txt).startswith("  ✓") and line("shaft-not-vert", txt).startswith("  ✗")
          and "90.000° from 90" in line("shaft-not-vert", txt), line("shaft-horizontal", txt) + "\n" +
          line("shaft-not-vert", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "axis([0,0,0],[0,0,1])", "b": "plane(z=3)"})
    check("axis(…) along the normal: normal to the plane (90°)", "axis is normal to the plane (90.000°)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.face(+Z)", "b": "CncPlate.face(+Z, at=8)"})
    check("plane ↔ plane unchanged", "planes at 0.000°" in t2 and "3.0000 apart" in t2, t2)

    print("distance with holes")
    check("hole ↔ own face (laser web) = 4.3211", line("web-hyp", txt).startswith("  ✓"), line("web-hyp", txt))
    check("hole ↔ another part's face uses the wall gap 88.2", line("hole-to-motor", txt).startswith("  ✓"),
          line("hole-to-motor", txt))

    print("check_fit")
    check("listed contact with nearest gap exactly the minimum passes", line("screws-seat", txt).startswith("  ✓"),
          line("screws-seat", txt))
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["Screws", "Bracket"], "min_clearance": 0.3,
                                      "contact_ok": [["Screws", "Bracket"]]})
    check("check_fit: equal gap within tolerance → all OK, threshold shown as required",
          "all OK ✓" in t2 and "required clearance ≥ 0.300 mm" in t2, t2)
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["Screws", "Bracket"], "min_clearance": 0.35,
                                      "contact_ok": [["Screws", "Bracket"]]})
    check("listed contact failing on its gap says so", "nearest other gap 0.3000 mm < required 0.35" in t2
          and "not listed" not in t2, t2)
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["Gusset"]})
    check("one object: nothing to check, not all OK", "nothing to check — 1 object(s)" in t2 and "all OK" not in t2,
          t2)

    print("bbox")
    check("bbox shows both limits", "≥ [59.99, 59.99, 2.99] and ≤ [60.01, 60.01, 3.01]" in line("gusset-env", txt),
          line("gusset-env", txt))

    print("face selection")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "CncPlate.face(+Z)"})
    check("face(+Z) with several faces says it took the outermost, at= with the coordinate",
          "outermost of 2 faces facing +Z: at=8 (z=8), at=5 (z=5)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Shelf.face(-Z)"})
    check("face(-Z): at= is the offset along the normal, the absolute z is printed too",
          "outermost of 2 faces facing -Z: at=-12 (z=12), at=-22 (z=22); at= is the offset along the face normal"
          in t2 and "through (705.00, 5.00, 12.00)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Shelf.face(-Z, at=-22)"})
    check("…and the printed at= picks the other face", "through (715.00, 5.00, 22.00)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Gusset.face(+X+Y)"})
    check("compound direction +X+Y selects the hypotenuse", "plane normal (0.707, 0.707, 0.000)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Gusset.face(+X-Y)"})
    check("…a direction no face has lists the faces there are", "its planar faces face" in t2 and "+Z" in t2, t2)

    print("positions / face_count")
    check("positions: 4 tapped holes where expected", line("m5-positions", txt).startswith("  ✓")
          and line("m5-pos-2d", txt).startswith("  ✓"), line("m5-positions", txt) + "\n" + line("m5-pos-2d", txt))
    pw = line("pos-wrong", txt)
    check("positions: a misplaced point names the nearest hole and the extra one", "no hole at (40.000, 180.500"
          in pw and "0.5000 mm off" in pw and "extra hole" in pw, pw)
    check("positions: allow_extra", line("pos-extra-ok", txt).startswith("  ✓"), line("pos-extra-ok", txt))
    check("face_count: 4 concave R3 corners, 4 chamfer cones (2 concave), OD split faces count once",
          all(line(k, txt).startswith("  ✓") for k in ("pocket-r3", "chamfers", "bore-chamfers", "od-split-once",
                                                         "fillets-none")),
          "\n".join(line(k, txt) for k in ("pocket-r3", "chamfers", "bore-chamfers", "od-split-once",
                                           "fillets-none")))

    print("measure in either order; frames and axes as lines (round 3)")
    for a, b in (("axis([400,0,0],[1,0,0])", [410, 3, 4]), ([410, 3, 4], "axis([400,0,0],[1,0,0])")):
        err, t2, _ = c.tool("measure", {"project": "fixa", "queries": [{"a": a, "b": b}]})
        check("axis ↔ point in either order: 5 from the axis, 10 along it (order %s first)" % (
            "axis" if isinstance(b, list) else "point"), not err and "5.0000 mm from the axis, 10.0000 along it"
              in t2 and "no shape" not in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "[410, 3, 4]", "b": "Motor.frame(shaft)"})
    check("point ↔ frame: radial distance from the frame's +Z axis, not the distance to its plane",
          "5.0000 mm from the frame's +Z axis, -10.0000 along it" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Motor.frame(shaft)", "b": "[410, 3, 4]"})
    check("…the same the other way round, labels in the order given",
          t2.splitlines()[-1].startswith("Motor.frame(shaft) ↔ [410.000") and "5.0000 mm from the frame's +Z axis" in t2,
          t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Housing", "b": "axis([600,0,0],[0,0,1])"})
    check("object ↔ axis: distance from the axis line 10.2 (the bore radius), the axis misses the material",
          "minimum distance from the axis (as an endless line) 10.2000 mm" in t2
          and "does not pass through Housing's material" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "axis([600,0,0],[0,0,1])", "b": "Shaft"})
    check("axis ↔ object: through the shaft over its 50 mm", "0.0000 mm" in t2
          and "passes through Shaft's material over 50.000 mm" in t2 and t2.splitlines()[-1].startswith("axis("), t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket", "b": "Motor.frame(shaft)"})
    check("object ↔ frame no longer fails ('has no shape')", not err and "no shape" not in t2
          and "the frame's +Z axis" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "plane(z=40)", "b": "Housing"})
    check("plane ↔ object: signed extent along the normal, 10 mm away",
          "Housing lies -40.0000 … -10.0000 mm from the plane" in t2 and "minimum distance 10.0000 mm" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "plane(z=0)", "b": "[1, 2, 3]"})
    check("plane ↔ point: signed distance 3, either order", "signed distance 3.0000 mm" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Screws", "b": "Bracket.holes[0]"})
    check("a hole given second is measured from the hole but labelled in the order given",
          t2.splitlines()[1].startswith("Screws ↔ Bracket.holes[0]: nearest distance from the hole wall"), t2)

    print("hole rim touching the other part (round 3)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Screws"})
    check("wall gap 0 explained: the rim touches the head's face; inside the hole the radial gap is 0.3",
          "0.0000 mm — the hole rim touches Screws.Face" in t2 and "(plane -Z)" in t2
          and "inside the hole the radial gap is 0.3000 mm" in t2, t2)

    print("tapped holes: wall from the thread major Ø (round 3)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "CncPlate.holes[0]", "b": "CncPlate"})
    check("M5 tapped: 7.5 from the major Ø, 7.9 from the Ø4.2 tap drill",
          "minimum wall around the hole 7.5000 mm from the M5 major Ø (7.9000 mm from the Ø4.2 tap drill)" in t2, t2)
    check("distance check uses the major-Ø wall", line("cnc-wall-major", txt).startswith("  ✓"),
          line("cnc-wall-major", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Spacer.holes[0]", "b": "Spacer"})
    check("a plain bore is unchanged (no thread wording)", "2.9000 mm at" in t2 and "major" not in t2, t2)

    print("bosses selector (round 3)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Spacer.bosses"})
    check("turned spacer: one Ø16 boss, 24 long between the chamfers",
          "1 boss(es)" in t2 and "[0] Ø16.000 axis +Z, length 24.00" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Screws.bosses(d=3)"})
    check("screws: the two shanks", "2 boss(es)" in t2 and t2.count("Ø3.000 axis +Z, length 10.00") == 2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Screws.bosses(d=3)[0]"})
    check("hole ↔ boss: coaxial, radial gap 0.3", "COAXIAL ✓" in t2 and "minimum radial gap 0.3000 mm" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Motor.bosses"})
    check("a box has no bosses: the error says what there is", "no bosses match Motor.bosses" in t2
          and "none" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Screws.bosses(r=3)"})
    check("bosses(...) with an unknown key is an error", "takes d= and axis=" in t2, t2)
    for cid in ("shank-pos", "boss-gap", "boss-coax", "frame-point", "axis-object"):
        check("check %s passes" % cid, line(cid, txt).startswith("  ✓"), line(cid, txt))

    print("hole_diameter check (round 3)")
    check("nominal: Ø3.6 modelled = Ø3.4 + 0.2 compensation", line("printed-nominal", txt).startswith("  ✓")
          and "FcHoleComp 0.2" in line("printed-nominal", txt), line("printed-nominal", txt))
    check("as modelled Ø3.6 passes; as modelled Ø3.4 fails and points at nominal: true",
          line("printed-modelled", txt).startswith("  ✓") and line("printed-as-nominal", txt).startswith("  ✗")
          and "nominal: true" in line("printed-as-nominal", txt), line("printed-modelled", txt) + "\n" +
          line("printed-as-nominal", txt))
    check("tapped holes are checked at their tap drill", line("tap-drill", txt).startswith("  ✓"), line("tap-drill", txt))

    print("section (round 3)")
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"z": 15}, "objects": ["Housing", "Shaft"]})
    check("exact extents of a curved region: u 590.05…610.05, v -10.00…10.00",
          re.search(r"Shaft\s+area 314\.16 mm²\s+extent u 590\.05…610\.05\s+v -10\.00…10\.00", t2), t2)
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"y": 0}, "objects": ["Housing", "Shaft"]})
    check("a cut through the bore axis: the gap on each side of the axis (0.15 / 0.25)",
          "Housing ↔ Shaft gap 0.1500 mm" in t2 and "across the axis of Housing's Ø20.400 hole" in t2
          and "+X side 0.1500 mm, -X side 0.2500 mm" in t2, t2)
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"y": 15}, "objects": ["Bracket", "Screws"]})
    check("touching pair: 'nearest other gap' with the edges it is between",
          "Bracket ↔ Screws touching; nearest other gap 0.3000 mm: Bracket line (288.20, " in t2
          and "↔ Screws line (288.50, " in t2 and t2.count("+X side 0.3000 mm, -X side 0.3000 mm") == 2, t2)
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"x": 40}, "objects": ["CncPlate", "Stud"]})
    check("an unlisted thread overlap is still ✗ and says how to allow it", "✗ CncPlate ↔ Stud OVERLAP" in t2
          and "interference_ok" in t2, t2)

    print("hole ↔ shaft sign: an oversize shaft is an interference (round 4 #2)")
    for b in ("BigShaft.bosses[0]", "BigShaft.Face1"):
        err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Housing2.holes[0]", "b": b})
        check("Ø8.02 shaft (%s) in a Ø8 bore: radial gap -0.01 ✗ OVERLAP, not +0.01" % b,
              "hole Ø8.000 / shaft Ø8.020 → minimum radial gap -0.0100 mm  ✗ OVERLAP" in t2
              and "interference" in t2, t2)
    for cid in ("big-gap", "big-gap-face"):
        check("radial_gap %s fails at -0.010" % cid, re.search(r"✗ %s\s+radial_gap\s+-0\.010" % cid, txt),
              line(cid, txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Screws.Face4"})
    check("a convex face in a hole keeps the positive gap (Ø3 shank in Ø3.6: 0.3)",
          "minimum radial gap 0.3000 mm" in t2 and "OVERLAP" not in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Cbored.holes[0]", "b": "CapScrew.bosses(d=5.5)"})
    check("a head seated in the counterbore: gap to the Ø6.5 step it sits in (0.5), not an interference with Ø3.4",
          "hole Ø6.500 / shaft Ø5.500 → minimum radial gap 0.5000 mm" in t2 and "OVERLAP" not in t2, t2)
    check("…radial_gap check on the head passes at 0.5", re.search(r"✓ head-cbore\s+radial_gap\s+0\.500", txt),
          line("head-cbore", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Bracket.holes[0]", "b": "Screws.bosses(d=5.5)[0]"})
    check("a head on the face, beyond the hole's end: no radial fit and no OVERLAP",
          "beyond the hole's ends" in t2 and "OVERLAP" not in t2 and "radial gap" not in t2, t2)
    check("…radial_gap check on it is an error that says to select the shank",
          re.search(r"✗ head-face\s+radial_gap\s+error", txt) and "select the shank" in line("head-face", txt),
          line("head-face", txt))

    print("hole ↔ hole / boss ↔ boss near-coaxial (round 4 #42)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "StackA.holes[0]", "b": "StackB.holes[0]"})
    check("stacked Ø3.4 holes 0.3 apart: misalignment and a Ø3.1 clear passage, no OVERLAP / radial gap",
          "axes 0.3000 mm apart → clear passage through both Ø3.100" in t2 and "OVERLAP" not in t2
          and "radial gap" not in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "StackA.holes[0]", "b": "StackC.holes[0]"})
    check("holes of two parts 2 apart (beyond the radius): the Ø1.4 passage left, not '✗ the holes intersect'",
          "axes 2.0000 mm apart → clear passage through both Ø1.400 (2.0000 less than the smaller hole)" in t2
          and "intersect" not in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "PinA.bosses[0]", "b": "PinB.bosses[0]"})
    check("two near-coaxial pins: spacing only", "axis spacing 0.2000 mm" in t2 and "radial gap" not in t2
          and "OVERLAP" not in t2, t2)

    print("hole surfaces include cones; wall ends sampled (round 4 #3 #12 #31)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Csk.holes[0]", "b": "Csk"})
    check("countersink 5 from the edge: 0.5 wall at the top (the cylinder alone gives 2.75)",
          "minimum wall around the hole 0.5000 mm" in t2 and ", 6.000) →" in t2, t2)
    check("…and the distance check fails on it", re.search(r"✗ csk-wall\s+distance\s+0\.500", txt),
          line("csk-wall", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Drilled.holes[0]", "b": "Drilled.face(-Z)"})
    check("drill point: 0.197 of material under the tip (not 2.0 from the cylinder end)",
          "minimum wall around the hole 0.1974 mm" in t2 and "the floor under the blind end" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Drilled.holes[0]", "b": "Drilled"})
    check("hole ↔ its own part: the floor under the drill point (0.197) is the minimum wall, not the 7.0 side",
          "minimum wall around the hole 0.1974 mm at (1 310.0, 10.000, 0.197) → (1 310.0, 10.000, 0.000) (the floor "
          "under the blind end, along the axis from the drill point's tip)" in t2, t2)
    check("…the floor check fails on it", re.search(r"✗ floor\s+distance\s+0\.197", txt), line("floor", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Drilled.holes"})
    check("blind depth is the full-diameter depth 18, the drill point tip reported separately",
          "blind ↧18.00 (full-diameter depth; drill point tip +1.80) at (1 310.0, 10.00, 11.00)" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Chamfered.holes[0]", "b": "Chamfered"})
    check("edge chamfer next to the hole: 0.5 wall at the hole's top end (2.0 at mid-height)",
          "minimum wall around the hole 0.5000 mm" in t2 and ", 10.000) →" in t2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Csk.holes"})
    check("a countersunk through hole still spans the plate (0…6)", "THRU at (1 205.0, 10.00, 3.00)" in t2, t2)

    print("coaxial holes in separate walls (round 4 #13)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Clevis.holes"})
    check("clevis: one Ø8 hole per lug, each in its lug (not one in the air between them)",
          "2 hole(s)" in t2 and "[0] Ø8.000 axis +X THRU at (1 502.5, 10.00, 20.00)" in t2
          and "[1] Ø8.000 axis +X THRU at (1 537.5, 10.00, 20.00)" in t2, t2)
    check("hole_count / positions see both", line("clevis-n", txt).startswith("  ✓")
          and line("clevis-pos", txt).startswith("  ✓"), line("clevis-n", txt) + "\n" + line("clevis-pos", txt))
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Spacer.holes"})
    check("a chamfered bore is still one hole", "1 hole(s)" in t2, t2)

    print("section: radial gap per side despite an axial contact (round 4 #14)")
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"y": 0}, "objects": ["Cbored", "CapScrew"]})
    check("screw head seated on the counterbore floor: +X/−X radial gap 0.2 (shank), axial contact named",
          "+X side 0.2000 mm, -X side 0.2000 mm (radial gaps; axial contact at (" in t2 and "touching," not in t2, t2)

    print("nearest other gap ignores the contact interface (round 4 #33)")
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["TapBlock", "Clamp"], "min_clearance": 1.0,
                                      "contact_ok": [["TapBlock", "Clamp"]]})
    check("tapped block under a Ø5.5 clamp: no 0.65 pseudo-gap between the hole rims → all OK",
          "all OK ✓" in t2 and "0.6500" not in t2, t2)
    check("…and the clearance check passes", line("tap-clamp", txt).startswith("  ✓"), line("tap-clamp", txt))
    err, t2, _ = c.tool("section", {"project": "fixa", "plane": {"y": 0}, "objects": ["TapBlock", "Clamp"]})
    check("section: touching, no nearest-other-gap along the contact line", "TapBlock ↔ Clamp touching" in t2
          and "nearest other gap" not in t2, t2)

    print("frames as axes (round 4 #38)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Motor2.frame(shaft)", "b": "Mount2.frame(bore)"})
    check("frame ↔ frame gives the +Z axes' angle and offset", "+Z axes at 0.000°, offset 0.0000 mm → COAXIAL ✓"
          in t2, t2)
    for cid in ("frames-coax", "frames-par", "frames-perp", "frames-align"):
        check("check %s passes" % cid, line(cid, txt).startswith("  ✓"), line(cid, txt))

    print("holes(d=) on compensated holes: design Ø only (round 4 #32)")
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Printed2.holes(d=3.6)"})
    check("d=3.6 picks the 2 pins (design Ø3.6, modelled 3.8), not the M3 holes modelled at 3.6",
          "2 hole(s)" in t2 and t2.count("matched by its design Ø3.6 (modelled Ø3.800") == 2, t2)
    err, t2, _ = c.tool("measure", {"project": "fixa", "a": "Printed2.holes(d=3.8)"})
    check("the modelled Ø of a compensated hole no longer matches; the error says to use the design Ø",
          "no holes match" in t2 and "design Ø3.6 + 0.2 compensation" in t2, t2)
    for cid in ("pins-n", "pins-pos", "m3-design"):
        check("check %s passes" % cid, line(cid, txt).startswith("  ✓"), line(cid, txt))

    print("malformed checks report an error, the build goes on (round 4 #39)")
    check("flat point list: error naming the expected shape", "even for one point: [[30, 20, 0]]"
          in line("bad-points", txt), line("bad-points", txt))
    check("null d / radius: error naming the field and an example", "'d' must be a number — got null" in
          line("bad-d", txt) and "'radius' must be a number — got null" in line("bad-radius", txt)
          and '"radius": 3' in line("bad-radius", txt), line("bad-d", txt) + "\n" + line("bad-radius", txt))
    check("missing field: named, with an example", "missing field 'points'" in line("bad-missing", txt)
          and '"points": [[x, y, z]' in line("bad-missing", txt), line("bad-missing", txt))

    print("contact_ok / interference_ok names resolved like objects (round 4 #43)")
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["screws", "bracket"], "min_clearance": 0.3,
                                      "contact_ok": [["screws", "bracket"]]})
    check("lower-case labels in contact_ok match: all OK", "all OK ✓" in t2 and "not listed" not in t2, t2)
    err, t2, _ = c.tool("check_fit", {"project": "fixa", "objects": ["Screws", "Bracket"],
                                      "contact_ok": [["Screws", "Brackett"]]})
    check("an unknown name in contact_ok is an error listing the objects", "contact_ok" in t2
          and "no object 'Brackett'" in t2, t2)
    c.close()

    print("per-part density (callable dens)")
    code = r'''
import os, sys
lib = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "lib")
sys.path.append(lib)
sys.path.insert(0, sys.argv[1])
import FreeCAD as App, Part
import fclib as fc, analysis as an
doc = App.newDocument("dens")
fc._setup(doc)
for name, x in (("Alu", 0), ("Steel", 20)):
    o = doc.addObject("Part::Feature", name)
    o.Shape = Part.makeBox(10, 10, 10, App.Vector(x, 0, 0))
doc.recompute()
dens = {"Alu": 2.7, "Steel": 7.85}.get
lines, res = an.evaluate_checks(doc, [{"id": "alu", "type": "mass", "object": "Alu", "max_g": 3},
                                      {"id": "steel", "type": "mass", "object": "Steel", "min_g": 7.8, "max_g": 7.9}],
                                dens=dens)
print("\n".join(lines))
print(an.op_measure(doc, ["Steel"], dens=dens)[0])
lines, res = an.evaluate_checks(doc, [{"id": "none", "type": "mass", "object": "Alu", "max_g": 3}],
                                dens=lambda label: None)
print("\n".join(lines))
'''
    out = subprocess.run([FC_PY, "-I", "-c", code, os.path.join(ROOT, "server")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=120).stdout.decode()
    check("mass checks take the density per part", re.search(r"✓ alu\s+mass\s+2\.7 g", out)
          and re.search(r"✓ steel\s+mass\s+7\.8 g", out), out)
    check("measure reports the per-part mass", "mass 7.8 g (7.85 g/cm³)" in out, out)
    check("no density for a part: the error says where to set it", "parts.Alu.density_g_cm3" in out, out)

    print("snapshot ignores float noise; malformed check entries (round 4 #11 #39)")
    code = r"""
import os, sys
lib = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "lib")
sys.path.append(lib)
sys.path.insert(0, sys.argv[1])
import FreeCAD as App
import analysis as an
V, R, P = App.Vector, App.Rotation, App.Placement
Q = App.Units.Quantity
a = P(V(10, 0, 5), R(V(0, 0, 1), 90))
b = P(V(10 + 1e-12, -1e-15, 5), R(V(1e-13, 0, 1), 90 + 1e-12))
print("PL", an._pstr(a) == an._pstr(b), an._pstr(a))
print("ID", an._pstr(P()) == an._pstr(P(V(), R(V(0, 0, 1), 1e-14))), an._pstr(P()))
print("REAL", an._pstr(a) != an._pstr(P(V(10, 0, 5), R(V(0, 0, 1), 90.01))),
      an._pstr(a) != an._pstr(P(V(10.001, 0, 5), R(V(0, 0, 1), 90))))
print("VEC", an._pstr(V(1e-17, 2, 3)) == an._pstr(V(0, 2.0000000000001, 3)), an._pstr(V(-1e-17, 2, 3)))
print("QTY", an._pstr(Q(5.000000000000001, App.Units.Length)) == an._pstr(Q(5, App.Units.Length)))
print("FLIP", an._pstr(R(V(1, 2, 3), 180 + 1e-9)) == an._pstr(R(V(1, 2, 3), 180 - 1e-9)),
      an._pstr(R(V(1, 2, 3), 180 + 1e-9)))
def snap(pl, ln):
    return {"params": {}, "objects": {"Body": {"type": "PartDesign::Body", "label": "Body", "expr": {},
                                               "props": {"Placement": pl, "Length": ln}}}}
c = P(V(10.123456789, 0, 5), R(V(0.3, 0.2, 1), 33.3333333))
legacy = snap(str(c), "10.000000000000002 mm")                 # a snapshot written before values were rounded
print("LEGACY", an.diff_snapshots(legacy, snap(an._pstr(c), an._pstr(Q(10, App.Units.Length))))[0] == [],
      len(an.diff_snapshots(legacy, snap(an._pstr(P(V(10.2, 0, 5), c.Rotation)), "10.0 mm"))[0]) == 1)
doc = App.newDocument("chk")
lines, res = an.evaluate_checks(doc, ["oops", {"id": "x", "type": "bbox", "object": None}])
print("\n".join(lines))
"""
    out = subprocess.run([FC_PY, "-I", "-c", code, os.path.join(ROOT, "server")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=120).stdout.decode()
    check("a mated placement's float noise is not a change", "PL True" in out and "ID True" in out, out)
    check("…a real change still is (0.01°, 0.001 mm)", "REAL True True" in out, out)
    check("vectors and quantities rounded too", "VEC True" in out and "QTY True" in out, out)
    check("a half-turn whose quaternion w flips sign with noise is one rotation", "FLIP True" in out, out)
    check("a snapshot from before the rounding (plain str of a Placement) still compares equal; a real move "
          "still shows", "LEGACY True True" in out, out)
    check("a non-object check entry and a null object are errors, not a crash",
          "each check is an object" in out and re.search(r"✗ x\s+bbox\s+error", out), out)

    print("check_fit pair_min, section interference_ok / contact_ok (analysis API)")
    code = r'''
import os, sys
lib = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "lib")
sys.path.append(lib)
sys.path.insert(0, sys.argv[1])
import FreeCAD as App
import analysis as an
doc = App.openDocument(sys.argv[2])
fit = lambda **kw: print("\n".join(an.op_check_fit(doc, **kw)[0]))
fit(objects=["Screws", "Bracket"], min_clearance=0.35, contact_ok=[["Screws", "Bracket"]],
    pair_min={"Bracket|Screws": 0.3})
print("=====")
fit(objects=["Screws", "Bracket"], min_clearance=0.2, contact_ok=[["Screws", "Bracket"]],
    pair_min={"Screws|Bracket": 0.4})
print("=====")
fit(objects=["Housing", "Shaft"], min_clearance=0.2, pair_min={"Housing|Gusset": 0.1})
print("=====")
try:
    fit(objects=["Housing", "Shaft"], pair_min={"Housing": 0.1})
except an.RefError as e:
    print("ERR", e)
print("=====")
print("\n".join(an.op_section(doc, {"x": 40}, ["CncPlate", "Stud"], interference_ok=[["Stud", "CncPlate"]])[0]))
print("=====")
print("\n".join(an.op_section(doc, {"y": 15}, ["Bracket", "Screws"], contact_ok=[["Screws", "Bracket"]])[0]))
'''
    out = subprocess.run([FC_PY, "-I", "-c", code, os.path.join(ROOT, "server"),
                          os.path.join(proj, "build", "fixa.FCStd")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=180).stdout.decode()
    parts = out.split("=====")
    check("pair_min below the general minimum passes that pair, and the header says so", len(parts) == 6
          and "all OK ✓" in parts[0] and "(pair_min: Screws ↔ Bracket ≥ 0.300)" in parts[0], out)
    check("pair_min above the gap fails it, naming pair_min", len(parts) == 6 and "1 problem(s) ✗" in parts[1]
          and "nearest other gap 0.3000 mm < required 0.400 (pair_min)" in parts[1], out)
    check("pair_min for a pair not being checked: a note, the general minimum applies", len(parts) == 6
          and "pair_min Housing ↔ Gusset not used" in parts[2] and "← < required 0.200" in parts[2], out)
    check("a malformed pair_min key is an error with an example", len(parts) == 6 and "ERR" in parts[3]
          and "A|B" in parts[3], out)
    check("section: a listed thread overlap is allowed (✓, not ✗ OVERLAP)", len(parts) == 6
          and "✓ CncPlate ↔ Stud overlap" in parts[4] and "allowed" in parts[4] and "✗" not in parts[4], out)
    check("section: a listed contact is marked expected", len(parts) == 6
          and "Bracket ↔ Screws touching (expected contact); nearest other gap 0.3000 mm" in parts[5], out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
