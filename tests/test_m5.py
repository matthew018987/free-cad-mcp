#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M5 tests: FEA (`analyze`) against answers known from theory — a cantilever's tip
deflection and root stress, its first natural frequency, the Euler buckling load of a
fixed-free column, two blocks bonded on their shared face against one solid block —
plus equilibrium, convergence (and when the refined solve is skipped), load cases in
fea.json, fea_* checks going stale when the model changes, and release re-solving
stale cases.

    python3 tests/test_m5.py
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
    single = res["max_disp"]
    fj = json.load(open(os.path.join(pdir, "fea.json")))
    check("case saved in fea.json", fj["cases"]["tip"]["fixed"] == ["Beam.face(-X)"], fj)

    err, txt2, _ = c.tool("analyze", {"project": "beam", "case": "tip", "refine": False})
    check("saved case re-runs by name", not err and "case 'tip'" in txt2, txt2)
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
    json.dump([{"id": "shank-gap", "type": "radial_gap", "a": "Plate.holes(d=5.5)", "b": "Screws.bosses(d=5)",
                "min": 0.2}], open(os.path.join(ppd, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "plate", "views": []})
    check("radial_gap against a pattern of shanks pairs each hole with its own", re.search(
        r"✓ shank-gap\s+radial_gap\s+0\.250", txt), txt)
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
    json.dump([{"id": "assy-strength", "type": "fea_safety", "case": "bonded", "min": 2},
               {"id": "upper-sag", "type": "fea_displacement", "case": "bonded", "part": "Upper", "max": 0.5}],
              open(os.path.join(spd, "checks.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "stack", "views": []})
    check("multi-part fea checks: safety over every part passes, a part's sag over its limit fails",
          re.search(r"✓ assy-strength\s+fea_safety\s+\d\.\d\d \(≥ 2", txt)
          and re.search(r"✗ upper-sag\s+fea_displacement\s+0\.5\d+ mm \(≤ 0\.5", txt) and "in Upper" in txt, txt)
    json.dump(params(t_up=6), open(os.path.join(spd, "params.json"), "w"))
    err, txt, _ = c.tool("build", {"project": "stack", "views": []})
    check("changing one part of the assembly makes its result stale", "stale" in txt
          and "analyze(case='bonded')" in txt, txt)

    print("speed: the refined solve is skipped when the first mesh is already large (F3)")
    err, txt, _ = timed(c, "large first mesh (auto)", {"project": "beam", "case": "fine", "fixed": ["Beam.face(-X)"],
                                                       "loads": [{"ref": "Beam.face(+X)", "force": 100,
                                                                  "direction": "-Z"}],
                                                       "mesh_size": 1.0, "save": False})
    nodes = num(r"refined solve skipped: ([\d ]+) nodes", txt)
    check("…says so, with the node count and how to force it", not err and nodes and nodes > 50000
          and "pass refine=true to force" in txt and "finer mesh changes" not in txt, txt)
    d = num(r"max displacement ([\d.]+) mm", txt)
    check("…and the single solve is still accurate (within 1 % of the converged tip deflection)",
          d and abs(d - single) / single < 0.01, txt)

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
