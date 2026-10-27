# SPDX-License-Identifier: Apache-2.0
"""Geometry helpers shared by the drawing worker (GUI) and the console worker.

same_signature needs no FreeCAD, so anything can import this module; the
helpers that look at shapes import analysis (and so FreeCAD) when called."""


def turned_axis(shape):
    """(axis, point, fraction) — the axis of the largest cylinder and the share
    of surface area that is a surface of revolution about it (or an end face).
    A lathe part with a cross hole or a flat still scores high."""
    import analysis as an
    cyl = [f for f in shape.Faces if f.Surface.__class__.__name__ == "Cylinder"]
    if not cyl:
        return None, None, 0.0
    big = max(cyl, key=lambda f: f.Area).Surface
    a, p = an._canon(big.Axis), an._axis_point(big)
    good = 0.0
    for f in shape.Faces:
        s = f.Surface
        k = s.__class__.__name__
        ok = False
        if k in ("Cylinder", "Cone"):
            ax = an._canon(s.Axis)
            pt = an._axis_point(s) if k == "Cylinder" else s.Apex
            ok = abs(abs(ax.dot(a)) - 1) < 1e-6 and ((pt - p) - a * (pt - p).dot(a)).Length < 1e-4
        elif k == "Plane":
            u0, u1, v0, v1 = f.ParameterRange
            ok = abs(abs(f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(a)) - 1) < 1e-6
        elif k in ("Toroid", "Sphere"):
            c = s.Center
            ok = ((c - p) - a * (c - p).dot(a)).Length < 1e-4
        if ok:
            good += f.Area
    return a, p, good / max(shape.Area, 1e-9)


def shape_signature(shape):
    """[volume, area, x, y, z size, face count] — enough to tell whether a part
    changed since its drawing was made (rounded so a save/reload compares equal)."""
    import analysis as an
    bb = an.tight_bb(shape)
    return [round(shape.Volume, 2), round(shape.Area, 2), round(bb.XLength, 3), round(bb.YLength, 3),
            round(bb.ZLength, 3), len(shape.Faces)]


# ---------------------------------------------------------------- pockets

POCKET_MAX = 8          # pockets, slots, steps and cut-outs dimensioned per view (more → a warning)


def _face_sample(f):
    """A point inside the trimmed face (its parameter middle can fall in a hole of the face)."""
    u0, u1, v0, v1 = f.ParameterRange
    for fu, fv in [(0.5, 0.5)] + [((i + 0.5) / 7, (j + 0.5) / 7) for i in range(7) for j in range(7)]:
        try:
            p = f.valueAt(u0 + (u1 - u0) * fu, v0 + (v1 - v0) * fv)
            if f.isInside(p, 1e-6, True):
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def _plane_normal(f):
    u0, u1, v0, v1 = f.ParameterRange
    return f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)


def _span(f, d):
    """(lo, hi) of p·d over a face (d a model axis: exact from its bounding box)."""
    bb = f.BoundBox
    lo = bb.XMin * d.x + bb.YMin * d.y + bb.ZMin * d.z
    hi = bb.XMax * d.x + bb.YMax * d.y + bb.ZMax * d.z
    return min(lo, hi), max(lo, hi)


def _vertical(f, d):
    """A wall seen edge-on along d: a plane parallel to d, or a cylinder / cone-free
    surface of revolution about an axis parallel to d."""
    s = f.Surface
    k = s.__class__.__name__
    if k == "Plane":
        return abs(_plane_normal(f).dot(d)) < 1e-6
    if k == "Cylinder":
        return abs(abs(s.Axis.dot(d)) - 1) < 1e-6
    return False


def _edge_extreme(e, w):
    """The point of edge e furthest along w (exact for lines and arcs)."""
    import Part
    k = e.Curve.__class__.__name__
    pts = [v.Point for v in e.Vertexes]
    if k == "Circle":
        c, a, r = e.Curve.Center, e.Curve.Axis, e.Curve.Radius
        q = w - a * w.dot(a)
        if q.Length > 1e-9:
            q.normalize()
            p = c + q * r
            if e.distToShape(Part.Vertex(p))[0] < 1e-6:
                pts.append(p)
    elif k not in ("Line", "LineSegment"):
        try:
            pts += e.discretize(64)
        except Exception:  # noqa: BLE001
            pass
    return max(pts, key=lambda p: p.dot(w)) if pts else None


def _below(shape, p, d, length):
    """How much material lies straight under the point p of a floor facing d (mm)."""
    import Part
    eps = 1e-4
    try:
        com = shape.common(Part.LineSegment(p - d * eps, p - d * (length + 1.0)).toShape())
    except Exception:  # noqa: BLE001
        return length
    start = Part.Vertex(p - d * eps)
    for e in com.Edges:
        if e.distToShape(start)[0] < 1e-5:
            return e.Length + eps
    return 0.0


def detect_pockets(shape, d, holes=None):
    """Pockets, slots, steps and through cut-outs seen looking along d (a model
    axis; the faces facing d are the ones the view shows): planar floors facing
    d below the part's top that a cutter reaches from d, with walls rising from
    at least one side of their outline — every side: a pocket; open at one end
    or at both opposite ends: a slot; else a step (slots and steps only when at
    most half as deep as the part is thick along d and at most three times as
    deep as the material under their floor, so the legs and ribs of a bracket
    are neither) — and closed outlines in the top faces whose walls run
    through the part (cut-outs). Hole
    floors (counterbores, flat-bottomed holes), the top face round a boss, and
    fillets or chamfers on outer edges are never pockets. Each record:
      kind      pocket | slot | step | cutout
      level     floor level (p·d); None for a cut-out
      rim       the level its walls rise to (p·d)
      depth     rim − level; None (THRU) for a cut-out
      box       {"X"|"Y"|"Z": (lo, hi)} of the region on the two model axes across d
      open      directions ('-X' …) in which the floor runs out of the part: a
                view from there sees the floor edge-on
      corners   [{r, edge: index in shape.Edges of the arc at the rim, center}] —
                the concave vertical corner radii
      ext       {'+X'|'-X'|…: model point}: the region's extreme points (on its
                outline, at the rim or the floor's edge)
      faces     every face index of it (floor and walls) — the same feature seen
                from two views has the same faces."""
    import Part
    import analysis as an
    from FreeCAD import Vector
    d = Vector(d)
    d.normalize()
    axes = [(n, Vector(*v)) for n, v in (("X", (1, 0, 0)), ("Y", (0, 1, 0)), ("Z", (0, 0, 1)))]
    across = [(n, a) for n, a in axes if abs(a.dot(d)) < 1e-6]
    if len(across) != 2:
        return []
    faces = list(shape.Faces)
    bb = an.tight_bb(shape)
    top = max(bb.XMin * d.x, bb.XMax * d.x) + max(bb.YMin * d.y, bb.YMax * d.y) + max(bb.ZMin * d.z, bb.ZMax * d.z)
    bot = min(bb.XMin * d.x, bb.XMax * d.x) + min(bb.YMin * d.y, bb.YMax * d.y) + min(bb.ZMin * d.z, bb.ZMax * d.z)
    eps = max(1e-4 * bb.DiagonalLength, 2e-3)
    holes = an.detect_holes(shape) if holes is None else holes
    hole_faces = set(int(n[4:]) - 1 for h in holes for n in h.get("faces") or [])
    by_hash = {}
    for i, f in enumerate(faces):
        by_hash.setdefault(f.hashCode(), []).append(i)

    def index(f):
        return next((i for i in by_hash.get(f.hashCode(), []) if faces[i].isSame(f)), None)

    def neighbours(e, skip):
        return [i for i in (index(g) for g in shape.ancestorsOfType(e, Part.Face)) if i is not None and i != skip]

    def edge_side(f, e):
        """(closed, outward direction) of a floor edge: material just beyond the
        edge and just below the floor means a wall (or floor fillet) rises there."""
        u = (e.FirstParameter + e.LastParameter) / 2
        m, t = e.valueAt(u), e.tangentAt(u)
        q = d.cross(t)
        if q.Length < 1e-9:
            return True, None
        q.normalize()
        if not f.isInside(m + q * eps, 1e-7, True):
            q = q * -1
        return shape.isInside(m - q * eps - d * eps, 1e-7, True), q * -1

    def facing(g):
        return g.Surface.__class__.__name__ == "Plane" and _plane_normal(g).dot(d) > 1 - 1e-6

    def surface(vert, wall_top):
        """The level of the surface the walls are cut into: the faces facing d at the
        walls' top edges, or beyond a chamfer or round on them (else the walls' top)."""
        lv = []
        for w in vert:
            for e in faces[w].Edges:
                lo, hi = _span(e, d)
                if abs(lo - wall_top) > 1e-6 or abs(hi - wall_top) > 1e-6:
                    continue
                for g in neighbours(e, w):
                    if facing(faces[g]):
                        lv.append(faces[g].CenterOfMass.dot(d))
                    elif not _vertical(faces[g], d):
                        for e2 in faces[g].Edges:
                            lv += [faces[h].CenterOfMass.dot(d) for h in neighbours(e2, g) if facing(faces[h])
                                   and faces[h].CenterOfMass.dot(d) > wall_top - 1e-6]
        return max(lv) if lv else wall_top

    def region(edges, wall_ids):
        """box and extreme points of the region outlined by edges; its corner arcs."""
        box, ext = {}, {}
        for n, a in across:
            for sgn, w in (("+", a), ("-", a * -1)):
                pts = [q for q in (_edge_extreme(e, w) for e in edges) if q is not None]
                ext[sgn + n] = max(pts, key=lambda q: q.dot(w))
            box[n] = (ext["-" + n].dot(a), ext["+" + n].dot(a))
        corners, seen = [], set()
        for wi in wall_ids:
            f = faces[wi]
            s = f.Surface
            if wi in hole_faces or s.__class__.__name__ != "Cylinder" or not an._is_concave(f) or \
                    not _vertical(f, d):
                continue
            c = an._axis_point(s)
            key = (round(c.x, 4), round(c.y, 4), round(c.z, 4), round(s.Radius, 4))
            if key in seen:
                continue
            seen.add(key)
            arcs = [e for e in f.Edges if e.Curve.__class__.__name__ == "Circle"]
            arc = max(arcs, key=lambda e: _span(e, d)[1]) if arcs else None      # the one at the rim
            ei = next((k for k, e2 in enumerate(shape.Edges) if arc is not None and e2.isSame(arc)), None)
            corners.append({"r": round(s.Radius, 4), "edge": ei, "center": arc.Curve.Center if arc else None})
        return box, ext, corners

    out, used = [], set()
    for fi, f in enumerate(faces):
        if f.Surface.__class__.__name__ != "Plane" or _plane_normal(f).dot(d) < 1 - 1e-6:
            continue
        level = f.CenterOfMass.dot(d)
        if level > top - 1e-6:
            continue
        p = _face_sample(f)
        if p is None:
            continue
        ray = Part.LineSegment(p + d * eps, p + d * (top - level + 1.0)).toShape()
        if ray.distToShape(shape)[0] < 1e-7:
            continue                                  # covered: no cutter reaches it from this side
        rising, opens = [], set()
        for e in f.OuterWire.Edges:
            closed, outward = edge_side(f, e)
            up = [gi for gi in neighbours(e, fi) if _span(faces[gi], d)[1] > level + 1e-6]
            if closed and up:                         # a wall (or floor fillet) rises from this edge
                rising += up
            elif outward is not None:                 # the floor runs out of the part (or down a bend)
                opens.add(an._axis_name(outward))
        if not rising:
            continue                                  # the top face round a boss, not a pocket
        walls = set()
        for gi in rising:                             # floor fillets / chamfers: the walls beyond them too
            walls.add(gi)
            if not _vertical(faces[gi], d):
                for e in faces[gi].Edges:
                    walls |= set(x for x in neighbours(e, gi) if x != fi and _vertical(faces[x], d)
                                 and _span(faces[x], d)[1] > level + 1e-6)
        if all(w in hole_faces for w in walls):
            continue                                  # a counterbore floor or a flat-bottomed hole
        vert = [w for w in walls if _vertical(faces[w], d) and w not in hole_faces]
        wall_top = max(_span(faces[w], d)[1] for w in (vert or walls))
        rim = surface(vert, wall_top)
        depth = rim - level
        opp = len(opens) == 2 and all(("-" + o[1:] if o[0] == "+" else "+" + o[1:]) in opens for o in opens)
        kind = "pocket" if not opens else ("slot" if len(opens) == 1 or opp else "step")
        if kind != "pocket" and (depth > 0.5 * (top - bot) + 1e-6 or depth > 3 * _below(shape, p, d, top - bot) + 1e-6):
            continue                                  # a leg or rib of the part, not a slot or step cut into it
        edges = list(f.OuterWire.Edges)
        for w in vert:
            edges += [e for e in faces[w].Edges if abs(_span(e, d)[0] - wall_top) < 1e-6
                      and abs(_span(e, d)[1] - wall_top) < 1e-6]
        box, ext, corners = region(edges, sorted(walls))
        used |= walls
        out.append({"kind": kind, "level": level, "rim": rim, "depth": depth, "box": box, "open": sorted(opens),
                    "corners": corners, "ext": ext, "faces": sorted(walls | {fi})})
    # through cut-outs: closed outlines in the top faces whose walls run down through the part
    for fi, f in enumerate(faces):
        if f.Surface.__class__.__name__ != "Plane" or _plane_normal(f).dot(d) < 1 - 1e-6 or \
                f.CenterOfMass.dot(d) < top - 1e-6:
            continue
        for w in f.Wires:
            if w.isSame(f.OuterWire):
                continue
            walls = set()
            for e in w.Edges:
                walls |= set(neighbours(e, fi))
            if not walls or walls & used or all(x in hole_faces for x in walls) or \
                    not all(_vertical(faces[x], d) for x in walls) or \
                    min(_span(faces[x], d)[0] for x in walls) > bot + 1e-6:
                continue                              # a hole, a pocket's opening, or no through walls
            box, ext, corners = region(list(w.Edges), sorted(walls))
            used |= walls
            out.append({"kind": "cutout", "level": None, "rim": top, "depth": None, "box": box, "open": [],
                        "corners": corners, "ext": ext, "faces": sorted(walls)})
    return out


def pocket_key(p):
    """What makes two pockets the same feature: kind (cut-out or not), size, depth and corner radii."""
    return (p["kind"] == "cutout", tuple(round(p["box"][n][1] - p["box"][n][0], 3) for n in sorted(p["box"])),
            None if p["depth"] is None else round(p["depth"], 3), tuple(sorted(c["r"] for c in p["corners"])))


# What a drawing shows beyond the shape signature (K1): hole and boss positions,
# turned fits and threads, the Hole features' specs and the chamfers. Each kind
# is hashed on its own so sheet_status can say WHAT changed.
CONTENT_KINDS = (("holes", "holes moved or resized"), ("bosses", "turned diameters or bosses moved or resized"),
                 ("fits_threads", "fit/thread changed"),
                 ("hole_specs", "hole spec changed (thread, fit, depth, counterbore/countersink or compensation)"),
                 ("chamfers", "chamfers changed"), ("pockets", "pockets, slots or steps moved or resized"),
                 ("sheet", "sheet-metal data changed (thickness, bends or K-factor)"))
POCKET_DIRS = (("+X", (1, 0, 0)), ("-X", (-1, 0, 0)), ("+Y", (0, 1, 0)), ("-Y", (0, -1, 0)), ("+Z", (0, 0, 1)),
               ("-Z", (0, 0, -1)))
HOLE_SPEC_PROPS = ("Diameter", "Threaded", "ThreadSize", "ThreadClass", "ThreadFit", "Depth", "DepthType",
                   "HoleCutType", "HoleCutDiameter", "HoleCutDepth", "HoleCutCountersinkAngle", "FcFit",
                   "FcHoleComp", "FcSpec")


def _r(x, nd=3):
    v = round(float(x), nd)
    return 0.0 if v == 0 else v                 # never -0.0: it would hash differently


def _plain(v):
    """A property value as plain JSON (Quantity → its value)."""
    v = getattr(v, "Value", v)
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return _r(v, 4)
    return str(v)


def content_parts(part, shape=None, holes=None):
    """{kind: sha1} of what the part's drawing shows beyond shape_signature:
    detected holes and bosses (axis, centre relative to the part's tight
    bounding box rounded to 1e-3 — a part that only moves keeps its drawing —,
    diameters, through, depth), FcSteps / FcFits / FcThreads, each
    PartDesign::Hole's spec and each chamfer's size and type; and, only when
    the part has them (so other parts keep their old hashes), its pockets /
    slots / steps / cut-outs from each side and its FcSheet data."""
    import hashlib
    import json
    import analysis as an
    shape = an.global_shape(part) if shape is None else shape
    bb = an.tight_bb(shape)

    def feats(recs):
        return sorted([h["axis_name"], [_r(h["at"].x - bb.XMin), _r(h["at"].y - bb.YMin), _r(h["at"].z - bb.ZMin)],
                       [_r(d, 4) for d in h["diameters"]], bool(h.get("through")), _r(h["depth"])] for h in recs)
    meta = {}
    for k in ("FcSteps", "FcFits", "FcThreads"):
        try:
            meta[k] = json.loads(getattr(part, k, "") or "null")
        except ValueError:
            meta[k] = str(getattr(part, k, ""))
    group = list(part.Group) if part.TypeId == "PartDesign::Body" else []
    specs, chamfers = [], []
    for f in group:
        if f.TypeId == "PartDesign::Hole":
            specs.append({k: _plain(getattr(f, k, None)) for k in HOLE_SPEC_PROPS})
        elif f.TypeId == "PartDesign::Chamfer":
            chamfers.append({k: _plain(getattr(f, k, None)) for k in ("ChamferType", "Size", "Size2", "Angle")})
    holes = an.detect_holes(shape) if holes is None else holes
    data = {"holes": feats(holes), "bosses": feats(an.detect_bosses(shape)),
            "fits_threads": meta, "hole_specs": specs, "chamfers": chamfers}
    from FreeCAD import Vector
    lo = {"X": bb.XMin, "Y": bb.YMin, "Z": bb.ZMin}
    pk = []
    for name, dv in POCKET_DIRS:
        for p in detect_pockets(shape, Vector(*dv), holes):
            pk.append([name, p["kind"], [[a, _r(p["box"][a][0] - lo[a]), _r(p["box"][a][1] - lo[a])]
                                         for a in sorted(p["box"])],
                       None if p["depth"] is None else _r(p["depth"]), sorted(_r(c["r"]) for c in p["corners"])])
    if pk:
        data["pockets"] = sorted(pk, key=lambda x: json.dumps(x))
    sheet = getattr(part, "FcSheet", "") or ""
    if sheet:
        try:
            data["sheet"] = json.loads(sheet)
        except ValueError:
            data["sheet"] = sheet
    return {k: hashlib.sha1(json.dumps(v, sort_keys=True).encode("utf-8")).hexdigest() for k, v in data.items()}


def content_hash(parts):
    """One sha1 over content_parts."""
    import hashlib
    import json
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def content_signature(part, shape=None, holes=None):
    """sha1 hex of content_parts(part): what a drawing shows beyond the shape signature."""
    return content_hash(content_parts(part, shape, holes))


def content_change(old, new):
    """What differs between two content_parts dicts, in words ('' when the old one is unknown)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return ""
    return ", ".join(words for k, words in CONTENT_KINDS if old.get(k) != new.get(k))


def same_signature(a, b):
    """Signatures equal: floats within a relative 1e-6, face count exact."""
    if not a or not b or len(a) != len(b):
        return False
    for x, y in zip(a[:-1], b[:-1]):
        if abs(float(x) - float(y)) > 1e-6 * max(1.0, abs(float(x)), abs(float(y))):
            return False
    return int(a[-1]) == int(b[-1])


def signature_change(old, new):
    """What differs between two signatures, in words."""
    names = ("volume", "area", "x size", "y size", "z size", "faces")
    units = (" mm³", " mm²", " mm", " mm", " mm", "")
    out = []
    for n, u, x, y in zip(names, units, old or [], new or []):
        if n == "faces":
            if int(x) != int(y):
                out.append("%s %d → %d" % (n, int(x), int(y)))
        elif abs(float(x) - float(y)) > 1e-6 * max(1.0, abs(float(x)), abs(float(y))):
            out.append("%s %g → %g%s" % (n, x, y, u))
    return ", ".join(out)
