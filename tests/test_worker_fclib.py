#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""worker.py and fclib.py: hole compensation, cross-hole
warning, reference-part BOM roles and mesh refs, fc.add with a feature,
frame_on 2D points, Param formatting, build revisions (reuse, lock,
report.txt, adopt refusal, FCBak cleanup), probe macro_dir, release_prepare
fields, STL inspect and run_script revisions; round 3: the missing-compensation
warning, backups of GUI-edited build files, params.json layout on adopt, Δ
against the same output with hole changes, .history snapshots, FcSteps, edge
selections (+, at=), run_script property diffs, image locations for loose
documents, joint report wording and fc.result in the build; round 4: inf/NaN and
unprintable fc.result values, spreadsheet cells in the run_script edit report,
hole moves in body coordinates and part moves in Δ, files read through fclib in
the inputs, last_build.json keyed inside the project — with real FreeCAD.

    python3 tests/test_worker_fclib.py
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
from mcp_client import FC_PY, ROOT, Client, check, fails  # noqa: E402

SERVER_DIR = os.path.join(ROOT, "server")
FC_LIB = os.path.join(os.path.dirname(os.path.dirname(FC_PY)), "lib")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def project(name, model, proj=None, params=None, checks=None):
    d = os.path.join(tmp, name)
    write(os.path.join(d, "model.py"), model)
    write(os.path.join(d, "project.json"), json.dumps(dict({"name": name}, **(proj or {})), indent=1))
    write(os.path.join(d, "params.json"), json.dumps(params or {}, indent=1))
    if checks is not None:
        write(os.path.join(d, "checks.json"), json.dumps(checks, indent=1))
    return d


def worker(op, args, timeout=300, env=None):
    """Call one worker op directly (no server), as run_worker does."""
    d = tempfile.mkdtemp(prefix="w-", dir=tmp)
    req, resp = os.path.join(d, "req.json"), os.path.join(d, "resp.json")
    with open(req, "w") as fh:
        json.dump({"op": op, "args": args}, fh)
    env = dict(os.environ, FCMCP_REQ=req, FCMCP_RESP=resp, FCMCP_SERVER_DIR=SERVER_DIR, FCMCP_LIB=FC_LIB,
               **(env or {}))
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(k, None)
    subprocess.run([FC_PY, "-I", os.path.join(SERVER_DIR, "worker.py"), req, resp], env=env, cwd=d,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if not os.path.isfile(resp):
        return {"ok": False, "error": "worker wrote no response"}
    with open(resp) as fh:
        return json.load(fh)


def result(txt):
    """The fc.result(...) JSON block of a run_script reply."""
    m = re.search(r"^result\n((?:  .*\n?)+)", txt, re.M)
    return json.loads(m.group(1)) if m else {}


def sha1(p):
    import hashlib
    with open(p, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def baks(d):
    return [f for f in os.listdir(d) if re.search(r"\.FCBak$|\.FCStd\d+$", f)] if os.path.isdir(d) else []


PLATE = '''P = fc.params()
b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(80, 40), name="Base"), 6)
top = fc.face(b, "+Z")
fc.holes(b, top, [(-30, 10)], "M3 clearance normal", name="Clr")
fc.holes(b, top, [(-30, -10)], "M3 tapped", name="Tap")
fc.holes(b, top, [(-10, 10)], "Ø4.2", name="Plain")
fc.holes(b, top, [(-10, -10)], "Ø8 H7", name="Fit")
fc.holes(b, top, [(10, 10)], "M3 clearance + cbore ISO4762", name="Cb")
fc.holes(b, top, [(10, -10)], "M3 insert", name="Ins")
fc.holes(b, top, [(30, 10)], "Ø2.5", compensation=0, name="NoComp")
fc.holes(b, top, [(30, -10)], "Ø2.5", compensation=0.3, name="Forced")
print(f"bore Ø{P.bore:g} / {P.bore:.2f} / {P.bore}")
'''

HOLE_PROBE = '''for o in doc.Objects:          # as the GUI would: everything recomputed after opening
    o.touch()
doc.recompute()
out = {}
for n in ("Clr", "Tap", "Plain", "Fit", "Cb", "Ins", "NoComp", "Forced"):
    h = doc.getObjectsByLabel(n)[0]
    out[n] = [round(h.Diameter.Value, 4), round(getattr(h, "FcHoleComp", 0.0), 4),
              round(h.HoleCutDiameter.Value, 4) if h.HoleCutType not in ("None", "") else None]
rs = sorted(set(round(f.Surface.Radius * 2, 3) for f in doc.getObjectsByLabel("Plate")[0].Shape.Faces
                if f.Surface.__class__.__name__ == "Cylinder"))
fc.result(holes=out, cyl=rs)
'''

tmp = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp-fixD-"))
try:
    c = Client(tmp)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    # ----------------------------------------------------------------- D1
    print("hole compensation (D1)")
    project("comp_fdm", PLATE, {"process": "fdm", "material": {"name": "PETG", "density_g_cm3": 1.27},
                                "dfm": {"hole_compensation_mm": 0.2}}, {"bore": {"value": 8.2, "unit": "mm"}})
    err, txt, _ = c.tool("build", {"project": "comp_fdm", "views": []})
    check("fdm project builds", not err and txt.startswith("BUILD ✓"), txt)
    check("build report: clearance Ø3.4 +0.2 comp → Ø3.6", "Clr ✓ (1× Ø3.4 +0.2 comp → Ø3.6 THRU  "
          "[M3 clearance normal])" in txt, txt)
    check("…plain Ø4.2 → Ø4.4; ISO 4762 counterbore Ø6 → Ø6.2", "Plain ✓ (1× Ø4.2 +0.2 comp → Ø4.4" in txt
          and "Cb ✓ (1× Ø3.4 +0.2 comp → Ø3.6 + Counterbore Ø6.2↧3.4 THRU" in txt, txt)
    check("…tapped, insert and H7 untouched", "Tap ✓ (1× M3x0.5-6H tap Ø2.50" in txt
          and "Ins ✓ (1× Ø4.00" in txt and "Fit ✓ (1× Ø8.00 THRU H7" in txt, txt)
    check("…compensation=0 and an explicit 0.3 override", "NoComp ✓ (1× Ø2.50 THRU" in txt
          and "Forced ✓ (1× Ø2.5 +0.3 comp → Ø2.8" in txt, txt)
    check("Param works in f-strings with format specs", "bore Ø8.2 / 8.20 / 8.2" in txt, txt)
    err, txt, _ = c.tool("run_script", {"project": "comp_fdm", "doc": "build/comp_fdm.FCStd", "code": HOLE_PROBE})
    r = result(txt)
    h = r.get("holes", {})
    check("reopened and recomputed: Ø3.6 clearance, FcHoleComp 0.2, cbore Ø6.2 (not reset to the table)",
          h.get("Clr") == [3.6, 0.2, None] and h.get("Cb") == [3.6, 0.2, 6.2] and h.get("Plain") == [4.4, 0.2, None]
          and 3.6 in r.get("cyl", []) and 3.4 not in r.get("cyl", []) and 6.0 not in r.get("cyl", []), txt)
    check("…tapped Ø2.5, insert and H7 bore without FcHoleComp",
          h.get("Tap", [0, 1])[1] == 0 and h.get("Ins", [0, 1])[1] == 0 and h.get("Fit") == [8.0, 0.0, None], txt)

    project("comp_cnc", PLATE.replace('fc.holes(b, top, [(30, -10)], "Ø2.5", compensation=0.3, name="Forced")',
                                      'fc.holes(b, top, [(30, -10)], "Ø2.5", compensation=0.3, name="Forced")\n'
                                      'clip = fc.body("Clip")\n'
                                      'fc.pad(clip, fc.sketch(clip, "XY", fc.rect(10, 10, center=(0, 40))), 3)\n'
                                      'fc.holes(clip, fc.face(clip, "+Z"), [(0, 40)], "M3 clearance", name="ClipHole")'),
            {"process": "fdm", "dfm": {"hole_compensation_mm": 0.2}, "parts": {"Plate": {"process": "cnc"}}},
            {"bore": 8.2})
    err, txt, _ = c.tool("build", {"project": "comp_cnc", "views": []})
    check("part made by CNC (parts.Plate.process) keeps nominal holes", not err and "Clr ✓ (1× Ø3.40 (M3 clearance" in txt
          and "Plain ✓ (1× Ø4.20 THRU" in txt and "comp → Ø3.6 (M3" not in txt.split("Clip")[0], txt)
    check("…explicit compensation still applies on a CNC part", "Forced ✓ (1× Ø2.5 +0.3 comp → Ø2.8" in txt, txt)
    check("…while an fdm part in the same project is compensated", "ClipHole ✓ (1× Ø3.4 +0.2 comp → Ø3.6 THRU" in txt,
          txt)

    print("cross-hole check (D1)")
    project("cross", '''b = fc.body("Rod")
fc.turned(b, [(10, 30)])
fc.holes(b, "XZ", [(0, 8)], "Ø3", offset=-6, name="Wrong")
fc.holes(b, "XZ", [(0, 20)], "Ø3", offset=+6, name="Right")
''', {"process": "turning"})
    err, txt, _ = c.tool("build", {"project": "cross", "views": []})
    check("a hole that cuts nothing is a build warning naming it", "hole Wrong removed no material" in txt
          and "offset=+(radius + 1)" in txt, txt)
    check("…the correctly offset cross hole is not flagged", "hole Right removed" not in txt, txt)

    # ----------------------------------------------------------------- D2
    print("reference parts (D2, D10)")
    refs = os.path.join(tmp, "refs_proj", "refs")
    os.makedirs(refs)
    err, txt, _ = c.tool("run_script", {"code": '''import MeshPart, Mesh
s = Part.makeBox(10, 20, 30, Vector(100, 0, 0)).fuse(Part.makeCylinder(5, 10, Vector(105, 10, 30)))
m = MeshPart.meshFromShape(Shape=s, LinearDeflection=0.05, AngularDeflection=0.2, Relative=False)
m.write(%r)
o = Mesh.Mesh(m)
o.removeFacets([0, 1])
o.write(%r)
fc.result(volume=s.Volume)
''' % (os.path.join(refs, "scan.stl"), os.path.join(refs, "open.stl"))})
    vol = result(txt).get("volume", 0)
    check("STL fixtures written", os.path.isfile(os.path.join(refs, "scan.stl")) and vol > 0, txt)
    project("refs_proj", '''b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 30)), 4)
fc.holes(b, fc.face(b, "+Z"), fc.rect_pattern(30, 20), "M3 clearance", name="Mount")
fc.ref_part("Motor", shape=Part.makeBox(20, 20, 20, Vector(-10, -10, 4)), bom="buy", qty=4, spec="NEMA17 42x40")
fc.ref_part("Table", shape=Part.makeBox(200, 200, 5, Vector(-100, -100, -5)))
fc.ref_part("KeepOut", shape=Part.makeBox(5, 5, 5, Vector(50, 0, 0)), bom="none")
fc.ref_part("Scan", step="refs/scan.stl")
fc.ref_part("Shell", step="refs/open.stl", bom="none")
''', {"process": "fdm", "material": {"name": "PETG", "density_g_cm3": 1.27}, "dfm": {"hole_compensation_mm": 0.2},
      "parts": {"Plate": {"qty": 2}, "Plaet": {"qty": 3}}})
    err, txt, _ = c.tool("build", {"project": "refs_proj", "views": []})
    check("refs build", not err and txt.startswith("BUILD ✓"), txt)
    check("…buy role with qty and spec shown", re.search(r"Motor\s+ref .*\[buy ×4: NEMA17 42x40\]", txt), txt)
    check("…keep-out marked, context unmarked", re.search(r"KeepOut\s+ref .*\[keep-out — not listed\]", txt)
          and re.search(r"Table\s+ref .*at \([-\d.]+, [-\d.]+, [-\d.]+\)$", txt, re.M), txt)
    check("…STL reference read as a solid, open STL warned",
          "Scan" in txt and "open.stl is not a closed mesh" in txt, txt)
    err, txt, _ = c.tool("run_script", {"project": "refs_proj", "doc": "build/refs_proj.FCStd", "code": '''import json
s = doc.getObjectsByLabel("Scan")[0]
fc.result(bom=json.loads(doc.getObjectsByLabel("Motor")[0].FcBom), solids=len(s.Shape.Solids),
          valid=s.Shape.isValid(), volume=s.Shape.Volume, default=json.loads(doc.getObjectsByLabel("Table")[0].FcBom))
'''})
    r = result(txt)
    check("FcBom JSON stored per I2", r.get("bom") == {"bom": "buy", "qty": 4, "spec": "NEMA17 42x40"}
          and r.get("default") == {"bom": "context", "qty": 1, "spec": None}, txt)
    check("…STL ref is one valid solid with the mesh's volume", r.get("solids") == 1 and r.get("valid")
          and abs(r.get("volume", 0) - vol) / vol < 0.002, txt)
    err, txt, _ = c.tool("run_script", {"code": 'fc.ref_part("X", shape=Part.makeBox(1, 1, 1), bom="purchase")'})
    check("unknown bom role is a readable error", "bom must be 'buy'" in txt and "RUN ✗" in txt, txt)
    err, txt, _ = c.tool("inspect", {"project": "refs_proj", "doc": "refs/scan.stl"})
    check("inspect opens an STL as a solid", not err and "solid ✓" in txt and "valid ✓" in txt, txt)
    err, txt, _ = c.tool("measure", {"project": "refs_proj", "doc": "refs/scan.stl", "a": "scan"})
    check("measure works on an STL", not err and "volume" in txt, txt)

    # ----------------------------------------------------------------- D3 / D4
    print("assemblies and frames (D3, D4)")
    err, txt, _ = c.tool("run_script", {"code": '''b = fc.body("B")
pad = fc.pad(b, fc.sketch(b, "XY", fc.rect(10, 10)), 5)
asm = fc.assembly("Asm")
r = fc.add(asm, pad)
g = fc.ground(asm, pad)
errs = []
for bad in (doc.addObject("Spreadsheet::Sheet", "Sheet"), asm):
    try:
        fc.add(asm, bad)
    except fc.FcError as e:
        errs.append(str(e))
fc.result(added=r.Label, ground=g.ObjectToGround.Label, volume=round(b.Shape.Volume, 3),
          pad_in_body=pad in b.Group, errs=errs)
'''})
    r = result(txt)
    check("fc.add / fc.ground with a Pad use its Body, which stays whole", r.get("added") == "B"
          and r.get("ground") == "B" and r.get("volume") == 500.0 and r.get("pad_in_body") is True, txt)
    check("…non-parts are refused with a hint", len(r.get("errs", [])) == 2
          and "Sheet (Spreadsheet::Sheet) is not a part" in r["errs"][0] and "fc.body" in r["errs"][0], txt)
    err, txt, _ = c.tool("run_script", {"code": '''b = fc.body("B")
fc.pad(b, fc.sketch(b, "XY", fc.rect(40, 20)), 5)
f = fc.frame_on(fc.face(b, "+Z"), at=(10, 5))
g = fc.frame_on(fc.face(b, "+Y"), at=(3, 2))
h = fc.frame_on(fc.face(b, "+Z"), at=(1, 2, 7))
errs = []
for bad in ((1, 2, 3, 4), (1,), "12"):
    try:
        fc.frame_on(fc.face(b, "+Z"), at=bad)
    except fc.FcError as e:
        errs.append(str(e))
v = lambda p: [round(p.Base.x, 6), round(p.Base.y, 6), round(p.Base.z, 6)]
fc.result(f=v(f), g=v(g), h=v(h), errs=errs)
'''})
    r = result(txt)
    check("frame_on 2D points use the face's axes: +Z (x, y), +Y (x, z)", r.get("f") == [10, 5, 5]
          and r.get("g") == [3, 10, 2] and r.get("h") == [1, 2, 7], txt)
    check("…anything but 2 or 3 numbers raises", len(r.get("errs", [])) == 3
          and all("2D point" in e for e in r["errs"]), txt)

    # ----------------------------------------------------------------- D7
    print("build revisions (D7)")
    pj = project("revs", '''P = fc.params()
b = fc.body("Block")
fc.pad(b, fc.sketch(b, "XY", fc.rect(P.w, 10)), 5)
''', {"process": "cnc"}, {"w": {"value": 20, "unit": "mm"}})
    err, txt, _ = c.tool("build", {"project": "revs", "views": []})
    check("first build r0001", not err and "  r0001  " in txt.splitlines()[0], txt)
    err, txt2, _ = c.tool("build", {"project": "revs", "views": []})
    check("unchanged inputs → same revision, said so", not err and "  r0001  " in txt2.splitlines()[0]
          and "inputs unchanged — still r0001" in txt2, txt2)
    hist = sorted(d for d in os.listdir(os.path.join(pj, ".history")) if re.match(r"r\d{4}$", d))
    rep = json.load(open(os.path.join(pj, ".history", "r0001", "report.json")))
    check("…no new history folder; report.json carries inputs_sha1", hist == ["r0001"]
          and re.match(r"^[0-9a-f]{40}$", rep.get("inputs_sha1", "")), hist)
    check("build/report.txt holds the latest report",
          open(os.path.join(pj, "build", "report.txt")).read().startswith(txt2.splitlines()[0]))
    write(os.path.join(pj, "params.json"), json.dumps({"w": {"value": 24, "unit": "mm"}}))
    err, txt, _ = c.tool("build", {"project": "revs", "views": []})
    check("changed params → next revision with Δ", "  r0002  " in txt.splitlines()[0] and "Δ r0001" in txt
          and "inputs unchanged" not in txt, txt)
    err, txt, _ = c.tool("build", {"project": "revs", "views": [], "params": {"w": 30}})
    check("a params override is a different input", "  r0003  " in txt.splitlines()[0], txt)
    err, txt, _ = c.tool("build", {"project": "revs", "views": []})
    check("back to the file's params → new revision (newest was the override)", "  r0004  " in txt.splitlines()[0], txt)
    err, txt, _ = c.tool("build", {"project": "revs", "views": [], "force": True})
    check("force with an untouched file reuses too", "still r0004" in txt, txt)
    write(os.path.join(pj, "drawings.json"), json.dumps({"_sheets": {"Block": {"file": "drawings/Block.FCStd"}}}))
    err, txt, _ = c.tool("build", {"project": "revs", "views": []})
    write(os.path.join(pj, "drawings.json"), json.dumps({"_sheets": {"Block": {"file": "drawings/Block.r02.FCStd"}}}))
    err, txt2, _ = c.tool("build", {"project": "revs", "views": []})
    check("drawings.json counts as an input, its _sheets bookkeeping does not",
          "  r0005  " in txt.splitlines()[0] and "still r0005" in txt2, txt + "\n" + txt2)
    check("no FreeCAD backup files left in build/", baks(os.path.join(pj, "build")) == [],
          baks(os.path.join(pj, "build")))
    c.tool("run_script", {"project": "revs", "doc": "build/revs.FCStd", "save": "inplace", "code": "pass"})
    check("in-place saves leave no FCBak either", baks(os.path.join(pj, "build")) == [],
          baks(os.path.join(pj, "build")))
    err, txt, _ = c.tool("build", {"project": "revs", "views": []})
    check("a touched build file gets a new revision", "  r0006  " in txt.splitlines()[0]
          and "touched outside the build loop" in txt, txt)

    print("adopt refused leaves params.json alone (D7d)")
    pa = project("adopt", '''P = fc.params()
b = fc.body("Block")
fc.pad(b, fc.sketch(b, "XY", fc.rect(20, 10)), P.height)
''', {"process": "cnc"}, {"height": {"value": 5, "unit": "mm"}})
    c.tool("build", {"project": "adopt", "views": []})
    before = open(os.path.join(pa, "params.json"), "rb").read()
    c.tool("run_script", {"project": "adopt", "doc": "build/adopt.FCStd", "save": "inplace",
                          "code": "sh = doc.getObject('Params')\nsh.set(sh.getCellFromAlias('height'), '7')\n"
                                  "doc.addObject('Part::Box', 'Extra')\ndoc.recompute()"})
    err, txt, _ = c.tool("build", {"project": "adopt", "views": [], "adopt_gui_params": True})
    check("structural + parameter edits: refused, and says params.json was left alone", err
          and "Extra" in txt and "params.json was left as it was" in txt and "Port structural edits" in txt, txt)
    check("…params.json byte-for-byte unchanged, no .bak written",
          open(os.path.join(pa, "params.json"), "rb").read() == before
          and not os.path.exists(os.path.join(pa, "params.json.bak")))

    print("concurrent builds (D7a)")
    pc = project("conc", '''P = fc.params()
b = fc.body("Block")
fc.pad(b, fc.sketch(b, "XY", fc.rect(P.w, 10)), 5)
''', {"process": "cnc"}, {"w": {"value": 20, "unit": "mm"}})
    ids = [c.send("tools/call", {"name": "build", "arguments": {"project": "conc", "views": [], "variant": v,
                                                                 "params": {"w": w}}})
           for v, w in (("a", 11), ("b", 12), ("c", 13))]
    outs = [Client.unpack(c.wait(i, 600)) for i in ids]
    revs = sorted(re.search(r"  (r\d{4})  ", o[1].splitlines()[0]).group(1) for o in outs if not o[0])
    lb = json.load(open(os.path.join(pc, ".history", "last_build.json")))
    check("three concurrent builds → three distinct revisions", revs == ["r0001", "r0002", "r0003"],
          [o[1][:200] for o in outs])
    check("…and every build file recorded in last_build.json", len(lb) == 3
          and all(v.get("rev") in revs for v in lb.values()), lb)

    # ----------------------------------------------------------------- D8 / D9
    print("probe and release_prepare (D8, D9)")
    res = worker("probe", {})
    check("probe returns macro_dir", res.get("ok") and str(res["data"].get("macro_dir", "")).rstrip("/").endswith("Macro"),
          res)
    pr = os.path.join(tmp, "refs_proj")
    res = worker("release_prepare", {"doc": os.path.join(pr, "build", "refs_proj.FCStd"), "project_dir": pr,
                                     "release_dir": os.path.join(tmp, "rel_out"), "qty": {"Plate": 3, "Nope": 1},
                                     "processes": {"Ghost": "cnc"}})
    data = res.get("data") or {}
    check("release_prepare runs", res.get("ok"), res.get("error") or res.get("traceback"))
    check("unknown keys from project.json parts, qty and processes; valid labels",
          data.get("unknown_keys") == {"parts": ["Plaet"], "qty": ["Nope"], "processes": ["Ghost"]}
          and data.get("labels") == ["Plate"], data.get("unknown_keys"))
    refs_ = {r["part"]: r for r in data.get("refs", [])}
    check("refs carry roles; bom 'none' left out", sorted(refs_) == ["Motor", "Scan", "Table"]
          and refs_["Motor"]["bom"] == "buy" and refs_["Motor"]["qty"] == 4 and refs_["Motor"]["spec"] == "NEMA17 42x40"
          and refs_["Table"]["bom"] == "context" and len(refs_["Table"]["size"]) == 3, data.get("refs"))
    part = (data.get("parts") or [{}])[0]
    up = ((part.get("dfm") or {}).get("metrics") or {}).get("orientation", {}).get("up")
    ex = {e["format"]: e for e in part.get("exports", [])}
    check("per-record exports with file/format/ok/check/oriented", set(ex) == {"3mf", "stl", "step"}
          and ex["stl"]["file"] == "Plate.stl" and all(e["ok"] for e in ex.values()), part.get("exports"))
    check("…fdm meshes oriented with the DFM best orientation", up and ex["3mf"]["oriented"] == up
          and ex["stl"]["oriented"] == up, (up, part.get("exports")))
    check("…hole_compensation 0.2, drawing None, qty override", part.get("hole_compensation") == 0.2
          and "drawing" in part and part["drawing"] is None and part.get("qty") == 3, part)

    # ----------------------------------------------------------------- D11
    print("run_script revisions (D11)")
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    shutil.copyfile(os.path.join(pr, "build", "refs_proj.FCStd"), os.path.join(work, "Part.FCStd"))
    err, txt, _ = c.tool("run_script", {"doc": os.path.join(work, "Part.FCStd"), "save": "revision", "code": "pass"})
    check("first patch → Part.r01.FCStd", not err and os.path.isfile(os.path.join(work, "Part.r01.FCStd")), txt)
    err, txt, _ = c.tool("run_script", {"doc": os.path.join(work, "Part.FCStd"), "save": "revision", "code": "pass"})
    check("patching the original again is refused, naming the newest revision", err and "Part.r01.FCStd" in txt
          and "from_older" in txt and not os.path.isfile(os.path.join(work, "Part.r02.FCStd")), txt)
    err, txt, _ = c.tool("run_script", {"doc": os.path.join(work, "Part.r01.FCStd"), "save": "revision",
                                        "code": "pass"})
    check("patching the newest → Part.r02.FCStd", not err and os.path.isfile(os.path.join(work, "Part.r02.FCStd")), txt)
    res = worker("run_script", {"doc": os.path.join(work, "Part.r01.FCStd"), "save": "revision", "code": "pass",
                                "from_older": True})
    check("from_older forks deliberately → Part.r03.FCStd, with a note", res.get("ok")
          and res["data"]["saved"].endswith("Part.r03.FCStd") and "forked from Part.r01.FCStd" in res.get("text", ""),
          res)
    check("no FCBak next to the user's files", baks(work) == [], baks(work))

    # ============================================================ round 3
    print("missing hole compensation is a warning (round 3 D1)")
    UNCOMP = '''b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40)), 4)
top = fc.face(b, "+Z")
fc.holes(b, top, fc.rect_pattern(40, 20), "M3 clearance", name="Mount")
fc.holes(b, top, [(0, 0)], "Ø5", name="Bore")
fc.holes(b, top, [(0, 15)], "M3 tapped", name="Tap")
'''
    project("nocomp", UNCOMP, {"process": "fdm"})
    err, txt, _ = c.tool("build", {"project": "nocomp", "views": []})
    check("fdm part, no dfm.hole_compensation_mm → one warning naming the part and its holes", not err
          and "Plate is fdm but project.json has no dfm.hole_compensation_mm — its 4 clearance holes and 1 plain Ø "
              "hole are NOT compensated and will print undersize; set 0.2 (typical FDM) or 0 to confirm none" in txt
          and txt.count("has no dfm.hole_compensation_mm") == 1, txt)
    project("nocomp_sla", UNCOMP, {"process": "sla"})
    err, txt, _ = c.tool("build", {"project": "nocomp_sla", "views": []})
    check("…sla says 0.1", "Plate is sla but" in txt and "set 0.1 (typical SLA)" in txt, txt)
    for nm, pj_ in (("zerocomp", {"process": "fdm", "dfm": {"hole_compensation_mm": 0}}),
                    ("cnccomp", {"process": "cnc"}),
                    ("partcnc", {"process": "fdm", "parts": {"Plate": {"process": "cnc"}}})):
        project(nm, UNCOMP, pj_)
        err, txt, _ = c.tool("build", {"project": nm, "views": []})
        check("…no warning for %s" % nm, not err and "hole_compensation_mm" not in txt, txt)

    print("GUI-edited build file is backed up (round 3 D2, D8)")
    pg = project("guiedit", '''P = fc.params()
b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(P.width, 40)), P.thickness)
''', {"process": "cnc"})
    compact = ('{"width": {"value": 70, "unit": "mm", "source": "user"},\n'
               ' "thickness": {"value": 5, "unit": "mm", "source": "user"},\n'
               ' "count": 3}\n')
    write(os.path.join(pg, "params.json"), compact)
    c.tool("build", {"project": "guiedit", "views": []})
    c.tool("run_script", {"project": "guiedit", "doc": "build/guiedit.FCStd", "save": "inplace",
                          "code": "sh = doc.getObject('Params')\nsh.set(sh.getCellFromAlias('thickness'), '8')\n"
                                  "doc.recompute()"})
    edited_sha = sha1(os.path.join(pg, "build", "guiedit.FCStd"))
    err, txt, _ = c.tool("build", {"project": "guiedit", "views": [], "adopt_gui_params": True})
    bk = sorted(os.listdir(os.path.join(pg, ".history", "backups"))) \
        if os.path.isdir(os.path.join(pg, ".history", "backups")) else []
    fcb = [b for b in bk if re.match(r"guiedit\.\d{8}-\d{6}(-\d+)?\.FCStd$", b)]
    check("adopt: the GUI-saved FCStd is copied to .history/backups/<name>.<timestamp>.FCStd first, and said so",
          not err and len(fcb) == 1 and sha1(os.path.join(pg, ".history", "backups", fcb[0])) == edited_sha
          and "kept the build file as saved outside the build loop: .history/backups/%s" % fcb[0] in txt, (bk, txt))
    newp = open(os.path.join(pg, "params.json")).read()
    check("adopt keeps params.json's layout and integers (8, not 8.0); previous file in backups",
          newp.splitlines()[0] == compact.splitlines()[0] and newp.splitlines()[2] == ' "count": 3}'
          and newp.splitlines()[1].startswith(' "thickness": {"value": 8, "unit": "mm", "source": "user", '
                                              '"note": "edited in the FreeCAD GUI ')
          and any(re.match(r"params\.json\.\d{8}-\d{6}(-\d+)?\.bak$", b) for b in bk)
          and not os.path.exists(os.path.join(pg, "params.json.bak"))
          and "adopted GUI parameter edits into params.json: thickness=8" in txt, newp + "\n" + txt)
    c.tool("run_script", {"project": "guiedit", "doc": "build/guiedit.FCStd", "save": "inplace",
                          "code": "doc.addObject('Part::Box', 'Extra')\ndoc.recompute()"})
    edited_sha = sha1(os.path.join(pg, "build", "guiedit.FCStd"))
    err, txt, _ = c.tool("build", {"project": "guiedit", "views": []})
    check("structural edit refused; the refusal says force keeps a copy", err and ".history/backups/" in txt, txt)
    err, txt, _ = c.tool("build", {"project": "guiedit", "views": [], "force": True})
    fcb2 = [b for b in os.listdir(os.path.join(pg, ".history", "backups")) if b.endswith(".FCStd") and b not in fcb]
    check("force over a structural GUI edit: the edited file is backed up too", not err and len(fcb2) == 1
          and sha1(os.path.join(pg, ".history", "backups", fcb2[0])) == edited_sha and "kept the build file" in txt,
          (fcb2, txt))
    err, txt, _ = c.tool("build", {"project": "guiedit", "views": [], "force": True})
    check("…an untouched build file is not backed up", "kept the build file" not in txt
          and len([b for b in os.listdir(os.path.join(pg, ".history", "backups")) if b.endswith(".FCStd")]) == 2, txt)

    print("Δ against the same output, features and holes; history snapshots; result line (round 3 D3, D4, D11)")
    DELTA = '''P = fc.params()
b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(P.w, 40)), 5)
fc.holes(b, fc.face(b, "+Z"), HOLES, "M3 clearance", name="Mount")
fc.result(area=P.w * 40 / 3.0, n=len(HOLES), tag="plate")
'''
    pd = project("delta", "HOLES = [(-10, 0), (10, 0)]\n" + DELTA, {"process": "cnc"},
                 {"w": {"value": 60, "unit": "mm"}}, checks=[])
    write(os.path.join(pd, "drawings.json"), json.dumps({"Plate": {"paper": "A4"}}))
    err, txt, _ = c.tool("build", {"project": "delta", "views": []})
    check("fc.result values from model.py on a 'result' line", not err
          and 'result   area=800 · n=2 · tag="plate"' in txt, txt)
    rep = json.load(open(os.path.join(pd, ".history", "r0001", "report.json")))
    check("…and in report.json", rep.get("results", {}).get("n") == 2, rep.get("results"))
    check(".history/r0001 keeps model.py, params.json, checks.json, project.json and drawings.json",
          all(os.path.isfile(os.path.join(pd, ".history", "r0001", f))
              for f in ("model.py", "params.json", "checks.json", "project.json", "drawings.json")),
          os.listdir(os.path.join(pd, ".history", "r0001")))
    err, txt, _ = c.tool("build", {"project": "delta", "views": [], "variant": "wide", "params": {"w": 90}})
    check("variant build r0002", not err and "  r0002  " in txt.splitlines()[0] and "Δ" not in txt, txt)
    write(os.path.join(pd, "model.py"), "HOLES = [(-10, 0), (10, 0), (-10, 12), (10, 12)]\n" + DELTA)
    write(os.path.join(pd, "params.json"), json.dumps({"w": {"value": 70, "unit": "mm"}}))
    err, txt, _ = c.tool("build", {"project": "delta", "views": []})
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("main build diffs against the previous MAIN build (r0001), not the variant", not err
          and "  r0003  " in txt.splitlines()[0] and dl and dl[0].startswith("Δ r0001 ")
          and "param w 60 → 70" in dl[0], txt)
    check("…with hole changes: count and new positions",
          dl and "Plate Mount 2 → 4 holes" in dl[0] and "Plate Mount holes added at (-10, 12), (10, 12)" in dl[0]
          and "removed at" not in dl[0], dl)
    write(os.path.join(pd, "model.py"), "HOLES = [(-10, 0), (10, 0), (-10, 12), (10, 12)]\n"
          + DELTA.replace('"M3 clearance"', '"M4 clearance"'))
    err, txt, _ = c.tool("build", {"project": "delta", "views": []})
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("…a resized hole is named with before and after", dl and dl[0].startswith("Δ r0003 ")
          and "Plate Mount was 4× Ø3.40" in dl[0] and "now 4× Ø4.50" in dl[0], dl)
    err, txt, _ = c.tool("build", {"project": "delta", "views": [], "variant": "wide", "params": {"w": 95}})
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("a variant diffs against its own previous build", dl and dl[0].startswith("Δ r0002 ")
          and "param w 90 → 95" in dl[0], dl)

    print("turned steps, edge selections (round 3 D5, D10)")
    err, txt, _ = c.tool("run_script", {"code": '''import json
s = fc.body("Shaft")
fc.turned(s, [(10, 5), (8, 10, "k6"), (6.5, 2), (8, 14)])
e = fc.edges(s, kind="circle", at=0) + fc.edges(s, kind="circle", at=31)
both = sum([fc.edges(s, kind="circle", at=0), fc.edges(s, kind="circle", at=31)])
fc.chamfer(s, e, 0.5, name="Ends")
cones = len([f for f in s.Shape.Faces if f.Surface.__class__.__name__ == "Cone"])
x = fc.edges(s, kind="circle", axis="Z", at=5)
errs = []
p = fc.body("P")
fc.pad(p, fc.sketch(p, "XY", fc.rect(10, 10)), 2)
try:
    fc.edges(s, kind="circle", at=0) + fc.edges(p, kind="line", parallel="X")
except fc.FcError as ex:
    errs.append(str(ex))
try:
    fc.edges(s, kind="circle", at=99)
except fc.FcError as ex:
    errs.append(str(ex))
fc.result(steps=json.loads(s.FcSteps), fits=json.loads(s.FcFits), n=len(e), sum=len(both), cones=cones,
          at5=len(x), errs=errs)
'''})
    r = result(txt)
    check("fc.turned writes FcSteps per step: d, z0, z1, fit — two Ø8 steps kept apart",
          r.get("steps") == [{"d": 10.0, "z0": 0.0, "z1": 5.0, "fit": None}, {"d": 8.0, "z0": 5.0, "z1": 15.0, "fit": "k6"},
                             {"d": 6.5, "z0": 15.0, "z1": 17.0, "fit": None}, {"d": 8.0, "z0": 17.0, "z1": 31.0, "fit": None}]
          and r.get("fits") == {"8": "k6"}, txt)
    check("fc.edges(...) + fc.edges(...) combine into one chamfer (both ends), sum() works too",
          r.get("n") == 2 and r.get("sum") == 2 and r.get("cones") == 2, txt)
    check("…at= selects edges in the plane at that coordinate along axis (z = 5: the Ø10 and Ø8 circles)",
          r.get("at5") == 2, txt)
    errs = r.get("errs", [])
    check("…combining edges of different bodies and an empty at= selection are readable errors", len(errs) == 2
          and "cannot combine edges of" in errs[0] and "at=99 along Z" in errs[1], errs)

    print("run_script property diff (round 3 D6)")
    src = os.path.join(tmp, "inbox", "bracket.FCStd")
    os.makedirs(os.path.dirname(src))
    err, txt, _ = c.tool("run_script", {"save_as": src, "save": "no", "code": '''b = fc.body("Bracket")
sk = fc.sketch(b, "XY", fc.rect(60, 30), name="BaseSketch")
fc.pad(b, sk, 6, name="Base")
fc.holes(b, fc.face(b, "+Z"), [(-20, 0)], "Ø5", name="CableHole")
fc.holes(b, fc.face(b, "+Z"), [(20, 0)], "M4 clearance", name="MountHoles")
'''})
    check("source document written", os.path.isfile(src), txt)
    err, txt, _ = c.tool("run_script", {"doc": src, "save": "revision", "code": '''h = doc.getObjectsByLabel("CableHole")[0]
h.Diameter = 6
'''})
    ed = txt.split("edits    ", 1)[-1]
    check("save='revision' reports the property-level diff vs the source", not err
          and "edits    vs bracket.FCStd" in txt and re.search(r'changed\s+CableHole "CableHole": Diameter 5(\.0)? mm → 6(\.0)? mm',
                                                               ed), txt)
    check("…and lists downstream-only shape changes separately", "downstream only" in ed
          and "MountHoles: shape follows CableHole" in ed and "Bracket: shape follows" in ed
          and "MountHoles\"" not in ed, txt)
    err, txt, _ = c.tool("run_script", {"doc": os.path.join(tmp, "inbox", "bracket.r01.FCStd"), "save": "revision",
                                        "code": "pass"})
    check("…a save with no edits says so", "edits    none — no property" in txt, txt)

    print("images of loose documents go to the cache (round 3 D7)")
    fake_home = os.path.join(tmp, "home")
    os.makedirs(fake_home)
    res = worker("run_script", {"doc": src, "views": ["iso"], "code": "pass"}, env={"HOME": fake_home})
    imgs = res.get("images") or []
    check("run_script views on a doc outside a project → ~/.cache/freecad-mcp/images, nothing next to the doc",
          res.get("ok") and len(imgs) == 1
          and imgs[0].startswith(os.path.join(fake_home, ".cache", "freecad-mcp", "images") + os.sep)
          and os.path.isfile(imgs[0]) and not os.path.exists(os.path.join(tmp, "inbox", "out")), res)
    res = worker("run_script", {"doc": os.path.join(pd, "build", "delta.FCStd"), "project_dir": pd, "views": ["iso"],
                                "code": "pass"}, env={"HOME": fake_home})
    imgs = res.get("images") or []
    check("…inside a project: its out/", res.get("ok") and imgs and imgs[0].startswith(os.path.join(pd, "out") + os.sep),
          res)

    print("joint report (round 3 D9)")
    shutil.copytree(os.path.join(HERE, "fixtures", "asmlab"), os.path.join(tmp, "asmlab"),
                    ignore=shutil.ignore_patterns("build", "out", ".history", "release", "drawings", "drawings.json"))
    err, txt, _ = c.tool("build", {"project": "asmlab", "views": []})
    check("joint lines say part b was placed by the joint and verified", not err
          and "Base ↔ Motor  holds ✓ (Motor placed by the joint and verified)" in txt
          and "Base ↔ Shaft  holds ✓ (Shaft placed by the joint and verified)" in txt, txt)

    # ============================================================ round 4
    print("fc.result with inf/NaN or unprintable values (round 4 #29)")
    pi = project("infres", """import math
class Bad:
    def __str__(self):
        raise RuntimeError("no str")
loop = [1]
loop.append(loop)
b = fc.body("B")
fc.pad(b, fc.sketch(b, "XY", fc.rect(10, 10)), 2)
fc.result(min_gap=min([], default=math.inf), neg=-math.inf, bad=float("nan"), ok=2.5, weird=Bad(), loop=loop,
          vals=[1.0, math.inf])
""", {"process": "cnc"})
    res = worker("build", {"project_dir": pi, "views": []})
    txt = res.get("text", "")
    check("build with inf/NaN/unprintable results succeeds with a report", res.get("ok") and txt.startswith("BUILD ✓")
          and os.path.isfile(os.path.join(pi, "build", "report.txt")), res.get("error") or txt)
    check("…result line shows inf, -inf, nan and names the unprintable value", "min_gap=inf" in txt and "neg=-inf" in txt
          and "bad=nan" in txt and "ok=2.5" in txt and "vals=[1, inf]" in txt
          and "weird=(unprintable Bad: RuntimeError)" in txt, txt)
    bad_consts = []
    rep = json.load(open(os.path.join(pi, ".history", "r0001", "report.json")), parse_constant=bad_consts.append)
    check("…report.json is strict JSON: non-finite values stored as text", not bad_consts
          and rep.get("results", {}).get("min_gap") == "inf" and rep["results"].get("bad") == "nan"
          and rep["results"].get("ok") == 2.5, (bad_consts, rep.get("results")))
    res = worker("build", {"project_dir": pi, "views": []})
    check("…the reused revision builds again", res.get("ok") and "still r0001" in res.get("text", ""),
          res.get("error") or res.get("text"))
    res = worker("run_script", {"code": "import math\nl = [1]\nl.append(l)\nfc.result(x=math.inf, l=l)"})
    check("run_script with inf and a self-containing list in fc.result", res.get("ok")
          and '"x": "inf"' in res.get("text", ""), res.get("error") or res.get("text"))

    print("run_script edit report: spreadsheet cells, downstream expressions (round 4 #30)")
    usr = os.path.join(tmp, "inbox4", "UserPart.FCStd")
    os.makedirs(os.path.dirname(usr))
    res = worker("run_script", {"save_as": usr, "save": "no", "code": """s = doc.addObject("Spreadsheet::Sheet", "Spreadsheet")
s.set("A1", "length"); s.set("B1", "10"); s.setAlias("B1", "length"); s.set("B2", "=B1 * 2"); s.set("B3", "5")
b = fc.body("Body")
sk = fc.sketch(b, "XY", fc.rect(20, 20), name="Sk")
pad = fc.pad(b, sk, 10, name="Pad")
pad.setExpression("Length", "Spreadsheet.length")
pk = fc.pocket(b, fc.sketch(b, fc.face(b, "+Z"), fc.rect(4, 4), name="PkSk"), 2, name="Pk")
pk.setExpression("Length", "<<Spreadsheet>>.B2 / 10")
doc.recompute()
"""})
    check("user document with a Spreadsheet written", res.get("ok") and os.path.isfile(usr), res)
    res = worker("run_script", {"doc": usr, "save": "revision", "code": "doc.getObject('Spreadsheet').set('B1', '25')"})
    txt = res.get("text", "")
    ed = txt.split("edits    ", 1)[-1]
    head = ed.split("\n", 1)[0]
    check("the cell edit is listed as the edit", "  cell     Spreadsheet.B1 (length): 10 → 25" in ed, txt)
    check("…properties bound to it are downstream, not edits", "changed  Pad" not in ed and "changed  Pk" not in ed
          and re.search(r"Pad\.Length follows Spreadsheet\.length: 10(\.0)? mm → 25(\.0)? mm", ed)
          and re.search(r"Pk\.Length follows <<Spreadsheet>>\.B2 / 10: 2(\.0)? mm → 5(\.0)? mm", ed)
          and "Spreadsheet.B2 (=B1 * 2) follows: 20 → 50" in ed, txt)
    check("…the header claims only what is compared", "spreadsheet cells" in head
          and "every object's properties" not in head and "not shapes" in head, head)
    res = worker("run_script", {"doc": os.path.join(tmp, "inbox4", "UserPart.r01.FCStd"), "save": "revision",
                                "code": "doc.getObject('Spreadsheet').set('B3', '6')"})
    txt = res.get("text", "")
    check("a cell that drives nothing is still an edit (not 'edits none')", "cell     Spreadsheet.B3: 5 → 6" in txt
          and "edits    none" not in txt, txt)
    check("…and data.edits lists it", "cell     Spreadsheet.B3: 5 → 6" in (res.get("data") or {}).get("edits", []),
          res.get("data"))

    print("Δ hole positions in body coordinates, part moves (round 4 #40)")
    pt = project("thick4", """P = fc.params()
b = fc.body("Plate")
fc.pad(b, fc.sketch(b, "XY", fc.rect(60, 40)), P.thickness)
top = fc.face(b, "+Z")
fc.holes(b, top, fc.rect_pattern(P.px, 20), "M3 clearance", name="Mount")
fc.holes(b, top, [(0, P.cy)], "M3 clearance", name="One")
fc.holes(b, fc.face(b, "-Y"), [(P.sx, 2.5)], "Ø2", depth=5, name="Side")
""", {"process": "cnc"}, {"thickness": 5, "px": 40, "cy": 0, "sx": 0})
    worker("build", {"project_dir": pt, "views": []})
    write(os.path.join(pt, "params.json"), json.dumps({"thickness": 6, "px": 40, "cy": 0, "sx": 0}))
    txt = worker("build", {"project_dir": pt, "views": []}).get("text", "")
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("a thickness change does not report holes added/removed", dl and "Plate bbox z 5.00 → 6.00" in dl[0]
          and "added" not in dl[0] and "removed" not in dl[0] and "moved" not in dl[0], txt)
    write(os.path.join(pt, "params.json"), json.dumps({"thickness": 6, "px": 44, "cy": 5, "sx": 7}))
    txt = worker("build", {"project_dir": pt, "views": []}).get("text", "")
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("equal counts with new positions → 'moved' with the delta, in the plane's own axes", dl
          and "Plate One hole at (0, 0) moved +5 y" in dl[0] and "Plate Side hole at (0, 2.5) moved +7 x" in dl[0]
          and "Plate Mount holes moved (-20, -10) → (-22, -10), (-20, 10) → (-22, 10)" in dl[0]
          and "added" not in dl[0] and "removed" not in dl[0], txt)
    write(os.path.join(pt, "model.py"), open(os.path.join(pt, "model.py")).read().replace(
        "fc.rect_pattern(P.px, 20)", "fc.rect_pattern(P.px, 20, center=(3, 0))"))
    txt = worker("build", {"project_dir": pt, "views": []}).get("text", "")
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("…a uniform shift of every hole is one 'all N holes moved'", dl
          and "Plate Mount all 4 holes moved +3 x" in dl[0], txt)
    pg4 = project("asmgap4", """P = fc.params()
base = fc.body("Base")
fc.pad(base, fc.sketch(base, "XY", fc.rect(40, 30)), 8)
fc.set_frames(base, {"top": fc.frame(0, 0, 8, "+Z")})
cov = fc.body("Cover")
fc.pad(cov, fc.sketch(cov, "XY", fc.rect(30, 20)), 2)
fc.holes(cov, fc.face(cov, "+Z"), fc.rect_pattern(20, 10), "M3 clearance", name="CoverHoles")
fc.set_frames(cov, {"bottom": fc.frame(0, 0, 0, "+Z")})
asm = fc.assembly("Asm")
fc.add(asm, base, cov)
fc.ground(asm, base)
fc.joint(asm, "fixed", (base, "top"), (cov, "bottom"), offset=P.gap, angle=P.turn, name="Lid")
fc.solve(asm)
""", {"process": "cnc"}, {"gap": 0, "turn": 0})
    worker("build", {"project_dir": pg4, "views": []})
    write(os.path.join(pg4, "params.json"), json.dumps({"gap": 2, "turn": 0}))
    txt = worker("build", {"project_dir": pg4, "views": []}).get("text", "")
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("a part moved by its joint: '<part> moved +2 z', its holes unchanged", dl and "Cover moved +2 z" in dl[0]
          and "CoverHoles" not in dl[0] and "Base" not in dl[0], txt)
    write(os.path.join(pg4, "params.json"), json.dumps({"gap": 2, "turn": 90}))
    txt = worker("build", {"project_dir": pg4, "views": []}).get("text", "")
    dl = [ln for ln in txt.splitlines() if ln.startswith("Δ")]
    check("…turned by the joint: '<part> rotated 90° about +Z'", dl and "Cover rotated 90° about +Z" in dl[0]
          and "CoverHoles" not in dl[0], txt)

    print("files read through fclib are build inputs (round 4 #52)")
    far = os.path.join(tmp, "elsewhere", "far.brep")
    pv = project("vend4", """import json
with open(fc.path("data/size.json")) as fh:
    S = json.load(fh)
fc.ref_part("Motor", step="vendor/m.brep")
fc.ref_part("Far", step=%r)
b = fc.body("B")
fc.pad(b, fc.sketch(b, "XY", fc.rect(S["w"], 10)), 2)
""" % far, {"process": "cnc"})
    write(os.path.join(pv, "data", "size.json"), json.dumps({"w": 10}))
    os.makedirs(os.path.join(pv, "vendor"))
    os.makedirs(os.path.dirname(far))
    mk = "Part.makeBox(%d, %d, %d).exportBrep(%r)"
    worker("run_script", {"code": mk % (10, 20, 30, os.path.join(pv, "vendor", "m.brep")) + "\n" + mk % (5, 5, 5, far)})
    res = worker("build", {"project_dir": pv, "views": []})
    rep = json.load(open(os.path.join(pv, ".history", "r0001", "report.json")))
    reads = rep.get("reads") or {}
    check("report.json records the files read, project-relative inside, absolute outside, with sha1",
          res.get("ok") and set(reads) == {"data/size.json", "vendor/m.brep", far}
          and reads.get("vendor/m.brep") == sha1(os.path.join(pv, "vendor", "m.brep"))
          and (res.get("data") or {}).get("reads") == reads, (res.get("error"), reads))
    res = worker("build", {"project_dir": pv, "views": []})
    check("…nothing changed → same revision", "still r0001" in res.get("text", ""), res.get("text"))
    worker("run_script", {"code": mk % (10, 10, 20, os.path.join(pv, "vendor", "m.brep"))})
    txt = worker("build", {"project_dir": pv, "views": []}).get("text", "")
    check("a changed vendor file outside refs/ → new revision, named, with the Δ", "  r0002  " in txt.splitlines()[0]
          and "vendor/m.brep changed since r0001 (read by model.py) — new revision" in txt
          and "still r0001" not in txt and "Motor bbox z 30.00 → 20.00" in txt, txt)
    worker("run_script", {"code": mk % (6, 5, 5, far)})
    txt = worker("build", {"project_dir": pv, "views": []}).get("text", "")
    check("…an absolute path outside the project too", "  r0003  " in txt.splitlines()[0]
          and "far.brep changed since r0002" in txt, txt)
    write(os.path.join(pv, "data", "size.json"), json.dumps({"w": 12}))
    txt = worker("build", {"project_dir": pv, "views": []}).get("text", "")
    check("…and a data file opened through fc.path", "  r0004  " in txt.splitlines()[0]
          and "data/size.json changed since r0003" in txt, txt)
    rj = os.path.join(pv, ".history", "r0004", "report.json")
    rep = json.load(open(rj))
    rep.pop("reads", None)                      # as an older server wrote it
    json.dump(rep, open(rj, "w"))
    txt = worker("build", {"project_dir": pv, "views": []}).get("text", "")
    check("…an older revision that did not record the files read is not reused", "  r0005  " in txt.splitlines()[0]
          and "r0004 did not record the files model.py reads (data/size.json" in txt, txt)
    rj = os.path.join(tmp, "revs", ".history", "r0006", "report.json")
    rep = json.load(open(rj))
    rep.pop("reads", None)
    json.dump(rep, open(rj, "w"))
    txt = worker("build", {"project_dir": os.path.join(tmp, "revs"), "views": []}).get("text", "")
    check("…but a model that reads no extra files still reuses it", "still r0006" in txt, txt)

    print("last_build.json is keyed inside the project (round 4 #11)")
    pm = project("movable", """b = fc.body("B")
fc.pad(b, fc.sketch(b, "XY", fc.rect(10, 10)), 2)
m = fc.ref_part("M", shape=Part.makeBox(5, 5, 5), frames={"f": fc.frame(0, 0, 0, "+Z")})
fc.mate(m, "f", fc.face(b, "+Z"), mode="against", rotate=33)
""", {"process": "cnc"})
    worker("build", {"project_dir": pm, "views": []})
    lb = json.load(open(os.path.join(pm, ".history", "last_build.json")))
    check("key is the build file's path inside the project", list(lb) == ["build/movable.FCStd"], lb)
    moved = os.path.join(tmp, "moved", "movable")
    shutil.copytree(pm, moved)
    shutil.rmtree(pm)
    txt = worker("build", {"project_dir": moved, "views": []}).get("text", "")
    check("a moved project is not 'edited outside the build loop'", "still r0001" in txt
          and "outside the build loop" not in txt, txt)
    # an older server's absolute key, then the project moves again
    lbp = os.path.join(moved, ".history", "last_build.json")
    lb = json.load(open(lbp))
    with open(lbp, "w") as fh:
        json.dump({os.path.join(moved, "build", "movable.FCStd"): lb["build/movable.FCStd"]}, fh)
    again = os.path.join(tmp, "moved2")
    shutil.copytree(moved, again)
    txt = worker("build", {"project_dir": again, "views": []}).get("text", "")
    lb = json.load(open(os.path.join(again, ".history", "last_build.json")))
    check("…an older absolute key is still recognised by its sha1, and replaced by the relative key",
          "still r0001" in txt and "outside the build loop" not in txt and list(lb) == ["build/movable.FCStd"],
          (txt, lb))

    c.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
