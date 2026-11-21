# Musikk-stack – start (Windows). Startes av Start.bat. Første gang kjøres veiviseren.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Say($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Say "Fant ikke Docker. Installer Docker Desktop (https://www.docker.com/products/docker-desktop/) og prøv igjen."
    Read-Host "Trykk Enter for å lukke"; exit 1
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say "Starter Docker Desktop …"
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) { Start-Process $dd }
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Say "Docker startet ikke. Start Docker Desktop selv og dobbeltklikk Start.bat igjen."
        Read-Host "Trykk Enter for å lukke"; exit 1
    }
}

if (-not (Test-Path ".env")) {
    Say "Første gang: starter veiviseren …"
    docker compose run --rm setup
    if ($LASTEXITCODE -ne 0) { Read-Host "Trykk Enter for å lukke"; exit 1 }
}

Say "Starter alt (første gang lastes rundt 1,5 GB ned) …"
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Say "Feilen nevner nettverket plex-stack_default? Da bruker du nedlasterne fra Plex-stack – start Plex-stack først."
    Read-Host "Trykk Enter for å lukke"; exit 1
}

Say "Følger oppsettet. Lukk vinduet eller trykk Ctrl+C når du er ferdig – ingenting stoppes."
docker compose logs -f --tail 50 autoconfig
