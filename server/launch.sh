#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Start the FreeCAD MCP server with FreeCAD's bundled Python, so FreeCAD is the
# only thing to install. Order: $FREECAD_PYTHON, $FREECAD_APP, /Applications/FreeCAD.app,
# other /Applications/FreeCAD*.app (newest first), the same in ~/Applications, then
# python3/python on PATH (the server is stdlib-only and finds FreeCAD itself for its
# workers). Each candidate is probed before use. On macOS /usr/bin/python3 is never
# tried: without Xcode it is a stub that pops an install dialog or fails on an
# unaccepted licence. FREECAD_APP may name FreeCAD.app, its bundled python or a
# freecadcmd binary; a value the user set is never replaced (a trailing / is dropped).
# Only when it is empty and a FreeCAD bundle is chosen is FREECAD_APP set — and exported —
# so the server's workers use that same FreeCAD.
PATH="${PATH:+$PATH:}/usr/bin:/bin"         # GUI apps may start us with a bare PATH
DIR=$(cd "$(dirname "$0")" && pwd)
TRIED=""
PY=""
OS=$(uname -s)
NL='
'
OLDIFS=$IFS

works() {   # works <python> — a Python 3.8+ that actually runs
    [ -n "$1" ] || return 1
    case "$OS:$1" in Darwin:/usr/bin/python*) return 1 ;; esac      # Apple's stub: never run it
    if [ ! -x "$1" ]; then
        TRIED="$TRIED
  $1 (not found)"
        return 1
    fi
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' </dev/null >/dev/null 2>&1 && return 0
    TRIED="$TRIED
  $1 (does not run, or is older than Python 3.8)"
    return 1
}

bundles() { # bundles <dir> — FreeCAD.app first, then other FreeCAD*.app newest version first
    [ -d "$1/FreeCAD.app" ] && printf '%s\n' "$1/FreeCAD.app"
    for app in "$1"/FreeCAD*.app; do
        [ -d "$app" ] && [ "$app" != "$1/FreeCAD.app" ] || continue
        v=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist" 2>/dev/null)
        [ -n "$v" ] || v=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Contents/Info.plist" 2>/dev/null)
        printf '%s\t%s\n' "${v:-0}" "$app"
    done | sort -t. -k1,1nr -k2,2nr -k3,3nr | cut -f2-
}

while :; do                                # '/Applications/FreeCAD_dev.app/' (tab completion) → no slash
    case "$FREECAD_APP" in */) FREECAD_APP=${FREECAD_APP%/} ;; *) break ;; esac
done
APP_PY=""                                   # the python of the FreeCAD that FREECAD_APP names, if any
case "$FREECAD_APP" in
    "") ;;
    *.app) APP_PY=$FREECAD_APP/Contents/Resources/bin/python ;;
    *.app/Contents/*) APP_PY=${FREECAD_APP%%.app/Contents/*}.app/Contents/Resources/bin/python ;;  # its python/freecadcmd
    */python*) APP_PY=$FREECAD_APP ;;
esac                                        # (a freecadcmd outside a bundle cannot run the server: kept for the workers)

if works "$FREECAD_PYTHON"; then
    case "$FREECAD_PYTHON" in
        *.app/Contents/Resources/bin/*) [ -n "$FREECAD_APP" ] || FREECAD_APP=${FREECAD_PYTHON%%/Contents/Resources/bin/*} ;;
    esac
    PY=$FREECAD_PYTHON
elif [ -n "$APP_PY" ] && works "$APP_PY"; then
    PY=$APP_PY
else
    APPS=$(bundles /Applications; bundles "$HOME/Applications")
    IFS=$NL; set -f                         # one bundle path per line; paths may hold spaces
    for app in $APPS; do
        if works "$app/Contents/Resources/bin/python"; then
            PY=$app/Contents/Resources/bin/python
            [ -n "$FREECAD_APP" ] || FREECAD_APP=$app     # never replace the user's choice
            break
        fi
    done
    IFS=$OLDIFS; set +f
    if [ -z "$PY" ]; then                   # every python3/python on PATH, in order (not only the first)
        IFS=:; set -f
        for d in $PATH; do
            for c in python3 python; do
                if [ -n "$d" ] && [ -x "$d/$c" ] && works "$d/$c"; then PY=$d/$c; break 2; fi
            done
        done
        IFS=$OLDIFS; set +f
    fi
fi

if [ -z "$PY" ]; then
    cat >&2 <<EOF
freecad-mcp: no working Python 3.8+ found to run the FreeCAD MCP server.
Install FreeCAD 1.x into /Applications (https://www.freecad.org/downloads.php) — the
server runs on FreeCAD's bundled Python — or set FREECAD_PYTHON to a Python 3.8+
interpreter (and FREECAD_APP to FreeCAD.app) in the MCP server's environment.
Tried:${TRIED:-
  (no candidates found)}
EOF
    exit 1
fi
[ -n "$FREECAD_APP" ] && export FREECAD_APP
exec "$PY" -I "$DIR/freecad_mcp.py" "$@"
