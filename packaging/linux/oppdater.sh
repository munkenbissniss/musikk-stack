#!/usr/bin/env bash
# Musikk-stack – oppdater alle apper og kjør oppsettet på nytt.
set -euo pipefail
cd "$(dirname "$0")"
docker compose pull
docker compose up -d
docker compose run --rm autoconfig
