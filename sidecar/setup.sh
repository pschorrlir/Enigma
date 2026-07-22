#!/usr/bin/env bash
# Create the sidecar venv. CPU torch by default; pass --cuda for GPU wheels.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
if [[ "${1:-}" == "--cuda" ]]; then
    pip install torch
else
    pip install torch --index-url https://download.pytorch.org/whl/cpu
fi
pip install -r requirements.txt
echo "done. start with: sidecar/run.sh"
