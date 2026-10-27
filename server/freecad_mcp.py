#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
FreeCAD MCP server — stdio, zero dependencies, Python 3.7+.

Claude writes FreeCAD Python (model.py, or a patch script); this server runs it
in a separate FreeCAD process and returns evidence: build reports, geometry
indexes, renders. The server itself never imports FreeCAD, so an OpenCascade
crash only ends the worker. Run it with FreeCAD's bundled interpreter so
FreeCAD is the only thing to install:

    /Applications/FreeCAD.app/Contents/Resources/bin/python -I freecad_mcp.py

Tools
  freecad_status   FreeCAD found? version, home, roots, GUI, jobs
  guide            the design playbook (or one section of it)
  create_project   scaffold project.json, params.json, model.py, requirements.md …
  list_projects    find projects under the home directory
  read_file        read a text file with line numbers
  write_file       create or overwrite a text file, or a base64 binary reference file (backups kept)
  edit_file        exact unique-match replacement (backups kept)
  build            run model.py → FCStd + build report + render
  run_script       run FreeCAD Python against a document (query or patch)
  inspect          model tree, geometry index, holes, sketches, parameters, GUI edits
  render           headless images: view grid, x-ray, drawing style, sections
  measure          distances, angles, coaxiality, hole-pattern alignment, radial gaps
  check_fit        interference / clearance / contact between every pair of parts
  section          2D cross-section with per-body areas and in-plane gaps (+ zoomed insets)
  lookup_standard  ISO holes, threads, fasteners, inserts, fits; process rules
  export           verified STEP / 3MF / STL / DXF … per part
  drawing          ISO part drawings: create (auto views, dims, callouts), read, edit
  check_manufacturing  DFM per process: print orientation/overhang/walls, CNC corners, lathe, laser
  release          verified supplier package: exports, drawings, BOM, notes, zip
  job_status       poll or collect background work (wait=false)
  open_in_gui      open a file in the FreeCAD GUI, starting it if needed

Environment
  FREECAD_APP         FreeCAD.app, its bundled python, or a freecadcmd binary
  FREECAD_MCP_HOME    projects directory (default ~/CAD; ~ and $VARS are expanded)
  FREECAD_MCP_ROOTS   ':'-separated allow-list for file access (default: unrestricted)
"""

import base64
import binascii
import difflib
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

SERVER_NAME = "freecad"
SERVER_VERSION = "0.5.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "worker.py")
DRAWING_WORKER = os.path.join(HERE, "drawing_worker.py")
sys.path.insert(0, HERE)            # -I leaves the script directory off sys.path
import standards  # noqa: E402
TEXT_EXT = {".py", ".json", ".md", ".txt", ".csv", ".fcmacro", ".yaml", ".yml", ".tsv"}
# binary reference files write_file accepts as base64 (vendor STEP, datasheet PDF, reference drawing …)
BINARY_EXT = {".step", ".stp", ".iges", ".igs", ".stl", ".obj", ".3mf", ".pdf", ".png", ".jpg", ".jpeg",
              ".dxf", ".fcstd"}
MAX_BINARY = 50 * 1024 * 1024

MATERIALS = {
    "fdm":   {"name": "PETG", "density_g_cm3": 1.27},
    "sla":   {"name": "Standard resin", "density_g_cm3": 1.15},
    "cnc":   {"name": "Aluminium 6061-T6", "density_g_cm3": 2.70},
    "turning": {"name": "Stainless steel 303", "density_g_cm3": 8.0},
    "laser": {"name": "Acrylic (PMMA)", "density_g_cm3": 1.19},
    "sheet": {"name": "Mild steel", "density_g_cm3": 7.85},
}

NOISE = re.compile(r"(Looking for thread definitions|3Dconnexion|^\s*\(\d+ %\)|Recompute\.\.\.|"
                   r"Importing project files|Postprocessing|^FreeCAD \d|\(C\) 20\d\d|LGPL|"
                   r"^\s*$)")


# ---------------------------------------------------------------- utilities

def log(msg):
    sys.stderr.write("[freecad-mcp] %s\n" % msg)
    sys.stderr.flush()


class ToolError(Exception):
    pass


def home_dir():
    """FREECAD_MCP_HOME with ~ and $VARS expanded; empty, or a placeholder the host
    left unexpanded ("${user_config.projects_dir}"), means the default ~/CAD."""
    raw = os.path.expanduser(os.path.expandvars((os.environ.get("FREECAD_MCP_HOME") or "").strip()))
    if not raw or "${" in raw:
        raw = os.path.expanduser("~/CAD")
    return os.path.abspath(raw)


def allowed_roots():
    raw = os.environ.get("FREECAD_MCP_ROOTS", "").strip()
    if not raw:
        return []
    return [os.path.abspath(os.path.expanduser(p)) for p in raw.split(":") if p.strip()]


def resolve(path, base=None, must_exist=False, kind="path"):
    if not path or not str(path).strip():
        raise ToolError("%s is required" % kind)
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = _join_base(base, p) if base else os.path.join(home_dir(), p)
    p = os.path.abspath(p)
    roots = allowed_roots()
    if roots and not any(p == r or p.startswith(r + os.sep) for r in roots):
        raise ToolError("%s is outside the allowed roots (%s). Set FREECAD_MCP_ROOTS to widen access."
                        % (p, os.pathsep.join(roots)))
    if must_exist and not os.path.exists(p):
        raise ToolError("no such %s: %s" % (kind, p))
    return p


def _join_base(base, rel):
    """base/rel — but 'bad_spacer/release/x' with base …/bad_spacer means the project's own
    release/x when the doubled path doesn't exist (and there is no such subfolder)."""
    p = os.path.join(base, rel)
    if os.path.exists(p):
        return p
    parts = os.path.normpath(rel).split(os.sep)
    name = os.path.basename(os.path.normpath(base))
    if parts and parts[0] == name and not os.path.isdir(os.path.join(base, parts[0])):
        return os.path.join(base, *parts[1:]) if len(parts) > 1 else base
    return p


def find_project_root(path):
    """The nearest folder above path that holds a project.json, or None."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.isfile(os.path.join(d, "project.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:
        return False


def backup_file(path, pdir=None):
    """Copy path before it is overwritten. Inside a project every version is kept as
    <project>/.history/backups/<relpath>.<YYYYmmdd-HHMMSS>.bak; elsewhere <file>.bak
    next to it. Returns the backup path."""
    pdir = pdir if pdir and _inside(path, pdir) else find_project_root(path)
    if pdir and _inside(path, pdir):
        stem = os.path.join(pdir, ".history", "backups", os.path.relpath(path, pdir) + "." + time.strftime("%Y%m%d-%H%M%S"))
        dst, n = stem + ".bak", 2
        while os.path.exists(dst):
            dst, n = "%s-%d.bak" % (stem, n), n + 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    else:
        dst = path + ".bak"
    shutil.copyfile(path, dst)
    return dst


def _rel(path, pdir):
    """path relative to the project when it is inside it (for messages)."""
    return os.path.relpath(path, pdir) if pdir and _inside(path, pdir) else path


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def project_dir(project, must_exist=True):
    """A project name (under the home directory) or a path."""
    p = resolve(project, kind="project")
    if must_exist and not os.path.isdir(p):
        raise ToolError("no project at %s — create_project first, or pass a path" % p)
    return p


def project_name(pdir):
    try:
        with open(os.path.join(pdir, "project.json")) as fh:
            return json.load(fh).get("name") or os.path.basename(pdir)
    except (OSError, ValueError):
        return os.path.basename(pdir)


def default_doc(pdir):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", project_name(pdir)).strip("_") or "model"
    return os.path.join(pdir, "build", stem + ".FCStd")


def doc_and_project(args):
    """Resolve (doc path, project dir or None) from doc/project arguments."""
    pdir = project_dir(args["project"]) if args.get("project") else None
    if args.get("doc"):
        doc = resolve(args["doc"], base=pdir, must_exist=True, kind="doc")
    elif pdir:
        doc = default_doc(pdir)
        if not os.path.isfile(doc):
            raise ToolError("%s has not been built yet — call build first" % pdir)
    else:
        raise ToolError("pass doc (a .FCStd/.step path) or project")
    if pdir is None:
        cand = os.path.dirname(os.path.dirname(doc))
        if os.path.isfile(os.path.join(cand, "project.json")):
            pdir = cand
    return doc, pdir


def check_text_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        raise ToolError("refusing to write '%s': only %s files are editable through this server"
                        % (ext or "(no extension)", ", ".join(sorted(TEXT_EXT))))


# ------------------------------------------------------------ FreeCAD lookup

def _app_version(app):
    """(major, minor, patch) from the bundle's Info.plist (CFBundleShortVersionString, else
    CFBundleVersion — FreeCAD 1.1 leaves the short one empty), () if unknown."""
    try:
        import plistlib
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
    except Exception:  # noqa: BLE001
        return ()
    v = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def freecad_bundles():
    """FreeCAD .app bundles in launch.sh's order: /Applications/FreeCAD.app, then the other
    FreeCAD*.app there newest first (1.x before 0.x), then the same in ~/Applications."""
    out = []
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        main = os.path.join(d, "FreeCAD.app")
        others = [a for a in glob.glob(os.path.join(d, "FreeCAD*.app")) if a != main]
        others.sort(key=lambda a: (_app_version(a) >= (1,), _app_version(a), a), reverse=True)
        out += ([main] if os.path.isdir(main) else []) + others
    return out


def find_freecad():
    """Return {kind: python|cmd, exe, lib, app, gui} or None. FREECAD_APP first, then
    the bundles in freecad_bundles() order, then freecadcmd on PATH."""
    cands = []
    env = (os.environ.get("FREECAD_APP") or "").strip()
    if env:
        cands.append(os.path.expanduser(env).rstrip("/") or "/")     # '…/FreeCAD.app/' from tab completion
    cands += freecad_bundles()
    for c in cands:
        # a python or freecadcmd inside a bundle: that bundle's GUI (drawings, open_in_gui) — never another FreeCAD
        m = re.match(r"^(.*?\.app)/Contents/", c)
        app = m.group(1) if m and os.path.isdir(m.group(1)) else None
        gui = os.path.join(app, "Contents", "Resources", "bin", "freecad") if app else None
        if c.endswith(".app") and os.path.isdir(c):
            py = os.path.join(c, "Contents", "Resources", "bin", "python")
            lib = os.path.join(c, "Contents", "Resources", "lib")
            if os.path.isfile(py) and os.path.isfile(os.path.join(lib, "FreeCAD.so")):
                return {"kind": "python", "exe": py, "lib": lib, "app": c,
                        "gui": os.path.join(c, "Contents", "Resources", "bin", "freecad")}
        elif os.path.basename(c).startswith("python") and os.path.isfile(c):
            lib = os.path.join(os.path.dirname(os.path.dirname(c)), "lib")
            return {"kind": "python", "exe": c, "lib": lib, "app": app, "gui": gui}
        elif os.path.isfile(c) and os.access(c, os.X_OK):
            return {"kind": "cmd", "exe": c, "lib": None, "app": app, "gui": gui or shutil.which("freecad")}
    cmd = shutil.which("freecadcmd") or shutil.which("FreeCADCmd")
    if cmd:
        return {"kind": "cmd", "exe": cmd, "lib": None, "app": None, "gui": shutil.which("freecad")}
    return None


def setup_hint(plat=None, node=None):
    """What to do when FreeCAD is not found — depends on where this server runs."""
    import platform
    plat = plat or sys.platform
    if plat == "darwin":
        return ("FreeCAD not found on this Mac. Install FreeCAD 1.x (https://www.freecad.org/downloads.php) "
                "into /Applications (or ~/Applications), or set FREECAD_APP in the MCP server's env to its .app "
                "(or a freecadcmd binary); then call freecad_status again.")
    node = node or platform.node()
    snippet = json.dumps({"mcpServers": {"freecad": {"command": "/bin/sh",
                                                     "args": [os.path.join(HERE, "launch.sh")],
                                                     "env": {"FREECAD_MCP_HOME": "~/CAD"}}}}, indent=2)
    return ("FreeCAD not found: this server is running on %s (host %s), not on your Mac — Cowork probably "
            "started it in its VM, where FreeCAD.app can't run. Run it on the Mac instead:\n"
            "  1. Quit Claude completely (⌘Q).\n"
            "  2. Add this to ~/Library/Application Support/Claude/claude_desktop_config.json (merge it into "
            "an existing \"mcpServers\"):\n%s\n"
            "     The args path must be server/launch.sh inside the freecad-design plugin (or this repo) as it "
            "is on your Mac — %s is this host's path and may differ.\n"
            "  3. Restart Claude and call freecad_status: host.platform should say darwin.\n"
            "(If this really is your own %s machine, install FreeCAD 1.x so freecadcmd is on PATH, or set "
            "FREECAD_APP.)" % (plat, node, "\n".join("     " + ln for ln in snippet.splitlines()),
                               os.path.join(HERE, "launch.sh"), plat))


def require_freecad():
    fcinfo = find_freecad()
    if not fcinfo:
        raise ToolError(setup_hint())
    return fcinfo


# a FreeCAD GUI executable at the start of the command line: …/MacOS/FreeCAD, …/bin/freecad
# (FreeCAD 1.1), /usr/bin/freecad. Spaces are allowed in the path ("FreeCAD 1.1.app") but not
# before an argument ("/usr/bin/vim /x/freecad" is not FreeCAD).
_GUI_EXE = re.compile(r"(?i)^(/(?:[^ ]| (?![/-]))*/)?freecad(?=\s|$)")


def gui_pids(ps_text=None):
    """PIDs of running FreeCAD GUIs, from `ps -axo pid=,command=` (or the given text),
    excluding this server's own GUI workers."""
    if ps_text is None:
        try:
            ps_text = subprocess.run(["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=15).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
    pids = []
    for ln in ps_text.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)$", ln)
        if not m or "drawing_worker.py" in m.group(2) or "freecad-mcp/gui/user.cfg" in m.group(2):
            continue
        if _GUI_EXE.match(m.group(2)):
            pids.append(m.group(1))
    return pids


# ------------------------------------------------------- process bookkeeping

_jobs = {}
_jobs_lock = threading.Lock()
_children = {}
_children_lock = threading.Lock()


def register_child(request_id, proc):
    if request_id is None:
        return
    with _children_lock:
        _children.setdefault(request_id, set()).add(proc)


def unregister_child(request_id, proc):
    if request_id is None:
        return
    with _children_lock:
        procs = _children.get(request_id)
        if procs:
            procs.discard(proc)
            if not procs:
                _children.pop(request_id, None)


def cancel_request(request_id):
    with _children_lock:
        procs = list(_children.pop(request_id, ()))
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass
    return len(procs)


def kill_all_children():
    with _children_lock:
        procs = [p for group in _children.values() for p in group]
        _children.clear()
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass
    return len(procs)


def clean_log(text):
    return "\n".join(ln for ln in text.splitlines() if not NOISE.search(ln))[-6000:]


def run_worker(op, args, timeout=300, request_id=None, job_key=None):
    """Run one worker op in a fresh FreeCAD process. Never raises: returns the
    response dict ({ok, text, images, error, ...})."""
    try:
        fcinfo = require_freecad()
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    tmp = tempfile.mkdtemp(prefix="fcmcp-")
    req = os.path.join(tmp, "req.json")
    resp = os.path.join(tmp, "resp.json")
    with open(req, "w") as fh:
        json.dump({"op": op, "args": args}, fh)
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(k, None)
    env.update(FCMCP_REQ=req, FCMCP_RESP=resp, FCMCP_SERVER_DIR=HERE, FCMCP_LIB=fcinfo["lib"] or "")
    if fcinfo["kind"] == "python":
        cmd = [fcinfo["exe"], "-I", WORKER, req, resp]
    else:
        cmd = [fcinfo["exe"], "-c", "exec(open(%r).read())" % WORKER]
    t0 = time.time()
    proc = None
    key = request_id if request_id is not None else job_key
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=args.get("project_dir") or tmp, env=env)
        register_child(key, proc)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            return {"ok": False, "error": "FreeCAD did not finish within %ds — simplify, use cosmetic "
                    "threads, or raise timeout (and pass wait=false)" % timeout,
                    "log": clean_log(out.decode("utf-8", "replace"))}
    except OSError as e:
        return {"ok": False, "error": "could not start FreeCAD (%s): %s" % (cmd[0], e)}
    finally:
        if proc is not None:
            unregister_child(key, proc)
    elapsed = time.time() - t0
    text_log = clean_log(out.decode("utf-8", "replace"))
    try:
        if os.path.isfile(resp):
            with open(resp) as fh:
                result = json.load(fh)
        else:
            rc = proc.returncode
            crash = re.search(r"Program received signal (SIG[A-Z0-9]+)", text_log)
            if rc is not None and rc < 0:
                why = "killed by signal %d" % -rc
            elif crash:
                why = "crashed with %s" % crash.group(1)
            else:
                why = "exited %s without a result" % rc
            result = {"ok": False, "error": "FreeCAD worker %s — usually an OpenCascade crash in a "
                      "boolean, fillet, sweep or modelled thread. The server is unaffected." % why}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result["elapsed"] = elapsed
    if text_log:
        result["log"] = text_log
    return result


def image_block(path):
    with open(path, "rb") as fh:
        return {"type": "image", "data": base64.b64encode(fh.read()).decode("ascii"), "mimeType": "image/png"}


_gui_lock = threading.Lock()


class _GuiFileLock:
    """Cross-process lock: one FreeCAD GUI worker at a time across every server
    instance (several Claude sessions, or a CLI), not just within this one."""

    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:          # Windows: the in-process lock still applies
            return self
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


# private FreeCAD preferences for the drawing worker: no splash screen
GUI_USER_CFG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
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


def run_gui_worker(op, args, timeout=300, request_id=None):
    """Run a drawing op in the FreeCAD GUI process (TechDraw export needs it).
    One at a time; a private user.cfg keeps the user's FreeCAD preferences
    (single instance, start page …) out of the way. Never raises."""
    try:
        fcinfo = require_freecad()
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    gui = fcinfo.get("gui")
    if not gui or not os.path.isfile(gui):
        return {"ok": False, "error": "drawings need the FreeCAD GUI binary, which was not found next to %s"
                % fcinfo["exe"]}
    cfg_dir = os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "gui")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "user.cfg"), "w") as fh:     # the worker exits without saving it
        fh.write(GUI_USER_CFG)
    tmp = tempfile.mkdtemp(prefix="fcmcp-gui-")
    req, resp = os.path.join(tmp, "req.json"), os.path.join(tmp, "resp.json")
    with open(req, "w") as fh:
        json.dump({"op": op, "args": args}, fh)
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(k, None)
    env.update(FCMCP_REQ=req, FCMCP_RESP=resp, FCMCP_SERVER_DIR=HERE)
    if sys.platform == "darwin":
        # stay a background process: no Dock icon, no window on screen, no focus taken from the user
        env["QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"] = "1"
    cmd = [gui, "-u", os.path.join(cfg_dir, "user.cfg"), DRAWING_WORKER]
    t0 = time.time()
    with _gui_lock, _GuiFileLock(os.path.join(cfg_dir, "gui.lock")):
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=tmp, env=env)
            register_child(request_id, proc)
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
                shutil.rmtree(tmp, ignore_errors=True)
                return {"ok": False, "error": "drawing did not finish within %ds" % timeout,
                        "log": clean_log(out.decode("utf-8", "replace"))}
        except OSError as e:
            return {"ok": False, "error": "could not start the FreeCAD GUI: %s" % e}
        finally:
            if proc is not None:
                unregister_child(request_id, proc)
    try:
        if os.path.isfile(resp):
            with open(resp) as fh:
                result = json.load(fh)
        else:
            result = {"ok": False, "error": "the FreeCAD GUI exited (%s) without a result" % proc.returncode,
                      "log": clean_log(out.decode("utf-8", "replace"))[-3000:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    result["elapsed"] = time.time() - t0
    return result


def as_content(result, return_image=True):
    if not result.get("ok"):
        msg = result.get("error") or "failed"
        if result.get("traceback"):
            msg += "\n" + result["traceback"]
        if result.get("log"):
            msg += "\nFreeCAD log:\n" + result["log"][-2500:]
        raise ToolError(msg)
    text = result.get("text", "")
    log_lines = [ln for ln in (result.get("log") or "").splitlines()
                 if re.search(r"(?i)(warn|error|exception|fail)", ln)]
    if log_lines:
        text += "\nfreecad log\n" + "\n".join("  " + ln for ln in log_lines[-15:])
    imgs = [p for p in result.get("images") or [] if os.path.isfile(p)]
    if return_image and imgs:
        return [{"type": "text", "text": text}] + [image_block(p) for p in imgs]
    return text


def maybe_background(kind, args, op, worker_args, timeout, request_id, describe):
    """Run now, or (wait=false) as a background job collected with job_status."""
    if args.get("wait") is False:
        job_id = "j-" + uuid.uuid4().hex[:8]
        record = {"id": job_id, "kind": kind, "status": "running", "started": time.time(),
                  "describe": describe}
        with _jobs_lock:
            _jobs[job_id] = record

        def worker():
            res = run_worker(op, worker_args, timeout=timeout, job_key=job_id)
            with _jobs_lock:
                if record["status"] == "cancelled":
                    return
                record["result"] = res
                record["elapsed"] = time.time() - record["started"]
                record["status"] = "done" if res.get("ok") else "failed"

        t = threading.Thread(target=worker, name="job-%s" % job_id)
        t.daemon = True
        t.start()
        record["thread"] = t
        return ("Started %s in the background.\n  job   %s\n  what  %s\nCollect it with "
                "job_status(job_id=\"%s\"). Other tools stay usable meanwhile." % (kind, job_id, describe, job_id))
    res = run_worker(op, worker_args, timeout=timeout, request_id=request_id)
    return as_content(res, return_image=args.get("return_image", True))


# -------------------------------------------------------------------- tools

def t_freecad_status(args):
    fcinfo = find_freecad()
    import platform
    out = {"freecad": fcinfo or "NOT FOUND — see setup_hint",
           "home": home_dir(), "home_exists": os.path.isdir(home_dir()),
           "allowed_roots": allowed_roots() or "unrestricted",
           "gui_running": bool(gui_pids()), "playbook": playbook_path() or "not found",
           "server": "%s %s (python %s)" % (SERVER_NAME, SERVER_VERSION, sys.version.split()[0]),
           # where this process runs — Cowork may start plugin servers on the Mac or in its VM
           "host": {"platform": sys.platform, "machine": platform.machine(), "node": platform.node(),
                    "plugin_root": os.environ.get("CLAUDE_PLUGIN_ROOT"), "server_dir": HERE}}
    with _jobs_lock:
        out["jobs_running"] = [j["id"] for j in _jobs.values() if j["status"] == "running"]
    if fcinfo:
        res = run_worker("probe", {}, timeout=60)
        out["version"] = res.get("text") if res.get("ok") else "probe failed: %s" % res.get("error")
        if res.get("ok"):
            data = res.get("data") or {}
            out["modules"] = data.get("modules")
            out["worker_start_s"] = round(res.get("elapsed", 0), 2)
            if data.get("user_data"):
                _fc_dirs_cache.update(user_data=data["user_data"], macro_dir=data.get("macro_dir"))
                out["reload_helper"] = reload_helper_state(data["user_data"], data.get("macro_dir"))
    else:
        out["setup_hint"] = setup_hint()
    return json.dumps(out, indent=2)


ADDON_SRC = os.path.join(HERE, "..", "freecad_addon")
_fc_dirs_cache = {}


def fc_user_dirs():
    """{user_data, macro_dir} of the FreeCAD the workers use (probed once per session)."""
    if not _fc_dirs_cache.get("user_data"):
        res = run_worker("probe", {}, timeout=60)
        data = (res.get("data") or {}) if res.get("ok") else {}
        if not data.get("user_data"):
            raise ToolError("could not find FreeCAD's user folder: %s" % (res.get("error") or "probe returned none"))
        _fc_dirs_cache.update(user_data=data["user_data"], macro_dir=data.get("macro_dir"))
    return dict(_fc_dirs_cache)


def reload_helper_state(user_data, macro_dir=None):
    """Where the MCP Reload addon and macro live and whether they are installed. The macro
    goes into FreeCAD's macro folder (App.getUserMacroDir(True)), which is what Macro ▸
    Macros… lists — not necessarily <user data>/Macro."""
    macro_dir = macro_dir or os.path.join(user_data, "Macro")
    addon = os.path.join(user_data, "Mod", "FreeCADMCPReload", "mcp_reload.py")
    macro = os.path.join(macro_dir, "MCP_Reload.FCMacro")
    return {"addon_installed": os.path.isfile(addon), "macro_installed": os.path.isfile(macro),
            "freecad_user_data": user_data, "macro_dir": macro_dir, "macro_path": macro}


def install_reload_helper():
    """Copy the MCP Reload addon into the user's FreeCAD Mod folder and the macro into
    their macro folder. Returns (addon dir, macro path)."""
    dirs = fc_user_dirs()
    st = reload_helper_state(dirs["user_data"], dirs.get("macro_dir"))
    src = os.path.abspath(os.path.join(ADDON_SRC, "FreeCADMCPReload"))
    macro = os.path.abspath(os.path.join(ADDON_SRC, "MCP_Reload.FCMacro"))
    if not os.path.isdir(src) or not os.path.isfile(macro):
        raise ToolError("reload helper files missing next to the server (%s) — reinstall the plugin" % ADDON_SRC)
    dst = os.path.join(dirs["user_data"], "Mod", "FreeCADMCPReload")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(st["macro_dir"], exist_ok=True)
    shutil.copyfile(macro, st["macro_path"])
    return dst, st["macro_path"]


def playbook_path():
    for p in (os.path.join(HERE, "playbook.md"),
              os.path.join(HERE, "..", "skills", "freecad-design", "SKILL.md"),
              os.path.join(HERE, "..", "prompt", "SKILL.md")):
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def load_playbook():
    p = playbook_path()
    if not p:
        return None
    with open(p) as fh:
        text = fh.read()
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    return re.sub(r"<!--.*?-->\n?", "", text, flags=re.S).strip()


def reference_files():
    p = playbook_path()
    d = os.path.join(os.path.dirname(p), "references") if p else None
    if not d or not os.path.isdir(d):
        return {}
    return {os.path.splitext(f)[0]: os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".md")}


def playbook_sections(text):
    """[{level, heading, number, sub, text}] for every ## and ### heading of the playbook
    (outside code fences). A ## section's text includes its ### sub-sections."""
    lines = text.splitlines(True)
    heads, fence = [], False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            fence = not fence
        elif not fence and re.match(r"^#{2,3} \S", ln):
            heads.append((i, ln.count("#", 0, 3)))
    out, sub, major = [], 0, None
    for k, (i, lvl) in enumerate(heads):
        end = next((j for j, l2 in heads[k + 1:] if l2 <= lvl), len(lines))
        head = lines[i].lstrip("#").strip()
        num = re.match(r"^(\d+)\.", head)
        if lvl == 2:
            sub, major = 0, (num.group(1) if num else head)
        else:
            sub += 1
        out.append({"level": lvl, "heading": head, "major": major, "sub": sub if lvl == 3 else 0,
                    "text": "".join(lines[i:end]).strip()})
    return out


# keywords people ask for that no heading contains
GUIDE_ALIASES = {"drawing": "document", "drawings": "document", "techdraw": "document", "annotate": "document",
                 "assembly": "layout", "assemblies": "layout", "joint": "layout", "joints": "layout",
                 "thread": "holes", "threads": "holes", "fastener": "holes", "fasteners": "holes", "fit": "holes",
                 "gui": "edits the user made", "tools": "tool quick reference", "done": "definition of done"}
_CIRCLED = "①②③④⑤⑥⑦⑧⑨"


def t_guide(args):
    text = load_playbook()
    if not text:
        raise ToolError("playbook not found next to the server (skills/freecad-design/SKILL.md) — reinstall "
                        "the plugin")
    topic = (args.get("topic") or "").strip().lower()
    refs = reference_files()
    if not topic:
        return text + ("\n\n---\nReference pages (guide(topic=…)): %s" % ", ".join(refs) if refs else "")
    if topic in refs:
        with open(refs[topic]) as fh:
            return fh.read()
    secs = playbook_sections(text)
    t = topic.lstrip("§").strip()
    hits = []
    m = re.match(r"^(\d+)(?:\.(\d+))?\.?$", t)
    if m:                                    # "4" → §4; "1.6" → §1's 6th step (⑥)
        hits = [s for s in secs if s["major"] == m.group(1)
                and ((s["level"] == 2 and not m.group(2)) or (m.group(2) and s["sub"] == int(m.group(2))))]
    elif t in _CIRCLED:
        hits = [s for s in secs if s["level"] == 3 and s["heading"].startswith(t)]
    else:
        for key in (t, GUIDE_ALIASES.get(t)):
            if key:
                hits = [s for s in secs if key in s["heading"].lower()]
                if hits:
                    break
        # a ### that sits inside a matched ## is already part of it
        tops = {s["major"] for s in hits if s["level"] == 2}
        hits = [s for s in hits if s["level"] == 2 or s["major"] not in tops]
    if not hits:
        heads = ["%s%s" % ("  " if s["level"] == 3 else "", s["heading"]) for s in secs]
        raise ToolError("no guide topic '%s'. Ask for one of these:\n  reference pages: %s\n  a section number "
                        "(e.g. '4', or '1.6' for step ⑥ of the loop) or a word from a heading:\n    %s\n  "
                        "or no topic for the whole playbook." % (
                            args.get("topic"), ", ".join(refs) or "none", "\n    ".join(heads)))
    return "\n\n".join(s["text"] for s in hits)


PROJECT_README = """# {name}

{description}

| File | Purpose |
|---|---|
| `project.json` | process, material, drawing standard, defaults |
| `requirements.md` | requirements, each with its source |
| `params.json` | driving dimensions (value, unit, source) |
| `model.py` | build script — the source of truth |
| `checks.json` | acceptance checks |
| `refs/` | datasheets, reference drawings, vendor STEP |
| `build/` | generated FreeCAD documents |
| `out/` | renders and exports |

Units are mm. Z up; the front view looks toward +Y.
"""

REQUIREMENTS = """# {name} — requirements

Every number carries a source: [file p.N], [user], [standard via lookup], or [assumption].

## Function
-

## Interfaces (what it mounts to / what mounts to it)
| Interface | Feature | Value | Tolerance | Source |
|---|---|---|---|---|

## Envelope and keep-outs
-

## Loads and environment
-

## Process and material
- {process} · {material}

## Acceptance checks (mirrored in checks.json)
-

## Assumptions
-

## Open questions
-
"""

MODEL_PART = '''# {name} — model.py
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. Z up; the front view looks toward +Y. Build with the `build` tool and read the
# whole report after every build — a failed feature can be invisible in the render.

P = fc.params()                    # params.json → Params spreadsheet; P.name is bound by expression

body = fc.body("{label}")
fc.pad(body, fc.sketch(body, "XY", fc.rrect(P.width, P.depth, r=P.corner), name="BaseSketch"),
       P.thickness, name="Base")
fc.holes(body, on=fc.face(body, "+Z"), at=fc.rect_pattern(P.hole_pitch_x, P.hole_pitch_y),
         spec="M3 clearance normal", name="MountHoles")
'''

MODEL_CNC = '''# {name} — model.py  (CNC milling)
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. Z up; the front view looks toward +Y. Build with the `build` tool and read the
# whole report after every build — a failed feature can be invisible in the render.

P = fc.params()                    # params.json → Params spreadsheet; P.name is bound by expression

body = fc.body("{label}")
fc.pad(body, fc.sketch(body, "XY", fc.rect(P.width, P.depth), name="BlockSketch"), P.height, name="Block")
# Pocket: inside vertical corners get a radius ≥ the end mill's radius (a cutter can't make a sharp one).
fc.pocket(body, fc.sketch(body, fc.face(body, "+Z"), fc.rrect(P.pocket_w, P.pocket_d, r=P.pocket_r),
                          name="PocketSketch"), P.pocket_depth, name="Pocket")
fc.holes(body, on=fc.face(body, "+Z"), at=fc.rect_pattern(P.hole_pitch_x, P.hole_pitch_y),
         spec="M4 tapped", depth=P.tap_depth, name="TappedHoles")
'''

MODEL_TURNED = '''# {name} — model.py  (CNC turning)
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. The turning axis is Z, starting at z = 0. Build with the `build` tool and read the
# whole report after every build.

P = fc.params()

body = fc.body("{label}")
# Diameter steps from z = 0 outward: (Ø, length) or (Ø, length, "k6") for a fit (drawn with its limits).
fc.turned(body, [(P.collar_d, P.collar_l), (P.shaft_d, P.shaft_l)], name="Turned")
# Lathe-friendly: chamfer both ends. Threads: fc.external_thread(body, "M8", length, z_from=…);
# cross holes are secondary ops: fc.holes(body, on="XZ", offset=r + 1, …).
fc.chamfer(body, fc.edges(body, kind="circle", on=fc.face(body, "+Z")), P.chamfer, name="EndChamfer")
fc.chamfer(body, fc.edges(body, kind="circle", on=fc.face(body, "-Z")), P.chamfer, name="CollarChamfer")
'''

MODEL_PLATE = '''# {name} — model.py  (laser / waterjet / sheet blank)
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. Z up. One uniform thickness; holes at least the thickness; hole edge distance ≥ 1–1.5 × t.

P = fc.params()

body = fc.body("{label}")
fc.pad(body, fc.sketch(body, "XY", fc.rrect(P.width, P.depth, r=P.corner), name="Profile"),
       P.thickness, name="Plate")
fc.holes(body, on=fc.face(body, "+Z"), at=fc.rect_pattern(P.hole_pitch_x, P.hole_pitch_y),
         spec="M4 clearance normal", name="Holes")
'''

MODEL_SHEET = '''# {name} — model.py  (bent sheet metal)
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. fc.sheet_metal builds the folded part and its flat pattern from the same numbers.
# The base lies in the XY plane (z = 0 … thickness) and its outline is the OUTSIDE footprint: a
# flange's outer face is flush with its edge. Flange lengths are outside dimensions; up = toward +Z.
# Base edges of fc.rect: 0 = −Y, 1 = +X, 2 = +Y, 3 = −X (or name them "+X" …); "flange1" is the
# far edge of the first flange. Holes on a flange use its plane's global axes: (y, z) facing ±X.
# Rules: bend radius ≥ thickness, flange ≥ 3 × thickness + radius, hole edge ≥ 2 × thickness + radius
# from a bend line — check_manufacturing checks them and reports the flat pattern and bends.

P = fc.params()

base_holes = [(-P.hole_offset, -P.hole_pitch / 2), (-P.hole_offset, P.hole_pitch / 2)]      # (x, y) on the base
flange_holes = [(-P.hole_pitch / 2, P.flange_hole_z), (P.hole_pitch / 2, P.flange_hole_z)]  # (y, z) on the +X flange
fc.sheet_metal("{label}", P.thickness, fc.rect(P.width, P.depth),
               flanges=[{{"edge": "+X", "length": P.flange_height, "angle": 90, "up": True}}],
               holes=[{{"on": "base", "at": base_holes, "spec": "M4 clearance normal"}},
                      {{"on": "flange1", "at": flange_holes, "spec": "M4 clearance normal"}}],
               radius=P.bend_radius)
'''

MODEL_ASSEMBLY = '''# {name} — model.py
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. Z up; the front view looks toward +Y.

P = fc.params()

# Reference (bought-out) part: a simplified envelope that keeps the true interface
# geometry, with a named frame on its mounting face. Replace with step="refs/<file>.step".
# bom="buy" (with qty) if it is bought with this order, "context" (default) if the customer
# already has it, "none" for keep-outs and envelopes.
ref = fc.ref_part("Reference", shape=Part.makeBox(40, 40, 20, Vector(-20, -20, -20)),
                  frames={{"mount": fc.frame(0, 0, 0, normal="+Z")}})   # mounting face, outward normal

body = fc.body("{label}")
fc.pad(body, fc.sketch(body, "XY", fc.rrect(P.width, P.depth, r=P.corner), name="BaseSketch"),
       P.thickness, name="Base")
fc.holes(body, on=fc.face(body, "+Z"), at=fc.rect_pattern(P.hole_pitch_x, P.hole_pitch_y),
         spec="M3 clearance normal", name="MountHoles")
# Named interface frame on the plate: the seat for the reference part, centre of the underside,
# +Z along the outward normal (-Z). Joints use named frames, never Face/Edge numbers.
fc.set_frames(body, {{"seat": fc.frame(0, 0, 0, normal="-Z")}})

# FreeCAD Assembly workbench: ground the plate, join the reference part to it by the named
# frames ("against" = face to face), then solve — fc.solve verifies every joint holds.
asm = fc.assembly("Assembly")
fc.add(asm, body, ref)
fc.ground(asm, body)
fc.joint(asm, "fixed", (body, "seat"), (ref, "mount"), mode="against", name="RefMount")
fc.solve(asm)
'''

MODEL_EMPTY = '''# {name} — model.py
# Runs inside FreeCAD with `fc` (fclib) preloaded; also App, Part, Sketcher, Vector, doc.
# Units mm. Z up; the front view looks toward +Y.

P = fc.params()
'''

def _ph(v, src="placeholder"):
    return {"value": v, "unit": "mm", "source": src}


PARAMS_DEFAULT = {
    "width": _ph(60, "placeholder — replace from requirements"), "depth": _ph(40), "thickness": _ph(5),
    "corner": _ph(3), "hole_pitch_x": _ph(50), "hole_pitch_y": _ph(30),
}
PARAMS_CNC = {
    "width": _ph(80, "placeholder — replace from requirements"), "depth": _ph(50), "height": _ph(15),
    "pocket_w": _ph(50), "pocket_d": _ph(26), "pocket_r": _ph(3, "placeholder — ≥ end mill radius (R3 → Ø6 cutter)"),
    "pocket_depth": _ph(6, "placeholder — ≤ 4 × cutter Ø"), "hole_pitch_x": _ph(66), "hole_pitch_y": _ph(36),
    "tap_depth": _ph(10, "placeholder — thread engagement ≥ 1.5 × D in aluminium"),
}
PARAMS_TURNED = {
    "collar_d": _ph(20, "placeholder — replace from requirements"), "collar_l": _ph(5),
    "shaft_d": _ph(12), "shaft_l": _ph(25), "chamfer": _ph(0.5, "general turning chamfer"),
}
PARAMS_PLATE = {
    "width": _ph(80, "placeholder — replace from requirements"), "depth": _ph(50),
    "thickness": _ph(3, "placeholder — the sheet thickness"), "corner": _ph(3),
    "hole_pitch_x": _ph(66, "placeholder — keep hole edges ≥ 1.5 × thickness from the outline"), "hole_pitch_y": _ph(36),
}
PARAMS_SHEET = {
    "thickness": _ph(2, "placeholder — the sheet thickness"),
    "width": _ph(60, "placeholder — outside size along X, replace from requirements"),
    "depth": _ph(40, "placeholder — outside size along Y (the bend line's length)"),
    "flange_height": _ph(30, "placeholder — outside height, ≥ 3 × thickness + bend radius"),
    "bend_radius": _ph(2, "placeholder — inside bend radius ≥ thickness; ask the shop for its tooling"),
    "hole_pitch": _ph(20), "hole_offset": _ph(10, "placeholder — base holes at x = −hole_offset"),
    "flange_hole_z": _ph(18, "placeholder — hole edge ≥ 2 × thickness + radius from the bend line"),
}
# part template per process: (model.py, params.json, what it is)
SCAFFOLDS = {"fdm": (MODEL_PART, PARAMS_DEFAULT, "a plate with M3 clearance holes"),
             "sla": (MODEL_PART, PARAMS_DEFAULT, "a plate with M3 clearance holes"),
             "cnc": (MODEL_CNC, PARAMS_CNC, "a block with an R3-cornered pocket and M4 tapped holes"),
             "turning": (MODEL_TURNED, PARAMS_TURNED, "a turned collar + shaft (fc.turned) with chamfered ends"),
             "laser": (MODEL_PLATE, PARAMS_PLATE, "a uniform 3 mm plate with M4 clearance holes"),
             "sheet": (MODEL_SHEET, PARAMS_SHEET, "a 2 mm bent L bracket (fc.sheet_metal: base + one 90° flange) "
                                                  "with M4 clearance holes")}
HOLE_COMPENSATION = {"fdm": 0.2, "sla": 0.1}


def project_material(process, name=None, density=None):
    """(project.json material, reply line, extra parts.<label> keys). Density: the
    density_g_cm3 argument (also set on the part, which the build reads first), else the
    material table — the same lookup the build makes for parts.<label>.material — else
    the process default, recorded as "assumed" in density_source and in the reply."""
    default = MATERIALS[process]
    mat = {"name": str(name or "").strip() or default["name"]}
    if density:
        d = float(density)
        if d <= 0:
            raise ToolError("density_g_cm3 must be > 0 (g/cm³), got %g" % d)
        mat.update(density_g_cm3=d, density_source="given (density_g_cm3)")
        return mat, "Density: %g g/cm³ (given) — also set on the part, so its mass uses it" % d, {"density_g_cm3": d}
    d, key = standards.material_density(mat["name"])
    if d:
        mat.update(density_g_cm3=d, density_source="material table ('%s')" % key)
        return mat, "Density: %g g/cm³ for %s (material table: '%s')" % (d, mat["name"], key), {}
    d = default["density_g_cm3"]
    mat.update(density_g_cm3=d, density_source="assumed (%s default, %s) — '%s' is not in the material table"
               % (process, default["name"], mat["name"]))
    return mat, ("⚠ Density %g g/cm³ is ASSUMED: '%s' is not in the material table, so the %s default (%s) is used "
                 "and every mass will be off unless it happens to match. Ask for (or look up) the real density and "
                 "set project.json material.density_g_cm3, or pass density_g_cm3." % (d, mat["name"], process,
                                                                                    default["name"])), {}


def t_create_project(args):
    name = (args.get("name") or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9._ -]+$", name):
        raise ToolError("name is required: letters, digits, space, dot, dash, underscore")
    parent = resolve(args.get("parent_dir") or home_dir(), kind="parent_dir")
    pdir = os.path.join(parent, name)
    if os.path.exists(pdir) and os.listdir(pdir):
        raise ToolError("directory already exists and is not empty: %s" % pdir)
    template = (args.get("template") or "part").lower()
    if template not in ("part", "assembly", "empty"):
        raise ToolError("template must be part | assembly | empty")
    process = (args.get("process") or "fdm").lower()
    if process not in MATERIALS:
        raise ToolError("process must be one of %s" % " | ".join(sorted(MATERIALS)))
    material, density_note, part_density = project_material(process, args.get("material"), args.get("density_g_cm3"))
    for d in ("refs", "build", "out"):
        os.makedirs(os.path.join(pdir, d), exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9 ]+", " ", name).title().replace(" ", "") or "Part"
    if label[0].isdigit():
        label = "Part" + label
    desc = args.get("description") or "FreeCAD project."
    model, params, what = {"part": SCAFFOLDS[process], "assembly": (MODEL_ASSEMBLY, PARAMS_DEFAULT, "a plate with "
                           "M3 clearance holes, grounded in an Assembly, with a reference part joined to it by named "
                           "frames"), "empty": (MODEL_EMPTY, {}, None)}[template]
    if label in set(re.findall(r'name="(\w+)"', model)) | {"Params", "Assembly", "Reference", "Joints"}:
        label += "Part"           # a feature with the body's name would be renamed '<name>001'
    project = {
        "name": name, "description": desc, "units": "mm", "process": process, "material": material,
        "clearance": {"default_min_mm": 0.3 if process in ("fdm", "sla") else 0.1},
        "drawing": {"standard": "ISO", "projection": "first-angle", "general_tolerance": "ISO 2768-m",
                    "paper": "auto"},
        "title_block": {"author": "", "company": "", "drawing_number": ""},
        "exports": ["step", "3mf"] if process in ("fdm", "sla") else ["step"],
        "parts": {} if template == "empty" else {label: dict({"process": process, "material": material["name"],
                                                              "finish": "", "qty": 1}, **part_density)},
    }
    if process in HOLE_COMPENSATION:
        # diametral, added by fc.holes to clearance and plain holes of fdm/sla parts (not tapped/insert/fit holes)
        project["dfm"] = {"hole_compensation_mm": HOLE_COMPENSATION[process]}
    files = {
        "project.json": json.dumps(project, indent=2) + "\n",
        "params.json": json.dumps(params, indent=2) + "\n",
        "model.py": model.format(name=name, label=label),
        "checks.json": "[]\n",
        "requirements.md": REQUIREMENTS.format(name=name, process=process, material=material["name"]),
        "README.md": PROJECT_README.format(name=name, description=desc),
    }
    for fn, content in files.items():
        with open(os.path.join(pdir, fn), "w") as fh:
            fh.write(content)
    lines = ["Created project %s (%s, %s)" % (pdir, process, material["name"])]
    lines += ["  " + f for f in files] + ["  refs/ build/ out/", density_note]
    if what:
        lines.append("Starter model: %s — a placeholder to replace from the requirements." % what)
        lines.append("Body label: \"%s\" — project.json \"parts\" uses this label as its key (process, material, "
                     "finish, qty). If you rename the body in model.py, rename the key too: release stops on "
                     "parts keys that match no object." % label)
    if process in HOLE_COMPENSATION:
        lines.append("Hole compensation: project.json dfm.hole_compensation_mm = %g — fc.holes enlarges clearance "
                     "and plain holes by this (diametral) so they print true; tapped, insert and fit holes are not "
                     "changed. Pass compensation=0 (or a number) to fc.holes to override one feature."
                     % HOLE_COMPENSATION[process])
    lines.append("Next: fill requirements.md and params.json from the brief and datasheets, record each part's "
                 "process/material/finish/qty in project.json \"parts\", edit model.py, then build(project=%r)."
                 % name)
    return "\n".join(lines)


def t_list_projects(args):
    root = resolve(args.get("root") or home_dir(), must_exist=True, kind="root")
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("build", "out", "refs")]
        if dirpath[len(root):].count(os.sep) >= int(args.get("max_depth", 3)):
            dirnames[:] = []
        if "model.py" in filenames or "project.json" in filenames:
            fcs = default_doc(dirpath)
            built = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fcs))) \
                if os.path.isfile(fcs) else "never"
            rows.append("%s   (built: %s)" % (dirpath, built))
            dirnames[:] = []
        else:
            fcstd = sorted(f for f in filenames if f.lower().endswith(".fcstd"))
            if fcstd:
                rows.append("%s   FreeCAD files: %s" % (dirpath, ", ".join(fcstd[:8])))
    return "\n".join(rows) if rows else "No projects under %s." % root


def _file_path(args, must_exist):
    pdir = project_dir(args["project"]) if args.get("project") else None
    return resolve(args.get("path"), base=pdir, must_exist=must_exist, kind="path"), pdir


def t_read_file(args):
    p, _ = _file_path(args, True)
    if os.path.isdir(p):
        return "%s\n%s" % (p, "\n".join("  " + f for f in sorted(os.listdir(p)) if not f.startswith(".")))
    with open(p, "r", errors="replace") as fh:
        lines = fh.read().splitlines()
    start = max(1, int(args.get("start_line", 1)))
    end = len(lines) if args.get("end_line") in (None, "") else min(len(lines), int(args["end_line"]))
    if start > len(lines):
        return "%s has %d lines; start_line %d is past the end." % (p, len(lines), start)
    width = len(str(end))
    body = "\n".join("%*d  %s" % (width, i, lines[i - 1]) for i in range(start, end + 1))
    head = "%s  (%d lines%s)" % (p, len(lines), ", showing %d-%d" % (start, end)
                                 if (start > 1 or end < len(lines)) else "")
    return head + "\n" + body


def t_write_file(args):
    p, pdir = _file_path(args, False)
    content = args.get("content")
    if content is None:
        raise ToolError("content is required")
    ext = os.path.splitext(p)[1].lower()
    binary = (args.get("encoding") or "utf-8") == "base64"
    if binary:
        if ext not in BINARY_EXT | TEXT_EXT:
            raise ToolError("refusing to write '%s' as base64: binary writes take reference files only (%s)"
                            % (ext or "(no extension)", " ".join(sorted(BINARY_EXT))))
        raw = "".join(content.split())
        if raw.startswith("data:") and "base64," in raw:          # tolerate a data: URL
            raw = raw.split("base64,", 1)[1]
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ToolError("content is not valid base64 (%s) — send the file's bytes base64-encoded "
                            "(standard alphabet, padded)" % e)
        if len(data) > MAX_BINARY:
            raise ToolError("%s is %.1f MB — the limit is %d MB; ask the user to copy it into the project's refs/ "
                            "folder themselves" % (os.path.basename(p), len(data) / 1048576.0, MAX_BINARY // 1048576))
        root = pdir if pdir and _inside(p, pdir) else find_project_root(p)
        top = os.path.relpath(p, root).split(os.sep)[0] if root else None
        if top in ("build", "out", "release", ".history"):
            raise ToolError("refusing to write into %s/: the server generates that folder. Put reference files in "
                            "refs/ (e.g. path=\"refs/%s\")" % (top, os.path.basename(p)))
    elif ext in BINARY_EXT:
        raise ToolError("refusing to write '%s' as text: it is a binary type — pass the file's bytes base64-encoded "
                        "with encoding=\"base64\" (≤ %d MB)" % (ext, MAX_BINARY // 1048576))
    else:
        check_text_file(p)
    exists = os.path.exists(p)
    if exists and not args.get("overwrite"):
        raise ToolError("%s already exists — pass overwrite=true (a backup is kept), or use edit_file" % p)
    if os.path.isdir(p):
        raise ToolError("%s is a directory — give a file path" % p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    bak = backup_file(p, pdir) if exists else None
    if binary:
        with open(p, "wb") as fh:
            fh.write(data)
        size = "%.1f KB" % (len(data) / 1024.0)
    else:
        with open(p, "w") as fh:
            fh.write(content)
        size = "%d lines" % (content.count("\n") + 1)
    pdir = pdir or find_project_root(p)
    return "%s %s (%s)%s" % ("Overwrote" if exists else "Wrote", p, size,
                             "; previous version backed up to %s" % _rel(bak, pdir) if bak else "")


def t_edit_file(args):
    p, pdir = _file_path(args, True)
    check_text_file(p)
    old, new = args.get("old_text"), args.get("new_text")
    if old is None or new is None:
        raise ToolError("old_text and new_text are both required")
    if old == new:
        raise ToolError("old_text and new_text are identical")
    with open(p, "r", errors="replace") as fh:
        src = fh.read()
    count = src.count(old)
    if count == 0:
        raise ToolError("old_text not found in %s (whitespace must match exactly)" % p)
    if count > 1 and not args.get("replace_all"):
        raise ToolError("old_text appears %d times in %s — add context or pass replace_all=true" % (count, p))
    bak = backup_file(p, pdir)
    with open(p, "w") as fh:
        fh.write(src.replace(old, new) if args.get("replace_all") else src.replace(old, new, 1))
    return "Edited %s — %d replacement%s (backup %s)" % (
        p, count if args.get("replace_all") else 1, "s" if args.get("replace_all") and count > 1 else "",
        _rel(bak, pdir or find_project_root(p)))


def _append_text(content, extra):
    """Add lines to a tool result (a string, or content blocks whose first block is text)."""
    if not extra:
        return content
    if isinstance(content, list):
        first = dict(content[0], text=content[0].get("text", "") + "\n" + extra)
        return [first] + content[1:]
    return content + "\n" + extra


def gui_reload_hint():
    """A line for build output when the FreeCAD GUI is open but MCP Reload is not installed."""
    try:
        if not gui_pids():
            return None
        d = fc_user_dirs()
        if reload_helper_state(d["user_data"], d.get("macro_dir"))["addon_installed"]:
            return None
    except ToolError:
        return None
    return ("FreeCAD is open — if it shows this file, File ▸ Revert to see the new build (or install the reload "
            "helper: open_in_gui(install_reload_helper=true), with the user's OK).")


_reload_hinted = set()          # projects whose build output already carried the "File ▸ Revert" hint


def t_build(args, request_id=None):
    pdir = project_dir(args.get("project"))
    wargs = {"project_dir": pdir, "params": args.get("params") or {}, "variant": args.get("variant"),
             "views": args.get("views", ["iso"]), "size": args.get("size"), "force": bool(args.get("force")),
             "adopt_gui_params": bool(args.get("adopt_gui_params")), "skip_checks": bool(args.get("skip_checks"))}
    timeout = int(args.get("timeout", 300))
    if args.get("wait") is False:
        return maybe_background("build", args, "build", wargs, timeout, request_id, "build %s%s" % (
            project_name(pdir), " [%s]" % args["variant"] if args.get("variant") else ""))
    res = run_worker("build", wargs, timeout=timeout, request_id=request_id)
    out = as_content(res, return_image=args.get("return_image", True))
    data = res.get("data") or {}
    # the hint matters only when a new revision was written ("inputs unchanged" rewrites nothing the GUI shows),
    # and once per project per session is enough
    if data.get("rev") and not data.get("reused") and pdir not in _reload_hinted:
        hint = gui_reload_hint()
        if hint:
            _reload_hinted.add(pdir)
            out = _append_text(out, hint)
    return out


def t_run_script(args, request_id=None):
    code = args.get("code")
    if not code:
        raise ToolError("code is required")
    pdir = project_dir(args["project"]) if args.get("project") else None
    doc = resolve(args["doc"], base=pdir, must_exist=True, kind="doc") if args.get("doc") else None
    save = args.get("save") or "no"
    if save not in ("no", "revision", "inplace"):
        raise ToolError("save must be no | revision | inplace")
    wargs = {"code": code, "doc": doc, "project_dir": pdir, "save": save,
             "save_as": resolve(args["save_as"], base=pdir) if args.get("save_as") else None,
             "from_older": bool(args.get("from_older")), "views": args.get("views"), "size": args.get("size")}
    if args.get("views"):
        wargs["image_path"] = os.path.join(image_dir(doc, pdir), "run_script_%s.png" % time.strftime("%H%M%S"))
    return maybe_background("run_script", args, "run_script", wargs, int(args.get("timeout", 300)),
                            request_id, "run_script on %s" % (doc or "a new document"))


def t_inspect(args, request_id=None):
    doc, pdir = doc_and_project(args)
    wargs = {"doc": doc, "project_dir": pdir, "object": args.get("object"), "level": args.get("level"),
             "max_faces": args.get("max_faces", 80)}
    return as_content(run_worker("inspect", wargs, timeout=int(args.get("timeout", 120)),
                                 request_id=request_id), return_image=False)


def t_render(args, request_id=None):
    doc, pdir = doc_and_project(args)
    views = args.get("views") or ["iso"]
    if args.get("out_path"):
        out = resolve(args["out_path"], base=pdir)
    else:
        base = image_dir(doc, pdir)
        tag = "_".join(v if isinstance(v, str) else "custom" for v in views[:4])
        if args.get("section"):
            tag += "_section"
        if args.get("style") and args["style"] != "shaded":
            tag += "_" + args["style"]
        out = os.path.join(base, "%s_%s.png" % (os.path.splitext(os.path.basename(doc))[0], tag))
    wargs = {"doc": doc, "project_dir": pdir, "views": views, "objects": args.get("objects"),
             "highlight": args.get("highlight"), "style": args.get("style"), "section": args.get("section"),
             "size": args.get("size"), "hidden": args.get("hidden", True), "labels": args.get("labels", True),
             "out": out}
    return maybe_background("render", args, "render", wargs, int(args.get("timeout", 180)), request_id,
                            "render %s %s" % (os.path.basename(doc), ",".join(map(str, views))))


def t_job_status(args):
    job_id = args.get("job_id")
    if not job_id:
        with _jobs_lock:
            jobs = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)
        if not jobs:
            return "No background jobs this session."
        return "\n".join(["%d job(s):" % len(jobs)] + [
            "  %-11s %-9s %5.0fs  %s" % (j["id"], j["status"], j.get("elapsed", time.time() - j["started"]),
                                         j["describe"]) for j in jobs[:25]])
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise ToolError("unknown job '%s' — call job_status with no job_id to list jobs" % job_id)
    wait = min(float(args.get("wait_seconds") or 0), 60.0)
    if wait and job["status"] == "running" and job.get("thread") is not None:
        job["thread"].join(wait)
    if job["status"] == "running":
        return "Job %s still running (%.0fs): %s. Call again, optionally with wait_seconds." % (
            job_id, time.time() - job["started"], job["describe"])
    if job["status"] == "cancelled":
        return "Job %s was cancelled." % job_id
    return as_content(job["result"], return_image=args.get("return_image", True))


def t_open_in_gui(args):
    fcinfo = require_freecad()
    note = ""
    was_running = bool(gui_pids())
    if args.get("install_reload_helper"):
        try:
            d = fc_user_dirs()
            before = reload_helper_state(d["user_data"], d.get("macro_dir"))["addon_installed"]
        except (ToolError, KeyError, TypeError):
            before = False
        dst, macro = install_reload_helper()
        note = "\n  reload helper %s: addon %s, macro %s — it starts with FreeCAD from now on." % (
            "updated (it was already installed)" if before else "installed", dst, macro)
        if was_running and before:
            # the macro is a toggle: run while the watcher is already running, it stops it
            note += (" FreeCAD is already running and the helper was installed before, so it is probably running "
                     "already: look in FreeCAD's Report view (View ▸ Panels ▸ Report view) for 'MCP Reload: "
                     "watching'. If it is there, do nothing — the MCP_Reload macro is a toggle and would stop it. "
                     "Only if it is absent, run Macro ▸ Macros… ▸ MCP_Reload once (or restart FreeCAD).")
        elif was_running:
            note += (" FreeCAD is already running, so start it there now: run Macro ▸ Macros… ▸ MCP_Reload once "
                     "(it prints 'MCP Reload: watching'), or restart FreeCAD.")
        elif args.get("launch") is not False:
            note += " The helper started with FreeCAD."
    if args.get("launch") is False:
        return "Not launching FreeCAD (launch=false)." + note
    target = None
    if args.get("project") and not args.get("path"):
        target = default_doc(project_dir(args["project"]))
        if not os.path.isfile(target):
            raise ToolError("project not built yet — call build first")
    elif args.get("path"):
        pdir = project_dir(args["project"]) if args.get("project") else None
        target = resolve(args["path"], base=pdir, must_exist=True)
    if fcinfo.get("app") and sys.platform == "darwin":
        r = subprocess.run(["open", "-a", fcinfo["app"]] + ([target] if target else []),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        if r.returncode != 0:
            raise ToolError("could not launch FreeCAD: %s" % r.stdout.decode("utf-8", "replace").strip())
    elif fcinfo.get("gui"):
        subprocess.Popen([fcinfo["gui"]] + ([target] if target else []), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        raise ToolError("no FreeCAD GUI binary found")
    for _ in range(40):
        if gui_pids():
            break
        time.sleep(0.25)
    state = "was already running" if was_running else ("started" if gui_pids() else "launch requested")
    msg = "FreeCAD GUI %s.%s" % (state, "\n  opened %s" % target if target else "")
    if was_running and target and not note:
        msg += ("\n  If this file was already open, FreeCAD keeps its old copy unless the MCP Reload helper is "
                "running (open_in_gui with install_reload_helper=true installs it, with the user's OK); "
                "otherwise use File ▸ Revert.")
    return msg + note


def image_dir(doc=None, pdir=None):
    """Where a read-only tool puts an image the caller did not place: the project's out/
    (doc inside a project), else ~/.cache/freecad-mcp/images — never an out/ folder next
    to a user's own file."""
    pdir = pdir or (find_project_root(doc) if doc else None)
    if pdir:
        return os.path.join(pdir, "out")
    return os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "images")


def _out_png(doc, pdir, tag):
    return os.path.join(image_dir(doc, pdir), "%s_%s_%s.png" % (os.path.splitext(os.path.basename(doc))[0], tag,
                                                                time.strftime("%H%M%S")))


def t_measure(args, request_id=None):
    doc, pdir = doc_and_project(args)
    queries = args.get("queries")
    if not queries:
        if not args.get("a"):
            raise ToolError("pass queries=[{a, b?}] or a (and optionally b)")
        queries = [{"a": args["a"], "b": args.get("b")}]
    wargs = {"doc": doc, "project_dir": pdir, "queries": queries, "tol": args.get("tol", 0.01)}
    return as_content(run_worker("measure", wargs, timeout=int(args.get("timeout", 180)), request_id=request_id),
                      return_image=False)


def t_check_fit(args, request_id=None):
    doc, pdir = doc_and_project(args)
    wargs = {"doc": doc, "project_dir": pdir, "objects": args.get("objects"),
             "min_clearance": args.get("min_clearance"), "contact_ok": args.get("contact_ok"),
             "interference_ok": args.get("interference_ok"), "pair_min": args.get("pair_min"),
             "within": args.get("within", 5.0),
             "render": bool(args.get("render")), "out": _out_png(doc, pdir, "fit"), "size": args.get("size")}
    return maybe_background("check_fit", args, "check_fit", wargs, int(args.get("timeout", 300)), request_id,
                            "check_fit %s" % os.path.basename(doc))


def t_section(args, request_id=None):
    doc, pdir = doc_and_project(args)
    if not args.get("plane"):
        raise ToolError("plane is required, e.g. {\"z\": 12} or {\"point\": [0,0,0], \"normal\": [0,1,0]}")
    wargs = {"doc": doc, "project_dir": pdir, "plane": args["plane"], "objects": args.get("objects"),
             "within": args.get("within", 5.0), "min_clearance": args.get("min_clearance"),
             "contact_ok": args.get("contact_ok"), "interference_ok": args.get("interference_ok"),
             "out": _out_png(doc, pdir, "section"), "size": args.get("size")}
    return as_content(run_worker("section", wargs, timeout=int(args.get("timeout", 180)), request_id=request_id),
                      return_image=args.get("return_image", True))


_ISO286_CLASS = re.compile(r"^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$")       # 'H7', 'k6', 'JS7'


def t_lookup_standard(args):
    out = []
    d = None if args.get("d") is None else float(args["d"])
    try:
        if args.get("size"):
            out.append(standards.metric_report(args["size"]))          # a bearing code ('608') → its bearing
        if args.get("fit"):
            if d is None:
                raise ToolError("fit needs d (nominal diameter in mm), e.g. lookup_standard(fit=%r, d=8)" % args["fit"])
            out.append(standards.fit_report(d, args["fit"]))           # 'H7/g6', one class 'k6', or 'ISO 2768-m'
        if args.get("tolerance"):
            spec = str(args["tolerance"]).strip()
            if "/" in spec or _ISO286_CLASS.match(spec.replace(" ", "")):
                if d is None:
                    raise ToolError("tolerance=%r is an ISO 286 class — it needs d (nominal diameter in mm)" % spec)
                out.append(standards.fit_report(d, spec.replace(" ", "")))
            else:                                                      # ISO 2768: the band at d, or the table
                out.append(standards.general_tolerance_report(spec, d))
        if args.get("bearing"):
            out.append(standards.bearing_report(args["bearing"]))
        if args.get("material"):
            out.append(standards.material_report(args["material"]))
        if args.get("process"):
            rules = standards.PROCESS_RULES.get(args["process"].lower())
            if not rules:
                raise ToolError("process must be one of %s" % ", ".join(sorted(standards.PROCESS_RULES)))
            out.append("%s design rules\n%s" % (args["process"].upper(), "\n".join("  - " + r for r in rules)))
    except (ValueError, KeyError) as e:
        raise ToolError(str(e.args[0] if e.args else e))
    if not out:
        return ("lookup_standard(size='M3') — holes, threads, heads, nuts, washers, inserts\n"
                "lookup_standard(fit='H7/g6', d=8) — ISO 286 limits and fit type; fit='k6' (one class) → its limits\n"
                "lookup_standard(tolerance='ISO 2768-m', d=80) — general tolerance at a size (without d: the table)\n"
                "lookup_standard(bearing='608') — deep-groove ball bearing d, D, B, r_s min, seat fits\n"
                "lookup_standard(process='fdm') — process rules of thumb\n"
                "sizes: %s · hole classes %s · shaft classes %s · up to 250 mm\n"
                "bearings: %s\nprocesses: %s" % (" ".join(standards.SIZES), standards.HOLE_CLASSES,
                                                 standards.SHAFT_CLASSES, standards.BEARING_RANGES,
                                                 ", ".join(sorted(standards.PROCESS_RULES))))
    return "\n\n".join(out)


def t_export(args, request_id=None):
    doc, pdir = doc_and_project(args)
    out_dir = resolve(args["out_dir"], base=pdir) if args.get("out_dir") else \
        os.path.join(pdir or os.path.dirname(doc), "out", "export")
    wargs = {"doc": doc, "project_dir": pdir, "objects": args.get("objects"),
             "formats": args.get("formats") or ["step"], "out_dir": out_dir,
             "tolerance": args.get("tolerance", 0.02), "angular_deg": args.get("angular_deg", 5.0),
             "view": args.get("view")}
    return as_content(run_worker("export", wargs, timeout=int(args.get("timeout", 300)), request_id=request_id),
                      return_image=False)


def _project_json(pdir):
    try:
        with open(os.path.join(pdir, "project.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}


DRAWING_KEYS = ("views", "style", "scale", "paper", "notes", "dimensions", "title_fields", "finish",
                "material", "process", "iso", "drawing_number", "revision", "ops", "hidden", "hole_dims", "section")
_drawings_lock = threading.Lock()


def _drawings_json(pdir):
    d = _read_json(os.path.join(pdir, "drawings.json"), {})
    return d if isinstance(d, dict) else {}


def drawing_spec_hash(spec, project):
    """K1: sha1 of everything a sheet is drawn from besides the model — the effective sheet
    spec (every DRAWING_KEYS option after merging, ops included) and the project data the
    drawing shows (the part's material/finish/process, title_block, project name, general
    tolerance). drawing create stores it in the sheet (FcSource.spec_hash) and _sheets;
    release recomputes it for the spec it would draw now, so a sheet whose material, notes
    or options changed is no longer "current"."""
    project = project if isinstance(project, dict) else {}
    part = spec.get("part")
    pcfg = project.get("parts") if isinstance(project.get("parts"), dict) else {}
    pcfg = pcfg.get(part) if part and isinstance(pcfg.get(part), dict) else {}
    pmat = project.get("material")
    pmat = pmat.get("name") if isinstance(pmat, dict) else pmat
    dcfg = project.get("drawing") if isinstance(project.get("drawing"), dict) else {}
    eff = {k: spec[k] for k in DRAWING_KEYS if spec.get(k) is not None}
    # the worker's fallbacks, so release's explicit process/material equal create's implicit ones
    eff["process"] = str(spec.get("process") or pcfg.get("process") or project.get("process") or "").strip().lower()
    eff["material"] = str(spec.get("material") or pcfg.get("material") or pmat or "")
    eff["finish"] = str(spec.get("finish") or pcfg.get("finish") or "")
    eff["_project"] = {"name": project.get("name") or "", "title_block": project.get("title_block") or {},
                       "general_tolerance": dcfg.get("general_tolerance") or "ISO 2768-m"}
    return hashlib.sha1(json.dumps(eff, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def drawing_sheets(pdir, parts=None, overrides=None, reset=False):
    """Sheet specs for the drawing worker, merged in this order: project.json defaults
    (drawing.paper/scale; parts.<label> process/material/finish) → drawings.json "*" →
    drawings.json <part> → overrides. Nothing is saved here — save_drawing_options does
    that after a successful create. reset=True skips the saved options of the parts drawn
    (or of "*" when no part is named); reset="all" skips every saved option ("*" and each
    part's — reset without a named part). "auto" paper/scale leave the choice to the worker."""
    saved = _drawings_json(pdir)
    proj = _project_json(pdir)
    part_cfg = proj.get("parts") or {}
    dcfg = proj.get("drawing") or {}
    sheets = []
    for part in (parts or [None]):
        spec = {k: dcfg[k] for k in ("paper", "scale") if dcfg.get(k) is not None}
        cfg = part_cfg.get(part) if part else None
        if isinstance(cfg, dict):
            spec.update({k: cfg[k] for k in ("process", "material", "finish") if cfg.get(k)})
        for key in ("*", part) if part else ("*",):
            layer = saved.get(key)
            if reset == "all" or (reset and key == (part or "*")) or not isinstance(layer, dict):
                continue
            spec.update({k: v for k, v in layer.items() if k in DRAWING_KEYS and not (k == "ops" and key == "*")})
        spec.update(overrides or {})
        for k in ("paper", "scale", "section"):
            if str(spec.get(k, "")).strip().lower() == "auto":
                spec.pop(k)
        if part:
            spec["part"] = part
        else:
            spec.pop("ops", None)                    # ops belong to one part's sheet
        sheets.append(spec)
    return sheets


def save_drawing_options(pdir, parts, overrides, reset, sheets_out=(), rev=None, spec_hashes=None):
    """After a successful create: store ONLY the options the caller passed, under each named
    part (or "*"), and record every saved sheet document in "_sheets" ({file, sha1, rev,
    dims, dims_total, spec_hash, ops_failed}) — release uses that record to tell a generated
    sheet from a hand-edited one. reset without a named part clears "*" and every part's
    options (the _sheets bookkeeping stays). spec_hashes: {part (None = every part): K1 hash}."""
    with _drawings_lock:
        path = os.path.join(pdir, "drawings.json")
        saved = _drawings_json(pdir)
        before = json.dumps(saved, sort_keys=True)
        if reset and not parts:
            for k in [k for k in saved if not k.startswith("_")]:
                saved.pop(k)
        if overrides or reset:
            for key in (parts or ["*"]):
                cur = {} if reset else dict(saved.get(key) or {})
                cur.update(overrides or {})
                if cur:
                    saved[key] = cur
                else:
                    saved.pop(key, None)
        hashes = spec_hashes or {}
        for sh in sheets_out or ():
            d = sh.get("document")
            if d and os.path.isfile(d) and sh.get("part"):
                ok, tot = _dims_verified(sh)
                rec = {"file": os.path.relpath(d, pdir), "sha1": _sha1(d), "rev": rev, "dims": ok, "dims_total": tot}
                if hashes.get(sh["part"], hashes.get(None)):
                    rec["spec_hash"] = hashes.get(sh["part"], hashes.get(None))
                if sh.get("ops_failed"):
                    rec["ops_failed"] = [str(x) for x in sh["ops_failed"]]
                saved.setdefault("_sheets", {})[sh["part"]] = rec
        if json.dumps(saved, sort_keys=True) != before:
            with open(path + ".tmp", "w") as fh:
                json.dump(saved, fh, indent=2)
                fh.write("\n")
            os.replace(path + ".tmp", path)


def _dims_verified(sheet):
    """(verified, total) dimensions of a drawing worker sheet result: the worker's own count
    (generated dimensions that passed, less those create ops removed, plus those ops added;
    total also counts the ones that failed verification) — else counted from the generated list."""
    v, t = sheet.get("dims_verified"), sheet.get("dims_total")
    if isinstance(v, int) and not isinstance(v, bool):
        return v, (t if isinstance(t, int) and not isinstance(t, bool) else None)
    dims = [d for d in sheet.get("dims") or [] if isinstance(d, dict)]
    return sum(1 for d in dims if d.get("ok")), len(dims)


def _sheet_stem(label):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", label)          # the drawing worker's sheet file name


def _sheet_family(doc):
    """(folder, stem): Plate.FCStd and Plate.r02.FCStd are revisions of <folder>/Plate."""
    folder, fn = os.path.split(os.path.abspath(doc))
    m = re.match(r"^(.*?)(?:\.r\d{2,})?\.fcstd$", fn, re.I)
    return folder, (m.group(1) if m else os.path.splitext(fn)[0])


def sheet_revisions(folder, stem):
    """[(n, path)] oldest first — <stem>.FCStd is 0, <stem>.rNN.FCStd is NN."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for fn in names:
        m = re.match(r"^%s(?:\.r(\d{2,}))?\.fcstd$" % re.escape(stem), fn, re.I)
        if m:
            out.append((int(m.group(1) or 0), os.path.join(folder, fn)))
    return sorted(out)


def newest_sheet(folder, stem):
    revs = sheet_revisions(folder, stem)
    return revs[-1][1] if revs else None


def sheet_families(folder):
    """{stem: newest revision} for every drawing document in folder."""
    out = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for fn in names:
        if fn.lower().endswith(".fcstd"):
            stem = _sheet_family(os.path.join(folder, fn))[1]
            out[stem] = newest_sheet(folder, stem)
    return out


def sheet_edited(pdir, label, path):
    """Why path counts as hand-edited ("" when it doesn't): it is not the sheet file the last
    create saved for label, or it changed since, or there is no record (an older server)."""
    rec = (_drawings_json(pdir).get("_sheets") or {}).get(label)
    if not isinstance(rec, dict) or not rec.get("file"):
        return "no record of drawing create making it, so it may hold hand edits"
    if os.path.abspath(os.path.join(pdir, rec["file"])) != os.path.abspath(path):
        return "a revision saved by drawing edit"
    if not os.path.isfile(path) or _sha1(path) != rec.get("sha1"):
        return "changed since drawing create saved it"
    return ""


def reserve_revision(doc):
    """Claim <stem>.rNN.FCStd after the newest revision with an O_EXCL placeholder, so two
    edits at the same time never write the same file."""
    folder, stem = _sheet_family(doc)
    n = max([r for r, _ in sheet_revisions(folder, stem)] + [0]) + 1
    while True:
        p = os.path.join(folder, "%s.r%02d.FCStd" % (stem, n))
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return p
        except FileExistsError:
            n += 1


def _drop_placeholder(path):
    try:
        if os.path.getsize(path) == 0:
            os.remove(path)
    except OSError:
        pass


def _drop_empty_fcbak(saved):
    """FreeCAD backs up the file it saves over; over a reserved (empty) placeholder that
    leaves an empty <stem>.<timestamp>.FCBak — remove it."""
    folder, fn = os.path.split(saved)
    stem = re.sub(r"(?i)\.fcstd$", "", fn)
    for f in glob.glob(os.path.join(glob.escape(folder), glob.escape(stem) + ".*.FCBak")):
        _drop_placeholder(f)


def built_parts(pdir):
    """Labels of the manufactured parts (solid, not reference) in the newest non-variant
    build report, or None when unknown."""
    h = os.path.join(pdir, ".history")
    try:
        revs = sorted((d for d in os.listdir(h) if re.match(r"^r\d{4}$", d)), reverse=True)
    except OSError:
        return None
    for r in revs[:30]:
        rep = _read_json(os.path.join(h, r, "report.json"))
        if not isinstance(rep, dict) or rep.get("variant"):
            continue
        labels = [o.get("label") for o in rep.get("objects") or []
                  if isinstance(o, dict) and o.get("role") != "ref" and o.get("solids")]
        return [lb for lb in labels if lb] or None
    return None


def _backup_edited_bases(pdir, labels):
    """Before create overwrites drawings/<stem>.FCStd: back up base sheets that were changed
    since the last create (or have no record). Returns [(path, backup)]."""
    ddir = os.path.join(pdir, "drawings")
    stems = {_sheet_stem(lb): lb for lb in labels} if labels else {s: s for s in sheet_families(ddir)}
    out = []
    for stem, label in sorted(stems.items()):
        base = os.path.join(ddir, stem + ".FCStd")
        if os.path.isfile(base) and sheet_edited(pdir, label, base):
            out.append((base, backup_file(base, pdir)))
    return out


def _set_aside_revisions(pdir, sheets_out):
    """After create regenerated a sheet, its hand-edited revisions (.rNN) are superseded: move
    them to .history/backups so the regenerated sheet is the newest. Returns [(path, backup)]."""
    out = []
    for sh in sheets_out:
        if not sh.get("document"):
            continue
        folder, stem = _sheet_family(sh["document"])
        for n, p in sheet_revisions(folder, stem):
            if n > 0:
                out.append((p, backup_file(p, pdir)))
                os.remove(p)
    return out


def _drawing_doc(args):
    """The sheet to read or edit: doc, else the project's newest sheet in drawings/ (of
    part, or of the only part that has sheets). Returns (doc, project dir, note)."""
    if args.get("doc"):
        doc, pdir = doc_and_project(args)
        return doc, pdir, None
    if not args.get("project"):
        raise ToolError("pass doc (a drawing .FCStd) or project (its newest sheet in drawings/ is used)")
    pdir = project_dir(args["project"])
    ddir = os.path.join(pdir, "drawings")
    fams = sheet_families(ddir)
    if args.get("part"):
        doc = fams.get(_sheet_stem(args["part"]))
        if not doc:
            raise ToolError("no drawing of %s in %s — make one with drawing(action=\"create\", part=%r)%s" % (
                args["part"], _rel(ddir, pdir), args["part"],
                "; sheets there: %s" % ", ".join(sorted(fams)) if fams else ""))
    elif len(fams) == 1:
        doc = list(fams.values())[0]
    elif not fams:
        raise ToolError("no drawings in %s yet — drawing(action=\"create\") makes one sheet per part; pass doc=… "
                        "for another drawing file" % _rel(ddir, pdir))
    else:
        raise ToolError("%d parts have sheets in drawings/: %s — pass part=… (or doc=…) to choose one" % (
            len(fams), ", ".join("%s (newest %s)" % (k, os.path.basename(v)) for k, v in sorted(fams.items()))))
    return doc, pdir, "sheet    %s (newest revision)" % _rel(doc, pdir)


def _drawing_create(args, request_id):
    named = args.get("parts") or ([args["part"]] if args.get("part") else None)
    overrides = {k: args[k] for k in DRAWING_KEYS if args.get(k) is not None}
    if "ops" in overrides and (not named or len(named) != 1):
        raise ToolError("ops belong to one part's sheet: pass part=\"<label>\" (exactly one) with ops. They are "
                        "saved in drawings.json and replayed on every regeneration of that sheet, release included")
    pdir = project_dir(args.get("project"))
    doc = resolve(args["doc"], base=pdir, must_exist=True, kind="doc") if args.get("doc") else default_doc(pdir)
    if not os.path.isfile(doc):
        raise ToolError("%s has not been built yet — call build first" % pdir)
    parts = named
    if parts is None and os.path.abspath(doc) == os.path.abspath(default_doc(pdir)):
        parts = built_parts(pdir)                  # so each part's saved options apply
    reset = bool(args.get("reset"))
    # reset without a named part clears "*" AND every part's options, so draw with none of them
    sheets = drawing_sheets(pdir, parts, overrides, "all" if reset and not named else reset)
    proj = _project_json(pdir)
    hashes = {}
    for sh in sheets:
        sh["_spec_hash"] = hashes[sh.get("part")] = drawing_spec_hash(sh, proj)
    backups = _backup_edited_bases(pdir, parts)
    rev = _doc_rev(pdir, doc)
    res = run_gui_worker("create", {"doc": doc, "sheets": sheets, "project": proj, "rev": rev,
                                    "out_dir": os.path.join(pdir, "out", "drawings"),
                                    "save_dir": os.path.join(pdir, "drawings")},
                         timeout=int(args.get("timeout", 400)), request_id=request_id)
    content = as_content(res, return_image=args.get("return_image", True))      # raises on failure
    out_sheets = res.get("sheets") or []
    save_drawing_options(pdir, named, overrides, reset, out_sheets, rev, hashes)
    moved = _set_aside_revisions(pdir, out_sheets)
    notes = []
    if reset and not named:
        notes.append("saved    drawings.json: cleared \"*\" and every part's saved options (create ops included)%s "
                     "— this sheet was drawn without them, as later creates and release will be" % (
                         "; saved %s for every part" % ", ".join(sorted(overrides)) if overrides else ""))
    elif overrides or reset:
        notes.append("saved    drawings.json %s: %s — reused by later creates and by release%s" % (
            ", ".join(named) if named else "\"*\" (every part)", ", ".join(sorted(overrides)) or "(cleared)",
            " (reset first)" if reset and overrides else ""))
    for sh in out_sheets:
        if sh.get("ops_failed"):
            notes.append("✗ %s: %d op(s) failed — %s. Fix them with drawing(action=\"create\", part=%r, ops=[…]) "
                         "(ops replace the saved list)" % (sh.get("part"), len(sh["ops_failed"]),
                                                           "; ".join(map(str, sh["ops_failed"])), sh.get("part")))
    for p, b in backups:
        notes.append("backup   %s had changes since it was generated — copied to %s" % (_rel(p, pdir), _rel(b, pdir)))
    for p, b in moved:
        notes.append("moved    %s (hand-edited revision, now superseded) → %s — re-apply its edits with "
                     "drawing(action=\"edit\") if they are still needed" % (_rel(p, pdir), _rel(b, pdir)))
    return _append_text(content, "\n".join(notes))


def t_drawing(args, request_id=None):
    action = (args.get("action") or "create").lower()
    if action == "create":
        return _drawing_create(args, request_id)
    if action == "read":
        doc, pdir, note = _drawing_doc(args)
        prev = os.path.join(pdir, ".history", "previews") if pdir else \
            os.path.join(os.path.expanduser("~"), ".cache", "freecad-mcp", "previews")
        res = run_gui_worker("read", {"doc": doc, "page": args.get("page"), "out_dir": prev},
                             timeout=int(args.get("timeout", 300)), request_id=request_id)
        content = as_content(res, return_image=args.get("return_image", True))
        return _append_text(content, note) if note else content
    if action == "edit":
        if not args.get("ops"):
            raise ToolError("edit needs ops, e.g. [{\"op\": \"add_note\", \"text\": \"…\"}]")
        doc, pdir, note = _drawing_doc(args)
        folder, stem = _sheet_family(doc)
        newest = newest_sheet(folder, stem) or doc
        if os.path.abspath(newest) != os.path.abspath(doc) and not args.get("from_older"):
            raise ToolError("%s is not the newest revision of this sheet — %s is, and it may hold edits this one "
                            "lacks. Edit the newest (doc=%r), or pass from_older=true to fork a new revision from "
                            "this older one deliberately." % (_rel(doc, pdir), _rel(newest, pdir), _rel(newest, pdir)))
        inplace = args.get("save") == "inplace"
        bak = backup_file(doc, pdir) if inplace else None
        save_as = doc if inplace else reserve_revision(doc)
        res = {"ok": False, "error": "edit did not run"}
        try:
            res = run_gui_worker("edit", {"doc": doc, "page": args.get("page"), "ops": args["ops"], "save_as": save_as,
                                          "export_name": re.sub(r"(?i)\.fcstd$", "", os.path.basename(save_as)),
                                          "out_dir": os.path.join(pdir or os.path.dirname(doc), "out", "drawings")},
                                 timeout=int(args.get("timeout", 300)), request_id=request_id)
        finally:
            if not inplace and not res.get("ok"):
                _drop_placeholder(save_as)
        if res.get("ok"):
            _drop_empty_fcbak(save_as)
        content = as_content(res, return_image=args.get("return_image", True))
        lines = [note] if note else []
        lines.append("saved    %s in place — previous version backed up to %s" % (_rel(doc, pdir), _rel(bak, pdir))
                     if inplace else "saved    %s (new revision; %s is unchanged)" % (_rel(save_as, pdir),
                                                                                    _rel(doc, pdir)))
        return _append_text(content, "\n".join(lines))
    raise ToolError("action must be create | read | edit")


def _last_build_rev(pdir):
    h = os.path.join(pdir, ".history")
    try:
        revs = sorted(d for d in os.listdir(h) if re.match(r"^r\d{4}$", d))
    except OSError:
        revs = []
    return revs[-1] if revs else "r0000"


def _doc_rev(pdir, doc):
    """The build revision of the document being drawn (#49): its .history/last_build.json
    record (keyed by the build file's path, absolute or relative to the project), else the
    newest report of the same build output (main or that variant), else the newest revision.
    Variant builds share the rev numbering, so the newest revision may be another design."""
    target = os.path.realpath(doc)
    lb = _read_json(os.path.join(pdir, ".history", "last_build.json"), {})
    for key, rec in (lb.items() if isinstance(lb, dict) else ()):
        p = key if os.path.isabs(key) else os.path.join(pdir, key)
        if isinstance(rec, dict) and rec.get("rev") and os.path.realpath(p) == target:
            return rec["rev"]
    stem = re.sub(r"(?i)\.fcstd$", "", os.path.basename(doc))
    if os.path.dirname(target) == os.path.realpath(os.path.join(pdir, "build")):
        want = stem.split("__", 1)[1] if "__" in stem else None
        h = os.path.join(pdir, ".history")
        try:
            revs = sorted((d for d in os.listdir(h) if re.match(r"^r\d{4}$", d)), reverse=True)
        except OSError:
            revs = []
        for r in revs:
            rep = _read_json(os.path.join(h, r, "report.json"))
            if isinstance(rep, dict):
                v = re.sub(r"[^A-Za-z0-9_-]+", "_", rep["variant"]) if rep.get("variant") else None
                if v == want:
                    return r
    return _last_build_rev(pdir)


def _g(v):
    return "—" if v is None else "%.4g" % v


def t_analyze(args, request_id=None):
    """FEA (static, frequency or buckling) of one part or of several bonded parts: a case
    from fea.json, merged with any given arguments, solved after a rebuild."""
    pdir = project_dir(args.get("project"))
    wargs = {k: args.get(k) for k in ("case", "part", "parts", "bonded", "analysis", "modes", "fixed", "loads",
                                      "gravity", "material", "mesh_size", "refine", "save", "params")}
    wargs["project_dir"] = pdir
    wargs["timeout"] = int(args.get("timeout", 900))      # the worker stops CalculiX in time
    return maybe_background("analyze", args, "analyze", wargs, int(args.get("timeout", 900)), request_id,
                            "FEA case '%s' of %s" % (args.get("case") or "default", os.path.basename(pdir)))


def t_check_manufacturing(args, request_id=None):
    doc, pdir = doc_and_project(args)
    wargs = {"doc": doc, "project_dir": pdir, "objects": args.get("objects"), "process": args.get("process")}
    return as_content(run_worker("dfm", wargs, timeout=int(args.get("timeout", 300)), request_id=request_id),
                      return_image=False)


PROCESS_LABEL = {"fdm": "3D print (FDM)", "sla": "3D print (SLA)", "cnc": "CNC milling", "turning": "CNC turning",
                 "laser": "laser/waterjet", "sheet": "sheet metal"}
NEEDS_DRAWING = ("cnc", "turning", "laser", "sheet")


def _safe_part(label):
    """manufacture._safe: the per-part folder name inside a release."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label).strip("_") or "part"


def load_checks(pdir):
    c = _read_json(os.path.join(pdir, "checks.json"), [])
    c = c.get("checks", []) if isinstance(c, dict) else c
    return c if isinstance(c, list) else []


def inputs_sha1(pdir):
    """Fallback when the build does not report inputs_sha1: model.py, params.json, checks.json,
    project.json, drawings.json (without the "_sheets" bookkeeping) and every file in refs/."""
    h = hashlib.sha1()
    for fn in ("model.py", "params.json", "checks.json", "project.json", "drawings.json"):
        p = os.path.join(pdir, fn)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            data = fh.read()
        if fn == "drawings.json":
            d = _read_json(p)
            if isinstance(d, dict):
                d.pop("_sheets", None)
                data = json.dumps(d, sort_keys=True).encode()
        h.update(fn.encode() + b"\0" + data + b"\0")
    for root, dirs, files in os.walk(os.path.join(pdir, "refs")):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, pdir).encode() + b"\0")
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


PACKAGE_ARGS = ("parts", "process", "processes", "qty", "drawings")


def package_signature(pdir, args):
    """(sha1, detail) of what decides a package's content besides the build inputs (#26): the
    release arguments that change it (parts, process, processes, qty, drawings) and the sha1
    of every sheet document in drawings/ (the sources of shipped drawings: a hand edit makes a
    new .rNN without touching the inputs). The shipped files themselves are not compared —
    STEP and PDF exports embed their export time — their sha1s go into .release.json."""
    a = {k: args[k] for k in PACKAGE_ARGS if args.get(k) not in (None, "", [], {})}
    if a.get("parts"):
        a["parts"] = sorted(set(map(str, a["parts"])))
    if a.get("drawings") == "auto":
        a.pop("drawings")
    sheets = {}
    if a.get("drawings") != "none":
        ddir = os.path.join(pdir, "drawings")
        for fn in sorted(os.listdir(ddir)) if os.path.isdir(ddir) else ():
            if fn.lower().endswith(".fcstd") and os.path.isfile(os.path.join(ddir, fn)):
                sheets[fn] = _sha1(os.path.join(ddir, fn))
    detail = {"args": a, "sheets": sheets}
    return hashlib.sha1(json.dumps(detail, sort_keys=True, default=str).encode("utf-8")).hexdigest(), detail


def release_target(rel_root, base, inputs, package=None):
    """(folder, replacing, note). release/<base> is replaced only when its .release.json has
    the same inputs_sha1 AND the same package signature (parts, qty, processes, drawing mode
    and sheets — package_signature); otherwise the next free <base>-N — a package is never
    silently overwritten."""
    n, why = 1, None
    while True:
        cand = os.path.join(rel_root, base if n == 1 else "%s-%d" % (base, n))
        if not os.path.exists(cand) and not os.path.exists(cand + ".zip"):
            if n == 1:
                return cand, False, None
            if why == "package":
                return cand, False, (
                    "⚠ release/%s holds a different package of this revision (other parts, quantities, processes, "
                    "drawing mode or drawing sheets — or it was made by an older server). This package is %s; the "
                    "earlier one is untouched (delete it if it is obsolete)." % (base, os.path.basename(cand)))
            return cand, False, (
                "⚠ release/%s already exists and was made from different inputs (or by an older server) — .history "
                "was probably reset, so the build revision number came round again. This package is %s; the "
                "earlier one is untouched (delete it if it is obsolete)." % (base, os.path.basename(cand)))
        meta = _read_json(os.path.join(cand, ".release.json"))
        same_inputs = bool(inputs) and isinstance(meta, dict) and meta.get("inputs_sha1") == inputs
        if same_inputs and package and meta.get("package_sha1") == package:
            return cand, True, ("replaced the earlier package %s — same inputs (model, parameters, checks, project, "
                                "drawing options and refs) and same package (parts, quantities, processes, drawing "
                                "mode and sheets)" % os.path.basename(cand))
        if why is None:
            why = "package" if same_inputs else "inputs"
        n += 1


def previous_release(rel_root, exclude=None):
    """The newest .release.json under release/ (other than exclude)."""
    best = None
    for mp in glob.glob(os.path.join(glob.escape(rel_root), "*", ".release.json")):
        if exclude and os.path.abspath(os.path.dirname(mp)) == os.path.abspath(exclude):
            continue
        m = _read_json(mp)
        if isinstance(m, dict) and (best is None or (m.get("released") or 0) > (best.get("released") or 0)):
            best = m
    return best


def checks_changes(prev, cur):
    """{since, added, removed, changed} between the previous release's checks and these, or None."""
    if not prev or not isinstance(prev.get("checks"), list):
        return None

    def by_id(lst):
        return {str(c.get("id") or "#%d" % (i + 1)) if isinstance(c, dict) else "#%d" % (i + 1): c
                for i, c in enumerate(lst)}
    old, new = by_id(prev["checks"]), by_id(cur)
    out = {"since": prev.get("rev"), "added": [k for k in new if k not in old],
           "removed": [k for k in old if k not in new],
           "changed": [k for k in new if k in old and json.dumps(new[k], sort_keys=True)
                       != json.dumps(old[k], sort_keys=True)]}
    return out if out["added"] or out["removed"] or out["changed"] else None


def _clean_staging(rel_root):
    for d in glob.glob(os.path.join(glob.escape(rel_root), ".staging-*")):
        try:
            if time.time() - os.path.getmtime(d) > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _release_drawings(pdir, proj, data, rev, mode, parts, rel, tmpd, request_id, allow_failed_ops=False):
    """Drawings for the release (I9 sheet bookkeeping): a current saved sheet is exported and
    shipped; a stale one is regenerated and saved — unless it was edited by hand, which stops
    the release. Current = same model shape and content (holes, fits, threads) AND the same
    drawing spec (K1: options, material, finish, title block …). A sheet whose saved create
    ops failed stops the release unless allow_failed_ops. Ships the PDF only. Returns
    (summary lines, preview PNG paths)."""
    need = [p for p in parts if p["process"] in NEEDS_DRAWING or mode == "all"]
    if not need or mode == "none":
        return [], []
    ddir = os.path.join(pdir, "drawings")
    by = {p["part"]: p for p in need}
    specs = {}                                  # the sheet spec release would draw now, with its K1 hash
    for sh in drawing_sheets(pdir, list(by)):
        pr = by[sh["part"]]
        sh["process"] = pr["process"]
        sh.setdefault("material", pr.get("material"))
        if pr.get("finish"):
            sh.setdefault("finish", pr["finish"])
        sh["_spec_hash"] = drawing_spec_hash(sh, proj)
        specs[sh["part"]] = sh
    recs = _drawings_json(pdir).get("_sheets") or {}
    plan = {}
    for lb in by:
        newest = newest_sheet(ddir, _sheet_stem(lb))
        plan[lb] = {"newest": newest, "edited": sheet_edited(pdir, lb, newest) if newest else "",
                    "rec": recs.get(lb) if isinstance(recs.get(lb), dict) else {}}
    lines, pngs, ops_bad, saved_now = [], [], {}, []

    def ship(lb, files, how, dims=None):
        pdf = (files or {}).get("pdf")
        if not pdf or not os.path.isfile(pdf):
            raise ToolError("RELEASE STOPPED — the drawing of %s produced no PDF. Check it with drawing(action="
                            "\"read\") or ship a fresh one with release(drawings=\"generate\")." % lb)
        dest = os.path.join(rel, _safe_part(lb))
        os.makedirs(dest, exist_ok=True)
        tgt = os.path.join(dest, "%s_drawing.pdf" % _safe_part(lb))
        shutil.copyfile(pdf, tgt)
        by[lb]["files"].append(tgt)
        by[lb]["drawing"] = tgt
        by[lb]["drawing_dims"] = dims             # [verified, total] against the model (README); None = unknown
        if (files or {}).get("png") and os.path.isfile(files["png"]):
            pngs.append(files["png"])
        lines.append("  drawing %-14s %s" % (lb, how))

    check = [lb for lb in plan if plan[lb]["newest"] and mode != "generate"]
    if check:
        st = run_gui_worker("sheet_status", {"build": data["fcstd"], "sheets": [
            {"part": lb, "doc": plan[lb]["newest"], "spec_hash": specs[lb]["_spec_hash"]} for lb in check]},
            timeout=600, request_id=request_id)
        if not st.get("ok"):
            raise ToolError("RELEASE STOPPED — could not check whether the saved drawings still match the model: %s\n"
                            "Retry, or pass drawings=\"generate\" to ship freshly generated sheets." % st.get("error"))
        for s in st.get("sheets") or []:
            if s.get("part") in plan:
                plan[s["part"]].update(current=bool(s.get("current")),
                                       detail=re.sub(r"^stale — ", "", s.get("detail") or ""),
                                       revision=s.get("revision") or "", dims=s.get("dims_verified"),
                                       dims_total=s.get("dims_total"))
        for lb in check:                        # the create record says which spec the sheet family was drawn from
            old = plan[lb]["rec"].get("spec_hash")
            if plan[lb].get("current") and old and old != specs[lb]["_spec_hash"]:
                plan[lb].update(current=False, detail="material/finish/notes, title block or drawing options changed "
                                                      "since it was drawn")
    stop = [lb for lb in check if not plan[lb].get("current") and plan[lb]["edited"]]
    if stop:
        raise ToolError(
            "RELEASE STOPPED — these sheets no longer match what release would draw now (the model, project.json or "
            "the drawing options changed since they were drawn), and they have hand edits that regenerating would "
            "lose:\n%s\nChoose one:\n"
            "  • drawing(action=\"create\", part=…) regenerates the sheet (saved options and create ops are "
            "replayed; the edited file is kept in .history/backups) — then re-apply the hand edits with "
            "drawing(action=\"edit\") and release again;\n"
            "  • release(drawings=\"generate\") ships freshly generated sheets without those edits (the edited "
            "files stay as they are)." % "\n".join("  %s: %s (%s)%s" % (
                lb, _rel(plan[lb]["newest"], pdir), plan[lb]["edited"],
                " — " + plan[lb]["detail"] if plan[lb].get("detail") else "") for lb in stop))
    for lb in check:
        if plan[lb].get("current"):
            # shipped AS IS: its title block keeps the revision it was drawn (or edited) at, so the PDF matches
            # the sheet file and out/drawings — the release README names the package revision
            rec = plan[lb]["rec"]
            if not plan[lb]["edited"] and rec.get("ops_failed"):
                ops_bad[lb] = rec["ops_failed"]       # drawing create reported them; the sheet still lacks them
            ex = run_gui_worker("export", {"doc": plan[lb]["newest"], "out_dir": tmpd,
                                           "name": "%s_drawing" % _safe_part(lb)},
                                timeout=600, request_id=request_id)
            if not ex.get("ok"):
                raise ToolError("RELEASE STOPPED — exporting %s failed: %s\nCheck the sheet with drawing(action="
                                "\"read\"), or pass drawings=\"generate\" to ship a freshly generated one."
                                % (_rel(plan[lb]["newest"], pdir), ex.get("error")))
            v, t = plan[lb].get("dims"), plan[lb].get("dims_total")
            if not plan[lb]["edited"]:             # recorded when drawing create saved this very file
                v = rec.get("dims") if v is None else v
                t = rec.get("dims_total") if t is None else t
            ship(lb, ex.get("files"), "shipped %s (%scurrent%s)" % (
                _rel(plan[lb]["newest"], pdir),
                "sheet rev %s, " % plan[lb]["revision"] if plan[lb].get("revision") else "",
                "; " + plan[lb]["edited"] if plan[lb]["edited"] else ""), [v, t] if v is not None else None)
    save = [lb for lb in plan if lb not in check or not plan[lb].get("current")]
    keep = [lb for lb in save if plan[lb]["newest"] and plan[lb]["edited"]]     # only with mode "generate"
    save = [lb for lb in save if lb not in keep]
    for group, to_drawings in ((save, True), (keep, False)):
        if not group:
            continue
        sheets = [dict(specs[lb]) for lb in group]
        wargs = {"doc": data["fcstd"], "sheets": sheets, "project": proj, "rev": rev, "out_dir": tmpd}
        if to_drawings:
            wargs["save_dir"] = ddir
        dres = run_gui_worker("create", wargs, timeout=600, request_id=request_id)
        if not dres.get("ok"):
            raise ToolError("RELEASE STOPPED — drawing generation failed: %s" % dres.get("error"))
        if to_drawings:
            save_drawing_options(pdir, None, None, False, dres.get("sheets") or [], rev,
                                 {lb: specs[lb]["_spec_hash"] for lb in group})
            saved_now += [_rel(sh["document"], pdir) for sh in dres.get("sheets") or [] if sh.get("document")]
        for sh in dres.get("sheets") or []:
            lb, p = sh["part"], plan.get(sh["part"], {})
            if sh.get("ops_failed"):
                ops_bad[lb] = [str(x) for x in sh["ops_failed"]]
            if not to_drawings:
                why = "generated for this release (drawings=\"generate\"); %s has hand edits and was left untouched" \
                      % _rel(p["newest"], pdir)
            elif mode == "generate":
                why = "regenerated (drawings=\"generate\"), saved %s" % _rel(sh.get("document") or ddir, pdir)
            elif p.get("newest"):
                why = "regenerated — %s was out of date%s; saved %s" % (
                    _rel(p["newest"], pdir), " (%s)" % p["detail"] if p.get("detail") else "",
                    _rel(sh.get("document") or ddir, pdir))
            else:
                why = "generated and saved %s (no sheet yet)" % _rel(sh.get("document") or ddir, pdir)
            n_ok, n_all = _dims_verified(sh)
            ship(lb, sh.get("files"), "%s · %s %s, %s dimensions verified%s" % (
                why, sh.get("paper"), sh.get("scale"), n_ok if n_all is None else "%d/%d" % (n_ok, n_all),
                " — " + "; ".join(sh["warnings"]) if sh.get("warnings") else ""), [n_ok, n_all])
    if ops_bad and not allow_failed_ops:
        raise ToolError(
            "RELEASE STOPPED — create ops saved for these sheets failed, so what they add (a dimension, note or "
            "removal the user asked for) is missing from the drawing:\n%s\nFix or drop the failed ops with "
            "drawing(action=\"create\", part=…, ops=[…]) — the list replaces the saved ops; drawing(action=\"read\") "
            "shows the sheet — then release again. Or pass allow_failed_ops=true to ship the sheets without them (only "
            "with the user's OK; the output and README then say what is missing).%s" % ("\n".join(
                "  %s: %s — fix: drawing(action=\"create\", part=%r, ops=[…])" % (
                    lb, "; ".join("✗ " + str(x) for x in f), lb) for lb, f in sorted(ops_bad.items())),
                "\n(Regenerated and saved before stopping: %s.)" % ", ".join(saved_now) if saved_now else ""))
    for lb, f in sorted(ops_bad.items()):
        lines.append("  ✗ drawing %s: %d create op(s) failed — shipped without them (allow_failed_ops): %s" % (
            lb, len(f), "; ".join(map(str, f))))
        by[lb]["notes"] = list(by[lb].get("notes") or []) + [
            "✗ The drawing lacks %d requested addition(s) that failed when it was generated: %s" % (
                len(f), "; ".join(map(str, f)))]
        by[lb]["drawing_ops_failed"] = list(map(str, f))
    return lines, pngs


def t_release(args, request_id=None):
    """Build → verify → manufacturability → exports → drawings → BOM + notes → zip, staged in
    release/.staging-* and moved into place only when everything passed."""
    pdir = project_dir(args.get("project"))
    proj = _project_json(pdir)
    mode = args.get("drawings") or "auto"
    t0 = time.time()
    b = run_worker("build", {"project_dir": pdir, "params": {}, "views": [], "force": False}, timeout=600,
                   request_id=request_id)
    if not b.get("ok"):
        raise ToolError("RELEASE STOPPED — the build did not run:\n%s" % b.get("error"))
    report = b.get("text", "")
    data = b.get("data") or {}
    if not data.get("ok"):
        raise ToolError("RELEASE STOPPED — the build has errors; fix them first.\n\n" + report)
    fea_lines = []
    if data.get("fea_stale"):
        # analysis checks must be evidence for this model: re-solve their load cases first
        for case in data["fea_stale"]:
            a = run_worker("analyze", {"project_dir": pdir, "case": case, "save": False, "timeout": 900}, timeout=900,
                           request_id=request_id)
            if not a.get("ok"):
                raise ToolError("RELEASE STOPPED — re-running FEA case '%s' (its checks were stale) failed:\n%s"
                                % (case, a.get("error") or a.get("text")))
            ad = a.get("data") or {}
            kind = ad.get("analysis") or "static"
            if kind == "frequency":
                got = "first natural frequency %s Hz" % _g((ad.get("frequencies_hz") or [None])[0])
            elif kind == "buckling":
                got = "lowest buckling factor %s" % (_g(ad.get("buckling_factor")) if ad.get("buckling_factor")
                                                     else "> %s" % _g(ad.get("buckling_factor_above")))
            else:
                got = "max von Mises %s MPa, safety factor %s, max displacement %s mm" % (
                    _g(ad.get("max_vm")), _g(ad.get("safety_factor")), _g(ad.get("max_disp")))
            fea_lines.append("  FEA      case '%s' re-solved for this model (%.0f s): %s" % (
                case, ad.get("time") or a.get("elapsed") or 0, got))
        b = run_worker("build", {"project_dir": pdir, "params": {}, "views": [], "force": False}, timeout=600,
                       request_id=request_id)
        if not b.get("ok"):
            raise ToolError("RELEASE STOPPED — the build did not run:\n%s" % b.get("error"))
        report = b.get("text", "")
        data = b.get("data") or {}
    if data.get("checks") and data.get("checks_passed") != data.get("checks") and not args.get("allow_failing_checks"):
        raise ToolError("RELEASE STOPPED — %d of %d acceptance checks fail (pass allow_failing_checks=true to "
                        "release anyway).\n\n%s" % (data["checks"] - data["checks_passed"], data["checks"], report))
    rev = data.get("rev", "r0000")
    inputs = data.get("inputs_sha1") or inputs_sha1(pdir)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", proj.get("name") or os.path.basename(pdir))
    rel_root = os.path.join(pdir, "release")
    final, replacing, where = release_target(rel_root, "%s_%s" % (name, rev), inputs,
                                             package_signature(pdir, args)[0])
    prev = previous_release(rel_root, exclude=final if replacing else None)
    os.makedirs(rel_root, exist_ok=True)
    _clean_staging(rel_root)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=rel_root)
    rel = os.path.join(staging, os.path.basename(final))
    try:
        prep = run_worker("release_prepare", {"doc": data["fcstd"], "project_dir": pdir, "release_dir": rel,
                                              "objects": args.get("parts"), "process": args.get("process"),
                                              "processes": args.get("processes"), "qty": args.get("qty"),
                                              "allow_dfm_errors": bool(args.get("allow_dfm_errors"))},
                          timeout=600, request_id=request_id)
        if not prep.get("ok"):
            raise ToolError("RELEASE STOPPED — preparing the package failed: %s" % prep.get("error"))
        pdata = prep.get("data") or {}
        if pdata.get("blocked"):
            raise ToolError(prep["text"])
        unknown = {k: v for k, v in (pdata.get("unknown_keys") or {}).items() if v}
        if unknown:
            where_k = {"parts": "project.json \"parts\"", "qty": "the qty argument", "processes": "the processes argument"}
            raise ToolError("RELEASE STOPPED — these names match no part in the build: %s.\nValid part labels: %s.\n"
                            "Rename them (labels are case-sensitive) and release again — otherwise their process, "
                            "material or quantity would silently not apply." % (
                                "; ".join("%s: %s" % (where_k.get(k, k), ", ".join(map(str, v)))
                                          for k, v in sorted(unknown.items())),
                                ", ".join(pdata.get("labels") or []) or "(none)"))
        bad = [e for e in pdata.get("exports") or [] if not e.get("ok")]
        if bad and not args.get("allow_failed_exports"):
            raise ToolError("RELEASE STOPPED — %d exported file(s) failed verification, so they may not match the "
                            "model:\n%s\nUsually the solid is invalid or not closed — inspect and check_manufacturing "
                            "show where. Fix the model and release again, or pass allow_failed_exports=true to ship "
                            "them anyway (only with the user's OK)." % (len(bad), "\n".join(
                                "  ✗ %s — %s" % (os.path.basename(e.get("path") or e.get("file") or "?"), e.get("check", ""))
                                for e in bad)))
        parts = pdata["parts"]
        for p in parts:
            p.setdefault("drawing", None)
            p.setdefault("drawing_dims", None)
        tmpd = os.path.join(staging, "_drawings")
        dlines, pngs = _release_drawings(pdir, proj, data, rev, mode, parts, rel, tmpd, request_id,
                                         bool(args.get("allow_failed_ops")))
        imgs = [image_block(i) for i in pngs[:3]] if args.get("return_image", True) else []
        checks = []
        for ln in report.splitlines():
            m = re.match(r"^\s+([✓✗]) (\S+)\s+\S+\s+(.*)$", ln)
            if m and "checks.json" in report:
                checks.append({"ok": m.group(1) == "✓", "id": m.group(2), "value": m.group(3).strip()})
        cur_checks = load_checks(pdir)
        changes = checks_changes(prev, cur_checks)
        fin = run_worker("release_finish", {"project_dir": pdir, "release_dir": rel, "rev": rev, "parts": parts,
                                            "refs": pdata.get("refs") or [], "checks": checks, "report": report,
                                            "checks_changes": changes},
                         timeout=120, request_id=request_id)
        if not fin.get("ok"):
            raise ToolError("RELEASE STOPPED — writing BOM/README/zip failed: %s" % fin.get("error"))
        zsrc = fin["data"]["zip"]
        # signature AFTER the drawings step: sheets it regenerated are saved, so an identical release next time
        # (same inputs, arguments and sheets) computes the same one and replaces this package
        psig, pdetail = package_signature(pdir, args)
        shipped = {}
        for root, dirs, files in os.walk(rel):
            dirs.sort()
            for fn in sorted(files):
                if fn != ".release.json":
                    shipped[os.path.relpath(os.path.join(root, fn), rel).replace(os.sep, "/")] = \
                        _sha1(os.path.join(root, fn))
        with open(os.path.join(rel, ".release.json"), "w") as fh:
            json.dump({"rev": rev, "inputs_sha1": inputs, "package_sha1": psig, "package_args": pdetail["args"],
                       "sheets": pdetail["sheets"], "files": shipped, "checks": cur_checks, "released": time.time(),
                       "date": time.strftime("%Y-%m-%d %H:%M"), "package": os.path.basename(final),
                       "drawings": {p["part"]: _rel(p["drawing"], rel) for p in parts if p.get("drawing")}},
                      fh, indent=1)
        if replacing:
            shutil.rmtree(final, ignore_errors=True)
            if os.path.exists(final + ".zip"):
                os.remove(final + ".zip")
        os.rename(rel, final)
        zpath = final + ".zip"
        os.rename(zsrc, zpath)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    lines = ["RELEASE %s %s → %s" % (name, rev, os.path.relpath(final, pdir))] + fea_lines
    if where:
        lines.append("  " + where)
    lines += dlines
    for p in parts:
        lines.append("  %-16s %-22s qty %-3d %s" % (p["part"], PROCESS_LABEL.get(p["process"], p["process"]), p["qty"],
                                                    ", ".join(os.path.basename(f) for f in p["files"])))
        for f in (p.get("dfm") or {}).get("findings", []):
            if f.get("severity") == "warn":
                lines.append("      ⚠ %s" % f["message"])
    refs = pdata.get("refs") or []
    buy = [r for r in refs if r.get("bom") == "buy"]
    ctx = [r for r in refs if r.get("bom") != "buy"]
    if buy:
        lines.append("  purchased: " + ", ".join("%s ×%s" % (r["part"], r.get("qty", 1)) if r.get("qty", 1) != 1
                                                 else r["part"] for r in buy))
    if ctx:
        lines.append("  reference parts (used for checks, not supplied): " + ", ".join(r["part"] for r in ctx))
    if changes:
        lines.append("  checks changed since %s: %s" % (changes.get("since") or "the previous release", " · ".join(
            "%s %s" % (k, ", ".join(changes[k])) for k in ("added", "removed", "changed") if changes[k])))
    lines.append("  BOM.csv · README.md (manufacturing notes, verification, build report)")
    lines.append("  zip      %s  (%.1f KB)" % (zpath, os.path.getsize(zpath) / 1024.0))
    lines.append("  %.1f s" % (time.time() - t0))
    if bad:
        lines.append("  ⚠ shipped with %d export(s) that failed verification (allow_failed_exports): %s" % (
            len(bad), ", ".join("%s (%s)" % (os.path.basename(e.get("path") or e.get("file") or "?"), e.get("check", ""))
                                for e in bad)))
    return [{"type": "text", "text": "\n".join(lines)}] + imgs


# ----------------------------------------------------------------- schemas

def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


VIEW_ENUM = ["iso", "front", "back", "right", "left", "top", "bottom", "iso-back", "iso-left", "iso-bottom"]
_VIEWS = {"type": "array", "items": {"anyOf": [{"type": "string", "enum": VIEW_ENUM},
                                               {"type": "object", "properties": {"az": {"type": "number"},
                                                                                 "el": {"type": "number"}}}]},
          "description": "Views to render as one labelled grid at a common scale. Presets: "
                         + ", ".join(VIEW_ENUM) + "; or {az, el} in degrees. Front looks toward +Y."}
_PROJECT = {"type": "string", "description": "Project name (under FREECAD_MCP_HOME, default ~/CAD) or path."}
_DOC = {"type": "string", "description": "A .FCStd, .step/.stp, .iges or .brep file (relative to the project "
                                         "if project is given). Default: the project's built document."}
_WAIT = {"type": "boolean", "description": "Default true. false = run in the background and return a job id "
                                           "at once; collect with job_status. Use for slow models."}
_REF = {"type": ["string", "array"], "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
        "description": "A reference string, or a point [x, y, z]."}

TOOLS = [
    {"name": "freecad_status", "handler": t_freecad_status,
     "description": "Check the setup: FreeCAD location and version (runs a worker), projects home, allowed "
                    "roots, whether the FreeCAD GUI is running, the MCP Reload helper, running jobs, and where "
                    "this server runs (host). Call once at the start. If FreeCAD is not found, setup_hint says "
                    "exactly what the user must do — relay it.",
     "inputSchema": _obj({})},
    {"name": "guide", "handler": t_guide,
     "description": "The FreeCAD design playbook: the design loop (intake → requirements → reference parts → "
                    "layout → model → verify → iterate → document → release), rules, fclib API, FreeCAD "
                    "pitfalls. Read it once at the start of a design task unless the freecad-design skill is "
                    "already loaded. topic = a reference page (fclib, checks, processes, troubleshooting), a "
                    "section number ('4', or '1.6' for step ⑥ of the loop) or a word from a section heading "
                    "('verify', 'release', 'drawing', 'holes'); an unknown topic is an error listing the valid ones.",
     "inputSchema": _obj({"topic": {"type": "string", "description": "Omit for the whole playbook."}})},
    {"name": "create_project", "handler": t_create_project,
     "description": "Scaffold a project directory: project.json (process, material, drawing defaults, the "
                    "part's entry in \"parts\" keyed by its body label), params.json, model.py, requirements.md, "
                    "checks.json, refs/ build/ out/. The part template's starter model suits the process: fdm/sla "
                    "a plate with clearance holes, cnc a block with a radiused pocket and tapped holes, turning an "
                    "fc.turned shaft with chamfered ends, laser a uniform plate with holes, sheet a bent bracket "
                    "(fc.sheet_metal: flanges, bends, flat pattern). fdm/sla projects "
                    "get dfm.hole_compensation_mm (0.2 / 0.1 mm, diametral): fc.holes adds it to clearance and "
                    "plain holes so they print true (not to tapped, insert or fit holes; compensation= overrides "
                    "per feature).",
     "inputSchema": _obj({
         "name": {"type": "string", "description": "Project and directory name."},
         "template": {"type": "string", "enum": ["part", "assembly", "empty"],
                      "description": "part = one body; assembly = a plate grounded in an Assembly with a reference "
                                     "part joined to it by named frames (fc.assembly, fc.ground, fc.joint). "
                                     "Default part."},
         "description": {"type": "string"},
         "process": {"type": "string", "enum": sorted(MATERIALS), "description": "Manufacturing process. Default fdm."},
         "material": {"type": "string", "description": "Material name (default per process). Its density comes "
                                                        "from the material table (e.g. '5052 aluminium', '1045 "
                                                        "steel'); an unknown name keeps the process default, "
                                                        "flagged as assumed."},
         "density_g_cm3": {"type": "number", "description": "Material density for mass reports (overrides the "
                                                            "table)."},
         "parent_dir": {"type": "string", "description": "Where to create it. Default FREECAD_MCP_HOME."}},
         ["name"])},
    {"name": "list_projects", "handler": t_list_projects,
     "description": "Find projects (and loose .FCStd files) under the projects home or a root.",
     "inputSchema": _obj({"root": {"type": "string"}, "max_depth": {"type": "integer"}})},
    {"name": "read_file", "handler": t_read_file,
     "description": "Read a text file with line numbers (or list a directory). Paths are relative to the "
                    "project when project is given.",
     "inputSchema": _obj({"path": {"type": "string"}, "project": _PROJECT,
                          "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"])},
    {"name": "write_file", "handler": t_write_file,
     "description": "Create a text file (model.py, params.json, checks.json, requirements.md …) or replace "
                    "one with overwrite=true. Prefer edit_file for small changes. Binary reference files the "
                    "user gave you (vendor STEP/IGES/STL, datasheet PDF, reference drawing PNG/JPG/DXF, FCStd; "
                    "≤ 50 MB) go in with encoding=\"base64\" and the bytes base64-encoded — usually into refs/. "
                    "Use it when your own file tools can't reach the user's projects folder (e.g. in Cowork). "
                    "Inside a project every overwritten version is kept in .history/backups/.",
     "inputSchema": _obj({"path": {"type": "string"}, "content": {"type": "string",
                                                                  "description": "Text, or base64 bytes."},
                          "project": _PROJECT, "overwrite": {"type": "boolean"},
                          "encoding": {"type": "string", "enum": ["utf-8", "base64"],
                                       "description": "Default utf-8 (text files only)."}}, ["path", "content"])},
    {"name": "edit_file", "handler": t_edit_file,
     "description": "Replace an exact string in a text file. old_text must match exactly and be unique unless "
                    "replace_all. Inside a project every previous version is kept in .history/backups/.",
     "inputSchema": _obj({"path": {"type": "string"}, "old_text": {"type": "string"},
                          "new_text": {"type": "string"}, "project": _PROJECT,
                          "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text"])},
    {"name": "build", "handler": t_build, "wants_request_id": True,
     "description": "Run the project's model.py in a fresh FreeCAD document, save build/<name>.FCStd and "
                    "return the BUILD REPORT: per-object validity, volume, mass, bbox; every PartDesign "
                    "feature with its error message (a failed feature is invisible in renders — FreeCAD "
                    "keeps the last good shape); hole specs (with any print hole compensation); sketch "
                    "DoF/conflicts; script errors with model.py line; checks.json acceptance checks (✓/✗ with "
                    "measured values — types: valid, bbox, bound/min_z…, mass, volume, distance, clearance, "
                    "interference, radial_gap, coaxial, align, pattern, parallel, perpendicular, coplanar, "
                    "hole_count, hole_diameter (holes + d; nominal=true compares with the design Ø before print "
                    "compensation), positions, face_count — guide('checks')); warnings; Δ against the previous build; plus a render. Read the whole "
                    "report every time. Unchanged inputs keep the same revision. params overrides parameters "
                    "without editing params.json; variant writes a separate file for what-if builds. If the user "
                    "edited the build file in the FreeCAD GUI, build refuses and lists the edits "
                    "(adopt_gui_params copies parameter edits back).",
     "inputSchema": _obj({
         "project": _PROJECT,
         "params": {"type": "object", "description": "Parameter overrides, e.g. {\"thickness\": 6}."},
         "variant": {"type": "string", "description": "Build into build/<name>__<variant>.FCStd."},
         "views": _VIEWS, "size": {"type": "string", "description": "Image size 'WxH'."},
         "force": {"type": "boolean", "description": "Overwrite a build file that was edited outside the build loop "
                                                     "(only after porting its edits into model.py)."},
         "adopt_gui_params": {"type": "boolean", "description": "If the build file was edited in the FreeCAD GUI, copy "
                              "Params-sheet edits into params.json and rebuild. Structural edits still block."},
         "skip_checks": {"type": "boolean", "description": "Do not evaluate checks.json."},
         "return_image": {"type": "boolean", "description": "Default true."},
         "timeout": {"type": "integer", "description": "Seconds. Default 300."}, "wait": _WAIT},
         ["project"])},
    {"name": "run_script", "handler": t_run_script, "wants_request_id": True,
     "description": "Run FreeCAD Python against a document (or a new empty one) with fc, App, Part, Sketcher, "
                    "Vector and doc predefined. Use it to query anything the other tools don't report, or to "
                    "patch an existing user model: save='revision' writes <name>.rNN.FCStd and never touches "
                    "the original; 'inplace' keeps a .bak. Return data with fc.result(key=value). Do not use "
                    "it to edit a project's built file — change model.py and build instead. Images (views) go to "
                    "the project's out/, or ~/.cache/freecad-mcp/images for a file outside a project.",
     "inputSchema": _obj({
         "code": {"type": "string"}, "doc": _DOC, "project": _PROJECT,
         "save": {"type": "string", "enum": ["no", "revision", "inplace"], "description": "Default no."},
         "save_as": {"type": "string", "description": "Save to this path instead."},
         "from_older": {"type": "boolean", "description": "save='revision' on an older revision when a newer "
                                                          ".rNN exists: fork a new revision deliberately."},
         "views": _VIEWS, "size": {"type": "string"}, "return_image": {"type": "boolean"},
         "timeout": {"type": "integer"}, "wait": _WAIT},
         ["code"])},
    {"name": "inspect", "handler": t_inspect, "wants_request_id": True,
     "description": "Read a document without changing it. level=summary: results, validity, mass, sketch "
                    "status, errors, parameters, object tree. level=object (with object): placement, bbox, "
                    "centroid, named frames, features and hole specs, DETECTED HOLES (diameters, axis, "
                    "through/blind with entry and bottom points), and a geometry index of every face "
                    "(plane normal/offset, cylinder radius/axis/concave). level=sketch: geometry and "
                    "constraints with values and expressions. level=params: spreadsheet values and every "
                    "expression using them. Works on .FCStd, STEP, IGES and BREP.",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT, "object": {"type": "string",
                                                                      "description": "Object label or name."},
                          "level": {"type": "string", "enum": ["summary", "object", "sketch", "params", "changes"],
                                    "description": "changes = edits made to the build file outside model.py "
                                                   "(e.g. in the FreeCAD GUI) since the last build."},
                          "max_faces": {"type": "integer"}, "timeout": {"type": "integer"}})},
    {"name": "render", "handler": t_render, "wants_request_id": True,
     "description": "Headless image of a document: one labelled grid of views at a common scale with a "
                    "scale bar and axis triad. style shaded | xray (see internal features) | drawing (black "
                    "visible lines, dashed hidden lines — compare against a reference drawing). section cuts "
                    "the model and hatches the cut faces: {\"y\": 20} removes the side toward the standard "
                    "viewer (+X, −Y, +Z; add \"flip\": true for the other side) or {point, normal} removes "
                    "the +normal side. highlight colours chosen objects and greys the rest. Without out_path the "
                    "image goes to the project's out/ (for a file outside a project: ~/.cache/freecad-mcp/images "
                    "— never next to the user's file). Images orient you; they do not prove fit.",
     "inputSchema": _obj({
         "doc": _DOC, "project": _PROJECT, "views": _VIEWS,
         "objects": {"type": "array", "items": {"type": "string"}, "description": "Only these objects."},
         "highlight": {"type": "array", "items": {"type": "string"}},
         "style": {"type": "string", "enum": ["shaded", "xray", "drawing"]},
         "section": {"type": "object", "description": "{x|y|z: offset, flip?} or {point:[x,y,z], normal:[x,y,z]}"},
         "size": {"type": "string", "description": "'WxH', default 1024x768."},
         "hidden": {"type": "boolean", "description": "Draw hidden edges (default true)."},
         "labels": {"type": "boolean", "description": "Label objects (default true)."},
         "out_path": {"type": "string"}, "return_image": {"type": "boolean"},
         "timeout": {"type": "integer"}, "wait": _WAIT})},
    {"name": "measure", "handler": t_measure, "wants_request_id": True,
     "description": "Measure geometry with numbers you can quote. One ref describes it (object: bbox, volume, "
                    "centroid, mass; holes: list). Two refs compare them, chosen by type (the order of a and b "
                    "does not matter): hole pattern ↔ hole pattern → best pairing, per-hole axis offset, max "
                    "offset, unmatched holes; axis ↔ axis (holes, bosses, cylinders, frames, lines) → angle, "
                    "offset, COAXIAL verdict and minimum radial gap when both have diameters; one hole ↔ object → "
                    "wall gap (explained when only the rim touches) and any coaxial shaft/boss with its radial gap; "
                    "hole ↔ its own part → minimum wall around it; axis ↔ plane → angle; plane ↔ plane → angle, "
                    "distance, parallel/coplanar; frame ↔ frame → translation and rotation; a FRAME against "
                    "anything else acts as its +Z axis: point ↔ frame/axis → radial and axial distance from the "
                    "axis, object/face ↔ frame/axis → minimum distance to the axis line and whether it passes "
                    "through the solid; anything else → minimum distance with closest points and any "
                    "interference. " + "References: an object label ('Bracket'); 'Bracket.Face6'/'.Edge3'/'.Vertex2' (this revision only); 'Bracket.face(+Z)', 'Bracket.face(-Y, at=0)', 'Bracket.face(+Z, contains=[0,0,8])'; 'Bracket.holes', 'Bracket.holes(d=3.4)', 'Bracket.holes(d=3.4)[0]', 'Bracket.holes(axis=Y)'; 'Motor.bosses(d=22)[0]', 'Motor.bosses(axis=Z)' (shafts, pilots, pins — usable wherever a hole is); 'Motor.frame(mount)'; a point [x,y,z]; 'plane(z=8)'; 'axis([x,y,z],[dx,dy,dz])'.",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT,
                          "a": dict(_REF, description="First reference (or use queries)."),
                          "b": dict(_REF, description="Second reference (optional)."),
                          "queries": {"type": "array", "items": {"type": "object", "properties": {
                              "a": _REF, "b": _REF, "tol": {"type": "number"}}},
                                      "description": "Several measurements: [{a, b?, tol?}, ...]."},
                          "tol": {"type": "number", "description": "Coaxial/coplanar tolerance in mm. Default 0.01."},
                          "timeout": {"type": "integer"}})},
    {"name": "check_fit", "handler": t_check_fit, "wants_request_id": True,
     "description": "Interference and clearance between every pair of solids (or the listed objects). Each pair "
                    "is INTERFERENCE (overlap volume and location), contact (touching, no overlap — with the "
                    "nearest real gap elsewhere, e.g. a pilot in its bore), gap (minimum distance and points), or "
                    "clear. Fails pairs below min_clearance (default: project.json clearance.default_min_mm; "
                    "pair_min overrides it per pair, for designed hardware gaps), unexpected contacts, "
                    "and interference not listed in interference_ok (press fits, threads). Renders interfering "
                    "volumes in red.",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT,
                          "objects": {"type": "array", "items": {"type": "string"}},
                          "min_clearance": {"type": "number", "description": "mm. Default project.json "
                                                                             "clearance.default_min_mm."},
                          "contact_ok": {"type": "array", "items": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                                                              {"type": "string"}]},
                                         "description": "Pairs allowed to touch, e.g. [[\"Bracket\", \"Motor\"]]."},
                          "interference_ok": {"type": "array", "items": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
                                              "description": "Pairs allowed to overlap (press fit, thread)."},
                          "pair_min": {"type": "object", "additionalProperties": {"type": "number"},
                                       "description": "Per-pair minimum clearance overriding min_clearance, keyed "
                                                      "\"A|B\" in either order, e.g. {\"M5Screws|M5Washers\": 0.1} "
                                                      "for an ISO 7089 washer bore around a shank."},
                          "within": {"type": "number", "description": "Analyse pairs closer than this (mm). Default 5."},
                          "render": {"type": "boolean", "description": "Render even without interference."},
                          "size": {"type": "string"}, "return_image": {"type": "boolean"},
                          "timeout": {"type": "integer"}, "wait": _WAIT})},
    {"name": "section", "handler": t_section, "wants_request_id": True,
     "description": "Cut the model with a plane and analyse the 2D section: each body's area and extent, and "
                    "every pair's in-plane gap (or overlap = interference; for touching parts, the nearest real "
                    "gap). Returns a hatched, dimensioned plot with zoomed insets of the tightest gaps. plane "
                    "{\"z\": 12} is viewed from +Z, {\"y\": 0} from −Y (like the front view), {\"x\": 5} "
                    "from +X; or {point, normal}. Cut through hole and shaft axes to check clearances (a cut "
                    "containing an axis reports the gap on each side of it). interference_ok / contact_ok take the "
                    "same pair lists as check_fit, so intended thread overlaps and contacts are not flagged. The plot "
                    "is saved in the project's out/ (for a file outside a project: ~/.cache/freecad-mcp/images).",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT, "plane": {"type": "object"},
                          "objects": {"type": "array", "items": {"type": "string"}},
                          "min_clearance": {"type": "number"}, "within": {"type": "number"},
                          "contact_ok": {"type": "array", "items": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                                                              {"type": "string"}]},
                                         "description": "Pairs allowed to touch, e.g. [[\"Bracket\", \"Motor\"]]."},
                          "interference_ok": {"type": "array", "items": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
                                              "description": "Pairs allowed to overlap (press fit, thread)."},
                          "size": {"type": "string"}, "return_image": {"type": "boolean"},
                          "timeout": {"type": "integer"}}, ["plane"])},
    {"name": "lookup_standard", "handler": t_lookup_standard,
     "description": "Standard dimensions — use instead of memory. size='M3' → pitch, tap drill, ISO 273 "
                    "clearance (fine/normal/coarse), ISO 4762 head + DIN 974 counterbore, ISO 10642 "
                    "countersink, ISO 7380 button head, ISO 4032 nut + nut-trap size, ISO 7089 washer, heat-set "
                    "insert hole (vendor-typical), thread engagement. fit='H7/g6' with d=8 → ISO 286 limits, "
                    "clearance range and fit type; fit='k6' (one class) with d → that class's limits. "
                    "tolerance='ISO 2768-m' (or 'f'|'m'|'c'|'v') with d=80 → the general tolerance band at that "
                    "size (without d: the whole table). bearing='608' → deep-groove ball bearing bore d, outer D, "
                    "width B, chamfer r_s min, seat fits. "
                    "process='fdm'|'sla'|'cnc'|'turning'|'laser'|'sheet' → design rules. Several keys can be "
                    "combined in one call.",
     "inputSchema": _obj({"size": {"type": "string", "description": "Metric thread size, e.g. 'M3'."},
                          "material": {"type": "string", "description": "Material name → E, ν, yield, density "
                                       "(the values analyze uses), e.g. 'PETG', '6061-T6'."},
                          "fit": {"type": "string", "description": "'H7/g6' (hole/shaft pair) or one class 'k6' / "
                                                                   "'H7'; needs d."},
                          "tolerance": {"type": "string", "description": "ISO 2768 general tolerance class: 'm' or "
                                                                         "'ISO 2768-m' (f, m, c, v); with d → the "
                                                                         "band at that size."},
                          "bearing": {"type": "string", "description": "Deep-groove ball bearing designation, e.g. "
                                                                       "'608', '6204-2RS', '625'."},
                          "d": {"type": "number", "description": "Nominal size in mm for fit / tolerance."},
                          "process": {"type": "string", "enum": ["fdm", "sla", "cnc", "turning", "laser", "sheet"]}})},
    {"name": "export", "handler": t_export, "wants_request_id": True,
     "description": "Export parts for manufacturing and verify every file: step/iges (named; re-imported and "
                    "volume-checked), stl/3mf/obj (watertight check, volume deviation, tolerance in mm), brep, "
                    "and dxf/svg flat profiles for laser/waterjet/router (cut along the plate's thin axis or view "
                    "x|y|z; flags parts that are not a uniform plate). Default: every non-reference solid, STEP, "
                    "into out/export. For a complete supplier package use release instead.",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT,
                          "objects": {"type": "array", "items": {"type": "string"}},
                          "formats": {"type": "array", "items": {"type": "string",
                                      "enum": ["step", "iges", "brep", "stl", "3mf", "obj", "dxf", "svg"]}},
                          "out_dir": {"type": "string"},
                          "tolerance": {"type": "number", "description": "Mesh chord tolerance mm (default 0.02)."},
                          "angular_deg": {"type": "number", "description": "Mesh angular tolerance (default 5°)."},
                          "view": {"type": "string", "description": "dxf/svg cut axis x|y|z (default: thinnest)."},
                          "timeout": {"type": "integer"}})},
    {"name": "drawing", "handler": t_drawing, "wants_request_id": True,
     "description": "Technical drawings (FreeCAD TechDraw, ISO). action=create: one sheet per manufactured part "
                    "(or part/parts): detects the style (from the part's process first — laser/sheet → plate, turning → turned — then the shape: "
                    "prismatic → front/top/left first-angle + iso; plate → edge + face view; turned → side view "
                    "with the axis horizontal + end view, a Ø dimension per step, fits and thread callouts on their "
                    "own steps, baseline lengths, and SECTION A-A through the axis when the part has a bore (section="
                    "auto|full|none); bent sheet (fc.sheet_metal) → three views, bend table and a flat-pattern view), "
                    "picks paper and the largest standard scale that fits, adds overall, hole-position and pocket "
                    "dimensions (size, location, depth, corner radii), hole/thread callouts from the Hole features (e.g. '4× ⌀3.4 THRU ⌴ "
                    "⌀6.5 ↧3.4', '2× M3×0.5-6H ↧5', '⌀8 H7 THRU'), notes (material, ISO 2768 general tolerance, "
                    "process notes) and the title block (revision = the build revision it was drawn from); checks every "
                    "dimension against the model; hidden=true shows hidden lines on the orthographic views (turned "
                    "parts show them on the side view by default); hole_dims=\"pitch\" dimensions a hole pattern by "
                    "its pitch plus one location from a datum edge (default \"baseline\": every hole from the edges); "
                    "section=\"full\" always draws a turned part's section A-A through the axis, \"none\" never "
                    "(default \"auto\": when it has bores or other internal features); saves "
                    "drawings/<Part>.FCStd, exports PDF/SVG/DXF/PNG to out/drawings and returns a preview. The "
                    "options you pass (only those) are saved in drawings.json — under the part, or for every part "
                    "without part — and reused by later creates and by release; reset=true clears that part's "
                    "saved options first (without part: \"*\" and every part's, create ops included — the sheets "
                    "are drawn without them); scale/paper \"auto\" go back to automatic. ops on create (one part) are "
                    "edit ops saved with the part and replayed on every regeneration, release included — use them "
                    "for additions that must survive. action=read: describe a drawing's pages, views, dimensions "
                    "and notes with a preview (default: the project's newest sheet). action=edit: ops on one sheet "
                    "file (default: the project's newest sheet; must be the newest revision unless from_older) — "
                    "add_dimension {view, a, b, type (DistanceX|DistanceY|Distance|Diameter|Radius), text?, "
                    "tolerance?, x?, y?} where a/b are hole/point refs ('Part.holes(d=3.4)[0]'), a planar face "
                    "('Part.face(+X)') or a view vertex as read prints it ('Top.Vertex16'), and x/y offset the "
                    "dimension from the view centre in paper mm (default: outside the view); add_hole_callout "
                    "{view, a: 'Part.holes(d=…)'}; add_note {text, x, y, size?, align?} with x/y the text centre in "
                    "page mm, or with align=\"left\" the left end of the first line's baseline; append_note {text} "
                    "adds a numbered line to the generated NOTES block; set_title {fields}; move {name, x, y}; "
                    "remove {name} or remove {match: {view, value, type}} (every generated dimension that matches — "
                    "use it in create ops, which are replayed after regenerations renumber the dimensions; a remove "
                    "that matches nothing is reported as failed). Notes you pass are kept exactly as written (not "
                    "upper-cased). Edits save a new revision <Part>.rNN.FCStd whose title-block revision gets an "
                    "edit suffix (r0001 → r0001.1 for .r01) (or save='inplace', backup in .history/backups) and "
                    "change only that file — a later create regenerates the sheet and sets hand-edited revisions "
                    "aside. Each call runs FreeCAD in the background for a few seconds (no splash screen; on macOS "
                    "nothing appears on screen).",
     "inputSchema": _obj({
         "action": {"type": "string", "enum": ["create", "read", "edit"], "description": "Default create."},
         "project": _PROJECT,
         "doc": {"type": "string", "description": "create: the model (default the project's build). read/edit: "
                                                  "the drawing .FCStd (default the project's newest sheet)."},
         "part": {"type": "string", "description": "Part label: the sheet to create, or whose newest sheet to "
                                                   "read/edit."},
         "parts": {"type": "array", "items": {"type": "string"}},
         "style": {"type": "string", "enum": ["auto", "prismatic", "plate", "turned"]},
         "views": {"type": "array", "items": {"type": "string",
                                              "enum": ["front", "top", "left", "right", "bottom", "back"]}},
         "iso": {"type": "boolean", "description": "Add an isometric view (default true; the reply says if "
                                                   "there was no room for it)."},
         "hidden": {"type": "boolean", "description": "create: show hidden lines on the orthographic views "
                                                      "(default false; turned parts show them on the side view)."},
         "hole_dims": {"type": "string", "enum": ["baseline", "pitch"],
                       "description": "create: hole-position dimensions — baseline (default): each hole from the "
                                      "datum edges; pitch: the pattern pitch plus one location from a datum edge."},
         "section": {"type": "string", "enum": ["auto", "full", "none"],
                     "description": "create: section through the axis of a turned part (hatched, labelled A-A) — "
                                    "auto (default): when it has bores or internal grooves/chamfers; full: always; "
                                    "none: never."},
         "scale": {"type": ["number", "string"], "description": "Force a scale: 0.5, \"1:2\", \"2:1\", \"1/2\"; "
                                                                "\"auto\" (default) picks the largest that fits."},
         "paper": {"type": "string", "enum": ["auto", "A4", "A3", "A2", "A1"], "description": "Default auto."},
         "notes": {"type": "array", "items": {"type": "string"}, "description": "Extra notes (added after the "
                                                                                "standard ones, kept as written)."},
         "finish": {"type": "string"}, "material": {"type": "string"},
         "process": {"type": "string", "enum": ["fdm", "sla", "cnc", "turning", "laser", "sheet"],
                     "description": "Default project.json parts.<Part>.process, else the project process."},
         "title_fields": {"type": "object", "description": "Title block overrides: title, creator, drawing_number, …"},
         "drawing_number": {"type": "string"}, "revision": {"type": "string"},
         "dimensions": {"type": "string", "enum": ["auto", "none"], "description": "auto (default) adds overall, "
                                                                                 "hole-position and Ø dimensions."},
         "reset": {"type": "boolean", "description": "create: clear the saved options of this part (without part: "
                                                     "of \"*\" and of every part, ops included) before applying "
                                                     "the ones passed; the sheet is drawn without the cleared ones."},
         "page": {"type": "string"},
         "ops": {"type": "array", "items": {"type": "object"},
                 "description": "edit: ops applied to the sheet file. create (with exactly one part): ops saved "
                                "with the part and replayed on every regeneration; they replace the saved list."},
         "save": {"type": "string", "enum": ["revision", "inplace"], "description": "edit: default revision."},
         "from_older": {"type": "boolean", "description": "edit: allow editing an older revision of a sheet "
                                                          "(saves a new revision after the newest)."},
         "return_image": {"type": "boolean"}, "timeout": {"type": "integer"}})},
    {"name": "analyze", "handler": t_analyze, "wants_request_id": True,
     "description": "FEA of one part, or of several parts bonded together (gmsh tet10 mesh, CalculiX on all cores). "
                    "Rebuilds first, then solves a case: `fixed` — faces/holes that are held (geometry references: "
                    "\"Bracket.holes(d=5.5)\" for bolted holes, \"Bracket.face(-Z)\" for a clamped face); `loads` — "
                    "[{\"ref\": face/hole, \"force\": N, \"direction\": \"-Z\" | [x,y,z]}, {\"ref\": …, \"force\": "
                    "[fx,fy,fz]}, {\"ref\": …, \"pressure\": MPa (pushes on the face)}] (a force is spread over the "
                    "referenced faces); gravity=true adds self weight. analysis=\"static\" (default): max von Mises "
                    "stress and where (also away from the supports, where rigid fixtures inflate it), safety factor "
                    "against yield, max displacement and where, support reactions vs the applied load (equilibrium ✓). "
                    "analysis=\"frequency\": the first `modes` (default 6) natural frequencies and mode shapes with the "
                    "supports as given (loads are not used; needs the material density). analysis=\"buckling\": linear "
                    "buckling load factors of the given loads (default 3 modes) — the lowest positive one is how many "
                    "times the load buckles the part. Several parts: parts=[\"Base\", \"Bracket\"] with bonded=[[\"Base."
                    "face(+Z)\", \"Bracket.face(-Z)\"], …] — face/hole pairs on two parts that touch and are glued, "
                    "clamped or pressed together (tied, CalculiX *TIE); supports and loads may be on any of the parts; "
                    "results per part, each against its own material. Material from project.json parts (E, ν, yield, "
                    "density from the material table) or material={\"E\": MPa, \"nu\": …, \"yield\": MPa, \"density\": "
                    "g/cm³} / a material name. refine re-solves on a finer mesh to show convergence: by default only "
                    "when the first mesh is small (≤ 50 000 nodes — the default mesh puts ≥ 2 elements through the "
                    "thinnest wall; the reply says when it skipped), true forces it, false skips it. Returns "
                    "an image (stress/deflection, or the mode shapes). The case is saved in fea.json under `case` "
                    "(default 'default') — re-run it by name; checks.json fea_safety / fea_stress / fea_displacement "
                    "(static), fea_frequency (first natural frequency ≥ min Hz) and fea_buckling (lowest positive "
                    "buckling factor ≥ min) use the latest result and go stale when the model or case changes (release "
                    "re-solves stale cases). Limits: linear elastic, small deflections, rigid supports, bonded faces "
                    "never slide or separate (no contact, no bolt preload).",
     "inputSchema": _obj({"project": _PROJECT, "case": {"type": "string", "description": "Load case name (fea.json)."},
                          "part": {"type": "string", "description": "Part label (default: the only part)."},
                          "parts": {"type": "array", "items": {"type": "string"},
                                    "description": "Several parts analysed together (instead of part)."},
                          "bonded": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                                     "description": "Pairs of touching faces/holes on two parts that are glued "
                                                    "together: [[\"Base.face(+Z)\", \"Bracket.face(-Z)\"], …]."},
                          "analysis": {"type": "string", "enum": ["static", "frequency", "buckling"],
                                       "description": "Default static."},
                          "modes": {"type": "integer", "description": "Modes to compute (frequency default 6, "
                                                                      "buckling default 3)."},
                          "fixed": {"type": "array", "items": {"type": "string"}},
                          "loads": {"type": "array", "items": {"type": "object"}},
                          "gravity": {"type": "boolean"},
                          "material": {"type": ["string", "object"]},
                          "mesh_size": {"type": "number", "description": "Element size in mm (default from the part)."},
                          "refine": {"type": "boolean", "description": "Also solve on a finer mesh: default only when "
                                                                       "the first mesh is small; true forces it, false "
                                                                       "skips it."},
                          "save": {"type": "boolean", "description": "Save the case in fea.json (default true)."},
                          "params": {"type": "object", "description": "What-if parameter overrides: builds a variant "
                                     "and solves it without touching params.json, the saved case or its result."},
                          "wait": {"type": "boolean"}, "return_image": {"type": "boolean"},
                          "timeout": {"type": "integer"}}, required=["project"])},
    {"name": "check_manufacturing", "handler": t_check_manufacturing, "wants_request_id": True,
     "description": "Design-for-manufacture checks per part for its process (from project.json parts.<name>.process, "
                    "else the project process, or the process argument): fdm/sla → best print orientation (least "
                    "overhang, most bed contact), overhang area, build-volume fit, thinnest wall, tiny holes, sagging "
                    "horizontal holes, tapped plastic threads; cnc → sharp internal corners a cutter can't make, thin "
                    "walls, deep holes, setup count; turning → lathe-part check, bar size, L/D (tailstock), secondary "
                    "ops, threads; laser/sheet → uniform plate, holes smaller than the thickness. Verdict per part: "
                    "ready / review warnings / not manufacturable.",
     "inputSchema": _obj({"doc": _DOC, "project": _PROJECT,
                          "objects": {"type": "array", "items": {"type": "string"}},
                          "process": {"type": "string", "enum": ["fdm", "sla", "cnc", "turning", "laser", "sheet"]},
                          "timeout": {"type": "integer"}})},
    {"name": "release", "handler": t_release, "wants_request_id": True,
     "description": "Make the supplier package in one step. Rebuilds model.py and stops if the build has errors, "
                    "if the build file has unported GUI edits, if any checks.json check fails, if a part is not "
                    "manufacturable for its process, if project.json parts / qty / processes name a part that "
                    "doesn't exist, or if an exported file fails verification. Then exports each part for its "
                    "process (fdm/sla → 3MF + STL oriented for printing + STEP; cnc/turning → STEP + PDF drawing; "
                    "laser → DXF + SVG + PDF drawing), writes BOM.csv (made parts and bom=\"buy\" reference "
                    "parts) and README.md (what to send to whom, quantities, material, finish, print orientation, "
                    "hole compensation, bar stock, holes, fits, threads, notes, verification table, checks changed "
                    "since the previous release, build report) and zips release/<name>_<rev>.zip. An earlier "
                    "package is replaced only if it was made from the same inputs AND holds the same package (parts, "
                    "qty, process(es), drawing mode and sheets); otherwise this one gets a -2 suffix. Drawings "
                    "(auto): a part's saved sheet in drawings/ is shipped as is when it still matches the model "
                    "(shape, hole positions, fits, threads) and what it is drawn from (saved options, material, "
                    "finish, notes, title block, general tolerance) — its title block keeps the revision it was "
                    "drawn or edited at; a stale one is regenerated with its saved options and create ops and saved "
                    "back — but if it was edited by hand, release stops and says how to proceed. If saved create ops "
                    "fail on a sheet, release stops (allow_failed_ops=true ships it without them, with the user's "
                    "OK). Processes, quantities, materials and finishes come from project.json parts.<label> (or the "
                    "arguments).",
     "inputSchema": _obj({"project": _PROJECT,
                          "parts": {"type": "array", "items": {"type": "string"}},
                          "process": {"type": "string", "enum": ["fdm", "sla", "cnc", "turning", "laser", "sheet"],
                                      "description": "Process for every part (else project.json)."},
                          "processes": {"type": "object", "description": "Per part, e.g. {\"Bracket\": \"cnc\"}."},
                          "qty": {"type": "object", "description": "Per part quantities, e.g. {\"Bracket\": 4}."},
                          "drawings": {"type": "string", "enum": ["auto", "all", "generate", "none"],
                                       "description": "auto (default): drawings for machined/cut parts, reusing "
                                                      "current saved sheets; all: also for fdm/sla parts; generate: "
                                                      "always regenerate (a hand-edited sheet is left untouched and "
                                                      "not shipped); none: no drawings. Only the PDF is shipped."},
                          "allow_failing_checks": {"type": "boolean"}, "allow_dfm_errors": {"type": "boolean"},
                          "allow_failed_exports": {"type": "boolean", "description": "Ship files that failed "
                                                   "verification (only with the user's OK)."},
                          "allow_failed_ops": {"type": "boolean", "description": "Ship drawings whose saved create "
                                               "ops failed, without what those ops add (only with the user's OK; "
                                               "listed with ✗ in the output and the README)."},
                          "return_image": {"type": "boolean", "description": "Default true: drawing previews."}})},
    {"name": "job_status", "handler": t_job_status,
     "description": "Collect or check a background job started with wait=false (no job_id lists all jobs).",
     "inputSchema": _obj({"job_id": {"type": "string"},
                          "wait_seconds": {"type": "number", "description": "Block up to this long (max 60)."},
                          "return_image": {"type": "boolean"}})},
    {"name": "open_in_gui", "handler": t_open_in_gui,
     "description": "Open a file (or a project's build) in the FreeCAD GUI so the user can look at it, "
                    "starting FreeCAD if needed. install_reload_helper=true (only with the user's OK — it adds "
                    "an addon to their FreeCAD Mod folder and the MCP_Reload macro to their macro folder) installs "
                    "MCP Reload: open documents then reload automatically whenever a build or drawing is "
                    "regenerated, keeping the camera, and never over unsaved edits. It starts with FreeCAD; if "
                    "FreeCAD is already running, the user runs Macro ▸ Macros… ▸ MCP_Reload once — unless it was "
                    "installed before and FreeCAD's Report view already shows 'MCP Reload: watching' (the macro is "
                    "a toggle: running it then stops the helper). Relay the reply's note as written.",
     "inputSchema": _obj({"path": {"type": "string"}, "project": _PROJECT,
                          "install_reload_helper": {"type": "boolean"},
                          "launch": {"type": "boolean", "description": "Default true; false only installs the helper."}})},
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ----------------------------------------------------------------- protocol

def server_instructions():
    p = os.path.join(HERE, "..", "prompt", "server_instructions.md")
    alt = os.path.join(HERE, "server_instructions.md")
    for cand in (alt, p):
        if os.path.isfile(cand):
            with open(cand) as fh:
                return re.sub(r"<!--.*?-->\n?", "", fh.read(), flags=re.S).strip()
    return ("FreeCAD design server. Units mm; Z up. Call guide once at the start of a design task. "
            "Write model.py, build, and read the whole build report; never claim fit from an image.")


def tool_descriptors():
    return [{k: v for k, v in t.items() if k not in ("handler", "wants_request_id")} for t in TOOLS]


_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _json_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    return {str: "string", dict: "object", list: "array"}.get(type(v), type(v).__name__)


def _type_ok(v, t):
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())
    if t == "null":
        return v is None
    return isinstance(v, _JSON_TYPES.get(t, object))


def _short(v):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else repr(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _schema_problems(v, sch, where):
    """Type, enum and required problems of a value against a (sub)schema."""
    alts = sch.get("anyOf") or sch.get("oneOf")
    if alts:
        errs = [_schema_problems(v, a, where) for a in alts]
        if all(errs):
            # report only the forms of the value's own type: 'sideways' → the string enum's message
            same = [e for e, a in zip(errs, alts) if not a.get("type") or any(
                _type_ok(v, x) for x in (a["type"] if isinstance(a["type"], list) else [a["type"]]))]
            if len(same) == 1:
                return same[0]
            return [min(errs, key=len)[0] if len(alts) == 1 else "%s does not match any allowed form (%s)" % (
                where, " | ".join(e[0] for e in (same or errs)))]
        return []
    t = sch.get("type")
    if t:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in ts):
            return ["%s must be %s, got %s %s" % (where, " or ".join(ts), _json_type(v), _short(v))]
    if "enum" in sch and v not in sch["enum"]:
        return ["%s must be one of %s, got %s" % (where, ", ".join(map(str, sch["enum"])), _short(v))]
    if isinstance(v, list) and ("minItems" in sch or "maxItems" in sch):
        lo, hi = sch.get("minItems", 0), sch.get("maxItems")
        if len(v) < lo or (hi is not None and len(v) > hi):
            return ["%s must have %s item%s, got %d: %s" % (where, lo if lo == hi else "%d–%s" % (lo, hi or "∞"),
                                                            "" if lo == hi == 1 else "s", len(v), _short(v))]
    errs = []
    if isinstance(v, list) and isinstance(sch.get("items"), dict):
        for i, x in enumerate(v):
            errs += _schema_problems(x, sch["items"], "%s[%d]" % (where, i))
    if isinstance(v, dict) and sch.get("properties"):
        props = sch["properties"]
        for k, x in v.items():
            if k in props and x is not None:
                errs += _schema_problems(x, props[k], "%s.%s" % (where, k))
            elif k not in props and sch.get("additionalProperties") is False:
                errs.append("%s has no field '%s' (fields: %s)" % (where, k, ", ".join(props)))
        errs += ["%s needs '%s'" % (where, k) for k in sch.get("required", []) if v.get(k) is None]
    if isinstance(v, dict) and isinstance(sch.get("additionalProperties"), dict):      # a map, e.g. pair_min
        for k, x in v.items():
            if k not in (sch.get("properties") or {}) and x is not None:
                errs += _schema_problems(x, sch["additionalProperties"], "%s[\"%s\"]" % (where, k))
    return errs


def validate_args(name, schema, args):
    """Check a tool call's arguments against its inputSchema before running it, so a
    misspelled or mistyped argument is an error instead of being silently ignored.
    Returns the arguments without null values (null = not given)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments for %s must be a JSON object, got %s" % (name, _json_type(args)))
    props = schema.get("properties") or {}
    errs = []
    for k in args:
        if k not in props and schema.get("additionalProperties") is False:
            near = difflib.get_close_matches(k, list(props), n=1, cutoff=0.6)
            errs.append("unknown argument '%s' for %s%s — valid: %s" % (
                k, name, " (did you mean '%s'?)" % near[0] if near else "", ", ".join(props) or "none"))
    for k in schema.get("required", []):
        if args.get(k) is None:
            errs.append("missing required argument '%s' for %s" % (k, name))
    for k, v in args.items():
        if k in props and v is not None:
            errs += _schema_problems(v, props[k], k)
    if errs:
        raise ToolError("%s\nNothing was run. Fix the arguments and call %s again (its description lists them)."
                        % ("\n".join(errs), name))
    return {k: v for k, v in args.items() if v is not None}


def call_tool(name, args, request_id=None):
    tool = TOOLS_BY_NAME.get(name)
    if not tool:
        return {"content": [{"type": "text", "text": "Unknown tool: %s — tools: %s" % (
            name, ", ".join(sorted(TOOLS_BY_NAME)))}], "isError": True}
    try:
        args = validate_args(name, tool["inputSchema"], args)
        if tool.get("wants_request_id"):
            result = tool["handler"](args or {}, request_id=request_id)
        else:
            result = tool["handler"](args or {})
    except ToolError as e:
        return {"content": [{"type": "text", "text": "Error: %s" % e}], "isError": True}
    except Exception as e:  # noqa: BLE001
        log("tool %s crashed: %r" % (name, e))
        return {"content": [{"type": "text", "text": "Error: %s: %s" % (type(e).__name__, e)}], "isError": True}
    if isinstance(result, list):
        return {"content": result}
    return {"content": [{"type": "text", "text": str(result)}]}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": server_instructions()}}
    if method == "notifications/cancelled":
        killed = cancel_request(params.get("requestId"))
        if killed:
            log("cancelled request %s — killed %d worker(s)" % (params.get("requestId"), killed))
        return None
    if method in ("notifications/initialized",):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_descriptors()}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid,
                "result": call_tool(params.get("name"), params.get("arguments"), request_id=mid)}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}


def main():
    """JSON-RPC over stdio, one request per line, each handled on its own thread
    so a slow build never blocks other tools. Responses may arrive out of
    order; clients match on id."""
    fcinfo = find_freecad()
    log("ready (freecad=%s, home=%s)" % (fcinfo["exe"] if fcinfo else "NOT FOUND", home_dir()))
    write_lock = threading.Lock()

    def emit(payload):
        with write_lock:
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()

    def serve(msg):
        try:
            if isinstance(msg, list):
                out = [r for r in (handle(m) for m in msg) if r]
                if out:
                    emit(out)
                return
            payload = handle(msg)
            if payload is not None:
                emit(payload)
        except Exception as e:  # noqa: BLE001
            log("handler crashed: %r" % e)
            mid = msg.get("id") if isinstance(msg, dict) else None
            if mid is not None:
                emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "Internal error: %s" % e}})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError as e:
                log("bad JSON: %s" % e)
                continue
            t = threading.Thread(target=serve, args=(msg,))
            t.daemon = True
            t.start()
    finally:
        killed = kill_all_children()
        if killed:
            log("shutting down — killed %d FreeCAD worker(s)" % killed)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
