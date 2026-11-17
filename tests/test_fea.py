#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M5 tests: FEA (`analyze`) against answers known from theory — a cantilever's tip
deflection and root stress, its first natural frequency, the Euler buckling load of a
fixed-free column, two blocks bonded on their shared face against one solid block —
plus equilibrium, convergence (and when the refined solve is skipped), load cases in
fea.json, fea_* checks going stale when the model changes, and release re-solving
stale cases.

    python3 tests/test_fea.py
"""
import json
import math
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcp_client import Client, check, fails  # noqa: E402

BEAM = '''P = fc.params()
body = fc.body("Beam")
fc.pad(body, fc.sketch(body, "YZ", fc.rect(P.w, P.h, center=(0, P.h / 2)), name="BeamSketch"), P.length)
'''
PLATE = '''P = fc.params()
body = fc.body("Plate")
fc.pad(body, fc.sketch(body, "XY", fc.rect(80, 30), name="PlateSketch"), 5)
fc.holes(body, on=fc.face(body, "+Z"), at=[(-30, -8), (-30, 8)], spec="M5 clearance normal")
shanks = Part.makeCompound([Part.makeCylinder(2.5, 12, App.Vector(-30, y, -4)) for y in (-8, 8)])
fc.ref_part("Screws", shape=shanks, bom="context", spec="ISO 4762 M5×12")
'''
COLUMN = '''P = fc.params()
body = fc.body("Column")
fc.pad(body, fc.sketch(body, "XY", fc.rect(P.w, P.w), name="ColumnSketch"), P.length)
'''
# the 10 × 10 × 100 cantilever as two 5 mm layers, one on the other
STACK = '''P = fc.params()
lower = fc.body("Lower")
fc.pad(lower, fc.sketch(lower, "YZ", fc.rect(10, 5, center=(0, 2.5)), name="LowerSketch"), 100)
upper = fc.body("Upper")
fc.pad(upper, fc.sketch(upper, "YZ", fc.rect(10, P.t_up, center=(0, 5 + P.t_up / 2)), name="UpperSketch"), 100)
'''


SHELF = """P = fc.params()
body = fc.body("ShelfBracket")
pts = [(0, -P.leg_h), (P.leg_t, -P.leg_h), (P.leg_t, 0), (P.arm_l, 0), (P.arm_l, P.arm_h), (0, P.arm_h)]
fc.pad(body, fc.sketch(body, "YZ", fc.polygon(pts), name="ProfileSketch"), P.width)
fc.holes(body, on=fc.face(body, "-Y"), at=[(P.width / 2, -15), (P.width / 2, -15 - P.hole_pitch)],
         spec="M5 clearance normal")
fc.fillet(body, fc.edges(body, kind="line", parallel="X", at=0, axis="Z"), P.corner_r)
"""
# a face slanted across a 40 mm cube (its bounding box is the whole cube) and a grid of points in the cube:
# which points lie within 5 mm of the face, against their distance to the plane x = z
ZONE = '''import numpy as np
import fea
f = Part.Face(Part.makePolygon([App.Vector(0, 0, 0), App.Vector(40, 0, 40), App.Vector(40, 40, 40),
                                App.Vector(0, 40, 0), App.Vector(0, 0, 0)]))
g = np.mgrid[0:41:2, 0:41:2, 0:41:2].reshape(3, -1).T.astype(float)
near = fea._within(g, fea._sample_faces([f], 5.0 / 6), 5.0)
d = np.abs(g[:, 0] - g[:, 2]) / 2 ** 0.5
fc.result(points=len(g), near=int(near.sum()), wrong=int((near != (d <= 5.0)).sum()))
'''
# a thick tube meshed twice at the size its refined solve uses (curved elements folded over there, and every run
# gave another mesh), then once with every curved element counted as folded, so that all are straightened
MESH = '''import types
import numpy as np
import fea
prefs = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Gmsh")
before = prefs.GetInts()
tube = Part.makeCylinder(20, 80).cut(Part.makeCylinder(10, 80))
def model():
    return types.SimpleNamespace(labels=["Tube"], parts=[None], solid_owner=[0],
                                 compound=Part.makeCompound(tube.Solids))
a, b = fea.Mesh(model(), 3.333), fea.Mesh(model(), 3.333)
fea.MIN_JACOBIAN = 0.999
c = fea.Mesh(model(), 5.0)
fc.result(same=bool(a.nodes == b.nodes and np.array_equal(a.xyz, b.xyz)), quality=a.quality, forced=c.quality,
          prefs_kept=before == prefs.GetInts())
'''
ROOT = os.path.dirname(HERE)
E_6061, RHO_6061 = 68900.0, 2.70e-9          # MPa, t/mm³ — the material table's 6061-T6
TIMES = {}


def cli(ws, tool, args):
    import subprocess
    p = subprocess.run([sys.executable, os.path.join(ROOT, "evals", "mcp_cli.py"), "--home", ws, tool,
                        json.dumps(args)], capture_output=True, text=True, timeout=900)
    return p.stdout


def grade(cid, ws):
    import subprocess
    out = subprocess.run([sys.executable, os.path.join(ROOT, "evals", "run_case.py"), "grade", cid, ws],
                         capture_output=True, text=True, timeout=1800).stdout
    return json.loads(out[out.find("{"):])


def shelf_solution(arm_h):
    """Scripted run of the shelf_bracket eval case; arm_h 12 meets it, 6 sags too far."""
    import subprocess
    ws = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp_shelf_"))
    subprocess.run([sys.executable, os.path.join(ROOT, "evals", "run_case.py"), "setup", "shelf_bracket", ws],
                   capture_output=True, timeout=600)
    cli(ws, "create_project", {"name": "shelf_bracket", "template": "part", "process": "fdm", "material": "PETG"})
    pd = os.path.join(ws, "shelf_bracket")
    open(os.path.join(pd, "model.py"), "w").write(SHELF)
    json.dump(params(arm_l=120, width=30, arm_h=arm_h, leg_t=8, leg_h=70, hole_pitch=40, corner_r=4),
              open(os.path.join(pd, "params.json"), "w"))
    json.dump([{"id": "strength", "type": "fea_safety", "case": "shelf", "min": 3},
               {"id": "sag", "type": "fea_displacement", "case": "shelf", "max": 2}],
              open(os.path.join(pd, "checks.json"), "w"))
    pj = json.load(open(os.path.join(pd, "project.json")))
    pj["parts"]["ShelfBracket"]["qty"] = 4
    json.dump(pj, open(os.path.join(pd, "project.json"), "w"), indent=2)
    a = cli(ws, "analyze", {"project": "shelf_bracket", "case": "shelf", "fixed": ["ShelfBracket.face(-Y)"],
                            "loads": [{"ref": "ShelfBracket.face(+Z)", "force": 50, "direction": "-Z"}]})
    r = cli(ws, "release", {"project": "shelf_bracket", "drawings": "none"})
    return grade("shelf_bracket", ws), a, r


def params(**kw):
    return {k: {"value": v, "unit": "mm", "source": "test"} for k, v in kw.items()}


def num(pat, txt):
    m = re.search(pat, txt)
    return float(m.group(1).replace(" ", "")) if m else None


def timed(c, key, args, timeout=900):
    t = time.time()
    out = c.tool("analyze", args, timeout=timeout)
    TIMES[key] = time.time() - t
    return out


def result(pdir, case):
    return json.load(open(os.path.join(pdir, "out", "fea", case, "result.json")))


def script_result(txt):
    """The fc.result(...) JSON block of a run_script reply."""
    m = re.search(r"^result\n((?:  .*\n?)+)", txt, re.M)
    return json.loads(m.group(1)) if m else {}


def project(c, home, name, model, prm, material="6061-T6"):
    c.tool("create_project", {"name": name, "template": "part", "process": "cnc", "material": material})
    pdir = os.path.join(home, name)
    open(os.path.join(pdir, "model.py"), "w").write(model)
    json.dump(prm, open(os.path.join(pdir, "params.json"), "w"))
    json.dump([], open(os.path.join(pdir, "checks.json"), "w"))
    return pdir


def main():
    home = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp_m5_"))
    c = Client(home)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}})
    tools = {t["name"]: t for t in c.rpc("tools/list")["result"]["tools"]}
    check("analyze is a tool", "analyze" in tools, list(tools))
    props = tools["analyze"]["inputSchema"]["properties"]
    check("analyze takes analysis, modes, parts and bonded", all(k in props for k in ("analysis", "modes", "parts",
                                                                                      "bonded")), sorted(props))
    check("drawing takes section auto|full|none (passed through to the drawing worker)",
          tools["drawing"]["inputSchema"]["properties"].get("section", {}).get("enum") == ["auto", "full", "none"],
          tools["drawing"]["inputSchema"]["properties"].get("section"))

    pdir = project(c, home, "beam", BEAM, params(length=100, w=10, h=10))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("cantilever builds", not err and "BUILD ✓" in txt, txt)

    print("cantilever, 100 N at the tip (E 68.9 GPa, 10 × 10 × 100)")
    err, txt, content = timed(c, "static beam", {"project": "beam", "case": "tip", "fixed": ["Beam.face(-X)"],
                                                 "loads": [{"ref": "Beam.face(+X)", "force": 100,
                                                            "direction": "-Z"}]})
    check("analyze runs", not err and txt.startswith("ANALYZE Beam"), txt)
    # Euler–Bernoulli FL³/3EI = 0.5805, + shear ≈ 0.0046 → 0.585
    d = num(r"max displacement ([\d.]+) mm", txt)
    check("tip deflection 0.58 mm (beam theory 0.5805–0.585)", d and 0.570 <= d <= 0.595, txt)
    s = num(r"max von Mises ([\d.]+) MPa", txt)
    check("root bending stress ≈ 60 MPa (M·c/I)", s and 54 <= s <= 70, txt)
    sa = num(r"away from the supports: ([\d.]+) MPa", txt)
    check("…away from the support ≈ 57 MPa (theory at x≈4.5: 57.3)", sa and 52 <= sa <= 62, txt)
    check("…and says how far from the support that is: the part's thinnest wall, at most 2/3 of its thinnest "
          "dimension", "(over 6.67 mm from a support)" in txt, txt)
    check("safety factor against 276 MPa yield", re.search(r"safety factor 4\.\d+ against yield 276", txt), txt)
    check("reactions balance the applied load", "✓ in equilibrium" in txt and "100.00) N" in txt, txt)
    check("refined mesh: converged (a small first mesh is re-solved finer by default)", "converged ✓" in txt, txt)
    check("material from project.json with its source", "6061-T6: E 68900 MPa" in txt and "material table" in txt,
          txt)
    check("stress / deflection image returned", any(x["type"] == "image" for x in content), [x["type"] for x in content])
    res = result(pdir, "tip")
    check("result.json has the evidence", res["case"] == "tip" and res["inputs_sha1"] and res["case_hash"]
          and abs(res["max_disp"] - d) < 1e-3 and res.get("analysis") == "static" and res.get("threads", 0) >= 1,
          res.keys())
    check("…with the analysis method and the programs behind it, as the report says",
          isinstance(res.get("method"), int) and (res.get("versions") or {}).get("calculix") and re.search(
              r"solver    CalculiX [\d.]+, gmsh [\d.]+, FreeCAD [\d.]+ \(analysis method %s\)" % res.get("method"),
              txt), (res.get("method"), res.get("versions"), txt))
    check("principal stresses: the largest tension is on top of the cantilever and the largest compression below, "
          "equal in size",
          res["max_principal_at"][2] > 9 and res["min_principal_at"][2] < 1
          and abs(res["max_principal"] + res["min_principal"]) < 0.15 * res["max_principal"]
          and re.search(r"principal largest tension [\d.]+ MPa at \(.*\) · largest compression -[\d.]+ MPa", txt)
          and res.get("criterion") == "von Mises" and res["max_stress"] == res["max_vm"], (res, txt))
    check("…and the mesh's worst element: the scaled Jacobian (1 is undistorted, 0 folded over)",
          re.search(r"mesh      tet10, .* · scaled Jacobian ≥ [01]\.\d+", txt)
          and res.get("mesh_quality", {}).get("min_jacobian", 0) > 0.3 and res["mesh_quality"]["straightened"] == 0,
          (res.get("mesh_quality"), txt))
    single = res["max_disp"]
    fj = json.load(open(os.path.join(pdir, "fea.json")))
    check("case saved in fea.json", fj["cases"]["tip"]["fixed"] == ["Beam.face(-X)"], fj)

    err, txt2, _ = c.tool("analyze", {"project": "beam", "case": "tip"})
    check("saved case re-runs by name", not err and "case 'tip'" in txt2 and "converged ✓" in txt2, txt2)
    before = result(pdir, "tip")
    err, txt3, _ = c.tool("analyze", {"project": "beam", "case": "tip", "params": {"h": 14}, "refine": False})
    after = result(pdir, "tip")
    wi = result(pdir, "tip__whatif")
    check("what-if params: solved as a variant, stiffer, case result and params.json untouched",
          not err and "what-if   params h=14" in txt3 and wi["max_disp"] < 0.5 * before["max_disp"]
          and after == before and json.load(open(os.path.join(pdir, "params.json")))["h"]["value"] == 10,
          txt3[:600])
    err, txt4, _ = c.tool("lookup_standard", {"material": "PETG"})
    check("lookup_standard(material) gives the values analyze uses", not err and "E 2100 MPa" in txt4
          and "yield 50 MPa" in txt4, txt4)

    print("the zone away from the supports")
    tip = {"project": "beam", "case": "margin", "fixed": ["Beam.face(-X)"], "refine": False, "save": False,
           "loads": [{"ref": "Beam.face(+X)", "force": 100, "direction": "-Z"}]}
    err, txt, _ = c.tool("analyze", dict(tip, support_margin=12))
    sa12 = num(r"away from the supports: ([\d.]+) MPa", txt)
    check("support_margin=12 moves it: M·c/I at x = 12 is 52.8 MPa", not err and sa12
          and abs(sa12 - 52.8) / 52.8 < 0.02
          and "(over 12.00 mm from a support)" in txt, txt)
    r12 = result(pdir, "margin") if not err else {}
    err, txt, _ = c.tool("analyze", dict(tip, support_margin=12))
    again = result(pdir, "margin") if not err else {}
    check("the same case twice gives the same mesh and the same numbers", r12.get("nodes") and all(
        r12.get(k) == again.get(k) for k in ("nodes", "elements", "max_vm", "max_disp", "max_stress_away")),
        [(r12.get(k), again.get(k)) for k in ("nodes", "max_vm", "max_disp")])
    err, txt, _ = c.tool("analyze", dict(tip, support_margin=0))
    check("support_margin=0 → refused", err and "support_margin must be" in txt, txt)
    err, txt, _ = c.tool("run_script", {"code": ZONE})
    z = script_result(txt)
    check("the zone is measured to the support face itself: every point of a grid within 5 mm of a slanted face, and "
          "no other (the face's bounding box would take all %s)" % z.get("points"),
          not err and z.get("wrong") == 0 and z.get("near") == 2835 and z.get("points") == 9261, txt)

    print("the mesh: repeatable, and no element folded over")
    err, txt, _ = c.tool("run_script", {"code": MESH}, timeout=600)
    z = script_result(txt)
    check("a curved part meshes the same on every run, with FreeCAD's gmsh preferences left as they were",
          not err and z.get("same") is True and z.get("prefs_kept") is True, txt)
    check("…and no curved element folds over (scaled Jacobian %s)" % (z.get("quality") or {}).get("min_jacobian"),
          (z.get("quality") or {}).get("min_jacobian", 0) > 0.3 and z["quality"]["straightened"] == 0, z)
    check("…elements that do fold over get straight edges (%s here, forced)" % (z.get("forced") or {}).get(
        "straightened"), (z.get("forced") or {}).get("straightened", 0) > 500
        and z["forced"]["min_jacobian"] >= 0.999, z)

    print("a brittle material is judged on its principal stresses")
    light = dict(tip, case="brittle", loads=[{"ref": "Beam.face(+X)", "force": 10, "direction": "-Z"}])
    err, txt, _ = c.tool("analyze", dict(light, material="resin"))
    br = result(pdir, "brittle") if not err else {}
    big = max(abs(br.get("max_principal", 0)), abs(br.get("min_principal", 0)))
    check("resin is brittle: judged on its largest principal stress against the breaking stress, and says so",
          not err and br.get("criterion") == "principal" and abs(br["max_stress"] - big) < 1e-9
          and abs(br["safety_factor"] - 50 / big) < 1e-6 and "resin: E 2500 MPa, ν 0.380, brittle, breaking stress "
          "50 MPa" in txt and re.search(r"stress    max principal stress [\d.]+ MPa at .* against the breaking "
                                        r"stress 50 MPa", txt) and "not on von Mises" in txt, txt)
    check("…5.6 MPa away from the support, as the bending stress there (M·c/I at x = 6.67)",
          abs((br.get("max_stress_away") or 0) - 5.6) < 0.15, br.get("max_stress_away"))
    err, txt, _ = c.tool("analyze", dict(light, material={"E": 2500, "nu": 0.38, "yield": 50}))
    check("the same values without the flag → von Mises against yield", not err
          and result(pdir, "brittle").get("criterion") == "von Mises" and "against yield 50 MPa" in txt, txt)
    err, txt, _ = c.tool("analyze", dict(light, material={"E": 2500, "nu": 0.38, "yield": 50, "brittle": True}))
    check("material={…, \"brittle\": true} → the principal stress", not err
          and result(pdir, "brittle").get("criterion") == "principal" and "max principal stress" in txt, txt)
    err, txt, _ = c.tool("lookup_standard", {"material": "acrylic"})
    check("lookup_standard(material) says which materials are brittle", not err and "brittle: analyze judges it on "
          "its largest principal stress" in txt, txt)

    print("outside the range of a linear analysis")
    held = {"project": "beam", "fixed": ["Beam.face(-X)"]}
    err, txt, _ = c.tool("analyze", dict(held, case="overload", loads=[
        {"ref": "Beam.face(+X)", "force": 600, "direction": "-Z"}]))
    over = result(pdir, "overload") if not err else {}
    check("600 N: the beam yields away from the support, and the reply says its numbers are no longer real",
          not err and over.get("beyond_yield") is True and over.get("large_deflection") is False
          and "Beam yields (" in txt and "the deflection is larger than shown" in txt, txt)
    err, txt, _ = c.tool("analyze", dict(held, case="soft", material="PETG", loads=[
        {"ref": "Beam.face(+X)", "force": 30, "direction": "-Z"}]))
    soft = result(pdir, "soft") if not err else {}
    check("a PETG beam bent by 6 % of its length: a large deflection, for which the result is approximate",
          not err and soft.get("large_deflection") is True and soft.get("beyond_yield") is False
          and "is 6 % of the longest dimension (100.0 mm)" in txt, txt)
    json.dump([{"id": "sag-over", "type": "fea_displacement", "case": "overload", "max": 10},
               {"id": "sag-soft", "type": "fea_displacement", "case": "soft", "max": 10}],
              open(os.path.join(pdir, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("a deflection check on a part that yields fails, however small the linear deflection",
          re.search(r"✗ sag-over\s+fea_displacement\s+3\.\d+ mm \(≤ 10\.0000\), beyond yield", txt)
          and "the real deflection is larger" in txt, txt)
    check("…and one on a large deflection passes with the caveat", re.search(
        r"✓ sag-soft\s+fea_displacement\s+5\.\d+ mm \(≤ 10\.0000\)   max displacement, case 'soft'; a large "
        r"deflection", txt), txt)
    json.dump([], open(os.path.join(pdir, "checks.json"), "w"))

    print("uniform pressure 0.1 MPa on the top face (w = 1 N/mm)")
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "press", "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "Beam.face(+Z)", "pressure": 0.1}]})
    d = num(r"max displacement ([\d.]+) mm", txt)
    check("UDL deflection wL⁴/8EI = 0.2177 mm", d and abs(d - 0.2177) / 0.2177 < 0.03, txt)
    s = num(r"max von Mises ([\d.]+) MPa", txt)
    check("UDL root stress wL²/2·c/I = 30 MPa", s and 26 <= s <= 36, txt)
    check("pressure pushes on the face: reactions balance", "✓ in equilibrium" in txt, txt)

    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "weight", "fixed": ["Beam.face(-X)"],
                                     "gravity": True, "refine": False})
    check("self weight 0.265 N, in equilibrium", "self weight 0.265 N" in txt and "✓ in equilibrium" in txt, txt)

    print("natural frequencies of the cantilever (F1)")
    err, txt, content = timed(c, "frequency beam", {"project": "beam", "case": "modes", "analysis": "frequency",
                                                    "fixed": ["Beam.face(-X)"]})
    I, A, L = 10 * 10 ** 3 / 12.0, 100.0, 100.0
    f_theory = 1.875 ** 2 / (2 * math.pi) * math.sqrt(E_6061 * I / (RHO_6061 * A * L ** 4))     # 815.9 Hz
    check("frequency analysis runs", not err and "(natural frequencies, CalculiX)" in txt
          and "density 2.70 g/cm³" in txt, txt)
    res = result(pdir, "modes") if not err else {}
    f = res.get("frequencies_hz") or [0, 0]
    check("first bending frequency %.0f Hz within 2 %% of 1.875²/2π·√(EI/ρAL⁴) = %.1f Hz (shear and rotary "
          "inertia make it slightly lower)" % (f[0], f_theory), abs(f[0] - f_theory) / f_theory < 0.02, txt)
    check("…the square section bends equally both ways: f1 ≈ f2, and 6 modes by default",
          len(f) == 6 and abs(f[1] - f[0]) / f[0] < 0.005, f)
    check("…the second bending pair below 6.27 × f1 (Timoshenko), the axial mode at √(E/ρ)/4L ≈ 12.6 kHz",
          4.5 * f[0] < f[2] < 6.3 * f[0] and any(abs(x - 12630) / 12630 < 0.02 for x in f), f)
    check("…converged on a finer mesh, mode shapes plotted", "finer mesh changes f1" in txt and "✓" in txt
          and any(x["type"] == "image" for x in content), txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "modes2", "analysis": "frequency", "modes": 2,
                                     "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "Beam.face(+X)", "force": 100, "direction": "-Z"}],
                                     "refine": False, "save": False})
    check("modes=2 → two frequencies; loads are said to be unused", not err and len(re.findall(
        r"\d Hz \(first 2\)", txt)) == 1 and "loads are not used" in txt, txt)

    print("errors")
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "loads": [{"ref": "Beam.face(+X)",
                                                                                   "force": 10, "direction": "-Z"}]})
    check("no supports → says what to give", err and "no supports" in txt and "fixed=" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "Beam.face(+X)", "force": 10}]})
    check("force without direction → says how", err and "direction" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "[0, 0, 0]", "force": 10, "direction": "-Z"}]})
    check("a point is not a load surface → says faces or holes", err and "faces or holes" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "Beam.face(-X)", "force": 10, "direction": "-Z"}]})
    check("a load on a support face → refused", err and "also a support" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "analysis": "modal",
                                     "fixed": ["Beam.face(-X)"]})
    check("an unknown analysis type → names the three", err and "static" in txt and "frequency" in txt
          and "buckling" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "analysis": "buckling",
                                     "fixed": ["Beam.face(-X)"]})
    check("buckling without loads → says loads are needed", err and "no loads" in txt and "buckl" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "bad", "part": "Beam", "fixed": ["Beam.face(-X)"],
                                     "loads": [{"ref": "Beam.face(+X)", "force": 10, "direction": "-Z"}],
                                     "bonded": [["Beam.face(+Z)", "Beam.face(-Z)"]]})
    check("bonded with one part → says to name the parts", err and "parts=" in txt, txt)
    fj = json.load(open(os.path.join(pdir, "fea.json")))
    check("failed cases are not saved", "bad" not in fj["cases"], list(fj["cases"]))

    print("bolted plate: holes as supports")
    ppd = project(c, home, "plate", PLATE, {}, material="5052 aluminium")
    err, txt, _ = c.tool("analyze", {"project": "plate", "fixed": ["Plate.holes(d=5.5)"],
                                     "loads": [{"ref": "Plate.face(+X)", "force": [0, 0, -20]}]})
    check("bolt holes as supports, vector force, equilibrium",
          not err and "Plate.holes(d=5.5)" in txt and "✓ in equilibrium" in txt and "5052" in txt, txt)
    check("…the peak at the rim of a rigidly held hole keeps growing with the mesh; the stress away from the "
          "holes and the sag have converged", re.search(r"peak stress \d+\.\d % ✗ · stress away from supports "
                                                        r"\d\.\d % ✓", txt) and "displacement 0." in txt, txt)
    json.dump([{"id": "shank-gap", "type": "radial_gap", "a": "Plate.holes(d=5.5)", "b": "Screws.bosses(d=5)",
                "min": 0.2},
               {"id": "rim", "type": "fea_stress", "max": 100},
               {"id": "rim-accepted", "type": "fea_stress", "max": 100, "accept_unconverged": True},
               {"id": "body", "type": "fea_safety", "min": 3, "away_from_supports": True},
               {"id": "sag", "type": "fea_displacement", "max": 0.2}], open(os.path.join(ppd, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("radial_gap against a pattern of shanks pairs each hole with its own", re.search(
        r"✓ shank-gap\s+radial_gap\s+0\.250", txt), txt)
    check("a check on a stress that has not converged fails, however far inside its limit, and says what to do",
          re.search(r"✗ rim\s+fea_stress\s+[\d.]+ MPa \(≤ 100\.000\), not converged", txt)
          and "the peak stress changed" in txt and "away_from_supports" in txt and "accept_unconverged" in txt, txt)
    check("…\"accept_unconverged\": true lets it pass, and the value says it was accepted",
          re.search(r"✓ rim-accepted\s+fea_stress\s+[\d.]+ MPa \(≤ 100\.000\), not converged \(accepted\)", txt),
          txt)
    check("…checks on what did converge pass: the stress away from the supports and the displacement",
          re.search(r"✓ body\s+fea_safety\s+[\d.]+ \(≥ 3\.00\)   max von Mises away", txt)
          and re.search(r"✓ sag\s+fea_displacement\s+0\.\d+ mm \(≤ 0\.2000\)   max", txt), txt)
    json.dump([{"id": "shank-gap", "type": "radial_gap", "a": "Plate.holes(d=5.5)", "b": "Screws.bosses(d=5)",
                "min": 0.2}], open(os.path.join(ppd, "checks.json"), "w"))
    err, txt, _ = c.tool("check_fit", {"project": "plate", "contact_ok": ["Plate|Screws"], "render": False})
    check("contact_ok takes 'A|B' pairs as well as lists", not err, txt[:400])
    pj = json.load(open(os.path.join(ppd, "project.json")))
    pj["parts"]["Plate"]["process"] = "fdm"
    pj["parts"]["Plate"]["print_up"] = "+Y"
    json.dump(pj, open(os.path.join(ppd, "project.json"), "w"), indent=2)
    err, txt, _ = c.tool("check_manufacturing", {"project": "plate"})
    check("print_up in project.json sets the print orientation", not err and "parts.Plate.print_up +Y" in txt
          and "the face that points -Y in the model down on the bed" in txt, txt)

    print("Euler buckling of a fixed-free column (F1)")
    cpd = project(c, home, "column", COLUMN, params(w=10, length=200))
    err, txt, content = timed(c, "buckling column", {
        "project": "column", "case": "buckle", "analysis": "buckling", "fixed": ["Column.face(-Z)"],
        "loads": [{"ref": "Column.face(+Z)", "force": 100, "direction": "-Z"}]})
    p_cr = math.pi ** 2 * E_6061 * (10 * 10 ** 3 / 12.0) / (4 * 200.0 ** 2)          # 3542 N
    lam = (result(cpd, "buckle") if not err else {}).get("buckling_factor") or 0
    check("buckling factor %.2f within 3 %% of Euler π²EI/(4L²) / 100 N = %.2f" % (lam, p_cr / 100),
          abs(lam - p_cr / 100) / (p_cr / 100) < 0.03, txt)
    check("…reported as 'buckles at N × the load' with the mode shape",
          re.search(r"buckles at 3\d\.\d\d × the load", txt) and any(x["type"] == "image" for x in content), txt)
    json.dump([{"id": "no-buckle", "type": "fea_buckling", "case": "buckle", "min": 10},
               {"id": "no-buckle-x50", "type": "fea_buckling", "case": "buckle", "min": 50}],
              open(os.path.join(cpd, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "column", "views": []})
    check("fea_buckling passes at min 10, fails at min 50",
          re.search(r"✓ no-buckle\s+fea_buckling\s+3\d\.\d\d \(≥ 10", txt)
          and re.search(r"✗ no-buckle-x50\s+fea_buckling\s+3\d\.\d\d \(≥ 50", txt), txt)

    print("two blocks bonded on their shared face vs one solid block (F2)")
    spd = project(c, home, "stack", STACK, params(t_up=5))
    end_load = [{"ref": "Lower.face(+X)", "force": 50, "direction": "-Z"},
                {"ref": "Upper.face(+X)", "force": 50, "direction": "-Z"}]
    err, txt, content = timed(c, "bonded stack", {
        "project": "stack", "case": "bonded", "parts": ["Lower", "Upper"],
        "bonded": [["Lower.face(+Z)", "Upper.face(-Z)"]], "fixed": ["Lower.face(-X)", "Upper.face(-X)"],
        "loads": end_load})
    res = result(spd, "bonded") if not err else {}
    db = res.get("max_disp") or 0
    check("bonded layers deflect like the one solid block: %.4f vs %.4f mm (within 3 %%)" % (db, single),
          abs(db - single) / single < 0.03, txt)
    check("…tied, in equilibrium, results per part", "bonded    Lower.face(+Z) ↔ Upper.face(-Z) — tied" in txt
          and "✓ in equilibrium" in txt and "part      Lower:" in txt and "part      Upper:" in txt
          and len(res.get("per_part") or []) == 2 and res.get("parts") == ["Lower", "Upper"], txt)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "loose", "parts": ["Lower", "Upper"],
                                     "fixed": ["Lower.face(-X)", "Upper.face(-X)"], "loads": end_load,
                                     "refine": False, "save": False})
    dl = (num(r"max displacement ([\d.]+) mm", txt) or 0)
    check("…without the bond the two layers bend on their own: ≈4 × the deflection (%.3f mm)" % dl,
          not err and 3.6 < dl / single < 4.4, txt)
    # each part its own material: an aluminium layer under a PETG one is a composite beam
    pj_path = os.path.join(spd, "project.json")
    pj0 = open(pj_path).read()
    pj = json.loads(pj0)
    pj.setdefault("parts", {}).setdefault("Upper", {})["material"] = "PETG"
    json.dump(pj, open(pj_path, "w"), indent=2)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "bimat", "parts": ["Lower", "Upper"],
                                     "bonded": [["Lower.face(+Z)", "Upper.face(-Z)"]],
                                     "fixed": ["Lower.face(-X)", "Upper.face(-X)"], "loads": end_load,
                                     "mesh_size": 2.5, "save": False})
    open(pj_path, "w").write(pj0)
    zna = (E_6061 * 2.5 + 2100 * 7.5) / (E_6061 + 2100)
    ei = sum(e * (10 * 5 ** 3 / 12.0 + 50 * (z - zna) ** 2) for e, z in ((E_6061, 2.5), (2100, 7.5)))
    dc, dm = 100 * 100 ** 3 / (3 * ei), num(r"max displacement ([\d.]+) mm", txt) or 0
    check("6061 under PETG, bonded: %.3f mm vs %.3f mm from the transformed section (within 3 %%), each part "
          "against its own yield" % (dm, dc), not err and abs(dm - dc) / dc < 0.03 and "Upper PETG" in txt
          and "against yield 50 MPa" in txt and "against yield 276 MPa" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "bad", "parts": ["Lower", "Upper"],
                                     "bonded": [["Lower.face(+Z)", "Lower.face(-Z)"]],
                                     "fixed": ["Lower.face(-X)"], "loads": end_load})
    check("bonded faces on one part → refused", err and "both are on Lower" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "bad", "parts": ["Lower", "Upper"],
                                     "bonded": [["Lower.face(-Z)", "Upper.face(+Z)"]],
                                     "fixed": ["Lower.face(-X)"], "loads": end_load})
    check("bonded faces that don't touch → says how far apart", err and "10.00 mm apart" in txt
          and "must touch" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "bad", "parts": ["Lower", "Upper"],
                                     "fixed": ["Lower.face(-X)"], "loads": end_load})
    check("a part neither supported nor bonded → named", err and "Upper is neither supported nor bonded" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "stack", "case": "bad", "part": "Lower",
                                     "fixed": ["Lower.face(-X)"],
                                     "loads": [{"ref": "Upper.face(+X)", "force": 10, "direction": "-Z"}]})
    check("a load on a part outside the case → says to analyse them together", err and "parts=[" in txt, txt)
    json.dump([{"id": "assy-strength", "type": "fea_safety", "case": "bonded", "min": 2, "away_from_supports": True},
               {"id": "upper-sag", "type": "fea_displacement", "case": "bonded", "part": "Upper", "max": 0.5}],
              open(os.path.join(spd, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "stack", "views": []})
    check("multi-part fea checks: safety over every part (away from the supports) passes, a part's sag over its "
          "limit fails",
          re.search(r"✓ assy-strength\s+fea_safety\s+\d\.\d\d \(≥ 2", txt)
          and re.search(r"✗ upper-sag\s+fea_displacement\s+0\.5\d+ mm \(≤ 0\.5", txt) and "in Upper" in txt, txt)
    json.dump(params(t_up=6), open(os.path.join(spd, "params.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "stack", "views": []})
    check("changing one part of the assembly makes its result stale", "stale" in txt
          and "analyze(case='bonded')" in txt, txt)

    print("speed: a large first mesh is compared with a coarser one, not with a finer one (F3)")
    err, txt, _ = timed(c, "large first mesh (auto)", {"project": "beam", "case": "fine", "fixed": ["Beam.face(-X)"],
                                                       "loads": [{"ref": "Beam.face(+X)", "force": 100,
                                                                  "direction": "-Z"}],
                                                       "mesh_size": 1.0, "save": False})
    nodes = num(r"a finer one than these ([\d ]+) nodes takes minutes", txt)
    check("…says so, with the node counts and how to get the finer one", not err and nodes and nodes > 50000
          and "convergence is shown against a coarser mesh (1.50 mm, " in txt and "refine=true solves it" in txt
          and re.search(r"at 1\.00 mm \(also solved at 1\.50 mm: [\d ]+ nodes\)", txt), txt)
    check("…and convergence is still shown: the 1 mm mesh against the 1.5 mm one", re.search(
        r"converge  finer mesh changes displacement 0\.\d % ✓", txt), txt)
    d = num(r"max displacement ([\d.]+) mm", txt)
    check("…the result is accurate (within 1 % of the converged tip deflection)",
          d and abs(d - single) / single < 0.01, txt)
    sa1 = num(r"away from the supports: ([\d.]+) MPa", txt)
    check("…with the same stress away from the support (%s vs %s MPa): the zone comes from the part, not the mesh"
          % (sa1, sa), sa1 and abs(sa1 - sa) / sa < 0.015, txt)

    print("fea checks in checks.json")
    json.dump([{"id": "strength", "type": "fea_safety", "case": "tip", "min": 3},
               {"id": "stiff", "type": "fea_displacement", "case": "tip", "max": 0.5},
               {"id": "stress", "type": "fea_stress", "case": "tip", "max": 80},
               {"id": "nocase", "type": "fea_safety", "case": "nope", "min": 2},
               {"id": "no-hum", "type": "fea_frequency", "case": "modes", "min": 500},
               {"id": "too-high", "type": "fea_frequency", "case": "modes", "min": 1000},
               {"id": "wrong-kind", "type": "fea_frequency", "case": "tip", "min": 100}],
              open(os.path.join(pdir, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("fea_safety passes from the latest result", re.search(r"✓ strength\s+fea_safety\s+4\.\d+ \(≥ 3(\.0+)?\)", txt), txt)
    check("fea_displacement fails over its limit", re.search(r"✗ stiff\s+fea_displacement\s+0\.5\d+ mm", txt), txt)
    check("fea_stress passes", re.search(r"✓ stress\s+fea_stress", txt), txt)
    check("unknown case → says how to define it", "no load case 'nope'" in txt, txt)
    check("fea_frequency: the first natural frequency ≥ 500 Hz passes, ≥ 1000 Hz fails",
          re.search(r"✓ no-hum\s+fea_frequency\s+8\d\d\.\d Hz \(≥ 500", txt)
          and re.search(r"✗ too-high\s+fea_frequency\s+8\d\d\.\d Hz \(≥ 1 ?000", txt), txt)
    check("fea_frequency on a static case → says it needs a frequency case",
          "case 'tip' is a static analysis — fea_frequency needs a frequency case" in txt, txt)
    err, txt, _ = c.tool("analyze", {"project": "beam", "case": "once", "fixed": ["Beam.face(-X)"], "refine": False,
                                     "loads": [{"ref": "Beam.face(+X)", "force": 100, "direction": "-Z"}]})
    check("refine=false solves one mesh and says its checks will fail", not err
          and "solved on one mesh only (refine=false)" in txt and "converge  " not in txt, txt)
    json.dump([{"id": "strength", "type": "fea_safety", "case": "tip", "min": 3},
               {"id": "once", "type": "fea_safety", "case": "once", "min": 3},
               {"id": "once-accepted", "type": "fea_safety", "case": "once", "min": 3, "accept_unconverged": True}],
              open(os.path.join(pdir, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("a result solved on one mesh is no evidence: its check fails unless it accepts that",
          re.search(r"✗ once\s+fea_safety\s+4\.\d+ \(≥ 3\.00\), not converged", txt)
          and "solved on one mesh only" in txt and "Re-run it without refine=false" in txt
          and re.search(r"✓ once-accepted\s+fea_safety\s+4\.\d+ \(≥ 3\.00\), not converged \(accepted\)", txt)
          and re.search(r"✓ strength\s+fea_safety\s+4\.\d+ \(≥ 3\.00\)   max", txt), txt)
    rp = os.path.join(pdir, "out", "fea", "tip", "result.json")
    kept = open(rp).read()
    old = json.loads(kept)
    json.dump(dict(old, method=old["method"] - 1), open(rp, "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("a result from an older analysis method is stale", re.search(r"✗ strength\s+fea_safety\s+stale", txt)
          and "analysis method" in txt and "analyze(case='tip')" in txt, txt)
    json.dump(dict(old, versions=dict(old["versions"], calculix="0.1")), open(rp, "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("…and so is one solved by another CalculiX", re.search(r"✗ strength\s+fea_safety\s+stale", txt)
          and "solved with CalculiX 0.1" in txt, txt)
    open(rp, "w").write(kept)
    p = params(length=100, w=10, h=12)
    json.dump(p, open(os.path.join(pdir, "params.json"), "w"))
    json.dump([{"id": "strength", "type": "fea_safety", "case": "tip", "min": 3},
               {"id": "stiff", "type": "fea_displacement", "case": "tip", "max": 1.0},
               {"id": "no-hum", "type": "fea_frequency", "case": "modes", "min": 500}],
              open(os.path.join(pdir, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "beam", "views": []})
    check("a model change makes the results stale", "✗ strength" in txt and "stale" in txt
          and "analyze(case='tip')" in txt and "analyze(case='modes')" in txt, txt)
    t = time.time()
    err, txt, _ = c.tool("release", {"project": "beam", "drawings": "none"}, timeout=1200)
    TIMES["release re-solving 2 cases"] = time.time() - t
    check("release re-solves the stale cases (static and frequency), then passes", not err
          and "case 'tip' re-solved" in txt and "case 'modes' re-solved" in txt
          and "first natural frequency" in txt, txt)
    res = result(pdir, "tip")
    check("…the new result is for the 12 mm beam (stiffer)", res["max_disp"] < 0.4, res["max_disp"])
    f = result(pdir, "modes")["frequencies_hz"]
    check("…and its frequencies follow (f ∝ depth in the bending direction): sideways bending stays at f1 "
          "(%.0f Hz), the 12 mm direction rises ×1.2 (%.0f Hz)" % (f[0], f[1]),
          abs(f[0] - f_theory) / f_theory < 0.02 and abs(f[1] / f[0] - 1.2) < 0.02, f)

    c.close()

    print("eval case shelf_bracket: scripted solutions through the harness")
    t = time.time()
    g, a, r = shelf_solution(12)
    TIMES["shelf_bracket solution (analyze + release + grade)"] = time.time() - t
    bad = [x for x in g["checks"] if not x["ok"]]
    check("a 12 mm arm with a filleted corner grades 100 %", not bad, (bad, a[-1500:], r[-800:]))
    check("…and the fillet lets the stress converge", "converged ✓" in a, a[-1500:])
    g, a, r = shelf_solution(6)
    failed = sorted(set(x["type"] for x in g["checks"] if not x["ok"]))
    check("a 6 mm arm sags too far: fails the FEA result, the checks and the release",
          "fea_result" in failed and "acceptance_checks" in failed and "RELEASE STOPPED" in r, (failed, r[-600:]))
    print("\ntimes: %s" % "; ".join("%s %.1f s" % kv for kv in TIMES.items()))
    print("\n%s" % ("all checks passed" if not fails else "%d failure(s): %s" % (len(fails), fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
