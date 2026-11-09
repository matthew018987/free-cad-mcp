# SPDX-License-Identifier: Apache-2.0
"""The live viewer's scene, written by every build inside the FreeCAD worker.

build/viewer/scene.glb holds one node per part ("p0", "p1", …), exported by
FreeCAD's own glTF writer (OCCT). Shapes are tessellated first, because a
console session has no view providers to do it. scene.json names each node
(label, colour, reference or made part) and carries the build status: revision,
errors and checks. viewer.write() then turns them into the self-contained page
<name>.html and the scene.js / model.js feed an open page follows.

Every file is written to a temporary name and then renamed, so a page never
reads half a file. The model goes first, then the status that names it."""
import json
import os
import time

import FreeCAD as App
import Import

import viewer

SUBDIR = os.path.join("build", "viewer")
GLB = "scene.glb"
JSON = "scene.json"
enabled = viewer.enabled


def tolerance(shape):
    """Chord tolerance in mm: fine enough that a Ø3 hole looks round, coarse enough for
    big assemblies."""
    return min(0.05, max(0.005, shape.BoundBox.DiagonalLength * 2e-4))


def write(pdir, items, refs, info):
    """Write build/viewer/ for a build: scene.glb, scene.json and the viewer page.

    items: render.Item list (label, shape in global coordinates, colour 0–1);
    refs: one bool per item, true for reference parts; info: extra fields for scene.json
    (rev, ok, summary, errors, checks …). Returns the page's path."""
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
    version = "%s-%d" % (info.get("rev") or "r", int(t * 1000))
    data.update({"version": version, "written": t, "glb": GLB if has_glb else None,
                 "glb_rev": info.get("rev") if has_glb else None, "model": version if has_glb else None,
                 "parts": parts, "units": "mm"})
    if not has_glb:
        # a build that made no geometry (e.g. model.py failed early): keep showing the last model
        try:
            with open(path) as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = {}
        if prev.get("glb") and os.path.isfile(os.path.join(out, GLB)):
            data.update(glb=GLB, glb_rev=prev.get("glb_rev") or prev.get("rev"),
                        model=prev.get("model") or prev.get("version"), parts=prev.get("parts", []))
    viewer.write_text(path,json.dumps(data, ensure_ascii=False, default=str))
    return viewer.write(out, data, os.path.join(out, GLB) if data.get("glb") else None, model_changed=has_glb)
