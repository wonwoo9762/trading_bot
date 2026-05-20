#!/usr/bin/env bash
# Optional: run the scheduler manually (same as launchd, but from a terminal).
# launchd uses .venv/bin/python scheduler.py directly — see com.wheelbot.scheduler.plist
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing $PY — run: cd \"$ROOT\" && uv sync" >&2
  exit 1
fi
exec "$PY" "$ROOT/scheduler.py"
