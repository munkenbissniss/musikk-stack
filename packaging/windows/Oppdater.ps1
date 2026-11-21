# Musikk-stack – oppdater alle apper og kjør oppsettet på nytt (Windows).
Set-Location -Path $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
docker compose pull
docker compose up -d
docker compose run --rm autoconfig
Read-Host "Ferdig. Trykk Enter for å lukke"
