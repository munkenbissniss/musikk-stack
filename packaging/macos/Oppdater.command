#!/bin/bash
# Musikk-stack – oppdater alle apper og kjør oppsettet på nytt (macOS). Dobbeltklikk.
cd "$(dirname "$0")" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.orbstack/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker compose pull && docker compose up -d && docker compose run --rm autoconfig
read -r -p "Ferdig. Trykk Enter for å lukke." _
