#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg ufw

if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if getent group docker >/dev/null 2>&1; then
  sudo usermod -aG docker "$USER" || true
fi

mkdir -p logs data

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

sudo ufw allow OpenSSH || true
sudo ufw allow 80/tcp || true
sudo ufw --force enable || true

echo "Setup complete. Edit .env before running docker compose up -d --build."
