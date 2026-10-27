# SPDX-License-Identifier: Apache-2.0
"""Shared stdio JSON-RPC client and check helpers for the test suites."""
import glob
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server", "freecad_mcp.py")
FC_PY = (sorted(glob.glob("/Applications/FreeCAD*.app/Contents/Resources/bin/python")) or [sys.executable])[-1]


class Client(object):
    """JSON-RPC client that tolerates out-of-order responses."""

    def __init__(self, home, extra_env=None):
        self.n = 0
        env = dict(os.environ, FREECAD_MCP_HOME=home)
        env.update(extra_env or {})
        self.p = subprocess.Popen([FC_PY, "-I", SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=home, env=env)
        self.responses = {}
        self.cv = threading.Condition()
        t = threading.Thread(target=self._reader)
        t.daemon = True
        t.start()

    def _reader(self):
        for line in self.p.stdout:
            msg = json.loads(line.decode())
            with self.cv:
                self.responses[msg.get("id")] = msg
                self.cv.notify_all()

    def send(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        return self.n

    def wait(self, mid, timeout=300):
        end = time.time() + timeout
        with self.cv:
            while mid not in self.responses:
                left = end - time.time()
                if left <= 0:
                    raise RuntimeError("no response to %s" % mid)
                self.cv.wait(left)
            return self.responses.pop(mid)

    def rpc(self, method, params=None, timeout=300):
        return self.wait(self.send(method, params), timeout)

    def notify(self, method, params=None):
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n").encode())
        self.p.stdin.flush()

    @staticmethod
    def unpack(r):
        res = r["result"]
        text = "".join(c.get("text", "") for c in res["content"] if c["type"] == "text")
        return res.get("isError", False), text, res["content"]

    def tool(self, name, args=None, timeout=300):
        return self.unpack(self.rpc("tools/call", {"name": name, "arguments": args or {}}, timeout))

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=20)


fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label +
          (("\n         " + str(detail).replace("\n", "\n         ")) if (detail and not ok) else ""))
    if not ok:
        fails.append(label)


def images(content):
    return [c for c in content if c["type"] == "image"]
