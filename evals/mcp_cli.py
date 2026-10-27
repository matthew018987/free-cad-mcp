#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Call the FreeCAD MCP server's tools from a shell — the eval harness's stand-in
for an MCP client. Every call is appended to <home>/.mcp_calls.jsonl so graders
can check what was done, and returned images are saved as PNG files (paths
printed) so the caller can look at them.

    python3 evals/mcp_cli.py --home <dir> <tool> '<json arguments>'
    python3 evals/mcp_cli.py --home <dir> --list            # tool names + descriptions

Every call starts a fresh server, so a background job could not outlive it:
`wait: false` is dropped and the call runs to completion (a note says so).
"""
import argparse
import base64
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))      # repository layout
sys.path.insert(0, HERE)                              # plugin layout (evals_harness/mcp_client.py)
from mcp_client import Client  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="FREECAD_MCP_HOME for this run (the eval workspace)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="don't record the call (graders, setup)")
    ap.add_argument("tool", nargs="?")
    ap.add_argument("arguments", nargs="?", default="{}")
    a = ap.parse_args()
    home = os.path.abspath(a.home)
    os.makedirs(home, exist_ok=True)
    c = Client(home)
    try:
        init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "eval-cli", "version": "1"}})
        if a.list:
            print(init["result"].get("instructions", ""))
            print()
            for t in c.rpc("tools/list")["result"]["tools"]:
                print("## %s\n%s\nparameters: %s\n" % (t["name"], t["description"],
                                                       ", ".join(sorted(t["inputSchema"].get("properties", {})))))
            return 0
        try:
            args = json.loads(a.arguments)
        except ValueError as e:
            print("Error: arguments must be JSON: %s" % e)
            return 2
        notes = []
        if isinstance(args, dict) and args.get("wait") is False:
            args = dict(args)
            del args["wait"]
            notes.append("note: wait:false was ignored — each mcp_cli call starts a fresh server, so a background "
                         "job would die with it. The call ran to completion; no job_status needed.")
        elif a.tool == "job_status":
            notes.append("note: job_status cannot see jobs from earlier mcp_cli calls (each call is a fresh "
                         "server) — run the original call again without wait:false.")
        t0 = time.time()
        err, text, content = c.tool(a.tool, args, timeout=900)
        shots = []
        img_dir = os.path.join(home, ".mcp_images")
        os.makedirs(img_dir, exist_ok=True)
        for i, block in enumerate(x for x in content if x["type"] == "image"):
            n = len(glob.glob(os.path.join(img_dir, "*.png")))
            p = os.path.join(img_dir, "%03d_%s_%d.png" % (n, a.tool, i))
            with open(p, "wb") as fh:
                fh.write(base64.b64decode(block["data"]))
            shots.append(p)
        if not a.no_log:
            with open(os.path.join(home, ".mcp_calls.jsonl"), "a") as fh:
                fh.write(json.dumps({"t": round(t0, 2), "tool": a.tool, "args": args, "error": err,
                                     "seconds": round(time.time() - t0, 2), "text": text[:4000]}) + "\n")
        print(("ERROR: " if err else "") + text)
        for n in notes:
            print(n)
        for p in shots:
            print("[image saved: %s]" % p)
        return 1 if err else 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
