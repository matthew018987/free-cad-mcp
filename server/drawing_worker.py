# SPDX-License-Identifier: Apache-2.0
"""
Drawing worker — runs inside the FreeCAD GUI process (TechDraw export needs it):

    freecad -u <private user.cfg> drawing_worker.py      (request/response via env)

Ops
  create        generate part drawings from a sheet spec: style from the part's
                process, else its shape (prismatic / plate / turned; bent sheet
                metal → three views), first-angle ISO views + isometric (hidden
                lines on request; a turned part's side view always), paper and
                standard scale chosen to fit, overall and hole-position
                dimensions (baseline or pitch), pockets / slots / steps /
                cut-outs (size, location, depth, corner radii), a Ø per turned
                step with its fit or thread callout — a turned part with bores in
                SECTION A-A through the axis (section auto|full|none), its bores
                dimensioned there —, hole callouts from the Hole features, a
                sheet-metal part's bend table and flat pattern view, notes,
                title block (long values shortened to fit); every dimension is
                checked against the 3D model (a turned step's Ø also where it is
                anchored); the sheet's own "ops" are applied on top; exports PDF,
                SVG, DXF and a PNG preview, and saves the drawing document.
  read          describe the pages of any drawing document (dimension texts and
                the model coordinates they are anchored at), with a preview.
  edit          add/move/remove dimensions (remove also by match), callouts,
                notes (append_note, add_note align=left) and title fields on an
                existing page (apply_ops); saves a revision, its title-block
                revision = build rev + edit number (r0001.2).
  sheet_status  is each saved sheet still drawn from the current build?
  export        PDF/SVG/DXF/PNG of a saved sheet (title fields applied to a copy).
"""

import json
import math
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import traceback

HERE = os.environ.get("FCMCP_SERVER_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import FreeCAD as App  # noqa: E402
import FreeCADGui as Gui  # noqa: E402
import Part  # noqa: E402
from PySide6 import QtCore, QtGui, QtSvg, QtWidgets  # noqa: E402

import analysis as an  # noqa: E402
import fclib as fc  # noqa: E402

# In the GUI, visibility comes from the view providers, not the file: a document
# saved by the console worker opens with everything hidden. Ignore it here.
an.IGNORE_VISIBILITY = True

Vector = App.Vector
fmt = an.fmt

PAPER = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0)}
SCALES = [10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01]
TITLE_H = 62.0          # ISO 5457 minimal title block incl. the material/tolerance row
TITLE_W = 182.0
DIM_STEP = 7.0          # spacing of stacked dimension lines (paper mm)
DIM_FONT = 3.5          # dimension text height (set_preferences)
NOTE_SIZE = 3.0         # notes text height
CHAR_W = 0.72           # osifont character width per mm of text height (calibrated, capitals)
LINE_H = 2.4            # line pitch per mm of text height (planning; conservative)
LINE_PITCH = 2.2333     # rendered annotation line pitch per mm of text height (measured, FreeCAD 1.1)
EM = 4.0 / 3            # TechDraw renders a text height of s mm with an osifont em of s × 4/3
TEXT_PAD = 0.4          # Qt document margin round annotation and dimension text (mm)
ASCENT = 1.3169         # osifont ascent per em
PART_VIEWS = ("TechDraw::DrawViewPart", "TechDraw::DrawProjGroupItem", "TechDraw::DrawViewSection",
              "TechDraw::DrawViewDetail")
COMPUTED_VIEWS = ("TechDraw::DrawViewPart", "TechDraw::DrawViewSection")     # waited for after a document opens


class DrawingError(Exception):
    pass


def pump(n=5, dt=0.02):
    for _ in range(n):
        QtWidgets.QApplication.processEvents()
        time.sleep(dt)


def wait_views(views, timeout=90):
    """TechDraw computes hidden lines in threads; wait until each view has geometry."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump(3)
        if all(v.getVisibleEdges() for v in views):
            pump(5)
            return True
    return False


def scale_text(s):
    if s >= 1:
        return "%g:1" % s
    return "1:%g" % round(1 / s, 6)


_FM = []


def _metrics():
    """Qt font metrics of osifont (TechDraw's font) at a 100 px em, or None."""
    if not _FM:
        fm = None
        try:
            import TechDrawGui  # noqa: F401 — registers osifont with Qt
            f = QtGui.QFont("osifont")
            f.setPixelSize(100)
            if QtGui.QFontInfo(f).family().lower().startswith("osifont"):
                fm = QtGui.QFontMetricsF(f)
        except Exception:  # noqa: BLE001
            fm = None
        _FM.append(fm)
    return _FM[0]


def em_width(t, em):
    """Width (mm) of the text t in osifont at an em of em mm (0.6 em per character without Qt)."""
    fm = _metrics()
    if fm is None:
        return 0.6 * em * len(t)
    return fm.horizontalAdvance(t) / 100.0 * em


def text_w(t, size=None):
    """Width (paper mm) of a dimension or annotation text of height size, margins included."""
    return em_width(str(t), (size or DIM_FONT) * EM) + 2 * TEXT_PAD


def text_h(size=None):
    return (size or DIM_FONT) * EM * 1.2 + 2 * TEXT_PAD


def parse_scale(s):
    """A scale from 2, 0.5, "1:2", "2:1", "1/2" or "auto"/None (→ None)."""
    if s is None or (isinstance(s, str) and s.strip().lower() in ("", "auto")):
        return None
    v = None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        v = float(s)
    else:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)\s*$", str(s))
        if m and float(m.group(2)) > 0:
            v = float(m.group(1)) / float(m.group(2))
        else:
            try:
                v = float(str(s).strip())
            except ValueError:
                v = None
    if v is None or not v > 0 or math.isinf(v):
        raise DrawingError("scale %r not understood — use a number (0.5), a ratio ('1:2', '2:1', '1/2') or 'auto'"
                           % (s,))
    return v


def paper_name(p):
    if p is None or str(p).strip().lower() in ("", "auto"):
        return None
    n = str(p).strip().upper()
    if n not in PAPER:
        raise DrawingError("paper %r not available — use %s or 'auto'" % (p, ", ".join(PAPER)))
    return n


def template_path(name):
    base = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO")
    if not name.endswith(".svg"):
        name += ".svg"
    p = os.path.join(base, name)
    if not os.path.isfile(p):
        raise DrawingError("template %s not found in %s" % (name, base))
    return p


def save_doc(doc, path, keep_label=False):
    """saveAs, then delete the backups FreeCAD made of the file it replaced
    (<stem>.<timestamp>.FCBak, <name>1 …): the server keeps its own.
    keep_label: saveAs renames the document after the file ('Plate' →
    'Plate.r01'); put the user's label back."""
    d, base = os.path.split(path)
    pat = re.compile(r"^(%s\.\d{8}-\d{6}(-\d+)?\.FCBak|%s\d+)$" % (re.escape(os.path.splitext(base)[0]),
                                                                     re.escape(base)))

    def baks():
        try:
            return set(n for n in os.listdir(d or ".") if pat.match(n))
        except OSError:
            return set()
    before = baks()
    label = doc.Label
    doc.saveAs(path)
    if keep_label and doc.Label != label:
        doc.Label = label
        doc.save()
    for n in baks() - before:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


# ---------------------------------------------------------------- styles

from drawing_support import (POCKET_DIRS, POCKET_MAX, content_change, content_hash, content_parts,  # noqa: E402
                             detect_pockets, pocket_key, same_signature, shape_signature, signature_change,
                             turned_axis)


def _outer_cylindrical(shape, a, p):
    """The largest convex cylinder about the axis (a, p) spans the part's
    extent across the axis — the OUTSIDE is turned, not just a bore."""
    ds = [2 * f.Surface.Radius for f in shape.Faces if f.Surface.__class__.__name__ == "Cylinder"
          and not an._is_concave(f) and abs(abs(an._canon(f.Surface.Axis).dot(a)) - 1) < 1e-6
          and ((an._axis_point(f.Surface) - p) - a * (an._axis_point(f.Surface) - p).dot(a)).Length < 1e-4]
    if not ds:
        return False
    D = max(ds)
    bb = an.tight_bb(shape)
    ext = [(abs(a.x), bb.XLength), (abs(a.y), bb.YLength), (abs(a.z), bb.ZLength)]
    if max(c for c, _ in ext) < 1 - 1e-6:
        return True                       # oblique axis: no cheap extent across it; trust the surface share
    # nothing sticks out beyond that cylinder (flats may cut into it)
    return all(0.8 * D <= L <= D + max(0.02 * D, 0.1) for c, L in ext if c < 0.5)


def detect_style(shape, process=None):
    """(style, axis, axis point, why). The part's process decides first:
    laser/sheet → plate, turning → turned. Otherwise (cnc, fdm, sla, unknown):
    plate when it is a uniform extrusion at most a quarter as thick as it is
    wide; turned only when ≥ 85 % of the surface is turned about one axis AND
    the outside is that turned cylinder (a plate with one big bore is not a
    lathe part); else prismatic."""
    proc = (process or "").strip().lower()
    a, p, frac = turned_axis(shape)
    if proc in ("laser", "sheet", "waterjet"):
        return "plate", None, None, "process %s" % proc
    if proc in ("turning", "lathe"):
        if a is not None and frac >= 0.5:
            return "turned", a, p, "process %s" % proc
    import manufacture as mf
    prof = mf.flat_profile(shape)
    bb = an.tight_bb(shape)
    dims = sorted([bb.XLength, bb.YLength, bb.ZLength])
    if prof["uniform"] and dims[0] <= 0.25 * dims[1]:
        return "plate", None, None, "uniform %s mm plate" % _w(dims[0])
    if a is not None and frac >= 0.85 and _outer_cylindrical(shape, a, p):
        return "turned", a, p, "axisymmetric (%d %% of the surface turned)" % round(100 * frac)
    return "prismatic", None, None, "not a uniform plate or a turned part" + (
        " (process %s, but not mostly turned)" % proc if proc in ("turning", "lathe") else "")


# --------------------------------------------------------- turned steps

def thread_text(t):
    """'M8×1.25-6g' from an FcThreads record."""
    size = str(t.get("size", "")).replace("x", "×").replace("X", "×")
    if "×" not in size and t.get("pitch"):
        size += "×%s" % _w(float(t["pitch"]))
    return size + ("-%s" % t["fit"] if t.get("fit") else "")


def _json_prop(o, name, default):
    try:
        v = json.loads(getattr(o, name, "") or "null")
    except ValueError:
        return default
    return default if v is None else v


def turned_profile(shape, axis, apt):
    """Outer diameter steps and silhouette of a turned part, along the axis
    (t measured from apt): steps [[d, t0, t1]] from the convex coaxial
    cylinders (split faces merged), silhouette [(t0, t1, r_min)] from every
    coaxial surface of revolution (chamfers and fillets included), bores
    [(r, t0, t1)] from the concave coaxial cylinders."""
    geo, sil, bores = [], [], []
    for f in shape.Faces:
        s = f.Surface
        k = s.__class__.__name__
        if k not in ("Cylinder", "Cone", "Toroid", "Sphere"):
            continue
        ax = getattr(s, "Axis", None)
        if ax is not None and abs(abs(an._canon(ax).dot(axis)) - 1) > 1e-6:
            continue
        c = an._axis_point(s) if k == "Cylinder" else (s.Apex if k == "Cone" else s.Center)
        if ((c - apt) - axis * (c - apt).dot(axis)).Length > 1e-4:
            continue
        ts = [(v.Point - apt).dot(axis) for v in f.Vertexes]
        rs = [((v.Point - apt) - axis * (v.Point - apt).dot(axis)).Length for v in f.Vertexes]
        if k == "Cylinder":
            t0, t1 = an._t_range(f, apt, axis)
            sil.append((t0, t1, s.Radius))
            if not an._is_concave(f):
                geo.append([round(2 * s.Radius, 4), t0, t1])
            else:
                bores.append((s.Radius, t0, t1))
        elif ts:
            sil.append((min(ts), max(ts), min(rs)))
    geo.sort(key=lambda g: (g[0], g[1]))
    steps = []
    for g in geo:
        if steps and abs(steps[-1][0] - g[0]) < 1e-6 and g[1] <= steps[-1][2] + 1e-6:
            steps[-1][2] = max(steps[-1][2], g[2])
        else:
            steps.append(list(g))
    steps.sort(key=lambda g: g[1])
    return steps, sil, bores


def turned_steps_info(part, shape, axis, apt, warnings=None):
    """Each outer step of a turned part with what its Ø dimension says:
    [{d, t0, t1, fit, thread, text}] in order along the axis. The fit and
    thread come from the body's FcSteps / FcThreads (z ranges, fc.turned and
    fc.external_thread), so a fit lands on the step that carries it even when
    another step has the same diameter; without FcSteps a fit from FcFits goes
    on the only plain step of that diameter (else it is reported, not drawn)."""
    warnings = [] if warnings is None else warnings
    geo, _, bores = turned_profile(shape, axis, apt)
    pl = an.global_placement(part)

    def t_of(z):
        return (pl.multVec(Vector(0, 0, float(z))) - apt).dot(axis)
    out = [{"d": g[0], "t0": g[1], "t1": g[2], "fit": None, "thread": None,
            "inner": sorted(set(round(r, 4) for r, a, b in bores if a < g[2] - 1e-6 and g[1] < b - 1e-6))}
           for g in geo]
    for t in _json_prop(part, "FcThreads", []):
        try:
            a, b = sorted((t_of(t["z_from"]), t_of(float(t["z_from"]) + float(t["length"]))))
            m = re.match(r"M(\d+(?:\.\d+)?)", str(t.get("size", "")).upper())
            major = float(m.group(1)) if m else None
        except (KeyError, TypeError, ValueError):
            continue
        hit = [s for s in out if major is not None and abs(s["d"] - major) < 0.05 and s["t0"] < b - 1e-6
               and a < s["t1"] - 1e-6]
        for s in hit:
            s["thread"] = dict(t, t0=a, t1=b)
        if not hit:
            warnings.append("thread %s (z %s–%s): no Ø%s step there — no thread callout drawn" % (
                thread_text(t), _w(float(t["z_from"])), _w(float(t["z_from"]) + float(t["length"])),
                _w(major or 0)))
    fsteps = _json_prop(part, "FcSteps", [])
    if fsteps:
        for st in fsteps:
            if not st.get("fit"):
                continue
            try:
                a, b = sorted((t_of(st["z0"]), t_of(st["z1"])))
            except (KeyError, TypeError, ValueError):
                continue
            hit = [s for s in out if abs(s["d"] - float(st["d"])) < 1e-3 and s["t0"] < b - 1e-6 and a < s["t1"] - 1e-6]
            for s in hit:
                s["fit"] = str(st["fit"])
            if not hit:
                warnings.append("fit Ø%s %s (z %s–%s): no step of that diameter there — fit not drawn" % (
                    _w(float(st["d"])), st["fit"], _w(float(st["z0"])), _w(float(st["z1"]))))
    else:
        for key, fit in sorted(_json_prop(part, "FcFits", {}).items()):
            try:
                d = float(key)
            except ValueError:
                continue
            cand = [s for s in out if abs(s["d"] - d) < 1e-3 and not s["thread"]]
            if len(cand) == 1:
                cand[0]["fit"] = str(fit)
            elif cand:
                warnings.append("fit Ø%s %s: %d steps have that diameter and the model has no FcSteps to say which "
                                "— fit not drawn; rebuild (fc.turned records each step's fit) or add it with an "
                                "add_dimension op" % (_w(d), fit, len(cand)))
    for s in out:
        if s["thread"]:
            s["text"] = thread_text(s["thread"])
        elif s["fit"]:
            import standards
            lo, hi = standards.tolerance(s["d"], s["fit"])
            s["text"] = "⌀%s %s (%+.3f/%+.3f)" % (_w(s["d"]), s["fit"], hi - s["d"], lo - s["d"])
        else:
            s["text"] = "⌀%s" % _w(s["d"])
    return out


def has_internal(shape, axis, apt):
    """A turned part with a bore or internal groove (a concave cylinder about
    its axis — internal chamfers and drill points come with one): what a
    section through the axis is for. Centre holes alone do not count."""
    for f in shape.Faces:
        s = f.Surface
        if s.__class__.__name__ != "Cylinder" or not an._is_concave(f) or \
                abs(abs(an._canon(s.Axis).dot(axis)) - 1) > 1e-6:
            continue
        c = an._axis_point(s)
        if ((c - apt) - axis * (c - apt).dot(axis)).Length < 1e-4:
            return True
    return False


def turned_bores_info(shape, axis, apt, holes=(), specs=()):
    """Each internal diameter of a turned part (concave coaxial cylinder runs,
    split faces merged) with what its Ø dimension in the section says:
    [{d, t0, t1, fit, tapped, text, bore: True}] along the axis. A bore made by
    a threaded Hole feature is called out by its thread (tapped: the size), one
    with an ISO 286 fit (FcFit) by the fit and its deviations."""
    _, _, bores = turned_profile(shape, axis, apt)
    runs = []
    for r, t0, t1 in sorted(bores, key=lambda b: (round(b[0], 6), b[1])):
        if runs and abs(runs[-1]["d"] - 2 * r) < 1e-6 and t0 <= runs[-1]["t1"] + 1e-6:
            runs[-1]["t1"] = max(runs[-1]["t1"], t1)
        else:
            runs.append({"d": round(2 * r, 4), "t0": t0, "t1": t1, "fit": None, "thread": None, "tapped": None,
                         "bore": True, "inner": []})
    runs.sort(key=lambda b: b["t0"])
    # one bore interrupted by internal grooves (only larger diameters between two runs of it): one Ø
    runs = [b for i, b in enumerate(runs) if not any(
        abs(a["d"] - b["d"]) < 1e-6 and runs[j + 1:i] and all(m["d"] > b["d"] + 1e-6 for m in runs[j + 1:i])
        for j, a in enumerate(runs[:i]))]
    for b in runs:
        b["text"] = "⌀%s" % _w(b["d"])
        for h in holes:
            if not _coaxial(h, axis, apt) or abs(h["diameters"][0] - b["d"]) > 1e-3:
                continue
            spec = hole_spec_for(h, specs)
            if not spec:
                continue
            if spec["threaded"]:
                b["tapped"], b["text"] = spec["thread"], spec["head"]
            elif " " in spec["head"] and spec["head"].startswith("⌀"):          # '⌀8 H7': a fit bore
                fit = spec["head"].split(" ", 1)[1]
                try:
                    import standards
                    lo, hi = standards.tolerance(b["d"], fit)
                    b["fit"] = fit
                    b["text"] = "⌀%s %s (%+.3f/%+.3f)" % (_w(b["d"]), fit, hi - b["d"], lo - b["d"])
                except Exception:  # noqa: BLE001 — not a class the table knows: as modelled
                    b["fit"], b["text"] = fit, "⌀%s %s" % (_w(b["d"]), fit)
            break
    return runs


def bore_depths(bores, tmin, tmax, zs):
    """The bore depths a section dimensions from the end face each bore opens
    to: [(t_face, t_in, r)] for a bore run that starts at (within 2 mm — a
    chamfer — of) an end face and stops inside the part, unless a planar
    shoulder at its end (zs, already a length) marks it or it runs on into a
    larger bore through a drill point or chamfer (the larger one's start is
    the depth that matters)."""
    out = []
    for b in bores:
        for t_face, t_open, t_in, sgn in ((tmin, b["t0"], b["t1"], 1), (tmax, b["t1"], b["t0"], -1)):
            if abs(t_open - t_face) > 2.0 or abs(t_in - (tmax if sgn > 0 else tmin)) <= 2.0:
                continue
            if any(abs(t_in - z) < 1e-3 for z in zs):
                continue
            if any(o["d"] > b["d"] + 1e-6 and 0 <= sgn * ((o["t0"] if sgn > 0 else o["t1"]) - t_in) <=
                   o["d"] - b["d"] + 1e-6 for o in bores):
                continue
            out.append((t_face, t_in, b["d"] / 2))
    return out


def turned_steps(shape, axis):
    """Positions along the axis of the end and shoulder faces of a turned part."""
    return sorted(set(round(f.CenterOfMass.dot(axis), 4) for f in shape.Faces
                      if f.Surface.__class__.__name__ == "Plane" and abs(abs(_face_normal(f).dot(axis)) - 1) < 1e-6))


def _coaxial(h, axis, apt):
    """A hole on the turning axis (a bore): no position dimensions."""
    if axis is None or apt is None:
        return False
    return abs(abs(h["axis"].dot(axis)) - 1) < 1e-6 and \
        ((h["point"] - apt) - axis * (h["point"] - apt).dot(axis)).Length < 1e-3


# ---------------------------------------------------------------- layout

VIEW_DIRS = {"front": (Vector(0, -1, 0), Vector(1, 0, 0)), "top": (Vector(0, 0, 1), Vector(1, 0, 0)),
             "left": (Vector(-1, 0, 0), Vector(0, -1, 0)), "right": (Vector(1, 0, 0), Vector(0, 1, 0)),
             "bottom": (Vector(0, 0, -1), Vector(1, 0, 0)), "back": (Vector(0, 1, 0), Vector(-1, 0, 0)),
             "iso": (Vector(1, -1, 1), None)}
# first-angle positions relative to the front view
SLOT_FROM_FRONT = {"top": "below", "bottom": "above", "left": "right", "right": "left", "back": "right"}


def _axes(d, xd):
    d = Vector(d)
    d.normalize()
    if xd is None:
        xd = Vector(0, 0, 1).cross(d)
        if xd.Length < 1e-6:
            xd = Vector(1, 0, 0)
    xd = Vector(xd)
    xd.normalize()
    return d, xd, d.cross(xd)


def _extent(shape, d, xd):
    """Projected (width, height) of shape seen along d with horizontal xd."""
    d, xd, yd = _axes(d, xd)
    bb = shape.BoundBox
    pts = [Vector(x, y, z) for x in (bb.XMin, bb.XMax) for y in (bb.YMin, bb.YMax) for z in (bb.ZMin, bb.ZMax)]
    us = [p.dot(xd) for p in pts]
    vs = [p.dot(yd) for p in pts]
    return max(us) - min(us), max(vs) - min(vs)


def hole_spec_for(h, specs):
    """The Hole feature spec that made detected hole h: by position (a profile
    centre on the hole's axis; the one matching its smallest Ø when several
    are), else — patterned or mirrored copies, profiles that cannot be read —
    by diameter, only when every feature of that diameter says the same."""
    dmin = h["diameters"][0]

    def off(c):
        return ((c - h["point"]) - h["axis"] * (c - h["point"]).dot(h["axis"])).Length
    on = [s for s in specs if s.get("centres") and any(off(c) < 1e-3 for c in s["centres"])]
    if on:
        return next((s for s in on if abs(s["d"] - dmin) < 0.011), on[0])
    cand = [s for s in specs if abs(s["d"] - dmin) < 0.011]
    return cand[0] if cand and len(set(s["head"] + s["depth"] + s["cut"] for s in cand)) == 1 else None


def hole_callout(h, spec):
    """Callout of one hole: its feature's thread / fit / Ø and counterbore or
    countersink, the depth as modelled (THRU when the hole opens at both ends,
    else the feature's depth, else the measured one)."""
    if spec:
        depth = "THRU" if h["through"] else (spec["depth"] if spec["depth"] != "THRU" else "↧%s" % _w(h["depth"]))
        return "%s %s%s" % (spec["head"], depth, spec["cut"])
    return "⌀%s %s" % (_w(h["diameters"][0]), "THRU" if h["through"] else "↧%s" % _w(h["depth"]))


def callout_text(hs, specs):
    """Callout for a group of identical holes (same feature spec, depth and
    diameters): 'N× …'. Returns (text, spec of the first hole)."""
    spec = hole_spec_for(hs[0], specs)
    txt = hole_callout(hs[0], spec)
    if len(hs) > 1:
        txt = "%d× %s" % (len(hs), txt)
    return txt, spec


def hole_groups(holes, specs=()):
    """Holes grouped by axis, diameters and callout (the Hole feature that made
    each one, matched by position, its depth and through state): one callout
    per distinct spec. [((axis name, diameters, callout), holes)]."""
    groups = {}
    for h in holes:
        groups.setdefault((h["axis_name"], tuple(h["diameters"]), hole_callout(h, hole_spec_for(h, specs))),
                          []).append(h)
    return sorted(groups.items(), key=lambda kv: kv[0])


BASELINE_MAX = 6        # more distinct hole coordinates along one axis: chain them in one row instead


def unique_coords(hs, xd, yd, skip_y=None):
    """Every distinct hole-centre coordinate along a view's horizontal and
    vertical axes, in hole order: ([(u, hole)], [(v, hole)])."""
    ux, uy = [], []
    for h in hs:
        u, v = h["from"].dot(xd), h["from"].dot(yd)
        if not any(abs(u - s) < 1e-3 for s, _ in ux):
            ux.append((u, h))
        if not (skip_y and skip_y(h)) and not any(abs(v - s) < 1e-3 for s, _ in uy):
            uy.append((v, h))
    return ux, uy


def position_plan(hs, xd, yd, skip_y, mode, done=None):
    """Which hole coordinates a group's position dimensions cover, per view
    axis: [(axis, [(coord, hole)], chain)]. Baseline: each coordinate from the
    edge in its own row, leaving out coordinates an earlier group of this view
    already has (done: {"x": [...], "y": [...]}, updated); more than
    BASELINE_MAX of them → a chain in one row. Pitch: always a chain."""
    done = {"x": [], "y": []} if done is None else done
    ux, uy = unique_coords(hs, xd, yd, skip_y)
    out = []
    for axis_, coords in (("x", ux), ("y", uy)):
        chain = mode == "pitch" or len(coords) > BASELINE_MAX
        if not chain:
            coords = [(c, h) for c, h in coords if not any(abs(c - q) < 1e-3 for q in done[axis_])]
        done[axis_] += [c for c, _ in coords]
        if coords:
            out.append((axis_, sorted(coords, key=lambda t: t[0]) if chain else coords, chain))
    return out


POCKET_WORD = {"pocket": "POCKET", "slot": "SLOT", "step": "STEP", "cutout": "CUT-OUT"}


def _model_axis(v):
    """'X' | 'Y' | 'Z' for a direction along a model axis, else None."""
    p = _principal(v)
    return p[0].upper() if p else None


def pocket_plan(shape, style, views, holes, hidden):
    """The pockets, slots, steps and cut-outs each orthographic view dimensions,
    as groups of identical features: {view: [group]}, and warnings. A feature
    seen from two views (a slot from the top and from its open end) goes where
    it is shallowest. Per group: pockets, rep (the one its callouts point at),
    L × W along the view's axes, depth_view (a view ⟂ to it that shows its floor
    edge-on: from an open side, or with hidden lines), linear (size as two
    dimensions on rep — one pocket, or no corner arc to hang a callout on),
    callout ('2× 40 × 20 ↧3', or '↧3' for one pocket whose depth no view
    shows), corners [(r, count)] for 'K× R3', and note (the size and depth as a
    NOTES line when there is no arc for a leader)."""
    found, warnings = [], []
    order = [n for n, _, _ in views if n != "iso"]
    for vi, (n, d, xd) in enumerate(views):
        if n == "iso" or not _principal(Vector(d).normalize()):
            continue
        for p in detect_pockets(shape, Vector(d), holes):
            found.append((p["depth"] if p["depth"] is not None else 1e18, vi, n, p))
    found.sort(key=lambda t: (t[0], t[1]))
    used, per_view = set(), {}
    for _, _, n, p in found:
        if used & set(p["faces"]):
            continue                                   # the same feature, already drawn where it is shallower
        used |= set(p["faces"])
        per_view.setdefault(n, []).append(p)
    seen = set(an._axis_name(Vector(d).normalize()) for n, d, _ in views if n != "iso")
    for name, dv in POCKET_DIRS:                       # cut from a side no view looks at: say so
        if name in seen:
            continue
        for p in detect_pockets(shape, Vector(*dv), holes):
            if used & set(p["faces"]):
                continue
            used |= set(p["faces"])
            a, b = sorted(p["box"])
            warnings.append("%s %s × %s%s from %s: no view looks at it — not dimensioned; add the view (views=[…, "
                            "'%s']) or add_dimension ops" % (
                                POCKET_WORD[p["kind"]].lower(), _w(p["box"][a][1] - p["box"][a][0]),
                                _w(p["box"][b][1] - p["box"][b][0]),
                                " ↧%s" % _w(p["depth"]) if p["depth"] is not None else " THRU", name,
                                next((k for k, (vd, _) in VIEW_DIRS.items() if k != "iso"
                                      and an._axis_name(Vector(vd)) == name), "?")))
    vdirs = {n: (Vector(d).normalize(), _axes(d, xd)) for n, d, xd in views if n != "iso"}
    out = {}
    for n in order:
        ps = per_view.get(n) or []
        if len(ps) > POCKET_MAX:
            warnings.append("%d more pockets/steps in the %s view were not dimensioned (at most %d per view) — add "
                            "them with add_dimension ops" % (len(ps) - POCKET_MAX, n, POCKET_MAX))
            ps = sorted(ps, key=lambda p: -(p["box"][sorted(p["box"])[0]][1] - p["box"][sorted(p["box"])[0]][0]) *
                        (p["box"][sorted(p["box"])[1]][1] - p["box"][sorted(p["box"])[1]][0]))[:POCKET_MAX]
        if not ps:
            continue
        d, (_, xd, yd) = vdirs[n]
        ax, ay = _model_axis(xd), _model_axis(yd)
        groups = {}
        for p in ps:
            groups.setdefault(pocket_key(p), []).append(p)
        glist = []
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            members.sort(key=lambda p: (p["box"][ax][0] * (1 if xd.dot(Vector(1, 1, 1)) > 0 else -1),
                                        p["box"][ay][0] * (1 if yd.dot(Vector(1, 1, 1)) > 0 else -1)))
            rep = max(members, key=lambda p: (p["box"][ax][1] + p["box"][ax][0]) * xd.dot(Vector(1, 1, 1)) +
                      (p["box"][ay][1] + p["box"][ay][0]) * yd.dot(Vector(1, 1, 1)))
            L, W = rep["box"][ax][1] - rep["box"][ax][0], rep["box"][ay][1] - rep["box"][ay][0]
            cut = rep["kind"] == "cutout"
            dv = None
            if not cut:
                side = [m for m, (d2, _) in vdirs.items() if m != n and abs(d2.dot(d)) < 1e-6]
                dv = next((m for m in side if an._axis_name(vdirs[m][0]) in rep["open"]), None) or \
                    (side[0] if side and hidden else None)
            if cut:
                depth_txt = "THRU" if style != "plate" else None
            else:
                depth_txt = None if dv else "↧%s" % _w(rep["depth"])
            radii = {}
            for p in members:
                for c in p["corners"]:
                    radii[c["r"]] = radii.get(c["r"], 0) + 1
            arcs = [c for c in rep["corners"] if c["edge"] is not None]
            size = "%s × %s" % (_w(L), _w(W))
            g = {"pockets": members, "rep": rep, "L": L, "W": W, "depth_view": dv, "corners": sorted(radii.items()),
                 "linear": len(members) == 1 or not arcs, "callout": None, "note": None, "depth_txt": depth_txt,
                 "size": size, "kind": rep["kind"]}
            if len(members) > 1 and arcs:
                g["callout"] = "%d× %s%s" % (len(members), size, " " + depth_txt if depth_txt else "")
            elif depth_txt and arcs:
                g["callout"] = depth_txt
            elif depth_txt or len(members) > 1:
                g["note"] = "%s%s %s%s (SEE VIEW)" % ("%d× " % len(members) if len(members) > 1 else "",
                                                     POCKET_WORD[rep["kind"]], size,
                                                     " " + depth_txt if depth_txt else "")
            glist.append(g)
        out[n] = glist
    return out, warnings


def plan_content(shape, style, axis, apt, views, holes, specs, dims_auto, steps=None, hole_mode="baseline",
                 pockets=None):
    """What each view will carry — rows of dimensions below (nx), columns of
    vertical dimensions left (their text widths, ny_w), hole and pocket
    callouts above, and for a turned side view its Ø texts — so the layout
    reserves room for exactly that."""
    c = {n: {"nx": 0, "ny_w": [], "callouts": [], "steps": None} for n, _, _ in views}
    if not dims_auto or not views:
        return c
    names = [n for n, _, _ in views]
    bb = an.tight_bb(shape)
    if style == "turned":
        zs = turned_steps(shape, axis) if axis is not None else []
        c[names[0]]["nx"] += max(0, len(zs) - 1) + (1 if any(st.get("thread") for st in steps or []) else 0)
        c[names[0]]["steps"] = steps or []
        if names[0] == "section":
            c[names[0]]["caption"] = SECTION_CAPTION
            c[names[0]]["section"] = True
            for n in names[1:2]:
                c[n]["cutline"] = True         # the base view: its cutting line and A–A letters stick out
            outer = [st for st in steps or [] if not st.get("bore")]
            if outer and apt is not None:
                c[names[0]]["nx"] += len(bore_depths([dict(st, t0=st["x0"], t1=st["x1"]) for st in steps
                                                      if st.get("bore")],
                                                     min(st["x0"] for st in outer), max(st["x1"] for st in outer),
                                                     [z - apt.dot(axis) for z in zs]))
    else:
        sizes = {n: _extent(shape, d, xd) for n, d, xd in views}
        dirs = {n: d for n, d, _ in views}

        def size_w(n):                      # a circular outline gets its Ø instead of width and height
            return text_w(("⌀" if outline_circle(shape, dirs[n]) else "") + _w(sizes[n][1]))
        c[names[0]]["nx"] += 1
        c[names[0]]["ny_w"].append(size_w(names[0]))
        if len(names) > 1:
            c[names[1]]["ny_w"].append(size_w(names[1]))
    by_axis = {}
    for n, d, xd in views:
        by_axis.setdefault(an._axis_name(an._canon(d)), (n, d, xd))
    done = {}
    for (axn, _, _), hs in hole_groups(holes, specs):
        if axn not in by_axis:
            continue
        n, d, xd = by_axis[axn]
        c[n]["callouts"].append(callout_text(hs, specs)[0])
        if style == "turned" and _coaxial(hs[0], axis, apt):
            continue
        _, xd, yd = _axes(d, xd)
        vmin = _extreme(shape, yd)
        for axis_, coords, chain in position_plan(hs, xd, yd, lambda h: style == "turned" and apt is not None
                                                  and abs((h["from"] - apt).dot(yd)) < 1e-3, hole_mode,
                                                  done.setdefault(n, {"x": [], "y": []})):
            if axis_ == "x":
                c[n]["nx"] += 1 if chain else len(coords)
                continue
            vs = [v for v, _ in coords]
            if chain:
                c[n]["ny_w"].append(max(text_w(_w(b - a)) for a, b in zip([vmin] + vs, vs)))
            else:
                c[n]["ny_w"] += [text_w(_w(v - vmin)) for v in vs]
    if style != "turned":
        for n, d, _ in views:
            bc = bolt_circle(shape, d, holes)
            if bc:
                c[n]["callouts"].append("⌀%s PCD" % _w(2 * bc[0]))
    axes_of = {n: _axes(d, xd) for n, d, xd in views if n != "iso"}
    for n, groups in (pockets or {}).items():
        if n not in c:
            continue
        d, xd, yd = axes_of[n]
        for g in groups:
            for p in g["pockets"]:
                for k_, (u0, u1), lo, hi in ((0, pocket_span(p, xd), _extreme(shape, xd), -_extreme(shape, xd * -1)),
                                             (1, pocket_span(p, yd), _extreme(shape, yd), -_extreme(shape, yd * -1))):
                    txt = ([_w(u0 - lo)] if u0 - lo > 1e-3 else []) + (
                        [_w(u1 - u0)] if g["linear"] and p is g["rep"] and not (u0 - lo < 1e-3 and hi - u1 < 1e-3)
                        else [])
                    if txt and k_ == 0:
                        c[n]["nx"] += 1
                    elif txt:
                        c[n]["ny_w"].append(max(text_w(t) for t in txt))
            c[n]["callouts"] += [corner_text(r, k) for r, k in g["corners"]] + ([g["callout"]] if g["callout"] else [])
            dv = g["depth_view"]
            if dv in c:
                _, _, yd2 = axes_of[dv]
                if abs(d.dot(yd2)) > 0.5:
                    c[dv]["ny_w"].append(text_w(_w(g["rep"]["depth"])))
                else:
                    c[dv]["nx"] += 1
    return c


def corner_text(r, k):
    """'4× R3' — k concave corners of radius r (just 'R3' for one)."""
    return ("%d× R%s" % (k, _w(r))) if k > 1 else "R%s" % _w(r)


def pocket_span(p, w):
    """(lo, hi) of a pocket's region along the view axis w (a model axis direction)."""
    a = _model_axis(w)
    return tuple(sorted((p["ext"]["-" + a].dot(w), p["ext"]["+" + a].dot(w))))


def _extreme(shape, d):
    """Minimum of p·d over the shape (d a unit vector): exact along a model
    axis (tight bounding box), else over finely sampled edges."""
    p = _principal(d)
    if p:
        bb = an.tight_bb(shape)
        lo, hi = {"x": (bb.XMin, bb.XMax), "y": (bb.YMin, bb.YMax), "z": (bb.ZMin, bb.ZMax)}[p[0]]
        return lo if p[1] > 0 else -hi
    pts = [v.Point for v in shape.Vertexes]
    for e in shape.Edges:
        try:
            pts += e.discretize(64)
        except Exception:  # noqa: BLE001
            pass
    return min(q.dot(d) for q in pts)


def turned_label_layout(steps, s, section=False):
    """Where each turned step's Ø text goes on the side view (paper mm along the
    axis as drawn, t × s; y from the axis): the dimension line about the middle
    of the step with its text to the right, inside the outline when the text
    fits on the step itself (clear of the centreline and of every other line),
    else stacked above the part — right to left, so a text never covers the
    line of a step that rises past it. In a section (section=True) no text
    sits on the hatching: an outer step's text always stacks above, a bore's
    (st["bore"]) goes inside the bore between the centreline and the bore line,
    clear of the outer steps' dimension lines, else above too.
    [{x_line, w, inside, Y, level}], in the order of steps."""
    if not steps:
        return []
    h = text_h()
    rmax = max(st["d"] for st in steps) / 2 * s
    out = []
    for st in steps:
        w = text_w(st["text"])
        a, b = sorted((st["x0"] * s, st["x1"] * s))
        mid, r = (a + b) / 2, st["d"] / 2 * s
        x_line = mid - w / 2 + 0.6
        if section:
            out.append({"x_line": mid, "w": w, "inside": False, "Y": r / 2, "level": None, "span": (a, b),
                        "bore": bool(st.get("bore")), "r": r})
            continue
        # the widest band between the centreline, the bore lines (drawn hidden) and the outline
        ys = sorted(set([0.0, r] + [q * s for q in st.get("inner") or [] if 0 < q * s < r]))
        lo, hi = max(zip(ys, ys[1:]), key=lambda g: g[1] - g[0])
        y_in = (lo + hi) / 2
        inside = x_line >= a + 0.5 and x_line + w <= b - 0.5 and hi - lo >= h + 1.0
        out.append({"x_line": x_line if inside else mid, "w": w, "inside": inside, "Y": y_in, "level": None})
    if section:                       # a bore's text inside the bore, off the outer steps' dimension lines
        lines = [o["x_line"] for o in out if not o["bore"]]
        for o in out:
            if not o["bore"] or o["r"] < h + 1.0:      # the band from the centreline to the bore line
                continue
            a, b = o["span"]
            cands = [a + 0.5 + 0.5 * i for i in range(int(max(0.0, b - a - 1.0 - o["w"]) / 0.5) + 1)]
            ok = [x for x in cands if x + o["w"] <= b - 0.5 and not any(x - 1.0 <= q <= x + o["w"] + 1.0
                                                                       for q in lines)]
            if ok:
                o["x_line"] = min(ok, key=lambda x: abs(x + o["w"] / 2 - (a + b) / 2))
                o["inside"], o["Y"] = True, o["r"] / 2
        for o in out:                 # a bore stacked above: its line ≥ 3 mm from the outer steps' lines
            if not o["bore"] or o["inside"]:
                continue
            a, b = o["span"]
            cands = [a + 1.0 + 0.25 * i for i in range(int(max(0.0, b - a - 2.0) / 0.25) + 1)]
            ok = [x for x in cands if all(abs(x - q) >= 3.0 for q in lines)]
            if ok:
                o["x_line"] = min(ok, key=lambda x: abs(x - (a + b) / 2))
    top = rmax + 4 + h / 2
    order = sorted((i for i, o in enumerate(out) if not o["inside"]), key=lambda i: (out[i]["x_line"], i))
    for k in reversed(range(len(order))):
        o = out[order[k]]
        lv = 0
        for q in (out[j] for j in order[k + 1:]):
            if o["x_line"] - 1.0 <= q["x_line"] <= o["x_line"] + o["w"] + 1.0:
                lv = max(lv, q["level"] + 1)
        o["level"], o["Y"] = lv, top + lv * DIM_STEP
    return out


CAPTION_SIZE = 5.0      # text height of a view caption ("SECTION A-A", "FLAT PATTERN")
SECTION_CAPTION = "SECTION A-A"
FLAT_CAPTION = "FLAT PATTERN"
SECTION_OPTIONS = ("auto", "full", "none")
CUTLINE_ROOM = 10.0     # paper mm a cutting line with its letters reaches beyond the base view's outline


def margins(cv, s, sizes=None):
    """(left, right, below, above) room around a view outline, paper mm."""
    left = (6 + sum(w + 3 for w in cv["ny_w"]) + 1) if cv["ny_w"] else 0.0
    below = (8 + DIM_STEP * (cv["nx"] - 1) + 3) if cv["nx"] else 0.0
    if cv.get("caption"):                 # its caption goes under the dimensions below it
        below += 4 + text_h(CAPTION_SIZE)
    pad = cv.get("pad", 0.0)              # labels round the view (a flat pattern's bend numbers)
    left, below = left + pad, below + pad
    nc = len(cv["callouts"])
    above = ((6 + 8 * (nc - 1) + 5) if nc else 0.0) + pad
    if cv.get("cutline"):                 # the cutting line's ends and letters; callouts start above them
        above = above + CUTLINE_ROOM if nc else CUTLINE_ROOM
        below = max(below, CUTLINE_ROOM)
    right = ((14 + max(text_w(t) for t in cv["callouts"]) / 2 + 3) if nc else 0.0) + pad
    if cv.get("cutline") and nc:          # … and further right, their leaders clear of the letters
        right += CUTLINE_ROOM
    if cv.get("steps"):
        lay = turned_label_layout(cv["steps"], s, bool(cv.get("section")))
        levels = [o["level"] for o in lay if not o["inside"]]
        if levels:
            above = 4 + text_h() + DIM_STEP * max(levels) + 2 + (8 * nc if nc else 0)
        xmax = max(max(st["x0"], st["x1"]) for st in cv["steps"]) * s
        right = max(right, max([o["x_line"] + o["w"] - xmax + 2 for o in lay] + [0]))
    return left, right, below, above


def notes_block(lines, avail_w):
    """Numbered notes wrapped to avail_w (paper mm) at NOTE_SIZE: (text lines, width, height)."""
    chars = max(16, int(avail_w / (CHAR_W * NOTE_SIZE)))
    text = ["NOTES"]
    for i, t in enumerate(lines, 1):
        head = "%d. " % i
        parts = textwrap.wrap(str(t), chars - len(head), break_on_hyphens=False) or [""]
        text.append(head + parts[0])
        text += ["\u00a0" * len(head) + p for p in parts[1:]]     # NBSP: TechDraw drops leading spaces
    w = CHAR_W * NOTE_SIZE * max(len(t) for t in text)
    return text, w, LINE_H * NOTE_SIZE * len(text)


def _overlap(a, b, gap=0.0):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


NEXT_PAPER = {"A4": "A3", "A3": "A2", "A2": "A1"}


def plan(shape, style, axis, spec, apt=None, holes=(), specs=(), note_lines=(), label="part", steps=None,
         section=False, flat=None, pockets=True):
    """Choose paper, scale and view positions (page mm, origin bottom-left).

    Each view reserves room for what it will carry (stacked dimensions, hole
    and pocket callouts, a turned part's Ø texts); views are packed first-angle
    around the first one, anchored at the top left; the notes go bottom left,
    above the title block or in the column right of the views (whichever
    fits), and the isometric view into the space left at the top right. The
    scale is the largest standard one at which all of that fits the paper.
    "remarks" says why a bigger paper or smaller scale was needed for the
    notes, and which requested views were left out. A turned part with
    section=True is drawn as SECTION A-A through the axis in place of its side
    view, with the end view (its base, carrying the cutting line) in
    projection with it. The pockets each view dimensions (pocket_plan) come
    back in "pockets", their NOTES lines added to "note_lines". flat (a sheet
    part's flat pattern face, at z=0) adds a FLAT PATTERN view in the row of
    the second view — left out (with a remark) when it would cost a bigger
    paper or a smaller scale. pockets=False: none dimensioned (a bent sheet
    part: its faces are the sheet's, everything it has is cut through)."""
    remarks = []
    if style == "turned":
        a = axis
        d = Vector(0, -1, 0) if abs(a.dot(Vector(0, 1, 0))) < 0.9 else Vector(1, 0, 0)
        d = d - a * d.dot(a)
        d.normalize()
        if section:              # end view in projection with the section: the cutting line vertical, arrows → it
            views = [("section", d, a), ("end", a * -1, d)]
        else:
            views = [("side", d, a), ("end", a * -1, d.cross(a) * -1)]
    elif style == "plate":
        bb = shape.BoundBox
        thin = min((bb.XLength, "x"), (bb.YLength, "y"), (bb.ZLength, "z"))[1]
        main = {"z": "top", "y": "front", "x": "right"}[thin]
        edge = "front" if main == "top" else "top"
        views = [(edge, ) + VIEW_DIRS[edge], (main, ) + VIEW_DIRS[main]]
    else:
        views = [("front", ) + VIEW_DIRS["front"], ("top", ) + VIEW_DIRS["top"], ("left", ) + VIEW_DIRS["left"]]
    wanted = spec.get("views")
    if wanted:
        bad = [n for n in wanted if n not in VIEW_DIRS]
        if bad:
            remarks.append("view %s unknown, left out — views are %s" % (", ".join(repr(n) for n in bad),
                                                                       ", ".join(VIEW_DIRS)))
        views = [(n, ) + VIEW_DIRS[n] for n in wanted if n in VIEW_DIRS and n != "iso"] or views
    iso = bool(spec.get("iso", True)) or "iso" in (wanted or [])
    sizes = {n: _extent(shape, d, xd) for n, d, xd in views}
    pockets_ok, pockets, pocket_warnings = pockets, {}, []
    if pockets_ok and style != "turned" and spec.get("dimensions", "auto") == "auto":
        pockets, pocket_warnings = pocket_plan(shape, style, views, list(holes), bool(spec.get("hidden")))
    note_lines = list(note_lines) + [g["note"] for gs in pockets.values() for g in gs if g["note"]]
    cont = plan_content(shape, style, axis, apt, views, list(holes), list(specs),
                        spec.get("dimensions", "auto") == "auto", steps, spec.get("hole_dims") or "baseline",
                        pockets)
    slots = {}
    for k, (n, _, _) in enumerate(views[1:], 1):
        if style == "turned":
            slots[n] = "right"
        elif views[0][0] == "front":
            slots[n] = SLOT_FROM_FRONT.get(n, "right")
        else:
            slots[n] = "below" if k == 1 else "right"
    fv = None
    if flat is not None:
        fv = ("flat", Vector(0, 0, 1), Vector(1, 0, 0))
        sizes["flat"] = _extent(flat, fv[1], fv[2])
        cont["flat"] = {"nx": 1, "ny_w": [text_w(_w(sizes["flat"][1]))], "callouts": [], "steps": None,
                        "caption": FLAT_CAPTION, "pad": 7.0}
        slots["flat"] = ("right_of", views[1][0] if len(views) > 1 else views[0][0])

    def arrange(s, use_flat=False):
        """View centres (local, first view at 0,0) and decorated boxes."""
        pos, boxes = {}, {}

        def deco(n, cx, cy):
            w, h = sizes[n][0] * s, sizes[n][1] * s
            L, R, B, T = margins(cont[n], s)
            return (cx - w / 2 - L, cy - h / 2 - B, cx + w / 2 + R, cy + h / 2 + T)
        n0 = views[0][0]
        pos[n0] = (0.0, 0.0)
        boxes[n0] = deco(n0, 0.0, 0.0)
        for n, _, _ in views[1:] + ([fv] if use_flat and fv else []):
            w, h = sizes[n][0] * s, sizes[n][1] * s
            L, R, B, T = margins(cont[n], s)
            sl = slots[n]
            if isinstance(sl, tuple):          # ("right_of", view): in that view's row, right of all there
                ry = pos[sl[1]][1]
                band = (ry - h / 2 - B, ry + h / 2 + T)
                hit = [b for b in boxes.values() if b[1] < band[1] and band[0] < b[3]]
                cx, cy = max(b[2] for b in hit) + 6 + L + w / 2, ry
            elif sl in ("right", "left"):
                band = (-h / 2 - B, h / 2 + T)
                hit = [b for b in boxes.values() if b[1] < band[1] and band[0] < b[3]]
                if sl == "right":
                    cx = max(b[2] for b in hit) + 6 + L + w / 2
                else:
                    cx = min(b[0] for b in hit) - 6 - R - w / 2
                cy = 0.0
            else:
                band = (-w / 2 - L, w / 2 + R)
                hit = [b for b in boxes.values() if b[0] < band[1] and band[0] < b[2]]
                if sl == "below":
                    cy = min(b[1] for b in hit) - 4 - T - h / 2
                else:
                    cy = max(b[3] for b in hit) + 4 + B + h / 2
                cx = 0.0
            pos[n] = (cx, cy)
            boxes[n] = deco(n, cx, cy)
        return pos, boxes

    def layout(s, paper, lines, use_flat=False):
        """The plan at scale s on paper, None when the views do not fit, or
        {"fail": …, "need", "room"} when only the notes do not."""
        W, H = PAPER[paper]
        pos, boxes = arrange(s, use_flat)
        X0 = min(b[0] for b in boxes.values())
        Y1 = max(b[3] for b in boxes.values())
        dx, dy = 24.0 - X0, H - 14.0 - Y1
        pos = {n: (p[0] + dx, p[1] + dy) for n, p in pos.items()}
        boxes = {n: (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for n, b in boxes.items()}
        right = max(b[2] for b in boxes.values())
        if right > W - 14.0 or min(b[1] for b in boxes.values()) < 14.0:
            return None
        title_left, title_top = W - 10.0 - TITLE_W, 10.0 + TITLE_H
        title = (title_left, 10.0, W - 10.0, title_top)
        tries = []
        for where in ("left", "above_title", "right"):
            if where == "left":
                text, nw, nh = notes_block(lines, title_left - 26.0 - 4.0)
                nx0, ny0 = 26.0, 18.0
                rect = (20.0, 10.0, nx0 + nw + 2, ny0 + nh + 2)
                if rect[2] > title_left:
                    continue
            elif where == "above_title":
                text, nw, nh = notes_block(lines, TITLE_W - 8.0)
                nx0, ny0 = title_left + 4.0, title_top + 4.0
                rect = (title_left, title_top, W - 10.0, ny0 + nh + 2)
            else:                                   # the column right of the views, above the title block
                x0 = right + 6.0
                if W - 14.0 - x0 < 45:
                    continue
                text, nw, nh = notes_block(lines, W - 14.0 - x0 - 2)
                nx0, ny0 = x0 + 2, title_top + 4.0
                rect = (x0, title_top, W - 10.0, ny0 + nh + 2)
            near = [b[1] for b in boxes.values() if b[0] < rect[2] and rect[0] < b[2]]
            room = min(near + [H - 14.0]) - (ny0 + 2)
            tries.append((nh, room))
            if rect[3] > H - 14.0 or any(_overlap(b, title) or _overlap(b, rect) for b in boxes.values()):
                continue
            return {"paper": paper, "W": W, "H": H, "scale": s, "pos": pos, "boxes": boxes, "right": right,
                    "notes": {"text": text, "x": nx0 + nw / 2, "y": ny0 + nh / 2, "rect": rect, "where": where},
                    "title_top": title_top}
        best = max(tries, key=lambda t: t[1] - t[0]) if tries else (0, 0)
        return {"fail": "notes", "need": best[0], "room": best[1]}

    forced_s, forced_p = parse_scale(spec.get("scale")), paper_name(spec.get("paper"))

    notes_fail = []

    def choose(lines, use_flat=False):
        choice = None
        for paper in ([forced_p] if forced_p else ["A4", "A3", "A2", "A1"]):
            for s in ([forced_s] if forced_s else SCALES):
                r = layout(s, paper, lines, use_flat)
                if r and "fail" in r:
                    notes_fail.append(r)
                if r and "fail" not in r:
                    if choice is None or s > choice["scale"]:
                        choice = r
                    break
            if choice and (choice["scale"] >= 1 or forced_p or forced_s):
                break
        return choice
    choice = choose(note_lines)
    use_flat, flat_free, flat_cost = False, None, None

    def free_column(ch):
        """(x0, y0, w, h) of the free column right of the views, above the title block and notes."""
        x0_ = ch["right"] + 8.0
        y0_ = max(ch["title_top"], ch["notes"]["rect"][3] if ch["notes"]["where"] != "left" else 0) + 4
        return x0_, y0_, ch["W"] - 14.0 - x0_, ch["H"] - 14.0 - y0_

    if fv is not None and choice:
        # the flat pattern: in that free column at its own scale (at most one standard step below the
        # sheet's), else in the layout at one step smaller overall, else left out
        fw, fh = sizes["flat"]
        L_, R_, B_, T_ = margins(cont["flat"], 1.0)
        fx0, fy0, fcw, fch = free_column(choice)
        room = fch - (45.0 if iso else 0.0)

        def fits_free(c):
            cap = em_width(FLAT_CAPTION + "  SCALE " + scale_text(c), CAPTION_SIZE * EM) + 4
            return fw * c + L_ + R_ <= fcw and cap <= fcw and fh * c + B_ + T_ + 7 <= room
        sf = max([c for c in SCALES if c <= choice["scale"] and fits_free(c)] or [0])
        if sf and sf >= choice["scale"] / 2.5 - 1e-9:
            flat_free = sf
        else:
            alt = choose(note_lines, True)
            if alt and alt["paper"] == choice["paper"] and alt["scale"] >= choice["scale"] / 2.5 - 1e-9:
                choice, use_flat = alt, True
            else:
                flat_cost = ("it needs %s at %s, the views fit %s at %s without it" % (
                    alt["paper"], scale_text(alt["scale"]), choice["paper"], scale_text(choice["scale"]))
                    if alt else "no room for it")
    if not choice:
        if forced_p or forced_s:
            raise DrawingError(
                "%s does not fit %s at %s with its views, dimensions and notes. %s set in this call or remembered "
                "from an earlier one in drawings.json — pass %s to go back to automatic (or choose a %s)." % (
                    label, forced_p or "any paper up to A1", scale_text(forced_s) if forced_s else "any standard scale",
                    " and ".join(x for x in ("paper=%s" % forced_p if forced_p else "",
                                             "scale=%s" % scale_text(forced_s) if forced_s else "") if x)
                    + (" were" if forced_p and forced_s else " was"),
                    " and/or ".join(x for x in ("scale=\"auto\"" if forced_s else "", "paper=\"auto\"" if forced_p
                                                else "") if x),
                    "smaller scale or a larger paper" if forced_p and forced_s else
                    ("larger paper" if forced_p else "smaller scale"))
                + (" The views fit; the notes block needs %d mm of height and only %d mm is free beside them — "
                   "shorten the notes." % (round(notes_fail[0]["need"]), max(0, round(notes_fail[0]["room"])))
                   if notes_fail else ""))
        raise DrawingError("%s does not fit A1 at 1:100 — draw it in parts (views=[…]) or check its size" % label)
    base = choose([], use_flat) if note_lines else None
    if base and (base["paper"], base["scale"]) != (choice["paper"], choice["scale"]):
        why = layout(base["scale"], base["paper"], note_lines, use_flat) or {}
        remarks.append("paper %s at %s: the notes need %d mm of height and %s at %s has only %d mm free for them "
                       "(beside the views) — shorten the notes or pass paper/scale to choose" % (
                           choice["paper"], scale_text(choice["scale"]), round(why.get("need", 0)), base["paper"],
                           scale_text(base["scale"]), max(0, round(why.get("room", 0)))))
    s, W, H = choice["scale"], choice["W"], choice["H"]
    out = {"paper": choice["paper"], "W": W, "H": H, "scale": s, "views": [], "style": style,
           "notes": choice["notes"], "remarks": remarks, "pockets": pockets, "note_lines": note_lines,
           "pocket_warnings": pocket_warnings}
    for n, d, xd in views + ([fv] if use_flat else []):
        out["views"].append({"name": n, "dir": d, "xdir": xd, "x": choice["pos"][n][0], "y": choice["pos"][n][1],
                             "w": sizes[n][0] * s, "h": sizes[n][1] * s})
    x0, y0, free_w, free_h = free_column(choice)
    if flat_free:                           # the flat pattern at its own scale low in the free column
        fw, fh = sizes["flat"]
        L_, R_, B_, T_ = margins(cont["flat"], 1.0)
        sf = flat_free
        out["views"].append({"name": "flat", "dir": fv[1], "xdir": fv[2], "scale": sf,
                             "x": x0 + (free_w - fw * sf - L_ - R_) / 2 + L_ + fw * sf / 2,
                             "y": y0 + B_ + 7 + fh * sf / 2, "w": fw * sf, "h": fh * sf,
                             "label": "SCALE %s" % scale_text(sf) if sf != s else None})
        y0 += B_ + 7 + fh * sf + T_
        free_h = H - 14.0 - y0
    elif fv is not None and not use_flat:
        remarks.append("flat pattern view left out: %s — the DXF carries the flat pattern (pass paper='%s' to draw "
                       "it)" % (flat_cost or "no room for it", NEXT_PAPER.get(choice["paper"], "A1")))
    if iso:
        iw, ih = _extent(shape, Vector(1, -1, 1), None)

        def iso_scale(fh):
            si_ = min(s, 0.9 * free_w / max(iw, 1e-6), 0.9 * fh / max(ih, 1e-6))
            return max([c for c in SCALES if c <= si_] or [0])
        si = iso_scale(free_h)
        if 0 < si < s:                              # room for its scale label underneath
            si = iso_scale(free_h - 7.0)
        if free_w > 35 and free_h > 30 and si > 0 and max(iw, ih) * si >= 20:
            out["views"].append({"name": "iso", "dir": Vector(1, -1, 1), "xdir": None, "scale": si,
                                 "x": x0 + free_w / 2, "y": H - 14.0 - 2 - ih * si / 2,
                                 "w": iw * si, "h": ih * si, "label": "SCALE %s" % scale_text(si) if si != s else None})
        else:
            smaller = next((c for c in SCALES if c < s), None)
            fixes = (["paper='%s'" % NEXT_PAPER[choice["paper"]]] if choice["paper"] in NEXT_PAPER else []) + \
                (["scale='%s'" % scale_text(smaller)] if smaller else [])
            remarks.append("iso view left out: no room on %s at %s — pass %s (or iso=false to stop asking)" % (
                choice["paper"], scale_text(s), " or ".join(fixes) or "fewer notes"))
    return out


# ---------------------------------------------------------------- page

def make_page(doc, name, paper):
    page = doc.addObject("TechDraw::DrawPage", re.sub(r"\W+", "_", name) + "_Sheet")
    page.Label = name + " sheet"        # labels are unique: the part already uses its own name
    tpl = doc.addObject("TechDraw::DrawSVGTemplate", page.Name + "_Template")
    orient = "Landscape"
    tpl.Template = template_path("%s_%s_ISO5457_minimal" % (paper, orient))
    page.Template = tpl
    return page, tpl


# editable fields of the ISO 5457 "minimal" title block (same layout on A4…A1):
# usable width (mm) and font size (template px = mm)
TITLE_FIELDS = {"part_material": (78.0, 5.0), "general_tolerances": (38.5, 5.0), "scale": (19.5, 5.0),
                "title": (78.0, 5.0), "creator": (58.0, 5.0), "approval_person": (58.0, 5.0),
                "document_type": (78.0, 5.0), "drawing_number": (78.0, 5.0), "revision_index": (19.5, 5.0),
                "date_of_issue": (38.0, 5.0), "sheet_number": (19.5, 5.0), "language_code": (19.5, 5.0),
                "legal_owner_1": (38.0, 3.5), "legal_owner_2": (38.0, 3.5), "legal_owner_3": (38.0, 3.5),
                "legal_owner_4": (38.0, 3.5)}


def fit_field(text, width, size):
    """text shortened to fit width mm at the template font size: first without
    its parenthetical parts, then cut with '…' (osifont widths, 5 % spare)."""
    t = str(text)

    def fits(x):
        return em_width(x, size) * 1.05 <= width
    if fits(t):
        return t
    t = re.sub(r"\s*\([^)]*\)", "", t).strip()
    if t and fits(t):
        return t
    while t and not fits(t.rstrip(" ,;:/-") + "…"):
        t = t[:-1]
    return t.rstrip(" ,;:/-") + "…"


IDENT_FIELDS = ("drawing_number", "revision_index")      # identifiers: never shortened, the text shrinks instead
MIN_IDENT_SIZE = 2.5                                      # mm: smallest text an identifier is shrunk to


def ident_size(text, width, size):
    """Largest text height (≤ size, 0.1 mm steps) at which text fits width, None below MIN_IDENT_SIZE."""
    w1 = em_width(str(text), 1.0) * 1.05
    s = min(size, math.floor(width / max(w1, 1e-9) * 10) / 10.0)
    return s if s >= MIN_IDENT_SIZE else None


def _field_sizes(tpl):
    """{field: text height} set on the template's own text elements (identifiers shrunk earlier)."""
    out = {}
    try:
        src = tpl.PageResult if os.path.isfile(tpl.PageResult or "") else tpl.Template
        svg = open(src, encoding="utf-8").read()
    except Exception:  # noqa: BLE001
        return out
    for m in re.finditer(r'<text\b[^>]*freecad:editable="([^"]+)"[^>]*>', svg):
        fs = re.search(r'font-size:\s*([\d.]+)', m.group(0))
        if fs:
            out[m.group(1)] = float(fs.group(1))
    return out


def _resize_fields(tpl, sizes):
    """Give title-block fields their own text height: a copy of the page's
    template with font-size set on those fields (stored in the document)."""
    src = tpl.PageResult if os.path.isfile(tpl.PageResult or "") else tpl.Template
    with open(src, encoding="utf-8") as fh:
        svg = fh.read()
    for k, size in sizes.items():
        def fix(m, size=size):
            tag = m.group(0)
            sty = re.search(r'style="([^"]*)"', tag)
            css = "font-size:%gpx" % size
            if sty:
                rest = re.sub(r'font-size:[^;"]*;?', "", sty.group(1)).strip(";")
                return tag.replace(sty.group(0), 'style="%s%s"' % (css, ";" + rest if rest else ""))
            return tag[:-1] + ' style="%s">' % css
        svg = re.sub(r'<text\b[^>]*freecad:editable="%s"[^>]*>' % re.escape(k), fix, svg, count=1)
    import hashlib
    stem = re.sub(r"\.[0-9a-f]{10}$", "", os.path.splitext(os.path.basename(tpl.Template or src))[0])
    folder = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "templates")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s.%s.svg" % (stem, hashlib.sha1(svg.encode("utf-8")).hexdigest()[:10]))
    if not os.path.isfile(path):
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(svg)
        os.replace(path + ".tmp", path)
    keep = dict(tpl.EditableTexts)
    tpl.Template = path                  # resets the editable texts to the template's: put them back
    tpl.EditableTexts = keep


def set_title(tpl, fields):
    """Fill title-block fields, each shortened to fit its cell on the ISO 5457
    templates — except the identifiers (drawing number, revision): their text
    shrinks to fit (down to MIN_IDENT_SIZE mm), else a DrawingError says to
    pass a shorter one. Returns (names the template does not have,
    [(name, full, shown)], [(identifier, text height)] shrunk)."""
    et = dict(tpl.EditableTexts)
    unknown, short, resize = [], [], {}
    minimal = "ISO5457_minimal" in os.path.basename(getattr(tpl, "Template", "") or "")
    cur = _field_sizes(tpl) if minimal and any(k in IDENT_FIELDS for k in fields) else {}
    for k, v in fields.items():
        if k in et:
            et[k] = str(v)
            if minimal and k in IDENT_FIELDS:
                width, size = TITLE_FIELDS[k]
                need = ident_size(str(v), width, size) if str(v) else size
                if need is None:
                    raise DrawingError("%s %r does not fit the title block even at %g mm text (the cell is %g mm "
                                       "wide) — an identifier is never shortened: pass a shorter %s" % (
                                           k, str(v), MIN_IDENT_SIZE, width,
                                           "drawing_number (drawing option drawing_number=… or title_fields)"
                                           if k == "drawing_number" else "revision_index"))
                if abs(cur.get(k, size) - need) > 1e-6:
                    resize[k] = need
            elif minimal and k in TITLE_FIELDS and str(v):
                et[k] = fit_field(v, *TITLE_FIELDS[k])
                if et[k] != str(v):
                    short.append((k, str(v), et[k]))
        else:
            unknown.append(k)
    if resize:
        _resize_fields(tpl, resize)
    tpl.EditableTexts = et
    return unknown, short, [(k, v) for k, v in sorted(resize.items()) if v < TITLE_FIELDS[k][1]]


def shrunk_text(shrunk):
    return "; ".join("%s text %g mm (instead of %g) so it fits whole" % (k, v, TITLE_FIELDS[k][1]) for k, v in shrunk)


def shortened_text(short):
    return "; ".join("%s '%s' → '%s'%s" % (k, full, shown, " (full text in the MATERIAL note)"
                                           if k == "part_material" else "") for k, full, shown in short)


def add_view(doc, page, part, vp, name, s, hidden=False):
    v = doc.addObject("TechDraw::DrawViewPart", "View_" + name)
    v.Label = name.title()
    page.addView(v)
    v.Source = [part]
    v.Direction = vp["dir"]
    if vp.get("xdir") is not None:
        v.XDirection = vp["xdir"]
    v.ScaleType = "Custom"
    v.Scale = vp.get("scale", s)
    v.X, v.Y = vp["x"], vp["y"]
    v.HardHidden = bool(hidden) and name != "iso"       # hidden edges come after the visible ones
    return v


SECTION_PATTERN = "Diagonal4"     # FCPAT.pat: 45° lines 4 mm apart
SECTION_HATCH = 0.6               # pattern scale → 2.4 mm between the hatch lines


def add_section_view(doc, page, part, vp, base, s, origin):
    """SECTION A-A: a full section through origin with normal vp["dir"],
    looking the same way as the view it stands in for (vp dir/xdir), hatched
    with 45° lines; TechDraw draws the cutting line with its A–A arrows on the
    base view. Its caption is a separate note under its dimensions."""
    v = doc.addObject("TechDraw::DrawViewSection", "View_section")
    v.Label = "Section"
    page.addView(v)
    v.BaseView = base
    v.Source = [part]
    v.ScaleType = "Custom"
    v.Scale = s
    v.SectionNormal = vp["dir"]
    v.Direction = vp["dir"]
    v.XDirection = vp["xdir"]
    v.SectionOrigin = origin
    v.SectionSymbol = "A"
    v.HardHidden = False
    try:
        v.CutSurfaceDisplay = "PatHatch"
        v.NameGeomPattern = SECTION_PATTERN
        v.HatchScale = SECTION_HATCH
    except Exception:  # noqa: BLE001 — another TechDraw: its default hatch
        pass
    vo = getattr(v, "ViewObject", None)
    for prop, val in (("FaceColor", (1.0, 1.0, 1.0, 1.0)), ("CutSurfaceColor", (1.0, 1.0, 1.0, 1.0)),
                      ("CutSurfaceTransparency", 100), ("GeomHatchColor", (0.0, 0.0, 0.0, 1.0)),
                      ("HatchColor", (0.0, 0.0, 0.0, 1.0))):
        try:                               # black hatch lines (1.1 defaults to a transparent green), no grey fill
            setattr(vo, prop, val)
        except Exception:  # noqa: BLE001
            pass
    v.X, v.Y = vp["x"], vp["y"]
    return v


def view_vertices(v):
    out = []
    for i in range(2000):
        try:
            x = v.getVertexByIndex(i)
        except Exception:  # noqa: BLE001
            break
        if x is None:
            break
        out.append((i, x.Point.x, x.Point.y))
    return out


def view_edges(v):
    out = []
    for i in range(4000):
        try:
            e = v.getEdgeByIndex(i)
        except Exception:  # noqa: BLE001
            break
        if e is None:
            break
        out.append((i, e))
    return out


def n_geom(view, verts=None):
    """Vertices that come from the model (cosmetic vertices follow them)."""
    verts = view_vertices(view) if verts is None else verts
    try:
        return max(0, len(verts) - len(view.CosmeticVertexes))
    except Exception:  # noqa: BLE001
        return len(verts)


def add_dim(doc, page, view, kind, refs2d=None, refs3d=None, x=0.0, y=0.0, text=None, fmt_spec=None):
    d = doc.addObject("TechDraw::DrawViewDimension", "Dim")
    d.Type = kind
    if refs3d:
        d.References3D = refs3d
        d.References2D = [(view, "")]
    else:
        d.References2D = [(view, r) for r in refs2d]
    page.addView(d)
    if text:
        d.Arbitrary = True
        d.FormatSpec = text
    elif fmt_spec:
        d.FormatSpec = fmt_spec
    d.X, d.Y = x, y
    return d


def _raw(d):
    try:
        return d.getRawValue()
    except Exception:  # noqa: BLE001
        return None


def view_axes(view):
    xd = Vector(view.XDirection) if view.XDirection.Length > 1e-9 else Vector(1, 0, 0)
    return _axes(view.Direction, xd)[1:]


def view_shape(view):
    srcs = [o for o in getattr(view, "Source", []) or [] if an.has_shape(o)]
    shapes = [an.global_shape(o) for o in srcs]
    return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)


def view_map(view, shape):
    """Model point → view coordinates (model units, the frame getVertexByIndex
    uses). projectPoint about the view's centroid, confirmed against the view's
    own vertices — TechDraw's Y convention differs between versions, so the
    one that lands on the drawn geometry wins; falls back to matching the
    extreme vertices."""
    verts = view_vertices(view)
    geom = [(x, y) for i, x, y in verts[:n_geom(view, verts)]]
    pts = [v.Point for v in shape.Vertexes[:300]]
    best = (0, None)
    try:
        c = view.getGeometricCenter()
        for inv in (False, True):
            o = view.projectPoint(c, inv)

            def f(p, inv=inv, o=o):
                q = view.projectPoint(p, inv)
                return q.x - o.x, q.y - o.y
            hits = 0
            for p in pts:
                u, v = f(p)
                if any(abs(u - x) < 1e-3 and abs(v - y) < 1e-3 for x, y in geom):
                    hits += 1
                    if hits >= 8:
                        break
            if hits > best[0]:
                best = (hits, f)
    except Exception:  # noqa: BLE001 — older TechDraw without projectPoint/getGeometricCenter
        pass
    if best[1] is not None:
        return best[1]
    if not geom or not pts:
        raise DrawingError("the %s view has no geometry yet — cannot place model points in it" % view.Label)
    xd, yd = view_axes(view)
    ox = min(p.dot(xd) for p in pts) - min(x for x, _ in geom)
    oy = min(p.dot(yd) for p in pts) - min(y for _, y in geom)
    return lambda p: (p.dot(xd) - ox, p.dot(yd) - oy)


def place_vertex(doc, view, pos, reuse=True):
    """A view vertex at pos (view coordinates): an existing one there, else a
    cosmetic vertex that is read back at pos (TechDraw's Y sign and units for
    cosmetic vertices differ between versions, so each is tried). Returns
    (index, cosmetic tag or None) or (None, None)."""
    ex, ey = pos
    if reuse:
        for i, x, y in view_vertices(view):
            if abs(x - ex) < 1e-3 and abs(y - ey) < 1e-3:
                return i, None
    sc = view.Scale
    for cand in (Vector(ex, ey, 0), Vector(ex, -ey, 0), Vector(ex * sc, ey * sc, 0), Vector(ex * sc, -ey * sc, 0)):
        before = set(i for i, _, _ in view_vertices(view))
        tag = view.makeCosmeticVertex(cand)
        doc.recompute()
        pump(2)
        new = [t for t in view_vertices(view) if t[0] not in before]
        if new and abs(new[-1][1] - ex) < 1e-3 and abs(new[-1][2] - ey) < 1e-3:
            return new[-1][0], tag
        if tag:
            view.removeCosmeticVertex(tag)
            doc.recompute()
    return None, None


# ----------------------------------------------------------- dimensioning

def view_box(shape, vmap):
    """The part's outline box in view coordinates (model units): its tight
    bounding box projected — exact for a view along a model axis, whatever
    the outline (a circle has no vertex at its extremes)."""
    bb = an.tight_bb(shape)
    pts = [vmap(Vector(x, y, z)) for x in (bb.XMin, bb.XMax) for y in (bb.YMin, bb.YMax) for z in (bb.ZMin, bb.ZMax)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def datum_vertex(doc, view, kk, shape, which):
    """A view vertex on the outline at its extreme ('left' | 'right' | 'low' |
    'high'): a drawn vertex there (the lowest / leftmost of them), else — a
    curved outline — a verified cosmetic vertex at the extreme itself.
    (index, (x, y)) or (None, None); one per view and side."""
    cache = kk.setdefault("datums", {})
    if which in cache:
        return cache[which]
    xmin, ymin, xmax, ymax = kk["box"]
    horiz = which in ("left", "right")
    target = {"left": xmin, "right": xmax, "low": ymin, "high": ymax}[which]
    geom = view_vertices(view)[:kk["n_geom"]]
    on = [t for t in geom if abs((t[1] if horiz else t[2]) - target) < 1e-3]
    res = (None, None)
    if on:
        t = min(on, key=lambda t: t[2] if horiz else t[1])
        res = (t[0], (t[1], t[2]))
    else:
        pts = []
        for e in shape.Edges:
            try:
                pts += [kk["vmap"](q) for q in e.discretize(48)]
            except Exception:  # noqa: BLE001
                pass
        if pts:
            q = min(pts, key=lambda p: abs((p[0] if horiz else p[1]) - target))
            pos = (target, q[1]) if horiz else (q[0], target)
            idx, _ = place_vertex(doc, view, pos)
            if idx is not None:
                res = (idx, pos)
    cache[which] = res
    return res


def outline_circle(shape, d):
    """(edge index, radius, centre) when the part seen along d is outlined by
    one circle (a disc or flange), else None."""
    d = Vector(d)
    d.normalize()
    bb = an.tight_bb(shape)
    ext = [(Vector(1, 0, 0), bb.XLength), (Vector(0, 1, 0), bb.YLength), (Vector(0, 0, 1), bb.ZLength)]
    across = [L for a, L in ext if abs(a.dot(d)) < 1e-6]
    if len(across) != 2 or abs(across[0] - across[1]) > 1e-3:
        return None
    c0 = bb.Center
    for i, e in enumerate(shape.Edges):
        cv = e.Curve
        if cv.__class__.__name__ != "Circle" or abs(abs(cv.Axis.dot(d)) - 1) > 1e-6 or \
                abs(2 * cv.Radius - across[0]) > 1e-3:
            continue
        off = (cv.Center - c0) - d * (cv.Center - c0).dot(d)
        if off.Length > 1e-3:
            continue
        pts = [v.Point for v in shape.Vertexes]
        for e2 in shape.Edges:
            try:
                pts += e2.discretize(16)
            except Exception:  # noqa: BLE001
                pass
        if all(((q - cv.Center) - d * (q - cv.Center).dot(d)).Length <= cv.Radius + 1e-3 for q in pts):
            return i, cv.Radius, cv.Center              # nothing sticks out of the circle
    return None


def overall_dims(doc, page, view, w_exp, h_exp, which, k, shape=None, part=None):
    """Overall horizontal/vertical size between the outline's extremes (drawn
    vertices, or cosmetic ones where a curved outline has none); a circular
    outline gets its Ø instead."""
    dims = []
    if not view_vertices(view):
        return dims
    sc = view.Scale             # vertices are model units; a dimension's X/Y is paper mm
    off = lambda k_: 8 + DIM_STEP * k_  # noqa: E731
    circ = outline_circle(shape, view.Direction) if shape is not None and part is not None else None
    if circ is not None:
        i, r, _ = circ
        xmin, ymin, xmax, ymax = k["box"]
        d = add_dim(doc, page, view, "Diameter", refs3d=[(part, "Edge%d" % (i + 1))], fmt_spec="⌀%.2w")
        d.X, d.Y = y_column(k, sc, "⌀" + _w(2 * r)), ((ymin + ymax) / 2 + 0.3 * (ymax - ymin)) * sc
        k["y"] += 1
        dims.append((d, 2 * r, "overall Ø"))
        return dims
    for axis_ in ("x", "y"):
        if axis_ not in which:
            continue
        lo, hi = ("left", "right") if axis_ == "x" else ("low", "high")
        (ia, a), (ib, b) = datum_vertex(doc, view, k, shape, lo), datum_vertex(doc, view, k, shape, hi)
        exp = w_exp if axis_ == "x" else h_exp
        if ia is None or ib is None:
            k.setdefault("missing", []).append("overall %s" % ("width" if axis_ == "x" else "height"))
            continue
        if axis_ == "x":
            d = add_dim(doc, page, view, "DistanceX", ["Vertex%d" % ia, "Vertex%d" % ib],
                        x=(a[0] + b[0]) / 2 * sc, y=k["ylow"] * sc - off(k["x"]))
        else:
            d = add_dim(doc, page, view, "DistanceY", ["Vertex%d" % ia, "Vertex%d" % ib],
                        x=y_column(k, sc, _w(exp)), y=(a[1] + b[1]) / 2 * sc)
        k[axis_] += 1
        dims.append((d, exp, "overall width" if axis_ == "x" else "overall height"))
        lay_add(k, d, axis_, (a[0] * sc, a[1] * sc), (b[0] * sc, b[1] * sc), _w(exp))
    return dims


def y_column(kk, sc, txt, w=None):
    """Label x (paper mm from the view centre) of the next vertical dimension
    left of the view: its line 6 mm out from the previous column's text, its
    horizontal text left of the line — columns never run through a text."""
    w = text_w(txt) if w is None else w
    line = kk.get("y_next", kk["xlow"] * sc - 6)
    kk["y_next"] = line - w - 3
    return line - w / 2


def hole_specs(part):
    """Callout parts per Hole feature: head (thread / fit / Ø), depth (THRU or
    ↧d as modelled), cut (counterbore / countersink), its modelled Ø and the
    profile centres in global coordinates (None when unreadable) — holes are
    matched to their feature by position (hole_spec_for)."""
    out = []
    body = part if part.TypeId == "PartDesign::Body" else None
    if not body:
        return out
    for h in body.Group:
        if h.TypeId != "PartDesign::Hole" or an.has_shape(h) is False:
            continue
        centres = None
        try:
            sk = h.Profile[0]
            pl = sk.getGlobalPlacement()
            centres = [pl.multVec(g.Center) for g in sk.Geometry if g.__class__.__name__ == "Circle"] or None
        except Exception:  # noqa: BLE001
            centres = None
        dia = h.Diameter.Value
        depth = "THRU" if h.DepthType == "ThroughAll" else "↧%s" % _w(h.Depth.Value)
        if h.Threaded:
            head = "%s-%s" % (h.ThreadSize.replace("x", "×"), h.ThreadClass)
        else:
            head = "⌀%s" % _w(dia)
        fit = getattr(h, "FcFit", "")
        if fit:
            head = "⌀%s %s" % (_w(dia), fit)
        cut = ""
        if h.HoleCutType not in ("None", ""):
            if "sink" in h.HoleCutType.lower() or h.HoleCutType in ("ISO 10642", "ISO 2009", "ISO 7046", "ISO 14583"):
                cut = "  ⌵ ⌀%s × %s°" % (_w(h.HoleCutDiameter.Value), _w(h.HoleCutCountersinkAngle.Value))
            else:
                cut = "  ⌴ ⌀%s ↧%s" % (_w(h.HoleCutDiameter.Value), _w(h.HoleCutDepth.Value))
        out.append({"d": round(dia, 3), "n": len(centres or [None]), "head": head, "depth": depth, "cut": cut,
                    "text": "%s %s%s" % (head, depth, cut), "threaded": bool(h.Threaded), "name": h.Label,
                    "thread": h.ThreadSize if h.Threaded else None, "centres": centres})
    return out


def hole_compensation(part):
    """Largest print compensation (FcHoleComp, mm) of the part's Hole features."""
    group = part.Group if part.TypeId == "PartDesign::Body" else []
    return max([float(getattr(h, "FcHoleComp", 0.0) or 0.0) for h in group if h.TypeId == "PartDesign::Hole"]
               or [0.0])


def _chamfer_where(f):
    """Where a chamfer feature's edges are: '⌀20' per circular edge, 'N straight edges'."""
    circles, lines = [], 0
    try:
        base, subs = f.Base[0], f.Base[1]
    except Exception:  # noqa: BLE001
        return []
    for sub in subs or []:
        try:
            e = base.Shape.getElement((re.findall(r"Edge\d+", str(sub)) or [str(sub)])[-1])
        except Exception:  # noqa: BLE001
            continue
        if e.Curve.__class__.__name__ == "Circle":
            circles.append(round(2 * e.Curve.Radius, 3))
        else:
            lines += 1
    out = ["⌀%s" % _w(d) for d in sorted(set(circles), reverse=True)]
    if lines:
        out.append("%d STRAIGHT EDGE%s" % (lines, "S" if lines > 1 else ""))
    return out


def chamfer_size(part, shape, axis=None):
    """The model's chamfers for the edge note: their leg (mm) when every one is
    the same equal-leg 45° size, 0 when there are none, else the sizes with
    where each one is ('1.5 × 45° AT ⌀20; 0.5 × 45° AT ⌀16, ⌀8') — a size the
    note does not state is never left to 'UNLESS STATED'."""
    sizes = []                                    # [(size text, [where])]
    if part.TypeId == "PartDesign::Body":
        for f in part.Group:
            if f.TypeId == "PartDesign::Chamfer" and an.has_shape(f) is not False:
                kind = getattr(f, "ChamferType", "Equal distance")
                if kind in ("Equal distance", 0):
                    txt = "%s × 45°" % _w(f.Size.Value)
                elif kind in ("Two distances", 1):
                    txt = "%s × %s" % (_w(f.Size.Value), _w(f.Size2.Value))
                else:
                    txt = "%s × %s°" % (_w(f.Size.Value), _w(f.Angle.Value))
                sizes.append((txt, _chamfer_where(f)))
    if not sizes and axis is not None:            # a turned shape without features: 45° cones
        for f in shape.Faces:
            s = f.Surface
            if s.__class__.__name__ == "Cone" and abs(abs(math.degrees(s.SemiAngle)) - 45) < 0.5 \
                    and abs(abs(s.Axis.dot(axis)) - 1) < 1e-6:
                t = [v.Point.dot(axis) for v in f.Vertexes]
                rs = [e.Curve.Radius for e in f.Edges if e.Curve.__class__.__name__ == "Circle"]
                if t and max(t) - min(t) < 5:
                    sizes.append(("%s × 45°" % _w(max(t) - min(t)), ["⌀%s" % _w(2 * max(rs))] if rs else []))
    if not sizes:
        return 0.0
    kinds = sorted(set(t for t, _ in sizes), key=lambda t: -float(t.split(" ")[0]))
    if len(kinds) == 1 and kinds[0].endswith("× 45°"):
        return float(kinds[0].split(" ")[0])
    out = []
    for k in kinds:
        where = []
        for t, w in sizes:
            if t == k:
                where += [x for x in w if x not in where]
        out.append(k + (" AT " + ", ".join(where) if where else ""))
    return "; ".join(out)


def _face_normal(f):
    u0, u1, v0, v1 = f.ParameterRange
    return f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)


def _w(x):
    t = ("%.2f" % x).rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _hole_edges(shape, h):
    """Indices (into shape.Edges) of the edges of hole h's own faces; None when
    the hole record does not list its faces."""
    names = h.get("faces")
    if not names:
        return None
    mine = []
    for n in names:
        try:
            mine += [e for e in shape.Faces[int(n[4:]) - 1].Edges if e.Curve.__class__.__name__ == "Circle"]
        except (IndexError, ValueError):
            return None
    return set(i for i, e in enumerate(shape.Edges) if any(e.isSame(m) for m in mine))


def section_keep(view):
    """(origin, normal) of a section view's cutting plane — what lies on the normal's side is cut away — or None."""
    if view.TypeId != "TechDraw::DrawViewSection":
        return None
    n = Vector(view.SectionNormal)
    n.normalize()
    return Vector(view.SectionOrigin), n


def _circle_edge(shape, h, outer=True, keep=None):
    """Index of a circle edge of hole h: its outermost one (counterbore,
    countersink) or the one nearest the hole diameter. Only the hole's own
    edges count — never a coaxial outside diameter (a turned part's OD) —
    and in a section view (keep: section_keep) only those it still shows."""
    idx, best = None, None
    own = _hole_edges(shape, h)
    for i, e in enumerate(shape.Edges):
        if e.Curve.__class__.__name__ != "Circle" or (own is not None and i not in own):
            continue
        c = e.Curve.Center
        if keep is not None and (c - keep[0]).dot(keep[1]) > 1e-6:
            continue
        off = ((c - h["point"]) - h["axis"] * (c - h["point"]).dot(h["axis"])).Length
        if off > 1e-4 or abs(abs(e.Curve.Axis.dot(h["axis"])) - 1) > 1e-6:
            continue
        key = e.Curve.Radius if outer else -abs(2 * e.Curve.Radius - h["diameters"][0])
        if best is None or key > best:
            best, idx = key, i
    return idx


def hole_dims(doc, page, part, shape, views_by_axis, k_by_view, out_dims, warnings, holes=None, specs=None,
              vmaps=None):
    """Callout + position dimensions for each group of identical holes, in the
    view that looks along their axis."""
    holes = an.detect_holes(shape) if holes is None else holes
    specs = hole_specs(part) if specs is None else specs
    vmaps = {} if vmaps is None else vmaps
    callouts = []
    for (axn, diams, _), hs in hole_groups(holes, specs):
        view = views_by_axis.get(axn)
        if view is None:
            hs_ = [h for h in hs if not any(_coaxial(h, kv.get("axis"), kv.get("axis_pt")) for kv in k_by_view.values())]
            if hs_:
                warnings.append("%s along %s: no view looks along that axis — no callout or position on the sheet; "
                                "add a view (views=[…]) or dimension them with add_dimension ops"
                                % (callout_text(hs_, specs)[0], axn))
                k_by_view[next(iter(k_by_view))].setdefault("missing", []).extend(
                    "hole at (%s)" % an._pt(h["from"]) for h in hs_)
            continue
        dmin = diams[0]
        txt, spec = callout_text(hs, specs)
        kk = k_by_view[view.Name]
        if view.Name not in vmaps:
            vmaps[view.Name] = kk.get("vmap") or view_map(view, shape)
        vmap = vmaps[view.Name]
        # the callout sits above right of the view: attach it to the hole nearest
        # there, on its outermost circle when it has a counterbore/countersink
        h0 = max(hs, key=lambda h: sum(vmap(h["at"])))
        cut = len(diams) > 1 or bool(spec and ("⌴" in spec["text"] or "⌵" in spec["text"]))
        edge_idx = _circle_edge(shape, h0, outer=cut, keep=section_keep(view))
        coax = _coaxial(h0, kk.get("axis"), kk.get("axis_pt"))     # a lathe bore: Ø only
        if edge_idx is None:
            # a hole in a curved face (cross hole) has no circular edge: dimension a
            # verified cosmetic circle at the hole instead
            d = cosmetic_circle_callout(doc, page, view, h0, dmin, txt, kk, vmap)
            if d is None:
                warnings.append("could not place the callout %s for the hole at (%s) — add it with an "
                                "add_hole_callout op" % (txt, an._pt(h0["at"])))
        else:
            d = add_dim(doc, page, view, "Diameter", refs3d=[(part, "Edge%d" % (edge_idx + 1))], text=txt)
            d.X, d.Y = kk["w"] / 2 + 14 + kk.get("callout_dx", 0), kk["h"] / 2 + 6 + 8 * kk["callouts"]      # paper mm
            kk["callouts"] += 1
        if d is not None:
            callouts.append((txt, view.Label, bool(spec and spec["threaded"])))
        if not coax:
            # positions: unique centre coordinates along the view's horizontal and vertical axes
            _position_dims(doc, page, view, hs, kk, out_dims, warnings, shape, vmap, kk.get("hole_mode", "baseline"))
    for view in dict((v.Name, v) for v in views_by_axis.values()).values():
        kk = k_by_view[view.Name]
        if "box" in kk and not kk.get("axis"):
            pcd_dim(doc, page, view, kk, shape, holes, out_dims, callouts)
    return callouts


def bolt_circle(shape, d, holes):
    """(radius, centre, n) of the largest set (≥ 3) of holes along d whose
    centres lie on one circle about the centre of a circular outline, else None."""
    circ = outline_circle(shape, d)
    if circ is None:
        return None
    d = Vector(d)
    d.normalize()
    c = circ[2]
    rs = []
    for h in holes:
        if abs(abs(h["axis"].dot(d)) - 1) > 1e-6:
            continue
        r = ((h["point"] - c) - d * (h["point"] - c).dot(d)).Length
        if r > 1e-3:
            rs.append(r)
    best = None
    for r in rs:
        n = sum(1 for q in rs if abs(q - r) < 1e-3)
        if n >= 3 and (best is None or n > best[2]):
            best = (r, c, n)
    return best


def pcd_dim(doc, page, view, kk, shape, holes, out_dims, callouts):
    """A circular part's hole circle: a chain-line cosmetic circle through the
    hole centres with its verified '⌀D PCD'."""
    bc = bolt_circle(shape, view.Direction, holes)
    if bc is None:
        return None
    r, c, n = bc
    idx, _ = place_circle(doc, view, kk["vmap"](c), r)
    if idx is None:
        return None
    d = add_dim(doc, page, view, "Diameter", ["Edge%d" % idx], fmt_spec="⌀%.2w PCD")
    d.X, d.Y = kk["w"] / 2 + 14 + kk.get("callout_dx", 0), kk["h"] / 2 + 6 + 8 * kk["callouts"]
    kk["callouts"] += 1
    out_dims.append((d, 2 * r, "hole circle Ø (%d holes)" % n))
    callouts.append(("⌀%s PCD" % _w(2 * r), view.Label, False))
    return d


def place_circle(doc, view, pos, r):
    """A cosmetic circle (chain line) centred at pos (view coordinates) that is
    read back there: (edge index, tag) or (None, None)."""
    ex, ey = pos
    for cand in (Vector(ex, ey, 0), Vector(ex, -ey, 0)):
        before = set(i for i, _ in view_edges(view))
        try:
            tag = view.makeCosmeticCircle(cand, r, 4)
        except Exception:  # noqa: BLE001 — no style argument in this TechDraw
            tag = view.makeCosmeticCircle(cand, r)
        doc.recompute()
        pump(2)
        new = [i for i, _ in view_edges(view) if i not in before]
        if new:
            e = view.getEdgeByIndex(new[-1])
            if e is not None and abs(e.Curve.Center.x - ex) < 1e-3 and abs(e.Curve.Center.y - ey) < 1e-3 \
                    and abs(e.Curve.Radius - r) < 1e-3:
                return new[-1], tag
        if tag:
            view.removeCosmeticEdge(tag)
        doc.recompute()
    return None, None


def place_line(doc, view, a, b, style=2):
    """A cosmetic line from a to b (view coordinates) that is read back there
    (TechDraw's Y sign and units for cosmetic geometry differ between
    versions, so each is tried): its tag, or None."""
    sc = view.Scale
    for k, sy in ((1.0, 1.0), (1.0, -1.0), (sc, 1.0), (sc, -1.0)):
        before = set(i for i, _ in view_edges(view))
        try:
            tag = view.makeCosmeticLine(Vector(a[0] * k, a[1] * k * sy, 0), Vector(b[0] * k, b[1] * k * sy, 0), style)
        except Exception:  # noqa: BLE001
            return None
        doc.recompute()
        pump(2)
        new = [i for i, _ in view_edges(view) if i not in before]
        if new:
            e = view.getEdgeByIndex(new[-1])
            ends = [(v.Point.x, v.Point.y) for v in e.Vertexes] if e is not None else []
            if len(ends) == 2 and any(all(abs(p[0] - q[0]) < 1e-3 and abs(p[1] - q[1]) < 1e-3 for p, q in zip(ends, ab))
                                      for ab in ((a, b), (b, a))):
                return tag
        if tag:
            view.removeCosmeticEdge(tag)
            doc.recompute()
    return None


def sheet_flat_pattern(part, sheet, warnings, infos):
    """{face, lines} of a sheet-metal part's flat pattern from fc.sheet_flat
    (rebuilt from its FcSheet), or None with the reason reported."""
    fn = getattr(fc, "sheet_flat", None)
    if fn is None:
        infos.append("flat      no flat pattern view (this fclib has no sheet_flat) — the notes point to the DXF")
        return None
    try:
        face, lines = fn(part)
    except Exception as e:  # noqa: BLE001
        warnings.append("flat pattern view left out: fc.sheet_flat(%s) failed (%s) — the notes point to the DXF"
                        % (part.Label, e))
        return None
    if face is None or face.isNull() or not face.Faces:
        warnings.append("flat pattern view left out: fc.sheet_flat(%s) gave no face — the notes point to the DXF"
                        % part.Label)
        return None
    return {"face": face, "lines": [e for e in lines or [] if e is not None and len(e.Vertexes) >= 2]}


def flat_bend_lines(doc, page, view, kk, lines, sheet, warnings):
    """The flat pattern's bend lines, dashed (each read back where the flat
    pattern has it), numbered like the bend table (B1 …) just past one end."""
    vmap, sc = kk["vmap"], view.Scale
    bends = (sheet or {}).get("bends") or []
    for i, e in enumerate(lines):
        n = bends[i].get("n", i + 1) if i < len(bends) and isinstance(bends[i], dict) else i + 1
        a, b = sorted((vmap(e.Vertexes[0].Point), vmap(e.Vertexes[-1].Point)), key=lambda q: (q[1], q[0]))
        if place_line(doc, view, a, b) is None:
            warnings.append("bend line B%s could not be drawn on the flat pattern view" % n)
            continue
        L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0          # the number past its upper (right) end
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        add_caption(doc, page, "%s_Bend%s" % (page.Name, n), "B%s" % n,
                    view.X.Value + b[0] * sc + ux * 4.5, view.Y.Value + b[1] * sc + uy * 4.5, size=NOTE_SIZE)


def cosmetic_circle_callout(doc, page, view, h, dia, txt, kk, vmap):
    """Callout on a cosmetic circle drawn at a hole whose own edges are not
    circles; the circle's diameter is verified before the text is applied."""
    ex, ey = vmap(h["at"])
    for cand in (Vector(ex, ey, 0), Vector(ex, -ey, 0)):
        before = set(i for i, _ in view_edges(view))
        tag = view.makeCosmeticCircle(cand, dia / 2)
        doc.recompute()
        pump(2)
        new = [i for i, _ in view_edges(view) if i not in before]
        if new:
            e = view.getEdgeByIndex(new[-1])
            if e is not None and abs(e.Curve.Center.x - ex) < 1e-3 and abs(e.Curve.Center.y - ey) < 1e-3:
                d = add_dim(doc, page, view, "Diameter", ["Edge%d" % new[-1]])
                doc.recompute()
                pump(2)
                if abs((_raw(d) or 0) - dia) < 1e-3:
                    d.Arbitrary = True
                    d.FormatSpec = txt
                    d.X, d.Y = kk["w"] / 2 + 14 + kk.get("callout_dx", 0), kk["h"] / 2 + 6 + 8 * kk["callouts"]
                    kk["callouts"] += 1
                    return d
                doc.removeObject(d.Name)
        if tag:
            view.removeCosmeticEdge(tag)
        doc.recompute()
    return None


def _position_dims(doc, page, view, hs, kk, out_dims, warnings, shape, vmap, mode="baseline"):
    """Hole-centre positions as seen in this view, every distinct coordinate,
    each created, measured and kept only if it shows the model's value (see
    place_checked_dim). Measured from the outline's left / bottom extreme
    (datum_vertex — a cosmetic vertex on a round outline). baseline: one
    dimension per centre from the edge, each in its own row (coordinates an
    earlier group already has are left out; more than BASELINE_MAX → a chain);
    pitch: a chain in one row — the first centre from the edge, then centre to
    centre. A coordinate that cannot be placed is a warning and counts as not
    verified (kk["missing"])."""
    if not kk.get("n_geom"):
        return
    xd, yd = view_axes(view)
    sc = view.Scale
    off = lambda k_: 8 + DIM_STEP * k_  # noqa: E731 — paper mm
    skip_y = lambda h: kk.get("axis") is not None and abs((h["from"] - kk["axis_pt"]).dot(yd)) < 1e-3  # noqa: E731
    for axis_, coords, chain in position_plan(hs, xd, yd, skip_y, mode, kk.setdefault("done", {"x": [], "y": []})):
        kind = "DistanceX" if axis_ == "x" else "DistanceY"
        side = "horizontal" if axis_ == "x" else "vertical"
        k_ = 0 if axis_ == "x" else 1
        base_idx, base_pos = datum_vertex(doc, view, kk, shape, "left" if axis_ == "x" else "low")
        edge = kk["box"][k_]
        pts = [(vmap(h["from"]), h) for _, h in coords]
        if chain:
            pts.sort(key=lambda t: t[0][k_])
        if base_idx is None:
            for _, h in pts:
                warnings.append("could not place a %s position dimension for the hole at (%s): no vertex at the "
                                "outline's edge — add one with an add_dimension op" % (side, an._pt(h["from"])))
                kk.setdefault("missing", []).append("hole at (%s) %s position" % (an._pt(h["from"]), side))
            continue
        vals = []
        for i, (p, _) in enumerate(pts):
            vals.append(p[k_] - (pts[i - 1][0][k_] if chain and i else edge))
        col_x = y_column(kk, sc, "", w=max(text_w(_w(v)) for v in vals)) if chain and axis_ == "y" else None
        prev = None                  # (vertex index, view coordinate, view point) of the last hole dimensioned
        for (ex, ey), h in pts:
            cv = (ex, ey)[k_]
            if chain and prev is not None:
                base, exp, bp = prev[0], cv - prev[1], prev[2]
            else:
                base, exp, bp = base_idx, cv - edge, base_pos
            if exp < 1e-3:           # the centre lies on the edge line: no zero-length dimension
                prev = None
                continue
            if axis_ == "x":
                at = ((ex + bp[0]) / 2 * sc, kk["ylow"] * sc - off(kk["x"]))
            else:
                at = (col_x if chain else y_column(kk, sc, _w(exp)), (ey + bp[1]) / 2 * sc)
            d = place_checked_dim(doc, page, view, kind, base, exp, (ex, ey), at)
            what = "hole %s (%s)" % ("pitch" if chain and prev is not None else "position", side)
            if d is None:
                warnings.append("could not place a %s position dimension for the hole at (%s) — it has no location "
                                "on the sheet; add one with an add_dimension op" % (side, an._pt(h["from"])))
                kk.setdefault("missing", []).append("hole at (%s) %s position" % (an._pt(h["from"]), side))
                prev = None
                continue
            if not chain:
                kk[axis_] += 1
            out_dims.append((d, exp, what))
            lay_add(kk, d, axis_, (bp[0] * sc, bp[1] * sc), (ex * sc, ey * sc), _w(exp))
            subs = _subs(d)
            prev = (int(subs[1][6:]), cv, (ex, ey)) if len(subs) > 1 and subs[1].startswith("Vertex") else None
        if chain:
            kk[axis_] += 1


def drawn_edges(view):
    """The edges the view draws from the model (visible, then hidden) — not
    the cosmetic lines and circles added to it."""
    edges = view_edges(view)
    try:
        n = len(edges) - len(view.CosmeticEdges) - len(getattr(view, "CenterLines", []) or [])
    except Exception:  # noqa: BLE001
        n = len(edges)
    return [e for _, e in edges[:max(n, 0)]]


def on_drawn(edges, pos, tol=1e-3):
    """The view point pos lies on one of the drawn edges."""
    v = Part.Vertex(Vector(pos[0], pos[1], 0))
    for e in edges:
        try:
            if e.distToShape(v)[0] < tol:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def pocket_dims(doc, page, part, shape, view, groups, views_by_name, k_by_view, out_dims, warnings, callouts):
    """Pockets, slots, steps and cut-outs in the view that looks at their
    floors (pocket_plan groups): each one's location from the outline's left /
    bottom extremes (the hole datums), chained with its size on the group's
    representative when the size is drawn as dimensions; 'K× R3' on a corner
    arc; the depth as a dimension in the view that shows the floor edge-on
    (from an open side, or with hidden lines), else in the group's callout
    ('2× 40 × 20 ↧3', or '↧3'). Every dimension is anchored on drawn edges of
    the pocket and checked against the model; the callouts sit on a corner
    arc whose radius is checked, and the walls they size must be drawn where
    the model has them."""
    kk = k_by_view[view.Name]
    if not kk.get("n_geom") or "vmap" not in kk:
        return
    sc = view.Scale
    vmap = kk["vmap"]
    xd, yd = view_axes(view)
    xmin, ymin, xmax, ymax = kk["box"]
    off = lambda k_: 8 + DIM_STEP * k_  # noqa: E731 — paper mm
    drawn = drawn_edges(view)
    done = kk.setdefault("pdone", {"x": [], "y": []})

    def ends(p, w):
        """(lo point, hi point) in view coordinates of pocket p along the view axis w."""
        a = _model_axis(w)
        pts = sorted((vmap(p["ext"]["-" + a]), vmap(p["ext"]["+" + a])), key=lambda q: q[0] if w is xd else q[1])
        return pts

    def miss(p, what):
        warnings.append("could not place the %s of the %s %s at (%s) — add it with an add_dimension op" % (
            what, POCKET_WORD[p["kind"]].lower(), "%s × %s" % (_w(p["box"][_model_axis(xd)][1] - p["box"][_model_axis(
                xd)][0]), _w(p["box"][_model_axis(yd)][1] - p["box"][_model_axis(yd)][0])),
            an._pt(p["ext"]["-" + _model_axis(xd)])))
        kk.setdefault("missing", []).append("%s %s" % (POCKET_WORD[p["kind"]].lower(), what))

    def chain(p, g, k_):
        """Location (from the datum) and — on the representative — size of p along view axis k_ (0 = x)."""
        w = xd if k_ == 0 else yd
        lo_pt, hi_pt = ends(p, w)
        lo, hi = lo_pt[k_], hi_pt[k_]
        edge, far = (xmin, xmax) if k_ == 0 else (ymin, ymax)
        side = "horizontal" if k_ == 0 else "vertical"
        kind = "DistanceX" if k_ == 0 else "DistanceY"
        todo = []
        if lo - edge > 1e-3 and not any(abs(lo - q) < 1e-3 for q in done["xy"[k_]]):
            todo.append(("location", lo - edge))
        if g["linear"] and p is g["rep"] and not (lo - edge < 1e-3 and far - hi < 1e-3):
            todo.append(("length" if k_ == 0 else "width", hi - lo))
        if not todo:
            return
        if not (on_drawn(drawn, lo_pt) and on_drawn(drawn, hi_pt)):
            miss(p, "%s dimensions (its outline is not drawn where the model has it)" % side)
            return
        texts = [_w(v) for _, v in todo]
        col_x = y_column(kk, sc, "", w=max(text_w(t) for t in texts)) if k_ == 1 else None
        y_row = kk["ylow"] * sc - off(kk["x"]) if k_ == 0 else None
        base_idx, base_pos = datum_vertex(doc, view, kk, shape, "left" if k_ == 0 else "low")
        for what, exp in todo:
            if what == "location":
                if base_idx is None:
                    miss(p, "%s location (no vertex on the outline's edge)" % side)
                    continue
                b_idx, b_pos, tgt = base_idx, base_pos, lo_pt
            else:
                b_idx, _ = place_vertex(doc, view, lo_pt)
                b_pos, tgt = lo_pt, hi_pt
                if b_idx is None:
                    miss(p, side + " size")
                    continue
            at = (((b_pos[0] + tgt[0]) / 2 * sc, y_row) if k_ == 0 else (col_x, (b_pos[1] + tgt[1]) / 2 * sc))
            d = place_checked_dim(doc, page, view, kind, b_idx, exp, tgt, at)
            if d is None:
                miss(p, "%s %s" % (side, what))
                continue
            label = "%s %s %s" % (POCKET_WORD[p["kind"]].lower(), what, "(%s)" % side if what == "location" else "")
            out_dims.append((d, exp, label.strip()))
            lay_add(kk, d, "xy"[k_], (b_pos[0] * sc, b_pos[1] * sc), (tgt[0] * sc, tgt[1] * sc), _w(exp))
            if what == "location":
                done["xy"[k_]].append(lo)
        if k_ == 0:
            kk["x"] += 1

    def callout_on_arc(p, txt, avoid=None, check=None):
        """A leader with txt on one of p's corner arcs (Radius dimension, its radius checked)."""
        arcs = [c for c in p["corners"] if c["edge"] is not None and c["edge"] != avoid]
        if not arcs:
            arcs = [c for c in p["corners"] if c["edge"] is not None]
        if not arcs:
            return None
        c = max(arcs, key=lambda c: sum(vmap(c["center"])) if c["center"] is not None else -1e18)
        d = add_dim(doc, page, view, "Radius", refs3d=[(part, "Edge%d" % (c["edge"] + 1))], text=txt)
        d.X, d.Y = kk["w"] / 2 + 14 + kk.get("callout_dx", 0), kk["h"] / 2 + 6 + 8 * kk["callouts"]
        kk["callouts"] += 1
        out_dims.append((d, c["r"], "%s callout %s" % (POCKET_WORD[p["kind"]].lower(), txt), check))
        callouts.append((txt, view.Label, False))
        return c["edge"]

    def walls_drawn(p):
        """The rep's outline is drawn at the size the callout states."""
        def check():
            for w in (xd, yd):
                for q in ends(p, w):
                    if not on_drawn(drawn_edges(view), q):
                        return "%s callout: the view does not show the %s outline where the model has it" % (
                            POCKET_WORD[p["kind"]].lower(), POCKET_WORD[p["kind"]].lower())
            return None
        return check

    def depth_dim(g):
        """Depth of the group's representative in the view that shows its floor edge-on."""
        p = g["rep"]
        sv = views_by_name.get(g["depth_view"])
        ks = k_by_view.get(sv.Name) if sv is not None else None
        if sv is None or not ks or "vmap" not in ks:
            return False
        smap = ks["vmap"]
        dn = Vector(view.Direction)
        dn.normalize()
        sx, sy = view_axes(sv)
        ax = next((a for a in (_model_axis(sx), _model_axis(sy)) if a in p["box"]), None)
        if ax is None:
            return False
        sdrawn = drawn_edges(sv)
        for q in (p["ext"]["-" + ax], p["ext"]["+" + ax], (p["ext"]["-" + ax] + p["ext"]["+" + ax]) * 0.5):
            top_pt = smap(q + dn * (p["rim"] - q.dot(dn)))
            flo_pt = smap(q + dn * (p["level"] - q.dot(dn)))
            if not (on_drawn(sdrawn, top_pt) and on_drawn(sdrawn, flo_pt)):
                continue
            vert = abs(top_pt[0] - flo_pt[0]) < 1e-3
            ssc = sv.Scale
            b_idx, _ = place_vertex(doc, sv, top_pt)
            if b_idx is None:
                continue
            txt = _w(p["depth"])
            if vert:
                at = (y_column(ks, ssc, txt), (top_pt[1] + flo_pt[1]) / 2 * ssc)
            else:
                at = ((top_pt[0] + flo_pt[0]) / 2 * ssc, ks["ylow"] * ssc - off(ks["x"]))
            d = place_checked_dim(doc, page, sv, "DistanceY" if vert else "DistanceX", b_idx, p["depth"], flo_pt, at)
            if d is None:
                continue
            if not vert:
                ks["x"] += 1
            out_dims.append((d, p["depth"], "%s depth" % POCKET_WORD[p["kind"]].lower()))
            lay_add(ks, d, "y" if vert else "x", (top_pt[0] * ssc, top_pt[1] * ssc), (flo_pt[0] * ssc,
                                                                                          flo_pt[1] * ssc), txt)
            return True
        return False

    for g in groups:
        for p in g["pockets"]:
            chain(p, g, 0)
            chain(p, g, 1)
        rep = g["rep"]
        callout, used = g["callout"], None
        if g["depth_view"] and not depth_dim(g):
            # the planned view does not show the floor after all: state the depth in the callout
            dt = "↧%s" % _w(rep["depth"])
            callout = "%s %s" % (callout, dt) if callout else dt
            if not [c for c in rep["corners"] if c["edge"] is not None]:
                warnings.append("the depth %s of the %s %s is not shown — no view shows its floor and it has no "
                                "corner arc for a callout; add hidden=true or an add_note op" % (
                                    _w(rep["depth"]), POCKET_WORD[rep["kind"]].lower(), g["size"]))
        if callout:
            used = callout_on_arc(rep, callout, check=walls_drawn(rep) if len(g["pockets"]) > 1 else None)
        for r, k in g["corners"]:
            arcs = [c for c in rep["corners"] if c["r"] == r and c["edge"] is not None]
            if arcs:
                callout_on_arc(dict(rep, corners=arcs), corner_text(r, k), avoid=used)


TRACE = []


def place_checked_dim(doc, page, view, kind, base_idx, expected, pos, at):
    """Dimension from a view vertex to the projected model point pos, verified:
    the marker vertex must land at pos (place_vertex) and the dimension must
    measure the model's value, else nothing is left behind."""
    idx, tag = place_vertex(doc, view, pos)
    if idx is None:
        TRACE.append("%s: no vertex lands at (%.3f, %.3f)" % (kind, pos[0], pos[1]))
        return None
    d = add_dim(doc, page, view, kind, ["Vertex%d" % base_idx, "Vertex%d" % idx], x=at[0], y=at[1])
    doc.recompute()
    pump(2)
    val = _raw(d)
    if val is not None and abs(val - expected) < 1e-3:
        return d
    TRACE.append("%s at (%.3f, %.3f) val=%s exp=%.3f" % (kind, pos[0], pos[1], val, expected))
    doc.removeObject(d.Name)
    if tag:
        view.removeCosmeticVertex(tag)
    doc.recompute()
    return None


def view_lines(view):
    """Visible straight edges of a view, (horizontal, vertical): lists of
    (index, coordinate, lo, hi) in view coordinates. TechDraw lists hidden
    edges after the visible ones (and cosmetic ones after those)."""
    edges = view_edges(view)
    try:                                     # getVisibleEdges counts the cosmetic edges too
        n_vis = len(view.getVisibleEdges()) - len(view.CosmeticEdges)
    except Exception:  # noqa: BLE001
        n_vis = len(edges)
    horiz, vert = [], []
    for i, e in edges[:n_vis]:
        if len(e.Vertexes) < 2:
            continue
        a, b = e.Vertexes[0].Point, e.Vertexes[-1].Point
        m = e.valueAt((e.FirstParameter + e.LastParameter) / 2)
        if abs(m.x - (a.x + b.x) / 2) > 1e-4 or abs(m.y - (a.y + b.y) / 2) > 1e-4:
            continue
        if abs(a.y - b.y) < 1e-4 and abs(a.x - b.x) > 1e-4:
            horiz.append((i, (a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
        elif abs(a.x - b.x) < 1e-4 and abs(a.y - b.y) > 1e-4:
            vert.append((i, (a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
    return horiz, vert


def lay_add(kk, d, dirn, a, b, txt, fixed=False):
    """Remember a generated linear dimension for declutter: anchors a, b and
    the text (paper mm from the view centre). fixed: its text stays where it
    is (a turned step's Ø) — the others keep clear of it."""
    kk.setdefault("lay", []).append({"d": d, "dir": dirn, "a": a, "b": b, "w": text_w(txt), "fixed": fixed})


def turned_dims(doc, page, view, out_dims, shape, axis, apt, steps, kk, warnings, callouts, section=False,
                bores=()):
    """Side view of a turned part (axis horizontal), or its section A-A: a Ø
    dimension on EACH outer step, anchored on that step's own outline (so two
    steps of the same diameter each get theirs and a fit or thread goes on the
    step that has it): plain ⌀, ⌀ with the ISO 286 fit and its deviations, or
    the thread callout plus the thread length (no ISO 6410 root line: TechDraw
    1.1 draws every Python-made cosmetic line dashed, which would read as a
    hidden edge). In a section each bore (bores: turned_bores_info) gets its
    own Ø the same way, anchored on its bore lines. Texts sit on the step when
    they fit — in a section only inside a bore, never on the hatching — else
    stacked above the part (turned_label_layout), never across another step's
    outline. Baseline lengths from the left end face to each shoulder (in a
    section the internal ones too) go below. Returns the number of rows used
    below."""
    sc = view.Scale
    horiz, vert = view_lines(view)
    vmap = view_map(view, shape)
    x0, yax = vmap(apt)
    xdir = 1.0 if vmap(apt + axis)[0] >= x0 else -1.0

    def X(t):
        return x0 + xdir * t
    rmax = max([abs(h[1] - yax) for h in horiz] or [0])
    steps = [dict(st, x0=X(st["t0"]), x1=X(st["t1"])) for st in list(steps) + list(bores if section else ())]
    layout = turned_label_layout(steps, sc, section)
    kk["callouts"] = 1 + max([o["level"] for o in layout if not o["inside"]] or [-1])   # hole callouts go above
    for st, o in zip(steps, layout):
        r = st["d"] / 2
        xl = o["x_line"] / sc
        lo, hi = sorted((st["x0"], st["x1"]))
        what = "bore" if st.get("bore") else "step"

        def on(y, xl=xl):
            return any(abs(h[1] - y) < 1e-4 and h[2] - 1e-6 <= xl <= h[3] + 1e-6 for h in horiz)
        if not (on(yax + r) and on(yax - r)):
            warnings.append("Ø%s %s (%s mm along the axis): its outline is not drawn at the %s's middle — not "
                            "dimensioned" % (_w(st["d"]), what, _w(st["t0"]), what))
            continue
        made = []
        vt, tt = place_vertex(doc, view, (xl, yax + r))
        vb, tb = place_vertex(doc, view, (xl, yax - r)) if vt is not None else (None, None)
        made = [t for t in (tt, tb) if t]
        if vt is None or vb is None:
            for t in made:
                view.removeCosmeticVertex(t)
            warnings.append("could not place the Ø%s dimension of the %s at %s mm" % (_w(st["d"]), what,
                                                                                     _w(st["t0"])))
            continue
        if st["thread"] or st.get("tapped"):
            kw = {"text": st["text"]}
        elif st["fit"]:
            kw = {"fmt_spec": "⌀%.2w" + st["text"][len("⌀%s" % _w(st["d"])):]}
        else:
            kw = {"fmt_spec": "⌀%.2w"}
        d = add_dim(doc, page, view, "DistanceY", ["Vertex%d" % vt, "Vertex%d" % vb],
                    x=o["x_line"] + o["w"] / 2, y=yax * sc + o["Y"], **kw)

        def anchored(d=d, lo=lo, hi=hi, r=r, txt=st["text"], what=what):
            pts = []
            for sub in _subs(d):
                try:
                    p = view.getVertexByIndex(int(sub[6:])).Point
                except Exception:  # noqa: BLE001
                    return "%s: its anchors could not be read back" % txt
                pts.append(p)
            ys = sorted(p.y - yax for p in pts)
            if len(pts) != 2 or abs(ys[0] + r) > 1e-3 or abs(ys[1] - r) > 1e-3 or not all(
                    lo - 1e-3 <= p.x <= hi + 1e-3 and on(p.y, p.x) for p in pts):
                return "%s: the dimension is not anchored on the Ø%s %s it names" % (txt, _w(2 * r), what)
            return None
        out_dims.append((d, st["d"], "Ø%s %s" % (_w(st["d"]), what), anchored))
        top = max((yax + r) * sc + 4, yax * sc + o["Y"])        # its line runs up to a text stacked above
        lay_add(kk, d, "y", (o["x_line"], top), (o["x_line"], (yax - r) * sc - 6), st["text"], fixed=True)
        if st["fit"] or st["thread"] or st.get("tapped"):
            callouts.append((st["text"], view.Label, bool(st["thread"] or st.get("tapped"))))
    xs = sorted(set(round(v[1], 4) for v in vert))
    if xs:                                   # centreline (ISO 128 long-dash-dot)
        try:
            view.makeCosmeticLine(Vector(min(xs) - 3 / sc, yax, 0), Vector(max(xs) + 3 / sc, yax, 0), 4)
        except Exception:  # noqa: BLE001
            pass
    rows = []                                # x-ranges taken in each row below the part
    if len(xs) >= 2:
        zs = turned_steps(shape, axis)
        if len(zs) >= 2:
            x0v = min(xs)
            offs = [z - zs[0] for z in zs]
            if not all(any(abs(x0v + o - x) < 1e-3 for x in xs) for o in offs):
                offs = [zs[-1] - z for z in reversed(zs)]       # axis runs right-to-left
            left = min((v for v in vert if abs(v[1] - x0v) < 1e-3), key=lambda v: v[2])
            for o in offs[1:]:
                cand = [v for v in vert if abs(v[1] - (x0v + o)) < 1e-3]
                if not cand:
                    continue
                e = max(cand, key=lambda v: v[3] - v[2])
                yrow = (yax - rmax) * sc - 8 - DIM_STEP * len(rows)
                d = add_dim(doc, page, view, "DistanceX", ["Edge%d" % left[0], "Edge%d" % e[0]],
                            x=(x0v + o / 2) * sc, y=yrow)
                out_dims.append((d, o, "length from the end face"))
                lay_add(kk, d, "x", (x0v * sc, left[2] * sc), ((x0v + o) * sc, e[2] * sc), _w(o))
                rows.append([(x0v * sc, (x0v + o) * sc)])
    outer = [st for st in steps if not st.get("bore")]
    if section and outer:                    # bore depths from the end face each bore opens to
        t_lo, t_hi = min(st["t0"] for st in outer), max(st["t1"] for st in outer)
        zrel = [z - apt.dot(axis) for z in turned_steps(shape, axis)]
        for t_face, t_in, r in bore_depths([st for st in steps if st.get("bore")], t_lo, t_hi, zrel):
            xf, xi = X(t_face), X(t_in)
            face = [(v[1], y) for v in vert if abs(v[1] - xf) < 1e-3 for y in (v[2], v[3])]
            if not face or not any(abs(h[1] - (yax + r)) < 1e-4 and h[2] - 1e-6 <= xi <= h[3] + 1e-6 for h in horiz):
                warnings.append("could not place the depth of the Ø%s bore (%s from the end face)" % (
                    _w(2 * r), _w(abs(t_in - t_face))))
                continue
            base = min(face, key=lambda q: abs(q[1] - (yax + r)))
            L = abs(t_in - t_face)
            xa, xb = sorted((xf, xi))
            span = (xa * sc - 3, xb * sc + 3)
            row = next((k for k, used in enumerate(rows) if not any(u[0] < span[1] and span[0] < u[1] for u in used)),
                       len(rows))
            vb_, tb_ = place_vertex(doc, view, base)
            d = None
            if vb_ is not None:
                d = place_checked_dim(doc, page, view, "DistanceX", vb_, L, (xi, yax + r),
                                      ((xa + xb) / 2 * sc, (yax - rmax) * sc - 8 - DIM_STEP * row))
            if d is None:
                if tb_:
                    view.removeCosmeticVertex(tb_)
                warnings.append("could not place the depth of the Ø%s bore (%s from the end face)" % (
                    _w(2 * r), _w(L)))
                continue
            out_dims.append((d, L, "Ø%s bore depth" % _w(2 * r)))
            lay_add(kk, d, "x", (base[0] * sc, base[1] * sc), (xi * sc, (yax + r) * sc), _w(L))
            if row == len(rows):
                rows.append([])
            rows[row].append(span)
    done = []
    for st in steps:
        th = st["thread"]
        if not th or any(th is t for t in done):
            continue
        done.append(th)
        r = st["d"] / 2
        xa, xb = sorted((X(th["t0"]), X(th["t1"])))
        L = abs(th["t1"] - th["t0"])
        span = (xa * sc - 3, xb * sc + 3)
        row = next((k for k, used in enumerate(rows) if not any(u[0] < span[1] and span[0] < u[1] for u in used)),
                   len(rows))
        va, ta = place_vertex(doc, view, (xa, yax - r))
        d = None
        if va is not None:
            d = place_checked_dim(doc, page, view, "DistanceX", va, L, (xb, yax - r),
                                  ((xa + xb) / 2 * sc, (yax - rmax) * sc - 8 - DIM_STEP * row))
        if d is None:
            if ta:
                view.removeCosmeticVertex(ta)
            warnings.append("could not place the length dimension of thread %s" % thread_text(th))
        else:
            out_dims.append((d, L, "thread length"))
            lay_add(kk, d, "x", (xa * sc, (yax - r) * sc), (xb * sc, (yax - r) * sc), _w(L))
            if row == len(rows):
                rows.append([])
            rows[row].append(span)
    return len(rows)


# ------------------------------------------------------------------ notes

def bend_table(sheet):
    """'BENDS (K 0.33): B1 90° UP R1.5 · B2 90° DOWN R1.5' from FcSheet data."""
    bends = sheet.get("bends") or []
    k = sheet.get("k_factor")
    items = []
    for i, b in enumerate(bends, 1):
        try:
            items.append("B%s %s° %s R%s" % (b.get("n", i), _w(float(b.get("angle", 90))),
                                             "UP" if b.get("up", True) else "DOWN",
                                             _w(float(b.get("radius", sheet.get("radius") or 0)))))
        except (TypeError, ValueError):
            items.append("B%s %s" % (b.get("n", i), b))
    return "BENDS%s: %s" % (" (K %s)" % _w(float(k)) if isinstance(k, (int, float)) else "", " · ".join(items))


def sheet_notes(sheet):
    """The NOTES lines of a sheet-metal part (FcSheet): thickness, the bend table, the flat pattern."""
    lines = []
    t = sheet.get("thickness")
    if isinstance(t, (int, float)):
        lines.append("SHEET THICKNESS %s mm" % _w(float(t)))
    if sheet.get("bends"):
        lines.append(bend_table(sheet))
    fs = sheet.get("flat_size")
    try:
        lines.append("FLAT PATTERN: SEE DXF (%s × %s mm)" % (_w(float(fs[0])), _w(float(fs[1]))))
    except (TypeError, ValueError, IndexError):
        lines.append("FLAT PATTERN: SEE DXF")
    return lines


def notes_for(spec, project, part, threaded=False, chamfer=0.0, comp=0.0, threads=(), sheet=None):
    """Generated notes (capitals, their own style) then the user's notes exactly
    as written — "k6" is a shaft fit, "K6" a hole fit, "mm" is not "MM".
    threads: [(FcThreads record, drawn on a view)] → one THREAD line each;
    sheet: FcSheet data → thickness, bend table and flat pattern lines."""
    proc = (spec.get("process") or project.get("process") or "").lower()
    mat = spec.get("material") or (project.get("material") or {}).get("name", "")
    lines = []
    if mat:
        lines.append("MATERIAL: %s" % mat)
    gt = (project.get("drawing") or {}).get("general_tolerance", "ISO 2768-m")
    lines.append("GENERAL TOLERANCES: %s" % gt)
    lines.append("DIMENSIONS IN mm. DO NOT SCALE.")
    if sheet:
        lines += sheet_notes(sheet)
    if proc in ("cnc", "turning", "lathe"):
        # one edge note, matching the model: its chamfers, or break the sharp edges
        if chamfer is None:
            lines.append("CHAMFERS AS SHOWN")
        elif isinstance(chamfer, str):
            lines.append("CHAMFERS AS SHOWN: %s" % chamfer)
        elif chamfer > 0:
            lines.append("CHAMFERS %s × 45° UNLESS STATED" % _w(chamfer))
        else:
            lines.append("BREAK SHARP EDGES 0.2–0.5 × 45°")
        lines.append("SURFACE ROUGHNESS Ra 3.2 UNLESS STATED")
    if proc in ("fdm", "sla"):
        if comp and comp > 0:
            lines.append("HOLES MODELLED +%s mm FOR PRINTING — PRINT AS MODELLED, NO FURTHER HOLE COMPENSATION"
                         % _w(comp))
        else:
            lines.append("PRINT AS MODELLED")
    if threaded:
        lines.append("THREADS: ISO METRIC COARSE, CLASS AS STATED")
    if spec.get("finish"):
        lines.append("FINISH: %s" % spec["finish"])
    for t, shown in threads:
        lines.append("THREAD %s, LENGTH %s%s" % (thread_text(t), _w(float(t.get("length") or 0)),
                                                 " (SEE VIEW)" if shown else ""))
    for n in spec.get("notes") or []:
        lines.append(str(n))
    return lines


def add_caption(doc, page, name, text, x, y, size=CAPTION_SIZE):
    """A view caption ('SECTION A-A') centred at x, y (page mm)."""
    a = doc.addObject("TechDraw::DrawViewAnnotation", name)
    page.addView(a)
    a.Text = [text]
    a.TextSize = size
    a.X, a.Y = x, y
    return a


def add_notes(doc, page, block):
    """The planned notes block (wrapped, NOTE_SIZE text) at its planned place."""
    a = doc.addObject("TechDraw::DrawViewAnnotation", page.Name + "_Notes")
    page.addView(a)
    a.Text = block["text"]
    a.TextSize = NOTE_SIZE
    a.X, a.Y = block["x"], block["y"]
    return a


# ---------------------------------------------------------------- export

def export(page, base, kinds=("pdf", "svg", "dxf", "png")):
    import TechDraw
    import TechDrawGui
    page.ViewObject.doubleClicked()
    pump(25)
    files = {}
    if "pdf" in kinds:
        TechDrawGui.exportPageAsPdf(page, base + ".pdf")
        files["pdf"] = base + ".pdf"
    tmp = None
    if "svg" in kinds:
        svg = base + ".svg"
    else:
        tmp = tempfile.mkdtemp(prefix="fcmcp-svg-")
        svg = os.path.join(tmp, "page.svg")
    TechDrawGui.exportPageAsSvg(page, svg)
    if "svg" in kinds:
        files["svg"] = svg
    if "dxf" in kinds:
        try:
            TechDraw.writeDXFPage(page, base + ".dxf")
            files["dxf"] = base + ".dxf"
        except Exception:  # noqa: BLE001
            pass
    if "png" in kinds:
        r = QtSvg.QSvgRenderer(svg)
        sz = r.defaultSize()
        w = 1400
        img = QtGui.QImage(w, int(w * sz.height() / max(sz.width(), 1)), QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.white)
        pa = QtGui.QPainter(img)
        r.render(pa)
        pa.end()
        img.save(base + ".png")
        files["png"] = base + ".png"
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return files


# --------------------------------------------------------------- page ops

OP_NAMES = ("add_dimension", "add_hole_callout", "add_note", "append_note", "set_title", "move", "remove")
DIM_TYPES = ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter")
_VREF = re.compile(r"^(.+)\.(Vertex|Edge)(\d+)$")


def page_views(page):
    out = {}
    for v in page.Views:
        items = list(v.Views) if v.TypeId == "TechDraw::DrawProjGroup" else [v]
        for x in items:
            if x.TypeId in PART_VIEWS:
                out[x.Label.lower()] = x
    return out


def page_dims(page, view=None):
    out = []
    for d in page.Views:
        if d.TypeId != "TechDraw::DrawViewDimension":
            continue
        refs = d.References2D or []
        if view is None or (refs and refs[0][0] == view):
            out.append(d)
    return out


def _subs(d):
    out = []
    for o, s in d.References2D or []:
        out += list(s) if isinstance(s, (list, tuple)) else [s]
    return [s for s in out if s]


def _find_obj(doc, page, name):
    if not name:
        raise DrawingError("this op needs name (a dimension, note or view as drawing read lists it, e.g. Dim008)")
    on_page = [x for x in page.Views]
    o = next((x for x in on_page if x.Name == str(name)), None) or \
        next((x for x in on_page if x.Label == str(name)), None)
    if o is None:
        raise DrawingError("nothing named %r on the page — use a name from drawing read (%s)" % (
            name, ", ".join(x.Name for x in page.Views if x.TypeId.startswith("TechDraw::DrawView"))[:300]))
    return o


def _view_of(views, op, refs):
    want = op.get("view")
    if want:
        v = views.get(str(want).lower())
        if v is None:
            raise DrawingError("no view %r on the page (views: %s)" % (want, ", ".join(v_.Label for v_ in views.values())))
        return v
    for r in refs:
        m = _VREF.match(str(r).strip()) if isinstance(r, str) else None
        if m and m.group(1).lower() in views:
            return views[m.group(1).lower()]
    ortho = [v for v in views.values() if not an._axis_name(an._canon(Vector(v.Direction))).startswith("(")]
    for r in refs if ortho else []:      # a hole: the view that looks along its axis
        try:
            e = an.resolve(ortho[0].Document, r)
        except Exception:  # noqa: BLE001
            continue
        if e.kind == "holes":
            for v in ortho:
                d = Vector(v.Direction)
                d.normalize()
                if abs(abs(e.holes[0]["axis"].dot(d)) - 1) < 1e-6:
                    return v
    raise DrawingError("say which view the dimension goes on: view=%s" % " | ".join(v.Label for v in views.values()))


def _outline(view, shape, vmap):
    """Bounding box of the drawn part in view coordinates (model units)."""
    us, vs = [], []
    for e in shape.Edges:
        try:
            pts = e.discretize(8)
        except Exception:  # noqa: BLE001
            pts = [v.Point for v in e.Vertexes]
        for p in pts:
            u, v = vmap(p)
            us.append(u)
            vs.append(v)
    if not us:
        verts = view_vertices(view)
        us, vs = [t[1] for t in verts] or [0], [t[2] for t in verts] or [0]
    return min(us), min(vs), max(us), max(vs)


def _default_dim_pos(page, view, kind, pa, pb, box):
    """Outside the outline, stacked after the dimensions already there: below
    for horizontal, right for vertical (paper mm from the view centre)."""
    sc = view.Scale
    xmin, ymin, xmax, ymax = box
    dims = page_dims(page, view)
    horizontal = kind == "DistanceX" or (kind == "Distance" and abs(pa[0] - pb[0]) >= abs(pa[1] - pb[1]))
    if horizontal:
        below = [d.Y.Value for d in dims if d.Type in ("DistanceX", "Distance") and d.Y.Value < ymin * sc]
        y = min([ymin * sc - 8] + [b - DIM_STEP for b in below])
        return (pa[0] + pb[0]) / 2 * sc, y
    right = [d.X.Value for d in dims if d.Type in ("DistanceY", "Distance") and d.X.Value > xmax * sc]
    x = max([xmax * sc + 8] + [r + 10 for r in right])
    return x, (pa[1] + pb[1]) / 2 * sc


def _resolve_end(doc, views, view, ref):
    """One end of a distance: a view vertex (Top.Vertex16), a model point
    (hole, vertex, frame, [x,y,z]) or a planar face."""
    if ref is None:
        raise DrawingError("a distance needs both ends: a and b")
    m = _VREF.match(ref.strip()) if isinstance(ref, str) else None
    if m and m.group(1).lower() in views:
        v = views[m.group(1).lower()]
        if v is not view:
            raise DrawingError("%s is in the %s view but the dimension goes on %s — use vertices of one view" % (
                ref, v.Label, view.Label))
        if m.group(2) != "Vertex":
            raise DrawingError("%s is a view edge — a distance needs vertices, holes, points or faces" % ref)
        n = int(m.group(3))
        verts = view_vertices(view)
        if n >= len(verts):
            raise DrawingError("the %s view has no Vertex%d (it has %d) — use one that drawing read lists" % (
                view.Label, n, len(verts)))
        return {"label": ref, "idx": n, "pos": (verts[n][1], verts[n][2])}
    e = an.resolve(doc, ref)
    p3 = e.as_point()
    if p3 is None and e.kind == "holes":
        return {"label": ref, "p3": e.holes[0]["from"], "axis": e.holes[0]["axis"]}     # any point of its axis
    if p3 is not None:
        return {"label": ref, "p3": p3}
    if e.kind == "face" and e.shape.Surface.__class__.__name__ == "Plane":
        return {"label": ref, "face": e.shape}
    raise DrawingError("%s is a %s — dimension ends must be holes, points, vertices, frames, planar faces or view "
                       "vertices like Top.Vertex16" % (ref, e.kind))


def _face_pos(face, other, vmap):
    """The point of a planar face to dimension to: nearest the other end, else its centre."""
    if other is not None and "p3" in other:
        q = face.distToShape(Part.Vertex(other["p3"]))[1][0][0]
        return vmap(q), q
    if other is not None and "pos" in other:
        pts = []
        for e in face.Edges:
            pts += [vmap(p) for p in e.discretize(12)]
        (x0, y0), (x1, y1) = max(((a, b) for a in pts for b in pts),
                                 key=lambda ab: (ab[0][0] - ab[1][0]) ** 2 + (ab[0][1] - ab[1][1]) ** 2)
        L2 = (x1 - x0) ** 2 + (y1 - y0) ** 2
        flat = L2 > 1e-12 and all(abs((x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0)) / math.sqrt(L2) < 1e-4
                                  for p in pts)
        if flat:                                   # seen edge-on: the foot of the perpendicular
            px, py = other["pos"]
            t = max(0.0, min(1.0, ((px - x0) * (x1 - x0) + (py - y0) * (y1 - y0)) / L2))
            return (x0 + t * (x1 - x0), y0 + t * (y1 - y0)), None
    q = face.CenterOfMass
    return vmap(q), q


def _op_xy(op):
    """The op's x / y as floats (None when not given), checked before anything is made."""
    out = []
    for k in ("x", "y"):
        v = op.get(k)
        try:
            out.append(None if v is None else float(v))
        except (TypeError, ValueError):
            raise DrawingError("%s %r is not a number — x/y are paper mm from the view centre" % (k, v))
    return out


def _dimension(doc, page, views, op):
    kind = op.get("op")
    typ = op.get("type") or ("Diameter" if kind == "add_hole_callout" else "Distance")
    if typ not in DIM_TYPES:
        raise DrawingError("type %r not supported — use %s" % (typ, ", ".join(DIM_TYPES)))
    tol = _parse_tolerance(op)               # validated before anything is made: a failing op leaves nothing
    ox, oy = _op_xy(op)
    refs = [op[k] for k in ("a", "b") if op.get(k) is not None]
    if not refs:
        raise DrawingError("%s needs a (and b for a distance), e.g. a='Plate.holes(d=4.5)[0]', "
                           "b='Plate.face(-X)' or a='Top.Vertex16'" % kind)
    view = _view_of(views, op, refs)
    shape = view_shape(view)
    vmap = view_map(view, shape)
    box = _outline(view, shape, vmap)
    if kind == "add_hole_callout" or typ in ("Radius", "Diameter") or len(refs) == 1:
        return _circle_dim(doc, page, views, view, op, typ if typ in ("Radius", "Diameter") else "Diameter",
                           refs[0], box, callout=kind == "add_hole_callout", tol=tol, xy=(ox, oy))
    ends = [_resolve_end(doc, views, view, r) for r in refs[:2]]
    for i, e in enumerate(ends):
        if "p3" in e:
            e["pos"] = vmap(e["p3"])
    for i, e in enumerate(ends):
        if "face" in e:
            other = ends[1 - i]
            if "face" in other and "pos" not in other:
                other = None
            e["pos"], q = _face_pos(e["face"], other, vmap)
            if q is not None:
                e["p3"] = q
    truth, why = _model_distance(view, typ, ends)
    if why and not op.get("text"):
        raise DrawingError(why)
    created = []

    def cleanup(d=None):
        if d is not None:
            doc.removeObject(d.Name)
        for t in created:
            view.removeCosmeticVertex(t)
        doc.recompute()
    for e in ends:
        if "idx" not in e:
            idx, tag = place_vertex(doc, view, e["pos"])
            if idx is None:
                cleanup()
                raise DrawingError("could not place a vertex for %s in the %s view" % (e["label"], view.Label))
            e["idx"] = idx
            if tag:
                created.append(tag)
    (xa, ya), (xb, yb) = ends[0]["pos"], ends[1]["pos"]
    expected = {"DistanceX": abs(xa - xb), "DistanceY": abs(ya - yb)}.get(typ, math.hypot(xa - xb, ya - yb))
    if expected < 1e-6:
        cleanup()
        raise DrawingError("%s and %s coincide in the %s view along %s — use another view or type" % (
            ends[0]["label"], ends[1]["label"], view.Label, typ))
    if truth is not None and abs(truth - expected) > 1e-3 and not op.get("text"):
        cleanup()
        raise DrawingError("%s ↔ %s: the %s view would show %s but the model distance is %s — use a view that "
                           "looks perpendicular to it (or pass text)" % (ends[0]["label"], ends[1]["label"],
                                                                         view.Label, fmt(expected, 3), fmt(truth, 3)))
    if ox is not None or oy is not None:
        dx, dy = _default_dim_pos(page, view, typ, (xa, ya), (xb, yb), box)
        x, y = (dx if ox is None else ox), (dy if oy is None else oy)
        where = "at (%s, %s) — your x/y: paper mm from the %s view centre" % (_w(x), _w(y), view.Label)
    else:
        x, y = _default_dim_pos(page, view, typ, (xa, ya), (xb, yb), box)
        where = "at (%s, %s) mm from the %s view centre, outside the outline (move it with move)" % (
            _w(x), _w(y), view.Label)
    d = None
    try:
        d = add_dim(doc, page, view, typ, ["Vertex%d" % ends[0]["idx"], "Vertex%d" % ends[1]["idx"]], x=x, y=y,
                    text=op.get("text"))
        doc.recompute()
        pump(2)
        val = _raw(d)
        st = list(d.State)
        if val is None or "Invalid" in st or "Error" in st or abs(val - expected) > 1e-3:
            raise DrawingError("TechDraw measured %s but the model says %s for %s ↔ %s in the %s view — dimension not "
                               "added; check the refs and the view" % ("-" if val is None else fmt(val, 3),
                                                                       fmt(expected, 3), ends[0]["label"],
                                                                       ends[1]["label"], view.Label))
        tol_txt = _tolerance(d, tol)
    except BaseException:
        cleanup(d)
        raise
    shown = ""
    if op.get("text"):
        shown = " text '%s' (%s)" % (op["text"], "the model value is %s" % fmt(truth, 3) if truth is not None else
                                     "your text, shown instead of the value")
    return "dimension %s %s %s ↔ %s verified ✓ %s%s%s, %s" % (d.Name, typ, ends[0]["label"], ends[1]["label"],
                                                               fmt(val, 3), tol_txt, shown, where)


def _model_distance(view, typ, ends):
    """(the model value the dimension must show, why it cannot be drawn): with
    model points at both ends, the 3D distance (Distance) or its component
    along the view's horizontal / vertical axis (DistanceX/Y, only in a view
    that looks along a model axis). A segment not parallel to the view plane (an iso view)
    would be shown foreshortened → why says so. (None, None) when an end is a
    view vertex or an edge-on face (the view plane is all that is known)."""
    xd, yd = view_axes(view)
    vd = Vector(view.Direction)
    vd.normalize()
    ortho = bool(_principal(vd)) and abs(xd.dot(vd)) < 1e-6      # looks along a model axis (not an iso view)
    if not all("p3" in e for e in ends):
        if not ortho or not _principal(xd) or not _principal(yd):
            return None, ("the %s view does not look along a model axis, so a dimension from its vertices or faces "
                          "would show a projected length — use an orthographic view (or pass text)" % view.Label)
        return None, None
    dv = ends[1]["p3"] - ends[0]["p3"]
    if any("axis" in e and abs(abs(e["axis"].dot(vd)) - 1) < 1e-6 for e in ends):
        dv = dv - vd * dv.dot(vd)            # a hole seen along its axis: measured at any depth
    if typ == "Distance":
        if abs(dv.dot(vd)) > 1e-6:
            return dv.Length, ("%s ↔ %s is not parallel to the %s view plane: the view would show %s for a model "
                               "distance of %s — use a view that looks perpendicular to it, or DistanceX/DistanceY "
                               "along a model axis (or pass text)" % (
                                   ends[0]["label"], ends[1]["label"], view.Label,
                                   fmt(math.sqrt(max(dv.Length ** 2 - dv.dot(vd) ** 2, 0.0)), 3), fmt(dv.Length, 3)))
        return dv.Length, None
    ax = xd if typ == "DistanceX" else yd
    if not ortho or not _principal(ax):
        return None, ("the %s view does not look along a model axis, so its %s axis is not a model axis and %s "
                      "there would be a projected length — use an orthographic view (Front, Top, …) or pass text" % (
                          view.Label, "horizontal" if typ == "DistanceX" else "vertical", typ))
    return abs(dv.dot(ax)), None


def _parse_tolerance(op):
    """(text, ±v) | (text, over, under) | None, from op['tolerance'] — checked before anything is made."""
    if not op.get("tolerance"):
        return None
    tol = str(op["tolerance"]).strip()
    m = re.match(r"^\+?(-?[\d.]+)\s*/\s*(-?[\d.]+)$", tol)
    try:
        if m:
            return (tol, float(m.group(1)), float(m.group(2)))
        if tol.startswith("±") or tol.startswith("+-"):
            return (tol, float(tol.lstrip("±+-")))
    except ValueError:
        pass
    raise DrawingError("tolerance %r not understood — use '±0.05' or '+0.1/-0.05'" % tol)


def _tolerance(d, tol):
    """Apply a parsed tolerance to dimension d; its text for the op result."""
    if not tol:
        return ""
    if len(tol) == 3:
        d.OverTolerance, d.UnderTolerance = tol[1], tol[2]
    else:
        d.EqualTolerance = True
        d.OverTolerance = tol[1]
    return " %s" % tol[0]


def _circle_dim(doc, page, views, view, op, typ, ref, box, callout, tol=None, xy=(None, None)):
    """Radius / diameter / hole callout on a circle: a hole, a circular model
    edge (Plate.Edge12) or a view edge (Top.Edge5); the value is verified."""
    m = _VREF.match(ref.strip()) if isinstance(ref, str) else None
    count, text, refs2d, refs3d = 1, op.get("text"), None, None
    vdir = Vector(view.Direction)
    vdir.normalize()
    if m and m.group(1).lower() in views:
        if views[m.group(1).lower()] is not view or m.group(2) != "Edge":
            raise DrawingError("%s: use a circular edge of the %s view (e.g. %s.Edge5)" % (ref, view.Label, view.Label))
        e = view.getEdgeByIndex(int(m.group(3)))
        if e is None or e.Curve.__class__.__name__ != "Circle":
            raise DrawingError("%s is not a circle or arc" % ref)
        radius, refs2d = e.Curve.Radius, ["Edge%s" % m.group(3)]
    else:
        ent = an.resolve(doc, ref)
        if ent.kind == "holes":
            h = ent.holes[0]
            shape = an.global_shape(ent.obj)
            idx = _circle_edge(shape, h, outer=callout, keep=section_keep(view))
            if idx is None or abs(abs(h["axis"].dot(vdir)) - 1) > 1e-6:
                look = [v.Label for v in views.values() if abs(abs(h["axis"].dot(Vector(v.Direction).normalize())) - 1)
                        < 1e-6]
                raise DrawingError("the %s view does not look along the hole axis %s — use %s" % (
                    view.Label, h["axis_name"], " or ".join(look) or "a view along the hole axis"))
            radius, refs3d = shape.Edges[idx].Curve.Radius, [(ent.obj, "Edge%d" % (idx + 1))]
            count = len(ent.holes)
            if callout and not text:
                specs = hole_specs(ent.obj)
                first = hole_callout(h, hole_spec_for(h, specs))
                same = [x for x in ent.holes if hole_callout(x, hole_spec_for(x, specs)) == first]
                text = callout_text(same, specs)[0]
        elif ent.kind == "edge" and ent.shape.Curve.__class__.__name__ == "Circle":
            if abs(abs(ent.shape.Curve.Axis.dot(vdir)) - 1) > 1e-6:
                raise DrawingError("%s is not seen as a circle in the %s view" % (ref, view.Label))
            n = re.search(r"Edge(\d+)$", ref)
            radius, refs3d = ent.shape.Curve.Radius, [(ent.obj, "Edge%s" % n.group(1))]
        else:
            raise DrawingError("%s is a %s — a radius, diameter or callout needs a hole (Plate.holes(d=6)[0]), a "
                               "circular model edge (Plate.Edge12) or a view edge (Top.Edge5)" % (ref, ent.kind))
    sc = view.Scale
    xmin, ymin, xmax, ymax = box
    above = [d for d in page_dims(page, view) if d.Type in ("Radius", "Diameter") and d.Y.Value > ymax * sc]
    dx, dy = xmax * sc + 14, ymax * sc + 6 + 8 * len(above)
    x, y = (dx if xy[0] is None else xy[0]), (dy if xy[1] is None else xy[1])
    d = None
    try:
        d = add_dim(doc, page, view, typ, refs2d=refs2d, refs3d=refs3d, x=x, y=y)
        doc.recompute()
        pump(2)
        val, exp = _raw(d), radius if typ == "Radius" else 2 * radius
        st = list(d.State)
        if val is None or "Invalid" in st or "Error" in st or abs(val - exp) > 1e-3:
            raise DrawingError("TechDraw measured %s but the model says %s for %s in the %s view — not added" % (
                "-" if val is None else fmt(val, 3), fmt(exp, 3), ref, view.Label))
        if text:
            d.Arbitrary = True
            d.FormatSpec = str(text)
        tol = _tolerance(d, tol)
    except BaseException:
        if d is not None:
            doc.removeObject(d.Name)
            doc.recompute()
        raise
    where = ("at (%s, %s) — your x/y: paper mm from the %s view centre" if op.get("x") is not None or
             op.get("y") is not None else "at (%s, %s) mm from the %s view centre (move it with move)") % (
        _w(x), _w(y), view.Label)
    return "%s %s %s verified ✓ %s%s%s, %s" % ("hole callout" if callout else "dimension", d.Name, typ,
                                               ("R" if typ == "Radius" else "⌀") + fmt(val, 3), tol,
                                               (" text '%s'" % text) if text else "", where)


def note_box(lines, size):
    """(width, height) of an annotation block as TechDraw renders it (page mm)."""
    return (max([em_width(t, size * EM) for t in lines] or [0]) + 2 * TEXT_PAD,
            LINE_PITCH * size * len(lines) + 2 * TEXT_PAD)


def notes_annotation(page):
    """The generated NOTES block of a page, or None."""
    return next((a for a in page.Views if a.TypeId == "TechDraw::DrawViewAnnotation"
                 and list(a.Text)[:1] == ["NOTES"]), None)


def _add_note(doc, page, views, op):
    if not op.get("text"):
        raise DrawingError("add_note needs text, e.g. {\"op\": \"add_note\", \"text\": \"DEBURR ALL EDGES\"}")
    text = [str(t) for t in op["text"]] if isinstance(op["text"], list) else str(op["text"]).split("\n")
    size = float(op.get("size", NOTE_SIZE))
    align = str(op.get("align") or "center").lower()
    if align not in ("center", "centre", "left"):
        raise DrawingError("align %r not understood — use 'left' (x, y = the left end of the first line's baseline) "
                           "or 'center' (x, y = the text centre)" % op.get("align"))
    w, h = note_box(text, size)
    asc = ASCENT * size * EM + TEXT_PAD               # block top → first baseline
    if op.get("x") is None or op.get("y") is None:
        notes = notes_annotation(page)
        if notes is not None:                    # just above the notes block, left-aligned with it
            ns = notes.TextSize.Value
            nw, nh = note_box(list(notes.Text), ns)
            lx = notes.X.Value - nw / 2 + TEXT_PAD
            by = notes.Y.Value + nh / 2 + 3 + h - asc
        else:
            lx, by = 40.0, 60.0
        if align == "left":
            x, y = float(op.get("x", lx)), float(op.get("y", by))
        else:
            x, y = float(op.get("x", lx - TEXT_PAD + w / 2)), float(op.get("y", by + asc - h / 2))
    else:
        x, y = float(op["x"]), float(op["y"])
    cx, cy = (x - TEXT_PAD + w / 2, y + asc - h / 2) if align == "left" else (x, y)
    a = doc.addObject("TechDraw::DrawViewAnnotation", "Note")
    page.addView(a)
    a.Text = text
    a.TextSize = size
    a.X, a.Y = cx, cy
    if align == "left":
        return ("note %s with its first line starting at (%s, %s) — x/y: the left end of the first line's baseline, "
                "page mm from the bottom-left corner; text %s mm" % (a.Name, _w(x), _w(y), _w(size)))
    return "note %s at (%s, %s) — x/y: the text centre in page mm from the bottom-left corner; text %s mm" % (
        a.Name, _w(x), _w(y), _w(size))


def _view_box(view):
    """Page box (mm) of a part view's drawn outline."""
    shape = view_shape(view)
    xmin, ymin, xmax, ymax = _outline(view, shape, view_map(view, shape))
    sc, X, Y = view.Scale, float(getattr(view.X, "Value", view.X)), float(getattr(view.Y, "Value", view.Y))
    return (X + xmin * sc, Y + ymin * sc, X + xmax * sc, Y + ymax * sc)


def _append_note(doc, page, views, op):
    """A numbered line at the end of the generated NOTES block, wrapped to the
    block's width; the block keeps its left and bottom edges and grows upward."""
    if not op.get("text"):
        raise DrawingError("append_note needs text, e.g. {\"op\": \"append_note\", \"text\": \"ANODISE BLACK\"}")
    notes = notes_annotation(page)
    if notes is None:
        raise DrawingError("this page has no generated NOTES block to append to — use add_note (x, y, align)")
    size = notes.TextSize.Value
    old = list(notes.Text)
    nums = [int(m.group(1)) for m in (re.match(r"^(\d+)\. ", t) for t in old) if m]
    head = "%d. " % (max(nums or [0]) + 1)
    ow, oh = note_box(old, size)
    limit = max(ow - 2 * TEXT_PAD, 60.0)
    lines, cur = [], head
    for word in str(op["text"]).split():
        trial = cur + ("" if cur.endswith((" ", " ")) else " ") + word
        if em_width(trial, size * EM) > limit and cur.strip("  ") not in ("", head.strip()):
            lines.append(cur)
            cur = " " * len(head) + word            # NBSP: TechDraw drops leading spaces
        else:
            cur = trial
    lines.append(cur)
    new = old + lines
    nw, nh = note_box(new, size)
    notes.Text = new
    notes.X = notes.X.Value + (nw - ow) / 2
    notes.Y = notes.Y.Value + (nh - oh) / 2
    box = (notes.X.Value - nw / 2, notes.Y.Value - nh / 2, notes.X.Value + nw / 2, notes.Y.Value + nh / 2)
    hit = []
    for v in views.values():
        try:
            if _overlap(box, _view_box(v), 1.0):
                hit.append(v.Label)
        except Exception:  # noqa: BLE001
            pass
    return "note line %s appended to %s (%d line(s))%s" % (head.strip(), notes.Name, len(lines), (
        " — ⚠ the NOTES block now reaches the %s view: move it (move %s to new x/y) or shorten the text"
        % (" and ".join(hit), notes.Name)) if hit else "")


def _set_title(doc, page, views, op):
    fields = op.get("fields") or {}
    if not fields:
        raise DrawingError("set_title needs fields, e.g. {\"creator\": \"…\", \"revision_index\": \"B\"}")
    keys = sorted(dict(page.Template.EditableTexts))
    bad = [k for k in fields if k not in keys]
    if bad:
        raise DrawingError("title block has no field %s — fields: %s" % (", ".join(bad), ", ".join(keys)))
    _, short, shrunk = set_title(page.Template, fields)
    return "title block %s%s%s" % (", ".join("%s=%s" % kv for kv in fields.items()),
                                   " (shortened to fit: %s)" % shortened_text(short) if short else "",
                                   " (%s)" % shrunk_text(shrunk) if shrunk else "")


def _move(doc, page, views, op):
    o = _find_obj(doc, page, op.get("name"))
    if op.get("x") is None or op.get("y") is None:
        raise DrawingError("move needs x and y")
    o.X, o.Y = float(op["x"]), float(op["y"])
    if o.TypeId == "TechDraw::DrawViewDimension":
        refs = o.References2D or []
        rel = "paper mm from the %s view centre" % (refs[0][0].Label if refs else "view")
    else:
        rel = "page mm from the bottom-left corner (the centre of %s)" % ("the text" if "Annotation" in o.TypeId
                                                                          else "the view")
    return "moved %s to (%s, %s) — %s" % (o.Name, _w(o.X.Value), _w(o.Y.Value), rel)


MATCH_KEYS = ("view", "type", "value", "text", "tol")


def _match_dims(page, views, match):
    """Dimensions on the page matching {view, type, value (± tol, default 0.001), text}."""
    if not isinstance(match, dict) or not match:
        raise DrawingError("match needs one or more of %s, e.g. {\"view\": \"Top\", \"type\": \"DistanceX\", "
                           "\"value\": 10}" % ", ".join(MATCH_KEYS))
    bad = [k for k in match if k not in MATCH_KEYS]
    if bad:
        raise DrawingError("match key %s unknown — use %s" % (", ".join(bad), ", ".join(MATCH_KEYS)))
    view = None
    if match.get("view"):
        view = views.get(str(match["view"]).lower())
        if view is None:
            raise DrawingError("no view %r on the page (views: %s)" % (match["view"], ", ".join(
                v.Label for v in views.values())))
    if match.get("type") and match["type"] not in DIM_TYPES:
        raise DrawingError("type %r unknown — use %s" % (match["type"], ", ".join(DIM_TYPES)))
    tol = float(match.get("tol", 1e-3))
    out = []
    for d in page_dims(page, view):
        if match.get("type") and d.Type != match["type"]:
            continue
        if match.get("value") is not None:
            val = _raw(d)
            if val is None or abs(val - float(match["value"])) > tol:
                continue
        if match.get("text") and not _text_matches(str(match["text"]), d):
            continue
        out.append(d)
    return out


def _text_matches(want, d):
    """The text a dimension shows (dim_text: its value with its format, or its
    own text) contains want as a whole token ('130' matches '130' and '⌀130'
    but not '1130' or '130.5')."""
    want = want.strip()
    for t in (dim_text(d), str(d.FormatSpec) if getattr(d, "Arbitrary", False) else None):
        if t and (t.strip() == want or re.search(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(want), t)):
            return True
    return False


def _remove(doc, page, views, op):
    """Remove a dimension, note or view by name, or every dimension that
    matches {view, type, value, text}; a remove that finds nothing fails."""
    if op.get("match") is not None:
        hits = _match_dims(page, views, op["match"])
        if not hits:
            have = ["%s (%s %s, %s)" % (d.Name, d.Type, "-" if _raw(d) is None else _w(_raw(d)),
                                        d.References2D[0][0].Label if d.References2D else "?")
                    for d in page_dims(page)][:16]
            raise DrawingError("no dimension matches %s — dimensions on the page: %s" % (
                json.dumps(op["match"], ensure_ascii=False), "; ".join(have) or "none"))
        desc = ["%s (%s %s)" % (d.Name, d.Type, "-" if _raw(d) is None else _w(_raw(d))) for d in hits]
        for d in hits:
            _remove_obj(doc, page, d)
        return "removed %d dimension(s) matching %s: %s" % (len(hits), json.dumps(op["match"], ensure_ascii=False),
                                                           ", ".join(desc))
    return _remove_obj(doc, page, _find_obj(doc, page, op.get("name")))


def _remove_obj(doc, page, o):
    """Remove one object; cosmetic vertices only that dimension used go too,
    and the other dimensions keep pointing at the same places."""
    name = o.Name
    if o.TypeId != "TechDraw::DrawViewDimension" or not o.References2D:
        doc.removeObject(name)
        return "removed %s" % name
    view = o.References2D[0][0]
    if not hasattr(view, "getCosmeticVertexBySelection"):
        doc.removeObject(name)
        return "removed %s" % name

    def tag_of(sub):
        try:
            cv = view.getCosmeticVertexBySelection(sub)
        except Exception:  # noqa: BLE001
            cv = None
        return getattr(cv, "Tag", None)
    mine = set(t for t in (tag_of(s) for s in _subs(o) if s.startswith("Vertex")) if t)
    others = [d for d in page_dims(page, view) if d.Name != name]
    verts = {i: (x, y) for i, x, y in view_vertices(view)}
    keep, before = set(), {}
    for d in others:
        subs = _subs(d)
        before[d.Name] = [(s, verts.get(int(s[6:])) if s.startswith("Vertex") else None) for s in subs]
        keep |= set(t for t in (tag_of(s) for s in subs if s.startswith("Vertex")) if t)
    doc.removeObject(name)
    gone = [t for t in mine if t not in keep]
    for t in gone:
        view.removeCosmeticVertex(t)
    doc.recompute()
    pump(2)
    if gone:                     # vertex indices shift: point every dimension back at its old position
        now = view_vertices(view)
        for d in others:
            subs, fixed = [], False
            for s, p in before[d.Name]:
                if p is not None:
                    cur = next((t for t in now if t[0] == int(s[6:])), None)
                    if cur is None or abs(cur[1] - p[0]) > 1e-6 or abs(cur[2] - p[1]) > 1e-6:
                        hit = next((t for t in now if abs(t[1] - p[0]) < 1e-6 and abs(t[2] - p[1]) < 1e-6), None)
                        if hit is not None:
                            s, fixed = "Vertex%d" % hit[0], True
                subs.append(s)
            if fixed:
                d.References2D = [(view, s) for s in subs]
        doc.recompute()
    return "removed %s%s" % (name, " and %d marker vertex(es) only it used" % len(gone) if gone else "")


OP_FUNCS = {"add_dimension": _dimension, "add_hole_callout": _dimension, "add_note": _add_note,
            "append_note": _append_note, "set_title": _set_title, "move": _move, "remove": _remove}


def apply_ops(doc, page, ops):
    """Apply drawing ops to a page: (done, failed), one line each. A failing op
    is reported with what to do and leaves nothing behind; the rest still run."""
    views = page_views(page)
    wait_views([v for v in views.values() if v.TypeId in COMPUTED_VIEWS])
    done, failed = [], []
    for i, op in enumerate(ops or [], 1):
        kind = op.get("op") if isinstance(op, dict) else None
        fn = OP_FUNCS.get(kind)
        if fn is None:
            failed.append("op %d: unknown op %r — valid ops: %s" % (i, kind, ", ".join(OP_NAMES)))
            continue
        try:
            done.append(fn(doc, page, views, op))
        except (DrawingError, an.RefError, fc.FcError) as e:
            failed.append("op %d %s: %s" % (i, kind, e))
        except Exception as e:  # noqa: BLE001
            failed.append("op %d %s: %s: %s" % (i, kind, type(e).__name__, e))
        views = page_views(page)
    doc.recompute()
    pump(10)
    return done, failed


# --------------------------------------------------------------------- ops

def _seg_hits(seg, b):
    """An axis-parallel segment (x0, y0, x1, y1) passes through box b."""
    x0, y0, x1, y1 = seg
    if abs(x0 - x1) < 1e-9:
        return b[0] < x0 < b[2] and min(y0, y1) < b[3] and b[1] < max(y0, y1)
    return b[1] < y0 < b[3] and min(x0, x1) < b[2] and b[0] < max(x0, x1)


def declutter(kk):
    """Slide each generated linear dimension's text along its own dimension
    line until no other dimension's extension or dimension line runs through
    it and it overlaps no other text (inside its own span when possible).
    Returns how many texts moved."""
    ents = kk.get("lay") or []
    hb = DIM_FONT * 1.25                      # text box height round the label centre
    for e in ents:
        d = e["d"]
        try:
            X, Y = d.X.Value, d.Y.Value
        except Exception:  # noqa: BLE001 — removed since
            e["dead"] = True
            continue
        (ax, ay), (bx, by) = e["a"], e["b"]
        if e["dir"] == "x":                  # extension lines overshoot the dimension line by ~3 mm
            ly = Y - DIM_FONT                 # the dimension line runs just under its text
            ov = -3.0 if ly < min(ay, by) else 3.0
            e["segs"] = [(ax, ay, ax, ly + ov), (bx, by, bx, ly + ov), (min(ax, bx), ly, max(ax, bx), ly)]
            e["span"], e["half"], e["cur"] = (min(ax, bx), max(ax, bx)), e["w"] / 2 + 1, X
        else:
            lx = X + e["w"] / 2 if X < min(ax, bx) else X - e["w"] / 2
            ov = -3.0 if lx < min(ax, bx) else 3.0
            e["segs"] = [(ax, ay, lx + ov, ay), (bx, by, lx + ov, by), (lx, min(ay, by), lx, max(ay, by))]
            e["span"], e["half"], e["cur"] = (min(ay, by), max(ay, by)), hb / 2 + 0.8, Y
        e["box"] = (X - e["w"] / 2 - 2, Y - hb / 2 - 0.8, X + e["w"] / 2 + 2, Y + hb / 2 + 0.8)    # kept clear
    live = [e for e in ents if not e.get("dead")]

    def clash(e, b):
        for f in live:
            if f is e:
                continue
            if any(_seg_hits(sg, b) for sg in f["segs"]) or _overlap(f["box"], b, 0.3):
                return True
        return False

    def box_at(e, c):
        X, Y = (c, e["d"].Y.Value) if e["dir"] == "x" else (e["d"].X.Value, c)
        return (X - e["w"] / 2 - 2, Y - hb / 2 - 0.8, X + e["w"] / 2 + 2, Y + hb / 2 + 0.8)
    moved = 0
    for e in live:
        if e.get("fixed") or not clash(e, e["box"]):
            continue
        lo, hi = e["span"]
        a, b = lo + e["half"] + 1.0, hi - e["half"] - 1.0
        cands = [a + 0.5 * i for i in range(int((b - a) / 0.5) + 1)] if b >= a else []
        cands += [lo - e["half"] - 1.5, hi + e["half"] + 1.5]
        for c in sorted(cands, key=lambda c: abs(c - e["cur"])):
            bx = box_at(e, c)
            if not clash(e, bx):
                if e["dir"] == "x":
                    e["d"].X = c
                else:
                    e["d"].Y = c
                e["box"], e["cur"] = bx, c
                moved += 1
                break
    return moved


def default_drawing_number(project, part_label):
    """(number, info): '<project or title_block.drawing_number>-<PART>' (just
    the part when both say the same); without the prefix when that is what it
    takes to fit the title block at full size — never shortened with '…'."""
    tb = project.get("title_block") or {}
    prefix = tb.get("drawing_number") or project.get("name", "DWG")
    pn = re.sub(r"\W+", "", part_label).upper()
    same = re.sub(r"[\W_]+", "", str(prefix)).lower() == re.sub(r"[\W_]+", "", pn).lower()
    full = pn if same else "%s-%s" % (prefix, pn)
    width, size = TITLE_FIELDS["drawing_number"]
    for cand, need in ((full, size), (pn, size), (full, MIN_IDENT_SIZE), (pn, MIN_IDENT_SIZE)):
        got = ident_size(cand, width, size)
        if got is not None and got >= need:
            return cand, (None if cand == full else
                          "title     drawing number %s — the project prefix (%s-…) was dropped so the number fits the "
                          "title block whole; pass drawing_number=… to choose one" % (cand, prefix))
    raise DrawingError("the drawing number for %s ('%s') does not fit the title block even without the project "
                       "prefix — pass a shorter drawing_number (drawing option drawing_number=…)" % (part_label, pn))


def op_create(args):
    """One drawing document per part: each sheet is generated from a fresh copy
    of the build, so drawing one part never disturbs another's sheet. Every
    sheet is generated (into a temporary folder) before anything is written:
    when one part fails, no part's sheet or export is replaced, and the error
    says how each part went. Then the sheets move to <save_dir>/<sheet>.FCStd
    and the exports to out_dir."""
    doc = App.openDocument(args["doc"])
    project = args.get("project") or {}
    out = {"sheets": [], "images": []}
    texts = []
    sheets = []
    for spec in args["sheets"]:
        if spec.get("part"):
            sheets.append(spec)
        else:                                   # every manufactured part
            for o in an.results(doc):
                if an.role(o) != "ref" and an.global_shape(o).Solids:
                    sheets.append(dict(spec, part=o.Label))
    if not sheets:
        seen = ["%s (%s, role %s, visible %s)" % (o.Label, o.TypeId, an.role(o), getattr(o, "Visibility", "?"))
                for o in doc.Objects if an.has_shape(o)][:12]
        raise DrawingError("no manufactured (non-reference) parts to draw; shapes in the document: %s"
                           % "; ".join(seen))
    App.closeDocument(doc.Name)
    tmp = tempfile.mkdtemp(prefix="fcmcp-create-")
    try:
        done, failed, moves = [], [], []
        for spec in sheets:
            try:
                sheet, lines, mv = _create_sheet(args, spec, project, tmp)
            except (DrawingError, an.RefError, fc.FcError) as e:
                for d_ in list(App.listDocuments().values()):
                    App.closeDocument(d_.Name)
                failed.append((spec.get("part"), str(e)))
                continue
            done.append(spec.get("part"))
            out["sheets"].append(sheet)
            out["images"].append(sheet["files"].get("png"))
            texts += lines
            moves += mv
        if failed:
            raise DrawingError("%s\nNothing was written: no sheet in drawings/ and no export was replaced%s." % (
                "\n".join("✗ %s: %s" % f for f in failed),
                " — %s drew fine and will be saved once the failing part(s) are fixed (or draw them one at a time "
                "with part=…)" % ", ".join(done) if done else ""))
        for src, dst in moves:                  # every part succeeded: now replace the files
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["text"] = "\n".join(texts)
    out["images"] = [i for i in out["images"] if i]
    if os.environ.get("FCMCP_TRACE"):
        out["trace"] = TRACE
    return out


def _create_sheet(args, spec, project, tmp):
    """Generate one part's sheet into tmp. Returns (sheet record with its final
    paths, report lines, [(temporary file, final path)])."""
    doc = App.openDocument(args["doc"])
    part = an.find(doc, spec["part"])
    shape = an.global_shape(part)
    if not shape.Solids:
        raise DrawingError("%s has no solid to draw" % part.Label)
    hole_mode = str(spec.get("hole_dims") or "baseline").lower()
    if hole_mode not in ("baseline", "pitch"):
        raise DrawingError("hole_dims %r not understood — use 'baseline' (each hole centre from the part's edge) "
                           "or 'pitch' (one location from the edge, then centre to centre)" % spec.get("hole_dims"))
    process = spec.get("process") or project.get("process")
    sheet = _json_prop(part, "FcSheet", None)
    sheet = sheet if isinstance(sheet, dict) else None
    if spec.get("style", "auto") == "auto":
        style, axis, _, why = detect_style(shape, process)
        nb = len((sheet or {}).get("bends") or [])
        if nb:                                  # bent: three views, not a plate's outline
            style, axis, why = "prismatic", None, "bent sheet metal (%d bend%s, FcSheet)" % (nb, "s" if nb > 1 else "")
    else:
        style, axis, why = spec["style"], None, "style=%s given" % spec["style"]
    if style == "turned" and axis is None:
        axis, _, frac = turned_axis(shape)
        if axis is None or frac < 0.5:
            raise DrawingError("style 'turned' needs a mostly axisymmetric part")
    apt = turned_axis(shape)[1] if style == "turned" else None
    warnings, infos = [], []
    holes = an.detect_holes(shape)
    specs = hole_specs(part)
    sec_opt = str(spec.get("section") or "auto").strip().lower()
    if sec_opt not in SECTION_OPTIONS:
        raise DrawingError("section %r not understood — use 'auto' (a section through the axis when a turned part "
                           "has bores or internal grooves), 'full' (always, for a turned part) or 'none'"
                           % spec.get("section"))
    section = style == "turned" and not spec.get("views") and (
        sec_opt == "full" or (sec_opt == "auto" and has_internal(shape, axis, apt)))
    if sec_opt == "full" and not section:
        infos.append("section   section='full' is for a turned part drawn in its own views — this %s sheet has no "
                     "section%s" % (style, " (views=… given)" if style == "turned" else ""))
    bores = turned_bores_info(shape, axis, apt, holes, specs) if section else []
    try:                                                # what the sheet shows beyond the shape (sheet_status)
        cparts = content_parts(part, shape, holes)
    except Exception as e:  # noqa: BLE001
        cparts = None
        warnings.append("could not record what the sheet shows (%s) — sheet_status and release will treat this "
                        "sheet as out of date" % e)
    steps = turned_steps_info(part, shape, axis, apt, warnings) if style == "turned" else None
    on_view = [st["thread"] for st in steps or [] if st["thread"]]
    threads = [(t, any(t.get("z_from") == v.get("z_from") and t.get("size") == v.get("size") for v in on_view))
               for t in _json_prop(part, "FcThreads", [])]
    note_lines = notes_for(spec, project, part, threaded=any(s["threaded"] for s in specs),
                           chamfer=chamfer_size(part, shape, axis if style == "turned" else None),
                           comp=hole_compensation(part), threads=threads, sheet=sheet)
    flat = sheet_flat_pattern(part, sheet, warnings, infos) if sheet else None
    if style == "turned":
        _, _, frac = turned_axis(shape)
        if frac < 0.999:
            note_lines.append("CROSS HOLES / FLATS: SECONDARY OPERATION (SEE VIEWS)")
    if spec.get("drawing_number"):
        number = spec["drawing_number"]
    else:
        number, why_n = default_drawing_number(project, part.Label)
        if why_n:
            infos.append(why_n)
    lay = plan(shape, style, axis, spec, apt, holes, specs, note_lines, part.Label,
               [{"d": st["d"], "x0": st["t0"], "x1": st["t1"], "text": st["text"], "thread": bool(st["thread"]),
                 "inner": st["inner"], "bore": bool(st.get("bore"))}
                for st in (steps or []) + bores], section=section, flat=flat["face"] if flat else None,
               pockets=not (sheet or {}).get("bends"))
    note_lines = lay["note_lines"]
    name = spec.get("sheet") or part.Label
    page, tpl = make_page(doc, name, lay["paper"])
    s = lay["scale"]
    hidden = spec.get("hidden")
    views = [None] * len(lay["views"])
    flat_obj = None
    for i, vp in enumerate(lay["views"]):
        if vp["name"] == "section":
            continue
        if vp["name"] == "flat":                # the flat pattern: in this drawing document only
            flat_obj = doc.addObject("Part::Feature", "FlatPattern")
            flat_obj.Shape = flat["face"]
            views[i] = add_view(doc, page, flat_obj, vp, "flat", s)
            continue
        hid = bool(hidden) if hidden is not None else (style == "turned" and i == 0)
        views[i] = add_view(doc, page, part, vp, vp["name"], s, hidden=hid)
    for i, vp in enumerate(lay["views"]):
        if vp["name"] == "section":            # after its base view, which carries the cutting line
            base = views[[k for k, q in enumerate(lay["views"]) if q["name"] == "end"][0]]
            views[i] = add_section_view(doc, page, part, vp, base, s, apt)
            infos.append("section   SECTION A-A through the axis, hatched, cutting line on the end view (%s) — "
                         "section='none' draws the side view with hidden lines instead" % (
                             "the part has bores or internal grooves" if sec_opt == "auto" else "section='full' given"))
    doc.recompute()
    if not wait_views(views):
        raise DrawingError("TechDraw did not finish computing the views")
    dims = []
    k_by_view = {}
    for v, vp in zip(views, lay["views"]):
        verts = view_vertices(v)
        kv = {"x": 0, "y": 0, "callouts": 0, "w": vp["w"], "h": vp["h"], "n_geom": len(verts),
              "hole_mode": hole_mode, "ylow": min([t[2] for t in verts] or [0]),
              "xlow": min([t[1] for t in verts] or [0])}
        if vp["name"] != "iso":
            shp = flat["face"] if vp["name"] == "flat" else shape
            try:                                # the outline's true extremes (a round outline has no vertex there)
                kv["vmap"] = view_map(v, shp)
                kv["box"] = view_box(shp, kv["vmap"])
                kv["xlow"], kv["ylow"] = kv["box"][0], kv["box"][1]
            except DrawingError:
                pass
        k_by_view[v.Name] = kv
    bb = an.tight_bb(shape)
    callouts = []
    if style == "turned":
        for kv in k_by_view.values():
            kv["axis"], kv["axis_pt"] = axis, apt
    for v, vp in zip(views, lay["views"]):
        if section and vp["name"] == "end":     # callouts start above and right of the cutting line's letters
            k_by_view[v.Name]["callouts"] = CUTLINE_ROOM / 8.0
            k_by_view[v.Name]["callout_dx"] = CUTLINE_ROOM
    if spec.get("dimensions", "auto") == "auto":
        if style == "turned":
            kk0 = k_by_view[views[0].Name]
            n_rows = turned_dims(doc, page, views[0], dims, shape, axis, apt, steps or [], kk0, warnings,
                                 callouts, section, bores)
            kk0["x"] = n_rows                   # hole positions stack below the lengths
        else:
            exp = {"front": (bb.XLength, bb.ZLength), "top": (bb.XLength, bb.YLength),
                   "left": (bb.YLength, bb.ZLength), "right": (bb.YLength, bb.ZLength),
                   "bottom": (bb.XLength, bb.YLength), "back": (bb.XLength, bb.ZLength)}
            first = views[0]
            n0 = lay["views"][0]["name"]
            if "box" in k_by_view[first.Name]:
                dims += overall_dims(doc, page, first, exp[n0][0], exp[n0][1], "xy", k_by_view[first.Name], shape,
                                     part)
            if len(views) > 1 and lay["views"][1]["name"] != "iso" and "box" in k_by_view[views[1].Name]:
                n1 = lay["views"][1]["name"]
                second = views[1]
                dims += overall_dims(doc, page, second, exp[n1][0], exp[n1][1], "y", k_by_view[second.Name], shape,
                                     part)
        by_axis = {}
        for v, vp in zip(views, lay["views"]):
            if vp["name"] in ("iso", "flat") or "box" not in k_by_view[v.Name]:
                continue
            ax = an._axis_name(an._canon(vp["dir"]))
            by_axis.setdefault(ax, v)
        callouts += hole_dims(doc, page, part, shape, by_axis, k_by_view, dims, warnings, holes, specs)
        by_name = {vp["name"]: v for v, vp in zip(views, lay["views"])}
        for v, vp in zip(views, lay["views"]):
            groups = (lay.get("pockets") or {}).get(vp["name"])
            if groups and "box" in k_by_view[v.Name]:
                pocket_dims(doc, page, part, shape, v, groups, by_name, k_by_view, dims, warnings, callouts)
        warnings += lay.get("pocket_warnings") or []
        if flat_obj is not None and "box" in k_by_view[by_name["flat"].Name]:
            fv, kf = by_name["flat"], k_by_view[by_name["flat"].Name]
            fbb = an.tight_bb(flat["face"])
            dims += overall_dims(doc, page, fv, fbb.XLength, fbb.YLength, "xy", kf, flat["face"], flat_obj)
            flat_bend_lines(doc, page, fv, kf, flat["lines"], sheet, warnings)
    doc.recompute()
    pump(10)
    moved = sum(declutter(kv) for kv in k_by_view.values())
    if moved:
        doc.recompute()
        pump(5)
    # every dimension against the model: its value, and for a turned step where it is anchored
    checked = []
    for entry in dims:
        d, expected, what = entry[:3]
        val = d.getRawValue() if hasattr(d, "getRawValue") else None
        st = list(d.State)
        bad = "Invalid" in st or "Error" in st
        if expected is not None and val is not None and abs(val - expected) > 1e-3:
            warnings.append("%s: TechDraw measured %s but the model says %s — dimension removed"
                            % (what, _w(val), _w(expected)))
            bad = True
        elif bad:
            warnings.append("%s: TechDraw could not compute it — dimension removed" % what)
        elif len(entry) > 3 and entry[3] is not None:
            msg = entry[3]()
            if msg:
                warnings.append(msg + " — dimension removed")
                bad = True
        if bad:
            doc.removeObject(d.Name)
        checked.append({"what": what, "value": val, "ok": not bad, "name": None if bad else d.Name})
    for kv in k_by_view.values():              # holes left without a location count as not verified
        for what in kv.get("missing") or []:
            checked.append({"what": what, "value": None, "ok": False, "name": None})
    tb = project.get("title_block") or {}
    fields = {"title": spec.get("title") or part.Label,
              "part_material": spec.get("material") or (project.get("material") or {}).get("name", ""),
              "general_tolerances": (project.get("drawing") or {}).get("general_tolerance", "ISO 2768-m"),
              "scale": scale_text(s), "date_of_issue": time.strftime("%d %b %Y"),
              "drawing_number": number,
              "revision_index": spec.get("revision") or args.get("rev", "A"),
              "creator": tb.get("author") or "",
              "approval_person": "", "document_type": "Part drawing", "sheet_number": "1 / 1",
              "legal_owner_1": tb.get("company") or "",
              "legal_owner_2": "", "legal_owner_3": "", "legal_owner_4": "", "language_code": "EN"}
    fields.update(spec.get("title_fields") or {})
    unknown, short, shrunk = set_title(tpl, fields)
    if unknown:
        warnings.append("title block has no field %s — fields: %s" % (
            ", ".join(sorted(unknown)), ", ".join(sorted(dict(tpl.EditableTexts)))))
    if short:
        infos.append("title     shortened to fit the title block: %s" % shortened_text(short))
    if shrunk:
        infos.append("title     %s" % shrunk_text(shrunk))
    add_notes(doc, page, lay["notes"])
    for v, vp in zip(views, lay["views"]):
        if vp["name"] == "flat":
            low = min([d.Y.Value for d in page_dims(page, v) if d.Y.Value < -vp["h"] / 2] + [-vp["h"] / 2 + DIM_FONT])
            cap = FLAT_CAPTION + ("  " + vp["label"] if vp.get("label") else "")
            cw = em_width(cap, CAPTION_SIZE * EM) / 2 + 2                    # kept on the page
            add_caption(doc, page, page.Name + "_FlatCaption", cap, min(max(vp["x"], 24.0 + cw), lay["W"] - 14.0 - cw),
                        vp["y"] + low - DIM_FONT - 3 - text_h(CAPTION_SIZE) / 2)
            continue
        if vp["name"] == "section":          # SECTION A-A under the dimensions below the view
            low = min([d.Y.Value for d in page_dims(page, v) if d.Y.Value < -vp["h"] / 2] + [-vp["h"] / 2 + DIM_FONT])
            cw = em_width(SECTION_CAPTION, CAPTION_SIZE * EM) / 2 + 2       # kept off the drawing frame
            add_caption(doc, page, page.Name + "_SectionCaption", SECTION_CAPTION, max(vp["x"], 24.0 + cw),
                        vp["y"] + low - DIM_FONT - 3 - text_h(CAPTION_SIZE) / 2)
        if vp.get("label"):                  # the iso view's own scale
            a = doc.addObject("TechDraw::DrawViewAnnotation", page.Name + "_IsoScale")
            page.addView(a)
            a.Text = [vp["label"]]
            a.TextSize = NOTE_SIZE
            a.X, a.Y = vp["x"], vp["y"] - vp["h"] / 2 - 4
    page.addProperty("App::PropertyString", "FcSource", "fclib", "what this sheet was drawn from (JSON)")
    # sheet_status: current only while the shape (sig), what the sheet shows of it (content) and the drawing
    # spec the server derived from drawings.json + project.json (spec_hash) are all unchanged
    page.FcSource = json.dumps({"part": part.Label, "rev": args.get("rev"), "sig": shape_signature(shape),
                                "content": content_hash(cparts) if cparts else None, "content_parts": cparts,
                                "spec_hash": spec.get("_spec_hash")})
    doc.recompute()
    pump(10)
    before_ops = set(d.Name for d in page_dims(page))
    ops_done, ops_failed = apply_ops(doc, page, spec.get("ops") or [])
    present = set(d.Name for d in page_dims(page))
    gen_ok = [c["name"] for c in checked if c["ok"]]
    kept = [n for n in gen_ok if n in present]
    added = sorted(present - before_ops)
    n_bad = sum(1 for c in checked if not c["ok"])
    fs = json.loads(page.FcSource)
    fs["verified"] = kept + added           # the dimensions checked against the model (edit keeps it up to date)
    fs["unverified"] = n_bad                # generated ones that failed or could not be placed (sheet_status total)
    page.FcSource = json.dumps(fs)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    os.makedirs(os.path.join(tmp, "out"), exist_ok=True)
    files = export(page, os.path.join(tmp, "out", stem))
    moves = [(f, os.path.join(args["out_dir"], os.path.basename(f))) for f in files.values()]
    files = {k: os.path.join(args["out_dir"], os.path.basename(f)) for k, f in files.items()}
    sheet = {"sheet": name, "part": part.Label, "style": style, "paper": lay["paper"],
             "scale": scale_text(s), "files": files, "dims": checked, "callouts": callouts,
             "notes": note_lines, "warnings": warnings, "ops_done": ops_done,
             "ops_failed": ops_failed, "remarks": lay["remarks"],
             "dims_verified": len(kept) + len(added), "dims_total": len(kept) + len(added) + n_bad}
    ok = len(kept) + len(added)
    extra = []
    if len(kept) < len(gen_ok):
        extra.append("%d removed by ops" % (len(gen_ok) - len(kept)))
    if added:
        extra.append("%d added by ops" % len(added))
    texts = ["SHEET %s  (%s, %s %s landscape, scale %s)\n  style     %s — %s%s\n  views     %s\n"
             "  dims      %d/%d verified against the model%s%s\n  callouts  %s\n  notes     %s\n  files     %s" % (
                 name, style, lay["paper"], "ISO 5457", scale_text(s), style, why,
                 "" if spec.get("style", "auto") != "auto" else " (pass style=… to override)",
                 ", ".join(vp["name"] for vp in lay["views"]), ok, ok + n_bad,
                 " (%d generated%s)" % (len(gen_ok) + n_bad, "; " + ", ".join(extra) if extra else "")
                 if extra or n_bad else "",
                 " (failed ones removed or not placed)" if n_bad else "", "; ".join(c[0] for c in callouts) or "none",
                 " · ".join(note_lines), " · ".join(os.path.basename(f) for f in files.values()))]
    for r in lay["remarks"]:
        texts.append("  layout    " + r)
    texts += ["  " + i for i in infos]
    for w in warnings:
        texts.append("  ✗ " + w)
    if ops_done or ops_failed:
        texts.append("  ops       %d of %d applied" % (len(ops_done), len(ops_done) + len(ops_failed)))
        texts += ["    ✓ " + x for x in ops_done]
        texts += ["    ✗ " + x for x in ops_failed]
        if ops_failed:
            texts.append("    (fix or drop the failed ops — they are replayed every time this sheet is drawn)")
    if args.get("save_dir"):
        os.makedirs(os.path.join(tmp, "save"), exist_ok=True)
        tpath = os.path.join(tmp, "save", "%s.FCStd" % stem)
        if os.path.exists(tpath):
            raise DrawingError("two sheets are both named %s — give one a different sheet=… name" % stem)
        save_doc(doc, tpath)
        dpath = os.path.join(args["save_dir"], stem + ".FCStd")
        moves.append((tpath, dpath))
        sheet["document"] = dpath
        texts.append("  document  %s" % dpath)
    App.closeDocument(doc.Name)
    return sheet, texts, moves


def page_scale(pg):
    """The scale the sheet is drawn at: that of its orthographic views."""
    views = list(page_views(pg).values())
    ortho = [v for v in views if not an._axis_name(an._canon(Vector(v.Direction))).startswith("(")]
    sc = [round(v.Scale, 6) for v in (ortho or views)]
    if not sc:
        return pg.Scale
    return max(set(sc), key=sc.count)


def dim_text(d):
    """The text a dimension shows: its own text, or its format with the value."""
    val = _raw(d)
    if getattr(d, "Arbitrary", False):
        return str(d.FormatSpec)
    if val is None:
        return "-"
    return re.sub(r"%[-+ #0-9.]*[wgfe]", lambda m: _w(val), str(d.FormatSpec or "%.2w"), count=1)


def _principal(v):
    """('x' | 'y' | 'z', ±1) for a principal direction, else None."""
    for n, c in (("x", v.x), ("y", v.y), ("z", v.z)):
        if abs(abs(c) - 1) < 1e-9:
            return n, (1 if c > 0 else -1)
    return None


def view_model_frame(view):
    """view point (u, v) → model coordinates along the view's principal axes:
    a function returning e.g. {"z": 13.0, "x": 4.0}, or None (oblique view)."""
    xd, yd = view_axes(view)
    px, py = _principal(xd), _principal(yd)
    if not px or not py:
        return None
    shape = view_shape(view)
    vmap = view_map(view, shape)
    p = shape.Vertexes[0].Point
    u0, v0 = vmap(p)
    cu, cv = p.dot(xd) - u0, p.dot(yd) - v0
    return lambda u, v: {px[0]: px[1] * (u + cu), py[0]: py[1] * (v + cv)}


def dim_anchors(d, frames):
    """'(z=13, x=4) (z=13, x=-4)': model coordinates of the points a dimension
    is attached to (its view vertices, or the middles of its view edges)."""
    refs = d.References2D or []
    if not refs or d.References3D:
        return ""
    view = refs[0][0]
    if view.Name not in frames:
        try:
            frames[view.Name] = view_model_frame(view)
        except Exception:  # noqa: BLE001
            frames[view.Name] = None
    f = frames[view.Name]
    if f is None:
        return ""
    out = []
    for sub in _subs(d):
        try:
            if sub.startswith("Vertex"):
                q = view.getVertexByIndex(int(sub[6:])).Point
            else:
                e = view.getEdgeByIndex(int(sub[4:]))
                q = e.valueAt((e.FirstParameter + e.LastParameter) / 2)
        except Exception:  # noqa: BLE001
            return ""
        out.append("(%s)" % ", ".join("%s=%s" % (k, _w(c)) for k, c in f(q.x, q.y).items()))
    return " ".join(out)


def describe_pages(doc):
    lines, pages = [], [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    frames = {}
    for pg in pages:
        tpl = pg.Template
        tname = re.sub(r"\.[0-9a-f]{10}(\.svg)$", r"\1", os.path.basename(getattr(tpl, "Template", "") or "-"))
        lines.append("PAGE %s  template %s  scale %s" % (pg.Label, tname, scale_text(page_scale(pg))))
        if tpl is not None and hasattr(tpl, "EditableTexts"):
            filled = {k: v for k, v in tpl.EditableTexts.items() if v.strip()}
            lines.append("  title block  " + " · ".join("%s=%s" % kv for kv in sorted(filled.items())))
        for v in pg.Views:
            t = v.TypeId.split("::")[-1]
            if t == "DrawViewDimension":
                refs = ", ".join("%s.%s" % (o.Label, ",".join(s) if isinstance(s, (list, tuple)) else s)
                                 for o, s in (v.References3D or v.References2D))
                anc = dim_anchors(v, frames)
                lines.append("  dim   %-12s %-10s %-20s at (%s, %s)  refs %s%s" % (
                    v.Name, v.Type, dim_text(v), _w(v.X.Value), _w(v.Y.Value), refs[:80],
                    "  anchors " + anc if anc else ""))
            elif t == "DrawViewAnnotation":
                lines.append("  note  %-12s %s  at (%s, %s)" % (v.Name, " / ".join(v.Text)[:1200], _w(v.X.Value),
                                                                _w(v.Y.Value)))
            elif t in ("DrawViewPart", "DrawProjGroupItem", "DrawViewSection", "DrawViewDetail"):
                src = ", ".join(o.Label for o in v.Source) if hasattr(v, "Source") else ""
                lines.append("  view  %-12s %-16s source %s  dir (%s)  scale %s  at (%s, %s)%s" % (
                    v.Label, t, src, an._pt(v.Direction), scale_text(v.Scale), _w(v.X), _w(v.Y),
                    "  hidden lines" if getattr(v, "HardHidden", False) else ""))
            elif t == "DrawProjGroup":
                lines.append("  group %-12s %d views, %s" % (v.Label, len(v.Views), v.ProjectionType))
            else:
                lines.append("  %-5s %s" % (t, v.Label))
    return lines, pages


def settle(doc, pages):
    """Views compute after the document opens; wait for them so dimension
    values are real, not 0."""
    views = [v for pg in pages for v in page_views(pg).values() if v.TypeId in COMPUTED_VIEWS]
    wait_views(views)
    dims = [d for pg in pages for d in page_dims(pg)]
    if any(not getattr(d, "Arbitrary", False) and not _raw(d) for d in dims):
        for d in dims:
            d.touch()
        doc.recompute()
        pump(10)


def find_page(pages, want):
    if not want:
        return pages[0]
    w = str(want).lower()
    for p in pages:
        if w in (p.Label.lower(), p.Name.lower(), p.Label.lower().replace(" sheet", "")):
            return p
    raise DrawingError("no page %r (pages: %s)" % (want, ", ".join(p.Label for p in pages)))


def op_read(args):
    doc = App.openDocument(args["doc"])
    pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    if not pages:
        raise DrawingError("no TechDraw pages in %s — read a drawing document (drawings/<part>.FCStd), or make one "
                           "with drawing create" % os.path.basename(args["doc"]))
    settle(doc, pages)
    lines, _ = describe_pages(doc)
    images = []
    if args.get("out_dir"):
        os.makedirs(args["out_dir"], exist_ok=True)
        pg = find_page(pages, args.get("page"))
        files = export(pg, os.path.join(args["out_dir"], re.sub(r"\W+", "_", pg.Label) + "_preview"), kinds=("png", ))
        images.append(files["png"])
    return {"text": "\n".join(lines), "images": images}


def op_edit(args):
    doc = App.openDocument(args["doc"])
    pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    if not pages:
        raise DrawingError("no TechDraw pages in %s" % args["doc"])
    page = find_page(pages, args.get("page"))
    ops = args.get("ops") or []
    before = set(d.Name for d in page_dims(page))
    done, failed = apply_ops(doc, page, ops)
    if failed:
        raise DrawingError("EDIT %s — nothing saved: %d of %d op(s) failed\n  %s%s\nFix the failed op(s) and send "
                           "the whole list again." % (page.Label, len(failed), len(ops),
                                                      "\n  ".join("✗ " + f for f in failed),
                                                      "".join("\n  (would have applied) " + x for x in done)))
    saved = args["save_as"]
    fs = _json_prop(page, "FcSource", None)
    if isinstance(fs, dict) and "verified" in fs:   # dimensions added here were verified as they were made
        present = set(d.Name for d in page_dims(page))
        fs["verified"] = [n for n in fs["verified"] if n in present] + sorted(present - before)
        page.FcSource = json.dumps(fs)
    rev = edit_revision(page, saved, ops)
    if rev:
        done.append(rev)
    save_doc(doc, saved, keep_label=True)
    os.makedirs(args["out_dir"], exist_ok=True)
    name = args.get("export_name") or re.sub(r"\W+", "_", page.Label) + "_edited"
    files = export(page, os.path.join(args["out_dir"], name))
    lines, _ = describe_pages(doc)
    return {"text": "EDIT %s\n  %s\n  saved %s\n%s" % (page.Label, "\n  ".join(done), saved, "\n".join(lines)),
            "images": [files["png"]], "files": files}


def edit_revision(page, save_as, ops):
    """Title-block revision of an edited sheet: the build rev it was drawn from
    plus the sheet file's edit number (Plate.r01.FCStd → r0001.1, .r02 → .2),
    so two PDFs of one drawing never carry the same revision. Left alone when
    the ops set revision_index themselves or the file has no rNN."""
    m = re.search(r"\.r(\d+)\.fcstd$", os.path.basename(save_as), re.I)
    tpl = page.Template
    et = dict(getattr(tpl, "EditableTexts", {}) or {}) if tpl is not None else {}
    if not m or "revision_index" not in et or any(
            isinstance(o, dict) and o.get("op") == "set_title" and "revision_index" in (o.get("fields") or {})
            for o in ops or []):
        return None
    old = str(et["revision_index"]).strip()
    base = re.sub(r"\.\d+$", "", old) or str(_json_prop(page, "FcSource", {}).get("rev") or "")
    new = "%s.%d" % (base, int(m.group(1))) if base else "%d" % int(m.group(1))
    if new == old:
        return None
    set_title(tpl, {"revision_index": new})
    return "revision %s → %s (title block: build revision . edit number of %s)" % (
        old or "-", new, os.path.basename(save_as))


def _sheet_page(doc, label):
    pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    for pg in pages:
        try:
            if json.loads(getattr(pg, "FcSource", "") or "{}").get("part") == label:
                return pg
        except ValueError:
            pass
    return pages[0] if pages else None


def op_sheet_status(args):
    """Is each sheet still drawn from the current build and spec? Current only
    when the part's shape inside the sheet document matches the build's
    (shape_signature), what the sheet shows of it — holes, bosses, fits,
    threads, hole specs, chamfers — matches (FcSource content), and the
    drawing spec the server derives from drawings.json + project.json matches
    (args sheets[].spec_hash vs FcSource spec_hash). A sheet drawn before
    content and spec were recorded is not current."""
    build = App.openDocument(args["build"])
    out, texts = [], []
    for sh in args.get("sheets") or []:
        label, path = sh.get("part"), sh.get("doc")
        rec = {"part": label, "doc": path, "current": False, "detail": "", "revision": "", "dims_verified": None,
               "dims_total": None, "changed": []}
        try:
            part_b = an.find(build, label)
            shape_b = an.global_shape(part_b)
            sig_b = shape_signature(shape_b)
        except Exception:  # noqa: BLE001
            sig_b = None
        if sig_b is None:
            rec["detail"] = "part %s is not in the build" % label
        elif not path or not os.path.isfile(path):
            rec["detail"] = "sheet file %s is missing" % path
        else:
            doc = App.openDocument(path)
            try:
                pg = _sheet_page(doc, label)
                if pg is not None and pg.Template is not None and hasattr(pg.Template, "EditableTexts"):
                    rec["revision"] = dict(pg.Template.EditableTexts).get("revision_index", "")
                fs = _json_prop(pg, "FcSource", {}) if pg is not None else {}
                fs = fs if isinstance(fs, dict) else {}
                try:
                    sig_s = shape_signature(an.global_shape(an.find(doc, label)))
                except Exception:  # noqa: BLE001
                    sig_s = fs.get("sig")
                if isinstance(fs.get("verified"), list):
                    rec["dims_verified"] = len(fs["verified"])
                    if isinstance(fs.get("unverified"), int):
                        rec["dims_total"] = len(fs["verified"]) + fs["unverified"]
                if sig_s is None:
                    rec["detail"] = "the sheet does not contain part %s" % label
                else:
                    why = []
                    if not same_signature(sig_s, sig_b):
                        rec["changed"].append("shape")
                        why.append("the part changed since the sheet was drawn (%s)" % signature_change(sig_s, sig_b))
                    if "content" not in fs or "spec_hash" not in fs:
                        rec["changed"].append("untracked")
                        why.append("drawn before sheets recorded what they show (holes, fits, threads) and the "
                                   "drawing spec — regenerate it to be sure it matches")
                    else:
                        try:
                            cp = content_parts(part_b, shape_b)
                        except Exception as e:  # noqa: BLE001 — never current when it cannot be compared
                            cp = {"error": str(e)}
                        if "error" in cp:
                            rec["changed"].append("content")
                            why.append("could not compare what the sheet shows (%s) — regenerate it" % cp["error"])
                        elif content_hash(cp) != fs["content"]:
                            rec["changed"].append("content")
                            why.append(content_change(fs.get("content_parts"), cp) or "holes, fits or threads changed")
                        if fs.get("spec_hash") != sh.get("spec_hash"):
                            rec["changed"].append("spec")
                            why.append("material/finish/notes, title block or drawing options changed since the "
                                       "sheet was drawn" if sh.get("spec_hash") else
                                       "the drawing spec was not compared (no spec_hash passed)")
                    if why:
                        rec["detail"] = "stale — " + "; ".join(why)
                    else:
                        rec["current"] = True
                        rec["detail"] = "current — the part, what the sheet shows of it and the drawing spec are " \
                                        "unchanged since the sheet was drawn"
            finally:
                App.closeDocument(doc.Name)
        out.append(rec)
        texts.append("  %s %-14s %s  %s%s" % ("✓" if rec["current"] else "✗", label, os.path.basename(path or "-"),
                                              rec["detail"], (" (title block rev %s)" % rec["revision"])
                                              if rec["revision"] else ""))
    App.closeDocument(build.Name)
    return {"sheets": out, "text": "SHEETS vs %s\n%s" % (os.path.basename(args["build"]), "\n".join(texts))}


def op_export(args):
    """Export a saved sheet from a temporary copy (title_fields applied to the
    copy): the document itself is never saved or touched."""
    tmp = tempfile.mkdtemp(prefix="fcmcp-export-")
    try:
        src = os.path.join(tmp, os.path.basename(args["doc"]))
        shutil.copyfile(args["doc"], src)
        doc = App.openDocument(src)
        pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
        if not pages:
            raise DrawingError("no TechDraw pages in %s" % args["doc"])
        page = find_page(pages, args.get("page"))
        settle(doc, [page])
        short = []
        if args.get("title_fields"):
            unknown, short, _ = set_title(page.Template, args["title_fields"])
            if unknown:
                raise DrawingError("title block has no field %s — fields: %s" % (
                    ", ".join(sorted(unknown)), ", ".join(sorted(dict(page.Template.EditableTexts)))))
            doc.recompute()
            pump(5)
        os.makedirs(args["out_dir"], exist_ok=True)
        label = page.Label
        revision = dict(getattr(page.Template, "EditableTexts", {}) or {}).get("revision_index", "") \
            if page.Template is not None else ""
        name = args.get("name") or re.sub(r"\W+", "_", label)
        files = export(page, os.path.join(args["out_dir"], name))
        App.closeDocument(doc.Name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"files": files, "images": [files["png"]] if files.get("png") else [], "revision": revision,
            "text": "EXPORT %s (%s, revision %s%s)\n  files  %s%s" % (
                label, os.path.basename(args["doc"]), revision or "-", ", title block %s" % ", ".join(
                    "%s=%s" % kv for kv in sorted(args["title_fields"].items())) if args.get("title_fields") else "",
                " · ".join(os.path.basename(f) for f in files.values()),
                "\n  title  shortened to fit: %s" % shortened_text(short) if short else "")}


OPS = {"create": op_create, "read": op_read, "edit": op_edit, "sheet_status": op_sheet_status, "export": op_export}


def set_preferences():
    """TechDraw settings for this private config: ISO referencing dimensions
    (horizontal text), no units on dimensions, 3.5 mm text."""
    dims = App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/Dimensions")
    dims.SetInt("StandardAndStyle", 1)
    dims.SetBool("ShowUnits", False)
    dims.SetFloat("FontSize", DIM_FONT)
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/General").SetInt("ProjectionAngle", 1)


def main():
    set_preferences()
    req_path, resp_path = os.environ["FCMCP_REQ"], os.environ["FCMCP_RESP"]
    with open(req_path) as fh:
        req = json.load(fh)
    t0 = time.time()
    try:
        # the server starts FreeCAD as a background process with no splash; TechDraw's page view still
        # activates the main window, so keep it fully transparent (showMinimized() would animate it)
        mw = Gui.getMainWindow()
        if mw is not None:
            mw.setWindowOpacity(0.0)
        resp = {"ok": True}
        if req["op"] not in OPS:
            raise DrawingError("unknown drawing op %r — %s" % (req["op"], ", ".join(OPS)))
        resp.update(OPS[req["op"]](req.get("args") or {}))
    except (DrawingError, an.RefError, fc.FcError) as e:
        resp = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "traceback": traceback.format_exc()[-4000:]}
    resp["worker_seconds"] = round(time.time() - t0, 3)
    with open(resp_path, "w") as fh:
        json.dump(resp, fh, default=str)


try:
    main()
finally:
    sys.stdout.flush()
    os._exit(0)
