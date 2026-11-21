"""Setter opp og kobler sammen alle appene automatisk via API-ene deres.

Kjøres av «docker compose up -d» (tjenesten autoconfig). Trygt å kjøre igjen
når som helst – den sjekker hva som allerede er gjort og retter bare avvik:
    docker compose run --rm autoconfig

To måter å kjøre på:
  • Alene: egen SABnzbd og/eller qBittorrent bak VPN, og egen Prowlarr.
  • Delt (SHARED=1): bruker nedlasterne i Plex-stack. Der røres bare det som trengs
    for musikk: en kategori i qBittorrent og NZBGet, og Lidarr i Prowlarr.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from common import (CFG, Session, _has_content, docker_action, docker_exec,  # noqa: E402
                    docker_logs, env, http, ini_get, ini_set, log, music_dir, ok, read, step,
                    wait_file, wait_http, warn, write)

PUID, PGID = int(env("PUID", "1000")), int(env("PGID", "1000"))
USER, PASSWORD = env("ADMIN_USER", "admin"), env("ADMIN_PASSWORD")
SHARED = env("SHARED") == "1"
QBIT = env("QBIT_URL").rstrip("/")
NZBGET = env("NZBGET_URL").rstrip("/")
SAB = env("SAB_URL").rstrip("/")
PROWLARR = env("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
LIDARR = "http://lidarr:8686"
ND = "http://navidrome:4533"
DASH = env("DASHBOARD_HOST", env("LAN_IP", "localhost"))
PROFILES = [p for p in env("COMPOSE_PROFILES").split(",") if p]
MUSIC = music_dir()
CATEGORY = "music"

# Containernavn og porter på maskinen: egne (musikk-*) eller Plex-stackens
C = {"qbit": "qbittorrent", "nzbget": "nzbget", "prowlarr": "prowlarr", "gluetun": "gluetun", "sab": ""} if SHARED else \
    {"qbit": "musikk-qbittorrent", "nzbget": "", "prowlarr": "musikk-prowlarr", "gluetun": "musikk-gluetun",
     "sab": "musikk-sabnzbd"}
PORT = {"qbit": 8080, "nzbget": 6789, "prowlarr": 9696} if SHARED else {"qbit": 8091, "sab": 8085, "prowlarr": 9698}

STATE = {}          # nøkler og ID-er vi finner underveis
RESULTS = []        # (app, status, melding) til oppsummeringen
TODO = []           # det som må gjøres for hånd


def result(app, good, msg):
    RESULTS.append((app, good, msg))
    (ok if good else warn)(f"{app}: {msg}")


def run(app, fn):
    step(app)
    try:
        fn()
    except Exception as e:  # én app som feiler skal ikke stoppe resten
        result(app, False, f"uventet feil: {e}")


def hostport(url, default):
    u = urllib.parse.urlparse(url)
    return u.hostname, u.port or default


# =========================================================================== *arr
class Arr:
    def __init__(self, name, url, key, ver="v1"):
        self.name, self.base, self.key, self.ver = name, url.rstrip("/"), key, ver

    def call(self, method, path, body=None):
        r = http(method, f"{self.base}/api/{self.ver}/{path}", json_body=body,
                 headers={"X-Api-Key": self.key, "Accept": "application/json"}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"{method} {path} → {r.status} {r.text[:200]}")
        return r.json()

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)

    def put_config(self, name, obj):
        try:
            return self.put(f"config/{name}/{obj.get('id', 1)}", obj)
        except RuntimeError:
            return self.put(f"config/{name}", obj)

    def set_auth(self):
        if not PASSWORD:
            return
        h = self.get("config/host")
        h.update(authenticationMethod="forms", authenticationRequired="enabled",
                 username=USER, password=PASSWORD, passwordConfirmation=PASSWORD)
        self.put_config("host", h)

    def upsert(self, kind, implementation, name, values, extra=None, match=None):
        """Oppretter eller oppdaterer en nedlastingsklient/app. Setter bare felt appen kjenner."""
        items = [i for i in self.get(kind) if i.get("implementation") == implementation
                 and (match is None or match(i))]
        if items:
            obj, verb = items[0], "oppdatert"
        else:
            obj = next(s for s in self.get(f"{kind}/schema") if s.get("implementation") == implementation)
            obj.update(name=name, enable=True)
            verb = "lagt til"
        for f in obj.get("fields", []):
            if f.get("name") in values:
                f["value"] = values[f["name"]]
        for k, v in (extra or {}).items():
            if k in obj:
                obj[k] = v
        if "id" in obj and items:
            self.put(f"{kind}/{obj['id']}?forceSave=true", obj)
        else:
            self.post(f"{kind}?forceSave=true", obj)
        return verb


def field(obj, name):
    return next((f.get("value") for f in obj.get("fields", []) if f.get("name") == name), None)


def key_from_xml(text):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", text or "")
    return m.group(1).strip() if m else ""


def arr_key(app):
    path = f"{CFG}/{app}/config.xml"
    if not wait_file(path, 180):
        return ""
    for _ in range(20):                 # fila lages før nøkkelen skrives
        k = key_from_xml(read(path))
        if k:
            return k
        time.sleep(3)
    return ""


def wait_api(a, url):
    if not wait_http(f"{url}/ping", 240, a.name):
        return False
    for _ in range(20):                 # API-et kan bruke litt tid etter /ping
        try:
            a.get("system/status")
            return True
        except RuntimeError:
            time.sleep(3)
    return False


# =========================================================================== qBittorrent
def setup_qbit():
    s = Session()
    h = {"Referer": QBIT, "Origin": QBIT}
    if not wait_http(f"{QBIT}/api/v2/app/version", 240, "qBittorrent"):
        TODO.append(f"qBittorrent/VPN: sjekk «docker logs {C['gluetun']}» – VPN-en må være oppe for nedlasting.")
        return result("qBittorrent", False, "svarer ikke (er VPN-en oppe?)")

    def logged_in():
        return s.http("GET", f"{QBIT}/api/v2/app/version", headers=h).status == 200

    def login(u, p):
        r = s.http("POST", f"{QBIT}/api/v2/auth/login", headers=h, form={"username": u, "password": p})
        if not (r.status in (200, 204) and "fail" not in r.text.lower()):   # qBittorrent 5 svarer 204
            log(f"  innlogging som {u}: {r.status} {r.text.strip()[:80]}")
            return False
        return True

    if not logged_in() and not (PASSWORD and login(USER, PASSWORD)):
        if SHARED:
            TODO.append("qBittorrent i Plex-stack: brukernavn/passord i .env må være de samme som der. "
                        "Rett ADMIN_USER/ADMIN_PASSWORD og kjør «docker compose run --rm autoconfig».")
            return result("qBittorrent", False, "kom ikke inn i Plex-stackens qBittorrent")
        temp = re.findall(r"temporary password[^:]*:\s*(\S+)", docker_logs(C["qbit"]), re.I)
        if not (temp and login("admin", temp[-1])):
            log("  kommer ikke inn med passord – skriver innlogging direkte i qBittorrent sin konfig")
            if not qbit_write_config():
                return result("qBittorrent", False, "fant ikke konfigfila til qBittorrent")
            if not wait_http(f"{QBIT}/api/v2/app/version", 120, "qBittorrent"):
                return result("qBittorrent", False, "startet ikke igjen")
            for _ in range(20):
                if logged_in():
                    break
                time.sleep(3)
            else:
                TODO.append(f"qBittorrent: kom ikke inn – send «docker logs {C['qbit']} --tail 30» til den som hjelper deg.")
                return result("qBittorrent", False, "kom ikke inn selv etter at konfigfila ble skrevet")
            ok("innlogging satt i konfigfila")

    if not SHARED:
        prefs = {"save_path": "/data/downloads", "temp_path_enabled": True,
                 "temp_path": "/data/downloads/incomplete",
                 "bypass_auth_subnet_whitelist_enabled": True,
                 "bypass_auth_subnet_whitelist": "172.16.0.0/12"}
        if PASSWORD:
            prefs.update(web_ui_username=USER, web_ui_password=PASSWORD)
        r = s.http("POST", f"{QBIT}/api/v2/app/setPreferences", headers=h, form={"json": json.dumps(prefs)})
        if not r.ok:
            return result("qBittorrent", False, f"innstillinger feilet ({r.status})")
        ok("nedlastingsmapper, passord og innlogging for containere satt")

    r = s.http("GET", f"{QBIT}/api/v2/torrents/categories", headers=h)
    cats = (r.json() or {}) if r.ok else {}
    if CATEGORY not in cats:
        s.http("POST", f"{QBIT}/api/v2/torrents/createCategory", headers=h,
               form={"category": CATEGORY, "savePath": f"/data/downloads/{CATEGORY}"})
    ok(f"kategorien {CATEGORY} finnes")
    STATE["qbit_ok"] = True
    result("qBittorrent", True, "klar" + (" (Plex-stackens)" if SHARED else ""))


def qbit_pbkdf2(password):
    """qBittorrent sitt passordformat: PBKDF2-HMAC-SHA512, 100 000 runder, 16 byte salt."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"


def qbit_write_config():
    paths = [os.path.join(d, f) for d, _, files in os.walk(f"{CFG}/qbittorrent")
             for f in files if f == "qBittorrent.conf"]
    if not paths:
        return False
    docker_action(C["qbit"], "stop")      # qBittorrent skriver over fila når den avslutter
    for path in paths:
        txt = read(path)
        sets = {"WebUI\\AuthSubnetWhitelistEnabled": "true",
                "WebUI\\AuthSubnetWhitelist": "172.16.0.0/12",
                "WebUI\\MaxAuthenticationFailCount": "10"}
        if PASSWORD:
            sets.update({"WebUI\\Username": USER, "WebUI\\Password_PBKDF2": f'"{qbit_pbkdf2(PASSWORD)}"'})
        for k, v in sets.items():
            txt = ini_set(txt, "Preferences", k, v, sep="=")
        write(path, txt, owner=(PUID, PGID))
        log(f"  skrev {path.replace(CFG, 'config')}")
    docker_action(C["qbit"], "start")
    return True


# =========================================================================== NZBGet
def setup_nzbget():
    if not wait_http(NZBGET + "/", 180, "NZBGet"):
        return result("NZBGet", False, "svarer ikke")

    def rpc(method, params, creds):
        auth = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        r = http("POST", NZBGET + "/jsonrpc", headers={"Authorization": f"Basic {auth}"},
                 json_body={"method": method, "params": params, "id": 1}, timeout=60)
        return r.json().get("result") if r.ok and r.json() else None

    tries = ((USER, PASSWORD),) if SHARED else ((USER, PASSWORD), ("nzbget", "tegbzn6789"))
    creds = next((c for c in tries if c[1] and rpc("version", [], c) is not None), None)
    if not creds:
        return result("NZBGet", False, "kom ikke inn (sjekk ADMIN_USER/ADMIN_PASSWORD i .env)")
    cfg = rpc("config", [], creds) or []
    if not cfg:
        return result("NZBGet", False, "fikk ikke lest innstillingene")
    current = {o["Name"]: o["Value"] for o in cfg}

    want = {}
    nums = [int(m.group(1)) for k in current if (m := re.fullmatch(r"Category(\d+)\.Name", k))]
    if not any(current.get(f"Category{n}.Name", "").lower() == CATEGORY for n in nums):
        n = max(nums, default=0) + 1
        want.update({f"Category{n}.Name": CATEGORY, f"Category{n}.DestDir": ""})
    if not SHARED:
        want.update({"DestDir": "/data/downloads/usenet/completed",
                     "InterDir": "/data/downloads/usenet/intermediate",
                     "PostStrategy": "sequential",   # én utpakking om gangen – skåner prosessoren
                     "HealthCheck": "delete"})       # ødelagt utgivelse: avbryt, så Lidarr tar neste
        if PASSWORD:
            want.update(ControlUsername=USER, ControlPassword=PASSWORD)
        host, port = env("USENET_HOST"), env("USENET_PORT", "563")
        if host and env("USENET_USER"):
            want.update({"Server1.Active": "yes", "Server1.Name": host, "Server1.Level": "0",
                         "Server1.Host": host, "Server1.Port": port,
                         "Server1.Encryption": "yes" if port in ("563", "443", "80", "81") else "no",
                         "Server1.Username": env("USENET_USER"), "Server1.Password": env("USENET_PASSWORD"),
                         "Server1.Connections": env("USENET_CONNECTIONS", "20")})
        else:
            TODO.append("NZBGet: legg inn Usenet-serveren under Settings → News-Servers (eller kjør veiviseren).")
    STATE["nzbget_ok"] = True
    if all(current.get(k) == v for k, v in want.items()):
        return result("NZBGet", True, "klar (uendret)")
    opts = [{"Name": o["Name"], "Value": want.pop(o["Name"], o["Value"])} for o in cfg]
    opts += [{"Name": k, "Value": v} for k, v in want.items()]
    if not rpc("saveconfig", [opts], creds):
        return result("NZBGet", False, "lagring av innstillinger feilet")
    rpc("reload", [], creds)
    ok(f"kategorien {CATEGORY} finnes" + ("" if SHARED else ", server og mapper satt"))
    result("NZBGet", True, "klar" + (" (Plex-stackens)" if SHARED else ""))


# =========================================================================== SABnzbd
def setup_sab():
    path = f"{CFG}/sabnzbd/sabnzbd.ini"
    if not wait_file(path, 180):
        return result("SABnzbd", False, "fant ikke sabnzbd.ini (har containeren startet?)")
    for _ in range(20):
        key = ini_get(read(path), "misc", "api_key")
        if key:
            break
        time.sleep(3)
    else:
        return result("SABnzbd", False, "fant ikke API-nøkkel")
    STATE["sab_key"] = key

    # Disse må stå i fila før SABnzbd starter: ellers avviser den andre containere
    # («Hostname verification failed») og bruker feil mapper.
    txt = read(path)
    wl = [h.strip() for h in ini_get(txt, "misc", "host_whitelist").split(",") if h.strip()]
    want = {"host_whitelist": ", ".join(dict.fromkeys(wl + ["sabnzbd", C["sab"]])),
            "download_dir": "/data/downloads/usenet/incomplete",
            "complete_dir": "/data/downloads/usenet/complete"}
    if PASSWORD:
        want.update(username=USER, password=PASSWORD)
    if any(ini_get(txt, "misc", k) != v for k, v in want.items()):
        docker_action(C["sab"], "stop")          # SABnzbd skriver over fila når den avslutter
        txt = read(path)
        for k, v in want.items():
            txt = ini_set(txt, "misc", k, v)
        write(path, txt, owner=(PUID, PGID))
        docker_action(C["sab"], "start")
        ok("tilgang for de andre containerne, mapper og innlogging satt")

    def api(mode, **params):
        q = urllib.parse.urlencode({"mode": mode, "apikey": key, "output": "json", **params})
        r = http("GET", f"{SAB}/api?{q}", timeout=30)
        return r.json() if r.ok else None

    for _ in range(40):
        if api("version"):
            break
        time.sleep(3)
    else:
        return result("SABnzbd", False, "svarer ikke")

    host = env("USENET_HOST")
    if host and env("USENET_USER"):
        port = env("USENET_PORT", "563")
        servers = ((api("get_config", section="servers") or {}).get("config") or {}).get("servers") or []
        cur = next((x for x in servers if x.get("name") == host or x.get("host") == host), {})
        srv = {"name": cur.get("name", host), "host": host, "port": port, "username": env("USENET_USER"),
               "password": env("USENET_PASSWORD"), "connections": env("USENET_CONNECTIONS", "20"),
               "ssl": 1 if port in ("563", "443") else 0, "enable": 1}
        if not cur or any(str(cur.get(k)) != str(v) for k, v in srv.items() if k != "password"):
            api("set_config", section="servers", **srv)
            ok(f"server {host} satt")
    else:
        TODO.append(f"SABnzbd (http://{DASH}:{PORT['sab']}): legg inn Usenet-serveren under Config → Servers.")
    cats = ((api("get_config", section="categories") or {}).get("config") or {}).get("categories") or []
    if not any(c.get("name") == CATEGORY for c in cats):
        api("set_config", section="categories", name=CATEGORY, dir=CATEGORY)
    ok(f"kategorien {CATEGORY} finnes")
    STATE["sab_ok"] = True
    result("SABnzbd", True, "klar")


# =========================================================================== Lidarr
CAT_FIELDS = ("musicCategory", "category")
QUALITY = {"standard": ["standard", "high quality"], "lossless": ["lossless", "flac"], "any": ["any"]}


def setup_lidarr():
    key = arr_key("lidarr")
    if not key:
        return result("Lidarr", False, "fant ikke API-nøkkel (har containeren startet?)")
    STATE["lidarr_key"] = key
    a = Arr("Lidarr", LIDARR, key)
    if not wait_api(a, LIDARR):
        return result("Lidarr", False, "svarer ikke")
    a.set_auth()
    ok("innlogging satt")

    qp = a.get("qualityprofile")
    names = QUALITY.get(env("MUSIC_QUALITY", "Standard").lower(), [env("MUSIC_QUALITY").lower()])
    prof = next((p for n in names for p in qp if p["name"].lower() == n), None) \
        or next((p for p in qp if p["name"].lower() == "any"), qp[0] if qp else None)
    mp = a.get("metadataprofile")
    meta = next((p for p in mp if p["name"].lower() == "standard"), mp[0] if mp else None)
    if not (prof and meta):
        return result("Lidarr", False, "fant ingen kvalitets- eller metadataprofil")

    path = f"/data/{MUSIC}"
    roots = a.get("rootfolder")
    mine = next((r for r in roots if r["path"].rstrip("/") == path), None)
    want = {"defaultQualityProfileId": prof["id"], "defaultMetadataProfileId": meta["id"],
            "defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all"}
    if not mine:
        a.post("rootfolder", {"name": "Musikk", "path": path, "defaultTags": [], **want})
        ok(f"rotmappe {path} lagt til (kvalitet {prof['name']})")
    elif any(mine.get(k) != v for k, v in want.items() if k in mine):
        a.put(f"rootfolder/{mine['id']}", {**mine, **want})
        ok(f"rotmappe {path} bruker nå kvaliteten {prof['name']}")
    else:
        ok(f"rotmappe {path} finnes")
    artists = a.get("artist")
    for r in roots:                      # rydd bort gamle, tomme rotmapper under /data
        rp = r["path"].rstrip("/")
        if rp == path or not rp.startswith("/data/"):
            continue
        if not any((x.get("path") or "").startswith(rp + "/") for x in artists) and not _has_content(rp):
            try:
                a.call("DELETE", f"rootfolder/{r['id']}")
                ok(f"fjernet ubrukt rotmappe {rp}")
            except RuntimeError:
                pass

    try:
        dc = a.get("config/downloadclient")
        if isinstance(dc, dict) and not dc.get("autoRedownloadFailed", True):
            dc["autoRedownloadFailed"] = True
            a.put_config("downloadclient", dc)
        ok("prøver neste utgivelse automatisk hvis en nedlasting feiler")
    except (RuntimeError, ValueError):
        pass
    mm = a.get("config/mediamanagement")
    if isinstance(mm, dict) and not mm.get("copyUsingHardlinks", True):
        mm["copyUsingHardlinks"] = True
        a.put_config("mediamanagement", mm)
    ok("hardlinks på (ingen ekstra plass brukes mens filen deles)")
    try:
        nm = a.get("config/naming")
        if isinstance(nm, dict) and not nm.get("renameTracks", False):
            nm["renameTracks"] = True
            a.put_config("naming", nm)
        ok("ryddige navn: Artist/Album/01 - Låt")
    except RuntimeError:
        pass

    clients = []
    if QBIT:
        clients.append(("qBittorrent", "QBittorrent", STATE.get("qbit_ok"),
                        dict(zip(("host", "port"), hostport(QBIT, 8080)), username=USER, password=PASSWORD)))
    if NZBGET:
        clients.append(("NZBGet", "Nzbget", STATE.get("nzbget_ok"),
                        dict(zip(("host", "port"), hostport(NZBGET, 6789)), username=USER, password=PASSWORD)))
    if SAB:
        clients.append(("SABnzbd", "Sabnzbd", STATE.get("sab_ok"),
                        dict(zip(("host", "port"), hostport(SAB, 8080)), apiKey=STATE.get("sab_key", ""),
                             username=USER, password=PASSWORD)))
    missing = []
    for label, impl, ready, conn in clients:
        if not ready:
            missing.append(label)
            warn(f"{label} hoppet over – ikke klar (se {label} over)")
            continue
        try:
            v = a.upsert("downloadclient", impl, label,
                         {**conn, "useSsl": False, **{f: CATEGORY for f in CAT_FIELDS}}, {"priority": 1})
            ok(f"{label} {v} (kategori {CATEGORY})")
        except RuntimeError as e:
            missing.append(label)
            warn(f"{label}: {str(e)[:160]}")
    if missing:
        TODO.append(f"{' og '.join(missing)} var ikke klar(e). Når de kjører: «docker compose run --rm autoconfig».")
    result("Lidarr", True, "klar" if not missing else f"klar, men mangler {' og '.join(missing)}")


# =========================================================================== Prowlarr
MUSIC_CATS = (3000, 3010, 3040)


def has_music(ix):
    """Har indekseren musikkategorier (lyd, MP3 eller tapsfri)?"""
    def walk(cats):
        return any(c.get("id") in MUSIC_CATS or walk(c.get("subCategories") or []) for c in cats or [])
    return walk((ix.get("capabilities") or {}).get("categories"))


def prowlarr_key():
    if not SHARED:
        return arr_key("prowlarr")
    code, out = docker_exec(C["prowlarr"], ["cat", "/config/config.xml"], timeout=30)
    return key_from_xml(out) if code == 0 else ""


def setup_prowlarr():
    key = prowlarr_key()
    if not key:
        return result("Prowlarr", False, "fant ikke API-nøkkel" + (" (kjører Plex-stack?)" if SHARED else ""))
    STATE["prowlarr_key"] = key
    p = Arr("Prowlarr", PROWLARR, key)
    if not wait_api(p, PROWLARR):
        return result("Prowlarr", False, "svarer ikke")

    tag = None
    if not SHARED:                       # Plex-stack har allerede innlogging og FlareSolverr
        p.set_auth()
        ok("innlogging satt")
        tags = p.get("tag")
        tag = next((t for t in tags if t["label"] == "flaresolverr"), None) or p.post("tag", {"label": "flaresolverr"})
        p.upsert("indexerproxy", "FlareSolverr", "FlareSolverr",
                 {"host": "http://flaresolverr:8191/", "requestTimeout": 60}, {"tags": [tag["id"]]})
        ok("FlareSolverr-proxy med taggen «flaresolverr»")

    if STATE.get("lidarr_key"):
        v = p.upsert("applications", "Lidarr", "Lidarr (musikk)",
                     {"prowlarrUrl": PROWLARR, "baseUrl": LIDARR, "apiKey": STATE["lidarr_key"],
                      "syncCategories": list(MUSIC_CATS)},
                     {"syncLevel": "fullSync"},
                     match=lambda i: (field(i, "baseUrl") or "").rstrip("/") == LIDARR)
        ok(f"Lidarr {v} – musikkindeksere synkes automatisk dit")

    indexers = p.get("indexer")
    if tag:
        tagged = 0
        for ix in indexers:
            if tag["id"] not in ix.get("tags", []):
                ix.setdefault("tags", []).append(tag["id"])
                try:
                    p.put(f"indexer/{ix['id']}?forceSave=true", ix)
                    tagged += 1
                except RuntimeError:
                    pass
        if tagged:
            ok(f"{tagged} indeksere fikk FlareSolverr-taggen")
    music = [ix for ix in indexers if has_music(ix)]
    if not music:
        TODO.append(f"Prowlarr (http://{DASH}:{PORT['prowlarr']}): legg til minst én indekser som har "
                    "musikk (Indexers → Add Indexer). Den kobles automatisk til Lidarr.")
    result("Prowlarr", True, f"klar ({len(indexers)} indeksere, {len(music)} med musikk"
           + (", Plex-stackens)" if SHARED else ")"))


# =========================================================================== Navidrome
def setup_navidrome():
    if not wait_http(f"{ND}/ping", 240, "Navidrome"):
        return result("Navidrome", False, "svarer ikke")
    if not PASSWORD:
        TODO.append(f"Navidrome: åpne http://{DASH}:4533 og lag en administrator.")
        return result("Navidrome", False, "mangler ADMIN_PASSWORD i .env")
    creds = {"username": USER, "password": PASSWORD}
    r = http("POST", f"{ND}/auth/login", json_body=creds)
    if not r.ok:
        c = http("POST", f"{ND}/auth/createAdmin", json_body=creds)
        if c.ok:
            ok(f"administrator {USER} laget")
            r = c
        else:
            TODO.append("Navidrome: kom ikke inn med brukernavn/passord fra .env. Er passordet endret i appen? "
                        "Sett det tilbake, eller rett ADMIN_PASSWORD og kjør autoconfig igjen.")
            return result("Navidrome", False, f"innlogging feilet ({r.status})")
    jwt = (r.json() or {}).get("token", "")

    # Subsonic-innlogging (brukes av dashboardet og for å starte en skanning)
    salt = secrets.token_hex(6)
    token = hashlib.md5((PASSWORD + salt).encode()).hexdigest()
    STATE["nd"] = {"user": USER, "token": token, "salt": salt}
    q = urllib.parse.urlencode({"u": USER, "t": token, "s": salt, "v": "1.16.1", "c": "musikk-stack", "f": "json"})
    r = http("GET", f"{ND}/rest/startScan?{q}")
    if r.ok:
        ok(f"skanner /{MUSIC}")

    users = http("GET", f"{ND}/api/user", headers={"X-ND-Authorization": f"Bearer {jwt}"}).json()
    if isinstance(users, list) and len(users) <= 1:
        TODO.append("Navidrome: lag en egen bruker til hver i familien (brukerikonet → Brukere). "
                    "Da får alle egne spillelister, favoritter og avspillingshistorikk.")
    result("Navidrome", True, "klar – lytt i nettleseren eller i en Subsonic-app")


# =========================================================================== Homepage
def setup_homepage():
    k = STATE
    src = " (Plex-stack)" if SHARED else ""

    def svc(name, icon, port, desc, cont=None, widget=None, href=True):
        lines = [f"    - {name}:", f"        icon: {icon}", f"        description: {desc}"]
        if href:
            lines.append(f"        href: http://{DASH}:{port}")
        if cont:
            lines += ["        server: my-docker", f"        container: {cont}"]
        if widget:
            lines.append("        widget:")
            lines += [f"          {a}: {json.dumps(b)}" for a, b in widget.items()]
        return "\n".join(lines)

    def w(typ, url, key=None, **extra):
        d = {"type": typ, "url": url}
        if key:
            d["key"] = key
        d.update(extra)
        return d

    nd = k.get("nd")
    groups = {
        "Lytt": [svc("Navidrome", "navidrome.png", 4533, "Musikken din", "navidrome",
                     w("navidrome", ND, None, **nd) if nd else None)],
        "Bibliotek": [svc("Lidarr", "lidarr.png", 8686, "Følg artister, hent album", "lidarr",
                          w("lidarr", LIDARR, k["lidarr_key"]) if k.get("lidarr_key") else None)],
        "Nedlasting": [svc("Prowlarr", "prowlarr.png", PORT["prowlarr"], "Indeksere" + src, C["prowlarr"],
                           w("prowlarr", PROWLARR, k["prowlarr_key"]) if k.get("prowlarr_key") else None)],
    }
    dl = groups["Nedlasting"]
    if SAB:
        dl.insert(0, svc("SABnzbd", "sabnzbd.png", PORT["sab"], "Usenet", C["sab"],
                         w("sabnzbd", SAB, k["sab_key"]) if k.get("sab_key") else None))
    if NZBGET:
        dl.insert(0, svc("NZBGet", "nzbget.png", PORT["nzbget"], "Usenet" + src, C["nzbget"],
                         w("nzbget", NZBGET, None, username=USER, password=PASSWORD) if PASSWORD else None))
    if QBIT:
        dl.insert(0, svc("qBittorrent", "qbittorrent.png", PORT["qbit"],
                         "Torrent" + (" via VPN" if "gluetun" in QBIT else "") + src, C["qbit"],
                         w("qbittorrent", QBIT, None, username=USER, password=PASSWORD) if PASSWORD else None))
        if "gluetun" in QBIT:
            dl.insert(1, svc("Gluetun", "gluetun.png", 0, "VPN-tunnel" + src, C["gluetun"], href=False))
    if "kjoling" in PROFILES:
        groups["Vedlikehold"] = [
            svc("Kjøling", "mdi-thermometer", 3012, "Maskinens belastning – roer ned ved behov", "musikk-kjoling",
                {"type": "customapi", "url": "http://kjoling:3012/api/status", "refreshInterval": 10000,
                 "mappings": [{"field": "status", "label": "Modus"},
                              {"field": "temp", "label": "Temp", "format": "number", "suffix": " °C"},
                              {"field": "cpu", "label": "Prosessor", "format": "number", "suffix": " %"},
                              {"field": "mem", "label": "Minne", "format": "number", "suffix": " %"}]}),
            svc("FlareSolverr", "flaresolverr.png", 0, "Cloudflare-hjelper", "musikk-flaresolverr", href=False),
        ]

    out = ["# Laget av autoconfig – kjøres på nytt med «docker compose run --rm autoconfig»", ""]
    for g, items in groups.items():
        out.append(f"- {g}:")
        out += items
    write(f"{CFG}/homepage/services.yaml", "\n".join(out) + "\n", 0o600, (PUID, PGID))
    result("Homepage", True, f"dashboard på http://{DASH}:3200")


# =========================================================================== oppsummering
def summary():
    lines = ["", "═" * 66, "  OPPSUMMERING", "═" * 66]
    for app, good, msg in RESULTS:
        lines.append(f"  {'✔' if good else '⚠'} {app:<12} {msg}")
    lines += ["", f"  Lytt:        http://{DASH}:4533",
              f"  Mobil:       en Subsonic-app (Symfonium, Amperfy …), server http://{env('LAN_IP') or DASH}:4533",
              f"  Dashboard:   http://{DASH}:3200",
              f"  Mappe:       {MUSIC}/ i mediemappa"]
    if PASSWORD:
        lines.append(f"  Innlogging:  {USER} / {PASSWORD}")
    if TODO:
        lines += ["", "  Gjør dette for hånd:"]
        lines += [f"   • {t}" for t in dict.fromkeys(TODO)]
    lines += ["", "  Kjør oppsettet på nytt når som helst:  docker compose run --rm autoconfig", ""]
    text = "\n".join(lines)
    log(text)
    write(f"{CFG}/OPPSUMMERING.txt", text.replace(f" / {PASSWORD}", " / (se .env)") if PASSWORD else text,
          owner=(PUID, PGID))


def main():
    log("autoconfig: setter opp og kobler sammen appene. Første gang tar dette 2–5 minutter.")
    log(f"Musikk: /data/{MUSIC}" + ("   Nedlasting: Plex-stack" if SHARED else ""))
    run("Navidrome", setup_navidrome)
    if QBIT:
        run("qBittorrent", setup_qbit)
    if NZBGET:
        run("NZBGet", setup_nzbget)
    if SAB:
        run("SABnzbd", setup_sab)
    run("Lidarr", setup_lidarr)
    run("Prowlarr", setup_prowlarr)
    run("Homepage", setup_homepage)
    summary()


if __name__ == "__main__":
    main()
    sys.exit(0)
