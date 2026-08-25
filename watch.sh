#!/usr/bin/env bash
# Launch the Copilot Agent Monitor in your terminal.
# Usage:
#   ./watch.sh                # terminal dashboard + web dashboard (port 8787)
#   MONITOR_PORT=9000 ./watch.sh
#   ./watch.sh --no-web       # terminal only
#   ./watch.sh --no-terminal  # web only (run in background)
cd "$(dirname "$0")" || exit 1
exec python3 monitor.py "$@"
