# SPDX-License-Identifier: Apache-2.0
"""
ISO GPS tolerancing, stdlib only. Shared by the MCP server (lookup_standard),
the build worker (tolerances.json), the drawing worker (symbols) and the
release (inspection list):

- requirements: the 14 ISO 1101 geometrical characteristics, sizes (±
  deviations, limits, ISO 286 classes) and surface texture (ISO 21920-1):
  parsing, validation rules and their text;
- symbols as SVG: feature control frames, datum features (ISO 5459) and
  surface texture, in ISO 7083 proportions, the characteristic symbols drawn
  as paths so no font glyph is needed (digits and letters use osifont);
- 1D tolerance stack-ups: worst case, RSS, Monte Carlo, contributions and
  ISO 286 grade allocation;
- the bolted-joint position rule (floating and fixed fasteners at MMC);
- inspection rows (the CSV a shop fills in) and stable balloon numbers.

Units: mm; surface texture in µm. "rule" marks a design rule of thumb, not
standard text. See docs/tolerancing-plan.md.
"""
import csv
import difflib
import io
import math
import random
import re

import standards


class ToleranceError(ValueError):
    """A malformed requirement; the message says what to write instead."""


# ------------------------------------------------------------------ the characteristics

# Feature kinds a geometry reference resolves to (the build worker classifies them):
#   plane      a planar face
#   hole, boss an internal / external cylinder: a feature of size with an axis
#   width      two opposed parallel faces (slot or tab): a feature of size with a median plane
#   revolution a cone, sphere or torus
#   surface    anything else (free-form, several faces)
KINDS = ("plane", "hole", "boss", "width", "revolution", "surface")
FOS = ("hole", "boss", "width")
AXIAL = ("hole", "boss")


def _ch(symbol, category, datums, on, mmr, words):
    return {"symbol": symbol, "category": category, "datums": datums, "on": on, "mmr": mmr, "words": words}


# datums: none (form: never relative to a datum) · required · axis (the primary datum must be an axis) ·
# usual (position: a warning without) · optional (profile). mmr: Ⓜ / Ⓛ allowed (ISO 2692, features of size).
CHARACTERISTICS = {
    "straightness": _ch("⏤", "form", "none", ("plane", "hole", "boss"), True,
                        "of a line on a surface (between two lines) or of an axis (a ⌀ cylinder)"),
    "flatness": _ch("⏥", "form", "none", ("plane",), False, "of a planar face: between two parallel planes"),
    "roundness": _ch("○", "form", "none", ("hole", "boss", "revolution"), False,
                     "of each cross-section: between two concentric circles"),
    "cylindricity": _ch("⌭", "form", "none", ("hole", "boss"), False,
                        "of a cylinder: between two coaxial cylinders"),
    "line_profile": _ch("⌒", "profile", "optional", KINDS, False,
                        "of each line of a surface about its true profile"),
    "surface_profile": _ch("⌓", "profile", "optional", KINDS, False,
                           "of a surface about its true (basic) shape, and location with datums"),
    "parallelism": _ch("∥", "orientation", "required", ("plane", "hole", "boss", "width"), True,
                       "of a face or axis to the datum"),
    "perpendicularity": _ch("⟂", "orientation", "required", ("plane", "hole", "boss", "width"), True,
                            "of a face or axis to the datum"),
    "angularity": _ch("∠", "orientation", "required", ("plane", "hole", "boss", "width"), True,
                      "of a face or axis at a basic angle to the datum"),
    "position": _ch("⌖", "location", "usual", ("hole", "boss", "width", "plane"), True,
                    "of an axis (a ⌀ cylinder), median plane or face about its basic location from the datums"),
    "coaxiality": _ch("◎", "location", "axis", AXIAL, True, "of an axis to the datum axis (a ⌀ cylinder)"),
    "symmetry": _ch("⌯", "location", "required", ("width",), True, "of a median plane about the datum"),
    "circular_runout": _ch("↗", "runout", "axis", ("hole", "boss", "revolution", "plane"), False,
                           "of each circle of the surface while the part turns about the datum axis"),
    "total_runout": _ch("⌰", "runout", "axis", ("hole", "boss", "plane"), False,
                        "of the whole surface while the part turns about the datum axis"),
}
CATEGORY_ORDER = ("form", "profile", "orientation", "location", "runout")
ALIASES = {"circularity": "roundness", "concentricity": "coaxiality", "profile": "surface_profile",
           "runout": "circular_runout", "true_position": "position", "parallel": "parallelism",
           "perpendicular": "perpendicularity", "texture": "surface", "roughness": "surface",
           "surface_texture": "surface"}
TYPES = tuple(CHARACTERISTICS) + ("size", "surface")
MODIFIER = {"M": "Ⓜ", "L": "Ⓛ", "P": "Ⓟ", "E": "Ⓔ", "F": "Ⓕ"}
LAYS = ("=", "⊥", "X", "M", "C", "R", "P")
DATUM_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"         # ISO 5459 practice: no I, O or Q

# The tolerance zone of each characteristic (t = the tolerance value)
ZONES = {
    "straightness": "two parallel lines t apart (a line on a surface), or a ⌀t cylinder (an axis)",
    "flatness": "two parallel planes t apart",
    "roundness": "two concentric circles t apart, in each cross-section",
    "cylindricity": "two coaxial cylinders t apart",
    "line_profile": "two lines enveloping circles of ⌀t centred on the true profile",
    "surface_profile": "two surfaces enveloping spheres of ⌀t centred on the true surface",
    "parallelism": "two planes t apart parallel to the datum, or a ⌀t cylinder for an axis to a datum axis",
    "perpendicularity": "two planes t apart perpendicular to the datum, or a ⌀t cylinder for an axis",
    "angularity": "two planes t apart at the basic angle to the datum, or a ⌀t cylinder for an axis to a datum axis",
    "position": "a ⌀t cylinder about the true position of an axis, or two planes t apart for a median plane or face",
    "coaxiality": "a ⌀t cylinder about the datum axis",
    "symmetry": "two planes t apart, symmetric about the datum",
    "circular_runout": "two circles t apart in each cross-section (on a face: at each radius) as the part turns",
    "total_runout": "two coaxial cylinders t apart (on a face: two planes) as the part turns",
}
# Typical values on machined metal parts (rule): what to start from before a requirement says otherwise
TYPICAL = {
    "straightness": "0.02 to 0.05 per 100 mm milled or turned; 0.01 ground",
    "flatness": "0.02 to 0.05 on a milled face up to 100 mm; 0.01 or less ground or lapped",
    "roundness": "0.005 to 0.01 turned or bored; 0.002 ground",
    "cylindricity": "0.01 to 0.02 turned or bored; 0.005 ground",
    "line_profile": "0.05 to 0.2",
    "surface_profile": "0.05 to 0.2 machined; 0.2 to 0.5 as a general profile",
    "parallelism": "0.02 to 0.05",
    "perpendicularity": "0.02 to 0.05 (a bore to its face: ⌀0.02 in one setup)",
    "angularity": "0.05 to 0.1",
    "position": "⌀0.05 to ⌀0.2; bolt patterns from the bolted-joint rule (lookup_standard joint='M5')",
    "coaxiality": "⌀0.02 to ⌀0.05 machined in one setup; ⌀0.01 ground",
    "symmetry": "0.05 to 0.1 (keyways, slots)",
    "circular_runout": "0.01 to 0.03 turned; 0.005 ground",
    "total_runout": "0.02 to 0.05 turned; 0.01 ground",
}


def characteristic(name):
    """Canonical requirement type for a name or alias ('circularity' → 'roundness'), or ToleranceError."""
    t = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    t = ALIASES.get(t, t)
    if t not in TYPES:
        close = difflib.get_close_matches(t, TYPES, 1)
        raise ToleranceError("type %r: use %s%s" % (name, ", ".join(TYPES),
                                                      " (did you mean %r?)" % close[0] if close else ""))
    return t


# ------------------------------------------------------------------ numbers

def _g(v, nd=4):
    """A number without trailing zeros: 0.25, 10.12, 7."""
    s = ("%." + str(nd) + "f") % float(v)
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return "0" if s in ("-0", "", "+0") else s


def _decimals(v):
    for nd in range(5):
        if abs(round(v, nd) - v) < 1e-9:
            return nd
    return 4


def _dev(v, nd):
    """A deviation with its sign; zero as '0' (ISO 129-1)."""
    if abs(v) < 0.5 * 10 ** -max(nd, 4):
        return "0"
    return "%+.*f" % (nd, v)


def deviation_pair(upper, lower):
    """'+0.021/0', '0/-0.12', '+0.10/-0.05': upper first, both with the same decimals (ISO 129-1)."""
    nd = max(_decimals(upper), _decimals(lower), 1)
    return "%s/%s" % (_dev(upper, nd), _dev(lower, nd))


def tolerance_text(upper, lower):
    """'±0.035' for a symmetric tolerance, else the deviation pair."""
    if abs(upper + lower) < 1e-9 and upper > 0:
        return "±" + _g(upper)
    return deviation_pair(upper, lower)


def _num(v, what):
    if isinstance(v, bool):
        raise ToleranceError("%s must be a number, got %r" % (what, v))
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToleranceError("%s must be a number, got %r" % (what, v))
    if math.isnan(f) or math.isinf(f):
        raise ToleranceError("%s must be a finite number, got %r" % (what, v))
    return f


# ------------------------------------------------------------------ sizes

_PM = re.compile(r"^(?:±|\+-|\+/-)\s*(\d+(?:\.\d*)?|\.\d+)$")
_DEVS = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*/\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))$")
_LIMITS = re.compile(r"^(\d+(?:\.\d*)?)\s*(?:\.\.|…)\s*(\d+(?:\.\d*)?)$")
_CLASS = re.compile(r"^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$")
_SIZE_HELP = ("write a size tolerance as '±0.05', deviations '+0.1/0' or '0/-0.12' (upper first), limits "
              "'10.00..10.05', or an ISO 286 class in \"class\" ('H7', 'h9')")


def parse_size(tol=None, cls=None, limits=None, nominal=None):
    """One size requirement → {kind: pm | dev | limits | class, upper, lower (deviations in mm, None until
    the nominal is known for limits and classes), limits, class}. Raises ToleranceError."""
    given = [k for k, v in (("tol", tol), ("class", cls), ("limits", limits)) if v not in (None, "", [])]
    if len(given) != 1:
        raise ToleranceError("a size needs exactly one of tol, class or limits (got %s): %s" % (
            ", ".join(given) or "none", _SIZE_HELP))
    out = {"kind": None, "upper": None, "lower": None, "limits": None, "class": None}
    if cls not in (None, ""):
        c = str(cls).strip().replace(" ", "")
        if not _CLASS.match(c):
            raise ToleranceError("class %r: an ISO 286 class such as 'H7' (hole) or 'h9' (shaft)" % cls)
        try:
            standards.parse_class(c)
        except ValueError as e:
            raise ToleranceError(str(e))
        out.update(kind="class", **{"class": c})
        if nominal is not None:
            try:
                up, lo = standards.deviations(float(nominal), c)
            except ValueError as e:
                raise ToleranceError(str(e))
            out.update(upper=up, lower=lo)
        return out
    if limits not in (None, "", []):
        if isinstance(limits, str):
            m = _LIMITS.match(limits.strip())
            if not m:
                raise ToleranceError("limits %r: write [10.00, 10.05] or '10.00..10.05'" % limits)
            lim = (float(m.group(1)), float(m.group(2)))
        else:
            if not isinstance(limits, (list, tuple)) or len(limits) != 2:
                raise ToleranceError("limits %r: write [lower, upper], e.g. [10.00, 10.05]" % (limits,))
            lim = (_num(limits[0], "limits[0]"), _num(limits[1], "limits[1]"))
        if not lim[0] < lim[1]:
            raise ToleranceError("limits %r: the lower limit comes first and is smaller" % (limits,))
        out.update(kind="limits", limits=lim)
        if nominal is not None:
            out.update(upper=round(lim[1] - float(nominal), 6), lower=round(lim[0] - float(nominal), 6))
        return out
    if isinstance(tol, (int, float)) and not isinstance(tol, bool):
        raise ToleranceError("tol %r: a bare number is ambiguous for a size: %s" % (tol, _SIZE_HELP))
    s = str(tol).strip().replace(" ", "").replace("−", "-")
    m = _PM.match(s)
    if m:
        v = float(m.group(1))
        if not v > 0:
            raise ToleranceError("tol %r: ± needs a value above 0" % tol)
        out.update(kind="pm", upper=v, lower=-v)
        return out
    m = _DEVS.match(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        up, lo = max(a, b), min(a, b)
        if up - lo < 1e-9:
            raise ToleranceError("tol %r: the two deviations are equal" % tol)
        out.update(kind="dev", upper=up, lower=lo)
        return out
    if _LIMITS.match(s.replace("…", "..")):
        raise ToleranceError("tol %r looks like limits: put them in \"limits\": [lower, upper]" % tol)
    if _CLASS.match(s):
        raise ToleranceError("tol %r looks like an ISO 286 class: put it in \"class\": %r" % (tol, s))
    raise ToleranceError("tol %r not understood: %s" % (tol, _SIZE_HELP))


def size_deviations(e, nominal):
    """(upper, lower) deviations of a normalized size entry at its nominal."""
    p = parse_size(e.get("tol"), e.get("class"), e.get("limits"), nominal)
    return p["upper"], p["lower"]


# ------------------------------------------------------------------ datum references

_DREF = re.compile(r"^([A-Z](?:-[A-Z])*)(?:\(([ML])\)|([ⓂⓁ]))?$")


def parse_datum_refs(spec):
    """'A', 'A B C', 'A|B|C', ['A', 'B(M)'], 'A-B' → [{letters: ['A'], mod: None}, …] (at most three)."""
    if spec in (None, "", []):
        return []
    toks = list(spec) if isinstance(spec, (list, tuple)) else re.split(r"[\s|,]+", str(spec).strip())
    out, seen = [], set()
    for t in toks:
        t = str(t).strip()
        if not t:
            continue
        m = _DREF.match(t)
        if not m:
            raise ToleranceError("datum reference %r: write letters such as 'A', 'A B C', a common datum 'A-B' or "
                                 "a modifier 'B(M)'" % t)
        letters = m.group(1).split("-")
        bad = [x for x in letters if x not in DATUM_LETTERS]
        if bad:
            raise ToleranceError("datum letter %s is not used (I, O and Q are left out): pick another" % bad[0])
        for x in letters:
            if x in seen:
                raise ToleranceError("datum %s appears twice in one frame" % x)
            seen.add(x)
        mod = m.group(2) or {"Ⓜ": "M", "Ⓛ": "L"}.get(m.group(3) or "")
        out.append({"letters": letters, "mod": mod or None})
    if len(out) > 3:
        raise ToleranceError("at most three datums in a frame (primary, secondary, tertiary), got %d" % len(out))
    return out


def datum_ref_text(d):
    return "-".join(d["letters"]) + (MODIFIER[d["mod"]] if d.get("mod") else "")


# ------------------------------------------------------------------ entries

_COMMON = {"id", "type", "source", "note"}
_FIELDS = {"size": _COMMON | {"feature", "between", "tol", "class", "limits", "envelope"},
           "surface": _COMMON | {"feature", "ra", "rz", "removal", "lay", "method"},
           "geometric": _COMMON | {"feature", "tol", "datums", "material", "projected", "all_around", "zone",
                                   "angle"}}
_MAT = {"M": "M", "MMC": "M", "MMR": "M", "Ⓜ": "M", "L": "L", "LMC": "L", "LMR": "L", "Ⓛ": "L"}


def _group(t):
    return t if t in ("size", "surface") else "geometric"


def normalize(raw):
    """One tolerances.json entry → its canonical dict, or ToleranceError naming the field. The feature
    references stay strings; the build worker resolves them."""
    if not isinstance(raw, dict):
        raise ToleranceError("each entry is an object {\"id\": …, \"type\": …, \"feature\": …}, got %r" % (raw,))
    eid = raw.get("id")
    if not isinstance(eid, str) or not re.match(r"^[A-Za-z0-9_-]+$", eid):
        raise ToleranceError("id %r: a short name of letters, digits, - and _ (e.g. 'A-flat'); stacks refer to it "
                             "as Part.id" % (eid,))
    t = characteristic(raw.get("type"))
    allowed = _FIELDS[_group(t)]
    for k in raw:
        if k not in allowed:
            close = difflib.get_close_matches(k, sorted(allowed), 1)
            raise ToleranceError("%s: unknown field %r for a %s entry%s" % (
                eid, k, t, " (did you mean %r?)" % close[0] if close else " (fields: %s)" % ", ".join(sorted(allowed))))
    e = {"id": eid, "type": t}
    for k in ("source", "note"):
        if raw.get(k) not in (None, ""):
            e[k] = str(raw[k])
    feat, between = raw.get("feature"), raw.get("between")
    if t == "size" and between is not None:
        if feat is not None:
            raise ToleranceError("%s: give feature (a hole, boss, slot or tab) or between (two faces), not both" % eid)
        if not isinstance(between, (list, tuple)) or len(between) != 2 or not all(
                isinstance(x, str) and x.strip() for x in between):
            raise ToleranceError("%s: between needs two references, e.g. [\"Plate.face(-Z)\", \"Plate.face(+Z)\"]"
                                 % eid)
        e["between"] = [between[0].strip(), between[1].strip()]
    else:
        if not isinstance(feat, str) or not feat.strip():
            raise ToleranceError("%s: feature is the toleranced feature's reference, e.g. \"Plate.holes(d=5.5)\" or "
                                 "\"Plate.face(-Z)\"" % eid)
        e["feature"] = feat.strip()
    if t == "size":
        try:
            p = parse_size(raw.get("tol"), raw.get("class"), raw.get("limits"))
        except ToleranceError as ex:
            raise ToleranceError("%s: %s" % (eid, ex))
        for k in ("tol", "class", "limits"):
            if raw.get(k) not in (None, "", []):
                e[k] = p["class"] if k == "class" else (list(p["limits"]) if k == "limits" else str(raw[k]).strip())
        env = raw.get("envelope")
        if env is not None and not isinstance(env, bool):
            raise ToleranceError("%s: envelope is true or false" % eid)
        # Ⓔ by default on ISO 286 fits (ISO 14405-1: under ISO 8015 a fit needs it); "envelope": false opts out
        e["envelope"] = bool(env) if env is not None else bool(e.get("class"))
        return e
    if t == "surface":
        given = [k for k in ("ra", "rz") if raw.get(k) is not None]
        if len(given) != 1:
            raise ToleranceError("%s: a surface needs one of ra or rz (µm), e.g. \"ra\": 1.6" % eid)
        v = _num(raw[given[0]], "%s: %s" % (eid, given[0]))
        if not v > 0:
            raise ToleranceError("%s: %s must be above 0 µm" % (eid, given[0]))
        e[given[0]] = v
        rem = raw.get("removal")
        if rem not in (None, True, False, "any"):
            raise ToleranceError("%s: removal is true (material removal required), false (not permitted) or "
                                 "\"any\"" % eid)
        e["removal"] = rem
        lay = raw.get("lay")
        if lay not in (None, ""):
            if str(lay) not in LAYS:
                raise ToleranceError("%s: lay is one of %s" % (eid, " ".join(LAYS)))
            e["lay"] = str(lay)
        if raw.get("method") not in (None, ""):
            e["method"] = str(raw["method"])
        return e
    tol = _num(raw.get("tol"), "%s: tol (the zone width in mm)" % eid)
    if not tol > 0:
        raise ToleranceError("%s: tol must be above 0 mm" % eid)
    e["tol"] = tol
    try:
        e["datums"] = parse_datum_refs(raw.get("datums"))
    except ToleranceError as ex:
        raise ToleranceError("%s: %s" % (eid, ex))
    mat = raw.get("material")
    if mat not in (None, ""):
        m = _MAT.get(str(mat).strip().upper()) or _MAT.get(str(mat).strip())
        if not m:
            raise ToleranceError("%s: material is \"M\" (Ⓜ maximum material) or \"L\" (Ⓛ least material)" % eid)
        e["material"] = m
    if raw.get("projected") is not None:
        pv = _num(raw["projected"], "%s: projected (the Ⓟ height in mm)" % eid)
        if not pv > 0:
            raise ToleranceError("%s: projected must be a height above 0 mm" % eid)
        e["projected"] = pv
    if raw.get("all_around") not in (None, False):
        if raw["all_around"] is not True:
            raise ToleranceError("%s: all_around is true or false" % eid)
        e["all_around"] = True
    if raw.get("zone") not in (None, ""):
        if raw["zone"] not in ("dia", "width"):
            raise ToleranceError("%s: zone is \"dia\" (a ⌀ cylinder) or \"width\" (two planes)" % eid)
        e["zone"] = raw["zone"]
    if t == "angularity":
        e["angle"] = _num(raw.get("angle"), "%s: angle (the basic angle in degrees to the datum)" % eid)
    elif raw.get("angle") is not None:
        raise ToleranceError("%s: angle belongs to angularity only" % eid)
    return e


def _finding(level, rule, text):
    return {"level": level, "rule": rule, "text": text}


def zone_is_dia(e, feature=None, datums=None):
    """Whether the tolerance zone is a ⌀ cylinder: the explicit zone, else an axis feature under
    straightness, perpendicularity, position or coaxiality, or under parallelism / angularity to a datum
    axis."""
    if e.get("zone"):
        return e["zone"] == "dia"
    if (feature or {}).get("kind") not in AXIAL:
        return False
    t = e["type"]
    if t in ("straightness", "perpendicularity", "position", "coaxiality"):
        return True
    if t in ("parallelism", "angularity") and e.get("datums"):
        prim = e["datums"][0]["letters"][0]
        return ((datums or {}).get(prim) or {}).get("kind") in AXIAL
    return False


def _kind_words(k):
    return {"plane": "a planar face", "hole": "a hole", "boss": "a shaft or boss", "width": "a slot or tab",
            "revolution": "a surface of revolution", "surface": "a general surface"}.get(k, k or "unknown")


def check_entry(e, feature, datums=None, part="the part"):
    """Rules for one normalized entry against its resolved feature. feature: {kind, count?, size?, length?,
    internal? (width: a slot True, a tab False), ref?}; datums: {letter: feature} of the part.
    Returns findings [{level: error | warn, rule, text}]."""
    out = []
    datums = datums or {}
    kind = (feature or {}).get("kind")
    t = e["type"]
    ref = (feature or {}).get("ref") or e.get("feature") or " ↔ ".join(e.get("between") or [])
    if t == "size":
        if e.get("feature") and kind not in FOS:
            out.append(_finding("error", "size-on-fos", "a size tolerance on feature needs a feature of size (a hole, "
                                "boss, slot or tab); %s is %s: use between: [face, face] for a distance" % (
                                    ref, _kind_words(kind))))
        c = e.get("class")
        if c and kind in ("hole", "boss"):
            hole_cls = c[0].isupper()
            if hole_cls and kind == "boss":
                out.append(_finding("error", "class-case", "%s is a hole class but %s is a shaft or boss: use %s" % (
                    c, ref, c.lower())))
            elif not hole_cls and kind == "hole":
                out.append(_finding("error", "class-case", "%s is a shaft class but %s is a hole: use %s" % (
                    c, ref, c.upper())))
        if c and kind == "width" and feature.get("internal") is not None:
            if c[0].isupper() != bool(feature["internal"]):
                out.append(_finding("error", "class-case", "%s is a %s class but %s is a %s" % (
                    c, "hole" if c[0].isupper() else "shaft", ref, "slot" if feature["internal"] else "tab")))
        if e.get("envelope") and kind not in FOS and e.get("feature"):
            out.append(_finding("error", "envelope", "Ⓔ applies to a feature of size only"))
        return out
    if t == "surface":
        v = e.get("ra", e.get("rz"))
        if e.get("ra") is not None and not any(abs(v - s) < 1e-9 for s in standards.RA_SERIES):
            near = min(standards.RA_SERIES, key=lambda s: abs(s - v))
            out.append(_finding("warn", "ra-series", "Ra %s is not in the preferred series (%s): use Ra %s" % (
                _g(v), ", ".join(_g(s) for s in standards.RA_SERIES), _g(near))))
        return out
    ch = CHARACTERISTICS[t]
    if kind and kind not in ch["on"]:
        alt = [n for n, c in CHARACTERISTICS.items() if kind in c["on"] and c["category"] == ch["category"]]
        out.append(_finding("error", "applies-to", "%s applies to %s; %s is %s%s" % (
            t, " or ".join(_kind_words(k) for k in ch["on"]) if len(ch["on"]) < len(KINDS) else "any feature",
            ref, _kind_words(kind), " (for it: %s)" % ", ".join(alt) if alt else "")))
    refs = e.get("datums") or []
    letters = [x for d in refs for x in d["letters"]]
    rule = ch["datums"]
    if rule == "none" and refs:
        out.append(_finding("error", "datums", "%s is a form tolerance: it is never relative to a datum (drop %s)" % (
            t, " ".join(letters))))
    if rule in ("required", "axis") and not refs:
        out.append(_finding("error", "datums", "%s needs a datum, e.g. \"datums\": \"A\"" % t))
    if rule == "usual" and not refs:
        out.append(_finding("warn", "datums", "position without datums locates the features only relative to "
                            "each other: add the datums that locate them (\"datums\": \"A B C\")"))
    for x in letters:
        if x not in datums:
            out.append(_finding("error", "datum-defined", "datum %s is not defined on %s (has %s)" % (
                x, part, ", ".join(sorted(datums)) or "no datums")))
    if rule == "axis" and refs and all(x in datums for x in refs[0]["letters"]):
        if any((datums[x] or {}).get("kind") not in AXIAL for x in refs[0]["letters"]):
            out.append(_finding("error", "datum-axis", "%s needs a datum axis as its primary datum (a hole or shaft, "
                                "or a common axis A-B); %s is not an axis" % (t, "-".join(refs[0]["letters"]))))
    for d in refs:
        if d.get("mod"):
            for x in d["letters"]:
                if x in datums and (datums[x] or {}).get("kind") not in FOS:
                    out.append(_finding("error", "datum-modifier", "%s on datum %s: a material modifier needs a "
                                        "datum feature of size (a hole, shaft, slot or tab)" % (
                                            MODIFIER[d["mod"]], x)))
    for x in letters:
        dref = (datums.get(x) or {}).get("ref")
        if dref and e.get("feature") and _same_ref(dref, e["feature"], datums):
            out.append(_finding("error", "self-datum", "%s is datum %s itself: a feature is not toleranced relative "
                                "to itself" % (ref, x)))
    if e.get("material"):
        if not ch["mmr"]:
            out.append(_finding("error", "material", "%s takes no %s (ISO 2692: only straightness of an axis, "
                                "orientation, position, coaxiality and symmetry)" % (t, MODIFIER[e["material"]])))
        elif kind and kind not in FOS:
            out.append(_finding("error", "material", "%s needs a feature of size (a hole, boss, slot or tab); %s is "
                                "%s" % (MODIFIER[e["material"]], ref, _kind_words(kind))))
    if e.get("projected") and (t not in ("position", "perpendicularity") or (kind and kind != "hole")):
        out.append(_finding("error", "projected", "Ⓟ (a projected zone) is for the position or perpendicularity "
                            "of holes, mostly tapped or press-fit holes"))
    if e.get("all_around") and ch["category"] != "profile":
        out.append(_finding("error", "all-around", "all around belongs to profile tolerances"))
    if zone_is_dia(e, feature, datums) and kind and kind not in AXIAL:
        out.append(_finding("error", "zone", "a ⌀ zone needs an axis (a hole or shaft); %s is %s" % (
            ref, _kind_words(kind))))
    size = (feature or {}).get("size")
    if size and kind in FOS and e["tol"] >= float(size):
        out.append(_finding("warn", "tol-size", "a %s zone of %s on a %s of %s is larger than the feature: check the "
                            "value" % (t, _g(e["tol"]), _kind_words(kind)[2:], _g(size))))
    screw = (feature or {}).get("screw")
    if t == "position" and e.get("material") == "M" and isinstance(screw, dict) and screw.get("size"):
        try:
            r = joint(screw["size"], screw.get("clearance") or "normal")
        except (ValueError, KeyError):
            r = None
        if r and e["tol"] > r["T"] + 1e-9:
            out.append(_finding("warn", "joint", "⌀%s Ⓜ is more than a floating %s screw allows in %s %s "
                                "clearance holes (⌀%s = %s − %s): the screws may not go in" % (
                                    _g(e["tol"]), r["size"], "⌀" + _g(r["hole"]), r["clearance"], _g(r["T"]),
                                    _g(r["hole_min"]), _g(r["screw"]))))
    return out


def _same_ref(a, b, datums=None):
    """Two references name the same feature: equal text, or 'Part.datum(A)' standing for datum A's own."""
    def canon(r):
        r = str(r).strip()
        m = re.match(r"^(.*)\.datum\(\s*([A-Z])\s*\)$", r)
        if m and datums and (datums.get(m.group(2)) or {}).get("ref"):
            return str(datums[m.group(2)]["ref"]).replace(" ", "")
        return r.replace(" ", "")
    return canon(a) == canon(b)


FORM = ("straightness", "flatness", "roundness", "cylindricity")
ORIENTATION = ("parallelism", "perpendicularity", "angularity")
LOCATION = ("position", "coaxiality", "symmetry", "circular_runout", "total_runout")


def check_scheme(entries, features, datums=None, part="the part"):
    """Every rule for one part's normalized entries. features: {entry id: feature}; datums: {letter:
    feature (with ref)}. Returns {entry id: [findings], "_part": [findings]}: the per-entry rules plus
    duplicate ids, the hierarchy form ≤ orientation ≤ location on one feature, datum features without a
    form control, and the surface texture of fits."""
    datums = datums or {}
    res = {"_part": []}
    seen = set()
    for e in entries:
        if e["id"] in seen:
            res["_part"].append(_finding("error", "duplicate-id", "id %s is used twice on %s" % (e["id"], part)))
        seen.add(e["id"])
        res.setdefault(e["id"], []).extend(check_entry(e, features.get(e["id"]) or {}, datums, part))

    def on(e, ref):
        return e.get("feature") and _same_ref(e["feature"], ref, datums)
    groups = {}
    for e in entries:
        if e.get("feature"):
            key = next((k for k in groups if _same_ref(k, e["feature"], datums)), e["feature"])
            groups.setdefault(key, []).append(e)
    for key, es in groups.items():
        form = [e for e in es if e["type"] in FORM]
        orient = [e for e in es if e["type"] in ORIENTATION]
        loc = [e for e in es if e["type"] in LOCATION]
        if form and orient:
            o = min(orient, key=lambda x: x["tol"])
            for f in form:
                if f["tol"] > o["tol"] + 1e-9:
                    res[f["id"]].append(_finding("warn", "hierarchy", "%s %s is looser than the %s %s of the same "
                                                 "feature, which already limits its form: make it tighter or drop it"
                                                 % (f["type"], _g(f["tol"]), o["type"], _g(o["tol"]))))
        if orient and loc:
            lo = min(loc, key=lambda x: x["tol"])
            for o in orient:
                if o["tol"] > lo["tol"] + 1e-9:
                    res[o["id"]].append(_finding("warn", "hierarchy", "%s %s is looser than the %s %s of the same "
                                                 "feature, which already limits its orientation: make it tighter or "
                                                 "drop it" % (o["type"], _g(o["tol"]), lo["type"], _g(lo["tol"]))))
        sizes = [e for e in es if e["type"] == "size" and e.get("class")]
        for s in [e for e in es if e["type"] == "surface" and e.get("ra") is not None]:
            for z in sizes:
                grade = standards.parse_class(z["class"])[1]
                lim = next((ra for g, ra in standards.FIT_RA if grade <= g), None)
                if lim is not None and s["ra"] > lim + 1e-9:
                    res[s["id"]].append(_finding("warn", "fit-ra", "Ra %s on the %s fit: IT%d surfaces want Ra %s or "
                                                 "finer (rule: bearing makers' seat recommendations)" % (
                                                     _g(s["ra"]), z["class"], grade, _g(lim))))
    for letter, d in sorted(datums.items()):
        dref = (d or {}).get("ref")
        if not dref:
            continue
        mine = [e for e in entries if on(e, dref)]
        controlled = any(e["type"] in FORM + ORIENTATION + LOCATION + ("surface_profile", "line_profile")
                         for e in mine) or any(e["type"] == "size" and e.get("envelope") for e in mine)
        if not controlled:
            want = "a flatness" if d.get("kind") == "plane" else "a cylindricity, or Ⓔ on its size"
            res["_part"].append(_finding("warn", "datum-form", "datum %s (%s) has no form tolerance: add %s so the "
                                         "feature the part is located on is controlled" % (letter, dref, want)))
    return res


# ------------------------------------------------------------------ text

def frame_text(e, feature=None, datums=None):
    """'⌖ ⌀0.25 Ⓜ | A | B': a feature control frame on one line."""
    sym = CHARACTERISTICS[e["type"]]["symbol"]
    tol = ("⌀" if zone_is_dia(e, feature, datums) else "") + _g(e["tol"])
    if e.get("material"):
        tol += " " + MODIFIER[e["material"]]
    if e.get("projected"):
        tol += " Ⓟ" + _g(e["projected"])
    s = "%s %s" % (sym, tol) + "".join(" | " + datum_ref_text(d) for d in e.get("datums") or [])
    if e.get("type") == "angularity":
        s += "  (basic angle %s°)" % _g(e["angle"])
    if e.get("all_around"):
        s += "  (all around)"
    return s


def size_text(e, feature=None, nominal=None):
    """'⌀22 H7 Ⓔ (+0.021/0)', '10.12 ±0.035', '8 +0.05/0'; without the nominal only the tolerance."""
    kind = (feature or {}).get("kind")
    head = (("⌀" if kind in AXIAL else "") + _g(nominal)) if nominal is not None else ""
    env = " Ⓔ" if e.get("envelope") else ""
    if e.get("class"):
        s = (head + " " if head else "") + e["class"] + env
        if nominal is not None:
            try:
                up, lo = standards.deviations(float(nominal), e["class"])
                s += " (%s)" % deviation_pair(up, lo)
            except ValueError:
                pass
        return s
    if e.get("limits"):
        lo, hi = e["limits"]
        return "%s…%s%s" % (_g(lo), _g(hi), env)
    p = parse_size(e.get("tol"))
    return (head + " " if head else "") + tolerance_text(p["upper"], p["lower"]) + env


def surface_text(e):
    """'√ Ra 1.6', '√ Rz 6.3 ground', '√ Ra 3.2 (no material removal)'."""
    s = "√ %s %s" % (("Ra", _g(e["ra"])) if e.get("ra") is not None else ("Rz", _g(e["rz"])))
    if e.get("method"):
        s += " " + e["method"]
    if e.get("lay"):
        s += " lay " + e["lay"]
    if e.get("removal") is False:
        s += " (no material removal)"
    return s


def entry_text(e, feature=None, datums=None, nominal=None):
    if e["type"] == "size":
        return size_text(e, feature, nominal)
    if e["type"] == "surface":
        return surface_text(e)
    return frame_text(e, feature, datums)


# ------------------------------------------------------------------ SVG symbols

EM = 4.0 / 3        # TechDraw draws a text of height h with an osifont em of h × 4/3: capitals are h tall


def _n(v):
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def text_width(s, h):
    """Estimated advance (mm) of s in osifont with capitals h tall (capitals and digits ≈ 0.72 h)."""
    w = 0.0
    for ch in str(s):
        if ch in ".,:;'|!":
            w += 0.3 * h
        elif ch in " ()[]/-":
            w += 0.45 * h
        elif ch.islower():
            w += 0.6 * h
        else:
            w += 0.72 * h
    return w


class _Svg(object):
    """Shapes in local mm (y down), optionally rotated 90° clockwise, gathered into one tight SVG."""

    def __init__(self, lw, rotate=False):
        self.lw, self.rot = lw, rotate
        self.items = []
        self.box = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    def p(self, x, y):
        return (-y, x) if self.rot else (x, y)

    def _grow(self, x, y):
        b = self.box
        b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)

    def path(self, pts, closed=False, fill=False, width=None):
        q = [self.p(x, y) for x, y in pts]
        for x, y in q:
            self._grow(x, y)
        d = "M" + " L".join("%s,%s" % (_n(x), _n(y)) for x, y in q) + (" Z" if closed else "")
        self.items.append('<path d="%s" fill="%s" stroke="#000000" stroke-width="%s" stroke-linejoin="round" '
                          'stroke-linecap="round"/>' % (d, "#000000" if fill else "none", _n(width or self.lw)))

    def circle(self, cx, cy, r, fill=False):
        x, y = self.p(cx, cy)
        self._grow(x - r, y - r)
        self._grow(x + r, y + r)
        self.items.append('<circle cx="%s" cy="%s" r="%s" fill="%s" stroke="#000000" stroke-width="%s"/>' % (
            _n(x), _n(y), _n(r), "#000000" if fill else "none", _n(self.lw)))

    def arc(self, cx, cy, r, closed=False):
        """The upper half of a circle (y down: towards −y), closed by its diameter when asked."""
        a, b = self.p(cx - r, cy), self.p(cx + r, cy)
        top = self.p(cx, cy - r)
        for x, y in (a, b, top):
            self._grow(x, y)
        d = "M%s,%s A%s,%s 0 0 1 %s,%s%s" % (_n(a[0]), _n(a[1]), _n(r), _n(r), _n(b[0]), _n(b[1]),
                                              " Z" if closed else "")
        self.items.append('<path d="%s" fill="none" stroke="#000000" stroke-width="%s" stroke-linecap="round"/>' % (
            d, _n(self.lw)))

    def text(self, x, y, s, h, anchor="middle"):
        """s with its baseline at y; capitals h tall."""
        w = text_width(s, h)
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        for cx, cy in ((x0, y - h), (x0 + w, y - h), (x0, y + 0.25 * h), (x0 + w, y + 0.25 * h)):
            self._grow(*self.p(cx, cy))
        X, Y = self.p(x, y)
        rot = ' transform="rotate(90 %s %s)"' % (_n(X), _n(Y)) if self.rot else ""
        self.items.append('<text x="%s" y="%s" font-family="osifont" font-size="%s" text-anchor="%s" '
                          'fill="#000000"%s>%s</text>' % (_n(X), _n(Y), _n(h * EM), anchor, rot, _esc(s)))

    def arrow(self, tail, tip, h):
        """A filled arrowhead at tip, pointing from tail to tip."""
        (x0, y0), (x1, y1) = tail, tip
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        a, w = 0.9 * h, 0.9 * h * math.tan(math.radians(12))
        bx, by = x1 - ux * a, y1 - uy * a
        self.path([(x1, y1), (bx - uy * w, by + ux * w), (bx + uy * w, by - ux * w)], closed=True, fill=True)

    def render(self, margin, points):
        """(svg text, width, height, {name: point}) with the drawing moved into a box with a margin."""
        x0, y0 = self.box[0] - margin, self.box[1] - margin
        W, H = self.box[2] - self.box[0] + 2 * margin, self.box[3] - self.box[1] + 2 * margin
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="%smm" height="%smm" '
               'viewBox="0 0 %s %s"><g transform="translate(%s,%s)">%s</g></svg>' % (
                   _n(W), _n(H), _n(W), _n(H), _n(-x0), _n(-y0), "".join(self.items)))
        moved = {}
        for k, pt in points.items():
            if pt is not None:
                x, y = self.p(*pt)
                moved[k] = (round(x - x0, 4), round(y - y0, 4))
        return svg, W, H, moved


def _symbol(s, name, cx, cy, h):
    """The ISO 1101 symbol of a characteristic, centred on (cx, cy), about h tall."""
    u = h
    if name == "straightness":
        s.path([(cx - 0.75 * u, cy), (cx + 0.75 * u, cy)])
    elif name == "flatness":
        s.path([(cx - 0.75 * u, cy + 0.3 * u), (cx + 0.45 * u, cy + 0.3 * u), (cx + 0.75 * u, cy - 0.3 * u),
                (cx - 0.45 * u, cy - 0.3 * u)], closed=True)
    elif name == "roundness":
        s.circle(cx, cy, 0.6 * u)
    elif name == "cylindricity":
        r = 0.42 * u
        s.circle(cx, cy, r)
        dx, dy = math.cos(math.radians(60)), -math.sin(math.radians(60))
        nx, ny = -dy, dx
        for side in (-1, 1):
            px, py = cx + side * r * nx, cy + side * r * ny
            s.path([(px - dx * 0.8 * u, py - dy * 0.8 * u), (px + dx * 0.8 * u, py + dy * 0.8 * u)])
    elif name in ("line_profile", "surface_profile"):
        s.arc(cx, cy + 0.3 * u, 0.65 * u, closed=name == "surface_profile")
    elif name == "parallelism":
        for off in (-0.25, 0.25):
            s.path([(cx + (off - 0.25) * u, cy + 0.6 * u), (cx + (off + 0.25) * u, cy - 0.6 * u)])
    elif name == "perpendicularity":
        s.path([(cx - 0.7 * u, cy + 0.6 * u), (cx + 0.7 * u, cy + 0.6 * u)])
        s.path([(cx, cy + 0.6 * u), (cx, cy - 0.7 * u)])
    elif name == "angularity":
        x0, y0 = cx - 0.7 * u, cy + 0.6 * u
        s.path([(x0 + 1.4 * u, y0), (x0, y0), (x0 + 1.4 * u * math.cos(math.radians(30)),
                                               y0 - 1.4 * u * math.sin(math.radians(30)))])
    elif name == "position":
        s.circle(cx, cy, 0.42 * u)
        s.path([(cx - 0.75 * u, cy), (cx + 0.75 * u, cy)])
        s.path([(cx, cy - 0.75 * u), (cx, cy + 0.75 * u)])
    elif name == "coaxiality":
        s.circle(cx, cy, 0.65 * u)
        s.circle(cx, cy, 0.32 * u)
    elif name == "symmetry":
        s.path([(cx - 0.75 * u, cy), (cx + 0.75 * u, cy)])
        for dy in (-0.4, 0.4):
            s.path([(cx - 0.45 * u, cy + dy * u), (cx + 0.45 * u, cy + dy * u)])
    elif name in ("circular_runout", "total_runout"):
        starts = [(-0.35, 0.55)] if name == "circular_runout" else [(-0.65, 0.55), (-0.1, 0.55)]
        for sx, sy in starts:
            tail, tip = (cx + sx * u, cy + sy * u), (cx + (sx + 0.75) * u, cy + (sy - 1.1) * u)
            s.path([tail, tip])
            s.arrow(tail, tip, 0.55 * h)
        if name == "total_runout":
            s.path([(cx - 0.65 * u, cy + 0.55 * u), (cx - 0.1 * u, cy + 0.55 * u)])
    else:
        raise ToleranceError("no symbol for %r" % name)


def _modifier(s, letter, cx, cy, h):
    """A circled letter (Ⓜ Ⓛ Ⓟ Ⓔ Ⓕ) centred on (cx, cy)."""
    s.circle(cx, cy, 0.55 * h)
    s.text(cx, cy + 0.35 * h, letter, 0.7 * h)


def _diameter(s, cx, cy, h):
    s.circle(cx, cy, 0.36 * h)
    s.path([(cx - 0.28 * h, cy + 0.55 * h), (cx + 0.28 * h, cy - 0.55 * h)])


def frame_svg(e, feature=None, datums=None, h=3.5, leader=None, above=None):
    """A feature control frame (ISO 1101, ISO 7083 proportions: frame 2h high, symbol cell 2h square,
    lines h/10) as SVG. leader: {"to": (dx, dy), "side": "left" | "right"}: the point the arrow ends on,
    paper mm from the middle of the frame's left or right side (y down). above: text over the frame
    ("4×"). Returns {svg, width, height, frame: [x, y, w, h], attach, anchor}: the frame box, the
    leader's start and its arrow tip in the SVG's own mm."""
    s = _Svg(h / 10.0)
    H = 2 * h
    tol_items = []
    if zone_is_dia(e, feature, datums):
        tol_items.append(("dia", 0.9 * h))
    tol_items.append(("text", _g(e["tol"]), text_width(_g(e["tol"]), h)))
    if e.get("material"):
        tol_items.append(("mod", e["material"], 1.1 * h))
    if e.get("projected"):
        tol_items.append(("mod", "P", 1.1 * h))
        tol_items.append(("text", _g(e["projected"]), text_width(_g(e["projected"]), h)))
    gap = 0.25 * h
    tol_w = max(2 * h, sum(it[-1] for it in tol_items) + gap * (len(tol_items) - 1) + 0.8 * h)
    cells = [2 * h, tol_w]
    dtexts = []
    for d in e.get("datums") or []:
        txt = "-".join(d["letters"])
        w = text_width(txt, h) + (gap + 1.1 * h if d.get("mod") else 0)
        dtexts.append((txt, d.get("mod"), w))
        cells.append(max(2 * h, w + 0.8 * h))
    W = sum(cells)
    s.path([(0, 0), (W, 0), (W, H), (0, H)], closed=True)
    x = 0.0
    for w in cells[:-1]:
        x += w
        s.path([(x, 0), (x, H)])
    _symbol(s, e["type"], h, h, h)
    x = 2 * h + (tol_w - (sum(it[-1] for it in tol_items) + gap * (len(tol_items) - 1))) / 2
    for it in tol_items:
        if it[0] == "dia":
            _diameter(s, x + it[-1] / 2, h, h)
        elif it[0] == "mod":
            _modifier(s, it[1], x + it[-1] / 2, h, h)
        else:
            s.text(x, 1.5 * h, it[1], h, anchor="start")
        x += it[-1] + gap
    x = 2 * h + tol_w
    for (txt, mod, w), cw in zip(dtexts, cells[2:]):
        start = x + (cw - w) / 2
        s.text(start, 1.5 * h, txt, h, anchor="start")
        if mod:
            _modifier(s, mod, start + text_width(txt, h) + gap + 0.55 * h, h, h)
        x += cw
    if above:
        s.text(0, -0.5 * h, str(above), h, anchor="start")
    attach = anchor = None
    if leader:
        side = leader.get("side") or "left"
        attach = (0.0, h) if side == "left" else (W, h)
        dx, dy = leader["to"]
        tip = (attach[0] + float(dx), attach[1] + float(dy))
        out = -1 if side == "left" else 1
        pts = [attach]
        if (tip[0] - attach[0]) * out > 2 * h:
            pts.append((attach[0] + out * 2 * h, attach[1]))
        pts.append(tip)
        s.path(pts)
        s.arrow(pts[-2], tip, h)
        if e.get("all_around") and len(pts) == 3:
            s.circle(pts[1][0], pts[1][1], 0.4 * h)
        anchor = tip
    svg, Wt, Ht, pt = s.render(h / 5.0, {"frame": (0.0, 0.0), "attach": attach, "anchor": anchor})
    fx, fy = pt["frame"]
    return {"svg": svg, "width": Wt, "height": Ht, "frame": [fx, fy, W, H], "attach": pt.get("attach"),
            "anchor": pt.get("anchor")}


def datum_svg(letter, h=3.5, direction=(0.0, -1.0), stem=None):
    """Datum feature symbol (ISO 5459): the letter in a 2h square, joined to a filled triangle whose base
    lies on the feature line. direction: unit vector (paper, y down) from the line towards the box.
    Returns {svg, width, height, anchor (the middle of the triangle's base), box: [x, y, w, h]}."""
    if str(letter) not in DATUM_LETTERS:
        raise ToleranceError("datum letter %r: one capital letter, not I, O or Q" % (letter,))
    nx, ny = float(direction[0]), float(direction[1])
    L = math.hypot(nx, ny)
    if L < 1e-9:
        raise ToleranceError("datum symbol direction must not be zero")
    nx, ny = nx / L, ny / L
    tx, ty = -ny, nx
    s = _Svg(h / 10.0)
    side = h
    th = side * math.sqrt(3) / 2
    s.path([(tx * side / 2, ty * side / 2), (-tx * side / 2, -ty * side / 2), (nx * th, ny * th)], closed=True,
           fill=True)
    stem = h if stem is None else float(stem)
    ex, ey = nx * (th + stem), ny * (th + stem)
    s.path([(nx * th, ny * th), (ex, ey)])
    # the box's side faces the stem: its centre one half-size further along the stem's main axis
    if abs(nx) >= abs(ny):
        cx, cy = ex + math.copysign(h, nx), ey
    else:
        cx, cy = ex, ey + math.copysign(h, ny)
    s.path([(cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h)], closed=True)
    s.text(cx, cy + 0.5 * h, str(letter), h)
    svg, W, H, pt = s.render(h / 5.0, {"anchor": (0.0, 0.0), "box": (cx - h, cy - h)})
    return {"svg": svg, "width": W, "height": H, "anchor": pt["anchor"], "box": [pt["box"][0], pt["box"][1],
                                                                                 2 * h, 2 * h]}


def surface_svg(e, h=3.5, orient="up", leader=None, general=False, process=None):
    """Surface texture symbol (ISO 21920-1; the ISO 1302 symbol shops know): two legs at 60° to the surface,
    the short one 1.4 h and the long one 3 h high, a bar when material removal is required (the default on
    machined parts) or a circle when it is not permitted, the requirement under the top line, a method over
    it, the lay to the right. orient: "up" (on a horizontal line, from above) or "right" (on a vertical line,
    from the right; turned 90° so it reads from the right of the sheet). leader: {"to": (dx, dy)} stands it
    on a shoulder whose leader ends at that point (paper mm from the apex). general: adds the "(√)" of a
    general requirement. Returns {svg, width, height, anchor (the apex or the leader's tip)}."""
    if orient not in ("up", "right"):
        raise ToleranceError("orient is \"up\" or \"right\" (a surface seen from below or the left takes a leader)")
    req = "Ra %s" % _g(e["ra"]) if e.get("ra") is not None else "Rz %s" % _g(e["rz"])
    removal = e.get("removal")
    if removal is None:
        removal = True if (process or "cnc") in ("cnc", "turning") else "any"
    s = _Svg(h / 10.0, rotate=orient == "right" and not leader)
    H1, H2 = h * 5.0 / 3.5, 3.0 * h
    c60 = 1.0 / math.tan(math.radians(60))
    ax, ay = 0.0, 0.0
    tip = None
    if leader:
        dx, dy = (float(v) for v in leader["to"])
        tip = (dx, dy)
        out = 1 if dx >= 0 else -1
        shoulder = (out * 2.0 * h, 0.0)
        s.path([(-out * 0.6 * h, 0.0), shoulder, tip])
        s.arrow(shoulder, tip, h)
    s.path([(ax - c60 * H1, ay - H1), (ax, ay), (ax + c60 * H2, ay - H2)])
    if removal is True:
        s.path([(ax - c60 * H1, ay - H1), (ax + c60 * H1, ay - H1)])
    elif removal is False:
        s.circle(ax, ay - 2 * H1 / 3, H1 / 3)
    top0 = ax + c60 * H2
    tw = max(text_width(req, h), text_width(e.get("method") or "", h))
    gen_w = 2.2 * h if general else 0.0
    top1 = top0 + 0.25 * h + tw + gen_w + 0.4 * h
    s.path([(top0, ay - H2), (top1, ay - H2)])
    s.text(top0 + 0.25 * h, ay - H2 + 1.25 * h, req, h, anchor="start")
    if e.get("method"):
        s.text(top0 + 0.25 * h, ay - H2 - 0.35 * h, e["method"], h, anchor="start")
    if e.get("lay"):
        lx = ax + c60 * 0.9 * h + 0.5 * h
        if e["lay"] == "⊥":
            s.path([(lx, ay - 0.2 * h), (lx + 0.9 * h, ay - 0.2 * h)])
            s.path([(lx + 0.45 * h, ay - 0.2 * h), (lx + 0.45 * h, ay - 1.1 * h)])
        else:
            s.text(lx, ay - 0.2 * h, e["lay"], h, anchor="start")
    if general:
        gx = top0 + 0.25 * h + tw + 0.4 * h
        base = ay - H2 + 1.25 * h
        s.text(gx, base, "(", h, anchor="start")
        mx, my = gx + 0.85 * h, base
        s.path([(mx - 0.3 * h, my - 0.5 * h), (mx, my), (mx + 0.55 * h, my - 0.95 * h)])
        s.text(gx + 1.5 * h, base, ")", h, anchor="start")
    svg, W, H, pt = s.render(h / 5.0, {"anchor": tip if leader else (ax, ay)})
    return {"svg": svg, "width": W, "height": H, "anchor": pt["anchor"]}


# ------------------------------------------------------------------ stack-ups

def make_link(name, nominal, sign, lower, upper, fixed=False, dist="normal", cp=1.0, source="", part=None,
              desc=""):
    """One link of a 1D stack: a dimension of magnitude nominal ≥ 0 with deviations lower ≤ upper,
    entering the gap with sign +1 or −1. fixed: a bought-out part's tolerance (allocation leaves it)."""
    if sign not in (1, -1):
        raise ToleranceError("link %s: sign is +1 or -1" % name)
    nominal, lower, upper = float(nominal), float(lower), float(upper)
    if upper < lower:
        raise ToleranceError("link %s: the upper deviation %s is below the lower %s" % (name, upper, lower))
    if dist not in ("normal", "uniform"):
        raise ToleranceError("link %s: dist is normal or uniform" % name)
    return {"name": name, "nominal": nominal, "sign": sign, "lower": lower, "upper": upper, "fixed": bool(fixed),
            "dist": dist, "cp": float(cp or 1.0), "source": source, "part": part, "desc": desc}


def _contribution(l):
    a = l["sign"] * (l["nominal"] + l["lower"])
    b = l["sign"] * (l["nominal"] + l["upper"])
    return min(a, b), max(a, b)


def worst_case(links):
    return {"nominal": sum(l["sign"] * l["nominal"] for l in links),
            "min": sum(_contribution(l)[0] for l in links), "max": sum(_contribution(l)[1] for l in links)}


def _sigma(l):
    T = l["upper"] - l["lower"]
    return T / math.sqrt(12.0) if l.get("dist") == "uniform" else T / (6.0 * (l.get("cp") or 1.0))


def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _out_of_spec(mean, sigma, spec):
    lo, hi = (spec or (None, None))
    if sigma <= 0:
        return (1.0 if lo is not None and mean < lo else 0.0), (1.0 if hi is not None and mean > hi else 0.0)
    return (_phi((lo - mean) / sigma) if lo is not None else 0.0), \
        (1.0 - _phi((hi - mean) / sigma) if hi is not None else 0.0)


def rss(links, spec=None):
    """Root sum of squares: means at band centres, σᵢ = Tᵢ / (6 Cp) (uniform: Tᵢ / √12); mean ± 3σ and the
    shares below and above spec (a normal model)."""
    mean = sum(l["sign"] * (l["nominal"] + (l["lower"] + l["upper"]) / 2.0) for l in links)
    sig = math.sqrt(sum(_sigma(l) ** 2 for l in links))
    lo, hi = _out_of_spec(mean, sig, spec)
    return {"mean": mean, "sigma": sig, "min": mean - 3 * sig, "max": mean + 3 * sig, "out_low": lo, "out_high": hi}


def monte_carlo(links, n=100000, seed=1, spec=None, use_numpy=None):
    """Sampled stack: each link normal (mean at its band centre, σ as in rss) or uniform over its band.
    numpy when it is there (FreeCAD's python has it), else pure Python; use_numpy=False forces the latter."""
    n = int(n)
    if n < 100:
        raise ToleranceError("Monte Carlo needs at least 100 samples")
    np = None
    if use_numpy is not False:
        try:
            import numpy as np
        except ImportError:
            np = None
    if np is not None:
        rng = np.random.default_rng(seed)
        tot = np.zeros(n)
        for l in links:
            lo, hi = l["nominal"] + l["lower"], l["nominal"] + l["upper"]
            x = rng.uniform(lo, hi, n) if l.get("dist") == "uniform" else rng.normal((lo + hi) / 2.0, _sigma(l), n)
            tot += l["sign"] * x
        vals = sorted(tot.tolist())
    else:
        rnd = random.Random(seed)
        vals = [0.0] * n
        for l in links:
            lo, hi = l["nominal"] + l["lower"], l["nominal"] + l["upper"]
            mid, sd, sg = (lo + hi) / 2.0, _sigma(l), l["sign"]
            if l.get("dist") == "uniform":
                for i in range(n):
                    vals[i] += sg * rnd.uniform(lo, hi)
            else:
                for i in range(n):
                    vals[i] += sg * rnd.gauss(mid, sd)
        vals.sort()
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    lo_s, hi_s = (spec or (None, None))
    return {"n": n, "seed": seed, "mean": mean, "sigma": math.sqrt(var), "min": vals[int(0.00135 * n)],
            "max": vals[min(n - 1, int(0.99865 * n))],
            "out_low": sum(1 for v in vals if lo_s is not None and v < lo_s) / float(n),
            "out_high": sum(1 for v in vals if hi_s is not None and v > hi_s) / float(n)}


def contributions(links):
    """Each link's share of the worst-case range and of the RSS variance."""
    T = [l["upper"] - l["lower"] for l in links]
    V = [_sigma(l) ** 2 for l in links]
    st, sv = sum(T) or 1.0, sum(V) or 1.0
    return [{"name": l["name"], "wc": t / st, "rss": v / sv} for l, t, v in zip(links, T, V)]


def _within(lo, hi, spec):
    smin, smax = spec
    return (smin is None or lo >= smin - 1e-9) and (smax is None or hi <= smax + 1e-9)


def _spec(spec):
    if spec is None:
        return None
    lo, hi = spec
    return (None if lo is None else float(lo), None if hi is None else float(hi))


def analyse(links, spec=None, methods=("worst_case", "rss"), n=100000, seed=1, max_out=0.0027):
    """A stack by every method asked for, with verdicts against spec (min, max); either side may be None."""
    if not links:
        raise ToleranceError("a stack needs at least one link")
    spec = _spec(spec)
    out = {"links": links, "spec": spec, "share": contributions(links), "ok": {}}
    out["worst_case"] = wc = worst_case(links)
    out["nominal"] = wc["nominal"]
    if spec:
        out["ok"]["worst_case"] = _within(wc["min"], wc["max"], spec)
    if "rss" in methods:
        out["rss"] = r = rss(links, spec)
        if spec:
            out["ok"]["rss"] = _within(r["min"], r["max"], spec)
    if "monte_carlo" in methods:
        out["monte_carlo"] = m = monte_carlo(links, n, seed, spec)
        if spec:
            out["ok"]["monte_carlo"] = m["out_low"] + m["out_high"] <= max_out + 1e-12
    return out


def _round_to(x, step):
    return math.floor(x / step + 0.5 + 1e-9) * step


def allocate(links, spec, method="worst_case", grades=None, round_to=0.01):
    """Equal-precision allocation: the coarsest ISO 286 grade that, given to every link that is not fixed
    as a symmetric ± tolerance, fits the band of spec (worst case: the sum of the bands; rss: 6σ); then the
    nominal shift that centres the stack in spec, put on the adjustable link with the largest nominal and
    rounded to round_to when that still fits. Returns {grade, links, band, used, coarser, shift, move,
    result, ok}."""
    spec = _spec(spec)
    if not spec or spec[0] is None or spec[1] is None:
        raise ToleranceError("allocation needs a target band with min and max")
    if method not in ("worst_case", "rss"):
        raise ToleranceError("allocation method is worst_case or rss")
    band = spec[1] - spec[0]
    adj = [l for l in links if not l.get("fixed")]
    if not adj:
        raise ToleranceError("every link is fixed: nothing to allocate")

    def with_grade(g):
        new = []
        for l in links:
            if l.get("fixed"):
                new.append(dict(l))
            else:
                half = standards.it_um(g, max(l["nominal"], 0.01)) / 2000.0
                new.append(dict(l, lower=-half, upper=half))
        return new

    def used(ls):
        return sum(l["upper"] - l["lower"] for l in ls) if method == "worst_case" else \
            6.0 * math.sqrt(sum(_sigma(l) ** 2 for l in ls))
    grades = sorted(grades or standards.IT, reverse=True)
    fixed_used = used([l for l in links if l.get("fixed")]) if method == "worst_case" else None
    best, coarser = None, None
    for g in grades:
        ls = with_grade(g)
        u = used(ls)
        if u <= band + 1e-9:
            best = (g, ls, u)
            break
        coarser = (g, u)
    out = {"method": method, "band": band, "fixed_used": fixed_used, "ok": False}
    if coarser:
        out["coarser"] = {"grade": coarser[0], "used": coarser[1]}
    if not best:
        out["why"] = ("even IT%d needs %s of the %s band%s: tighten or change a fixed part, or widen the target" % (
            min(grades), _g(used(with_grade(min(grades))), 3), _g(band, 3),
            ", %s of it for the fixed links" % _g(fixed_used, 3) if fixed_used else ""))
        return out
    g, ls, u = best
    res = worst_case(ls) if method == "worst_case" else rss(ls, spec)
    mid_now = (res["min"] + res["max"]) / 2.0
    shift = (spec[0] + spec[1]) / 2.0 - mid_now
    out.update(grade=g, links=ls, used=u, shift=shift)
    target = max((l for l in ls if not l.get("fixed")), key=lambda l: l["nominal"])
    final = ls
    if abs(shift) > 1e-9:
        exact = target["nominal"] + target["sign"] * shift
        cand = [_round_to(exact, round_to), _round_to(exact, round_to / 2.0), exact] if round_to else [exact]
        for nom in cand:
            trial = [dict(l, nominal=nom) if l["name"] == target["name"] else l for l in ls]
            r2 = worst_case(trial) if method == "worst_case" else rss(trial, spec)
            if _within(r2["min"], r2["max"], spec):
                final, res = trial, r2
                out["move"] = {"link": target["name"], "from": target["nominal"], "to": round(nom, 6)}
                break
    out["links"], out["result"] = final, res
    out["ok"] = _within(res["min"], res["max"], spec)
    return out


def _pct(x):
    return "%.0f %%" % (100 * x) if x >= 0.0995 else ("%.1f %%" % (100 * x) if x >= 0.000995 else "%.2g %%" % (100 * x))


def stack_text(name, result, direction=None, rev=None, closure=None, alloc=None):
    """The stack-up as the stackup tool and the release README show it."""
    links, spec, ok = result["links"], result.get("spec"), result.get("ok") or {}

    def lim(lo, hi):
        return "%.3f … %.3f" % (lo, hi)
    verdict = " · ".join("%s %s" % ({"worst_case": "worst case", "rss": "RSS", "monte_carlo": "Monte Carlo"}[k],
                                    "✓" if ok[k] else "✗") for k in ("worst_case", "rss", "monte_carlo") if k in ok)
    target = ("  (%s … %s)" % tuple("" if x is None else _g(x, 3) for x in spec)) if spec else ""
    lines = ["STACKUP %s%s%s%s%s" % (name, "  along %s" % direction if direction else "", "  %s" % rev if rev else "",
                                     "  " + verdict if verdict else "", target)]
    lines.append(" #  %-9s %-36s %9s  %-11s %s" % ("part", "link", "nominal", "tolerance", "from"))
    for i, l in enumerate(links, 1):
        lines.append("%2d  %-9s %-36s %+9.3f  %-11s %s" % (
            i, (l.get("part") or "")[:9], (l.get("desc") or l["name"])[:36], l["sign"] * l["nominal"],
            tolerance_text(l["upper"], l["lower"]), l.get("source") or ""))
    g = "gap        %.3f" % result["nominal"]
    if closure is not None:
        g += " · chain closes %s (model gap %.3f)" % ("✓" if abs(closure - result["nominal"]) < 1e-3 else "✗",
                                                     closure)
    lines.append(g)
    wc = result["worst_case"]
    lines.append("worst case %s   %s" % (lim(wc["min"], wc["max"]), _side(wc["min"], wc["max"], spec)))
    if "rss" in result:
        r = result["rss"]
        lines.append("RSS ±3σ    %s   %s%s" % (lim(r["min"], r["max"]), _side(r["min"], r["max"], spec),
                                              _outs(r, spec) + " (normal, Cp 1)" if spec else ""))
    if "monte_carlo" in result:
        m = result["monte_carlo"]
        lines.append("Monte Carlo %s (0.135 … 99.865 %%, %d samples)%s" % (lim(m["min"], m["max"]), m["n"],
                                                                         _outs(m, spec) if spec else ""))
    sh = sorted(result["share"], key=lambda s: -s["wc"])
    lines.append("share      WC %s     RSS %s" % (
        " · ".join("%s %.0f %%" % (s["name"], 100 * s["wc"]) for s in sh),
        " · ".join("%s %.0f %%" % (s["name"], 100 * s["rss"]) for s in sorted(sh, key=lambda s: -s["rss"]))))
    gen = [l["name"] for l in links if "not specified" in (l.get("source") or "")]
    if gen:
        lines.append("note       %s on the general tolerance: specify %s if %s critical" % (
            ", ".join(gen), "them" if len(gen) > 1 else "it", "they are" if len(gen) > 1 else "it is"))
    if alloc:
        lines += allocation_lines(alloc)
    return "\n".join(lines)


def _side(lo, hi, spec):
    if not spec:
        return ""
    bad = []
    if spec[0] is not None and lo < spec[0] - 1e-9:
        bad.append("below %s" % _g(spec[0], 3))
    if spec[1] is not None and hi > spec[1] + 1e-9:
        bad.append("above %s" % _g(spec[1], 3))
    return ("✗ " + " and ".join(bad)) if bad else "✓"


def _outs(r, spec):
    parts = []
    if spec[0] is not None and r["out_low"] > 1e-6:
        parts.append("≈ %s below %s" % (_pct(r["out_low"]), _g(spec[0], 3)))
    if spec[1] is not None and r["out_high"] > 1e-6:
        parts.append("≈ %s above %s" % (_pct(r["out_high"]), _g(spec[1], 3)))
    return ("  " + ", ".join(parts)) if parts else ""


def allocation_lines(a):
    if not a.get("grade"):
        return ["allocate   ✗ %s" % a.get("why", "no grade fits")]
    fixed = [l for l in a["links"] if l.get("fixed")]
    adj = [l for l in a["links"] if not l.get("fixed")]
    s = "allocate   %sIT%d: %s → %s ≤ %s ✓" % (
        ("%s fixed (%s of the %s band) · " % (", ".join(l["name"] for l in fixed), _g(a["fixed_used"], 3),
                                               _g(a["band"], 3))) if fixed and a.get("fixed_used") is not None else "",
        a["grade"], ", ".join("%s %s" % (l["name"], tolerance_text(l["upper"], l["lower"])) for l in adj),
        _g(a["used"], 3), _g(a["band"], 3))
    if a.get("coarser"):
        s += " (IT%d %s ✗)" % (a["coarser"]["grade"], _g(a["coarser"]["used"], 3))
    out = [s]
    if a.get("move"):
        mv = a["move"]
        out.append("           centre the mean: %s %s → %s → %s %s %s" % (
            mv["link"], _g(mv["from"], 3), _g(mv["to"], 3), "worst case" if a["method"] == "worst_case" else "RSS",
            "%.3f … %.3f" % (a["result"]["min"], a["result"]["max"]), "✓" if a["ok"] else "✗"))
    return out


# ------------------------------------------------------------------ bolted joints

_CLEAR = {"fine": "fine", "close": "fine", "normal": "normal", "medium": "normal", "coarse": "coarse",
          "loose": "coarse"}


def joint(size, clearance="normal", screw_d=None):
    """The bolted-joint rule, position tolerance for bolted holes at MMC: H = the smallest ISO 273 clearance
    hole (H12 / H13 / H14, so the nominal), F = the largest screw (its nominal major Ø, conservative, unless
    screw_d is given). T = H − F: each part's zone with a floating fastener, the two parts' zones together
    with a fixed one."""
    s = standards.norm_size(size)
    key = _CLEAR.get(str(clearance or "normal").strip().lower())
    if not key:
        raise ToleranceError("clearance is fine, normal or coarse (ISO 273), got %r" % (clearance,))
    H = standards.clearance(s, key)
    cls = standards.CLEARANCE_CLASS[key]
    lo, hi = standards.tolerance(H, cls)
    F = float(screw_d) if screw_d is not None else float(s[1:])
    return {"size": s, "clearance": key, "hole": H, "hole_class": cls, "hole_min": lo, "hole_max": hi, "screw": F,
            "T": round(lo - F, 4)}


def equivalent_position(tx, ty=None):
    """The ⌀ position zone equal to ± tx (and ± ty) location tolerances: the square's diagonal, 2√(tx² + ty²)."""
    tx = abs(float(tx))
    ty = tx if ty is None else abs(float(ty))
    return 2.0 * math.hypot(tx, ty)


def joint_check(t_a, t_b, size, clearance="normal", mount="floating", screw_d=None):
    """(ok, value text, detail) for two mating hole patterns with position zones t_a and t_b (⌀ mm at MMC)."""
    r = joint(size, clearance, screw_d)
    if mount not in ("floating", "fixed"):
        raise ToleranceError("mount is floating (clearance holes in both parts) or fixed (one part tapped or "
                             "pressed)")
    T = r["T"]
    if mount == "floating":
        ok = t_a <= T + 1e-9 and t_b <= T + 1e-9
        return ok, "⌀%s and ⌀%s, each ≤ ⌀%s" % (_g(t_a), _g(t_b), _g(T)), \
            "floating %s in Ø%s %s holes: T = %s − %s per part" % (r["size"], _g(r["hole"]), r["hole_class"],
                                                                   _g(r["hole_min"]), _g(r["screw"]))
    ok = t_a + t_b <= T + 1e-9
    return ok, "⌀%s + ⌀%s = ⌀%s ≤ ⌀%s" % (_g(t_a), _g(t_b), _g(t_a + t_b), _g(T)), \
        "fixed %s: T₁ + T₂ = %s − %s (use Ⓟ on the tapped holes)" % (r["size"], _g(r["hole_min"]), _g(r["screw"]))


def joint_report(size, clearance="normal", mount=None, screw_d=None):
    r = joint(size, clearance, screw_d)
    lines = ["%s screw in %s clearance holes Ø%s %s (%s … %s, ISO 273)" % (
                 r["size"], r["clearance"], _g(r["hole"]), r["hole_class"], _g(r["hole_min"], 3), _g(r["hole_max"], 3)),
             "  F = Ø%s (the screw's nominal major Ø%s) · H = Ø%s (the smallest hole) · T = H − F = %s" % (
                 _g(r["screw"]), ", conservative" if screw_d is None else " as given", _g(r["hole_min"]), _g(r["T"]))]
    if mount in (None, "floating"):
        lines.append("  floating (clearance holes in both parts): ⌖ ⌀%s Ⓜ on each part's pattern" % _g(r["T"]))
    if mount in (None, "fixed"):
        lines.append("  fixed (one part tapped or pressed): the two zones share ⌀%s, e.g. ⌀%s Ⓜ on the clearance "
                     "holes and ⌀%s Ⓟ<thickness of the other part> on the tapped ones" % (
                         _g(r["T"]), _g(r["T"] / 2), _g(r["T"] / 2)))
    lines.append("  holes on the general tolerance: ±t on x and y is a ⌀%s·t zone (ISO 2768-m ±0.2 → ⌀%s)" % (
        _g(2 * math.sqrt(2), 3), _g(equivalent_position(0.2), 2)))
    lines.append("  the two patterns must also line up as modelled: an align check in checks.json (max_offset)")
    return "\n".join(lines)


# ------------------------------------------------------------------ lookup cards

def gdt_card(name=None):
    """lookup_standard(gdt=…): one characteristic's symbol, zone, datum rule and modifiers with typical values,
    or (no name, 'all') the table of all 14 and the modifiers."""
    if not name or str(name).strip().lower() in ("all", "list", "*", "table"):
        lines = ["ISO 1101 geometrical characteristics (lookup_standard gdt='position' for one)"]
        for cat in CATEGORY_ORDER:
            lines.append("  %-12s %s" % (cat, " · ".join("%s %s" % (c["symbol"], n.replace("_", " "))
                                                       for n, c in CHARACTERISTICS.items() if c["category"] == cat)))
        lines += ["  modifiers    Ⓜ maximum material (ISO 2692, features of size: bonus tolerance) · "
                  "Ⓛ least material · Ⓟ projected zone (tapped and press-fit holes) · Ⓔ envelope (on a size, "
                  "ISO 14405-1) · Ⓕ free state",
                  "  datums       A primary (3 points: what the part sits on) · B secondary (2: what locates it) · C "
                  "tertiary (1: what stops rotation); letters without I, O and Q"]
        return "\n".join(lines)
    t = characteristic(name)
    if t == "size":
        return ("size: '±0.05', '+0.1/0', limits 10.00 … 10.05 or an ISO 286 class ('H7', 'h9', lookup_standard "
                "fit='H7' with d); under ISO 8015 a fit needs Ⓔ, the envelope requirement (ISO 14405-1)")
    if t == "surface":
        return surface_report()
    c = CHARACTERISTICS[t]
    datum_words = {"none": "never (a form tolerance)", "required": "at least one",
                   "axis": "a datum axis as the primary datum", "usual": "usually A B C (without them the features "
                   "are located only to each other)", "optional": "optional: with them the profile also locates"}
    lines = ["%s %s (ISO 1101, %s): %s" % (c["symbol"], t.replace("_", " "), c["category"], c["words"]),
             "  applies to   %s" % ", ".join(_kind_words(k) for k in c["on"]),
             "  zone         %s" % ZONES[t],
             "  datums       %s" % datum_words[c["datums"]],
             "  Ⓜ / Ⓛ        %s" % ("on features of size (ISO 2692)" if c["mmr"] else "not allowed"),
             "  typical      %s (rule)" % TYPICAL[t]]
    if t == "position":
        lines.append("  basic dimensions (framed) locate the feature from the datums; bolted joints: lookup_standard "
                     "joint='M5' gives the zone")
    if t == "angularity":
        lines.append("  the angle to the datum is a basic (framed) dimension")
    return "\n".join(lines)


def surface_report(spec=None):
    """lookup_standard(surface=…): a requirement ('Ra 0.8') against every process, a process ('turning') with
    what it reaches, or the series and the fit rule."""
    s = str(spec or "").strip()
    m = re.match(r"^(ra|rz)\s*([\d.]+)$", s, re.I)
    procs = sorted(standards.CAPABILITY)
    lines = []
    if m:
        kind, v = m.group(1).capitalize(), float(m.group(2))
        if kind == "Ra":
            in_series = any(abs(v - x) < 1e-9 for x in standards.RA_SERIES)
            lines.append("Ra %s µm (ISO 21920-1)%s" % (_g(v), "" if in_series else "  ⚠ not in the preferred series: "
                                                                                  "use Ra %s" % _g(min(
                                                                                      standards.RA_SERIES,
                                                                                      key=lambda x: abs(x - v)))))
            for p in procs:
                level, note = standards.capability(p, "ra", v)
                std, fine, op = standards.CAPABILITY[p]["ra"]
                lines.append("  %-8s %-9s (standard Ra %s · finest Ra %s%s)%s" % (
                    p, level, _g(std), _g(fine), ", then " + op if op else "", ("  " + note) if note else ""))
        else:
            lines.append("Rz %s µm (ISO 21920-1): Rz is roughly 4 to 7 × Ra on machined surfaces (rule); the process "
                         "table is in Ra (lookup_standard surface='Ra 1.6')" % _g(v))
    elif s.lower() in standards.CAPABILITY:
        std, fine, op = standards.CAPABILITY[s.lower()]["ra"]
        lines.append("%s: standard Ra %s µm, finest Ra %s with care%s (rule)" % (
            s.lower(), _g(std), _g(fine), ", finer needs " + op if op else "; finer is not reachable by the process"))
    elif s:
        raise ToleranceError("surface %r: give a requirement ('Ra 1.6', 'Rz 6.3') or a process (%s)" % (
            spec, ", ".join(procs)))
    else:
        lines.append("Surface texture (ISO 21920-1, which replaced ISO 1302): requirements in µm, upper limits")
        for p in procs:
            std, fine, op = standards.CAPABILITY[p]["ra"]
            lines.append("  %-8s standard Ra %s · finest Ra %s%s" % (
                p, _g(std), _g(fine), ", then " + op if op else ""))
    lines.append("  preferred Ra series: %s" % " · ".join(_g(x) for x in standards.RA_SERIES))
    lines.append("  fit surfaces (rule): %s" % " · ".join(
        "IT%d%s Ra ≤ %s" % (g, " and finer" if i == 0 else "", _g(ra)) for i, (g, ra) in enumerate(standards.FIT_RA)))
    lines.append("  cnc and turning drawings already note the general requirement: SURFACE ROUGHNESS Ra 3.2 UNLESS "
                 "STATED")
    return "\n".join(lines)


# ------------------------------------------------------------------ inspection

INSPECTION_COLUMNS = ("Balloon", "Characteristic", "Requirement", "Nominal", "Lower limit", "Upper limit", "Unit",
                      "Datums", "Feature", "View", "Source", "Measured", "Gauge", "Result")


def characteristic_item(e, feature=None, datums=None, nominal=None, view="", key=None):
    """A scheme entry as an inspection item: what it is, as drawn, with its limits."""
    t = e["type"]
    item = {"key": key or e["id"], "requirement": entry_text(e, feature, datums, nominal), "view": view,
            "feature": e.get("feature") or " ↔ ".join(e.get("between") or []), "source": e.get("source", ""),
            "datums": " ".join(datum_ref_text(d) for d in e.get("datums") or []), "unit": "mm"}
    if t == "size":
        item["name"] = "size" if e.get("feature") else "distance"
        item["nominal"] = nominal
        if nominal is not None:
            up, lo = size_deviations(e, nominal)
            item["lower"], item["upper"] = round(nominal + lo, 6), round(nominal + up, 6)
    elif t == "surface":
        item.update(name="surface texture", unit="µm", upper=e.get("ra", e.get("rz")))
    else:
        item.update(name=t.replace("_", " "), upper=e["tol"])
    return item


def dimension_item(key, text, nominal, general=None, upper=None, lower=None, view="", name="dimension",
                   feature="", source=""):
    """A drawn dimension or callout as an inspection item; without its own deviations it takes the
    general tolerance (ISO 2768-1 at its nominal)."""
    item = {"key": key, "name": name, "requirement": text, "nominal": nominal, "view": view, "feature": feature,
            "source": source, "unit": "mm", "datums": ""}
    if nominal is not None:
        if upper is None and lower is None and general:
            v = standards.general_tolerance(general, nominal)
            upper, lower = v, -v
            item["source"] = source or "general tolerance %s" % general
        if upper is not None and lower is not None:
            item["lower"], item["upper"] = round(nominal + lower, 6), round(nominal + upper, 6)
    return item


def note_item(key, text, source="notes"):
    return {"key": key, "name": "note", "requirement": text, "source": source, "unit": ""}


def inspection_rows(items, numbers):
    """Rows (dicts by INSPECTION_COLUMNS) in balloon order; numbers: {item key: balloon number}."""
    rows = []
    for it in sorted(items, key=lambda i: numbers[i["key"]]):
        def num(v):
            return "" if v is None else _g(v, 4)
        rows.append({"Balloon": numbers[it["key"]], "Characteristic": it.get("name", ""),
                     "Requirement": it.get("requirement", ""), "Nominal": num(it.get("nominal")),
                     "Lower limit": num(it.get("lower")), "Upper limit": num(it.get("upper")),
                     "Unit": it.get("unit", ""), "Datums": it.get("datums", ""), "Feature": it.get("feature", ""),
                     "View": it.get("view", ""), "Source": it.get("source", ""), "Measured": "", "Gauge": "",
                     "Result": ""})
    return rows


def inspection_csv(rows):
    """The first-article inspection list as CSV text: UTF-8 with a byte-order mark so spreadsheet apps show
    the GD&T symbols."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(INSPECTION_COLUMNS)
    for r in rows:
        w.writerow([r.get(c, "") for c in INSPECTION_COLUMNS])
    return "﻿" + buf.getvalue()


def assign_balloons(register, keys):
    """Stable balloon numbers. register: {"next": n, "numbers": {key: n}, "retired": {key: n}} or None; keys:
    the characteristics on the sheet now, in drawing order. A key keeps its number, a key that comes back gets
    its old one, a new key takes the next free number, and a key that is gone is retired: its number is never
    given to another. Returns ({key: number}, new register)."""
    reg = register if isinstance(register, dict) else {}
    nums = {k: int(v) for k, v in (reg.get("numbers") or {}).items()}
    retired = {k: int(v) for k, v in (reg.get("retired") or {}).items()}
    if len(set(keys)) != len(keys):
        dup = sorted(set(k for k in keys if list(keys).count(k) > 1))
        raise ToleranceError("characteristic keys must be unique on a sheet: %s twice" % ", ".join(dup))
    nxt = max([int(reg.get("next") or 1)] + [v + 1 for v in list(nums.values()) + list(retired.values())])
    out = {}
    for k in keys:
        if k in nums:
            out[k] = nums[k]
        elif k in retired:
            out[k] = retired.pop(k)
        else:
            out[k] = nxt
            nxt += 1
    for k, v in nums.items():
        if k not in out:
            retired[k] = v
    return out, {"next": nxt, "numbers": out, "retired": retired}
