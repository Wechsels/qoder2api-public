#!/usr/bin/env bash
# ===========================================================
#  qoder2api-public supervisor (thin wrapper)
#
#  Usage: ./start-qoder-public.sh start|stop|restart|status|run
#
#  All real logic lives in qoder_public_runtime.py; this file
#  only locates Python 3.9+ and forwards arguments.
# ===========================================================
set -u

if [ -z "${BASH_VERSION:-}" ]; then
    # `sh start-qoder-public.sh` under dash: hand ourselves to bash
    exec bash "$0" "$@"
fi

if [ "$#" -lt 1 ]; then
    echo "Usage: $(basename "$0") start|stop|restart|status|run [--config PATH]"
    exit 2
fi

SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd -- "$(dirname -- "$SRC")" && pwd)"
SCRIPT="$HERE/qoder_public_runtime.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[ERROR] $SCRIPT not found."
    exit 1
fi

# A candidate is usable when it exists, is executable and is Python >= 3.9
usable() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1
}

PYEXE="${QDP_PUBLIC_PYTHON:-}"

# 1) explicit interpreter
if [ -n "$PYEXE" ]; then
    if [ ! -x "$PYEXE" ] || ! usable "$PYEXE"; then
        echo "[ERROR] QDP_PUBLIC_PYTHON is set to '$PYEXE', but that is not"
        echo "        an executable Python 3.9+ interpreter."
        exit 1
    fi
fi

# 2) python3 / python on PATH, 3) bundled portable runtime
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
    echo
    echo "Options:"
    echo "  1. Install Python 3.9+ (apt install python3 / dnf install python3)"
    echo "  2. Or point the script at an interpreter explicitly:"
    echo "       QDP_PUBLIC_PYTHON=/usr/bin/python3.12 ./start-qoder-public.sh start"
    exit 1
fi

exec "$PYEXE" "$SCRIPT" "$@"
