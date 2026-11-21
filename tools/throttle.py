"""Kjøling: måler maskinen og roer ned bakgrunnsappene når den blir varm eller travel.

Navidrome begrenses ALDRI – avspilling går som normalt.
I rolig modus:
  • bakgrunnsappene får en lav prosessorgrense (docker update, ingen omstart)
  • qBittorrent bytter til alternative (lave) fartsgrenser
  • SABnzbd venter med utpakking/reparasjon
Alt settes tilbake når maskinen har roet seg.

Nettside med status og knapper: http://<server>:3012   API: /api/status
"""
import glob
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from common import CFG, docker, env, http, ini_get, log, read, write  # noqa: E402

BG = [c.strip() for c in env("THROTTLE_CONTAINERS", "lidarr,musikk-qbittorrent,musikk-sabnzbd,"
                                                    "musikk-prowlarr,musikk-flaresolverr,musikk-homepage").split(",") if c.strip()]
HOT = float(env("HOT_TEMP", "85"))          # °C – roe ned over dette
COOL = float(env("COOL_TEMP", "72"))        # °C – tilbake til normal under dette
HIGH = float(env("HIGH_LOAD", "90"))        # % prosessor over tid – roe ned
LOW = float(env("LOW_LOAD", "50"))          # % prosessor over tid – tilbake til normal
CALM_CPUS = float(env("CALM_CPUS", "0.3"))  # prosessorgrense per bakgrunnsapp i rolig modus
HOT_SECS, COOL_SECS, TICK = 120, 300, 10
STATE_FILE = f"{CFG}/throttle/state.json"
QBIT = env("QBIT_URL").rstrip("/")
SAB = env("SAB_URL").rstrip("/")
PORT = 3012
USER, PASSWORD = env("ADMIN_USER", "admin"), env("ADMIN_PASSWORD")

LOCK = threading.Lock()
S = {"mode": "auto", "calm": False, "reason": "", "since": time.time(), "orig": {},
     "temp": None, "cpu": None, "mem": None, "history": []}


# --------------------------------------------------------------------------- målinger
def cpu_temp():
    vals = []
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_input") + glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            v = int(open(f).read().strip()) / 1000
            if 5 < v < 130:
                vals.append(v)
        except (OSError, ValueError):
            pass
    return round(max(vals), 1) if vals else None


_last_cpu = None


def cpu_percent():
    """Hele maskinens prosessorbruk siden forrige måling (fra /proc/stat)."""
    global _last_cpu
    try:
        f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle, total = f[3] + (f[4] if len(f) > 4 else 0), sum(f)
    prev, _last_cpu = _last_cpu, (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def mem_percent():
    try:
        m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        return round(100 * (1 - m["MemAvailable"] / m["MemTotal"]), 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------- handlinger
def set_cpus(name, nano):
    s, _ = docker("POST", f"/containers/{name}/update", {"NanoCpus": int(nano)})
    return s == 200


def container_nanocpus(name):
    s, b = docker("GET", f"/containers/{name}/json")
    if s != 200:
        return None
    c = json.loads(b)
    if not c.get("State", {}).get("Running"):
        return None
    return c.get("HostConfig", {}).get("NanoCpus", 0) or 0


def qbit_alt_speed(on):
    if not QBIT:
        return
    r = http("GET", f"{QBIT}/api/v2/transfer/speedLimitsMode", headers={"Referer": QBIT})
    if r.ok and r.text.strip() in ("0", "1") and (r.text.strip() == "1") != on:
        http("POST", f"{QBIT}/api/v2/transfer/toggleSpeedLimitsMode", headers={"Referer": QBIT})


def sab(mode):
    """SABnzbd: pause_pp / resume_pp (utpakking og reparasjon)."""
    if not SAB:
        return
    key = ini_get(read(f"{CFG}/sabnzbd/sabnzbd.ini"), "misc", "api_key")
    if key:
        http("GET", f"{SAB}/api?mode={mode}&apikey={key}&output=json", timeout=10)


def calm_on(reason):
    target = int(CALM_CPUS * 1e9)
    for name in BG:
        cur = container_nanocpus(name)
        if cur is None:
            continue
        S["orig"].setdefault(name, cur)
        orig = S["orig"][name]
        set_cpus(name, min(target, orig) if orig else target)
    qbit_alt_speed(True)
    sab("pause_pp")
    S.update(calm=True, reason=reason, since=time.time())
    log(f"{time.strftime('%H:%M')} ROLIG: {reason}")
    save()


def calm_off(reason):
    for name, orig in list(S["orig"].items()):
        set_cpus(name, orig)
    S["orig"] = {}
    qbit_alt_speed(False)
    sab("resume_pp")
    S.update(calm=False, reason=reason, since=time.time())
    log(f"{time.strftime('%H:%M')} NORMAL: {reason}")
    save()


def save():
    write(STATE_FILE, json.dumps({k: S[k] for k in ("mode", "calm", "reason", "since", "orig")}))


def load():
    try:
        S.update(json.loads(read(STATE_FILE) or "{}"))
    except ValueError:
        pass


# --------------------------------------------------------------------------- regler
def sustained(hist, secs, test):
    now = time.time()
    window = [h for h in hist if now - h[0] <= secs]
    return bool(window) and now - window[0][0] >= secs * 0.8 and all(test(h) for h in window)


def decide():
    now = time.time()
    t, c, m = cpu_temp(), cpu_percent(), mem_percent()
    S.update(temp=t, cpu=c if c is not None else S["cpu"], mem=m)
    S["history"] = [h for h in S["history"] if now - h[0] <= COOL_SECS + 30] + [(now, t, S["cpu"])]
    hist = S["history"]
    if S["mode"] == "calm":
        if not S["calm"]:
            calm_on("slått på manuelt")
        return
    if S["mode"] == "normal":
        if S["calm"]:
            calm_off("slått av manuelt")
        return
    hot_temp = t is not None and t >= HOT
    busy = sustained(hist, HOT_SECS, lambda h: h[2] is not None and h[2] >= HIGH)
    cool = sustained(hist, COOL_SECS, lambda h: (h[1] is None or h[1] <= COOL) and (h[2] is None or h[2] <= LOW))
    if not S["calm"] and (hot_temp or busy):
        calm_on(f"{t:.0f} °C" if hot_temp else f"prosessoren over {HIGH:.0f} % i {HOT_SECS // 60} min")
    elif S["calm"] and cool:
        calm_off("maskinen har roet seg")


def status():
    mins = int((time.time() - S["since"]) // 60)
    return {
        "status": "Rolig" if S["calm"] else "Normal",
        "modus": {"auto": "Automatisk", "calm": "Alltid rolig", "normal": "Aldri rolig"}[S["mode"]],
        "temp": S["temp"], "cpu": S["cpu"], "mem": S["mem"],
        "grunn": S["reason"] or "–", "siden": f"{mins // 60} t {mins % 60} min" if mins >= 60 else f"{mins} min",
        "grenser": {"varm": HOT, "kjolig": COOL, "travel": HIGH},
    }


# --------------------------------------------------------------------------- nettside
PAGE = """<!doctype html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>Kjøling</title><style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;max-width:520px;margin:2rem auto;padding:0 1rem}}
.card{{background:#1e293b;border-radius:12px;padding:1.2rem;margin-bottom:1rem}}
.big{{font-size:2rem;font-weight:600}} .row{{display:flex;justify-content:space-between;padding:.3rem 0}}
.calm{{color:#38bdf8}} .normal{{color:#4ade80}} button{{font-size:1rem;padding:.7rem 1rem;margin:.2rem;border:0;
border-radius:8px;background:#334155;color:#e2e8f0;cursor:pointer}} button.on{{background:#2563eb}}
small{{color:#94a3b8}}</style></head><body>
<div class="card"><div class="big {cls}">{status}</div><small>{grunn} · {siden}</small></div>
<div class="card"><div class="row"><span>Temperatur</span><b>{temp}</b></div>
<div class="row"><span>Prosessor</span><b>{cpu}</b></div><div class="row"><span>Minne</span><b>{mem}</b></div></div>
<div class="card"><form method="post">
<button name="mode" value="auto" class="{a}">Automatisk</button>
<button name="mode" value="calm" class="{c}">Alltid rolig</button>
<button name="mode" value="normal" class="{n}">Aldri rolig</button></form>
<small>Automatisk: rolig over {hot:.0f} °C eller over {high:.0f} % prosessor i 2 min, normal igjen under
{cool:.0f} °C og {low:.0f} % i 5 min. Navidrome begrenses aldri.</small></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        with LOCK:
            st = status()
        if self.path.startswith("/api/status"):
            return self.send(200, json.dumps(st), "application/json")
        fmt = lambda v, u: "–" if v is None else f"{v:.0f} {u}"  # noqa: E731
        self.send(200, PAGE.format(
            cls="calm" if st["status"] == "Rolig" else "normal", status=st["status"], grunn=st["grunn"],
            siden=st["siden"], temp=fmt(st["temp"], "°C"), cpu=fmt(st["cpu"], "%"), mem=fmt(st["mem"], "%"),
            a="on" if S["mode"] == "auto" else "", c="on" if S["mode"] == "calm" else "",
            n="on" if S["mode"] == "normal" else "", hot=HOT, high=HIGH, cool=COOL, low=LOW),
            "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        mode = urllib.parse.parse_qs(self.rfile.read(n).decode()).get("mode", [""])[0]
        if mode in ("auto", "calm", "normal"):
            with LOCK:
                S["mode"] = mode
                save()
                decide()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def main():
    load()
    if S["calm"] and S["mode"] != "calm":    # startet på nytt mens den var rolig – rydd opp
        calm_off("kjøling startet på nytt")
    log(f"Kjøling startet. Rolig over {HOT:.0f} °C / {HIGH:.0f} % prosessor, normal under "
        f"{COOL:.0f} °C / {LOW:.0f} %. Temperatur nå: {cpu_temp()} °C. Side: http://<server>:3012")
    cpu_percent()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    while True:
        time.sleep(TICK)
        with LOCK:
            try:
                decide()
            except Exception as e:  # måling eller Docker-kall feilet – prøv igjen neste runde
                log(f"feil: {e}")


if __name__ == "__main__":
    main()
