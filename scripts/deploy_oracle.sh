#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Edit it before enabling live/testnet trading."
fi

mkdir -p logs data backups

if docker info >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker compose)
else
  DOCKER_COMPOSE=(sudo docker compose)
fi

"${DOCKER_COMPOSE[@]}" -f docker-compose.yml -f docker-compose.prod.yml pull || true
"${DOCKER_COMPOSE[@]}" -f docker-compose.yml -f docker-compose.prod.yml up -d --build
"${DOCKER_COMPOSE[@]}" ps

echo "ProScalp AI Trader deployed. Health: http://YOUR_ORACLE_PUBLIC_IP/health"
