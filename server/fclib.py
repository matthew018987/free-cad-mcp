# SPDX-License-Identifier: Apache-2.0
"""
fclib — helpers for FreeCAD model scripts, available as `fc` inside model.py
and run_script code.

Only the parts of FreeCAD's API that are easy to get wrong are wrapped: fully
constrained sketches, attachment, standard holes, reference parts and mating
frames. Everything else is plain FreeCAD.

Coordinates are global millimetres. A 2D point (a, b) given for a plane or face
is read in that plane's dominant global axes: (x, y) for faces normal to Z,
(x, z) for faces normal to Y, (y, z) for faces normal to X. Pass 3D points to
be explicit.
"""

import json
import math
import os
import re

import FreeCAD as App
import Part
import Sketcher

import standards

Vector = App.Vector


class FcError(Exception):
    """A modelling mistake with a readable explanation."""


# ------------------------------------------------------------------ context

class _Context:
    def __init__(self):
        self.doc = None
        self.project_dir = None
        self.param_values = {}
        self.param_meta = {}
        self.overrides = {}
        self.results = {}
        self.warnings = []
        self.uncomp = {}          # body label → uncompensated print holes (one warning per body)
        self.reads = []           # files resolved through path() / ref_part (the build hashes them)
        self._params = None
        self._project = None


_ctx = _Context()


def _setup(doc, project_dir=None, overrides=None):
    """Called by the worker before running a script."""
    global _ctx
    _ctx = _Context()
    _ctx.doc = doc
    _ctx.project_dir = project_dir
    _ctx.overrides = dict(overrides or {})


def doc():
    """The FreeCAD document being built (also available as `doc`)."""
    return _ctx.doc


def warn(msg):
    """Add a warning to the build report (shown under 'warnings')."""
    _ctx.warnings.append(str(msg))


def result(**kw):
    """Return structured data: from run_script it is shown in the tool result,
    from model.py on a 'result' line of the build report (and in report.json)."""
    _ctx.results.update(kw)


def path(rel):
    """Resolve a path relative to the project directory. Files read this way
    (and by ref_part) are part of the build's inputs: when one changes, the
    next build is a new revision — read data files through fc.path."""
    p = rel if os.path.isabs(rel) or not _ctx.project_dir else os.path.join(_ctx.project_dir, rel)
    if p not in _ctx.reads:
        _ctx.reads.append(p)
    return p


def _project():
    """project.json of the project being built ({} without one)."""
    if _ctx._project is None:
        _ctx._project = {}
        pj = path("project.json") if _ctx.project_dir else None
        if pj and os.path.isfile(pj):
            try:
                with open(pj) as fh:
                    _ctx._project = json.load(fh) or {}
            except (OSError, ValueError):
                pass
    return _ctx._project


# --------------------------------------------------------------- parameters

class Param(float):
    """A float that remembers which parameter it came from, so features and
    constraints given a Param are bound to the Params spreadsheet by expression
    and stay editable in the GUI."""

    def __new__(cls, value, name):
        obj = float.__new__(cls, value)
        obj.name = name
        return obj

    def __format__(self, spec):
        # f"Ø{P.bore:g}" formats the value like any float
        return format(float(self), spec)


class Params:
    def __init__(self, values):
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise FcError("no parameter '%s' — defined: %s"
                          % (name, ", ".join(sorted(self._values)) or "(none)"))

    def __getitem__(self, name):
        return self.__getattr__(name)

    def __contains__(self, name):
        return name in self._values

    def __setattr__(self, name, value):
        raise FcError("parameters are read-only in model.py — change params.json "
                      "or pass params overrides to build")

    def names(self):
        return sorted(self._values)


def params():
    """Load params.json (+ build overrides) into a Params spreadsheet and return
    attribute access to the values. Numeric values come back as Param."""
    if _ctx._params is not None:
        return _ctx._params
    raw = {}
    pj = path("params.json") if _ctx.project_dir else None
    if pj and os.path.isfile(pj):
        with open(pj) as fh:
            raw = json.load(fh)
    for k, v in _ctx.overrides.items():
        if isinstance(raw.get(k), dict):
            raw[k] = dict(raw[k], value=v)
        else:
            raw[k] = v
    sheet = _ctx.doc.getObject("Params") or _ctx.doc.addObject("Spreadsheet::Sheet", "Params")
    sheet.set("A1", "parameter")
    sheet.set("B1", "value")
    sheet.set("C1", "unit")
    sheet.set("D1", "source")
    values = {}
    row = 2
    for name in sorted(raw):
        spec = raw[name]
        meta = spec if isinstance(spec, dict) else {"value": spec}
        val = meta.get("value")
        _ctx.param_meta[name] = meta
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            values[name] = val
            continue
        sheet.set("A%d" % row, name)
        sheet.set("B%d" % row, repr(float(val)))
        alias = name
        try:
            sheet.setAlias("B%d" % row, alias)
        except ValueError:
            # FreeCAD rejects aliases that read as units or cells (t, d, w, A1…)
            alias = "p_" + name
            sheet.setAlias("B%d" % row, alias)
            warn("parameter '%s' is aliased as Params.%s in the spreadsheet "
                 "(FreeCAD reserves '%s')" % (name, alias, name))
        sheet.set("C%d" % row, str(meta.get("unit", "mm")))
        if meta.get("source"):
            sheet.set("D%d" % row, "'" + str(meta["source"]))
        values[name] = Param(float(val), alias)
        row += 1
    _ctx.param_values = {k: (float(v) if isinstance(v, Param) else v) for k, v in values.items()}
    _ctx._params = Params(values)
    return _ctx._params


def _set(obj, prop, value):
    """Set obj.prop, binding it to the Params sheet when value is a Param."""
    setattr(obj, prop, float(value) if isinstance(value, Param) else value)
    if isinstance(value, Param):
        obj.setExpression(prop, "Params.%s" % value.name)


def _bind_constraint(sk, idx, value):
    if isinstance(value, Param):
        sk.setExpression("Constraints[%d]" % idx, "Params.%s" % value.name)


# -------------------------------------------------------------- directions

_AXES = {"+X": Vector(1, 0, 0), "-X": Vector(-1, 0, 0), "+Y": Vector(0, 1, 0),
         "-Y": Vector(0, -1, 0), "+Z": Vector(0, 0, 1), "-Z": Vector(0, 0, -1)}


def _dir(d):
    if isinstance(d, Vector):
        v = Vector(d)
    elif isinstance(d, (tuple, list)):
        v = Vector(*d)
    else:
        key = str(d).upper()
        if key in ("X", "Y", "Z"):
            key = "+" + key
        if key not in _AXES:
            raise FcError("direction must be one of +X -X +Y -Y +Z -Z or a vector, got %r" % (d,))
        v = Vector(_AXES[key])
    if v.Length < 1e-12:
        raise FcError("zero-length direction")
    v.normalize()
    return v


def _plane_axes(normal):
    """Global axes a 2D point is read in, for a plane with this normal."""
    n = [abs(normal.x), abs(normal.y), abs(normal.z)]
    k = n.index(max(n))
    return [(1, 2), (0, 2), (0, 1)][k]


# ---------------------------------------------------------------- selectors

class FaceRef:
    """A planar face of a feature: (feature, 'FaceN') plus its geometry."""

    def __init__(self, obj, name, face):
        self.obj, self.name, self.face = obj, name, face
        u0, u1, v0, v1 = face.ParameterRange
        self.normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        self.center = face.CenterOfMass

    def support(self):
        return [(self.obj, self.name)]

    def __repr__(self):
        return "FaceRef(%s.%s normal=%s)" % (self.obj.Name, self.name, _fmtv(self.normal))


class EdgeRef:
    """Edges of one feature, from fc.edges(...). Selections combine with +:
    fc.edges(b, kind="circle", at=0) + fc.edges(b, kind="circle", at=34)."""

    def __init__(self, obj, names):
        self.obj, self.names = obj, names

    def __len__(self):
        return len(self.names)

    def __add__(self, other):
        if isinstance(other, int) and other == 0:        # sum([...]) starts from 0
            return self
        if not isinstance(other, EdgeRef):
            return NotImplemented
        if (other.obj.Document.Name, other.obj.Name) != (self.obj.Document.Name, self.obj.Name):
            raise FcError("cannot combine edges of %s and %s — select both from the same body after its last "
                          "feature (fc.edges(...) + fc.edges(...) with no feature added in between)"
                          % (self.obj.Label, other.obj.Label))
        return EdgeRef(self.obj, self.names + [n for n in other.names if n not in self.names])

    __radd__ = __add__

    def __repr__(self):
        return "EdgeRef(%s %s)" % (self.obj.Name, ",".join(self.names))


def _fmtv(v):
    return "(%.3g, %.3g, %.3g)" % (v.x, v.y, v.z)


def _feature(obj):
    """The object whose sub-elements may be referenced: a Body's Tip."""
    if obj.TypeId == "PartDesign::Body":
        if obj.Tip is None:
            raise FcError("Body %s has no features yet" % obj.Label)
        return obj.Tip
    return obj


def _shape_of(obj):
    _ctx.doc.recompute()
    feat = _feature(obj)
    s = feat.Shape
    if s.isNull():
        st = list(feat.State)
        if "Invalid" in st or "Error" in st:
            raise FcError("%s's last feature %s failed (%s) — fix or remove it before "
                          "selecting geometry from %s" % (obj.Label, feat.Label,
                                                          feat.getStatusString(), obj.Label))
        raise FcError("%s has no shape yet" % obj.Label)
    return s


def face(obj, direction, at=None, contains=None, tol=1e-6):
    """The planar face of obj facing `direction`. With several candidates the
    outermost one wins; `at` selects the plane offset along the direction and
    `contains` a point that must lie on the face."""
    d = _dir(direction)
    feat = _feature(obj)
    s = _shape_of(obj)
    hits = []
    for i, f in enumerate(s.Faces):
        if f.Surface.__class__.__name__ != "Plane":
            continue
        u0, u1, v0, v1 = f.ParameterRange
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        if n.dot(d) < 1 - 1e-6:
            continue
        off = f.CenterOfMass.dot(d)
        if at is not None and abs(off - float(at)) > 1e-4:
            continue
        if contains is not None and not f.isInside(Vector(*contains), 1e-4, True):
            continue
        hits.append((off, f.Area, i, f))
    if not hits:
        raise FcError("no planar face of %s faces %s%s" % (
            obj.Label, direction, "" if at is None else " at %g" % float(at)))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    _, _, i, f = hits[0]
    return FaceRef(feat, "Face%d" % (i + 1), f)


def edges(obj, kind=None, parallel=None, on=None, r=None, at=None, axis="Z", tol=1e-6):
    """Edges of obj, filtered (all filters combine):
      kind      'line' | 'circle'
      parallel  lines along 'X' | 'Y' | 'Z' (or a vector)
      on        a FaceRef (fc.face(...)): edges lying on that face
      r         circles of radius r
      at        edges lying entirely in the plane at coordinate `at` along
                `axis` (default 'Z'): at=34 → edges in the plane z = 34, e.g.
                the end circles of a part turned along Z; axis='X', at=-20 →
                edges in the plane x = -20.
    Selections combine with +: fc.edges(b, kind="circle", at=0) +
    fc.edges(b, kind="circle", at=34) chamfers both ends in one feature."""
    feat = _feature(obj)
    s = _shape_of(obj)
    on_face = on.face if isinstance(on, FaceRef) else None
    at_dir = _dir(axis) if at is not None else None
    names = []
    for i, e in enumerate(s.Edges):
        cname = e.Curve.__class__.__name__
        if kind == "line" and cname not in ("Line", "LineSegment"):
            continue
        if kind == "circle" and cname != "Circle":
            continue
        if parallel is not None:
            if cname not in ("Line", "LineSegment"):
                continue
            dv = e.Vertexes[-1].Point - e.Vertexes[0].Point
            if dv.Length < tol or abs(abs(dv.normalize().dot(_dir(parallel))) - 1) > 1e-6:
                continue
        if r is not None and (cname != "Circle" or abs(e.Curve.Radius - float(r)) > 1e-4):
            continue
        if on_face is not None:
            if not all(on_face.isInside(v.Point, 1e-4, True) for v in e.Vertexes) or \
               not on_face.isInside(e.valueAt((e.FirstParameter + e.LastParameter) / 2), 1e-4, True):
                continue
        if at_dir is not None:
            ps = [v.Point for v in e.Vertexes] + [e.valueAt(e.FirstParameter + (e.LastParameter - e.FirstParameter)
                                                            * k / 4.0) for k in (1, 2, 3)]
            if any(abs(p.dot(at_dir) - float(at)) > 1e-4 for p in ps):
                continue
        names.append("Edge%d" % (i + 1))
    if not names:
        raise FcError("no edges of %s match (kind=%s parallel=%s r=%s on=%s%s)"
                      % (obj.Label, kind, parallel, r, on, "" if at is None else " at=%g along %s" % (float(at), axis)))
    return EdgeRef(feat, names)


# ------------------------------------------------------------------ bodies

def body(name):
    """A new PartDesign Body labelled `name` — one per manufactured part."""
    b = _ctx.doc.addObject("PartDesign::Body", _safe_name(name))
    b.Label = name
    return b


def _safe_name(label):
    s = "".join(c if c.isalnum() else "_" for c in str(label))
    return s if s and not s[0].isdigit() else "_" + s


def _origin_plane(bd, which):
    want = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}[which]
    for f in bd.Origin.OriginFeatures:
        if f.Role == want or f.Name.startswith(want):
            return f
    raise FcError("body %s has no %s" % (bd.Label, want))


# --------------------------------------------------------------- sketching

class _Frame:
    """Maps global points to sketch-local 2D coordinates."""

    def __init__(self, sk):
        self.pl = sk.getGlobalPlacement()
        self.inv = self.pl.inverse()
        self.normal = self.pl.Rotation.multVec(Vector(0, 0, 1))
        self.axes = _plane_axes(self.normal)
        self.origin = self.pl.Base

    def local(self, p):
        if len(p) == 3:
            g = Vector(*p)
        else:
            g = Vector(self.origin)
            comps = [g.x, g.y, g.z]
            comps[self.axes[0]] = float(p[0])
            comps[self.axes[1]] = float(p[1])
            g = Vector(*comps)
        q = self.inv.multVec(g)
        return q.x, q.y

    def local_dir(self, axis_index):
        v = [0.0, 0.0, 0.0]
        v[axis_index] = 1.0
        q = self.inv.Rotation.multVec(Vector(*v))
        return q.x, q.y


def sketch(bd, on, *profiles, name=None, offset=0.0):
    """A fully constrained sketch in body `bd` on an origin plane ('XY', 'XZ',
    'YZ'), a FaceRef from fc.face, or a Placement. Profiles are fc.rect,
    fc.rrect, fc.circle, fc.polygon, fc.points."""
    sk = bd.newObject("Sketcher::SketchObject", _safe_name(name or "Sketch"))
    if name:
        sk.Label = name
    if isinstance(on, str):
        sk.AttachmentSupport = [(_origin_plane(bd, on.upper()), "")]
        sk.MapMode = "FlatFace"
    elif isinstance(on, FaceRef):
        sk.AttachmentSupport = on.support()
        sk.MapMode = "FlatFace"
    elif isinstance(on, App.Placement):
        sk.MapMode = "Deactivated"
        sk.Placement = on
    else:
        raise FcError("sketch `on` must be 'XY'|'XZ'|'YZ', a FaceRef or a Placement")
    if offset:
        sk.AttachmentOffset = App.Placement(Vector(0, 0, float(offset)), App.Rotation())
    _ctx.doc.recompute()
    fr = _Frame(sk)
    for p in profiles:
        p._add(sk, fr)
    _ctx.doc.recompute()
    return sk


def _dist_xy(sk, geo, pos, u, v):
    """Lock a point to local (u, v) relative to the sketch origin."""
    a = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, geo, pos, u))
    b = sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, geo, pos, v))
    return a, b


class _Profile:
    def _add(self, sk, fr):
        raise NotImplementedError


class rect(_Profile):
    """Axis-aligned rectangle w x h (global axes of the plane) centred at `center`."""

    def __init__(self, w, h, center=(0, 0)):
        self.w, self.h, self.center = w, h, center

    def _add(self, sk, fr):
        u0, v0 = fr.local(self.center)
        w, h = self.w, self.h
        # does the plane's first global axis run along local u or local v?
        du = fr.local_dir(fr.axes[0])
        if abs(du[1]) > abs(du[0]):
            w, h = h, w
        W, H = float(w), float(h)
        c = [(u0 - W / 2, v0 - H / 2), (u0 + W / 2, v0 - H / 2),
             (u0 + W / 2, v0 + H / 2), (u0 - W / 2, v0 + H / 2)]
        g = [sk.addGeometry(Part.LineSegment(Vector(*c[i], 0), Vector(*c[(i + 1) % 4], 0)))
             for i in range(4)]
        cp = sk.addGeometry(Part.Point(Vector(u0, v0, 0)), True)
        for i in range(4):
            sk.addConstraint(Sketcher.Constraint("Coincident", g[i], 2, g[(i + 1) % 4], 1))
        sk.addConstraint(Sketcher.Constraint("Horizontal", g[0]))
        sk.addConstraint(Sketcher.Constraint("Horizontal", g[2]))
        sk.addConstraint(Sketcher.Constraint("Vertical", g[1]))
        sk.addConstraint(Sketcher.Constraint("Vertical", g[3]))
        iw = sk.addConstraint(Sketcher.Constraint("DistanceX", g[0], 1, g[0], 2, W))
        ih = sk.addConstraint(Sketcher.Constraint("DistanceY", g[1], 1, g[1], 2, H))
        _bind_constraint(sk, iw, w)
        _bind_constraint(sk, ih, h)
        sk.addConstraint(Sketcher.Constraint("Symmetric", g[0], 1, g[2], 1, cp, 1))
        _dist_xy(sk, cp, 1, u0, v0)


class rrect(_Profile):
    """Rounded rectangle w x h with corner radius r, centred at `center`."""

    def __init__(self, w, h, r, center=(0, 0)):
        self.w, self.h, self.r, self.center = w, h, r, center

    def _add(self, sk, fr):
        if float(self.r) <= 0:
            return rect(self.w, self.h, self.center)._add(sk, fr)
        u0, v0 = fr.local(self.center)
        w, h = self.w, self.h
        du = fr.local_dir(fr.axes[0])
        if abs(du[1]) > abs(du[0]):
            w, h = h, w
        W, H, r = float(w), float(h), float(self.r)
        if 2 * r >= min(W, H):
            raise FcError("rrect radius %g too large for %g x %g" % (r, W, H))
        x0, x1, y0, y1 = u0 - W / 2, u0 + W / 2, v0 - H / 2, v0 + H / 2
        L = [sk.addGeometry(Part.LineSegment(Vector(x0 + r, y0, 0), Vector(x1 - r, y0, 0))),   # bottom
             sk.addGeometry(Part.LineSegment(Vector(x1, y0 + r, 0), Vector(x1, y1 - r, 0))),   # right
             sk.addGeometry(Part.LineSegment(Vector(x1 - r, y1, 0), Vector(x0 + r, y1, 0))),   # top
             sk.addGeometry(Part.LineSegment(Vector(x0, y1 - r, 0), Vector(x0, y0 + r, 0)))]   # left
        z = Vector(0, 0, 1)
        A = [sk.addGeometry(Part.ArcOfCircle(Part.Circle(Vector(x1 - r, y0 + r, 0), z, r), -math.pi / 2, 0)),
             sk.addGeometry(Part.ArcOfCircle(Part.Circle(Vector(x1 - r, y1 - r, 0), z, r), 0, math.pi / 2)),
             sk.addGeometry(Part.ArcOfCircle(Part.Circle(Vector(x0 + r, y1 - r, 0), z, r), math.pi / 2, math.pi)),
             sk.addGeometry(Part.ArcOfCircle(Part.Circle(Vector(x0 + r, y0 + r, 0), z, r), math.pi, 1.5 * math.pi))]
        # arc k runs from line k's end to line k+1's start (counter-clockwise)
        for k in range(4):
            sk.addConstraint(Sketcher.Constraint("Tangent", L[k], 2, A[k], 1))
            sk.addConstraint(Sketcher.Constraint("Tangent", A[k], 2, L[(k + 1) % 4], 1))
        sk.addConstraint(Sketcher.Constraint("Horizontal", L[0]))
        sk.addConstraint(Sketcher.Constraint("Horizontal", L[2]))
        sk.addConstraint(Sketcher.Constraint("Vertical", L[1]))
        sk.addConstraint(Sketcher.Constraint("Vertical", L[3]))
        for k in range(1, 4):
            sk.addConstraint(Sketcher.Constraint("Equal", A[0], A[k]))
        ir = sk.addConstraint(Sketcher.Constraint("Radius", A[0], r))
        iw = sk.addConstraint(Sketcher.Constraint("DistanceX", L[3], 1, L[1], 1, W))
        ih = sk.addConstraint(Sketcher.Constraint("DistanceY", L[0], 1, L[2], 1, H))
        _bind_constraint(sk, ir, self.r)
        _bind_constraint(sk, iw, w)
        _bind_constraint(sk, ih, h)
        cp = sk.addGeometry(Part.Point(Vector(u0, v0, 0)), True)
        sk.addConstraint(Sketcher.Constraint("Symmetric", L[0], 1, L[2], 1, cp, 1))
        _dist_xy(sk, cp, 1, u0, v0)


class circle(_Profile):
    """Circle of diameter d (or radius r) centred at `at`."""

    def __init__(self, d=None, at=(0, 0), r=None):
        if d is None and r is None:
            raise FcError("circle needs d or r")
        self.d, self.r, self.at = d, r, at

    def _add(self, sk, fr):
        u, v = fr.local(self.at)
        rad = float(self.r) if self.r is not None else float(self.d) / 2
        g = sk.addGeometry(Part.Circle(Vector(u, v, 0), Vector(0, 0, 1), rad))
        if self.d is not None:
            i = sk.addConstraint(Sketcher.Constraint("Diameter", g, float(self.d)))
            _bind_constraint(sk, i, self.d)
        else:
            i = sk.addConstraint(Sketcher.Constraint("Radius", g, rad))
            _bind_constraint(sk, i, self.r)
        _dist_xy(sk, g, 3, u, v)


class polygon(_Profile):
    """Closed polygon through points (each vertex locked in place)."""

    def __init__(self, pts):
        if len(pts) < 3:
            raise FcError("polygon needs at least 3 points")
        self.pts = pts

    def _add(self, sk, fr):
        loc = [fr.local(p) for p in self.pts]
        n = len(loc)
        g = [sk.addGeometry(Part.LineSegment(Vector(*loc[i], 0), Vector(*loc[(i + 1) % n], 0)))
             for i in range(n)]
        for i in range(n):
            sk.addConstraint(Sketcher.Constraint("Coincident", g[i], 2, g[(i + 1) % n], 1))
            _dist_xy(sk, g[i], 1, *loc[i])


class points(_Profile):
    """Small circles used only as positions (e.g. hole centres)."""

    def __init__(self, pts, d=1.0):
        self.pts, self.d = pts, d

    def _add(self, sk, fr):
        for p in self.pts:
            circle(d=self.d, at=p)._add(sk, fr)


# ---------------------------------------------------------------- features

def pad(bd, sk, length, reverse=False, symmetric=False, name=None):
    """Extrude a sketch by `length` (a Param stays bound to the spreadsheet);
    reverse flips the direction, symmetric pads both ways."""
    f = bd.newObject("PartDesign::Pad", _safe_name(name or "Pad"))
    if name:
        f.Label = name
    f.Profile = sk
    _set(f, "Length", length)
    f.Reversed = bool(reverse)
    f.Midplane = bool(symmetric)
    _check(f)
    return f


def pocket(bd, sk, depth="through", reverse=False, name=None):
    """Cut a sketch into the body: depth 'through' or mm."""
    f = bd.newObject("PartDesign::Pocket", _safe_name(name or "Pocket"))
    if name:
        f.Label = name
    f.Profile = sk
    if depth == "through":
        f.Type = "ThroughAll"
    else:
        f.Type = "Length"
        _set(f, "Length", depth)
    f.Reversed = bool(reverse)
    _check(f)
    return f


def revolve(bd, sk, axis="V_Axis", angle=360.0, name=None):
    """PartDesign Revolution of a sketch about one of its own axes
    ('V_Axis' | 'H_Axis') — the basis of every turned part."""
    f = bd.newObject("PartDesign::Revolution", _safe_name(name or "Revolution"))
    if name:
        f.Label = name
    f.Profile = sk
    f.ReferenceAxis = (sk, [axis])
    _set(f, "Angle", angle)
    _check(f)
    return f


def turned(bd, sections, axis="Z", start=0.0, name="Turned"):
    """A turned (lathe) part from its diameter steps, as one fully constrained
    half-profile revolved about a global axis ('Z' only for now).

    sections: [(diameter, length) or (diameter, length, fit), ...] from the
    start face outward, e.g. [(20, 6), (12, 28, "k6"), (8, 14)] → a Ø20×6
    collar, a Ø12×28 k6 bearing seat, a Ø8×14 spigot. Fits go on the drawing
    with their ISO 286 limits, on the step that carries them. The body records
    each step in FcSteps (JSON list of {"d", "z0", "z1", "fit"}; z along the
    turning axis in the body's coordinates, the first step starting at
    `start`) — the same z as fc.external_thread's z_from. Returns the
    Revolution feature."""
    if str(axis).upper().lstrip("+") != "Z":
        raise FcError("turned(): only axis='Z' is supported — orient the part along Z and place it with a frame")
    pts = [(0.0, float(start))]
    z = float(start)
    fits = {}
    steps = []
    for sec in sections:
        d, L = sec[0], sec[1]
        fit = None
        if len(sec) > 2 and sec[2]:
            standards.tolerance(float(d), str(sec[2]))          # validates the class
            fit = str(sec[2])
            fits["%g" % float(d)] = fit
        r = float(d) / 2
        pts.append((r, z))
        steps.append({"d": float(d), "z0": round(z, 6), "z1": round(z + float(L), 6), "fit": fit})
        z += float(L)
        pts.append((r, z))
    pts.append((0.0, z))
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) > 1e-9 or abs(p[1] - clean[-1][1]) > 1e-9:
            clean.append(p)
    sk = sketch(bd, "XZ", polygon([(x, 0.0, zz) for x, zz in clean]), name=name + "Profile")
    f = revolve(bd, sk, axis="V_Axis", name=name)
    if fits:
        if "FcFits" not in bd.PropertiesList:
            bd.addProperty("App::PropertyString", "FcFits", "fclib", "ISO 286 fits by diameter (JSON)")
        data = json.loads(bd.FcFits or "{}")
        data.update(fits)
        bd.FcFits = json.dumps(data)
    if "FcSteps" not in bd.PropertiesList:
        bd.addProperty("App::PropertyString", "FcSteps", "fclib",
                       "turned steps: [{d, z0, z1, fit}] along the turning axis (JSON)")
    bd.FcSteps = json.dumps(json.loads(bd.FcSteps or "[]") + [st for st in steps if st["z1"] > st["z0"]])
    return f


def external_thread(obj, size, length, z_from, fit="6g", name=None):
    """Record an external thread (cosmetic): spec, length and where it starts,
    for the drawing callout and the release notes. The model keeps the major
    diameter — model the actual thread only when 3D printing it."""
    s = standards.norm_size(size)
    data = json.loads(getattr(obj, "FcThreads", "") or "[]")
    data.append({"size": s, "pitch": standards.PITCH[s], "fit": fit, "length": float(length),
                 "z_from": float(z_from), "name": name or "%s thread" % s})
    if "FcThreads" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "FcThreads", "fclib", "external threads (JSON)")
    obj.FcThreads = json.dumps(data)
    return data[-1]


def fillet(bd, edge_ref, r, name=None):
    """Round the edges selected with fc.edges(...) — do fillets last."""
    f = bd.newObject("PartDesign::Fillet", _safe_name(name or "Fillet"))
    if name:
        f.Label = name
    f.Base = (edge_ref.obj, edge_ref.names)
    _set(f, "Radius", r)
    _check(f)
    return f


def chamfer(bd, edge_ref, size, name=None):
    """Chamfer the edges selected with fc.edges(...) by `size` mm."""
    f = bd.newObject("PartDesign::Chamfer", _safe_name(name or "Chamfer"))
    if name:
        f.Label = name
    f.Base = (edge_ref.obj, edge_ref.names)
    _set(f, "Size", size)
    _check(f)
    return f


def _check(feat):
    """Recompute and turn a silent feature failure into a warning now, so the
    script author sees it at the line that caused it."""
    _ctx.doc.recompute()
    st = feat.State
    if "Invalid" in st or "Error" in st:
        msg = feat.getStatusString() if hasattr(feat, "getStatusString") else "failed"
        warn("%s failed: %s" % (feat.Label, msg))


# ------------------------------------------------------------------- holes

_CUT_TYPES = {"ISO4762": "ISO 4762", "ISO4762+7089": "ISO 4762 + 7089", "DIN7984": "DIN 7984",
              "ISO10642": "ISO 10642", "ISO7046": "ISO 7046", "ISO14583": "ISO 14583",
              "ISO2009": "ISO 2009"}
_CSK_TYPES = ("ISO 10642", "ISO 7046", "ISO 2009")      # standard cuts that are countersinks
_FITS = {"fine": "Fine", "close": "Fine", "normal": "Medium", "medium": "Medium",
         "coarse": "Coarse", "loose": "Coarse"}


def _parse_spec(spec):
    """'M3 tapped' | 'M3 clearance normal + cbore ISO4762' | 'M3 insert' | 'Ø4.2' |
    'Ø8 H7' | 'd4.2 + cbore 8x3' | 'M3 clearance + cbore DIN974'."""
    s = str(spec).replace("Ø", "d").replace("ø", "d").strip()
    main, _, cut = s.partition("+")
    toks = main.split()
    if not toks:
        raise FcError("empty hole spec")
    out = {"size": toks[0], "kind": "plain", "fit": "normal", "cut": None, "fit_class": None}
    t0 = toks[0].lower()
    if t0.startswith("m"):
        try:
            out["size"] = standards.norm_size(toks[0])
        except ValueError as e:
            raise FcError(str(e))
        out["kind"] = toks[1].lower() if len(toks) > 1 else "clearance"
        if out["kind"] in ("insert", "heat-set", "heatset"):
            out["kind"] = "insert"
        elif len(toks) > 2:
            out["fit"] = toks[2].lower()
    elif t0.startswith("d"):
        out["diameter"] = float(t0[1:])
        if len(toks) > 1:
            if not re.match(r"^(H|G|JS)\d+$", toks[1], re.I):
                raise FcError("after Ø<d> expect a hole class like H7, got %r" % toks[1])
            out["fit_class"] = toks[1].upper()
    else:
        raise FcError("hole spec must start with M<size> or Ø<diameter>, got %r" % spec)
    if t0.startswith("m") and out["kind"] not in ("tapped", "clearance", "insert"):
        raise FcError("hole kind must be 'tapped', 'clearance' or 'insert', got %r" % out["kind"])
    if out["fit"] not in _FITS:
        raise FcError("clearance fit must be fine | normal | coarse, got %r" % out["fit"])
    cut = cut.strip()
    if cut:
        ct = cut.split()
        what = ct[0].lower()
        if what not in ("cbore", "csk"):
            raise FcError("hole cut must be 'cbore ...' or 'csk ...', got %r" % cut)
        rest = " ".join(ct[1:]).replace(" ", "")
        if rest.upper() in _CUT_TYPES:
            out["cut"] = ("standard", _CUT_TYPES[rest.upper()])
        elif rest.upper() == "DIN974":
            if not t0.startswith("m"):
                raise FcError("'cbore DIN974' needs a metric size (e.g. 'M4 clearance + cbore DIN974')")
            d, h = standards.counterbore(out["size"])
            out["cut"] = ("cbore", d, h)
        elif "x" in rest.lower():
            dd, hh = rest.lower().split("x", 1)
            out["cut"] = (what, float(dd), float(hh))
        elif what == "csk" and rest:
            out["cut"] = ("csk", float(rest), 90.0)
        else:
            raise FcError("unknown hole cut %r (use ISO4762, ISO10642, DIN974 …, or "
                          "'cbore <dia>x<depth>' / 'csk <dia>')" % cut)
    return out


def _norm_process(p):
    from manufacture import norm_process        # lazy: manufacture imports analysis
    return norm_process(p)


def _body_process(bd):
    pj = _project()
    return _norm_process(((pj.get("parts") or {}).get(bd.Label) or {}).get("process") or pj.get("process") or "fdm")


def _hole_comp(bd, compensation):
    """Diametral hole compensation (mm) for body bd: the explicit value, else
    project.json dfm.hole_compensation_mm when the body's process is fdm/sla;
    None when a printed part's project does not set it."""
    if compensation is not None:
        try:
            return float(compensation)
        except (TypeError, ValueError):
            raise FcError("holes(): compensation must be a number of mm (diametral), got %r" % (compensation,))
    if _body_process(bd) not in ("fdm", "sla"):
        return 0.0
    v = (_project().get("dfm") or {}).get("hole_compensation_mm")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        warn("project.json dfm.hole_compensation_mm is not a number — holes are not compensated")
        return 0.0


_TYPICAL_COMP = {"fdm": 0.2, "sla": 0.1}


def _warn_uncompensated(bd, kind, n):
    """One warning per printed body whose project sets no hole compensation,
    counting its clearance and plain holes (updated as more are added)."""
    proc = _body_process(bd)
    rec = _ctx.uncomp.setdefault(bd.Label, {"clearance": 0, "plain": 0, "idx": None})
    rec[kind] += n
    bits = ["%d %s hole%s" % (rec[k], w, "" if rec[k] == 1 else "s")
            for k, w in (("clearance", "clearance"), ("plain", "plain Ø")) if rec[k]]
    total = rec["clearance"] + rec["plain"]
    msg = ("%s is %s but project.json has no dfm.hole_compensation_mm — its %s %s NOT compensated and will print "
           "undersize; set %g (typical %s) or 0 to confirm none" % (
               bd.Label, proc, " and ".join(bits), "is" if total == 1 else "are", _TYPICAL_COMP.get(proc, 0.2),
               proc.upper()))
    if rec["idx"] is None:
        rec["idx"] = len(_ctx.warnings)
        _ctx.warnings.append(msg)
    else:
        _ctx.warnings[rec["idx"]] = msg


def holes(bd, on, at, spec, depth=None, thread="cosmetic", name=None, reverse=False, offset=0.0,
          compensation=None):
    """Standard holes drilled into `on` (a FaceRef or 'XY'|'XZ'|'YZ') at the
    given points. spec examples:
      'M3 tapped'                         threaded 6H, cosmetic unless thread='modelled'
      'M3 clearance normal'               ISO 273 fine | normal | coarse
      'M3 clearance normal + cbore ISO4762'  FreeCAD's counterbore for a socket head
      'M3 clearance + cbore DIN974'       DIN 974-1 counterbore (standards table)
      'M3 clearance + csk ISO10642'       countersink for a flat head
      'M3 insert'                         heat-set insert hole (vendor-typical; blind)
      'Ø8 H7'                             fit bore, nominal Ø8, class recorded for drawings
      'Ø4.2'  ·  'Ø6 + cbore 10x4'        plain / custom
    depth: 'through' (default; inserts default to insert length + 1) or mm.
    offset moves the sketch plane along its normal — e.g. start a cross hole
    from outside a round part: on="XZ", offset=+(radius + 1).
    compensation (mm, diametral) enlarges clearance and plain Ø holes and their
    counterbore/countersink for printing; None takes project.json
    dfm.hole_compensation_mm when the body's process is fdm or sla (a printed
    part whose project.json has no such key gets a build warning — set it, or
    0 to confirm none). Tapped, insert and fit (H7) holes are never
    compensated."""
    sp = _parse_spec(spec)
    if sp["kind"] == "insert":
        ins_d, ins_len, ins_depth = standards.heat_set_insert(sp["size"])
        sp["diameter"] = ins_d
        if depth is None:
            depth = ins_depth
        warn("%s: heat-set insert hole Ø%g ↧%s from vendor-typical data — confirm with the "
             "insert's datasheet" % (name or "Holes", ins_d, depth))
    if depth is None:
        depth = "through"
    pts = list(at)
    if not pts:
        raise FcError("holes(): `at` is empty")
    d_marker = sp.get("diameter") or float(sp["size"][1:])
    sk = sketch(bd, on, points(pts, d=d_marker), name=(name or "Holes") + "_pts", offset=offset)
    h = bd.newObject("PartDesign::Hole", _safe_name(name or "Holes"))
    if name:
        h.Label = name
    h.Profile = sk
    if "diameter" in sp:
        h.ThreadType = "None"
        h.Threaded = False
        h.Diameter = sp["diameter"]
    else:
        h.ThreadType = "ISOMetricProfile"
        size = sp["size"].upper()
        choices = [c for c in h.getEnumerationsOfProperty("ThreadSize") if c.upper().startswith(size + "X")]
        if not choices:
            raise FcError("unknown metric size %s" % size)
        h.ThreadSize = choices[0]
        if sp["kind"] == "tapped":
            h.Threaded = True
            if "6H" in h.getEnumerationsOfProperty("ThreadClass"):
                h.ThreadClass = "6H"
            h.ModelThread = (thread == "modelled")
        else:
            h.Threaded = False
            h.ThreadFit = _FITS[sp["fit"]]
    cut = sp["cut"]
    if cut:
        if cut[0] == "standard":
            h.HoleCutType = cut[1]
        elif cut[0] == "cbore":
            h.HoleCutType = "Counterbore"
            h.HoleCutDiameter = cut[1]
            h.HoleCutDepth = cut[2]
        else:
            h.HoleCutType = "Countersink"
            h.HoleCutDiameter = cut[1]
            h.HoleCutCountersinkAngle = cut[2]
    if depth == "through":
        h.DepthType = "ThroughAll"
    else:
        h.DepthType = "Dimension"
        _set(h, "Depth", depth)
    h.Reversed = bool(reverse)
    h.addProperty("App::PropertyString", "FcSpec", "fclib", "hole spec as written")
    h.FcSpec = str(spec)
    if sp.get("fit_class"):
        h.addProperty("App::PropertyString", "FcFit", "fclib", "ISO 286 hole class")
        h.FcFit = sp["fit_class"]
    eligible = sp["kind"] in ("plain", "clearance") and not sp.get("fit_class")
    comp = _hole_comp(bd, compensation) if eligible else 0.0
    if comp is None:                          # printed part, project sets no compensation
        comp = 0.0
        if _project():
            _warn_uncompensated(bd, sp["kind"], len(pts))
    if compensation and not eligible:
        warn("%s: compensation ignored — tapped, insert and fit (H7 …) holes are modelled at their nominal size"
             % (name or "Holes"))
    want = None
    if comp:
        # FreeCAD re-derives a clearance hole's diameter and a standard cut from
        # its tables (on ThreadFit/DepthType changes and whenever the file is
        # opened), so a compensated hole is stored as a plain hole with explicit
        # values; the spec as written stays in FcSpec
        cutv = None
        if cut:
            csk = cut[0] == "csk" or h.HoleCutType in _CSK_TYPES or h.HoleCutType == "Countersink"
            cutv = ("Countersink" if csk else "Counterbore", h.HoleCutDiameter.Value + comp, h.HoleCutDepth.Value,
                    h.HoleCutCountersinkAngle.Value)
        want = (h.Diameter.Value + comp, cutv)
        h.ThreadType = "None"
        h.Threaded = False
        h.addProperty("App::PropertyFloat", "FcHoleComp", "fclib", "diametral hole compensation for printing (mm)")
        h.FcHoleComp = comp
        _set_comp(h, want)
    _check(h)
    if want and abs(h.Diameter.Value - want[0]) > 1e-6:      # reset during recompute: set it once more
        _set_comp(h, want)
        _check(h)
    _check_removed(h)
    return h


def _set_comp(h, want):
    d, cutv = want
    if cutv:
        h.HoleCutType = cutv[0]
        h.HoleCutDiameter = cutv[1]
        if cutv[0] == "Counterbore":
            h.HoleCutDepth = cutv[2]
        else:
            h.HoleCutCountersinkAngle = cutv[3]
    h.Diameter = d


def _check_removed(h):
    """Warn when a hole cut nothing — it drilled away from the part or missed it."""
    base = getattr(h, "BaseFeature", None)
    st = list(h.State)
    if base is None or "Invalid" in st or "Error" in st:
        return
    try:
        removed = base.Shape.Volume - h.Shape.Volume
    except Exception:  # noqa: BLE001
        return
    if removed < 1e-6:
        warn("hole %s removed no material — check on/at/offset/reverse (it may drill away from the part: "
             "a cross hole on a round part needs offset=+(radius + 1), or reverse=True)" % h.Label)


def nut_trap(bd, on, at, size, depth=None, clearance=0.2, angle=0.0, name=None):
    """Hexagonal pockets for ISO 4032 nuts at the given points: across-flats
    + clearance, depth = nut height + clearance unless given. Add the bolt's
    clearance hole separately with fc.holes."""
    af, dflt_depth = standards.nut_trap(size, clearance)
    R = af / math.sqrt(3.0)
    polys = []
    for p in at:
        if len(p) == 3:
            raise FcError("nut_trap points must be 2D, in the face's global axes")
        verts = [(p[0] + R * math.cos(math.radians(angle + 30 + 60 * k)),
                  p[1] + R * math.sin(math.radians(angle + 30 + 60 * k))) for k in range(6)]
        polys.append(polygon(verts))
    sk = sketch(bd, on, *polys, name=(name or "NutTrap") + "_hex")
    f = pocket(bd, sk, depth if depth is not None else dflt_depth, name=name or "NutTrap")
    f.addProperty("App::PropertyString", "FcSpec", "fclib", "nut trap spec")
    f.FcSpec = "%s nut-trap ISO4032 AF%g" % (standards.norm_size(size), af)
    return f


# ------------------------------------------------------- reference parts

def _role(obj, role):
    if "FcRole" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "FcRole", "fclib", "part | ref")
    obj.FcRole = role


_BOM_ROLES = ("buy", "context", "none")


def _mesh_shape(p, tol=0.05):
    """(shape, closed) from an STL/OBJ file: one solid per closed shell, or the
    bare triangles when the mesh is not closed."""
    import Mesh
    m = Mesh.Mesh(p)
    if not m.CountFacets:
        raise FcError("%s contains no triangles" % p)
    sh = Part.Shape()
    sh.makeShapeFromMesh(m.Topology, tol)
    closed = bool(m.isSolid()) and not m.hasNonManifolds() and bool(sh.Shells)
    if not closed:
        return sh, False
    if len(sh.Shells) == 1:
        return Part.Solid(sh.Shells[0]), True
    s = Part.makeSolid(sh)                       # nested shells → one solid with voids
    if not s.isValid():
        s = Part.makeCompound([Part.Solid(x) for x in sh.Shells])
    return s, True


def ref_part(name, step=None, shape=None, placement=None, frames=None, color=None, bom="context", qty=1,
             spec=None):
    """A bought-out / reference part used for fit and alignment checks and
    excluded from mass. Give a vendor file (`step`: STEP, IGES, BREP, STL or
    OBJ, path relative to the project) or a Part shape built from primitives;
    `frames` names its interfaces.
    bom: 'buy' — purchased with this order (qty = total to buy, spec = order
    text, e.g. 'NEMA17 42x40 1.8°') · 'context' (default) — customer-owned or
    environment, listed as reference only · 'none' — keep-outs and envelopes,
    listed nowhere."""
    if bom not in _BOM_ROLES:
        raise FcError("ref_part %s: bom must be 'buy' (purchased with this order), 'context' (customer-owned / "
                      "environment, listed as reference) or 'none' (keep-out, not listed), got %r" % (name, bom))
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        raise FcError("ref_part %s: qty must be a whole number ≥ 1 (the total to buy)" % name)
    if step:
        p = path(step)
        if not os.path.isfile(p):
            raise FcError("reference file not found: %s — put it in the project's refs/ folder" % p)
        ext = os.path.splitext(p)[1].lower()
        if ext in (".stl", ".obj"):
            shp, closed = _mesh_shape(p)
            if not closed:
                warn("ref_part %s: %s is not a closed mesh — kept as surfaces, so volume, interference and "
                     "clearance checks against it are unreliable; repair the mesh or use the vendor STEP"
                     % (name, os.path.basename(p)))
        elif ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
            shp = Part.read(p)
        else:
            raise FcError("ref_part %s: unsupported file type %s — use STEP, IGES, BREP, STL or OBJ" % (name, ext))
    elif shape is not None:
        shp = shape
    else:
        raise FcError("ref_part needs step= (a STEP/IGES/BREP/STL/OBJ file) or shape=")
    o = _ctx.doc.addObject("Part::Feature", _safe_name(name))
    o.Label = name
    o.Shape = shp
    _role(o, "ref")
    o.addProperty("App::PropertyString", "FcBom", "fclib", "BOM role (JSON: bom, qty, spec)")
    o.FcBom = json.dumps({"bom": bom, "qty": qty, "spec": None if spec is None else str(spec)})
    if placement is not None:
        o.Placement = placement
    if frames:
        set_frames(o, frames)
    if color:
        set_color(o, color)
    return o


def set_frames(obj, frames):
    """Attach named local frames (Placements) to an object."""
    if "FcFrames" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "FcFrames", "fclib", "named local frames (JSON)")
    data = json.loads(obj.FcFrames or "{}")
    for k, pl in frames.items():
        q = pl.Rotation.Q
        data[k] = [pl.Base.x, pl.Base.y, pl.Base.z, q[0], q[1], q[2], q[3]]
    obj.FcFrames = json.dumps(data)


def get_frame(obj, name, local=False):
    """The named frame of obj (set with set_frames or ref_part(frames=…)) as a
    Placement: global by default, in obj's own coordinates with local=True."""
    data = json.loads(getattr(obj, "FcFrames", "") or "{}")
    if name not in data:
        raise FcError("%s has no frame '%s' (frames: %s)" % (obj.Label, name, ", ".join(data) or "none"))
    x, y, z, a, b, c, d = data[name]
    pl = App.Placement(Vector(x, y, z), App.Rotation(a, b, c, d))
    return pl if local else obj.getGlobalPlacement().multiply(pl)


def set_color(obj, rgb):
    """Colour obj in renders and build images: rgb = (r, g, b), each 0..1."""
    try:
        vals = [float(c) for c in rgb]
    except (TypeError, ValueError):
        vals = []
    if len(vals) != 3:
        raise FcError("set_color: rgb must be three numbers 0..1, e.g. (0.8, 0.2, 0.2), got %r" % (rgb,))
    if "FcColor" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "FcColor", "fclib", "render colour r,g,b 0..1")
    obj.FcColor = ",".join("%g" % c for c in vals)


def frame(x=0.0, y=0.0, z=0.0, normal="+Z", xdir=None):
    """A Placement at (x, y, z) whose local +Z points along `normal`."""
    n = _dir(normal)
    rot = App.Rotation(Vector(0, 0, 1), n)
    if xdir is not None:
        xd = _dir(xdir)
        cur = rot.multVec(Vector(1, 0, 0))
        xd = xd - n * xd.dot(n)
        if xd.Length > 1e-9:
            xd.normalize()
            ang = math.degrees(math.atan2(cur.cross(xd).dot(n), cur.dot(xd)))
            rot = App.Rotation(n, ang).multiply(rot)
    return App.Placement(Vector(float(x), float(y), float(z)), rot)


def _coords(p, what):
    try:
        vals = [] if isinstance(p, str) else \
            [float(p.x), float(p.y), float(p.z)] if isinstance(p, Vector) else [float(v) for v in p]
    except (TypeError, ValueError):
        vals = []
    if len(vals) not in (2, 3):
        raise FcError("%s must be a 2D point in the face's axes ((x, y) on ±Z faces, (x, z) on ±Y, (y, z) on ±X) "
                      "or a 3D point, got %r" % (what, p))
    return vals


def frame_on(face_ref, at=None):
    """A frame on a face: origin at its centre (or `at`), +Z along the outward
    normal. `at` is a 3D point, or a 2D point in the face's dominant global
    axes like every fclib 2D point — (x, y) on ±Z faces, (x, z) on ±Y faces,
    (y, z) on ±X faces — projected onto the face's plane. Coordinates are
    those of the face's feature (global for bodies at the origin, which is how
    fclib creates them)."""
    c, n = face_ref.center, face_ref.normal
    if at is None:
        p = c
    else:
        vals = _coords(at, "frame_on(at=…)")
        if len(vals) == 3:
            p = Vector(*vals)
        else:
            comps = [c.x, c.y, c.z]
            ax = _plane_axes(n)
            comps[ax[0]], comps[ax[1]] = vals
            g = Vector(*comps)
            p = g - n * (g - c).dot(n)
    return frame(p.x, p.y, p.z, normal=n)


def mate(part, part_frame, target, target_frame=None, mode="against", offset=0.0, rotate=0.0):
    """Place `part` so its named frame meets a target frame.

    target: a Placement (global), or an object with target_frame naming one of
    its frames. mode 'against' puts the frames face to face (Z axes opposed,
    as when a motor's mounting face sits on a plate); 'aligned' makes them
    coincide. offset moves along the target's +Z; rotate spins about it (deg)."""
    if isinstance(target, App.Placement):
        T = target
    else:
        if target_frame is None:
            raise FcError("mate(): target_frame is required when target is an object")
        T = get_frame(target, target_frame)
    T = T.multiply(App.Placement(Vector(0, 0, float(offset)), App.Rotation(Vector(0, 0, 1), float(rotate))))
    if mode == "against":
        T = T.multiply(App.Placement(Vector(), App.Rotation(Vector(1, 0, 0), 180)))
    elif mode != "aligned":
        raise FcError("mate mode must be 'against' or 'aligned'")
    F = get_frame(part, part_frame, local=True)
    parent = part.getGlobalPlacement().multiply(part.Placement.inverse())
    part.Placement = parent.inverse().multiply(T).multiply(F.inverse())
    return part


# -------------------------------------------------------------- assemblies
#
# FreeCAD 1.x Assembly workbench. Joints are defined from NAMED FRAMES (or
# planar faces), never from element names like "Vertex6", which renumber when
# the model changes: each joint references the whole part and carries the
# frame as its offset, so the solver aligns exactly the frames you named.

_JOINT_KINDS = {"fixed": "Fixed", "revolute": "Revolute", "cylindrical": "Cylindrical",
                "slider": "Slider", "ball": "Ball", "distance": "Distance", "parallel": "Parallel",
                "perpendicular": "Perpendicular", "angle": "Angle"}


def _joint_module():
    try:
        import JointObject
    except ImportError as e:
        raise FcError("FreeCAD's Assembly workbench is not available (%s)" % e)
    return JointObject


def assembly(name="Assembly"):
    """An Assembly workbench assembly with its joint group."""
    _joint_module()
    asm = _ctx.doc.addObject("Assembly::AssemblyObject", _safe_name(name))
    asm.Label = name
    asm.newObject("Assembly::JointGroup", "Joints")
    return asm


def _joints(asm):
    for o in asm.Group:
        if o.TypeId == "Assembly::JointGroup":
            return o
    return asm.newObject("Assembly::JointGroup", "Joints")


def add(asm, *objs):
    """Move parts (bodies, reference parts) into the assembly. A PartDesign
    feature (Pad, Pocket …) stands for its Body."""
    parts = [_top_part(o, "add") for o in objs]
    for o in parts:
        if o not in asm.Group:
            asm.addObject(o)
    return parts[0] if len(parts) == 1 else tuple(parts)


def ground(asm, obj):
    """Fix one part in place; every other part is positioned by joints."""
    jm = _joint_module()
    obj = _top_part(obj, "ground")
    g = _joints(asm).newObject("App::FeaturePython", "Ground_" + obj.Name)
    jm.GroundedJoint(g, obj)
    g.Label = "Ground %s" % obj.Label
    return g


def _part_of(obj):
    """The assembly-level part owning obj (a Body for a PartDesign feature)."""
    try:
        par = obj.getParentGeoFeatureGroup()
    except Exception:  # noqa: BLE001
        par = None
    return par if par is not None and par.TypeId == "PartDesign::Body" else obj


_PART_TYPES = ("PartDesign::Body", "App::Part", "App::Link")


def _top_part(obj, fn):
    """obj as an assembly-level part: anything inside a Body (Pad, Pocket,
    sketch …) → that Body; free sketches, spreadsheets and objects nested in
    another container are refused instead of being pulled out of it."""
    if not hasattr(obj, "TypeId"):
        raise FcError("%s(): expected a part (fc.body / fc.ref_part), got %r" % (fn, obj))
    part = _part_of(obj)
    if part is not obj:
        return part
    t = obj.TypeId
    if not (t in _PART_TYPES or (t.startswith("Part::") and t != "Part::Part2DObject")):
        raise FcError("%s(): %s (%s) is not a part — pass a Body (fc.body), a reference part (fc.ref_part) or "
                      "another top-level solid" % (fn, obj.Label, t))
    try:
        par = obj.getParentGeoFeatureGroup()
    except Exception:  # noqa: BLE001
        par = None
    if par is not None and not par.TypeId.startswith("Assembly::"):
        raise FcError("%s(): %s is inside %s — pass %s instead" % (fn, obj.Label, par.Label, par.Label))
    return obj


def _local_frame(ref):
    """(part, frame placement local to the part) from (obj, 'frame') or a FaceRef."""
    if isinstance(ref, (tuple, list)) and len(ref) == 2 and isinstance(ref[1], str):
        part = _top_part(ref[0], "joint")
        if part is ref[0]:
            return part, get_frame(part, ref[1], local=True)
        # frame stored on a feature inside the Body: re-express it in the Body
        return part, part.getGlobalPlacement().inverse().multiply(get_frame(ref[0], ref[1]))
    if isinstance(ref, FaceRef):
        part = _top_part(ref.obj, "joint")
        g = frame_on(ref)
        return part, part.getGlobalPlacement().inverse().multiply(g)
    raise FcError("joint ends must be (part, 'frame_name') or fc.face(...), got %r" % (ref,))


def joint(asm, kind, a, b, mode="aligned", offset=0.0, angle=0.0, name=None):
    """Join two parts by frames. kind: fixed | revolute (about the frames' Z) |
    cylindrical | slider (along Z) | ball | distance | parallel | perpendicular |
    angle. mode 'against' puts the frames face to face (Z axes opposed, e.g. a
    motor face on a plate); 'aligned' makes them coincide. offset shifts b
    along a's Z; angle turns b about it (degrees).

    Unless b is grounded, joint() PLACES part b where the joint puts it (any
    placement given to b before is replaced): FreeCAD's solver accepts
    parallel or anti-parallel axes and keeps the solution nearest the current
    position, so starting b in place is what makes the result exact. fc.solve
    then verifies every joint; the build report says "holds ✓ (b placed by
    the joint and verified)" — the solver did not have to move it."""
    jm = _joint_module()
    k = _JOINT_KINDS.get(str(kind).lower())
    if not k:
        raise FcError("joint kind must be one of %s" % ", ".join(_JOINT_KINDS))
    pa, fa = _local_frame(a)
    pb, fb = _local_frame(b)
    if mode == "against":
        fb = fb.multiply(App.Placement(Vector(), App.Rotation(Vector(1, 0, 0), 180)))
    elif mode != "aligned":
        raise FcError("joint mode must be 'aligned' or 'against'")
    fa = fa.multiply(App.Placement(Vector(0, 0, float(offset)), App.Rotation(Vector(0, 0, 1), float(angle))))
    j = _joints(asm).newObject("App::FeaturePython", _safe_name(name or k))
    jm.Joint(j, jm.JointTypes.index(k))
    j.Proxy.setJointConnectors(j, [[pa, [pa.Name + ".", pa.Name + "."]], [pb, [pb.Name + ".", pb.Name + "."]]])
    j.Offset1 = fa
    j.Offset2 = fb
    # The solver accepts parallel OR anti-parallel axes and returns the
    # solution nearest the current position, so start part b exactly where the
    # joint puts it; solve() then verifies nothing flipped.
    placed = not _is_grounded(asm, pb)
    if placed:
        target = pa.getGlobalPlacement().multiply(fa).multiply(fb.inverse())
        parent = pb.getGlobalPlacement().multiply(pb.Placement.inverse())
        pb.Placement = parent.inverse().multiply(target)
    if name:
        j.Label = name
    j.addProperty("App::PropertyString", "FcJoint", "fclib", "joint as written")
    j.FcJoint = json.dumps({"kind": kind, "mode": mode, "a": pa.Name, "b": pb.Name, "placed": placed})
    return j


def _is_grounded(asm, obj):
    return any(getattr(g, "ObjectToGround", None) == obj for g in _joints(asm).Group)


def solve(asm):
    """Solve the assembly, then check each joint actually holds: its two frames
    must coincide (fixed/revolute/ball), share an axis (cylindrical) or a
    direction (slider). Problems become build warnings."""
    r = asm.solve()
    _ctx.doc.recompute()
    if r != 0:
        warn("assembly %s did not solve (code %s) — over-constrained or conflicting joints" % (asm.Label, r))
    for j in _joints(asm).Group:
        err = joint_error(j)
        if err and (err[0] > 1e-4 or err[1] > 1e-3):
            warn("joint %s does not hold after solving: %.4f mm, %.4f°" % (j.Label, err[0], err[1]))
    return r


def joint_error(j):
    """(position error mm, angle error deg) for a joint, measured between its
    two frames in global coordinates; None for grounded/unsupported joints."""
    if not hasattr(j, "Offset1") or not getattr(j, "Reference1", None):
        return None
    kind = getattr(j, "JointType", "")
    a, b = j.Reference1[0], j.Reference2[0]
    ga = a.getGlobalPlacement().multiply(j.Offset1)
    gb = b.getGlobalPlacement().multiply(j.Offset2)
    za, zb = ga.Rotation.multVec(Vector(0, 0, 1)), gb.Rotation.multVec(Vector(0, 0, 1))
    ang = math.degrees(math.acos(max(-1.0, min(1.0, za.dot(zb)))))
    d = gb.Base - ga.Base
    if kind in ("Fixed", "Revolute", "Ball"):
        pos = d.Length
    elif kind in ("Cylindrical", "Slider"):
        pos = (d - za * d.dot(za)).Length
    else:
        return None
    if kind == "Ball":
        ang = 0.0
    if kind == "Fixed":
        xa, xb = ga.Rotation.multVec(Vector(1, 0, 0)), gb.Rotation.multVec(Vector(1, 0, 0))
        ang = max(ang, math.degrees(math.acos(max(-1.0, min(1.0, xa.dot(xb))))))
    return pos, ang


std = standards              # fc.std.clearance("M3"), fc.std.fit(8, "H7/g6") …


# ---------------------------------------------------------------- patterns

def bolt_circle(pcd, n, start=0.0, center=(0, 0)):
    """n points equally spaced on a circle of diameter pcd, first at `start` deg."""
    r = float(pcd) / 2
    return [(center[0] + r * math.cos(math.radians(float(start) + 360.0 * k / n)),
             center[1] + r * math.sin(math.radians(float(start) + 360.0 * k / n))) for k in range(int(n))]


def grid(nx, ny, px, py, center=(0, 0)):
    """nx x ny points at pitch px, py, centred on `center`."""
    return [(center[0] + (i - (nx - 1) / 2.0) * float(px), center[1] + (j - (ny - 1) / 2.0) * float(py))
            for j in range(int(ny)) for i in range(int(nx))]


def rect_pattern(w, h, center=(0, 0)):
    """The four corners of a w x h rectangle (e.g. a square bolt pattern)."""
    return grid(2, 2, w, h, center)


# ------------------------------------------------------------- sheet metal
#
# A bent part is built from its spec, so the folded solid and the flat pattern come
# from the same numbers (no general unfolding): every face is a flat plate, every
# bend a sector of a tube; the flat pattern lays the same plates out with a strip
# of bend allowance BA = π/180·angle·(r + k·t) for each bend.

SHEET_K = 0.33                 # K-factor for air bending (rule of thumb) when none is given
_SHEET_FLANGE_KEYS = ("edge", "length", "angle", "up", "radius", "inset", "relief")
_SHEET_HOLE_KEYS = ("on", "at", "spec")


def sheet_metal(name, thickness, base, flanges=(), holes=(), radius=None, k_factor=None, placement=None):
    """A bent sheet-metal part of one thickness, built from a spec so the folded
    solid and its flat pattern come from the same numbers. Returns a
    Part::Feature labelled `name` (process "sheet" in project.json) whose FcSheet
    (JSON) records the spec, every bend and the flat size; fc.sheet_flat(part)
    gives the flat pattern, and export/release write it as the DXF.
      base      the base face in the XY plane: fc.rect(w, h, center), fc.polygon([...])
                or a list of (x, y) points — its OUTSIDE footprint: a flange's outer
                face is flush with the edge it hangs on. The sheet lies on z = 0 … t;
                its top side (+Z) is "up".
      flanges   [{"edge": 1, "length": 25, "angle": 90, "up": True}, …] — flange N is
                "flangeN" (1, 2 … in this order). Keys:
                edge    a base edge: index i runs from point i to point i + 1 (fc.rect:
                        0 = −Y, 1 = +X, 2 = +Y, 3 = −X), or the way it faces ("+X"); or
                        "flangeN" — the far edge of an earlier flange (lips, Z-bends)
                length  outside length: from the outside of its bend (the parent's bottom
                        face for an up bend, its top face for a down bend) to the free edge
                        or to the outside of the next bend — a 90° flange on the base is
                        as tall as the part
                angle   bend angle in degrees (default 90; 0 < angle < 180)
                up      True (default) folds toward the top side, False away from it
                radius  inside bend radius (default `radius`)
                inset   [from the edge's first point, from its last] (mm): a flange
                        narrower than its edge, e.g. a tab (a flange's far edge runs
                        the same way as the edge that flange hangs on)
                relief  where a bend ends inside an edge, cut a slot this wide (mm;
                        True = thickness) beside it, past the bend, so it does not tear
      holes     [{"on": "base" | "flange1" …, "at": [(a, b), …], "spec": "M4 clearance
                normal" | "Ø5"}] — clearance or plain holes through the sheet; points in
                that face's 2D coordinates like every fclib 2D point: the dominant global
                axes of its plane in the part's own coordinates ((x, y) on the base, (y, z)
                on a flange facing ±X, (x, z) facing ±Y), or 3D points. A hole must lie on
                its face's flat part, clear of the bends.
      radius    default inside bend radius (default = thickness)
      k_factor  where the neutral axis lies in the bend (default 0.33, air bending)
      placement where the finished part goes (fc.frame(...)); the coordinates above
                are the part's own, before it.
    Raises FcError when a hole is off its face, a flange is too short for its bends,
    the flanges collide when folded or the flat pattern overlaps itself."""
    spec = _sheet_spec(name, thickness, base, flanges, holes, radius, k_factor)
    g = _sheet_geometry(spec)
    o = _ctx.doc.addObject("Part::Feature", _safe_name(name))
    o.Label = str(name)
    o.Shape = g["solid"]
    _role(o, "part")
    o.addProperty("App::PropertyString", "FcSheet", "fclib",
                  "bent sheet metal: thickness, radius, k_factor, spec, bends, flat_size (JSON)")
    o.FcSheet = json.dumps(_sheet_record(spec, g))
    if placement is not None:
        if not isinstance(placement, App.Placement):
            raise FcError("sheet_metal %s: placement must be an App.Placement (e.g. fc.frame(...)), got %r"
                          % (name, placement))
        o.Placement = placement
    return o


def _sheet_record(spec, g):
    """FcSheet for a spec and its geometry: thickness, default radius, K-factor,
    the spec, the bends (flat-pattern coordinates, mm) and the flat size."""
    r6 = lambda x: round(float(x), 6) + 0.0  # noqa: E731
    return {"thickness": spec["thickness"], "radius": spec["radius"], "k_factor": spec["k_factor"], "spec": spec,
            "bends": [{"n": b["n"], "angle": b["angle"], "radius": b["radius"], "up": b["up"],
                       "length": r6(b["a1"] - b["a0"]), "line": [[r6(c) for c in p] for p in b["line"]]}
                      for b in g["bends"]],
            "flat_size": [r6(v) for v in g["flat_size"]]}


def sheet_flat(obj):
    """The flat pattern of a part made by fc.sheet_metal, rebuilt from its FcSheet:
    (a Part.Face at z = 0 — the blank with its holes, seen from the sheet's top side,
    lower-left corner at the origin — and the bend lines as Part.Edges in the same
    coordinates, in order B1, B2 …). Up bends fold toward the viewer. obj may also be
    the FcSheet data (dict or JSON text)."""
    data = sheet_data(obj)
    if data is None:
        raise FcError("%s was not made by fc.sheet_metal (it has no FcSheet)" % getattr(obj, "Label", obj))
    g = _sheet_geometry(data["spec"], solid=False)
    edges = [Part.LineSegment(Vector(a[0], a[1], 0), Vector(b[0], b[1], 0)).toShape() for a, b in
             (bd["line"] for bd in g["bends"])]
    return g["flat"], edges


def sheet_data(obj):
    """FcSheet of obj as a dict (None for a part not made by fc.sheet_metal)."""
    raw = obj if isinstance(obj, (dict, str)) else getattr(obj, "FcSheet", "")
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        return None
    return d if isinstance(d, dict) and "spec" in d else None


def _num_arg(v, what, positive=True):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise FcError("%s must be a number, got %r" % (what, v))
    if not math.isfinite(x) or (positive and x <= 0):
        raise FcError("%s must be a number > 0, got %r" % (what, v))
    return x


def _sheet_outline(base, name):
    if isinstance(base, rect):
        cx, cy = _coords(base.center, "sheet_metal base centre")[:2]
        w, h = float(base.w), float(base.h)
        pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
    elif isinstance(base, polygon):
        pts = base.pts
    elif isinstance(base, _Profile):
        raise FcError("sheet_metal %s: base must be fc.rect(...), fc.polygon([...]) or a list of (x, y) points, "
                      "not %s (round corners are not supported on a bent part's outline)" % (name, type(base).__name__))
    else:
        pts = base
    try:
        pts = [tuple(float(c) for c in p) for p in pts]
    except (TypeError, ValueError):
        raise FcError("sheet_metal %s: base must be fc.rect(...), fc.polygon([...]) or a list of (x, y) points, got %r"
                      % (name, base))
    if len(pts) < 3 or any(len(p) != 2 for p in pts):
        raise FcError("sheet_metal %s: the base outline needs at least 3 (x, y) points in the XY plane" % name)
    if any(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) < 1e-6 for i in range(len(pts))):
        raise FcError("sheet_metal %s: the base outline repeats a point — list each corner once (it closes itself)"
                      % name)
    if abs(_signed_area(pts)) < 1e-6:
        raise FcError("sheet_metal %s: the base outline has no area" % name)
    return [[p[0], p[1]] for p in pts]


def _sheet_spec(name, thickness, base, flanges, holes, radius, k_factor):
    """The builder's inputs as plain JSON data (FcSheet's "spec"), defaults filled in."""
    if not str(name or "").strip():
        raise FcError("sheet_metal needs a name (the part's label)")
    t = _num_arg(thickness, "sheet_metal %s: thickness" % name)
    r_def = t if radius is None else _num_arg(radius, "sheet_metal %s: radius (inside bend radius)" % name)
    k = SHEET_K if k_factor is None else _num_arg(k_factor, "sheet_metal %s: k_factor" % name, positive=False)
    if not 0 <= k <= 0.5:
        raise FcError("sheet_metal %s: k_factor must be between 0 and 0.5 (air bending ≈ 0.33), got %g" % (name, k))
    out = {"name": str(name), "thickness": t, "radius": r_def, "k_factor": k, "base": _sheet_outline(base, name),
           "flanges": [], "holes": []}
    if isinstance(flanges, dict):
        flanges = [flanges]
    for i, fl in enumerate(flanges or [], 1):
        who = "sheet_metal %s flange%d" % (name, i)
        if not isinstance(fl, dict):
            raise FcError("%s must be a dict like {\"edge\": 1, \"length\": 25}, got %r" % (who, fl))
        bad = sorted(set(fl) - set(_SHEET_FLANGE_KEYS))
        if bad:
            raise FcError("%s: unknown key(s) %s — use %s" % (who, ", ".join(bad), ", ".join(_SHEET_FLANGE_KEYS)))
        if "edge" not in fl or "length" not in fl:
            raise FcError("%s needs edge and length (outside length, mm)" % who)
        edge = fl["edge"]
        if isinstance(edge, bool) or not isinstance(edge, (int, float, str)):
            raise FcError("%s: edge must be a base edge index, '+X' / '-X' / '+Y' / '-Y' or 'flangeN', got %r"
                          % (who, edge))
        if isinstance(edge, float):
            if edge != int(edge):
                raise FcError("%s: edge index must be a whole number, got %r" % (who, edge))
            edge = int(edge)
        ang = _num_arg(fl.get("angle", 90), who + ": angle")
        if ang >= 180:
            raise FcError("%s: angle must be below 180° (hems are not supported), got %g" % (who, ang))
        up = fl.get("up", True)
        if not isinstance(up, bool):
            raise FcError("%s: up must be True or False, got %r" % (who, up))
        ins = fl.get("inset", 0)
        ins = [ins, ins] if not isinstance(ins, (list, tuple)) else list(ins)
        if len(ins) != 2:
            raise FcError("%s: inset is one number or [from the edge's first point, from its last], got %r"
                          % (who, fl.get("inset")))
        ins = [_num_arg(x, who + ": inset", positive=False) for x in ins]
        if min(ins) < 0:
            raise FcError("%s: inset must be ≥ 0, got %r" % (who, fl.get("inset")))
        rel = fl.get("relief")
        if rel is True:
            rel = t
        elif rel in (None, False, 0):
            rel = None
        else:
            rel = _num_arg(rel, who + ": relief (slot width, mm)")
        out["flanges"].append({"edge": edge, "length": _num_arg(fl["length"], who + ": length"), "angle": ang,
                               "up": up, "radius": _num_arg(fl.get("radius", r_def), who + ": radius"),
                               "inset": ins, "relief": rel})
    if isinstance(holes, dict):
        holes = [holes]
    for j, h in enumerate(holes or [], 1):
        who = "sheet_metal %s hole group %d" % (name, j)
        if not isinstance(h, dict):
            raise FcError("%s must be a dict like {\"on\": \"base\", \"at\": [(x, y)], \"spec\": \"Ø5\"}, got %r"
                          % (who, h))
        bad = sorted(set(h) - set(_SHEET_HOLE_KEYS))
        if bad:
            raise FcError("%s: unknown key(s) %s — use on, at, spec" % (who, ", ".join(bad)))
        if not h.get("spec") or not h.get("at"):
            raise FcError("%s needs at (a list of points) and spec ('M4 clearance normal' or 'Ø5')" % who)
        sp = _parse_spec(h["spec"])
        if sp["kind"] not in ("plain", "clearance") or sp["cut"] or sp.get("fit_class"):
            raise FcError("%s: %r — sheet-metal holes are cut through the sheet: use a clearance or plain spec "
                          "('M4 clearance normal', 'Ø5'); tap or countersink them as a secondary operation noted "
                          "on the drawing" % (who, h["spec"]))
        d = sp["diameter"] if "diameter" in sp else standards.clearance(sp["size"], sp["fit"])
        at = h["at"]
        if isinstance(at, (tuple, list)) and at and not isinstance(at[0], (tuple, list, Vector)):
            at = [at]                                  # one point given bare
        out["holes"].append({"on": str(h.get("on", "base")), "at": [_coords(p, who + " point") for p in at],
                             "spec": str(h["spec"]), "d": float(d)})
    return out


def _signed_area(pts):
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


class _SFace(object):
    """One flat face of a bent part: its outline (outer mold line) as a CCW polygon in
    its own 2D coordinates (u, v); its bottom-surface frame in 3D (material from 0 to t
    along T, the sheet's top side); and the rigid map of (u, v) into the flat pattern."""

    def __init__(self, name, poly, O, U, V, T, R, d):
        self.name, self.poly = name, poly
        self.O, self.U, self.V, self.T = O, U, V, T
        self.R, self.d = R, d               # flat = R·(u, v) + d; R a row-major 2×2 rotation
        self.cuts = []                      # local polygons removed: bend setbacks, reliefs
        self.holes = []                     # (u, v, diameter, spec, given point)
        self.region = None                  # local face at z = 0: the flat part with its holes
        self.swap = False                   # its far edge runs against the user's outline order

    def edge(self, i):
        n = len(self.poly)
        p0, p1 = self.poly[i % n], self.poly[(i + 1) % n]
        L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        e = ((p1[0] - p0[0]) / L, (p1[1] - p0[1]) / L)
        return p0, e, (e[1], -e[0]), L            # start, direction, outward normal, length

    def convex(self, i):
        """Is vertex i (where edge i starts) a convex corner?"""
        n = len(self.poly)
        a, b, c = self.poly[(i - 1) % n], self.poly[i % n], self.poly[(i + 1) % n]
        return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) > -1e-9

    def to3d(self, u, v, w=0.0):
        return self.O + self.U * u + self.V * v + self.T * w

    def flat(self, u, v):
        R, d = self.R, self.d
        return (R[0] * u + R[1] * v + d[0], R[2] * u + R[3] * v + d[1])

    def matrix3d(self):
        U, V, T, O = self.U, self.V, self.T, self.O
        return App.Matrix(U.x, V.x, T.x, O.x, U.y, V.y, T.y, O.y, U.z, V.z, T.z, O.z, 0, 0, 0, 1)

    def matrix_flat(self):
        R, d = self.R, self.d
        return App.Matrix(R[0], R[1], 0, d[0], R[2], R[3], 0, d[1], 0, 0, 1, 0, 0, 0, 0, 1)

    def describe(self):
        """'(y, z) on a plane facing −X' — how points on this face are given."""
        ax = _plane_axes(self.T)
        k = 3 - ax[0] - ax[1]
        return "(%s, %s) on a plane facing %s%s" % ("xyz"[ax[0]], "xyz"[ax[1]],
                                                    "+" if [self.T.x, self.T.y, self.T.z][k] > 0 else "−", "XYZ"[k])


def _poly_face(pts):
    if _signed_area(pts) < 0:                     # every 2D face faces +Z (mixed normals upset the booleans)
        pts = pts[::-1]
    vs = [Vector(p[0], p[1], 0) for p in pts]
    return Part.Face(Part.makePolygon(vs + [vs[0]]))


def _mat2(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3])


def _sheet_edge(ref, base, faces, flip, who):
    """(parent face, edge index on its CCW outline, insets reversed?) for a flange's edge."""
    n = len(base.poly)
    if isinstance(ref, int):
        if not 0 <= ref < n:
            raise FcError("%s: base edge %d does not exist — the base has edges 0 … %d (%s)"
                          % (who, ref, n - 1, _base_edges(base, flip)))
        return (base, (n - 1 - ref) % n, True) if flip else (base, ref, False)
    s = ref.strip()
    if s.lower().startswith("flange"):
        head, _, tail = s.partition(".")
        head = head.lower()
        if head not in faces:
            raise FcError("%s: edge %r — there is no %s before it (a flange can carry a later one on its far edge)"
                          % (who, ref, head))
        if tail.lower() not in ("", "end", "edge2"):
            raise FcError("%s: edge %r — only a flange's far edge carries another flange: '%s' (or '%s.end')"
                          % (who, ref, head, head))
        return faces[head], 2, faces[head].swap
    key = s.upper()
    key = "+" + key if key in ("X", "Y") else key
    dirs = {"+X": (1, 0), "-X": (-1, 0), "+Y": (0, 1), "-Y": (0, -1)}
    if key not in dirs:
        raise FcError("%s: edge %r — give a base edge index (%s), '+X' / '-X' / '+Y' / '-Y', or 'flangeN'"
                      % (who, ref, _base_edges(base, flip)))
    hits = [j for j in range(n) if base.edge(j)[2][0] * dirs[key][0] + base.edge(j)[2][1] * dirs[key][1] > 1 - 1e-6]
    if len(hits) != 1:
        raise FcError("%s: %s base edge%s face%s %s — give the edge index (%s)" % (
            who, len(hits) or "no", "s" if len(hits) != 1 else "", "" if len(hits) > 1 else "s", key,
            _base_edges(base, flip)))
    return base, hits[0], flip


def _base_edges(base, flip):
    """'0 −Y 60 mm, 1 +X 40 mm …' in the user's numbering."""
    n = len(base.poly)
    out = []
    for i in range(n):
        p0, e, nrm, L = base.edge((n - 1 - i) % n if flip else i)
        face = next((k for k, d in (("+X", (1, 0)), ("−X", (-1, 0)), ("+Y", (0, 1)), ("−Y", (0, -1)))
                     if nrm[0] * d[0] + nrm[1] * d[1] > 1 - 1e-6), None)
        out.append("%d%s %s mm" % (i, " " + face if face else "", _g(L)))
    return ", ".join(out)


def _g(x):
    s = ("%.3f" % x).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _face_uv(f, p):
    """A point given for face f (its dominant global axes, or 3D) → its own (u, v)."""
    if len(p) == 3:
        P = Vector(*p)
    else:
        ax = _plane_axes(f.T)
        k = 3 - ax[0] - ax[1]
        comps = [0.0, 0.0, 0.0]
        comps[ax[0]], comps[ax[1]] = p
        T, O = [f.T.x, f.T.y, f.T.z], [f.O.x, f.O.y, f.O.z]
        comps[k] = O[k] - sum((comps[m] - O[m]) * T[m] for m in range(3) if m != k) / T[k]
        P = Vector(*comps)
    q = P - f.O
    return q.dot(f.U), q.dot(f.V)


def _face_range(f, reg):
    """'y −20…20, z 4…25' — where points on the flat part of f lie, in f's 2D point axes."""
    ax = _plane_axes(f.T)
    bb = reg.BoundBox
    pts = [f.to3d(u, v) for u in (bb.XMin, bb.XMax) for v in (bb.YMin, bb.YMax)]
    return ", ".join("%s %s…%s" % ("xyz"[a], _g(min(p[a] for p in pts)), _g(max(p[a] for p in pts))) for a in ax)


def _sheet_geometry(spec, solid=True):
    """Faces, bends, flat pattern (and with solid=True the folded solid) of a sheet spec.
    Bends: n, face, parent, angle, radius, up, ossb (outside setback (r + t)·tan(θ/2)),
    ba (bend allowance), a0/a1 (span along the parent edge), inset, relief, length
    (outside), line (bend centre line in flat coordinates)."""
    t, k = float(spec["thickness"]), float(spec["k_factor"])
    who = "sheet_metal %s" % spec.get("name", "")
    pts = [tuple(p) for p in spec["base"]]
    flip = _signed_area(pts) < 0                       # work on a CCW outline; user edge i keeps its meaning
    if flip:
        pts = [pts[0]] + pts[:0:-1]
    base = _SFace("base", pts, Vector(0, 0, 0), Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1),
                  (1.0, 0.0, 0.0, 1.0), (0.0, 0.0))
    faces, order, bends = {"base": base}, [base], []
    margin = max(1.0, 2 * t)
    far = {}                                            # flange name → largest setback of a flange on its far edge
    for i, fl in enumerate(spec.get("flanges") or [], 1):
        name = "flange%d" % i
        parent, idx, swap = _sheet_edge(fl["edge"], base, faces, flip, "%s %s" % (who, name))
        p0, e, nrm, elen = parent.edge(idx)
        i0, i1 = fl["inset"][::-1] if swap else fl["inset"]
        a0, a1 = float(i0), elen - float(i1)
        if a1 - a0 < 1e-3:
            raise FcError("%s %s: inset %s leaves nothing of its %s mm edge" % (who, name, fl["inset"], _g(elen)))
        th, r, L, up = float(fl["angle"]), float(fl["radius"]), float(fl["length"]), bool(fl["up"])
        ossb = (r + t) * math.tan(math.radians(th) / 2)
        ba = math.pi / 180.0 * th * (r + k * t)
        P = lambda a, w: (p0[0] + a * e[0] + w * nrm[0], p0[1] + a * e[1] + w * nrm[1])  # noqa: E731
        # the parent loses the strip under the bend, from its outline in to the tangent line
        x0 = margin if a0 < 1e-6 and parent.convex(idx) else 0.0
        x1 = margin if elen - a1 < 1e-6 and parent.convex(idx + 1) else 0.0
        parent.cuts.append([P(a0 - x0, -ossb), P(a1 + x1, -ossb), P(a1 + x1, margin), P(a0 - x0, margin)])
        rel = fl.get("relief")
        if rel:
            if a0 > 1e-6:
                parent.cuts.append([P(max(0.0, a0 - rel), -(ossb + rel / 2)), P(a0, -(ossb + rel / 2)), P(a0, margin),
                                    P(max(0.0, a0 - rel), margin)])
            if elen - a1 > 1e-6:
                parent.cuts.append([P(a1, -(ossb + rel / 2)), P(min(elen, a1 + rel), -(ossb + rel / 2)),
                                    P(min(elen, a1 + rel), margin), P(a1, margin)])
        if parent is not base:
            far[parent.name] = max(far.get(parent.name, 0.0), ossb)
        # the flange's frame: virtual sharp on the outside of the bend, v along the flange
        E = parent.U * e[0] + parent.V * e[1]
        W = parent.U * nrm[0] + parent.V * nrm[1]
        sT = parent.T * (1.0 if up else -1.0)
        VS0 = parent.to3d(p0[0], p0[1], 0.0 if up else t)
        c, sn = math.cos(math.radians(th)), math.sin(math.radians(th))
        D = W * c + sT * sn
        M = sT * c - W * sn                             # into the flange from its outer face
        TF = M if up else M * -1.0
        UF = D.cross(TF)
        UF.normalize()
        sg = 1.0 if UF.dot(E) > 0 else -1.0
        ulo, uhi = sorted((sg * a0, sg * a1))
        delta = ba - 2 * ossb
        Rl = (sg * e[0], nrm[0], sg * e[1], nrm[1])      # (u, v) → parent: p0 + sg·u·e + (v + delta)·n
        dl = (p0[0] + delta * nrm[0], p0[1] + delta * nrm[1])
        R = _mat2(parent.R, Rl)
        d = (parent.R[0] * dl[0] + parent.R[1] * dl[1] + parent.d[0],
             parent.R[2] * dl[0] + parent.R[3] * dl[1] + parent.d[1])
        child = _SFace(name, [(ulo, 0.0), (uhi, 0.0), (uhi, L), (ulo, L)], VS0 if up else VS0 + M * t, UF, D, TF,
                       R, d)
        child.cuts.append([(ulo - margin, -margin), (uhi + margin, -margin), (uhi + margin, ossb),
                           (ulo - margin, ossb)])
        child.swap = swap
        faces[name] = child
        order.append(child)
        mid = ba / 2 - ossb
        bends.append({"n": i, "face": name, "parent": parent.name, "angle": th, "radius": r, "up": up,
                      "ossb": ossb, "ba": ba, "a0": a0, "a1": a1, "edge_len": elen, "length": L,
                      "inset": (a0, elen - a1), "relief": rel,
                      "zone": [parent.flat(*q) for q in (P(a0, -ossb), P(a1, -ossb), P(a1, ba - ossb),
                                                         P(a0, ba - ossb))],
                      "line": [parent.flat(*P(a0, mid)), parent.flat(*P(a1, mid))],
                      "_3d": (VS0, E, W, sT)})
    for b in bends:                                    # flat length left between a flange's own bends
        b["straight"] = b["length"] - b["ossb"] - far.get(b["face"], 0.0)
        if b["straight"] < 1e-3:
            raise FcError("%s %s: length %s leaves no flat part between its bends (their setbacks (r + t)·tan(θ/2) "
                          "take %s) — make it longer than that" % (who, b["face"], _g(b["length"]),
                                                                  _g(b["length"] - b["straight"])))
    for h in spec.get("holes") or []:
        f = faces.get(h["on"].strip().lower())
        if f is None:
            raise FcError("%s: holes on %r — no such face; the faces are %s" % (who, h["on"], ", ".join(faces)))
        for p in h["at"]:
            u, v = _face_uv(f, p)
            f.holes.append((u, v, float(h["d"]), h.get("spec"), p))
    for f in order:
        reg = _poly_face(f.poly)
        for cut in f.cuts:
            reg = reg.cut(_poly_face(cut))
        parts = sorted(reg.Faces, key=lambda x: -x.Area)
        if not parts:
            raise FcError("%s: the bends' setbacks leave nothing flat of %s — make it larger or the flanges "
                          "narrower" % (who, _face_name(f)))
        whole = _poly_face(f.poly).Area
        big = [x for x in parts[1:] if x.Area > 0.1 * whole]
        if big:
            raise FcError("%s: the bends' setbacks split %s into pieces of %s mm² — the flanges leave no flat "
                          "between them" % (who, _face_name(f),
                                            " and ".join("%.0f" % x.Area for x in [parts[0]] + big)))
        reg = parts[0]                    # corner islands cut free by two reliefs fall out of the blank
        for (u, v, dd, sp, given) in f.holes:
            circ = Part.Face(Part.Wire(Part.makeCircle(dd / 2, Vector(u, v, 0), Vector(0, 0, 1))))
            if circ.cut(reg).Area > 1e-6 * circ.Area:
                raise FcError("%s: hole Ø%s at %s is not on the flat part of %s — points there are %s, "
                              "within %s (clear of the bends, hole edge inside the outline)" % (
                                  who, _g(dd), "(%s)" % ", ".join(_g(c) for c in given), _face_name(f), f.describe(),
                                  _face_range(f, reg)))
            reg = reg.cut(circ).Faces[0]
        f.region = reg
    # flat pattern: the faces' flat parts plus a bend-allowance strip per bend
    pieces = []
    for f in order:
        g = f.region.copy()
        g.transformShape(f.matrix_flat())
        pieces.append((f.name, g))
    for b in bends:
        pieces.append(("%s's bend" % b["face"], _poly_face(b["zone"])))
    flat = pieces[0][1].fuse([p for _, p in pieces[1:]]).removeSplitter() if len(pieces) > 1 else pieces[0][1]
    total = sum(p.Area for _, p in pieces)
    if abs(flat.Area - total) > 1e-6 * max(total, 1.0) or len(flat.Faces) != 1:
        raise FcError("%s: the flat pattern overlaps itself (%s) — it cannot be cut from one sheet. Flanges that meet "
                      "at a corner need inset ≥ the other bend's setback (r + t)·tan(θ/2) = %s mm at that end, and a "
                      "relief; otherwise shorten them" % (
                          who, _overlaps(pieces, lambda a, b: a.common(b).Area, 1e-6) or "%d pieces" % len(flat.Faces),
                          _g(max(b["ossb"] for b in bends))))
    ff = flat.Faces[0]
    bb = ff.BoundBox
    ff.translate(Vector(-bb.XMin, -bb.YMin, 0))         # lower-left corner at the origin, like the DXF
    sh = lambda p: (p[0] - bb.XMin, p[1] - bb.YMin)  # noqa: E731
    for b in bends:
        b["line"] = [sh(p) for p in b["line"]]
        b["zone"] = [sh(p) for p in b["zone"]]
    out = {"faces": order, "bends": bends, "flat": ff, "flat_size": [bb.XLength, bb.YLength],
           "holes": [{"face": f.name, "d": dd, "spec": sp, "at": list(given), "flat": sh(f.flat(u, v))}
                     for f in order for (u, v, dd, sp, given) in f.holes]}
    if solid:
        sol = []
        for f in order:
            g = f.region.copy()
            g.transformShape(f.matrix3d())
            sol.append((f.name, g.extrude(f.T * t)))
        for b in bends:
            sol.append(("%s's bend" % b["face"], _bend_solid(b, t)))
        fused = sol[0][1].fuse([s for _, s in sol[1:]]).removeSplitter() if len(sol) > 1 else sol[0][1]
        vsum = sum(s.Volume for _, s in sol)
        if abs(fused.Volume - vsum) > 1e-6 * vsum + 1e-6:
            raise FcError("%s: the flanges collide when folded (%s) — shorten them, inset them or change their angle"
                          % (who, _overlaps(sol, lambda a, b: a.common(b).Volume, 1e-6) or "overlap"))
        if len(fused.Solids) != 1 or not fused.isValid():
            raise FcError("%s: the folded part is not one valid solid (%d solids) — check the flanges' edges"
                          % (who, len(fused.Solids)))
        out["solid"] = fused.Solids[0]
    return out


def _face_name(f):
    return "the base" if f.name == "base" else f.name


def _overlaps(named, amount, tol):
    """'flange1 and flange2' for the pieces that overlap."""
    hits = []
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            a, b = named[i][1], named[j][1]
            if not a.BoundBox.intersect(b.BoundBox):
                continue
            try:
                if amount(a, b) > tol:
                    hits.append("%s and %s" % (named[i][0], named[j][0]))
            except Exception:  # noqa: BLE001
                continue
    return "; ".join(hits)


def _bend_solid(b, t):
    """The bend as a sector of a tube: inside radius r, outside r + t, through the angle."""
    VS0, E, W, sT = b["_3d"]
    r, th = b["radius"], math.radians(b["angle"])
    C = VS0 + E * b["a0"] - W * ((r + t) * math.tan(th / 2)) + sT * (r + t)

    def pt(rho, phi):
        return C + W * (rho * math.cos(phi)) + sT * (rho * math.sin(phi))
    f0, f1 = -math.pi / 2, th - math.pi / 2
    fm = (f0 + f1) / 2
    w = Part.Wire([Part.Arc(pt(r + t, f0), pt(r + t, fm), pt(r + t, f1)).toShape(),
                   Part.LineSegment(pt(r + t, f1), pt(r, f1)).toShape(),
                   Part.Arc(pt(r, f1), pt(r, fm), pt(r, f0)).toShape(),
                   Part.LineSegment(pt(r, f0), pt(r + t, f0)).toShape()])
    return Part.Face(w).extrude(E * (b["a1"] - b["a0"]))
