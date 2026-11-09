#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
M6.0 spike (docs/tolerancing-plan.md §8): how FreeCAD 1.1 TechDraw can carry
GD&T, surface texture symbols and inspection balloons.

Run it on the Mac; it starts FreeCAD's GUI in the background with a private
user.cfg, exactly like the drawing worker, so your FreeCAD settings are never
touched (about 20 s):

    /Applications/FreeCAD.app/Contents/Resources/bin/python docs/prototypes/gdt_prototype.py [out_dir]

(any Python 3 works as the launcher; out_dir defaults to ./gdt_spike_out).
It prints a summary and writes into out_dir:

  findings.json        every measurement: send it back
  spike.pdf            the test sheet as a shop would get it: look at it
  spike.svg/.png/.dxf  the other exports
  crop_*.png           zoomed crops, a red cross where each symbol should touch
  spike.FCStd          the drawing, saved and reopened once
  frame.svg, datum.svg, surface.svg   the generated symbols on their own

Experiments
  E1  TechDraw object types: is there a native frame or datum object?
  E2  a basic dimension (TheoreticalExact) and the tolerance properties
  E3  DrawViewSymbol with the frame, datum and surface SVGs of server/
      tolerancing.py: where X/Y put a symbol (hypothesis H1: its centre;
      H2: its top-left corner), at what scale, what survives each export
  E4  DrawViewBalloon from Python: OriginX/Y and X/Y conventions, shapes
  E5  DrawLeaderLine + DrawRichAnno (the fallback for frames)
  E6  osifont coverage of the GD&T characters
  E7  save and reopen
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SERVER = os.path.join(ROOT, "server")
PAGE = (420.0, 297.0)              # A3 landscape
SCALE = 0.5                        # not 1:1, so view units and paper mm differ
TOKENS = {"frame": "0.137", "frame_K": "K", "datum": "W", "surface": "Ra 6.3", "basic": "100",
          "toleranced": "60", "balloon41": "41", "balloon42": "42", "balloon43": "43"}


# ======================================================================== launcher (any Python 3)

def find_gui():
    env = (os.environ.get("FREECAD_APP") or "").strip().rstrip("/")
    cands = []
    if env:
        cands.append(env)
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        main = os.path.join(d, "FreeCAD.app")
        if os.path.isdir(main):
            cands.append(main)
        try:
            cands += sorted((os.path.join(d, n) for n in os.listdir(d) if n.startswith("FreeCAD") and
                             n.endswith(".app") and os.path.join(d, n) != main), reverse=True)
        except OSError:
            pass
    for c in cands:
        m = re.match(r"^(.*?\.app)(/|$)", c)
        if m:
            g = os.path.join(m.group(1), "Contents", "Resources", "bin", "freecad")
            if os.path.isfile(g):
                return g
        elif os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("freecad") or shutil.which("FreeCAD")


USER_CFG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">
        <FCParamGroup Name="General">
          <FCBool Name="ShowSplasher" Value="0"/>
        </FCParamGroup>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""


def launch(out):
    gui = find_gui()
    if not gui:
        sys.exit("FreeCAD's GUI binary not found: install FreeCAD 1.x in /Applications or set FREECAD_APP")
    os.makedirs(out, exist_ok=True)
    run = tempfile.mkdtemp(prefix="gdt-spike-")
    try:
        cfg = os.path.join(run, "user.cfg")
        with open(cfg, "w") as fh:
            fh.write(USER_CFG)
        env = dict(os.environ)
        for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "FCMCP_REQ", "FCMCP_RESP"):
            env.pop(k, None)
        env.update(FCMCP_SPIKE_OUT=os.path.abspath(out), FCMCP_SERVER_DIR=SERVER,
                   FREECAD_USER_DATA=os.path.join(run, "user_data"))
        os.makedirs(env["FREECAD_USER_DATA"])
        if sys.platform == "darwin":
            env["QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"] = "1"      # no Dock icon, no focus taken
        cmd = [gui, "-u", cfg, os.path.abspath(__file__)]
        if shutil.which("perl"):
            cmd = ["perl", "-e", "alarm 300; exec @ARGV"] + cmd
        lock = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "gui", "gui.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        if os.path.isfile(os.path.join(out, "findings.json")):
            os.remove(os.path.join(out, "findings.json"))
        print("running %s (the server's GUI lock keeps drawing workers out of the way)…" % gui)
        t0 = time.time()
        with open(lock, "a") as lk:
            try:
                import fcntl
                fcntl.flock(lk, fcntl.LOCK_EX)
            except ImportError:
                pass
            r = subprocess.run(cmd, env=env, cwd=run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with open(os.path.join(out, "freecad.log"), "wb") as fh:
            fh.write(r.stdout)
        print("FreeCAD exited %s after %.1f s" % (r.returncode, time.time() - t0))
    finally:
        shutil.rmtree(run, ignore_errors=True)
    path = os.path.join(out, "findings.json")
    if not os.path.isfile(path):
        sys.exit("no findings.json: see %s" % os.path.join(out, "freecad.log"))
    with open(path) as fh:
        summary(json.load(fh), out)


def summary(F, out):
    ex = F.get("experiments", {})
    print("\nFreeCAD %s" % ".".join(F.get("freecad", [])[:3]))
    e1 = ex.get("E1_types") or {}
    print("E1 TechDraw types: %d; GD&T-like: %s" % (len(e1.get("techdraw", [])), ", ".join(e1.get("gdt_like", []))
                                                   or "none"))
    e2 = ex.get("E2_dimensions") or {}
    print("E2 TheoreticalExact property: %s; basic dimension value %s" % (e2.get("has_theoretical_exact"),
                                                                          e2.get("basic_value")))
    an = ex.get("analysis") or {}
    for name, s in sorted((an.get("symbols") or {}).items()):
        print("E3 %-8s found in SVG: %-5s  H1 error %s mm · H2 error %s mm · scale %s" % (
            name, s.get("found"), s.get("h1_error"), s.get("h2_error"), s.get("scale", "-")))
    print("E3 verdict: %s" % an.get("symbol_verdict", "-"))
    for name, b in sorted((an.get("balloons") or {}).items()):
        print("E4 %s found: %s; bubble at page %s; best X/Y model: %s" % (
            name, b.get("found"), b.get("page"), b.get("best_xy")))
    e4 = ex.get("E4_balloons") or {}
    print("E4 BubbleShape %s · EndType %s" % (e4.get("bubble_shapes"), e4.get("end_types")))
    e5 = ex.get("E5_leader_richanno") or {}
    print("E5 leader + rich annotation made: %s" % bool(e5.get("made")))
    e6 = ex.get("E6_fonts") or {}
    print("E6 osifont: %s; GD&T glyphs present: %s; missing: %s" % (
        e6.get("family"), "".join(e6.get("present", [])), "".join(e6.get("missing", [])) or "none"))
    e7 = ex.get("E7_reopen") or {}
    print("E7 after reopening: %s" % e7.get("summary"))
    dx = an.get("dxf") or {}
    print("DXF has the symbol texts: %s" % dx)
    if F.get("errors"):
        print("\nERRORS in: %s (details in findings.json)" % ", ".join(sorted(F["errors"])))
    print("\nLook at %s and the crop_*.png files (the red cross is where each symbol should touch); "
          "send back findings.json." % os.path.join(out, "spike.pdf"))


# ======================================================================== inside FreeCAD's GUI

def inside():
    out = os.environ["FCMCP_SPIKE_OUT"]
    F = {"started": time.time(), "experiments": {}, "errors": {}, "timings": {}, "files": {}}
    try:
        body(F, out, os.environ.get("FCMCP_SERVER_DIR") or SERVER)
    except Exception:  # noqa: BLE001
        F["errors"]["fatal"] = traceback.format_exc()[-4000:]
    finally:
        F["seconds"] = round(time.time() - F["started"], 2)
        with open(os.path.join(out, "findings.json"), "w") as fh:
            json.dump(F, fh, indent=1, default=str)
        sys.stdout.flush()
        os._exit(0)


def settings_groups(server):
    """The drawing worker's SETTINGS_GROUPS, read from its source (one list for both)."""
    with open(os.path.join(server, "drawing_worker.py")) as fh:
        src = fh.read()
    m = re.search(r"^SETTINGS_GROUPS = (.*?)\n\n", src, re.S | re.M)
    ns = {}
    exec("SETTINGS_GROUPS = " + m.group(1), ns)      # our own source: a list built from literals
    return ns["SETTINGS_GROUPS"]


def body(F, out, server):
    sys.path.insert(0, server)
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtGui, QtSvg, QtWidgets
    import tolerancing as tol
    V = App.Vector
    F["freecad"] = list(App.Version()[:4])

    def pump(n=5, dt=0.02):
        for _ in range(n):
            QtWidgets.QApplication.processEvents()
            time.sleep(dt)

    def val(q):
        return float(getattr(q, "Value", q))

    def step(name, fn):
        t0 = time.time()
        try:
            F["experiments"][name] = fn()
        except Exception:  # noqa: BLE001
            F["errors"][name] = traceback.format_exc()[-3000:]
        F["timings"][name] = round(time.time() - t0, 3)

    for g in settings_groups(server):           # before any view computes (the drawing worker's crash fix)
        App.ParamGet("User parameter:BaseApp/Preferences/" + g)
    dims = App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/Dimensions")
    dims.SetInt("StandardAndStyle", 1)
    dims.SetBool("ShowUnits", False)
    dims.SetFloat("FontSize", 3.5)
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/TechDraw/General").SetInt("ProjectionAngle", 1)
    mw = Gui.getMainWindow()
    if mw is not None:
        mw.setWindowOpacity(0.0)

    doc = App.newDocument("GdtSpike")

    def e1():
        types = sorted(t for t in doc.supportedTypes() if t.startswith("TechDraw::"))
        like = [t for t in types if re.search(r"(?i)datum|gdt|toleran|feature|frame|symbol|balloon|leader|rich|"
                                             r"weld|tile|surface", t)]
        return {"techdraw": types, "gdt_like": like}
    step("E1_types", e1)

    # a 100 × 60 × 10 plate: Ø22 bore at (50, 30), 4 × Ø5.5 at 10 mm from the edges
    shape = Part.makeBox(100, 60, 10)
    for x, y, d in [(50, 30, 22)] + [(x, y, 5.5) for x in (10, 90) for y in (10, 50)]:
        shape = shape.cut(Part.makeCylinder(d / 2.0, 12, V(x, y, -1)))
    plate = doc.addObject("Part::Feature", "Plate")
    plate.Shape = shape

    page = doc.addObject("TechDraw::DrawPage", "Spike_Sheet")
    tpl = doc.addObject("TechDraw::DrawSVGTemplate", "Spike_Template")
    tpl.Template = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO",
                                "A3_Landscape_ISO5457_minimal.svg")
    page.Template = tpl

    def add_view(name, direction, x, y):
        v = doc.addObject("TechDraw::DrawViewPart", "View_" + name)
        v.Label = name
        page.addView(v)
        v.Source = [plate]
        v.Direction = direction
        v.XDirection = V(1, 0, 0)
        v.ScaleType = "Custom"
        v.Scale = SCALE
        v.X, v.Y = x, y
        return v
    top = add_view("Top", V(0, 0, 1), 140, 200)
    front = add_view("Front", V(0, -1, 0), 140, 110)
    doc.recompute()
    t0 = time.time()
    while time.time() - t0 < 90 and not all(v.getVisibleEdges() for v in (top, front)):
        pump(3)
    pump(5)
    F["experiments"]["views"] = {"computed": all(v.getVisibleEdges() for v in (top, front)),
                                 "seconds": round(time.time() - t0, 2)}

    def vertices(view):
        out_ = []
        for i in range(2000):
            try:
                x = view.getVertexByIndex(i)
            except Exception:  # noqa: BLE001
                break
            if x is None:
                break
            out_.append((x.Point.x, x.Point.y))
        return out_

    def view_map(view):
        """Model point → view coordinates (model units about the projected geometric centre), the way the
        drawing worker's view_map confirms it against the view's own vertices."""
        geom = vertices(view)
        pts = [v.Point for v in shape.Vertexes]
        c = view.getGeometricCenter()
        best = (-1, None, None)
        for inv in (False, True):
            o = view.projectPoint(c, inv)

            def f(p, inv=inv, o=o):
                q = view.projectPoint(p, inv)
                return q.x - o.x, q.y - o.y
            proj = [f(p) for p in pts]
            hits = sum(1 for u, w in proj if any(abs(u - x) < 1e-3 and abs(w - y) < 1e-3 for x, y in geom))
            if hits > best[0]:
                best = (hits, f, inv)
        F["experiments"].setdefault("view_map", {})[view.Label] = {"hits": best[0], "of": len(pts), "inv": best[2]}
        return best[1]
    maps = {"Top": view_map(top), "Front": view_map(front)}

    def page_pt(view, p):
        """Paper position (mm, Y up from the bottom-left) of model point p, as the drawing worker places
        dimensions: the view's X/Y plus view coordinates × scale."""
        u, v = maps[view.Label](p)
        return (val(view.X) + u * view.Scale, val(view.Y) + v * view.Scale)

    ctx = {"symbols": {}, "balloons": {}, "targets": {}}

    def e2():
        res = {}
        line = next(i for i, e in enumerate(shape.Edges) if e.Curve.__class__.__name__ in ("Line", "LineSegment")
                    and abs(e.Length - 100) < 1e-6 and abs(e.Vertexes[0].Point.y - 60) < 1e-6
                    and abs(e.Vertexes[0].Point.z - 10) < 1e-6)
        side = next(i for i, e in enumerate(shape.Edges) if e.Curve.__class__.__name__ in ("Line", "LineSegment")
                    and abs(e.Length - 60) < 1e-6 and abs(e.Vertexes[0].Point.x) < 1e-6
                    and abs(e.Vertexes[0].Point.z - 10) < 1e-6)
        d = doc.addObject("TechDraw::DrawViewDimension", "Dim_basic")
        d.Type = "DistanceX"
        d.References3D = [(plate, "Edge%d" % (line + 1))]
        d.References2D = [(top, "")]
        page.addView(d)
        d.X, d.Y = 0, 22
        res["properties"] = sorted(p for p in d.PropertiesList if re.search(r"(?i)toler|exact|format|arbitr|invert",
                                                                            p))
        res["has_theoretical_exact"] = "TheoreticalExact" in d.PropertiesList
        if res["has_theoretical_exact"]:
            d.TheoreticalExact = True
        d2 = doc.addObject("TechDraw::DrawViewDimension", "Dim_tol")
        d2.Type = "DistanceY"
        d2.References3D = [(plate, "Edge%d" % (side + 1))]
        d2.References2D = [(top, "")]
        page.addView(d2)
        d2.X, d2.Y = -36, 0
        d2.EqualTolerance = True
        d2.OverTolerance = 0.05
        doc.recompute()
        pump(5)
        res["basic_value"] = d.getRawValue()
        res["toleranced_value"] = d2.getRawValue()
        res["state"] = [list(d.State), list(d2.State)]
        return res
    step("E2_dimensions", e2)

    def place(name, view, info, target, measure_token):
        """A DrawViewSymbol placed by hypothesis H1 (TechDraw centres the symbol's box on X/Y): its
        anchor lands on target (paper mm, Y up)."""
        o = doc.addObject("TechDraw::DrawViewSymbol", "Sym_" + name)
        o.Symbol = info["svg"]
        page.addView(o)
        W, H = info["width"], info["height"]
        ax, ay = info["anchor"]
        X, Y = target[0] + W / 2.0 - ax, target[1] - H / 2.0 + ay
        o.X, o.Y = X, Y
        tx, ty = symbol_text_pos(info["svg"], measure_token)
        ctx["symbols"][name] = {"object": o.Name, "X": X, "Y": Y, "W": W, "H": H, "anchor": [ax, ay],
                                "target": list(target), "token": measure_token, "token_in_symbol": [tx, ty]}
        ctx["targets"][name] = target
        with open(os.path.join(out, name + ".svg"), "w") as fh:
            fh.write(info["svg"])
        return o

    def e3():
        res = {}
        par = tol.normalize({"id": "par", "type": "parallelism", "feature": "Plate.face(+Z)", "tol": 0.137,
                             "datums": "J K"})
        tf = page_pt(front, V(70, 0, 10))                          # the top face, edge-on in the front view
        attach = (tf[0] + 15.0, tf[1] + 12.0)
        fr = tol.frame_svg(par, {"kind": "plane"}, h=3.5, leader={"to": (tf[0] - attach[0], -(tf[1] - attach[1])),
                                                                  "side": "left"})
        place("frame", front, fr, tf, TOKENS["frame"])
        ctx["symbols"]["frame"]["token2"] = TOKENS["frame_K"]
        ctx["symbols"]["frame"]["token2_in_symbol"] = list(symbol_text_pos(fr["svg"], TOKENS["frame_K"]))
        bf = page_pt(front, V(30, 0, 0))                            # the bottom face: datum below it
        dt = tol.datum_svg("W", h=3.5, direction=(0.0, 1.0))
        place("datum", front, dt, bf, TOKENS["datum"])
        sf = page_pt(front, V(20, 0, 10))
        su = tol.surface_svg({"ra": 6.3}, h=3.5, orient="up", process="cnc")
        place("surface", front, su, sf, TOKENS["surface"])
        hp = page_pt(top, V(92.75, 50, 10))                          # the Ø5.5 hole's rightmost point
        pos = tol.normalize({"id": "pos", "type": "position", "feature": "Plate.holes(d=5.5)", "tol": 0.25,
                             "material": "M", "datums": "J K"})
        attach = (hp[0] + 14.0, hp[1] + 10.0)
        pf = tol.frame_svg(pos, {"kind": "hole"}, h=3.5, leader={"to": (hp[0] - attach[0], -(hp[1] - attach[1])),
                                                                 "side": "left"})
        place("position", top, pf, hp, "0.25")
        doc.recompute()
        pump(10)
        sym = doc.getObject(ctx["symbols"]["frame"]["object"])
        res["properties"] = sorted(sym.PropertiesList)
        res["placed"] = ctx["symbols"]
        return res
    step("E3_symbols", e3)

    def e4():
        res = {}
        probe = doc.addObject("TechDraw::DrawViewBalloon", "Balloon_probe")
        res["properties"] = sorted(probe.PropertiesList)
        res["bubble_shapes"] = probe.getEnumerationsOfProperty("BubbleShape") if "BubbleShape" in \
            probe.PropertiesList else None
        res["end_types"] = probe.getEnumerationsOfProperty("EndType") if "EndType" in probe.PropertiesList else None
        doc.removeObject(probe.Name)
        bore = maps["Top"](V(50, 30, 10))
        hole = maps["Top"](V(10, 10, 10))
        s = top.Scale
        specs = [("41", bore, (bore[0], bore[1]), (25.0, 15.0), None, "OriginX/Y in view units (model mm)"),
                 ("42", bore, (bore[0] * s, bore[1] * s), (25.0, -15.0), None, "OriginX/Y in paper mm"),
                 ("43", hole, (hole[0], hole[1]), (-20.0, -15.0), "Inspection", "view units, Inspection shape")]
        for text, model_uv, origin, xy, shape_name, why in specs:
            b = doc.addObject("TechDraw::DrawViewBalloon", "Balloon_" + text)
            b.SourceView = top
            if "OriginIsSet" in b.PropertiesList:
                b.OriginIsSet = True
            b.OriginX, b.OriginY = origin
            b.Text = text
            if shape_name and res["bubble_shapes"] and shape_name in res["bubble_shapes"]:
                b.BubbleShape = shape_name
            page.addView(b)
            b.X, b.Y = xy
            target = (val(top.X) + model_uv[0] * s, val(top.Y) + model_uv[1] * s)
            ctx["balloons"]["balloon" + text] = {"object": b.Name, "origin": list(origin), "X": xy[0], "Y": xy[1],
                                                 "hypothesis": why, "target": list(target),
                                                 "view_xy": [val(top.X), val(top.Y)], "scale": s}
            ctx["targets"]["balloon" + text] = target
        doc.recompute()
        pump(10)
        res["made"] = ctx["balloons"]
        return res
    step("E4_balloons", e4)

    def e5():
        res = {}
        ld = doc.addObject("TechDraw::DrawLeaderLine", "Leader")
        res["leader_properties"] = sorted(ld.PropertiesList)
        ld.LeaderParent = front
        page.addView(ld)
        tgt = maps["Front"](V(85, 0, 10))
        ld.X, ld.Y = tgt[0] * front.Scale, tgt[1] * front.Scale
        ld.WayPoints = [V(0, 0, 0), V(12, 10, 0), V(22, 10, 0)]
        for p in ("StartSymbol", "EndSymbol"):
            if p in ld.PropertiesList:
                try:
                    res[p] = ld.getEnumerationsOfProperty(p)
                except Exception:  # noqa: BLE001
                    res[p] = str(getattr(ld, p))
        ra = doc.addObject("TechDraw::DrawRichAnno", "RichAnno")
        res["rich_properties"] = sorted(ra.PropertiesList)
        ra.AnnoParent = ld
        ra.AnnoText = ('<html><body><table border="1" cellspacing="0" cellpadding="2"><tr><td>∥</td><td>0.05</td>'
                       '<td>A</td></tr></table></body></html>')
        if "ShowFrame" in ra.PropertiesList:
            ra.ShowFrame = False
        page.addView(ra)
        ra.X, ra.Y = 22, 10
        ctx["targets"]["leader"] = (val(front.X) + tgt[0] * front.Scale, val(front.Y) + tgt[1] * front.Scale)
        doc.recompute()
        pump(10)
        res["made"] = True
        return res
    step("E5_leader_richanno", e5)

    def e6():
        import importlib
        importlib.import_module("TechDrawGui")          # registers osifont with Qt
        f = QtGui.QFont("osifont")
        fm = QtGui.QFontMetrics(f)
        chars = "⏤⏥○⌭⌒⌓∥⟂∠⌖◎⌯↗⌰ⓂⓁⓅⒺⒻ⌀√⊥"
        have = [c for c in chars if fm.inFontUcs4(ord(c))]
        return {"family": QtGui.QFontInfo(f).family(), "present": have, "missing": [c for c in chars if c not in have]}
    step("E6_fonts", e6)

    def export():
        import TechDraw
        import TechDrawGui
        page.ViewObject.doubleClicked()
        pump(25)
        base = os.path.join(out, "spike")
        TechDrawGui.exportPageAsPdf(page, base + ".pdf")
        TechDrawGui.exportPageAsSvg(page, base + ".svg")
        try:
            TechDraw.writeDXFPage(page, base + ".dxf")
        except Exception as e:  # noqa: BLE001
            F["errors"]["dxf"] = str(e)
        r = QtSvg.QSvgRenderer(base + ".svg")
        sz = r.defaultSize()
        w = 2800
        img = QtGui.QImage(w, int(w * sz.height() / max(sz.width(), 1)), QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.white)
        pa = QtGui.QPainter(img)
        r.render(pa)
        pa.end()
        img.save(base + ".png")
        return {"files": [base + x for x in (".pdf", ".svg", ".dxf", ".png")]}
    step("export", export)

    def analysis():
        res = analyse_positions(os.path.join(out, "spike.svg"), os.path.join(out, "spike.dxf"), ctx)
        res["crops"] = render_crops(os.path.join(out, "spike.svg"), ctx, res["svg_root"]["units_per_mm"], QtCore,
                                    QtGui, QtSvg, out)
        return res
    step("analysis", analysis)

    def e7():
        names = [v["object"] for v in list(ctx["symbols"].values()) + list(ctx["balloons"].values())] + [
            "Dim_basic", "Dim_tol", "Leader", "RichAnno"]
        path = os.path.join(out, "spike.FCStd")
        doc.saveAs(path)
        App.closeDocument(doc.Name)
        d2 = App.openDocument(path)
        pump(10)
        res = {}
        for n in names:
            o = d2.getObject(n)
            if o is None:
                res[n] = "missing"
                continue
            rec = {"type": o.TypeId, "X": val(getattr(o, "X", 0)), "Y": val(getattr(o, "Y", 0))}
            if hasattr(o, "Symbol"):
                rec["symbol_chars"] = len(o.Symbol or "")
            if hasattr(o, "Text") and o.TypeId == "TechDraw::DrawViewBalloon":
                rec["text"] = o.Text
            if hasattr(o, "TheoreticalExact"):
                rec["theoretical_exact"] = bool(o.TheoreticalExact)
            res[n] = rec
        res["summary"] = "%d of %d objects back" % (sum(1 for n in names if res[n] != "missing"), len(names))
        return res
    step("E7_reopen", e7)


def symbol_text_pos(svg, token):
    """(x, y) of the text element `token` in a generated symbol, in the symbol's own mm."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(svg)
    g = root.find("{http://www.w3.org/2000/svg}g")
    m = re.match(r"translate\((-?[\d.]+),(-?[\d.]+)\)", g.get("transform") or "translate(0,0)")
    dx, dy = float(m.group(1)), float(m.group(2))
    for t in root.iter("{http://www.w3.org/2000/svg}text"):
        if (t.text or "").strip() == token:
            return float(t.get("x")) + dx, float(t.get("y")) + dy
    return None, None


# --------------------------------------------------------------- reading the exported page back

def _mul(a, b):
    """3×3 affine matrices as (a, b, c, d, e, f): x' = a x + c y + e, y' = b x + d y + f."""
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3],
            a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


def _parse_transform(s):
    import math
    m_ = (1, 0, 0, 1, 0, 0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "matrix":
            t = tuple(v[:6])
        elif name == "rotate":
            a = math.radians(v[0])
            t = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m_ = _mul(m_, t)
    return m_


def _length_mm(s):
    m = re.match(r"^\s*(-?[\d.]+)\s*([a-z%]*)\s*$", s or "")
    if not m:
        return None, None
    return float(m.group(1)), m.group(2) or "px"


def analyse_positions(svg_path, dxf_path, ctx):
    """Find each symbol's and balloon's text in the exported page SVG, in paper mm (Y up), and compare with
    the placement hypotheses (pure Python: tested without FreeCAD)."""
    import math
    import xml.etree.ElementTree as ET
    ns = "{http://www.w3.org/2000/svg}"
    tree = ET.parse(svg_path)
    root = tree.getroot()
    vb = [float(x) for x in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if x]
    k = (vb[2] / PAGE[0]) if len(vb) == 4 else 1.0            # user units per paper mm
    res = {"svg_root": {"width": root.get("width"), "height": root.get("height"), "viewBox": root.get("viewBox"),
                        "units_per_mm": k}, "symbols": {}, "balloons": {}, "texts": {}}
    found = {}

    def walk(el, m_, path):
        m2 = _mul(m_, _parse_transform(el.get("transform")))
        if el.tag == ns + "svg" and el is not root:          # a nested viewport
            x, y = float(el.get("x") or 0), float(el.get("y") or 0)
            vb2 = [float(v) for v in re.split(r"[\s,]+", (el.get("viewBox") or "").strip()) if v]
            m2 = _mul(m2, (1, 0, 0, 1, x, y))
            if len(vb2) == 4:
                w2, _ = _length_mm(el.get("width"))
                h2, _ = _length_mm(el.get("height"))
                sx, sy = (w2 or vb2[2]) / vb2[2], (h2 or vb2[3]) / vb2[3]
                m2 = _mul(m2, (sx, 0, 0, sy, -vb2[0] * sx, -vb2[1] * sy))
        if el.tag == ns + "text":
            txt = "".join(el.itertext()).strip()
            if txt in TOKENS.values() or txt in ("0.25",):
                x = float((el.get("x") or "0").split()[0])
                y = float((el.get("y") or "0").split()[0])
                if x == 0 and y == 0:
                    ts = el.find(ns + "tspan")
                    if ts is not None:
                        x, y = float((ts.get("x") or "0").split()[0]), float((ts.get("y") or "0").split()[0])
                px, py = m2[0] * x + m2[2] * y + m2[4], m2[1] * x + m2[3] * y + m2[5]
                found.setdefault(txt, []).append({"page": [round(px / k, 3), round(PAGE[1] - py / k, 3)],
                                                  "path": path})
        for i, ch in enumerate(list(el)):
            walk(ch, m2, path + [ch.tag.replace(ns, "") + str(i)])
    walk(root, (1, 0, 0, 1, 0, 0), [])
    res["texts"] = found
    for name, s in ctx["symbols"].items():
        rec = {"found": False}
        hits = found.get(s["token"]) or []
        if hits and s["token_in_symbol"][0] is not None:
            tx, ty = s["token_in_symbol"]
            h1 = (s["X"] - s["W"] / 2 + tx, s["Y"] + s["H"] / 2 - ty)
            h2 = (s["X"] + tx, s["Y"] - ty)
            got = min(hits, key=lambda h: math.hypot(h["page"][0] - h1[0], h["page"][1] - h1[1]))["page"]
            rec.update(found=True, measured=got, h1=[round(h1[0], 3), round(h1[1], 3)],
                       h2=[round(h2[0], 3), round(h2[1], 3)],
                       h1_error=round(math.hypot(got[0] - h1[0], got[1] - h1[1]), 3),
                       h2_error=round(math.hypot(got[0] - h2[0], got[1] - h2[1]), 3))
            if s.get("token2") and found.get(s["token2"]):
                t2 = s["token2_in_symbol"]
                g2 = found[s["token2"]][0]["page"]
                d_sym = math.hypot(t2[0] - tx, t2[1] - ty)
                d_page = math.hypot(g2[0] - got[0], g2[1] - got[1])
                rec["scale"] = round(d_page / d_sym, 4) if d_sym else None
        res["symbols"][name] = rec
    errs = [(r.get("h1_error"), r.get("h2_error")) for r in res["symbols"].values() if r.get("found")]
    if errs:
        h1 = max(e[0] for e in errs)
        h2 = max(e[1] for e in errs)
        res["symbol_verdict"] = ("H1: X/Y is the symbol's centre (max error %.2f mm)" % h1 if h1 < 1.5 else
                                 "H2: X/Y is the symbol's top-left corner (max error %.2f mm)" % h2 if h2 < 1.5 else
                                 "neither: H1 off by up to %.2f mm, H2 by %.2f mm, see texts" % (h1, h2))
    else:
        res["symbol_verdict"] = "no symbol text found in the exported SVG (texts may be paths): check the PDF"
    for name, b in ctx["balloons"].items():
        hits = found.get(TOKENS[name]) or []
        rec = {"found": bool(hits)}
        if hits:
            p = hits[0]["page"]
            vx, vy = b["view_xy"]
            cands = {"X/Y absolute paper mm": (b["X"], b["Y"]),
                     "X/Y paper mm from the view centre": (vx + b["X"], vy + b["Y"]),
                     "X/Y view units from the view centre": (vx + b["X"] * b["scale"], vy + b["Y"] * b["scale"])}
            best = min(cands, key=lambda c: math.hypot(p[0] - cands[c][0], p[1] - cands[c][1]))
            rec.update(page=p, best_xy=best, errors={c: round(math.hypot(p[0] - v[0], p[1] - v[1]), 2)
                                                     for c, v in cands.items()})
        res["balloons"][name] = rec
    if dxf_path and os.path.isfile(dxf_path):
        with open(dxf_path, errors="replace") as fh:
            dxf = fh.read()
        res["dxf"] = {name: (tok in dxf) for name, tok in TOKENS.items()}
    return res


def render_crops(svg_path, ctx, k, QtCore, QtGui, QtSvg, out):
    """60 × 40 mm around each target at 12 px/mm, with a red cross on the target: where the arrow tip,
    triangle base, symbol apex or balloon leader should be."""
    r = QtSvg.QSvgRenderer(svg_path)
    files = []
    for name, (tx, ty) in ctx["targets"].items():
        img = QtGui.QImage(720, 480, QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.white)
        r.setViewBox(QtCore.QRectF((tx - 30) * k, (PAGE[1] - ty - 20) * k, 60 * k, 40 * k))
        pa = QtGui.QPainter(img)
        r.render(pa)
        pen = QtGui.QPen(QtGui.QColor(230, 0, 0))
        pen.setWidth(2)
        pa.setPen(pen)
        pa.drawLine(350, 240, 370, 240)
        pa.drawLine(360, 230, 360, 250)
        pa.end()
        path = os.path.join(out, "crop_%s.png" % name)
        img.save(path)
        files.append(path)
    return files


if __name__ == "__main__" or os.environ.get("FCMCP_SPIKE_OUT"):
    if os.environ.get("FCMCP_SPIKE_OUT"):
        inside()
    else:
        launch(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "gdt_spike_out"))
