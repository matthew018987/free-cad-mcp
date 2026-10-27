# SPDX-License-Identifier: Apache-2.0
"""
Standard dimensions for holes, threads, fasteners, ISO 286 tolerance classes
and fits, ISO 2768 general tolerances, deep-groove ball bearings, process
rules, material densities and bar stock — stdlib only, used by the MCP server
(lookup_standard) and by fclib, analysis, manufacture and the drawing worker
inside FreeCAD.

Values are nominal metric (mm) from the cited standards. Rows marked "rule"
are design rules of thumb rather than standard values, heat-set inserts are
vendor-typical and densities are typical: always prefer the datasheet of the
part or material actually used.
"""
import re

SIZES = ["M1.6", "M2", "M2.5", "M3", "M4", "M5", "M6", "M8", "M10", "M12", "M14", "M16", "M20", "M24"]

# ISO 261/262 coarse pitch; common fine pitches
PITCH = {"M1.6": 0.35, "M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0,
         "M8": 1.25, "M10": 1.5, "M12": 1.75, "M14": 2.0, "M16": 2.0, "M20": 2.5, "M24": 3.0}
FINE_PITCH = {"M8": [1.0], "M10": [1.25, 1.0], "M12": [1.5, 1.25], "M14": [1.5], "M16": [1.5],
              "M20": [1.5], "M24": [2.0]}

# ISO 273 clearance holes: fine, medium ("normal"), coarse
CLEARANCE = {"M1.6": (1.7, 1.8, 2.0), "M2": (2.2, 2.4, 2.6), "M2.5": (2.7, 2.9, 3.1),
             "M3": (3.2, 3.4, 3.6), "M4": (4.3, 4.5, 4.8), "M5": (5.3, 5.5, 5.8),
             "M6": (6.4, 6.6, 7.0), "M8": (8.4, 9.0, 10.0), "M10": (10.5, 11.0, 12.0),
             "M12": (13.0, 13.5, 14.5), "M14": (15.0, 15.5, 16.5), "M16": (17.0, 17.5, 18.5),
             "M20": (21.0, 22.0, 24.0), "M24": (25.0, 26.0, 28.0)}

# Tap drill for coarse threads (D − P, rounded to common drill sizes)
TAP_DRILL = {"M1.6": 1.25, "M2": 1.6, "M2.5": 2.05, "M3": 2.5, "M4": 3.3, "M5": 4.2, "M6": 5.0,
             "M8": 6.8, "M10": 8.5, "M12": 10.2, "M14": 12.0, "M16": 14.0, "M20": 17.5, "M24": 21.0}

# ISO 4762 socket head cap screw: head diameter dk (max), head height k (max)
SHCS = {"M1.6": (3.0, 1.6), "M2": (3.8, 2.0), "M2.5": (4.5, 2.5), "M3": (5.5, 3.0), "M4": (7.0, 4.0),
        "M5": (8.5, 5.0), "M6": (10.0, 6.0), "M8": (13.0, 8.0), "M10": (16.0, 10.0),
        "M12": (18.0, 12.0), "M14": (21.0, 14.0), "M16": (24.0, 16.0), "M20": (30.0, 20.0),
        "M24": (36.0, 24.0)}
# DIN 974-1 counterbore diameter for ISO 4762 heads (without washer)
CBORE_D = {"M1.6": 3.5, "M2": 4.4, "M2.5": 5.5, "M3": 6.5, "M4": 8.0, "M5": 10.0, "M6": 11.0,
           "M8": 15.0, "M10": 18.0, "M12": 20.0, "M14": 24.0, "M16": 26.0, "M20": 33.0, "M24": 40.0}

# ISO 10642 countersunk socket head (90°): theoretical max head dk, head height k (max)
CSK_HEAD = {"M3": (6.72, 1.86), "M4": (8.96, 2.48), "M5": (11.20, 3.10), "M6": (13.44, 3.72),
            "M8": (17.92, 4.96), "M10": (22.40, 6.20), "M12": (26.88, 7.44), "M16": (33.60, 8.80),
            "M20": (40.32, 10.16)}

# ISO 7380-1 button head: dk, k
BUTTON_HEAD = {"M3": (5.7, 1.65), "M4": (7.6, 2.2), "M5": (9.5, 2.75), "M6": (10.5, 3.3),
               "M8": (14.0, 4.4), "M10": (17.5, 5.5), "M12": (21.0, 6.6)}

# ISO 4032 hex nut: across flats s, height m
NUT = {"M2": (4.0, 1.6), "M2.5": (5.0, 2.0), "M3": (5.5, 2.4), "M4": (7.0, 3.2), "M5": (8.0, 4.7),
       "M6": (10.0, 5.2), "M8": (13.0, 6.8), "M10": (16.0, 8.4), "M12": (18.0, 10.8),
       "M16": (24.0, 14.8), "M20": (30.0, 18.0), "M24": (36.0, 21.5)}

# ISO 7089 plain washer: inner d1, outer d2, thickness h
WASHER = {"M2": (2.2, 5.0, 0.3), "M2.5": (2.7, 6.0, 0.5), "M3": (3.2, 7.0, 0.5), "M4": (4.3, 9.0, 0.8),
          "M5": (5.3, 10.0, 1.0), "M6": (6.4, 12.0, 1.6), "M8": (8.4, 16.0, 1.6),
          "M10": (10.5, 20.0, 2.0), "M12": (13.0, 24.0, 2.5), "M16": (17.0, 30.0, 3.0),
          "M20": (21.0, 37.0, 3.0), "M24": (25.0, 44.0, 4.0)}

# Heat-set inserts for 3D prints — vendor-typical (CNC Kitchen / ruthex style):
# hole diameter, insert length. VERIFY against the insert actually used.
INSERT = {"M2": (3.2, 4.0), "M2.5": (3.6, 5.7), "M3": (4.0, 5.7), "M4": (5.6, 8.1),
          "M5": (6.4, 9.5), "M6": (8.0, 12.7)}

# ISO 286-1: size ranges (upper bound, mm) and standard tolerance grades IT5–IT14 (µm)
_RANGES = [3, 6, 10, 18, 30, 50, 80, 120, 180, 250]
IT = {5: [4, 5, 6, 8, 9, 11, 13, 15, 18, 20],
      6: [6, 8, 9, 11, 13, 16, 19, 22, 25, 29],
      7: [10, 12, 15, 18, 21, 25, 30, 35, 40, 46],
      8: [14, 18, 22, 27, 33, 39, 46, 54, 63, 72],
      9: [25, 30, 36, 43, 52, 62, 74, 87, 100, 115],
      10: [40, 48, 58, 70, 84, 100, 120, 140, 160, 185],
      11: [60, 75, 90, 110, 130, 160, 190, 220, 250, 290],
      12: [100, 120, 150, 180, 210, 250, 300, 350, 400, 460],
      13: [140, 180, 220, 270, 330, 390, 460, 540, 630, 720],
      14: [250, 300, 360, 430, 520, 620, 740, 870, 1000, 1150]}
# shaft fundamental deviations: upper (es) for d e f g h; lower (ei) for k m n p
# (k: the tabulated ei holds for k4–k7; k3 and k8 upwards have ei = 0)
SHAFT_FD = {"d": ("es", [-20, -30, -40, -50, -65, -80, -100, -120, -145, -170]),
            "e": ("es", [-14, -20, -25, -32, -40, -50, -60, -72, -85, -100]),
            "f": ("es", [-6, -10, -13, -16, -20, -25, -30, -36, -43, -50]),
            "g": ("es", [-2, -4, -5, -6, -7, -9, -10, -12, -14, -15]),
            "h": ("es", [0] * 10),
            "k": ("ei", [0, 1, 1, 1, 2, 2, 2, 3, 3, 4]),
            "m": ("ei", [2, 4, 6, 7, 8, 9, 11, 13, 15, 17]),
            "n": ("ei", [4, 8, 10, 12, 15, 17, 20, 23, 27, 31]),
            "p": ("ei", [6, 12, 15, 18, 22, 26, 32, 37, 43, 50])}
# hole fundamental deviations (lower, EI) for D–H: EI = −es of the shaft letter
HOLE_FD = {L.upper(): [-v for v in SHAFT_FD[L][1]] for L in "defgh"}
# K M N P holes: ES = −ei(shaft) + Δ (Δ = IT(n) − IT(n−1), 0 up to 3 mm) for K/M/N ≤ IT8 and P ≤ IT7;
# N above IT8: ES = 0 (−4 µm up to 3 mm); P above IT7: ES = −ei
_HOLE_ES = ("K", "M", "N", "P")
HOLE_CLASSES = "D E F G H JS K M N P"
SHAFT_CLASSES = "d e f g h js k m n p"
_CLASS_HELP = ("write a tolerance class as a letter and an IT grade, e.g. 'k6', 'H7', 'H11', 'JS7', 'g6' — hole "
               "classes upper case (%s), shaft classes lower case (%s), IT5–IT14, sizes up to 250 mm; or a fit pair "
               "'H7/g6'; general tolerances: 'ISO 2768-m'" % (HOLE_CLASSES, SHAFT_CLASSES))

# Minimum wall check_manufacturing applies per process (mm); project.json dfm.min_wall_mm overrides it.
MIN_WALL = {"fdm": 1.2, "sla": 0.8, "cnc": 0.8, "turning": 0.8, "laser": 0.0, "sheet": 0.0}

# ISO 2768-1 general tolerances (linear sizes without an individual tolerance), ± mm per class.
# Bands: 0.5–3, >3–6, >6–30, >30–120, >120–400, >400–1000, >1000–2000, >2000–4000 (None = not defined)
GT_BANDS = [3, 6, 30, 120, 400, 1000, 2000, 4000]
GT_LINEAR = {"f": [0.05, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, None],
             "m": [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0],
             "c": [0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 4.0],
             "v": [None, 0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0]}
# external radii and chamfer heights ("broken edges"): 0.5–3, >3–6, >6
GT_EDGE_BANDS = [3, 6, None]
GT_EDGE = {"f": [0.2, 0.5, 1.0], "m": [0.2, 0.5, 1.0], "c": [0.4, 1.0, 2.0], "v": [0.4, 1.0, 2.0]}
# angles, by the length of the shorter leg: ≤10, >10–50, >50–120, >120–400, >400 (± minutes of arc)
GT_ANGLE_BANDS = [10, 50, 120, 400, None]
GT_ANGLE = {"f": [60, 30, 20, 10, 5], "m": [60, 30, 20, 10, 5], "c": [90, 60, 30, 15, 10],
            "v": [180, 120, 60, 30, 20]}
GT_NAMES = {"f": "fine", "m": "medium", "c": "coarse", "v": "very coarse"}

# Deep-groove ball bearings, ISO 15 boundary dimensions: d, D, B, r_s min (chamfer). Seals/shields (2RS, ZZ…)
# and clearance suffixes (C3) don't change them.
BEARINGS = {
    "604": (4, 12, 4, 0.2), "605": (5, 14, 5, 0.2), "606": (6, 17, 6, 0.3), "607": (7, 19, 6, 0.3),
    "608": (8, 22, 7, 0.3), "609": (9, 24, 7, 0.3),
    "623": (3, 10, 4, 0.15), "624": (4, 13, 5, 0.2), "625": (5, 16, 5, 0.3), "626": (6, 19, 6, 0.3),
    "627": (7, 22, 7, 0.3), "628": (8, 24, 8, 0.3), "629": (9, 26, 8, 0.3),
    "6000": (10, 26, 8, 0.3), "6001": (12, 28, 8, 0.3), "6002": (15, 32, 9, 0.3), "6003": (17, 35, 10, 0.3),
    "6004": (20, 42, 12, 0.6), "6005": (25, 47, 12, 0.6), "6006": (30, 55, 13, 1.0),
    "6200": (10, 30, 9, 0.6), "6201": (12, 32, 10, 0.6), "6202": (15, 35, 11, 0.6), "6203": (17, 40, 12, 0.6),
    "6204": (20, 47, 14, 1.0), "6205": (25, 52, 15, 1.0), "6206": (30, 62, 16, 1.0),
    "6300": (10, 35, 11, 0.6), "6301": (12, 37, 12, 1.0), "6302": (15, 42, 13, 1.0), "6303": (17, 47, 14, 1.0),
    "6304": (20, 52, 15, 1.1), "6305": (25, 62, 17, 1.1), "6306": (30, 72, 19, 1.1),
    "6800": (10, 19, 5, 0.3), "6801": (12, 21, 5, 0.3), "6802": (15, 24, 5, 0.3), "6803": (17, 26, 5, 0.3),
    "6804": (20, 32, 7, 0.3), "6805": (25, 37, 7, 0.3),
    "6900": (10, 22, 6, 0.3), "6901": (12, 24, 6, 0.3), "6902": (15, 28, 7, 0.3), "6903": (17, 30, 7, 0.3),
    "6904": (20, 37, 9, 0.3), "6905": (25, 42, 9, 0.3),
}
BEARING_RANGES = "604–609, 623–629, 6000–6006, 6200–6206, 6300–6306, 6800–6805 (618xx), 6900–6905 (619xx)"
# ISO 492 normal tolerance class: mean bore / outside diameter deviation (upper 0, lower µm) by size (upper bound)
_BRG_BORE = [(10, -8), (18, -8), (30, -10), (50, -12), (80, -15)]
_BRG_OD = [(18, -8), (30, -9), (50, -11), (80, -13), (120, -15)]

PROCESS_RULES = {
    "fdm": ["holes print 0.1–0.3 mm undersize: fc.holes enlarges clearance and plain Ø holes of fdm parts by "
            "project.json dfm.hole_compensation_mm (create_project sets 0.2; the build warns when it is missing) "
            "— don't add it again by hand (rule)",
            "sliding fit ≈ 0.3–0.4 mm diametral clearance; press fit ≈ 0.05–0.1 interference (rule, test-print)",
            "min wall ≥ %g mm (≈ 3 perimeters at 0.4–0.45 mm) — check_manufacturing warns below %g mm, or "
            "below project.json dfm.min_wall_mm when set; 2 mm+ around screw bosses and inserts (rule)"
            % (MIN_WALL["fdm"], MIN_WALL["fdm"]),
            "overhangs ≤ 45° unsupported; horizontal holes > Ø6 need a teardrop or flat top (rule)",
            "tapped threads in plastic are weak: prefer heat-set inserts or nut traps (rule)",
            "general tolerance ≈ ±0.2 mm; test-print critical fits (rule)"],
    "sla": ["holes print ~0.05–0.1 undersize: fc.holes enlarges clearance and plain Ø holes of sla parts by "
            "project.json dfm.hole_compensation_mm (create_project sets 0.1; the build warns when it is missing) "
            "— don't add it again by hand (rule)",
            "min wall ≥ %g mm — check_manufacturing warns below %g mm, or below project.json dfm.min_wall_mm "
            "when set; ≥ 1 mm for large unsupported walls (rule)" % (MIN_WALL["sla"], MIN_WALL["sla"]),
            "general tolerance ≈ ±0.1 mm (rule)"],
    "cnc": ["internal corner radius ≥ tool radius; vertical internal corners cannot be sharp (rule)",
            "pocket depth ≤ 4 × tool diameter: an R3 corner allows an Ø6 end mill → pocket ≤ 24 deep (rule)",
            "min wall ≥ %g mm in metal — check_manufacturing warns below %g mm, or below project.json "
            "dfm.min_wall_mm when set; thicker in plastics (rule)" % (MIN_WALL["cnc"], MIN_WALL["cnc"]),
            "untoleranced sizes follow the general tolerance, usually ISO 2768-m: ±0.1 up to 6 mm, ±0.2 6–30, "
            "±0.3 30–120, ±0.5 120–400 (lookup_standard tolerance='m' with d)",
            "use ISO 286 fits for bearing/pin seats (lookup_standard fit='H7/g6' or a single class 'k6' with d)"],
    "turning": ["length/diameter ≤ 3 held in the chuck alone; ≤ 6 with a tailstock centre; longer needs a "
                "steady rest — or shorten / split the part (rule)",
                "internal corners (shoulders) get the tool nose radius 0.2–0.8 mm: model a radius ≥ R0.4, or an "
                "undercut where a mating part must seat against the shoulder (rule)",
                "thread relief / undercut ≥ 1.5 × pitch long (≈ DIN 76) where an external thread runs up to a "
                "shoulder; chamfer the thread start (rule)",
                "chamfer both ends and the outer edge of every step (0.3–0.5 × 45° typical) (rule)",
                "bar stock = next standard bar ≥ finished OD + ~1 mm (e.g. Ø18 bar for Ø16 OD — "
                "lookup via bar_for); cut length ≥ finished length + 3 mm for facing and parting off (rule)",
                "cross holes, flats, slots and keyways are secondary drilling/milling operations — keep them few "
                "(rule)",
                "fits only where needed (bearing seat k6/j6, sliding h6/g6); all else to the general tolerance "
                "(rule)"],
    "laser": ["kerf ≈ 0.1–0.2 mm: the cutter offsets the path — draw nominal sizes; adjust only press-fit tabs "
              "and slots (rule)",
              "min hole Ø ≥ material thickness in metal (≥ 0.5 × t in plastics/wood) (rule)",
              "hole edge distance ≥ 1–1.5 × thickness from any edge or other hole (rule)",
              "webs, tabs and slots ≥ material thickness wide (rule)",
              "inside corners come out with the beam radius: add a small relief or radius (≥ 0.5 mm) where a "
              "mating part needs the corner (rule)",
              "flat blanks only: every feature goes through the full thickness — no pockets, steps or edge "
              "chamfers (rule)",
              "state material and thickness with the order — the DXF (1:1, mm) carries only the outline (rule)"],
    "sheet": ["bent parts: build them with fc.sheet_metal — base outline = the outside footprint, flange lengths "
              "outside dimensions — so the folded STEP and the flat-pattern DXF come from the same numbers (rule)",
              "inside bend radius ≥ material thickness (≈ 1 × t for mild steel and 5052 aluminium; 6061-T6 cracks — "
              "use ≥ 2–3 × t or 5052); one radius for every bend keeps it to one punch (rule)",
              "K-factor (where the neutral axis lies in the bend) ≈ 0.33 for air bending (0.3–0.45): bend allowance "
              "BA = π/180 · angle · (r + K·t) sets the flat length — confirm the shop's K or bend deduction when the "
              "flat size matters (rule)",
              "flange length (outside) ≥ 3 × t + r: the die needs a flat of ≥ 2 × t beyond the bend (rule)",
              "holes ≥ t in diameter; hole edge ≥ 2 × t + r from a bend line, or the hole distorts when bent (rule)",
              "a bend that ends inside an edge (a flange narrower than its edge) needs a relief slot ≥ t wide running "
              "past the bend (relief= in fc.sheet_metal); flanges that meet at a corner need inset + relief (rule)",
              "hole and outline edge distance ≥ 1–1.5 × t; tabs and slots ≥ t wide (rule)",
              "release ships the flat-pattern DXF (outline and holes on layer CUT, bend lines with angle, direction "
              "and inside radius on layer BEND), the folded STEP and the drawing; state material and thickness (rule)"],
}

# Densities (g/cm³) by material name, lower case. material_density() matches a key
# that appears in the material name as a whole word/token ('pla' is not in 'plastic');
# an alloy number beats a word, then the longest key, then the earliest in the name.
MATERIAL_DENSITY = {
    "pla": 1.24, "petg": 1.27, "pet": 1.38, "abs": 1.04, "asa": 1.07, "tpu": 1.21, "nylon": 1.14,
    "nylon 12": 1.01, "pa6": 1.14, "pa66": 1.14, "pa12": 1.01, "polycarbonate": 1.20, "pc": 1.20, "pc-abs": 1.13,
    "pc/abs": 1.13, "pet-g": 1.27, "pp": 0.90, "resin": 1.15,
    "acrylic": 1.19, "pmma": 1.19, "pom": 1.41, "acetal": 1.41, "delrin": 1.41, "hdpe": 0.95, "uhmw": 0.93,
    "polypropylene": 0.90, "ptfe": 2.20, "peek": 1.32, "plywood": 0.60, "mdf": 0.75,
    "aluminium": 2.70, "aluminum": 2.70, "6061": 2.70, "6082": 2.70, "5052": 2.68, "5083": 2.66,
    "7075": 2.81, "2024": 2.78, "steel": 7.85, "mild steel": 7.85, "s235": 7.85, "s355": 7.85, "c45": 7.85,
    "stainless": 8.00, "303": 8.00, "304": 8.00, "316": 8.00, "17-4": 7.80, "tool steel": 7.80,
    "cast iron": 7.20, "brass": 8.50, "bronze": 8.80, "aluminium bronze": 7.60, "copper": 8.96,
    "titanium": 4.43, "ti-6al-4v": 4.43, "inconel": 8.44, "magnesium": 1.80, "zinc": 7.10,
}

# Metric round bar (bright / drawn stock) diameters commonly held by suppliers, mm.
STANDARD_BAR = [3, 4, 5, 6, 8, 10, 12, 14, 15, 16, 18, 20, 22, 25, 28, 30, 32, 35, 38, 40, 42, 45, 50, 55,
                60, 65, 70, 75, 80, 90, 100, 110, 120, 130, 140, 150, 160, 180, 200, 220, 250, 300]


def _pos_float(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _key_at(k, n):
    """Index of table key k in name n as a whole token, or -1: a letter at the key's edge
    may not touch another letter, a digit not another digit ('pla' is not in 'plastic';
    '304L', 'AISI304', 'S235JR', 'Nylon12' match)."""
    def cls(c):
        return "a-z" if c.isalpha() else "0-9" if c.isdigit() else None
    left = "(?<![%s])" % cls(k[0]) if cls(k[0]) else ""
    right = "(?![%s])" % cls(k[-1]) if cls(k[-1]) else ""
    m = re.search(left + re.escape(k) + right, n)
    return m.start() if m else -1


def material_density(name):
    """(density, matched key) for a material name from MATERIAL_DENSITY, or (None, None)."""
    n = str(name or "").lower()
    keys = [(k, i) for k, i in ((k, _key_at(k, n)) for k in MATERIAL_DENSITY) if i >= 0]
    if not keys:
        return None, None
    # an alloy number beats 'aluminium', then the longest key, then the earliest in the name
    k = max(keys, key=lambda ki: (any(c.isdigit() for c in ki[0]), len(ki[0]), -ki[1], ki[0]))[0]
    return MATERIAL_DENSITY[k], k


def same_material(a, b):
    """Two material names are the same (case and spacing ignored)."""
    return " ".join(str(a or "").lower().split()) == " ".join(str(b or "").lower().split())


def density_info(project, label):
    """(density g/cm³ or None, where it came from) for one part: parts.<label>.density_g_cm3 →
    project material.density_g_cm3 when the part has no material of its own or the same one as
    the project (an explicit project density beats the table guess) → the part's material in
    MATERIAL_DENSITY → project material.density_g_cm3."""
    project = project or {}
    pcfg = (project.get("parts") or {}).get(label) or {}
    pmat = project.get("material") or {}
    d = _pos_float(pcfg.get("density_g_cm3"))
    if d:
        return d, "parts.%s.density_g_cm3" % label
    pd = _pos_float(pmat.get("density_g_cm3"))
    own = str(pcfg.get("material") or "").strip()
    if own and not (pd and same_material(own, pmat.get("name"))):
        d, key = material_density(own)
        if d:
            src = "'%s' in the material table" % key
            if pd:
                src += (" for parts.%s.material '%s' (not the project density %g g/cm³, which is for '%s'; set "
                        "parts.%s.density_g_cm3 to override)" % (label, own, pd, pmat.get("name") or "?", label))
            return d, src
    if pd:
        return pd, "project material"
    return None, None


# Linear-elastic properties for analysis: E (MPa), Poisson ν, yield (MPa), note. Typical datasheet
# values (rule) — prints are weaker across layers and plastics creep: say so, and prefer the user's data.
MATERIAL_MECH = {
    "pla": (3500, 0.36, 50, "FDM: weaker across layers (≈50 %); creeps under load"),
    "petg": (2100, 0.38, 50, "FDM: weaker across layers (≈50 %); creeps under load"),
    "pet-g": (2100, 0.38, 50, "FDM: weaker across layers (≈50 %); creeps under load"),
    "abs": (2200, 0.35, 40, "FDM: weaker across layers (≈50 %)"),
    "asa": (2000, 0.35, 45, "FDM: weaker across layers (≈50 %)"),
    "pc": (2300, 0.37, 60, "FDM: weaker across layers"),
    "nylon": (1700, 0.40, 45, "absorbs moisture; values dry"),
    "pa12": (1700, 0.40, 45, "absorbs moisture; values dry"),
    "pa6": (2500, 0.40, 60, "absorbs moisture; values dry"),
    "resin": (2500, 0.38, 50, "SLA resin is brittle: treat the yield as the breaking stress"),
    "pom": (3000, 0.35, 65, "creeps under load"), "delrin": (3000, 0.35, 65, "creeps under load"),
    "acrylic": (3200, 0.37, 70, "brittle: treat the yield as the breaking stress"),
    "pmma": (3200, 0.37, 70, "brittle: treat the yield as the breaking stress"),
    "6061": (68900, 0.33, 276, "6061-T6; much lower near welds"),
    "6082": (70000, 0.33, 250, "6082-T6"),
    "7075": (71700, 0.33, 503, "7075-T6"),
    "5052": (70300, 0.33, 193, "5052-H32"),
    "2024": (73100, 0.33, 324, "2024-T3"),
    "aluminium": (69000, 0.33, 100, "generic aluminium — alloys range 35–500 MPa yield: name the alloy"),
    "aluminum": (69000, 0.33, 100, "generic aluminium — alloys range 35–500 MPa yield: name the alloy"),
    "303": (193000, 0.29, 240, "annealed"), "304": (193000, 0.29, 215, "annealed"),
    "316": (193000, 0.29, 205, "annealed"),
    "stainless": (193000, 0.29, 205, "generic austenitic stainless, annealed"),
    "s235": (210000, 0.30, 235, ""), "s355": (210000, 0.30, 355, ""),
    "1018": (205000, 0.29, 370, "cold drawn"), "1045": (205000, 0.29, 450, "cold drawn; normalised ≈ 310"),
    "steel": (210000, 0.30, 235, "generic structural steel (S235)"),
    "brass": (97000, 0.34, 140, "free-cutting brass, conservative"),
    "titanium": (113800, 0.34, 880, "Ti-6Al-4V"),
}


def material_mech(name):
    """((E MPa, ν, yield MPa, note), matched key) for a material name, or (None, None)."""
    n = str(name or "").lower()
    keys = [(k, i) for k, i in ((k, _key_at(k, n)) for k in MATERIAL_MECH) if i >= 0]
    if not keys:
        return None, None
    k = max(keys, key=lambda ki: (any(c.isdigit() for c in ki[0]), len(ki[0]), -ki[1], ki[0]))[0]
    return MATERIAL_MECH[k], k


def material_report(name):
    """E, ν, yield, density and notes for a material name (lookup_standard(material=…))."""
    vals, key = material_mech(name)
    dens, dkey = material_density(name)
    if not vals and not dens:
        raise ValueError("no material %r in the table — known: %s; or give its values per part in project.json "
                         "(parts.<Part>.youngs_modulus_mpa, poisson, yield_mpa, density_g_cm3)"
                         % (name, ", ".join(sorted(MATERIAL_MECH))))
    out = ["%s  (material table: '%s')" % (name, key or dkey)]
    if vals:
        out.append("  E %d MPa · ν %.2f · yield %d MPa%s" % (vals[0], vals[1], vals[2],
                                                          ("  — " + vals[3]) if vals[3] else ""))
    if dens:
        out.append("  density %g g/cm³" % dens)
    out.append("  typical values (rule) — prefer the supplier's datasheet; override per part in project.json: "
               "parts.<Part>.youngs_modulus_mpa, poisson, yield_mpa, density_g_cm3")
    return "\n".join(out)


def mech_info(project, label, override=None):
    """Mechanical properties for one part: {E, nu, yield, density, name, source, note}.
    Order: an explicit override {name | E, nu, yield} → parts.<label>.{youngs_modulus_mpa,
    poisson, yield_mpa} → the part's material (or the project material) in MATERIAL_MECH.
    Raises ValueError naming what to set when nothing is known."""
    project = project or {}
    pcfg = (project.get("parts") or {}).get(label) or {}
    name = pcfg.get("material") or (project.get("material") or {}).get("name") or ""
    ov = override if isinstance(override, dict) else ({"name": override} if override else {})
    if ov.get("name"):
        name = ov["name"]
    vals, key = material_mech(name)
    E = _pos_float(ov.get("E") or ov.get("youngs_modulus_mpa") or pcfg.get("youngs_modulus_mpa"))
    nu = _pos_float(ov.get("nu") or ov.get("poisson") or pcfg.get("poisson"))
    yld = _pos_float(ov.get("yield") or ov.get("yield_mpa") or pcfg.get("yield_mpa"))
    given = [x for x in (E, nu, yld) if x]
    if vals:
        E, nu, yld = E or vals[0], nu or vals[1], yld or vals[2]
    if not (E and nu and yld):
        raise ValueError("no mechanical properties for %s's material %r — name a known material (e.g. 6061-T6, "
                         "PETG, 304) or give material={\"E\": MPa, \"nu\": 0.33, \"yield\": MPa}"
                         % (label, name or "(none)"))
    src = ("'%s' in the material table" % key) if vals and not given else (
        "given" if not vals else "'%s' in the material table, with given values" % key)
    dens, _ = density_info(project, label)
    return {"E": E, "nu": nu, "yield": yld, "density": dens, "name": name or "(given)", "source": src,
            "note": vals[3] if vals else ""}


def density_for(project, label):
    return density_info(project, label)[0]


def bar_for(od, allowance=1.0):
    """Smallest standard round bar ≥ od + allowance (mm), or None above the table."""
    need = float(od) + allowance
    for b in STANDARD_BAR:
        if b >= need - 1e-6:
            return b
    return None


def norm_size(size):
    s = str(size).strip().upper().replace(" ", "")
    if not s.startswith("M"):
        s = "M" + s
    s = s.split("X")[0]
    if s.endswith(".0"):
        s = s[:-2]
    if s not in PITCH:
        raise ValueError("unknown metric size %r (have %s)" % (size, ", ".join(SIZES)))
    return s


def clearance(size, fit="normal"):
    i = {"fine": 0, "close": 0, "normal": 1, "medium": 1, "coarse": 2, "loose": 2}[fit]
    return CLEARANCE[norm_size(size)][i]


def counterbore(size):
    """DIN 974-1 diameter for an ISO 4762 head; depth = k + 0.4 (rule: head sits just below flush)."""
    s = norm_size(size)
    return CBORE_D[s], round(SHCS[s][1] + 0.4, 2)


def heat_set_insert(size):
    """(hole diameter, insert length, recommended hole depth = length + 1)."""
    s = norm_size(size)
    if s not in INSERT:
        raise ValueError("no insert data for %s" % s)
    d, L = INSERT[s]
    return d, L, round(L + 1.0, 2)


def nut_trap(size, clearance_mm=0.2):
    """(across-flats incl. clearance, depth = m + clearance) for an ISO 4032 nut."""
    s = norm_size(size)
    af, m = NUT[s]
    return round(af + clearance_mm, 2), round(m + clearance_mm, 2)


def _range_index(d):
    if not d > 0:
        raise ValueError("d must be a size > 0 mm (got %r)" % d)
    for i, ub in enumerate(_RANGES):
        if d <= ub:
            return i
    raise ValueError("ISO 286 table here covers sizes up to 250 mm (got %g)" % d)


def parse_class(cls):
    """'k6' → ('k', 6); 'JS7' → ('JS', 7). ValueError with examples otherwise."""
    s = str(cls or "").strip()
    m = re.match(r"^([A-Za-z]{1,2})(\d{1,2})$", s)
    if not m:
        raise ValueError("tolerance class %r: %s" % (cls, _CLASS_HELP))
    letter, grade = m.groups()
    if letter not in HOLE_FD and letter not in SHAFT_FD and letter not in ("JS", "js") + _HOLE_ES:
        raise ValueError("tolerance class %r: '%s' is not tabulated here%s — %s" % (
            cls, letter, " (j/J are not symmetric: use js/JS)" if letter in ("j", "J") else "", _CLASS_HELP))
    if int(grade) not in IT:
        raise ValueError("tolerance class %r: IT grades here are IT%d–IT%d" % (cls, min(IT), max(IT)))
    return letter, int(grade)


def _deviations_um(d, cls):
    """(lower, upper) deviation in µm of class cls at nominal size d (ISO 286-1/-2)."""
    letter, grade = parse_class(cls)
    i = _range_index(float(d))
    it = IT[grade][i]
    if letter in ("JS", "js"):
        half = (it - 1) // 2 if 7 <= grade <= 11 and it % 2 else it / 2.0   # ISO 286: odd IT7–11 round down
        return -half, half
    if letter in HOLE_FD:
        lo = HOLE_FD[letter][i]
        return lo, lo + it
    if letter in _HOLE_ES:
        ei = SHAFT_FD[letter.lower()][1][i]
        lim = 7 if letter == "P" else 8
        if grade <= lim:
            if grade - 1 not in IT:
                raise ValueError("tolerance class %r: %s needs IT%d or above here" % (cls, letter, min(IT) + 1))
            es = -ei + (0 if i == 0 else it - IT[grade - 1][i])
        elif letter == "N":
            es = -4 if i == 0 else 0
        elif letter == "P":
            es = -ei
        else:
            raise ValueError("tolerance class %r: %s holes are tabulated here up to IT8 — use the maker's or "
                             "ISO 286-2 tables" % (cls, letter))
        return es - it, es
    kind, vals = SHAFT_FD[letter]
    if kind == "es":
        return vals[i] - it, vals[i]
    ei = vals[i] if letter != "k" or 4 <= grade <= 7 else 0
    return ei, ei + it


def tolerance(d, cls):
    """Limits (lower, upper) in mm for a hole ('H7', 'G7', 'JS7', 'K7') or shaft ('g6', 'k6'…) class."""
    lo, hi = _deviations_um(d, cls)
    return round(d + lo / 1000.0, 4), round(d + hi / 1000.0, 4)


def deviations(d, cls):
    """(upper, lower) deviation in mm — the order a drawing writes them."""
    lo, hi = _deviations_um(d, cls)
    return round(hi / 1000.0, 4), round(lo / 1000.0, 4)


def _dev(v, nd):
    if abs(v * 1000 - round(v * 1000)) > 1e-6:      # half microns (js/JS of an odd IT)
        nd = max(nd, 4)
    return "%+.*f" % (nd, v + 0.0)


def deviation_text(d, cls, nd=3):
    """'+0.010/+0.001' — upper deviation first, as on the drawing."""
    up, lo = deviations(d, cls)
    return "%s/%s" % (_dev(up, nd), _dev(lo, nd))


def _split_fit(spec):
    s = str(spec or "").replace(" ", "")
    parts = s.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("fit %r: give a hole/shaft pair like 'H7/g6' (clearance), 'H7/k6' (transition), "
                         "'H7/p6' (press) — or a single class like 'k6', 'H11', 'JS7' for its limits, or "
                         "'ISO 2768-m' for a general tolerance" % spec)
    h, s_ = parts
    if h[:1].islower() and s_[:1].isupper():
        raise ValueError("fit %r: write the hole class (upper case) first, e.g. '%s/%s'" % (spec, s_, h))
    if not h[:1].isupper() or not s_[:1].islower():
        raise ValueError("fit %r: the hole class is upper case and the shaft class lower case, e.g. 'H7/g6'" % spec)
    return h, s_


def fit(d, spec):
    """'H7/g6' at nominal d → hole and shaft limits, clearance range and fit type."""
    hole_cls, shaft_cls = _split_fit(spec)
    h = tolerance(d, hole_cls)
    s = tolerance(d, shaft_cls)
    cmin = round(h[0] - s[1], 4)
    cmax = round(h[1] - s[0], 4)
    kind = "clearance" if cmin >= 0 else ("interference" if cmax <= 0 else "transition")
    return {"d": d, "hole": hole_cls, "hole_limits": h, "shaft": shaft_cls, "shaft_limits": s,
            "clearance_min": cmin, "clearance_max": cmax, "type": kind}


def tolerance_report(d, cls):
    """One ISO 286 class at nominal d: deviations (upper first), limits and IT grade."""
    d = float(d)
    letter, grade = parse_class(cls)
    lo, hi = tolerance(d, cls)
    up, dn = deviations(d, cls)
    kind = "hole" if letter[0].isupper() else "shaft"
    return ("Ø%g %s  (ISO 286, %s, IT%d = %g µm)\n"
            "  deviations  upper %s  lower %s mm\n"
            "  limits      %.4f … %.4f mm\n"
            "  on a drawing: Ø%g %s (%s)" % (
                d, cls, kind, grade, IT[grade][_range_index(d)], _dev(up, 3), _dev(dn, 3), lo, hi, d, cls,
                deviation_text(d, cls)))


# ---------------------------------------------------------------- ISO 2768-1 general tolerances

def parse_general_class(cls):
    """'m', 'ISO 2768-m', '2768-mK', 'medium' → 'm'. ValueError with examples otherwise."""
    s = str(cls or "").strip().lower().replace("iso", "").replace("2768", "").replace("-", "").replace(" ", "")
    for k, name in GT_NAMES.items():
        if s == name.replace(" ", ""):
            return k
    if s[:1] in GT_LINEAR and (len(s) == 1 or (len(s) == 2 and s[1] in "hkl")):
        return s[0]
    raise ValueError("general tolerance class %r: use ISO 2768-1 'f' (fine), 'm' (medium), 'c' (coarse) or 'v' "
                     "(very coarse) — e.g. 'm' or 'ISO 2768-m'" % cls)


def _gt_band(bands, x):
    for i, ub in enumerate(bands):
        if ub is None or x <= ub:
            return i
    return None


def general_tolerance(cls, d):
    """ISO 2768-1 ± deviation (mm) for a linear size d without an individual tolerance:
    general_tolerance('m', 80) → 0.3."""
    k = parse_general_class(cls)
    d = float(d)
    if d < 0.5:
        raise ValueError("ISO 2768-1 starts at 0.5 mm — give sizes below that an explicit tolerance")
    i = _gt_band(GT_BANDS, d)
    if i is None:
        raise ValueError("ISO 2768-1 covers linear sizes up to 4000 mm")
    v = GT_LINEAR[k][i]
    if v is None:
        raise ValueError("ISO 2768-%s defines no general tolerance for %g mm — state an explicit tolerance" % (k, d))
    return v


def general_angle_tolerance(cls, length):
    """ISO 2768-1 angular ± deviation in degrees for the shorter leg `length` (mm)."""
    k = parse_general_class(cls)
    return GT_ANGLE[k][_gt_band(GT_ANGLE_BANDS, float(length))] / 60.0


def _arcmin(m):
    return "±%d°%s" % (m // 60, "%d'" % (m % 60) if m % 60 else "")


def general_tolerance_report(cls, d=None):
    k = parse_general_class(cls)
    lows = [0.5] + GT_BANDS[:-1]
    lines = ["ISO 2768-%s (%s) — general tolerances for sizes without an individual tolerance" % (k, GT_NAMES[k])]
    if d is not None:
        v = general_tolerance(k, d)
        i = _gt_band(GT_BANDS, float(d))
        lines.append("  %g mm → ±%g mm  (band over %g up to %g)" % (float(d), v, lows[i], GT_BANDS[i]))
    lines.append("  linear     " + " · ".join("%g–%g ±%g" % (lo, hi, v)
                                             for lo, hi, v in zip(lows, GT_BANDS, GT_LINEAR[k]) if v is not None))
    lines.append("  radii and chamfer heights   0.5–3 ±%g · 3–6 ±%g · over 6 ±%g" % tuple(GT_EDGE[k]))
    lines.append("  angles (shorter leg)   up to 10 %s · 10–50 %s · 50–120 %s · 120–400 %s · over 400 %s"
                 % tuple(_arcmin(m) for m in GT_ANGLE[k]))
    lines.append("  mating sizes need a fit, not the general tolerance (lookup_standard fit='H7/g6' or 'k6' with d)")
    return "\n".join(lines)


def is_general_class(spec):
    try:
        parse_general_class(spec)
        return "2768" in str(spec) or str(spec).strip().lower() in GT_NAMES.values()
    except ValueError:
        return False


# ------------------------------------------------------------------------------- bearings

def _bearing_key(designation):
    """'SKF 6204-2RS' → ('6204', '2RS'); '61804' → ('6804', ''); no designation → (None, '')."""
    s = str(designation or "").strip().upper()
    m = re.match(r"^(?:(?:SKF|FAG|NSK|NTN|KOYO|INA|NACHI|TIMKEN)\s*)?(\d{3,5})\s*[-/ ]?\s*([A-Z0-9/ -]*)$", s)
    if not m:
        return None, ""
    digits, suffix = m.group(1), m.group(2).strip(" -/")
    if len(digits) == 5 and digits[:3] in ("618", "619"):          # ISO 61800 = 6800, 61904 = 6904
        digits = "6" + digits[2:]
    return digits, suffix


def bearing(designation):
    """Deep-groove ball bearing by designation ('608', '6204-2RS', '61804') →
    {designation, d, D, B, r_s_min, suffix, bore_tol, od_tol}. ValueError naming the table otherwise."""
    key, suffix = _bearing_key(designation)
    if key not in BEARINGS:
        raise ValueError("no data for bearing %r — tabulated deep-groove ball bearings: %s (ISO 15); for others "
                         "use the maker's catalogue" % (designation, BEARING_RANGES))
    d, D, B, rs = BEARINGS[key]
    bore = next(v for ub, v in _BRG_BORE if d <= ub)
    od = next(v for ub, v in _BRG_OD if D <= ub)
    return {"designation": key, "d": d, "D": D, "B": B, "r_s_min": rs, "suffix": suffix,
            "bore_tol": (0.0, bore / 1000.0), "od_tol": (0.0, od / 1000.0)}


def bearing_report(designation):
    b = bearing(designation)
    d, D, B, rs = b["d"], b["D"], b["B"], b["r_s_min"]
    return "\n".join([
        "%s deep-groove ball bearing  (ISO 15 boundary dimensions%s)" % (
            b["designation"], "; suffix %s (seals/shields/clearance) doesn't change them" % b["suffix"]
            if b["suffix"] else ""),
        "  bore d %g · outside D %g · width B %g · chamfer r_s min %g" % (d, D, B, rs),
        "  ISO 492 normal class: bore 0/%+.3f (Ø%.3f … %g) · OD 0/%+.3f (Ø%.3f … %g)" % (
            b["bore_tol"][1], d + b["bore_tol"][1], d, b["od_tol"][1], D + b["od_tol"][1], D),
        "  shaft/housing fillet radius ≤ %g (r_s min) so the ring seats on the shoulder (rule)" % rs,
        "  shoulders: high enough to clear the ring chamfer, low enough to bear on that ring's face only — take "
        "da min/max and Da max from the maker's catalogue (rule)",
        "  seats: rotating shaft, normal load → shaft k6 (js6/j6 for light loads or easy assembly), housing H7; "
        "a floating bearing → housing H7/G7 (rule — confirm with the maker's fit tables)",
        "  model it: fc.turned/fc.ref_part with these sizes; ISO 286 limits: lookup_standard fit='k6', d=%g" % d])


def is_bearing(designation):
    return _bearing_key(designation)[0] in BEARINGS


def metric_report(size):
    """Thread/fastener table for 'M3'; a bearing designation ('608') gives its bearing report."""
    try:
        s = norm_size(size)
    except ValueError as e:
        if is_bearing(size):
            return bearing_report(size)
        raise ValueError("%s — for rolling bearings use bearing='608' (have %s)" % (e, BEARING_RANGES))
    lines = ["%s  (metric coarse, ISO 261/262)" % s,
             "  pitch                 %g mm%s" % (PITCH[s], ("   fine: " + ", ".join("%g" % p for p in FINE_PITCH[s]))
                                                if s in FINE_PITCH else ""),
             "  tap drill             Ø%g" % TAP_DRILL[s],
             "  clearance ISO 273     fine Ø%g · normal Ø%g · coarse Ø%g" % CLEARANCE[s]]
    if s in SHCS:
        dk, k = SHCS[s]
        cd, cdepth = counterbore(s)
        lines.append("  socket head ISO 4762  head Ø%g × %g  →  counterbore Ø%g ↧%g (DIN 974-1; depth = k + 0.4 rule)"
                     % (dk, k, cd, cdepth))
    if s in CSK_HEAD:
        dk, k = CSK_HEAD[s]
        lines.append("  countersunk ISO 10642 head Ø%g (theoretical) × %g, 90°  →  countersink Ø%g (rule: dk + 0.2)"
                     % (dk, k, round(dk + 0.2, 2)))
    if s in BUTTON_HEAD:
        lines.append("  button head ISO 7380  head Ø%g × %g" % BUTTON_HEAD[s])
    if s in NUT:
        af, m = NUT[s]
        lines.append("  hex nut ISO 4032      AF %g · across corners %.2f · height %g  →  trap AF %g ↧%g (+0.2 rule)"
                     % (af, af / 0.8660254, m, af + 0.2, m + 0.2))
    if s in WASHER:
        lines.append("  washer ISO 7089       Ø%g / Ø%g × %g" % WASHER[s])
    if s in INSERT:
        d, L, depth = heat_set_insert(s)
        lines.append("  heat-set insert       hole Ø%g, length %g → hole depth ≥ %g, wall ≥ 1.5 around (vendor-typical, VERIFY)"
                     % (d, L, depth))
    lines.append("  thread engagement     steel ≥ 1×D · aluminium 1.5–2×D · plastic ≥ 2×D (rule)")
    return "\n".join(lines)


def fit_report(d, spec):
    """'H7/g6' → hole/shaft limits and fit type; a single class ('k6', 'H11') → its limits;
    'ISO 2768-m' → the general tolerance at d."""
    if is_general_class(spec):
        return general_tolerance_report(spec, d)
    if "/" not in str(spec):
        return tolerance_report(d, str(spec).strip())
    f = fit(float(d), spec)
    return ("Ø%g %s/%s  (ISO 286)\n  hole  %s  %.4f … %.4f\n  shaft %s  %.4f … %.4f\n"
            "  clearance %+.4f … %+.4f mm  →  %s fit" % (
                f["d"], f["hole"], f["shaft"], f["hole"], f["hole_limits"][0], f["hole_limits"][1],
                f["shaft"], f["shaft_limits"][0], f["shaft_limits"][1], f["clearance_min"],
                f["clearance_max"], f["type"]))
