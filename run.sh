#!/bin/zsh
# Options Lab launcher. Venv lives on the internal disk (exFAT breaks venvs).
VENV="$HOME/venvs/options-lab"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating venv at $VENV ..."
  python3.12 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install -r "$DIR/requirements.txt"
fi

cd "$DIR"
exec "$VENV/bin/uvicorn" backend.app:app --host 127.0.0.1 --port 8420 "$@"
