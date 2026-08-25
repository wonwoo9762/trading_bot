#!/usr/bin/env bash
# Optional: run the scheduler manually (same as launchd, but from a terminal).
# launchd uses uv run python scheduler.py directly — see com.wheelbot.scheduler.plist
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
PY="$ROOT/.venv/bin/python"
if [[ -x "$PY" ]]; then
  exec "$PY" "$ROOT/scheduler.py" "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run python "$ROOT/scheduler.py" "$@"
fi
echo "Missing $PY and uv is not on PATH — install uv or run uv sync first." >&2
exit 1
