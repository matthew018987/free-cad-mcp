# SPDX-License-Identifier: Apache-2.0
"""
Headless renderer for FreeCAD shapes — runs inside the FreeCAD worker.

FreeCAD cannot render its own 3D view without an exposed window on macOS (the
offscreen Qt platform segfaults and Coin's offscreen renderer has no GL
context), so this draws shapes itself: faces are tessellated and rasterised
into a numpy z-buffer, and the exact BREP edges are drawn over the top with a
visibility test against that buffer. numpy and PIL ship with FreeCAD.

Rasterisation is vectorised: triangles are bucketed by screen size and each
bucket is sampled on a fixed stencil (8, 16, 32 or 64 px) in one numpy batch;
only triangles larger than 64 px go through a per-triangle loop.
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import FreeCAD as App
import MeshPart
import Part

# azimuth, elevation in degrees. The eye sits along
# d = (sin az cos el, -cos az cos el, sin el), so "front" looks toward +Y.
VIEWS = {
    "front":      (0, 0),
    "back":       (180, 0),
    "right":      (90, 0),
    "left":       (-90, 0),
    "top":        (0, 90),
    "bottom":     (0, -90),
    "iso":        (45, 35.264),
    "iso-back":   (225, 35.264),
    "iso-left":   (-45, 35.264),
    "iso-bottom": (45, -35.264),
}
ORTHO_LABELS = {"front", "back", "right", "left", "top", "bottom"}

PALETTE = [
    (0.55, 0.68, 0.86), (0.92, 0.72, 0.38), (0.55, 0.80, 0.58), (0.86, 0.55, 0.55),
    (0.70, 0.60, 0.85), (0.45, 0.78, 0.80), (0.85, 0.75, 0.55), (0.65, 0.65, 0.65),
]
REF_COLOUR = (0.78, 0.78, 0.74)
HIGHLIGHT = (0.95, 0.35, 0.15)
DIM = (0.80, 0.80, 0.80)
BG = (255, 255, 255)
STENCILS = (8, 16, 32, 64)
CAP_ID = 1 << 20          # object ids at or above this are section caps


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow < 10.1
        return ImageFont.load_default()


def view_basis(az, el):
    """Rows: screen x, screen y, depth (toward the eye)."""
    a, e = math.radians(az), math.radians(el)
    d = np.array([math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)])
    up = np.array([0.0, 0.0, 1.0]) if abs(el) < 89.0 else np.array([-math.sin(a), math.cos(a), 0.0])
    r = np.cross(up, d)
    r /= np.linalg.norm(r)
    u = np.cross(d, r)
    return np.stack([r, u, d])


def parse_view(v):
    if isinstance(v, dict):
        return "az%g el%g" % (v.get("az", 0), v.get("el", 0)), (float(v.get("az", 0)), float(v.get("el", 0)))
    key = str(v).lower()
    if key not in VIEWS:
        raise ValueError("unknown view %r (choose: %s, or {az, el})" % (v, ", ".join(VIEWS)))
    return key, VIEWS[key]


def _half_space(shape, point, normal):
    """A box covering everything on the +normal side of the plane."""
    big = max(shape.BoundBox.DiagonalLength, 1.0) * 4
    box = Part.makeBox(big, big, big, App.Vector(-big / 2, -big / 2, 0))
    box.Placement = App.Placement(App.Vector(*point), App.Rotation(App.Vector(0, 0, 1), App.Vector(*normal)))
    return box


def cut_for_section(shape, point, normal):
    """Remove material on the +normal side; return (kept shape, cap faces)."""
    kept = shape.cut(_half_space(shape, point, normal))
    n = App.Vector(*normal)
    p = App.Vector(*point)
    caps = []
    for f in kept.Faces:
        if f.Surface.__class__.__name__ != "Plane":
            continue
        try:
            fn = f.normalAt(*f.ParameterRange[:1], f.ParameterRange[2])
        except Exception:  # noqa: BLE001
            continue
        if abs(abs(fn.dot(n)) - 1) < 1e-6 and abs((f.CenterOfMass - p).dot(n)) < 1e-5:
            caps.append(f)
    return kept, caps


class Item:
    """One thing to draw: a shape, its colour, and how to draw it."""

    def __init__(self, label, shape, colour, ghost=False):
        self.label = label
        self.shape = shape
        self.colour = colour
        self.ghost = ghost        # drawn faint (context objects when highlighting)
        self.cap_faces = []


def _tessellate(item, tol):
    """Return [(points, triangles, colours, is_cap)] — cut faces kept separate so
    they can be hatched."""
    groups = {False: [[], [], []], True: [[], [], []]}
    base = {False: 0, True: 0}
    caps = item.cap_faces
    for f in item.shape.Faces:
        try:
            # MeshPart honours an angular limit; Part's tessellate() reuses any
            # finer triangulation already cached on the shape (often 5x denser).
            p, t = MeshPart.meshFromShape(Shape=f, LinearDeflection=tol,
                                          AngularDeflection=0.35, Relative=False).Topology
        except Exception:  # noqa: BLE001
            continue
        if not t:
            continue
        cap = bool(caps) and any(f.isSame(c) or _same_plane_face(f, c) for c in caps)
        g = groups[cap]
        g[0].append(np.array([[q.x, q.y, q.z] for q in p], dtype=float))
        g[1].append(np.array(t, dtype=np.int64) + base[cap])
        g[2].append(np.tile(item.colour, (len(t), 1)))
        base[cap] += len(p)
    return [(np.vstack(g[0]), np.vstack(g[1]), np.vstack(g[2]), cap)
            for cap, g in groups.items() if g[0]]


def _seams(shape):
    """Hashes of seam edges — not drawn. In a closed solid every real edge
    borders two faces; a seam borders one face on both sides."""
    out = set()
    if not shape.Solids:
        return out
    for e in shape.Edges:
        try:
            if len(shape.ancestorsOfType(e, Part.Face)) == 1:
                out.add(e.hashCode())
        except Exception:  # noqa: BLE001
            pass
    return out


def _cylinder_silhouettes(shape, d, samples=12):
    """Silhouette generator lines of cylindrical faces seen along view direction d.

    Curved faces have no BREP edge where they turn away from the viewer; drawing
    style needs these (e.g. the dashed sides of a hidden hole)."""
    out = []
    dv = App.Vector(*d)
    for f in shape.Faces:
        srf = f.Surface
        if srf.__class__.__name__ != "Cylinder":
            continue
        a = srf.Axis
        if abs(a.dot(dv)) > 0.999:
            continue
        side = a.cross(dv)
        side.normalize()
        v0, v1 = f.ParameterRange[2:]
        for sgn in (1, -1):
            base = srf.Center + side * (srf.Radius * sgn)
            pts = [base + a * (v0 + (v1 - v0) * k / (samples - 1)) for k in range(samples)]
            inside = [f.isInside(q, 1e-4, True) for q in pts]
            run = []
            for q, ok in zip(pts + [None], inside + [False]):
                if ok:
                    run.append([q.x, q.y, q.z])
                elif len(run) >= 2:
                    out.append(np.array(run))
                    run = []
                else:
                    run = []
    return out


def _polyline(dr, pts, fill, width, dash=None):
    """Draw a polyline, optionally dashed with (on, off) lengths in pixels."""
    if not dash:
        dr.line([tuple(p) for p in pts], fill=fill, width=width)
        return
    on, off = dash
    period, acc = on + off, 0.0
    for a, b in zip(pts[:-1], pts[1:]):
        seg = float(np.hypot(*(b - a)))
        t = 0.0
        while t < seg:
            phase = (acc + t) % period
            step = min(seg - t, (on - phase) if phase < on else (period - phase))
            if phase < on:
                p0, p1 = a + (b - a) * (t / seg), a + (b - a) * ((t + step) / seg)
                dr.line([tuple(p0), tuple(p1)], fill=fill, width=width)
            t += max(step, 1e-6)
        acc += seg


def _same_plane_face(a, b):
    return abs(a.Area - b.Area) < 1e-6 and (a.CenterOfMass - b.CenterOfMass).Length < 1e-6


class Raster:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.z = np.full(h * w, -np.inf)
        self.rgb = np.empty((h * w, 3))
        self.rgb[:] = np.array(BG) / 255.0
        self.oid = np.full(h * w, -1, dtype=np.int32)
        self.cur = 0

    def draw(self, S, T, C, shade, oid=0):
        """S: screen points (n,3); T: triangles (m,3); C: colours (m,3); shade (m,)."""
        if len(T) == 0:
            return
        self.cur = oid
        A, B, Cc = S[T[:, 0]], S[T[:, 1]], S[T[:, 2]]
        xmin = np.floor(np.minimum(np.minimum(A[:, 0], B[:, 0]), Cc[:, 0]))
        ymin = np.floor(np.minimum(np.minimum(A[:, 1], B[:, 1]), Cc[:, 1]))
        xmax = np.maximum(np.maximum(A[:, 0], B[:, 0]), Cc[:, 0])
        ymax = np.maximum(np.maximum(A[:, 1], B[:, 1]), Cc[:, 1])
        on = (xmax >= 0) & (ymax >= 0) & (xmin < self.w) & (ymin < self.h)
        den = (B[:, 1] - Cc[:, 1]) * (A[:, 0] - Cc[:, 0]) + (Cc[:, 0] - B[:, 0]) * (A[:, 1] - Cc[:, 1])
        on &= np.abs(den) > 1e-12
        col = C * shade[:, None]
        span = np.maximum(xmax - xmin, ymax - ymin)
        done = ~on
        # Batch triangles by bbox size: each batch samples a fixed stencil, so
        # long thin triangles (common between holes) stay vectorised too.
        for st in STENCILS:
            pick = ~done & (span < st)
            done |= pick
            idx = np.nonzero(pick)[0]
            chunk = max(1, (1 << 21) // (st * st))
            for s in range(0, len(idx), chunk):
                self._batch(idx[s:s + chunk], A, B, Cc, den, xmin, ymin, col, st)
        for i in np.nonzero(~done)[0]:
            self._one(A[i], B[i], Cc[i], den[i], col[i])

    def _resolve(self, pix, z, colour):
        if len(pix) == 0:
            return
        order = np.lexsort((-z, pix))
        pix, z, colour = pix[order], z[order], colour[order]
        first = np.ones(len(pix), bool)
        first[1:] = pix[1:] != pix[:-1]
        pix, z, colour = pix[first], z[first], colour[first]
        win = z > self.z[pix]
        self.z[pix[win]] = z[win]
        self.rgb[pix[win]] = colour[win]
        self.oid[pix[win]] = self.cur

    def outlines(self, jump):
        """Pixels on silhouettes: object changes or depth jumps larger than `jump`.

        Curved surfaces have no BREP edge at their silhouette, so these come
        from the buffers rather than from the model."""
        z = self.z.reshape(self.h, self.w)
        o = self.oid.reshape(self.h, self.w)
        zf = np.where(np.isfinite(z), z, -1e30)
        m = np.zeros((self.h, self.w), bool)
        for a, b in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                     ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
            d = (o[a] != o[b]) | (np.abs(zf[a] - zf[b]) > jump)
            m[a] |= d
            m[b] |= d
        return m

    def _batch(self, idx, A, B, C, den, xmin, ymin, col, st):
        g = np.arange(st) + 0.5
        gx, gy = np.meshgrid(g, g)
        xs = xmin[idx, None] + gx.ravel()[None, :]
        ys = ymin[idx, None] + gy.ravel()[None, :]
        a, b, c, d = A[idx], B[idx], C[idx], den[idx][:, None]
        w0 = ((b[:, 1:2] - c[:, 1:2]) * (xs - c[:, 0:1]) + (c[:, 0:1] - b[:, 0:1]) * (ys - c[:, 1:2])) / d
        w1 = ((c[:, 1:2] - a[:, 1:2]) * (xs - c[:, 0:1]) + (a[:, 0:1] - c[:, 0:1]) * (ys - c[:, 1:2])) / d
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9) & (xs >= 0) & (ys >= 0) & (xs < self.w) & (ys < self.h)
        if not m.any():
            return
        z = w0 * a[:, 2:3] + w1 * b[:, 2:3] + w2 * c[:, 2:3]
        tri = np.broadcast_to(np.arange(len(idx))[:, None], m.shape)[m]
        pix = ys[m].astype(np.int64) * self.w + xs[m].astype(np.int64)
        self._resolve(pix, z[m], col[idx][tri])

    def _one(self, a, b, c, den, colour):
        x0, x1 = int(max(min(a[0], b[0], c[0]), 0)), int(min(max(a[0], b[0], c[0]) + 1, self.w))
        y0, y1 = int(max(min(a[1], b[1], c[1]), 0)), int(min(max(a[1], b[1], c[1]) + 1, self.h))
        if x0 >= x1 or y0 >= y1:
            return
        xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        w0 = ((b[1] - c[1]) * (xs - c[0]) + (c[0] - b[0]) * (ys - c[1])) / den
        w1 = ((c[1] - a[1]) * (xs - c[0]) + (a[0] - c[0]) * (ys - c[1])) / den
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        if not m.any():
            return
        z = (w0 * a[2] + w1 * b[2] + w2 * c[2])[m]
        pix = ys[m].astype(np.int64) * self.w + xs[m].astype(np.int64)
        self._resolve(pix, z, np.tile(colour, (len(pix), 1)))


def _nice(x):
    """Largest 1/2/5 x 10^n not above x."""
    if x <= 0:
        return 1.0
    e = math.floor(math.log10(x))
    for m in (5, 2, 1):
        if m * 10 ** e <= x:
            return m * 10 ** e
    return 10 ** e


def render(items, path, views=("iso",), size=(1024, 768), style="shaded", hidden=True,
           labels=True, section=None, ss=2):
    """Render items to a PNG grid, one cell per view, all at one common scale.

    style: shaded | xray (faces faint, all edges solid) | drawing (white faces,
    black visible edges, dashed hidden edges).
    section: (point, normal) — material on the +normal side is removed and the
    cut faces are shown darker.
    Returns a dict with per-view scale (px/mm at output resolution).
    """
    views = [parse_view(v) for v in views] or [parse_view("iso")]
    n = len(views)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = int(math.ceil(n / float(cols)))
    W, H = int(size[0]), int(size[1])
    cw, ch = W // cols, H // rows

    if section:
        for it in items:
            try:
                it.shape, it.cap_faces = cut_for_section(it.shape, section[0], section[1])
            except Exception:  # noqa: BLE001
                pass
    items = [it for it in items if it.shape is not None and not it.shape.isNull() and it.shape.Faces]
    if not items:
        raise ValueError("nothing to render: no faces in the selected objects")

    bb = App.BoundBox()
    for it in items:
        bb.add(it.shape.BoundBox)
    diag = max(bb.DiagonalLength, 1e-6)
    corners = np.array([[x, y, z] for x in (bb.XMin, bb.XMax) for y in (bb.YMin, bb.YMax)
                        for z in (bb.ZMin, bb.ZMax)])

    # one scale for every cell: fit the largest projected extent
    pad = 0.86
    scale = float("inf")
    for _, (az, el) in views:
        q = corners @ view_basis(az, el).T
        ext = q[:, :2].max(0) - q[:, :2].min(0)
        scale = min(scale, pad * cw / max(ext[0], 1e-9), pad * (ch - 22) / max(ext[1], 1e-9))
    tol = min(max(0.5 / scale, diag / 4000.0), diag / 300.0)

    meshes = []
    for it in items:
        for m in _tessellate(it, tol):
            meshes.append((it, m))
    edges = []
    for it in items:
        seams = _seams(it.shape)
        for e in it.shape.Edges:
            if e.hashCode() in seams:
                continue
            try:
                pts = e.discretize(Deflection=tol / 2)
            except Exception:  # noqa: BLE001
                continue
            if len(pts) >= 2:
                edges.append((it, np.array([[q.x, q.y, q.z] for q in pts])))

    sheet = Image.new("RGB", (W, H), BG)
    font = _font(14)
    small = _font(12)
    light = np.array([0.35, 0.45, 0.82])
    light /= np.linalg.norm(light)
    eps = 3 * tol + 1e-4 * diag
    info = []

    for k, (name, (az, el)) in enumerate(views):
        R = view_basis(az, el)
        cx = (corners @ R.T)[:, :2]
        ctr = (cx.max(0) + cx.min(0)) / 2
        sw, sh = cw * ss, ch * ss
        s = scale * ss

        def proj(P, R=R, ctr=ctr, s=s, sw=sw, sh=sh):
            q = P @ R.T
            return np.column_stack([(q[:, 0] - ctr[0]) * s + sw / 2,
                                    sh / 2 + 11 * ss - (q[:, 1] - ctr[1]) * s, q[:, 2]])

        ras = Raster(sw, sh)
        # with something highlighted, context parts are wireframe only so they
        # can never hide (or z-fight with) the highlighted geometry
        wire_ghosts = any(not it.ghost for it in items) and any(it.ghost for it in items)
        for oid, (it, (P, T, C, cap)) in enumerate(meshes):
            if wire_ghosts and it.ghost:
                continue
            S = proj(P)
            nrm = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
            nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
            ndl = np.abs((nrm @ R.T) @ light)
            if style == "drawing":
                shade = np.ones(len(T))
                cols_ = np.ones_like(C)
            elif style == "xray" or it.ghost:
                shade = 0.92 + 0.08 * ndl
                cols_ = 0.25 * C + 0.75
            else:
                shade = 0.38 + 0.62 * ndl
                cols_ = C
            ras.draw(S, T, cols_, shade, oid + (CAP_ID if cap else 0))
        rgb = ras.rgb.reshape(sh, sw, 3)
        capm = ras.oid.reshape(sh, sw) >= CAP_ID
        if capm.any():                     # 45-degree section hatching
            yy, xx = np.nonzero(capm)
            hatch = ((xx + yy) // (3 * ss)) % 3 == 0
            rgb[yy[hatch], xx[hatch]] *= 0.45
        sil = ras.outlines(max(8 * eps, 0.02 * diag))
        rgb[sil] = (0.1, 0.1, 0.1) if style != "xray" else (0.35, 0.35, 0.35)
        img = Image.fromarray((rgb * 255).astype(np.uint8))
        dr = ImageDraw.Draw(img)
        zb = ras.z.reshape(sh, sw)
        view_edges = list(edges)
        if style in ("drawing", "xray"):
            for it in items:
                view_edges += [(it, E) for E in _cylinder_silhouettes(it.shape, R[2])]
        for it, E in view_edges:
            Q = proj(E)
            mid = (Q[1:] + Q[:-1]) / 2
            xi = np.clip(mid[:, 0].astype(int), 0, sw - 1)
            yi = np.clip(mid[:, 1].astype(int), 0, sh - 1)
            vis = mid[:, 2] >= zb[yi, xi] - eps
            # split the polyline into runs of equal visibility
            start = 0
            for j in range(1, len(vis) + 1):
                if j < len(vis) and vis[j] == vis[start]:
                    continue
                run = Q[start:j + 1, :2]
                if vis[start]:
                    if it.ghost and style != "drawing":
                        _polyline(dr, run, (150, 150, 150), ss)
                    else:
                        _polyline(dr, run, (25, 25, 25), int(1.5 * ss))
                elif style == "xray":
                    _polyline(dr, run, (90, 90, 90), ss)
                elif hidden and not it.ghost:
                    if style == "drawing":
                        _polyline(dr, run, (60, 60, 60), ss, dash=(6 * ss, 4 * ss))
                    else:
                        _polyline(dr, run, (185, 185, 185), ss)
                start = j
        img = img.resize((cw, ch), Image.LANCZOS)
        dr = ImageDraw.Draw(img)

        # view label, scale bar, axis triad
        dr.text((8, 4), name, fill=(40, 40, 40), font=font)
        bar_mm = _nice(0.22 * cw / scale)
        bar_px = bar_mm * scale
        y = ch - 14
        dr.line([(10, y), (10 + bar_px, y)], fill=(40, 40, 40), width=2)
        for x in (10, 10 + bar_px):
            dr.line([(x, y - 4), (x, y + 4)], fill=(40, 40, 40), width=2)
        dr.text((14 + bar_px, y - 8), "%g mm" % bar_mm, fill=(40, 40, 40), font=small)
        ox, oy, L = cw - 34, ch - 30, 18
        for vec, colr, lab in ((np.array([1, 0, 0]), (200, 40, 40), "X"),
                               (np.array([0, 1, 0]), (40, 150, 40), "Y"),
                               (np.array([0, 0, 1]), (40, 70, 210), "Z")):
            v = R @ vec
            if abs(v[0]) < 1e-6 and abs(v[1]) < 1e-6:
                dr.ellipse([ox - 4, oy - 4, ox + 4, oy + 4], outline=colr, width=1)
                if v[2] > 0:       # toward the viewer
                    dr.ellipse([ox - 1, oy - 1, ox + 1, oy + 1], fill=colr)
                else:
                    dr.line([(ox - 3, oy - 3), (ox + 3, oy + 3)], fill=colr)
                    dr.line([(ox - 3, oy + 3), (ox + 3, oy - 3)], fill=colr)
                dr.text((ox - 16, oy - 7), lab, fill=colr, font=small)
                continue
            ex, ey = ox + v[0] * L, oy - v[1] * L
            dr.line([(ox, oy), (ex, ey)], fill=colr, width=2)
            dr.text((ex + 2, ey - 7), lab, fill=colr, font=small)
        if labels and len(items) > 1:
            taken = []
            for it in items:
                c = it.shape.BoundBox.Center
                q = proj(np.array([[c.x, c.y, c.z]]))[0] / ss
                x, y = q[0] + 4, q[1] - 7
                w = dr.textlength(it.label, font=small)
                while any(abs(y - ty) < 14 and x < tx + tw and tx < x + w for tx, ty, tw in taken):
                    y += 14
                taken.append((x, y, w))
                dr.text((x, y), it.label, fill=(10, 10, 10), font=small,
                        stroke_width=2, stroke_fill=(255, 255, 255))
        sheet.paste(img, ((k % cols) * cw, (k // cols) * ch))
        info.append({"view": name, "px_per_mm": round(scale, 4)})
    if cols > 1 or rows > 1:
        d = ImageDraw.Draw(sheet)
        for c in range(1, cols):
            d.line([(c * cw, 0), (c * cw, H)], fill=(215, 215, 215))
        for r in range(1, rows):
            d.line([(0, r * ch), (W, r * ch)], fill=(215, 215, 215))
    sheet.save(path)
    return {"path": path, "views": info, "tolerance_mm": round(tol, 4),
            "triangles": int(sum(len(m[1]) for _, m in meshes))}


def pick_colours(objs, highlight=None):
    """Assign colours: reference parts grey, others from the palette, highlights on top."""
    out = []
    i = 0
    for label, shape, is_ref in objs:
        if highlight:
            hit = label in highlight
            colour = HIGHLIGHT if hit else DIM
            out.append(Item(label, shape, colour, ghost=not hit))
            continue
        if is_ref:
            out.append(Item(label, shape, REF_COLOUR))
        else:
            out.append(Item(label, shape, PALETTE[i % len(PALETTE)]))
            i += 1
    return out
