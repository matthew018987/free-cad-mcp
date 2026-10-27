#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bent sheet metal: fc.sheet_metal / fc.sheet_flat and FcSheet (geometry checked
against numbers worked out by hand: folded size and volume, flat size = flat lengths
+ bend allowances, bend lines and holes in the flat pattern), the build report line,
check_manufacturing for bent parts, the flat-pattern DXF/SVG export (layers CUT and
BEND with a note per bend), the release README's bend table, the sheet scaffold and
rules, and the sheet_bracket eval case with scripted good and bad solutions — with
real FreeCAD.

    python3 tests/test_sheet_metal.py
"""
import glob
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import ROOT, Client, check, fails  # noqa: E402

RC = os.path.join(ROOT, "evals", "run_case.py")
_spec = importlib.util.spec_from_file_location("run_case", RC)
rcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rcm)

T, R, K = 2.0, 2.0, 0.33
OSSB = R + T                                    # outside setback of a 90° bend: (r + t)·tan 45°
BA = math.pi / 2 * (R + K * T)                  # bend allowance of a 90° bend
FLAT_X = 60 + 25 + 25 - 4 * OSSB + 2 * BA       # outside lengths − 2 setbacks per bend + BA per bend
HOLE_A = math.pi * 2.25 ** 2                    # M4 clearance normal: Ø4.5

U_MODEL = '''P = fc.params()
fc.sheet_metal("Bracket", P.thickness, fc.rect(P.width, P.depth),
               flanges=[{"edge": "+X", "length": P.flange_h}, {"edge": "-X", "length": P.flange_h, "up": P.second_up}],
               holes=[{"on": "base", "at": fc.rect_pattern(P.pitch_x, P.pitch_y), "spec": "M4 clearance normal"},
                      {"on": "flange1", "at": [(0, P.hole_z)], "spec": "M4 clearance normal"},
                      {"on": "flange2", "at": [(0, P.hole_z if P.second_up else P.thickness - P.hole_z)],
                       "spec": "M4 clearance normal"}],
               radius=P.bend_r)
'''
# the same bracket as a PartDesign extrusion of a U profile with sharp corners: not bent sheet metal
EXTRUDED_MODEL = '''P = fc.params()
b = fc.body("Bracket")
t, w, h = P.thickness, P.width / 2, P.flange_h
pts = [(-w, 0), (w, 0), (w, h), (w - t, h), (w - t, t), (-w + t, t), (-w + t, h), (-w, h)]
fc.pad(b, fc.sketch(b, "XZ", fc.polygon([(x, 0, z) for x, z in pts]), name="Profile"), P.depth, symmetric=True)
fc.holes(b, on=fc.face(b, "+Z", at=t), at=fc.rect_pattern(P.pitch_x, P.pitch_y), spec="M4 clearance normal")
'''


def params(**kw):
    return {k: ({"value": v, "unit": "mm", "source": "test"} if not isinstance(v, bool) else {"value": v})
            for k, v in kw.items()}


GOOD = dict(thickness=2, width=60, depth=40, flange_h=25, pitch_x=36, pitch_y=20, hole_z=15, bend_r=2, second_up=True)


def result(txt):
    """The fc.result(...) JSON block of a run_script reply."""
    m = re.search(r"^result\n((?:  .*\n?)+)", txt, re.M)
    return json.loads(m.group(1)) if m else {}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def near(a, b, tol=1e-4):
    return a is not None and b is not None and abs(a - b) <= tol


GEOMETRY = r'''import json, math
import analysis as an
out = {}
def summary(o):
    s = o.Shape
    bb = s.BoundBox
    return {"valid": s.isValid(), "solids": len(s.Solids), "volume": s.Volume,
            "bbox": [bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax], "sheet": json.loads(o.FcSheet)}
u = fc.sheet_metal("U", 2, fc.rect(60, 40), flanges=[{"edge": "+X", "length": 25}, {"edge": 3, "length": 25}],
                   holes=[{"on": "base", "at": fc.rect_pattern(36, 20), "spec": "M4 clearance normal"},
                          {"on": "flange1", "at": [(0, 15)], "spec": "M4 clearance normal"},
                          {"on": "flange2", "at": [(0, 15)], "spec": "Ø4.5"}])
out["U"] = summary(u)
face, lines = fc.sheet_flat(u)
fb = face.BoundBox
out["flat"] = {"valid": face.isValid(), "area": face.Area, "wires": len(face.Wires), "z": [fb.ZMin, fb.ZMax],
               "bbox": [fb.XMin, fb.YMin, fb.XMax, fb.YMax], "normal": list(face.normalAt(0, 0)),
               "holes": sorted([round(w.Edges[0].Curve.Center.x, 4), round(w.Edges[0].Curve.Center.y, 4),
                                round(w.Edges[0].Curve.Radius, 4)] for w in face.Wires[1:]),
               "lines": [[list(e.Vertexes[0].Point), list(e.Vertexes[-1].Point)] for e in lines]}
out["U_holes3d"] = sorted([round(h["at"].x, 3), round(h["at"].y, 3), round(h["at"].z, 3), h["axis_name"]]
                          for h in an.detect_holes(u.Shape))
out["Z"] = summary(fc.sheet_metal("Z", 2, fc.rect(60, 40), flanges=[{"edge": 1, "length": 25},
                                                                     {"edge": 3, "length": 25, "up": False}]))
out["A45"] = summary(fc.sheet_metal("A45", 2, fc.rect(60, 40), flanges=[{"edge": 1, "length": 25, "angle": 45}]))
out["lips"] = summary(fc.sheet_metal("Lips", 1.5, fc.rect(60, 40), k_factor=0.4,
                                     flanges=[{"edge": 1, "length": 25}, {"edge": 3, "length": 25},
                                              {"edge": "flange1", "length": 10}, {"edge": "flange2.end", "length": 10}]))
out["zz"] = summary(fc.sheet_metal("ZZ", 2, fc.rect(60, 40), flanges=[{"edge": 1, "length": 30},
                                                                       {"edge": "flange1", "length": 20, "up": False}]))
out["tab"] = summary(fc.sheet_metal("Tab", 2, fc.rect(60, 40), flanges=[{"edge": 1, "length": 20, "inset": [10, 10]}]))
out["tab_relief"] = summary(fc.sheet_metal("TabR", 2, fc.rect(60, 40),
                                           flanges=[{"edge": 1, "length": 20, "inset": [10, 10], "relief": 2}]))
out["cw"] = summary(fc.sheet_metal("CW", 2, [(-30, 20), (30, 20), (30, -20), (-30, -20)],
                                   flanges=[{"edge": 0, "length": 25, "inset": [5, 0]}]))
out["k5"] = summary(fc.sheet_metal("K5", 2, fc.rect(60, 40), k_factor=0.5, flanges=[{"edge": 1, "length": 25}]))
out["tray"] = summary(fc.sheet_metal("Tray", 1, fc.rect(60, 40), flanges=[
    {"edge": i, "length": 15, "radius": 1, "inset": [2.5, 2.5], "relief": 1} for i in range(4)]))
pl = fc.sheet_metal("Placed", 2, fc.rect(60, 40), flanges=[{"edge": 1, "length": 25}], placement=fc.frame(0, 0, 100))
doc.recompute()
out["placed_z"] = an.global_shape(pl).BoundBox.ZMin
errors = {}
cases = {
    "edge7": dict(flanges=[{"edge": 7, "length": 25}]),
    "edgeZ": dict(flanges=[{"edge": "+Z", "length": 25}]),
    "typo": dict(flanges=[{"edge": 1, "lenght": 25}]),
    "hem": dict(flanges=[{"edge": 1, "length": 25, "angle": 180}]),
    "tapped": dict(holes=[{"on": "base", "at": [(0, 0)], "spec": "M4 tapped"}]),
    "offface": dict(flanges=[{"edge": 1, "length": 25}], holes=[{"on": "flange1", "at": [(0, 2)], "spec": "Ø4"}]),
    "noface": dict(holes=[{"on": "flange3", "at": [(0, 2)], "spec": "Ø4"}]),
    "short": dict(flanges=[{"edge": 1, "length": 6}, {"edge": "flange1", "length": 10}]),
    "corner": dict(flanges=[{"edge": 0, "length": 25}, {"edge": 1, "length": 25}]),
    "collide": dict(flanges=[{"edge": 1, "length": 25}, {"edge": 3, "length": 25}, {"edge": "flange1", "length": 50},
                             {"edge": "flange2", "length": 50}]),
    "side": dict(flanges=[{"edge": 1, "length": 25}, {"edge": "flange1.edge1", "length": 10}]),
    "later": dict(flanges=[{"edge": "flange2", "length": 25}]),
}
for k, kw in cases.items():
    try:
        fc.sheet_metal("E", 2, fc.rect(60, 40), **kw)
        errors[k] = None
    except fc.FcError as e:
        errors[k] = str(e)
for k, base in (("rrect", fc.rrect(60, 40, 3)), ("repeat", [(0, 0), (10, 0), (10, 0), (0, 10)])):
    try:
        fc.sheet_metal("E", 2, base)
        errors[k] = None
    except fc.FcError as e:
        errors[k] = str(e)
out["errors"] = errors
fc.result(**out)
'''


def geometry_checks(c):
    print("fc.sheet_metal geometry against hand-worked numbers")
    err, txt, _ = c.tool("run_script", {"code": GEOMETRY}, timeout=600)
    g = result(txt)
    check("geometry script runs", not err and g, txt[-2000:])
    if not g:
        return
    u = g["U"]
    check("U bracket: one valid solid, 60 × 40 × 25 outside (base on z = 0)", u["valid"] and u["solids"] == 1
          and all(near(a, b) for a, b in zip(u["bbox"], [-30, 30, -20, 20, 0, 25])), u["bbox"])
    vol = (60 - 2 * OSSB) * 40 * T + 2 * (25 - OSSB) * 40 * T + 2 * (math.pi / 4) * ((R + T) ** 2 - R ** 2) * 40 \
        - 6 * HOLE_A * T
    check("…its volume is base + flanges + two quarter tubes − 6 holes = %.3f mm³" % vol, near(u["volume"], vol, 1e-3),
          u["volume"])
    s = u["sheet"]
    check("FcSheet has exactly the S1 keys", sorted(s) == ["bends", "flat_size", "k_factor", "radius", "spec",
                                                           "thickness"]
          and all(sorted(b) == ["angle", "length", "line", "n", "radius", "up"] for b in s["bends"]), sorted(s))
    check("FcSheet: t 2, default radius = thickness, K 0.33", s["thickness"] == 2 and s["radius"] == 2
          and s["k_factor"] == 0.33, s)
    check("flat size = 60 + 25 + 25 − 4·(r + t) + 2·BA = %.4f × 40" % FLAT_X,
          near(s["flat_size"][0], FLAT_X) and near(s["flat_size"][1], 40), s["flat_size"])
    b1, b2 = s["bends"]
    x2 = (25 - OSSB) + BA / 2                     # B2: flange2's flat, then half its bend allowance
    check("bend lines at the middle of each bend allowance: x = %.4f and %.4f, 40 long" % (FLAT_X - x2, x2),
          b1["n"] == 1 and b2["n"] == 2 and all(near(p[0], FLAT_X - x2) for p in b1["line"])
          and all(near(p[0], x2) for p in b2["line"]) and b1["length"] == 40 and b2["length"] == 40
          and b1["angle"] == 90 and b1["up"] is True and b1["radius"] == 2, s["bends"])
    check("FcSheet spec keeps the inputs (holes with their resolved Ø, flanges with defaults)",
          s["spec"]["holes"][0]["d"] == 4.5 and s["spec"]["flanges"][1]["edge"] == 3
          and s["spec"]["flanges"][0]["up"] is True and s["spec"]["base"][0] == [-30, -20], s["spec"])
    f = g["flat"]
    check("sheet_flat: a valid face at z = 0, facing +Z, corner at the origin, flat size, outline + 6 holes",
          f["valid"] and f["z"] == [0, 0] and near(f["normal"][2], 1) and near(f["bbox"][0], 0) and near(f["bbox"][1], 0)
          and near(f["bbox"][2], FLAT_X) and near(f["bbox"][3], 40) and f["wires"] == 7, f)
    check("…its area × t + (½ − K)·t² per radian·mm of bend = the folded volume",
          near(f["area"] * T + 2 * (math.pi / 2) * T * T * (0.5 - K) * 40, u["volume"], 1e-3), (f["area"], u["volume"]))
    base_x0 = (25 - OSSB) + BA                    # where the base's flat part starts in the flat pattern
    want = sorted([[round(base_x0 + 8, 4), y, 2.25] for y in (10, 30)] + [[round(base_x0 + 44, 4), y, 2.25]
                                                                           for y in (10, 30)]
                  + [[round(FLAT_X - 10, 4), 20, 2.25], [10, 20, 2.25]])
    check("holes in the flat pattern: base holes 8 mm past the base's tangent line, flange holes 10 mm from the "
          "flange ends (25 − 15)", all(all(near(a, b, 1e-3) for a, b in zip(p, q)) for p, q in zip(f["holes"], want))
          and len(f["holes"]) == 6, (f["holes"], want))
    check("sheet_flat's bend edges are FcSheet's bend lines", all(
        near(e[0][0], b["line"][0][0]) and near(e[0][1], b["line"][0][1]) and near(e[1][1], b["line"][1][1])
        for e, b in zip(f["lines"], s["bends"])), f["lines"])
    h3 = g["U_holes3d"]
    check("holes in the folded part: 4 along Z in the base, the flange holes along X at y 0, z 15",
          len([h for h in h3 if h[3].endswith("Z")]) == 4 and len([h for h in h3 if h[3].endswith("X")]) == 2
          and all(near(h[1], 0, 1e-3) and near(h[2], 15, 1e-3) and near(abs(h[0]), 29, 1e-3)
                  for h in h3 if h[3].endswith("X")), h3)
    z = g["Z"]
    check("Z (second flange down): z from −23 to 25, same flat size", z["valid"] and z["solids"] == 1
          and near(z["bbox"][4], -23) and near(z["bbox"][5], 25) and near(z["sheet"]["flat_size"][0], FLAT_X)
          and z["sheet"]["bends"][1]["up"] is False, z["bbox"])
    a = g["A45"]
    o45, ba45 = (R + T) * math.tan(math.radians(22.5)), math.pi / 4 * (R + K * T)
    check("45° flange: setback (r + t)·tan 22.5°, flat 60 − s + BA + (25 − s), tip at x = 30 + 25·cos 45°",
          a["valid"] and near(a["sheet"]["flat_size"][0], 60 - o45 + ba45 + 25 - o45)
          and near(a["bbox"][1], 30 + 25 * math.cos(math.pi / 4))
          and near(a["bbox"][5], 25 * math.sin(math.pi / 4) + T * math.cos(math.pi / 4)), (a["bbox"], a["sheet"]))
    lp = g["lips"]
    oss, bal = 1.5 + 1.5, math.pi / 2 * (1.5 + 0.4 * 1.5)
    check("U channel with lips (flange on flange, K 0.4): flat = 130 − 8·(r + t) + 4·BA, lips inside the channel",
          lp["valid"] and lp["solids"] == 1 and near(lp["sheet"]["flat_size"][0], 130 - 8 * oss + 4 * bal)
          and all(near(x, y) for x, y in zip(lp["bbox"], [-30, 30, -20, 20, 0, 25])), (lp["bbox"], lp["sheet"]["flat_size"]))
    zz = g["zz"]
    check("Z-bend on a flange: the second flange's length runs from the outside of its bend (the first flange's "
          "inner face, x = 28) → tip at x = 48, top at z = 30", zz["valid"] and near(zz["bbox"][1], 48)
          and near(zz["bbox"][5], 30) and near(zz["sheet"]["flat_size"][0], 60 + 30 + 20 - 4 * OSSB + 2 * BA), zz["bbox"])
    tab, tr = g["tab"], g["tab_relief"]
    check("a tab (inset 10 each side): bend line 20 long from y 10 to 30 in the flat",
          tab["valid"] and tab["sheet"]["bends"][0]["length"] == 20
          and sorted(p[1] for p in tab["sheet"]["bends"][0]["line"]) == [10, 30], tab["sheet"]["bends"])
    check("relief=2 cuts two 2 × (setback + 1) slots: 2·2·5·t = 40 mm³ less", near(tab["volume"] - tr["volume"], 40, 1e-3)
          and near(tab["sheet"]["flat_size"][0], tr["sheet"]["flat_size"][0]), (tab["volume"], tr["volume"]))
    cw = g["cw"]
    check("clockwise outline: edge 0 is still the user's first edge (+Y), insets from its first point",
          cw["valid"] and near(cw["bbox"][3], 20) and near(cw["sheet"]["flat_size"][1], 40 - OSSB + BA + 25 - OSSB)
          and sorted(round(p[0], 3) for p in cw["sheet"]["bends"][0]["line"]) == [5, 60], cw["sheet"]["bends"])
    check("K 0.5 lengthens the flat by π/2·(0.5 − 0.33)·t", near(g["k5"]["sheet"]["flat_size"][0] - (60 + 25 - 2 * OSSB + BA),
                                                              math.pi / 2 * 0.17 * T), g["k5"]["sheet"]["flat_size"])
    tray = g["tray"]
    check("tray: four inset flanges with reliefs fold into one valid solid (corner islands fall out)",
          tray["valid"] and tray["solids"] == 1 and len(tray["sheet"]["bends"]) == 4, tray)
    check("placement moves the folded part, not its flat pattern", near(g["placed_z"], 100), g["placed_z"])
    e = g["errors"]
    want_err = {"edge7": ["base edge 7 does not exist", "1 +X 40 mm"], "edgeZ": ["'+Z'", "'+X' / '-X'"],
                "typo": ["unknown key(s) lenght"], "hem": ["hems are not supported"],
                "tapped": ["clearance or plain spec"],
                "offface": ["not on the flat part of flange1", "(y, z) on a plane facing −X", "z 4…25"],
                "noface": ["no such face", "base"], "short": ["leaves no flat part between its bends", "take 8"],
                "corner": ["overlaps itself", "inset ≥", "relief"], "collide": ["collide when folded",
                                                                                 "flange3 and flange4"],
                "side": ["only a flange's far edge"], "later": ["no flange2 before it"],
                "rrect": ["round corners are not supported"], "repeat": ["repeats a point"]}
    for k, subs in want_err.items():
        check("error %s says what to do" % k, e.get(k) and all(x in e[k] for x in subs), e.get(k))


def make_project(home, name, model, prm, process="sheet", qty=25, material="5052-H32 aluminium", extra=None):
    d = os.path.join(home, name)
    write(os.path.join(d, "model.py"), model)
    write(os.path.join(d, "params.json"), json.dumps(params(**prm), indent=1))
    write(os.path.join(d, "checks.json"), "[]")
    pj = {"name": name, "units": "mm", "process": process, "material": {"name": material, "density_g_cm3": 2.68},
          "parts": {"Bracket": dict({"process": process, "material": material, "qty": qty}, **(extra or {}))}}
    write(os.path.join(d, "project.json"), json.dumps(pj, indent=1))
    return d


def server_checks(c, home):
    print("build report, check_manufacturing, export, release")
    make_project(home, "ub", U_MODEL, GOOD)
    err, txt, _ = c.tool("build", {"project": "ub", "views": []})
    check("build: the object line says 'Sheet … sheet t=2, 2 bends'", not err and re.search(
        r"Bracket\s+Sheet\s+solid ✓ valid ✓ .*bbox 60\.0 × 40\.0 × 25\.0  sheet t=2, 2 bends", txt), txt)
    check("…and the flat pattern, K and each bend on the next line",
          "flat pattern 102.36 × 40 mm · K 0.33 · B1 90° up R2 · B2 90° up R2" in txt and " 0 warnings" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "ub"})
    check("check_manufacturing: a bent part is ready (not 'not a uniform plate') with its flat size and bends",
          not err and "Bracket — sheet: ready" in txt and "flat pattern 102.36 × 40 mm" in txt
          and "B1 90° UP R2" in txt and "uniform" not in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "ub", "process": "laser"})
    check("…as a laser part it is blocked: flat blanks only", "laser: not manufacturable" in txt
          and "set its process to \"sheet\"" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "ub", "process": "fdm"})
    check("…printed, it is warned that it was built as sheet metal", "built as bent sheet metal" in txt, txt)

    bad = '''fc.sheet_metal("Bracket", 2, fc.rect(60, 40),
               flanges=[{"edge": 1, "length": 7, "radius": 1}, {"edge": 0, "length": 25, "inset": [12, 12]},
                        {"edge": 2, "length": 25, "inset": [12, 12], "relief": 2},
                        {"edge": 3, "length": 11}, {"edge": "flange4", "length": 9}],
               holes=[{"on": "base", "at": [(24, 0)], "spec": "Ø4"}, {"on": "base", "at": [(0, 0)], "spec": "Ø1.5"},
                      {"on": "base", "at": [(-23, 17.5)], "spec": "Ø3"}])
'''
    make_project(home, "badsheet", bad, {})
    err, txt, _ = c.tool("build", {"project": "badsheet", "views": []})
    err, txt, _ = c.tool("check_manufacturing", {"project": "badsheet"})
    check("DFM warns: bend radius below t", "B1 inside radius R1 is below the thickness 2" in txt, txt)
    check("…a flange with under 2t flat between its two bends (flange4: 11 − 2 × 4 = 3 mm); a 7 mm flange with R1 is "
          "just 3t + r and passes", "flange4 leaves 3 mm flat between its bends" in txt and "flange1 is" not in txt
          and "flange5 is" not in txt, txt)
    check("…a bend that ends inside its edge without relief (and not the one with relief)",
          "flange2's bend B2 ends inside its edge" in txt and "flange3's bend" not in txt, txt)
    check("…a hole too close to a bend line (2t + r) and a hole smaller than t",
          re.search(r"1× hole Ø4 on the base: its edge is [\d.]+ mm from bend line B1 \(keep ≥ 2t \+ r = 5\)", txt)
          and "1× hole Ø1.5 on the base is smaller than the sheet thickness 2" in txt, txt)
    check("…and a hole edge closer than t to the outline (beside a relief)",
          "1× hole Ø3 on the base: its edge is 1 mm from the outline (keep ≥ 1–1.5 × t = 2)" in txt
          and txt.count("from the outline") == 1, txt)
    tamper = U_MODEL + '''o = doc.getObjectsByLabel("Bracket")[0]
o.Shape = o.Shape.cut(Part.makeBox(4, 4, 4, App.Vector(-31, -21, -1)))
'''
    make_project(home, "tamper", tamper, GOOD)
    c.tool("build", {"project": "tamper", "views": []})
    err, txt, _ = c.tool("check_manufacturing", {"project": "tamper"})
    check("a solid changed after fc.sheet_metal is blocked: its flat pattern would not make it",
          "sheet: not manufacturable" in txt and "changed after fc.sheet_metal" in txt, txt)
    make_project(home, "extruded", EXTRUDED_MODEL, GOOD)
    c.tool("build", {"project": "extruded", "views": []})
    err, txt, _ = c.tool("check_manufacturing", {"project": "extruded"})
    check("an extruded U profile with process sheet is blocked and pointed at fc.sheet_metal",
          "not a uniform plate" in txt and "fc.sheet_metal" in txt, txt)

    out = os.path.join(home, "ub", "out", "export")
    err, txt, _ = c.tool("export", {"project": "ub", "formats": ["dxf", "svg", "step"]})
    check("export: dxf and svg are the flat pattern (verified), step the folded part", not err
          and "all verified ✓" in txt and "flat pattern 102.36 × 40 mm from 2 mm sheet, 2 bends" in txt
          and "bend lines with notes on layer BEND" in txt and "folded volume = flat area × t ✓" in txt, txt)
    ents = rcm.dxf_entities(open(os.path.join(out, "Bracket.dxf")).read())
    layer = lambda e: (e.get("8") or ["?"])[0]  # noqa: E731
    cut = [e for e in ents if layer(e) == "CUT"]
    bend = [e for e in ents if layer(e) == "BEND"]
    circles, box = rcm.dxf_profile(cut)
    check("DXF layer CUT: the outline (102.357 × 40, corner at 0,0) and 6 Ø4.5 holes",
          box and near(box[0], 0) and near(box[1], 0) and near(box[2], FLAT_X, 1e-5) and near(box[3], 40)
          and len(circles) == 6 and all(near(r, 2.25) for _, _, r in circles), (box, circles))
    lines = [e for e in bend if e["type"] == "LINE"]
    texts = [e["1"][0] for e in bend if e["type"] == "TEXT"]
    check("DXF layer BEND: two 40 mm bend lines where FcSheet has them, with a note each",
          len(lines) == 2 and sorted(round(float(e["10"][0]), 4) for e in lines) == sorted(
              [round((25 - OSSB) + BA / 2, 4), round(FLAT_X - (25 - OSSB) - BA / 2, 4)])
          and sorted(texts) == ["B1 90%%d UP R2", "B2 90%%d UP R2"], (lines, texts))
    raw = open(os.path.join(out, "Bracket.dxf")).read()
    check("DXF declares its layers (CUT, BEND dashed red) and mm units", "$INSUNITS\n70\n4" in raw
          and "LAYER\n2\nBEND\n70\n0\n62\n1\n6\nDASHED" in raw and "LAYER\n2\nCUT" in raw, raw[:600])
    svg_chk = rcm.check(home, {}, {"type": "svg_matches_dxf", "glob": "ub/out/export/*.svg"}, {})
    svg = open(os.path.join(out, "Bracket.svg")).read()
    check("SVG: the same holes as the DXF, not mirrored, and the bend lines dashed red with notes",
          svg_chk[0] and svg.count("<circle") == 6 and 'stroke="red"' in svg and ">B1 90° UP R2</text>" in svg, svg_chk)
    err, txt, _ = c.tool("run_script", {"code": '''import Import
d = App.newDocument("Dxf")
Import.readDXF(%r, d.Name)
fc.result(layers=sorted(o.Label for o in d.Objects if o.Name.startswith("Layer") and o.Label != "Layers"),
          shapes=len([o for o in d.Objects if o.TypeId == "Part::Feature"]))''' % os.path.join(out, "Bracket.dxf")})
    rd = result(txt)
    check("FreeCAD's own DXF importer reads it: layers 0, BEND, CUT; 12 shapes (4 lines, 6 circles, 2 bend lines)",
          rd.get("layers") == ["0", "BEND", "CUT"] and rd.get("shapes") == 12, txt)

    err, txt, _ = c.tool("release", {"project": "ub", "drawings": "none"})
    check("release: sheet part ships the flat-pattern DXF and the folded STEP", not err
          and "Bracket.dxf, Bracket.step" in txt, txt)
    z = sorted(glob.glob(os.path.join(home, "ub", "release", "*.zip")))[-1]
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        readme = zf.read(next(n for n in names if n.endswith("README.md"))).decode()
        bom = zf.read(next(n for n in names if n.endswith("BOM.csv"))).decode()
    check("README: thickness, flat size with K-factor and radius, layers", "Material thickness: 2 mm." in readme
          and "Flat pattern 102.36 × 40 mm in the DXF" in readme and "K-factor 0.33" in readme
          and "inside bend radius R2" in readme and "layer BEND" in readme, readme)
    check("README: a bend table (angle, direction, inside radius, bend line in DXF coordinates, length)",
          "| Bend | Angle | Direction | Inside radius | Bend line (DXF, mm) | Length |" in readme
          and "| B1 | 90° | UP | R2 | (79.27, 0) → (79.27, 40) | 40 |" in readme
          and "| B2 | 90° | UP | R2 | (23.09, 40) → (23.09, 0) | 40 |" in readme, readme)
    check("README: holes per face as specified", "4× Ø4.5 THRU (M4 clearance normal) on the base" in readme
          and "on flange1 (at B1)" in readme, readme)
    check("BOM unchanged: one make line, qty 25, sheet metal",
          bom.splitlines()[0].startswith("item,part,qty,make/buy,process,material")
          and ",Bracket,25,make,sheet metal,5052-H32 aluminium," in bom, bom)

    print("create_project sheet scaffold and the sheet rules")
    err, txt, _ = c.tool("create_project", {"name": "scaffold", "process": "sheet"})
    check("sheet scaffold: a bent L bracket made with fc.sheet_metal", not err and "bent L bracket" in txt
          and "fc.sheet_metal" in open(os.path.join(home, "scaffold", "model.py")).read(), txt)
    err, txt, _ = c.tool("build", {"project": "scaffold", "views": []})
    check("…builds clean: one bend, 0 warnings", not err and txt.startswith("BUILD ✓") and " 0 warnings" in txt
          and "sheet t=2, 1 bend" in txt, txt)
    err, txt, _ = c.tool("check_manufacturing", {"project": "scaffold"})
    check("…and is ready for the sheet-metal shop", "sheet: ready" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"process": "sheet"})
    check("lookup_standard(process='sheet'): bend radius, K-factor, flange length, hole-to-bend distance, relief",
          not err and "inside bend radius ≥ material thickness" in txt and "K-factor" in txt
          and "flange length (outside) ≥ 3 × t + r" in txt and "2 × t + r from a bend line" in txt
          and "relief" in txt and "fc.sheet_metal" in txt, txt)


def cli(ws, tool, args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "evals", "mcp_cli.py"), "--home", ws, tool,
                        json.dumps(args)], capture_output=True, text=True, timeout=900)
    return p.stdout


def grade(cid, ws):
    out = subprocess.run([sys.executable, RC, "grade", cid, ws], capture_output=True, text=True, timeout=1800).stdout
    return json.loads(out[out.find("{"):])


def sheet_solution(model, prm):
    """Scripted run of the sheet_bracket eval case through the harness."""
    ws = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp_sheet_"))
    subprocess.run([sys.executable, RC, "setup", "sheet_bracket", ws], capture_output=True, timeout=600)
    cli(ws, "create_project", {"name": "sheet_bracket", "template": "part", "process": "sheet",
                               "material": "5052-H32 aluminium"})
    pd = os.path.join(ws, "sheet_bracket")
    write(os.path.join(pd, "model.py"), model)
    write(os.path.join(pd, "params.json"), json.dumps(params(**prm), indent=1))
    pj = json.load(open(os.path.join(pd, "project.json")))
    label = list(pj["parts"])[0]
    pj["parts"]["Bracket"] = dict(pj["parts"].pop(label), qty=25)
    write(os.path.join(pd, "project.json"), json.dumps(pj, indent=2))
    b = cli(ws, "build", {"project": "sheet_bracket", "views": []})
    m = cli(ws, "check_manufacturing", {"project": "sheet_bracket"})
    r = cli(ws, "release", {"project": "sheet_bracket", "drawings": "none"})
    g = grade("sheet_bracket", ws)
    shutil.rmtree(ws, ignore_errors=True)
    return g, b, m, r


def eval_checks():
    print("eval case sheet_bracket: scripted solutions through the harness")
    g, b, m, r = sheet_solution(U_MODEL, GOOD)
    bad = [x for x in g["checks"] if not x["ok"]]
    check("a U bracket from fc.sheet_metal, released for the sheet-metal shop, grades 100 %% (%d checks)" % g["total"],
          not bad and g["total"] >= 10, (bad, b[-800:], m[-400:], r[-800:]))
    g, b, m, r = sheet_solution(U_MODEL, dict(GOOD, second_up=False))
    failed = sorted(set(x["type"] for x in g["checks"] if not x["ok"]))
    check("the second flange bent down (a Z, 60 × 40 × 48) fails the size and the bend check, nothing else",
          failed == ["geometry", "object_bbox"], (failed, g["checks"]))
    g, b, m, r = sheet_solution(EXTRUDED_MODEL, GOOD)
    failed = sorted(set(x["type"] for x in g["checks"] if not x["ok"]))
    check("an extruded U profile (no bends) fails the sheet checks, the manufacturability check and the release",
          {"geometry", "cli_regex", "zip_contains", "dxf_layers", "zip_text"} <= set(failed)
          and "object_bbox" not in failed and "RELEASE STOPPED" in r, (failed, r[-600:]))


def main():
    home = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp_sm_"))
    c = Client(home)
    try:
        c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "1"}})
        geometry_checks(c)
        server_checks(c, home)
    finally:
        c.close()
        shutil.rmtree(home, ignore_errors=True)
    eval_checks()
    print("\n%s" % ("all checks passed" if not fails else "%d failure(s): %s" % (len(fails), fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
