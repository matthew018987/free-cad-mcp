#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""server/viewer.py (the live 3D viewer's web server) and, with --real, the scene a
real build writes (server/scene.py through the worker).

Part 1 runs a Viewer in-process against a temporary home with hand-made scene files:
endpoints, newest-project following, the Host check, path rules, port reuse and
takeover. Part 2 (--real) drives the real server over stdio: a build writes
build/viewer/scene.glb + scene.json, freecad_status and create_project report the
viewer link, and a second build changes the scene's version.

    python3 tests/test_viewer.py            # part 1
    python3 tests/test_viewer.py --real     # both parts
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
from mcp_client import check, fails  # noqa: E402
import viewer  # noqa: E402

tmp = tempfile.mkdtemp(prefix="viewer_test_")
home = os.path.join(tmp, "home")
outside = os.path.join(tmp, "outside")
os.makedirs(home)
os.makedirs(outside)


def make_scene(pdir, rev, glb=b"glTF-test", parts=("Bracket",)):
    d = os.path.join(pdir, "build", "viewer")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "scene.glb"), "wb") as fh:
        fh.write(glb)
    with open(os.path.join(d, "scene.json"), "w") as fh:
        json.dump({"rev": rev, "ok": True, "version": "%s-%d" % (rev, time.time() * 1000), "glb": "scene.glb",
                   "parts": [{"node": "p%d" % i, "label": p, "ref": False, "colour": [0.5, 0.5, 0.5]}
                             for i, p in enumerate(parts)], "checks": []}, fh)


def get(url, host=None):
    req = urllib.request.Request(url)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def resolver(key):
    """Like freecad_mcp.project_dir: a name under the home or a path, kept inside allowed roots."""
    p = os.path.abspath(os.path.join(home, os.path.expanduser(key)))
    if not (p.startswith(home + os.sep) or p.startswith(outside + os.sep)):
        raise ValueError("%s is outside the allowed roots" % p)
    if not os.path.isdir(p):
        raise ValueError("no project at %s" % p)
    return p


print("viewer server (part 1)")
try:
    v = viewer.Viewer(home, resolver, port=0)
    url = v.ensure()
    check("starts on 127.0.0.1 and returns its URL", url and url.startswith("http://127.0.0.1:"), url)
    check("ensure() again returns the same URL", v.ensure() == url)
    code, body, ctype = get(url)
    check("/ serves the page", code == 200 and b"FreeCAD Live View" in body and ctype.startswith("text/html"),
          (code, body[:80]))
    check("…which loads three.js from this server, not a CDN (works offline)",
          b'"/vendor/three/build/three.module.min.js"' in body and b"cdn.jsdelivr" not in body and b"unpkg" not in body)
    code, js, ctype = get(url + "vendor/three/build/three.module.min.js")
    check("/vendor serves the bundled three.js as JavaScript", code == 200 and len(js) > 600000
          and ctype.startswith("text/javascript"), (code, len(js), ctype))
    code, js, _ = get(url + "vendor/three/examples/jsm/loaders/GLTFLoader.js")
    check("…and the add-ons the page imports", code == 200 and b"class GLTFLoader" in js, code)
    lic = open(os.path.join(viewer.VENDOR, "three", "LICENSE")).read()
    check("three.js's MIT licence ships next to it", "The MIT License" in lic and "three.js authors" in lic)
    for bad in ("vendor/three/LICENSE", "vendor/../viewer.py", "vendor/three/../../../viewer.py",
                "vendor/%2e%2e/%2e%2e/viewer.py", "vendor/three/build/missing.js"):
        code, _, _ = get(url + bad)
        check("…but nothing else under /vendor: %s → 404" % bad, code == 404, code)
    code, body, _ = get(url + "api/ping")
    info = json.loads(body)
    check("/api/ping names the app and home", info.get("app") == viewer.APP and info.get("home") == home, info)

    code, body, _ = get(url + "api/scene")
    data = json.loads(body)
    check("no build yet → waiting (200, so an open page logs no errors)", code == 200 and data.get("waiting") is True
          and data.get("project") is None, (code, body))

    os.makedirs(os.path.join(home, "plate"))
    code, body, _ = get(url + "api/scene?project=plate")
    data = json.loads(body)
    check("a project without a scene → waiting", code == 200 and data.get("waiting") is True
          and data.get("project") == "plate", data)

    make_scene(os.path.join(home, "plate"), "r0001")
    time.sleep(0.02)
    os.makedirs(os.path.join(home, "shaft"))
    make_scene(os.path.join(home, "shaft"), "r0004", glb=b"glTF-shaft", parts=("Shaft", "Bearing"))
    code, body, _ = get(url + "api/scene")
    data = json.loads(body)
    check("no project in the URL → the newest built project", code == 200 and data.get("project") == "shaft"
          and data.get("rev") == "r0004" and data.get("mtime"), data)
    code, body, ctype = get(url + "api/glb?project=plate&v=1")
    check("/api/glb returns the project's GLB bytes", code == 200 and body == b"glTF-test"
          and ctype == "model/gltf-binary", (code, body, ctype))
    code, body, _ = get(url + "api/projects")
    names = [p["project"] for p in json.loads(body)["projects"]]
    check("/api/projects lists built projects, newest first", names == ["shaft", "plate"], names)

    time.sleep(0.02)
    make_scene(os.path.join(home, "plate"), "r0002")
    data = json.loads(get(url + "api/scene")[1])
    check("a rebuild makes that project the newest", data.get("project") == "plate" and data.get("rev") == "r0002",
          data)

    ext = os.path.join(outside, "far_project")
    os.makedirs(ext)
    time.sleep(0.02)
    make_scene(ext, "r0009")
    data = json.loads(get(url + "api/scene")[1])
    check("a project outside the home is not followed until this process builds it", data.get("project") == "plate",
          data)
    v.note_built(ext)
    data = json.loads(get(url + "api/scene")[1])
    check("…then it is (keyed by its path)", data.get("project") == ext and data.get("rev") == "r0009", data)
    check("link() names the project", v.link(ext).endswith("?project=" + urllib.request.quote(ext, safe="")),
          v.link(ext))
    check("link() of a home project uses its folder name", v.link(os.path.join(home, "plate")).endswith("?project=plate"))

    code, body, _ = get(url + "api/scene?project=" + urllib.request.quote("../../etc"))
    check("a key outside the allowed roots → 404, nothing read", code == 404 and b"outside" in body, (code, body))
    code, body, _ = get(url + "api/glb?project=" + urllib.request.quote(os.path.join(home, "plate", "build")))
    check("only build/viewer/scene.glb inside a project is served", code == 404, (code, body))
    code, _, _ = get(url + "etc/passwd")
    check("unknown paths → 404", code == 404)

    port = int(url.rstrip("/").rsplit(":", 1)[1])
    code, _, _ = get(url + "api/ping", host="evil.example:%d" % port)
    check("a non-local Host header is refused (DNS rebinding)", code == 403, code)
    code, _, _ = get(url + "api/ping", host="localhost:%d" % port)
    check("Host localhost is accepted", code == 200, code)

    v2 = viewer.Viewer(home, resolver, port=port)
    check("a second server for the same home on a taken port reuses the first",
          v2.ensure() == url and not v2.owner, (v2.url, v2.owner))
    other_home = os.path.join(tmp, "other_home")
    os.makedirs(other_home)
    v3 = viewer.Viewer(other_home, resolver, port=port)
    u3 = v3.ensure()
    check("a server for another home does not reuse it — it takes a free port", u3 and u3 != url and v3.owner, u3)
    v3.stop()
    v.stop()
    check("after the first server stops, the reusing one takes the port over",
          v2.ensure() == url and v2.owner, (v2.url, v2.owner))
    check("…and serves the same scenes", json.loads(get(url + "api/scene?project=shaft")[1]).get("rev") == "r0004")
    v2.stop()

    os.environ["FREECAD_MCP_VIEWER"] = "0"
    check("FREECAD_MCP_VIEWER=0 turns the viewer off", viewer.get(home, resolver) is None)
    del os.environ["FREECAD_MCP_VIEWER"]
except Exception:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("part 1 ran to the end", False)


def glb_json(path):
    with open(path, "rb") as fh:
        d = fh.read()
    if d[:4] != b"glTF":
        return None
    n = struct.unpack("<I", d[12:16])[0]
    return json.loads(d[20:20 + n])


if "--real" in sys.argv:
    print("real build (part 2)")
    from mcp_client import Client
    rhome = os.path.join(tmp, "real")
    os.makedirs(rhome)
    shutil.copytree(os.path.join(ROOT, "examples", "nema17_bracket"), os.path.join(rhome, "nema17_bracket"),
                    ignore=shutil.ignore_patterns(".history", "build", "out", "release", "drawings"))
    os.environ["FREECAD_MCP_VIEWER_PORT"] = "0"
    c = Client(rhome)
    try:
        err, txt, _ = c.tool("freecad_status", {})
        st = json.loads(txt) if not err else {}
        vurl = st.get("viewer", "")
        check("freecad_status reports the viewer link", not err and vurl.startswith("http://127.0.0.1:"), txt[:400])
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "return_image": False})
        check("build ok", not err and "BUILD ✓" in txt, txt[:600])
        sj = os.path.join(rhome, "nema17_bracket", "build", "viewer", "scene.json")
        gp = os.path.join(rhome, "nema17_bracket", "build", "viewer", "scene.glb")
        scene = json.load(open(sj)) if os.path.isfile(sj) else {}
        check("the build wrote scene.json with its revision, status and checks",
              scene.get("rev") == "r0001" and scene.get("ok") is True and len(scene.get("checks", [])) == 5, scene)
        labels = {p["label"]: p for p in scene.get("parts", [])}
        check("…naming each part with its colour and role", set(labels) == {"Bracket", "Motor"}
              and labels["Motor"]["ref"] is True and labels["Bracket"]["ref"] is False, labels)
        gj = glb_json(gp) if os.path.isfile(gp) else None
        nodes = {n.get("name"): n for n in (gj or {}).get("nodes", [])}
        check("scene.glb is glTF with a meshed node per part", gj is not None
              and all(nodes.get(p["node"], {}).get("mesh") is not None for p in scene.get("parts", [])), nodes)
        check("the build reply links the viewer once", "viewer   http://127.0.0.1:" in txt, txt[-400:])
        code, body, _ = get(vurl + "api/scene")
        check("the running server follows the build", code == 200 and json.loads(body).get("rev") == "r0001",
              (code, body[:200]))
        v1 = scene.get("version")
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "params": {"plate_h": 60},
                                       "return_image": False})
        scene2 = json.load(open(sj))
        check("a rebuild writes a new scene version (what the page polls for)", not err and scene2.get("version") != v1
              and scene2.get("rev") == "r0002" and scene2.get("params", {}).get("plate_h") == 60, scene2.get("rev"))
        check("…and does not repeat the viewer line", "viewer   http" not in txt, txt[-300:])
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "variant": "tall", "params": {"plate_h": 80},
                                       "return_image": False})
        scene3 = json.load(open(sj))
        check("a variant build leaves the main scene alone", not err and scene3.get("version") == scene2.get("version"),
              scene3.get("rev"))
        mp = os.path.join(rhome, "nema17_bracket", "model.py")
        good = open(mp).read()
        with open(mp, "w") as fh:
            fh.write("raise ValueError('broken on purpose')\n" + good)
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "return_image": False})
        scene4 = json.load(open(sj))
        check("a build that makes no geometry keeps the last model, says which, and carries the error",
              scene4.get("ok") is False and scene4.get("glb") == "scene.glb" and scene4.get("glb_rev") == "r0002"
              and scene4.get("rev") != "r0002" and "broken on purpose" in (scene4.get("script_error") or "")
              and [p["label"] for p in scene4.get("parts", [])] == [p["label"] for p in scene2.get("parts", [])],
              {k: scene4.get(k) for k in ("ok", "rev", "glb", "glb_rev", "script_error")})
        with open(mp, "w") as fh:
            fh.write(good)
        err, txt, _ = c.tool("create_project", {"name": "fresh_part"})
        check("create_project gives the viewer link for the new project",
              not err and "?project=fresh_part" in txt, txt[-500:])
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("part 2 ran to the end", False)
    finally:
        c.close()

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
