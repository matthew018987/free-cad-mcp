# SPDX-License-Identifier: Apache-2.0
"""File helpers shared by the MCP server and the FreeCAD workers (stdlib only):
content hashes and numbered revisions (<stem>.rNN.FCStd next to <stem>.FCStd)."""
import hashlib
import os
import re


def sha1(path):
    """sha1 hex of a file's bytes."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def revisions(folder, stem):
    """[(n, path)] oldest first: <stem>.FCStd is 0, <stem>.rNN.FCStd is NN."""
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return []
    out = []
    for fn in names:
        m = re.match(r"^%s(?:\.r(\d{2,}))?\.fcstd$" % re.escape(stem), fn, re.I)
        if m:
            out.append((int(m.group(1) or 0), os.path.join(folder, fn)))
    return sorted(out)


def reserve_revision(folder, stem):
    """Claim <stem>.rNN.FCStd after the newest revision with an O_EXCL placeholder,
    so two writers at the same time never get the same file."""
    n = max([r for r, _ in revisions(folder, stem)] + [0]) + 1
    while True:
        p = os.path.join(folder, "%s.r%02d.FCStd" % (stem, n))
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return p
        except FileExistsError:
            n += 1
