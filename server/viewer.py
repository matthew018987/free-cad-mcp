# SPDX-License-Identifier: Apache-2.0
"""Live 3D viewer page (stdlib only): one self-contained HTML file per build.

Every main build writes into <project>/build/viewer/ (scene.py exports the glTF
inside the FreeCAD worker, then calls write() here):

  <name>.html   the viewer: three.js, the page's code and the model, all inline, so it
                opens from Finder, in Cowork's file preview or attached to an email
  scene.js      the build's status, checks and parameters
  model.js      the model (GLB, base64) with its part list; rewritten only when it changes
  scene.glb, scene.json   the same data, for other tools

An open page re-reads scene.js once a second through a <script> tag (browsers
allow that from a local file, where fetch() is blocked) and loads model.js when
the model changed, so a tab kept open follows every build and keeps its camera.
Where those files can't be read (a preview, an emailed copy) the page shows the
build it was written with. Nothing listens on a port.

Browsers refuse ES module files from a local file, so bundle() turns three.js's
modules and viewer/viewer.js into one classic script: each module becomes a
function returning its exports, imports become lookups."""
import base64
import glob
import json
import os
import posixpath
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(HERE, "viewer")
TEMPLATE = os.path.join(PAGE_DIR, "index.html")
THREE = os.path.join(PAGE_DIR, "vendor", "three")        # three.js r160, unmodified (MIT, see LICENSE there)
MODULES = [                                              # in dependency order
    ("three", "vendor/three/build/three.module.min.js"),
    ("three/addons/utils/BufferGeometryUtils.js", "vendor/three/examples/jsm/utils/BufferGeometryUtils.js"),
    ("three/addons/controls/OrbitControls.js", "vendor/three/examples/jsm/controls/OrbitControls.js"),
    ("three/addons/loaders/GLTFLoader.js", "vendor/three/examples/jsm/loaders/GLTFLoader.js"),
    ("viewer", "viewer.js"),
]
SCENE_JS, MODEL_JS = "scene.js", "model.js"
_SPECS = {s for s, _ in MODULES}
REG = "__fcmcpModules"                   # the bundle's module table (no module uses this name)


def enabled():
    """FREECAD_MCP_VIEWER=0 turns the viewer page (and the scene export) off."""
    return os.environ.get("FREECAD_MCP_VIEWER", "1").strip().lower() not in ("0", "off", "false", "no")


# ------------------------------------------------------------ bundling

_IMPORT = re.compile(r"""^[ \t]*import\s+(?:\*\s+as\s+(\w+)|\{([^}]*)\})\s+from\s+['"]([^'"]+)['"][ \t]*;?""", re.M)
_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}\s*;?")
_EXPORT_DECL = re.compile(r"^([ \t]*)export\s+((?:async\s+)?(?:function\*?|class|const|let|var)\s+([\w$]+))", re.M)


def _names(spec):
    """'a, b as c' → [('a', 'a'), ('b', 'c')] as (local or source name, bound name)."""
    out = []
    for part in spec.split(","):
        bits = part.split()
        if len(bits) == 1:
            out.append((bits[0], bits[0]))
        elif len(bits) == 3 and bits[1] == "as":
            out.append((bits[0], bits[2]))
        elif bits:
            raise ValueError("cannot read import/export list entry %r" % part.strip())
    return out


def _as_function(spec, src):
    """An ES module's source as `(function () { … return {exports}; })()`; imports read REG[…]."""
    def imp(m):
        star, names, frm = m.group(1), m.group(2), m.group(3)
        if frm.startswith("."):
            frm = posixpath.normpath(posixpath.join(posixpath.dirname(spec), frm))
        if frm not in _SPECS:
            raise ValueError("%s imports %s, which is not bundled" % (spec, frm))
        ref = "%s[%s]" % (REG, json.dumps(frm))
        if star:
            return "const %s = %s;" % (star, ref)
        return "const { %s } = %s;" % (", ".join(a if a == b else "%s: %s" % (a, b) for a, b in _names(names)), ref)
    exports = []

    def exp_list(m):
        exports.extend(_names(m.group(1)))
        return ""

    def exp_decl(m):
        exports.append((m.group(3), m.group(3)))
        return m.group(1) + m.group(2)
    src = _IMPORT.sub(imp, src)
    src = _EXPORT_DECL.sub(exp_decl, src)
    src = _EXPORT_LIST.sub(exp_list, src)
    body = ", ".join(json.dumps(name) + ": " + local for local, name in exports)
    return "%s[%s] = (function () {\n%s\nreturn { %s };\n})();\n" % (REG, json.dumps(spec), src, body)


def bundle():
    """three.js, its add-ons and viewer.js as one classic script, three.js's licence on top."""
    with open(os.path.join(THREE, "LICENSE")) as fh:
        lic = fh.read().strip()
    parts = ["/*! three.js r160 (https://threejs.org), bundled unmodified apart from module wrapping.\n\n%s\n*/\n"
             % lic.replace("*/", "* /"),
             "(function () {\n\"use strict\";\nconst %s = {};\n" % REG]
    for spec, rel in MODULES:
        with open(os.path.join(PAGE_DIR, rel)) as fh:
            parts.append(_as_function(spec, fh.read()))
    parts.append("})();\n")
    return "".join(parts).replace("</script", "<\\/script")


# ------------------------------------------------------------ the page and the live feed

def _json(data):
    """JSON safe inside <script> and as a JavaScript expression (no '<', no raw U+2028)."""
    return json.dumps(data, ensure_ascii=True, default=str).replace("<", "\\u003c")


def page(scene, model=None, glb=None):
    """The viewer HTML for one build: scene = the build's status (scene.json), model = {version,
    rev, parts} of the GLB bytes `glb`, or None when there is no model to show."""
    with open(TEMPLATE) as fh:
        html = fh.read()
    data = ['<script type="application/json" id="fcmcp-scene">%s</script>' % _json(scene)]
    if model and glb:
        data.append('<script type="application/json" id="fcmcp-model">%s</script>' % _json(model))
        data.append('<script type="text/plain" id="fcmcp-glb">%s</script>' % base64.b64encode(glb).decode("ascii"))
    for marker, text in (("<!--fcmcp:data-->", "\n".join(data)), ("<!--fcmcp:script-->",
                                                                  "<script>\n%s</script>" % bundle())):
        if html.count(marker) != 1:
            raise ValueError("viewer/index.html must contain %s once" % marker)
        html = html.replace(marker, text)
    return html


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def page_name(name):
    return (re.sub(r"[^\w.-]+", "_", name or "").strip("._") or "viewer") + ".html"


def write(out, scene, glb_path=None, model_changed=True):
    """Write model.js (when the model changed or is missing), scene.js and <name>.html into
    `out` from the build's scene.json data and GLB. Returns the page's path."""
    glb, model = None, None
    if glb_path and scene.get("model"):
        with open(glb_path, "rb") as fh:
            glb = fh.read()
        model = {"version": scene["model"], "rev": scene.get("glb_rev"), "parts": scene.get("parts", [])}
        mp = os.path.join(out, MODEL_JS)
        if model_changed or not os.path.isfile(mp):
            write_text(mp, "fcmcpModel(%s, \"%s\");\n" % (_json(model), base64.b64encode(glb).decode("ascii")))
    write_text(os.path.join(out, SCENE_JS), "fcmcpScene(%s);\n" % _json(scene))
    path = os.path.join(out, page_name(scene.get("name")))
    write_text(path, page(scene, model, glb))
    return path


def find_page(pdir):
    """The newest viewer page a build wrote in the project, or None."""
    pages = glob.glob(os.path.join(glob.escape(pdir), "build", "viewer", "*.html"))
    return max(pages, key=os.path.getmtime) if pages else None
