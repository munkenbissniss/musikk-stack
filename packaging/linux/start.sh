#!/usr/bin/env bash
# Musikk-stack – start (Linux). Første gang: installerer Docker ved behov og kjører veiviseren.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  say "Docker er ikke installert."
  read -r -p "Installere Docker nå (offisielt skript fra get.docker.com, krever sudo)? [J/n] " a
  [[ "${a:-j}" =~ ^[nN] ]] && { echo "Avbrutt. Installer Docker og kjør ./start.sh igjen."; exit 1; }
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  say "Docker er installert. Logg ut og inn igjen (eller start maskinen på nytt), og kjør ./start.sh på nytt."
  exit 0
fi

if ! docker info >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null && sudo docker info >/dev/null 2>&1; then
    say "Brukeren din er ikke med i docker-gruppen ennå."
    echo "Kjør: sudo usermod -aG docker $USER  – logg så ut og inn igjen, og kjør ./start.sh på nytt."
  else
    say "Får ikke kontakt med Docker. Kjører tjenesten?  sudo systemctl start docker"
  fi
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  say "Docker Compose mangler. Installer pakken docker-compose-plugin og prøv igjen."
  exit 1
fi

if [ ! -f .env ]; then
  say "Første gang: starter veiviseren …"
  docker compose run --rm setup
fi

say "Starter alt (første gang lastes rundt 1,5 GB ned) …"
docker compose up -d || { say "Feilen nevner nettverket plex-stack_default? Da bruker du nedlasterne fra Plex-stack – start Plex-stack først."; exit 1; }

say "Følger oppsettet. Trykk Ctrl+C for å gå ut – ingenting stoppes."
docker compose logs -f --tail 50 autoconfig || true
