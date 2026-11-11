#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""fclib features beyond pad / pocket / holes: fc.faces, fc.shell, fc.draft,
fc.linear_pattern, fc.polar_pattern, fc.mirror (holes keep their spec on
every copy: fc.hole_centres, build report counts, detected-hole matching),
fc.fasteners (ISO 4762 / 10642 / 7380 screws, ISO 4032 nuts, ISO 7089
washers as reference parts) and lookup_standard(fastener=…).

The standards checks run on any Python; the rest needs real FreeCAD.

    python3 tests/test_features.py
"""
import json
import math
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import FC_PY, ROOT, Client, check, fails, write  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "server"))
import standards  # noqa: E402

# ------------------------------------------------------------ no FreeCAD
print("standards.fastener")
f = standards.fastener("ISO4762 M3x10")
check("ISO 4762 M3×10: dk 5.5, k 3, socket 2.5, l 10", (f["dk"], f["k"], f["s"], f["l"], f["kind"]) ==
      (5.5, 3.0, 2.5, 10.0, "screw") and f["spec"] == "ISO 4762 M3×10", f)
f = standards.fastener("din 912 m4 × 11")
check("DIN 912 read as ISO 4762, ×/case/spaces accepted, odd length noted",
      f["standard"] == "ISO 4762" and any("DIN 912" in n for n in f["notes"])
      and any("not a preferred length" in n for n in f["notes"]), f)
f = standards.fastener("ISO10642 M4x12")
check("ISO 10642: 90° head consistent with dk and k ((dk − d) / 2 = k)", abs((f["dk"] - f["d"]) / 2 - f["k"]) < 0.01,
      f)
check("ISO 4032 M3 nut and ISO 7089 M3 washer", standards.fastener("ISO4032 M3")["s"] == 5.5
      and standards.fastener("ISO7089 M3")["d2"] == 7.0)
for bad, why in (("ISO4762 M3", "needs a length"), ("ISO4032 M3x5", "takes no length"),
                 ("ISO1234 M3", "unknown fastener standard"), ("ISO10642 M2x8", "no ISO 10642 data"),
                 ("ISO10642 M3x1", "must exceed the head height"), ("bolt", "must read like")):
    try:
        standards.fastener(bad)
        check("%r is refused" % bad, False)
    except ValueError as e:
        check("%r is refused (%s)" % (bad, why), why in str(e), str(e))
rep = standards.fastener_report("ISO7380 M3x8")
check("fastener_report names the dimensions and the fc call", "dk 5.7 × k 1.65" in rep and "fc.fasteners(" in rep, rep)

if not os.path.isfile(FC_PY) or "FreeCAD" not in FC_PY:
    print("\nFreeCAD not found — the FreeCAD checks were skipped (%d failure(s) so far)" % len(fails))
    sys.exit(1 if fails else 2)


def project(name, model, proj=None):
    d = os.path.join(tmp, name)
    write(os.path.join(d, "model.py"), model)
    write(os.path.join(d, "project.json"), json.dumps(dict({"name": name}, **(proj or {"process": "cnc"})), indent=1))
    write(os.path.join(d, "params.json"), "{}")
    return d


def result(txt):
    m = re.search(r"^result\n((?:  .*\n?)+)", txt, re.M)
    return json.loads(m.group(1)) if m else {}


def probe(c, name, code):
    err, txt, _ = c.tool("run_script", {"project": name, "doc": "build/%s.FCStd" % name, "code": code})
    return err, txt, result(txt)


# matches every detected hole of a body to its Hole feature, as reports, drawings and checks do
HOLE_MATCH = '''import analysis as an
b = fc.doc().getObjectsByLabel(%r)[0]
hs = an.detect_holes(an.global_shape(b))
names = []
for h in hs:
    f = an._feature_for(b, h)
    names.append(f["feature"].Label if f else None)
cent = []
for o in b.Group:
    if o.TypeId == "PartDesign::Hole":
        cent += [[round(v.x, 3), round(v.y, 3), round(v.z, 3)] for v in fc.hole_centres(o)]
fc.result(n=len(hs), names=names, centres=sorted(cent), at=sorted([round(h["point"].x, 3), round(h["point"].y, 3)]
                                                                    for h in hs))
'''

tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-feat-"))
try:
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    # ------------------------------------------------------------- shell
    print("fc.shell")
    project("shell_in", '''b = fc.body("Box")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40)), 30)
fc.shell(b, fc.face(b, "+Z"), 2, join="intersection")
''')
    err, txt, _ = c.tool("build", {"project": "shell_in", "views": []})
    check("inward shell builds", not err and txt.startswith("BUILD ✓") and "Shell ✓" in txt, txt)
    err, txt, r = probe(c, "shell_in", '''s = fc.doc().getObjectsByLabel("Box")[0].Shape
bb = s.BoundBox
fc.result(v=s.Volume, bb=[bb.XLength, bb.YLength, bb.ZLength])''')
    check("…keeps the outside 60×40×30, walls 2 thick (volume exact)", [round(x, 3) for x in r.get("bb", [])] ==
          [60, 40, 30] and abs(r.get("v", 0) - (60 * 40 * 30 - 56 * 36 * 28)) < 1e-3, txt)
    project("shell_out", '''b = fc.body("Box")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40)), 30)
fc.shell(b, fc.face(b, "+Z"), 2, inward=False, join="intersection")
''')
    err, txt, _ = c.tool("build", {"project": "shell_out", "views": []})
    err2, txt2, r = probe(c, "shell_out", '''bb = fc.doc().getObjectsByLabel("Box")[0].Shape.BoundBox
fc.result(bb=[bb.XLength, bb.YLength, bb.ZLength])''')
    check("outward shell keeps the inside: 64×44×32 outside", not err and [round(x, 3) for x in r.get("bb", [])] ==
          [64, 44, 32], (txt, txt2))
    project("shell_two", '''b = fc.body("Tube")
fc.pad(b, fc.sketch(b, "XY", fc.rect(30, 30)), 50)
fc.shell(b, fc.faces(b, facing="+Z") + fc.face(b, "-Z"), 3, join="intersection")
''')
    err, txt, _ = c.tool("build", {"project": "shell_two", "views": []})
    err2, txt2, r = probe(c, "shell_two", 'fc.result(v=fc.doc().getObjectsByLabel("Tube")[0].Shape.Volume)')
    check("two open faces combined with + → a square tube", not err and abs(r.get("v", 0) - (30 * 30 - 24 * 24) * 50)
          < 1e-3, (txt, txt2))
    project("shell_bad", '''b = fc.body("Box")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40)), 30)
fc.shell(b, [], 2)
''')
    err, txt, _ = c.tool("build", {"project": "shell_bad", "views": []})
    check("shell without faces is a readable error", "select faces with fc.face" in txt, txt)

    # ------------------------------------------------------------- draft
    print("fc.draft")
    project("draft", '''b = fc.body("Boss")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30)), 20)
fc.draft(b, fc.faces(b, parallel="Z"), 3)
c = fc.body("Pin")
fc.pad(c, fc.sketch(c, "XY", fc.circle(d=20, at=(60, 0))), 15)
fc.draft(c, fc.faces(c, parallel="Z", kind="cylinder"), 2, name="PinDraft")
''')
    err, txt, _ = c.tool("build", {"project": "draft", "views": []})
    check("draft builds on planar walls and on a cylinder", not err and txt.startswith("BUILD ✓")
          and "Draft ✓" in txt and "PinDraft ✓" in txt and "wall(s) open" not in txt, txt)
    err, txt, r = probe(c, "draft", '''import math
out = {}
for lab, ang in (("Boss", 3), ("Pin", 2)):
    s = fc.doc().getObjectsByLabel(lab)[0].Shape
    tilt = []
    for f in s.Faces:
        n = fc._face_normal(f)
        if abs(n.z) < 0.999:
            tilt.append(round(n.z, 5))
    top = max((f for f in s.Faces if f.Surface.__class__.__name__ == "Plane"), key=lambda f: f.CenterOfMass.z)
    bot = min((f for f in s.Faces if f.Surface.__class__.__name__ == "Plane"), key=lambda f: f.CenterOfMass.z)
    out[lab] = {"tilt": sorted(set(tilt)), "want": round(math.sin(math.radians(ang)), 5),
                "top": top.Area, "bottom": bot.Area}
fc.result(**out)''')
    ok = all(r.get(k, {}).get("tilt") == [r.get(k, {}).get("want")] for k in ("Boss", "Pin"))
    check("…every wall leans towards the pull (n·Z = +sin angle)", ok, txt)
    check("…the neutral (bottom) face keeps its size, the top shrinks", abs(r.get("Boss", {}).get("bottom", 0) - 1200)
          < 1e-3 and r["Boss"]["top"] < 1200 and r["Pin"]["top"] < r["Pin"]["bottom"], txt)
    project("draft_bad", '''b = fc.body("Boss")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30)), 20)
fc.draft(b, fc.faces(b, parallel="Z"), 50)
''')
    err, txt, _ = c.tool("build", {"project": "draft_bad", "views": []})
    check("a 50° draft is refused with typical values", "between 0 and 45°" in txt, txt)

    # ---------------------------------------------------------- patterns
    print("fc.linear_pattern / fc.polar_pattern / fc.mirror")
    project("linpat", '''b = fc.body("Rail")
fc.pad(b, fc.sketch(b, "XY", fc.rect(100, 30)), 6)
h = fc.holes(b, fc.face(b, "+Z"), [(-40, 0)], "M3 tapped", name="Tap")
fc.linear_pattern(b, h, "+X", 5, 20)
''')
    err, txt, _ = c.tool("build", {"project": "linpat", "views": []})
    check("linear pattern of a tapped hole builds; report counts all 5", not err and txt.startswith("BUILD ✓")
          and "LinearPattern ✓" in txt and "Tap ✓ (5× M3x0.5-6H" in txt, txt)
    err, txt, r = probe(c, "linpat", HOLE_MATCH % "Rail")
    check("…5 holes detected, each matched to the Hole feature (keeps M3 tapped)", r.get("n") == 5
          and r.get("names") == ["Tap"] * 5, txt)
    check("…hole_centres at x = −40 … 40 step 20", [p[0] for p in r.get("centres", [])] == [-40, -20, 0, 20, 40], txt)

    project("linpat_neg", '''b = fc.body("Rail")
fc.pad(b, fc.sketch(b, "XY", fc.rect(100, 30)), 6)
h = fc.holes(b, fc.face(b, "+Z"), [(40, 0)], "Ø4", name="Hole")
fc.linear_pattern(b, h, "-X", 3, 25)
''')
    err, txt, _ = c.tool("build", {"project": "linpat_neg", "views": []})
    err2, txt2, r = probe(c, "linpat_neg", HOLE_MATCH % "Rail")
    check("pattern along −X: copies at 40, 15, −10 and all matched", not err and r.get("names") == ["Hole"] * 3
          and [p[0] for p in r.get("centres", [])] == [-10, 15, 40], (txt, txt2))

    project("polpat", '''b = fc.body("Flange")
fc.pad(b, fc.sketch(b, "XY", fc.circle(d=60)), 8)
h = fc.holes(b, fc.face(b, "+Z"), [(20, 0)], "M4 clearance", name="Bolt")
fc.polar_pattern(b, h, 6)
c = fc.body("Off")
fc.pad(c, fc.sketch(c, "XY", fc.circle(d=40, at=(100, 0))), 5)
k = fc.holes(c, fc.face(c, "+Z"), [(112, 0)], "Ø3", name="OffHole")
fc.polar_pattern(c, k, 3, angle=180, center=(100, 0), name="Half")
''')
    err, txt, _ = c.tool("build", {"project": "polpat", "views": []})
    check("polar patterns build (about Z and about an off-centre axis)", not err and txt.startswith("BUILD ✓")
          and "Bolt ✓ (6×" in txt and "OffHole ✓ (3×" in txt, txt)
    err, txt, r = probe(c, "polpat", HOLE_MATCH % "Flange")
    angs = sorted(round(math.degrees(math.atan2(p[1], p[0])) % 360, 3) for p in r.get("at", []))
    check("…6 holes every 60° on r = 20, all matched to Bolt", r.get("names") == ["Bolt"] * 6
          and angs == [0, 60, 120, 180, 240, 300], (txt, angs))
    err, txt, r = probe(c, "polpat", HOLE_MATCH % "Off")
    pts = sorted((round(x - 100, 3), round(y, 3)) for x, y in r.get("at", []))
    check("…angle=180 with 3 copies: 0°, 90°, 180° around (100, 0)", r.get("names") == ["OffHole"] * 3
          and pts == sorted([(12.0, 0.0), (0.0, 12.0), (-12.0, 0.0)]), (txt, pts))

    project("mirror", '''b = fc.body("Sym")
fc.pad(b, fc.sketch(b, "XY", fc.rect(20, 20)), 5)
boss = fc.pad(b, fc.sketch(b, fc.face(b, "+Z"), fc.circle(d=6, at=(7, 0))), 4, name="Boss")
h = fc.holes(b, fc.face(b, "-Z"), [(-6, 6)], "M2.5 tapped", depth=3, name="Blind")
fc.mirror(b, boss, "YZ")
fc.mirror(b, h, "XZ", at=2, name="Off")
''')
    err, txt, _ = c.tool("build", {"project": "mirror", "views": []})
    check("mirrors build (origin plane, offset plane)", not err and txt.startswith("BUILD ✓") and "Off ✓" in txt
          and "Blind ✓ (2×" in txt, txt)
    err, txt, r = probe(c, "mirror", HOLE_MATCH % "Sym" + '''
s = b.Shape
fc.result(n=len(hs), names=names, centres=sorted(cent), bb=[s.BoundBox.XMin, s.BoundBox.XMax],
          bosses=sorted(round(f.Surface.Center.x, 3) for f in s.Faces if f.Surface.__class__.__name__ == "Cylinder"
                        and abs(f.Surface.Radius - 3) < 1e-6))''')
    check("…boss mirrored to x = −7", r.get("bosses") == [-7.0, 7.0], txt)
    check("…blind tapped hole mirrored across y = 2 to (−6, −2), matched", r.get("names") == ["Blind"] * 2
          and sorted((p[0], p[1]) for p in r.get("centres", [])) == [(-6.0, -2.0), (-6.0, 6.0)], txt)

    project("patpat", '''b = fc.body("Grid")
fc.pad(b, fc.sketch(b, "XY", fc.rect(80, 50)), 4)
h = fc.holes(b, fc.face(b, "+Z"), [(-30, -15)], "Ø3", name="Vent")
row = fc.linear_pattern(b, h, "+X", 3, 30, name="Row")
fc.linear_pattern(b, row, "+Y", 2, 30, name="Rows")
''')
    err, txt, _ = c.tool("build", {"project": "patpat", "views": []})
    err2, txt2, r = probe(c, "patpat", HOLE_MATCH % "Grid")
    check("pattern of a pattern: 6 holes, 6 centres, all matched", not err and r.get("n") == 6
          and len(r.get("centres", [])) == 6 and r.get("names") == ["Vent"] * 6, (txt, txt2))

    project("pat_bad", '''b = fc.body("Rail")
fc.pad(b, fc.sketch(b, "XY", fc.rect(100, 30)), 6)
h = fc.holes(b, fc.face(b, "+Z"), [(-40, 0)], "Ø3")
fc.linear_pattern(b, h, "+X", 1, 20)
''')
    err, txt, _ = c.tool("build", {"project": "pat_bad", "views": []})
    check("n = 1 is refused", "whole number ≥ 2" in txt, txt)

    # --------------------------------------------------------- fasteners
    print("fc.fasteners")
    PTS = "[(-15, -10), (15, -10), (15, 10), (-15, 10)]"
    project("hardware", '''P = %s
b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30)), 6)
h = fc.holes(b, fc.face(b, "+Z"), P, "M3 clearance normal + cbore ISO4762", name="Cb")
depth = h.HoleCutDepth.Value
fc.fasteners("ISO4762 M3x16", fc.face(b, "+Z"), P, offset=-depth, name="Screws")
fc.fasteners("ISO7089 M3", fc.face(b, "-Z"), P, name="Washers")
fc.fasteners("ISO4032 M3", fc.face(b, "-Z"), P, offset=0.5, name="Nuts")
''' % PTS)
    err, txt, _ = c.tool("build", {"project": "hardware", "views": []})
    check("screws in counterbores, washers and nuts underneath: builds", not err and txt.startswith("BUILD ✓"), txt)
    check("…each a bought reference part with qty and the order text",
          re.search(r"Screws\s+ref .*\[buy ×4: ISO 4762 M3×16\]", txt)
          and re.search(r"Nuts\s+ref .*\[buy ×4: ISO 4032 M3\]", txt)
          and re.search(r"Washers\s+ref .*\[buy ×4: ISO 7089 M3\]", txt), txt)
    err, txt, _ = c.tool("check_fit", {"project": "hardware", "contact_ok": [["Plate", "Screws"], ["Plate", "Washers"],
                                                                              ["Washers", "Nuts"], ["Screws", "Nuts"]]})
    check("…check_fit: no interference anywhere", not err and "INTERFERENCE" not in txt and "✗" not in txt, txt)
    err, txt, r = probe(c, "hardware", '''import json, math
o = fc.doc().getObjectsByLabel("Screws")[0]
s = o.Shape
one = math.pi * 2.75 ** 2 * 3 - math.sqrt(3) / 2 * 2.5 ** 2 * 1.5 + math.pi * 1.5 ** 2 * 16
zs = sorted(round(v.Point.z, 3) for v in s.Vertexes)
fc.result(n=len(s.Solids), v=s.Volume, want=4 * one, top=round(s.BoundBox.ZMax, 3), low=round(s.BoundBox.ZMin, 3),
          frames=sorted(json.loads(o.FcFrames)), rec=json.loads(o.FcFastener))''')
    check("…4 screw solids with the ISO 4762 volume (head − socket + shank)", r.get("n") == 4
          and abs(r.get("v", 0) - r.get("want", 1)) < 1e-2, txt)
    check("…head seated on the counterbore floor (top = 6 − 3.4 + 3), shank 16 below it",
          r.get("top") == round(6 - 3.4 + 3, 3) and r.get("low") == round(6 - 3.4 - 16, 3), txt)
    check("…seat frames and the FcFastener record", r.get("frames") == ["seat0", "seat1", "seat2", "seat3"]
          and r.get("rec", {}).get("count") == 4 and r["rec"].get("l") == 16, txt)

    project("csk", '''b = fc.body("Lid")
fc.pad(b, fc.sketch(b, "XY", fc.rect(30, 30)), 5)
fc.holes(b, fc.face(b, "+Z"), [(0, 0)], "M4 clearance + csk ISO10642", name="Csk")
fc.fasteners("ISO10642 M4x12", fc.face(b, "+Z"), [(0, 0)], name="Flat")
fc.fasteners("ISO7380 M3x8", "XY", [Placement(Vector(30, 0, 0), Rotation())], name="Button")
''')
    err, txt, _ = c.tool("build", {"project": "csk", "views": []})
    err2, txt2, r = probe(c, "csk", '''s = fc.doc().getObjectsByLabel("Flat")[0].Shape
u = fc.doc().getObjectsByLabel("Button")[0].Shape
fc.result(top=round(s.BoundBox.ZMax, 3), low=round(s.BoundBox.ZMin, 3), btop=round(u.BoundBox.ZMax, 3),
          bd=round(u.BoundBox.XLength, 3))''')
    bt = standards.fastener("ISO7380 M3x8")
    R = ((bt["dk"] / 2) ** 2 + bt["k"] ** 2) / (2 * bt["k"])            # the dome: k high over dk
    rim = round(bt["k"] - (R - math.sqrt(R * R - (bt["s"] / 2) ** 2)), 3)  # its height where the socket cuts it
    check("countersunk head flush with the face, l overall; button head from a Placement, its top where the socket "
          "cuts the dome (%g, under k = %g)" % (rim, bt["k"]), not err and r.get("top") == 5 and r.get("low") == -7
          and r.get("btop") == rim and r.get("bd") == 5.7, (txt, txt2))
    err, txt, _ = c.tool("check_fit", {"project": "csk", "objects": ["Lid", "Flat"], "contact_ok": [["Lid", "Flat"]]})
    check("…the countersunk screw does not interfere with the ISO 10642 countersink", not err
          and "INTERFERENCE" not in txt, txt)

    project("hw_bad", '''b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30)), 6)
fc.fasteners("ISO4762 M3", fc.face(b, "+Z"), [(0, 0)])
''')
    err, txt, _ = c.tool("build", {"project": "hw_bad", "views": []})
    check("a screw without a length is a readable build error", "needs a length" in txt, txt)

    err, txt, _ = c.tool("lookup_standard", {"fastener": "ISO4762 M5x20"})
    check("lookup_standard(fastener=…)", not err and "dk 8.5 × k 5" in txt and "socket s 4" in txt, txt)

    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
