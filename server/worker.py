# SPDX-License-Identifier: Apache-2.0
"""
FreeCAD worker — runs one request inside FreeCAD's Python, then exits.

The MCP server never imports FreeCAD: it spawns this script with FreeCAD's
bundled interpreter for every geometry request, so an OpenCascade crash only
ends this process. Request and response travel as JSON files:

    python -I worker.py request.json response.json

Ops: probe, build, run_script, inspect, render, measure, check_fit, section.

Images of a document outside a project go to ~/.cache/freecad-mcp/images/,
never into a folder next to the user's file.
"""

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
import traceback

try:
    HERE = os.environ.get("FCMCP_SERVER_DIR") or os.path.dirname(os.path.abspath(__file__))
except NameError:          # exec()'d by freecadcmd, where __file__ is undefined
    HERE = os.environ["FCMCP_SERVER_DIR"]
sys.path.insert(0, HERE)
_lib = os.environ.get("FCMCP_LIB")
if _lib and _lib not in sys.path:
    sys.path.append(_lib)
if _lib:
    # FreeCAD's workbench Python modules (Assembly joints, TechDraw helpers …),
    # which the GUI normally puts on sys.path at start-up
    _mod = os.path.join(os.path.dirname(_lib), "Mod")
    for _d in ("Assembly", "TechDraw", "Part", "PartDesign", "Mesh", "MeshPart", "Import"):
        _p = os.path.join(_mod, _d)
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.append(_p)

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
import Sketcher  # noqa: E402

import analysis as an  # noqa: E402
import fea  # noqa: E402
import fclib as fc  # noqa: E402
import manufacture as mf  # noqa: E402
import render as rd  # noqa: E402
import standards  # noqa: E402
from analysis import (_axis_name, _axis_point, _canon, _is_concave, _pt, detect_holes, fmt,  # noqa: E402
                      global_placement, global_shape, has_shape, is_result, results, role)


class UserError(Exception):
    """A problem the caller can fix; reported without a worker traceback."""


# ------------------------------------------------------------------ helpers

def dims(bb):
    return "%s × %s × %s" % (fmt(bb.XLength, 1), fmt(bb.YLength, 1), fmt(bb.ZLength, 1))


def load_json(p, default=None):
    if p and os.path.isfile(p):
        with open(p) as fh:
            return json.load(fh)
    return default


def load_doc(path):
    if not os.path.isfile(path):
        raise UserError("no such file: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fcstd":
        return App.openDocument(path)
    stem = re.sub(r"\W+", "_", os.path.splitext(os.path.basename(path))[0]) or "Doc"
    doc = App.newDocument(stem)
    if ext in (".step", ".stp", ".iges", ".igs"):
        import Import
        Import.insert(path, doc.Name)
    elif ext in (".brep", ".brp"):
        o = doc.addObject("Part::Feature", stem)
        o.Shape = Part.read(path)
    elif ext in (".stl", ".obj"):
        # a mesh becomes a solid Part feature, so inspect/measure/render work on it
        o = doc.addObject("Part::Feature", stem)
        o.Shape, _ = fc._mesh_shape(path)
    else:
        raise UserError("unsupported file type %s (FCStd, STEP, IGES, BREP, STL, OBJ)" % ext)
    doc.recompute()
    return doc


def save_as(doc, path, inplace=False):
    """Save without leaving behind the backup FreeCAD makes of the file it
    replaces (<stem>.<date>.FCBak, or <name>.FCStdN)."""
    d = os.path.dirname(path) or "."
    stem = os.path.splitext(os.path.basename(path))[0] + "."
    before = set(os.listdir(d)) if os.path.isdir(d) else set()
    if inplace:
        doc.save()
    else:
        doc.saveAs(path)
    for f in set(os.listdir(d)) - before:
        if f.startswith(stem) and re.search(r"\.FCBak$|\.FCStd\d+$", f, re.I):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass


def image_dir():
    """Where images of documents outside a project go (never next to the user's file)."""
    d = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "images")
    os.makedirs(d, exist_ok=True)
    return d


def image_path(args, name):
    """<project>/out/<name> inside a project, else the image cache."""
    pdir = args.get("project_dir")
    d = os.path.join(pdir, "out") if pdir else image_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def find(doc, ref):
    try:
        return an.find(doc, ref)
    except an.RefError as e:
        raise UserError(str(e))


def state_error(o):
    st = list(o.State)
    if "Invalid" in st or "Error" in st:
        msg = o.getStatusString() if hasattr(o, "getStatusString") else ""
        return msg or "failed"
    return None


def doc_errors(doc):
    return [(o, state_error(o)) for o in doc.Objects if state_error(o)]


def sketch_info(sk):
    try:
        sk.solve()
    except Exception:  # noqa: BLE001
        pass
    dof = getattr(sk, "DoF", None)
    return {"name": sk.Label, "dof": dof if isinstance(dof, int) else None,
            "full": bool(getattr(sk, "FullyConstrained", False)),
            "conflicting": list(getattr(sk, "ConflictingConstraints", []) or []),
            "redundant": list(getattr(sk, "RedundantConstraints", []) or []),
            "malformed": list(getattr(sk, "MalformedConstraints", []) or [])}


def hole_summary(h):
    try:
        n = len([g for g in h.Profile[0].Geometry if g.__class__.__name__ == "Circle"])
    except Exception:  # noqa: BLE001
        n = 0
    parts = ["%d×" % n if n else ""]
    spec = getattr(h, "FcSpec", "")
    comp = float(getattr(h, "FcHoleComp", 0.0) or 0.0)
    if h.Threaded:
        parts.append("%s-%s" % (h.ThreadSize, h.ThreadClass))
        parts.append("tap Ø%s" % fmt(h.Diameter.Value, 2))
        if h.ModelThread:
            parts.append("modelled thread")
    else:
        if comp:
            parts.append("Ø%s %+g comp → Ø%s" % (_num(round(h.Diameter.Value - comp, 4)), comp,
                                                _num(round(h.Diameter.Value, 4))))
        else:
            parts.append("Ø%s" % fmt(h.Diameter.Value, 2))
        if h.ThreadType != "None":
            parts.append("(%s clearance, %s)" % (h.ThreadSize.split("x")[0], h.ThreadFit))
    if h.HoleCutType not in ("None", ""):
        parts.append("+ %s Ø%s↧%s" % (h.HoleCutType, fmt(h.HoleCutDiameter.Value, 1),
                                       fmt(h.HoleCutDepth.Value, 1)))
    parts.append("THRU" if h.DepthType == "ThroughAll" else "↧%s" % fmt(h.Depth.Value, 1))
    if getattr(h, "FcFit", ""):
        parts.append(h.FcFit)
    out = " ".join(p for p in parts if p)
    return out + ("  [%s]" % spec if spec else "")


def hole_points(h):
    """(normal axis, centres) of a Hole feature's sketch circles in its body's
    coordinates, each centre as the two in-plane coordinates of the sketch
    plane — (x, y) on a plane normal to Z, (x, z) normal to Y, (y, z) normal to
    X, as fc.holes takes them — rounded to 0.001 mm. A thickness change that
    moves the sketch face, or the body moved by its placement or a joint, is
    then not a hole change. (None, None) when unknown."""
    try:
        sk = h.Profile[0]
        body = h.getParentGeoFeatureGroup()
        if body is not None and sk.getParentGeoFeatureGroup() == body:
            pl = sk.Placement                    # PartDesign: relative to the body
        else:
            pl = sk.getGlobalPlacement()
            if body is not None:
                pl = body.getGlobalPlacement().inverse().multiply(pl)
        n = pl.Rotation.multVec(App.Vector(0, 0, 1))
        k = max(range(3), key=lambda i: abs(n[i]))
        keep = [i for i in range(3) if i != k]
        pts = []
        for g in sk.Geometry:
            if g.__class__.__name__ == "Circle":
                p = pl.multVec(g.Center)
                pts.append([round(p[i], 3) + 0.0 for i in keep])
        return "XYZ"[k], sorted(pts)
    except Exception:  # noqa: BLE001
        return None, None


def body_features(b):
    feats = [f for f in b.Group if f.TypeId.startswith("PartDesign::")
             and not f.TypeId.startswith(("PartDesign::Plane", "PartDesign::Line", "PartDesign::Point",
                                          "PartDesign::CoordinateSystem", "PartDesign::ShapeBinder",
                                          "PartDesign::SubShapeBinder"))]
    tip = b.Tip
    out = []
    for f in feats:
        err = state_error(f)
        d = {"name": f.Label, "type": f.TypeId.split("::")[1], "error": err}
        if f.TypeId == "PartDesign::Hole":
            d["hole"] = hole_summary(f)
            d["on"], d["at"] = hole_points(f)        # in-plane body coordinates (older reports: global xyz)
        out.append(d)
    tip_idx = feats.index(tip) if tip in feats else -1
    return out, (tip.Label if tip else None), tip_idx, len(feats)


def densities(project):
    """dens(label) → g/cm³ or None per part: project.json parts.<label>
    (density or material), else the project material (standards.density_for)."""
    project = project or {}
    return lambda label: standards.density_for(project, label)


def material_of(project, label):
    project = project or {}
    return (((project.get("parts") or {}).get(label) or {}).get("material")
            or (project.get("material") or {}).get("name") or "")


def mass_note(project, recs):
    """'   (mass: PETG, 1.27 g/cm³)' — per part when the parts differ."""
    groups = []
    for r in recs:
        if r.get("density"):
            k = (material_of(project, r["label"]), r["density"])
            g = next((g for g in groups if g[0] == k), None)
            if g is None:
                groups.append((k, [r["label"]]))
            else:
                g[1].append(r["label"])
    if not groups:
        return ""
    if len(groups) == 1:
        (m, d), _ = groups[0]
        return "   (mass: %s, %g g/cm³)" % (m or "material", d)
    return "   (mass: %s)" % " · ".join("%s: %s %g g/cm³" % (", ".join(labels), m or "material", d)
                                       for (m, d), labels in groups)


def bom_of(o):
    """A reference part's BOM role (fc.ref_part bom/qty/spec); 'context' by default."""
    try:
        b = json.loads(getattr(o, "FcBom", "") or "{}")
    except ValueError:
        b = {}
    try:
        qty = int(b.get("qty") or 1)
    except (TypeError, ValueError):
        qty = 1
    return {"bom": b.get("bom") if b.get("bom") in ("buy", "context", "none") else "context",
            "qty": qty, "spec": b.get("spec")}


def object_record(o, dens=None):
    s = global_shape(o)
    tb = an.tight_bb(s)
    rec = {"name": o.Name, "label": o.Label, "type": o.TypeId, "role": role(o),
           "solids": len(s.Solids), "valid": bool(s.isValid()),
           "volume": s.Volume if s.Solids else 0.0,
           "bbox": [tb.XMin, tb.YMin, tb.ZMin, tb.XMax, tb.YMax, tb.ZMax]}
    try:
        rec["closed"] = all(sh.isClosed() for sh in s.Shells) if s.Shells else False
    except Exception:  # noqa: BLE001
        rec["closed"] = None
    d = dens(o.Label) if callable(dens) else dens
    if d and rec["role"] != "ref" and s.Solids:
        rec["density"] = d
        rec["mass_g"] = rec["volume"] * d / 1000.0
    if rec["role"] == "ref":
        rec["bom"] = bom_of(o)
    try:
        pl = global_placement(o)
        q = list(pl.Rotation.Q)
        if next((c for c in q if abs(c) > 1e-12), 1) < 0:      # q and −q are the same rotation
            q = [-c for c in q]
        rec["placement"] = [round(c, 6) + 0.0 for c in pl.Base] + [round(c, 9) + 0.0 for c in q]
    except Exception:  # noqa: BLE001
        pass
    if o.TypeId == "PartDesign::Body":
        feats, tip, tip_idx, n = body_features(o)
        rec["features"], rec["tip"], rec["tip_index"], rec["n_features"] = feats, tip, tip_idx, n
    sheet = fc.sheet_data(o) if getattr(o, "FcSheet", "") else None
    if sheet:                                       # bent sheet metal (fc.sheet_metal)
        try:
            rec["sheet"] = {"t": float(sheet["thickness"]), "k": float(sheet["k_factor"]),
                            "flat": [float(x) for x in sheet["flat_size"]],
                            "bends": [[float(b["angle"]), bool(b["up"]), float(b["radius"])] for b in sheet["bends"]]}
        except (KeyError, TypeError, ValueError):
            pass
    return rec


def bb_of(rec):
    b = rec["bbox"]
    return App.BoundBox(b[0], b[1], b[2], b[3], b[4], b[5])


# ---------------------------------------------------------------- reporting

def object_lines(recs):
    lines = []
    w = max([len(r["label"]) for r in recs] + [6])
    for r in recs:
        bb = bb_of(r)
        if r["role"] == "ref":
            b = r.get("bom") or {}
            tag = ""
            if b.get("bom") == "buy":
                tag = "  [buy ×%d%s]" % (b.get("qty", 1), ": %s" % b["spec"] if b.get("spec") else "")
            elif b.get("bom") == "none":
                tag = "  [keep-out — not listed]"
            lines.append("  %-*s  ref      bbox %s  at (%s, %s, %s)%s" % (
                w, r["label"], dims(bb), fmt(bb.XMin, 1), fmt(bb.YMin, 1), fmt(bb.ZMin, 1), tag))
            continue
        sm = r.get("sheet")
        kind = "Body" if r["type"] == "PartDesign::Body" else "Sheet" if sm else r["type"].split("::")[-1]
        ok = r["solids"] > 0 and r["valid"]
        head = "  %-*s  %-7s  solid %s valid %s  %s mm³" % (
            w, r["label"], kind, "✓" if r["solids"] else "✗ (%d)" % r["solids"],
            "✓" if r["valid"] else "✗", fmt(r["volume"], 1))
        if "mass_g" in r:
            head += "  %s g" % fmt(r["mass_g"], 1)
        head += "  bbox %s" % dims(bb)
        if sm:
            head += "  sheet t=%g, %d bend%s" % (sm["t"], len(sm["bends"]), "" if len(sm["bends"]) == 1 else "s")
        if not ok:
            head += "  ← NOT A VALID SOLID"
        lines.append(head)
        if sm:
            lines.append("    flat pattern %s × %s mm · K %g%s" % (
                an._g(round(sm["flat"][0], 2)), an._g(round(sm["flat"][1], 2)), sm["k"], "".join(
                    " · B%d %g° %s R%g" % (i, a, "up" if u else "down", rr)
                    for i, (a, u, rr) in enumerate(sm["bends"], 1))))
        if "features" in r:
            bits = []
            for f in r["features"]:
                if f["error"]:
                    bits.append("%s ✗ \"%s\"" % (f["name"], f["error"]))
                elif "hole" in f:
                    bits.append("%s ✓ (%s)" % (f["name"], f["hole"]))
                else:
                    bits.append("%s ✓" % f["name"])
            if bits:
                lines.append("    " + "  ".join(bits))
            bad = [f for f in r["features"] if f["error"]]
            if bad or r["tip_index"] != r["n_features"] - 1:
                shown = r["tip"]
                good = [f["name"] for f in r["features"] if not f["error"]]
                lines.append("    ⚠ Body result is NOT the full feature list: tip=%s; failed features "
                             "contribute nothing (last good: %s)" % (shown, good[-1] if good else "none"))
    return lines


def _pts(ps, limit=4):
    ps = sorted(ps)
    return ", ".join("(%s)" % ", ".join(_num(c) for c in p) for p in ps[:limit]) + (
        " …+%d" % (len(ps) - limit) if len(ps) > limit else "")


def feature_delta(k, a, b):
    """Feature and hole changes of body k between two object records: features
    added/removed, holes added/removed/moved/resized (from the hole summary)."""
    if "features" not in a or "features" not in b:
        return []
    out = []
    pf = {f["name"]: f for f in a["features"]}
    cf = {f["name"]: f for f in b["features"]}
    for n, f in cf.items():
        if n not in pf:
            out.append("%s +%s%s" % (k, n, " (%s)" % f["hole"] if f.get("hole") else ""))
    for n in pf:
        if n not in cf:
            out.append("%s −%s" % (k, n))
    for n, f in cf.items():
        g = pf.get(n)
        if not g:
            continue
        ha, hb = g.get("hole"), f.get("hole")
        if ha and hb and ha != hb:
            sa, sb = re.match(r"(.*?)(\s+\[.*\])?$", ha), re.match(r"(.*?)(\s+\[.*\])?$", hb)
            if sa.group(2) == sb.group(2):            # same spec as written: compare without it
                ha, hb = sa.group(1), sb.group(1)
            ma, mb = re.match(r"(\d+)× (.*)$", ha), re.match(r"(\d+)× (.*)$", hb)
            if ma and mb and ma.group(2) == mb.group(2):
                out.append("%s %s %s → %s holes" % (k, n, ma.group(1), mb.group(1)))
            else:
                out.append("%s %s was %s, now %s" % (k, n, ha, hb))
        pa, pb = g.get("at"), f.get("at")
        # positions are compared in the body's in-plane coordinates; an older
        # report (global xyz, no "on") cannot be compared
        if pa is None or pb is None or not g.get("on") or not f.get("on"):
            continue
        if g["on"] != f["on"]:
            out.append("%s %s now on a plane normal to %s (was %s) at %s" % (k, n, f["on"], g["on"], _pts(pb)))
            continue
        sa, sb = set(map(tuple, pa)), set(map(tuple, pb))
        add, rem = sb - sa, sa - sb
        axes = _IN_PLANE[f["on"]]
        if add and rem and len(add) == len(rem):
            pairs = _pair_points(rem, add)
            ds = set(tuple(round(q[i] - p[i], 3) + 0.0 for i in range(len(p))) for p, q in pairs)
            if len(ds) == 1:
                who = ("all %d holes" % len(pb)) if len(pairs) == len(pb) and len(pb) > 1 else \
                    "hole%s at %s" % ("s" if len(pairs) > 1 else "", _pts(rem))
                out.append("%s %s %s moved %s" % (k, n, who, _dvec(ds.pop(), axes)))
            else:
                out.append("%s %s holes moved %s%s" % (k, n, ", ".join(
                    "(%s) → (%s)" % (", ".join(_num(c) for c in p), ", ".join(_num(c) for c in q))
                    for p, q in pairs[:4]), " …+%d" % (len(pairs) - 4) if len(pairs) > 4 else ""))
            continue
        bits = []
        if add:
            bits.append("hole%s added at %s" % ("s" if len(add) > 1 else "", _pts(add)))
        if rem:
            bits.append("removed at %s" % _pts(rem))
        if bits:
            out.append("%s %s %s" % (k, n, "; ".join(bits)))
    return out


_IN_PLANE = {"X": "yz", "Y": "xz", "Z": "xy"}


def _pair_points(a, b):
    """Pair every point of a with a distinct point of b, nearest first."""
    a, b = sorted(a), sorted(b)
    cand = sorted((sum((p[i] - q[i]) ** 2 for i in range(len(p))), i, j)
                  for i, p in enumerate(a) for j, q in enumerate(b))
    used_a, used_b, pairs = set(), set(), []
    for _, i, j in cand:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            pairs.append((a[i], b[j]))
    return sorted(pairs)


def _dvec(d, axes="xyz"):
    """'+2 z' / '+1 x −0.5 y' for a displacement (zero components left out)."""
    bits = ["%s%s %s" % ("+" if c > 0 else "−", _num(abs(c)), ax) for c, ax in zip(d, axes) if abs(c) > 1e-9]
    return " ".join(bits) or "0"


def _placement_delta(k, a, b):
    """'<k> moved +2 z' / '<k> rotated 90° about +Z' when the part's placement
    (its own, or set by an assembly joint) changed; else, for a part of the
    same size and volume whose bounding box shifted, '<k> shape shifted …'."""
    pa, pb = a.get("placement"), b.get("placement")
    out = []
    if pa and pb and len(pa) == 7 and len(pb) == 7:
        d = tuple(round(pb[i] - pa[i], 4) + 0.0 for i in range(3))
        if any(abs(c) > 1e-6 for c in d):
            out.append("%s moved %s" % (k, _dvec(d)))
        r = App.Rotation(*pa[3:]).inverted().multiply(App.Rotation(*pb[3:]))
        ang = math.degrees(r.Angle)
        if ang > 1e-4 and abs(ang - 360) > 1e-4:
            out.append("%s rotated %s° about %s" % (k, _num(round(ang, 3) + 0.0), _axis_name(r.Axis)))
        if out:
            return out
    ab, bb = bb_of(a), bb_of(b)
    same = all(abs(x1 - x0) < 1e-6 for x0, x1 in ((ab.XLength, bb.XLength), (ab.YLength, bb.YLength),
                                                  (ab.ZLength, bb.ZLength)))
    va, vb = a.get("volume") or 0.0, b.get("volume") or 0.0
    if same and abs(vb - va) <= 1e-6 * max(abs(va), 1.0):
        d = (round(bb.XMin - ab.XMin, 4) + 0.0, round(bb.YMin - ab.YMin, 4) + 0.0, round(bb.ZMin - ab.ZMin, 4) + 0.0)
        if any(abs(c) > 1e-6 for c in d):
            out.append("%s shape shifted %s (same size and volume)" % (k, _dvec(d)))
    return out


def delta_lines(prev, cur):
    """Changes since the previous build of the same output: objects, volume,
    bbox, features and holes, parameters."""
    if not prev:
        return []
    out = []
    pmap = {r["label"]: r for r in prev.get("objects", [])}
    cmap = {r["label"]: r for r in cur.get("objects", [])}
    for k in cmap:
        if k not in pmap:
            out.append("+%s" % k)
            continue
        a, b = pmap[k], cmap[k]
        if a.get("volume") and b.get("volume") is not None and abs(b["volume"] - a["volume"]) > 1e-6:
            out.append("%s volume %+.1f %% (%s → %s mm³)" % (
                k, 100.0 * (b["volume"] - a["volume"]) / a["volume"], fmt(a["volume"], 0), fmt(b["volume"], 0)))
        ab, bb = bb_of(a), bb_of(b)
        for ax, x0, x1 in (("x", ab.XLength, bb.XLength), ("y", ab.YLength, bb.YLength),
                           ("z", ab.ZLength, bb.ZLength)):
            if abs(x1 - x0) > 1e-6:
                out.append("%s bbox %s %s → %s" % (k, ax, fmt(x0, 2), fmt(x1, 2)))
        out += _placement_delta(k, a, b)
        out += feature_delta(k, a, b)
    for k in pmap:
        if k not in cmap:
            out.append("−%s" % k)
    pp, cp = prev.get("params", {}), cur.get("params", {})
    for k in sorted(set(pp) | set(cp)):
        if pp.get(k) != cp.get(k):
            out.append("param %s %s → %s" % (k, _num(pp[k]) if k in pp else "∅", _num(cp[k]) if k in cp else "∅"))
    return out or ["no geometric change"]


def _rval(v, depth=0):
    """A result value for the report: floats to 6 significant digits; inf/nan as is."""
    if depth > 6:
        return "…"
    if isinstance(v, float):
        return _num(float("%.6g" % v)) if math.isfinite(v) else str(v)
    if isinstance(v, (list, tuple)):
        s = ", ".join(_rval(x, depth + 1) for x in v[:50])
        return "[%s%s]" % (s, ", …+%d" % (len(v) - 50) if len(v) > 50 else "")
    if isinstance(v, dict):
        return "{%s}" % ", ".join("%s: %s" % (k, _rval(x, depth + 1)) for k, x in list(v.items())[:50])
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def result_lines(results):
    """fc.result(...) values from model.py: one 'result' line, or one per key when long.
    A value that cannot be shown is named, never a reason to fail the build."""
    if not results:
        return []
    items = []
    for k, v in results.items():
        try:
            items.append("%s=%s" % (k, _rval(v)[:300]))
        except Exception as e:  # noqa: BLE001 — user data: anything can be in it
            items.append("%s=(unprintable %s: %s)" % (k, type(v).__name__, type(e).__name__))
    one = "result   " + " · ".join(items)
    return [one] if len(one) <= 160 else ["result"] + ["  " + it for it in items]


def _json_safe(v, depth=0):
    """fc.result values as strict JSON: inf/nan and anything not serialisable
    become strings (report.json and the worker response must always write)."""
    if depth > 20:
        return "…"
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x, depth + 1) for k, x in v.items()}
    try:
        return str(v)
    except Exception as e:  # noqa: BLE001
        return "(unprintable %s: %s)" % (type(v).__name__, type(e).__name__)


def assembly_lines(doc):
    """analysis.assembly_lines, saying for each joint fc.joint pre-placed that
    part b was placed by the joint (not moved there by the solver)."""
    lines = an.assembly_lines(doc)
    placed = {}
    for j in doc.Objects:
        if "FcJoint" in j.PropertiesList:
            try:
                info = json.loads(j.FcJoint or "{}")
            except ValueError:
                continue
            if info.get("placed"):
                b = doc.getObject(info.get("b") or "")
                placed[j.Label] = b.Label if b else info.get("b")
    out = []
    for ln in lines:
        for lab, b in placed.items():
            if ln.startswith("  %s " % lab) and ln.endswith("holds ✓"):
                ln += " (%s placed by the joint and verified)" % b
                break
        out.append(ln)
    return out


def collect(doc, dens):
    recs = [object_record(o, dens) for o in results(doc)]
    sks = [sketch_info(o) for o in doc.Objects if o.TypeId == "Sketcher::SketchObject"]
    errs = [(o.Label, e) for o, e in doc_errors(doc)]
    return recs, sks, errs


def sketch_lines(sks):
    if not sks:
        return []
    bad = [s for s in sks if not s["full"] or s["conflicting"] or s["redundant"] or s["malformed"]]
    if not bad:
        return ["sketches  %d, all fully constrained ✓" % len(sks)]
    out = ["sketches  %d, %d need attention:" % (len(sks), len(bad))]
    for s in bad:
        msg = []
        if s["conflicting"]:
            msg.append("conflicting constraints %s" % s["conflicting"])
        if s["redundant"]:
            msg.append("redundant %s" % s["redundant"])
        if s["malformed"]:
            msg.append("malformed %s" % s["malformed"])
        if not s["full"]:
            msg.append("%s DoF (under-constrained)" % (s["dof"] if s["dof"] is not None else "?"))
        out.append("  %s: %s" % (s["name"], "; ".join(msg)))
    return out


def hide_consumed(doc):
    """Console-built documents have every object visible; hide the inputs so
    the file opens in the GUI showing only finished parts."""
    keep = set(o.Name for o in results(doc))
    for o in doc.Objects:
        if hasattr(o, "Visibility") and has_shape(o) and o.Name not in keep \
                and o.TypeId not in an._GROUPS:
            try:
                o.Visibility = False
            except Exception:  # noqa: BLE001
                pass


def render_items(doc, objs, highlight=None):
    trip = []
    for o in objs:
        s = global_shape(o)
        if not s.Faces:
            continue
        trip.append((o.Label, s, role(o) == "ref"))
    items = rd.pick_colours(trip, highlight=highlight)
    for it, o in zip(items, [o for o in objs if global_shape(o).Faces]):
        col = getattr(o, "FcColor", "")
        if col and not highlight:
            try:
                it.colour = tuple(float(c) for c in col.split(","))
            except ValueError:
                pass
    return items


def parse_size(size, default=(1024, 768)):
    if not size:
        return default
    if isinstance(size, (list, tuple)):
        return int(size[0]), int(size[1])
    m = re.match(r"^\s*(\d+)\s*[x,×]\s*(\d+)\s*$", str(size))
    if not m:
        raise UserError("size must be 'WIDTHxHEIGHT', e.g. '1024x768'")
    return int(m.group(1)), int(m.group(2))


_SECTION_NORMALS = {"x": (1, 0, 0), "y": (0, -1, 0), "z": (0, 0, 1)}


def parse_section(sec):
    """{'z': 12} → cut at z=12 removing the side toward the standard viewer
    (+X, −Y, +Z); 'flip': true removes the other side. Or {point, normal}:
    removes the +normal side."""
    if not sec:
        return None
    flip = bool(sec.get("flip"))
    for ax in ("x", "y", "z"):
        if ax in sec:
            n = _SECTION_NORMALS[ax]
            p = [0.0, 0.0, 0.0]
            p["xyz".index(ax)] = float(sec[ax])
            if flip:
                n = tuple(-c for c in n)
            return tuple(p), n
    if "point" in sec and "normal" in sec:
        n = tuple(float(c) for c in sec["normal"])
        if flip:
            n = tuple(-c for c in n)
        return tuple(float(c) for c in sec["point"]), n
    raise UserError("section must be {x|y|z: offset} or {point: [..], normal: [..]}")


def run_user_code(src, filename, glb):
    """exec() user code; return (stdout, error-text or None)."""
    buf = io.StringIO()
    err = None
    try:
        code = compile(src, filename, "exec")
        with contextlib.redirect_stdout(buf):
            exec(code, glb)  # noqa: S102 — running the user's model is the point
    except Exception as e:  # noqa: BLE001
        err = format_user_error(e, filename, src)
    return buf.getvalue(), err


def format_user_error(e, filename, src):
    tb = traceback.extract_tb(e.__traceback__)
    lines = src.splitlines()
    frames = [f for f in tb if f.filename == filename]
    where = ""
    if frames:
        f = frames[-1]
        code = lines[f.lineno - 1].strip() if 0 < f.lineno <= len(lines) else ""
        where = "%s:%d  %s\n" % (os.path.basename(filename), f.lineno, code)
    inner = [f for f in tb if f.filename != filename and "fclib" in f.filename]
    via = (" (in fclib.%s)" % inner[-1].name) if inner else ""
    if isinstance(e, SyntaxError):
        return "%s:%s  SyntaxError: %s\n  %s" % (os.path.basename(filename), e.lineno, e.msg,
                                                 (e.text or "").strip())
    return "%s%s%s: %s" % (where, type(e).__name__, via, e)


def base_globals(doc):
    import Sketcher as _Sk
    return {"__name__": "__fc_model__", "fc": fc, "App": App, "FreeCAD": App, "Part": Part,
            "Sketcher": _Sk, "Vector": App.Vector, "Placement": App.Placement,
            "Rotation": App.Rotation, "doc": doc}


# ---------------------------------------------------------------------- ops

def op_probe(args):
    mods = {}
    for m in ("Part", "PartDesign", "Sketcher", "TechDraw", "Import", "Mesh", "MeshPart", "numpy", "PIL"):
        try:
            __import__(m)
            mods[m] = True
        except Exception:  # noqa: BLE001
            mods[m] = False
    v = App.Version()
    return {"text": "FreeCAD %s.%s.%s (%s)" % (v[0], v[1], v[2], v[3] if len(v) > 3 else ""),
            "data": {"version": ".".join(v[:3]), "modules": mods, "python": sys.version.split()[0],
                     "user_data": App.getUserAppDataDir(), "macro_dir": App.getUserMacroDir(True)}}


def _history_dir(pdir):
    h = os.path.join(pdir, ".history")
    os.makedirs(h, exist_ok=True)
    return h


def _revs(pdir):
    h = _history_dir(pdir)
    return sorted(d for d in os.listdir(h) if re.match(r"^r\d{4}$", d))


def _last_rev(pdir):
    revs = _revs(pdir)
    return revs[-1] if revs else None


def _new_rev(pdir, after):
    """Allocate the next rNNNN folder; mkdir is atomic, so two builds never get the same one."""
    n = int(after[1:]) + 1 if after else 1
    while True:
        rev = "r%04d" % n
        try:
            os.mkdir(os.path.join(_history_dir(pdir), rev))
            return rev
        except FileExistsError:
            n += 1


@contextlib.contextmanager
def history_lock(pdir):
    """Serialise builds of one project (rev numbers, last_build.json); yields
    the seconds spent waiting for another build."""
    try:
        import fcntl
    except ImportError:                  # Windows: rev folders are still allocated atomically
        yield 0.0
        return
    with open(os.path.join(_history_dir(pdir), ".lock"), "a") as fh:
        t0 = time.time()
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield time.time() - t0
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def inputs_sha1(pdir, overrides=None, variant=None):
    """sha1 over everything a build reads: model.py, params/checks/project/
    drawings JSON (without the server's "_sheets" bookkeeping), every file
    under refs/, the overrides and the variant."""
    h = hashlib.sha1()

    def add(rel, p):
        h.update(rel.encode("utf-8") + b"\0")
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")

    for rel in ("model.py", "params.json", "checks.json", "project.json", "drawings.json"):
        p = os.path.join(pdir, rel)
        if not os.path.isfile(p):
            continue
        try:
            d = load_json(p) if rel == "drawings.json" else None
        except ValueError:
            d = None
        if isinstance(d, dict):              # a saved sheet is not a model input
            d.pop("_sheets", None)
            h.update(rel.encode("utf-8") + b"\0" + json.dumps(d, sort_keys=True).encode("utf-8") + b"\0")
        else:
            add(rel, p)
    refs = os.path.join(pdir, "refs")
    for root, dirs, files in os.walk(refs):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if not f.startswith("."):
                p = os.path.join(root, f)
                add(os.path.relpath(p, pdir).replace(os.sep, "/"), p)
    h.update(json.dumps({"params": overrides or {}, "variant": variant or None}, sort_keys=True,
                        default=str).encode("utf-8"))
    return h.hexdigest()


def op_build(args):
    pdir = args["project_dir"]
    model = os.path.join(pdir, "model.py")
    if not os.path.isfile(model):
        raise UserError("no model.py in %s" % pdir)
    with history_lock(pdir) as waited:
        return _build(args, pdir, model, waited)


def _build(args, pdir, model, waited):
    project = load_json(os.path.join(pdir, "project.json"), {}) or {}
    name = project.get("name") or os.path.basename(pdir.rstrip("/"))
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "model"
    variant = args.get("variant")
    if variant:
        stem += "__" + re.sub(r"[^A-Za-z0-9_-]+", "_", variant)
    out_fc = os.path.join(pdir, "build", stem + ".FCStd")
    os.makedirs(os.path.dirname(out_fc), exist_ok=True)
    os.makedirs(os.path.join(pdir, "out"), exist_ok=True)

    # Refuse to clobber a file edited outside the build loop (e.g. in the
    # FreeCAD GUI) — but say exactly what changed, and adopt parameter edits
    # back into params.json when asked and nothing structural would be lost.
    lb_path = os.path.join(_history_dir(pdir), "last_build.json")
    last_builds = load_json(lb_path, {}) or {}
    snap_path = os.path.join(_history_dir(pdir), "snapshot_%s.json" % stem)
    notes = []
    if waited > 0.5:
        notes.append("waited %.1f s for another build of this project to finish" % waited)
    disk_sha = _sha1(out_fc) if os.path.exists(out_fc) else None
    lb_key = _lb_key(pdir, out_fc)
    rec = _lb_record(last_builds, lb_key, out_fc, disk_sha)
    if disk_sha and isinstance(rec, (int, float)):   # record from an older server version
        rec = {"sha1": disk_sha} if abs(os.path.getmtime(out_fc) - rec) < 1e-3 else None
    # the file on disk is not the one the last build wrote (saved in the GUI …)
    edited = bool(disk_sha) and (not isinstance(rec, dict) or rec.get("sha1") != disk_sha)
    adopt_changes = None
    if edited and not args.get("force"):
        changes, pchanges = gui_changes(pdir, out_fc, snap_path, model, args)
        if changes is None:
            raise UserError(
                "%s was modified outside the build loop and there is no snapshot of the last "
                "build to compare against. Inspect it, port any edits into model.py / params.json, "
                "then build with force=true." % out_fc)
        adopt = bool(pchanges) and bool(args.get("adopt_gui_params"))
        structural = [c for c in changes if not c.startswith("param ")]
        if structural or (pchanges and not adopt):
            if structural:
                todo = ("Port structural edits into model.py, then build again%s — or build with force=true "
                        "to rebuild from model.py and discard the GUI edits (the edited file is copied to "
                        ".history/backups/ first)." % (
                            " with adopt_gui_params=true to keep the parameter edits" if pchanges else ""))
                if adopt:
                    todo = ("params.json was left as it was: parameter edits are adopted only when nothing "
                            "structural would be lost. " + todo)
            else:
                todo = ("Pass adopt_gui_params=true to copy the parameter edits into params.json, or build "
                        "with force=true to discard them (the edited file is copied to .history/backups/ first).")
            raise UserError("%s was edited outside the build loop (FreeCAD GUI?). Rebuilding would discard:\n"
                            "  %s\n%s" % (out_fc, "\n  ".join(changes), todo))
        if adopt:
            adopt_changes = pchanges
        else:
            notes.append("build file was touched outside the build loop but has no edits — rebuilt")
    if edited:
        # never overwrite the user's saved file without a copy: the snapshot
        # diff does not cover everything (view properties, colours …)
        bk = _backup_build(pdir, out_fc)
        notes.append("kept the build file as saved outside the build loop: %s (build/%s is rebuilt from "
                     "model.py)" % (os.path.relpath(bk, pdir), os.path.basename(out_fc)))
    if adopt_changes:
        bk = adopt_params(pdir, adopt_changes)
        args = dict(args, params={k: v for k, v in (args.get("params") or {}).items() if k not in adopt_changes})
        notes.append("adopted GUI parameter edits into params.json: %s (previous params.json: %s)" % (
            ", ".join("%s=%s" % (k, _num(v)) for k, v in sorted(adopt_changes.items())),
            os.path.relpath(bk, pdir) if bk else "none"))

    # Same inputs as the newest revision and its build file untouched → the
    # same revision (a rebuild for a release, a retry); otherwise a new one.
    in_sha = inputs_sha1(pdir, args.get("params"), variant)
    revs = _revs(pdir)
    cand, last = None, {}
    if revs and disk_sha and isinstance(rec, dict) and rec.get("sha1") == disk_sha and rec.get("rev") == revs[-1]:
        last = load_json(os.path.join(pdir, ".history", revs[-1], "report.json"), {}) or {}
        if last.get("inputs_sha1") == in_sha:
            cand = revs[-1]

    t0 = time.time()
    doc = App.newDocument(re.sub(r"\W+", "_", stem))
    fc._setup(doc, pdir, args.get("params") or {})
    with open(model) as fh:
        src = fh.read()
    stdout, script_err = run_user_code(src, model, base_globals(doc))
    try:
        doc.recompute()
    except Exception as e:  # noqa: BLE001
        script_err = (script_err + "\n" if script_err else "") + "recompute failed: %s" % e
    hide_consumed(doc)
    save_as(doc, out_fc)
    # files model.py read through fclib (fc.ref_part, fc.path) that inputs_sha1
    # does not cover must match the newest revision's too
    reads = _reads(pdir, fc._ctx.reads)
    reuse = None
    if cand:
        was = last.get("reads") if isinstance(last.get("reads"), dict) else None
        if was == reads or (was is None and not reads):
            reuse = cand
        elif was is None:
            notes.append("%s did not record the files model.py reads (%s) — new revision" % (
                cand, _few(sorted(reads, key=_inside_first))))
        else:
            notes.append("%s changed since %s (read by model.py) — new revision" % (
                _few(sorted((k for k in set(was) | set(reads) if was.get(k) != reads.get(k)), key=_inside_first)),
                cand))
    if reuse:
        notes.append("inputs unchanged — still %s" % reuse)
    rev = reuse or _new_rev(pdir, revs[-1] if revs else None)
    # Δ against the previous build of the SAME output (main or this variant)
    prev_rev, prev = _prev_build(pdir, revs, rev, variant)
    last_builds = load_json(lb_path, {}) or {}
    # keyed by the path inside the project, so a moved or copied project still matches
    tail = os.sep + lb_key.replace("/", os.sep)
    for k in [k for k in last_builds if os.path.isabs(k) and k.endswith(tail)]:
        del last_builds[k]                       # an older server's absolute key for this file
    last_builds[lb_key] = {"sha1": _sha1(out_fc), "mtime": os.path.getmtime(out_fc), "rev": rev}
    with open(lb_path, "w") as fh:
        json.dump(last_builds, fh, indent=1)
    with open(snap_path, "w") as fh:
        json.dump(dict(an.snapshot(doc), overrides=args.get("params") or {}), fh)
    elapsed = time.time() - t0

    dens = densities(project)
    recs, sks, errs = collect(doc, dens)
    params = dict(fc._ctx.param_values)
    checks = load_json(os.path.join(pdir, "checks.json"), []) or []
    if isinstance(checks, dict):
        checks = checks.get("checks", [])
    check_lines, check_res, fea_stale = [], [], []
    if checks and not args.get("skip_checks"):
        clr = float(((project.get("clearance") or {}).get("default_min_mm")) or 0.0)
        geo = [c for c in checks if not (isinstance(c, dict) and str(c.get("type", "")).startswith("fea_"))]
        check_lines, check_res = an.evaluate_checks(doc, geo, dens, clr)
        fl, fr, fea_stale = fea_checks(pdir, project, [c for c in checks if c not in geo], doc)
        check_lines += fl
        check_res += fr
    report = {"objects": recs, "sketches": sks, "errors": errs, "params": params,
              "warnings": list(fc._ctx.warnings), "script_error": script_err, "checks": check_res,
              "variant": variant, "overrides": args.get("params") or {}, "time": elapsed,
              "rev": rev, "inputs_sha1": in_sha, "results": _json_safe(fc._ctx.results),
              "reads": reads}

    rdir = os.path.join(pdir, ".history", rev)
    os.makedirs(rdir, exist_ok=True)
    # everything needed to rebuild this revision
    for f in SNAPSHOT_FILES:
        if os.path.isfile(os.path.join(pdir, f)):
            shutil.copyfile(os.path.join(pdir, f), os.path.join(rdir, f))
    with open(os.path.join(rdir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    n_err = len(errs) + (1 if script_err else 0) + sum(
        1 for r in recs if r["role"] != "ref" and (not r["valid"] or not r["solids"]))
    n_warn = len(report["warnings"]) + sum(1 for s in sks if not s["full"])
    ok = n_err == 0
    n_pass = sum(1 for c in check_res if c["ok"])
    lines = ["BUILD %s  %s%s  %s  %.1f s   (%d error%s · %d warning%s%s)" % (
        "✓" if ok else "✗", name, " [%s]" % variant if variant else "", rev, elapsed,
        n_err, "" if n_err == 1 else "s", n_warn, "" if n_warn == 1 else "s",
        (" · checks %d/%d%s" % (n_pass, len(check_res), "" if n_pass == len(check_res) else " ✗"))
        if check_res else "")]
    lines += ["note     " + n for n in notes]
    if script_err:
        lines.append("script error")
        lines += ["  " + ln for ln in script_err.splitlines()]
        lines.append("  (the document was saved as far as the script got)")
    if params:
        ov = args.get("params") or {}
        lines.append("params   " + " · ".join("%s=%s%s" % (k, _num(v), "*" if k in ov else "")
                                              for k, v in sorted(params.items())))
    if recs:
        lines.append("objects" + mass_note(project, recs))
        lines += object_lines(recs)
    else:
        lines.append("objects  none — model.py created no solid geometry")
    lines += assembly_lines(doc)
    lines += sketch_lines(sks)
    lines += result_lines(fc._ctx.results)
    if check_lines:
        lines.append("checks   (checks.json)")
        lines += check_lines
    if errs:
        lines.append("errors")
        lines += ["  %s: %s" % (n, e) for n, e in errs]
    if report["warnings"]:
        lines.append("warnings")
        lines += ["  " + w for w in report["warnings"]]
    if stdout.strip():
        lines.append("stdout")
        lines += ["  " + ln for ln in stdout.strip().splitlines()[-30:]]
    d = delta_lines(prev, report)
    if d:
        lines.append("Δ %s   %s%s" % (prev_rev, " · ".join(d[:12]), " · …%d more" % (len(d) - 12) if len(d) > 12 else ""))
    images = []
    views = args.get("views", ["iso"])
    shown = [o for o in results(doc)]
    if views and shown:
        png = os.path.join(pdir, "out", stem + "_build.png")
        try:
            rd.render(render_items(doc, shown), png, views=views,
                      size=parse_size(args.get("size"), (900, 675) if len(views) == 1 else (1024, 768)))
            images.append(png)
        except Exception as e:  # noqa: BLE001
            lines.append("render failed: %s" % e)
    lines.append("files    %s%s" % (os.path.relpath(out_fc, pdir),
                                    "  ·  " + os.path.relpath(images[0], pdir) if images else ""))
    text = "\n".join(lines) + "\n"
    for p in (os.path.join(rdir, "report.txt"), os.path.join(pdir, "build", "report.txt")):
        with open(p, "w") as fh:
            fh.write(text)
    return {"text": "\n".join(lines), "images": images,
            "data": {"ok": ok, "rev": rev, "fcstd": out_fc, "checks_passed": n_pass, "checks": len(check_res),
                     "inputs_sha1": in_sha, "reads": report["reads"], "reused": bool(reuse), "prev_rev": prev_rev,
                     "results": report["results"], "fea_stale": fea_stale}}


SNAPSHOT_FILES = ("model.py", "params.json", "checks.json", "project.json", "drawings.json")
_OUTPUT_DIRS = ("build", "out", ".history", "release", "drawings")


def _lb_key(pdir, out_fc):
    """last_build.json key: the build file's path inside the project ('build/x.FCStd')."""
    return os.path.relpath(out_fc, pdir).replace(os.sep, "/")


def _lb_record(last_builds, key, out_fc, disk_sha):
    """The last_build.json record of this build file: by its project-relative
    key; else an older server's absolute key — the same path, or (a moved or
    copied project) any path ending in the same build/<file> whose sha1 is
    the file's."""
    if key in last_builds:
        return last_builds[key]
    if out_fc in last_builds:
        return last_builds[out_fc]
    tail = os.sep + key.replace("/", os.sep)
    for k, v in last_builds.items():
        if os.path.isabs(k) and k.endswith(tail) and isinstance(v, dict) and disk_sha and v.get("sha1") == disk_sha:
            return v
    return None


def _read_key(pdir, p):
    """A read file's key: project-relative inside the project, else absolute;
    None for files inputs_sha1 already covers and for build outputs."""
    ap = os.path.abspath(p)
    rel = os.path.relpath(ap, pdir) if pdir else ap
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return ap
    rel = rel.replace(os.sep, "/")
    if rel in SNAPSHOT_FILES or rel == "." or rel.split("/")[0] in ("refs",) + _OUTPUT_DIRS:
        return None
    return rel


def _reads(pdir, paths):
    """{file: sha1 (None when missing or not a file)} of the files model.py read
    through fclib (fc.ref_part, fc.path) that inputs_sha1 does not cover."""
    out = {}
    for p in paths:
        k = _read_key(pdir, p)
        if k is not None and k not in out:
            out[k] = _file_sha(k if os.path.isabs(k) else os.path.join(pdir, k))
    return out


def _file_sha(p):
    try:
        return _sha1(p) if os.path.isfile(p) else None
    except OSError:
        return None


def _inside_first(k):
    return os.path.isabs(k), k


def _few(names, n=4):
    return ", ".join(names[:n]) + (" …+%d" % (len(names) - n) if len(names) > n else "")


def _prev_build(pdir, revs, rev, variant):
    """(rev, report) of the newest build before `rev` of the same output — the
    main build or the same variant — or (None, None)."""
    for r in reversed(revs):
        if r >= rev:
            continue
        try:
            rep = load_json(os.path.join(pdir, ".history", r, "report.json"))
        except ValueError:
            continue
        if isinstance(rep, dict) and (rep.get("variant") or None) == (variant or None):
            return r, rep
    return None, None


def _backup_name(bdir, stem, ext):
    ts = time.strftime("%Y%m%d-%H%M%S")
    p, k = os.path.join(bdir, "%s.%s%s" % (stem, ts, ext)), 2
    while os.path.exists(p):
        p, k = os.path.join(bdir, "%s.%s-%d%s" % (stem, ts, k, ext)), k + 1
    return p


def _backup_build(pdir, out_fc):
    """Copy a build file edited outside the build loop to
    .history/backups/<name>.<timestamp>.FCStd before it is rebuilt."""
    bdir = os.path.join(_history_dir(pdir), "backups")
    os.makedirs(bdir, exist_ok=True)
    p = _backup_name(bdir, os.path.splitext(os.path.basename(out_fc))[0], ".FCStd")
    shutil.copy2(out_fc, p)
    return p


def _sha1(path):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gui_changes(pdir, out_fc, snap_path, model, args):
    """(change lines, {param: new value}) between the last build and the file
    on disk, or (None, {}) without a snapshot. Parameter edits are separated
    from structural edits by rebuilding model.py with the edited parameters
    and diffing that against the file."""
    old = load_json(snap_path)
    if not old:
        return None, {}
    disk_doc = App.openDocument(out_fc)
    disk = an.snapshot(disk_doc)
    App.closeDocument(disk_doc.Name)
    plines, pchanges = an.diff_snapshots({"params": old.get("params", {}), "objects": {}},
                                         {"params": disk.get("params", {}), "objects": {}})
    base = old
    if pchanges:
        tmp = App.newDocument("Rebuild")
        fc._setup(tmp, pdir, dict(old.get("overrides") or {}, **pchanges))
        with open(model) as fh:
            _, err = run_user_code(fh.read(), model, base_globals(tmp))
        tmp.recompute()
        hide_consumed(tmp)
        base = an.snapshot(tmp) if not err else old
        App.closeDocument(tmp.Name)
        if err:
            plines.append("note     could not rebuild with the edited parameters (%s); the list below "
                          "may include their side effects" % err.splitlines()[-1])
    slines, _ = an.diff_snapshots(dict(base, params={}), dict(disk, params={}))
    return plines + slines, pchanges


def adopt_params(pdir, changes):
    """Write GUI parameter edits into params.json, changing only those values
    in the file's text (its layout is kept; whole numbers stay integers). The
    previous file goes to .history/backups/params.json.<timestamp>.bak, whose
    path is returned (None when there was no params.json)."""
    pj = os.path.join(pdir, "params.json")
    text = open(pj).read() if os.path.isfile(pj) else ""
    data = json.loads(text) if text.strip() else {}
    bk = None
    if os.path.isfile(pj):
        bdir = os.path.join(_history_dir(pdir), "backups")
        os.makedirs(bdir, exist_ok=True)
        bk = _backup_name(bdir, "params.json", ".bak")
        shutil.copyfile(pj, bk)
    note = "edited in the FreeCAD GUI %s" % time.strftime("%Y-%m-%d %H:%M")
    want = json.loads(json.dumps(data))
    for k, v in changes.items():
        v = int(v) if isinstance(v, float) and v.is_integer() else v
        if isinstance(want.get(k), dict):
            want[k]["value"] = v
            want[k]["note"] = note
        elif k in want:
            want[k] = v                          # a bare number stays a bare number
        else:
            want[k] = {"value": v, "unit": "mm", "note": note}
    try:
        new = _patch_json(text, data, want)
        if json.loads(new) != want:
            raise ValueError("patch mismatch")
    except (ValueError, IndexError, KeyError, AttributeError):
        new = json.dumps(want, indent=2) + "\n"
    with open(pj, "w") as fh:
        fh.write(new)
    return bk


_DEC = json.JSONDecoder()
_WS_COMMA = re.compile(r"[\s,]*")
_COLON = re.compile(r"\s*:\s*")


def _members(text, i):
    """([(key, key_start, value_start, value_end)], closing-brace index) of the
    JSON object starting at text[i] == '{'."""
    if text[i] != "{":
        raise ValueError("not an object")
    out = []
    i += 1
    while True:
        i = _WS_COMMA.match(text, i).end()
        if text[i] == "}":
            return out, i
        k0 = i
        key, i = _DEC.raw_decode(text, i)
        i = _COLON.match(text, i).end()
        _, j = _DEC.raw_decode(text, i)
        out.append((key, k0, i, j))
        i = j


def _insert_member(text, members, close, key, value, edits):
    """Edit that adds "key": value to an object after its last member, with
    the separator the object already uses."""
    item = json.dumps(key) + ": " + json.dumps(value)
    if not members:
        edits.append((close, close, item))
        return
    sep = text[members[0][3]:members[1][1]] if len(members) > 1 else ", "
    edits.append((members[-1][3], members[-1][3], sep + item))


def _patch_json(text, old, want):
    """`text` with the changed top-level values of `want` written in place."""
    top, close = _members(text, _WS_COMMA.match(text, 0).end())
    pos = {m[0]: m for m in top}
    edits = []
    for k, v in want.items():
        if k in old and old[k] == v and type(old[k]) is type(v):
            continue
        if k not in pos:
            _insert_member(text, top, close, k, v, edits)
            continue
        _, _, v0, v1 = pos[k]
        if isinstance(v, dict) and isinstance(old.get(k), dict):
            sub, sclose = _members(text, v0)
            spos = {m[0]: m for m in sub}
            for sk, sv in v.items():
                if sk in old[k] and old[k][sk] == sv and type(old[k][sk]) is type(sv):
                    continue
                if sk in spos:
                    edits.append((spos[sk][2], spos[sk][3], json.dumps(sv)))
                else:
                    _insert_member(text, sub, sclose, sk, sv, edits)
        else:
            edits.append((v0, v1, json.dumps(v)))
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text


def _num(v):
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return str(int(v))
    return str(v)


def _revision_base(docpath):
    """(path stem without .rNN, own revision number): Plate.r02.FCStd → (Plate, 2), Plate.step → (Plate, 0)."""
    stem = os.path.splitext(docpath)[0]
    m = re.search(r"\.r(\d{2,})$", stem)
    return (stem[:m.start()], int(m.group(1))) if m else (stem, 0)


def _revisions(stem):
    """{n: path} of the <stem>.rNN.FCStd files that exist."""
    d, b = os.path.split(stem)
    out = {}
    for f in os.listdir(d or "."):
        m = re.match(re.escape(b) + r"\.r(\d{2,})\.FCStd$", f, re.I)
        if m:
            out[int(m.group(1))] = os.path.join(d, f)
    return out


def _reserve_revision(stem):
    """Claim the next free <stem>.rNN.FCStd with an O_EXCL placeholder, so two
    patches started together never write the same revision."""
    n = max(_revisions(stem) or [0]) + 1
    while True:
        p = "%s.r%02d.FCStd" % (stem, n)
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return p
        except FileExistsError:
            n += 1


def op_run_script(args):
    docpath = args.get("doc")
    pdir = args.get("project_dir") or (os.path.dirname(docpath) if docpath else None)
    save = args.get("save") or "no"
    forked = None
    if save == "revision" and not args.get("save_as"):
        if not docpath:
            raise UserError("save='revision' needs doc (or use save_as for a new document)")
        stem, own = _revision_base(docpath)
        revs = _revisions(stem)
        if revs and max(revs) > own:
            newest = revs[max(revs)]
            if not args.get("from_older"):
                raise UserError(
                    "%s is not the newest revision — %s is. Run the script on doc=\"%s\" to build on the latest "
                    "edits, or pass from_older=true to fork a new revision from %s deliberately."
                    % (os.path.basename(docpath), os.path.basename(newest), newest, os.path.basename(docpath)))
            forked = newest
    doc = load_doc(docpath) if docpath else App.newDocument("Scratch")
    fc._setup(doc, pdir, {})
    before = {o.Name: (o.TypeId, round(o.Shape.Volume, 6) if has_shape(o) and o.Shape.Solids else None)
              for o in doc.Objects}
    snap0 = dict(an.snapshot(doc), sheets=sheet_cells(doc)) if docpath else None
    stdout, err = run_user_code(args["code"], "<run_script>", base_globals(doc))
    try:
        doc.recompute()
    except Exception as e:  # noqa: BLE001
        err = (err + "\n" if err else "") + "recompute failed: %s" % e
    after = {o.Name: (o.TypeId, round(o.Shape.Volume, 6) if has_shape(o) and o.Shape.Solids else None)
             for o in doc.Objects}
    added = [n for n in after if n not in before]
    removed = [n for n in before if n not in after]
    changed = [n for n in after if n in before and after[n] != before[n]]

    lines = ["RUN %s" % ("✗" if err else "✓")]
    if err:
        lines += ["error"] + ["  " + ln for ln in err.splitlines()]
    if stdout.strip():
        out = stdout.strip().splitlines()
        lines.append("stdout" + (" (last 60 of %d lines)" % len(out) if len(out) > 60 else ""))
        lines += ["  " + ln for ln in out[-60:]]
    if fc._ctx.results:
        lines.append("result")
        lines.append("  " + json.dumps(_json_safe(fc._ctx.results), indent=1).replace("\n", "\n  "))
    if added or removed or changed:
        lab = lambda n: (doc.getObject(n).Label if doc.getObject(n) else n)  # noqa: E731
        lines.append("changes  added: %s · removed: %s · shape changed: %s" % (
            ", ".join(lab(n) for n in added) or "-", ", ".join(removed) or "-",
            ", ".join(lab(n) for n in changed) or "-"))
    edits = []
    if snap0 is not None:
        edits = edit_lines(doc, snap0, dict(an.snapshot(doc), sheets=sheet_cells(doc)), changed,
                           os.path.basename(docpath),
                           show_none=save != "no" or bool(args.get("save_as")))
        lines += edits
    errs = doc_errors(doc)
    if errs:
        lines.append("errors")
        lines += ["  %s: %s" % (o.Label, e) for o, e in errs]
    if fc._ctx.warnings:
        lines.append("warnings")
        lines += ["  " + w for w in fc._ctx.warnings]

    saved = None
    if save == "revision":
        if args.get("save_as"):
            saved = args["save_as"]
            save_as(doc, saved)
        else:
            saved = _reserve_revision(_revision_base(docpath)[0])
            try:
                save_as(doc, saved)
            except Exception:
                os.remove(saved)            # drop the empty placeholder
                raise
    elif save == "inplace":
        if not docpath or not docpath.lower().endswith(".fcstd"):
            raise UserError("save='inplace' needs an .FCStd doc")
        shutil.copyfile(docpath, docpath + ".bak")
        save_as(doc, docpath, inplace=True)
        saved = docpath
    elif args.get("save_as"):
        saved = args["save_as"]
        save_as(doc, saved)
    if saved:
        lines.append("saved    %s%s" % (saved, "  (backup: %s.bak)" % os.path.basename(docpath)
                                         if save == "inplace" else ""))
        if forked:
            lines.append("note     forked from %s although %s is newer (from_older=true)" % (
                os.path.basename(docpath), os.path.basename(forked)))
    images = []
    if args.get("views"):
        # inside a project: its out/; a loose document: the image cache, never
        # a folder next to the user's file
        png = args.get("image_path") or image_path(args, "run_script_%s.png" % time.strftime("%H%M%S"))
        os.makedirs(os.path.dirname(png), exist_ok=True)
        shown = results(doc)
        if shown:
            rd.render(render_items(doc, shown), png, views=args["views"], size=parse_size(args.get("size")))
            images.append(png)
    return {"text": "\n".join(lines), "images": images,
            "data": {"ok": not err, "saved": saved, "edits": [ln.strip() for ln in edits[1:]]}}


def sheet_cells(doc):
    """{sheet Name: {label, cells: {addr: content}, values: {addr: value text},
    aliases: {addr: alias}}} of every spreadsheet. The value cells of fclib's
    Params rows are left out: they are compared as parameters."""
    out = {}
    for sh in doc.Objects:
        if sh.TypeId != "Spreadsheet::Sheet":
            continue
        rec = {"label": sh.Label, "cells": {}, "values": {}, "aliases": {}}
        try:
            used = list(sh.getUsedCells())
        except Exception:  # noqa: BLE001
            used = []
        for a in used:
            row = re.match(r"^B(\d+)$", a) if sh.Name == "Params" else None
            try:
                c = sh.getContents(a) or ""
                al = sh.getAlias(a) or ""
                pname = (sh.getContents("A" + row.group(1)) or "").lstrip("'") \
                    if row and int(row.group(1)) > 1 else ""
            except Exception:  # noqa: BLE001
                continue
            if pname:                            # a parameter's value: compared as a parameter
                rec.setdefault("params", {})[al or pname] = pname
                continue
            if al:
                rec["aliases"][a] = al
            if c:
                rec["cells"][a] = c
                try:
                    v = sh.get(a)
                    rec["values"][a] = _num(round(v, 6) + 0.0) if isinstance(v, float) else str(v)
                except Exception:  # noqa: BLE001
                    rec["values"][a] = "?"
        out[sh.Name] = rec
    return out


def _cell_order(a):
    m = re.match(r"^([A-Z]+)(\d+)$", a)
    return (int(m.group(2)), len(m.group(1)), m.group(1)) if m else (0, 0, a)


_REF = re.compile(r"(?:<<(.+?)>>|(?<![\w.])([A-Za-z_]\w*))\.([A-Za-z_]\w*)")


def _expr_refs(doc, expr, aliases):
    """(object Name, member) pairs an expression reads: 'Spreadsheet.length' →
    (Spreadsheet, B1) through the sheet's aliases, '<<Pad>>.Length' → (Pad, Length)."""
    out = set()
    for lab, name, member in _REF.findall(expr or ""):
        o = doc.getObject(name) if name else None
        if o is None:
            hits = doc.getObjectsByLabel(lab or name)
            o = hits[0] if hits else None
        if o is not None:
            out.add((o.Name, aliases.get(o.Name, {}).get(member, member)))
    return out


def _downstream(doc, old, new):
    """Split property changes that only follow an edit through an expression
    (a spreadsheet cell, Params, another property) from real edits. Returns
    (cell edit lines, downstream lines, a copy of new with the followed values
    set back to old, sheet Names with cell edits)."""
    so, sn = old.get("sheets") or {}, new.get("sheets") or {}
    cell_lines, down, changed, edited_sheets = [], [], set(), set()
    for name, b in sn.items():
        a = so.get(name)
        if a is None:
            continue
        for addr in sorted(set(a["cells"]) | set(b["cells"]), key=_cell_order):
            ca, cb = a["cells"].get(addr, ""), b["cells"].get(addr, "")
            if ca != cb:
                al = b["aliases"].get(addr) or a["aliases"].get(addr)
                cell_lines.append("cell     %s.%s%s: %s → %s" % (b["label"], addr, " (%s)" % al if al else "",
                                                               ca or "∅", cb or "∅"))
                changed.add((name, addr))
                edited_sheets.add(name)
        for addr in sorted(set(a["aliases"]) | set(b["aliases"]), key=_cell_order):
            if a["aliases"].get(addr) != b["aliases"].get(addr):
                cell_lines.append("alias    %s.%s: %s → %s" % (b["label"], addr, a["aliases"].get(addr) or "∅",
                                                             b["aliases"].get(addr) or "∅"))
                changed.add((name, addr))
                edited_sheets.add(name)
        for addr in sorted(set(a["values"]) | set(b["values"]), key=_cell_order):
            if (name, addr) not in changed and a["values"].get(addr) != b["values"].get(addr):
                down.append("%s.%s (%s) follows: %s → %s" % (b["label"], addr, b["cells"].get(addr, ""),
                                                            a["values"].get(addr, "∅"), b["values"].get(addr, "∅")))
                changed.add((name, addr))
    po, pn = old.get("params") or {}, new.get("params") or {}
    changed |= set(("Params", k) for k in set(po) | set(pn) if po.get(k) != pn.get(k))
    oo, no = old.get("objects", {}), json.loads(json.dumps(new.get("objects", {})))
    for name, b in no.items():
        a = oo.get(name)
        if a is None:
            continue
        changed |= set((name, p) for p in set(a["props"]) | set(b["props"]) if a["props"].get(p) != b["props"].get(p))
        if a.get("constraints") != b.get("constraints"):
            changed.add((name, "Constraints"))
    if not changed:
        return cell_lines, down, dict(new, objects=no), edited_sheets
    aliases = {}
    for name, rec in list(so.items()) + list(sn.items()):
        for addr, al in rec["aliases"].items():
            aliases.setdefault(name, {})[al] = addr
    for name, rec in list(so.items()) + list(sn.items()):
        for al, pname in (rec.get("params") or {}).items():       # Params.<alias> → the parameter
            aliases.setdefault(name, {})[al] = pname
    for name, b in no.items():
        a = oo.get(name)
        if a is None or not b.get("expr"):
            continue
        o = doc.getObject(name)
        for prop, expr in b["expr"].items():
            if a["expr"].get(prop) != expr:
                continue                         # the expression itself was edited
            refs = _expr_refs(doc, expr, aliases) - {(name, prop)}
            if not refs & changed:
                continue
            m = re.match(r"^\.?Constraints(?:\[(\d+)\]|\.(\w+))$", prop)
            if m:
                idx = int(m.group(1)) if m.group(1) else next(
                    (i for i, c in enumerate(getattr(o, "Constraints", []) or []) if c.Name == m.group(2)), None)
                ca, cb = a.get("constraints") or [], b.get("constraints") or []
                if idx is not None and idx < len(ca) and idx < len(cb) and ca[idx] != cb[idx]:
                    down.append("%s.%s follows %s: %s → %s" % (b["label"], prop.lstrip("."), expr, ca[idx], cb[idx]))
                    cb[idx] = ca[idx]
                continue
            p = prop.lstrip(".").split(".")[0]
            if p in b["props"] and a["props"].get(p) != b["props"][p]:
                down.append("%s.%s follows %s: %s → %s" % (b["label"], p, expr, a["props"].get(p, "∅"), b["props"][p]))
                if p in a["props"]:
                    b["props"][p] = a["props"][p]
                else:
                    del b["props"][p]
    return cell_lines, down, dict(new, objects=no), edited_sheets


def edit_lines(doc, old, new, shape_changed, src, show_none=True):
    """What a script changed in a document, at property level: spreadsheet
    cells, changed properties, constraints and parameters (analysis.
    diff_snapshots), then what only followed — properties bound by expression
    to something edited, and objects whose shape changed because something
    upstream was edited."""
    cells, down, new_, edited_sheets = _downstream(doc, old, new)
    dl, _ = an.diff_snapshots(old, new_)
    dl = cells + dl
    edited = set(edited_sheets)
    for ln in dl:
        m = re.match(r"^(?:changed|added|removed)\s+(\S+)", ln)
        if m:
            edited.add(m.group(1))
    if any(ln.startswith("param") for ln in dl):
        edited.add("Params")
    follow = list(down)
    for n in shape_changed:
        o = doc.getObject(n)
        if o is None or n in edited:
            continue
        deps = getattr(o, "OutListRecursive", None) or o.OutList
        up = sorted(set(d.Label for d in deps if d.Name in edited))
        follow.append("%s: shape follows %s" % (o.Label, ", ".join(up)) if up else
                      "%s: shape changed on recompute, nothing upstream was edited" % o.Label)
    what = "object properties, expressions, sketch constraints, spreadsheet cells and parameters"
    if not dl and not follow:
        return ["edits    none — no property, expression, sketch constraint, spreadsheet cell or parameter "
                "differs from %s" % src] if show_none else []
    out = ["edits    vs %s — compared %s (not shapes, free sketch geometry, view properties or file "
           "metadata):" % (src, what)]
    out += ["  " + ln for ln in dl] or ["  none"]
    if follow:
        out.append("  downstream only (no edit of their own — they follow an edit):")
        out += ["    " + f for f in follow]
    return out


# ------------------------------------------------------------------ inspect

def classify_face(f):
    s = f.Surface
    k = s.__class__.__name__
    u0, u1, v0, v1 = f.ParameterRange
    try:
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    except Exception:  # noqa: BLE001
        n = None
    if k == "Plane" and n is not None:
        ax = _axis_name(n)
        c = f.CenterOfMass
        off = c.dot(n)
        return "plane    normal %-14s offset %s  area %s  centre (%s, %s, %s)" % (
            ax, fmt(off, 3), fmt(f.Area, 1), fmt(c.x, 2), fmt(c.y, 2), fmt(c.z, 2))
    if k == "Cylinder":
        concave = _is_concave(f)
        full = (u1 - u0) >= 2 * math.pi - 1e-6
        what = ("hole/bore" if full else "internal round") if concave else \
            ("boss/shaft" if full else "fillet/rounded edge")
        return "cylinder r %s  axis %s  through (%s)  %s → %s  length %s" % (
            fmt(s.Radius, 3), _axis_name(_canon(s.Axis)), _pt(_axis_point(s)),
            "concave" if concave else "convex", what, fmt(_axial_len(f, s), 2))
    if k == "Cone":
        return "cone     apex (%s)  axis %s  half-angle %s°" % (
            _pt(s.Apex), _axis_name(_canon(s.Axis)), fmt(abs(math.degrees(s.SemiAngle)), 1))
    if k == "Sphere":
        return "sphere   r %s  centre (%s)" % (fmt(s.Radius, 3), _pt(s.Center))
    if k == "Toroid":
        return "torus    R %s r %s  axis %s  (fillet around an axis)" % (
            fmt(s.MajorRadius, 3), fmt(s.MinorRadius, 3), _axis_name(s.Axis))
    return "%-8s area %s" % (k.lower(), fmt(f.Area, 1))


def _axial_len(f, s):
    vals = [(v.Point - s.Center).dot(s.Axis) for v in f.Vertexes] or [0]
    try:
        bbvals = [(p - s.Center).dot(s.Axis) for p in f.discretize(12)] if hasattr(f, "discretize") else []
    except Exception:  # noqa: BLE001
        bbvals = []
    vals += bbvals
    return max(vals) - min(vals)


def hole_lines(shape, limit=60):
    hs = detect_holes(shape)
    if not hs:
        return ["holes    none detected"]
    out = ["holes    %d detected (concave cylinders grouped by axis)" % len(hs)]
    for k, h in enumerate(hs[:limit]):
        if h["through"]:
            span = "THRU  (%s) ↔ (%s)" % (_pt(h["from"]), _pt(h["to"]))
        else:
            entry, end = (h["from"], h["to"]) if h["opens"] == "+" else (h["to"], h["from"])
            span = "blind ↧%s  entry (%s) → bottom (%s)" % (fmt(h["depth"], 2), _pt(entry), _pt(end))
        out.append("  [%d] Ø%s  axis %s  %s  %s" % (
            k, "/Ø".join(fmt(d, 3) for d in h["diameters"]), h["axis_name"], span, ",".join(h["faces"])))
    if len(hs) > limit:
        out.append("  ... %d more" % (len(hs) - limit))
    return out


def op_inspect(args):
    level = args.get("level") or ("object" if args.get("object") else "summary")
    if level == "changes":
        pdir = args.get("project_dir")
        if not pdir:
            raise UserError("level=changes needs a project")
        stem = os.path.splitext(os.path.basename(args["doc"]))[0]
        changes, pch = gui_changes(pdir, args["doc"], os.path.join(pdir, ".history", "snapshot_%s.json" % stem),
                                   os.path.join(pdir, "model.py"), args)
        if changes is None:
            return {"text": "no snapshot of the last build for %s — build first" % stem, "images": []}
        if not changes:
            return {"text": "CHANGES %s: none — the file matches the last build" % stem, "images": []}
        return {"text": "CHANGES %s since the last build (edits made outside model.py):\n  %s" % (
            stem, "\n  ".join(changes)), "images": [], "data": {"params": pch}}
    doc = load_doc(args["doc"])
    project = load_json(os.path.join(args.get("project_dir") or "", "project.json"), {}) \
        if args.get("project_dir") else {}
    dens = densities(project)
    lines = []
    if level == "summary":
        recs, sks, errs = collect(doc, dens)
        lines.append("DOCUMENT %s  (%d objects, %d results)" % (
            os.path.basename(args["doc"]), len(doc.Objects), len(recs)))
        lines += object_lines(recs) if recs else ["  no solid results"]
        lines += sketch_lines(sks)
        if errs:
            lines.append("errors")
            lines += ["  %s: %s" % (n, e) for n, e in errs]
        sheet = [o for o in doc.Objects if o.TypeId == "Spreadsheet::Sheet"]
        for sh in sheet:
            lines += _sheet_lines(sh)
        lines.append("tree")
        lines += _tree_lines(doc)
    elif level == "object":
        o = find(doc, args["object"])
        s = global_shape(o)
        bb = an.tight_bb(s)
        lines.append("OBJECT %s (%s, %s)  role %s" % (o.Label, o.Name, o.TypeId, role(o)))
        pl = global_placement(o)
        lines.append("placement  base (%s)  rotation axis %s angle %s°" % (
            _pt(pl.Base), _axis_name(pl.Rotation.Axis) if pl.Rotation.Angle else "-",
            fmt(math.degrees(pl.Rotation.Angle), 2)))
        lines.append("shape    %d solid(s), valid %s, volume %s mm³, area %s mm²" % (
            len(s.Solids), "✓" if s.isValid() else "✗", fmt(s.Volume, 2), fmt(s.Area, 1)))
        lines.append("bbox     %s  from (%s) to (%s)" % (dims(bb), _pt(App.Vector(bb.XMin, bb.YMin, bb.ZMin)),
                                                          _pt(App.Vector(bb.XMax, bb.YMax, bb.ZMax))))
        if s.Solids:
            com = s.Solids[0].CenterOfMass if len(s.Solids) == 1 else s.CenterOfMass if hasattr(s, "CenterOfMass") else None
            if com is not None:
                lines.append("centroid (%s)" % _pt(com))
        if "FcFrames" in o.PropertiesList:
            lines.append("frames")
            for k in json.loads(o.FcFrames or "{}"):
                g = an.get_frame(o, k)
                lines.append("  %-10s origin (%s)  +Z %s" % (k, _pt(g.Base), _axis_name(g.Rotation.multVec(App.Vector(0, 0, 1)))))
        if o.TypeId == "PartDesign::Body":
            feats, tip, _, _ = body_features(o)
            lines.append("features " + "  ".join("%s%s" % (f["name"], " ✗" if f["error"] else "") for f in feats)
                         + "   (tip: %s)" % tip)
            for f in o.Group:
                if f.TypeId == "PartDesign::Hole":
                    lines.append("  %s: %s" % (f.Label, hole_summary(f)))
        if o.ExpressionEngine:
            lines.append("expressions  " + " · ".join("%s = %s" % (p, e) for p, e in o.ExpressionEngine))
        lines += hole_lines(s)
        nf = len(s.Faces)
        limit = int(args.get("max_faces", 80))
        lines.append("faces    %d  (names valid for this revision only; prefer fc.face/holes selectors)" % nf)
        for i, f in enumerate(s.Faces[:limit]):
            lines.append("  Face%-4d %s" % (i + 1, classify_face(f)))
        if nf > limit:
            lines.append("  ... %d more (raise max_faces)" % (nf - limit))
        kinds = {}
        for e in s.Edges:
            k = e.Curve.__class__.__name__
            kinds[k] = kinds.get(k, 0) + 1
        lines.append("edges    %d  (%s)" % (len(s.Edges), ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    elif level == "sketch":
        o = find(doc, args["object"]) if args.get("object") else None
        sks = [o] if o is not None else [x for x in doc.Objects if x.TypeId == "Sketcher::SketchObject"]
        for sk in sks:
            if sk.TypeId != "Sketcher::SketchObject":
                raise UserError("%s is not a sketch" % sk.Label)
            info = sketch_info(sk)
            lines.append("SKETCH %s  DoF %s  %s" % (sk.Label, info["dof"],
                                                     "fully constrained ✓" if info["full"] else "under-constrained"))
            sup = getattr(sk, "AttachmentSupport", None)
            if sup:
                lines.append("  on %s (%s)" % (", ".join("%s.%s" % (s[0].Label, ",".join(s[1]) or "-") for s in sup),
                                               sk.MapMode))
            for i, g in enumerate(sk.Geometry):
                lines.append("  g%-3d %s%s" % (i, _geo(g), "  (construction)" if sk.getConstruction(i) else ""))
            for i, c in enumerate(sk.Constraints):
                val = ""
                if c.Type in ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"):
                    val = " = %s" % fmt(c.Value if c.Type != "Angle" else math.degrees(c.Value), 3)
                expr = [e for p, e in sk.ExpressionEngine if p == "Constraints[%d]" % i]
                lines.append("  c%-3d %-11s %s%s%s" % (i, c.Type, _cref(c), val, "  ← " + expr[0] if expr else ""))
            for k in ("conflicting", "redundant", "malformed"):
                if info[k]:
                    lines.append("  %s: %s" % (k, info[k]))
    elif level == "params":
        sheets = [o for o in doc.Objects if o.TypeId == "Spreadsheet::Sheet"]
        if not sheets:
            lines.append("no spreadsheets in %s" % os.path.basename(args["doc"]))
        for sh in sheets:
            lines += _sheet_lines(sh)
        uses = []
        for o in doc.Objects:
            for p, e in o.ExpressionEngine:
                uses.append("  %s.%s = %s" % (o.Label, p, e))
        lines.append("expressions (%d)" % len(uses))
        lines += uses[:200]
    else:
        raise UserError("level must be summary | object | sketch | params | changes")
    return {"text": "\n".join(lines), "images": []}


def _geo(g):
    k = g.__class__.__name__
    if k == "LineSegment":
        return "line  (%s, %s) → (%s, %s)" % (fmt(g.StartPoint.x), fmt(g.StartPoint.y), fmt(g.EndPoint.x), fmt(g.EndPoint.y))
    if k == "Circle":
        return "circle centre (%s, %s) r %s" % (fmt(g.Center.x), fmt(g.Center.y), fmt(g.Radius))
    if k == "ArcOfCircle":
        return "arc   centre (%s, %s) r %s" % (fmt(g.Center.x), fmt(g.Center.y), fmt(g.Radius))
    if k == "Point":
        return "point (%s, %s)" % (fmt(g.X), fmt(g.Y))
    return k


def _cref(c):
    refs = []
    for gi, pi in ((c.First, c.FirstPos), (c.Second, c.SecondPos), (c.Third, getattr(c, "ThirdPos", 0))):
        if gi is None or gi == -2000:
            continue
        name = "root" if gi == -1 else "H-axis" if gi == -1 and pi == 0 else "V-axis" if gi == -2 else "g%d" % gi
        refs.append(name + ("" if not pi else ".%s" % {1: "start", 2: "end", 3: "centre"}.get(pi, pi)))
    return " ".join(refs)


def _sheet_lines(sh):
    out = ["sheet %s" % sh.Label]
    for row in range(1, 200):
        a = (sh.getContents("A%d" % row) if hasattr(sh, "getContents") else "").lstrip("'")
        if not a:
            if row > 1:
                break
            continue
        if row == 1 and a == "parameter":
            continue
        try:
            val = sh.get("B%d" % row)
        except Exception:  # noqa: BLE001
            val = "?"
        alias = sh.getAlias("B%d" % row) if hasattr(sh, "getAlias") else ""
        try:
            src = sh.get("D%d" % row)
        except Exception:  # noqa: BLE001
            src = ""
        out.append("  %-16s %-10s %s%s" % (a, _num(getattr(val, "Value", val)),
                                           "Params.%s" % alias if alias else "",
                                           ("  [%s]" % src) if src else ""))
    return out


def _tree_lines(doc, limit=80):
    out = []
    for o in doc.Objects:
        if o.TypeId.startswith(("App::Origin", "App::Line", "App::Plane", "App::Point")):
            continue
        par = None
        try:
            par = o.getParentGeoFeatureGroup()
        except Exception:  # noqa: BLE001
            pass
        indent = "    " if par is not None else "  "
        err = state_error(o)
        out.append("%s%s (%s)%s%s" % (indent, o.Label, o.TypeId.split("::")[-1],
                                      "  ✗ %s" % err if err else "",
                                      "" if is_result(o) or par is not None else ""))
        if len(out) >= limit:
            out.append("  ... (%d objects total)" % len(doc.Objects))
            break
    return out


def op_render(args):
    doc = load_doc(args["doc"])
    if args.get("objects"):
        objs = [find(doc, r) for r in args["objects"]]
    else:
        objs = results(doc)
    if not objs:
        raise UserError("nothing to render — no solid results in the document")
    hl = None
    if args.get("highlight"):
        hl = set(find(doc, r).Label for r in args["highlight"])
        if not args.get("objects"):
            objs = results(doc)
    items = render_items(doc, objs, highlight=hl)
    png = args.get("out") or image_path(args, "%s_render_%s.png" % (
        os.path.splitext(os.path.basename(args["doc"]))[0], time.strftime("%H%M%S")))
    os.makedirs(os.path.dirname(png), exist_ok=True)
    info = rd.render(items, png, views=args.get("views") or ["iso"], size=parse_size(args.get("size")),
                     style=args.get("style") or "shaded", hidden=args.get("hidden", True),
                     labels=args.get("labels", True), section=parse_section(args.get("section")))
    text = "RENDER %s\n  views  %s\n  style  %s%s\n  scale  %s px/mm   tessellation %s mm   %d triangles" % (
        png, ", ".join(v["view"] for v in info["views"]), args.get("style") or "shaded",
        ("   section %s" % json.dumps(args["section"])) if args.get("section") else "",
        info["views"][0]["px_per_mm"], info["tolerance_mm"], info["triangles"])
    return {"text": text, "images": [png], "data": info}


def _project(args):
    pdir = args.get("project_dir")
    return (load_json(os.path.join(pdir, "project.json"), {}) or {}) if pdir else {}


def op_measure(args):
    doc = load_doc(args["doc"])
    dens = densities(_project(args))
    text, data = an.op_measure(doc, args.get("queries") or [], dens, float(args.get("tol", 0.01)))
    return {"text": "MEASURE %s\n%s" % (os.path.basename(args["doc"]), text), "images": [], "data": data}


def op_check_fit(args):
    doc = load_doc(args["doc"])
    project = _project(args)
    mc = args.get("min_clearance")
    if mc is None:
        mc = float(((project.get("clearance") or {}).get("default_min_mm")) or 0.0)
    try:
        lines, recs, objs = an.op_check_fit(doc, args.get("objects"), float(mc), args.get("contact_ok"),
                                            args.get("interference_ok"), float(args.get("within", 5.0)),
                                            pair_min=args.get("pair_min"))
    except an.RefError as e:
        raise UserError(str(e))
    images = []
    bad = [r for r in recs if r["status"] == "interference"]
    if args.get("out") and (bad or args.get("render")):
        # frame only the parts involved, so a small overlap is visible
        involved = set(r["a"] for r in bad) | set(r["b"] for r in bad) if bad else set(o.Label for o in objs)
        items = [rd.Item(o.Label, global_shape(o), rd.DIM, ghost=True) for o in objs if o.Label in involved]
        items += [rd.Item("overlap %s/%s" % (r["a"], r["b"]), r["common"], (0.9, 0.1, 0.1))
                  for r in bad if r.get("common") is not None]
        os.makedirs(os.path.dirname(args["out"]), exist_ok=True)
        rd.render(items, args["out"], views=args.get("views") or ["iso", "front", "top", "right"],
                  size=parse_size(args.get("size")))
        images.append(args["out"])
        lines.append("image    interfering volume in red, parts ghosted")
    data = [{k: v for k, v in r.items() if k != "common"} for r in recs]
    return {"text": "\n".join(lines), "images": images, "data": data}


def op_section(args):
    doc = load_doc(args["doc"])
    project = _project(args)
    mc = args.get("min_clearance")
    if mc is None:
        mc = (project.get("clearance") or {}).get("default_min_mm")
    try:
        lines, data, png = an.op_section(doc, args["plane"], args.get("objects"), float(args.get("within", 5.0)),
                                         args.get("out"), parse_size(args.get("size"), (1100, 800)),
                                         None if mc is None else float(mc),
                                         contact_ok=args.get("contact_ok"), interference_ok=args.get("interference_ok"))
    except an.RefError as e:
        raise UserError(str(e))
    return {"text": "\n".join(lines), "images": [png] if png else [], "data": data}


def _parts(doc, refs=None, include_refs=False):
    """Objects to manufacture: the listed ones, else every solid result that is
    not a reference (bought-out) part."""
    if refs:
        return [find(doc, r) for r in refs]
    return [o for o in results(doc) if global_shape(o).Solids and (include_refs or role(o) != "ref")]


def op_export(args):
    doc = load_doc(args["doc"])
    objs = _parts(doc, args.get("objects"))
    if not objs:
        raise UserError("nothing to export — no manufactured (non-reference) solids in the document")
    try:
        recs = mf.export_parts(doc, objs, args.get("formats") or ["step"], args["out_dir"],
                               float(args.get("tolerance", 0.02)), float(args.get("angular_deg", 5.0)),
                               args.get("view"))
    except mf.MfgError as e:
        raise UserError(str(e))
    bad = [r for r in recs if not r["ok"]]
    lines = ["EXPORT  %d file(s) to %s — %s" % (len(recs), args["out_dir"],
                                                "all verified ✓" if not bad else "%d problem(s) ✗" % len(bad))]
    for r in recs:
        lines.append("  %s %-24s %-5s %8s  %s" % ("✓" if r["ok"] else "✗", os.path.basename(r["path"]),
                                                  r["format"], _kb(r["bytes"]), r["check"]))
    return {"text": "\n".join(lines), "images": [], "data": recs}


def _kb(n):
    return "%.1f KB" % (n / 1024.0) if n < 1024 * 1024 else "%.1f MB" % (n / 1048576.0)


def part_process(project, label, override=None):
    if override:
        return mf.norm_process(override)
    cfg = (project.get("parts") or {}).get(label) or {}
    return mf.norm_process(cfg.get("process") or project.get("process") or "fdm")


def op_dfm(args):
    doc = load_doc(args["doc"])
    project = _project(args)
    objs = _parts(doc, args.get("objects"))
    if not objs:
        raise UserError("no manufactured (non-reference) solids to check")
    res = [mf.dfm(o, part_process(project, o.Label, args.get("process")), project) for o in objs]
    lines = ["MANUFACTURABILITY  %d part(s)" % len(res)]
    for r in res:
        lines += mf.dfm_lines(r)
    return {"text": "\n".join(lines), "images": [], "data": res}


def hole_compensation(o):
    """Largest FcHoleComp (mm) among o's Hole features (0.0 if none)."""
    return max([float(getattr(h, "FcHoleComp", 0.0) or 0.0) for h in getattr(o, "Group", [])
                if h.TypeId == "PartDesign::Hole"] or [0.0])


def op_release_prepare(args):
    """DFM + verified exports + part records for every manufactured part."""
    doc = load_doc(args["doc"])
    project = _project(args)
    rel = args["release_dir"]
    os.makedirs(rel, exist_ok=True)
    parts = _parts(doc, args.get("objects"))
    if not parts:
        raise UserError("no manufactured (non-reference) parts in the build")
    # keys that name no object: a typo here silently drops a quantity, process or note
    made = [o.Label for o in _parts(doc)]
    known = set(o.Label for o in doc.Objects)
    unknown = {"parts": sorted(k for k in (project.get("parts") or {}) if k not in known),
               "qty": sorted(k for k in (args.get("qty") or {}) if k not in made),
               "processes": sorted(k for k in (args.get("processes") or {}) if k not in made)}
    overrides = args.get("processes") or {}
    recs, blocked, lines = [], [], []
    for o in parts:
        proc = part_process(project, o.Label, overrides.get(o.Label) or args.get("process"))
        d = mf.dfm(o, proc, project)
        lines += mf.dfm_lines(d)
        rec = mf.part_record(o, proc, project)
        rec["dfm"] = d
        rec.setdefault("hole_compensation", hole_compensation(o))
        rec["drawing"] = None                          # set by the server when a drawing ships
        if args.get("qty") and o.Label in args["qty"]:
            rec["qty"] = int(args["qty"][o.Label])
        if d["verdict"] == "blocked" and not args.get("allow_dfm_errors"):
            blocked.append(o.Label)
        recs.append(rec)
    if blocked:
        return {"text": "RELEASE STOPPED — not manufacturable as is: %s\n%s\n(fix the model, change the process, or "
                        "pass allow_dfm_errors=true)" % (", ".join(blocked), "\n".join(lines)),
                "images": [], "data": {"ok": False, "blocked": blocked, "unknown_keys": unknown, "labels": made}}
    exports = []
    for rec, o in zip(recs, parts):
        pdir_ = os.path.join(rel, mf._safe(o.Label))
        # prints: meshes turned so the DFM's best orientation is +Z, sitting on z = 0
        up = ((rec["dfm"].get("metrics") or {}).get("orientation") or {}).get("up") \
            if rec["process"] in ("fdm", "sla") else None
        er = mf.export_parts(doc, [o], mf.RELEASE_FORMATS.get(rec["process"], ["step"]), pdir_, print_up=up)
        rec["files"] = [r["path"] for r in er if os.path.isfile(r["path"])]
        rec["exports"] = [{"file": os.path.basename(r["path"]), "format": r["format"], "ok": r["ok"],
                           "check": r["check"], "oriented": r.get("oriented")} for r in er]
        exports += er
    refs = []
    for o in results(doc):
        if role(o) == "ref":
            b = bom_of(o)
            if b["bom"] == "none":
                continue
            refs.append(dict({"part": o.Label, "bom": b["bom"], "qty": b["qty"], "spec": b["spec"]},
                             **mf.item_size(global_shape(o))))
    bad = [e for e in exports if not e["ok"]]
    return {"text": "\n".join(lines), "images": [],
            "data": {"ok": not bad, "parts": recs, "refs": refs, "unknown_keys": unknown, "labels": made,
                     "exports": [{k: v for k, v in e.items()} for e in exports]}}


def op_release_finish(args):
    rel = args["release_dir"]
    project = _project(args)
    mf.write_release_docs(rel, project, args["rev"], args["parts"], args["refs"], args.get("checks") or [],
                          args.get("report") or "", checks_changes=args.get("checks_changes"))
    z = mf.zip_dir(rel, rel.rstrip("/") + ".zip")
    return {"text": z, "images": [], "data": {"zip": z}}


FEA_CHECKS = {"fea_safety": "min", "fea_stress": "max", "fea_displacement": "max", "fea_frequency": "min",
              "fea_buckling": "min"}
FEA_KIND = {"fea_frequency": "frequency", "fea_buckling": "buckling"}       # the others need a static case


def fea_checks(pdir, project, checks, doc):
    """Checks on analysis results (out/fea/<case>/result.json). A result counts only
    if it was solved for this geometry (geom_sig of each part in this build) and this
    case and material (case_hash); otherwise the check fails as stale and names the
    case to re-run. Static checks on a multi-part case judge every part (each against
    its own yield) unless the check names one with part."""
    lines, res, stale = [], [], []
    cases = (load_json(os.path.join(pdir, "fea.json"), {}) or {}).get("cases") or {}
    for c in checks:
        cid, t = str(c.get("id") or c.get("type")), c.get("type")
        name = str(c.get("case") or "default")
        ok, val, detail = False, "error", ""
        try:
            if t not in FEA_CHECKS:
                raise ValueError("unknown check type %r (%s)" % (t, ", ".join(FEA_CHECKS)))
            lim = c.get(FEA_CHECKS[t], 2.0 if t == "fea_safety" else None)
            if lim is None:
                raise ValueError("%s needs %s" % (t, FEA_CHECKS[t]))
            lim = float(lim)
            want = FEA_KIND.get(t, "static")
            if name not in cases:
                raise ValueError("no load case '%s' in fea.json — define it with analyze(case='%s'%s, fixed=[…]%s)"
                                 % (name, name, "" if want == "static" else ", analysis='%s'" % want,
                                    "" if want == "frequency" else ", loads=[…]"))
            case = cases[name]
            kind = fea.analysis_kind(case)
            if kind != want:
                raise ValueError("case '%s' is a %s analysis — %s needs a %s case: analyze(case=…, "
                                 "analysis='%s', …)" % (name, kind, t, want, want))
            r = load_json(os.path.join(pdir, "out", "fea", name, "result.json"), None)
            labels = fea.case_parts(case) or ([r["part"]] if r and r.get("part") else [])
            mats = fea.materials(project, case, labels) if labels else {}
            sig = (r or {}).get("geom_sig")
            current = (all(sig[lb] == _geom_sig_of(doc, lb) for lb in sig) if isinstance(sig, dict) and sig
                       else sig is not None and sig == _geom_sig_of(doc, r.get("part")))
            if not r or not mats:
                val, detail = "no result", "run analyze(case='%s')" % name
                stale.append(name)
            elif r.get("case_hash") != fea.result_hash(case, mats) or not current:
                val, detail = "stale", ("the model or case changed since %s — run analyze(case='%s') "
                                        "(release re-runs it)" % (r.get("rev", "?"), name))
                stale.append(name)
            elif kind == "frequency":
                f1 = (r.get("frequencies_hz") or [0.0])[0]
                ok, val = f1 >= lim - 1e-9, "%s Hz (≥ %s)" % (fmt(f1, 1), fmt(lim, 1))
                detail = "first natural frequency, case '%s'%s" % (
                    name, " — mode 1 is a rigid-body mode: the supports leave the part free" if f1 < 0.5 else "")
                if r.get("converged_freq") is False:
                    detail += " — f1 changed %.1f %% on a finer mesh (see the analyze notes)" % (
                        100 * (r.get("change") or {}).get("f1", 0))
            elif kind == "buckling":
                lam = r.get("buckling_factor")
                if lam is None:
                    above = r.get("buckling_factor_above") or 0.0
                    ok, val = above >= lim - 1e-9, "> %s (≥ %s)" % (fmt(above, 2), fmt(lim, 2))
                    detail = ("no positive buckling factor among the %d computed, so the lowest is above %s%s, case "
                              "'%s'" % (len(r.get("buckling_factors") or []), fmt(above, 2),
                                        "" if ok else " — re-run with more modes to find it", name))
                else:
                    ap = math.sqrt(sum(x * x for x in r.get("applied") or [0, 0, 0]))
                    ok, val = lam >= lim - 1e-9, "%s (≥ %s)" % (fmt(lam, 2), fmt(lim, 2))
                    detail = "lowest positive buckling factor — buckles at %s N, case '%s'" % (fmt(lam * ap, 1), name)
                if r.get("converged_buckling") is False:
                    detail += " — the factor changed %.1f %% on a finer mesh (see the analyze notes)" % (
                        100 * (r.get("change") or {}).get("factor", 0))
            else:
                per = r.get("per_part") or [dict(part=r.get("part"), max_vm=r["max_vm"], max_disp=r["max_disp"],
                                                 max_vm_away=r.get("max_vm_away"))]
                if c.get("part"):
                    per = [p for p in per if p["part"] == c["part"]]
                    if not per:
                        raise ValueError("part %r is not in case '%s' (%s)" % (c["part"], name, ", ".join(labels)))
                away = bool(c.get("away_from_supports")) and all(p.get("max_vm_away") is not None for p in per)
                where = " away from the supports" if away else ""
                for p in per:
                    p["_vm"] = (p["max_vm_away"] if away else p["max_vm"]) or 0.0
                    p["_y"] = mats[p["part"]]["yield"]
                    p["_sf"] = p["_y"] / p["_vm"] if p["_vm"] > 1e-12 else float("inf")
                def inpart(p):
                    return " in %s" % p["part"] if len(mats) > 1 else ""
                if t == "fea_safety":
                    g = min(per, key=lambda p: p["_sf"])
                    ok, val = g["_sf"] >= lim - 1e-9, "%s (≥ %s)" % (fmt(g["_sf"], 2), fmt(lim, 2))
                    detail = "max von Mises%s %s MPa vs yield %s MPa (%s)%s, case '%s'" % (
                        where, fmt(g["_vm"], 3), fmt(g["_y"], 0), mats[g["part"]]["name"], inpart(g), name)
                elif t == "fea_stress":
                    g = max(per, key=lambda p: p["_vm"])
                    ok, val = g["_vm"] <= lim + 1e-9, "%s MPa (≤ %s)" % (fmt(g["_vm"], 3), fmt(lim, 3))
                    detail = "max von Mises%s%s, case '%s'" % (where, inpart(g), name)
                else:
                    g = max(per, key=lambda p: p["max_disp"] or 0.0)
                    d = g["max_disp"] or 0.0
                    ok, val = d <= lim + 1e-12, "%s mm (≤ %s)" % (fmt(d, 4), fmt(lim, 4))
                    detail = "max displacement%s, case '%s'" % (inpart(g), name)
                ch = r.get("change") or {}
                if t == "fea_displacement":
                    conv, delta, what = r.get("converged_disp", r.get("converged")), ch.get("disp"), "displacement"
                elif away:
                    conv, delta, what = r.get("converged_stress_away"), ch.get("vm_away"), "stress away from the supports"
                else:
                    conv, delta, what = r.get("converged_stress", r.get("converged")), ch.get("vm"), "peak stress"
                if conv is False:
                    detail += (" — the %s changed %.0f %% on a finer mesh (a singularity at a sharp corner or a rigid "
                               "support edge, or a mesh too coarse: see the analyze notes)" % (what, 100 * (delta or 0)))
        except (ValueError, TypeError, KeyError, fea.FeaError) as e:
            detail = str(e)
        res.append({"id": cid, "type": t, "ok": ok, "value": val, "detail": detail})
        lines.append("  %s %-18s %-11s %s%s" % ("✓" if ok else "✗", cid, t, val, ("   " + detail) if detail else ""))
    return lines, res, sorted(set(stale))


def _geom_sig_of(doc, label):
    try:
        return fea.geom_sig(global_shape(find(doc, label)))
    except Exception:  # noqa: BLE001 — a part that no longer exists is simply stale
        return None


def op_analyze(args):
    """Rebuild (cheap when nothing changed), then solve a case from fea.json merged with
    any given arguments — static, frequency or buckling, of one part or of several parts
    bonded together; save the result and (save) the case."""
    t_start = time.time()
    pdir = args["project_dir"]
    project = _project(args)
    fpath = os.path.join(pdir, "fea.json")
    fj = load_json(fpath, {}) or {}
    cases = fj.setdefault("cases", {})
    name = str(args.get("case") or "default")
    case = dict(cases.get(name) or {})
    if args.get("parts") is not None:          # part= and parts= replace each other
        case.pop("part", None)
    if args.get("part") is not None:
        case.pop("parts", None)
        case.pop("bonded", None)
    for k in ("part", "parts", "bonded", "analysis", "modes", "fixed", "loads", "gravity", "material", "mesh_size",
              "refine"):
        if args.get(k) is not None:
            case[k] = args[k]
    known = ", ".join(sorted(cases)) or "none yet"
    try:
        kind = fea.analysis_kind(case)
        fea.mode_count(case)
    except fea.FeaError as e:
        raise UserError(str(e))
    if not case.get("fixed"):
        raise UserError("load case '%s' has no supports — give fixed=[…] with the faces or holes that are held "
                        "(e.g. \"Bracket.holes(d=5.5)\" for bolted holes, \"Bracket.face(-Z)\" for a clamped "
                        "face)%s. Saved cases: %s" % (
                            name, "; a frequency analysis needs them too — the part vibrates as it is mounted"
                            if kind == "frequency" else "", known))
    if kind != "frequency" and not case.get("loads") and not case.get("gravity"):
        raise UserError("load case '%s' has no loads — give loads=[{\"ref\": \"Bracket.face(+Z)\", \"force\": "
                        "50, \"direction\": \"-Z\"}] (N) or {\"ref\": …, \"pressure\": 0.2} (MPa), or "
                        "gravity=true%s" % (name, " (buckling finds the factor these loads must grow by to buckle the "
                                                  "part)" if kind == "buckling" else ""))
    labels = fea.case_parts(case)
    if case.get("bonded") and len(labels) < 2:
        raise UserError("bonded=[[face, face], …] glues faces of different parts — name them with parts=[\"Base\", "
                        "\"Bracket\"]")
    if len(set(labels)) != len(labels):
        raise UserError("parts names a part twice: %s" % ", ".join(labels))
    whatif = args.get("params") or None
    # a what-if (params overrides) builds a variant file and leaves params.json, the case and its result alone
    b = op_build(dict({"project_dir": pdir, "params": whatif or {}, "views": [], "force": False, "skip_checks": True},
                      **({"variant": "fea"} if whatif else {})))
    bd = b.get("data") or {}
    if not bd.get("ok"):
        raise UserError("the build has errors — fix them before analysing:\n" + b.get("text", ""))
    doc = load_doc(bd["fcstd"])
    cands = [o for o in results(doc) if global_shape(o).Solids and role(o) != "ref"]
    if labels:
        parts = [find(doc, lb) for lb in labels]
    elif len(cands) == 1:
        parts = cands
        case["part"] = cands[0].Label
    else:
        raise UserError("several parts — say which one with part=… (%s), or analyse several together with "
                        "parts=[…] and bonded=[[face, face], …]" % ", ".join(o.Label for o in cands))
    # CalculiX must finish before the server gives up on this worker (a killed worker would orphan it)
    deadline = t_start + float(args["timeout"]) - 30 if args.get("timeout") else None
    try:
        mats = fea.materials(project, case, [p.Label for p in parts])
        res, png = fea.run_case(doc, parts, case, mats, os.path.join(pdir, "out", "fea", name + ("__whatif" if whatif
                                                                                                else "")),
                                refine=case.get("refine"), mesh_size=case.get("mesh_size"), deadline=deadline)
    except (fea.FeaError, ValueError, an.RefError) as e:
        raise UserError(str(e))
    res.update(case=name, rev=bd.get("rev"), inputs_sha1=bd.get("inputs_sha1"), case_hash=fea.result_hash(case, mats),
               geom_sig=fea.geom_sig(global_shape(parts[0])) if len(parts) == 1 else
               {p.Label: fea.geom_sig(global_shape(p)) for p in parts})
    res["whatif"] = whatif
    out = os.path.join(pdir, "out", "fea", name + ("__whatif" if whatif else ""))
    with open(os.path.join(out, "result.json"), "w") as fh:
        json.dump(_json_safe(res), fh, indent=1)
    save = args.get("save", True) is not False and not whatif
    if save:
        cases[name] = case
        with open(fpath, "w") as fh:
            json.dump(fj, fh, indent=1)
            fh.write("\n")
    lines = fea.report_lines(res, name)
    if whatif:
        lines.insert(1, "what-if   params %s — built as variant 'fea'; params.json, the saved case and its result are "
                        "unchanged (write a value into params.json and re-run the case to keep it)" % ", ".join(
                            "%s=%s" % kv for kv in sorted(whatif.items())))
    lines.append("files     %s%s%s" % (os.path.relpath(os.path.join(out, "result.json"), pdir),
                                       "  ·  " + os.path.relpath(png, pdir) if png else "",
                                       "  ·  case saved in fea.json" if save else ""))
    with open(os.path.join(out, "report.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"text": "\n".join(lines), "images": [png] if png else [], "data": _json_safe(res)}


OPS = {"probe": op_probe, "build": op_build, "run_script": op_run_script,
       "inspect": op_inspect, "render": op_render, "measure": op_measure,
       "check_fit": op_check_fit, "section": op_section, "export": op_export, "dfm": op_dfm,
       "release_prepare": op_release_prepare, "analyze": op_analyze, "release_finish": op_release_finish}


def main():
    req_path = os.environ.get("FCMCP_REQ") or sys.argv[1]
    resp_path = os.environ.get("FCMCP_RESP") or sys.argv[2]
    with open(req_path) as fh:
        req = json.load(fh)
    t0 = time.time()
    try:
        fn = OPS[req["op"]]
        out = fn(req.get("args") or {})
        resp = {"ok": True}
        resp.update(out)
    except UserError as e:
        resp = {"ok": False, "error": str(e)}
    except (fc.FcError, an.RefError) as e:
        resp = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "traceback": traceback.format_exc()[-4000:]}
    resp["worker_seconds"] = round(time.time() - t0, 3)
    with open(resp_path, "w") as fh:
        json.dump(resp, fh, default=str)


if __name__ == "__main__":
    main()
    # FreeCAD's atexit handlers occasionally crash on teardown after work is
    # done; the response is already written, so leave without them.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
