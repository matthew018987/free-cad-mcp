# SPDX-License-Identifier: Apache-2.0
"""
Manufacturing outputs inside the FreeCAD worker: verified exports (STEP, 3MF,
STL, OBJ, IGES, BREP, DXF, SVG), manufacturability (DFM) checks per process,
and the release package a supplier receives.

Every export is checked after writing: STEP files are re-imported and their
volume compared, meshes must be watertight with matching volume, and flat
DXF profiles must come from a uniform-thickness plate.
"""

import csv
import io
import json
import math
import os
import time
import zipfile

import FreeCAD as App
import Part

import analysis as an
import standards

Vector = App.Vector
fmt = an.fmt

MESH_FORMATS = ("stl", "3mf", "obj")
ALL_FORMATS = ("step", "iges", "brep", "stl", "3mf", "obj", "dxf", "svg")


class MfgError(Exception):
    pass


# ------------------------------------------------------------------ export

def _safe(label):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label).strip("_") or "part"


def _norm_up(up):
    if not up:
        return None
    u = str(up).strip().upper()
    u = u if u[:1] in "+-" else "+" + u
    if u not in UPS:
        raise MfgError("print_up %r: use one of %s (the model direction that points up on the printer)"
                       % (up, ", ".join(UPS)))
    return u


def export_parts(doc, objs, formats, out_dir, tol=0.02, ang_deg=5.0, view=None, stem_prefix="", print_up=None):
    """Export each object in each format. Returns a list of records
    {part, format, path, bytes, ok, check, oriented}. print_up (+X … -Z):
    mesh files (3mf, stl, obj) are rotated so that model direction points
    up (+Z) and stand on z = 0, ready to print; STEP/BREP/IGES keep the model
    orientation. oriented is print_up for those mesh files, else None."""
    print_up = _norm_up(print_up)
    os.makedirs(out_dir, exist_ok=True)
    recs = []
    for o in objs:
        shape = an.global_shape(o)
        stem = stem_prefix + _safe(o.Label)
        for f in formats:
            f = f.lower().lstrip(".")
            if f == "stp":
                f = "step"
            if f not in ALL_FORMATS:
                raise MfgError("unknown format %r (choose: %s)" % (f, ", ".join(ALL_FORMATS)))
            path = os.path.join(out_dir, "%s.%s" % (stem, f))
            up = print_up if f in MESH_FORMATS else None
            try:
                ok, check = _export_one(doc, o, shape, f, path, tol, ang_deg, view, up)
            except MfgError as e:
                ok, check = False, str(e)
            recs.append({"part": o.Label, "format": f, "path": path, "ok": ok, "check": check,
                         "bytes": os.path.getsize(path) if os.path.isfile(path) else 0, "oriented": up})
    return recs


def _orient_mesh(m, up):
    """Rotate mesh m so that model direction `up` points to +Z, then put its
    lowest point on z = 0."""
    if up == "-Z":
        rot = App.Rotation(Vector(1, 0, 0), 180)
    else:
        rot = App.Rotation(UPS[up], Vector(0, 0, 1))
    if up != "+Z":
        m.transform(App.Placement(Vector(), rot).toMatrix())
    m.translate(0, 0, -m.BoundBox.ZMin)
    return m


def _export_one(doc, obj, shape, f, path, tol, ang_deg, view, up=None):
    if f in ("step", "iges"):
        import Import
        tmp = App.newDocument("Export")
        try:
            o = tmp.addObject("Part::Feature", _safe(obj.Label))
            o.Label = obj.Label
            o.Shape = shape
            Import.export([o], path)
        finally:
            App.closeDocument(tmp.Name)
        back = Part.read(path)
        if shape.Solids:
            dv = abs(back.Volume - shape.Volume) / max(shape.Volume, 1e-9)
            return dv < 1e-4, "re-imported: %d solid(s), volume Δ %.4f %%" % (len(back.Solids), 100 * dv)
        return True, "re-imported: %d face(s)" % len(back.Faces)
    if f == "brep":
        shape.exportBrep(path)
        return True, "BREP written"
    if f in MESH_FORMATS:
        import MeshPart
        m = MeshPart.meshFromShape(Shape=shape, LinearDeflection=float(tol),
                                   AngularDeflection=math.radians(float(ang_deg)), Relative=False)
        if up:
            _orient_mesh(m, up)
        m.write(path)
        solid = m.isSolid() and not m.hasNonManifolds()
        dv = abs(m.Volume - shape.Volume) / max(shape.Volume, 1e-9) if shape.Solids else 0.0
        return solid, "%d triangles, %s, volume Δ %.3f %% (tolerance %g mm)%s" % (
            m.CountFacets, "watertight" if solid else "NOT WATERTIGHT", 100 * dv, tol,
            ", oriented for printing: model %s up, on z = 0" % up if up else "")
    if f in ("dxf", "svg") and sheet_of(obj) is not None:
        return _export_flat_pattern(obj, shape, f, path, view)
    if f in ("dxf", "svg"):
        prof = flat_profile(shape, view)
        if f == "dxf":
            n = write_dxf(path, prof["edges2d"])
        else:
            n = write_svg(path, prof["edges2d"], seen_from=prof["seen_from"])
        note = "%d entities, profile %s × %s mm, thickness %s mm (%s)" % (
            n, fmt(prof["size"][0]), fmt(prof["size"][1]), fmt(prof["thickness"]), prof["axis"])
        if not prof["uniform"]:
            note += " — NOT a uniform plate: pockets/steps will not be cut"
        return prof["uniform"], note
    raise MfgError("unsupported format %s" % f)


# ------------------------------------------------------------ flat profiles

_AXES = {"x": Vector(1, 0, 0), "y": Vector(0, 1, 0), "z": Vector(0, 0, 1)}


def flat_profile(shape, view=None):
    """The 2D outline of a plate seen along its thin axis (or `view` x|y|z):
    edges as 2D primitives, size, thickness and whether the part is a uniform
    extrusion (what a laser/waterjet/router cut actually produces)."""
    bb = shape.BoundBox
    if view:
        ax = str(view).lower().lstrip("+-")[-1]
        if ax not in _AXES:
            raise MfgError("view must be x, y or z")
    else:
        dims = {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength}
        ax = min(dims, key=dims.get)
    n = _AXES[ax]
    lo = {"x": bb.XMin, "y": bb.YMin, "z": bb.ZMin}[ax]
    hi = {"x": bb.XMax, "y": bb.YMax, "z": bb.ZMax}[ax]
    u, v = {"z": (Vector(1, 0, 0), Vector(0, 1, 0)), "y": (Vector(1, 0, 0), Vector(0, 0, 1)),
            "x": (Vector(0, 1, 0), Vector(0, 0, 1))}[ax]
    # uniform extrusion: every face is a cap (normal ∥ axis, at lo or hi) or a wall (normal ⟂ axis)
    uniform = True
    for f in shape.Faces:
        srf = f.Surface.__class__.__name__
        if srf == "Plane":
            u0, u1, v0, v1 = f.ParameterRange
            fn = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
            if abs(abs(fn.dot(n)) - 1) < 1e-6:
                off = f.CenterOfMass.dot(n)
                if abs(off - lo) > 1e-5 and abs(off - hi) > 1e-5:
                    uniform = False
            elif abs(fn.dot(n)) > 1e-6:
                uniform = False
        elif srf == "Cylinder":
            if abs(abs(f.Surface.Axis.dot(n)) - 1) > 1e-6:
                uniform = False
        elif srf not in ("Plane",):
            # B-spline / extrusion walls are fine if they are parallel to the axis
            bbf = f.BoundBox
            span = {"x": bbf.XLength, "y": bbf.YLength, "z": bbf.ZLength}[ax]
            if abs(span - (hi - lo)) > 1e-4:
                uniform = False
    # outline: section through the middle of the plate
    wires = shape.slice(n, (lo + hi) / 2)
    edges2d = []
    origin = n * ((lo + hi) / 2)
    for w in wires:
        for e in w.Edges:
            edges2d.append(_edge2d(e, origin, u, v))
    us = [p[0] for e in edges2d for p in _points(e)]
    vs = [p[1] for e in edges2d for p in _points(e)]
    size = (max(us) - min(us), max(vs) - min(vs)) if us else (0.0, 0.0)
    if us:                                   # lower-left corner at 0,0 for the cutter
        edges2d = [_shift(e, -min(us), -min(vs)) for e in edges2d]
    return {"edges2d": edges2d, "size": size, "thickness": hi - lo, "uniform": uniform,
            "axis": "cut along %s" % ax.upper(), "seen_from": {"z": "+Z", "y": "-Y", "x": "+X"}[ax]}


def _uv(p, origin, u, v):
    w = p - origin
    return (w.dot(u), w.dot(v))


def _edge2d(e, origin, u, v):
    c = e.Curve.__class__.__name__
    if c in ("Line", "LineSegment"):
        return ("line", _uv(e.Vertexes[0].Point, origin, u, v), _uv(e.Vertexes[-1].Point, origin, u, v))
    if c == "Circle":
        cen = _uv(e.Curve.Center, origin, u, v)
        r = e.Curve.Radius
        if e.isClosed():
            return ("circle", cen, r)
        p0 = _uv(e.Vertexes[0].Point, origin, u, v)
        p1 = _uv(e.Vertexes[-1].Point, origin, u, v)
        pm = _uv(e.valueAt((e.FirstParameter + e.LastParameter) / 2), origin, u, v)
        a0 = math.degrees(math.atan2(p0[1] - cen[1], p0[0] - cen[0]))
        a1 = math.degrees(math.atan2(p1[1] - cen[1], p1[0] - cen[0]))
        am = math.degrees(math.atan2(pm[1] - cen[1], pm[0] - cen[0]))
        # DXF arcs run counter-clockwise from start to end angle
        if not _ccw_contains(a0, a1, am):
            a0, a1 = a1, a0
        return ("arc", cen, r, a0 % 360, a1 % 360)
    pts = [_uv(p, origin, u, v) for p in e.discretize(Deflection=0.01)]
    return ("poly", pts)


def _shift(e, dx, dy):
    if e[0] == "line":
        return ("line", (e[1][0] + dx, e[1][1] + dy), (e[2][0] + dx, e[2][1] + dy))
    if e[0] == "circle":
        return ("circle", (e[1][0] + dx, e[1][1] + dy), e[2])
    if e[0] == "arc":
        return ("arc", (e[1][0] + dx, e[1][1] + dy), e[2], e[3], e[4])
    return ("poly", [(x + dx, y + dy) for x, y in e[1]])


def _ccw_contains(a0, a1, am):
    span = (a1 - a0) % 360
    return (am - a0) % 360 <= span


def _points(e):
    """Extreme points of a 2D primitive (for extents): an arc's ends plus the
    quadrant points it actually passes through, not its whole circle."""
    if e[0] == "line":
        return [e[1], e[2]]
    (cx, cy), r = e[1], e[2]
    if e[0] == "circle":
        return [(cx - r, cy - r), (cx + r, cy + r)]
    if e[0] == "arc":
        a0, a1 = e[3], e[4]
        angs = [a0, a1] + [q for q in (0, 90, 180, 270) if _ccw_contains(a0, a1, q)]
        return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in angs]
    return e[1]


def write_dxf(path, edges2d):
    """Minimal ASCII DXF (R12): LINE, CIRCLE, ARC, POLYLINE — readable by every
    laser, waterjet and CAM package. Units are millimetres ($INSUNITS 4 = mm,
    $MEASUREMENT 1 = metric)."""
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
           "9", "$MEASUREMENT", "70", "1", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    n = 0
    for e in edges2d:
        if e[0] == "line":
            (x0, y0), (x1, y1) = e[1], e[2]
            out += ["0", "LINE", "8", "0", "10", _f(x0), "20", _f(y0), "30", "0.0",
                    "11", _f(x1), "21", _f(y1), "31", "0.0"]
        elif e[0] == "circle":
            (cx, cy), r = e[1], e[2]
            out += ["0", "CIRCLE", "8", "0", "10", _f(cx), "20", _f(cy), "30", "0.0", "40", _f(r)]
        elif e[0] == "arc":
            (cx, cy), r, a0, a1 = e[1], e[2], e[3], e[4]
            out += ["0", "ARC", "8", "0", "10", _f(cx), "20", _f(cy), "30", "0.0", "40", _f(r),
                    "50", _f(a0), "51", _f(a1)]
        else:
            out += ["0", "POLYLINE", "8", "0", "66", "1", "10", "0.0", "20", "0.0", "30", "0.0"]
            for x, y in e[1]:
                out += ["0", "VERTEX", "8", "0", "10", _f(x), "20", _f(y), "30", "0.0"]
            out += ["0", "SEQEND", "8", "0"]
        n += 1
    out += ["0", "ENDSEC", "0", "EOF"]
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    return n


def write_svg(path, edges2d, seen_from=None, bends=None):
    """SVG of the same 2D profile as the DXF, 1 unit = 1 mm. SVG's y axis runs
    downward, so y is flipped (svg y = height − DXF y): the picture is the
    profile as seen in the model, not its mirror image — no transform needed.
    bends: [((x0, y0), (x1, y1), label)] — dashed red bend lines with their notes
    (a sheet-metal flat pattern)."""
    pts = [p for e in edges2d for p in _points(e)]
    if not pts:
        raise MfgError("empty profile")
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    w, h = x1 - x0, y1 - y0
    els = []
    Y = lambda y: y1 - y  # noqa: E731 — SVG y runs downward
    for e in edges2d:
        if e[0] == "line":
            els.append('<line x1="%s" y1="%s" x2="%s" y2="%s"/>' % (_f(e[1][0] - x0), _f(Y(e[1][1])),
                                                                   _f(e[2][0] - x0), _f(Y(e[2][1]))))
        elif e[0] == "circle":
            els.append('<circle cx="%s" cy="%s" r="%s"/>' % (_f(e[1][0] - x0), _f(Y(e[1][1])), _f(e[2])))
        elif e[0] == "arc":
            (cx, cy), r, a0, a1 = e[1], e[2], e[3], e[4]
            sx, sy = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
            ex, ey = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
            large = 1 if (a1 - a0) % 360 > 180 else 0
            els.append('<path d="M %s %s A %s %s 0 %d 0 %s %s"/>' % (
                _f(sx - x0), _f(Y(sy)), _f(r), _f(r), large, _f(ex - x0), _f(Y(ey))))
        else:
            els.append('<polyline points="%s"/>' % " ".join("%s,%s" % (_f(x - x0), _f(Y(y))) for x, y in e[1]))
    bend_els = []
    for (bx0, by0), (bx1, by1), label in bends or []:
        bend_els.append('<line x1="%s" y1="%s" x2="%s" y2="%s"/>' % (_f(bx0 - x0), _f(Y(by0)), _f(bx1 - x0),
                                                                    _f(Y(by1))))
        (tx, ty), ang = _bend_label_at(((bx0, by0), (bx1, by1)), 1.5)
        bend_els.append('<text x="%s" y="%s" fill="red" stroke="none" transform="rotate(%s %s %s)">%s</text>' % (
            _f(tx - x0), _f(Y(ty)), _f(-ang), _f(tx - x0), _f(Y(ty)), label.replace("&", "&amp;").replace("<", "&lt;")))
    bend_g = ('\n<g fill="none" stroke="red" stroke-width="0.1" stroke-dasharray="2 1" font-size="2.5" '
              'font-family="sans-serif" text-anchor="middle">\n%s\n</g>' % "\n".join(bend_els)) if bend_els else ""
    with open(path, "w") as fh:
        fh.write('<svg xmlns="http://www.w3.org/2000/svg" width="%smm" height="%smm" viewBox="0 0 %s %s">\n'
                 '<!-- 1:1 in mm%s, same coordinates as the DXF: DXF x = svg x%s, DXF y = %s - svg y '
                 '(SVG y runs downward; the profile is not mirrored)%s -->\n'
                 '<g fill="none" stroke="black" stroke-width="0.1">\n%s\n</g>%s</svg>\n'
                 % (_f(w), _f(h), _f(w), _f(h), ", seen from %s" % seen_from if seen_from else "",
                    " + %s" % _f(x0) if abs(x0) > 1e-9 else "", _f(y1),
                    "; red dashed lines are bend lines, UP = toward you" if bend_els else "", "\n".join(els), bend_g))
    return len(els) + len(bends or [])


def _f(x):
    return ("%.6f" % x).rstrip("0").rstrip(".") if abs(x) > 1e-12 else "0"


def _g(x, nd=2):
    """Short number for text: 3, 3.4, 0.25."""
    s = ("%." + str(nd) + "f") % x
    return (s.rstrip("0").rstrip(".") if "." in s else s) or "0"


# ---------------------------------------------------- sheet metal (fc.sheet_metal)

def sheet_of(obj):
    """FcSheet of a part made by fc.sheet_metal (a dict), else None."""
    if not getattr(obj, "FcSheet", ""):
        return None
    import fclib
    return fclib.sheet_data(obj)


def bend_text(b, deg="°"):
    """'B1 90° UP R2' — a bend's note on the flat pattern."""
    return "B%d %s%s %s R%s" % (b["n"], _g(b["angle"], 2), deg, "UP" if b["up"] else "DOWN", _g(b["radius"], 2))


def _bend_label_at(line, off):
    """Where a bend note goes: beside the middle of its line, reading along it → ((x, y), angle°)."""
    (x0, y0), (x1, y1) = line
    ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
    if ang <= -90 or ang > 90:                         # keep text readable: left to right, or bottom to top
        ang = ang - 180 if ang > 90 else ang + 180
    a = math.radians(ang)
    return ((x0 + x1) / 2 - off * math.sin(a), (y0 + y1) / 2 + off * math.cos(a)), ang


def sheet_flat_pattern(obj, shape=None):
    """The flat pattern of a sheet part, rebuilt from its FcSheet and checked against
    the folded solid. → {edges2d (outline and holes), bends (FcSheet), size, thickness,
    k_factor, holes, ok, problem}. ok is False when the rebuilt flat pattern does not
    match FcSheet or the solid is no longer what the spec builds (changed afterwards)."""
    import fclib
    sheet = sheet_of(obj)
    t, k = float(sheet["thickness"]), float(sheet["k_factor"])
    g = fclib._sheet_geometry(sheet["spec"], solid=False)
    face = g["flat"]
    o, u, v = Vector(0, 0, 0), Vector(1, 0, 0), Vector(0, 1, 0)
    edges2d = [_edge2d(e, o, u, v) for w in face.Wires for e in w.Edges]
    problems = []
    size = g["flat_size"]
    if any(abs(a - b) > 1e-3 for a, b in zip(size, sheet.get("flat_size") or [0, 0])):
        problems.append("the flat pattern rebuilt from FcSheet is %s × %s mm, FcSheet says %s × %s — rebuild the model"
                        % (_g(size[0]), _g(size[1]), _g(sheet["flat_size"][0]), _g(sheet["flat_size"][1])))
    # the folded solid = the flat blank × t, plus (½ − K)·t² per radian and mm of bend
    vexp = face.Area * t + sum(math.radians(b["angle"]) * t * t * (0.5 - k) * (b["a1"] - b["a0"]) for b in g["bends"])
    vol = (shape if shape is not None else an.global_shape(obj)).Volume
    dv = (vol - vexp) / max(vexp, 1e-9)
    if abs(dv) > 1e-4:
        problems.append("the solid (%s mm³) is not what its fc.sheet_metal spec builds (%s mm³, %+.2f %%) — it was "
                        "changed after fc.sheet_metal, so this flat pattern would not make it; make every change in "
                        "the sheet_metal call" % (fmt(vol, 1), fmt(vexp, 1), 100 * dv))
    if not face.isValid():
        problems.append("the flat pattern is not a valid face")
    return {"edges2d": edges2d, "bends": sheet.get("bends") or [], "size": size, "thickness": t, "k_factor": k,
            "radius": sheet.get("radius"), "holes": g["holes"], "geometry": g, "volume_delta": dv,
            "ok": not problems, "problem": "; ".join(problems)}


_DXF_TABLES = ["0", "SECTION", "2", "TABLES",
               "0", "TABLE", "2", "LTYPE", "70", "2",
               "0", "LTYPE", "2", "CONTINUOUS", "70", "0", "3", "Solid line", "72", "65", "73", "0", "40", "0.0",
               "0", "LTYPE", "2", "DASHED", "70", "0", "3", "Dashed __ __ __", "72", "65", "73", "2", "40", "6.0",
               "49", "4.0", "49", "-2.0",
               "0", "ENDTAB",
               "0", "TABLE", "2", "LAYER", "70", "3",
               "0", "LAYER", "2", "0", "70", "0", "62", "7", "6", "CONTINUOUS",
               "0", "LAYER", "2", "CUT", "70", "0", "62", "7", "6", "CONTINUOUS",
               "0", "LAYER", "2", "BEND", "70", "0", "62", "1", "6", "DASHED",
               "0", "ENDTAB",
               "0", "TABLE", "2", "STYLE", "70", "1",
               "0", "STYLE", "2", "STANDARD", "70", "0", "40", "0.0", "41", "1.0", "50", "0.0", "71", "0", "42", "2.5",
               "3", "txt",
               "0", "ENDTAB",
               "0", "ENDSEC"]


def write_sheet_dxf(path, fp):
    """The flat pattern as an R12 ASCII DXF (mm): the cut outline and holes on layer
    CUT; each bend line on layer BEND (red, dashed) with a TEXT note beside it —
    'B1 90° UP R2' (UP = toward the viewer: the DXF shows the sheet's top side)."""
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
           "9", "$MEASUREMENT", "70", "1", "0", "ENDSEC"] + _DXF_TABLES + ["0", "SECTION", "2", "ENTITIES"]
    n = 0
    for e in fp["edges2d"]:
        out += _dxf_entity(e, "CUT")
        n += 1
    hgt = 2.5 if min(fp["size"]) >= 25 else 1.5
    for b in fp["bends"]:
        (x0, y0), (x1, y1) = b["line"]
        out += ["0", "LINE", "8", "BEND", "10", _f(x0), "20", _f(y0), "30", "0.0", "11", _f(x1), "21", _f(y1),
                "31", "0.0"]
        (tx, ty), ang = _bend_label_at(b["line"], 0.6 * hgt)
        out += ["0", "TEXT", "8", "BEND", "10", _f(tx), "20", _f(ty), "30", "0.0", "40", _f(hgt), "1",
                bend_text(b, deg="%%d"), "50", _f(ang), "72", "1", "11", _f(tx), "21", _f(ty), "31", "0.0"]
        n += 2
    out += ["0", "ENDSEC", "0", "EOF"]
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    return n


def _dxf_entity(e, layer):
    if e[0] == "line":
        (x0, y0), (x1, y1) = e[1], e[2]
        return ["0", "LINE", "8", layer, "10", _f(x0), "20", _f(y0), "30", "0.0", "11", _f(x1), "21", _f(y1),
                "31", "0.0"]
    if e[0] == "circle":
        (cx, cy), r = e[1], e[2]
        return ["0", "CIRCLE", "8", layer, "10", _f(cx), "20", _f(cy), "30", "0.0", "40", _f(r)]
    if e[0] == "arc":
        (cx, cy), r, a0, a1 = e[1], e[2], e[3], e[4]
        return ["0", "ARC", "8", layer, "10", _f(cx), "20", _f(cy), "30", "0.0", "40", _f(r), "50", _f(a0),
                "51", _f(a1)]
    out = ["0", "POLYLINE", "8", layer, "66", "1", "10", "0.0", "20", "0.0", "30", "0.0"]
    for x, y in e[1]:
        out += ["0", "VERTEX", "8", layer, "10", _f(x), "20", _f(y), "30", "0.0"]
    return out + ["0", "SEQEND", "8", layer]


def _export_flat_pattern(obj, shape, f, path, view=None):
    """DXF / SVG of a sheet part: its flat pattern (not a section of the folded part)."""
    try:
        fp = sheet_flat_pattern(obj, shape)
    except Exception as e:  # noqa: BLE001 — FcSheet whose spec no longer builds
        raise MfgError("the flat pattern of %s could not be rebuilt from its FcSheet (%s) — build the model again"
                       % (obj.Label, e))
    if f == "dxf":
        n = write_sheet_dxf(path, fp)
    else:
        n = write_svg(path, fp["edges2d"], seen_from="the sheet's top side (flat pattern)",
                      bends=[(b["line"][0], b["line"][1], bend_text(b)) for b in fp["bends"]])
    nb = len(fp["bends"])
    note = "flat pattern %s × %s mm from %s mm sheet, %d bend%s%s, K %s — %d entities: outline + %d hole%s%s" % (
        _g(fp["size"][0]), _g(fp["size"][1]), _g(fp["thickness"]), nb, "" if nb == 1 else "s",
        " (%s)" % ", ".join(bend_text(b) for b in fp["bends"]) if nb else "", _g(fp["k_factor"], 3), n,
        len(fp["holes"]), "" if len(fp["holes"]) == 1 else "s",
        (" on layer CUT, bend lines with notes on layer BEND" if f == "dxf" else ", bend lines dashed red") if nb else
        (" on layer CUT" if f == "dxf" else ""))
    if view:
        note += " (view ignored: a sheet part's %s is its flat pattern)" % f.upper()
    if fp["ok"]:
        note += "; folded volume = flat area × t ✓"
    else:
        note += " — ✗ %s" % fp["problem"]
    return fp["ok"], note


def _dfm_sheet(obj, shape, sheet, process, out):
    """Bent sheet metal: bend radius, flange lengths, holes near bends, reliefs, and
    the flat size and bends for the shop."""
    F, m = out["findings"], out["metrics"]
    try:
        fp = sheet_flat_pattern(obj, shape)
    except Exception as e:  # noqa: BLE001 — a spec that no longer builds
        F.append(_finding("error", "its fc.sheet_metal spec (FcSheet) does not build: %s — build the model again" % e))
        return
    g, t, k = fp["geometry"], fp["thickness"], fp["k_factor"]
    bends = g["bends"]
    m.update(thickness=t, flat_size=fp["size"], bends=len(bends), k_factor=k)
    if process == "laser":
        F.append(_finding("error", "%d bend%s — a laser part is a flat blank; set its process to \"sheet\" (bent sheet "
                                   "metal: flat pattern, bending) in project.json parts" % (
                                       len(bends), "" if len(bends) == 1 else "s")))
        return
    if not fp["ok"]:
        F.append(_finding("error", fp["problem"]))
    F.append(_finding("info", "bent from %s mm sheet: %d bend%s%s, K %s; flat pattern %s × %s mm" % (
        _g(t), len(bends), "" if len(bends) == 1 else "s",
        " (%s)" % ", ".join(bend_text(b) for b in sheet.get("bends") or []) if bends else "", _g(k, 3),
        _g(fp["size"][0]), _g(fp["size"][1]))))
    for b in bends:
        name = b["face"]
        if b["radius"] < t - 1e-9:
            F.append(_finding("warn", "B%d inside radius R%s is below the thickness %s — the outside of the bend may "
                              "crack (most shops bend R ≥ t); use radius ≥ %s"
                              % (b["n"], _g(b["radius"]), _g(t), _g(t))))
        if b["straight"] < 2 * t - 1e-9:
            if b["straight"] < b["length"] - b["ossb"] - 1e-9:           # another bend on its far edge
                F.append(_finding("warn", "%s leaves %s mm flat between its bends — the press-brake die needs ≥ 2t = "
                                  "%s; lengthen it" % (name, _g(b["straight"]), _g(2 * t))))
            else:
                need = 2 * t + b["ossb"]
                F.append(_finding("warn", "%s is %s mm long (outside) — below the %s mm minimum for its %s° bend "
                                  "(3t + r at 90°: the die needs a flat of ≥ 2t beyond the bend); lengthen it"
                                  % (name, _g(b["length"]), _g(need), _g(b["angle"]))))
        if not b["relief"]:
            ends = [w for w, gap in (("first", b["inset"][0]), ("last", b["inset"][1])) if gap > 1e-6]
            if ends:
                F.append(_finding("warn", "%s's bend B%d ends inside its edge (inset at the %s end) with no relief — "
                                  "the sheet tears or bulges there when bent; give it relief=%s (a slot ≥ t wide, "
                                  "past the bend)" % (name, b["n"], " and ".join(ends), _g(t))))
    small, near = {}, {}
    for h in fp["holes"]:
        if h["d"] < t - 1e-9:
            small.setdefault((h["face"], round(h["d"], 3)), []).append(h)
        best = None
        for b in bends:
            dist = _seg_dist(h["flat"], b["line"][0], b["line"][1]) - h["d"] / 2
            need = 2 * t + b["radius"]
            if dist < need - 1e-6 and (best is None or dist - need < best[0] - best[1]):
                best = (dist, need, b["n"])
        if best:
            near.setdefault((h["face"], round(h["d"], 3), best[2], round(best[0], 2), best[1]), []).append(h)
    for (face, d), hs in sorted(small.items()):
        F.append(_finding("warn", "%d× hole Ø%s on %s is smaller than the sheet thickness %s — it may not punch or cut "
                          "cleanly" % (len(hs), _g(d), _face_words(face), _g(t))))
    outer, edge = g["flat"].OuterWire, {}
    for h in fp["holes"]:
        dist = Part.Vertex(Vector(h["flat"][0], h["flat"][1], 0)).distToShape(outer)[0] - h["d"] / 2
        if dist < t - 1e-6:
            edge.setdefault((h["face"], round(h["d"], 3), round(dist, 2)), []).append(h)
    for (face, d, dist), hs in sorted(edge.items()):
        F.append(_finding("warn", "%d× hole Ø%s on %s: its edge is %s mm from the outline (keep ≥ 1–1.5 × t = %s) — "
                          "the web tears or bulges when punched or bent" % (len(hs), _g(d), _face_words(face), _g(dist),
                                                                         _g(t))))
    for (face, d, n, dist, need), hs in sorted(near.items()):
        F.append(_finding("warn", "%d× hole Ø%s on %s: its edge is %s mm from bend line B%d (keep ≥ 2t + r = %s) — it "
                          "will distort when bent; move it away from the bend" % (len(hs), _g(d), _face_words(face),
                                                                               _g(dist), n, _g(need))))


def _face_words(face):
    """'the base' / 'flange1 (at B1)': flange N is the face bent at bend BN."""
    return "the base" if face == "base" else "%s (at B%s)" % (face, face[len("flange"):]) \
        if face.startswith("flange") else face


def _seg_dist(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L2 = ax * ax + ay * ay
    s = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L2))
    return math.hypot(p[0] - a[0] - s * ax, p[1] - a[1] - s * ay)


# --------------------------------------------------------------------- DFM

PROCESS_ALIASES = {"3dp": "fdm", "print": "fdm", "fff": "fdm", "resin": "sla", "milling": "cnc", "mill": "cnc",
                   "lathe": "turning", "turn": "turning", "waterjet": "laser", "router": "laser",
                   "sheetmetal": "sheet"}
MIN_WALL = standards.MIN_WALL         # one table for check_manufacturing and lookup_standard's rules
PRINT_VOLUME = {"fdm": (250.0, 210.0, 220.0), "sla": (143.0, 89.0, 175.0)}
UPS = {"+Z": Vector(0, 0, 1), "-Z": Vector(0, 0, -1), "+X": Vector(1, 0, 0), "-X": Vector(-1, 0, 0),
       "+Y": Vector(0, 1, 0), "-Y": Vector(0, -1, 0)}


def norm_process(p):
    p = str(p or "").lower().replace("-", "").replace(" ", "")
    return PROCESS_ALIASES.get(p, p)


def _finding(sev, msg, where=None):
    return {"severity": sev, "message": msg, "where": where}


def _mesh_arrays(shape, tol=0.1):
    import MeshPart
    import numpy as np
    m = MeshPart.meshFromShape(Shape=shape, LinearDeflection=tol, AngularDeflection=0.3, Relative=False)
    pts, tris = m.Topology
    P = np.array([[p.x, p.y, p.z] for p in pts])
    T = np.array(tris)
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    n = np.cross(b - a, c - a)
    area = np.linalg.norm(n, axis=1) / 2
    n = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)
    cen = (a + b + c) / 3
    return n, area, cen, P


def print_orientation(shape, overhang_deg=45.0):
    """Score the six axis-aligned orientations: least unsupported overhang,
    most flat contact with the bed, then lowest height."""
    import numpy as np
    n, area, cen, P = _mesh_arrays(shape)
    lim = -math.cos(math.radians(90 - overhang_deg))
    out = []
    for name, up in UPS.items():
        u = np.array([up.x, up.y, up.z])
        h = cen @ u
        base = (P @ u).min()
        nu = n @ u
        on_bed = (h - base) < 0.05
        contact = area[(nu < -0.999) & on_bed].sum()
        overhang = area[(nu < lim) & ~on_bed].sum()
        height = (P @ u).max() - base
        out.append({"up": name, "overhang_mm2": float(overhang), "contact_mm2": float(contact),
                    "height": float(height), "score": float(overhang - 0.25 * contact + 0.01 * height)})
    out.sort(key=lambda r: r["score"])
    return out


def bed_face_words(up):
    """'-X up' in words a print service can follow: which model face goes on the bed."""
    down = ("-" if up[0] == "+" else "+") + up[1:]
    return "the face that points %s in the model down on the bed%s" % (
        down, " (as modelled)" if up == "+Z" else "")


def turned_size(shape, axis, apt):
    """(largest distance from the turning axis, length along it) for a part
    turned about the axis through apt: the vertices (planar faces and coaxial
    circles peak there) and the exact box of the part turned so the axis is Z,
    at 0° and 45° (a cone, sphere or torus can peak inside a face; a dome end
    need not end in a vertex)."""
    def rad(p):
        d = p - apt
        return (d - axis * d.dot(axis)).Length
    r = max([rad(v.Point) for v in shape.Vertexes] or [0.0])
    ts = [v.Point.dot(axis) for v in shape.Vertexes] or [0.0]
    length = max(ts) - min(ts)
    to_z = App.Rotation(axis, App.Vector(0, 0, 1))
    for ang in (0, 45):
        rot = App.Rotation(App.Vector(0, 0, 1), ang).multiply(to_z)
        s = shape.copy()
        s.Placement = App.Placement(rot.multVec(apt) * -1, rot).multiply(s.Placement)
        bb = an.tight_bb(s)
        r = max(r, abs(bb.XMin), abs(bb.XMax), abs(bb.YMin), abs(bb.YMax))
        length = max(length, bb.ZLength)
    return r, length


def stock_words(od, bar):
    if bar is None:
        return "Ø%s finished OD is above the standard bar sizes — use a forging, tube or plate blank" % _g(od)
    return "turned from Ø%s bar for Ø%s finished OD" % (_g(bar), _g(od))


def wall_thickness(shape, samples_per_face=3, limit=None):
    """Minimum local thickness: from points on each face, the length of the
    inward ray that stays inside the solid. Returns (min, where, n_samples)."""
    best = (None, None)
    n = 0
    diag = shape.BoundBox.DiagonalLength
    for f in shape.Faces:
        if f.Area < 1e-3:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        for k in range(samples_per_face):
            fu = (k + 1.0) / (samples_per_face + 1.0)
            u, v = u0 + (u1 - u0) * fu, v0 + (v1 - v0) * (0.5 if k % 2 == 0 else fu)
            try:
                p = f.valueAt(u, v)
                if not f.isInside(p, 1e-4, True):
                    continue
                nrm = f.normalAt(u, v)
            except Exception:  # noqa: BLE001
                continue
            if f.Orientation == "Reversed":
                pass
            seg = Part.LineSegment(p - nrm * 1e-4, p - nrm * diag).toShape()
            try:
                inside = shape.common(seg)
            except Exception:  # noqa: BLE001
                continue
            n += 1
            if not inside.Edges:
                continue
            # the first inside run starting at the face
            runs = sorted(inside.Edges, key=lambda e: min((v.Point - p).Length for v in e.Vertexes))
            t = runs[0].Length
            if t > 1e-4 and (best[0] is None or t < best[0]):
                best = (t, p)
    return best[0], best[1], n


def concave_edges(shape):
    """Straight concave edges between two planar faces — internal corners a
    rotating cutter cannot make sharp when it runs along them. Concave means
    face 1, followed away from the edge, runs into the side face 2 faces."""
    out = []
    for e in shape.Edges:
        if e.Curve.__class__.__name__ not in ("Line", "LineSegment"):
            continue
        faces = shape.ancestorsOfType(e, Part.Face)
        if len(faces) != 2 or any(f.Surface.__class__.__name__ != "Plane" for f in faces):
            continue
        m = e.valueAt((e.FirstParameter + e.LastParameter) / 2)
        t = e.Vertexes[-1].Point - e.Vertexes[0].Point
        if t.Length < 1e-9:
            continue
        t.normalize()
        n1, n2 = _fn(faces[0], m), _fn(faces[1], m)
        if abs(n1.dot(n2)) > 0.999:
            continue
        v1 = n1.cross(t)
        eps = max(1e-3, 1e-4 * shape.BoundBox.DiagonalLength)
        if not faces[0].isInside(m + v1 * eps, 1e-6, True):
            v1 = v1 * -1
        if v1.dot(n2) > 1e-6:
            out.append({"mid": m, "dir": t, "length": e.Length, "normals": (n1, n2)})
    return out


def _axis_aligned(v):
    for name, u in UPS.items():
        if v.dot(u) > 0.999:
            return name, u
    return None, None


def corner_problems(shape):
    """Concave edges an end mill cannot make sharp. A concave edge is fine
    when one of its faces is an axis-aligned floor that a cutter can reach
    (a ray off the floor leaves the part): a flat end mill cuts floor-to-wall
    corners sharp. Anything else needs a radius (vertical internal corners)
    or a ball/5-axis tool (angled ones)."""
    diag = shape.BoundBox.DiagonalLength
    eps = max(0.02, 1e-4 * diag)
    bad = []
    for c in concave_edges(shape):
        n1, n2 = c["normals"]
        ok = False
        for nf, no in ((n1, n2), (n2, n1)):
            name, u = _axis_aligned(nf)
            if not name:
                continue
            q = c["mid"] + nf * eps + no * eps
            seg = Part.LineSegment(q, q + u * diag * 1.5).toShape()
            try:
                hit = shape.common(seg)
            except Exception:  # noqa: BLE001
                continue
            if not hit.Edges or sum(e.Length for e in hit.Edges) < 1e-6:
                ok = True
                break
        if not ok:
            bad.append(c)
    return bad


def _fn(f, p):
    try:
        u, v = f.Surface.parameter(p)
        return f.normalAt(u, v)
    except Exception:  # noqa: BLE001
        u0, u1, v0, v1 = f.ParameterRange
        return f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)


def _face_sample(f):
    """A point inside the (trimmed) face and its outward normal there — the
    parameter midpoint can fall in a hole of the face (a plate top around a pocket)."""
    u0, u1, v0, v1 = f.ParameterRange
    for fu, fv in [(0.5, 0.5)] + [((i + 0.5) / 7, (j + 0.5) / 7) for i in range(7) for j in range(7)]:
        u, v = u0 + (u1 - u0) * fu, v0 + (v1 - v0) * fv
        try:
            p = f.valueAt(u, v)
            if f.isInside(p, 1e-6, True):
                return p, f.normalAt(u, v)
        except Exception:  # noqa: BLE001
            continue
    return None, None


def _ray_tester(shape):
    """free(q, u, length): True when the ray from q (a point in free space)
    meets no face of shape. Only the faces whose bounding box the ray crosses
    are sectioned, which keeps it fast on parts with many holes."""
    boxes = [(f, f.BoundBox) for f in shape.Faces]

    def free(q, u, length):
        end = q + u * length
        rb = App.BoundBox()
        rb.add(q)
        rb.add(end)
        rb.enlarge(1e-6)
        cands = [f for f, b in boxes if b.intersect(rb)]
        if not cands:
            return True
        try:
            sec = Part.LineSegment(q, end).toShape().section(Part.makeCompound(cands))
        except Exception:  # noqa: BLE001
            return False
        return not sec.Vertexes and not sec.Edges
    return free


def machining_access(shape):
    """Split the faces into outer faces (on the stock's bounding box, or
    full-height profile walls: facing and profiling) and feature faces
    (pockets, steps, holes, chamfers…), find the machine-axis directions a
    cutter can reach each feature face from, and pick a small set of
    directions that covers them all (greedy). Also returns the vertical
    internal corner radii (concave cylinders, not full holes, that only a
    setup along their axis can cut) with the wall depth at each, and the
    radii of concave cylinders a setup across their axis reaches (floor
    fillets, grooves, half-bores: ball/bull-nose features)."""
    bb = an.tight_bb(shape)
    diag = bb.DiagonalLength
    eps = max(0.02, 1e-4 * diag)
    lo = {"X": bb.XMin, "Y": bb.YMin, "Z": bb.ZMin}
    hi = {"X": bb.XMax, "Y": bb.YMax, "Z": bb.ZMax}
    span = {"X": bb.XLength, "Y": bb.YLength, "Z": bb.ZLength}
    free = _ray_tester(shape)
    outer, feats, unreachable = set(), [], 0
    for f in shape.Faces:
        if f.Area < 1e-6:
            continue
        p, n = _face_sample(f)
        if p is None:
            continue
        srf = f.Surface.__class__.__name__
        name, u = _axis_aligned(n)
        if srf == "Plane" and name:
            ax = name[1]
            side = hi[ax] if name[0] == "+" else lo[ax]
            if abs({"X": p.x, "Y": p.y, "Z": p.z}[ax] - side) < 1e-4:
                outer.add(name)
                continue
        elif not (srf == "Cylinder" and an._is_concave(f)):
            fb = f.BoundBox
            fspan = {"X": fb.XLength, "Y": fb.YLength, "Z": fb.ZLength}
            if any(abs(n.dot(UPS["+" + a])) < 1e-6 and abs(fspan[a] - span[a]) < 1e-3 for a in "XYZ") \
                    and free(p + n * eps, n, diag * 1.5):
                continue                      # full-height profile wall (rounded corner, angled edge)
        q = p + n * eps
        dirs = [d for d, v in UPS.items() if v.dot(n) > -1e-6 and free(q, v, diag * 1.5)]
        if not dirs:
            unreachable += 1
            continue
        feats.append((f, dirs, p))
    # greedy cover, preferring +Z, -Z, … on ties
    order = ["+Z", "-Z", "+X", "-X", "+Y", "-Y"]
    left, picked = list(range(len(feats))), []
    while left:
        best = max(order, key=lambda d: (sum(1 for i in left if d in feats[i][1]), -order.index(d)))
        picked.append(best)
        left = [i for i in left if best not in feats[i][1]]
    # concave cylinders that are not full holes, grouped per corner (the faces of one corner share the
    # directions that reach any of them). An end-mill corner is cut along its axis: reached along the axis
    # by a chosen setup and from no chosen setup across it. A cylinder reached across its axis (floor
    # fillet, groove, half-bore) — or running through along its axis while also reachable across it, so
    # whatever setups the cover happened to pick — is cut with a ball or bull-nose cutter: no depth limit.
    groups = {}
    for f, dirs, p in feats:
        if f.Surface.__class__.__name__ != "Cylinder" or not an._is_concave(f):
            continue
        s = f.Surface
        a = an._canon(s.Axis)
        an_ = an._axis_name(a)
        if an_[0] not in "+-":
            continue                          # tilted axis: a compound-angle feature, reported as such
        c = an._axis_point(s)
        key = (an_, round(c.x, 3), round(c.y, 3), round(c.z, 3), round(s.Radius, 4))
        g = groups.setdefault(key, {"r": s.Radius, "span": 0.0, "ts": [], "at": p, "ax": an_[1:], "dirs": set()})
        u0, u1 = f.ParameterRange[:2]
        g["span"] += abs(u1 - u0)
        g["ts"] += [v.Point.dot(a) for v in f.Vertexes]
        g["dirs"].update(dirs)
    part = [g for g in groups.values() if g["span"] < 2 * math.pi * 0.98 and g["ts"]]     # full holes left out
    for g in part:
        along = {d for d in g["dirs"] if d[1:] == g["ax"]}
        across = g["dirs"] - along
        g["corner"] = (bool(along & set(picked)) and not (across & set(picked))
                       and not (len(along) == 2 and across))
    corners = [{"r": g["r"], "depth": max(g["ts"]) - min(g["ts"]), "at": g["at"]} for g in part if g["corner"]]
    return {"outer": sorted(outer, key=order.index), "feature_dirs": picked, "unreachable": unreachable,
            "corners": corners, "across_radii": sorted(set(round(g["r"], 4) for g in part if not g["corner"]))}


NOT_PRINTED = ("cnc", "turning", "laser", "sheet")


def built_process(project, label):
    """The process a part is built for (its holes compensated for): parts.<label>.process → process → fdm."""
    project = project or {}
    return norm_process(((project.get("parts") or {}).get(label) or {}).get("process") or project.get("process")
                        or "fdm")


def print_comp_text(comp, process, label, built=None):
    """The warning for a part built with 3D-print hole compensation but made another way."""
    fix = ("set project.json parts.%s.process to \"%s\" (or fc.holes(..., compensation=0)) and build again"
           % (label, process) if built != process else
           "remove compensation=… from its fc.holes(...) calls in model.py (or set it to 0) and build again")
    return ("holes are modelled %s mm larger in diameter by the 3D-print hole compensation, so the model's holes are "
            "oversize for %s — rebuild with compensation 0 before %s: %s" % (
                _g(comp), PROCESS_NAME.get(process, process), "cutting" if process in ("laser", "sheet")
                else "machining", fix))


def dfm(obj, process, project=None):
    """Manufacturability findings for one part and process."""
    process = norm_process(process)
    shape = an.global_shape(obj)
    bb = an.tight_bb(shape)
    out = {"part": obj.Label, "process": process, "findings": [], "metrics": {}}
    F = out["findings"]
    if not shape.Solids:
        F.append(_finding("error", "not a solid"))
        return out
    holes = an.detect_holes(shape)
    hole_feats = [h for h in getattr(obj, "Group", []) if h.TypeId == "PartDesign::Hole"]
    project = project or {}
    minwall = float(((project.get("dfm") or {}).get("min_wall_mm")) or MIN_WALL.get(process, 1.0))
    comp = max([float(getattr(h, "FcHoleComp", 0.0) or 0.0) for h in hole_feats] or [0.0])
    if comp > 0 and process in NOT_PRINTED:
        F.append(dict(_finding("warn", print_comp_text(comp, process, obj.Label, built_process(project, obj.Label))),
                      code="print_comp"))
    if process in ("fdm", "sla"):
        ori = print_orientation(shape)
        best = ori[0]
        want = (((project or {}).get("parts") or {}).get(obj.Label) or {}).get("print_up")
        if want:
            # chosen for strength (layers along the bending stress) rather than overhang and bed contact
            try:
                w = _norm_up(want)
            except MfgError as e:
                F.append(_finding("error", "parts.%s.print_up: %s" % (obj.Label, e)))
                w = None
            chosen = next((o for o in ori if o["up"] == w), None)
            if chosen is not None:
                if chosen is not best:
                    F.append(_finding("info", "print orientation set in project.json (parts.%s.print_up %s); by "
                                      "overhang and bed contact alone the best would be to %s" % (
                                          obj.Label, w, bed_face_words(best["up"]))))
                best = chosen
        out["metrics"]["orientation"] = best
        F.append(_finding("info", "print with %s: overhang %.0f mm², bed contact %.0f mm², height %s mm" % (
            bed_face_words(best["up"]), best["overhang_mm2"], best["contact_mm2"], fmt(best["height"], 1))))
        if best["contact_mm2"] < 20:
            F.append(_finding("warn", "little flat area on the bed in the best orientation — add a brim or a flat"))
        if best["overhang_mm2"] > 0.05 * shape.Area:
            F.append(_finding("warn", "%s mm² of overhang beyond 45° even in the best orientation — needs supports "
                              "or a chamfer/teardrop redesign" % fmt(best["overhang_mm2"], 0)))
        vol = (project.get("printer") or {}).get("volume") or PRINT_VOLUME.get(process)
        size = sorted([bb.XLength, bb.YLength, bb.ZLength])
        if vol and any(s > v for s, v in zip(size, sorted(vol))):
            F.append(_finding("error", "part %s × %s × %s mm does not fit the build volume %s" % (
                fmt(bb.XLength, 1), fmt(bb.YLength, 1), fmt(bb.ZLength, 1), " × ".join("%g" % v for v in vol))))
        up = UPS[best["up"]]
        for h in holes:
            d = h["diameters"][0]
            if d < (2.0 if process == "fdm" else 1.0):
                F.append(_finding("warn", "hole Ø%s is below the reliable printable size — drill it out after "
                                  "printing" % fmt(d, 2), an._pt(h["at"])))
            if abs(h["axis"].dot(up)) < 0.1 and max(h["diameters"]) > 6 and process == "fdm":
                F.append(_finding("warn", "horizontal hole Ø%s in the print orientation: its top will sag — use a "
                                  "teardrop or flat-topped profile" % fmt(max(h["diameters"]), 1), an._pt(h["at"])))
        for hf in hole_feats:
            if hf.Threaded and process == "fdm":
                F.append(_finding("warn", "%s is tapped into plastic (%s) — prefer heat-set inserts ('M3 insert') or "
                                  "a nut trap" % (hf.Label, hf.ThreadSize)))
    if process in ("fdm", "sla", "cnc"):
        t, where, n = wall_thickness(shape)
        out["metrics"]["min_wall"] = t
        if t is not None and t < minwall:
            F.append(_finding("warn", "thinnest wall %s mm < %s mm for %s" % (fmt(t, 2), fmt(minwall, 2), process),
                              an._pt(where)))
        elif t is not None:
            F.append(_finding("info", "thinnest wall %s mm (≥ %s ✓, %d samples)" % (fmt(t, 2), fmt(minwall, 2), n)))
    if process == "cnc":
        bad = corner_problems(shape)
        vertical = [c for c in bad if _axis_aligned(an._canon(c["dir"]))[0]]
        angled = [c for c in bad if not _axis_aligned(an._canon(c["dir"]))[0]]
        by_axis = {}
        for c in vertical:
            by_axis.setdefault(an._axis_name(an._canon(c["dir"])), []).append(c)
        for ax, cs in by_axis.items():
            F.append(_finding("warn", "%d sharp internal corner(s) along %s that an end mill cannot reach as a floor "
                              "corner — add fillets ≥ 1 mm (ideally ≥ ⅓ of the pocket depth) or accept tool-radius "
                              "corners on the drawing" % (len(cs), ax), an._pt(cs[0]["mid"])))
        if angled:
            F.append(_finding("warn", "%d sharp internal corner(s) at an angle to every machine axis — need a ball "
                              "end mill, 4/5-axis machining or a fillet" % len(angled), an._pt(angled[0]["mid"])))
        out["metrics"]["sharp_internal_corners"] = len(bad)
        compound = 0
        for f in shape.Faces:
            if f.Surface.__class__.__name__ != "Plane":
                continue
            nv = _fn(f, f.CenterOfMass)
            if not _axis_aligned(nv)[0] and min(abs(nv.x), abs(nv.y), abs(nv.z)) > 1e-6:
                compound += 1                  # tilted to all three axes: not a side-milled wall
        acc = machining_access(shape)
        fd = acc["feature_dirs"]
        out["metrics"].update(setups_estimate=max(1, len(fd)), feature_access=fd, outer_sides=acc["outer"])
        F.append(_finding("info", "%s; outer faces from %d side(s) (facing and profiling)" % (
            "features need access from %s" % " and ".join(fd) if fd else "no features beyond the outer faces",
            len(acc["outer"]))))
        if acc["unreachable"]:
            F.append(_finding("info", "%d feature face(s) cannot be reached straight along any machine axis — check "
                              "for undercuts (T-slots, dovetails, internal grooves) that need special cutters or "
                              "a redesign" % acc["unreachable"]))
        if acc["corners"]:
            rmin = min(c["r"] for c in acc["corners"])
            deep = [c for c in acc["corners"] if c["depth"] > 4 * 2 * c["r"] + 1e-6]
            dmax = max(c["depth"] for c in acc["corners"] if abs(c["r"] - rmin) < 1e-6)
            out["metrics"].update(min_corner_radius=rmin, max_tool_d=2 * rmin)
            F.append(_finding("info", "internal corners R%s → end mill ≤ Ø%s%s" % (
                _g(rmin), _g(2 * rmin), "" if deep else " (pocket depth %s ≤ 4 × Ø%s ✓)" % (_g(dmax), _g(2 * rmin)))))
            by_r = {}
            for c in deep:
                by_r.setdefault(round(c["r"], 4), []).append(c)
            for r, cs in sorted(by_r.items()):
                dd = max(c["depth"] for c in cs)
                F.append(_finding("warn", "%d internal corner(s) R%s on walls up to %s mm deep — deeper than 4 × the "
                                  "Ø%s end mill that fits them; use corner radii ≥ R%s (depth ÷ 8) or a shallower "
                                  "pocket" % (len(cs), _g(r), _g(dd), _g(2 * r), _g(math.ceil(dd / 8.0 * 10) / 10.0)),
                                  an._pt(cs[0]["at"])))
        if acc["across_radii"]:
            ar = acc["across_radii"]
            F.append(_finding("info", "concave radii %s across the cutting direction (floor fillets, grooves, "
                              "half-bores) → ball or bull-nose end mill, corner radius ≤ R%s" % (
                                  ", ".join("R%s" % _g(x) for x in ar), _g(ar[0]))))
        if compound:
            F.append(_finding("warn", "%d face(s) tilted to all three machine axes — need a 4/5-axis machine, an "
                              "angled fixture or a redesign" % compound))
        for h in holes:
            if not h["through"] and h["depth"] > 10 * h["diameters"][0]:
                F.append(_finding("warn", "blind hole Ø%s is %s deep (> 10 × D) — gun drilling or redesign" % (
                    fmt(h["diameters"][0], 2), fmt(h["depth"], 1)), an._pt(h["at"])))
    if process == "turning":
        import drawing_support as ds
        axis, apt, frac = ds.turned_axis(shape)
        out["metrics"]["turned_fraction"] = frac
        if axis is None or frac < 0.85:
            F.append(_finding("error", "only %d %% of the surface is turned about one axis — this is a milled part"
                              % round(100 * frac)))
        else:
            radii = [f.Surface.Radius for f in shape.Faces if f.Surface.__class__.__name__ == "Cylinder"
                     and abs(abs(f.Surface.Axis.dot(axis)) - 1) < 1e-6 and not an._is_concave(f)]
            # bar and L/D from the largest diameter of the whole part (a cone head, ball or bead can
            # be larger than every cylinder); the slenderest section from the cylinders
            rmax, length = turned_size(shape, axis, apt)
            dmax, length = round(2 * rmax, 4), round(length, 4)
            dmin = 2 * min(radii) if radii else 0
            bar = standards.bar_for(dmax) if dmax else None
            out["metrics"].update(length=length, max_d=dmax, min_d=dmin, bar_d=bar, cut_length=length + 3.0)
            ratio = length / dmax if dmax else 0
            F.append(_finding("info", "%s; finished length %s, cut length ≥ %s (L/D %s)" % (
                stock_words(dmax, bar), _g(length, 1), _g(length + 3.0, 1), fmt(ratio, 1))))
            if ratio > 6:
                F.append(_finding("warn", "L/D %s > 6 — needs a tailstock and steady rest; consider shortening" % fmt(ratio, 1)))
            elif ratio > 3:
                F.append(_finding("info", "L/D %s > 3 — will need tailstock support" % fmt(ratio, 1)))
            if frac < 0.999:
                F.append(_finding("info", "non-turned features (cross holes, flats) need a secondary drilling/milling op"))
            if dmin and dmin < 2:
                F.append(_finding("warn", "Ø%s section is very slender for turning" % fmt(dmin, 2)))
            threads = json.loads(getattr(obj, "FcThreads", "") or "[]")
            for t in threads:
                F.append(_finding("info", "external thread %s×%s-%s, %s long" % (
                    t["size"], t["pitch"], t["fit"], fmt(t["length"], 1))))
    sheet = sheet_of(obj)
    if sheet is not None and process in ("fdm", "sla", "cnc", "turning"):
        F.append(_finding("warn", "built as bent sheet metal (fc.sheet_metal) but its process is %s — set project.json "
                          "parts.%s.process to \"sheet\" for the flat pattern and bend checks" % (process, obj.Label)))
    if sheet is not None and process in ("laser", "sheet") and (sheet.get("bends") or process == "sheet"):
        _dfm_sheet(obj, shape, sheet, process, out)
    elif process in ("laser", "sheet"):
        prof = flat_profile(shape)
        out["metrics"]["thickness"] = prof["thickness"]
        if not prof["uniform"]:
            F.append(_finding("error", "not a uniform plate — pockets, steps or chamfers cannot be cut from sheet%s" % (
                "; a bent part is built with fc.sheet_metal (flanges, bends, flat pattern)" if process == "sheet"
                else "")))
        else:
            F.append(_finding("info", "cut from %s mm sheet, profile %s × %s mm" % (
                fmt(prof["thickness"], 2), fmt(prof["size"][0], 1), fmt(prof["size"][1], 1))))
        small = {}
        for h in holes:
            if h["diameters"][0] < prof["thickness"]:
                small.setdefault(round(h["diameters"][0], 3), []).append(h)
        for d, hs in sorted(small.items()):
            F.append(_finding("warn", "%d× hole Ø%s smaller than the sheet thickness %s — may not cut cleanly in "
                              "metal (fine in plastics/wood)" % (len(hs), fmt(d, 2), fmt(prof["thickness"], 2)),
                              an._pt(hs[0]["at"])))
    if process not in ("fdm", "sla", "cnc", "turning", "laser", "sheet"):
        F.append(_finding("error", "unknown process %r (fdm, sla, cnc, turning, laser, sheet)" % process))
    sev = [f["severity"] for f in F]
    out["verdict"] = "blocked" if "error" in sev else ("check" if "warn" in sev else "ok")
    return out


def dfm_lines(res):
    mark = {"ok": "✓", "check": "⚠", "blocked": "✗"}[res["verdict"]]
    lines = ["%s %s — %s: %s" % (mark, res["part"], res["process"], {"ok": "ready", "check": "review the warnings",
                                                                   "blocked": "not manufacturable as is"}[res["verdict"]])]
    icon = {"error": "✗", "warn": "⚠", "info": "·"}
    for f in res["findings"]:
        lines.append("    %s %s%s" % (icon[f["severity"]], f["message"], "  at (%s)" % f["where"] if f.get("where") else ""))
    return lines


# ----------------------------------------------------------------- release

RELEASE_FORMATS = {"fdm": ["3mf", "stl", "step"], "sla": ["3mf", "stl", "step"], "cnc": ["step"],
                   "turning": ["step"], "laser": ["dxf", "svg", "step"], "sheet": ["dxf", "step"]}
NEEDS_DRAWING = ("cnc", "turning", "laser", "sheet")
PROCESS_NAME = {"fdm": "3D print (FDM)", "sla": "3D print (resin/SLA)", "cnc": "CNC milling", "turning": "CNC turning",
                "laser": "laser / waterjet cutting", "sheet": "sheet metal"}
GENERAL_TOL_PRINT = {"fdm": "±0.2 mm", "sla": "±0.1 mm"}


def part_record(obj, process, project):
    """Everything the release notes need to know about one part."""
    shape = an.global_shape(obj)
    bb = an.tight_bb(shape)
    pmat = project.get("material") or {}
    pcfg = (project.get("parts") or {}).get(obj.Label) or {}
    mat = pcfg.get("material") or pmat.get("name", "")
    dens, dsrc = standards.density_info(project, obj.Label)
    dnote = None
    if dens and dsrc == "project material" and pcfg.get("material") and \
            not standards.same_material(pcfg["material"], pmat.get("name")):
        dnote = ("mass uses the project density %s g/cm³ (%s), but this part is %s — set parts.%s.density_g_cm3 "
                 "in project.json" % (_g(dens), pmat.get("name", "?"), pcfg["material"], obj.Label))
    holes, comp = [], 0.0
    for h in getattr(obj, "Group", []):
        if h.TypeId == "PartDesign::Hole":
            try:
                n = len([g for g in h.Profile[0].Geometry if g.__class__.__name__ == "Circle"])
            except Exception:  # noqa: BLE001
                n = 1
            hc = float(getattr(h, "FcHoleComp", 0.0) or 0.0)
            comp = max(comp, hc)
            if h.Threaded:
                txt = "%d× %s-%s %s" % (n, h.ThreadSize, h.ThreadClass,
                                        "THRU" if h.DepthType == "ThroughAll" else "depth %s" % fmt(h.Depth.Value, 1))
            else:
                txt = "%d× Ø%s %s%s" % (n, _g(h.Diameter.Value),
                                        "THRU" if h.DepthType == "ThroughAll" else "depth %s" % fmt(h.Depth.Value, 1),
                                        " %s" % h.FcFit if getattr(h, "FcFit", "") else "")
            if h.HoleCutType not in ("None", ""):
                txt += " + %s Ø%s" % (h.HoleCutType, fmt(h.HoleCutDiameter.Value, 1))
            if hc > 0:
                txt += " (Ø%s nominal + %s print compensation)" % (_g(h.Diameter.Value - hc), _g(hc))
            holes.append(txt)
    sheet = sheet_of(obj)
    sheet_rec = None
    if sheet is not None:                       # fc.sheet_metal: holes as specified, per face; the bend table
        groups = {}
        for h in (sheet.get("spec") or {}).get("holes") or []:
            if h.get("d") is None:
                continue
            k = (h.get("on", "base"), round(float(h["d"]), 4), h.get("spec") or "")
            groups[k] = groups.get(k, 0) + len(h.get("at") or [])
        holes = ["%d× Ø%s THRU%s on %s" % (n, _g(d), " (%s)" % sp if sp and not sp.startswith(("Ø", "ø")) else "",
                                           _face_words(face)) for (face, d, sp), n in groups.items()] or holes
        bends = [b for b in sheet.get("bends") or [] if isinstance(b, dict) and len(b.get("line") or []) == 2]
        sheet_rec = {"thickness": sheet.get("thickness"), "radius": sheet.get("radius"),
                     "k_factor": sheet.get("k_factor"), "flat_size": sheet.get("flat_size") or [0, 0],
                     "bends": [{"n": b.get("n", i), "angle": b.get("angle", 90), "radius": b.get("radius", 0),
                                "up": bool(b.get("up", True)), "length": b.get("length", 0), "line": b["line"]}
                               for i, b in enumerate(bends, 1)]}
    if not holes:
        groups = {}
        for h in an.detect_holes(shape):
            k = (tuple(h["diameters"]), h["through"])
            groups[k] = groups.get(k, 0) + 1
        holes = ["%d× Ø%s %s" % (n, "/Ø".join(fmt(d, 2) for d in k[0]), "THRU" if k[1] else "blind")
                 for k, n in sorted(groups.items())]
    fits = json.loads(getattr(obj, "FcFits", "") or "{}")
    fit_txt = []
    for d, cls in sorted(fits.items(), key=lambda kv: float(kv[0])):    # upper deviation first, as the drawing
        fit_txt.append("Ø%s %s (%s)" % (d, cls, standards.deviation_text(float(d), cls)))
    threads = ["%s×%s-%s, %s long" % (t["size"], t["pitch"], t["fit"], fmt(t["length"], 1))
               for t in json.loads(getattr(obj, "FcThreads", "") or "[]")]
    notes = pcfg.get("notes") or []
    notes = [notes] if isinstance(notes, str) else list(notes)
    return {"part": obj.Label, "process": process, "material": mat, "finish": pcfg.get("finish", ""),
            "qty": int(pcfg.get("qty", 1)), "size": [bb.XLength, bb.YLength, bb.ZLength],
            "mass_g": shape.Volume * dens / 1000.0 if dens else None, "density": dens,
            "density_source": dsrc, "density_note": dnote, "holes": holes, "fits": fit_txt, "threads": threads,
            "notes": [str(t).strip() for t in notes if str(t).strip()], "hole_compensation": comp,
            "sheet": sheet_rec}


def item_size(shape):
    """Size of ONE item of a reference part that may hold several separate
    items (4 screws, 2 washers in one compound): touching solids form one item.
    → {"size": [x, y, z] of the largest item, "count": items found,
    "identical": every item has that size (±0.01)}."""
    solids = list(shape.Solids)
    if len(solids) <= 1:
        bb = an.tight_bb(shape)
        return {"size": [bb.XLength, bb.YLength, bb.ZLength], "count": 1, "identical": True}
    parent = list(range(len(solids)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    bbs = [s_.BoundBox for s_ in solids]
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            a, b = bbs[i], bbs[j]
            if (a.XMin > b.XMax + 1e-3 or b.XMin > a.XMax + 1e-3 or a.YMin > b.YMax + 1e-3
                    or b.YMin > a.YMax + 1e-3 or a.ZMin > b.ZMax + 1e-3 or b.ZMin > a.ZMax + 1e-3):
                continue
            if root(i) != root(j) and solids[i].distToShape(solids[j])[0] < 1e-3:
                parent[root(i)] = root(j)
    groups = {}
    for i, s_ in enumerate(solids):
        groups.setdefault(root(i), []).append(s_)
    items = []
    for g in groups.values():
        bb = an.tight_bb(Part.makeCompound(g))
        items.append((sum(x.Volume for x in g), [bb.XLength, bb.YLength, bb.ZLength]))
    items.sort(key=lambda it: -it[0])
    big = sorted(items[0][1])
    same = all(all(abs(a - b) <= 0.01 for a, b in zip(sorted(it[1]), big)) for it in items)
    return {"size": items[0][1], "count": len(items), "identical": same}


def _ref_size(r):
    """'each 10.0 × 10.0 × 4.0; 2 in model' for a ref holding several items (item_size), else its size."""
    n = int(r.get("count") or 1)
    if n > 1:
        return "%s %s; %d in model" % ("each" if r.get("identical", True) else "largest", _size(r["size"]), n)
    return _size(r["size"])


def _short_qty(r):
    """'⚠ qty 1 but 4 in the model — …' for a bought ref whose qty is below the items found, else ''."""
    n = int(r.get("count") or 1)
    try:
        q = int(r.get("qty", 1))
    except (TypeError, ValueError):
        return ""
    if r.get("bom") != "buy" or n <= q:
        return ""
    return ("⚠ qty %d but %d in the model — set fc.ref_part(..., qty=%d) (the total to buy) in model.py if all of "
            "them are bought" % (q, n, n))


def _dims_status(v):
    """drawing_dims from the server ([verified, total], {verified, total}, 'n/m' or a bare count) →
    (text, mark): ✓ only when every dimension on the sheet was verified and there is at least one;
    ⚠ when some (or all) were not; — when the total is unknown."""
    if v is None or v == "":
        return "dimension check not reported", "—"
    ok, tot = v, None
    if isinstance(v, dict):
        ok, tot = v.get("verified"), v.get("total")
    elif isinstance(v, (list, tuple)) and len(v) == 2:
        ok, tot = v
    elif isinstance(v, str):
        a, _, b = v.partition("/")
        if not (a.strip().isdigit() and (not b or b.strip().isdigit())):
            return v, "—"
        ok, tot = int(a), (int(b) if b else None)
    try:
        ok, tot = int(ok), (int(tot) if tot is not None else None)
    except (TypeError, ValueError):
        return str(v), "—"
    if tot is None:
        if ok <= 0:
            return "no dimensions verified against the model", "⚠"
        return "%d dimension%s verified against the model (total not reported)" % (ok, "" if ok == 1 else "s"), "—"
    if tot <= 0:
        return "no dimensions on the sheet to verify", "⚠"
    return "%d/%d dimensions verified against the model" % (ok, tot), "✓" if ok >= tot and ok > 0 else "⚠"


# Build-report lines that are bookkeeping for the designer, not facts for the supplier:
# reuse/adoption notes, the Δ against an earlier (maybe internal) build, file paths, model.py's stdout.
_REPORT_DROP = ("note", "Δ", "files", "stdout")


def supplier_report(text):
    """The build report as shown in the release README: without internal lines
    (a dropped header takes its indented lines with it)."""
    out, skipping = [], False
    for ln in (text or "").splitlines():
        if ln.startswith(" ") and skipping:
            continue
        skipping = ln.split(" ", 1)[0] in _REPORT_DROP or ln.startswith("render failed")
        if not skipping:
            out.append(ln)
    return "\n".join(out).strip()


def _drawing_of(p):
    d = p.get("drawing")
    if d:
        return d
    return next((f for f in p.get("files") or [] if f.lower().endswith("_drawing.pdf")), None)


def _send_text(p, dwg, oriented):
    proc = p["process"]
    if proc in ("fdm", "sla"):
        s = "the 3MF (or STL) to the %sprint service — %s" % (
            "resin " if proc == "sla" else "",
            "already oriented for printing, place as imported" if oriented else "orientation below")
        return s + ("; the PDF drawing for reference" if dwg else "")
    s = {"cnc": "the STEP model", "turning": "the STEP model", "laser": "the DXF profile (1:1, mm)",
         "sheet": "the DXF flat pattern and the STEP model"}.get(proc, "the files below")
    if dwg:
        s += {"cnc": " and the PDF drawing (the drawing governs tolerances and threads)",
              "turning": " and the PDF drawing (the drawing governs diameters, fits and threads)"}.get(
            proc, " and the PDF drawing")
    if proc in ("laser", "sheet"):
        s += "; state material and thickness"
    if proc in NEEDS_DRAWING and not dwg:
        s += (" — ⚠ no drawing included: tolerances, threads and fits are not communicated (release with "
              "drawings=\"auto\" to include one)")
    return s


def _size(v):
    return " × ".join("%.1f" % x for x in v)


def write_release_docs(rel_dir, project, rev, parts, refs, checks, report_text, checks_changes=None):
    """BOM.csv and README.md (manufacturing notes) for the release folder.
    parts: part_record()s with dfm, files, exports, drawing and optional
    drawing_dims ([verified, total] dimensions from the drawing tool); refs:
    {part, bom: buy|context|none, qty, spec, size, count?, identical?} where
    size is ONE item and count the items found (item_size); checks_changes:
    {since, added, removed, changed} against the previous release, or None."""
    name = project.get("name", "project")
    refs = refs or []
    buy = [r for r in refs if r.get("bom") == "buy"]
    context = [r for r in refs if (r.get("bom") or "context") == "context"]
    files_of = {}
    for p in parts:
        fs = list(p.get("files") or [])
        dwg = _drawing_of(p)
        if dwg and dwg not in fs:
            fs.append(dwg)
        files_of[p["part"]] = fs
    with open(os.path.join(rel_dir, "BOM.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item", "part", "qty", "make/buy", "process", "material", "finish", "mass_g", "size_mm", "files",
                    "spec", "notes"])
        for i, p in enumerate(parts, 1):
            w.writerow([i, p["part"], p["qty"], "make", PROCESS_NAME.get(p["process"], p["process"]), p["material"],
                        p["finish"], "%.1f" % p["mass_g"] if p.get("mass_g") else "", _size(p["size"]),
                        " ".join(os.path.basename(f) for f in files_of[p["part"]]), "",
                        "; ".join(p.get("notes") or [])])
        for j, r in enumerate(buy, len(parts) + 1):
            n = int(r.get("count") or 1)
            note = [("size of one item; %d in model" if r.get("identical", True) else
                     "size of the largest item; %d in model (sizes differ)") % n] if n > 1 else []
            if _short_qty(r):
                note.append(_short_qty(r))
            w.writerow([j, r["part"], r.get("qty", 1), "buy", "", "", "", "", _size(r["size"]), "",
                        r.get("spec") or "", "; ".join(note)])
    exports = [(p, e) for p in parts for e in p.get("exports") or []]
    failed = [e for _, e in exports if not e.get("ok")]
    L = ["# %s — release %s" % (name, rev), "",
         "Generated %s from `model.py` (build %s). %s" % (
             time.strftime("%Y-%m-%d %H:%M"), rev,
             "All %d acceptance checks pass." % len(checks) if checks and all(c["ok"] for c in checks)
             else "Acceptance checks: %d/%d pass." % (sum(1 for c in checks if c["ok"]), len(checks)) if checks
             else "No acceptance checks defined."), ""]
    if failed:
        L += ["**⚠ %d exported file(s) failed verification — see Files below. Do not send them as they are.**"
              % len(failed), ""]
    if project.get("description"):
        L += [project["description"], ""]
    L += ["## Parts to make", "", "| # | Part | Qty | Process | Material | Finish | Files |", "|---|---|---|---|---|---|---|"]
    for i, p in enumerate(parts, 1):
        L.append("| %d | %s | %d | %s | %s | %s | %s |" % (
            i, p["part"], p["qty"], PROCESS_NAME.get(p["process"], p["process"]), p["material"] or "—",
            p["finish"] or "—", ", ".join("`%s`" % os.path.relpath(f, rel_dir) for f in files_of[p["part"]])))
    gt = (project.get("drawing") or {}).get("general_tolerance", "ISO 2768-m")
    for p in parts:
        proc = p["process"]
        dwg = _drawing_of(p)
        oriented = next((e.get("oriented") for e in p.get("exports") or [] if e.get("oriented")), None)
        dfm = p.get("dfm") or {}
        m = dfm.get("metrics") or {}
        L += ["", "### %s — %s" % (p["part"], PROCESS_NAME.get(proc, proc)), ""]
        L.append("- **Send:** %s." % _send_text(p, dwg, oriented))
        mass = ""
        if p.get("mass_g"):
            mass = ", mass %.1f g" % p["mass_g"] + (" (%s g/cm³)" % _g(p["density"]) if p.get("density") else "")
        L.append("- Size %s mm%s; material %s; quantity %d." % (
            _size(p["size"]), mass, p["material"] or "—", p["qty"]))
        if p.get("density_note"):
            L.append("- ⚠ %s." % p["density_note"])
        ori = m.get("orientation") if proc in ("fdm", "sla") else None
        if ori or oriented:
            up = oriented or ori["up"]
            face = "%sthe face that points %s in the model goes down on the bed" % (
                "as modelled — " if up == "+Z" else "", ("-" if up[0] == "+" else "+") + up[1:])
            if ori and ori["up"] == up:
                face += "; overhang %.0f mm², bed contact %.0f mm²" % (ori["overhang_mm2"], ori["contact_mm2"])
            if oriented:
                L.append("- Print orientation: 3MF/STL are already oriented for printing — place as imported "
                         "(%s)." % face)
            else:
                L.append("- Print orientation: %s. The 3MF/STL are in model orientation — rotate them before "
                         "slicing." % (face[0].upper() + face[1:]))
        sm = p.get("sheet") if proc == "sheet" else None
        if proc in ("laser", "sheet"):
            t = (sm or {}).get("thickness") or m.get("thickness") or min(p["size"])
            L.append("- Material thickness: %s mm." % _g(t))
        if sm:
            nb = len(sm.get("bends") or [])
            L.append("- Flat pattern %s × %s mm in the DXF (1:1, mm): cut outline and holes on layer CUT%s. Developed "
                     "with K-factor %s and inside bend radius R%s (bend allowance π/180 · angle · (R + K·t)) — if your "
                     "tooling bends with a different allowance, develop the flat from the STEP: the folded dimensions "
                     "govern." % (_g(sm["flat_size"][0]), _g(sm["flat_size"][1]),
                                  ", bend lines with their notes on layer BEND" if nb else "",
                                  _g(sm.get("k_factor") or 0, 3),
                                  _g(sm.get("radius") or 0)))
            if nb:
                L.append("- %d bend%s — see the bend table below (UP = toward you, with the DXF as drawn)."
                         % (nb, "" if nb == 1 else "s"))
        if proc == "turning" and m.get("max_d"):
            bar = m.get("bar_d") or standards.bar_for(m["max_d"])
            L.append("- Stock: %s; cut length ≥ %s mm (finished length %s + 3 mm for facing and parting off)." % (
                stock_words(m["max_d"], bar).replace("turned from ", ""), _g(m["length"] + 3.0, 1),
                _g(m["length"], 1)))
        if p["holes"]:
            L.append("- Holes: " + "; ".join(p["holes"]) + ".")
        if p["fits"]:
            L.append("- Fits: " + "; ".join(p["fits"]) + ".")
        if p["threads"]:
            L.append("- External threads: " + "; ".join(p["threads"]) + ".")
        if proc in NEEDS_DRAWING:
            L.append("- General tolerances %s%s." % (gt, " unless stated on the drawing" if dwg else ""))
        if proc in GENERAL_TOL_PRINT:
            L.append("- General tolerance: typ. %s (%s)%s." % (
                GENERAL_TOL_PRINT[proc], proc.upper(), " unless stated on the drawing" if dwg else ""))
            if (p.get("hole_compensation") or 0) > 0:
                L.append("- Holes are modelled %s mm larger in diameter to compensate for printing (in the 3MF/STL "
                         "and the STEP) — print as modelled, no further hole compensation." % _g(p["hole_compensation"]))
        if (p.get("hole_compensation") or 0) > 0 and proc not in GENERAL_TOL_PRINT:
            warn = print_comp_text(p["hole_compensation"], proc, p["part"], built_process(project, p["part"]))
            L.append("- ⚠ The %s." % warn.replace("holes are modelled", "holes in the exported files are modelled", 1))
        if p["finish"]:
            L.append("- Finish: %s." % p["finish"])
        if dwg:
            L.append("- Drawing: `%s`." % os.path.relpath(dwg, rel_dir))
        if p.get("notes"):
            L.append("- Notes:")
            L += ["  - %s" % t for t in p["notes"]]
        notes = [f for f in dfm.get("findings", []) if (f["severity"] in ("warn", "error")
                 or (proc == "turning" and any(k in f["message"] for k in ("secondary", "tailstock", "steady"))))
                 and f.get("code") != "print_comp"]                  # already a ⚠ line above
        if notes:
            L.append("- Manufacturability notes:")
            for f in notes:
                L.append("  - %s" % f["message"])
        if sm and sm.get("bends"):
            L += ["", "| Bend | Angle | Direction | Inside radius | Bend line (DXF, mm) | Length |",
                  "|---|---|---|---|---|---|"]
            for b in sm["bends"]:
                (x0, y0), (x1, y1) = b["line"]
                L.append("| B%d | %s° | %s | R%s | (%s, %s) → (%s, %s) | %s |" % (
                    b["n"], _g(b["angle"]), "UP" if b["up"] else "DOWN", _g(b["radius"]), _g(x0), _g(y0), _g(x1),
                    _g(y1), _g(b["length"])))
    if buy:
        L += ["", "## Purchased parts (buy)", "", "| Part | Qty | Spec | Size (mm) |", "|---|---|---|---|"]
        for r in buy:
            L.append("| %s | %s%s | %s | %s |" % (r["part"], r.get("qty", 1), " ⚠" if _short_qty(r) else "",
                                                  r.get("spec") or "—", _ref_size(r)))
        short = [r for r in buy if _short_qty(r)]
        if short:
            L.append("")
            L += ["- %s: %s." % (r["part"], _short_qty(r)) for r in short]
    if context:
        L += ["", "## Reference parts (used for checks — not supplied)", "", "| Part | Spec | Size (mm) |",
              "|---|---|---|"]
        for r in context:
            L.append("| %s | %s | %s |" % (r["part"], r.get("spec") or "—", _ref_size(r)))
    rows = []
    for p in parts:
        comp = p.get("hole_compensation") or 0
        for e in p.get("exports") or []:
            path = next((f for f in files_of[p["part"]] if os.path.basename(f) == e["file"]),
                        os.path.join(rel_dir, _safe(p["part"]), e["file"]))
            chk = e.get("check") or ""
            if e.get("oriented") and "oriented for printing" not in chk:
                chk += "; oriented for printing (model %s up)" % e["oriented"]
            if comp > 0 and (e.get("format") in ("step", "iges", "brep") or p["process"] not in GENERAL_TOL_PRINT):
                chk += "; holes include +%s print compensation" % _g(comp)
                if p["process"] not in GENERAL_TOL_PRINT:
                    chk += " ⚠ rebuild with compensation 0 before %s" % (
                        "cutting" if p["process"] in ("laser", "sheet") else "machining")
            rows.append("| `%s` | %s | %s | %s |" % (os.path.relpath(path, rel_dir), e.get("format", ""),
                                                    "✓" if e.get("ok") else "✗", chk.replace("|", "/")))
        dwg = _drawing_of(p)
        if dwg:
            txt, mark = _dims_status(p.get("drawing_dims"))
            if not os.path.isfile(dwg):
                txt, mark = "missing from the package", "✗"
            rows.append("| `%s` | pdf | %s | drawing sheet%s |" % (
                os.path.relpath(dwg, rel_dir), mark, "; " + txt.replace("|", "/") if txt else ""))
    if rows:
        L += ["", "## Files", "", "| File | Format | Verified | Check |", "|---|---|---|---|"] + rows
    if checks:
        L += ["", "## Verification", "", "| Check | Result | Value |", "|---|---|---|"]
        for c in checks:
            L.append("| %s | %s | %s |" % (c["id"], "✓" if c["ok"] else "✗", c.get("value", "")))
    cc = checks_changes or {}
    if any(cc.get(k) for k in ("added", "removed", "changed")):
        L += ["", "## Checks changed since %s" % cc.get("since", "the previous release"), ""]
        for k in ("added", "removed", "changed"):
            if cc.get(k):
                L.append("- %s: %s" % (k, ", ".join("`%s`" % i for i in cc[k])))
    L += ["", "## Build report", "", "```", supplier_report(report_text), "```", ""]
    with open(os.path.join(rel_dir, "README.md"), "w") as fh:
        fh.write("\n".join(L))


def zip_dir(src, zpath):
    base = os.path.basename(src.rstrip("/"))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src):
            for f in sorted(files):
                full = os.path.join(root, f)
                z.write(full, os.path.join(base, os.path.relpath(full, src)))
    return zpath
