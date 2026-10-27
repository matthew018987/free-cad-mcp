# SPDX-License-Identifier: Apache-2.0
"""Draws the reference drawing for the reference_drawing eval case (a customer's
scanned-looking part drawing that has to be transcribed into a model).

    /Applications/FreeCAD.app/Contents/Resources/bin/python -I evals/fixtures/reference_drawing_png.py

writes evals/inbox/reference_drawing/plate_drawing.png. The part: 90 × 50 × 4 mm
plate, R5 corners, 4 × Ø4.5 holes 7 mm in from each edge (76 × 36 pattern), Ø20
hole 35 mm from the left edge on the horizontal centreline.
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import patches  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "inbox", "reference_drawing",
                   "plate_drawing.png")
W, H, R, T = 90.0, 50.0, 5.0, 4.0
SMALL = [(7, 7), (83, 7), (7, 43), (83, 43)]
BIG = (35, 25)
K = "black"

fig = plt.figure(figsize=(11.69, 8.27), dpi=110)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(-45, 160)
ax.set_ylim(-52, 93)
ax.set_aspect("equal")
ax.axis("off")
ax.add_patch(patches.Rectangle((-42, -49), 199, 139, fill=False, lw=1.2, color=K))   # sheet frame

ax.add_patch(patches.FancyBboxPatch((0, 0), W, H, boxstyle="round,pad=0,rounding_size=%g" % R, fill=False,
                                    lw=1.6, color=K))
for x, y in SMALL:
    ax.add_patch(patches.Circle((x, y), 2.25, fill=False, lw=1.3, color=K))
ax.add_patch(patches.Circle(BIG, 10, fill=False, lw=1.3, color=K))
for x, y, r in [(x, y, 2.25) for x, y in SMALL] + [(BIG[0], BIG[1], 10)]:      # centre marks
    e = r + 2.5
    ax.plot([x - e, x + e], [y, y], color=K, lw=0.5, dashes=(6, 2, 1, 2))
    ax.plot([x, x], [y - e, y + e], color=K, lw=0.5, dashes=(6, 2, 1, 2))


def dim_h(x0, x1, y, yref, text):
    for x in (x0, x1):
        ax.plot([x, x], [yref, y + (1.5 if y > yref else -1.5)], color=K, lw=0.5)
    ax.annotate("", xy=(x0, y), xytext=(x1, y), arrowprops=dict(arrowstyle="<|-|>", color=K, lw=0.7,
                                                                 shrinkA=0, shrinkB=0, mutation_scale=9))
    ax.text((x0 + x1) / 2, y + 1.2, text, ha="center", va="bottom", fontsize=11, family="DejaVu Sans")


def dim_v(y0, y1, x, xref, text):
    for y in (y0, y1):
        ax.plot([xref, x + (1.5 if x > xref else -1.5)], [y, y], color=K, lw=0.5)
    ax.annotate("", xy=(x, y0), xytext=(x, y1), arrowprops=dict(arrowstyle="<|-|>", color=K, lw=0.7,
                                                                 shrinkA=0, shrinkB=0, mutation_scale=9))
    ax.text(x - 1.2, (y0 + y1) / 2, text, ha="right", va="center", rotation=90, fontsize=11, family="DejaVu Sans")


dim_h(0, W, -16, 0, "90")
dim_h(7, 83, -9, 7, "76")
dim_h(0, BIG[0], 62, H, "35")
dim_v(0, H, -16, 0, "50")
dim_v(7, 43, -9, 7, "36")
dim_v(0, BIG[1], 104, W, "25")
ax.plot([W, 106], [0, 0], color=K, lw=0.5)

ax.annotate("4× ⌀4.5 THRU", xy=(83 + 1.6, 43 + 1.6), xytext=(100, 70), fontsize=11, family="DejaVu Sans",
            arrowprops=dict(arrowstyle="-|>", color=K, lw=0.6, mutation_scale=9))
ax.annotate("⌀20 THRU", xy=(BIG[0] - 7.1, BIG[1] + 7.1), xytext=(8, 72), fontsize=11, family="DejaVu Sans",
            arrowprops=dict(arrowstyle="-|>", color=K, lw=0.6, mutation_scale=9))
ax.annotate("4× R5", xy=(W - 1.5, 1.5), xytext=(112, 10), fontsize=11, family="DejaVu Sans",
            arrowprops=dict(arrowstyle="-|>", color=K, lw=0.6, mutation_scale=9))

ax.add_patch(patches.Rectangle((92, -49), 65, 26, fill=False, lw=1.0, color=K))           # title block
for i, line in enumerate(["FLANGE PLATE", "MATERIAL: ALUMINIUM 5052, 4 mm SHEET",
                          "LASER CUT · DEBURR · QTY AS ORDERED", "ALL DIMENSIONS IN mm · DO NOT SCALE",
                          "GENERAL TOLERANCE ISO 2768-m"]):
    ax.text(94, -27 - i * 4.6, line, fontsize=12 if i == 0 else 9, family="DejaVu Sans",
            weight="bold" if i == 0 else "normal", va="top")
ax.text(-40, 88, "SHEET 1/1 — TOP VIEW (PLATE THICKNESS 4)", fontsize=9, family="DejaVu Sans", va="top")

os.makedirs(os.path.dirname(OUT), exist_ok=True)
fig.savefig(OUT, facecolor="white")
print(OUT)
