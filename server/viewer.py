# SPDX-License-Identifier: Apache-2.0
"""Live 3D viewer: a small localhost web server (stdlib only) for the browser page
viewer/index.html.

Every build writes <project>/build/viewer/scene.json and scene.glb (scene.py). The
page polls /api/scene and reloads the model when the version changes, so the
user can keep one tab open through the whole design and watch each build
arrive. With no project in the URL it follows the most recently built project
under the projects home.

All state is in the project files, so any freecad-mcp process can serve any
project. If the port is already held by another freecad-mcp server for the
same home, that server is reused. If the server holding it stops, the next
ensure() takes the port over and an open page carries on.

Safety: binds 127.0.0.1 only, rejects requests whose Host header is not local
(DNS rebinding), sends no CORS headers, and serves only the page plus
scene.json / scene.glb inside a project the server's own path rules allow."""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "freecad-mcp-viewer"
DEFAULT_PORT = 7787
SUBDIR = ("build", "viewer")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "viewer", "index.html")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class NotFound(Exception):
    pass


def scene_files(pdir):
    d = os.path.join(pdir, *SUBDIR)
    return os.path.join(d, "scene.json"), os.path.join(d, "scene.glb")


def project_key(pdir, home):
    """How the page names a project: its folder name when it sits directly in the
    home, else its absolute path."""
    pdir = os.path.abspath(pdir)
    return os.path.basename(pdir) if os.path.dirname(pdir) == os.path.abspath(home) else pdir


class Viewer:
    """One viewer web server. resolve_project(key) → project dir, raising on a bad or
    disallowed key (freecad_mcp passes its own project_dir, so the usual roots apply)."""

    def __init__(self, home, resolve_project, port=None):
        self.home = os.path.abspath(home)
        self.resolve_project = resolve_project
        self.port = DEFAULT_PORT if port is None else int(port)
        self.httpd = None
        self.url = None
        self.owner = False
        self.built = {}                      # project dir → time of the last build seen by this process
        self.lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def ensure(self):
        """Start serving (or reuse a live freecad-mcp viewer on the port) and return the URL."""
        with self.lock:
            if self.owner:
                return self.url
            if self.url and self._ping(self.url):
                return self.url
            self.url = None
            for port in ([self.port, 0] if self.port else [0]):
                try:
                    self._serve(port)
                    return self.url
                except OSError:
                    other = "http://127.0.0.1:%d/" % port
                    if port and self._ping(other):
                        self.url = other            # another freecad-mcp server for this home
                        return self.url
            return None

    def _serve(self, port):
        viewer = self

        class Handler(_Handler):
            pass
        Handler.viewer = viewer
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        httpd.daemon_threads = True
        self.httpd = httpd
        self.owner = True
        self.url = "http://127.0.0.1:%d/" % httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, name="viewer", daemon=True)
        t.start()

    def _ping(self, url):
        try:
            with urllib.request.urlopen(url + "api/ping", timeout=1.0) as r:
                info = json.loads(r.read().decode("utf-8"))
            return info.get("app") == APP and os.path.abspath(info.get("home", "")) == self.home
        except Exception:  # noqa: BLE001
            return False

    def stop(self):
        with self.lock:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
            self.httpd, self.url, self.owner = None, None, False

    # ------------------------------------------------------------ state

    def note_built(self, pdir):
        self.built[os.path.abspath(pdir)] = time.time()

    def link(self, pdir=None):
        url = self.ensure()
        if url and pdir:
            url += "?project=" + urllib.parse.quote(project_key(pdir, self.home), safe="")
        return url

    def projects(self):
        """[(mtime, key, pdir)] of projects with a scene: those in the home, plus any
        this process built elsewhere. Newest first."""
        seen = {}
        try:
            entries = list(os.scandir(self.home))
        except OSError:
            entries = []
        for e in entries:
            if e.is_dir() and not e.name.startswith("."):
                sj, _ = scene_files(e.path)
                try:
                    seen[os.path.abspath(e.path)] = os.path.getmtime(sj)
                except OSError:
                    pass
        for pdir in list(self.built):
            sj, _ = scene_files(pdir)
            try:
                seen[pdir] = os.path.getmtime(sj)
            except OSError:
                pass
        out = [(m, project_key(p, self.home), p) for p, m in seen.items()]
        return sorted(out, reverse=True)

    def project(self, key):
        """(key, project dir) for a key from the page; no key = the newest project."""
        if not key:
            ps = self.projects()
            if not ps:
                raise NotFound("no project has been built yet")
            return ps[0][1], ps[0][2]
        try:
            pdir = self.resolve_project(key)
        except Exception as e:  # noqa: BLE001
            raise NotFound(str(e))
        return project_key(pdir, self.home), pdir

    def scene(self, key):
        if not key and not self.projects():
            return {"project": None, "waiting": True}          # nothing built yet: the page keeps waiting
        key, pdir = self.project(key)
        sj, _ = scene_files(pdir)
        out = {"project": key, "name": os.path.basename(pdir)}
        try:
            with open(sj) as fh:
                out.update(json.load(fh))
            out["mtime"] = os.path.getmtime(sj)
        except (OSError, ValueError):
            out["waiting"] = True            # created, not built yet (or mid-write)
        return out

    def glb(self, key):
        _, pdir = self.project(key)
        _, gp = scene_files(pdir)
        try:
            with open(gp, "rb") as fh:
                return fh.read()
        except OSError:
            raise NotFound("no model yet")


class _Handler(BaseHTTPRequestHandler):
    viewer = None
    server_version = "freecad-mcp-viewer"

    def log_message(self, *args):             # stderr is the MCP server's log; keep it quiet
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False, default=str), "application/json; charset=utf-8")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.split(":")[0]
        return name in LOCAL_HOSTS

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        key = (q.get("project") or [""])[0]
        v = self.viewer
        try:
            if u.path in ("/", "/index.html"):
                with open(PAGE, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if u.path == "/api/ping":
                return self._json(200, {"app": APP, "home": v.home, "pid": os.getpid()})
            if u.path == "/api/projects":
                return self._json(200, {"projects": [{"project": k, "name": os.path.basename(p), "mtime": m}
                                                     for m, k, p in v.projects()]})
            if u.path == "/api/scene":
                return self._json(200, v.scene(key))
            if u.path == "/api/glb":
                return self._send(200, v.glb(key), "model/gltf-binary")
            return self._json(404, {"error": "not found"})
        except NotFound as e:
            return self._json(404, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": str(e)})


# ------------------------------------------------------------ the server's one viewer

_viewer = None
_viewer_lock = threading.Lock()


def enabled():
    return os.environ.get("FREECAD_MCP_VIEWER", "1").strip().lower() not in ("0", "off", "false", "no")


def get(home, resolve_project):
    """The process's Viewer (None when FREECAD_MCP_VIEWER=0). A changed home starts a new one."""
    global _viewer
    if not enabled():
        return None
    with _viewer_lock:
        if _viewer is None or _viewer.home != os.path.abspath(home):
            if _viewer is not None:
                _viewer.stop()
            port = os.environ.get("FREECAD_MCP_VIEWER_PORT", "").strip()
            _viewer = Viewer(home, resolve_project, port=int(port) if port.isdigit() else None)
        return _viewer


if __name__ == "__main__":                    # serve a home on its own, e.g. for working on the page
    import argparse
    ap = argparse.ArgumentParser(description="Serve the live 3D viewer for a projects home.")
    ap.add_argument("--home", default=os.path.expanduser("~/CAD"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()

    def _resolve(key):
        p = os.path.abspath(os.path.join(a.home, os.path.expanduser(key)))
        if not os.path.isdir(p):
            raise NotFound("no project %s" % key)
        return p
    vw = Viewer(a.home, _resolve, port=a.port)
    print("viewer: %s (home %s)" % (vw.ensure(), vw.home), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
