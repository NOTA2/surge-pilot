#!/bin/zsh
cd "$(dirname "$0")" || exit 1
if curl -fsS --max-time 1 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
  open http://127.0.0.1:8765/
  exit 0
fi
./run-web.sh
