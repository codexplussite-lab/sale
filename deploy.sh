#!/usr/bin/env bash
set -euo pipefail

echo "=== Software House Lead Engine Web App - Contabo VPS Deploy ==="

if ! command -v docker &> /dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo systemctl enable --now docker
fi

if ! docker compose version &> /dev/null; then
  echo "Installing Docker Compose plugin..."
  sudo apt-get update && sudo apt-get install -y docker-compose-plugin
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Edit .env to add your GEMINI_API_KEY, REDDIT_CLIENT_ID/SECRET, X_BEARER_TOKEN, and DISCORD_WEBHOOK_URL."
fi

docker compose up -d --build

echo ""
echo "Web App is running on port $(grep ^APP_PORT .env | cut -d '=' -f2 || echo 8000)."
