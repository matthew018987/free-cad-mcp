#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drawings (drawing_worker.py, drawing_support.py): real dimension
values on read, create ops + FcSource, verified edit dimensions (no Y mirror,
face and view-vertex refs, outside placement, Radius, remove cleanup, note
size), scale strings and paper planning, notes (units, chamfer vs break edges,
print compensation, wrapping), turned parts, drawing numbers, sheet_status,
export and FCBak cleanup; pockets, slots, steps and cut-outs (size, location,
depth, corner radii, patterns), turned parts in SECTION A-A (bores, fits,
threads, depths; section auto/full/none) and bent sheet metal (bend table,
flat pattern view) — with real FreeCAD (the drawing ops run the FreeCAD GUI
in the background; they are serialised, so this takes a few minutes).

    python3 tests/test_drawings.py
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import ROOT, Client, check, fails, images  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "server"))
import drawing_support as ds  # noqa: E402  (stdlib-only part)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def project(name, model, proj, params):
    d = os.path.join(tmp, name)
    write(os.path.join(d, "model.py"), model)
    write(os.path.join(d, "project.json"), json.dumps(dict({"name": name, "units": "mm"}, **proj), indent=1))
    write(os.path.join(d, "params.json"), json.dumps({k: {"value": v, "unit": "mm", "source": "test"}
                                                      for k, v in params.items()}, indent=1))
    return d


def sha1(p):
    with open(p, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def dims_of(txt):
    """drawing read lines → [(name, type, value text, x, y, refs)]."""
    return re.findall(r"dim\s+(\S+)\s+(\S+)\s+(.+?)\s+at \((-?[\d.]+), (-?[\d.]+)\)\s+refs (\S+)", txt)


PLATE = """P = fc.params()
plate = fc.body("Plate")
fc.pad(plate, fc.sketch(plate, "XY", fc.rect(P.width, P.depth), name="PlateSketch"), P.thickness, name="Base")
fc.holes(plate, on=fc.face(plate, "+Z"), at=[(-30, -15), (30, -15), (-30, 15), (15, 15)],
         spec="M4 clearance normal", name="MountHoles")
"""
GUSSET = """P = fc.params()
body = fc.body("Gusset")
fc.pad(body, fc.sketch(body, "XY", fc.polygon([(0, 0), (P.leg, 0), (0, P.leg)]), name="Outline"), P.thickness,
       name="Plate")
fc.holes(body, on=fc.face(body, "+Z"), at=[(10, 10), (40, 10), (10, 40)], spec="Ø5.5", depth="through",
         name="BoltHoles")
"""
SPACER = """P = fc.params()
body = fc.body("Spacer")
fc.turned(body, [(P.od, P.length)], name="Turned")
fc.holes(body, on=fc.face(body, "+Z"), at=[(0, 0)], spec="Ø%g" % float(P.bore), depth="through", name="Bore")
fc.chamfer(body, fc.edges(body, kind="circle"), P.chamfer, name="Chamfers")
"""
PRINTED = """P = fc.params()
for name, comp in (("Comp", 0.2), ("Plain", 0.0)):
    b = fc.body(name)
    fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 20), name=name + "Sketch"), 4, name=name + "Base")
    fc.holes(b, on=fc.face(b, "+Z"), at=[(-10, 0), (10, 0)], spec="M3 clearance normal", name=name + "Holes",
             compensation=comp)
"""

SHAFT = """P = fc.params()
body = fc.body("Shaft")
fc.turned(body, [(16, 8), (8, 10, "k6"), (6.5, 2), (8, 14)], name="Profile")
fc.external_thread(body, "M8", length=14, z_from=20, name="ThreadM8")
fc.chamfer(body, fc.edges(body, kind="circle", on=fc.face(body, "+Z")), 0.5, name="EndChamfer")
"""
FLANGE = """P = fc.params()
b = fc.body("Flange")
fc.pad(b, fc.sketch(b, "XY", fc.rrect(90, 50, 5), name="FlangeSketch"), 4, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=[(-38, -18), (38, -18), (-38, 18), (38, 18)], spec="Ø5.5", depth="through",
         name="Bolts")
fc.holes(b, on=fc.face(b, "+Z"), at=[(-10, 0)], spec="Ø20", depth="through", name="Bore")
"""
MOVER = """P = fc.params()
b = fc.body("Bracket")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40), name="BracketSketch"), 5, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=[(P.hx, 10), (-20, -10)], spec="Ø5.5", depth="through", name="Holes")
"""
TAPS = """P = fc.params()
b = fc.body("Block")
fc.pad(b, fc.sketch(b, "XY", fc.rect(100, 60), name="BlockSketch"), 20, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=[(-35, -20), (-35, 20)], spec="M4 tapped", depth=6, name="ShallowTaps")
fc.holes(b, on=fc.face(b, "+Z"), at=[(-15, -20), (-15, 20)], spec="M4 tapped", depth=12, name="DeepTaps")
fc.holes(b, on=fc.face(b, "+Z"), at=[(15, 0)], spec="Ø6.6", depth="through", name="Thru")
fc.holes(b, on=fc.face(b, "+Z"), at=[(35, 0)], spec="Ø6.6", depth=5, name="Blind")
"""
PIN = """P = fc.params()
b = fc.body("Pin")
fc.turned(b, [(10, 10), (20, 5), (10, 10)], name="Profile")
fc.chamfer(b, fc.edges(b, kind="circle", r=10), 1.5, name="BigChamfer")
fc.chamfer(b, fc.edges(b, kind="circle", at=0) + fc.edges(b, kind="circle", at=25), 0.5, name="EndChamfers")
"""
RAIL = """P = fc.params()
b = fc.body("Rail")
fc.pad(b, fc.sketch(b, "XY", fc.rect(130, 30), name="RailSketch"), 6, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=[(x, 0) for x in range(-50, 51, 20)], spec="Ø5.5", depth="through",
         name="Holes")
"""
DISC = """P = fc.params()
b = fc.body("Disc")
fc.pad(b, fc.sketch(b, "XY", fc.circle(d=100), name="DiscSketch"), 5, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=fc.bolt_circle(80, 4, start=45), spec="Ø6.6", depth="through", name="Bolts")
fc.holes(b, on=fc.face(b, "+Z"), at=[(0, 0)], spec="Ø30", depth="through", name="Bore")
"""
BRACKETS = """P = fc.params()
for name in ("MotorMountBracketLeft", "MotorMountBracketRight"):
    b = fc.body(name)
    fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30), name=name + "Sketch"), 4, name=name + "Base")
"""
PAIR = """P = fc.params()
for name, (w, h) in (("Small", (20, 20)), ("Big", (200, 120))):
    b = fc.body(name)
    fc.pad(b, fc.sketch(b, "XY", fc.rect(w, h), name=name + "Sketch"), 3, name=name + "Base")
"""
CENTRE = """P = fc.params()
b = fc.body("Mount")
fc.pad(b, fc.sketch(b, "XY", fc.rect(42.3, 42.3), name="MountSketch"), 4, name="Base")
fc.holes(b, on=fc.face(b, "+Z"), at=[(0, 0)], spec="Ø22.6", depth="through", name="Pilot")
"""
# D1 · pockets: the cnc_plate eval part (pocket position a parameter), a pattern of two, an open slot + a
# rebate + a sharp pocket (outer edges filleted underneath), a pocket under a rim chamfer, a laser cut-out
# and a pocket cut from the underside
POCKETS = """P = fc.params()
body = fc.body("AdapterPlate")
fc.pad(body, fc.sketch(body, "XY", fc.rect(100, 60), name="PlateSketch"), 8, name="Plate")
fc.holes(body, on="XY", offset=8, at=fc.rect_pattern(80, 40), spec="M5 tapped", depth="through", name="M5Tapped")
fc.pocket(body, fc.sketch(body, "XY", fc.rrect(40, 20, r=3, center=(P.px, 0)), name="PocketSketch", offset=8), 3,
          name="Pocket")
"""
TRAY = """P = fc.params()
body = fc.body("Tray")
fc.pad(body, fc.sketch(body, "XY", fc.rect(120, 60), name="TraySketch"), 10, name="Base")
fc.pocket(body, fc.sketch(body, "XY", fc.rrect(30, 20, r=4, center=(-30, 0)), fc.rrect(30, 20, r=4, center=(30, 0)),
                         name="PocketSketch", offset=10), 4, name="Pockets")
"""
SLOTS = """P = fc.params()
body = fc.body("Slotted")
fc.pad(body, fc.sketch(body, "XY", fc.rect(100, 60), name="Sk"), 12, name="Base")
fc.pocket(body, fc.sketch(body, "XY", fc.rect(40, 12, center=(-40, 0)), name="SlotSk", offset=12), 5, name="Slot")
fc.pocket(body, fc.sketch(body, "XY", fc.rect(110, 10, center=(0, 27)), name="StepSk", offset=12), 4, name="Step")
fc.pocket(body, fc.sketch(body, "XY", fc.rect(20, 16, center=(25, -5)), name="PocketSk", offset=12), 6, name="Pocket")
fc.fillet(body, fc.edges(body, kind="line", on=fc.face(body, "-Z")), 1, name="BottomFillet")
"""
CHAMFERED = """P = fc.params()
body = fc.body("Block")
fc.pad(body, fc.sketch(body, "XY", fc.rect(60, 40), name="Sk"), 30, name="Base")
fc.pocket(body, fc.sketch(body, "XY", fc.rrect(30, 20, r=5, center=(5, 0)), name="PocketSk", offset=30), 10,
          name="Pocket")
fc.chamfer(body, fc.edges(body, kind="line", on=fc.face(body, "+Z")), 1, name="TopChamfer")
"""
WINDOW = """P = fc.params()
body = fc.body("Frame")
fc.pad(body, fc.sketch(body, "XY", fc.rect(80, 50), name="Sk"), 3, name="Base")
fc.pocket(body, fc.sketch(body, "XY", fc.rrect(40, 20, r=2), name="WinSk", offset=3), "through", name="Window")
fc.holes(body, on="XY", offset=3, at=fc.rect_pattern(66, 36), spec="Ø4.5", depth="through", name="Holes")
"""
UNDER = """P = fc.params()
body = fc.body("Cover")
fc.pad(body, fc.sketch(body, "XY", fc.rect(80, 50), name="Sk"), 10, name="Base")
fc.pocket(body, fc.sketch(body, "XY", fc.rrect(60, 30, r=4), name="UnderSk"), 6, reverse=True, name="Under")
"""
# D2 · turned parts with internal features: a counterbored bush, a tapped + H7 seat adapter, a sleeve with an
# internal groove and chamfered bore mouths (Part API)
BUSH = """P = fc.params()
body = fc.body("Bush")
fc.turned(body, [(30, 30), (24, 10)], name="Profile")
fc.holes(body, on=fc.face(body, "+Z"), at=[(0, 0)], spec="Ø16", depth="through", name="Bore")
fc.holes(body, on=fc.face(body, "+Z"), at=[(0, 0)], spec="Ø20", depth=8, name="Counterbore")
"""
SEAT = """P = fc.params()
body = fc.body("Adapter")
fc.turned(body, [(30, 12), (24, 28, "h6")], name="Profile")
fc.holes(body, on=fc.face(body, "+Z"), at=[(0, 0)], spec="M10 tapped", depth=15, name="Tapped")
fc.holes(body, on=fc.face(body, "-Z"), at=[(0, 0)], spec="Ø12 H7", depth=10, name="Seat")
"""
SLEEVE = """P = fc.params()
import FreeCAD as App, Part
V = App.Vector
prof = [(10, 1), (11, 0), (19, 0), (20, 1), (20, 49), (19, 50), (11, 50), (10, 49), (10, 23), (12, 23), (12, 20),
        (10, 20)]
w = Part.makePolygon([V(x, 0, z) for x, z in prof] + [V(10, 0, 1)])
o = doc.addObject("Part::Feature", "Sleeve")
o.Shape = Part.makeSolid(Part.Face(w).revolve(V(0, 0, 0), V(0, 0, 1), 360)).removeSplitter()
"""
# D3 · sheet metal: an L-bracket with a hand-made FcSheet (no builder spec), and fc.sheet_metal's U-bracket
BENT = """P = fc.params()
import json, math
import FreeCAD as App, Part
V = App.Vector
t, r, k, W = 2.0, 2.0, 0.33, 60.0
yb, c, s45 = 40.0 - r - t, r + t, math.sqrt(0.5)
prof = Part.Face(Part.Wire([Part.LineSegment(V(0, 0, 0), V(0, yb, 0)).toShape(),
    Part.Arc(V(0, yb, 0), V(0, yb + (r + t) * s45, c - (r + t) * s45), V(0, yb + r + t, c)).toShape(),
    Part.LineSegment(V(0, yb + r + t, c), V(0, yb + r + t, 30)).toShape(),
    Part.LineSegment(V(0, yb + r + t, 30), V(0, yb + r, 30)).toShape(),
    Part.LineSegment(V(0, yb + r, 30), V(0, yb + r, c)).toShape(),
    Part.Arc(V(0, yb + r, c), V(0, yb + r * s45, c - r * s45), V(0, yb, t)).toShape(),
    Part.LineSegment(V(0, yb, t), V(0, 0, t)).toShape(), Part.LineSegment(V(0, 0, t), V(0, 0, 0)).toShape()]))
solid = prof.extrude(V(W, 0, 0))
for x in (15, 45):
    solid = solid.cut(Part.makeCylinder(2.75, 10, V(x, 15, -1), V(0, 0, 1)))
o = doc.addObject("Part::Feature", "Bracket")
o.Shape = solid
ba = math.pi / 180 * 90 * (r + k * t)
o.addProperty("App::PropertyString", "FcSheet", "fclib", "sheet metal (JSON)")
o.FcSheet = json.dumps({"thickness": t, "radius": r, "k_factor": k, "spec": {"hand-made": True},
    "bends": [{"n": 1, "angle": 90, "radius": r, "up": True, "length": W, "line": [[0, yb + ba / 2], [W, yb + ba / 2]]}],
    "flat_size": [W, yb + ba + 30 - r - t]})
"""
UBRACKET = """P = fc.params()
fc.sheet_metal("UBracket", 2, fc.rect(80, 40), flanges=[
    {"edge": 1, "length": 30, "angle": 90, "up": True}, {"edge": 3, "length": 30, "angle": 90, "up": False}],
    holes=[{"on": "base", "at": [(-25, 0), (25, 0)], "spec": "M5 clearance normal"}], radius=2, k_factor=P.k)
"""
HAS_SHEET_METAL = "def sheet_metal(" in open(os.path.join(ROOT, "server", "fclib.py")).read()


def has_dim(txt, text, **want):
    """drawing read lists a dimension showing text whose two anchors have these model coordinates
    (has_dim(t, "40", x=[-20, 20]): anchored at x = -20 and x = 20)."""
    for t, pts in anchors(txt, lambda s_: s_ == text):
        if len(pts) == 2 and all(sorted(round(p.get(k, 1e9), 3) for p in pts) == sorted(float(v) for v in vs)
                                 for k, vs in want.items()):
            return True
    return False


def callouts_of(txt):
    m = re.search(r"callouts\s+(.*)", txt)
    return [x.strip() for x in m.group(1).split(";")] if m else []


def _w_(x):
    """A number as a dimension shows it (2 decimals, no trailing zeros)."""
    return ("%.2f" % float(x)).rstrip("0").rstrip(".")


def anchors(txt, pred):
    """[(dim text, [{axis: value}, …])] of the read dimensions whose text matches pred."""
    out = []
    for line in txt.splitlines():
        m = re.match(r"\s*dim\s+\S+\s+\S+\s+(.+?)\s+at \(.*?anchors (.*)$", line)
        if m and pred(m.group(1).strip()):
            out.append((m.group(1).strip(), [dict((k, float(v)) for k, v in re.findall(r"(\w)=(-?[\d.]+)", a))
                                             for a in re.findall(r"\(([^)]*)\)", m.group(2))]))
    return out


def fc_source(fcstd, page_hint=None):
    """The FcSource JSON of the (first) page stored in an FCStd file, or {}."""
    import html
    import zipfile
    with zipfile.ZipFile(fcstd) as z:
        xml = z.read("Document.xml").decode("utf-8", "replace")
    m = re.search(r'<Property name="FcSource" type="App::PropertyString"[^>]*>\s*<String value="([^"]*)"', xml)
    return json.loads(html.unescape(m.group(1))) if m else {}


def doc_label(fcstd):
    """The document Label stored in an FCStd file."""
    import zipfile
    with zipfile.ZipFile(fcstd) as z:
        xml = z.read("Document.xml").decode("utf-8", "replace")
    m = re.search(r'<Property name="Label" type="App::PropertyString"[^>]*>\s*<String value="([^"]*)"', xml)
    return m.group(1) if m else None


def svg_text_origin(svg, needle):
    """(x, y) page mm of the start of the baseline of the SVG text line containing needle."""
    for m in re.finditer(r'transform="matrix\(1,0,0,1,(-?[\d.]+),(-?[\d.]+)\)">\s*<text[^>]*x="(-?[\d.]+)"[^>]*'
                         r'y="(-?[\d.]+)"[^>]*>([^<]*)</text>', svg):
        if needle in m.group(5):
            return (float(m.group(1)) + float(m.group(3))) / 10, (float(m.group(2)) + float(m.group(4))) / 10
    return None


tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixB-"))
try:
    print("drawing_support signatures (no FreeCAD)")
    a = [23618.3, 9772.06, 80.0, 50.0, 6.0, 10]
    check("same_signature: equal within 1e-6 relative", ds.same_signature(a, [23618.30001, 9772.06, 80, 50, 6, 10]))
    check("…a changed size or face count is different", not ds.same_signature(a, a[:4] + [8.0, 10])
          and not ds.same_signature(a, a[:5] + [11]) and not ds.same_signature(a, None))
    check("…signature_change names what changed", ds.signature_change(a, a[:4] + [8.0, 10]) == "z size 6 → 8 mm",
          ds.signature_change(a, a[:4] + [8.0, 10]))

    project("plate4", PLATE, {"process": "cnc", "material": {"name": "Aluminium 6061-T6", "density_g_cm3": 2.7},
                              "parts": {"Plate": {"process": "cnc", "qty": 1}}},
            {"width": 80, "depth": 50, "thickness": 6})
    project("gusset", GUSSET, {"process": "laser", "material": {"name": "5052-H32 aluminium"},
                               "parts": {"Gusset": {"process": "laser", "qty": 20}}},
            {"leg": 60, "thickness": 3})
    project("spacer", SPACER, {"process": "turning", "material": {"name": "303 stainless steel"},
                               "parts": {"Spacer": {"process": "turning", "qty": 10}}},
            {"od": 16, "length": 25, "bore": 8.2, "chamfer": 0.5})
    project("printed", PRINTED, {"process": "fdm", "material": {"name": "PETG"}}, {})
    project("shaft", SHAFT, {"process": "turning", "material": {"name": "1045 steel"},
                             "parts": {"Shaft": {"process": "turning", "qty": 5,
                                                 "material": "1045 steel (C45), as supplied, no heat treatment"}}}, {})
    project("flange", FLANGE, {"process": "laser", "material": {"name": "5052 aluminium"},
                               "parts": {"Flange": {"process": "laser", "qty": 4}}}, {})
    project("centre", CENTRE, {"process": "fdm", "material": {"name": "PETG"}}, {})
    project("mover", MOVER, {"process": "laser", "material": {"name": "5052 aluminium"}}, {"hx": 20})
    project("taps", TAPS, {"process": "cnc", "material": {"name": "6061-T6"}}, {})
    project("pin", PIN, {"process": "turning", "material": {"name": "303 stainless steel"}}, {})
    project("rail", RAIL, {"process": "cnc", "material": {"name": "6061-T6"}}, {})
    project("disc", DISC, {"process": "laser", "material": {"name": "5052 aluminium"}}, {})
    project("conveyor-v2", BRACKETS, {"process": "laser", "material": {"name": "5052 aluminium"}}, {})
    project("pair", PAIR, {"process": "laser", "material": {"name": "5052 aluminium"}}, {})
    cnc = {"process": "cnc", "material": {"name": "6061-T6"}}
    project("pockets", POCKETS, cnc, {"px": 0})
    project("tray", TRAY, cnc, {})
    project("slots", SLOTS, cnc, {})
    project("chamfered", CHAMFERED, cnc, {})
    project("window", WINDOW, {"process": "laser", "material": {"name": "5052 aluminium"}}, {})
    project("under", UNDER, cnc, {})
    turning = {"process": "turning", "material": {"name": "CuSn8 bronze"}}
    project("bush", BUSH, turning, {})
    project("seat", SEAT, turning, {})
    project("sleeve", SLEEVE, turning, {})
    project("bent", BENT, {"process": "sheet", "material": {"name": "5052-H32 aluminium"}}, {})
    if HAS_SHEET_METAL:
        project("ubracket", UBRACKET, {"process": "sheet", "material": {"name": "5052-H32 aluminium"}}, {"k": 0.33})
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    for p in ("plate4", "gusset", "spacer", "printed", "shaft", "flange", "centre", "mover", "taps", "pin", "rail",
              "disc", "conveyor-v2", "pair", "pockets", "tray", "slots", "chamfered", "window", "under", "bush", "seat",
              "sleeve", "bent") + (("ubracket", ) if HAS_SHEET_METAL else ()):
        err, txt, _ = c.tool("build", {"project": p, "views": []})
        check("%s builds" % p, not err and "BUILD ✓" in txt, txt[:600])

    print("create: ops, FcSource, notes, drawing number, FCBak")
    dd = os.path.join(tmp, "plate4", "drawings")
    write(os.path.join(dd, "Plate.FCStd"), "old sheet")              # replaced by the save; no backup left
    err, txt, content = c.tool("drawing", {"project": "plate4", "part": "Plate", "ops": [
        {"op": "add_note", "text": "QTY 2"}, {"op": "frobnicate"}]})
    check("plate drawing: A4 1:1, every dimension verified", not err and "(plate, A4 ISO 5457 landscape, scale 1:1)"
          in txt and "8/8 verified" in txt and "4× ⌀4.5 THRU" in txt, txt)
    check("…create ops applied; an unknown op fails alone and names the valid ops", "ops       1 of 2 applied" in txt
          and "✓ note" in txt and "unknown op 'frobnicate' — valid ops: add_dimension" in txt, txt)
    check("…one edge note for an unchamfered CNC part", "BREAK SHARP EDGES" in txt and "CHAMFERS" not in txt, txt)
    svg = open(os.path.join(tmp, "plate4", "out", "drawings", "Plate.svg")).read()
    check("…drawing number <project>-<PART>", ">plate4-PLATE<" in svg)
    check("…no FreeCAD backup file left next to the sheet", sorted(os.listdir(dd)) == ["Plate.FCStd"], os.listdir(dd))

    print("read: real values and scale, preview in the cache folder")
    err, txt, content = c.tool("drawing", {"action": "read", "project": "plate4", "doc": "drawings/Plate.FCStd"})
    ds_ = dims_of(txt)
    check("read: dimension values are the model's (not 0)", not err and len(ds_) >= 8
          and not any(v.strip() in ("0", "-") for _, _, v, _, _, _ in ds_)
          and any(t == "DistanceX" and v.strip() == "80" for _, t, v, _, _, _ in ds_), txt)
    check("…page scale reported", re.search(r"PAGE Plate sheet .* scale 1:1", txt), txt[:300])
    check("…one preview image, nothing written to out/drawings", len(images(content)) == 1
          and not [f for f in os.listdir(os.path.join(tmp, "plate4", "out", "drawings")) if "preview" in f])

    print("edit: verified dimensions (no mirroring), faces, view vertices, Radius, notes, remove")
    err, txt, content = c.tool("drawing", {"action": "edit", "project": "plate4", "doc": "drawings/Plate.FCStd", "ops": [
        {"op": "add_dimension", "view": "Top", "a": "Plate.holes(d=4.5)[0]", "b": "Plate.holes(d=4.5)[1]",
         "type": "DistanceX"},
        {"op": "add_dimension", "a": "Plate.holes(d=4.5)[1]", "b": "Plate.face(+Y)", "type": "DistanceY"},
        {"op": "add_dimension", "view": "Top", "a": "Plate.face(+X)", "b": "Plate.holes(d=4.5)[3]",
         "type": "DistanceX"},
        {"op": "add_dimension", "a": "Top.Vertex0", "b": "Top.Vertex3"},
        {"op": "add_dimension", "view": "Top", "a": "Plate.holes(d=4.5)[3]", "type": "Radius"},
        {"op": "add_dimension", "view": "Top", "a": [-30, -15, 6], "b": [15, 15, 6], "type": "DistanceX",
         "x": 0, "y": 40},
        {"op": "add_note", "text": "ANODISE BLACK"}]})
    check("edit: hole ↔ hole verified 60", not err and re.search(r"DistanceX Plate\.holes\(d=4\.5\)\[0\] ↔ "
                                                                  r"Plate\.holes\(d=4\.5\)\[1\] verified ✓ 60\.000", txt), txt)
    check("…hole at y=-15 ↔ face +Y is 40, not mirrored (10); the view is found from the hole axis",
          re.search(r"DistanceY Plate\.holes\(d=4\.5\)\[1\] ↔ Plate\.face\(\+Y\) verified ✓ 40\.000", txt), txt)
    check("…planar face ↔ hole", re.search(r"Plate\.face\(\+X\) ↔ Plate\.holes\(d=4\.5\)\[3\] verified ✓ 25\.000",
                                            txt), txt)
    check("…view vertices as drawing read prints them", re.search(r"Top\.Vertex0 ↔ Top\.Vertex3 verified ✓ 94\.340",
                                                                   txt), txt)
    check("…Radius on a hole is a radius", re.search(r"Radius verified ✓ R2\.250", txt), txt)
    m = re.search(r"verified ✓ 60\.000, at \((-?[\d.]+), (-?[\d.]+)\) mm from the Top view centre, outside", txt)
    check("…default placement below the outline (view half height 25 mm)", m and float(m.group(2)) < -25, txt)
    check("…explicit x/y kept and explained", "at (0, 40) — your x/y: paper mm from the Top view centre" in txt, txt)
    check("…note at the generated notes size, x/y documented", re.search(r"note \S+ at .* text centre in page mm.*"
                                                                           r"text 3 mm", txt), txt)
    r01 = os.path.join(dd, "Plate.r01.FCStd")
    check("…saved as a revision, exports named after it", os.path.isfile(r01) and os.path.isfile(
        os.path.join(tmp, "plate4", "out", "drawings", "Plate.r01.pdf")), os.listdir(dd))
    check("…title-block revision bumped with the edit number and said so", "revision r0001 → r0001.1" in txt
          and "revision_index=r0001.1" in txt, txt)
    check("…the document keeps its label (not renamed after the file)", doc_label(r01) == doc_label(
        os.path.join(dd, "Plate.FCStd")) and ".r01" not in (doc_label(r01) or ".r01"), (doc_label(r01),))
    face_dim = re.search(r"dimension (\S+) DistanceX Plate\.face\(\+X\)", txt)
    err, txt, _ = c.tool("drawing", {"action": "edit", "project": "plate4", "doc": "drawings/Plate.r01.FCStd",
                                     "ops": [{"op": "remove", "name": face_dim.group(1) if face_dim else "?"}]})
    check("remove: the marker vertex only that dimension used goes too", not err and "1 marker vertex(es)" in txt, txt)
    check("…second edit: revision r0001.1 → r0001.2", "revision r0001.1 → r0001.2" in txt, txt)
    kept = dims_of(txt)
    check("…the other dimensions still measure their values", any(v.strip() == "60" for _, _, v, _, _, _ in kept)
          and any(v.strip() == "40" for _, t, v, _, _, _ in kept if t == "DistanceY")
          and any(v.strip() == "94.34" for _, _, v, _, _, _ in kept), txt)
    err, txt, _ = c.tool("drawing", {"action": "edit", "project": "plate4", "doc": "drawings/Plate.r02.FCStd", "ops": [
        {"op": "add_dimension", "a": "Top.Vertex99", "b": "Top.Vertex3"}, {"op": "bogus"}]})
    check("failing edit ops: nothing saved, each failure says what to do", err and "nothing saved" in txt
          and "has no Vertex99" in txt and "valid ops:" in txt and not os.path.isfile(os.path.join(dd, "Plate.r03.FCStd")),
          txt)
    check("no FCBak files after edits", not [f for f in os.listdir(dd) if f.endswith(".FCBak")], os.listdir(dd))

    print("sheet_status and export (worker ops used by release)")
    import freecad_mcp as srv
    build = os.path.join(tmp, "plate4", "build", "plate4.FCStd")
    r02 = os.path.join(dd, "Plate.r02.FCStd")
    spec_hash = fc_source(r02).get("spec_hash")         # what drawing create recorded (the server's hash)
    st = srv.run_gui_worker("sheet_status", {"build": build, "sheets": [
        {"part": "Plate", "doc": r02, "spec_hash": spec_hash}, {"part": "Nope", "doc": r02}]})
    sh = st.get("sheets") or [{}, {}]
    check("sheet_status: current sheet, title-block revision, missing part", st.get("ok") and sh[0].get("current")
          is True and sh[0].get("revision") == "r0001.2" and sh[1].get("current") is False
          and "not in the build" in sh[1]["detail"], st)
    check("…and how many of its dimensions were verified (create + edits)", isinstance(sh[0].get("dims_verified"), int)
          and sh[0]["dims_verified"] >= 10, st)
    check("…and of how many (dims_total ≥ verified, for the release README)", isinstance(sh[0].get("dims_total"), int)
          and sh[0]["dims_total"] >= sh[0]["dims_verified"], st)
    before = sha1(r02)
    exd = os.path.join(tmp, "exp")
    ex = srv.run_gui_worker("export", {"doc": r02, "out_dir": exd, "name": "Plate_drawing",
                                       "title_fields": {"revision_index": "r0099"}})
    files = ex.get("files") or {}
    check("export: PDF/SVG/PNG from a copy with the title fields applied", ex.get("ok") and all(
        os.path.isfile(files.get(k, "")) for k in ("pdf", "svg", "png")) and "r0099" in open(files["svg"]).read(), ex)
    ex2 = srv.run_gui_worker("export", {"doc": r02, "out_dir": exd, "name": "Plate_asis"})
    check("…without title fields the sheet ships as is and its revision is reported", ex2.get("ok")
          and ex2.get("revision") == "r0001.2" and "revision r0001.2" in ex2.get("text", ""), ex2)
    check("…the sheet document is not modified", sha1(r02) == before)
    ex = srv.run_gui_worker("export", {"doc": r02, "out_dir": exd, "title_fields": {"colour": "red"}})
    check("…an unknown title field is an error listing the fields", not ex.get("ok") and "revision_index" in
          ex.get("error", ""), ex)
    pj = os.path.join(tmp, "plate4", "params.json")
    pr = json.load(open(pj))
    pr["thickness"]["value"] = 8
    write(pj, json.dumps(pr))
    err, txt, _ = c.tool("build", {"project": "plate4", "views": []})
    st = srv.run_gui_worker("sheet_status", {"build": build, "sheets": [{"part": "Plate", "doc": r02,
                                                                         "spec_hash": spec_hash}]})
    sh = (st.get("sheets") or [{}])[0]
    check("sheet_status: stale after the model changed, saying what changed", st.get("ok") and sh.get("current") is
          False and "z size 6 → 8" in sh.get("detail", ""), st)

    print("scale strings and paper planning (60 mm laser triangle)")
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "paper": "A4", "scale": "1:1",
                                     "notes": ["laser cut from 3 mm sheet"]})
    check("A4 at '1:1' fits", not err and "(plate, A4 ISO 5457 landscape, scale 1:1)" in txt
          and "7/7 verified" in txt, txt)
    check("…user note kept exactly as written (no upper-casing: 'mm' stays 'mm')", "laser cut from 3 mm sheet" in txt
          and "LASER CUT" not in txt, txt)
    gsvg = open(os.path.join(tmp, "gusset", "out", "drawings", "Gusset.svg")).read()
    check("…drawing number not doubled when project and part match", ">GUSSET<" in gsvg and "gusset-GUSSET" not in gsvg)
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "paper": "auto", "scale": "auto"})
    check("auto paper/scale: A4 at 1:1", not err and "(plate, A4 ISO 5457 landscape, scale 1:1)" in txt, txt)
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "scale": "1/2"})
    check("'1/2' → 1:2", not err and "scale 1:2)" in txt, txt)
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "paper": "A4", "scale": "5:1"})
    check("a forced paper/scale that does not fit names them and how to reset", err and "A4" in txt and "5:1" in txt
          and "drawings.json" in txt and 'scale="auto"' in txt, txt)
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "scale": "one to two"})
    check("an unreadable scale says which forms work", err and "'1:2'" in txt and "'auto'" in txt, txt)

    print("turned part")
    err, txt, _ = c.tool("drawing", {"project": "spacer", "part": "Spacer"})
    check("turned spacer: no position dimension for the coaxial bore", not err and "(turned," in txt
          and "could not place" not in txt and "⌀8.2 THRU" in txt, txt)
    check("…one edge note matching the model (chamfers)", "CHAMFERS 0.5 × 45°" in txt and "BREAK SHARP" not in txt, txt)
    views_line = re.search(r"views\s+(.*)", txt)
    check("…a requested view left out for lack of room is reported with the fix",
          views_line and ("iso" in views_line.group(1) or re.search(r"iso view left out: no room on A\d at \S+ — pass "
                                                                  r"paper='A\d'", txt)), txt)
    ssvg = open(os.path.join(tmp, "spacer", "out", "drawings", "Spacer.svg")).read()
    check("…drawing number SPACER (not spacer-SPACER)", ">SPACER<" in ssvg and "spacer-SPACER" not in ssvg)
    err, txt, _ = c.tool("drawing", {"action": "read", "project": "spacer", "doc": "drawings/Spacer.FCStd"})
    od = [d for d in dims_of(txt) if d[1] == "DistanceY" and d[2].strip().lstrip("⌀").startswith("16")]
    check("…the Ø16 text sits off the centreline", od and abs(float(od[0][4])) > 5, txt)
    check("D2 · the bore is shown in SECTION A-A (section auto): a section view in place of the side view, no hidden "
          "lines, its caption on the sheet", re.search(r"view\s+Section\s+DrawViewSection", txt)
          and not re.search(r"view\s+Side\b", txt) and "hidden lines" not in txt
          and re.search(r"note\s+\S+\s+SECTION A-A", txt), txt)
    check("…the bore's own ⌀8.2 in the section, verified", any(t == "DistanceY" and v.strip() == "⌀8.2"
                                                               for _, t, v, _, _, _ in dims_of(txt)), txt)
    err, txt, _ = c.tool("drawing", {"project": "spacer", "part": "Spacer", "section": "none"})
    check("…section='none' (passed through by the server and saved): the side view with hidden lines (the bore), "
          "the end view without", not err and "views     side, end" in txt
          and json.load(open(os.path.join(tmp, "spacer", "drawings.json")))["Spacer"].get("section") == "none", txt)
    err, txt, _ = c.tool("drawing", {"action": "read", "project": "spacer", "return_image": False})
    check("…read: hidden lines on Side, not on End", re.search(r"view\s+Side .*hidden lines", txt)
          and not re.search(r"view\s+End .*hidden lines", txt), txt)
    c.tool("drawing", {"project": "spacer", "part": "Spacer", "reset": True})

    print("printed parts: hole compensation note, wrapping")
    long_note = "support material only under the two mounting tabs, never inside the holes or the slot"
    err, txt, _ = c.tool("drawing", {"project": "printed", "part": "Comp", "notes": [long_note]})
    check("compensated FDM part: holes-modelled note", not err and "HOLES MODELLED +0.2 mm FOR PRINTING — PRINT AS "
          "MODELLED, NO FURTHER HOLE COMPENSATION" in txt, txt)
    err, txt, _ = c.tool("drawing", {"action": "read", "project": "printed", "doc": "drawings/Comp.FCStd"})
    notes = re.search(r"note\s+\S+_Notes\s+(.*?)\s+at \(", txt)
    check("…long note wrapped onto several lines", notes and "support material" in notes.group(1)
          and len([s for s in notes.group(1).split(" / ") if "tabs" in s or "slot" in s or "support" in s]) >= 2,
          txt)
    err, txt, _ = c.tool("drawing", {"project": "printed", "part": "Plain"})
    check("uncompensated FDM part: PRINT AS MODELLED only", not err and "PRINT AS MODELLED" in txt
          and "HOLES MODELLED" not in txt, txt)

    print("turned shaft: a dimension per step, fit and thread on their own steps")
    err, txt, _ = c.tool("drawing", {"project": "shaft", "part": "Shaft", "notes": ["seat k6: 8.001–8.010 (1:1, mm)"]})
    check("shaft drawn turned (process turning), every dimension verified", not err and "(turned," in txt
          and "process turning" in txt and re.search(r"dims\s+(\d+)/\1 verified", txt), txt)
    check("…callouts: the k6 fit with upper deviation first, and the thread", "⌀8 k6 (+0.010/+0.001)" in txt
          and "M8×1.25-6g" in txt, txt)
    check("…thread note without model coordinates", "THREAD M8×1.25-6g, LENGTH 14 (SEE VIEW)" in txt
          and "FROM Z=" not in txt, txt)
    check("…the user's note kept exactly ('k6' stays a shaft fit, 'mm' stays mm)",
          "seat k6: 8.001–8.010 (1:1, mm)" in txt and "K6" not in txt and "MM)" not in txt, txt)
    check("…a long material is shortened in the title block, reported, and kept whole in the notes",
          "shortened to fit the title block: part_material" in txt
          and "MATERIAL: 1045 steel (C45), as supplied, no heat treatment" in txt, txt)
    shsvg = open(os.path.join(tmp, "shaft", "out", "drawings", "Shaft.svg")).read()
    check("…the title-block material ends in '…'", re.search(r">1045 steel[^<]*…<", shsvg), "")
    err, txt, _ = c.tool("drawing", {"action": "read", "project": "shaft", "return_image": False})
    an_ = anchors(txt, lambda t: t.startswith("⌀") or t.startswith("M8"))

    def on_step(pred, lo, hi):
        hit = [a for t, a in an_ if pred(t)]
        return len(hit) == 1 and len(hit[0]) == 2 and all(lo - 1e-3 <= p.get("z", -1e9) <= hi + 1e-3 for p in hit[0])
    check("…the k6 dimension is anchored on the seat (z 8–18), not on the thread", on_step(lambda t: "k6" in t, 8, 18),
          txt)
    check("…the thread callout is anchored on the thread (z 20–34)", on_step(lambda t: t.startswith("M8"), 20, 34), txt)
    check("…Ø16 on the shoulder (z 0–8) and Ø6.5 on the relief (z 18–20)", on_step(lambda t: t == "⌀16", 0, 8)
          and on_step(lambda t: t == "⌀6.5", 18, 20), txt)
    tl = anchors(txt, lambda t: t == "14")
    check("…thread length 14 from the thread start to its end", tl and sorted(round(p["z"], 3) for p in tl[0][1])
          == [20, 34], txt)
    check("…the side view shows hidden lines by default", re.search(r"view\s+Side .*hidden lines", txt), txt)

    print("laser plate: style from the process, pitch dims, remove by match, notes ops, counts")
    err, txt, _ = c.tool("drawing", {"project": "flange", "part": "Flange"})
    check("90×50×4 laser plate with a Ø20 bore is drawn as a plate (process laser), not turned", not err
          and "(plate," in txt and "process laser" in txt and "⌀80" not in txt and "⌀90" not in txt
          and "CROSS HOLES" not in txt, txt)
    check("D1 · its R5 outline corners are not pockets: no R5, pocket or depth callout", "R5" not in txt
          and "POCKET" not in txt and "↧" not in txt, txt)
    proj = json.load(open(os.path.join(tmp, "flange", "project.json")))
    wargs = {"doc": os.path.join(tmp, "flange", "build", "flange.FCStd"), "project": proj, "rev": "r0001",
             "out_dir": os.path.join(tmp, "flange_out"), "save_dir": os.path.join(tmp, "flange_out"),
             "sheets": [{"part": "Flange", "process": "laser", "hole_dims": "pitch", "hidden": True, "ops": [
                 {"op": "remove", "match": {"view": "Top", "type": "DistanceY", "value": 50}},
                 {"op": "remove", "match": {"view": "Top", "value": 123.4}},
                 {"op": "append_note", "text": "deburr all edges after cutting"},
                 {"op": "add_note", "text": "QTY 4 PER ASSEMBLY", "x": 30, "y": 100, "align": "left"}]}]}
    res = srv.run_gui_worker("create", wargs, timeout=600)
    sh = (res.get("sheets") or [{}])[0]
    check("pitch mode: create ok", res.get("ok"), res)
    check("…remove by match removes the matching generated dimension", any(
        "removed 1 dimension(s) matching" in x for x in sh.get("ops_done", [])), sh.get("ops_done"))
    check("…a remove that matches nothing fails and lists what is there", any(
        "no dimension matches" in x and "DistanceX" in x for x in sh.get("ops_failed", [])), sh.get("ops_failed"))
    check("…the dims count follows the ops", re.search(r"dims\s+\d+/\d+ verified against the model \(\d+ generated; "
                                                      r"1 removed by ops\)", res.get("text", "")), res.get("text"))
    err, txt, _ = c.tool("drawing", {"action": "read", "doc": sh.get("document", "?"), "return_image": False})
    ds_ = dims_of(txt)
    check("…hole pattern dimensioned by its pitch: 76 and 36, plus one location from the edge (7)",
          any(t == "DistanceX" and v.strip() == "76" for _, t, v, _, _, _ in ds_)
          and any(t == "DistanceY" and v.strip() == "36" for _, t, v, _, _, _ in ds_)
          and any(v.strip() == "7" for _, _, v, _, _, _ in ds_)
          and not any(v.strip() in ("83", "43") for _, _, v, _, _, _ in ds_), txt)
    check("…hidden: true shows hidden lines on the orthographic views", len(re.findall(r"view\s+\S+ .*hidden lines",
                                                                                     txt)) >= 2, txt)
    notes = re.search(r"note\s+\S+_Notes\s+(.*?)\s+at \(", txt)
    check("…append_note adds a numbered line to the NOTES block", notes and re.search(
        r"\d+\. deburr all edges after cutting", notes.group(1)), txt)
    fsvg = open(os.path.join(tmp, "flange_out", "Flange.svg")).read()
    org = svg_text_origin(fsvg, "QTY 4 PER ASSEMBLY")
    H = 210.0 if "A4" in res.get("text", "") else 297.0
    check("…add_note align=left puts the first line's baseline start at x, y", org and abs(org[0] - 30) < 0.6
          and abs((H - org[1]) - 100) < 0.6, (org, H))
    nl = svg_text_origin(fsvg, "1. MATERIAL")
    ap = svg_text_origin(fsvg, "deburr all edges after cutting")
    check("…the appended line starts where the other notes start", nl and ap and abs(nl[0] - ap[0]) < 0.3, (nl, ap))

    print("layout: texts clear of other dimensions' lines")
    err, txt, _ = c.tool("drawing", {"project": "centre", "part": "Mount"})
    check("centred pilot: drawn", not err and "verified against the model" in txt, txt)
    err, txt, _ = c.tool("drawing", {"action": "read", "project": "centre", "return_image": False})
    ov = [d for d in dims_of(txt) if d[2].strip() == "42.3" and d[1] == "DistanceY"]
    check("…the overall 42.3 text moved off the line the hole's position dimension runs along (y = 0)",
          len(ov) == 1 and abs(float(ov[0][4])) > 3, txt)

    print("notes that need room")
    many = ["note %d: keep this sheet with the parts and check the outline against the template before cutting" % i
            for i in range(1, 9)]
    err, txt, _ = c.tool("drawing", {"project": "gusset", "part": "Gusset", "paper": "auto", "scale": "auto",
                                     "notes": many})
    check("eight long notes: A4 at 1:1 kept (notes moved), or the change of paper/scale explained", not err and (
        "(plate, A4 ISO 5457 landscape, scale 1:1)" in txt or "the notes need" in txt), txt)
    print("K1 · sheet_status: holes moved / drawing spec changed with the shape signature unchanged")
    err, txt, _ = c.tool("drawing", {"project": "mover", "part": "Bracket"})
    check("mover: drawn", not err and "verified against the model" in txt, txt)
    msheet = os.path.join(tmp, "mover", "drawings", "Bracket.FCStd")
    mfs = fc_source(msheet)
    check("…FcSource records sig, content (with its parts) and the spec hash", mfs.get("sig") and
          len(mfs.get("content") or "") == 40 and "holes" in (mfs.get("content_parts") or {}) and "spec_hash" in mfs,
          mfs)
    mbuild = os.path.join(tmp, "mover", "build", "mover.FCStd")
    msh = mfs.get("spec_hash")

    def mstatus(spec_hash=msh):
        st_ = srv.run_gui_worker("sheet_status", {"build": mbuild, "sheets": [
            {"part": "Bracket", "doc": msheet, "spec_hash": spec_hash}]})
        return (st_.get("sheets") or [{}])[0]
    check("…current right after create", mstatus().get("current") is True, mstatus())
    mp = os.path.join(tmp, "mover", "params.json")
    pr = json.load(open(mp))
    pr["hx"]["value"] = 5
    write(mp, json.dumps(pr))
    err, txt, _ = c.tool("build", {"project": "mover", "views": []})
    sh = mstatus()
    check("…a hole moved (volume, area, bbox and faces unchanged): stale, saying the holes moved",
          not err and sh.get("current") is False and "holes moved" in sh.get("detail", "")
          and "content" in sh.get("changed", []) and "shape" not in sh.get("changed", []), sh)
    pr["hx"]["value"] = 20
    write(mp, json.dumps(pr))
    c.tool("build", {"project": "mover", "views": []})
    check("…moved back: current again", mstatus().get("current") is True, mstatus())
    sh = mstatus("0" * 40)
    check("…another drawing spec (material/finish/notes): stale, saying so", sh.get("current") is False
          and "material/finish/notes" in sh.get("detail", "") and sh.get("changed") == ["spec"], sh)
    check("content_change names the kind that changed (stdlib)", ds.content_change(
        {"holes": "a", "fits_threads": "b"}, {"holes": "a", "fits_threads": "c"}) == "fit/thread changed")

    print("#5 · hole callouts per Hole feature, depth and through state")
    err, txt, _ = c.tool("drawing", {"project": "taps", "part": "Block"})
    co = re.search(r"callouts\s+(.*)", txt)
    co = co.group(1) if co else ""
    check("taps: one callout per distinct spec — ↧6 and ↧12 taps, a THRU and a blind Ø6.6", not err
          and "2× M4×0.7-6H ↧6" in co and "2× M4×0.7-6H ↧12" in co and "⌀6.6 THRU" in co and "⌀6.6 ↧5" in co
          and "4× M4" not in co and "2× ⌀6.6" not in co, txt)

    print("#15 · chamfers of different sizes are all stated")
    err, txt, _ = c.tool("drawing", {"project": "pin", "part": "Pin"})
    check("pin: 'CHAMFERS AS SHOWN' with each size and where it is, no 'UNLESS STATED' size", not err
          and "CHAMFERS AS SHOWN: 1.5 × 45° AT ⌀20; 0.5 × 45° AT ⌀10" in txt and "CHAMFERS 0.5" not in txt
          and "CHAMFERS 1.5" not in txt, txt)

    print("#16 · every distinct hole coordinate is dimensioned")
    err, txt, _ = c.tool("drawing", {"project": "rail", "part": "Rail"})
    check("rail: all dimensions verified, nothing left unplaced", not err and re.search(r"dims\s+(\d+)/\1 verified",
                                                                                        txt) and "✗" not in txt, txt)
    err, rtxt, _ = c.tool("drawing", {"action": "read", "project": "rail", "return_image": False})
    vals = sorted(float(v) for _, t, v, _, _, _ in dims_of(rtxt) if t == "DistanceX" and re.match(r"^[\d.]+$", v))
    check("…the 6 hole positions 15 … 115 from the edge (not only the first 4)", all(
        x in vals for x in (15, 35, 55, 75, 95, 115)), vals)
    res = srv.run_gui_worker("create", {
        "doc": os.path.join(tmp, "rail", "build", "rail.FCStd"), "project": json.load(open(os.path.join(
            tmp, "rail", "project.json"))), "rev": "r0001", "out_dir": os.path.join(tmp, "rail_pitch"),
        "save_dir": os.path.join(tmp, "rail_pitch"), "sheets": [{"part": "Rail", "hole_dims": "pitch", "ops": [
            {"op": "add_dimension", "view": "Top", "a": "Rail.holes(d=5.5)[4]", "b": "Rail.holes(d=5.5)[5]",
             "type": "DistanceX", "tolerance": "0.05"},
            {"op": "add_dimension", "view": "Top", "a": "Rail.holes(d=5.5)[0]", "b": "Rail.holes(d=5.5)[1]",
             "type": "DistanceX", "x": "left"},
            {"op": "remove", "match": {"text": "130"}}]}]}, timeout=600)
    sh = (res.get("sheets") or [{}])[0]
    err, ptxt, _ = c.tool("drawing", {"action": "read", "doc": sh.get("document", "?"), "return_image": False})
    pv = [float(v) for _, t, v, _, _, _ in dims_of(ptxt) if t == "DistanceX" and re.match(r"^[\d.]+$", v)]
    check("…pitch mode: the edge location and a 20 pitch for every hole (5 pitches)", res.get("ok")
          and pv.count(20) == 5 and 15 in pv, (res.get("error"), pv))
    print("#44 #45 · failing ops leave nothing; remove matches the shown text")
    check("…a bad tolerance and a bad x fail before anything is made", len(sh.get("ops_failed") or []) == 2
          and any("tolerance '0.05' not understood" in x for x in sh["ops_failed"])
          and any("'left' is not a number" in x for x in sh["ops_failed"]), sh.get("ops_failed"))
    check("…nothing they started is counted as added by ops", "added by ops" not in res.get("text", "")
          and pv.count(20) == 5, res.get("text"))
    check("…remove {match: {text: '130'}} removes the dimension that shows 130", any(
        "removed 1 dimension(s)" in x for x in sh.get("ops_done") or []) and 130 not in pv, (sh.get("ops_done"), pv))

    print("#17 · round outline: datums at the true extremes, outer Ø and the hole circle")
    err, txt, _ = c.tool("drawing", {"project": "disc", "part": "Disc"})
    check("disc: every dimension verified, nothing unplaced or removed", not err and re.search(
        r"dims\s+(\d+)/\1 verified", txt) and "✗" not in txt, txt)
    err, dtxt, _ = c.tool("drawing", {"action": "read", "project": "disc", "return_image": False})
    dd_ = dims_of(dtxt)
    nums = [float(v) for _, t, v, _, _, _ in dd_ if t in ("DistanceX", "DistanceY") and re.match(r"^[\d.]+$", v)]
    check("…outer ⌀100 and the ⌀80 PCD", any(v.strip() == "⌀100" for _, _, v, _, _, _ in dd_)
          and any(v.strip() == "⌀80 PCD" for _, _, v, _, _, _ in dd_), dtxt)
    check("…hole positions measured from the outline's edge (21.72 / 78.28 / 50), none zero", nums and min(nums) > 0
          and any(abs(v - 21.72) < 0.01 for v in nums) and any(abs(v - 78.28) < 0.01 for v in nums)
          and any(abs(v - 50) < 0.01 for v in nums), nums)

    print("#18 · identifiers are never shortened")
    err, txt, _ = c.tool("drawing", {"project": "conveyor-v2"})
    dns = []
    for nm in ("MotorMountBracketLeft", "MotorMountBracketRight"):
        sv = open(os.path.join(tmp, "conveyor-v2", "out", "drawings", nm + ".svg")).read()
        m = re.search(r'freecad:editable="drawing_number"[^>]*>\s*<tspan[^>]*>([^<]*)<', sv)
        dns.append(m.group(1) if m else None)
    check("conveyor-v2: two different, whole drawing numbers", not err and dns[0] and dns[1] and dns[0] != dns[1]
          and dns[0].endswith("MOTORMOUNTBRACKETLEFT") and dns[1].endswith("MOTORMOUNTBRACKETRIGHT")
          and "drawing_number" not in (re.search(r"shortened to fit[^\n]*", txt) or [""])[0], (dns, txt))
    err, txt, _ = c.tool("drawing", {"project": "conveyor-v2", "part": "MotorMountBracketLeft",
                                     "drawing_number": "CV2-" + "X" * 70})
    check("…a drawing number too long even in small text: refused, asking for a shorter one", err
          and "never shortened" in txt and "shorter drawing_number" in txt, txt)
    c.tool("drawing", {"project": "conveyor-v2", "part": "MotorMountBracketLeft", "reset": True})

    print("#19 · a multi-part create that fails writes nothing")
    err, txt, _ = c.tool("drawing", {"project": "pair"})
    check("pair: both drawn", not err and "SHEET Small" in txt and "SHEET Big" in txt, txt)
    small = os.path.join(tmp, "pair", "drawings", "Small.FCStd")
    small_pdf = os.path.join(tmp, "pair", "out", "drawings", "Small.pdf")
    before = (sha1(small), sha1(small_pdf))
    err, txt, _ = c.tool("drawing", {"project": "pair", "paper": "A4", "scale": "2:1", "notes": ["deburr"]})
    check("…Big does not fit A4 at 2:1: an error naming it, saying nothing was written", err and "Big" in txt
          and "Nothing was written" in txt and "Small drew fine" in txt, txt)
    check("…Small's sheet and PDF are untouched", (sha1(small), sha1(small_pdf)) == before)

    print("#21 · add_dimension is checked against the model, not the projection")
    pdoc = os.path.join(tmp, "plate4", "drawings", "Plate.r02.FCStd")
    err, rt, _ = c.tool("drawing", {"action": "read", "doc": pdoc, "return_image": False})
    if re.search(r"view\s+Iso\b", rt):
        err, txt, _ = c.tool("drawing", {"action": "edit", "doc": pdoc, "ops": [
            {"op": "add_dimension", "view": "Iso", "a": "Plate.holes(d=4.5)[0]", "b": "Plate.holes(d=4.5)[1]",
             "type": "Distance"}]})
        check("an Iso-view distance (foreshortened) is refused, with the model value", err and "not parallel to the "
              "Iso view plane" in txt and "60.000" in txt, txt)
        err, txt, _ = c.tool("drawing", {"action": "edit", "doc": pdoc, "ops": [
            {"op": "add_dimension", "view": "Iso", "a": "Plate.holes(d=4.5)[0]", "b": "Plate.holes(d=4.5)[1]",
             "type": "DistanceX"}]})
        check("…DistanceX along the Iso view's oblique axis is refused too", err and "not a model axis" in txt, txt)
    else:
        check("plate4 sheet has an Iso view to test on", False, rt)
    err, txt, _ = c.tool("drawing", {"action": "edit", "doc": pdoc, "ops": [
        {"op": "add_dimension", "view": "Top", "a": "Plate.holes(d=4.5)[0]", "b": "Plate.holes(d=4.5)[3]"}]})
    check("…an in-plane Top distance between two holes is the 3D distance (45 × 30 → 54.083)", not err
          and re.search(r"verified ✓ 54\.083", txt), txt)

    def wcreate(proj, sheet, out=None):
        """drawing create through the worker (options the tool may not take), into <tmp>/<out>."""
        o = os.path.join(tmp, out or proj + "_w")
        return srv.run_gui_worker("create", {"doc": os.path.join(tmp, proj, "build", proj + ".FCStd"),
                                             "project": json.load(open(os.path.join(tmp, proj, "project.json"))),
                                             "rev": "r0001", "sheets": [sheet], "out_dir": o, "save_dir": o},
                                  timeout=600)

    def wread(res):
        sh_ = (res.get("sheets") or [{}])[0]
        return c.tool("drawing", {"action": "read", "doc": sh_.get("document", "?"), "return_image": False})[1]

    print("D1 · pockets: size, location from the hole datums, depth and corner radii — each verified")
    err, txt, _ = c.tool("drawing", {"project": "pockets", "part": "AdapterPlate"})
    co = callouts_of(txt)
    check("cnc plate with a 40 × 20 × 3 R3 pocket: every dimension verified", not err
          and re.search(r"dims\s+(\d+)/\1 verified", txt) and "✗" not in txt, txt)
    check("…no view shows its floor: the depth is a '↧3' callout; the corners '4× R3'", "↧3" in co and "4× R3" in co,
          co)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "pockets", "return_image": False})
    check("…its length 40 and width 20 between its walls", has_dim(rt, "40", x=[-20, 20])
          and has_dim(rt, "20", y=[-10, 10]), rt)
    check("…located from the same datum edges as the holes (left 30, bottom 20)", has_dim(rt, "30", x=[-50, -20])
          and has_dim(rt, "20", y=[-30, -10]), rt)
    check("…'4× R3' is a radius dimension on a corner arc (TechDraw measures R3)",
          re.search(r"dim\s+\S+\s+Radius\s+4× R3\b", rt), rt)
    psheet = os.path.join(tmp, "pockets", "drawings", "AdapterPlate.FCStd")
    pfs = fc_source(psheet)
    check("…FcSource records the pockets in what the sheet shows", "pockets" in (pfs.get("content_parts") or {}), pfs)
    pbuild = os.path.join(tmp, "pockets", "build", "pockets.FCStd")

    def pstatus():
        st_ = srv.run_gui_worker("sheet_status", {"build": pbuild, "sheets": [
            {"part": "AdapterPlate", "doc": psheet, "spec_hash": pfs.get("spec_hash")}]})
        return (st_.get("sheets") or [{}])[0]
    check("…sheet_status: current right after create", pstatus().get("current") is True, pstatus())
    pp = os.path.join(tmp, "pockets", "params.json")
    pr = json.load(open(pp))
    pr["px"]["value"] = 5
    write(pp, json.dumps(pr))
    c.tool("build", {"project": "pockets", "views": []})
    sh = pstatus()
    check("…the pocket moved 5 mm (shape signature unchanged): stale, saying the pockets moved", sh.get("current")
          is False and "pockets, slots or steps moved or resized" in sh.get("detail", "")
          and sh.get("changed") == ["content"], sh)
    pr["px"]["value"] = 0
    write(pp, json.dumps(pr))
    c.tool("build", {"project": "pockets", "views": []})
    res = wcreate("pockets", {"part": "AdapterPlate", "process": "cnc", "hidden": True})
    rt = wread(res)
    check("hidden=true: the depth 3 is a dimension in the Front view, floor ↔ top; no '↧3' callout",
          res.get("ok") and has_dim(rt, "3", z=[5, 8]) and re.search(r"DistanceY\s+3\s+at .*refs Front\.", rt)
          and "↧3" not in res.get("text", ""), (res.get("text"), rt))
    err, txt, _ = c.tool("drawing", {"project": "tray", "part": "Tray"})
    co = callouts_of(txt)
    check("two identical pockets share one callout '2× 30 × 20 ↧4' and '8× R4'", not err and "2× 30 × 20 ↧4" in co
          and "8× R4" in co and re.search(r"dims\s+(\d+)/\1 verified", txt), txt)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "tray", "return_image": False})
    check("…each located from the left edge (15, 75), their common bottom location once (20)",
          has_dim(rt, "15", x=[-60, -45]) and has_dim(rt, "75", x=[-60, 15]) and len(
              [1 for t, a in anchors(rt, lambda s_: s_ == "20") if sorted(p.get("y") for p in a) == [-30, -10]]) == 1,
          rt)
    err, txt, _ = c.tool("drawing", {"project": "slots", "part": "Slotted"})
    check("open slot, rebate and a sharp-cornered pocket: all verified", not err
          and re.search(r"dims\s+(\d+)/\1 verified", txt) and "✗" not in txt, txt)
    check("…the sharp pocket (no arc for a leader) states size and depth in the notes",
          "POCKET 20 × 16 ↧6 (SEE VIEW)" in txt, txt)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "slots", "return_image": False})
    check("…slot 30 long from the open end and 12 wide, 24 from the bottom", has_dim(rt, "30", x=[-50, -20])
          and has_dim(rt, "12", y=[-6, 6]) and has_dim(rt, "24", y=[-30, -6]), rt)
    check("…rebate 8 wide, 52 from the bottom (its length is the part's, not repeated)", has_dim(rt, "8", y=[22, 30])
          and has_dim(rt, "52", y=[-30, 22]), rt)
    check("…slot depth 5 and rebate depth 4 in the Left view, which shows their floors from the open ends",
          re.search(r"DistanceY\s+5\s+at .*refs Left\.", rt) and has_dim(rt, "5", z=[7, 12])
          and re.search(r"DistanceY\s+4\s+at .*refs Left\.", rt) and has_dim(rt, "4", z=[8, 12]), rt)
    check("…the bottom edge fillets are not pockets (no R1)", "R1" not in txt, txt)
    err, txt, _ = c.tool("drawing", {"project": "chamfered", "part": "Block"})
    co = callouts_of(txt)
    check("pocket under a chamfered rim: depth 10 from the top surface (not 9 from the chamfer), one pocket",
          not err and "↧10" in co and "↧9" not in txt and len([x for x in co if "↧" in x]) == 1, txt)
    err, txt, _ = c.tool("drawing", {"project": "window", "part": "Frame"})
    co = callouts_of(txt)
    check("laser cut-out: size, location and '4× R2', no THRU text on a laser plate", not err and "4× R2" in co
          and "THRU" not in [x for x in co if not x.startswith("4× ⌀")] and re.search(r"dims\s+(\d+)/\1 verified", txt),
          txt)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "window", "return_image": False})
    check("…40 × 20, 20 from the left and 15 from the bottom", has_dim(rt, "40", x=[-20, 20])
          and has_dim(rt, "20", x=[-40, -20]) and has_dim(rt, "15", y=[-25, -10]), rt)
    err, txt, _ = c.tool("drawing", {"project": "under", "part": "Cover"})
    check("a pocket cut from the underside, which no default view looks at: said, with the fix", not err
          and "pocket 60 × 30 ↧6 from -Z: no view looks at it" in txt and "'bottom'" in txt, txt)
    res = wcreate("under", {"part": "Cover", "views": ["front", "top", "bottom"]})
    rt = wread(res)
    check("…with a bottom view it is dimensioned there (60, 30, ↧6, 4× R4)", res.get("ok")
          and re.search(r"DistanceX\s+60\s+at .*refs Bottom\.", rt) and re.search(r"DistanceY\s+30\s+at .*refs Bottom\.",
                                                                                  rt)
          and "↧6" in res.get("text", "") and "4× R4" in res.get("text", ""), (res.get("text"), rt))
    check("content_change names moved pockets and sheet-metal changes (stdlib)", ds.content_change(
        {"pockets": "a"}, {"pockets": "b"}) == "pockets, slots or steps moved or resized" and ds.content_change(
        {"sheet": "a"}, {"sheet": "b"}) == "sheet-metal data changed (thickness, bends or K-factor)")

    print("D2 · turned parts with bores: SECTION A-A through the axis")
    err, txt, _ = c.tool("drawing", {"project": "bush", "part": "Bush"})
    check("counterbored bush: section auto, every dimension verified", not err and "views     section, end" in txt
          and re.search(r"dims\s+(\d+)/\1 verified", txt) and "✗" not in txt and "SECTION A-A through the axis" in txt,
          txt)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "bush", "return_image": False})
    ys = [v.strip() for _, t, v, _, _, _ in dims_of(rt) if t == "DistanceY"]
    check("…outer ⌀30 and ⌀24 and the bores' own ⌀16 and ⌀20 in the section", all(
        x in ys for x in ("⌀30", "⌀24", "⌀16", "⌀20")) and re.search(r"refs Section\.", rt), (ys, rt))
    check("…the counterbore depth 8 from the end face it opens to", has_dim(rt, "8", z=[32, 40]), rt)
    check("…lengths 30 and 40 from the left end", has_dim(rt, "30", z=[0, 30]) and has_dim(rt, "40", z=[0, 40]), rt)
    bsvg = open(os.path.join(tmp, "bush", "out", "drawings", "Bush.svg")).read()
    check("…hatched in black lines (TechDraw 1.1's default hatch colour is a transparent green)",
          'stroke="#00ff00"' not in bsvg and len(re.findall(r'stroke="#000000" stroke-linecap="square"', bsvg)) > 20,
          len(re.findall(r'stroke="#000000" stroke-linecap="square"', bsvg)))
    check("…cutting line letters A on the end view and the SECTION A-A caption", bsvg.count(">A<") >= 2
          and "SECTION A-A" in bsvg, bsvg.count(">A<"))
    err, txt, _ = c.tool("drawing", {"project": "seat", "part": "Adapter"})
    check("tapped + H7 seat adapter: section, all verified", not err and "views     section, end" in txt
          and re.search(r"dims\s+(\d+)/\1 verified", txt) and "✗" not in txt, txt)
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "seat", "return_image": False})
    check("…the tapped bore called out by its thread on the tap-drill Ø, the seat by its fit with limits",
          has_dim(rt, "M10×1.5-6H", x=[-4.25, 4.25]) and has_dim(rt, "⌀12 H7 (+0.018/+0.000)", x=[-6, 6]), rt)
    check("…their depths 15 and 10 from the end faces they open to", has_dim(rt, "15", z=[25, 40])
          and has_dim(rt, "10", z=[0, 10]), rt)
    err, txt, _ = c.tool("drawing", {"project": "sleeve", "part": "Sleeve"})
    err, rt, _ = c.tool("drawing", {"action": "read", "project": "sleeve", "return_image": False})
    ys = [v.strip() for _, t, v, _, _, _ in dims_of(rt) if t == "DistanceY"]
    check("sleeve with an internal groove: one ⌀20 for the bore it interrupts, the groove's ⌀24, its walls at 20 "
          "and 23", not err and ys.count("⌀20") == 1 and "⌀24" in ys and "⌀40" in ys and has_dim(rt, "20", z=[0, 20])
          and has_dim(rt, "23", z=[0, 23]) and re.search(r"dims\s+(\d+)/\1 verified", txt), (txt, rt))
    res = wcreate("shaft", {"part": "Shaft", "process": "turning", "section": "full"})
    check("section='full' sections a solid shaft too", res.get("ok") and "views     section, end" in res.get("text", "")
          and re.search(r"dims\s+(\d+)/\1 verified", res.get("text", "")), res.get("text") or res)
    res = wcreate("shaft", {"part": "Shaft", "section": "half"})
    check("…an unknown section value is refused, listing auto / full / none", not res.get("ok")
          and "'auto'" in res.get("error", "") and "'full'" in res.get("error", "") and "'none'" in res.get("error", ""),
          res)
    res = wcreate("pockets", {"part": "AdapterPlate", "section": "full"})
    check("…section='full' on a milled plate: drawn without one, saying why", res.get("ok")
          and "section='full' is for a turned part" in res.get("text", ""), res.get("text") or res)

    print("D3 · bent sheet metal: bend table, thickness, flat pattern")
    err, txt, _ = c.tool("drawing", {"project": "bent", "part": "Bracket"})
    check("L-bracket with FcSheet: three views (bent, not a plate), thickness, bend table and the DXF pointer",
          not err and "bent sheet metal (1 bend, FcSheet)" in txt and "SHEET THICKNESS 2 mm" in txt
          and "BENDS (K 0.33): B1 90° UP R2" in txt and "FLAT PATTERN: SEE DXF (60 × 66.18 mm)" in txt
          and re.search(r"dims\s+(\d+)/\1 verified", txt), txt)
    check("…no builder spec in its FcSheet: the flat view is left out, saying why",
          "flat pattern view left out" in txt or "no flat pattern view" in txt, txt)
    if HAS_SHEET_METAL:
        err, txt, _ = c.tool("drawing", {"project": "ubracket", "part": "UBracket"})
        check("fc.sheet_metal U-bracket: a FLAT PATTERN view with its size verified", not err and ", flat" in txt
              and "BENDS (K 0.33): B1 90° UP R2 · B2 90° DOWN R2" in txt and re.search(r"dims\s+(\d+)/\1 verified", txt)
              and "✗" not in txt, txt)
        err, rt, _ = c.tool("drawing", {"action": "read", "project": "ubracket", "return_image": False})
        fl = re.search(r"flat pattern (\d+(?:\.\d+)?) × (\d+(?:\.\d+)?) mm",
                       c.tool("build", {"project": "ubracket", "views": []})[1])
        check("…the flat view (source FlatPattern, in the drawing document only) carries the flat size",
              re.search(r"view\s+Flat\s+DrawViewPart\s+source FlatPattern", rt) and fl and any(
                  v.strip() == _w_(fl.group(1)) for _, t, v, _, _, r_ in dims_of(rt) if r_.startswith("Flat."))
              and any(v.strip() == _w_(fl.group(2)) for _, t, v, _, _, r_ in dims_of(rt) if r_.startswith("Flat.")),
              (fl and fl.groups(), rt))
        check("…bend lines numbered like the bend table", re.search(r"note\s+\S+\s+B1\b", rt)
              and re.search(r"note\s+\S+\s+B2\b", rt), rt)
        usheet = os.path.join(tmp, "ubracket", "drawings", "UBracket.FCStd")
        ufs = fc_source(usheet)
        up = os.path.join(tmp, "ubracket", "params.json")
        pr = json.load(open(up))
        pr["k"]["value"] = 0.4
        write(up, json.dumps(pr))
        c.tool("build", {"project": "ubracket", "views": []})
        st = srv.run_gui_worker("sheet_status", {"build": os.path.join(tmp, "ubracket", "build", "ubracket.FCStd"),
                                                 "sheets": [{"part": "UBracket", "doc": usheet,
                                                             "spec_hash": ufs.get("spec_hash")}]})
        sh = (st.get("sheets") or [{}])[0]
        check("…a new K-factor (folded part unchanged): the sheet is stale, saying the sheet-metal data changed",
              sh.get("current") is False and "sheet-metal data changed" in sh.get("detail", "")
              and "shape" not in sh.get("changed", []), sh)
    else:
        print("  (fc.sheet_metal not in this fclib yet — flat pattern view checks skipped)")
    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
