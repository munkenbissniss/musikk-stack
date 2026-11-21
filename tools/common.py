"""Felles hjelpefunksjoner for setup.py, init.py og autoconfig.py.
Bare Pythons standardbibliotek – ingenting trenger å installeres."""
import http.client as httpclient
import http.cookiejar as cookiejar
import json
import os
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

CFG = "/config"
DATA = "/data"


# --------------------------------------------------------------------------- logg
def log(msg=""):
    print(msg, flush=True)


def step(msg):
    log(f"\n== {msg} ==")


def ok(msg):
    log(f"  ✔ {msg}")


def warn(msg):
    log(f"  ⚠ {msg}")


def env(name, default=""):
    v = os.environ.get(name, "")
    return v if v != "" else default


# --------------------------------------------------------------------------- HTTP
class Resp:
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body or b"", headers or {}

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    @property
    def ok(self):
        return 200 <= self.status < 300


class Session:
    """HTTP-klient med informasjonskapsler (trengs for qBittorrent)."""

    def __init__(self, cookies=True):
        handlers = [urllib.request.HTTPCookieProcessor(cookiejar.CookieJar())] if cookies else []
        self.opener = urllib.request.build_opener(*handlers)

    def http(self, method, url, headers=None, json_body=None, form=None, timeout=30):
        data, h = None, {"User-Agent": "musikk-stack-autoconfig"}
        h.update(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h.setdefault("Content-Type", "application/json")
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return Resp(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read(), dict(e.headers or {}))
        except Exception as e:  # nettverksfeil, tidsavbrudd …
            return Resp(0, str(e).encode())


_default = Session(cookies=False)


def http(method, url, **kw):
    return _default.http(method, url, **kw)


def wait_http(url, timeout=300, name=None, headers=None):
    """Venter til en tjeneste svarer (med hvilken som helst HTTP-kode)."""
    end = time.time() + timeout
    while time.time() < end:
        r = http("GET", url, headers=headers, timeout=10)
        if r.status:
            return True
        time.sleep(3)
    warn(f"{name or url} svarte ikke innen {timeout} sekunder")
    return False


def wait_file(path, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(3)
    return False


# --------------------------------------------------------------------------- Docker
DOCKER_SOCK = "/var/run/docker.sock"


class _UnixConn(httpclient.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def docker(method, path, body=None, timeout=120):
    if not os.path.exists(DOCKER_SOCK):
        return 0, b""
    try:
        c = _UnixConn(DOCKER_SOCK, timeout)
        h = {"Host": "docker"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
        c.close()
        return r.status, b
    except Exception as e:
        return 0, str(e).encode()


def demux(b):
    """Docker sender stdout/stderr i rammer med 8 byte hode. Fjern hodene."""
    if len(b) < 8 or b[0] not in (0, 1, 2) or b[1:4] != b"\x00\x00\x00":
        return b
    out, i = bytearray(), 0
    while i + 8 <= len(b):
        size = int.from_bytes(b[i + 4:i + 8], "big")
        out += b[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(out)


def docker_info():
    s, b = docker("GET", "/info")
    return json.loads(b) if s == 200 else {}


def container(name):
    s, b = docker("GET", f"/containers/{name}/json")
    return json.loads(b) if s == 200 else None


def container_running(name):
    c = container(name)
    return bool(c and c.get("State", {}).get("Running"))


def wait_container(name, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if container_running(name):
            return True
        time.sleep(3)
    return False


def docker_logs(name, tail=1000):
    s, b = docker("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail={tail}")
    return demux(b).decode("utf-8", errors="replace") if s == 200 else ""


def docker_exec(name, cmd, timeout=900):
    s, b = docker("POST", f"/containers/{name}/exec",
                  {"AttachStdout": True, "AttachStderr": True, "Cmd": cmd})
    if s != 201:
        return None, b.decode(errors="replace")
    eid = json.loads(b)["Id"]
    s, b = docker("POST", f"/exec/{eid}/start", {"Detach": False, "Tty": False}, timeout=timeout)
    out = demux(b).decode("utf-8", errors="replace")
    s2, b2 = docker("GET", f"/exec/{eid}/json")
    code = json.loads(b2).get("ExitCode") if s2 == 200 else None
    return code, out


def docker_action(name, action):  # start | stop | restart
    s, _ = docker("POST", f"/containers/{name}/{action}?t=15", timeout=60)
    return s in (204, 304)


# --------------------------------------------------------------------------- konfigfiler
def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write(path, text, mode=None, owner=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    if owner is not None:
        try:
            os.chown(path, *owner)
        except OSError:
            pass


def xml_apikey(path):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", read(path))
    return m.group(1).strip() if m else ""


def ini_set(text, section, key, value, sep=" = "):
    """Setter key = value i [section] uten å røre resten av fila."""
    lines = text.splitlines()
    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    start = next((i for i, l in enumerate(lines) if (m := sec_re.match(l)) and m.group(1) == section), None)
    if start is None:
        lines += ["", f"[{section}]", f"{key}{sep}{value}"]
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if sec_re.match(lines[i])), len(lines))
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = f"{key}{sep}{value}"
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, f"{key}{sep}{value}")
    return "\n".join(lines) + "\n"


def ini_get(text, section, key):
    cur = None
    for l in text.splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", l)
        if m:
            cur = m.group(1)
            continue
        if cur == section:
            m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", l)
            if m:
                return m.group(1).strip('"')
    return ""


# --------------------------------------------------------------------------- mediemapper
MUSIC_NAMES = ["Musikk", "Music", "Musik", "Musikkbibliotek", "Låter", "Album"]
SKIP = {"downloads", "lost+found", "recycle", "$recycle.bin", "@eadir", "#recycle"}


def _has_content(path):
    try:
        with os.scandir(path) as it:
            return any(not e.name.startswith(".") for e in it)
    except OSError:
        return False


def _norm(name):
    name = unicodedata.normalize("NFC", name)   # macOS lagrer filnavn dekomponert
    return name.lower().replace("å", "a").replace(" ", "")


def music_dir(data=None):
    """Finner musikkmappa (øverst eller én mappe ned, f.eks. Media/Music).
    Mapper med innhold vinner over tomme. Returnerer sti relativt til mediemappa."""
    data = data or DATA
    if env("MUSIC_DIR"):
        return env("MUSIC_DIR").strip("/")
    found = []
    try:
        for e in os.scandir(data):
            if e.is_dir() and not e.name.startswith(".") and e.name.lower() not in SKIP:
                found.append((e.name, 1))
                try:
                    for e2 in os.scandir(e.path):
                        if e2.is_dir() and not e2.name.startswith(".") and e2.name.lower() not in SKIP:
                            found.append((f"{e.name}/{e2.name}", 2))
                except OSError:
                    pass
    except OSError:
        pass
    order = [_norm(n) for n in MUSIC_NAMES]
    hits = [(rel, depth) for rel, depth in found if _norm(rel.rsplit("/", 1)[-1]) in order]
    if not hits:
        return "Musikk"
    hits.sort(key=lambda h: (not _has_content(os.path.join(data, h[0])), h[1],
                             order.index(_norm(h[0].rsplit("/", 1)[-1]))))
    return hits[0][0]
