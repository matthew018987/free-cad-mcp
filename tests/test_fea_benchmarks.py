#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""FEA benchmarks beyond beams: the stress where real parts fail, against answers known from
theory.

- A strip with a hole, pulled: the peak at the edge of the hole against the stress concentration
  factor of a plate of finite width (Heywood: Ktn = 2 + (1 - d/W)³ on the net section).
- A thick tube under internal pressure (Lamé): the von Mises stress and the largest principal
  stress at the bore. They differ, so an aluminium tube and an acrylic one are judged on
  different numbers. The load is a pressure on a curved face that balances itself.
- A lug pulled by a pin: the contact pressure of a bearing load (peak 4F / (π d t)), and the
  stress beside the hole, which a force spread over the whole wall gives at about half.

    python3 tests/test_fea_benchmarks.py
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
from test_fea import num, params, project, result  # noqa: E402

STRIP = '''P = fc.params()
body = fc.body("Strip")
fc.pad(body, fc.sketch(body, "XY", fc.rect(P.length, P.width), name="StripSketch"), P.thick)
fc.pocket(body, fc.sketch(body, fc.face(body, "+Z"), fc.circle(d=P.d), name="HoleSketch"), "through")
'''
TUBE = '''P = fc.params()
body = fc.body("Tube")
fc.pad(body, fc.sketch(body, "XY", fc.circle(d=P.od), fc.circle(d=P.bore), name="TubeSketch"), P.length)
'''
# the hole's centre is at the origin, `end` from the free end; the other end is held
LUG = '''P = fc.params()
body = fc.body("Lug")
fc.pad(body, fc.sketch(body, "XY", fc.rect(P.length, P.width, center=(P.end - P.length / 2, 0)),
                       name="LugSketch"), P.thick)
fc.pocket(body, fc.sketch(body, fc.face(body, "+Z"), fc.circle(d=P.d), name="HoleSketch"), "through")
'''
CBORE = '''body = fc.body("Block")
fc.pad(body, fc.sketch(body, "XY", fc.rect(60, 30), name="BlockSketch"), 12)
fc.holes(body, on=fc.face(body, "+Z"), at=[(15, 0)], spec="M5 clearance normal + cbore ISO4762")
'''
TIMES = {}


def analyze(c, key, args, timeout=900):
    t = time.time()
    out = c.tool("analyze", args, timeout=timeout)
    TIMES[key] = time.time() - t
    return out


def main():
    home = os.path.realpath(tempfile.mkdtemp(prefix="fcmcp_bench_"))
    c = Client(home)
    c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}})

    print("a strip with a hole, pulled (160 × 50 × 5, hole Ø10, 5 kN)")
    W, d, t, F = 50.0, 10.0, 5.0, 5000.0
    pdir = project(c, home, "strip", STRIP, params(length=160, width=W, thick=t, d=d))
    err, txt, _ = analyze(c, "strip", {"project": "strip", "case": "pull", "fixed": ["Strip.face(-X)"],
                                       "loads": [{"ref": "Strip.face(+X)", "force": F, "direction": "+X"}]})
    res = result(pdir, "pull") if not err else {}
    ktn = 2 + (1 - d / W) ** 3                              # Heywood; Howland's series gives 2.52 at d/W = 0.2
    peak = ktn * F / ((W - d) * t)                          # 62.8 MPa
    check("the peak stress %.1f MPa is within 4 %% of Ktn · F / ((W - d) t) = %.1f MPa (Ktn %.3f)" % (
        res.get("max_vm", 0), peak, ktn), not err and abs(res["max_vm"] - peak) / peak < 0.04, txt)
    at = res.get("max_vm_at") or [99, 99, 99]
    check("…at the edge of the hole, across the pull (%s)" % ", ".join("%.1f" % x for x in at),
          abs(abs(at[1]) - d / 2) < 0.3 and abs(at[0]) < 1.5, at)
    check("…in equilibrium, on a mesh with no distorted element", "✓ in equilibrium" in txt
          and res.get("mesh_quality", {}).get("distorted") == 0, (res.get("mesh_quality"), txt))

    print("a thick tube under internal pressure (Ø40 / Ø20 × 80, 10 MPa; Lamé)")
    a, b, p = 10.0, 20.0, 10.0
    hoop = p * (b * b + a * a) / (b * b - a * a)            # 16.67 MPa at the bore; radial -p, axial 0
    mises = math.sqrt(hoop * hoop + p * p + hoop * p)       # 23.33 MPa
    tdir = project(c, home, "tube", TUBE, params(od=2 * b, bore=2 * a, length=80))
    # held at one end, which stops the tube widening there: the disturbance overshoots by 4 % some 30 mm
    # along and has died out by 45 mm, so the tube is judged beyond that
    case = {"project": "tube", "case": "press", "fixed": ["Tube.face(-Z)"], "support_margin": 45,
            "loads": [{"ref": "Tube.holes(d=20)", "pressure": p}]}
    err, txt, _ = analyze(c, "tube", case)
    res = result(tdir, "press") if not err else {}
    check("aluminium: von Mises %.2f MPa at the bore within 3 %% of Lamé's %.2f MPa" % (
        res.get("max_stress_away", 0), mises), not err and res.get("criterion") == "von Mises"
        and abs(res["max_stress_away"] - mises) / mises < 0.03, txt)
    check("…the pressure on the bore balances itself, and the report says so rather than 'out of balance'",
          "✓ in equilibrium (the loads balance each other)" in txt, txt)
    err, txt, _ = analyze(c, "tube acrylic", dict(case, material="acrylic", save=False))
    res = result(tdir, "press") if not err else {}
    # the largest of the bore's nodal values, which scatter a few % about Lamé's (more at ν 0.37 than at 0.33):
    # on the safe side
    got = res.get("max_stress_away", 0)
    check("acrylic is brittle: judged on the hoop stress, %.2f MPa against Lamé's %.2f MPa (-3 … +12 %%), not on "
          "von Mises (%.2f MPa)" % (got, hoop, mises), not err and res.get("criterion") == "principal"
          and 0.97 * hoop < got < 1.12 * hoop and "against the breaking stress 70 MPa" in txt, txt)
    sf = (res.get("material") or {}).get("yield", 0) / max(got, 1e-9)
    check("…so its safety factor is %.2f, where von Mises would have given %.2f" % (sf, 70 / mises),
          70 / (1.12 * hoop) < sf < 70 / (0.97 * hoop), sf)
    err, txt, _ = analyze(c, "tube acrylic at 2 mm", dict(case, material="acrylic", mesh_size=2.0, refine=False,
                                                          save=False))
    fine = result(tdir, "press").get("max_stress_away", 0) if not err else 0
    check("…and closer on a 2 mm mesh: %.2f MPa" % fine, not err and 0.97 * hoop < fine <= got, txt)
    for size in (3.33, 2.0):
        err, txt, _ = analyze(c, "tube at %s mm" % size, dict(case, mesh_size=size, refine=False, save=False))
        res = result(tdir, "press") if not err else {}
        check("a %s mm mesh of the tube solves (its curved elements used to fold over): %.2f MPa" % (
            size, res.get("max_stress_away", 0)), not err and abs(res["max_stress_away"] - mises) / mises < 0.03
            and res["mesh_quality"]["min_jacobian"] > 0.3, txt)

    print("a lug pulled by a pin (30 wide, 6 thick, hole Ø12, 1 kN)")
    Wl, tl, dl, Fl = 30.0, 6.0, 12.0, 1000.0
    ldir = project(c, home, "lug", LUG, params(length=80, width=Wl, thick=tl, d=dl, end=20))
    hold = {"project": "lug", "fixed": ["Lug.face(-X)"]}
    err, txt, _ = analyze(c, "lug bearing", dict(hold, case="pin", loads=[
        {"ref": "Lug.holes(d=12)", "bearing": Fl, "direction": "+X"}]))
    pin = result(ldir, "pin") if not err else {}
    b0 = (pin.get("bearing") or [{}])[0]
    pmax = 4 * Fl / (math.pi * dl * tl)                     # 17.68 MPa
    check("bearing load: peak contact pressure %.2f MPa within 2 %% of 4F / (π d t) = %.2f MPa" % (
        b0.get("peak_pressure", 0), pmax), not err and abs(b0["peak_pressure"] - pmax) / pmax < 0.02
        and "peak contact pressure" in txt, txt)
    check("…on the half of the wall the pin presses against (%.0f of %.0f mm²)" % (
        b0.get("area", 0), math.pi * dl * tl), abs(b0.get("area", 0) - math.pi * dl * tl / 2)
        < 0.03 * math.pi * dl * tl / 2, b0)
    check("…the wall is squeezed by that pressure where the pin bears on it (%.1f MPa at x = %.1f)" % (
        pin.get("min_principal", 0), (pin.get("min_principal_at") or [0])[0]),
        abs(-pin.get("min_principal", 0) - pmax) / pmax < 0.08 and abs(pin["min_principal_at"][0] - dl / 2) < 0.3
        and abs(pin["min_principal_at"][1]) < 1.0, pin.get("min_principal_at"))
    check("…and the resultant is the force exactly: in equilibrium", "✓ in equilibrium" in txt
          and re.search(r"supports carry \(-1 000\.0, -?0\.00, -?0\.00\) N", txt), txt)
    err, txt, _ = analyze(c, "lug force", dict(hold, case="spread", loads=[
        {"ref": "Lug.holes(d=12)", "force": Fl, "direction": "+X"}]))
    spread = result(ldir, "spread") if not err else {}
    ratio = pin.get("max_vm", 0) / max(spread.get("max_vm", 1e-9), 1e-9)
    check("a force spread over the whole wall gives about half the stress beside the hole (%.1f vs %.1f MPa, "
          "× %.2f)" % (spread.get("max_vm", 0), pin.get("max_vm", 0), ratio), not err and 1.7 < ratio < 2.2, txt)
    check("…and the reply says so, naming the bearing load", "write \"bearing\" in place of \"force\"" in txt, txt)
    at = pin.get("max_vm_at") or [99, 99, 99]
    check("the bearing load's peak is beside the hole, across the pull (%s)" % ", ".join("%.1f" % x for x in at),
          abs(abs(at[1]) - dl / 2) < 0.4 and abs(at[0]) < 2.0, at)
    err, txt, _ = c.tool("analyze", dict(hold, case="bad", loads=[
        {"ref": "Lug.holes(d=12)", "bearing": Fl, "direction": "+Z"}]))
    check("a bearing load along the hole axis → says to turn it or use force", err and "square to the axis" in txt,
          txt)
    err, txt, _ = c.tool("analyze", dict(hold, case="bad", loads=[
        {"ref": "Lug.face(+X)", "bearing": Fl, "direction": "+X"}]))
    check("a bearing load on a flat face → says what it presses on", err and "presses on the wall of a hole" in txt,
          txt)
    check("failed cases are not saved", "bad" not in json.load(open(os.path.join(ldir, "fea.json")))["cases"])

    print("a counterbored hole: which wall a bearing load presses on")
    project(c, home, "block", CBORE, {})
    hold = {"project": "block", "fixed": ["Block.face(-X)"], "refine": False, "save": False, "case": "screw"}
    err, txt, _ = c.tool("analyze", dict(hold, loads=[{"ref": "Block.holes(d=5.5)", "bearing": 200,
                                                       "direction": "+X"}]))
    check("the smallest diameter by default: where the screw passes", not err and "pressing on the Ø5.5 wall" in txt,
          txt)
    err, txt, _ = c.tool("analyze", dict(hold, loads=[{"ref": "Block.holes(d=5.5)", "bearing": 200,
                                                       "direction": "+X", "d": 10}]))
    check("\"d\": 10 → the counterbore's wall", not err and "pressing on the Ø10 wall" in txt, txt)
    err, txt, _ = c.tool("analyze", dict(hold, loads=[{"ref": "Block.holes(d=5.5)", "bearing": 200,
                                                       "direction": "+X", "d": 8}]))
    check("a diameter the hole does not have → names the ones it has", err and "no Ø8 wall" in txt
          and "Ø5.5, Ø10" in txt, txt)

    c.close()
    print("\ntimes: %s" % "; ".join("%s %.1f s" % kv for kv in TIMES.items()))
    print("\n%s" % ("all checks passed" if not fails else "%d failure(s): %s" % (len(fails), fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
