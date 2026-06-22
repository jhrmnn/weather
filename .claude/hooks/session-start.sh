#!/bin/bash
# SessionStart hook: install Python dependencies and check out the data store.
#
# The "data store" is the orphan `data` branch (an append-only archive of raw
# Open-Meteo API responses). CI checks it out into a `data-store/` directory
# via actions/checkout; this hook mirrors that locally as a git worktree so the
# same `--data-dir data-store` commands (collect.py, build.py) work in a session.
set -euo pipefail

# Only run in Claude Code on the web; a local machine manages its own setup.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

echo "Installing Python dependencies..."
pip install --quiet -r requirements.txt

echo "Checking out the data store (data branch -> data-store/)..."
git fetch --quiet origin data
if [ -e data-store ]; then
  # Existing checkout: fast-forward it to the latest archived data.
  git -C data-store fetch --quiet origin data
  git -C data-store checkout --quiet data
  git -C data-store reset --quiet --hard origin/data
else
  # Fresh linked worktree tracking the data branch, sharing this repo's objects.
  git worktree add --quiet -B data data-store origin/data
fi

echo "Session setup complete."
