# SPDX-License-Identifier: Apache-2.0
"""FEA inside the FreeCAD worker: linear static, natural frequencies and linear
buckling of one part, or of several parts bonded together.

Supports, loads and bonded faces are faces or holes chosen with the usual
geometry references. gmsh meshes the parts (second-order tetrahedra, the same
mesh on every run) and labels every mesh face and element with the solid face
and solid it lies on, so the sets are exact — also on two coincident faces of
different parts. This module writes the CalculiX input itself (each part its
own material, bonded face pairs tied with *TIE, a bearing load as the pressure
of a pin on the side of the hole it presses against), runs ccx on all cores
and reads its results with numpy.

The result is evidence, not a picture. static: the stress each part is judged
on and where (von Mises, or for a brittle material the largest principal
stress; also away from the supports, where rigid fixtures inflate it), the
principal stresses, the largest displacement and where, the safety factor (per
part), the support reactions against the loads (equilibrium). frequency: the
first natural frequencies and the mode shapes (loads are not used). buckling:
the factors by which the loads must grow to buckle the part, and the mode
shape. A second mesh, finer or for a large first mesh coarser, shows whether
the numbers have converged. Every result names the method version and the
programs that produced it, and is stale once those change.

Limits: linear elastic, small displacements, isotropic material, rigid
supports; bonded faces are glued (no contact, sliding or bolt preload)."""
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time

import FreeCAD as App
import Part
from FreeCAD import Vector

import analysis as an

METHOD = 2               # version of the analysis method: raise it when the same case on the same geometry would
                         # give different numbers, so that results from an older method go stale (1: no stamp)
REFINE = 1.5             # finer mesh = size / REFINE
MAX_ELEMENTS = 200000    # estimated elements: coarsen the first mesh above this (time, memory)
AUTO_REFINE = 50000      # refine by default only while the first mesh has at most this many nodes
HARD_MAX = 300000        # estimated elements: never solve a refined mesh above this, even when forced
                         # (CalculiX's solver needs ≈9 GB for 320 000 nodes and grows faster than the mesh)
GRAVITY = 9810.0         # mm/s²
CCX_TIMEOUT = 1800       # s per CalculiX run
ANALYSES = ("static", "frequency", "buckling")
DEFAULT_MODES = {"frequency": 6, "buckling": 3}
LARGE_DEFLECTION = 0.05  # of the parts' longest dimension: above it a linear (small-deflection) result is approximate
TITLES = {"static": "linear static", "frequency": "natural frequencies", "buckling": "linear buckling"}
MAT_KEYS = ("name", "E", "nu", "yield", "density", "brittle", "source", "note")     # of a material, kept in a result
MIN_JACOBIAN = 0.02      # scaled Jacobian below which a curved element counts as folded over (and is straightened)
POOR_JACOBIAN = 0.2      # … and below which it is reported as badly distorted
C3D10_FACES = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))   # CalculiX tet faces S1..S4 (corner indices)
C3D10_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))  # corners of the mid-side nodes 5..10
SMESH_TO_CCX = (1, 0, 2, 3, 4, 6, 5, 8, 7, 9)              # FreeCAD tet10 node order → CalculiX C3D10


class FeaError(Exception):
    pass


class CcxTimeout(FeaError):
    pass


# ------------------------------------------------------------------ the case

def analysis_kind(case):
    k = str(case.get("analysis") or "static").strip().lower()
    if k not in ANALYSES:
        raise FeaError("analysis must be \"static\", \"frequency\" or \"buckling\" — got %r" % case.get("analysis"))
    return k


def mode_count(case):
    """Modes to compute: 0 for static, else modes (default 6 for frequency, 3 for buckling)."""
    kind = analysis_kind(case)
    if kind == "static":
        return 0
    m = case.get("modes")
    try:
        m = DEFAULT_MODES[kind] if m is None else int(m)
    except (TypeError, ValueError):
        raise FeaError("modes must be a whole number (default %d for %s)" % (DEFAULT_MODES[kind], kind))
    if not 1 <= m <= 30:
        raise FeaError("modes must be between 1 and 30")
    return m


def case_parts(case):
    """Part labels of a case: parts=[…] or part=…."""
    ps = case.get("parts")
    if ps:
        return [str(p) for p in ([ps] if isinstance(ps, str) else ps)]
    return [str(case["part"])] if case.get("part") else []


def materials(project, case, labels):
    """{label: mechanical properties} for the parts of a case: the case's material (a name or
    {E, nu, yield, density (g/cm³)}) for every part, else each part's own from project.json."""
    import standards
    ov = case.get("material")
    out = {}
    for lb in labels:
        m = standards.mech_info(project, lb, ov)
        if isinstance(ov, dict) and ov.get("density") is not None:
            m["density"] = float(ov["density"])
        out[lb] = m
    return out


def case_hash(case, mats):
    """Identity of a load case and the material(s) it was solved with — results are stale when
    this (or the model) changes. mats: the material of a one-part case, or {label: material}."""
    keep = {k: case.get(k) for k in ("part", "fixed", "loads", "gravity", "mesh_size", "refine")}
    for k in ("parts", "bonded", "modes", "support_margin"):
        if case.get(k):
            keep[k] = case[k]
    kind = str(case.get("analysis") or "static").strip().lower()
    if kind != "static":
        keep["analysis"] = kind
    dens = kind == "frequency" or bool(case.get("gravity"))

    def one(m):
        v = [round(float(m["E"]), 6), round(float(m["nu"]), 6), round(float(m["yield"]), 6)]
        if m.get("brittle"):
            v.append("brittle")
        return v + [round(float(m.get("density") or 0.0), 6)] if dens else v
    single = not isinstance(mats.get("E", {}), dict)
    keep["material"] = one(mats) if single else {k: one(v) for k, v in sorted(mats.items())}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def result_hash(case, mats):
    """case_hash with mats {label: material}: a one-part case hashes its single material."""
    return case_hash(case, list(mats.values())[0] if len(mats) == 1 else mats)


def geom_sig(shape):
    """Fingerprint of a part's geometry (volume, area, bbox and every face's area
    and centre): a result is stale when this changes — moving a hole changes it,
    editing checks.json or drawing options does not."""
    bb = shape.BoundBox
    faces = sorted((round(f.Area, 3), round(f.CenterOfMass.x, 3), round(f.CenterOfMass.y, 3),
                    round(f.CenterOfMass.z, 3), f.Surface.__class__.__name__) for f in shape.Faces)
    data = [round(shape.Volume, 3), round(shape.Area, 3),
            [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)], faces]
    return hashlib.sha1(json.dumps(data).encode()).hexdigest()


def _direction(d):
    try:
        v = an._dir(d)
    except an.RefError as e:
        raise FeaError("load direction: %s" % e)
    return Vector(v)


def _int(n):
    return "{:,}".format(int(n)).replace(",", " ")


class Model:
    """The parts analysed together, as one compound of their solids (in global coordinates):
    which compound face and solid belongs to which part."""

    def __init__(self, doc, parts):
        self.doc, self.parts = doc, list(parts)
        self.labels = [p.Label for p in self.parts]
        self.shapes = [an.global_shape(p) for p in self.parts]
        self.solids, self.solid_owner = [], []
        for k, s in enumerate(self.shapes):
            if not s.Solids:
                raise FeaError("%s has no solid to analyse" % self.labels[k])
            for so in s.Solids:
                self.solids.append(so)
                self.solid_owner.append(k)
        self.compound = Part.makeCompound(self.solids)
        by_area = {}
        for i, so in enumerate(self.solids):
            for f in so.Faces:
                by_area.setdefault(round(f.Area, 6), []).append((f, self.solid_owner[i]))
        self.face_owner = []
        for f in self.compound.Faces:
            own = None
            for g, k in by_area.get(round(f.Area, 6), ()):
                if g.isSame(f):
                    own = k
                    break
            self.face_owner.append(own)

    def entity(self, ref):
        """(part index, resolved reference) of a reference on one of the parts."""
        try:
            e = an.resolve(self.doc, ref)
        except an.RefError as ex:
            raise FeaError("%s: %s" % (ref, ex))
        obj = getattr(e, "obj", None)
        if obj not in self.parts:
            raise FeaError("%s is not on %s — supports, loads and bonded faces must be faces or holes of the "
                           "part%s being analysed%s" % (
                               ref, " or ".join(self.labels), "s" if len(self.parts) > 1 else "",
                               "" if len(self.parts) > 1 else " (to include another part, analyse them together: "
                               "parts=[…] with bonded=[[face, face]])"))
        return self.parts.index(obj), e

    def axis(self, ref):
        """Unit axis of a hole, boss or cylindrical face reference (the first hole's), else None."""
        _, e = self.entity(ref)
        if e.kind in ("holes", "bosses"):
            return Vector(e.feats()[0]["axis"])
        if e.kind == "face" and e.shape.Surface.__class__.__name__ == "Cylinder":
            return Vector(an._canon(e.shape.Surface.Axis))
        return None

    def cylinders(self, ref, d=None):
        """(part index, [compound face indices], label, [diameters]) of the cylinder walls a bearing
        load presses on: a cylindrical face, or of every referenced hole or boss the wall of one
        diameter (d, else its smallest, where the screw, pin or shaft passes)."""
        k, e = self.entity(ref)
        want, diams = [], []
        if e.kind in ("holes", "bosses"):
            for h in e.feats():
                cyl = [f for f in (self.shapes[k].Faces[int(n[4:]) - 1] for n in h["faces"])
                       if f.Surface.__class__.__name__ == "Cylinder"]
                have = sorted(set(round(2 * f.Surface.Radius, 4) for f in cyl))
                if d is None:
                    pick = have[0]
                else:
                    near = [x for x in have if min(abs(x - float(d)), abs(x - h.get("design_comp", 0.0) - float(d)))
                            < 0.011]
                    if not near:
                        raise FeaError("%s has no Ø%s wall: its diameters are %s" % (
                            ref, an._g(float(d)), ", ".join("Ø%s" % an._g(x) for x in have)))
                    pick = near[0]
                want += [f for f in cyl if abs(2 * f.Surface.Radius - pick) < 1e-4]
                if pick not in diams:
                    diams.append(pick)
        elif e.kind == "face" and e.shape.Surface.__class__.__name__ == "Cylinder":
            want, diams = [e.shape], [round(2 * e.shape.Surface.Radius, 4)]
        else:
            raise FeaError("%s is a %s: a bearing load presses on the wall of a hole, a boss or a cylindrical face "
                           "(e.g. %s.holes(d=8)); on a flat face use pressure or force" % (
                               ref, "flat face" if e.kind == "face" else e.kind, self.labels[k]))
        return k, self._compound_faces(k, want, ref), e.label, diams

    def faces(self, ref):
        """(part index, [compound face indices], label) of a face / hole / boss reference."""
        k, e = self.entity(ref)
        if e.kind in ("holes", "bosses"):
            names = []
            for h in (e.holes if e.kind == "holes" else e.bosses):
                names += [n for n in h["faces"] if n not in names]
            want = [self.shapes[k].Faces[int(n[4:]) - 1] for n in names]
        elif e.kind == "face":
            want = [e.shape]
        else:
            lb = self.labels[k]
            raise FeaError("%s is a %s — supports, loads and bonded faces go on faces or holes, e.g. %s.face(-Z) "
                           "or %s.holes(d=5.5)" % (ref, e.kind, lb, lb))
        return k, self._compound_faces(k, want, ref), e.label

    def _compound_faces(self, k, want, ref):
        """Indices in the compound of faces of part k's shape."""
        out = []
        for f in want:
            hit = None
            for j, g in enumerate(self.compound.Faces):
                if self.face_owner[j] == k and abs(g.Area - f.Area) <= 1e-6 * max(1.0, f.Area) and \
                        (g.CenterOfMass - f.CenterOfMass).Length < 1e-6:
                    hit = j
                    break
            if hit is None:
                raise FeaError("could not find %s among %s's faces" % (ref, self.labels[k]))
            if hit not in out:
                out.append(hit)
        return out


# ------------------------------------------------------------------ mesh

def default_size(shapes):
    """(size, note, thinnest wall, size from the parts alone) — mesh size from the parts: a third
    of the thinnest overall dimension and at most half the thinnest wall (two second-order elements
    through it), clamped. Coarsened only when the mesh would be very large — and then the note says
    so; the last value is the size before that."""
    if not isinstance(shapes, (list, tuple)):
        shapes = [shapes]
    size, wall, vol = None, None, 0.0
    for shape in shapes:
        bb = an.tight_bb(shape)
        dims = sorted(d for d in (bb.XLength, bb.YLength, bb.ZLength) if d > 1e-6)
        s = dims[0] / 3.0
        try:
            import manufacture as mf
            t, _, _ = mf.wall_thickness(shape, samples_per_face=2)
            if t and t > 0.05:
                s = min(s, t / 2.0)
                wall = t if wall is None else min(wall, t)
        except Exception:  # noqa: BLE001
            pass
        size = s if size is None else min(size, s)
        vol += shape.Volume
    size = base = max(0.4, min(20.0, size))
    est = vol / (0.118 * size ** 3)
    note = None
    if est > MAX_ELEMENTS:
        size = (vol / (0.118 * MAX_ELEMENTS)) ** (1.0 / 3.0)
        thin = wall is not None and size > 0.51 * wall
        note = "mesh coarsened to %.2f mm to keep the solve within a minute or two%s — pass mesh_size to refine " \
               "(slower)" % (size, "; the thinnest wall (%s mm) gets fewer than 2 elements through it, so stress and "
                             "deflection there are less accurate" % an.fmt(wall, 2) if thin else "")
    return round(size, 3), note, wall, round(base, 3)


def support_margin(case, base):
    """How far from a support the "away from the supports" zone starts (mm): support_margin of the
    case, else twice the size the parts alone give the mesh, which is their thinnest wall (at most
    2/3 of their thinnest dimension). It comes from the parts, not from the mesh that was solved,
    so the stress a part is judged on does not move with mesh_size."""
    m = case.get("support_margin")
    if m is None:
        return 2.0 * base
    try:
        m = float(m)
    except (TypeError, ValueError):
        m = -1.0
    if not m > 0:
        raise FeaError("support_margin must be a distance in mm above 0, got %r" % (case.get("support_margin"),))
    return m


def _sample_faces(faces, step, limit=300000):
    """Points on solid faces, about `step` apart at most (n, 3): a tessellation's triangles, each
    filled with a regular grid."""
    import numpy as np
    tris = []
    for f in faces:
        pts, idx = f.tessellate(max(0.01, min(0.2, step / 5.0)))
        if idx:
            p = np.array([(v.x, v.y, v.z) for v in pts], dtype=float)
            tris.append(p[np.array(idx, dtype=np.int64)])
    if not tris:
        return np.zeros((0, 3))
    t = np.concatenate(tris)                                     # (m, 3 corners, xyz)
    longest = np.sqrt(np.max([((t[:, i] - t[:, (i + 1) % 3]) ** 2).sum(axis=1) for i in range(3)], axis=0))
    n = np.maximum(1, np.ceil(longest / step)).astype(np.int64)
    total = int(((n + 1) * (n + 2) // 2).sum())
    if total > limit:                    # very large faces: a coarser grid (the zone's rim loses a little)
        n = np.maximum(1, np.ceil(n / math.sqrt(total / float(limit)))).astype(np.int64)
    out = []
    for k in np.unique(n):
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        keep = i + j <= k
        w = np.stack([i[keep], j[keep], k - i[keep] - j[keep]], axis=1) / float(k)
        out.append(np.einsum("qc,mcd->mqd", w, t[n == k]).reshape(-1, 3))
    return np.concatenate(out)


def _within(xyz, pts, r):
    """Which of the points xyz (n, 3) lie within r of any of pts (m, 3). A grid of r-sized cells:
    a point is compared with the pts of its own cell and of the 26 around it."""
    import numpy as np
    out = np.zeros(len(xyz), dtype=bool)
    if not len(pts) or not len(xyz) or r <= 0:
        return out
    cand = np.flatnonzero(((xyz >= pts.min(axis=0) - r) & (xyz <= pts.max(axis=0) + r)).all(axis=1))
    if not len(cand):
        return out
    lo = pts.min(axis=0) - 2.0 * r                               # so a candidate's neighbour cells are ≥ 0
    cp = np.floor((pts - lo) / r).astype(np.int64)
    cx = np.floor((xyz[cand] - lo) / r).astype(np.int64)
    dims = cp.max(axis=0) + 3                                    # … and below dims

    def key(c):
        return (c[:, 0] * dims[1] + c[:, 1]) * dims[2] + c[:, 2]
    kp = key(cp)
    order = np.argsort(kp, kind="stable")
    kp, pts = kp[order], pts[order]
    cells, start, count = np.unique(kp, return_index=True, return_counts=True)
    found = np.zeros(len(cand), dtype=bool)
    for off in ((a, b, c) for a in (0, -1, 1) for b in (0, -1, 1) for c in (0, -1, 1)):
        todo = np.flatnonzero(~found)
        if not len(todo):
            break
        k = key(cx[todo] + np.array(off, dtype=np.int64))
        i = np.minimum(np.searchsorted(cells, k), len(cells) - 1)
        hit = cells[i] == k
        rows, first, n = todo[hit], start[i[hit]], count[i[hit]]
        for j in range(int(n.max()) if len(n) else 0):
            m = (n > j) & ~found[rows]
            if not m.any():
                break
            d = xyz[cand[rows[m]]] - pts[first[m] + j]
            found[rows[m][(d * d).sum(axis=1) <= r * r]] = True
    out[cand[found]] = True
    return out


@contextlib.contextmanager
def _one_gmsh_thread():
    """gmsh on one thread. On several it returns a different mesh on every run, so the same case
    would give different numbers each time; one thread costs no time. FreeCAD passes gmsh its FEM
    preference NumOfThreads, which is put back afterwards."""
    prm = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Gmsh")
    had = "NumOfThreads" in prm.GetInts()
    old = prm.GetInt("NumOfThreads", 1)
    prm.SetInt("NumOfThreads", 1)
    try:
        yield
    finally:
        if had:
            prm.SetInt("NumOfThreads", old)
        else:
            prm.RemInt("NumOfThreads")


def _tet10_gradients():
    """Derivatives of the ten C3D10 shape functions at CalculiX's four integration points:
    (4, 10, 3), with respect to the element's own coordinates."""
    import numpy as np
    a, b = 0.5854101966249685, 0.1381966011250105
    dl = np.array([[-1.0, -1.0, -1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    out = np.zeros((4, 10, 3))
    for g in range(4):
        w = [a if i == g else b for i in range(4)]              # the point's four volume coordinates
        for i in range(4):
            out[g, i] = (4 * w[i] - 1) * dl[i]
        for k, (i, j) in enumerate(C3D10_EDGES):
            out[g, 4 + k] = 4 * (w[i] * dl[j] + w[j] * dl[i])
    return out


class Mesh:
    """A tet10 mesh as numpy arrays: nodes (ids, xyz), elements (ids, CalculiX connectivity, part),
    the triangles of every solid face (FreeCAD tria6 node ids), and a lookup from a boundary
    triangle to its element face."""

    def __init__(self, model, size):
        import numpy as np
        import ObjectsFem
        from femmesh.gmshtools import GmshTools
        self.size = size
        fdoc = App.newDocument("FeaMesh")
        try:
            P = fdoc.addObject("Part::Feature", "Part")
            P.Shape = model.compound
            m = ObjectsFem.makeMeshGmsh(fdoc, "Mesh")
            m.Shape = P
            m.CharacteristicLengthMax = "%g mm" % size
            m.ElementOrder = "2nd"
            m.CoherenceMesh = False          # each part keeps its own nodes; bonded faces are tied
            try:                             # gmsh untangles the curved elements that fold over at a curved face
                m.HighOrderOptimize = "Optimization"
            except (AttributeError, TypeError, ValueError):
                pass
            fdoc.recompute()
            with _one_gmsh_thread():
                err = GmshTools(m).create_mesh()
            fm = m.FemMesh
            if err or fm.VolumeCount == 0:
                raise FeaError("gmsh could not mesh %s at %g mm (%s) — try a different mesh_size" % (
                    " + ".join(model.labels), size, str(err or "no volume elements").strip()[:300]))
            nodes = fm.Nodes
            self.nid = np.fromiter(nodes.keys(), np.int64, len(nodes))
            self.xyz = np.array([(v.x, v.y, v.z) for v in nodes.values()], dtype=float)
            vols = sorted(fm.Volumes)
            conn = [fm.getElementNodes(v) for v in vols]
            if any(len(c) != 10 for c in conn[:50]):
                raise FeaError("the mesh is not second-order tetrahedra")
            self.eid = np.array(vols, dtype=np.int64)
            self.conn = np.array(conn, dtype=np.int64)[:, list(SMESH_TO_CCX)]
            groups = {fm.getGroupName(g): g for g in fm.Groups}
            self.epart = np.full(len(vols), -1, dtype=np.int64)
            for i, k in enumerate(model.solid_owner):
                g = groups.get("Solid%d" % (i + 1))
                if g is not None:
                    els = np.array(fm.getGroupElements(g), dtype=np.int64)
                    pos = np.searchsorted(self.eid, els)
                    ok = (pos < len(self.eid)) & (self.eid[np.minimum(pos, len(self.eid) - 1)] == els)
                    self.epart[pos[ok]] = k
            if (self.epart < 0).any():
                if len(model.parts) == 1:
                    self.epart[:] = 0
                else:
                    raise FeaError("could not tell which mesh elements belong to which part (gmsh groups "
                                   "missing) — analyse the parts one at a time")
            self.tris = {}
            for j in range(len(model.compound.Faces)):
                g = groups.get("Face%d" % (j + 1))
                if g is None:
                    raise FeaError("gmsh did not label face %d — try a different mesh_size" % (j + 1))
                self.tris[j] = np.array([fm.getElementNodes(t) for t in fm.getGroupElements(g)],
                                        dtype=np.int64).reshape(-1, 6)
        finally:
            App.closeDocument(fdoc.Name)
        self.pos = np.full(int(self.nid.max()) + 1, -1, dtype=np.int64)
        self.pos[self.nid] = np.arange(len(self.nid))
        self.quality = self._untangle(model)
        # every element face (row 4·e + s is face s+1 of element e), sorted by its corner nodes
        tf = self.conn[:, [c for f in C3D10_FACES for c in f]].reshape(-1, 3)
        keys = _tri_keys(tf)
        self._order = np.argsort(keys, kind="stable")
        self._keys = keys[self._order]
        uniq, first, cnt = np.unique(keys, return_index=True, return_counts=True)
        rows = first[cnt == 1]
        # boundary triangles (node positions), oriented outward (away from the element's 4th corner)
        tri = self.pos[tf[rows]]
        other = np.array([3, 2, 0, 1])[rows % 4]
        opp = self.pos[self.conn[rows // 4, other]]
        p0, p1, p2 = self.xyz[tri[:, 0]], self.xyz[tri[:, 1]], self.xyz[tri[:, 2]]
        n = np.cross(p1 - p0, p2 - p0)
        flip = (n * (self.xyz[opp] - p0)).sum(axis=1) > 0
        tri[flip] = tri[flip][:, [0, 2, 1]]
        self.surface = tri
        self.npart = np.full(len(self.nid), -1, dtype=np.int64)
        self.npart[self.pos[self.conn.ravel()]] = np.repeat(self.epart, 10)

    def _untangle(self, model):
        """No element may fold over: where the Jacobian determinant of a curved element is ≤ 0 at
        an integration point CalculiX stops. The few that gmsh's optimiser leaves get straight
        edges (their mid-side nodes go to the middle of their corners), which keeps them valid
        and makes the surface locally faceted. Returns {min_jacobian, distorted, straightened}.
        The scaled Jacobian is the smallest determinant over that of the straight-sided element:
        1 is undistorted, 0 folded."""
        import numpy as np
        dn = _tet10_gradients()
        straightened = np.zeros(len(self.eid), dtype=bool)
        for attempt in range(5):
            X = self.xyz[self.pos[self.conn]]
            ref = np.einsum("mi,mi->m", np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0]), X[:, 3] - X[:, 0])
            if (ref <= 0).any():
                raise FeaError("gmsh returned %d inverted elements for %s at %g mm: try a different mesh_size" % (
                    int((ref <= 0).sum()), " + ".join(model.labels), self.size))
            q = np.linalg.det(np.einsum("mai,gaj->mgij", X, dn)).min(axis=1) / ref
            bad = q < MIN_JACOBIAN
            if not bad.any():
                return {"min_jacobian": round(float(q.min()), 3), "distorted": int((q < POOR_JACOBIAN).sum()),
                        "straightened": int(straightened.sum())}
            straightened |= bad
            c = self.pos[self.conn[bad]]
            for k, (i, j) in enumerate(C3D10_EDGES):
                self.xyz[c[:, 4 + k]] = 0.5 * (self.xyz[c[:, i]] + self.xyz[c[:, j]])
        raise FeaError("the mesh of %s at %g mm keeps %d elements folded over at a curved face: try a different "
                       "mesh_size" % (" + ".join(model.labels), self.size, int(bad.sum())))

    @property
    def nodes(self):
        return len(self.nid)

    @property
    def elements(self):
        return len(self.eid)

    def face_nodes(self, faces):
        import numpy as np
        if not faces:
            return np.zeros(0, dtype=np.int64)
        return np.unique(np.concatenate([self.tris[j].ravel() for j in faces]))

    def face_tris(self, faces):
        import numpy as np
        return np.concatenate([self.tris[j] for j in faces]) if faces else np.zeros((0, 6), dtype=np.int64)

    def _rows(self, tris):
        """Rows (4 · element + face) of boundary triangles (tria6 node ids) in the element-face table."""
        import numpy as np
        k = _tri_keys(tris[:, :3])
        i = np.searchsorted(self._keys, k)
        i = np.minimum(i, len(self._keys) - 1)
        if not (self._keys[i] == k).all():
            raise FeaError("a face triangle has no element face — the mesh is inconsistent; try another mesh_size")
        return self._order[i]

    def element_faces(self, tris):
        """(element ids, face numbers 1–4) of boundary triangles (tria6 node ids)."""
        row = self._rows(tris)
        return self.eid[row // 4], row % 4 + 1

    def outward(self, tris):
        """(unit outward normals, areas) of boundary triangles, taken flat between their corners."""
        import numpy as np
        row = self._rows(tris)
        p = self.xyz[self.pos[tris[:, :3]]]
        n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        inside = self.xyz[self.pos[self.conn[row // 4, np.array([3, 2, 0, 1])[row % 4]]]]
        n[(n * (inside - p[:, 0])).sum(axis=1) > 0] *= -1.0
        a = 0.5 * np.linalg.norm(n, axis=1)
        return n / (2.0 * a)[:, None], a

    def areas(self, tris):
        import numpy as np
        p = self.xyz[self.pos[tris[:, :3]]]
        return 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)


def _tri_keys(t):
    import numpy as np
    s = np.sort(t, axis=1).astype(np.int64)
    if s.size and s.max() >= 1 << 21:
        raise FeaError("mesh too large (over 2 million nodes) — pass a larger mesh_size")
    return (s[:, 0] << 42) | (s[:, 1] << 21) | s[:, 2]


# ------------------------------------------------------------------ CalculiX input

def _area_normal(model, faces):
    """∫ n dA over compound faces (outward normals) — the resultant direction of a pressure."""
    tot = Vector()
    for j in faces:
        f = model.compound.Faces[j]
        shape = model.shapes[model.face_owner[j]]
        eps = 1e-3 * max(1.0, shape.BoundBox.DiagonalLength)
        pts, tris = f.tessellate(0.05)
        acc = Vector()
        for a, b, c in tris:
            acc += (pts[b] - pts[a]).cross(pts[c] - pts[a]) * 0.5
        # orient by the outward normal at the face centre (tessellation winding is not guaranteed)
        u0, u1, v0, v1 = f.ParameterRange
        p = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        out = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        if shape.isInside(p + out * eps, 1e-6, False):
            out = out * -1
        if acc.dot(out) < 0:
            acc = acc * -1
        tot += acc
    return tot


def _ids(f, ids, per=16):
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), per):
        f.write(", ".join(str(x) for x in ids[i:i + per]) + "\n")


def _resolve(model, case, mats, kind, size, margin):
    """Supports, loads and bonded pairs as solid faces, checked before anything is meshed."""
    fixed_faces, fixed_labels, fixed_parts = [], [], set()
    for ref in case.get("fixed") or []:
        k, faces, label = model.faces(ref)
        fixed_faces += [j for j in faces if j not in fixed_faces]
        fixed_parts.add(k)
        fixed_labels.append("%s (%d face%s)" % (label, len(faces), "" if len(faces) == 1 else "s"))
    applied, load_lines, loads, notes = Vector(), [], [], []
    scale = 0.0                          # the size of the loads (N): what a balance error is measured against

    def force_of(ld, key, k, label):
        """(magnitude, unit direction) of a force given as N + direction or as a vector."""
        fv = ld[key]
        if isinstance(fv, (list, tuple)):
            vec = Vector(*[float(c) for c in fv])
            if vec.Length < 1e-12:
                raise FeaError("load %d: %s vector is zero" % (k + 1, key))
            d = Vector(vec)
            d.normalize()
            return vec.Length, d
        if "direction" not in ld:
            raise FeaError("load %d on %s: give direction (e.g. \"-Z\" or [0, 0, -1]) with a %s "
                           "magnitude, or %s as a vector [fx, fy, fz] in N" % (k + 1, label, key, key))
        return float(fv), _direction(ld["direction"])
    if kind != "frequency":
        for k, ld in enumerate(case.get("loads") or []):
            if not isinstance(ld, dict) or "ref" not in ld:
                raise FeaError("each load is {\"ref\": face/hole, \"force\": N, \"direction\": \"-Z\"}, "
                               "{\"ref\": hole, \"bearing\": N, \"direction\": \"+X\"} or "
                               "{\"ref\": …, \"pressure\": MPa} — got %s" % json.dumps(ld))
            _, faces, label = model.faces(ld["ref"])
            if set(faces) & set(fixed_faces):
                raise FeaError("%s is also a support — a load on a fixed face does nothing" % ld["ref"])
            if "bearing" in ld:
                mag, d = force_of(ld, "bearing", k, label)
                _, faces, label, diams = model.cylinders(ld["ref"], ld.get("d"))
                across = abs(d.dot(model.axis(ld["ref"])))
                if across > 0.05:
                    raise FeaError("load %d on %s: a bearing load presses across the hole, but this direction is "
                                   "%.0f° off that (the axis is along %s): give a direction square to the axis, or "
                                   "use force for a load along it" % (
                                       k + 1, label, math.degrees(math.asin(min(1.0, across))),
                                       an._axis_name(model.axis(ld["ref"]))))
                loads.append({"faces": faces, "bearing": (d.x * mag, d.y * mag, d.z * mag), "label": label})
                applied += d * mag
                scale += mag
                load_lines.append("bearing load %s N along (%s) on %s (pressing on the %s wall, on the side it "
                                  "pushes against)" % (an.fmt(mag, 3), an._pt(d, 3), label,
                                                       " and ".join("Ø%s" % an._g(x) for x in diams)))
            elif "force" in ld:
                mag, d = force_of(ld, "force", k, label)
                loads.append({"faces": faces, "force": (d.x * mag, d.y * mag, d.z * mag)})
                applied += d * mag
                scale += mag
                load_lines.append("force %s N along (%s) on %s" % (an.fmt(mag, 3), an._pt(d, 3), label))
                ax = model.axis(ld["ref"])
                if ax is not None and abs(d.dot(ax)) < 0.95:
                    notes.append("the force on %s is spread over the whole wall, so it also pulls on the far side, "
                                 "which a pin, screw or shaft cannot do: the stress around it comes out too low. "
                                 "For a load that presses on the wall write \"bearing\" in place of \"force\""
                                 % label)
            elif "pressure" in ld:
                p = float(ld["pressure"])
                loads.append({"faces": faces, "pressure": p})
                applied += _area_normal(model, faces) * (-p)
                scale += abs(p) * sum(model.compound.Faces[j].Area for j in faces)
                load_lines.append("pressure %s MPa on %s (pushing on the face)" % (an.fmt(p, 4), label))
            else:
                raise FeaError("load %d on %s needs force (N), bearing (N) or pressure (MPa)" % (k + 1, label))
    grav = bool(case.get("gravity")) and kind != "frequency"
    if grav:
        w = sum(s.Volume * float(mats[k]["density"]) * 1e-6 * 9.81 for k, s in enumerate(model.shapes))
        applied += Vector(0, 0, -w)
        scale += w
        load_lines.append("self weight %s N (-Z)" % an.fmt(w, 3))
    bonds, links = [], []
    for pair in case.get("bonded") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise FeaError("bonded takes pairs of references: [[\"Base.face(+Z)\", \"Bracket.face(-Z)\"], …] — "
                           "got %s" % json.dumps(pair))
        ka, fa, la = model.faces(pair[0])
        kb, fb, lb = model.faces(pair[1])
        if ka == kb:
            raise FeaError("bonded pair %s ↔ %s: both are on %s — bond faces of two different parts" % (
                pair[0], pair[1], model.labels[ka]))
        A = sum(model.compound.Faces[j].Area for j in fa)
        B = sum(model.compound.Faces[j].Area for j in fb)
        gap = Part.makeCompound([model.compound.Faces[j] for j in fa]).distToShape(
            Part.makeCompound([model.compound.Faces[j] for j in fb]))[0]
        if gap > max(0.5, size):
            raise FeaError("bonded pair %s ↔ %s: the faces are %s mm apart — bonded faces must touch (glued, "
                           "clamped or pressed together)" % (pair[0], pair[1], an.fmt(gap, 2)))
        # the smaller face follows the larger one (its nodes are tied to the other face's elements)
        slave, master = ((ka, fa), (kb, fb)) if A <= B else ((kb, fb), (ka, fa))
        bonds.append({"refs": (str(pair[0]), str(pair[1])), "slave": slave, "master": master, "gap": gap})
        links.append((ka, kb))
    # every part must be held: supported itself, or bonded (through other parts) to a supported one
    held, grow = set(fixed_parts), True
    while grow:
        grow = False
        for a, b in links:
            if (a in held) != (b in held):
                held.update((a, b))
                grow = True
    free = [model.labels[k] for k in range(len(model.parts)) if k not in held]
    if free:
        raise FeaError("%s %s neither supported nor bonded to a supported part — add fixed=[…] on it or a bonded "
                       "pair that joins it to one" % (", ".join(free), "is" if len(free) == 1 else "are"))
    return {"fixed_faces": fixed_faces, "fixed_labels": fixed_labels, "loads": loads, "gravity": grav,
            "bonds": bonds, "applied": applied, "load_scale": scale, "load_lines": load_lines, "notes": notes,
            "fixed_points": _sample_faces([model.compound.Faces[j] for j in fixed_faces], margin / 6.0)}


def _sets(model, mesh, plan):
    """The plan as mesh sets for the input writer: fixed nodes, nodal forces, face pressures, ties."""
    import numpy as np
    fixed = mesh.face_nodes(plan["fixed_faces"])
    cload = np.zeros((mesh.nodes, 3))
    dload, bearing = [], []
    for ld in plan["loads"]:
        tris = mesh.face_tris(ld["faces"])
        if "bearing" in ld:
            # a pin, screw or shaft only pushes: a pressure on the side of the wall the load presses on, in
            # proportion to the cosine of the angle between the push and the wall's inward normal
            force = np.array(ld["bearing"], dtype=float)
            mag = float(np.linalg.norm(force))
            d = force / mag
            n, a = mesh.outward(tris)
            w = np.clip(-(n @ d), 0.0, None)
            f = -n * (w * a)[:, None]                             # each triangle's push per unit of peak pressure
            along = float(f.sum(axis=0) @ d)
            off = float(np.linalg.norm(f.sum(axis=0) - along * d)) / max(along, 1e-30)
            if along <= 1e-12 or off > 0.05:
                raise FeaError("bearing load on %s: %s" % (ld["label"], (
                    "no part of the wall faces the load" if along <= 1e-12 else
                    "the wall is not the same on both sides of the load direction (the push it can give is %.0f° "
                    "off it): turn the direction, or use force" % math.degrees(math.atan(off)))))
            f *= mag / along
            pressed = w > 0
            # the remainder of an uneven mesh goes back in evenly: the resultant is the force exactly
            f[pressed] -= np.outer(a[pressed] / a[pressed].sum(), f.sum(axis=0) - force)
            for c in (3, 4, 5):
                np.add.at(cload, mesh.pos[tris[:, c]], f / 3.0)
            bearing.append({"on": ld["label"], "force": mag, "peak_pressure": mag / along * float(w.max()),
                            "area": float(a[pressed].sum())})
        elif "force" in ld:
            # a uniform traction: each quadratic triangle's share goes to its three mid-side nodes
            a = mesh.areas(tris)
            share = a / a.sum() / 3.0
            for c in (3, 4, 5):
                np.add.at(cload, mesh.pos[tris[:, c]], np.outer(share, ld["force"]))
        else:
            el, fno = mesh.element_faces(tris)
            dload += [(int(e), int(s), ld["pressure"]) for e, s in zip(el, fno)]
    ties, tie_lines = [], []
    taken = set(int(x) for x in fixed)          # a held node, or one already tied, is not tied again
    for b in plan["bonds"]:
        (ks, fs), (km, fm) = b["slave"], b["master"]
        slave = [int(x) for x in mesh.face_nodes(fs) if int(x) not in taken]
        taken.update(slave)
        el, fno = mesh.element_faces(mesh.face_tris(fm))
        ties.append({"slave": slave, "master": list(zip(el.tolist(), fno.tolist())),
                     "tol": b["gap"] + max(0.01, 0.1 * mesh.size)})
        tie_lines.append([b["refs"][0], b["refs"][1], model.labels[ks], model.labels[km], len(slave)])
    return {"fixed": fixed, "cload": cload, "dload": dload, "gravity": plan["gravity"], "ties": ties,
            "tie_lines": tie_lines, "bearing": bearing}


def _write_inp(path, model, mesh, mats, kind, modes, st):
    import numpy as np
    with open(path, "w") as f:
        f.write("** CalculiX input written by freecad-mcp (fea.py): mm, N, s, t\n*NODE, NSET=NALL\n")
        np.savetxt(f, np.column_stack([mesh.nid, mesh.xyz]), fmt="%d, %.13g, %.13g, %.13g")
        f.write("*ELEMENT, TYPE=C3D10, ELSET=EALL\n")
        np.savetxt(f, np.column_stack([mesh.eid, mesh.conn]), fmt="%d", delimiter=", ")
        for k, lb in enumerate(model.labels):
            f.write("** %s\n*ELSET, ELSET=P%d\n" % (re.sub(r"[^\w.-]", "_", lb), k + 1))
            _ids(f, mesh.eid[mesh.epart == k])
            m = mats[k]
            f.write("*MATERIAL, NAME=M%d\n*ELASTIC\n%.13g, %.13g\n" % (k + 1, float(m["E"]), float(m["nu"])))
            if m.get("density"):
                f.write("*DENSITY\n%.13g\n" % (float(m["density"]) * 1e-9))     # g/cm³ → t/mm³
            f.write("*SOLID SECTION, ELSET=P%d, MATERIAL=M%d\n" % (k + 1, k + 1))
        f.write("*NSET, NSET=FIXED\n")
        _ids(f, st["fixed"])
        for i, t in enumerate(st["ties"]):
            if not t["slave"]:                 # every node of the face is held or tied already
                continue
            f.write("*NSET, NSET=TN%d\n" % (i + 1))
            _ids(f, t["slave"])
            f.write("*SURFACE, NAME=TS%d, TYPE=NODE\nTN%d\n" % (i + 1, i + 1))
            f.write("*SURFACE, NAME=TM%d, TYPE=ELEMENT\n" % (i + 1))
            f.write("".join("%d, S%d\n" % (e, s) for e, s in t["master"]))
            f.write("*TIE, NAME=T%d, POSITION TOLERANCE=%.6g\nTS%d, TM%d\n" % (i + 1, t["tol"], i + 1, i + 1))
        f.write("*STEP\n")
        if kind == "static":
            f.write("*STATIC\n")
        elif kind == "frequency":
            f.write("*FREQUENCY\n%d\n" % modes)
        else:
            f.write("*BUCKLE\n%d, 0.01\n" % modes)
        f.write("*BOUNDARY\nFIXED, 1, 3\n")
        if kind != "frequency":
            cl = st["cload"]
            rows = np.nonzero(np.abs(cl).max(axis=1) > 0)[0]
            if len(rows):
                f.write("*CLOAD\n")
                for r in rows:
                    for c in range(3):
                        if cl[r, c] != 0.0:
                            f.write("%d, %d, %.13g\n" % (mesh.nid[r], c + 1, cl[r, c]))
            if st["dload"] or st["gravity"]:
                f.write("*DLOAD\n")
                f.write("".join("%d, P%d, %.13g\n" % x for x in st["dload"]))
                if st["gravity"]:
                    f.write("EALL, GRAV, %.6g, 0., 0., -1.\n" % GRAVITY)
        f.write("*NODE FILE\nU\n")
        if kind == "static":
            f.write("*EL FILE\nS\n*NODE PRINT, NSET=FIXED, TOTALS=ONLY\nRF\n")
        f.write("*END STEP\n")


# ------------------------------------------------------------------ CalculiX run and results

def _ccx_binary():
    pref = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Ccx").GetString("ccxBinaryPath", "")
    for c in ([pref] if pref else []) + ["ccx", os.path.join(App.getHomePath(), "bin", "ccx")]:
        w = shutil.which(c)
        if w:
            return w
    raise FeaError("CalculiX (ccx) not found — it ships with FreeCAD (bin/ccx); set its path in FreeCAD's FEM "
                   "preferences if you moved it")


def _gmsh_binary():
    """The gmsh FreeCAD's mesher runs: its FEM preference, else gmsh on PATH or beside FreeCAD."""
    pref = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Gmsh").GetString("gmshBinaryPath", "")
    for c in ([pref] if pref else []) + ["gmsh", "/Applications/Gmsh.app/Contents/MacOS/gmsh",
                                         os.path.join(App.getHomePath(), "bin", "gmsh")]:
        w = shutil.which(c)
        if w:
            return w
    return None


def _says(cmd, pattern):
    """What a program prints for its version (the first group of pattern), or None."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(pattern, p.stdout.decode("utf-8", "replace"))
    return m.group(1) if m else None


_VERSIONS = None


def versions():
    """{calculix, gmsh, freecad}: the versions of the programs behind a result (None for one that
    does not say)."""
    global _VERSIONS
    if _VERSIONS is None:
        try:
            ccx = _says([_ccx_binary(), "-v"], r"Version\s+([\w.]+)")
        except FeaError:
            ccx = None
        gmsh = _gmsh_binary()
        _VERSIONS = {"calculix": ccx, "gmsh": _says([gmsh, "--version"], r"(\d+\.\d+(?:\.\d+)*)") if gmsh else None,
                     "freecad": ".".join(str(x) for x in App.Version()[:3])}
    return dict(_VERSIONS)


def outdated(res):
    """Why a stored result no longer counts although its model and case are unchanged: another
    version of the method, of CalculiX or of gmsh produced it. None while it is current."""
    if res.get("method") != METHOD:
        return "it comes from analysis method %s, now %s" % (res.get("method") or 1, METHOD)
    now, then = versions(), res.get("versions") or {}
    for k, name in (("calculix", "CalculiX"), ("gmsh", "gmsh")):
        if now.get(k) and then.get(k) and now[k] != then[k]:
            return "it was solved with %s %s, now %s" % (name, then[k], now[k])
    return None


def threads():
    return max(1, os.cpu_count() or 1)


def _run_ccx(workdir, job, deadline=None):
    limit = CCX_TIMEOUT if deadline is None else max(10.0, min(CCX_TIMEOUT, deadline - time.time()))
    env = dict(os.environ)
    n = str(threads())
    # CalculiX assembles the matrix and computes stresses on these threads (its SPOOLES solver is serial)
    env.update(OMP_NUM_THREADS=n, CCX_NPROC_STIFFNESS=n, CCX_NPROC_RESULTS=n)
    try:
        p = subprocess.run([_ccx_binary(), "-i", job], cwd=workdir, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=limit)
    except subprocess.TimeoutExpired:
        raise CcxTimeout("CalculiX did not finish within %d s — pass a larger mesh_size (or a longer timeout)"
                         % limit)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def read_frd(path, want=("DISP", "STRESS")):
    """Result blocks of a CalculiX .frd: [(name, value, ids, values)] — value is the step time, the
    frequency (Hz) of a mode or the buckling factor. Fixed-width lines parsed with numpy."""
    import numpy as np
    lines = open(path, "rb").read().split(b"\n")
    out, i, n, val = [], 0, len(lines), None
    while i < n:
        ln = lines[i]
        if ln[:7] == b"  100CL":
            try:
                val = float(ln[12:24])
            except ValueError:
                val = None
        elif ln[:5] == b" -4  ":
            name = ln[5:13].strip().decode()
            j = i + 1
            while j < n and lines[j][:3] != b" -1" and lines[j][:3] != b" -3":
                j += 1
            k = j
            while k < n and lines[k][:3] == b" -1":
                k += 1
            if name in want and k > j:
                rows = lines[j:k]
                L = len(rows[0])
                nv = (L - 13) // 12
                if all(len(r) == L for r in rows):
                    a = np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(-1, L)
                    ids = a[:, 3:13].copy().view("S10").ravel().astype(np.int64)
                    vals = a[:, 13:13 + 12 * nv].copy().view("S12").reshape(-1, nv).astype(np.float64)
                else:
                    ids = np.array([int(r[3:13]) for r in rows], dtype=np.int64)
                    vals = np.array([[float(r[13 + 12 * q:25 + 12 * q]) for q in range(nv)] for r in rows])
                out.append((name, val, ids, vals))
            i = k
            continue
        i += 1
    return out


def _reactions(dat):
    """Sum of the support reaction totals CalculiX prints into the .dat file."""
    tot, found = [0.0, 0.0, 0.0], False
    lines = dat.splitlines()
    for i, ln in enumerate(lines):
        if "total force" not in ln.lower():
            continue
        for nxt in lines[i + 1:i + 4]:          # the totals follow after a blank line
            nums = re.findall(r"[-+]?\d*\.\d+(?:[eE][-+]?\d+)?", nxt)
            if len(nums) >= 3:
                tot = [t + float(x) for t, x in zip(tot, nums[:3])]
                found = True
                break
    return tot if found else None


def _table(dat, title):
    """Rows of numbers under a CalculiX .dat section title (e.g. E I G E N V A L U E   O U T P U T)."""
    rows, on = [], False
    for ln in dat.splitlines():
        if title in ln:
            on, rows = True, []
            continue
        if not on:
            continue
        nums = re.findall(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", ln)
        if nums and re.match(r"^\s*\d+\s", ln):
            rows.append([float(x) for x in nums])
        elif rows and ln.strip():
            break
    return rows


def _von_mises(s):
    import numpy as np
    xx, yy, zz, xy, yz, zx = (s[:, i] for i in range(6))
    return np.sqrt(0.5 * ((xx - yy) ** 2 + (yy - zz) ** 2 + (zz - xx) ** 2) + 3.0 * (xy ** 2 + yz ** 2 + zx ** 2))


def _principal(s):
    """(largest, smallest) principal stress at each node, from the six stress components."""
    import numpy as np
    t = np.empty((len(s), 3, 3))
    for (i, j), c in (((0, 0), 0), ((1, 1), 1), ((2, 2), 2), ((0, 1), 3), ((1, 2), 4), ((0, 2), 5)):
        t[:, i, j] = t[:, j, i] = s[:, c]
    w = np.linalg.eigvalsh(t)                       # ascending
    return w[:, 2], w[:, 0]


def _solve(model, plan, mats, size, workdir, kind, modes, deadline=None):
    import numpy as np
    t0 = time.time()
    mesh = Mesh(model, size)
    t_mesh = time.time() - t0
    st = _sets(model, mesh, plan)
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    _write_inp(os.path.join(workdir, "fea.inp"), model, mesh, mats, kind, modes, st)
    t1 = time.time()
    rc, log = _run_ccx(workdir, "fea", deadline)
    t_ccx = time.time() - t1
    dat = ""
    if os.path.isfile(os.path.join(workdir, "fea.dat")):
        dat = open(os.path.join(workdir, "fea.dat"), errors="replace").read()
    frd = os.path.join(workdir, "fea.frd")
    blocks = read_frd(frd) if os.path.isfile(frd) else []
    out_lines = log.splitlines()
    errors = [(ln.strip() + (" " + out_lines[i + 1].strip() if i + 1 < len(out_lines) and "*" not in out_lines[i + 1]
                             else "")) for i, ln in enumerate(out_lines) if "*ERROR" in ln]
    if rc != 0 or errors or not any(b[0] == "DISP" for b in blocks):
        sta = ""
        for fn in ("fea.sta", "fea.cvg"):
            if os.path.isfile(os.path.join(workdir, fn)):
                sta += open(os.path.join(workdir, fn), errors="replace").read()[-400:]
        text = log + sta
        hint = ("the mesh has elements folded over at a curved face (nonpositive Jacobian): pass another mesh_size"
                if re.search(r"nonpositive jacobian", text, re.I) else
                "the part can still move or rotate — add supports so it is held in every direction"
                if re.search(r"zero pivot|singular|not converge|nonpositive|no convergence", text, re.I)
                else "see the CalculiX output below")
        raise FeaError("CalculiX produced no result: %s\n%s" % (hint, "\n".join(errors[:6]) or log.strip()[-600:]))
    s = {"size": size, "nodes": mesh.nodes, "elements": mesh.elements, "quality": mesh.quality,
         "xyz": mesh.xyz, "surface": mesh.surface,
         "npart": mesh.npart, "applied": plan["applied"], "load_scale": plan["load_scale"],
         "fixed": plan["fixed_labels"], "loads": plan["load_lines"], "bearing": st["bearing"],
         "fixed_points": plan["fixed_points"],
         "ccx_seconds": round(t_ccx, 2), "mesh_seconds": round(t_mesh, 2),
         "warnings": [ln.strip() for ln in out_lines if "*WARNING" in ln and "gentiedmpc" not in ln]}
    # a follower node with no face of the other part within the tolerance stays untied (ccx warns per node)
    untied = set(int(x) for x in re.findall(r"no tied MPC\s+generated for node\s+(\d+)", log))
    s["ties"] = [tl + [len(untied.intersection(t["slave"]))] for tl, t in zip(st["tie_lines"], st["ties"])]

    def field(ids, vals, width):
        a = np.zeros((mesh.nodes, width))
        p = mesh.pos[ids]
        ok = p >= 0
        a[p[ok]] = vals[ok]
        return a
    if kind == "static":
        disp = [b for b in blocks if b[0] == "DISP"][-1]
        s["U"] = field(disp[2], disp[3][:, :3], 3)
        stress = [b for b in blocks if b[0] == "STRESS"]
        sig = field(stress[-1][2], stress[-1][3][:, :6], 6) if stress else np.zeros((mesh.nodes, 6))
        s["vm"] = _von_mises(sig)
        s["p1"], s["p3"] = _principal(sig)
        s["reaction"] = _reactions(dat)
    else:
        shapes = [field(b[2], b[3][:, :3], 3) for b in blocks if b[0] == "DISP"]
        if kind == "frequency":
            rows = _table(dat, "E I G E N V A L U E   O U T P U T")
            vals = [r[3] if len(r) >= 4 else math.sqrt(max(r[1], 0.0)) / (2 * math.pi) for r in rows]
            if not vals:          # fall back to the mode values in the .frd
                vals = [b[1] for b in blocks if b[0] == "DISP"]
        else:
            rows = _table(dat, "B U C K L I N G   F A C T O R   O U T P U T")
            vals = [r[1] for r in rows if len(r) >= 2]
            if not vals:
                vals = [b[1] for b in blocks if b[0] == "DISP"]
        if not vals:
            raise FeaError("CalculiX produced no %s — see %s" % (
                "frequencies" if kind == "frequency" else "buckling factors", os.path.join(workdir, "fea.dat")))
        s["values"] = [float(v) for v in vals]
        s["shapes"] = shapes[-len(vals):] if len(shapes) >= len(vals) else shapes
        s["reaction"] = None
    return s


# ------------------------------------------------------------------ summaries

def _summary(s, model, mats, margin):
    """Static: the stress each part is judged on, overall and away from the supports; the largest
    tension and compression; displacement; safety factor — overall and per part. A ductile part
    is judged on its von Mises stress (yielding). A brittle one breaks on its largest principal
    stress, so it is judged on that by size: tension, or compression against the same strength,
    which is on the safe side."""
    import numpy as np
    xyz, vm, p1, p3 = s["xyz"], s["vm"], s["p1"], s["p3"]
    dl = np.linalg.norm(s["U"], axis=1)
    near = _within(xyz, s["fixed_points"], margin)       # by distance to the fixed faces themselves
    brittle = np.array([bool(m.get("brittle")) for m in mats])
    judged = np.where(brittle[np.maximum(s["npart"], 0)], np.maximum(np.abs(p1), np.abs(p3)), vm)
    s["judged"] = judged

    def peak(mask, vals, low=False):
        if not mask.any():
            return None, None
        idx = np.flatnonzero(mask)
        i = int(idx[np.argmin(vals[mask]) if low else np.argmax(vals[mask])])
        return float(vals[i]), [float(c) for c in xyz[i]]
    per = []
    for k, lb in enumerate(model.labels):
        mk = s["npart"] == k
        v, at = peak(mk, judged)
        va, aat = peak(mk & ~near, judged)
        d, dat = peak(mk, dl)
        y = float(mats[k]["yield"])
        per.append({"part": lb, "criterion": "principal" if brittle[k] else "von Mises",
                    "max_stress": v, "max_stress_at": at, "max_stress_away": va, "max_stress_away_at": aat,
                    "max_vm": peak(mk, vm)[0], "max_vm_away": peak(mk & ~near, vm)[0],
                    "max_disp": d, "max_disp_at": dat, "yield": y,
                    "safety_factor": (y / v) if v and v > 1e-12 else None,
                    "safety_factor_away": (y / va) if va and va > 1e-12 else None})
    allm = np.ones(len(vm), dtype=bool)
    v, at = peak(allm, judged)
    va, aat = peak(~near, judged)
    w, wat = peak(allm, vm)
    wa, waat = peak(~near, vm)
    t, tat = peak(allm, p1)
    c, cat = peak(allm, p3, low=True)
    i = int(np.argmax(dl))
    sfs = [p["safety_factor"] for p in per if p["safety_factor"] is not None]
    out = {"criterion": "principal" if brittle.all() else "von Mises" if not brittle.any() else "per part",
           "max_stress": v, "max_stress_at": at, "max_stress_away": va, "max_stress_away_at": aat,
           "max_vm": w, "max_vm_at": wat, "max_vm_away": wa, "max_vm_away_at": waat,
           "max_principal": t, "max_principal_at": tat, "min_principal": c, "min_principal_at": cat,
           "max_disp": float(dl[i]), "max_disp_at": [float(c) for c in xyz[i]],
           "max_disp_vec": [float(c) for c in s["U"][i]],
           "safety_factor": min(sfs) if sfs else None,
           "nodes": s["nodes"], "elements": s["elements"], "size": s["size"], "mesh_quality": s["quality"]}
    if len(per) > 1:
        out["per_part"] = per
    return out


def _mode_summary(s, kind):
    """Frequency / buckling: the values, and where and which way the first relevant mode moves."""
    import numpy as np
    vals = s["values"]
    out = {"nodes": s["nodes"], "elements": s["elements"], "size": s["size"], "mesh_quality": s["quality"]}
    if kind == "frequency":
        out["frequencies_hz"] = vals
        pick = 0
    else:
        out["buckling_factors"] = vals
        pos = [i for i, v in enumerate(vals) if v > 0]
        pick = min(pos, key=lambda i: vals[i]) if pos else None
        out["buckling_factor"] = vals[pick] if pick is not None else None
        out["buckling_mode"] = pick + 1 if pick is not None else None
    if pick is not None and pick < len(s["shapes"]):
        U = s["shapes"][pick]
        dl = np.linalg.norm(U, axis=1)
        i = int(np.argmax(dl))
        d = U[i] / (dl[i] or 1.0)
        out["mode_max_at"] = [float(c) for c in s["xyz"][i]]
        out["mode_direction"] = [float(c) for c in d]
    return out


def _axis_words(d):
    a = [abs(c) for c in d]
    k = max(range(3), key=lambda i: a[i])
    others = [i for i in range(3) if i != k and a[i] > 0.35 * a[k]]
    return "+".join("XYZ"[i] for i in sorted([k] + others))


# ------------------------------------------------------------------ run a case

def run_case(doc, parts, case, mats, out_dir, refine=None, mesh_size=None, deadline=None):
    """Solve a case → (result dict, png path or None). parts: the part objects; mats: {label:
    material}. A second mesh shows whether the result has converged. refine None: a finer one
    while the first is small enough (AUTO_REFINE nodes; the finer one is then the result), else a
    coarser one; True: the finer one; False: no second mesh. deadline: time.time() by which
    CalculiX must be done."""
    t0 = time.time()
    if not isinstance(parts, (list, tuple)):
        parts = [parts]
    if not isinstance(mats.get("E", {}), dict):           # one part's material
        mats = {parts[0].Label: mats}
    kind = analysis_kind(case)
    modes = mode_count(case)
    model = Model(doc, parts)
    mlist = [mats[lb] for lb in model.labels]
    if kind == "frequency" or case.get("gravity"):
        miss = [lb for lb, m in zip(model.labels, mlist) if not m.get("density")]
        if miss:
            raise FeaError("%s needs the density of %s's material — set project.json material.density_g_cm3 "
                           "or parts.%s.density_g_cm3, or give material={…, \"density\": g/cm³}" % (
                               "a frequency analysis" if kind == "frequency" else "self weight", miss[0], miss[0]))
    notes = []
    size, note, wall, base = default_size(model.shapes)
    if mesh_size:
        size = float(mesh_size)
    elif note:
        notes.append(note)
    work = os.path.join(out_dir, "ccx")
    margin = support_margin(case, base)
    summ = (lambda s: _summary(s, model, mlist, margin)) if kind == "static" else (lambda s: _mode_summary(s, kind))
    plan = _resolve(model, case, mlist, kind, size, margin)
    notes += plan["notes"]
    s1 = _solve(model, plan, mlist, size, work, kind, modes, deadline)
    r1 = summ(s1)
    final, s_final, coarse, fine, extra = r1, s1, None, None, 0.0     # coarse, fine: the two meshes' summaries
    if mesh_size and wall and size > 0.51 * wall:
        notes.append("the %s mm wall gets fewer than 2 elements through it at this mesh size: stress and deflection "
                     "there are less accurate, so pass a smaller mesh_size" % an.fmt(wall, 2))
    if refine is False:
        notes.append("solved on one mesh only (refine=false), so convergence is not shown: a fea_* check on this "
                     "result fails unless it sets accept_unconverged")
    else:
        est = s1["elements"] * REFINE ** 3
        finer = (refine is True or s1["nodes"] <= AUTO_REFINE) and est <= HARD_MAX
        other = round(size / REFINE if finer else size * REFINE, 3)
        side = work if finer else work + "_coarse"
        try:
            s2 = _solve(model, plan, mlist, other, side, kind, modes, deadline)
            extra = s2["ccx_seconds"]
            if finer:
                coarse, fine = r1, summ(s2)
                final, s_final = fine, s2
            else:
                coarse, fine = summ(s2), r1
                notes.append("convergence is shown against a coarser mesh (%s mm, %s nodes): %s" % (
                    an.fmt(other, 2), _int(s2["nodes"]),
                    "a finer one would have ≈%s elements, more than CalculiX solves in reasonable memory and time "
                    "(≈9 GB for 320 000 nodes)" % _int(est) if est > HARD_MAX else
                    "a finer one than these %s nodes takes minutes, and refine=true solves it" % _int(s1["nodes"])))
        except CcxTimeout:
            notes.append("the second solve (at %s mm) did not finish in the time left, so convergence is not shown: "
                         "pass a longer timeout" % an.fmt(other, 2))
        except FeaError as e:
            notes.append("the second solve (at %s mm) failed, so convergence is not shown: %s" % (
                an.fmt(other, 2), (str(e).splitlines() or [""])[0][:300]))
        finally:
            if not finer:
                shutil.rmtree(side, ignore_errors=True)
    res = dict(final)
    res.update(analysis=kind, material={k: mlist[0].get(k) for k in MAT_KEYS},
               fixed=s1["fixed"], loads=s1["loads"], applied=list(s1["applied"]), coarse=coarse,
               notes=notes, ccx_seconds=round(s1["ccx_seconds"] + extra, 2),
               threads=threads(), method=METHOD, versions=versions())
    if kind == "static":
        res["support_margin"] = round(margin, 3)
    if s_final["bearing"]:
        res["bearing"] = s_final["bearing"]
    if len(model.parts) == 1:
        res["part"] = model.labels[0]
    else:
        res["parts"] = model.labels
        res["materials"] = {lb: {k: m.get(k) for k in MAT_KEYS} for lb, m in zip(model.labels, mlist)}
    q = s_final["quality"]
    if q["straightened"]:
        res["notes"].append("%d element%s folded over at a curved face and got straight edges: the surface is "
                            "faceted there" % (q["straightened"], "" if q["straightened"] == 1 else "s"))
    if q["distorted"]:
        res["notes"].append("%d element%s badly distorted at curved faces (scaled Jacobian down to %s, 1 is "
                            "undistorted): the stress next to them is less accurate, and a smaller mesh_size helps"
                            % (q["distorted"], " is" if q["distorted"] == 1 else "s are",
                               an.fmt(q["min_jacobian"], 2)))
    if s_final["ties"]:
        res["bonded"] = [{"a": a, "b": b, "follower": sl, "leader": ml, "tied_nodes": n - u, "untied_nodes": u}
                         for a, b, sl, ml, n, u in s_final["ties"]]
        for b in res["bonded"]:
            if b["untied_nodes"] > 0.02 * (b["tied_nodes"] + b["untied_nodes"]):
                res["notes"].append("bonded %s ↔ %s: %s of %s's nodes on it found no face of %s to follow — the "
                                    "faces touch over only part of their area (fine if intended; otherwise check "
                                    "the pair)" % (b["a"], b["b"], _int(b["untied_nodes"]), b["follower"],
                                                   b["leader"]))
    if kind == "static":
        _finish_static(res, s_final, coarse, fine, mlist, model)
    else:
        _finish_modes(res, coarse, fine, kind, modes, case)
    if s_final["warnings"]:
        res["ccx_warnings"] = s_final["warnings"][:20]
    for m, lb in zip(mlist, model.labels):
        if m.get("note"):
            res["notes"].append("material%s: %s" % (" (%s)" % lb if len(mlist) > 1 else "", m["note"]))
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, "fea.png")
    try:
        if kind == "static":
            plot(s_final, res, png)
        else:
            plot_modes(s_final, res, png, kind)
    except Exception as e:  # noqa: BLE001
        res["notes"].append("plot failed: %s" % e)
        png = None
    res["time"] = round(time.time() - t0, 2)
    return res, png


def _finish_static(res, s_final, r1, r2, mlist, model):
    """r1, r2: the summaries of the coarser and the finer mesh, or None when one mesh was solved."""
    applied, reaction = s_final["applied"], s_final["reaction"]
    # measured against the size of the loads, not their resultant: loads that balance each other
    # (pressure inside a bore, two opposing forces) have no resultant to compare with
    scale = max(s_final["load_scale"], applied.Length)
    bal = None
    if reaction is not None and scale > 1e-9:
        bal = (Vector(*reaction) + applied).Length / scale
    res.update(reaction=reaction, balance_error=bal, load_scale=scale)
    if len(model.parts) > 1:
        gov = min(res["per_part"], key=lambda p: p["safety_factor"] if p["safety_factor"] is not None else 1e99)
        res["governing_part"] = gov["part"]
        k = model.labels.index(gov["part"])
        res["material"] = {kk: mlist[k].get(kk) for kk in MAT_KEYS}
    if r2:
        dd = abs(r2["max_disp"] - r1["max_disp"]) / max(r2["max_disp"], 1e-12)
        ds = abs(r2["max_stress"] - r1["max_stress"]) / max(r2["max_stress"], 1e-12)
        da = (abs(r2["max_stress_away"] - r1["max_stress_away"]) / max(r2["max_stress_away"], 1e-12)
              if r1.get("max_stress_away") and r2.get("max_stress_away") else None)
        res["change"] = {"disp": dd, "stress": ds, "stress_away": da}
        # per quantity: a displacement can converge while a peak stress at a singularity never does
        res["converged_disp"] = dd < 0.03
        res["converged_stress"] = ds < 0.10
        res["converged_stress_away"] = None if da is None else da < 0.10
        res["converged"] = res["converged_disp"] and (res["converged_stress"] or bool(res["converged_stress_away"]))
        if dd < 0.03 and ds >= 0.10:
            res["notes"].append("the peak stress still changes %.0f %% with a finer mesh — a stress singularity "
                                "(sharp internal corner, edge of a rigid support or a point-like load): judge the "
                                "stress away from it, or add a fillet" % (100 * ds))
    if res.get("max_stress_away") and res["max_stress"] > 1.3 * res["max_stress_away"]:
        res["notes"].append("the peak is at a support: a rigid fixture inflates the stress at its edge; away from "
                            "the supports the peak is %s MPa" % an.fmt(res["max_stress_away"], 3))
    # A linear analysis holds for stresses below the strength and for small deflections. Outside that the
    # numbers are not physical, and a check on them would pass on nothing.
    over = []
    for p in res.get("per_part") or [dict(res, part=res.get("part"), **{"yield": mlist[0]["yield"]})]:
        stress = p.get("max_stress_away") if p.get("max_stress_away") is not None else p.get("max_stress")
        if stress and stress > p["yield"]:
            over.append("%s %s (%s MPa away from the supports, against %s %s MPa)" % (
                p["part"], "breaks" if p.get("criterion") == "principal" else "yields", an.fmt(stress, 3),
                _strength(p.get("criterion")), an.fmt(p["yield"], 0)))
    res["beyond_yield"] = bool(over)
    if over:
        res["notes"].append("%s: past that the material no longer follows this linear analysis, so the stresses "
                            "are not real and the deflection is larger than shown" % "; ".join(over))
    bb = model.compound.BoundBox
    span = max(bb.XLength, bb.YLength, bb.ZLength)
    res["large_deflection"] = bool(res["max_disp"] > LARGE_DEFLECTION * span)
    if res["large_deflection"]:
        res["notes"].append("the largest displacement, %s mm, is %.0f %% of the longest dimension (%s mm): beyond "
                            "small deflections a linear analysis is approximate, and too favourable where the part "
                            "is in compression (check buckling)" % (
                                an.fmt(res["max_disp"], 3), 100 * res["max_disp"] / span, an.fmt(span, 1)))


def _finish_modes(res, r1, r2, kind, modes, case):
    """r1, r2: the summaries of the coarser and the finer mesh, or None when one mesh was solved."""
    res["modes"] = modes
    if kind == "frequency":
        f = res["frequencies_hz"]
        if case.get("loads") or case.get("gravity"):
            res["notes"].append("loads are not used by a frequency analysis (natural frequencies depend on the "
                                "supports, stiffness and mass only)")
        res["notes"].append("these are upper bounds: only the part's own mass is in the analysis and its supports "
                            "are rigid. What it carries (a motor, a load) and real fixings both lower the "
                            "frequencies")
        if len(f) > 1 and f[0] > 0.5 and abs(f[1] - f[0]) < 0.005 * f[0]:
            res["notes"].append("modes 1 and 2 are a pair at the same frequency: the part is equally stiff in two "
                                "directions, so either shape (or a mix) can occur")
        if f and f[0] < max(1e-3 * f[-1], 0.5):
            res["notes"].append("mode 1 at %s Hz is a rigid-body mode: the supports leave the part free to move — "
                                "add supports so it is held in every direction" % an.fmt(f[0], 2))
        if r2 and r1.get("frequencies_hz") and f:
            ch = abs(f[0] - r1["frequencies_hz"][0]) / max(abs(f[0]), 1e-12)
            res["change"] = {"f1": ch}
            res["converged"] = res["converged_freq"] = ch < 0.02
    else:
        lam = res.get("buckling_factor")
        if lam is None:
            big = max(abs(v) for v in res["buckling_factors"])
            res["notes"].append("no positive buckling factor among the %d computed: the given loads do not buckle "
                                "the part below %s × (only the reversed loads would, at %s ×) — pass modes=… to "
                                "look further" % (len(res["buckling_factors"]), an.fmt(big, 3),
                                                  an.fmt(-max(res["buckling_factors"]), 3)))
            res["buckling_factor_above"] = big
        elif any(v < 0 and abs(v) < lam for v in res["buckling_factors"]):
            neg = max(v for v in res["buckling_factors"] if v < 0)
            res["notes"].append("a negative factor (%s) means the reversed loads would buckle the part at %s × — "
                                "check if the load can act both ways" % (an.fmt(neg, 3), an.fmt(-neg, 3)))
        if r2 and r1.get("buckling_factor") and lam:
            ch = abs(lam - r1["buckling_factor"]) / max(abs(lam), 1e-12)
            res["change"] = {"factor": ch}
            res["converged"] = res["converged_buckling"] = ch < 0.03
        elif r2:        # the two meshes must agree on whether the loads buckle the part at all
            res["converged"] = res["converged_buckling"] = lam is None and r1.get("buckling_factor") is None
        res["notes"].append("linear (eigenvalue) buckling assumes a perfect part: real parts buckle earlier "
                            "(imperfections, eccentric loads) — keep a margin of 3 or more on the factor")


# ------------------------------------------------------------------ report

def report_lines(res, case_name):
    f = an.fmt
    kind = res.get("analysis", "static")
    who = res.get("part") or " + ".join(res.get("parts") or [])
    lines = ["ANALYZE %s — case '%s' (%s, CalculiX)  %s  %.1f s" % (
        who, case_name, TITLES[kind], res.get("rev", ""), res.get("time", 0))]
    mats = res.get("materials") or {who: res["material"]}
    for lb, m in mats.items():
        lines.append("material  %s%s: E %.0f MPa, ν %s, %s %.0f MPa%s (%s)" % (
            "%s " % lb if len(mats) > 1 else "", m["name"], m["E"], f(m["nu"], 3),
            "brittle, breaking stress" if m.get("brittle") else "yield", m["yield"],
            ", density %s g/cm³" % f(m["density"], 2) if kind == "frequency" and m.get("density") else "",
            m["source"]))
    for b in res.get("bonded") or []:
        lines.append("bonded    %s ↔ %s — tied: %s nodes of %s follow %s%s" % (
            b["a"], b["b"], _int(b["tied_nodes"]), b["follower"], b["leader"],
            " (%s more found nothing to follow)" % _int(b["untied_nodes"]) if b.get("untied_nodes") else ""))
    lines.append("supports  fixed: %s" % "; ".join(res["fixed"]))
    if kind != "frequency":
        for ld in res["loads"]:
            lines.append("load      %s" % ld)
        for b in res.get("bearing") or []:
            lines.append("          %s: peak contact pressure %s MPa, pressing on %s mm² of the wall" % (
                b["on"], f(b["peak_pressure"], 3), f(b["area"], 1)))
    mesh = "mesh      tet10, %s nodes, %s elements at %s mm" % (_int(res["nodes"]), _int(res["elements"]),
                                                               f(res["size"], 2))
    if res.get("coarse"):
        mesh += " (also solved at %s mm: %s nodes)" % (f(res["coarse"]["size"], 2), _int(res["coarse"]["nodes"]))
    if res.get("mesh_quality"):
        mesh += " · scaled Jacobian ≥ %s" % f(res["mesh_quality"]["min_jacobian"], 2)
    lines.append(mesh)
    if kind == "static":
        _report_static(lines, res)
    elif kind == "frequency":
        fr = res["frequencies_hz"]
        lines.append("modes     %s Hz (first %d)" % (" · ".join(f(x, 1) for x in fr), len(fr)))
        if res.get("mode_max_at"):
            lines.append("mode 1    %s Hz — moves mostly along %s, most at (%s)" % (
                f(fr[0], 1), _axis_words(res["mode_direction"]), an._pt(Vector(*res["mode_max_at"]), 2)))
        if res.get("change"):
            lines.append("converge  finer mesh changes f1 %.1f %% %s" % (
                100 * res["change"]["f1"], "✓" if res.get("converged") else "✗ (see notes)"))
    else:
        bf = res["buckling_factors"]
        lines.append("buckling  load factors %s (lowest %d by size)" % (" · ".join(f(x, 3) for x in bf), len(bf)))
        lam = res.get("buckling_factor")
        if lam:
            ap = math.sqrt(sum(c * c for c in res["applied"]))
            at = an._pt(Vector(*res["mode_max_at"]), 2) if res.get("mode_max_at") else "?"
            lines.append("          → buckles at %s × the load (%s N for the %s N applied) — mode %d, most at "
                         "(%s)" % (f(lam, 2), f(lam * ap, 1), f(ap, 1), res["buckling_mode"], at))
        if res.get("change"):
            lines.append("converge  finer mesh changes the factor %.1f %% %s" % (
                100 * res["change"]["factor"], "✓" if res.get("converged") else "✗ (see notes)"))
    v = res.get("versions") or {}
    lines.append("solver    %s (analysis method %s)" % (", ".join(
        "%s %s" % (name, v[k]) for k, name in (("calculix", "CalculiX"), ("gmsh", "gmsh"), ("freecad", "FreeCAD"))
        if v.get(k)) or "CalculiX", res.get("method", 1)))
    for n in res.get("notes") or []:
        lines.append("note      %s" % n)
    return lines


def _judged(criterion):
    return "max principal stress" if criterion == "principal" else "max von Mises"


def _strength(criterion):
    return "the breaking stress" if criterion == "principal" else "yield"


def _report_static(lines, res):
    f = an.fmt
    m = res["material"]
    margin = f(res.get("support_margin", 0), 2)
    if res.get("reaction") is not None:
        ok = res.get("balance_error") is not None and res["balance_error"] < 0.01
        lines.append("reaction  supports carry (%s) N vs applied (%s) N  %s" % (
            an._pt(Vector(*res["reaction"]), 2), an._pt(Vector(*res["applied"]), 2),
            "✓ in equilibrium%s" % (" (the loads balance each other)" if sum(
                c * c for c in res["applied"]) ** 0.5 < 0.01 * (res.get("load_scale") or 0) else "")
            if ok else "✗ out of balance by %.1f %% — check the loads and supports"
            % (100 * (res.get("balance_error") or 0))))
    per = res.get("per_part")
    sf = res.get("safety_factor")
    if per:
        top = max(per, key=lambda p: p["max_stress"] or 0)
        lines.append("stress    %s %s MPa at (%s) in %s" % (
            _judged(top["criterion"]), f(res["max_stress"], 3), an._pt(Vector(*res["max_stress_at"]), 2), top["part"]))
        for p in per:
            lines.append("part      %s: %s %s MPa → safety factor %s against %s %.0f MPa%s · max displacement %s "
                         "mm" % (
                p["part"], _judged(p["criterion"]), f(p["max_stress"], 3), f(p["safety_factor"], 2),
                _strength(p["criterion"]), p["yield"],
                " (away from the supports %s MPa → %s)" % (f(p["max_stress_away"], 3), f(p["safety_factor_away"], 2))
                if p.get("max_stress_away") is not None and abs(p["max_stress_away"] - p["max_stress"]) > 1e-9
                else "", f(p["max_disp"], 4)))
        lines.append("          → governing: %s, safety factor %s%s" % (
            res.get("governing_part"), f(sf, 2), "  (away from the supports: over %s mm from a support)" % margin
            if any(p.get("max_stress_away") is not None for p in per) else ""))
    else:
        crit = res.get("criterion")
        lines.append("stress    %s %s MPa at (%s)%s" % (
            _judged(crit), f(res["max_stress"], 3), an._pt(Vector(*res["max_stress_at"]), 2),
            "  → safety factor %s against %s %.0f MPa" % (f(sf, 2), _strength(crit), m["yield"]) if sf else ""))
        if res.get("max_stress_away") is not None and abs(res["max_stress_away"] - res["max_stress"]) > 1e-9:
            lines.append("          away from the supports: %s MPa at (%s)  → safety factor %s  (over %s mm from a "
                         "support)" % (
                             f(res["max_stress_away"], 3), an._pt(Vector(*res["max_stress_away_at"]), 2),
                             f(m["yield"] / res["max_stress_away"], 2) if res["max_stress_away"] > 1e-12 else "∞",
                             margin))
    if res.get("criterion") != "von Mises":
        lines.append("          a brittle material breaks on its largest principal stress, so it is judged on that "
                     "(by size: tension or compression), not on von Mises (%s MPa)" % f(res["max_vm"], 3))
    t, c = res.get("max_principal"), res.get("min_principal")
    if t is not None and c is not None:
        lines.append("principal %s · %s" % (
            "largest tension %s MPa at (%s)" % (f(t, 3), an._pt(Vector(*res["max_principal_at"]), 2))
            if t > 1e-9 else "no tension",
            "largest compression %s MPa at (%s)" % (f(c, 3), an._pt(Vector(*res["min_principal_at"]), 2))
            if c < -1e-9 else "no compression"))
    lines.append("deflect   max displacement %s mm at (%s), direction (%s)" % (
        f(res["max_disp"], 4), an._pt(Vector(*res["max_disp_at"]), 2), an._pt(Vector(*res["max_disp_vec"]), 4)))
    if res.get("change"):
        ch = res["change"]

        def mark(x):
            return "✓" if x else "✗"
        lines.append("converge  finer mesh changes displacement %.1f %% %s · peak stress %.1f %% %s%s%s" % (
            100 * ch["disp"], mark(res.get("converged_disp")), 100 * ch["stress"], mark(res.get("converged_stress")),
            " · stress away from supports %.1f %% %s" % (100 * ch["stress_away"],
                                                         mark(res.get("converged_stress_away")))
            if ch.get("stress_away") is not None else "",
            "  → converged ✓" if res.get("converged") else "  (see notes)"))


# ------------------------------------------------------------------ pictures

def _render(ax, xyz, tri, vals, vmax, cmap, title):
    """The outer surface, seen from the usual iso direction: back faces culled, painter's order,
    a little shading — a 2D picture, fast for large meshes."""
    import numpy as np
    from matplotlib.collections import PolyCollection
    elev, azim = math.radians(24), math.radians(-58)
    eye = np.array([math.cos(elev) * math.cos(azim), math.cos(elev) * math.sin(azim), math.sin(elev)])
    right = np.array([-math.sin(azim), math.cos(azim), 0.0])
    up = np.cross(eye, right)
    p = xyz[tri]                                     # (m, 3, 3)
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    nl = np.linalg.norm(n, axis=1) + 1e-30
    facing = (n @ eye) / nl
    keep = facing > 0
    p, facing = p[keep], facing[keep]
    v = vals[tri[keep]].mean(axis=1)
    depth = (p.mean(axis=1) @ eye)
    order = np.argsort(depth)
    uv = np.stack([p @ right, p @ up], axis=-1)[order]
    col = cmap(np.clip(v[order] / (vmax or 1.0), 0, 1))
    shade = 0.55 + 0.45 * facing[order]
    col[:, :3] *= shade[:, None]
    ax.add_collection(PolyCollection(uv, facecolors=col, edgecolors="none", antialiased=False))
    allp = np.stack([xyz @ right, xyz @ up], axis=-1)
    lo, hi = allp.min(axis=0), allp.max(axis=0)
    pad = 0.04 * max(hi - lo)
    ax.set_xlim(lo[0] - pad, hi[0] + pad)
    ax.set_ylim(lo[1] - pad, hi[1] + pad)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title, fontsize=10)
    # axis triad in the lower left corner
    L = 0.12 * max(hi - lo)
    o = np.array([lo[0] - pad * 0.5, lo[1] - pad * 0.5])
    for vec, name, c in (((1, 0, 0), "X", "#c0392b"), ((0, 1, 0), "Y", "#27ae60"), ((0, 0, 1), "Z", "#2e6fd8")):
        d = np.array([np.dot(vec, right), np.dot(vec, up)]) * L
        ax.annotate("", xy=o + d, xytext=o, arrowprops=dict(arrowstyle="->", color=c, lw=1.2),
                    annotation_clip=False)
        ax.text(*(o + d * 1.25), name, color=c, fontsize=8, ha="center", va="center", clip_on=False)
    return right, up


def plot(s, res, png):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xyz, tri, U = s["xyz"], s["surface"], s["U"]
    span = float(max(np.ptp(xyz, axis=0).max(), 1e-9))
    scale = 0.06 * span / max(res["max_disp"], 1e-12)
    dl = np.linalg.norm(U, axis=1)
    fig = plt.figure(figsize=(12, 5.4), dpi=110)
    cmap = plt.get_cmap("turbo")
    for k, (title, vals, unit, pts, at, peak, nd) in enumerate((
            ({"von Mises": "von Mises stress", "principal": "largest principal stress"}.get(
                res.get("criterion"), "judged stress"), s["judged"], "MPa", xyz, res["max_stress_at"],
             res["max_stress"], 3),
            ("displacement (×%.3g)" % scale, dl, "mm", xyz + scale * U, res["max_disp_at"], res["max_disp"], 4))):
        ax = fig.add_subplot(1, 2, k + 1)
        vmax = float(vals.max()) or 1.0
        right, up = _render(ax, pts, tri, vals, vmax, cmap, "%s — max %s %s" % (title, an.fmt(peak, nd), unit))
        a = np.array(at, dtype=float)
        if k == 1:
            i = int(np.argmax(dl))
            a = pts[i]
        ax.plot([a @ right], [a @ up], "o", color="black", ms=5)
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, vmax))
        fig.colorbar(sm, ax=ax, shrink=0.6, label=unit)
    who = res.get("part") or " + ".join(res.get("parts") or [])
    fig.suptitle("%s — %s" % (who, " / ".join(sorted(set(m["name"] for m in (res.get("materials") or
                                                                              {who: res["material"]}).values())))),
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)


def plot_modes(s, res, png, kind):
    """Mode shapes: frequency → modes 1 and 2; buckling → the lowest positive mode (and the next)."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xyz, tri, vals = s["xyz"], s["surface"], s["values"]
    if kind == "frequency":
        picks = list(range(min(2, len(s["shapes"]))))
    else:
        pos = sorted((i for i, v in enumerate(vals) if v > 0 and i < len(s["shapes"])), key=lambda i: vals[i])
        picks = (pos or list(range(min(1, len(s["shapes"])))))[:2]
    if not picks:
        raise FeaError("no mode shapes to plot")
    span = float(max(np.ptp(xyz, axis=0).max(), 1e-9))
    fig = plt.figure(figsize=(6 * len(picks), 5.4), dpi=110)
    cmap = plt.get_cmap("turbo")
    for k, i in enumerate(picks):
        U = s["shapes"][i]
        dl = np.linalg.norm(U, axis=1)
        top = float(dl.max()) or 1.0
        pts = xyz + (0.1 * span / top) * U
        title = ("mode %d — %s Hz" % (i + 1, an.fmt(vals[i], 1)) if kind == "frequency" else
                 "mode %d — buckles at %s × the load" % (i + 1, an.fmt(vals[i], 2)) if vals[i] > 0 else
                 "mode %d — factor %s: the reversed load" % (i + 1, an.fmt(vals[i], 2)))
        ax = fig.add_subplot(1, len(picks), k + 1)
        _render(ax, pts, tri, dl / top, 1.0, cmap, title)
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, 1))
        fig.colorbar(sm, ax=ax, shrink=0.6, label="relative displacement")
    who = res.get("part") or " + ".join(res.get("parts") or [])
    fig.suptitle("%s — %s (deformation exaggerated)" % (who, TITLES[kind]), fontsize=11)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)
