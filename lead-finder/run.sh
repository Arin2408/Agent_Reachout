#!/bin/bash
# Start Lead Finder at http://127.0.0.1:5050
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi
(sleep 1.5 && open http://127.0.0.1:5050) &
exec .venv/bin/python app.py
