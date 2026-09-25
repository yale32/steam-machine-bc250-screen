#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# One-time setup: Python venv + dependencies + the screen driver library.
#
# Nothing is installed system-wide, so this works on SteamOS with the root
# filesystem left read-only. Safe to re-run.
#
set -euo pipefail
cd "$(dirname "$0")"

# Driver library (GPL-3.0), fetched rather than vendored. Pinned to the commit
# the RevA / 3.5" code in monitor.py was written against.
LIB_URL=https://github.com/mathoudebine/turing-smart-screen-python.git
LIB_REF=2b33ab4f00a096916dd6a1174441a53a7ec33b03
LIB_DIR=vendor/turing-smart-screen-python

[ -d .venv ] || "${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -d "$LIB_DIR/library" ]; then
    mkdir -p "$LIB_DIR"
    git -C "$LIB_DIR" init --quiet
    git -C "$LIB_DIR" remote add origin "$LIB_URL"
    # Only the driver code is needed; the full repo is hundreds of MB of theme previews.
    git -C "$LIB_DIR" sparse-checkout set library
    git -C "$LIB_DIR" fetch --quiet --depth 1 --filter=blob:none origin "$LIB_REF"
    git -C "$LIB_DIR" checkout --quiet FETCH_HEAD
fi

mkdir -p run
echo "setup complete:"
.venv/bin/python -c "import serial, PIL, numpy, psutil; print('  deps ok: pyserial', serial.__version__, '| Pillow', PIL.__version__, '| numpy', numpy.__version__, '| psutil', psutil.__version__)"
echo "  driver library: $(git -C "$LIB_DIR" rev-parse --short HEAD)"
