# SPDX-License-Identifier: Apache-2.0
"""
Geometry analysis inside the FreeCAD worker: object introspection, geometry
references, measure, check_fit, section, checks.json evaluation and GUI-edit
change detection.

Every shape is taken in GLOBAL coordinates: an object's own Shape is relative
to its container (App::Part, Assembly), so placements are composed up the
container chain. Links (App::Link) are resolved through Part.getShape.
"""

import itertools
import json
import math
import os
import re

import FreeCAD as App
import Part

import fclib as fc

Vector = App.Vector


class RefError(Exception):
    pass


# ------------------------------------------------------------- formatting

def fmt(x, nd=2):
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return "{:,.{p}f}".format(x, p=1).replace(",", " ")
    return ("%." + str(nd) + "f") % x


def _g(x):
    """A number as typed: 8, 2.5, -0.25."""
    s = ("%.4f" % x).rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _pt(v, nd=2):
    return "%s, %s, %s" % (fmt(v.x, nd), fmt(v.y, nd), fmt(v.z, nd))


def _axis_name(v):
    for name, d in (("+X", (1, 0, 0)), ("-X", (-1, 0, 0)), ("+Y", (0, 1, 0)), ("-Y", (0, -1, 0)),
                    ("+Z", (0, 0, 1)), ("-Z", (0, 0, -1))):
        if v.dot(Vector(*d)) > 1 - 1e-6:
            return name
    return "(%.3f, %.3f, %.3f)" % (v.x, v.y, v.z)


def _canon(a):
    """Direction with its dominant component positive (axes have no sign)."""
    comps = [a.x, a.y, a.z]
    k = max(range(3), key=lambda i: abs(comps[i]))
    v = Vector(a)
    v.normalize()
    return v * (-1 if comps[k] < 0 else 1)


# ---------------------------------------------------------- introspection

_SKIP_TYPES = ("Sketcher::", "Spreadsheet::", "TechDraw::", "App::Origin", "App::Line",
               "App::Plane", "App::Point", "PartDesign::Plane", "PartDesign::Line",
               "PartDesign::Point", "PartDesign::CoordinateSystem", "App::DocumentObjectGroup",
               "App::Part", "Part::Part2DObject", "App::LocalCoordinateSystem", "Assembly::")
_GROUPS = ("App::Part", "App::DocumentObjectGroup", "Assembly::AssemblyObject", "Assembly::JointGroup")
IGNORE_VISIBILITY = False      # set by the GUI drawing worker


def has_shape(o):
    try:
        return hasattr(o, "Shape") and not o.Shape.isNull()
    except Exception:  # noqa: BLE001
        return False


def parent_of(o):
    try:
        return o.getParentGeoFeatureGroup()
    except Exception:  # noqa: BLE001
        return None


def role(o):
    return getattr(o, "FcRole", "") or "part"


def is_result(o):
    """Finished geometry: Bodies, standalone Part features, links and
    reference parts — not sketches, datums, features inside a Body, boolean
    inputs, or the source object of a link."""
    if not has_shape(o) or o.TypeId.startswith(_SKIP_TYPES):
        return False
    par = parent_of(o)
    if o.TypeId != "PartDesign::Body" and par is not None and par.TypeId == "PartDesign::Body":
        return False
    for p in o.InList:
        t = p.TypeId
        if t.startswith(("Sketcher::", "TechDraw::", "Spreadsheet::")) or t in _GROUPS:
            continue
        if t == "App::FeaturePython" and not has_shape(p):     # assembly joints
            continue
        if t == "App::Link":
            return False                                         # a link's source
        if t == "PartDesign::Body":
            if getattr(p, "BaseFeature", None) == o:
                return False
            continue
        if has_shape(p):
            return False
    return IGNORE_VISIBILITY or getattr(o, "Visibility", True) or role(o) == "ref"


def results(doc):
    return [o for o in doc.Objects if is_result(o)]


def tight_bb(shape):
    """Exact bounding box (BoundBox is padded by tolerances, e.g. 10.0029 for a
    Ø20 cylinder with a cross hole)."""
    try:
        bb = shape.optimalBoundingBox(False, False)
    except Exception:  # noqa: BLE001
        return shape.BoundBox
    r = lambda v: 0.0 if abs(v) < 1e-6 else v  # noqa: E731
    return App.BoundBox(r(bb.XMin), r(bb.YMin), r(bb.ZMin), r(bb.XMax), r(bb.YMax), r(bb.ZMax))


def global_placement(o):
    pl = App.Placement(o.Placement) if hasattr(o, "Placement") else App.Placement()
    p = parent_of(o)
    while p is not None:
        if hasattr(p, "Placement"):
            pl = p.Placement.multiply(pl)
        p = parent_of(p)
    return pl


def global_shape(o):
    """o's shape in global coordinates (container placements applied)."""
    s = Part.getShape(o).copy()
    acc = App.Placement()
    p = parent_of(o)
    while p is not None:
        if hasattr(p, "Placement") and p.TypeId != "PartDesign::Body":
            acc = p.Placement.multiply(acc)
        p = parent_of(p)
    if not acc.isIdentity():
        s.Placement = acc.multiply(s.Placement)
    return s


def find(doc, ref):
    o = doc.getObject(ref)
    if o is not None:
        return o
    hits = doc.getObjectsByLabel(ref)
    if hits:
        return hits[0]
    for o in doc.Objects:
        if o.Label.lower() == str(ref).lower():
            return o
    raise RefError("no object '%s' — objects: %s" % (ref, ", ".join(o.Label for o in results(doc)) or "(none)"))


def get_frame(o, name):
    data = json.loads(getattr(o, "FcFrames", "") or "{}")
    if name not in data:
        raise RefError("%s has no frame '%s' (frames: %s)" % (o.Label, name, ", ".join(data) or "none"))
    x, y, z, a, b, c, d = data[name]
    return global_placement(o).multiply(App.Placement(Vector(x, y, z), App.Rotation(a, b, c, d)))


# ------------------------------------------------------------------- holes

def _axis_point(s):
    a = s.Axis
    c = s.Center
    return c - a * c.dot(a)


def _is_concave(f):
    """Cylinder or cone face whose material lies outside it (a bore, not a shaft)."""
    s = f.Surface
    u0, u1, v0, v1 = f.ParameterRange
    u, v = (u0 + u1) / 2, (v0 + v1) / 2
    p = f.valueAt(u, v)
    n = f.normalAt(u, v)
    ap = _axis_point(s)
    radial = p - ap - s.Axis * (p - ap).dot(s.Axis)
    return n.dot(radial) < 0


def _perp(a):
    """A unit vector perpendicular to a."""
    x = a.cross(Vector(1, 0, 0)) if abs(a.x) < 0.9 else a.cross(Vector(0, 1, 0))
    x.normalize()
    return x


def _turn(faces, a, p):
    """Fraction of a full turn about the axis (p, a) that cylindrical faces
    cover together (union of their angular spans, in 0.5° bins)."""
    x = _perp(a)
    y = a.cross(x)
    bins = set()
    for f in faces:
        u0, u1, v0, v1 = f.ParameterRange
        ang = []
        for u in (u0, u0 + min(0.1, (u1 - u0) / 2)):
            w = f.valueAt(u, (v0 + v1) / 2) - p
            ang.append(math.degrees(math.atan2(w.dot(y), w.dot(x))))
        start = ang[0]
        sign = 1 if (ang[1] - ang[0]) % 360 < 180 else -1      # which way u turns about a
        for k in range(int(round(math.degrees(u1 - u0) * 2))):
            bins.add(int(math.floor((start + sign * (k + 0.5) / 2) * 2)) % 720)
    return len(bins) / 720.0


def _t_range(f, p, a):
    """Extent of a coaxial cylinder face along the axis (p, a)."""
    u0, u1, v0, v1 = f.ParameterRange
    ts = [(f.valueAt(u0, v) - p).dot(a) for v in (v0, v1)]
    return min(ts), max(ts)


def _hole_face_t(f, p, a):
    """(t0, t1, tip) of a hole face along the axis (p, a): the extent of its
    vertices and parameter range without a drill point's apex, which is
    returned as tip (None for cylinders, countersinks and chamfers)."""
    tip = None
    if f.Surface.__class__.__name__ == "Cone" and f.distToShape(Part.Vertex(f.Surface.Apex))[0] < 1e-4:
        tip = (f.Surface.Apex - p).dot(a)
    ts = [(v.Point - p).dot(a) for v in f.Vertexes] + list(_t_range(f, p, a))
    ts = [t for t in ts if tip is None or abs(t - tip) > 1e-6] or ts
    return min(ts), max(ts), tip


_HOLE_TURN = 0.55    # a hole wraps more than half a turn: fillets (≤ ½) and slot ends (½) are not holes


def detect_holes(shape):
    """Concave cylinders (and coaxial concave cones: drill points,
    countersinks, internal chamfers) grouped by axis and split into unbroken
    runs along it (coaxial holes in two lugs are two holes). A run is a hole
    when its smallest-diameter wall wraps round the axis (D-bores and keyed
    bores count; fillets, pocket corners and slot ends do not). Each hole:
    diameters (several = counterbore or step), extent along the axis, through
    or blind; depth is the full-diameter depth and a drill point's tip length
    is 'tip'; 'wall' is the whole hole surface (cylinders and cones)."""
    groups = []
    for i, f in enumerate(shape.Faces):
        s = f.Surface
        k = s.__class__.__name__
        if k not in ("Cylinder", "Cone") or not _is_concave(f):
            continue                       # external chamfers and shafts are not part of a hole
        a = _canon(s.Axis)
        p = _axis_point(s) if k == "Cylinder" else s.Apex - a * s.Apex.dot(a)
        for g in groups:
            if abs(abs(g["axis"].dot(a)) - 1) < 1e-6 and \
                    ((p - g["point"]) - g["axis"] * (p - g["point"]).dot(g["axis"])).Length < 1e-4:
                g["faces"].append((i, f))
                break
        else:
            groups.append({"axis": a, "point": p, "faces": [(i, f)]})
    out = []
    bb = shape.BoundBox
    eps = max(bb.DiagonalLength * 1e-4, 1e-3)
    for g in groups:
        a, p = g["axis"], g["point"]
        runs = []                                   # split the axis into its separate axial runs
        for i, f in g["faces"]:
            t0, t1, tip = _hole_face_t(f, p, a)
            lo, hi = min([t0] + ([tip] if tip is not None else [])), max([t1] + ([tip] if tip is not None else []))
            runs.append({"lo": lo, "hi": hi, "faces": [(i, f, t0, t1, tip)]})
        runs.sort(key=lambda r: r["lo"])
        merged = []
        for r in runs:
            if merged and r["lo"] <= merged[-1]["hi"] + 1e-5:
                merged[-1]["faces"] += r["faces"]
                merged[-1]["hi"] = max(merged[-1]["hi"], r["hi"])
            else:
                merged.append(r)
        for run in merged:
            cyl = [(i, f) for i, f, _, _, _ in run["faces"] if f.Surface.__class__.__name__ == "Cylinder"]
            if not cyl:
                continue
            rmin = min(f.Surface.Radius for _, f in cyl)
            if _turn([f for _, f in cyl if f.Surface.Radius < rmin + 1e-6], a, p) < _HOLE_TURN:
                continue
            t0 = min(x[2] for x in run["faces"])
            t1 = max(x[3] for x in run["faces"])
            lo, hi = run["lo"], run["hi"]                       # with a drill point's tip
            open_lo = not shape.isInside(p + a * (lo - eps), 1e-6, True)
            open_hi = not shape.isInside(p + a * (hi + eps), 1e-6, True)
            tip = max(t0 - lo, hi - t1)
            diams = sorted(set(round(2 * f.Surface.Radius, 4) for _, f in cyl))
            out.append({"diameters": diams, "axis": a, "axis_name": _axis_name(a), "point": p,
                        "at": p + a * ((t0 + t1) / 2), "depth": t1 - t0, "tip": tip if tip > 1e-6 else 0.0,
                        "tip_at": (p + a * (lo if t0 - lo > hi - t1 else hi)) if tip > 1e-6 else None,
                        "span": (t0, t1),
                        "through": open_lo and open_hi, "from": p + a * t1, "to": p + a * t0,
                        "opens": "both" if open_lo and open_hi else ("+" if open_hi else "-"),
                        "faces": ["Face%d" % (i + 1) for i in sorted(x[0] for x in run["faces"])],
                        "wall": Part.makeCompound([f for _, f, _, _, _ in run["faces"]]),
                        "segments": [(2 * f.Surface.Radius,) + _t_range(f, p, a) for _, f in cyl]})
    out.sort(key=lambda h: (h["axis_name"], round(h["at"].z, 3), round(h["at"].y, 3), round(h["at"].x, 3)))
    return out


def detect_bosses(shape):
    """Convex cylinders that wrap round their axis — shafts, pilots, pins,
    bosses, a turned part's diameters — one per axis, diameter and unbroken
    run along the axis (fillets and rounded edges wrap ≤ ¼ turn and are not
    bosses). Same record layout as detect_holes, one diameter each."""
    groups = []
    for i, f in enumerate(shape.Faces):
        s = f.Surface
        if s.__class__.__name__ != "Cylinder" or _is_concave(f):
            continue
        a, p, r = _canon(s.Axis), _axis_point(s), s.Radius
        for g in groups:
            if abs(g["r"] - r) < 1e-6 and abs(abs(g["axis"].dot(a)) - 1) < 1e-6 and \
                    ((p - g["point"]) - g["axis"] * (p - g["point"]).dot(g["axis"])).Length < 1e-4:
                g["faces"].append((i, f))
                break
        else:
            groups.append({"axis": a, "point": p, "r": r, "faces": [(i, f)]})
    out = []
    for g in groups:
        a, p = g["axis"], g["point"]
        runs = []                                   # split one diameter into its separate axial runs
        for i, f in sorted(g["faces"], key=lambda x: _t_range(x[1], p, a)):
            t0, t1 = _t_range(f, p, a)
            if runs and t0 <= runs[-1]["t1"] + 1e-6:
                runs[-1]["faces"].append((i, f))
                runs[-1]["t1"] = max(runs[-1]["t1"], t1)
            else:
                runs.append({"t0": t0, "t1": t1, "faces": [(i, f)]})
        for run in runs:
            if _turn([f for _, f in run["faces"]], a, p) < _HOLE_TURN:
                continue
            t0, t1, d = run["t0"], run["t1"], round(2 * g["r"], 4)
            out.append({"diameters": [d], "axis": a, "axis_name": _axis_name(a), "point": p,
                        "at": p + a * ((t0 + t1) / 2), "depth": t1 - t0, "length": t1 - t0, "through": False,
                        "span": (t0, t1),
                        "from": p + a * t1, "to": p + a * t0, "faces": ["Face%d" % (i + 1) for i, _ in run["faces"]],
                        "wall": Part.makeCompound([f for _, f in run["faces"]]), "segments": [(d, t0, t1)]})
    out.sort(key=lambda h: (h["axis_name"], round(h["at"].z, 3), round(h["at"].y, 3), round(h["at"].x, 3),
                            h["diameters"][0]))
    return out


def _hole_features(o):
    """PartDesign::Hole features of o's Body: Diameter (as modelled), thread
    size and major Ø when tapped, print compensation, and the hole centres
    in global coordinates (None when they cannot be read)."""
    if o is None or o.TypeId != "PartDesign::Body":
        return []
    out = []
    for h in o.Group:
        if h.TypeId != "PartDesign::Hole":
            continue
        try:
            dia = float(h.Diameter.Value)
        except Exception:  # noqa: BLE001
            continue
        thread, major = None, None
        if getattr(h, "Threaded", False):
            m = re.match(r"^M(\d+(?:\.\d+)?)", str(getattr(h, "ThreadSize", "")))
            if m:
                thread, major = str(h.ThreadSize).replace("x", "×"), float(m.group(1))
        centres = None
        try:
            sk = h.Profile[0]
            pl = sk.getGlobalPlacement()
            centres = [pl.multVec(g.Center) for g in sk.Geometry if g.__class__.__name__ == "Circle"]
        except Exception:  # noqa: BLE001
            pass
        out.append({"feature": h, "d": dia, "thread": thread, "major": major, "centres": centres,
                    "comp": float(getattr(h, "FcHoleComp", 0.0) or 0.0)})
    return out


def _feature_for(o, h, feats=None):
    """The Hole feature that made detected hole h (its modelled Ø matches the
    hole's smallest Ø and, when known, a profile centre lies on its axis)."""
    d = h["diameters"][0]
    best = None
    for f in _hole_features(o) if feats is None else feats:
        if abs(f["d"] - d) > 0.011:
            continue
        if f["centres"]:
            on_axis = any(((c - h["point"]) - h["axis"] * (c - h["point"]).dot(h["axis"])).Length < 1e-3
                          for c in f["centres"])
            if not on_axis:
                continue
        if best is None or (f["major"] or 0) > (best["major"] or 0):      # same drill, two sizes: worst case
            best = f
    return best


# -------------------------------------------------------- geometry refs

class Entity:
    """A resolved reference. kind: object | face | edge | vertex | holes |
    bosses | frame | point | plane | axis."""

    def __init__(self, kind, label, **kw):
        self.kind, self.label = kind, label
        self.__dict__.update(kw)

    def feats(self):
        """The holes or bosses of a holes/bosses entity (same record layout)."""
        return self.holes if self.kind == "holes" else self.bosses if self.kind == "bosses" else []

    # what this entity can act as
    def as_shape(self):
        if self.kind in ("object", "face", "edge", "vertex"):
            return self.shape
        if self.kind in ("holes", "bosses"):
            return Part.makeCompound([h["wall"] for h in self.feats()])
        if self.kind == "point":
            return Part.Vertex(self.point)
        raise RefError("%s (%s) has no shape to measure against" % (self.label, self.kind))

    def has_shape(self):
        return self.kind not in ("axis", "frame", "plane")

    def as_axis(self):
        """(point, unit direction, diameter or None) or None."""
        if self.kind in ("holes", "bosses") and len(self.feats()) == 1:
            h = self.feats()[0]
            return h["point"], h["axis"], h["diameters"][0]
        if self.kind == "axis":
            return self.point, self.direction, None
        if self.kind == "frame":
            return self.placement.Base, self.placement.Rotation.multVec(Vector(0, 0, 1)), None
        if self.kind == "face" and self.shape.Surface.__class__.__name__ == "Cylinder":
            s = self.shape.Surface
            return _axis_point(s), _canon(s.Axis), 2 * s.Radius
        if self.kind == "edge":
            c = self.shape.Curve.__class__.__name__
            if c in ("Line", "LineSegment"):
                p0, p1 = self.shape.Vertexes[0].Point, self.shape.Vertexes[-1].Point
                d = p1 - p0
                d.normalize()
                return p0, d, None
            if c == "Circle":
                return self.shape.Curve.Center, _canon(self.shape.Curve.Axis), 2 * self.shape.Curve.Radius
        return None

    def as_plane(self):
        """(point, unit normal) or None."""
        if self.kind == "plane":
            return self.point, self.normal
        if self.kind == "frame":
            return self.placement.Base, self.placement.Rotation.multVec(Vector(0, 0, 1))
        if self.kind == "face" and self.shape.Surface.__class__.__name__ == "Plane":
            f = self.shape
            u0, u1, v0, v1 = f.ParameterRange
            return f.CenterOfMass, f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        return None

    def as_point(self):
        if self.kind == "point":
            return self.point
        if self.kind == "vertex":
            return self.shape.Point
        if self.kind == "frame":
            return self.placement.Base
        return None


_NUM = r"-?\d+(?:\.\d+)?(?:e-?\d+)?"


def _parse_args(s):
    """'+Z, at=8, contains=[0,0,25]' → (['+Z'], {'at': 8.0, 'contains': [0,0,25]})."""
    pos, kw = [], {}
    depth, cur, parts = 0, "", []
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    for p in parts:
        p = p.strip()
        if "=" in p:
            k, v = p.split("=", 1)
            kw[k.strip()] = _value(v.strip())
        else:
            pos.append(_value(p))
    return pos, kw


def _value(v):
    if v.startswith("["):
        return [float(x) for x in v.strip("[]").split(",") if x.strip()]
    if re.match("^%s$" % _NUM, v):
        return float(v)
    return v


def _vec(v):
    if isinstance(v, Vector):
        return v
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return Vector(*[float(c) for c in v])
    raise RefError("expected [x, y, z], got %r" % (v,))


_UNIT = {"X": Vector(1, 0, 0), "Y": Vector(0, 1, 0), "Z": Vector(0, 0, 1)}


def _dir(d):
    """A unit direction from '+X' … '-Z', a combination such as '+X+Y' or
    '-X+Z' (normalised), or a vector [x, y, z]."""
    if isinstance(d, Vector):
        v = Vector(d)
    elif isinstance(d, (list, tuple)) and len(d) == 3:
        v = Vector(*[float(c) for c in d])
    else:
        k = str(d).replace(" ", "").upper()
        toks = re.findall(r"([+-]?)([XYZ])", k)
        if not toks or "".join(sg + ax for sg, ax in toks) != k:
            raise RefError("direction must be +X -X +Y -Y +Z -Z, a combination such as +X+Y or -X+Z, "
                           "or a vector [x, y, z] — got %r" % (d,))
        v = Vector()
        for sg, ax in toks:
            v += _UNIT[ax] * (-1 if sg == "-" else 1)
    if v.Length < 1e-9:
        raise RefError("direction %r has zero length — use e.g. +X+Y or [1, 1, 0]" % (d,))
    v.normalize()
    return v


def resolve(doc, ref):
    """Turn a reference (string or [x,y,z]) into an Entity in global coordinates."""
    if isinstance(ref, (list, tuple)):
        return Entity("point", "[%s]" % ", ".join(fmt(float(c), 3) for c in ref), point=_vec(ref))
    r = str(ref).strip()
    m = re.match(r"^\[(.*)\]$", r)
    if m:
        return resolve(doc, [float(x) for x in m.group(1).split(",")])
    m = re.match(r"^plane\((.*)\)$", r)
    if m:
        pos, kw = _parse_args(m.group(1))
        for ax, n in (("x", (1, 0, 0)), ("y", (0, 1, 0)), ("z", (0, 0, 1))):
            if ax in kw:
                p = [0.0, 0.0, 0.0]
                p["xyz".index(ax)] = float(kw[ax])
                return Entity("plane", r, point=Vector(*p), normal=Vector(*n))
        if len(pos) == 2:
            return Entity("plane", r, point=_vec(pos[0]), normal=_dir(pos[1]))
        raise RefError("plane(...) takes x=|y=|z= or ([point], normal) — normal [x, y, z] or +X, +X+Y …")
    m = re.match(r"^axis\((.*)\)$", r)
    if m:
        pos, _ = _parse_args(m.group(1))
        if len(pos) != 2:
            raise RefError("axis(...) takes ([point], direction) — direction [x, y, z] or +Z, +X+Y …")
        return Entity("axis", r, point=_vec(pos[0]), direction=_dir(pos[1]))
    # Object or Object.selector — object labels may contain dots, so try splits
    candidates = [(r, None)] + [(r[:i], r[i + 1:]) for i in range(len(r) - 1, 0, -1) if r[i] == "."]
    last_err = None
    for objref, sel in candidates:
        try:
            o = find(doc, objref)
        except RefError as e:
            last_err = e
            continue
        return _select(o, sel, r)
    raise last_err or RefError("cannot resolve %r" % ref)


def _select(o, sel, label):
    shape = global_shape(o)
    if not sel:
        return Entity("object", o.Label, obj=o, shape=shape)
    m = re.match(r"^(Face|Edge|Vertex)(\d+)$", sel)
    if m:
        kind, n = m.group(1), int(m.group(2))
        items = {"Face": shape.Faces, "Edge": shape.Edges, "Vertex": shape.Vertexes}[kind]
        if not 1 <= n <= len(items):
            raise RefError("%s has %d %ss" % (o.Label, len(items), kind.lower()))
        return Entity(kind.lower(), label, obj=o, shape=items[n - 1])
    m = re.match(r"^face\((.*)\)$", sel)
    if m:
        pos, kw = _parse_args(m.group(1))
        if not pos:
            raise RefError("face(...) needs a direction, e.g. face(+Z)")
        d = _dir(pos[0])
        hits, normals = [], []
        for i, f in enumerate(shape.Faces):
            if f.Surface.__class__.__name__ != "Plane":
                continue
            u0, u1, v0, v1 = f.ParameterRange
            n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            normals.append(_axis_name(n))
            if n.dot(d) < 1 - 1e-6:
                continue
            off = f.CenterOfMass.dot(d)
            if "at" in kw and abs(off - float(kw["at"])) > 1e-4:
                continue
            if "contains" in kw and not f.isInside(_vec(kw["contains"]), 1e-4, True):
                continue
            hits.append((-off, -f.Area, i, f))
        if not hits:
            raise RefError("%s has no planar face facing %s%s — its planar faces face %s; drop at=/contains= or "
                           "fix them, or give the normal as a vector [x, y, z]" % (
                               o.Label, pos[0], " matching %s" % kw if kw else "",
                               ", ".join("%s%s" % (n, " ×%d" % normals.count(n) if normals.count(n) > 1 else "")
                                         for n in sorted(set(normals))) or "nothing (no planar faces)"))
        hits.sort(key=lambda h: (h[0], h[1]))
        lab = "%s.Face%d" % (o.Label, hits[0][2] + 1)
        if len(hits) > 1 and "at" not in kw and "contains" not in kw:
            ax = _axis_name(d)
            k = ax[1].lower() if len(ax) == 2 else None          # an axis direction: at= ↔ that coordinate
            offs = []
            for h in hits:
                if all(abs(-h[0] - x[0]) > 1e-4 for x in offs):
                    offs.append((-h[0], h[3].CenterOfMass))
            lab += " (outermost of %d faces facing %s: %s; at= is the offset along the face normal — use at= or " \
                   "contains=[x, y, z] to pick another)" % (len(hits), pos[0], ", ".join(
                       "at=%s (%s)" % (_g(x), "%s=%s" % (k, _g(x * (1 if ax[0] == "+" else -1))) if k else
                                       "centre (%s)" % _pt(c)) for x, c in offs))
        return Entity("face", lab, obj=o, shape=hits[0][3])
    m = re.match(r"^(holes|bosses)(?:\((.*?)\))?(?:\[(\d+)\])?$", sel)
    if m:
        what = m.group(1)
        comp_only = []                       # holes whose modelled Ø (not their design Ø) equals d=
        hs = detect_holes(shape) if what == "holes" else detect_bosses(shape)
        if m.group(2):
            _, kw = _parse_args(m.group(2))
            bad = sorted(set(kw) - {"d", "axis"})
            if bad:
                raise RefError("%s(...) takes d= and axis= — got %s" % (what, ", ".join(bad)))
            if "d" in kw:
                want = float(kw["d"])
                feats = _hole_features(o) if what == "holes" else []
                keep = []
                for h in hs:
                    f = _feature_for(o, h, feats) if feats else None
                    if f and f["comp"]:
                        # a print-compensated hole answers to its design Ø only (holes(d=3.4) for an M3 clearance
                        # hole modelled at Ø3.6), so checks written with design values pick exactly those holes
                        if any(abs(d - f["comp"] - want) < 0.011 for d in h["diameters"]):
                            h["design_comp"] = f["comp"]
                            keep.append(h)
                        elif any(abs(d - want) < 0.011 for d in h["diameters"]):
                            comp_only.append("Ø%s (design Ø%s + %s compensation)" % (
                                fmt(h["diameters"][0], 3), _g(h["diameters"][0] - f["comp"]), _g(f["comp"])))
                    elif any(abs(d - want) < 0.011 for d in h["diameters"]):
                        keep.append(h)
                hs = keep
            if "axis" in kw:
                want = _canon(_dir(kw["axis"]))
                hs = [h for h in hs if abs(h["axis"].dot(want)) > 1 - 1e-6]
        if m.group(3) is not None:
            k = int(m.group(3))
            if k >= len(hs):
                raise RefError("%s has %d matching %s; index %d is out of range" % (label, len(hs), what, k))
            hs = [hs[k]]
        if not hs:
            if what == "holes":
                raise RefError("no holes match %s (inspect the object to list its holes)%s" % (label, (
                    " — %s carry print compensation and are modelled at %s; holes(d=) takes the design Ø of a "
                    "compensated hole" % ("these holes" if len(comp_only) > 1 else "a hole", ", ".join(
                        sorted(set(comp_only))))) if comp_only else ""))
            allb = detect_bosses(shape)
            raise RefError("no bosses match %s — %s's convex full-round cylinders: %s" % (
                label, o.Label, ", ".join(sorted(set("Ø%s axis %s" % (_g(b["diameters"][0]), b["axis_name"])
                                                     for b in allb))) or "none"))
        if what == "holes":
            return Entity("holes", label, obj=o, holes=hs, solid=shape)
        return Entity("bosses", label, obj=o, bosses=hs, solid=shape)
    m = re.match(r"^frame\((\w+)\)$", sel)
    if m:
        return Entity("frame", label, obj=o, placement=get_frame(o, m.group(1)))
    raise RefError("unknown selector '%s' — use FaceN/EdgeN/VertexN, face(+Z, at=…), holes, "
                   "holes(d=3.4)[0], bosses(d=22)[0] (shafts, pilots, pins), frame(name)" % sel)


# ----------------------------------------------------------------- measure

def _angle(d1, d2):
    c = max(-1.0, min(1.0, abs(d1.dot(d2))))
    return math.degrees(math.acos(c))


def _axis_offset(p1, d1, p2, d2):
    """Shortest distance between two lines (parallel-safe)."""
    w = p2 - p1
    cr = d1.cross(d2)
    if cr.Length < 1e-9:
        return (w - d1 * w.dot(d1)).Length
    return abs(w.dot(cr)) / cr.Length


def _density(dens, label):
    """dens is g/cm³, a callable label → g/cm³ (per-part density), or None."""
    try:
        v = dens(label) if callable(dens) else dens
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _describe(e, dens=None):
    if e.kind == "object":
        s = e.shape
        bb = tight_bb(s)
        out = {"bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax],
               "size": [bb.XLength, bb.YLength, bb.ZLength], "volume": s.Volume if s.Solids else 0.0}
        txt = "%s: bbox %s × %s × %s from (%s) to (%s), volume %s mm³" % (
            e.label, fmt(bb.XLength), fmt(bb.YLength), fmt(bb.ZLength), _pt(Vector(bb.XMin, bb.YMin, bb.ZMin)),
            _pt(Vector(bb.XMax, bb.YMax, bb.ZMax)), fmt(out["volume"], 1))
        if s.Solids:
            com = s.Solids[0].CenterOfMass if len(s.Solids) == 1 else None
            if com is not None:
                out["centroid"] = [com.x, com.y, com.z]
                txt += ", centroid (%s)" % _pt(com)
            dv = _density(dens, e.obj.Label)
            if dv and role(e.obj) != "ref":
                out["mass_g"] = out["volume"] * dv / 1000.0
                txt += ", mass %s g (%s g/cm³)" % (fmt(out["mass_g"], 1), fmt(dv, 2))
        return txt, out
    if e.kind == "holes":
        rows = []
        for k, h in enumerate(e.holes):
            rows.append("  [%d] Ø%s axis %s %s at (%s)%s" % (
                k, "/Ø".join(fmt(d, 3) for d in h["diameters"]), h["axis_name"], "THRU" if h["through"] else
                "blind ↧%s%s" % (fmt(h["depth"]), " (full-diameter depth; drill point tip +%s)" % fmt(h["tip"])
                                 if h.get("tip") else ""), _pt(h["at"]),
                "  — matched by its design Ø%s (modelled Ø%s with %s mm print compensation)" % (
                    _g(h["diameters"][0] - h["design_comp"]), fmt(h["diameters"][0], 3), _g(h["design_comp"]))
                if h.get("design_comp") else ""))
        return "%s: %d hole(s)\n%s" % (e.label, len(e.holes), "\n".join(rows)), {"count": len(e.holes)}
    if e.kind == "bosses":
        rows = ["  [%d] Ø%s axis %s, length %s from (%s) to (%s)" % (
            k, fmt(b["diameters"][0], 3), b["axis_name"], fmt(b["length"]), _pt(b["to"]), _pt(b["from"]))
            for k, b in enumerate(e.bosses)]
        return "%s: %d boss(es) — convex full-round cylinders\n%s" % (e.label, len(e.bosses), "\n".join(rows)), \
            {"count": len(e.bosses), "diameters": [b["diameters"][0] for b in e.bosses]}
    if e.kind == "frame":
        z = e.placement.Rotation.multVec(Vector(0, 0, 1))
        return "%s: origin (%s), +Z %s" % (e.label, _pt(e.placement.Base), _axis_name(z)), {}
    if e.kind == "face":
        pl = e.as_plane()
        ax = e.as_axis()
        if pl:
            return "%s: plane normal %s through (%s), area %s" % (e.label, _axis_name(pl[1]), _pt(pl[0]),
                                                                 fmt(e.shape.Area, 2)), {}
        if ax:
            return "%s: cylinder Ø%s axis %s through (%s)" % (e.label, fmt(ax[2], 3), _axis_name(ax[1]),
                                                              _pt(ax[0])), {}
        return "%s: %s face, area %s" % (e.label, e.shape.Surface.__class__.__name__, fmt(e.shape.Area, 2)), {}
    if e.kind in ("point", "vertex"):
        return "%s: (%s)" % (e.label, _pt(e.as_point(), 3)), {}
    return "%s (%s)" % (e.label, e.kind), {}


def _coaxial_cylinders(shape, p, d, max_offset):
    """Convex cylinders of shape whose axis is parallel to (p, d) and within
    max_offset: (diameter, axis offset, angle, t0, t1) with t0…t1 the
    cylinder's extent along (p, d)."""
    out = []
    for f in shape.Faces:
        s = f.Surface
        if s.__class__.__name__ != "Cylinder" or _is_concave(f):
            continue
        a = _canon(s.Axis)
        if _angle(a, d) > 0.5:
            continue
        off = _axis_offset(p, d, _axis_point(s), a)
        if off <= max_offset:
            out.append((2 * s.Radius, off, _angle(a, d)) + _t_range(f, p, d))
    return out


def _ray_runs(solid, rays, diag, face=None):
    """For rays [(wall point p, unit direction r)]: the solid run from p
    straight along r, (length, p, end) or None (ray starts in a void, or —
    with face — the run does not end on that face). One boolean for all rays."""
    segs = [Part.LineSegment(p + r * 1e-4, p + r * diag).toShape() for p, r in rays]
    try:
        groups = [solid.common(Part.makeCompound(segs)).Edges] if segs else []
    except Exception:  # noqa: BLE001 — a batch the boolean cannot take: one ray at a time
        groups = []
        for sg in segs:
            try:
                groups.append(solid.common(sg).Edges)
            except Exception:  # noqa: BLE001
                pass
    ends = [(e.Vertexes[0].Point, e.Vertexes[-1].Point) for g in groups for e in g if len(e.Vertexes) == 2]
    out = []
    for p, r in rays:
        run, s0 = None, p + r * 1e-4
        for v0, v1 in ends:
            for a, q in ((v0, v1), (v1, v0)):
                if (a - s0).Length < 2e-5 and (q - p).dot(r) > 0:
                    run = ((q - p).Length, p, q)
        if run and face is not None and face.distToShape(Part.Vertex(run[2]))[0] > 1e-3:
            run = None
        out.append(run)
    return out


def hole_wall(solid, h, face=None, n_ang=24, n_h=3, major=None):
    """Minimum material around hole h: rays cast radially outward from
    points on each wall face (cylinders and the hole's cones: countersinks,
    drill points) — n_ang round, n_h rows plus both ends of the wall — each
    measured to where it leaves the solid (with face: only rays that leave
    through that face), the best one refined in angle and height (4 rounds,
    each 4× finer). major: a thread's major Ø — a ray from inside it counts
    from the major Ø (the thread cuts out to it). A blind hole's floor
    counts too: a ray along the axis from its closed end (the drill point's
    tip). Returns (length, wall point, outside point, length from the
    modelled wall, floor?) or None."""
    diag = solid.BoundBox.DiagonalLength
    a, ap = h["axis"], h["point"]

    def ray(f, u, v):
        try:
            p = f.valueAt(u, v)
            if not f.isInside(p, 1e-4, True):
                return None
        except Exception:  # noqa: BLE001
            return None
        r = p - ap - a * (p - ap).dot(a)
        if r.Length < 1e-9:
            return None
        rho = r.Length
        r.normalize()
        return p, r, rho

    def eff(rr, rho):
        """(length, wall point) counted from the thread's major Ø when the ray starts inside it."""
        cut = max(0.0, major / 2.0 - rho) if major else 0.0
        if cut <= 0 or rr[0] <= 1e-9:
            return rr[0], rr[1]
        w = rr[2] - rr[1]
        w.normalize()
        return rr[0] - cut, rr[1] + w * cut
    samples = []
    for f in h["wall"].Faces:
        u0, u1, v0, v1 = f.ParameterRange
        n = max(4, int(round(n_ang * (u1 - u0) / (2 * math.pi))))
        du, dv = (u1 - u0) / n, (v1 - v0) / n_h
        ins = min(1e-5, (v1 - v0) / 100)             # the wall's ends, just inside so a ray runs through material
        vs = [v0 + ins] + [v0 + dv * (j + 0.5) for j in range(n_h)] + [v1 - ins]
        samples += [(f, u0 + (k + 0.5) * du, v, du, dv, v0 + ins, v1 - ins) for v in vs for k in range(n)]
    best = None
    for _ in range(5):
        rays = [ray(f, u, v) for f, u, v, _, _, _, _ in samples]
        idx = [i for i, rr in enumerate(rays) if rr]
        for i, rr in zip(idx, _ray_runs(solid, [rays[i][:2] for i in idx], diag, face)):
            if not rr:
                continue
            ln, wp = eff(rr, rays[i][2])
            if best is None or ln < best[0][0] - 1e-9:
                best = ((ln, wp, rr[2], rr[0]), samples[i])
        if best is None:
            break
        f, u, v, du, dv, vlo, vhi = best[1]
        du, dv = du / 4, dv / 4
        samples = [(f, u + i * du, min(vhi, max(vlo, v + j * dv)), du, dv, vlo, vhi)
                   for i in (-2, -1, 0, 1, 2) for j in (-2, -1, 0, 1, 2) if i or j]
    if not h.get("through"):                             # the floor under the closed end
        lo = h.get("opens") == "+"
        c = h["tip_at"] if h.get("tip_at") is not None else ap + a * h["span"][0 if lo else 1]
        run = _ray_runs(solid, [(c, a * (-1 if lo else 1))], diag, face)[0]
        if run and (best is None or run[0] < best[0][0] - 1e-9):
            return run[0], run[1], run[2], run[0], True
    return best[0] + (False,) if best else None


def _same_obj(a, b):
    oa, ob = getattr(a, "obj", None), getattr(b, "obj", None)
    return oa is not None and ob is not None and oa.Name == ob.Name and oa.Document.Name == ob.Document.Name


def _hole_pair(h, o):
    """Measure holes h against o as hole ↔ shape: o is the part that has the
    hole (or a face of it), or one hole against another object or a planar
    face (a cylindrical face of another part is measured axis ↔ axis)."""
    if o.kind not in ("object", "face"):
        return False
    return _same_obj(h, o) or (len(h.holes) == 1 and not o.as_axis())


def _concave_axis(e):
    """True for a hole (or a concave cylindrical face), False for a boss or
    shaft (a convex one), None when unknown (circle edges, axis(), frames)."""
    if e.kind in ("holes", "bosses"):
        return e.kind == "holes"
    if e.kind == "face" and e.shape.Surface.__class__.__name__ == "Cylinder":
        return _is_concave(e.shape)
    return None


def _axial_cyls(e, p, a):
    """[(Ø, t0, t1)] of a hole's diameter steps, a boss's run or a
    cylindrical face along the axis (p, a)."""
    if e.kind in ("holes", "bosses"):
        h = e.feats()[0]
        return [(d,) + tuple(sorted((h["point"] + h["axis"] * s - p).dot(a) for s in (s0, s1)))
                for d, s0, s1 in h["segments"]]
    return [(2 * e.shape.Surface.Radius,) + _t_range(e.shape, p, a)]


def _is_plane(e):
    """A real plane (planar face or plane()) — a frame acts as an axis next to one."""
    return e.kind == "plane" or (e.kind == "face" and e.shape.Surface.__class__.__name__ == "Plane")


def _axis_plane_text(p, d, q, n, ang_tol):
    """Angle between an axis (p, d) and a plane (q, n): 0° = the axis lies
    parallel to the plane, 90° = along its normal. Returns (text, data)."""
    ang = 90.0 - _angle(d, n)
    dist = abs((p - q).dot(n))
    data = {"angle_deg": ang, "parallel": ang <= ang_tol, "normal": 90.0 - ang <= ang_tol}
    if data["parallel"]:
        data["axis_distance"] = dist
        return "axis lies parallel to the plane (%s°), %s mm from it" % (fmt(ang, 3), fmt(dist, 4)), data
    hit = p + d * ((q - p).dot(n) / d.dot(n))
    data["meets"] = [hit.x, hit.y, hit.z]
    if data["normal"]:
        return "axis is normal to the plane (%s°), meets it at (%s)" % (fmt(ang, 3), _pt(hit, 3)), data
    return "axis at %s° to the plane (0° = parallel, 90° = normal), meets it at (%s)" % (
        fmt(ang, 3), _pt(hit, 3)), data


def _touching_face(shape, q, e):
    """The face of entity e (shape) at point q, described: 'Motor.Face3 (plane +Z)'."""
    if e.kind == "face":
        return e.label
    hits = []
    for f in shape.Faces:
        bb = f.BoundBox
        bb.enlarge(1e-4)
        if bb.isInside(q) and f.distToShape(Part.Vertex(q))[0] < 1e-6:
            hits.append(f)
    if not hits:
        return e.label
    hits.sort(key=lambda f: f.Surface.__class__.__name__ != "Plane")      # a mounting face reads best
    return _face_desc(hits[0], e.obj.Label if getattr(e, "obj", None) is not None else e.label, shape)


def _hole_vs_shape(a, b, ang_tol):
    """A hole (a) against a face or object (b): the material around the hole
    when b is the part that has the hole, else the wall gap plus any coaxial
    cylinder inside the hole (a screw shank, a shaft)."""
    data = {"a": a.label, "b": b.label}
    pl = b.as_plane() if _is_plane(b) else None

    def axis_line(k):
        """With a plane: the hole axis against it (parallel/perpendicular checks read angle_deg)."""
        if not pl:
            return []
        txt, dd = _axis_plane_text(a.holes[k]["point"], a.holes[k]["axis"], pl[0], pl[1], ang_tol)
        data.update(dd)
        return ["  hole%s axis ↔ plane: %s" % (" [%d]" % k if len(a.holes) > 1 else "", txt)]
    if _same_obj(a, b):
        solid = getattr(a, "solid", None) or global_shape(a.obj)
        face = b.shape if b.kind == "face" else None
        feats = _hole_features(a.obj)
        rows, floors = [], set()
        for k, h in enumerate(a.holes):
            # a tapped hole is modelled at its tap drill: the thread cuts out to the major Ø
            f = _feature_for(a.obj, h, feats)
            if not (f and f["major"] and f["major"] > h["diameters"][0] + 1e-6):
                f = None
            r = hole_wall(solid, h, face, major=f["major"] if f else None)
            if not r:
                continue
            if r[4]:
                floors.add(k)
            if f and r[3] - r[0] > 1e-9:
                rows.append((r[0], k, r[1], r[2], r[3], f))
            else:
                rows.append((r[0], k, r[1], r[2], None, None))
        if rows:
            t, k, p, q, t_drill, f = min(rows, key=lambda x: (x[0], x[1]))
            data.update(kind="hole_wall", min_wall=t, where=[[p.x, p.y, p.z], [q.x, q.y, q.z]], hole=k,
                        floor=k in floors)
            thr = ""
            if f:
                data.update(min_wall_drill=t_drill, thread=f["thread"], major_d=f["major"])
                thr = " from the M%s major Ø (%s mm from the Ø%s tap drill)" % (
                    _g(f["major"]), fmt(t_drill, 4), _g(a.holes[k]["diameters"][0]))
            lines = ["%s ↔ %s: minimum wall around the %s %s mm%s at (%s) → (%s) (%s%s)" % (
                a.label, b.label, "hole" if len(a.holes) == 1 else "holes (worst: [%d])" % k, fmt(t, 4),
                thr, _pt(p, 3), _pt(q, 3), "the floor under the blind end, along the axis from %s" % (
                    "the drill point's tip" if a.holes[k].get("tip_at") else "its bottom") if k in floors else
                "edge distance, measured straight out from the hole wall", " to this face" if face is not None
                else "")]
            if len(rows) > 1:
                lines += ["  [%d] Ø%s at (%s): %s mm%s%s" % (
                    kk, fmt(a.holes[kk]["diameters"][0], 3), _pt(a.holes[kk]["at"]), fmt(tt, 4),
                    " from the M%s major Ø (%s from the tap drill)" % (_g(ff["major"]), fmt(td, 4)) if ff else "",
                    " (floor)" if kk in floors else "")
                    for tt, kk, _, _, td, ff in sorted(rows, key=lambda r: r[1])]
            if any(r[5] for r in rows):
                lines.append("  tapped: the wall is taken from the thread's major Ø (distance checks use it)")
            return "\n".join(lines + axis_line(k)), data
        # no radial ray reaches this face: it caps the hole or lies beyond its ends
        d, _, _ = Part.makeCompound([h["wall"] for h in a.holes]).distToShape(b.shape)
        data.update(kind="hole_shape", wall_gap=d, hole_d=a.holes[0]["diameters"][0])
        return "\n".join(["%s ↔ %s: no material between the hole wall and this face straight out from the "
                          "hole (the hole opens on it or the face lies beyond its ends); nearest distance from "
                          "the hole wall %s mm" % (a.label, b.label, fmt(d, 4))] + axis_line(0)), data
    h = a.holes[0]
    sb = b.as_shape()
    d, pts, _ = h["wall"].distToShape(sb)
    data.update(kind="hole_shape", wall_gap=d, hole_d=h["diameters"][0])
    lines = ["%s ↔ %s: nearest distance from the hole wall %s mm" % (a.label, b.label, fmt(d, 4))]
    touch = ""
    if d < 1e-6 and pts:
        # where the wall touches: at an end of the wall (the rim, e.g. on a mounting face) or inside the hole
        t = (pts[0][0] - h["point"]).dot(h["axis"])
        rim = min(abs(t - h["span"][0]), abs(t - h["span"][1])) < 1e-3
        data["rim_contact"] = rim
        touch = " — the hole %s touches %s at (%s)" % ("rim" if rim else "wall", _touching_face(sb, pts[0][1], b),
                                                      _pt(pts[0][1], 3))
        lines[0] += touch
    cyl = _coaxial_cylinders(sb, h["point"], h["axis"], h["diameters"][0] / 2)
    if cyl:
        near = min(cyl, key=lambda c: c[1])
        data.update(offset=near[1], angle_deg=near[2])
        # cylinders inside the hole's depth, each against the hole diameter(s) it passes through
        pairs = []
        for c in cyl:
            for sd, s0, s1 in h["segments"]:
                if min(c[4], s1) - max(c[3], s0) > 1e-4:
                    pairs.append(((sd - c[0]) / 2 - c[1], c, sd))
        outside = sorted(set(round(c[0], 4) for c in cyl) - set(round(p[1][0], 4) for p in pairs))
        if pairs:
            rg, c, sd = min(pairs, key=lambda p: p[0])
            data.update(inner_d=c[0], hole_d=sd, offset=c[1], angle_deg=c[2], radial_gap_min=rg)
            if touch and rg > 1e-6:
                lines[0] += "; inside the hole the radial gap is %s mm (not an interference)" % fmt(rg, 4)
            lines.append("  coaxial Ø%s inside Ø%s: axis offset %s, minimum radial gap %s mm%s" % (
                fmt(c[0], 3), fmt(sd, 3), fmt(c[1], 4), fmt(rg, 4), "  ✗ OVERLAP" if rg < -1e-6 else ""))
            if outside:
                lines.append("  (coaxial Ø%s outside the hole's depth ignored — e.g. a screw head on the face)"
                             % "/Ø".join(fmt(x, 3) for x in outside))
        else:
            lines.append("  no coaxial cylinder inside the hole (coaxial Ø%s lies outside its depth, axis offset %s)"
                         % ("/Ø".join(fmt(x, 3) for x in outside), fmt(near[1], 4)))
    elif b.kind == "object":
        lines.append("  no coaxial cylinder inside the hole")
    return "\n".join(lines + axis_line(0)), data


def pattern_match(ha, hb):
    pairs = []
    used = set()
    cand = []
    for i, a in enumerate(ha):
        for j, b in enumerate(hb):
            if _angle(a["axis"], b["axis"]) > 1.0:
                continue
            cand.append((_axis_offset(a["point"], a["axis"], b["point"], b["axis"]), i, j))
    cand.sort()
    ua = set()
    for off, i, j in cand:
        if i in ua or j in used:
            continue
        ua.add(i)
        used.add(j)
        pairs.append((i, j, off, _angle(ha[i]["axis"], hb[j]["axis"])))
    pairs.sort()
    return pairs, [i for i in range(len(ha)) if i not in ua], [j for j in range(len(hb)) if j not in used]


def _swap_labels(txt, data, a, b):
    """A result measured as (b, a), relabelled in the order the user gave (a, b)."""
    for sep in (" ↔ ", " → "):
        head = "%s%s%s:" % (b.label, sep, a.label)
        if txt.startswith(head):
            txt = "%s ↔ %s:%s" % (a.label, b.label, txt[len(head):])
            break
    return txt, dict(data, a=a.label, b=b.label)


def _plane_bb(shape, p, u, v):
    """Exact bounding box of shape in plane coordinates: X along u, Y along v
    (Z along u × v), origin p — the shape mapped onto XY, then tight_bb."""
    w = u.cross(v)
    g = shape.copy()
    g.transformShape(App.Matrix(u.x, u.y, u.z, -p.dot(u), v.x, v.y, v.z, -p.dot(v), w.x, w.y, w.z, -p.dot(w),
                                0, 0, 0, 1))
    return tight_bb(g)


def _shape_axis(se, oe):
    """A shape against an axis line (axis(...), a frame's +Z): the minimum
    distance from the shape to the line and whether the line runs through the
    material. Returns (text, data)."""
    p, d, _ = oe.as_axis()
    s = se.as_shape()
    bb = s.BoundBox
    c = p + d * (bb.Center - p).dot(d)                 # the point of the axis abreast of the shape
    span = bb.DiagonalLength + 1.0
    line = Part.LineSegment(c - d * span, c + d * span).toShape()
    dist, pts, _ = s.distToShape(line)
    what = "the frame's +Z axis" if oe.kind == "frame" else "the axis"
    data = {"kind": "shape_axis", "distance": dist}
    txt = "minimum distance from %s (as an endless line) %s mm" % (what, fmt(dist, 4))
    if pts:
        q, r = pts[0]
        data["closest"] = [[q.x, q.y, q.z], [r.x, r.y, r.z]]
        txt += " at (%s)" % _pt(q, 3)
    if s.Solids:
        try:
            segs = [e for e in s.common(line).Edges if len(e.Vertexes) == 2]
        except Exception:  # noqa: BLE001
            segs = None
        if segs:
            ts = sorted((v.Point - p).dot(d) for e in segs for v in e.Vertexes)
            data.update(through=True, inside_length=sum(e.Length for e in segs))
            txt += " — the axis passes through %s's material over %s mm (%s … %s along it from (%s))" % (
                se.label, fmt(data["inside_length"], 3), fmt(ts[0], 3), fmt(ts[-1], 3), _pt(p))
        elif segs is not None:
            data["through"] = False
            txt += " — the axis %s %s's material" % ("touches" if dist < 1e-6 else "does not pass through",
                                                      se.label)
    return txt, data


def _shape_plane(se, oe):
    """A shape against a plane(...): its signed extent along the normal."""
    q, n = oe.as_plane()
    u = _perp(n)
    bb = _plane_bb(se.as_shape(), q, u, n.cross(u))
    lo, hi = bb.ZMin, bb.ZMax
    data = {"kind": "shape_plane", "signed_min": lo, "signed_max": hi}
    if lo < -1e-6 and hi > 1e-6:
        data["distance"] = 0.0
        return "the plane cuts %s: it spans %s … %s mm along the plane's normal (signed)" % (
            se.label, fmt(lo, 4), fmt(hi, 4)), data
    data["distance"] = min(abs(lo), abs(hi))
    return "%s lies %s … %s mm from the plane along its normal (signed) → minimum distance %s mm%s" % (
        se.label, fmt(lo, 4), fmt(hi, 4), fmt(data["distance"], 4),
        " (touching)" if data["distance"] < 1e-6 else ""), data


def measure_pair(a, b, tol=0.01, ang_tol=0.05):
    """Measure entity a against entity b; returns (text, data). Either order
    gives the same measurement, labelled in the order given."""
    data = {"a": a.label, "b": b.label}
    # hole / boss patterns
    if a.kind in ("holes", "bosses") and b.kind in ("holes", "bosses") and \
            (len(a.feats()) > 1 or len(b.feats()) > 1):
        pairs, ua, ub = pattern_match(a.feats(), b.feats())
        lines = ["%s ↔ %s: %s pattern, %d matched pair(s)" % (
            a.label, b.label, "hole" if a.kind == b.kind == "holes" else "axis", len(pairs))]
        for i, j, off, ang in pairs:
            ha, hb = a.feats()[i], b.feats()[j]
            lines.append("  A[%d] Ø%s ↔ B[%d] Ø%s   axis offset %s  angle %s°  at (%s)" % (
                i, fmt(ha["diameters"][0], 3), j, fmt(hb["diameters"][0], 3), fmt(off, 4), fmt(ang, 3), _pt(ha["at"])))
        mx = max([p[2] for p in pairs] or [0.0])
        data.update(kind="pattern", pairs=len(pairs), max_offset=mx, unmatched_a=len(ua), unmatched_b=len(ub))
        lines.append("  max offset %s mm%s%s" % (fmt(mx, 4), "   unmatched in A: %s" % ua if ua else "",
                                                  "   unmatched in B: %s" % ub if ub else ""))
        return "\n".join(lines), data
    # a hole given second: measure from the hole, label in the given order
    if b.kind == "holes" and a.kind != "holes" and _hole_pair(b, a):
        txt, dd = measure_pair(b, a, tol, ang_tol)
        return _swap_labels(txt, dd, a, b)
    # frame ↔ frame
    if a.kind == "frame" and b.kind == "frame":
        delta = a.placement.inverse().multiply(b.placement)
        ang = math.degrees(delta.Rotation.Angle)
        dv = b.placement.Base - a.placement.Base
        # their +Z axes as lines too, so coaxial / parallel / perpendicular / align / coplanar work on two frames
        za, zb = a.placement.Rotation.multVec(Vector(0, 0, 1)), b.placement.Rotation.multVec(Vector(0, 0, 1))
        zang, zoff = _angle(za, zb), _axis_offset(a.placement.Base, za, b.placement.Base, zb)
        data.update(kind="frames", translation=[dv.x, dv.y, dv.z], distance=dv.Length, rotation_deg=ang,
                    angle_deg=zang, offset=zoff, coaxial=zoff <= tol and zang <= ang_tol,
                    coplanar=zang <= ang_tol and abs(dv.dot(za)) <= tol)
        return "%s → %s: translation (%s) |%s| mm, rotation %s°; +Z axes at %s°, offset %s mm → %s" % (
            a.label, b.label, _pt(dv, 4), fmt(dv.Length, 4), fmt(ang, 3), fmt(zang, 3), fmt(zoff, 4),
            "COAXIAL ✓" if data["coaxial"] else ("parallel, not coaxial" if zang <= ang_tol else
                                               "not parallel")), data
    # hole vs shape: the material around it (its own part) or the wall gap and any coaxial shank/shaft
    if a.kind == "holes" and _hole_pair(a, b):
        return _hole_vs_shape(a, b, ang_tol)
    # axis ↔ axis
    xa, xb = a.as_axis(), b.as_axis()
    if xa and xb:
        ang = _angle(xa[1], xb[1])
        off = _axis_offset(xa[0], xa[1], xb[0], xb[1])
        data.update(kind="axes", angle_deg=ang, offset=off, coaxial=off <= tol and ang <= ang_tol)
        txt = "%s ↔ %s: axes at %s°, offset %s mm → %s" % (
            a.label, b.label, fmt(ang, 3), fmt(off, 4), "COAXIAL ✓" if data["coaxial"] else
            ("parallel, not coaxial" if ang <= ang_tol else "not parallel"))
        if xa[2] and xb[2]:
            big, small = max(xa[2], xb[2]), min(xa[2], xb[2])
            data["diameters"] = [xa[2], xb[2]]
            ca, cb = _concave_axis(a), _concave_axis(b)
            if ang <= ang_tol and ca is not None and ca == cb and (off < big / 2 or (
                    ca and not _same_obj(a, b) and off < (xa[2] + xb[2]) / 2)):
                if ca:                                   # two holes (stacked parts): misalignment, clear passage
                    clear = min(small, (xa[2] + xb[2]) / 2 - off)
                    data.update(clear_d=clear, misalignment=off)
                    txt += "\n  two holes Ø%s / Ø%s, axes %s mm apart → clear passage through both Ø%s%s" % (
                        fmt(xa[2], 3), fmt(xb[2], 3), fmt(off, 4), fmt(clear, 3),
                        " (the smaller hole is fully open)" if clear >= small - 1e-9 else
                        " (%s less than the smaller hole)" % fmt(small - clear, 4))
                else:                                    # two shafts / bosses
                    data["spacing"] = off
                    txt += "\n  two convex cylinders Ø%s / Ø%s, axis spacing %s mm" % (
                        fmt(xa[2], 3), fmt(xb[2], 3), fmt(off, 4))
            elif ang <= ang_tol and off < big / 2:      # one inside the other
                if ca is not None and cb is not None:    # a hole and a shaft: the sign says clearance or interference
                    (he, se, hx) = (a, b, xa) if ca else (b, a, xb)
                    # only where they overlap along the axis, against the hole step there (a head in a counterbore)
                    pairs = [((hd - sd) / 2 - off, hd, sd) for hd, h0, h1 in _axial_cyls(he, hx[0], hx[1])
                             for sd, s0, s1 in _axial_cyls(se, hx[0], hx[1]) if min(h1, s1) - max(h0, s0) > 1e-4]
                    if not pairs:
                        data["axially_apart"] = True
                        txt += "\n  hole Ø%s / shaft Ø%s: the shaft lies beyond the hole's ends (e.g. a screw head " \
                               "on the face), so there is no radial fit — use the cylinder inside the hole (the " \
                               "shank) or the whole part" % (fmt(hx[2], 3), fmt(xb[2] if ca else xa[2], 3))
                        return txt, data
                    rg, hd, sd = min(pairs)
                    data.update(radial_gap_min=rg, hole_d=hd, inner_d=sd)
                    txt += "\n  hole Ø%s / shaft Ø%s → minimum radial gap %s mm%s" % (
                        fmt(hd, 3), fmt(sd, 3), fmt(rg, 4), "" if rg >= -1e-6 else
                        "  ✗ OVERLAP (the shaft is larger than the hole: interference)" if sd > hd else
                        "  ✗ OVERLAP (the offset puts the shaft into the hole wall)")
                else:                                    # circle edges, axis(...): which one is the hole is unknown
                    rg = (big - small) / 2 - off
                    data["radial_gap_min"] = rg
                    txt += "\n  diameters Ø%s / Ø%s → minimum radial gap %s mm%s" % (
                        fmt(xa[2], 3), fmt(xb[2], 3), fmt(rg, 4), "  ✗ OVERLAP" if rg < -1e-6 else "")
            elif ang <= ang_tol:                         # side by side
                edge = off - (xa[2] + xb[2]) / 2
                data.update(spacing=off, edge_gap=edge)
                txt += "\n  Ø%s / Ø%s side by side: axis spacing %s mm; wall-to-wall %s mm%s" % (
                    fmt(xa[2], 3), fmt(xb[2], 3), fmt(off, 4), fmt(edge, 4),
                    "  ✗ the %s intersect" % ("holes" if a.kind == b.kind == "holes" else "cylinders")
                    if edge < -1e-6 else "")
            else:
                txt += "\n  diameters Ø%s / Ø%s (axes not parallel)" % (fmt(xa[2], 3), fmt(xb[2], 3))
        return txt, data
    # axis ↔ plane (a frame's +Z is its axis)
    ax_pl = (a, xa, b) if xa and _is_plane(b) else ((b, xb, a) if xb and _is_plane(a) else None)
    if ax_pl:
        ae, (p, d, _), pe = ax_pl
        q, n = pe.as_plane()
        txt, dd = _axis_plane_text(p, d, q, n, ang_tol)
        data.update(dd, kind="axis_plane")
        od = abs((p - q).dot(n))
        if ae.kind == "frame":
            data.update(distance=od, coplanar=dd["normal"] and od <= tol)
            if not dd["parallel"]:
                txt += "; frame origin %s mm from the plane" % fmt(od, 4)
        else:
            data.update(distance=od if dd["parallel"] else None, coplanar=dd["parallel"] and od <= tol)
        if ae.kind in ("face", "edge") and pe.kind == "face":        # two real shapes: their gap
            sd, _, _ = ae.shape.distToShape(pe.shape)
            data["distance"] = sd
            txt += "; minimum distance between the shapes %s mm" % fmt(sd, 4)
        return "%s ↔ %s: %s" % (a.label, b.label, txt), data
    # plane ↔ plane
    pa, pb = a.as_plane(), b.as_plane()
    if pa and pb:
        ang = _angle(pa[1], pb[1])
        dist = abs((pb[0] - pa[0]).dot(pa[1]))
        data.update(kind="planes", angle_deg=ang, distance=dist if ang <= ang_tol else None,
                    parallel=ang <= ang_tol, coplanar=ang <= ang_tol and dist <= tol)
        txt = "%s ↔ %s: planes at %s°%s" % (a.label, b.label, fmt(ang, 3),
                                           ", %s apart → %s" % (fmt(dist, 4), "COPLANAR ✓" if data["coplanar"]
                                                                 else "parallel") if ang <= ang_tol else
                                           " → not parallel")
        if ang <= ang_tol:
            same = pa[1].dot(pb[1]) > 0
            txt += " (normals %s)" % ("same direction" if same else "opposed — facing each other")
        return txt, data
    # point ↔ point / frame / axis / plane, either order (a frame acts as its +Z axis)
    pt_a = a.as_point() if a.kind in ("point", "vertex") else None
    pt_b = b.as_point() if b.kind in ("point", "vertex") else None
    if pt_a is not None and pt_b is not None:
        dv = pt_b - pt_a
        data.update(kind="points", distance=dv.Length, delta=[dv.x, dv.y, dv.z])
        return "%s → %s: %s mm (Δ %s)" % (a.label, b.label, fmt(dv.Length, 4), _pt(dv, 4)), data
    if (pt_a is None) != (pt_b is None):
        pt, oe = (pt_a, b) if pt_a is not None else (pt_b, a)
        head = "%s %s %s: " % (a.label, "→" if pt_a is not None else "↔", b.label)
        ox, opl = oe.as_axis(), oe.as_plane()
        if ox and (oe.kind == "frame" or not opl):
            w = pt - ox[0]
            radial = (w - ox[1] * w.dot(ox[1])).Length
            data.update(kind="point_axis", radial=radial, axial=w.dot(ox[1]))
            return head + "%s%s mm from %s, %s along it%s" % (
                "" if pt_a is not None else "the point is ", fmt(radial, 4),
                "the frame's +Z axis" if oe.kind == "frame" else "the axis", fmt(data["axial"], 4),
                " (from the frame origin)" if oe.kind == "frame" else ""), data
        if opl:
            sd = (pt - opl[0]).dot(opl[1])
            data.update(kind="point_plane", signed_distance=sd)
            return head + "signed distance %s mm%s" % (
                fmt(sd, 4), "" if pt_a is not None else " (of the point from the plane, along its normal)"), data
    # a shape against an axis line (axis(...), a frame) or a plane(...), either order
    if a.has_shape() != b.has_shape():
        se, oe = (a, b) if a.has_shape() else (b, a)
        txt, dd = _shape_axis(se, oe) if oe.as_axis() else _shape_plane(se, oe)
        data.update(dd)
        return "%s ↔ %s: %s" % (a.label, b.label, txt), data
    # generic shape distance
    sa, sb = a.as_shape(), b.as_shape()
    d, pts, _ = sa.distToShape(sb)
    data.update(kind="distance", distance=d)
    txt = "%s ↔ %s: minimum distance %s mm" % (a.label, b.label, fmt(d, 4))
    if pts:
        p, q = pts[0]
        data["closest"] = [[p.x, p.y, p.z], [q.x, q.y, q.z]]
        txt += "  between (%s) and (%s)" % (_pt(p, 3), _pt(q, 3))
    if sa.Solids and sb.Solids:
        try:
            inter = sa.common(sb).Volume
        except Exception:  # noqa: BLE001
            inter = None
        if inter and inter > 1e-4:
            data["interference_mm3"] = inter
            txt += "\n  ✗ INTERFERENCE %s mm³" % fmt(inter, 3)
    return txt, data


def op_measure(doc, queries, dens=None, tol=0.01):
    lines, out = [], []
    for q in queries:
        if isinstance(q, str):
            q = {"a": q}
        try:
            a = resolve(doc, q["a"])
            if q.get("b") is None:
                txt, data = _describe(a, dens)
            else:
                b = resolve(doc, q["b"])
                txt, data = measure_pair(a, b, tol=float(q.get("tol", tol)))
        except (RefError, fc.FcError) as e:
            txt, data = "✗ %s → %s" % (json.dumps(q), e), {"error": str(e)}
        lines.append(txt)
        out.append(data)
    return "\n".join(lines), out


# --------------------------------------------------------------- check_fit

def _bbox_gap(a, b):
    dx = max(0.0, max(a.XMin, b.XMin) - min(a.XMax, b.XMax))
    dy = max(0.0, max(a.YMin, b.YMin) - min(a.YMax, b.YMax))
    dz = max(0.0, max(a.ZMin, b.ZMin) - min(a.ZMax, b.ZMax))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _face_desc(f, label, shape):
    idx = next((i for i, g in enumerate(shape.Faces) if g.isSame(f)), None)
    s = f.Surface
    k = s.__class__.__name__
    name = "%s.Face%d" % (label, idx + 1) if idx is not None else label
    if k == "Cylinder":
        return "%s (cyl Ø%s)" % (name, fmt(2 * s.Radius, 3))
    if k == "Plane":
        u0, u1, v0, v1 = f.ParameterRange
        return "%s (plane %s)" % (name, _axis_name(f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)))
    return "%s (%s)" % (name, k.lower())


def _opens(f, q, w):
    """Face f's outward normal at q (pointing away from its part's material)
    has a component along w — a gap from q along w leaves f into open space,
    not along f or into the material behind it."""
    try:
        u, v = f.Surface.parameter(q)
        return f.normalAt(u, v).dot(w) > 1e-6
    except Exception:  # noqa: BLE001
        return True


def _in_contact(pa, pb, ea, eb, contacts):
    """Both gap ends lie on a contact plane / line (where the parts touch) and
    the gap cannot slide off it with both ends staying on their faces or
    edges ea / eb: a pseudo-gap along the interface, not a clearance (a real
    gap beside a part resting on a face slides up along its walls)."""
    for q, n in contacts:
        if abs((pa - q).dot(n)) > 1e-6 or abs((pb - q).dot(n)) > 1e-6:
            continue
        for sg in (1e-3, -1e-3):
            try:
                if ea.distToShape(Part.Vertex(pa + n * sg))[0] < 1e-5 and \
                        eb.distToShape(Part.Vertex(pb + n * sg))[0] < 1e-5:
                    break
            except Exception:  # noqa: BLE001
                break
        else:
            return True
    return False


def _real_gap(fa, fb, pts, contacts=()):
    """A closest-point pair (pa on fa, pb on fb) that crosses open space:
    the segment leaves both faces on their open side and does not run along
    the interface where the parts touch. A segment along a part's surface or
    through its material is no clearance — e.g. between the rims of two
    coaxial holes of different Ø across the plane where two stacked parts
    touch (the faces that really bound a gap face each other, so the gap is
    still found through them)."""
    for pa, pb in pts or []:
        w = pb - pa
        if w.Length < 1e-9:
            continue
        w.normalize()
        if _opens(fa, pa, w) and _opens(fb, pb, w * -1) and not _in_contact(pa, pb, fa, fb, contacts):
            return pa, pb
    return None


def _plane_of(f):
    u0, u1, v0, v1 = f.ParameterRange
    return f.valueAt((u0 + u1) / 2, (v0 + v1) / 2), f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)


def _contact_planes(sa, sb):
    """Planes where planar faces of sa and sb lie against each other: [(point, normal)]."""
    out = []
    pb = [(fb, _plane_of(fb)) for fb in sb.Faces if fb.Surface.__class__.__name__ == "Plane"]
    for fa in sa.Faces:
        if fa.Surface.__class__.__name__ != "Plane":
            continue
        qa, na = _plane_of(fa)
        for fb, (qb, nb) in pb:
            if na.dot(nb) < -1 + 1e-6 and abs((qb - qa).dot(na)) < 1e-6 and \
                    _bbox_gap(fa.BoundBox, fb.BoundBox) < 1e-6 and fa.distToShape(fb)[0] < 1e-6 and \
                    not any(abs(abs(na.dot(n)) - 1) < 1e-9 and abs((qa - q).dot(n)) < 1e-6 for q, n in out):
                out.append((qa, na))
    return out


def nearest_gap(sa, sb, la, lb, within):
    """Smallest non-zero face-to-face distance across open space — the real
    clearance when two parts touch on a mounting face."""
    best = None
    contacts = None
    for fa in sa.Faces:
        for fb in sb.Faces:
            if _bbox_gap(fa.BoundBox, fb.BoundBox) > within:
                continue
            d, pts, _ = fa.distToShape(fb)
            if d <= 1e-6 or (best is not None and d >= best[0]):
                continue
            if contacts is None:
                contacts = _contact_planes(sa, sb)
            pp = _real_gap(fa, fb, pts, contacts) if pts else None
            if pts and pp is None:
                continue
            best = (d, pp, fa, fb)
    if best is None:
        return None
    d, pts, fa, fb = best
    return {"gap": d, "where": [[pts[0].x, pts[0].y, pts[0].z], [pts[1].x, pts[1].y, pts[1].z]] if pts else None,
            "faces": "%s ↔ %s" % (_face_desc(fa, la, sa), _face_desc(fb, lb, sb))}


def fit_pair(a, b, min_clearance=0.0, contact_ok=False, interference_ok=False, within=5.0):
    sa, sb = global_shape(a), global_shape(b)
    rec = {"a": a.Label, "b": b.Label}
    bg = _bbox_gap(sa.BoundBox, sb.BoundBox)
    if bg > max(within, min_clearance):
        rec.update(status="clear", distance=None, bbox_gap=bg, ok=True, interference=0.0)
        return rec
    d, pts, _ = sa.distToShape(sb)
    inter = 0.0
    common = None
    if sa.Solids and sb.Solids and d < 1e-6:
        try:
            common = sa.common(sb)
            inter = common.Volume
        except Exception:  # noqa: BLE001
            inter = 0.0
    rec.update(distance=d, interference=inter)
    if pts:
        rec["closest"] = [[pts[0][0].x, pts[0][0].y, pts[0][0].z], [pts[0][1].x, pts[0][1].y, pts[0][1].z]]
    if inter > 1e-3:
        bb = common.BoundBox
        rec.update(status="interference", where=[bb.Center.x, bb.Center.y, bb.Center.z],
                   ok=bool(interference_ok), common=common)
    elif d < 1e-6:
        g = nearest_gap(sa, sb, a.Label, b.Label, within)
        rec.update(status="contact", nearest_gap=g, contact_ok=bool(contact_ok))
        ok = contact_ok or min_clearance <= 0
        if g and g["gap"] < min_clearance - 1e-6 and contact_ok:
            ok = False
        rec["ok"] = ok
    else:
        rec.update(status="gap", ok=d >= min_clearance - 1e-6)
    return rec


def _pairset(doc, pairs, what="contact_ok"):
    """contact_ok / interference_ok pairs → {frozenset(labels)}; names are
    resolved like objects and pair_min (Name, Label or label in any case)."""
    out = set()
    for p in pairs or []:
        if isinstance(p, str) and "|" in p:
            p = [x.strip() for x in p.split("|")]
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise RefError("%s takes pairs of object labels, e.g. [[\"Screws\", \"Bracket\"]] — got %r" % (what, p))
        try:
            out.add(frozenset(find(doc, str(x)).Label for x in p))
        except RefError as e:
            raise RefError("%s %r: %s" % (what, list(p), e))
    return out


def _pair_min(doc, pair_min, labels):
    """pair_min {"A|B": mm} (or [[A, B, mm], …]) → ({frozenset(labels): mm}, notes)."""
    out, notes = {}, []
    if not pair_min:
        return out, notes
    items = pair_min.items() if isinstance(pair_min, dict) else \
        [(p[:2], p[2]) if isinstance(p, (list, tuple)) and len(p) == 3 else (p, None) for p in pair_min]
    for k, v in items:
        names = [x.strip() for x in k.split("|")] if isinstance(k, str) else list(k)
        if len(names) != 2 or v is None:
            raise RefError("pair_min takes {\"A|B\": mm} — two object labels joined by | and the minimum "
                           "clearance for that pair, e.g. {\"M5Screws|M5Washers\": 0.1} — got %r" % (k,))
        a, b = find(doc, names[0]).Label, find(doc, names[1]).Label
        if a == b:
            raise RefError("pair_min %r names the same object twice" % (k,))
        out[frozenset([a, b])] = float(v)
        if a not in labels or b not in labels:
            notes.append("  note: pair_min %s ↔ %s not used — %s is not among the checked objects" % (
                a, b, a if a not in labels else b))
    return out, notes


def op_check_fit(doc, objects=None, min_clearance=0.0, contact_ok=None, interference_ok=None, within=5.0,
                 pair_min=None):
    """Every pair of objects: interference, contact (+ nearest other gap) or gap
    against min_clearance; pair_min {"A|B": mm} sets a pair's own minimum
    (designed hardware gaps), order-independent."""
    objs = [find(doc, r) for r in objects] if objects else [o for o in results(doc) if global_shape(o).Solids]
    contact = _pairset(doc, contact_ok, "contact_ok")
    allow = _pairset(doc, interference_ok, "interference_ok")
    pmin, notes = _pair_min(doc, pair_min, set(o.Label for o in objs))
    recs = []
    for a, b in itertools.combinations(objs, 2):
        key = frozenset([a.Label, b.Label])
        mn = pmin.get(key, min_clearance)
        r = fit_pair(a, b, mn, key in contact, key in allow, within)
        r["min_clearance"] = mn
        if key in pmin:
            r["pair_min"] = mn
        recs.append(r)
    order = {"interference": 0, "contact": 1, "gap": 2, "clear": 3}
    recs.sort(key=lambda r: (r["ok"], order[r["status"]], r.get("distance") or 0))
    bad = [r for r in recs if not r["ok"]]
    if not recs:
        return ["FIT  nothing to check — %d object(s)%s: check_fit compares pairs of parts — name two or more "
                "objects, or leave objects out to check every solid in the document" % (
                    len(objs), " (%s)" % ", ".join(o.Label for o in objs) if objs else "")], recs, objs
    used = [r for r in recs if "pair_min" in r]
    lines = ["FIT  %d object(s), %d pair(s), required clearance ≥ %s mm%s — %s" % (
        len(objs), len(recs), fmt(min_clearance, 3),
        " (pair_min: %s)" % ", ".join("%s ↔ %s ≥ %s" % (r["a"], r["b"], fmt(r["pair_min"], 3)) for r in used)
        if used else "", "all OK ✓" if not bad else "%d problem(s) ✗" % len(bad))]
    for r in recs:
        mark = "✓" if r["ok"] else "✗"
        pair = "%s ↔ %s" % (r["a"], r["b"])
        mn = r["min_clearance"]
        req = "%s%s" % (fmt(mn, 3), " (pair_min)" if "pair_min" in r else "")
        if r["status"] == "interference":
            lines.append("  %s %-28s INTERFERENCE %s mm³ around (%s)%s" % (
                mark, pair, fmt(r["interference"], 3), _pt(Vector(*r["where"])),
                "  (allowed: press fit / thread)" if r["ok"] else ""))
        elif r["status"] == "contact":
            g = r.get("nearest_gap")
            extra = ("; nearest gap elsewhere %s mm: %s" % (fmt(g["gap"], 4), g["faces"])) if g else ""
            if r["ok"]:
                why = "  (pair_min %s)" % fmt(mn, 3) if "pair_min" in r and g else ""
            elif r.get("contact_ok"):
                why = "  ← nearest other gap %s mm < required %s" % (fmt(g["gap"], 4), req)
            else:
                why = "  ← not listed as an expected contact (add the pair to contact_ok if they are meant to touch)"
            lines.append("  %s %-28s contact (touching, no overlap)%s%s" % (mark, pair, extra, why))
        elif r["status"] == "clear":
            continue
        elif r["status"] == "gap":
            lines.append("  %s %-28s gap %s mm  between (%s) and (%s)%s" % (
                mark, pair, fmt(r["distance"], 4), _pt(Vector(*r["closest"][0])), _pt(Vector(*r["closest"][1])),
                "  ← < required %s" % req if not r["ok"] else
                "  (pair_min %s)" % fmt(mn, 3) if "pair_min" in r else ""))
    clear = [r for r in recs if r["status"] == "clear"]
    if clear:
        lines.append("  ✓ %d other pair(s) clear (> %s mm apart)" % (
            len(clear), fmt(max([within, min_clearance] + list(pmin.values())), 1)))
    return lines + notes, recs, objs


# ----------------------------------------------------------------- section

_SEC_AXES = {"x": ((1, 0, 0), (0, 1, 0), (0, 0, 1), "viewed from +X"),
             "y": ((0, -1, 0), (1, 0, 0), (0, 0, 1), "viewed from −Y"),
             "z": ((0, 0, 1), (1, 0, 0), (0, 1, 0), "viewed from +Z")}


def section_plane(sec):
    for ax in ("x", "y", "z"):
        if ax in sec:
            n, u, v, note = _SEC_AXES[ax]
            p = [0.0, 0.0, 0.0]
            p["xyz".index(ax)] = float(sec[ax])
            return Vector(*p), Vector(*n), Vector(*u), Vector(*v), "%s = %s (%s)" % (ax, fmt(float(sec[ax]), 3), note)
    if "point" in sec and "normal" in sec:
        p, n = _vec(sec["point"]), _vec(sec["normal"])
        n.normalize()
        u = n.cross(Vector(0, 0, 1))
        if u.Length < 1e-6:
            u = Vector(1, 0, 0)
        u.normalize()
        v = n.cross(u) * -1
        return p, n, u, v, "point (%s) normal (%s)" % (_pt(p), _pt(n, 3))
    raise RefError("plane must be {x|y|z: offset} or {point: [..], normal: [..]}")


def section_faces(shape, p, n):
    wires = shape.slice(n, p.dot(n))
    if not wires:
        return None
    closed = [w for w in wires if w.isClosed()]
    if not closed:
        return None
    try:
        return Part.makeFace(closed, "Part::FaceMakerBullseye")
    except Exception:  # noqa: BLE001
        return Part.makeCompound([Part.Face(w) for w in closed])


def _edge_uv(e, p, u, v):
    """A section edge in plane coordinates: 'line (u0, v0)–(u1, v1)', 'arc Ø3.600 centre (u, v)'."""
    try:
        c = e.Curve.__class__.__name__
        if c in ("Line", "LineSegment"):
            (u0, v0), (u1, v1) = _uv(e.Vertexes[0].Point, p, u, v), _uv(e.Vertexes[-1].Point, p, u, v)
            return "line (%s, %s)–(%s, %s)" % (fmt(u0), fmt(v0), fmt(u1), fmt(v1))
        if c == "Circle":
            cu, cv = _uv(e.Curve.Center, p, u, v)
            return "%s Ø%s centre (%s, %s)" % ("circle" if e.isClosed() else "arc", fmt(2 * e.Curve.Radius, 3),
                                              fmt(cu), fmt(cv))
        mu, mv = _uv(e.valueAt((e.FirstParameter + e.LastParameter) / 2), p, u, v)
        return "%s edge through (%s, %s)" % (c.lower(), fmt(mu), fmt(mv))
    except Exception:  # noqa: BLE001
        return "edge"


def _parallel_lines(ea, eb):
    try:
        if ea.Curve.__class__.__name__ not in ("Line", "LineSegment") or \
                eb.Curve.__class__.__name__ not in ("Line", "LineSegment"):
            return False
        da = ea.Vertexes[-1].Point - ea.Vertexes[0].Point
        db = eb.Vertexes[-1].Point - eb.Vertexes[0].Point
        return da.Length > 1e-9 and db.Length > 1e-9 and _angle(da.normalize(), db.normalize()) < 1e-3
    except Exception:  # noqa: BLE001
        return False


def _edge_opens(fs, e, q, w, n):
    """In a section: edge e of region fs, at q — a gap from q along w leaves
    the region's boundary into open space (not along another of its edges
    or into its material). n is the section plane's normal."""
    try:
        t0, t1 = e.FirstParameter, e.LastParameter
        k = min(t1 - (t1 - t0) * 1e-3, max(t0 + (t1 - t0) * 1e-3, e.Curve.parameter(q)))
        tan = e.tangentAt(k)
        m = n.cross(tan)
        m.normalize()
        c = e.valueAt(k)
        ins = [fs.isInside(c + m * s, 1e-7, True) for s in (1e-3, -1e-3)]
        if ins[0] == ins[1]:
            return True
        out = m * (-1 if ins[0] else 1)                 # away from the region's material
        return out.dot(w) > 1e-6
    except Exception:  # noqa: BLE001
        return True


def _contact_lines(fa, fb, n):
    """In a section: lines where edges of regions fa and fb lie against each
    other, as [(point, in-plane normal of the line)] (n: the plane normal)."""
    out = []
    for ea in fa.Edges:
        if ea.Curve.__class__.__name__ not in ("Line", "LineSegment") or len(ea.Vertexes) != 2:
            continue
        p0 = ea.Vertexes[0].Point
        da = ea.Vertexes[-1].Point - p0
        if da.Length < 1e-9:
            continue
        da.normalize()
        for eb in fb.Edges:
            if eb.Curve.__class__.__name__ not in ("Line", "LineSegment") or len(eb.Vertexes) != 2 or \
                    _bbox_gap(ea.BoundBox, eb.BoundBox) > 1e-6:
                continue
            q0, q1 = eb.Vertexes[0].Point, eb.Vertexes[-1].Point
            off = lambda q: ((q - p0) - da * (q - p0).dot(da)).Length  # noqa: E731
            if off(q0) < 1e-6 and off(q1) < 1e-6 and ea.distToShape(eb)[0] < 1e-6:
                m = n.cross(da)
                m.normalize()
                if not any(abs(abs(m.dot(k)) - 1) < 1e-9 and abs((p0 - q).dot(k)) < 1e-6 for q, k in out):
                    out.append((p0, m))
    return out


def _side_name(w, u, v):
    """A direction in the section plane: +X … when axis-aligned, else ±u / ±v."""
    nm = _axis_name(w)
    if not nm.startswith("("):
        return nm
    for name, d in (("u", u), ("v", v)):
        if abs(abs(w.dot(d)) - 1) < 1e-6:
            return ("+" if w.dot(d) > 0 else "-") + name
    return nm


def _axis_lines(f, q, a, w, t0, t1, reach):
    """Section edges of f parallel to the axis (q, a) on the side w, within
    reach of the axis: [(distance from the axis, t from, t to)] clipped to t0…t1."""
    out = []
    for e in f.Edges:
        try:
            if e.Curve.__class__.__name__ not in ("Line", "LineSegment") or len(e.Vertexes) != 2:
                continue
            p0, p1 = e.Vertexes[0].Point, e.Vertexes[1].Point
        except Exception:  # noqa: BLE001
            continue
        dv = p1 - p0
        if dv.Length < 1e-9 or abs(abs(dv.dot(a)) / dv.Length - 1) > 1e-6:
            continue
        s = (p0 - q).dot(w)
        if s < -1e-9 or s > reach:
            continue
        ta, tb = sorted([(p0 - q).dot(a), (p1 - q).dot(a)])
        if min(tb, t1) - max(ta, t0) > 1e-4:
            out.append((s, max(ta, t0), min(tb, t1)))
    return out


def _side_gaps(fh, fi, h, n, u, v, within):
    """The radial gap on each side of hole h's axis (which lies in the
    section plane) between the holder's section fh and the inner part's
    section fi over the hole's depth — between their edges parallel to the
    axis (the hole wall and the shank or shaft), so an axial contact (a head
    seated on a counterbore floor, a shoulder on a bore step) does not hide
    the radial clearance; the contact is returned as axial_contact:
    [{"side", "gap", "points", "axial_contact"}]."""
    a, q = h["axis"], h["point"]
    t0, t1 = h["span"]
    w = n.cross(a)
    w.normalize()
    reach = max(h["diameters"]) / 2 + within
    out = []
    for sg in (1, -1):
        c0, c1 = q + a * t0 - n, q + a * t1 - n
        try:
            slab = Part.Face(Part.makePolygon([c0, c1, c1 + w * (sg * reach), c0 + w * (sg * reach), c0])) \
                .extrude(n * 2)
            ca, cb = fh.common(slab), fi.common(slab)
            if ca.Area < 1e-9 or cb.Area < 1e-9:
                continue
            d, pts, _ = ca.distToShape(cb)
            pts = list(pts[0]) if pts else None
        except Exception:  # noqa: BLE001
            continue
        rec = {"side": _side_name(w * sg, u, v), "gap": d}
        best = None                                  # radial: the hole wall against the shank / shaft
        for sh, a0, a1 in _axis_lines(fh, q, a, w * sg, t0, t1, reach):
            for si, b0, b1 in _axis_lines(fi, q, a, w * sg, t0, t1, reach):
                g = sh - si
                if min(a1, b1) - max(a0, b0) > 1e-4 and g > -1e-9 and (best is None or g < best[0]):
                    best = (max(g, 0.0), si, sh, (max(a0, b0) + min(a1, b1)) / 2)
        if best:
            g, si, sh, tm = best
            rec["gap"] = g
            if d < 1e-6 and g > 1e-6 and pts:                # touching, but not across the axis
                rec["axial_contact"] = [pts[0].x, pts[0].y, pts[0].z]
            pts = [q + a * tm + w * (sg * si), q + a * tm + w * (sg * sh)]       # shown at mid-overlap
        elif pts and d > 1e-6:
            # parallel walls: show the gap at mid-depth rather than at an end of the hole
            shift = a * ((t0 + t1) / 2 - ((pts[0] + pts[1]) * 0.5 - q).dot(a))
            r0, r1 = pts[0] + shift, pts[1] + shift
            if ca.distToShape(Part.Vertex(r0))[0] < 1e-6 and cb.distToShape(Part.Vertex(r1))[0] < 1e-6:
                pts = [r0, r1]
        rec["points"] = [[x.x, x.y, x.z] for x in pts] if pts else None
        out.append(rec)
    return sorted(out, key=lambda x: x["side"])


def _hole_axes(objs_secs, p, n):
    """Per object: its holes whose axis lies in the section plane."""
    out = {}
    for o, f in objs_secs:
        hs = []
        for h in detect_holes(global_shape(o)):
            if abs(h["axis"].dot(n)) < 1e-6 and abs((h["point"] - p).dot(n)) < 1e-4:
                hs.append(h)
        out[o.Name] = hs
    return out


def op_section(doc, plane, objects=None, within=5.0, png=None, size=(1100, 800), min_clearance=None,
               contact_ok=None, interference_ok=None):
    """Cut the objects with a plane: per object area and exact extents, per
    pair the gap in the plane (touching pairs: the nearest other gap and its
    edges; a hole whose axis lies in the plane with a part inside it: the gap
    on each side of the axis). interference_ok / contact_ok pair lists as in
    check_fit."""
    p, n, u, v, desc = section_plane(plane)
    objs = [find(doc, r) for r in objects] if objects else [o for o in results(doc) if global_shape(o).Solids]
    contact = _pairset(doc, contact_ok, "contact_ok")
    allow = _pairset(doc, interference_ok, "interference_ok")
    secs = []
    for o in objs:
        f = section_faces(global_shape(o), p, n)
        if f is not None and f.Area > 1e-9:
            secs.append((o, f))
    if not secs:
        raise RefError("the plane %s does not cut any of the objects" % desc)
    lines = ["SECTION %s — %d object(s) cut" % (desc, len(secs))]
    data = {"plane": desc, "objects": [], "pairs": []}
    for o, f in secs:
        bb = _uv_bbox(f, p, u, v)
        lines.append("  %-20s area %s mm²  extent u %s…%s  v %s…%s  regions %d" % (
            o.Label, fmt(f.Area, 2), fmt(bb[0]), fmt(bb[2]), fmt(bb[1]), fmt(bb[3]), len(f.Faces)))
        data["objects"].append({"label": o.Label, "area": f.Area, "uv_bbox": bb})
    axes = None
    gaps = []
    for (oa, fa), (ob, fb) in itertools.combinations(secs, 2):
        if _bbox_gap(fa.BoundBox, fb.BoundBox) > within:
            continue
        key = frozenset([oa.Label, ob.Label])
        d, pts, _ = fa.distToShape(fb)
        overlap = 0.0
        if d < 1e-6:
            try:
                overlap = fa.common(fb).Area
            except Exception:  # noqa: BLE001
                overlap = 0.0
        rec = {"a": oa.Label, "b": ob.Label, "gap": d, "overlap_area": overlap}
        if overlap > 1e-6 and key in allow:
            rec["allowed"] = True
        if key in contact:
            rec["contact_ok"] = True
        if pts:
            rec["points"] = [[q.x, q.y, q.z] for q in pts[0]]
        # touching parts: the smallest real gap between their section edges, and which edges
        if d < 1e-6 and overlap < 1e-6:
            g, clines = None, None
            for ea in fa.Edges:
                for eb in fb.Edges:
                    if _bbox_gap(ea.BoundBox, eb.BoundBox) > within:
                        continue
                    dd, pp, _ = ea.distToShape(eb)
                    if dd <= 1e-6 or (g is not None and dd > g[0] + 1e-6):
                        continue
                    # only gaps across open space: not along the line where the parts touch (hole rims)
                    if clines is None:
                        clines = _contact_lines(fa, fb, n)
                    sol = None
                    for qa, qb in pp or []:
                        w = qb - qa
                        if w.Length > 1e-9:
                            w.normalize()
                            if _edge_opens(fa, ea, qa, w, n) and _edge_opens(fb, eb, qb, w * -1, n) and \
                                    not _in_contact(qa, qb, ea, eb, clines):
                                sol = (qa, qb)
                                break
                    if sol is None:
                        continue
                    par = _parallel_lines(ea, eb)            # on a tie, two parallel walls read best
                    if g is None or dd < g[0] - 1e-6 or (dd < g[0] + 1e-6 and par and not g[4]):
                        g = (dd, sol, ea, eb, par)
            if g:
                rec["nearest_gap"] = g[0]
                rec["nearest_points"] = [[q.x, q.y, q.z] for q in g[1]]
                rec["nearest_edges"] = "%s %s ↔ %s %s" % (oa.Label, _edge_uv(g[2], p, u, v), ob.Label,
                                                          _edge_uv(g[3], p, u, v))
        # a hole whose axis lies in the plane with the other part inside it: the gap on each side of the axis
        if overlap < 1e-6:
            if axes is None:
                axes = _hole_axes(secs, p, n)
            sides = []
            for (oh, fh), (oi, fi) in (((oa, fa), (ob, fb)), ((ob, fb), (oa, fa))):
                si = global_shape(oi)
                for h in axes.get(oh.Name, []):
                    t0, t1 = h["span"]
                    cyl = [c for c in _coaxial_cylinders(si, h["point"], h["axis"], h["diameters"][0] / 2)
                           if min(c[4], t1) - max(c[3], t0) > 1e-4]
                    if not cyl or len(sides) >= 6:
                        continue
                    per = _side_gaps(fh, fi, h, n, u, v, within)
                    if len(per) == 2:
                        c = (h["point"] + h["axis"] * ((t0 + t1) / 2))
                        sides.append({"holder": oh.Label, "inner": oi.Label, "hole_d": h["diameters"][0],
                                      "hole_ds": list(h["diameters"]), "inner_d": max(x[0] for x in cyl),
                                      "inner_ds": sorted(set(round(x[0], 4) for x in cyl)),
                                      "axis": _axis_name(h["axis"]), "at": [c.x, c.y, c.z], "sides": per})
            if sides:
                rec["across_axis"] = sides
                best = min((x for s in sides for x in s["sides"]), key=lambda x: x["gap"])
                if d > 1e-6 and abs(best["gap"] - d) < 1e-6 and best["points"]:
                    rec["points"] = best["points"]              # plot the smallest gap mid-depth
        gaps.append(rec)
        data["pairs"].append({k: v_ for k, v_ in rec.items()})
    if gaps:
        lines.append("  gaps in this plane:")
        for r in sorted(gaps, key=lambda r: r["gap"]):
            if r["overlap_area"] > 1e-6:
                if r.get("allowed"):
                    lines.append("    ✓ %s ↔ %s overlap %s mm² (allowed: press fit / thread)" % (
                        r["a"], r["b"], fmt(r["overlap_area"], 3)))
                else:
                    lines.append("    ✗ %s ↔ %s OVERLAP %s mm² (interference in this plane — add the pair to "
                                 "interference_ok if it is a thread or press fit)" % (
                                     r["a"], r["b"], fmt(r["overlap_area"], 3)))
            elif r["gap"] < 1e-6:
                ng = r.get("nearest_gap")
                lines.append("    %s ↔ %s touching%s%s" % (
                    r["a"], r["b"], " (expected contact)" if r.get("contact_ok") else "",
                    "; nearest other gap %s mm: %s" % (fmt(ng, 4), r["nearest_edges"]) if ng else ""))
            else:
                flag = ""
                if min_clearance is not None and r["gap"] < min_clearance:
                    flag = "  ✗ below %s" % fmt(min_clearance, 3)
                lines.append("    %s ↔ %s gap %s mm%s" % (r["a"], r["b"], fmt(r["gap"], 4), flag))
            for s in r.get("across_axis", []):
                ax_c = [x["axial_contact"] for x in s["sides"] if x.get("axial_contact")]
                lines.append("      across the axis of %s's Ø%s hole (axis %s, Ø%s of %s inside) at (%s): %s%s" % (
                    s["holder"], "/Ø".join(fmt(x, 3) for x in s["hole_ds"]), s["axis"],
                    "/Ø".join(fmt(x, 3) for x in s["inner_ds"]), s["inner"],
                    _pt(Vector(*s["at"])), ", ".join(
                        "%s side %s" % (x["side"], "touching" if x["gap"] < 1e-6 else "%s mm%s" % (
                            fmt(x["gap"], 4), "  ✗ below %s" % fmt(min_clearance, 3)
                            if min_clearance is not None and x["gap"] < min_clearance else ""))
                        for x in s["sides"]),
                    " (radial gaps; axial contact at (%s) — e.g. a head or shoulder seated on a step)" % _pt(
                        Vector(*ax_c[0])) if ax_c else ""))
    if png:
        _plot_section(secs, gaps, p, u, v, desc, png, size)
    return lines, data, png


def _uv(pt, p, u, v):
    w = pt - p
    return w.dot(u), w.dot(v)


def _uv_bbox(f, p, u, v):
    """Exact extents of a section face in plane coordinates [umin, vmin, umax, vmax]."""
    try:
        bb = _plane_bb(f, p, u, v)
        return [bb.XMin, bb.YMin, bb.XMax, bb.YMax]
    except Exception:  # noqa: BLE001 — fall back to a fine polyline
        pts = [_uv(q, p, u, v) for e in f.Edges for q in e.discretize(Deflection=0.001)]
        us, vs = [a for a, _ in pts], [b for _, b in pts]
        return [min(us), min(vs), max(us), max(vs)]


def _rings(face, p, u, v, defl):
    rings = []
    outer = face.OuterWire
    for w in face.Wires:
        pts = [_uv(q, p, u, v) for q in w.discretize(Deflection=defl)]
        if len(pts) < 3:
            continue
        area = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
        is_outer = w.isSame(outer)
        if (area < 0) == is_outer:          # outer CCW, holes CW
            pts.reverse()
        rings.append(pts)
    return rings


def _plot_section(secs, gaps, p, u, v, desc, png, size):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.path import Path
    from matplotlib.patches import PathPatch

    import render as rd
    tight = sorted([g for g in gaps if (g.get("nearest_gap") or g["gap"]) > 1e-6
                    and (g.get("nearest_gap") or g["gap"]) < 2.0],
                   key=lambda g: g.get("nearest_gap") or g["gap"])[:3]
    ninset = len(tight)
    fig = plt.figure(figsize=(size[0] / 100.0, size[1] / 100.0), dpi=100)
    if ninset:
        gs = fig.add_gridspec(ninset, 3)
        ax = fig.add_subplot(gs[:, :2])
        insets = [fig.add_subplot(gs[k, 2]) for k in range(ninset)]
    else:
        ax = fig.add_subplot(1, 1, 1)
        insets = []
    allb = [_uv_bbox(f, p, u, v) for _, f in secs]
    span = max(max(b[2] for b in allb) - min(b[0] for b in allb), max(b[3] for b in allb) - min(b[1] for b in allb), 1e-3)
    hatches = ["////", "\\\\\\\\", "xx", "..", "--", "++"]
    for axis in [ax] + insets:
        for k, (o, f) in enumerate(secs):
            col = rd.REF_COLOUR if role(o) == "ref" else rd.PALETTE[k % len(rd.PALETTE)]
            for fi, face in enumerate(f.Faces):
                verts, codes = [], []
                for ring in _rings(face, p, u, v, span / 2000.0):
                    verts += ring + [ring[0]]
                    codes += [Path.MOVETO] + [Path.LINETO] * (len(ring) - 1) + [Path.CLOSEPOLY]
                patch = PathPatch(Path(verts, codes), facecolor=col, edgecolor=(0.1, 0.1, 0.1),
                                  hatch=hatches[k % len(hatches)], linewidth=0.8, alpha=0.9,
                                  label=o.Label if (axis is ax and fi == 0) else None)
                axis.add_patch(patch)
        axis.set_aspect("equal")
        axis.grid(True, linewidth=0.3, alpha=0.5)
    ax.set_xlim(min(b[0] for b in allb) - 0.05 * span, max(b[2] for b in allb) + 0.05 * span)
    ax.set_ylim(min(b[1] for b in allb) - 0.05 * span, max(b[3] for b in allb) + 0.05 * span)
    ax.set_title("Section %s" % desc, fontsize=11)
    ax.set_xlabel("u (mm)")
    ax.set_ylabel("v (mm)")
    ax.legend(loc="upper right", fontsize=8)
    for g in gaps:
        pts = g.get("nearest_points") if g["gap"] < 1e-6 else g.get("points")
        val = g.get("nearest_gap") if g["gap"] < 1e-6 else g["gap"]
        if not pts or not val:
            continue
        (u0, v0), (u1, v1) = _uv(Vector(*pts[0]), p, u, v), _uv(Vector(*pts[1]), p, u, v)
        ax.annotate("", xy=(u1, v1), xytext=(u0, v0), arrowprops=dict(arrowstyle="<->", color="red", lw=1))
        ax.text((u0 + u1) / 2, (v0 + v1) / 2, " %s" % fmt(val, 3), color="red", fontsize=8)
        for s in g.get("across_axis", []):           # the gap on each side of a hole axis in the plane
            for x in s["sides"]:
                if not x["points"] or x["gap"] < 1e-6 or x["points"] == pts:
                    continue
                (u0, v0), (u1, v1) = _uv(Vector(*x["points"][0]), p, u, v), _uv(Vector(*x["points"][1]), p, u, v)
                ax.annotate("", xy=(u1, v1), xytext=(u0, v0), arrowprops=dict(arrowstyle="<->", color="red", lw=1))
                ax.text((u0 + u1) / 2, (v0 + v1) / 2, " %s" % fmt(x["gap"], 3), color="red", fontsize=8)
    for axis, g in zip(insets, tight):
        pts = g.get("nearest_points") if g["gap"] < 1e-6 else g["points"]
        val = g.get("nearest_gap") if g["gap"] < 1e-6 else g["gap"]
        (u0, v0), (u1, v1) = _uv(Vector(*pts[0]), p, u, v), _uv(Vector(*pts[1]), p, u, v)
        cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
        w = max(8 * val, 1.0)
        axis.set_xlim(cu - w, cu + w)
        axis.set_ylim(cv - w, cv + w)
        axis.annotate("", xy=(u1, v1), xytext=(u0, v0), arrowprops=dict(arrowstyle="<->", color="red", lw=1))
        axis.set_title("%s ↔ %s: %s mm" % (g["a"], g["b"], fmt(val, 3)), fontsize=8)
        axis.tick_params(labelsize=7)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)


# ------------------------------------------------------------- checks.json

_CHECK_SHAPES = {
    "clearance": '{"a": "PartA", "b": "PartB", "min": 0.3, "contact_ok": true}',
    "interference": '{"a": "PartA", "b": "PartB", "max": 0}',
    "radial_gap": '{"a": "Plate.holes(d=3.4)", "b": "Screws", "min": 0.1}',
    "align": '{"a": "A.holes", "b": "B.holes", "max_offset": 0.05}',
    "coaxial": '{"a": "Housing.holes[0]", "b": "Shaft", "max_offset": 0.01, "max_angle": 0.05}',
    "parallel": '{"a": "A.face(+Z)", "b": "B.face(+Z)", "max_angle": 0.05}',
    "coplanar": '{"a": "A.face(+Z)", "b": "B.face(+Z)"}',
    "distance": '{"a": "Plate.holes[0]", "b": "Plate", "min": 1.5} (or "max", or "value" with "tol")',
    "bound": '{"object": "Part", "axis": "z", "side": "min", "min": 0}',
    "bbox": '{"object": "Part", "min": [x, y, z], "max": [x, y, z]}',
    "mass": '{"object": "Part", "max_g": 20}',
    "volume": '{"object": "Part", "min": 1000}',
    "hole_count": '{"object": "Plate", "count": 4} or {"holes": "Plate.holes(d=3.4)", "count": 4}',
    "hole_diameter": '{"holes": "Plate.holes(axis=+Z)", "d": 3.4, "tol": 0.01, "nominal": true}',
    "positions": '{"holes": "Plate.holes(d=3.4)", "points": [[x, y, z], …], "tol": 0.05} '
                 '(points [[u, v], …] together with "axis": "+Z")',
    "face_count": '{"object": "Part", "surface": "cylinder", "radius": 3, "concave": true, "count": 4}',
    "valid": '{"object": "Part"}',
}
_CHECK_SHAPES.update(pattern=_CHECK_SHAPES["align"], perpendicular=_CHECK_SHAPES["parallel"],
                     **{k: '{"object": "Part", "min": 0}' for k in ("min_x", "max_x", "min_y", "max_y", "min_z",
                                                                     "max_z")})
_REQ = object()


def _fnum(c, k, default=_REQ):
    """Check field k as a number (default when absent): null, true or text is
    an error that names the field."""
    if k not in c:
        if default is _REQ:
            raise KeyError(k)
        return default
    v = c[k]
    try:
        if v is None or isinstance(v, bool):
            raise TypeError
        return float(v)
    except (TypeError, ValueError):
        raise ValueError("'%s' must be a number — got %s" % (k, json.dumps(v)))


def evaluate_checks(doc, checks, dens=None, default_clearance=0.0):
    """Evaluate acceptance checks. Returns (lines, results). A malformed
    check reports an error naming the expected fields — it never stops the
    build."""
    out = []
    for c in checks or []:
        if not isinstance(c, dict):
            out.append({"id": "?", "type": None, "ok": False, "value": "error",
                        "detail": "each check is an object {\"id\": …, \"type\": …, …} — got %s"
                                  % json.dumps(c)})
            continue
        cid = str(c.get("id") or c.get("type", "?"))
        t = c.get("type")
        shape = "expected e.g. {\"type\": \"%s\", …%s" % (t, _CHECK_SHAPES[t][1:]) if t in _CHECK_SHAPES else \
            "see checks.md for this check type's fields"
        try:
            ok, val, detail = _check_one(doc, c, t, dens, default_clearance)
        except KeyError as e:
            k = e.args[0] if e.args else "?"
            ok, val, detail = False, "error", ("missing field '%s' — %s" % (k, shape)) if k not in c else str(e)
        except (RefError, fc.FcError) as e:
            ok, val, detail = False, "error", str(e)
        except ValueError as e:
            ok, val, detail = False, "error", "%s — %s" % (e, shape) if str(e).startswith(
                ("'", "could not convert", "invalid literal")) else str(e)
        except Exception as e:  # noqa: BLE001 — a malformed entry must not crash the build
            ok, val, detail = False, "error", "malformed %s check (%s: %s) — %s" % (t, type(e).__name__, e, shape)
        out.append({"id": cid, "type": t, "ok": ok, "value": val, "detail": detail})
    lines = []
    for r in out:
        lines.append("  %s %-18s %-11s %s%s" % ("✓" if r["ok"] else "✗", r["id"], r["type"], r["value"],
                                               ("   " + r["detail"]) if r["detail"] else ""))
    return lines, out


def _limits(c, val, unit="mm", nd=3):
    parts = []
    ok = True
    if "min" in c:
        ok &= val >= _fnum(c, "min") - 1e-9
        parts.append("≥ %s" % fmt(_fnum(c, "min"), nd))
    if "max" in c:
        ok &= val <= _fnum(c, "max") + 1e-9
        parts.append("≤ %s" % fmt(_fnum(c, "max"), nd))
    if "value" in c:
        tol = _fnum(c, "tol", 0.01)
        ok &= abs(val - _fnum(c, "value")) <= tol
        parts.append("= %s ± %s" % (fmt(_fnum(c, "value"), nd), fmt(tol, nd)))
    return ok, "%s %s (%s)" % (fmt(val, nd), unit, ", ".join(parts) or "measured")


def _check_one(doc, c, t, dens, default_clearance):
    if t in ("clearance", "interference"):
        a, b = find(doc, c["a"]), find(doc, c["b"])
        mn = _fnum(c, "min", default_clearance)
        r = fit_pair(a, b, mn, contact_ok=bool(c.get("contact_ok")), within=max(5.0, mn))
        if t == "interference":
            v = r.get("interference", 0.0)
            ok = v <= _fnum(c, "max", 0.0) + 1e-6
            return ok, "%s mm³ (≤ %s)" % (fmt(v, 3), fmt(_fnum(c, "max", 0.0), 3)), ""
        if r["status"] == "interference":
            return False, "%s mm³ overlap" % fmt(r["interference"], 3), "interference"
        if r["status"] == "contact":
            g = r.get("nearest_gap")
            if not c.get("contact_ok"):
                return mn <= 0, "contact", "parts touch — set contact_ok if intended"
            if g is None:
                return True, "contact", "no other gap within range"
            ok = g["gap"] >= mn - 1e-6
            return ok, "%s mm (≥ %s)" % (fmt(g["gap"], 4), fmt(mn, 3)), \
                ("contact + nearest gap: " if ok else "contact; nearest other gap %s mm < required %s: " % (
                    fmt(g["gap"], 4), fmt(mn, 3))) + g["faces"]
        if r["status"] == "clear":
            return True, "> %s mm" % fmt(max(5.0, mn), 1), ""
        return r["ok"], "%s mm (≥ %s)" % (fmt(r["distance"], 4), fmt(mn, 3)), ""
    if t == "radial_gap":
        a, b = resolve(doc, c["a"]), resolve(doc, c["b"])
        if a.kind != "holes":
            raise RefError("radial_gap 'a' must be a hole reference, e.g. Bracket.holes(d=22.5)")
        worst, fallback = None, False
        for h in a.holes:
            one = Entity("holes", a.label, obj=a.obj, holes=[h], solid=getattr(a, "solid", None))
            bb = b
            if b.kind == "bosses" and len(b.bosses) > 1:
                # a pattern of shanks: the one whose axis runs through this hole
                near = min(b.bosses, key=lambda x: (_angle(x["axis"], h["axis"]) > 0.5,
                                                    _axis_offset(h["point"], h["axis"], x["point"], x["axis"])))
                bb = Entity("bosses", b.label, obj=b.obj, bosses=[near], solid=getattr(b, "solid", None))
            _, d = measure_pair(one, bb)
            if d.get("kind") == "hole_wall":
                raise RefError("%s is the part that has the hole — radial_gap compares a hole with the part inside "
                               "it (a shaft, pin or screw); for the material around the hole use a distance check "
                               "(it reports the minimum wall)" % b.label)
            g = d.get("radial_gap_min")
            if g is None and d.get("axially_apart"):
                raise RefError("%s lies beyond the ends of the hole at (%s) (e.g. a screw head on the face) — "
                               "radial_gap needs the cylinder inside the hole: select the shank, e.g. "
                               "bosses(d=3), or give the whole part" % (b.label, _pt(h["at"])))
            if g is None and "spacing" in d:
                raise RefError("%s is not inside the hole at (%s): axis spacing %s mm — radial_gap needs the "
                               "cylinder inside the hole; use coaxial to check alignment or distance for the gap" % (
                                   b.label, _pt(h["at"]), fmt(d["spacing"], 4)))
            if g is None:
                g, fallback = d.get("wall_gap"), True
            if g is None:
                raise RefError("no radial gap between %s and %s — use a part or cylindrical face that passes "
                               "through the hole" % (a.label, b.label))
            if worst is None or g < worst:
                worst = g
        ok, txt = _limits(c, worst)
        return ok, txt, "over %d hole(s)%s" % (len(a.holes), "; no coaxial cylinder inside a hole — nearest "
                                                 "distance from its wall used" if fallback else "")
    if t in ("align", "pattern"):
        a, b = resolve(doc, c["a"]), resolve(doc, c["b"])
        _, d = measure_pair(a, b)
        if d.get("kind") == "pattern":
            mx = d["max_offset"]
            ok = mx <= _fnum(c, "max_offset", 0.05) + 1e-9 and not d["unmatched_a"] and \
                (not d["unmatched_b"] or c.get("allow_unmatched"))
            return ok, "max offset %s mm (≤ %s)" % (fmt(mx, 4), fmt(_fnum(c, "max_offset", 0.05), 3)), \
                "%d pairs%s" % (d["pairs"], ", unmatched %d/%d" % (d["unmatched_a"], d["unmatched_b"])
                                if d["unmatched_a"] or d["unmatched_b"] else "")
        if d.get("kind") in ("axes", "frames"):
            ok = d["offset"] <= _fnum(c, "max_offset", 0.05) and d["angle_deg"] <= _fnum(c, "max_angle", 0.1)
            return ok, "offset %s mm, %s°" % (fmt(d["offset"], 4), fmt(d["angle_deg"], 3)), ""
        raise RefError("align needs hole patterns, axes (holes, cylindrical faces, axis(...)) or frames — got "
                       "%s ↔ %s" % (a.label, b.label))
    if t == "coaxial":
        a, b = resolve(doc, c["a"]), resolve(doc, c["b"])
        _, d = measure_pair(a, b)
        if d.get("kind") == "hole_shape" and "offset" not in d:
            raise RefError("%s has no cylinder coaxial with %s — measure them to see where it is" % (
                b.label, a.label))
        if d.get("kind") not in ("axes", "hole_shape", "frames"):
            raise RefError("coaxial needs axis-like references (holes, cylindrical faces, frames, axis(...)) or a "
                           "hole and the part inside it — got %s ↔ %s" % (a.label, b.label))
        ok = d["offset"] <= _fnum(c, "max_offset", 0.01) and d["angle_deg"] <= _fnum(c, "max_angle", 0.05)
        return ok, "offset %s mm, %s°" % (fmt(d["offset"], 4), fmt(d["angle_deg"], 3)), ""
    if t in ("parallel", "coplanar", "perpendicular", "distance"):
        a, b = resolve(doc, c["a"]), resolve(doc, c["b"])
        _, d = measure_pair(a, b)
        kind = d.get("kind")
        if t in ("parallel", "perpendicular"):
            if d.get("angle_deg") is None:
                raise RefError("%s needs two directions — planes or planar faces, axes (holes, cylindrical faces, "
                               "line edges, axis(...)) or frames (+Z) — got %s ↔ %s" % (t, a.label, b.label))
            ang = d["angle_deg"]
            what = ("axis ↔ plane: the axis lies at %s° to the plane (parallel = along the plane, perpendicular = "
                    "along its normal)" % fmt(ang, 3)) if "normal" in d else ""
            if t == "parallel":
                return ang <= _fnum(c, "max_angle", 0.05), "%s°" % fmt(ang, 3), what
            dev = abs(90 - ang)
            return dev <= _fnum(c, "max_angle", 0.05), "%s° from 90" % fmt(dev, 3), what
        if t == "coplanar":
            return bool(d.get("coplanar")), "%s°, %s mm" % (fmt(d.get("angle_deg"), 3), fmt(d.get("distance"), 4)), \
                ("axis ↔ plane: coplanar = the axis lies in the plane (a frame: its XY plane)"
                 if kind == "axis_plane" else "")
        if kind == "hole_wall":
            val = d["min_wall"]
        elif kind == "hole_shape":
            val = d["wall_gap"]
        elif kind == "point_axis":
            val = d["radial"]
        elif "distance" in d:
            val = d["distance"]
        else:
            val = d.get("signed_distance", d.get("offset"))
        if val is None:
            if kind in ("planes", "axis_plane"):
                raise RefError("%s ↔ %s are not parallel (%s°), so no single distance — check the angle with "
                               "parallel/perpendicular, or measure from a point" % (
                                   a.label, b.label, fmt(d.get("angle_deg"), 3)))
            raise RefError("distance not defined between %s and %s — use planes/faces, a point and a plane or "
                           "axis, a hole and a face or part, or two shapes" % (a.label, b.label))
        ok, txt = _limits(c, abs(val))
        return ok, txt, {"hole_wall": "minimum wall around the hole", "hole_shape": "from the hole wall"}.get(kind, "")
    if t in ("bound", "min_x", "max_x", "min_y", "max_y", "min_z", "max_z"):
        o = find(doc, c["object"])
        bb = tight_bb(global_shape(o))
        axis, side = (c.get("axis", "z"), c.get("side", "min")) if t == "bound" else (t[-1], t[:3])
        val = getattr(bb, "%s%s" % (axis.upper(), "Min" if side == "min" else "Max"))
        ok, txt = _limits(c, val)
        return ok, txt, ""
    if t == "bbox":
        o = find(doc, c["object"])
        bb = tight_bb(global_shape(o))
        size = [bb.XLength, bb.YLength, bb.ZLength]
        lim, bad = [], []
        for key, sign in (("min", "≥"), ("max", "≤")):
            if key not in c:
                continue
            lim.append("%s [%s]" % (sign, ", ".join("-" if m is None else _g(float(m)) for m in c[key])))
            for ax, s, m in zip("XYZ", size, c[key]):
                if m is not None and (s < float(m) - 1e-9 if key == "min" else s > float(m) + 1e-9):
                    bad.append("%s %s %s %s" % (ax, fmt(s, 3), "<" if key == "min" else ">", _g(float(m))))
        if not lim:
            raise ValueError("bbox needs min and/or max: [x, y, z] sizes in mm")
        return not bad, "%s × %s × %s" % tuple(fmt(s, 2) for s in size), \
            "limit %s%s" % (" and ".join(lim), ("; ✗ " + ", ".join(bad)) if bad else "")
    if t in ("mass", "volume"):
        o = find(doc, c["object"])
        vol = global_shape(o).Volume
        if t == "volume":
            ok, txt = _limits(c, vol, "mm³", 1)
            return ok, txt, ""
        dv = _density(dens, o.Label)
        if not dv:
            raise ValueError("mass check needs a density for %s — set material.density_g_cm3 in project.json, or "
                             "parts.%s.density_g_cm3 / parts.%s.material" % (o.Label, o.Label, o.Label))
        m = vol * dv / 1000.0
        cc = dict(c)
        if "max_g" in c:
            cc["max"] = c["max_g"]
        if "min_g" in c:
            cc["min"] = c["min_g"]
        ok, txt = _limits(cc, m, "g", 1)
        return ok, txt, "at %s g/cm³" % fmt(dv, 2)
    if t == "hole_count":
        e = resolve(doc, c["holes"] if "holes" in c else "%s.holes" % c["object"])
        n = len(e.holes)
        return n == int(_fnum(c, "count")), "%d holes (expected %d)" % (n, int(_fnum(c, "count"))), ""
    if t == "hole_diameter":
        return _check_hole_diameter(doc, c)
    if t == "positions":
        return _check_positions(doc, c)
    if t == "face_count":
        return _check_face_count(doc, c)
    if t == "valid":
        o = find(doc, c["object"])
        s = global_shape(o)
        return bool(s.isValid() and s.Solids), "valid" if s.isValid() else "INVALID", ""
    raise ValueError("unknown check type %r" % t)


def _check_hole_diameter(doc, c):
    """Every matched hole's smallest Ø equals d ± tol; nominal: true compares
    with d + the print compensation (FcHoleComp) of the Hole feature that made
    it, so the check states the design value."""
    if "d" not in c:
        raise ValueError("hole_diameter needs d (the expected Ø in mm), e.g. {\"holes\": \"Plate.holes(d=5.7)\", "
                         "\"d\": 5.5, \"nominal\": true}")
    e = resolve(doc, c["holes"] if "holes" in c else "%s.holes" % c["object"])
    if e.kind != "holes":
        raise RefError("hole_diameter needs a holes reference, e.g. Plate.holes(axis=+Z)")
    want, tol, nominal = _fnum(c, "d"), _fnum(c, "tol", 0.01), bool(c.get("nominal"))
    feats = _hole_features(e.obj)
    bad, vals, comps = [], [], set()
    for k, h in enumerate(e.holes):
        dd = h["diameters"][0]
        f = _feature_for(e.obj, h, feats)
        comps.add(round(f["comp"], 4) if f else 0.0)
        comp = f["comp"] if f and nominal else 0.0
        vals.append(dd - comp)
        if abs(dd - comp - want) > tol + 1e-9:
            bad.append("[%d] Ø%s%s at (%s)" % (k, fmt(dd, 3), " − %s comp = Ø%s" % (_g(comp), fmt(dd - comp, 3))
                                               if comp else "", _pt(h["at"])))
    lo, hi = min(vals), max(vals)
    val = ("Ø%s" % fmt(lo, 3)) if hi - lo < 1e-6 else "Ø%s…Ø%s" % (fmt(lo, 3), fmt(hi, 3))
    cs = sorted(x for x in comps if x)
    if nominal:
        how = "nominal: modelled Ø − print compensation (%s)" % (
            "FcHoleComp %s" % "/".join(_g(x) for x in cs) if cs else "none on these holes")
    else:
        how = "as modelled%s" % ("; these holes carry %s mm print compensation — nominal: true checks the design Ø"
                                 % "/".join(_g(x) for x in cs) if cs else "")
    return not bad, "%s (= Ø%s ± %s), %d hole(s)" % (val, _g(want), _g(tol), len(e.holes)), \
        "; ".join([how] + (["✗ " + ", ".join(bad)] if bad else []))


def _check_positions(doc, c):
    """Every expected axis point has its own hole (or boss) within tol (any order)."""
    e = resolve(doc, c["holes"])
    if e.kind not in ("holes", "bosses"):
        raise RefError("positions needs a holes or bosses reference, e.g. Plate.holes(d=4.2) or Base.bosses(d=6)")
    hs, want = e.feats(), None
    if c.get("axis"):
        want = _canon(_dir(c["axis"]))
        hs = [h for h in hs if abs(h["axis"].dot(want)) > 1 - 1e-6]
    raw = c["points"]
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)  # noqa: E731
    if not isinstance(raw, list) or not raw or not all(
            isinstance(p, list) and len(p) in (2, 3) and all(num(x) for x in p) for p in raw):
        raise ValueError("positions 'points' must be a list of points, each [x, y, z] (or [u, v] together with "
                         "axis=) — e.g. [[30, 20, 0], [-30, 20, 0]], even for one point: [[30, 20, 0]]; got %s"
                         % json.dumps(raw))
    pts = []
    for p in raw:
        if len(p) == 2 and want is not None:
            k = max(range(3), key=lambda m: abs([want.x, want.y, want.z][m]))
            i, j = [m for m in range(3) if m != k]           # the two coordinates across the axis
            q = [0.0, 0.0, 0.0]
            q[i], q[j] = float(p[0]), float(p[1])
            pts.append(Vector(*q))
        elif len(p) == 3:
            pts.append(_vec(p))
        else:
            raise ValueError("positions points are [x, y, z] (two numbers only together with axis=)")
    tol = _fnum(c, "tol", 0.05)

    def off(p, h):
        w = p - h["point"]
        return (w - h["axis"] * w.dot(h["axis"])).Length
    cand = sorted((off(p, h), i, j) for i, p in enumerate(pts) for j, h in enumerate(hs))
    used_p, used_h, pairs = set(), set(), []
    for dd, i, j in cand:
        if i not in used_p and j not in used_h and dd <= tol:
            used_p.add(i)
            used_h.add(j)
            pairs.append(dd)
    missing = [i for i in range(len(pts)) if i not in used_p]
    extra = [j for j in range(len(hs)) if j not in used_h]
    ok = not missing and (not extra or bool(c.get("allow_extra")))
    notes = []
    for i in missing:
        near = min(((off(pts[i], h), j) for j, h in enumerate(hs)), default=None)
        notes.append("no hole at (%s)%s" % (_pt(pts[i], 3), "; nearest [%d] at (%s) is %s mm off" % (
            near[1], _pt(hs[near[1]]["at"], 3), fmt(near[0], 4)) if near else ""))
    if extra:
        notes.append("%d extra hole(s)%s: %s" % (len(extra), " (allowed)" if c.get("allow_extra") else
                                                 " — add their points or set allow_extra",
                                                 ", ".join("[%d] Ø%s at (%s)" % (j, fmt(hs[j]["diameters"][0], 3),
                                                                                _pt(hs[j]["at"], 3)) for j in extra)))
    return ok, "%d/%d points matched, worst %s mm (≤ %s), %d hole(s)" % (
        len(pairs), len(pts), fmt(max(pairs or [0.0]), 4), fmt(tol, 3), len(hs)), "; ".join(notes)


_SURFACES = {"plane": "Plane", "cylinder": "Cylinder", "cone": "Cone", "torus": "Toroid", "sphere": "Sphere"}


def _surface(f):
    """(kind, identity, radius, concave) of a face's underlying surface: split
    faces of one cylinder, cone, … share the identity."""
    s = f.Surface
    k = s.__class__.__name__
    r4 = lambda v: (round(v.x, 4), round(v.y, 4), round(v.z, 4))  # noqa: E731
    u0, u1, v0, v1 = f.ParameterRange
    p, n = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2), f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    if k == "Plane":
        return k, r4(n) + (round(f.CenterOfMass.dot(n), 4),), None, None
    if k in ("Cylinder", "Cone"):
        cc = _is_concave(f)
        ident = r4(_canon(s.Axis)) + (r4(_axis_point(s)) + (round(s.Radius, 4),) if k == "Cylinder"
                                      else r4(s.Apex) + (round(s.SemiAngle, 6),))
        return k, ident + (cc,), s.Radius if k == "Cylinder" else None, cc
    if k == "Sphere":
        cc = n.dot(p - s.Center) < 0
        return k, r4(s.Center) + (round(s.Radius, 4), cc), s.Radius, cc
    if k == "Toroid":
        a = s.Axis
        w = p - s.Center
        rad = w - a * w.dot(a)
        rad.normalize()
        cc = n.dot(p - (s.Center + rad * s.MajorRadius)) < 0
        return k, r4(_canon(a)) + r4(s.Center) + (round(s.MajorRadius, 4), round(s.MinorRadius, 4), cc), \
            s.MinorRadius, cc
    return k, (id(f),), None, None


def _check_face_count(doc, c):
    """Distinct surfaces of a kind (optionally radius, concave) — chamfers
    (cones), fillets and internal corner radii (concave cylinders)."""
    o = find(doc, c["object"])
    kind = str(c.get("surface", "any")).lower()
    if kind != "any" and kind not in _SURFACES:
        raise ValueError("face_count surface must be plane, cylinder, cone, torus, sphere or any — got %r" % kind)
    if "radius" in c and kind in ("plane", "cone"):
        raise ValueError("radius applies to cylinders, spheres and tori (fillet = minor radius), not %ss" % kind)
    if "concave" in c and kind == "plane":
        raise ValueError("concave applies to curved surfaces, not planes")
    groups = {}
    for f in global_shape(o).Faces:
        k, ident, r, cc = _surface(f)
        if kind != "any" and k != _SURFACES[kind]:
            continue
        if "radius" in c and (r is None or abs(r - _fnum(c, "radius")) > _fnum(c, "tol", 0.01)):
            continue
        if "concave" in c and (cc is None or cc != bool(c["concave"])):
            continue
        groups.setdefault((k, ident), f)
    n = len(groups)
    words = ["concave"] if c.get("concave") is True else ["convex"] if c.get("concave") is False else []
    words += [kind] if kind != "any" else []
    words += ["surface(s)"] + (["R%s" % _g(_fnum(c, "radius"))] if "radius" in c else [])
    what = " ".join(words)
    if "count" in c:
        ok, txt = n == int(_fnum(c, "count")), "%d %s (expected %d)" % (n, what, int(_fnum(c, "count")))
    elif "min" in c or "max" in c:
        lo = int(_fnum(c, "min")) if "min" in c else None
        hi = int(_fnum(c, "max")) if "max" in c else None
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        txt = "%d %s (%s)" % (n, what, ", ".join(x for x in ("≥ %d" % lo if lo is not None else "",
                                                             "≤ %d" % hi if hi is not None else "") if x))
    else:
        raise ValueError("face_count needs count, or min and/or max")
    where = ["(%s)" % _pt(f.CenterOfMass, 1) for f in list(groups.values())[:6]]
    return ok, txt, ("at " + " ".join(where) + (" …" if n > 6 else "")) if where else ""


# ------------------------------------------------- GUI-edit change tracking

_SKIP_PROPS = {"Shape", "Proxy", "ExpressionEngine", "Label2", "Visibility", "Geometry", "Constraints",
               "Content", "cells", "ExternalGeometry", "Group", "OriginFeatures", "Tip", "_Body",
               "AddSubShape", "SuppressedShape", "BaseFeature", "AttacherEngine", "AttacherType", "Uid"}


def snapshot(doc):
    """A comparable record of every object's editable state."""
    snap = {"objects": {}, "params": {}}
    for o in doc.Objects:
        props = {}
        for p in o.PropertiesList:
            if p in _SKIP_PROPS or p.startswith("_"):
                continue
            try:
                t = o.getTypeIdOfProperty(p)
            except Exception:  # noqa: BLE001
                continue
            if t in ("Part::PropertyPartShape", "App::PropertyPythonObject", "App::PropertyMap",
                     "App::PropertyFileIncluded", "Part::PropertyGeometryList", "App::PropertyExpressionEngine"):
                continue
            try:
                val = getattr(o, p)
            except Exception:  # noqa: BLE001
                continue
            s = _pstr(val)
            if s is not None:
                props[p] = s
        rec = {"type": o.TypeId, "label": o.Label, "props": props,
               "expr": {p: e for p, e in getattr(o, "ExpressionEngine", [])}}
        if o.TypeId == "Sketcher::SketchObject":
            rec["constraints"] = ["%s %s" % (c.Type, fmt(c.Value, 4) if c.Type in (
                "Distance", "DistanceX", "DistanceY", "Radius", "Diameter") else
                fmt(math.degrees(c.Value), 3) if c.Type == "Angle" else "") for c in o.Constraints]
            rec["geometry"] = len(o.Geometry)
        snap["objects"][o.Name] = rec
        if o.TypeId == "Spreadsheet::Sheet" and o.Name == "Params":
            for row in range(2, 400):
                name = (o.getContents("A%d" % row) or "").lstrip("'")
                if not name:
                    break
                try:
                    snap["params"][name] = float(o.get("B%d" % row))
                except Exception:  # noqa: BLE001
                    pass
    return snap


def _r6(x):
    """A float rounded to 1e-6 as text, without float noise or -0."""
    s = ("%.6f" % x).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _rot_str(r):
    """A rotation as angle and axis from its normalised quaternion, the sign
    of q / -q (one rotation) fixed on the components rounded to 1e-6 and the
    result rounded to 1e-6, so the noise of a recomputed placement is not an
    edit (a rotation under ~1e-4° is none)."""
    n = math.sqrt(sum(x * x for x in r.Q)) or 1.0
    q = [x / n for x in r.Q]
    qr = [round(x, 6) for x in q]
    k = next((i for i in (3, 0, 1, 2) if qr[i]), 3)          # first non-zero component positive
    q = [-x if qr[k] < 0 else x for x in q]
    s = math.sqrt(q[0] ** 2 + q[1] ** 2 + q[2] ** 2)
    if round(s, 6) == 0:
        return "none"
    return "%s° about (%s)" % (_r6(2 * math.degrees(math.atan2(s, q[3]))), ", ".join(_r6(x / s) for x in q[:3]))


def _pstr(v):
    if isinstance(v, (bool, int, float, str)):
        return str(round(v, 6)) if isinstance(v, float) else str(v)
    if hasattr(v, "Value") and hasattr(v, "Unit"):
        s = str(v)
        m = re.match(r"^(-?[\d.]+(?:e-?\d+)?)(\s.*)?$", s)
        return str(round(float(m.group(1)), 6) + 0.0) + (m.group(2) or "") if m else s
    if isinstance(v, Vector):
        return "(%s)" % ", ".join(_r6(x) for x in (v.x, v.y, v.z))
    if isinstance(v, App.Rotation):
        return "Rotation %s" % _rot_str(v)
    if isinstance(v, App.Placement):
        return "Placement [Pos=(%s), Rot=%s]" % (", ".join(_r6(x) for x in (v.Base.x, v.Base.y, v.Base.z)),
                                                 _rot_str(v.Rotation))
    if hasattr(v, "Name") and hasattr(v, "TypeId"):
        return "→" + v.Name
    if isinstance(v, (list, tuple)):
        try:
            return "[" + ", ".join(_pstr(x) or "?" for x in v[:20]) + "]"
        except Exception:  # noqa: BLE001
            return None
    return None


_NUM = r"-?\d+(?:\.\d*)?(?:e[-+]?\d+)?"


def _geom(s):
    """A Vector / Rotation / Placement back from its snapshot text — the
    rounded form above or the plain str() older snapshots stored — or None."""
    n = [float(x) for x in re.findall(_NUM, s)]
    rot = lambda m: App.Rotation(Vector(*m[1:]), m[0]) if m else App.Rotation()  # noqa: E731
    if s.startswith(("Vector (", "(")) and len(n) == 3:
        return Vector(*n)
    if s.startswith("Rotation ("):
        return App.Rotation(*n) if len(n) == 4 else None
    if s.startswith("Rotation ") and len(n) in (0, 4):
        return rot(n)
    if s.startswith("Placement [Pos=("):
        if "Yaw-Pitch-Roll" in s:
            return App.Placement(Vector(*n[:3]), App.Rotation(*n[3:])) if len(n) == 6 else None
        return App.Placement(Vector(*n[:3]), rot(n[3:])) if len(n) in (3, 7) else None
    return None


def _same_val(a, b):
    """Two snapshot values are the same: equal text, a number / quantity
    within 1e-6, or a Vector / Rotation / Placement from a snapshot made
    before values were rounded (6 significant digits) within that precision."""
    if a == b:
        return True
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    ma, mb = (re.match(r"^(%s)(\s.*)?$" % _NUM, x) for x in (a, b))
    if ma and mb:
        x, y = float(ma.group(1)), float(mb.group(1))
        return ma.group(2) == mb.group(2) and abs(x - y) <= 1e-6 * max(1.0, abs(x))
    if not any(x.startswith(("Vector (", "Rotation (")) or "Yaw-Pitch-Roll" in x for x in (a, b)):
        return False
    try:
        ga, gb = _geom(a), _geom(b)
    except Exception:  # noqa: BLE001
        return False
    if ga is None or type(ga) is not type(gb):
        return False

    def rot_ok(r1, r2):
        t = r1.inverted().multiply(r2).Angle
        return min(t, 2 * math.pi - t) <= math.radians(5e-4)
    if isinstance(ga, Vector):
        return (ga - gb).Length <= 1e-4 * max(1.0, ga.Length)
    if isinstance(ga, App.Rotation):
        return rot_ok(ga, gb)
    return (ga.Base - gb.Base).Length <= 1e-4 * max(1.0, ga.Base.Length) and rot_ok(ga.Rotation, gb.Rotation)


def diff_snapshots(old, new):
    """Human-readable change list from old (last build) to new (file on disk)."""
    lines = []
    param_changes = {}
    for k in sorted(set(old.get("params", {})) | set(new.get("params", {}))):
        a, b = old["params"].get(k), new["params"].get(k)
        if a != b:
            param_changes[k] = b
            lines.append("param    %s: %s → %s (Params sheet)" % (k, _num(a), _num(b)))
    oo, no = old.get("objects", {}), new.get("objects", {})
    for name in no:
        if name not in oo:
            r = no[name]
            interesting = {k: v for k, v in r["props"].items()
                           if k in ("Length", "Radius", "Size", "Depth", "Diameter", "Base", "Profile", "Type",
                                    "Placement", "Reversed", "Midplane", "Offset")}
            lines.append("added    %s \"%s\" (%s) %s" % (name, r["label"], r["type"],
                                                         " ".join("%s=%s" % kv for kv in interesting.items())[:300]))
    for name in oo:
        if name not in no:
            lines.append("removed  %s \"%s\" (%s)" % (name, oo[name]["label"], oo[name]["type"]))
    for name in no:
        if name not in oo:
            continue
        a, b = oo[name], no[name]
        if a["type"] == "Spreadsheet::Sheet":
            continue
        changes = []
        attached = b["props"].get("MapMode") not in (None, "Deactivated")
        for p in sorted(set(a["props"]) | set(b["props"])):
            if p in ("Label",) or (p == "Placement" and attached):     # attached: placement is derived
                continue
            if (p == "Depth" and b["props"].get("DepthType") == "ThroughAll") or \
                    (p == "ThreadDepth" and b["props"].get("ThreadDepthType") != "Dimension"):
                continue                                                # computed by the Hole feature
            if not _same_val(a["props"].get(p), b["props"].get(p)):
                changes.append("%s %s → %s" % (p, a["props"].get(p, "∅"), b["props"].get(p, "∅")))
        for p in sorted(set(a["expr"]) | set(b["expr"])):
            if a["expr"].get(p) != b["expr"].get(p):
                changes.append("expression %s: %s → %s" % (p, a["expr"].get(p, "∅"), b["expr"].get(p, "∅")))
        if a.get("constraints") != b.get("constraints"):
            ca, cb = a.get("constraints") or [], b.get("constraints") or []
            for i in range(max(len(ca), len(cb))):
                x = ca[i] if i < len(ca) else "∅"
                y = cb[i] if i < len(cb) else "∅"
                if x != y:
                    changes.append("constraint c%d %s → %s" % (i, x, y))
        if a.get("geometry") != b.get("geometry"):
            changes.append("geometry count %s → %s" % (a.get("geometry"), b.get("geometry")))
        if a["label"] != b["label"]:
            changes.append("label %s → %s" % (a["label"], b["label"]))
        # a value change caused purely by a Params edit is not a separate edit
        changes = [c for c in changes if not _explained_by_params(c, b, param_changes)]
        if changes:
            lines.append("changed  %s \"%s\": %s" % (name, b["label"], "; ".join(changes)[:600]))
    return lines, param_changes


def _explained_by_params(change, rec, param_changes):
    if not param_changes:
        return False
    prop = change.split(" ", 1)[0]
    expr = rec.get("expr", {})
    if prop in expr and expr[prop].startswith("Params."):
        return True
    if change.startswith("constraint c"):
        idx = change.split(" ")[1][1:]
        e = expr.get("Constraints[%s]" % idx, "")
        return e.startswith("Params.")
    return False


def _num(v):
    if v is None:
        return "∅"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


# -------------------------------------------------------------- assemblies

def assembly_lines(doc):
    """Assembly workbench summary: parts, grounded part, joints and whether
    each joint holds (its frames measured after solving)."""
    lines = []
    for asm in [o for o in doc.Objects if o.TypeId == "Assembly::AssemblyObject"]:
        jg = [o for o in asm.Group if o.TypeId == "Assembly::JointGroup"]
        joints = jg[0].Group if jg else []
        parts = [o for o in asm.Group if o.TypeId != "Assembly::JointGroup" and not o.TypeId.startswith("App::Origin")
                 and (has_shape(o) or o.TypeId == "App::Link")]
        grounded = [j.ObjectToGround.Label for j in joints if hasattr(j, "ObjectToGround") and j.ObjectToGround]
        lines.append("assembly %s  %d part(s) · grounded: %s · %d joint(s)" % (
            asm.Label, len(parts), ", ".join(grounded) or "NONE ⚠ (nothing anchors the assembly)",
            len([j for j in joints if hasattr(j, "JointType")])))
        for j in joints:
            if not hasattr(j, "JointType"):
                continue
            err = fc.joint_error(j)
            a = j.Reference1[0].Label if getattr(j, "Reference1", None) else "?"
            b = j.Reference2[0].Label if getattr(j, "Reference2", None) else "?"
            if err is None:
                state = ""
            elif err[0] <= 1e-4 and err[1] <= 1e-3:
                state = "holds ✓"
            else:
                state = "✗ off by %s mm / %s°" % (fmt(err[0], 4), fmt(err[1], 3))
            lines.append("  %-14s %-12s %s ↔ %s  %s" % (j.Label, j.JointType, a, b, state))
    return lines
