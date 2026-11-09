#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""M6.1, the tolerancing core (server/tolerancing.py) and its standards tables: known
answers for stack-ups (worst case, RSS, Monte Carlo, contributions, IT allocation), the
bolted-joint rule, ISO 2768-2, ISO 286 grades to 500 mm, process capability, tolerance
parsing, every scheme validation rule, the frame / datum / surface texture SVGs, stable
balloon numbers and the inspection CSV; then lookup_standard's new keys over real stdio.
Stdlib only: no FreeCAD needed (the server runs on any Python 3).

    python3 tests/test_tolerancing.py
"""
import csv
import importlib.util
import io
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
from mcp_client import Client, check, fails  # noqa: E402
import standards as S  # noqa: E402
import tolerancing as T  # noqa: E402


def err_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    return ""


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def rules(findings):
    return sorted(set(f["rule"] for f in findings))


# ------------------------------------------------------------------------------------------- standards
print("standards: ISO 286 grades, ISO 273 classes, ISO 2768-2, bearings, capability")
check("IT grades to 500 mm: IT10 at 10.1 = 70, IT7 at 250 = 46, at 250.1 = 52, IT14 at 500 = 1550",
      (S.it_um(10, 10.1), S.it_um(7, 250), S.it_um(7, 250.1), S.it_um(14, 500)) == (70, 46, 52, 1550),
      [S.it_um(10, 10.1), S.it_um(7, 250), S.it_um(7, 250.1), S.it_um(14, 500)])
check("…out of range is a clear error, and fits still stop at 250 mm",
      "IT5 to IT14" in err_of(S.it_um, 4, 10) and "500 mm" in err_of(S.it_um, 7, 501)
      and "250 mm" in err_of(S.tolerance, 300, "H7"), [err_of(S.it_um, 4, 10), err_of(S.it_um, 7, 501)])
check("ISO 273 hole classes: M5 normal Ø5.5 H13 → 5.5 … 5.68; M8 coarse Ø10 H14 → 10 … 10.36",
      S.tolerance(S.clearance("M5", "normal"), S.CLEARANCE_CLASS["normal"]) == (5.5, 5.68)
      and S.tolerance(S.clearance("M8", "coarse"), S.CLEARANCE_CLASS["coarse"]) == (10.0, 10.36))
PG = S.parse_general_geometric
check("ISO 2768-2 class from the spec: mK → K, m → none, '2768-2 L' → L, fH → H, lower case",
      (PG("ISO 2768-mK"), PG("ISO 2768-m"), PG("ISO 2768-2 L"), PG("fH"), PG("iso 2768-cl")) ==
      ("K", None, "L", "H", "L"))
GG = S.general_geometric
check("ISO 2768-2 K flatness by length: ≤10 → 0.05, 10.1 → 0.1, 100 → 0.2, 3000 → 0.8",
      (GG("mK", "flatness", 10), GG("mK", "flatness", 10.1), GG("mK", "flatness", 100),
       GG("mK", "flatness", 3000)) == (0.05, 0.1, 0.2, 0.8))
check("…perpendicularity K 100 → 0.4, 101 → 0.6; symmetry H 500 → 0.5; L 2000 → 2; run-out H/K/L 0.1/0.2/0.5",
      (GG("K", "perpendicularity", 100), GG("K", "perpendicularity", 101), GG("H", "symmetry", 500),
       GG("L", "perpendicularity", 2000), GG("H", "runout"), GG("K", "circular_runout"), GG("L", "runout")) ==
      (0.4, 0.6, 0.5, 2.0, 0.1, 0.2, 0.5))
check("…lengths over 3000 and other characteristics are errors naming what ISO 2768-2 covers",
      "3000" in err_of(GG, "K", "flatness", 3001) and "straightness, flatness, perpendicularity" in
      err_of(GG, "K", "parallelism", 10))
t_m = S.general_tolerance_report("m", 80)
t_mk = S.general_tolerance_report("ISO 2768-mK", 80)
t_k = S.general_tolerance_report("ISO 2768-2 K")
check("ISO 2768 reports: 'm' unchanged (no geometric part); 'mK' adds class K with the withdrawn note; "
      "'2768-2 K' gives the geometric table alone",
      "ISO 2768-2" not in t_m and "80 mm → ±0.3 mm" in t_mk and "ISO 2768-2 class K" in t_mk
      and "80 mm → flatness 0.2 · perpendicularity 0.4" in t_mk and "withdrawn in 2021" in t_mk
      and "ISO 22081" in t_mk and t_k.startswith("ISO 2768-2 class K") and "linear" not in t_k, t_mk)
b = S.bearing("608")
check("bearing width tolerance (ISO 492 normal): 608 and 6205 0/-0.12, in the report",
      b["width_tol"] == (0.0, -0.12) and S.bearing("6205")["width_tol"] == (0.0, -0.12)
      and "width B 7 0/-0.120 (ISO 492 normal class" in S.bearing_report("608"), S.bearing_report("608"))
C = S.capability
check("capability cnc position: 0.1 standard, 0.05 precision, 0.01 special (jig boring)",
      C("cnc", "position", 0.1)[0] == "standard" and C("cnc", "position", 0.05)[0] == "precision"
      and C("cnc", "position", 0.01)[0] == "special" and "jig boring" in C("cnc", "position", 0.01)[1])
check("…ISO 286 grades on cnc: IT8 standard, IT7 precision, IT5 honing or grinding; fdm ±0.05 beyond reach",
      C("cnc", "it", 8)[0] == "standard" and C("cnc", "it", 7)[0] == "precision"
      and C("cnc", "it", 5) == ("special", "IT5 needs honing or grinding")
      and C("fdm", "linear", 0.05)[0] == "beyond" and "machine or finish" in C("fdm", "linear", 0.05)[1])
check("…form per 100 mm: flatness 0.02 on 300 mm is special, on 50 mm precision; table overrides apply",
      C("cnc", "form", 0.02, length=300)[0] == "special" and C("cnc", "form", 0.02, length=50)[0] == "precision"
      and C("cnc", "position", 0.05, table={"position": [0.04, 0.01]})[0] == "standard")
check("…Ra: turning 1.6 standard, cnc 1.6 precision, cnc 0.4 needs grinding or lapping",
      C("turning", "ra", 1.6)[0] == "standard" and C("cnc", "ra", 1.6)[0] == "precision"
      and C("cnc", "ra", 0.4) == ("special", "ra 0.4 µm needs grinding or lapping"))
check("…unknown kinds and processes are errors", "capability kind" in err_of(C, "cnc", "colour", 1)
      and "no capability data" in err_of(C, "woodwork", "linear", 0.1))

# ------------------------------------------------------------------------------------------- parsing
print("tolerance parsing and text")
P = T.parse_size
check("± and deviations: ±0.05, +-0.05, +0.1/0, 0/-0.12, -0.05/+0.1 (sorted, upper first)",
      (P("±0.05")["upper"], P("±0.05")["lower"], P("+-0.05")["upper"]) == (0.05, -0.05, 0.05)
      and (P("+0.1/0")["upper"], P("+0.1/0")["lower"]) == (0.1, 0.0)
      and (P("0/-0.12")["upper"], P("0/-0.12")["lower"]) == (0.0, -0.12)
      and (P("-0.05/+0.1")["upper"], P("-0.05/+0.1")["lower"]) == (0.1, -0.05))
check("classes and limits need the nominal: H7 at 22 → +0.021/0; limits [10, 10.05] at 10.02 → +0.03/-0.02",
      (P(cls="H7", nominal=22)["upper"], P(cls="H7", nominal=22)["lower"]) == (0.021, 0.0)
      and P(cls="H7")["upper"] is None
      and close(P(limits=[10.0, 10.05], nominal=10.02)["upper"], 0.03)
      and close(P(limits=[10.0, 10.05], nominal=10.02)["lower"], -0.02)
      and P(limits="10.00..10.05")["limits"] == (10.0, 10.05))
check("…bad sizes say what to write: a bare number, two kinds, a class in tol, equal deviations, IT17",
      "ambiguous" in err_of(P, 0.05) and "exactly one of" in err_of(P, "±0.05", "H7")
      and "put it in \"class\"" in err_of(P, "H7") and "equal" in err_of(P, "0.1/0.1")
      and "IT5–IT14" in err_of(P, cls="H17") and "limits" in err_of(P, "10..10.5"))
check("deviation pairs (ISO 129-1): +0.021/0, 0/-0.12, +0.10/-0.05; ±0.035, ±0.0055",
      (T.deviation_pair(0.021, 0), T.deviation_pair(0, -0.12), T.deviation_pair(0.1, -0.05),
       T.tolerance_text(0.035, -0.035), T.tolerance_text(0.0055, -0.0055)) ==
      ("+0.021/0", "0/-0.12", "+0.10/-0.05", "±0.035", "±0.0055"))
D = T.parse_datum_refs
check("datum references: 'A B C', 'A|B(M)|C', a common datum 'A-B', 'BⓂ'",
      [d["letters"] for d in D("A B C")] == [["A"], ["B"], ["C"]] and D("A|B(M)|C")[1]["mod"] == "M"
      and D(["A-B"])[0]["letters"] == ["A", "B"] and D("BⓂ")[0]["mod"] == "M" and D(None) == [])
check("…I/O/Q, repeats, more than three and 'AB' are errors",
      "I, O and Q" in err_of(D, "A O") and "twice" in err_of(D, "A B A") and "at most three" in err_of(D, "A B C D")
      and "common datum 'A-B'" in err_of(D, "AB"))

N = T.normalize
check("normalize: types by alias, typo suggestions, unknown fields named with the right one",
      N({"id": "a", "type": "circularity", "feature": "P.bosses(d=8)", "tol": 0.01})["type"] == "roundness"
      and "did you mean 'flatness'" in err_of(N, {"id": "a", "type": "flatnes", "feature": "f", "tol": 0.02})
      and "did you mean 'datums'" in err_of(N, {"id": "a", "type": "flatness", "feature": "f", "tol": 1, "datum": "A"}))
check("…ids without dots (stacks write Part.id), a feature or between, positive numbers",
      "Part.id" in err_of(N, {"id": "a.b", "type": "flatness", "feature": "f", "tol": 0.02})
      and "feature is the toleranced" in err_of(N, {"id": "a", "type": "flatness", "tol": 0.02})
      and "not both" in err_of(N, {"id": "a", "type": "size", "feature": "f", "between": ["a", "b"], "tol": "±0.1"})
      and "above 0" in err_of(N, {"id": "a", "type": "flatness", "feature": "f", "tol": 0}))
e_cls = N({"id": "s", "type": "size", "feature": "P.holes(d=22)", "class": "H7"})
e_off = N({"id": "s", "type": "size", "feature": "P.holes(d=22)", "class": "H7", "envelope": False})
e_tol = N({"id": "s", "type": "size", "between": ["P.face(-Z)", "P.face(+Z)"], "tol": "±0.05"})
check("Ⓔ by default on ISO 286 classes (decision 4), off when asked, not on ± tolerances",
      e_cls["envelope"] is True and e_off["envelope"] is False and e_tol["envelope"] is False)
check("surface entries: one of ra/rz, removal true/false/'any', a known lay",
      N({"id": "r", "type": "surface", "feature": "f", "ra": 1.6, "lay": "C"})["lay"] == "C"
      and "one of ra or rz" in err_of(N, {"id": "r", "type": "surface", "feature": "f", "ra": 1.6, "rz": 6.3})
      and "removal" in err_of(N, {"id": "r", "type": "surface", "feature": "f", "ra": 1.6, "removal": "yes"})
      and "lay is one of" in err_of(N, {"id": "r", "type": "surface", "feature": "f", "ra": 1.6, "lay": "Z"}))
check("geometric fields: MMC → M, Ⓟ height > 0, zone dia/width, angularity needs its angle",
      N({"id": "p", "type": "position", "feature": "f", "tol": 0.1, "material": "MMC"})["material"] == "M"
      and "height above 0" in err_of(N, {"id": "p", "type": "position", "feature": "f", "tol": 0.1, "projected": -1})
      and "zone is" in err_of(N, {"id": "p", "type": "position", "feature": "f", "tol": 0.1, "zone": "round"})
      and "basic angle" in err_of(N, {"id": "a", "type": "angularity", "feature": "f", "tol": 0.1, "datums": "A"})
      and "angularity only" in err_of(N, {"id": "a", "type": "flatness", "feature": "f", "tol": 0.1, "angle": 30}))

# ------------------------------------------------------------------------------------------- rules
print("scheme rules")
DAT = {"A": {"kind": "plane", "ref": "P.face(-Z)"}, "B": {"kind": "hole", "ref": "P.holes(d=22)"}}


def found(raw, feature, datums=DAT):
    return rules(T.check_entry(N(raw), feature, datums, "Plate"))


H, PL, BO = {"kind": "hole", "size": 5.5}, {"kind": "plane"}, {"kind": "boss"}
cases = [
    ("applies-to", {"id": "x", "type": "flatness", "feature": "P.holes(d=8)", "tol": 0.02}, H),
    ("datums", {"id": "x", "type": "flatness", "feature": "P.face(+Z)", "tol": 0.02, "datums": "A"}, PL),
    ("datums", {"id": "x", "type": "perpendicularity", "feature": "P.face(+X)", "tol": 0.02}, PL),
    ("datums", {"id": "x", "type": "position", "feature": "P.holes(d=5.5)", "tol": 0.2}, H),
    ("datum-defined", {"id": "x", "type": "parallelism", "feature": "P.face(+Z)", "tol": 0.05, "datums": "C"}, PL),
    ("datum-axis", {"id": "x", "type": "circular_runout", "feature": "P.bosses(d=12)", "tol": 0.02,
                    "datums": "A"}, BO),
    ("datum-modifier", {"id": "x", "type": "position", "feature": "P.holes(d=5.5)", "tol": 0.2, "datums": "A(M)"},
     H),
    ("self-datum", {"id": "x", "type": "perpendicularity", "feature": "P.holes(d=22)", "tol": 0.02,
                    "datums": "B"}, {"kind": "hole", "ref": "P.holes(d=22)"}),
    ("material", {"id": "x", "type": "flatness", "feature": "P.face(+Z)", "tol": 0.02, "material": "M"}, PL),
    ("material", {"id": "x", "type": "position", "feature": "P.face(+Z)", "tol": 0.2, "material": "M",
                  "datums": "A"}, PL),
    ("projected", {"id": "x", "type": "flatness", "feature": "P.face(+Z)", "tol": 0.02, "projected": 6}, PL),
    ("all-around", {"id": "x", "type": "flatness", "feature": "P.face(+Z)", "tol": 0.02, "all_around": True}, PL),
    ("zone", {"id": "x", "type": "position", "feature": "P.face(+Z)", "tol": 0.2, "datums": "A", "zone": "dia"},
     PL),
    ("tol-size", {"id": "x", "type": "position", "feature": "P.holes(d=5.5)", "tol": 6, "datums": "A"}, H),
    ("fastener", {"id": "x", "type": "position", "feature": "P.holes(d=5.5)", "tol": 0.6, "material": "M",
                  "datums": "A B"}, dict(H, fastener={"size": "M5", "clearance": "normal"})),
    ("class-case", {"id": "x", "type": "size", "feature": "P.bosses(d=12)", "class": "H7"}, BO),
    ("class-case", {"id": "x", "type": "size", "feature": "P.holes(d=8)", "class": "h7"}, H),
    ("class-case", {"id": "x", "type": "size", "feature": "P.slot", "class": "h9"}, {"kind": "width",
                                                                                      "internal": True}),
    ("size-on-fos", {"id": "x", "type": "size", "feature": "P.face(+Z)", "tol": "±0.1"}, PL),
    ("ra-series", {"id": "x", "type": "surface", "feature": "P.face(+Z)", "ra": 2.0}, PL),
]
missing = [(r, raw["id"], raw["type"], found(raw, f)) for r, raw, f in cases if r not in found(raw, f)]
check("each rule fires on its case (%d cases, %d rules)" % (len(cases), len(set(c[0] for c in cases))),
      not missing, missing)
check("…and stays quiet on correct entries: position ⌀0.5 Ⓜ on M5 clearance holes, ⟂ of the bore to A, an h9 tab",
      not found({"id": "x", "type": "position", "feature": "P.holes(d=5.5)", "tol": 0.5, "material": "M",
                 "datums": "A B"}, dict(H, fastener={"size": "M5", "clearance": "normal"}))
      and not found({"id": "x", "type": "perpendicularity", "feature": "P.holes(d=22)", "tol": 0.02, "datums": "A"},
                    {"kind": "hole", "ref": "P.holes(d=22)", "size": 22})
      and not found({"id": "x", "type": "size", "feature": "P.tab", "class": "h9"}, {"kind": "width",
                                                                                   "internal": False}))
msg = T.check_entry(N({"id": "x", "type": "flatness", "feature": "P.holes(d=8)", "tol": 0.02}), H, DAT)[0]["text"]
check("findings name the alternatives: flatness on a hole suggests straightness, roundness, cylindricity",
      "straightness, roundness, cylindricity" in msg, msg)

es = [N(x) for x in [
    {"id": "flat", "type": "flatness", "feature": "P.face(+Z)", "tol": 0.1},
    {"id": "par", "type": "parallelism", "feature": "P.face(+Z)", "tol": 0.05, "datums": "A"},
    {"id": "perp", "type": "perpendicularity", "feature": "P.holes(d=8)", "tol": 0.1, "datums": "A"},
    {"id": "pos", "type": "position", "feature": "P.holes(d=8)", "tol": 0.05, "datums": "A B"},
    {"id": "seat", "type": "size", "feature": "P.holes(d=22)", "class": "H6"},
    {"id": "seatRa", "type": "surface", "feature": "P.holes(d=22)", "ra": 1.6},
    {"id": "Aflat", "type": "flatness", "feature": "P.face(-Z)", "tol": 0.02},
    {"id": "par", "type": "flatness", "feature": "P.face(-X)", "tol": 0.02}]]
F = {"flat": PL, "par": PL, "perp": {"kind": "hole"}, "pos": {"kind": "hole"}, "seat": {"kind": "hole"},
     "seatRa": {"kind": "hole"}, "Aflat": PL}
dat3 = dict(DAT, C={"kind": "plane", "ref": "P.face(+X)"})
res = T.check_scheme(es, F, dat3, "Plate")
got = {k: rules(v) for k, v in res.items() if v}
check("scheme rules: duplicate ids, hierarchy (form vs orientation, orientation vs location), Ra on a fit, a datum "
      "without form control",
      "duplicate-id" in got.get("_part", []) and "datum-form" in got.get("_part", [])
      and got.get("flat") == ["hierarchy"] and got.get("perp") == ["hierarchy"] and got.get("seatRa") == ["fit-ra"]
      and "seat" not in got and "Aflat" not in got, got)
check("…the datum without a form control is C, not A (flatness) or B (Ⓔ on its H6)",
      [f["text"] for f in res["_part"] if f["rule"] == "datum-form"] == [
          "datum C (P.face(+X)) has no form tolerance: add a flatness so the feature the part is located on is "
          "controlled"], res["_part"])

# the plan's running example (docs/tolerancing-plan.md §3) is clean
housing = [N(x) for x in [
    {"id": "A-flat", "type": "flatness", "feature": "Housing.face(-Z)", "tol": 0.02},
    {"id": "seat", "type": "size", "feature": "Housing.holes(d=22)", "class": "H7", "envelope": True},
    {"id": "seat-perp", "type": "perpendicularity", "feature": "Housing.holes(d=22)", "tol": 0.02, "datums": "A"},
    {"id": "seat-Ra", "type": "surface", "feature": "Housing.holes(d=22)", "ra": 1.6},
    {"id": "depth", "type": "size", "between": ["Housing.face(+Z)", "Housing.face(+Z, at=-10.12)"], "tol": "±0.035"},
    {"id": "m5-tapped", "type": "position", "feature": "Housing.holes(d=4.2)", "tol": 0.25, "projected": 6,
     "datums": "A B"}]]
hf = {"A-flat": PL, "seat": {"kind": "hole", "size": 22}, "seat-perp": {"kind": "hole", "size": 22},
      "seat-Ra": {"kind": "hole", "size": 22}, "depth": {}, "m5-tapped": {"kind": "hole", "size": 4.2}}
hd = {"A": {"kind": "plane", "ref": "Housing.face(-Z)"}, "B": {"kind": "hole", "ref": "Housing.holes(d=22)"}}
cover = [N(x) for x in [
    {"id": "A-flat", "type": "flatness", "feature": "Cover.datum(A)", "tol": 0.05},
    {"id": "spigot", "type": "size", "between": ["Cover.datum(A)", "Cover.face(-Z)"], "tol": "±0.02"},
    {"id": "m5-clear", "type": "position", "feature": "Cover.holes(d=5.5)", "tol": 0.25, "material": "M",
     "datums": "A"}]]
cf = {"A-flat": PL, "spigot": {}, "m5-clear": {"kind": "hole", "size": 5.5,
                                               "fastener": {"size": "M5", "clearance": "normal"}}}
cd = {"A": {"kind": "plane", "ref": "Cover.face(-Z, contains=[25, 0, 0])"}}
rh, rc = T.check_scheme(housing, hf, hd, "Housing"), T.check_scheme(cover, cf, cd, "Cover")
check("the plan's Housing and Cover schemes have no findings (Cover.datum(A) stands for datum A's face)",
      not any(rh.values()) and not any(rc.values()), [rh, rc])
texts = [T.entry_text(e, hf[e["id"]], hd, nominal={"seat": 22, "depth": 10.12}.get(e["id"])) for e in housing]
check("entry texts as the report shows them",
      texts == ["⏥ 0.02", "⌀22 H7 Ⓔ (+0.021/0)", "⟂ ⌀0.02 | A", "√ Ra 1.6", "10.12 ±0.035", "⌖ ⌀0.25 Ⓟ6 | A | B"],
      texts)
par_axis = N({"id": "x", "type": "parallelism", "feature": "P.holes(d=8)", "tol": 0.05, "datums": "B"})
par_plane = N({"id": "x", "type": "parallelism", "feature": "P.holes(d=8)", "tol": 0.05, "datums": "A"})
check("⌀ zones: an axis parallel to a datum axis has one, to a datum plane not; Ⓜ on datums shows; angularity "
      "names its basic angle",
      T.frame_text(par_axis, {"kind": "hole"}, DAT) == "∥ ⌀0.05 | B"
      and T.frame_text(par_plane, {"kind": "hole"}, DAT) == "∥ 0.05 | A"
      and T.frame_text(N({"id": "x", "type": "position", "feature": "f", "tol": 0.1, "material": "M",
                          "datums": "A B(M)"}), {"kind": "hole"}, DAT) == "⌖ ⌀0.1 Ⓜ | A | BⓂ"
      and "(basic angle 30°)" in T.frame_text(N({"id": "x", "type": "angularity", "feature": "f", "tol": 0.1,
                                                 "datums": "A", "angle": 30}), PL, DAT))

# ------------------------------------------------------------------------------------------- symbols
print("symbols (SVG)")


def parse(svg):
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    return root, [t.text for t in root.iter(ns + "text")], list(root.iter(ns + "path")), list(root.iter(ns + "circle"))


def pts(path):
    return [tuple(float(v) for v in p.split(",")) for p in re.findall(r"-?[\d.]+,-?[\d.]+", path.get("d"))]


def shift(root):
    g = root.find("{http://www.w3.org/2000/svg}g")
    m = re.match(r"translate\((-?[\d.]+),(-?[\d.]+)\)", g.get("transform"))
    return float(m.group(1)), float(m.group(2))


pos = N({"id": "p", "type": "position", "feature": "f", "tol": 0.25, "material": "M", "datums": "A B C"})
fr = T.frame_svg(pos, {"kind": "hole"}, h=3.5)
root, txt, paths, circles = parse(fr["svg"])
check("frame: 2h high; cells 2h symbol + tolerance + three 2h datums (48.16 mm), mm units and viewBox",
      close(fr["frame"][3], 7.0) and close(fr["frame"][2], 48.16, 1e-6) and root.get("width").endswith("mm")
      and root.get("viewBox") == "0 0 %s %s" % (root.get("width")[:-2], root.get("height")[:-2]), fr["frame"])
check("…text is digits and letters only (the symbols, ⌀ and the circled M are paths and circles)",
      txt == ["0.25", "M", "A", "B", "C"] and all(t.isascii() for t in txt) and len(circles) >= 3, txt)
lead = T.frame_svg(pos, {"kind": "hole"}, h=3.5, leader={"to": (-15.0, 8.0), "side": "left"})
check("…a leader ends exactly where asked: anchor − attach = (−15, 8), the attach point on the frame's left side",
      close(lead["anchor"][0] - lead["attach"][0], -15.0, 1e-3) and close(lead["anchor"][1] - lead["attach"][1], 8.0,
                                                                         1e-3)
      and close(lead["attach"][0], lead["frame"][0], 1e-3), lead)
proj = T.frame_svg(N({"id": "p", "type": "position", "feature": "f", "tol": 0.25, "projected": 6, "datums": "A B"}),
                   {"kind": "hole"})
check("…Ⓟ and its height in the tolerance cell", parse(proj["svg"])[1] == ["0.25", "P", "6", "A", "B"],
      parse(proj["svg"])[1])
all_svgs = [T.frame_svg(N({"id": "x", "type": t, "feature": "f", "tol": 0.05,
                           "datums": "" if T.CHARACTERISTICS[t]["datums"] == "none" else "A",
                           **({"angle": 30} if t == "angularity" else {})}), PL)
            for t in T.CHARACTERISTICS]
check("every one of the 14 characteristics draws a well-formed frame",
      len(all_svgs) == 14 and all(parse(s["svg"])[0] is not None for s in all_svgs))

for direction, axis, sign in (((0, -1), 1, -1), ((0, 1), 1, 1), ((1, 0), 0, 1), ((-1, 0), 0, -1)):
    d = T.datum_svg("A", direction=direction)
    root, txt, paths, _ = parse(d["svg"])
    tri = pts(paths[0])
    dx, dy = shift(root)
    base_mid = ((tri[0][0] + tri[1][0]) / 2 + dx, (tri[0][1] + tri[1][1]) / 2 + dy)
    box_c = (d["box"][0] + d["box"][2] / 2, d["box"][1] + d["box"][3] / 2)
    ok = (close(base_mid[0], d["anchor"][0], 1e-3) and close(base_mid[1], d["anchor"][1], 1e-3)
          and (box_c[axis] - d["anchor"][axis]) * sign > 0 and txt == ["A"])
    if not ok:
        break
check("datum symbol: the anchor is the middle of the triangle's base, the box lies in the asked direction (4 ways)",
      ok, (direction, d))
check("…letters follow ISO 5459 practice", "not I, O or Q" in err_of(T.datum_svg, "O"))

ra = {"ra": 1.6, "method": "ground"}
up = T.surface_svg(ra, process="cnc")
root, txt, paths, circles = parse(up["svg"])
dx, dy = shift(root)
v = pts(paths[0])
check("surface symbol: the apex of the V is the anchor; requirement and method written; a bar for material "
      "removal on cnc",
      close(v[1][0] + dx, up["anchor"][0], 1e-3) and close(v[1][1] + dy, up["anchor"][1], 1e-3)
      and txt == ["Ra 1.6", "ground"] and len(paths) >= 3 and not circles, (v, up["anchor"], txt))
nr = T.surface_svg({"ra": 3.2, "removal": False})
check("…a circle when material removal is not permitted", len(parse(nr["svg"])[3]) == 1)
rt = T.surface_svg({"ra": 0.8}, orient="right")
check("…turned to read from the right on a vertical line", 'rotate(90' in rt["svg"]
      and rt["anchor"][0] < rt["width"] / 4, rt["anchor"])
ld = T.surface_svg({"ra": 0.8}, leader={"to": (12.0, 9.0)})
root, txt, paths, _ = parse(ld["svg"])
dx, dy = shift(root)
v = pts(paths[2])                         # shoulder + leader, its arrowhead, then the V
check("…on a leader: the arrow tip is the anchor, 12 right and 9 down of the apex",
      close(ld["anchor"][0] - (v[1][0] + dx), 12.0, 1e-3) and close(ld["anchor"][1] - (v[1][1] + dy), 9.0, 1e-3),
      (ld["anchor"], v))
gen = T.surface_svg({"ra": 3.2}, general=True)
check("…the general requirement adds (√)", parse(gen["svg"])[1] == ["Ra 3.2", "(", ")"], parse(gen["svg"])[1])
check("…'up' or 'right' only (a surface seen from below takes a leader)",
      "leader" in err_of(T.surface_svg, {"ra": 1.6}, orient="down"))

# ------------------------------------------------------------------------------------------- stacks
print("stack-ups")
L = T.make_link
ex = [L("Bearing.width", 7.0, -1, -0.12, 0.0, fixed=True, part="Bearing", source="ISO 492 normal"),
      L("Housing.depth", 10.2, +1, 0.0, 0.05, part="Housing"),
      L("Cover.spigot", 3.0, -1, -0.1, 0.1, part="Cover", source="ISO 2768-m at 3 mm  ⚠ not specified")]
wc = T.worst_case(ex)
check("the plan's end-play stack, worst case: nominal 0.200, 0.100 … 0.470",
      close(wc["nominal"], 0.2, 1e-9) and close(wc["min"], 0.1, 1e-9) and close(wc["max"], 0.47, 1e-9), wc)
r = T.rss(ex, (0.05, 0.30))
check("…RSS: mean 0.285 (band centres), σ 0.03976, 0.166 … 0.404, ≈ 35.3 % above 0.30, none below",
      close(r["mean"], 0.285, 1e-9) and close(r["sigma"], 0.039758, 1e-5) and close(r["min"], 0.166, 1e-3)
      and close(r["max"], 0.404, 1e-3) and close(r["out_high"], 0.353, 2e-3) and r["out_low"] < 1e-4, r)
sh = {s["name"]: s for s in T.contributions(ex)}
check("…shares: the spigot 54 % of the worst case and 70 % of the variance",
      close(sh["Cover.spigot"]["wc"], 0.2 / 0.37, 1e-9) and close(sh["Cover.spigot"]["rss"], 0.7030, 1e-3)
      and close(sum(s["wc"] for s in sh.values()), 1.0, 1e-9), sh)
mc1 = T.monte_carlo(ex, n=20000, seed=3, spec=(0.05, 0.30), use_numpy=False)
mc2 = T.monte_carlo(ex, n=20000, seed=3, spec=(0.05, 0.30), use_numpy=False)
check("…Monte Carlo (pure Python): reproducible with a seed, mean ≈ 0.285, ≈ 35 % above",
      mc1 == mc2 and close(mc1["mean"], 0.285, 2e-3) and close(mc1["out_high"], 0.353, 0.02), mc1)
if importlib.util.find_spec("numpy"):
    mcn = T.monte_carlo(ex, n=50000, seed=3, spec=(0.05, 0.30))
    check("…Monte Carlo (numpy) agrees", close(mcn["mean"], 0.285, 2e-3) and close(mcn["out_high"], 0.353, 0.02), mcn)
else:
    print("  skip …Monte Carlo (numpy): numpy not installed")
uni = [L("u", 10, 1, -0.1, 0.1, dist="uniform")]
check("uniform links: σ = T/√12", close(T.rss(uni)["sigma"], 0.2 / math.sqrt(12), 1e-12))
an = T.analyse(ex, (0.05, 0.30), methods=("worst_case", "rss"))
check("analyse: worst case ✗ and RSS ✗ against 0.05 … 0.30; worst case ✓ against 0.05 … 0.5",
      an["ok"] == {"worst_case": False, "rss": False}
      and T.analyse(ex, (0.05, 0.5))["ok"]["worst_case"] is True and T.analyse(ex, (None, 0.5))["ok"]["worst_case"])
book = [L("A", 50, 1, -0.1, 0.1), L("B", 20, -1, -0.05, 0.05), L("C", 15, -1, -0.05, 0.05),
        L("D", 14.9, -1, -0.05, 0.05)]
bw, br = T.worst_case(book), T.rss(book)
check("a textbook four-link stack: gap 0.1, worst case ±0.25, RSS ±√Σt² = ±0.1323",
      close(bw["nominal"], 0.1, 1e-9) and close(bw["max"] - bw["nominal"], 0.25, 1e-9)
      and close(3 * br["sigma"], math.sqrt(0.1 ** 2 + 3 * 0.05 ** 2), 1e-12), (bw, br))
a = T.allocate(ex, (0.05, 0.30))
check("allocation: bearing fixed, IT10 is the coarsest grade (0.23 ≤ 0.25; IT11 0.29 ✗)",
      a["grade"] == 10 and close(a["used"], 0.23, 1e-9) and a["coarser"] == {"grade": 11, "used": a["coarser"]["used"]}
      and close(a["coarser"]["used"], 0.29, 1e-9) and close(a["fixed_used"], 0.12, 1e-9), a)
check("…centring moves the bore depth 10.20 → 10.12 (rounded to 0.01): worst case 0.065 … 0.295 ✓",
      a["move"] == {"link": "Housing.depth", "from": 10.2, "to": 10.12} and close(a["result"]["min"], 0.065, 1e-9)
      and close(a["result"]["max"], 0.295, 1e-9) and a["ok"], a.get("move"))
ar = T.allocate(ex, (0.05, 0.30), method="rss")
check("…the RSS method allows a coarser grade and still centres inside the band",
      ar["grade"] >= 10 and ar["ok"] and ar["result"]["min"] >= 0.05 and ar["result"]["max"] <= 0.30, ar)
check("…nothing to allocate, no band, or fixed links that use it all are explained",
      "every link is fixed" in err_of(T.allocate, [ex[0]], (0.05, 0.3))
      and "min and max" in err_of(T.allocate, ex, (None, 0.3))
      and "0.3 of it for the fixed links" in T.allocate([L("f", 7, 1, -0.3, 0, fixed=True), ex[1]], (0.0, 0.2))["why"])
txt = T.stack_text("end-play", T.analyse(ex, (0.05, 0.30)), direction="+Z", rev="r0012", closure=0.2, alloc=a)
check("the report: verdicts, worst case, RSS share out of spec, closure, the general-tolerance note, allocation",
      "worst case ✗ · RSS ✗" in txt and "worst case 0.100 … 0.470   ✗ above 0.3" in txt
      and "≈ 35 % above 0.3" in txt and "chain closes ✓ (model gap 0.200)" in txt
      and "Cover.spigot on the general tolerance" in txt and "IT10: Housing.depth ±0.035, Cover.spigot ±0.02" in txt
      and "(IT11 0.29 ✗)" in txt and "Housing.depth 10.2 → 10.12 → worst case 0.065 … 0.295 ✓" in txt, txt)
check("…a chain that does not close says so", "chain closes ✗ (model gap 0.250)" in T.stack_text(
    "x", T.analyse(ex), closure=0.25))
check("links: a sign of ±1, ordered deviations", "sign" in err_of(L, "x", 1, 0, 0, 0.1)
      and "below" in err_of(L, "x", 1, 1, 0.1, -0.1))

# ------------------------------------------------------------------------------------------- bolted joints
print("bolted joints")
f5 = T.fastener("M5")
check("M5 normal: Ø5.5 H13 (5.5 … 5.68), screw Ø5, T = ⌀0.5",
      (f5["hole"], f5["hole_class"], f5["hole_min"], f5["hole_max"], f5["screw"], f5["T"]) ==
      (5.5, "H13", 5.5, 5.68, 5.0, 0.5), f5)
check("…M8 fine Ø8.4 H12 → ⌀0.4; M3 coarse Ø3.6 H14 → ⌀0.6; a given screw Ø4.976 → ⌀0.524",
      T.fastener("M8", "fine")["T"] == 0.4 and T.fastener("M3", "coarse")["T"] == 0.6
      and T.fastener("M5", screw_d=4.976)["T"] == 0.524)
FC = T.fastener_check
check("floating: ⌀0.5 each ✓, ⌀0.6 ✗; fixed: ⌀0.25 + ⌀0.25 ✓, ⌀0.3 + ⌀0.25 ✗",
      FC(0.5, 0.5, "M5")[0] and not FC(0.5, 0.6, "M5")[0] and FC(0.25, 0.25, "M5", mount="fixed")[0]
      and not FC(0.3, 0.25, "M5", mount="fixed")[0])
check("±0.2 on x and y is a ⌀0.566 position zone", close(T.equivalent_position(0.2), 0.565685, 1e-6))
rep = T.fastener_report("M5", mount="fixed")
check("fastener report: hole, screw, T, the fixed split with Ⓟ, the checks.json line for the same mount",
      "Ø5.5 H13 (5.5 … 5.68" in rep and "T = H − F = 0.5" in rep and "⌀0.25 Ⓟ" in rep and "floating (" not in rep
      and "\"mount\": \"fixed\"" in rep, rep)
check("…bad clearance series and sizes are errors", "fine, normal or coarse" in err_of(T.fastener, "M5", "tight")
      and "unknown metric size" in err_of(T.fastener, "M7"))

# ------------------------------------------------------------------------------------------- inspection
print("inspection list and balloons")
nums, reg = T.assign_balloons(None, ["a", "b", "c"])
n2, reg2 = T.assign_balloons(reg, ["a", "d", "b", "c"])
n3, reg3 = T.assign_balloons(reg2, ["a", "d", "c", "e"])
n4, reg4 = T.assign_balloons(reg3, ["a", "b", "d", "c", "e"])
check("balloons: 1, 2, 3; a new item takes 4 without renumbering; a removed one retires its number (e gets 5); "
      "it gets its number back when it returns",
      nums == {"a": 1, "b": 2, "c": 3} and n2 == {"a": 1, "d": 4, "b": 2, "c": 3}
      and n3 == {"a": 1, "d": 4, "c": 3, "e": 5} and reg3["retired"] == {"b": 2} and n4["b"] == 2
      and reg4["next"] == 6 and T.assign_balloons(reg4, ["a", "b", "d", "c", "e"])[0] == n4, [n2, n3, n4, reg4])
check("…keys must be unique on a sheet", "twice" in err_of(T.assign_balloons, None, ["a", "a"]))
it_size = T.characteristic_item(housing[1], hf["seat"], hd, nominal=22, view="Top")
it_pos = T.characteristic_item(housing[5], hf["m5-tapped"], hd, view="Top")
it_ra = T.characteristic_item(housing[3], hf["seat-Ra"], hd)
it_dim = T.dimension_item("overall-width", "80", 80, general="m", view="Front")
check("items: the H7 seat 22.000 … 22.021; position ≤ 0.25 to A B; Ra ≤ 1.6 µm; an 80 on ISO 2768-m 79.7 … 80.3",
      (it_size["lower"], it_size["upper"], it_size["requirement"]) == (22.0, 22.021, "⌀22 H7 Ⓔ (+0.021/0)")
      and (it_pos["upper"], it_pos["datums"], it_pos["name"]) == (0.25, "A B", "position")
      and (it_ra["unit"], it_ra["upper"]) == ("µm", 1.6)
      and (it_dim["lower"], it_dim["upper"], it_dim["source"]) == (79.7, 80.3, "general tolerance m"),
      [it_size, it_pos, it_ra, it_dim])
items = [it_dim, it_size, it_pos, it_ra, T.note_item("material", "MATERIAL: 6082-T6")]
numbers, _ = T.assign_balloons(None, [i["key"] for i in items])
rows = T.inspection_rows(items, numbers)
text = T.inspection_csv(rows)
parsed = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
check("the CSV: a byte-order mark, the header, one row per balloon in order, the symbols kept, blank shop columns",
      text.startswith("﻿") and tuple(parsed[0]) == T.INSPECTION_COLUMNS and [r[0] for r in parsed[1:]] ==
      ["1", "2", "3", "4", "5"] and parsed[3][2] == "⌖ ⌀0.25 Ⓟ6 | A | B" and parsed[2][4:6] == ["22", "22.021"]
      and all(r[-3:] == ["", "", ""] for r in parsed[1:]), parsed)

# ------------------------------------------------------------------------------------------- over stdio
print("lookup_standard over stdio")
home = tempfile.mkdtemp(prefix="fcmcp-tol-")
try:
    c = Client(home)
    c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}})

    def look(**a):
        return c.tool("lookup_standard", a)
    e1, t1, _ = look(gdt="position")
    e2, t2, _ = look(gdt="all")
    check("gdt='position' and gdt='all'", not e1 and "⌖ position (ISO 1101, location)" in t1 and "fastener='M5'" in t1
          and not e2 and "⏥ flatness" in t2 and "Ⓟ projected zone" in t2, t1 + t2)
    e3, t3, _ = look(fastener="M5", mount="floating")
    e4, t4, _ = look(fastener="M6", clearance="fine", mount="fixed")
    check("fastener='M5' floating; M6 fine fixed (Ø6.4 H12, ⌀0.4 shared)",
          not e3 and "⌖ ⌀0.5 Ⓜ on each part's pattern" in t3 and not e4 and "Ø6.4 H12" in t4 and "share ⌀0.4" in t4,
          t3 + t4)
    e5, t5, _ = look(surface="Ra 0.8")
    e6, t6, _ = look(surface="turning")
    check("surface='Ra 0.8' per process; surface='turning'", not e5 and "cnc      precision" in t5
          and "fdm      beyond" in t5 and not e6 and "standard Ra 1.6 µm, finest Ra 0.4" in t6, t5 + t6)
    e7, t7, _ = look(tolerance="ISO 2768-mK", d=40)
    check("tolerance='ISO 2768-mK' with d: linear and geometric", not e7 and "40 mm → ±0.3 mm" in t7
          and "40 mm → flatness 0.2 · perpendicularity 0.4" in t7, t7)
    e8, t8, _ = look(gdt="flatnes")
    e9, t9, _ = look(fastener="M5", mount="sideways")
    e10, t10, _ = look(surface="Ra x")
    check("errors: a typo suggests the name, a bad mount and a bad surface are refused",
          e8 and "did you mean 'flatness'" in t8 and e9 and "mount" in t9 and e10 and "Ra 1.6" in t10, [t8, t9, t10])
    e11, t11, _ = look()
    props = [t for t in c.rpc("tools/list")["result"]["tools"] if t["name"] == "lookup_standard"][0]["inputSchema"]
    check("no arguments lists the new keys; the schema has gdt, fastener, clearance, mount, surface",
          "gdt='position'" in t11 and "fastener='M5'" in t11 and "surface='Ra 0.8'" in t11
          and {"gdt", "fastener", "clearance", "mount", "surface"} <= set(props["properties"]), t11)
    c.close()
finally:
    shutil.rmtree(home, ignore_errors=True)

print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
