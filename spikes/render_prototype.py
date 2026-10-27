"""Prototype headless renderer: numpy z-buffer + BREP edge overlay, PIL output."""
import math, time
import numpy as np
from PIL import Image, ImageDraw
import FreeCAD as App, Part

VIEWS = {"iso": (45, 35.264), "front": (0, 0), "top": (0, 90), "right": (90, 0), "hero": (-35, 25)}

def view_matrix(az, el):
    az, el = math.radians(az), math.radians(el)
    # camera looks along -d ; d = direction from target to eye
    d = np.array([math.sin(az)*math.cos(el), -math.cos(az)*math.cos(el), math.sin(el)])
    up = np.array([0, 0, 1.0]) if abs(el) < math.radians(89) else np.array([0, 1.0, 0])
    r = np.cross(up, d); r /= np.linalg.norm(r)
    u = np.cross(d, r)
    return np.stack([r, u, d])  # rows: screen x, screen y, depth (toward eye)

def render(parts, path, view="iso", size=(1000, 750), ss=2, section=None):
    """parts: list of (shape, rgb). section: (point, normal) keeps the half behind the plane."""
    t0 = time.time()
    W, H = size[0]*ss, size[1]*ss
    R = view_matrix(*VIEWS[view])
    light = np.array([0.35, 0.5, 0.8]); light /= np.linalg.norm(light)
    meshes, edges = [], []
    for shape, rgb in parts:
        cap = None
        if section:
            p, n = App.Vector(*section[0]), App.Vector(*section[1])
            big = shape.BoundBox.DiagonalLength * 4
            half = Part.makeBox(big, big, big)
            # orient half-space box so its -Z face lies on the plane, extending along +n
            rot = App.Rotation(App.Vector(0, 0, 1), n)
            half.Placement = App.Placement(p, rot).multiply(App.Placement(App.Vector(-big/2, -big/2, 0), App.Rotation()))
            cut = shape.cut(half)
            cap = shape.common(half).Faces
            cap = [f for f in cut.Faces if abs(f.Surface.Axis.dot(n)) > 0.999 and abs((f.CenterOfMass - p).dot(n)) < 1e-6] if hasattr(Part,'Plane') else []
            shape = cut
        for f in shape.Faces:
            pts, tris = f.tessellate(shape.BoundBox.DiagonalLength / 600)
            if not tris: continue
            col = (0.85, 0.2, 0.2) if cap and any(f.isSame(c) for c in cap) else rgb
            meshes.append((np.array([[q.x, q.y, q.z] for q in pts]), np.array(tris), col))
        for e in shape.Edges:
            try: pts = e.discretize(Deflection=shape.BoundBox.DiagonalLength/2000)
            except Exception: continue
            edges.append(np.array([[q.x, q.y, q.z] for q in pts]))
    allp = np.vstack([m[0] for m in meshes])
    sp = allp @ R.T
    lo, hi = sp[:, :2].min(0), sp[:, :2].max(0)
    scale = 0.88 * min(W / (hi[0]-lo[0] + 1e-9), H / (hi[1]-lo[1] + 1e-9))
    ctr = (lo + hi) / 2
    def proj(P):
        q = P @ R.T
        return np.column_stack([(q[:, 0]-ctr[0])*scale + W/2, H/2 - (q[:, 1]-ctr[1])*scale, q[:, 2]])
    zbuf = np.full((H, W), -np.inf); img = np.ones((H, W, 3))
    ntri = 0
    for P, T, col in meshes:
        S = proj(P); ntri += len(T)
        A, B, C = S[T[:, 0]], S[T[:, 1]], S[T[:, 2]]
        nrm = np.cross(P[T[:, 1]]-P[T[:, 0]], P[T[:, 2]]-P[T[:, 0]])
        nrm /= (np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12)
        nv = nrm @ R.T
        shade = 0.35 + 0.65*np.abs(nv @ light)
        for i in range(len(T)):
            a, b, c = A[i], B[i], C[i]
            x0, x1 = int(max(min(a[0], b[0], c[0]), 0)), int(min(max(a[0], b[0], c[0]) + 1, W))
            y0, y1 = int(max(min(a[1], b[1], c[1]), 0)), int(min(max(a[1], b[1], c[1]) + 1, H))
            if x0 >= x1 or y0 >= y1: continue
            den = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
            if abs(den) < 1e-12: continue
            xs, ys = np.meshgrid(np.arange(x0, x1) + .5, np.arange(y0, y1) + .5)
            w0 = ((b[1]-c[1])*(xs-c[0]) + (c[0]-b[0])*(ys-c[1])) / den
            w1 = ((c[1]-a[1])*(xs-c[0]) + (a[0]-c[0])*(ys-c[1])) / den
            w2 = 1 - w0 - w1
            m = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not m.any(): continue
            z = w0*a[2] + w1*b[2] + w2*c[2]
            zb = zbuf[y0:y1, x0:x1]
            m &= z > zb
            zb[m] = z[m]
            img[y0:y1, x0:x1][m] = np.array(col) * shade[i]
    im = Image.fromarray((img*255).astype(np.uint8))
    dr = ImageDraw.Draw(im)
    eps = 1e-3 * (hi - lo).max() * scale
    for E in edges:
        S = proj(E)
        for j in range(len(S)-1):
            p0, p1 = S[j], S[j+1]
            mid = (p0 + p1) / 2
            xi, yi = int(min(max(mid[0], 0), W-1)), int(min(max(mid[1], 0), H-1))
            vis = mid[2] >= zbuf[yi, xi] - 2*eps/scale*10 - 0.02*((hi-lo).max())
            dr.line([(p0[0], p0[1]), (p1[0], p1[1])], fill=(20, 20, 20) if vis else (170, 170, 170), width=ss*2 if vis else ss)
    im = im.resize(size, Image.LANCZOS)
    im.save(path)
    return {"tris": ntri, "secs": round(time.time()-t0, 2)}
