#!/usr/bin/env bash
# NOCP — NeoCloud OS Control Plane :: dev launcher
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  "$(command -v python3.11 || command -v python3)" -m venv .venv   # Python >= 3.10 (starlette 1.x)
  . .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
else
  . .venv/bin/activate
fi

# Dev launcher keeps the implicit operator plane for the bundled static pages;
# production leaves both 0 and clients send Authorization: Bearer (see README).
export NOCP_DEV_OPERATOR_DEFAULT="${NOCP_DEV_OPERATOR_DEFAULT:-1}"
export NOCP_DEV_INSECURE_HEADERS="${NOCP_DEV_INSECURE_HEADERS:-1}"

echo "NOCP starting →  dashboard http://127.0.0.1:8000/   ·   API docs http://127.0.0.1:8000/docs"
exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
