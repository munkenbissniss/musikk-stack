"""Veiviser: stiller noen få spørsmål og lager .env.
Kjøres med:  docker compose run --rm setup
Kan kjøres på nytt når som helst – svarene du ga sist blir standardvalg."""
import json
import os
import secrets
import socket
import string
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from common import demux, docker, docker_info, log, ok, warn  # noqa: E402

ENV_PATH = "/stack/.env"
PROJECT = "musikk-stack"

PROVIDERS = [
    ("protonvpn", "ProtonVPN (WireGuard, støtter port forwarding)"),
    ("mullvad", "Mullvad (WireGuard)"),
    ("nordvpn", "NordVPN (WireGuard)"),
    ("surfshark", "Surfshark (WireGuard)"),
    ("airvpn", "AirVPN (WireGuard, støtter port forwarding)"),
    ("private internet access", "Private Internet Access (OpenVPN)"),
    ("custom", "Annen – egen WireGuard-konfig"),
    ("", "Ingen VPN (ikke anbefalt)"),
]

QUALITY = [
    ("Standard", "Standard – MP3 320 / AAC, tar lite plass"),
    ("Lossless", "Tapsfri – FLAC, best lyd, 3–5 ganger så stort"),
    ("Any", "Alt – tar det som finnes"),
]


# --------------------------------------------------------------------------- hjelpere
def load_env():
    vals = {}
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            vals[k.strip()] = v
    except OSError:
        pass
    return vals


def ask(prompt, default="", secret_hint=False, allow_empty=True):
    shown = default
    if secret_hint and default:
        shown = default[:4] + "…"
    while True:
        try:
            ans = input(f"  {prompt}" + (f" [{shown}]" if shown else "") + ": ").strip()
        except EOFError:
            ans = ""
        if not ans:
            ans = default
        if "'" in ans:
            warn("Bruk ikke apostrof (') i svaret.")
            continue
        if ans or allow_empty:
            return ans
        warn("Dette feltet må fylles ut.")


def yes(prompt, default=True):
    d = "J/n" if default else "j/N"
    try:
        a = input(f"  {prompt} [{d}]: ").strip().lower()
    except EOFError:
        a = ""
    return default if not a else a.startswith(("j", "y"))


def header(t):
    log(f"\n─── {t} " + "─" * max(0, 60 - len(t)))


def guess_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def gen_password(n=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


VOLUME = "musikk-stack_media"


def test_share(typ, opts, device):
    """Kobler til delingen i en kortvarig container og viser innholdet."""
    tmp = "musikk-stack_sharetest"
    docker("DELETE", f"/volumes/{tmp}?force=1")
    s, b = docker("POST", "/volumes/create", {"Name": tmp, "Driver": "local",
                                              "DriverOpts": {"type": typ, "o": opts, "device": device}})
    if s != 201:
        return False, b.decode(errors="replace")
    s, b = docker("POST", "/containers/create", {
        "Image": "python:3.13-alpine", "Cmd": ["sh", "-c", "ls -1 /data | head -20 && touch /data/.musikk-test && rm /data/.musikk-test && echo SKRIV_OK"],
        "User": f"{PUID_GUESS[0]}:{PUID_GUESS[1]}", "HostConfig": {"Binds": [f"{tmp}:/data"]}})
    if s != 201:
        docker("DELETE", f"/volumes/{tmp}?force=1")
        return False, b.decode(errors="replace")
    cid = json.loads(b)["Id"]
    try:
        s, b = docker("POST", f"/containers/{cid}/start")
        if s not in (204, 304):
            return False, b.decode(errors="replace")
        docker("POST", f"/containers/{cid}/wait", timeout=60)
        s, b = docker("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
        return True, demux(b).decode(errors="replace")
    finally:
        docker("DELETE", f"/containers/{cid}?force=1")
        docker("DELETE", f"/volumes/{tmp}?force=1")


PUID_GUESS = [1000, 1000]


def choose_storage(old, puid, pgid):
    """Spør om mediemappa ligger lokalt eller på en nettverksdeling. Returnerer {} for lokal."""
    PUID_GUESS[:] = [puid, pgid]
    cur = {"nfs": "2", "cifs": "3"}.get(old.get("SHARE_TYPE", ""), "1")
    log("\n  Hvor skal musikken ligge?")
    log("    1. På denne maskinen (intern eller USB-disk, eller en mappe som allerede er montert)")
    log("    2. NFS-deling på en annen maskin (Linux, NAS)")
    log("    3. SMB-deling (Windows-deling, NAS, Mac-deling)")
    sel = ask("Velg", cur)
    if sel not in ("2", "3"):
        return {}
    while True:
        server = ask("Serverens IP-adresse", old.get("SHARE_SERVER", ""), allow_empty=False)
        if sel == "2":
            log("  Stien som er delt (står i /etc/exports på serveren, f.eks. /srv/media)")
            path = ask("Delt sti", old.get("SHARE_PATH", ""), allow_empty=False)
            typ, device = "nfs", ":" + path
            opts = f"addr={server},rw,nfsvers=4,soft,timeo=150,retrans=3"
            extra = {}
        else:
            log("  Navnet på delingen, f.eks. Media (som i \\\\server\\Media)")
            path = ask("Delingsnavn", old.get("SHARE_PATH", ""), allow_empty=False).strip("/\\")
            user = ask("Brukernavn på serveren", old.get("SHARE_USER", ""), allow_empty=False)
            pw = ask("Passord", old.get("SHARE_PASSWORD", ""), True, False)
            if "," in pw or "," in user:
                warn("Brukernavn og passord kan ikke inneholde komma (,) for SMB-deling.")
                continue
            typ, device = "cifs", f"//{server}/{path}"
            opts = (f"addr={server},username={user},password={pw},uid={puid},gid={pgid},"
                    "file_mode=0664,dir_mode=0775,vers=3.0")
            extra = {"SHARE_USER": user, "SHARE_PASSWORD": pw}
        log("  Tester tilkoblingen …")
        good, out = test_share(typ, opts, device)
        if good and out.strip():
            log("  Innhold:")
            for line in out.strip().splitlines():
                if line != "SKRIV_OK":
                    log("    " + line)
            if "SKRIV_OK" in out:
                ok("Tilkoblet, og skriving fungerer.")
            else:
                warn(f"Tilkoblet, men bruker {puid} kan ikke skrive. Sjekk eier/rettigheter på serveren "
                     "(samme bruker-ID på begge maskiner, «id -u»).")
        else:
            warn("Fikk ikke koblet til: " + (out.strip()[-300:] or "ukjent feil"))
            log("  Sjekk IP, sti/navn, at delingen tillater denne maskinens IP, og brannmuren.")
        if good and yes("Bruke denne delingen?", True):
            break
        if not yes("Prøve på nytt?", True):
            return {}
    remove_old_volume(typ, opts, device)
    return {"SHARE_TYPE": typ, "SHARE_SERVER": server, "SHARE_PATH": path,
            "SHARE_OPTS": opts, "SHARE_DEVICE": device, **extra}


def remove_old_volume(typ, opts, device):
    """Docker husker en deling til volumet slettes. Fjern det gamle hvis noe er endret."""
    s, b = docker("GET", f"/volumes/{VOLUME}")
    if s != 200:
        return
    o = json.loads(b).get("Options") or {}
    if o.get("type") == typ and o.get("o") == opts and o.get("device") == device:
        return
    s, _ = docker("DELETE", f"/volumes/{VOLUME}")
    if s == 409:
        warn("Den gamle delingen er i bruk. Kjør «docker compose down» og så veiviseren igjen.")


def as_path(p):
    """Relative stier må starte med ./ – ellers tolker Docker dem som et navngitt volum."""
    p = p.strip().replace("\\", "/")
    if p and not (p.startswith(("/", "./", "../", "~")) or (len(p) > 1 and p[1] == ":")):
        p = "./" + p
    return p.rstrip("/") if len(p) > 1 else p


def q(v):
    return f"'{v}'"



# --------------------------------------------------------------------------- Plex-stack
def _inspect(cid):
    s, b = docker("GET", f"/containers/{cid}/json")
    return json.loads(b) if s == 200 else None


def _find(service, project=None):
    """Finner en Compose-container etter tjenestenavn (kjørende foretrekkes)."""
    labels = [f"com.docker.compose.service={service}"]
    if project:
        labels.append(f"com.docker.compose.project={project}")
    s, b = docker("GET", "/containers/json?all=1&filters=" + urllib.parse.quote(json.dumps({"label": labels})))
    found = [c for c in (json.loads(b) if s == 200 else [])
             if (c.get("Labels") or {}).get("com.docker.compose.project") != PROJECT]
    found.sort(key=lambda c: c.get("State") != "running")
    return _inspect(found[0]["Id"]) if found else None


def _envmap(c):
    return dict(e.split("=", 1) for e in ((c or {}).get("Config") or {}).get("Env") or [] if "=" in e)


def detect_plexstack():
    """Ser etter Plex-stack (eller en lignende stack med Prowlarr og qBittorrent) på maskinen."""
    prow = _find("prowlarr")
    if not prow:
        return None
    project = (prow["Config"].get("Labels") or {}).get("com.docker.compose.project", "")
    qbit = _find("qbittorrent", project)
    if not qbit:
        return None
    nzb = _find("nzbget", project)
    nets = list((prow.get("NetworkSettings") or {}).get("Networks") or {})
    network = f"{project}_default" if f"{project}_default" in nets else (nets[0] if nets else "")
    via_vpn = (qbit.get("HostConfig") or {}).get("NetworkMode", "").startswith("container:")
    qport = _envmap(qbit).get("WEBUI_PORT", "8080")
    mount = next((m for m in qbit.get("Mounts") or [] if m.get("Destination") == "/data"), {})
    creds = {}
    for c, u, p in ((_find("autoconfig", project), "ADMIN_USER", "ADMIN_PASSWORD"),
                    (nzb, "NZBGET_USER", "NZBGET_PASS")):
        e = _envmap(c)
        if e.get(p):
            creds = {"user": e.get(u) or "admin", "password": e[p]}
            break
    return {
        "project": project, "network": network,
        "running": all(bool((c or {}).get("State", {}).get("Running")) for c in (prow, qbit)),
        "qbit_url": f"http://{'gluetun' if via_vpn else 'qbittorrent'}:{qport}", "vpn": via_vpn,
        "nzbget_url": "http://nzbget:6789" if nzb else "",
        "data_type": mount.get("Type", ""), "data_source": mount.get("Source", ""),
        "data_volume": mount.get("Name", ""), **creds,
    }


# --------------------------------------------------------------------------- veiviser
def choose_vpn(old):
    header("VPN for torrent")
    for i, (_, label) in enumerate(PROVIDERS, 1):
        log(f"    {i}. {label}")
    cur = old.get("VPN_SERVICE_PROVIDER", "protonvpn") if old.get("NO_VPN") != "1" else ""
    cur_idx = next((i for i, (p, _) in enumerate(PROVIDERS, 1) if p == cur), 1)
    try:
        provider = PROVIDERS[int(ask("Velg", str(cur_idx))) - 1][0]
    except (ValueError, IndexError):
        provider = "protonvpn"
    vpn = {}
    if provider:
        vpn["VPN_SERVICE_PROVIDER"] = provider
        if provider == "private internet access":
            vpn["VPN_TYPE"] = "openvpn"
            vpn["OPENVPN_USER"] = ask("PIA-brukernavn", old.get("OPENVPN_USER", ""), allow_empty=False)
            vpn["OPENVPN_PASSWORD"] = ask("PIA-passord", old.get("OPENVPN_PASSWORD", ""), True, False)
        else:
            vpn["VPN_TYPE"] = "wireguard"
            log("  Last ned en WireGuard-konfig fra leverandøren. Der står «PrivateKey = …» og «Address = …».")
            vpn["WIREGUARD_PRIVATE_KEY"] = ask("PrivateKey", old.get("WIREGUARD_PRIVATE_KEY", ""), True, False)
            vpn["WIREGUARD_ADDRESSES"] = ask("Address (Enter = tom for ProtonVPN)", old.get("WIREGUARD_ADDRESSES", ""))
        if provider != "custom":
            vpn["VPN_SERVER_COUNTRIES"] = ask("Land", old.get("VPN_SERVER_COUNTRIES", "Norway"))
        if provider in ("protonvpn", "airvpn", "private internet access"):
            vpn["VPN_PORT_FORWARDING"] = "on" if yes("Slå på port forwarding (raskere deling)?", True) else "off"
    return provider, vpn


def main():
    old = load_env()
    info = docker_info()
    os_name = info.get("OperatingSystem", "")
    kernel = info.get("KernelVersion", "").lower()
    desktop = ("Docker Desktop" in os_name or "orbstack" in kernel or "linuxkit" in kernel
               or "OrbStack" in os_name)
    native_linux = bool(info) and not desktop

    log("\n╔══════════════════════════════════════════════════════════════╗")
    log("║              Musikk-stack – oppsettsveiviser                 ║")
    log("╚══════════════════════════════════════════════════════════════╝")
    log(f"  Docker: {os_name or 'ukjent'}")
    log("  Trykk Enter for å godta forslaget i [klammer].")

    st = os.stat("/stack")
    puid, pgid = (st.st_uid, st.st_gid) if native_linux and st.st_uid != 0 else (1000, 1000)

    header("Grunnleggende")
    tz = ask("Tidssone", old.get("TZ", "Europe/Oslo"))

    # ------------------------------------------------------------- Plex-stack?
    plex = detect_plexstack()
    shared = False
    if plex:
        header("Plex-stack funnet")
        log(f"  Prosjekt: {plex['project']}   nettverk: {plex['network']}")
        log(f"  qBittorrent {'bak VPN' if plex['vpn'] else 'uten VPN'}"
            + (", NZBGet (Usenet)" if plex["nzbget_url"] else "") + ", Prowlarr")
        where = plex["data_source"] if plex["data_type"] == "bind" else f"nettverksdeling ({plex['data_volume']})"
        log(f"  Mediedisk: {where}")
        log("  Musikken kan bruke nedlasterne og disken derfra. Det sparer minne og en ekstra")
        log("  VPN-tilkobling, og indekserne dine i Prowlarr brukes automatisk for musikk.")
        if not plex["running"]:
            warn("Plex-stack kjører ikke nå. Start den før du starter musikk-stacken.")
        shared = yes("Bruke nedlasterne og mediedisken fra Plex-stack?", old.get("SHARED", "1") != "0")

    share, vpn, usenet, opt = {}, {}, {}, []
    provider, torrent = "", False
    urls = {}
    if shared:
        urls = {"SHARED_NETWORK": plex["network"], "QBIT_URL": plex["qbit_url"],
                "NZBGET_URL": plex["nzbget_url"], "SAB_URL": "", "PROWLARR_URL": "http://prowlarr:9696"}
        if plex["data_type"] == "volume":
            urls["SHARED_VOLUME"] = plex["data_volume"]
            data_root = old.get("DATA_ROOT", "./media")
        else:
            log("\n  Mediemappa må være den samme som Plex-stack bruker, så nedlastinger kan")
            log("  flyttes rett inn i biblioteket uten å kopieres.")
            data_root = as_path(ask("Mediemappe", plex["data_source"] or old.get("DATA_ROOT", ""), allow_empty=False))
    else:
        share = choose_storage(old, puid, pgid)
        if share:
            data_root = old.get("DATA_ROOT", "./media")
        else:
            log("\n  Oppgi stien til mediemappa på DENNE maskinen, f.eks.:")
            log("    Linux: /mnt/media     Mac: /Volumes/Media     Windows: D:/Media")
            data_root = as_path(ask("Mediemappe", old.get("DATA_ROOT", "/mnt/media" if native_linux else ""),
                                    allow_empty=False))

    log("\n  Mappa for musikk, relativt til mediemappa (f.eks. Musikk eller Media/Music).")
    log("  Tomt = finn automatisk (Musikk/Music, øverst eller én mappe ned), ellers lages Musikk.")
    music_dir = ask("Musikkmappe", old.get("MUSIC_DIR", ""))
    config_root = as_path(ask("Mappe for innstillinger", old.get("CONFIG_ROOT", "./config")))

    header("Lydkvalitet")
    log("  Hvilken kvalitet Lidarr skal hente:")
    for i, (_, label) in enumerate(QUALITY, 1):
        log(f"    {i}. {label}")
    cur = next((i for i, (q_, _) in enumerate(QUALITY, 1) if q_ == old.get("MUSIC_QUALITY", "Standard")), 1)
    try:
        quality = QUALITY[int(ask("Velg", str(cur))) - 1][0]
    except (ValueError, IndexError):
        quality = "Standard"

    header("Felles innlogging")
    if shared:
        log("  Må være den samme som i Plex-stack – da kommer autooppsettet inn i qBittorrent,")
        log("  NZBGet og Prowlarr der. Settes også på Navidrome og Lidarr.")
        if plex.get("password"):
            ok(f"Fant innloggingen til Plex-stack ({plex['user']}).")
    else:
        log("  Samme brukernavn og passord settes på Navidrome, Lidarr, Prowlarr, SABnzbd og qBittorrent.")
    admin_user = ask("Brukernavn", plex["user"] if shared and plex.get("password")
                     else old.get("ADMIN_USER", "admin"))
    default_pw = (plex.get("password") if shared else "") or old.get("ADMIN_PASSWORD") or gen_password()
    admin_pw = ask("Passord (Enter = bruk forslaget)", default_pw)

    header("Nettverk")
    ip = old.get("LAN_IP") or guess_ip()
    if not native_linux:
        log("  Finn Macens/PC-ens IP på hjemmenettet:")
        log("    Mac: ipconfig getifaddr en0        Windows: ipconfig (IPv4-adresse)")
        if ip.startswith(("192.168.65.", "172.", "10.0.2.")):
            ip = old.get("LAN_IP", "")
    lan_ip = ask("Maskinens IP-adresse på hjemmenettet", ip, allow_empty=False)
    hostname = socket.gethostname() if native_linux else ""
    dash_host = ask("Navn/IP du bruker i nettleseren (eller Tailscale-navn)",
                    old.get("DASHBOARD_HOST", lan_ip))

    profiles = [p for p in old.get("COMPOSE_PROFILES", "").split(",") if p]
    if not shared:
        opt += ["indeksere", "kjoling"]
        while True:
            header("Usenet (nyhetsgrupper) med SABnzbd")
            log("  Trenger et abonnement hos en Usenet-leverandør (f.eks. Newshosting, Eweka,")
            log("  Frugal) og minst én NZB-indekser (legges inn i Prowlarr etterpå).")
            usenet = {}
            if yes("Bruke Usenet?", "usenet" in profiles or not old):
                usenet["USENET_HOST"] = ask("Server", old.get("USENET_HOST", "news.newshosting.com"))
                usenet["USENET_PORT"] = ask("Port (563 = SSL)", old.get("USENET_PORT", "563"))
                usenet["USENET_USER"] = ask("Brukernavn hos leverandøren", old.get("USENET_USER", ""), allow_empty=False)
                usenet["USENET_PASSWORD"] = ask("Passord hos leverandøren", old.get("USENET_PASSWORD", ""), True, False)
                usenet["USENET_CONNECTIONS"] = ask("Antall tilkoblinger (sjekk abonnementet ditt)",
                                                   old.get("USENET_CONNECTIONS", "20"))
            header("Torrent med qBittorrent")
            torrent = yes("Bruke torrent også?", "torrent" in profiles)
            if torrent:
                provider, vpn = choose_vpn(old)
            if usenet or torrent:
                break
            warn("Lidarr trenger minst én måte å laste ned på. Velg Usenet, torrent eller begge.")
        if usenet:
            opt.append("usenet")
        if torrent:
            opt.append("torrent")
        urls = {"QBIT_URL": f"http://{'gluetun' if provider else 'qbittorrent'}:8080" if torrent else "",
                "NZBGET_URL": "", "SAB_URL": "http://sabnzbd:8080" if usenet else "",
                "PROWLARR_URL": "http://prowlarr:9696"}

    header("Rolig modus")
    log("  Begrenser hvor mye prosessor bakgrunnsappene får bruke. Navidrome begrenses")
    log("  aldri, så avspillingen påvirkes ikke. Anbefalt for bærbare og eldre maskiner.")
    cores = os.cpu_count() or 4
    quiet = yes(f"Slå på rolig modus? (maskinen har {cores} tråder)",
                "compose.quiet.yaml" in old.get("COMPOSE_FILE", "") or (not old and cores <= 4))

    # ------------------------------------------------------------- skriv .env
    files = ["compose.yaml"]
    if shared:
        files.append("compose.shared.yaml")
        if "SHARED_VOLUME" in urls:
            files.append("compose.sharedvol.yaml")
    if share:
        files.append("compose.netshare.yaml")
    if torrent and not provider:
        files.append("compose.novpn.yaml")
    if quiet:
        files.append("compose.quiet.yaml")
    allowed = sorted({f"{h}:3200" for h in ("localhost", "127.0.0.1", lan_ip, dash_host,
                                            f"{hostname}.local" if hostname else "") if h})

    lines = [
        "# Laget av veiviseren (docker compose run --rm setup). Kan redigeres for hånd.",
        "", "# --- Grunnleggende",
        f"TZ={q(tz)}", f"PUID={puid}", f"PGID={pgid}",
        f"DATA_ROOT={q(data_root)}", f"CONFIG_ROOT={q(config_root)}",
        f"MUSIC_DIR={q(music_dir)}", f"MUSIC_QUALITY={q(quality)}",
        "", "# --- Felles innlogging",
        f"ADMIN_USER={q(admin_user)}", f"ADMIN_PASSWORD={q(admin_pw)}",
        "", "# --- Nettverk",
        f"LAN_IP={q(lan_ip)}", f"DASHBOARD_HOST={q(dash_host)}",
        f"HOMEPAGE_ALLOWED_HOSTS={q(','.join(allowed))}",
        "", "# --- Nedlasting" + (" (fra Plex-stack)" if shared else ""),
        f"SHARED={1 if shared else 0}",
    ]
    lines += [f"{k}={q(v)}" for k, v in urls.items()]
    if torrent:
        lines.append(f"NO_VPN={0 if provider else 1}")
        lines += [f"{k}={q(v)}" for k, v in vpn.items()]
    if share:
        lines += ["", "# --- Nettverksdeling for mediemappa"] + [f"{k}={q(v)}" for k, v in share.items()]
    if usenet:
        lines += ["", "# --- Usenet"] + [f"{k}={q(v)}" for k, v in usenet.items()]
    lines += [
        "", "# --- Hvilke filer og valgfrie apper Docker Compose bruker",
        "COMPOSE_PATH_SEPARATOR=':'",
        f"COMPOSE_FILE={q(':'.join(files))}",
        f"COMPOSE_PROFILES={q(','.join(opt))}",
        "",
    ]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    try:
        os.chmod(ENV_PATH, 0o600)
        os.chown(ENV_PATH, st.st_uid, st.st_gid)
    except OSError:
        pass

    log("\n╔══════════════════════════════════════════════════════════════╗")
    log("║  Ferdig! .env er lagret.                                     ║")
    log("╚══════════════════════════════════════════════════════════════╝")
    log("  Start alt (første gang tar det noen minutter å laste ned):\n")
    log("      docker compose up -d")
    log("      docker compose logs -f autoconfig\n")
    log(f"  Lytt:       http://{dash_host}:4533   (eller en Subsonic-app på mobilen)")
    log(f"  Dashboard:  http://{dash_host}:3200")
    log(f"  Innlogging overalt: {admin_user} / {admin_pw}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nAvbrutt – ingenting er lagret.")
        sys.exit(1)
