# Musikk-stack

En musikkserver på Docker som setter seg opp selv. Du svarer på noen spørsmål, og etter et par minutter har du ditt eget «Spotify» hjemme: hele musikksamlingen i nettleseren og på mobilen, og nye album som hentes av seg selv når artistene du følger gir ut noe.

Fungerer på Linux, macOS og Windows. Kjører du [Plex-stack](../../../plex-stack) på samme maskin, kan musikken bruke nedlasterne derfra.

Bygget på det enkle oppsettet Navidrome + Lidarr + SABnzbd, med automatisk oppsett i stedet for håndarbeid.

## Last ned

| System | Pakke | Start med |
|---|---|---|
| Linux | [musikk-stack-linux.zip](../../releases/latest/download/musikk-stack-linux.zip) | `./start.sh` |
| macOS | [musikk-stack-macos.zip](../../releases/latest/download/musikk-stack-macos.zip) | dobbeltklikk `Start.command` |
| Windows | [musikk-stack-windows.zip](../../releases/latest/download/musikk-stack-windows.zip) | dobbeltklikk `Start.bat` |

Pakkene inneholder det samme. Forskjellen er startskriptene, og Linux-pakken har i tillegg skript for å gjøre en gammel PC eller Mac til hjemmeserver.

## Hva du får

| App | Adresse | Hva den gjør |
|---|---|---|
| Navidrome | `:4533` | Musikkserveren. Spillelister, favoritter og egne brukere. Virker med alle Subsonic-apper. |
| Lidarr | `:8686` | Følger artister og henter nye album automatisk, i kvaliteten du velger. |
| Dashboard | `:3200` | Alt på ett sted, med tall fra appene. |
| SABnzbd | `:8085` | Usenet-nedlasting. |
| qBittorrent bak VPN | `:8091` | Torrent med killswitch (valgfritt). |
| Prowlarr | `:9698` | Indeksere, synkes automatisk til Lidarr. |
| Kjøling | `:3012` | Viser belastningen og roer ned nedlastingen når maskinen blir varm. |

Nedlasterne kjører bare når stacken står alene. Bruker du Plex-stackens, ligger de på Plex-stackens vanlige porter (qBittorrent 8080, NZBGet 6789, Prowlarr 9696).

Portene er valgt så Musikk-stack, Lydbok-stack og Plex-stack kan kjøre på samme maskin.

Autooppsettet gjør dette for deg:

- lager administratoren i Navidrome og starter første skanning
- gir Lidarr rotmappe, kvalitetsprofil, ryddige navn (Artist/Album/01 - Låt), hardlinks og nedlastingsklienter
- legger inn Usenet-serveren og kategorien `music` i SABnzbd, og slipper de andre containerne inn
- lager kategorien `music` i qBittorrent (og i NZBGet når Plex-stackens brukes)
- kobler Lidarr til Prowlarr, så indekserne med musikk brukes automatisk
- setter samme innlogging på alle appene
- lager dashboardet med nøkler og tall

Det sjekker hva som allerede er gjort, så det er trygt å kjøre igjen så ofte du vil.

## Dette gjør du for hånd

1. **Indeksere i Prowlarr.** Legg til minst én indekser som har musikk. Den kobles automatisk til Lidarr. Har du Plex-stack, holder det å legge den til i Prowlarr der.
2. **Brukere i Navidrome.** Lag en bruker til hver i familien (brukerikonet øverst til høyre → Brukere). Da får alle egne spillelister og favoritter.
3. **En app på mobilen.** Navidrome snakker Subsonic, så du kan velge fritt: Symfonium (Android), Amperfy eller play:Sub (iPhone), Feishin (PC og Mac). Logg inn med `http://<maskinens-ip>:4533`.

Oppsummeringen på slutten av autooppsettet sier hva som gjenstår hos deg. Den lagres også i `config/OPPSUMMERING.txt`.

## Sammen med Plex-stack

Finner veiviseren Plex-stack på maskinen, spør den om musikken skal bruke nedlasterne og disken derfra. Det anbefales, særlig på en maskin med lite minne. Da:

- startes ingen ekstra VPN, qBittorrent, SABnzbd eller Prowlarr
- legges musikken på samme disk som filmene, så nedlastinger hardlinkes rett inn i biblioteket uten å kopieres
- røres bare det som trengs i Plex-stack: kategorien `music` i qBittorrent og NZBGet, og Lidarr i Prowlarr
- brukes samme innlogging som i Plex-stack (veiviseren finner den selv)

Plex-stack må kjøre før du starter musikk-stacken. Bruker Plex-stack en nettverksdeling, bruker musikken den samme.

## Installer Docker

- **Linux:** `start.sh` tilbyr å installere Docker for deg.
- **Mac:** [OrbStack](https://orbstack.dev) (raskest) eller [Docker Desktop](https://www.docker.com/products/docker-desktop/).
- **Windows:** [Docker Desktop](https://www.docker.com/products/docker-desktop/).

## Kjør veiviseren

Startskriptet kjører veiviseren første gang. Den spør om:

- hvor musikken skal ligge (lokal disk, NFS- eller SMB-deling), og tester delingen før den går videre
- lydkvalitet: Standard (MP3 320/AAC), tapsfri (FLAC) eller alt som finnes
- felles brukernavn og passord (forslag lages for deg)
- Usenet og/eller torrent med VPN, hvis stacken står alene. Minst én må velges
- rolig modus, som anbefales på bærbare og eldre maskiner

Svarene lagres i `.env`. Kjører du veiviseren igjen, er de gamle svarene standardvalg.

Uten startskriptene:

```bash
docker compose run --rm setup
docker compose up -d
docker compose logs -f autoconfig
```

## Musikk du har fra før

Legg den i musikkmappa, helst som `Artist/Album/spor`. Finner veiviseren en mappe som heter `Musikk` eller `Music` (øverst eller én mappe ned), brukes den. Navidrome leser taggene i filene, så mappenavnene betyr lite for selve avspillingen. Lidarr trenger dem for å kjenne igjen hva du har: åpne Lidarr → Library → Import, så legges artistene til og følges videre.

Er taggene rotete, rydd dem først med [MusicBrainz Picard](https://picard.musicbrainz.org). Da kjenner både Navidrome og Lidarr igjen albumene.

## Hente ny musikk

Åpne Lidarr, søk opp en artist og legg den til. Lidarr søker i indekserne, laster ned og legger albumene i `Artist/Album`. Navidrome finner dem ved neste skanning (hver time), og nye album fra artister du følger, hentes automatisk.

## VPN

Torrent-trafikken går gjennom [gluetun](https://github.com/qdm12/gluetun) med killswitch: faller VPN-en ut, stopper nedlastingen. Veiviseren har ProtonVPN, Mullvad, NordVPN, Surfshark, AirVPN og PIA, eller din egen WireGuard-konfig. Usenet går kryptert direkte og trenger ikke VPN.

## Usenet

Svar ja på Usenet i veiviseren og oppgi leverandøren (f.eks. Newshosting). NZB-indeksere legger du inn i Prowlarr. Får du «Too many connections», senk `USENET_CONNECTIONS` i `.env` og kjør autooppsettet igjen.

## Nettverksdeling

Ligger disken på en annen maskin, velg NFS eller SMB i veiviseren. Docker kobler seg til delingen selv, så ingenting må monteres. Veiviseren tester at den kan lese og skrive.

## Rolig modus og kjøling

Rolig modus gir bakgrunnsappene en fast grense for prosessorbruk. Kjøling (`:3012`) følger med på temperatur og belastning, og når maskinen blir varm eller travel, får nedlasterne enda lavere grenser, qBittorrent går over til lave fartsgrenser og SABnzbd venter med utpakking. Navidrome begrenses aldri, så avspillingen går som normalt.

## Kjøre oppsettet på nytt

```bash
docker compose run --rm autoconfig
```

## Oppdatere

Kjør `oppdater.sh` (Linux), `Oppdater.command` (Mac) eller `Oppdater.bat` (Windows).

## Lytte borte

Installer [Tailscale](https://tailscale.com) på serveren og mobilen, så når du Navidrome hjemmefra uten å åpne porter i ruteren.

## Feilsøking

| Problem | Løsning |
|---|---|
| «network plex-stack_default … not found» | Du bruker nedlasterne fra Plex-stack. Start Plex-stack først. |
| Lidarr finner ikke artister, eller søket henger | Lidarr henter artistinformasjon fra en felles server som tidvis er treg eller nede. Vent og prøv igjen. |
| Lidarr finner ingenting å laste ned | Indekserne mangler musikkategorier. Legg til en indekser med musikk i Prowlarr. |
| SABnzbd: «Hostname verification failed» | Kjør autooppsettet igjen. Det slipper de andre containerne inn. |
| Navidrome: innlogging feilet | Passordet er endret i appen. Sett det tilbake, eller rett `ADMIN_PASSWORD` i `.env` og kjør autooppsettet. |
| Nye album vises ikke i Navidrome | Navidrome skanner hver time. Vil du ha dem med en gang: Navidrome → aktivitetsikonet → Skann. |
| Appen på mobilen finner ikke serveren | Bruk IP-adressen (`http://192.168.x.x:4533`), ikke `localhost`. |

Loggene til en app: `docker logs <navn>` (f.eks. `navidrome`, `lidarr`, `musikk-sabnzbd`).

## Ansvar

Stacken er et verktøy. Du er selv ansvarlig for hva du laster ned, og for å følge opphavsretten der du bor. Bruk den til musikk du har rett til.

## Takk til

[Navidrome](https://www.navidrome.org), [Lidarr](https://lidarr.audio), [SABnzbd](https://sabnzbd.org), [Prowlarr](https://prowlarr.com), [qBittorrent](https://www.qbittorrent.org), [gluetun](https://github.com/qdm12/gluetun), [Homepage](https://gethomepage.dev) og [MusicBrainz](https://musicbrainz.org).
