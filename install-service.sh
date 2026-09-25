#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Install monitor.py as a systemd *user* service so it starts at login and is
# restarted if it exits. Needs no root and touches nothing outside $HOME.
#
#   ./install-service.sh            install, enable and start
#   ./install-service.sh --remove   stop, disable and delete
#
set -euo pipefail
cd "$(dirname "$0")"

UNIT=bc250-screen.service
DEST="$HOME/.config/systemd/user/$UNIT"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$DEST"
    systemctl --user daemon-reload
    echo "removed $UNIT"
    exit 0
fi

[ -x .venv/bin/python ] || { echo "run ./setup.sh first" >&2; exit 1; }

mkdir -p "$(dirname "$DEST")"
sed "s|@DIR@|$PWD|g" bc250-screen.service > "$DEST"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
echo "installed and started $UNIT"
echo "  logs:    journalctl --user -u $UNIT -f"
echo "  status:  systemctl --user status $UNIT"
echo "  stop:    systemctl --user stop $UNIT"
