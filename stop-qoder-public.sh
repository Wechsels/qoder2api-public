#!/usr/bin/env bash
# ===========================================================
#  qoder2api-public supervisor stop (thin wrapper)
#
#  Usage: ./stop-qoder-public.sh [--config PATH]
#  (also accepts start|restart|status|run for convenience;
#   the .sh/.bat pair is symmetric on purpose)
# ===========================================================
set -u

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd -- "$(dirname -- "$SRC")" && pwd)"
SCRIPT="$HERE/qoder_public_runtime.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[ERROR] $SCRIPT not found."
    exit 1
fi

usable() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1
}

PYEXE="${QDP_PUBLIC_PYTHON:-}"
if [ -z "$PYEXE" ]; then
    for cand in python3 python "$HERE/python/bin/python3" "$HERE/python/bin/python"; do
        if usable "$cand"; then
            PYEXE="$cand"
            break
        fi
    done
fi

if [ -z "$PYEXE" ]; then
    echo "[ERROR] No usable Python 3.9+ found."
    exit 1
fi

if [ "$#" -lt 1 ]; then
    exec "$PYEXE" "$SCRIPT" stop
fi
exec "$PYEXE" "$SCRIPT" "$@"
