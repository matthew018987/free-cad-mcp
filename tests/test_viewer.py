#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""server/viewer.py (the self-contained viewer page) and, with --real, the page a real
build writes (server/scene.py through the worker).

Part 1 runs viewer.py in-process: the bundle (three.js + viewer.js as one classic
script, compiled by macOS's JavaScriptCore when osascript is there), the page and
its inline data, the scene.js / model.js feed and escaping. It then starts the
server (no FreeCAD needed) for the view a chat app shows inline with each build
(MCP App): build names the ui:// resource, resources/read returns the page with no
build in it, and viewer_data hands that page the scene and the model in chunks.
Part 2 (--real) drives the real server over stdio: a build writes
build/viewer/<name>.html with its feed, the reply names it once, rebuilds, variants
and failed builds update it as they should, open_in_gui(viewer=true) refuses a
project with no page yet, and viewer_data returns the real build.

    python3 tests/test_viewer.py            # part 1
    python3 tests/test_viewer.py --real     # both parts
"""
import base64
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
from mcp_client import Client, check, fails  # noqa: E402
import viewer  # noqa: E402

tmp = tempfile.mkdtemp(prefix="viewer_test_")


def inline(html, ident):
    m = re.search(r'<script type="[^"]+" id="%s">(.*?)</script>' % ident, html, re.S)
    return m.group(1) if m else None


def feed(path, fn):
    """The arguments of scene.js / model.js: fn(<json>[, "<base64>"]);"""
    text = open(path).read()
    m = re.match(r'^%s\((.*?)(?:, "([A-Za-z0-9+/=]*)")?\);\n$' % fn, text, re.S)
    return (json.loads(m.group(1)), m.group(2)) if m else (None, None)


print("viewer page (part 1)")
try:
    b = viewer.bundle()
    check("the bundle has no import/export left (a classic script runs from a local file)",
          not re.search(r"(?m)^\s*(?:import|export)\s*[{*\w]", b), re.findall(r"(?m)^\s*(?:import|export).{0,60}", b)[:3])
    check("…defines every module, three.js first", [m for m in re.findall(r'__fcmcpModules\["([^"]+)"\] = \(function', b)]
          == [s for s, _ in viewer.MODULES])
    check("…starts with three.js's MIT licence", b.startswith("/*! three.js r160") and "The MIT License" in b[:2000]
          and "three.js authors" in b[:2000])
    check("…and nothing that would end the inline <script> early", "</script" not in b.lower() and "<!--" not in b)
    lic = open(os.path.join(viewer.THREE, "LICENSE")).read()
    check("three.js's licence file ships with the vendored files", "The MIT License" in lic)
    if shutil.which("osascript"):
        js = os.path.join(tmp, "bundle.js")
        with open(js, "w") as fh:
            fh.write(b)
        jxa = os.path.join(tmp, "compile.js")
        with open(jxa, "w") as fh:
            fh.write("ObjC.import('Foundation');\nfunction run(argv) {\n  new Function($.NSString."
                     "stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js);\n"
                     "  return 'ok';\n}\n")
        r = subprocess.run(["osascript", "-l", "JavaScript", jxa, js], capture_output=True, text=True, timeout=120)
        check("…and it compiles (JavaScriptCore)", r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-400:])

    out = os.path.join(tmp, "p", "build", "viewer")
    os.makedirs(out)
    glb = os.path.join(out, "scene.glb")
    data = b"glTF\x02\x00\x00\x00" + bytes(range(256)) * 4
    with open(glb, "wb") as fh:
        fh.write(data)
    evil = "</script><script>alert(1)</script>"
    scene = {"name": "Bracket v2", "rev": "r0003", "ok": True, "version": "r0003-1", "model": "r0003-1",
             "glb": "scene.glb", "glb_rev": "r0003", "checks": [{"id": "seat", "ok": True, "value": "0.35 mm"}],
             "parts": [{"node": "p0", "label": evil, "ref": False, "colour": [0.5, 0.5, 0.5]}],
             "params": {"note": "a\u2028b <!-- c"}}
    page = viewer.write(out, scene, glb)
    check("write() makes <name>.html with a safe file name", os.path.basename(page) == "Bracket_v2.html", page)
    html = open(page).read()
    outside = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    check("the page loads nothing from elsewhere (no src/href except data:, no import map, no modules)",
          not re.search(r"""(?:src|href)\s*=\s*["'](?!data:)""", outside) and "importmap" not in html
          and 'type="module"' not in html, re.findall(r"(?:src|href)=\S+", outside))
    check("…and has exactly its four scripts: scene, model, GLB, code",
          len(re.findall(r"<script\b", html)) == 4 and html.count("</script>") == 4,
          re.findall(r"<script\b[^>]*>", html))
    check("the build's status is inline", json.loads(inline(html, "fcmcp-scene")) == json.loads(json.dumps(scene)))
    m = json.loads(inline(html, "fcmcp-model") or "{}")
    check("…with the model's version, revision and parts", m.get("version") == "r0003-1" and m.get("rev") == "r0003"
          and m.get("parts", [{}])[0].get("label") == evil, m)
    check("…and the GLB, byte for byte", base64.b64decode(inline(html, "fcmcp-glb") or "") == data)
    check("a label with </script> stays inside its JSON", evil not in html and "\\u003c/script>" in html)
    st, _ = feed(os.path.join(out, "scene.js"), "fcmcpScene")
    check("scene.js calls fcmcpScene(status)", st == json.loads(json.dumps(scene)), open(os.path.join(out, "scene.js")).read()[:200])
    check("…with U+2028 escaped (a valid script)", "\u2028" not in open(os.path.join(out, "scene.js")).read())
    mm, b64 = feed(os.path.join(out, "model.js"), "fcmcpModel")
    check("model.js calls fcmcpModel(model, base64)", mm == m and base64.b64decode(b64 or "") == data)

    mtime = os.path.getmtime(os.path.join(out, "model.js"))
    os.utime(os.path.join(out, "model.js"), (mtime - 100, mtime - 100))
    scene2 = dict(scene, rev="r0004", version="r0004-1", ok=False, glb_rev="r0003", script_error="boom")
    viewer.write(out, scene2, glb, model_changed=False)
    check("a build that keeps the model leaves model.js alone",
          os.path.getmtime(os.path.join(out, "model.js")) == mtime - 100)
    st, _ = feed(os.path.join(out, "scene.js"), "fcmcpScene")
    html = open(page).read()
    check("…but updates scene.js and the page", st.get("rev") == "r0004" and st.get("script_error") == "boom"
          and json.loads(inline(html, "fcmcp-scene"))["rev"] == "r0004" and inline(html, "fcmcp-glb"))
    viewer.write(out, dict(scene, rev="r0005", version="r0005-1", model=None, glb=None, parts=[]), None)
    html = open(page).read()
    check("a scene with no model writes a page without model data", inline(html, "fcmcp-model") is None
          and inline(html, "fcmcp-glb") is None and json.loads(inline(html, "fcmcp-scene"))["rev"] == "r0005")
    check("find_page() finds the project's page", viewer.find_page(os.path.join(tmp, "p")) == page)
    check("…and None for a project without one", viewer.find_page(tmp) is None)
    os.environ["FREECAD_MCP_VIEWER"] = "0"
    check("FREECAD_MCP_VIEWER=0 turns the viewer off", not viewer.enabled())
    del os.environ["FREECAD_MCP_VIEWER"]
    check("…which is on by default", viewer.enabled())
except Exception:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("part 1 ran to the end", False)

VIEW = "ui://freecad/viewer.html"
MIME = "text/html;profile=mcp-app"
UI_CLIENT = {"protocolVersion": "2025-06-18", "capabilities": {"extensions": {"io.modelcontextprotocol/ui":
                                                                              {"mimeTypes": [MIME]}}}}


def viewer_data(c, **args):
    """(is an error, the reply's JSON or its error text)"""
    err, txt, _ = c.tool("viewer_data", args)
    return err, (txt if err else json.loads(txt))


def model_bytes(c, project, version):
    """The model as the inline view fetches it: chunk after chunk, base64 joined before decoding."""
    parts, offset, total, calls = [], 0, 1, 0
    while offset < total and calls < 50:
        err, d = viewer_data(c, project=project, model=version, offset=offset)
        if err or d.get("stale") or not d.get("data"):
            return None, calls
        parts.append(d["data"])
        offset, total, calls = d["next"], d["total"], calls + 1
    return base64.b64decode("".join(parts)), calls


print("the view inline in a chat (MCP App)")
try:
    app = viewer.app_page()
    check("app_page() is the page with no build in it, marked as the app", inline(app, "fcmcp-app") == "true"
          and inline(app, "fcmcp-scene") is None and inline(app, "fcmcp-glb") is None
          and len(re.findall(r"<script\b", app)) == 2, re.findall(r"<script\b[^>]*>", app))
    outside = re.sub(r"<script\b.*?</script>", "", app, flags=re.S)
    check("…that loads nothing from elsewhere (a chat app frames it with no network)",
          not re.search(r"""(?:src|href)\s*=\s*["'](?!data:)""", outside) and 'type="module"' not in app)
    check("a build's own page is not marked as the app", inline(html, "fcmcp-app") is None)

    ahome = os.path.join(tmp, "app")
    vdir = os.path.join(ahome, "plate", "build", "viewer")
    os.makedirs(vdir)
    os.makedirs(os.path.join(ahome, "unbuilt"))
    big = b"glTF" + bytes(range(256)) * (viewer.CHUNK // 256 + 5)        # more than one chunk
    with open(os.path.join(vdir, "scene.glb"), "wb") as fh:
        fh.write(big)
    sc = dict(scene, name="plate")
    with open(os.path.join(vdir, "scene.json"), "w") as fh:
        json.dump(sc, fh)
    check("read_scene() reads the project's scene.json", viewer.read_scene(os.path.join(ahome, "plate")) == sc)
    check("…and is None for a project with no build", viewer.read_scene(os.path.join(ahome, "unbuilt")) is None)

    c = Client(ahome)
    try:
        r = c.rpc("initialize", UI_CLIENT)["result"]
        check("the server offers resources", "resources" in r["capabilities"], r["capabilities"])
        tools = {t["name"]: t for t in c.rpc("tools/list")["result"]["tools"]}
        meta = tools["build"].get("_meta", {})
        check("build names the view's ui:// resource (both spellings of the key)",
              meta.get("ui", {}).get("resourceUri") == VIEW and meta.get("ui/resourceUri") == VIEW, meta)
        check("viewer_data is for the app, not for Claude",
              tools.get("viewer_data", {}).get("_meta") == {"ui": {"visibility": ["app"]}}
              and tools["viewer_data"]["description"].startswith("Not for Claude"), tools.get("viewer_data"))
        check("no other tool shows a view", [n for n, t in tools.items() if "_meta" in t] == ["build", "viewer_data"],
              [n for n, t in tools.items() if "_meta" in t])
        res = c.rpc("resources/list")["result"]["resources"]
        check("resources/list has the view as an MCP App page", len(res) == 1 and res[0]["uri"] == VIEW
              and res[0]["mimeType"] == MIME, res)
        body = c.rpc("resources/read", {"uri": VIEW})["result"]["contents"]
        check("resources/read returns app_page()", len(body) == 1 and body[0]["uri"] == VIEW
              and body[0]["mimeType"] == MIME and body[0]["text"] == app, [b.get("mimeType") for b in body])
        r = c.rpc("resources/read", {"uri": "ui://freecad/other.html"})
        check("…and an error for any other resource", r.get("error", {}).get("code") == -32002, r)

        err, d = viewer_data(c, project="plate")
        check("viewer_data returns the project's scene", not err and d == {"scene": sc}, d)
        err, d = viewer_data(c, project="plate", have=sc["version"])
        check("…only its version to a view that already shows it", not err and d == {"version": sc["version"]}, d)
        err, d = viewer_data(c, project="plate", have="r0001-1")
        check("…and the scene again to a view that shows an older build", not err and d == {"scene": sc}, d)
        got, calls = model_bytes(c, "plate", sc["model"])
        check("the model comes in chunks that join into scene.glb", got == big and calls == 2, calls)
        err, d = viewer_data(c, project="plate", model="r0002-9")
        check("a model version that is no longer the build's is stale", not err and d == {"stale": True}, d)
        err, d = viewer_data(c, project="unbuilt")
        check("a project with no build has no scene", not err and d == {"scene": None}, d)
        err, d = viewer_data(c, project="nowhere")
        check("an unknown project is an error", err and "no project" in d, d)
    finally:
        c.close()

    c = Client(ahome, {"FREECAD_MCP_VIEWER": "0"})
    try:
        tools = {t["name"]: t for t in c.rpc("tools/list")["result"]["tools"]}
        check("FREECAD_MCP_VIEWER=0 leaves nothing of the inline view: no viewer_data, no view on build",
              "viewer_data" not in tools and "_meta" not in tools["build"], sorted(tools))
        check("…no resource", c.rpc("resources/list")["result"]["resources"] == []
              and "error" in c.rpc("resources/read", {"uri": VIEW}))
        err, d = viewer_data(c, project="plate")
        check("…and viewer_data says the view is off", err and "FREECAD_MCP_VIEWER=0" in d, d)
    finally:
        c.close()
except Exception:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("the inline view checks ran to the end", False)


def glb_json(path):
    with open(path, "rb") as fh:
        d = fh.read()
    if d[:4] != b"glTF":
        return None
    n = struct.unpack("<I", d[12:16])[0]
    return json.loads(d[20:20 + n])


if "--real" in sys.argv:
    print("real build (part 2)")
    rhome = os.path.join(tmp, "real")
    os.makedirs(rhome)
    shutil.copytree(os.path.join(ROOT, "examples", "nema17_bracket"), os.path.join(rhome, "nema17_bracket"),
                    ignore=shutil.ignore_patterns(".history", "build", "out", "release", "drawings"))
    c = Client(rhome)
    try:
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "return_image": False})
        check("build ok", not err and "BUILD ✓" in txt, txt[:600])
        vdir = os.path.join(rhome, "nema17_bracket", "build", "viewer")
        sj, gp = os.path.join(vdir, "scene.json"), os.path.join(vdir, "scene.glb")
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
        pages = glob.glob(os.path.join(vdir, "*.html"))
        check("…and the viewer page, named after the model", [os.path.basename(p) for p in pages]
              == ["nema17_bracket.html"], pages)
        html = open(pages[0]).read() if pages else ""
        check("the page carries this build and its model", json.loads(inline(html, "fcmcp-scene") or "{}").get("rev")
              == "r0001" and base64.b64decode(inline(html, "fcmcp-glb") or "") == open(gp, "rb").read())
        check("the build reply names the page once", "viewer   build/viewer/nema17_bracket.html" in txt, txt[-400:])
        m1, _ = feed(os.path.join(vdir, "model.js"), "fcmcpModel")
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "params": {"plate_h": 60},
                                       "return_image": False})
        st2, _ = feed(os.path.join(vdir, "scene.js"), "fcmcpScene")
        m2, _ = feed(os.path.join(vdir, "model.js"), "fcmcpModel")
        check("a rebuild writes a new scene.js and model.js (what an open page follows)", not err
              and st2.get("rev") == "r0002" and st2.get("params", {}).get("plate_h") == 60
              and m2.get("version") == st2.get("model") != (m1 or {}).get("version"), (st2.get("rev"), m2))
        check("…and does not repeat the viewer line", "viewer   build" not in txt, txt[-300:])
        before = open(os.path.join(vdir, "scene.js")).read()
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "variant": "tall", "params": {"plate_h": 80},
                                       "return_image": False})
        check("a variant build leaves the page and its feed alone",
              not err and open(os.path.join(vdir, "scene.js")).read() == before)
        mp = os.path.join(rhome, "nema17_bracket", "model.py")
        good = open(mp).read()
        with open(mp, "w") as fh:
            fh.write("raise ValueError('broken on purpose')\n" + good)
        model_js = open(os.path.join(vdir, "model.js")).read()
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "return_image": False})
        st4, _ = feed(os.path.join(vdir, "scene.js"), "fcmcpScene")
        html = open(os.path.join(vdir, "nema17_bracket.html")).read()
        check("a build that makes no geometry keeps the last model, says which, and carries the error",
              st4.get("ok") is False and st4.get("model") == m2.get("version") and st4.get("glb_rev") == "r0002"
              and "broken on purpose" in (st4.get("script_error") or "")
              and open(os.path.join(vdir, "model.js")).read() == model_js and inline(html, "fcmcp-glb"),
              {k: st4.get(k) for k in ("ok", "rev", "model", "glb_rev", "script_error")})
        with open(mp, "w") as fh:
            fh.write(good)
        err, txt, _ = c.tool("create_project", {"name": "fresh_part"})
        err, txt, _ = c.tool("open_in_gui", {"project": "fresh_part", "viewer": True})
        check("open_in_gui(viewer=true) before the first build says to build first", err and "build the project" in txt,
              txt)
        err, txt, _ = c.tool("build", {"project": "nema17_bracket", "return_image": False})
        scene = json.load(open(sj))
        err2, d = viewer_data(c, project="nema17_bracket")
        check("viewer_data returns the real build's scene", not err and not err2 and d.get("scene") == scene
              and scene.get("ok") is True, d if err2 else (d.get("scene") or {}).get("rev"))
        got, calls = model_bytes(c, "nema17_bracket", scene.get("model"))
        check("…and its model, byte for byte", got == open(gp, "rb").read(), calls)
        c2 = Client(rhome)
        try:
            c2.rpc("initialize", UI_CLIENT)
            err, txt, _ = c2.tool("build", {"project": "fresh_part", "return_image": False})
            check("to a chat app that shows MCP Apps, the first build says the view is inline",
                  not err and "inline with every build's result" in txt and "viewer   build" not in txt, txt[-500:])
        finally:
            c2.close()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("part 2 ran to the end", False)
    finally:
        c.close()

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d failure(s)" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
