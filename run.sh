#!/usr/bin/env bash
# NOCP — NeoCloud OS Control Plane :: dev launcher
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
else
  . .venv/bin/activate
fi

echo "NOCP starting →  dashboard http://127.0.0.1:8000/   ·   API docs http://127.0.0.1:8000/docs"
exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
