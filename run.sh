#!/usr/bin/env bash
# Lancement de la station de prévision Aurora France.
set -euo pipefail

cd "$(dirname "$0")"

VENV="${AURORA_VENV:-.venv}"
HOST="${AURORA_HOST:-0.0.0.0}"
PORT="${AURORA_PORT:-8077}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "→ Création de l'environnement virtuel ($VENV)…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip -q
  "$VENV/bin/pip" install -q -r requirements.txt
fi

echo "→ Station Aurora France : http://127.0.0.1:$PORT"
echo "→ Administration        : http://127.0.0.1:$PORT/admin"

if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]]; then
  while read -r ip; do
    [[ -n "$ip" ]] && echo "→ Réseau local          : http://$ip:$PORT"
  done < <(hostname -I 2>/dev/null | tr ' ' '\n')
  if [[ -z "${AURORA_ADMIN_TOKEN:-}" ]]; then
    echo "⚠ Administration à distance désactivée (aucun AURORA_ADMIN_TOKEN défini)."
    echo "  Les postes distants peuvent consulter les prévisions, pas piloter le modèle."
  fi
fi

exec "$VENV/bin/python" -m uvicorn backend.main:app \
  --host "$HOST" --port "$PORT" "$@"
