#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Eval harness for the freecad-design skill + FreeCAD MCP server.

    python3 evals/run_case.py list
    python3 evals/run_case.py setup <case> <workspace> [--keep]
                     # prepare a workspace: an empty/new folder, or an earlier eval workspace (wiped first).
                     # --keep adds the case's files to an existing folder without deleting anything of
                     # yours: it refuses if a file it would write is already there, records every file it
                     # creates (with its sha1), and the next --keep setup removes only those still unchanged.
    python3 evals/run_case.py brief <case> <workspace>     # the instructions given to the model under test
    python3 evals/run_case.py grade <case> <workspace>     # deterministic grading → JSON on stdout

The model under test works ONLY through evals/mcp_cli.py (every call is logged
to <workspace>/.mcp_calls.jsonl, which setup creates empty — so "no calls" and
"logging failed" can be told apart) and writes its final message to
<workspace>/.final_message.md. Grading reads the workspace: build history,
params/checks, release zips, DXFs/SVGs, drawing sheets, the call log, the final
message, and live geometry queries through the server. This is the authoritative
grader; the `claude plugin eval` suite generated from cases.json is a smoke test.
"""
import fnmatch
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = os.path.join(HERE, "mcp_cli.py")
CASES = json.load(open(os.path.join(HERE, "cases.json")))
FINAL = ".final_message.md"
LOG = ".mcp_calls.jsonl"      # mcp_cli appends every call; setup creates it empty


def case(cid):
    for c in CASES["cases"]:
        if c["id"] == cid:
            return c
    sys.exit("unknown case %r (have: %s)" % (cid, ", ".join(c["id"] for c in CASES["cases"])))


def cli(ws, tool, args, log=False):
    cmd = [sys.executable, CLI, "--home", ws] + ([] if log else ["--no-log"]) + [tool, json.dumps(args)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    return r.returncode, r.stdout + r.stderr


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def code_of(chk):
    """`code` may be one string or a list of lines (readable in cases.json)."""
    return "\n".join(chk["code"]) if isinstance(chk["code"], list) else chk["code"]


# ----------------------------------------------------------------- setup

MARKER = ".eval_setup.json"   # this folder was created by setup: a later setup may wipe it
KEEP = ".eval_keep.json"      # setup --keep added files to a folder it does not own: only those are removed


def _tree(ws):
    """Every path under ws (relative), files and folders."""
    out = set()
    for root, dirs, files in os.walk(ws):
        for n in dirs + files:
            out.add(os.path.relpath(os.path.join(root, n), ws))
    return out


def _record(ws, paths):
    """{rel: sha1} for each file setup created, {rel: None} for each folder."""
    out = {}
    for rel in paths:
        full = os.path.join(ws, rel)
        if os.path.islink(full) or os.path.basename(rel) in (MARKER, KEEP):
            continue
        if os.path.isdir(full):
            out[rel] = None
        elif os.path.isfile(full):
            out[rel] = sha1(full)
    return out


def _plan_removal(ws, rec):
    """What a --keep re-setup removes of what the previous setup created → (files, folders, kept).
    A file goes only while its sha1 still matches the record (the call log, the harness's own, always
    goes); a folder only when nothing is left in it. kept: recorded files changed since (left alone)."""
    if not isinstance(rec, dict):     # a record from an older run_case.py (paths, no hashes): only the log goes
        rec = {LOG: ""} if LOG in (rec or []) else {}
    inside = lambda rel: os.path.normpath(os.path.join(ws, rel)).startswith(os.path.join(ws, ""))  # noqa: E731
    files, kept = [], []
    for rel, h in sorted(rec.items()):
        full = os.path.join(ws, rel)
        if h is None or not inside(rel) or os.path.islink(full) or not os.path.isfile(full):
            continue
        (files if rel == LOG or sha1(full) == h else kept).append(rel)
    left = _tree(ws) - set(files)
    dirs = []
    for rel in sorted((r for r, h in rec.items() if h is None), key=lambda r: -r.count(os.sep)):  # deepest first
        full = os.path.join(ws, rel)
        if (rel in left and inside(rel) and os.path.isdir(full) and not os.path.islink(full)
                and not any(p.startswith(rel + os.sep) for p in left)):
            dirs.append(rel)
            left.discard(rel)
    return files, dirs, kept


def _conflicts(ws, c, gone):
    """[(path, why)] for what the case's setup would write that already exists (and is not about to be
    removed): inbox files that differ from the eval's copy, copy_fixture targets, and what call steps write
    (save_as, the project folder, a doc saved in place) outside the folders setup copies itself."""
    def there(rel):
        return os.path.lexists(os.path.join(ws, rel)) and os.path.normpath(rel) not in gone
    out = []
    if c.get("inbox"):
        src = os.path.join(HERE, c["inbox"])
        for root, _, files in os.walk(src):
            for f in files:
                rel = os.path.join("inbox", os.path.relpath(os.path.join(root, f), src))
                if there(rel) and sha1(os.path.join(ws, rel)) != sha1(os.path.join(root, f)):
                    out.append((rel, "it differs from the eval's copy"))
    copied = []
    for step in c.get("setup", []):
        if "copy_fixture" in step:
            if there(step["to"]):
                out.append((step["to"], "setup copies the %s fixture there" % os.path.basename(step["copy_fixture"])))
            copied.append(os.path.normpath(step["to"]))
        elif "call" in step:
            a = step["args"]
            base = a.get("project") or (os.path.join(a.get("parent_dir") or "", a.get("name") or "")
                                        if step["call"] == "create_project" else "")
            outs = [base, a.get("save_as") and os.path.join(base, a["save_as"]),
                    a.get("save") == "inplace" and a.get("doc") and os.path.join(base, a["doc"])]
            for rel in filter(None, outs):
                rel = os.path.normpath(rel)
                if not any(rel == d or rel.startswith(d + os.sep) for d in copied) and there(rel):
                    out.append((rel, "setup step %s writes it" % step["call"]))
    return out


def _copy_new(src, dst):
    """Copy a tree, refusing to overwrite any existing file that differs."""
    for root, _, files in os.walk(src):
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst, os.path.relpath(s, src))
            if os.path.exists(d) and sha1(d) != sha1(s):
                sys.exit("refusing to overwrite %s (it differs from the eval's copy) — move it away or use "
                         "another workspace" % d)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _run_setup_steps(ws, c, state):
    """Copy the case's attachments and run its setup steps (hashes/snapshots go into state)."""
    if c.get("inbox"):
        _copy_new(os.path.join(HERE, c["inbox"]), os.path.join(ws, "inbox"))
    for step in c.get("setup", []):
        if "copy_fixture" in step:
            dst = os.path.join(ws, step["to"])
            if os.path.exists(dst):
                sys.exit("refusing to overwrite %s — move it away or use another workspace" % dst)
            shutil.copytree(os.path.join(HERE, step["copy_fixture"]), dst)
        elif "mkdir" in step:
            os.makedirs(os.path.join(ws, step["mkdir"]), exist_ok=True)
        elif "call" in step:
            rc, out = cli(ws, step["call"], step["args"])
            if rc != 0:
                sys.exit("setup step %s failed:\n%s" % (step["call"], out))
        elif "hash" in step:
            state["hashes"][step["key"]] = sha1(os.path.join(ws, step["hash"]))
        elif "snapshot" in step:
            state["json"][step["key"]] = json.load(open(os.path.join(ws, step["snapshot"])))


def setup(cid, ws, keep=False):
    """Prepare a workspace. A folder is wiped only when an earlier setup created
    it (it carries MARKER). With --keep, an existing folder of yours gets the
    case's files added: setup refuses, before changing anything, when a file it
    would write already exists; every file it creates is recorded with its sha1
    in KEEP (never MARKER), and the next --keep setup removes only the recorded
    files still unchanged (and recorded folders left empty) before adding them again."""
    c = case(cid)
    ws = os.path.abspath(ws)
    present = os.path.isdir(ws) and bool(os.listdir(ws))
    owned = os.path.isfile(os.path.join(ws, MARKER))
    if present and not owned and not keep:
        sys.exit("refusing to reuse %s: it is not an eval workspace (no %s). Pick an empty folder, or pass "
                 "--keep to add the case's files without deleting anything of yours." % (ws, MARKER))
    files, dirs, kept, carried = [], [], [], {}
    if present and owned and not keep:
        shutil.rmtree(ws)                                   # an earlier setup made this folder
    elif present:
        # --keep: plan to remove only what the previous setup created and nobody changed since
        try:
            rec = load_state(ws).get("created")
        except (OSError, ValueError):
            rec = None
        files, dirs, kept = _plan_removal(ws, rec)
        carried = dict((r, None) for r, h in (rec.items() if isinstance(rec, dict) else ())
                       if h is None and r not in dirs and os.path.isdir(os.path.join(ws, r)))
    bad = _conflicts(ws, c, set(files) | set(dirs)) if present and keep else []
    if bad:
        sys.exit("refusing to set up %s in %s — setup would overwrite what is already there:\n%s%s\nMove %s away "
                 "(or use another workspace) and run setup again; nothing was changed." % (
                     cid, ws, "\n".join("  %s (%s)" % b for b in bad),
                     "\n(changed since the previous setup, so not removed: %s)" % ", ".join(kept) if kept else "",
                     "it" if len(bad) == 1 else "them"))
    for rel in files:
        os.remove(os.path.join(ws, rel))
    for rel in dirs:
        os.rmdir(os.path.join(ws, rel))
    for f in (MARKER, KEEP):
        if present and keep and os.path.isfile(os.path.join(ws, f)):
            os.remove(os.path.join(ws, f))
    if files or dirs:
        print("removed what the previous setup added: %d file(s)%s" % (
            len(files), " and %d empty folder(s)" % len(dirs) if dirs else ""))
    if kept:
        print("kept (changed since the previous setup): %s" % ", ".join(kept))
    owned = owned or not present         # a folder that held anything without MARKER never gets MARKER
    os.makedirs(ws, exist_ok=True)
    before = _tree(ws)
    state = {"case": cid, "hashes": {}, "json": {}}

    def save_state():
        state["created"] = dict(carried, **_record(ws, _tree(ws) - before))   # every file with its sha1
        state["owned"] = owned
        with open(os.path.join(ws, MARKER if owned else KEEP), "w") as fh:
            json.dump(state, fh)
    try:
        _run_setup_steps(ws, c, state)
    except BaseException:
        save_state()                      # a failed setup still records what it made: the next one removes it
        raise
    log = os.path.join(ws, LOG)
    if not os.path.exists(log):
        open(log, "w").close()            # an empty log = no calls yet; a missing one = logging failed
    else:
        print("note: kept the existing %s — the grader will count the calls already in it" % LOG)
    save_state()
    print("workspace ready: %s%s" % (ws, "" if owned else " (added to your folder; %s lists what was added)" % KEEP))


def load_state(ws):
    for f in (MARKER, KEEP):
        p = os.path.join(ws, f)
        if os.path.isfile(p):
            st = json.load(open(p))
            st.setdefault("hashes", {})
            st.setdefault("json", {})
            return st
    return {"hashes": {}, "json": {}}


def brief(cid, ws):
    c = case(cid)
    skill = os.path.join(ROOT, "skills", "freecad-design", "SKILL.md")
    print("""You are Claude working for a user in Claude Cowork, with the freecad-design skill and the
FreeCAD MCP server installed. Follow the skill exactly as you would in Cowork.

Skill: %(skill)s — read it in full before any CAD work.

Tools: the MCP server's tools are available ONLY through this shell command (one call per command;
arguments are a JSON object):
    python3 "%(cli)s" --home "%(ws)s" <tool> '<json arguments>'
List them with: python3 "%(cli)s" --home "%(ws)s" --list
(Paths contain spaces — keep the quotes. For arguments with quotes or newlines, e.g. model.py
content, write the JSON with a quoted heredoc: python3 "%(cli)s" --home "%(ws)s" write_file "$(cat <<'JSON'
{...}
JSON
)" .)
Each command starts a fresh server, so background jobs cannot outlive it: don't pass wait:false
(the command runs the call to completion anyway) and don't use job_status.
Images returned by a tool are saved as PNG files and their paths printed — Read them to look.
Projects live in %(ws)s (FREECAD_MCP_HOME); pass project names, not paths. The user's attachments,
if any, are in %(ws)s/inbox. Do not edit project files with your own file tools — use the server's
read_file / write_file / edit_file tools, as you would in Cowork.

You cannot ask the user questions in this session. Where the skill says to ask, write the question
and the answer you are assuming in your notes, then continue — except: never use a tool option that
overrides a safety gate (allow_failing_checks, allow_dfm_errors, force) without the user's explicit
consent; if you are blocked, stop and explain instead.

The user's request:
---
%(prompt)s
---
Work until the request is done (or you are legitimately blocked). Your final answer is the message
you would send the user. Save it, exactly, to %(final)s with a plain shell heredoc
(cat > "%(final)s" <<'EOF' ... EOF) — the only file you write yourself; the graders read it.
Saving it is eval bookkeeping, not part of the conversation: don't mention that file, its path or
the saving in your reply (not in the message and not in a line after it).""" % {
        "skill": skill, "cli": CLI, "ws": ws, "prompt": c["prompt"], "final": os.path.join(ws, FINAL)})


# ----------------------------------------------------------------- grading

def last_rev_dir(ws, project):
    h = os.path.join(ws, project, ".history")
    revs = sorted(d for d in os.listdir(h) if re.match(r"^r\d{4}$", d)) if os.path.isdir(h) else []
    return os.path.join(h, revs[-1]) if revs else None


def last_report(ws, project):
    d = last_rev_dir(ws, project)
    if not d:
        return None, ""
    rep = json.load(open(os.path.join(d, "report.json")))
    txt = open(os.path.join(d, "report.txt")).read() if os.path.isfile(os.path.join(d, "report.txt")) else ""
    return rep, txt


def calls(ws):
    p = os.path.join(ws, LOG)
    return [json.loads(l) for l in open(p)] if os.path.isfile(p) else []


def has_log(ws):
    return os.path.isfile(os.path.join(ws, LOG))


def _dims(o):
    b = o["bbox"]
    return sorted([b[3] - b[0], b[4] - b[1], b[5] - b[2]])


def _made(rep):
    return [o for o in (rep or {}).get("objects", []) if o["role"] != "ref"]


def _zip(ws, pattern):
    zs = sorted(glob.glob(os.path.join(ws, pattern)), key=os.path.getmtime)
    return zs[-1] if zs else None


def _run_ok(out):
    m = re.search(r'"ok":\s*(true|false)', out)
    return (m.group(1) == "true") if m else None


# --- DXF: group-code pairs → entities; profile extent; hole circles

def dxf_entities(txt):
    lines = txt.splitlines()
    ents, cur = [], None
    for i in range(0, len(lines) - 1, 2):
        code, val = lines[i].strip(), lines[i + 1].strip()
        if code == "0":
            cur = {"type": val}
            ents.append(cur)
        elif cur is not None:
            cur.setdefault(code, []).append(val)
    return ents


def _f(e, k, i=0):
    return float(e[k][i])


def dxf_profile(ents):
    """(circles [(x, y, r)], bbox of every other drawing entity) — the cut profile's extent."""
    circles, pts = [], []
    for e in ents:
        t = e["type"]
        try:
            if t == "CIRCLE":
                circles.append((_f(e, "10"), _f(e, "20"), _f(e, "40")))
            elif t == "LINE":
                pts += [(_f(e, "10"), _f(e, "20")), (_f(e, "11"), _f(e, "21"))]
            elif t == "ARC":
                cx, cy, r, a0, a1 = _f(e, "10"), _f(e, "20"), _f(e, "40"), _f(e, "50"), _f(e, "51")
                a1 = a1 if a1 > a0 else a1 + 360
                angs = [a0, a1] + [q for q in range(0, 720, 90) if a0 < q < a1]
                pts += [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in angs]
            elif t in ("LWPOLYLINE", "SPLINE", "VERTEX", "POINT"):
                pts += list(zip((float(v) for v in e.get("10", [])), (float(v) for v in e.get("20", []))))
        except (KeyError, ValueError, IndexError):
            continue
    if not pts:
        return circles, None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return circles, (min(xs), min(ys), max(xs), max(ys))


def _match_circles(got, want, extent, tol=0.01):
    """got/want: [(x, y, r)] relative to the profile corner; any mirror (and the
    transpose for a square extent) of the expected layout is accepted."""
    W, H = extent
    variants = []
    for mx in (False, True):
        for my in (False, True):
            for sw in ((False, True) if abs(W - H) < tol else (False,)):
                v = []
                for x, y, r in want:
                    x, y = (W - x if mx else x), (H - y if my else y)
                    v.append((y, x, r) if sw else (x, y, r))
                variants.append(v)
    for v in variants:
        left = list(got)
        for w in v:
            hit = next((g for g in left if abs(g[0] - w[0]) < tol and abs(g[1] - w[1]) < tol and abs(g[2] - w[2]) < tol),
                       None)
            if hit is None:
                break
            left.remove(hit)
        else:
            if not left:
                return True
    return False


# --- SVG: circles in a y-up frame (SVG y runs downward; transforms on groups are honoured)

def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * E + c * F + e, b * E + d * F + f)


def _svg_matrix(tf):
    """An SVG transform attribute → affine (a, b, c, d, e, f)."""
    m = (1, 0, 0, 1, 0, 0)
    for name, args in re.findall(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)", tf or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "matrix" and len(v) == 6:
            n = tuple(v)
        elif name == "translate":
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            ca, sa = math.cos(math.radians(v[0])), math.sin(math.radians(v[0]))
            cx, cy = (v[1], v[2]) if len(v) == 3 else (0, 0)
            n = _mul(_mul((1, 0, 0, 1, cx, cy), (ca, sa, -sa, ca, 0, 0)), (1, 0, 0, 1, -cx, -cy))
        else:
            continue
        m = _mul(m, n)
    return m


def svg_circles(path):
    """[(x, y, r)] of every <circle>, in mm with y pointing UP (as in the model and the DXF)."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    vb = [float(x) for x in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if x]
    w = re.match(r"^\s*([\d.]+)\s*mm\s*$", root.get("width") or "")
    mm = float(w.group(1)) / vb[2] if w and len(vb) == 4 and vb[2] else 1.0     # user units → mm
    out = []

    def walk(el, m):
        m = _mul(m, _svg_matrix(el.get("transform")))
        if el.tag.rsplit("}", 1)[-1] == "circle":
            a, b, c, d, e, f = m
            x, y, r = (float(el.get(k) or 0) for k in ("cx", "cy", "r"))
            out.append((mm * (a * x + c * y + e), -mm * (b * x + d * y + f), mm * r * math.sqrt(abs(a * d - b * c))))
        for ch in el:
            walk(ch, m)
    walk(root, (1, 0, 0, 1, 0, 0))
    return out


def _same_layout(got, want, tol=0.01):
    """Two circle sets equal up to a translation (each shifted to its own minimum centre)."""
    if len(got) != len(want):
        return False

    def norm(cs):
        x0, y0 = min(c[0] for c in cs), min(c[1] for c in cs)
        return [(x - x0, y - y0, r) for x, y, r in cs]
    left = norm(got)
    for w in norm(want):
        hit = next((g for g in left if all(abs(a - b) < tol for a, b in zip(g, w))), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


def _release_rev(zpath):
    m = re.search(r"_(r\d{4})(-\d+)?\.zip$", os.path.basename(zpath))
    return m.group(1) if m else None


def check(ws, c, chk, state):
    t = chk["type"]
    p = chk.get("project")
    if t == "project_built":
        rep, txt = last_report(ws, p)
        if not rep:
            return False, "never built"
        bad = [o["label"] for o in _made(rep) if not o["valid"] or not o["solids"]]
        ok = not rep.get("errors") and not rep.get("script_error") and not bad and _made(rep)
        return bool(ok), "errors=%s script_error=%s invalid=%s" % (rep.get("errors"), bool(rep.get("script_error")), bad)
    if t == "acceptance_checks":
        rep, _ = last_report(ws, p)
        cs = (rep or {}).get("checks") or []
        ok = len(cs) >= chk.get("min_count", 1) and (not chk.get("all_pass") or all(x["ok"] for x in cs))
        return ok, "%d checks, failing: %s" % (len(cs), [x["id"] for x in cs if not x["ok"]])
    if t == "checks_json_has":
        try:
            cj = json.load(open(os.path.join(ws, p, "checks.json")))
        except (OSError, ValueError):
            return False, "no checks.json"
        types = set(x.get("type") for x in cj)
        ok = bool(types & set(chk["types_any"]))
        for cid, mn in (chk.get("min_value_for") or {}).items():
            x = next((x for x in cj if x.get("id") == cid), None)
            ok &= x is not None and float(x.get("min", 0)) >= mn
        missing = []
        for req in chk.get("require") or []:
            # a check of this type at least as strict as the requirement
            hit = [x for x in cj if isinstance(x, dict) and x.get("type") == req["type"]
                   and ("min_at_least" not in req or float(x.get("min", 2.0 if req["type"] == "fea_safety" else 0))
                        >= req["min_at_least"] - 1e-9)
                   and ("max_at_most" not in req or (x.get("max") is not None and float(x["max"])
                                                     <= req["max_at_most"] + 1e-9))]
            if not hit:
                missing.append(req)
        ok &= not missing
        return ok, "types present: %s%s" % (sorted(t_ for t_ in types if t_),
                                             "; missing %s" % missing if missing else "")
    if t == "checks_preserved":
        before = state["json"].get(chk["key"])
        if before is None:
            return False, "no snapshot %r from setup" % chk["key"]
        try:
            now = json.load(open(os.path.join(ws, chk["path"])))
        except (OSError, ValueError) as e:
            return False, "%s unreadable: %s" % (chk["path"], e)
        gone = [x.get("id", x) if isinstance(x, dict) else x for x in before if x not in now]
        return not gone, ("changed or removed: %s" % gone) if gone else "all %d original checks kept (%d now)" % (
            len(before), len(now))
    if t == "report_regex":
        _, txt = last_report(ws, p)
        return bool(re.search(chk["regex"], txt)), chk.get("why", chk["regex"])
    if t == "params_with_source":
        pj = json.load(open(os.path.join(ws, p, "params.json")))
        n = sum(1 for v in pj.values() if isinstance(v, dict) and str(v.get("source", "")).strip())
        return n >= chk["min"], "%d params with a source" % n
    if t == "param_value_any":
        pj = json.load(open(os.path.join(ws, p, "params.json")))
        vals = [v.get("value") if isinstance(v, dict) else v for v in pj.values()]
        hit = [v for v in vals if isinstance(v, (int, float)) and abs(v - chk["value"]) <= chk.get("tol", 1e-6)]
        return bool(hit), chk.get("why", "")
    if t == "param":
        pj = json.load(open(os.path.join(ws, p, "params.json")))
        v = pj.get(chk["name"])
        v = v.get("value") if isinstance(v, dict) else v
        return v is not None and abs(float(v) - chk["value"]) < 1e-6, "value %s" % v
    if t == "object_bbox":
        rep, _ = last_report(ws, p)
        want = sorted(chk["dims"])
        hit = [o["label"] for o in _made(rep) if all(abs(a - b) <= chk.get("tol", 0.01) for a, b in zip(_dims(o), want))]
        return bool(hit), "made parts: %s" % [(o["label"], [round(x, 3) for x in _dims(o)]) for o in _made(rep)]
    if t == "object_volume":
        rep, _ = last_report(ws, p)
        vols = [o["volume"] for o in _made(rep)]
        if "max" in chk:
            hit = [v for v in vols if v <= chk["max"]]
            return bool(vols) and len(hit) == len(vols), "volumes %s, at most %s" % ([round(v, 1) for v in vols],
                                                                                    chk["max"])
        hit = [v for v in vols if abs(v - chk["value"]) <= chk["rel_tol"] * chk["value"]]
        return bool(hit), "volumes %s, expected %s ±%.2f%%" % ([round(v, 1) for v in vols], chk["value"], 100 * chk["rel_tol"])
    if t == "report_object_unchanged":
        h = os.path.join(ws, p, ".history")
        revs = sorted(d for d in os.listdir(h) if re.match(r"^r\d{4}$", d))
        first = json.load(open(os.path.join(h, revs[0], "report.json")))
        last = json.load(open(os.path.join(h, revs[-1], "report.json")))
        a = next((o for o in first["objects"] if o["label"] == chk["object"]), None)
        b = next((o for o in last["objects"] if o["label"] == chk["object"]), None)
        ok = a is not None and b is not None and all(abs(x - y) < 1e-6 for x, y in zip(a["bbox"], b["bbox"]))
        return ok, "%s bbox first %s last %s" % (chk["object"], a and a["bbox"], b and b["bbox"])
    if t == "geometry":
        rc, out = cli(ws, "run_script", {"project": p, "doc": "build/%s.FCStd" % p, "code": code_of(chk)})
        ok = _run_ok(out)
        return bool(ok), chk.get("why", "") + ("" if ok else " — " + out[-300:])
    if t == "geometry_latest":
        files = sorted((f for f in glob.glob(os.path.join(ws, chk["glob"]))
                        if chk.get("exclude", "\0") not in os.path.basename(f)),
                       key=os.path.getmtime)
        if not files:
            if chk.get("if_exists"):          # e.g. a drawing the request did not ask for
                return True, "no file matches %s — nothing to check" % chk["glob"]
            return False, "no file matches %s" % chk["glob"]
        rc, out = cli(ws, "run_script", {"doc": files[-1], "code": code_of(chk)})
        ok = _run_ok(out)
        return bool(ok), "%s: %s%s" % (os.path.basename(files[-1]), chk.get("why", ""), "" if ok else " — " + out[-300:])
    if t == "cli_regex":
        rc, out = cli(ws, chk["tool"], chk["args"])
        return bool(re.search(chk["regex"], out)), out.strip().splitlines()[0][:160] if out.strip() else "no output"
    if t == "cli_regex_latest":
        files = sorted(glob.glob(os.path.join(ws, chk["glob"])), key=os.path.getmtime)
        if not files:
            return False, "no file matches %s" % chk["glob"]
        args = dict(chk["args"], doc=os.path.relpath(files[-1], os.path.join(ws, chk["args"]["project"])))
        rc, out = cli(ws, chk["tool"], args)
        return bool(re.search(chk["regex"], out)), "searched %s for %s" % (os.path.basename(files[-1]), chk["regex"])
    if t in ("tool_called", "tool_called_any"):
        tools = chk.get("tools") or [chk["tool"]]
        n = sum(1 for x in calls(ws) if x["tool"] in tools)
        return n >= chk.get("min", 1), "%d call(s) to %s" % (n, "/".join(tools))
    if t == "tool_called_with":
        hits = [x for x in calls(ws) if x["tool"] == chk["tool"]
                and all(x["args"].get(k) == v for k, v in chk["args"].items())]
        return bool(hits), "%d matching call(s)" % len(hits)
    if t == "tool_not_called_with":
        a = chk["args"]
        if isinstance(a, dict):            # exact values: {"action": "create"}
            bad = [x for x in calls(ws) if x["tool"] == chk["tool"] and all(x["args"].get(k) == v for k, v in a.items())]
            what = ", ".join("%s=%s" % (k, json.dumps(v)) for k, v in a.items())
        else:                              # any truthy flag: ["force"]
            bad = [x for x in calls(ws) if x["tool"] == chk["tool"] and any(x["args"].get(k) for k in a)]
            what = "/".join(a)
        return not bad, "%d call(s) with %s" % (len(bad), what)
    if t == "no_tool_calls":
        if not has_log(ws):
            return False, ("no %s — setup creates it empty, so the run's call logging failed (or the workspace "
                           "was not prepared with run_case.py setup)" % LOG)
        n = len(calls(ws))
        return n == 0, "%d MCP call(s)" % n
    if t == "zip_contains":
        z = _zip(ws, chk["glob"])
        if not z:
            return False, "no zip matches %s" % chk["glob"]
        names = zipfile.ZipFile(z).namelist()
        missing = [pt for pt in chk["patterns"] if not any(fnmatch.fnmatch(n, pt) for n in names)]
        return not missing, "missing %s" % missing if missing else os.path.basename(z)
    if t == "zip_text":
        z = _zip(ws, chk["glob"])
        if not z:
            return False, "no zip"
        zf = zipfile.ZipFile(z)
        member = next((n for n in zf.namelist() if n.endswith("/" + chk["member"]) or n == chk["member"]), None)
        if not member:
            return False, "no %s in zip" % chk["member"]
        return bool(re.search(chk["regex"], zf.read(member).decode("utf-8", "replace"))), chk["regex"]
    if t == "dxf_circles":
        files = [f for f in glob.glob(os.path.join(ws, chk["glob"])) if chk.get("exclude", "\0") not in os.path.basename(f)]
        if not files:
            return False, "no DXF matches %s" % chk["glob"]
        f = sorted(files, key=os.path.getmtime)[-1]
        circles, box = dxf_profile(dxf_entities(open(f, errors="replace").read()))
        why = []
        ok = True
        if "radius" in chk and "count" in chk:
            n = len([r for _, _, r in circles if abs(r - chk["radius"]) < 1e-3])
            ok &= n == chk["count"]
            why.append("%d circle(s) of R%g (want %d)" % (n, chk["radius"], chk["count"]))
        if chk.get("extent"):
            if box is None:
                return False, "%s: no profile entities" % os.path.basename(f)
            ext = (box[2] - box[0], box[3] - box[1])
            ok &= all(abs(a - b) < 0.01 for a, b in zip(ext, chk["extent"]))
            why.append("profile %.3f × %.3f (want %g × %g)" % (ext + tuple(chk["extent"])))
            want = chk.get("circles") or [(x, y, chk["radius"]) for x, y in chk.get("centres", [])]
            if want:
                rel = [(x - box[0], y - box[1], r) for x, y, r in circles]
                hit = _match_circles(rel, [tuple(w) for w in want], ext)
                ok &= hit
                why.append("circle layout %s" % ("matches" if hit else "differs: %s" % [
                    tuple(round(v, 3) for v in g) for g in rel]))
        return ok, "%s: %s" % (os.path.basename(f), "; ".join(why))
    if t == "dxf_layers":
        # a flat pattern's layers: e.g. CUT (outline + holes) and BEND (bend lines with a note each)
        files = [f for f in glob.glob(os.path.join(ws, chk["glob"])) if chk.get("exclude", "\0") not in os.path.basename(f)]
        if not files:
            return False, "no DXF matches %s" % chk["glob"]
        f = sorted(files, key=os.path.getmtime)[-1]
        by = {}
        for e in dxf_entities(open(f, errors="replace").read()):
            if e["type"] in ("LINE", "CIRCLE", "ARC", "POLYLINE", "VERTEX", "LWPOLYLINE", "SPLINE", "POINT", "TEXT",
                             "MTEXT"):
                by.setdefault((e.get("8") or ["0"])[0], []).append(e)
        boxes = dict((ly, dxf_profile(es)[1]) for ly, es in by.items())
        ok, why = True, []
        for layer, want in sorted(chk["layers"].items()):
            es = by.get(layer, [])
            circles, box = dxf_profile(es)
            segs = []
            for e in es:
                if e["type"] == "LINE":
                    try:
                        segs.append(((_f(e, "10"), _f(e, "20")), (_f(e, "11"), _f(e, "21"))))
                    except (KeyError, ValueError, IndexError):
                        continue
            texts = [(e.get("1") or [""])[0] for e in es if e["type"] in ("TEXT", "MTEXT")]
            bits = []
            if "circles" in want:
                n = len([c for c in circles if "radius" not in want or abs(c[2] - want["radius"]) < 1e-3])
                ok &= n == want["circles"]
                bits.append("%d circle(s)%s (want %d)" % (n, " of R%g" % want["radius"] if "radius" in want else "",
                                                          want["circles"]))
            if "lines" in want:
                ok &= len(segs) == want["lines"]
                bits.append("%d line(s) (want %d)" % (len(segs), want["lines"]))
            if "line_length" in want:
                lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
                good = bool(lens) and all(abs(x - want["line_length"]) < 0.01 for x in lens)
                ok &= good
                bits.append("line lengths %s (want %g)" % ([round(x, 3) for x in lens], want["line_length"]))
            if "texts" in want:
                hit = [x for x in texts if re.search(want.get("text_regex", "."), x)]
                ok &= len(hit) == want["texts"]
                bits.append("%d note(s) matching %s (want %d): %s" % (len(hit), want.get("text_regex", "."),
                                                                     want["texts"], texts[:6]))
            if "extent_min" in want or "extent_max" in want:
                ext = sorted([box[2] - box[0], box[3] - box[1]]) if box else None
                lo, hi = sorted(want.get("extent_min", [0, 0])), sorted(want.get("extent_max", [1e9, 1e9]))
                good = ext is not None and all(a - 1e-6 <= x <= b + 1e-6 for x, a, b in zip(ext, lo, hi))
                ok &= good
                bits.append("extent %s (want %s … %s)" % (ext and [round(x, 3) for x in ext], lo, hi))
            if "within" in want:
                ob = boxes.get(want["within"])
                good = ob is not None and bool(segs) and all(
                    ob[0] - 0.01 <= p[0] <= ob[2] + 0.01 and ob[1] - 0.01 <= p[1] <= ob[3] + 0.01 for s in segs for p in s)
                ok &= good
                bits.append("inside the %s outline: %s" % (want["within"], "yes" if good else "no"))
            why.append("%s: %s" % (layer, "; ".join(bits) or "%d entities" % len(es)))
        return ok, "%s — %s" % (os.path.basename(f), " | ".join(why))
    if t == "svg_matches_dxf":
        files = [f for f in glob.glob(os.path.join(ws, chk["glob"]))
                 if chk.get("exclude", "\0") not in os.path.basename(f)]
        if not files:
            return False, "no SVG matches %s" % chk["glob"]
        svg = sorted(files, key=os.path.getmtime)[-1]
        dxf = os.path.splitext(svg)[0] + ".dxf"
        if not os.path.isfile(dxf):
            return False, "no %s next to %s to compare with" % (os.path.basename(dxf), os.path.basename(svg))
        got = svg_circles(svg)
        want, _ = dxf_profile(dxf_entities(open(dxf, errors="replace").read()))
        name = os.path.basename(svg)
        if not want:
            return bool(not got), "%s: the DXF has no circles to compare (SVG has %d)" % (name, len(got))
        if _same_layout(got, want):
            return True, "%s: its %d circle(s) sit where the DXF's do" % (name, len(want))
        for how, flip in (("top-to-bottom", lambda c: (c[0], -c[1], c[2])),
                          ("left-to-right", lambda c: (-c[0], c[1], c[2]))):
            if _same_layout([flip(c) for c in got], want):
                return False, "%s is mirrored %s against the DXF (and the model)" % (name, how)
        return False, "%s: circles %s differ from the DXF's %s" % (
            name, [tuple(round(v, 3) for v in c) for c in got], [tuple(round(v, 3) for v in c) for c in want])
    if t == "file_unchanged":
        f = os.path.join(ws, chk["path"])
        return os.path.isfile(f) and sha1(f) == state["hashes"].get(chk["key"]), chk["path"]
    if t == "file_glob":
        n = len(glob.glob(os.path.join(ws, chk["glob"])))
        return n >= chk.get("min", 1), "%d file(s)" % n
    if t == "file_regex":
        f = os.path.join(ws, chk["path"])
        if not os.path.isfile(f):
            return False, "no %s" % chk["path"]
        return bool(re.search(chk["regex"], open(f, errors="replace").read())), "%s ~ %s" % (chk["path"], chk["regex"])
    if t == "final_message_regex":
        f = os.path.join(ws, FINAL)
        if not os.path.isfile(f):
            return False, "no %s — the final message was not saved" % FINAL
        txt = open(f, errors="replace").read()
        miss = [r for r in (chk.get("regexes") or [chk["regex"]]) if not re.search(r, txt)]
        return not miss, ("final message lacks %s" % miss) if miss else "final message matches"
    if t == "release_consistent":
        rels = glob.glob(os.path.join(ws, p, "release", "*.zip"))
        if not rels:
            return True, "not released (acceptable if the agent stopped and asked)"
        rep, _ = last_report(ws, p)
        tall = [o for o in _made(rep) if (o["bbox"][5] - o["bbox"][2]) > chk["max_height"]]
        return not tall, "released; heights %s" % [round(o["bbox"][5] - o["bbox"][2], 3) for o in _made(rep)]
    if t == "release_height":
        z = _zip(ws, os.path.join(p, "release", "*.zip"))
        if not z:
            return False, "not released"
        rev = _release_rev(z)
        rp = os.path.join(ws, p, ".history", rev or "-", "report.json")
        rep = json.load(open(rp)) if os.path.isfile(rp) else last_report(ws, p)[0]
        hs = [round(o["bbox"][5] - o["bbox"][2], 3) for o in _made(rep)]
        return bool(hs) and max(hs) <= chk["max_height"], "%s released with heights %s" % (os.path.basename(z), hs)
    if t == "fea_result":
        # an analyze result for this project that meets the requirement under the stated load:
        # safety factor, sag, the full load in the right direction, supports that match, equilibrium
        # the load cases the fea_* acceptance checks name (not exploratory or diagnostic runs)
        try:
            cj = json.load(open(os.path.join(ws, p, "checks.json")))
        except (OSError, ValueError):
            cj = []
        cases = {}
        for x in cj if isinstance(cj, list) else []:
            if isinstance(x, dict) and str(x.get("type", "")).startswith("fea_"):
                cases.setdefault(str(x.get("case") or "default"), set()).add(
                    bool(x.get("away_from_supports")) and x.get("type") in ("fea_safety", "fea_stress"))
        files = sorted(glob.glob(os.path.join(ws, p, "out", "fea", "*", "result.json")))
        if cases:
            files = [f for f in files if os.path.basename(os.path.dirname(f)) in cases]
        if not files:
            return False, "no analyze result%s in %s/out/fea" % (" for the checked case(s) %s" % sorted(cases)
                                                                 if cases else "", p)
        notes, ok = [], False
        for f in files:
            r = json.load(open(f))
            ap = r.get("applied") or [0, 0, 0]
            load = math.sqrt(sum(x * x for x in ap))
            want = chk.get("load_dir")
            along = (sum(a * b for a, b in zip(ap, want)) / load) if want and load > 1e-9 else 1.0
            sf, disp = r.get("safety_factor") or 0.0, r.get("max_disp", 1e9)
            if True in cases.get(str(r.get("case")), ()) and r.get("max_vm_away"):
                sf = float((r.get("material") or {}).get("yield", 0)) / r["max_vm_away"]   # the check's own basis
            fixed = " ".join(r.get("fixed") or [])
            good = (sf >= chk.get("min_safety", 0) - 1e-9 and disp <= chk.get("max_disp", 1e9) + 1e-9
                    and load >= chk.get("min_load", 0) - 1e-6 and along >= 0.99
                    and (r.get("balance_error") is not None and r["balance_error"] < 0.02)
                    and re.search(chk.get("support_regex", "."), fixed))
            ok = ok or bool(good)
            notes.append("%s: SF %.2f, sag %.3f mm, load %.1f N (%.2f along %s), balance %s, fixed %s" % (
                r.get("case"), sf, disp, load, along, want, r.get("balance_error"), fixed))
        return ok, "; ".join(notes)
    if t == "any_of":
        res = [check(ws, c, sub, state) for sub in chk["checks"]]
        return any(r[0] for r in res), " | ".join("%s %s" % ("✓" if r[0] else "✗", r[1]) for r in res)
    return False, "unknown check type %s" % t


def grade(cid, ws):
    c = case(cid)
    state = load_state(ws)
    out = []
    for chk in c["checks"]:
        try:
            ok, detail = check(ws, c, chk, state)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, "grader error: %s: %s" % (type(e).__name__, e)
        out.append({"type": chk["type"], "ok": bool(ok), "detail": detail, "why": chk.get("why", "")})
    cl = calls(ws)
    res = {"case": cid, "passed": sum(1 for x in out if x["ok"]), "total": len(out), "checks": out,
           "tool_calls": len(cl), "call_log": has_log(ws), "tools_used": sorted(set(x["tool"] for x in cl)),
           "tool_errors": sum(1 for x in cl if x.get("error")),
           "final_message": os.path.isfile(os.path.join(ws, FINAL))}
    print(json.dumps(res, indent=1))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "list":
        for c in CASES["cases"]:
            print("%-18s %s" % (c["id"], c["title"]))
        return
    rest = [a for a in sys.argv[2:] if a != "--keep"]
    if len(rest) != 2 or cmd not in ("setup", "brief", "grade"):
        sys.exit(__doc__)
    cid, ws = rest[0], os.path.abspath(rest[1])
    if cmd == "setup":
        setup(cid, ws, keep="--keep" in sys.argv)
    else:
        {"brief": brief, "grade": grade}[cmd](cid, ws)


if __name__ == "__main__":
    main()
