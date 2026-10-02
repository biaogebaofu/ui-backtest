#!/usr/bin/env sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -X utf8 ui.py
fi
exec python3 -X utf8 ui.py
