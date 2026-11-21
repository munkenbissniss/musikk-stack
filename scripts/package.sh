#!/usr/bin/env bash
# Bygger dist/musikk-stack-{linux,macos,windows}.zip fra repoet.
# Brukes av GitHub Actions ved ny versjon (tag v*), men kan også kjøres lokalt.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="$(cat VERSION)"
COMMON=(compose.yaml compose.*.yaml tools homepage README.md LICENSE VERSION CHANGELOG.md)

rm -rf dist && mkdir -p dist

build() {
  local os="$1"; shift
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/musikk-stack"
  cp -r "${COMMON[@]}" "$tmp/musikk-stack/"
  for extra in "$@"; do cp -r $extra "$tmp/musikk-stack/"; done
  find "$tmp" \( -name __pycache__ -o -name '*.pyc' -o -name .DS_Store \) -prune -exec rm -rf {} +
  find "$tmp" \( -name '*.sh' -o -name '*.command' \) -exec chmod 755 {} +
  (cd "$tmp" && zip -qrX "$ROOT/dist/musikk-stack-$os.zip" musikk-stack)
  rm -rf "$tmp"
  echo "  dist/musikk-stack-$os.zip"
}

echo "Musikk-stack $VERSION"
build linux   linux "packaging/linux/*"
build macos   "packaging/macos/*"
build windows "packaging/windows/*"
