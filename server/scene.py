# SPDX-License-Identifier: Apache-2.0
"""The live viewer's scene, written by every build inside the FreeCAD worker.

build/viewer/scene.glb holds one node per part ("p0", "p1", …), exported by
FreeCAD's own glTF writer (OCCT). Shapes are tessellated first, because a
console session has no view providers to do it. scene.json names each node
(label, colour, reference or made part) and carries the build status: revision,
errors and checks. The page served by viewer.py polls scene.json and reloads
the GLB when the version changes.

Both files are written to a temporary name and then renamed, so a page never
reads half a file. The GLB is written first, then the JSON that names it."""
import json
import os
import time

import FreeCAD as App
import Import

SUBDIR = os.path.join("build", "viewer")
GLB = "scene.glb"
JSON = "scene.json"


def enabled():
    """FREECAD_MCP_VIEWER=0 turns the live viewer (and this export) off."""
    return os.environ.get("FREECAD_MCP_VIEWER", "1").strip().lower() not in ("0", "off", "false", "no")


def tolerance(shape):
    """Chord tolerance in mm: fine enough that a Ø3 hole looks round, coarse enough for
    big assemblies."""
    return min(0.05, max(0.005, shape.BoundBox.DiagonalLength * 2e-4))


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def write(pdir, items, refs, info):
    """Write build/viewer/scene.glb and scene.json for a build.

    items: render.Item list (label, shape in global coordinates, colour 0–1);
    refs: one bool per item, true for reference parts; info: extra fields for scene.json
    (rev, ok, summary, errors, checks …). Returns the scene.json path."""
    out = os.path.join(pdir, SUBDIR)
    os.makedirs(out, exist_ok=True)
    parts = []
    has_glb = False
    if items:
        doc = App.newDocument("viewer_scene")
        try:
            feats = []
            for i, (it, is_ref) in enumerate(zip(items, refs)):
                s = it.shape.copy()
                s.tessellate(tolerance(s))
                f = doc.addObject("Part::Feature", "p%d" % i)
                f.Label = "p%d" % i
                f.Shape = s
                feats.append(f)
                parts.append({"node": "p%d" % i, "label": it.label, "ref": bool(is_ref),
                              "colour": [round(float(c), 4) for c in it.colour]})
            tmp = os.path.join(out, ".scene.tmp.glb")       # the exporter picks the format by extension
            Import.export(feats, tmp)
            os.replace(tmp, os.path.join(out, GLB))
            has_glb = True
        finally:
            App.closeDocument(doc.Name)
    t = time.time()
    path = os.path.join(out, JSON)
    data = dict(info)
    data.update({"version": "%s-%d" % (info.get("rev") or "r", int(t * 1000)), "written": t,
                 "glb": GLB if has_glb else None, "glb_rev": info.get("rev") if has_glb else None,
                 "parts": parts, "units": "mm"})
    if not has_glb:
        # a build that made no geometry (e.g. model.py failed early): keep showing the last model
        try:
            with open(path) as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = {}
        if prev.get("glb") and os.path.isfile(os.path.join(out, GLB)):
            data.update(glb=GLB, glb_rev=prev.get("glb_rev") or prev.get("rev"), parts=prev.get("parts", []))
    _write_json(path, data)
    return path
