"""Kjører før alt annet: lager mapper med riktige eiere, så ingen container
starter mot feil rettigheter. Avslutter alltid med 0 (feil logges)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import CFG, DATA, env, log, music_dir, ok, step, warn, write  # noqa: E402

PUID, PGID = int(env("PUID", "1000")), int(env("PGID", "1000"))
APPS = ["navidrome", "lidarr", "sabnzbd", "gluetun", "qbittorrent", "prowlarr", "homepage",
        "throttle", "musikk"]


def chown(path, uid=PUID, gid=PGID):
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass  # Docker Desktop (Mac/Windows) håndterer rettigheter selv


def mkdir(path, uid=PUID, gid=PGID):
    new = not os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    if new or os.stat(path).st_uid == 0:
        chown(path, uid, gid)
    return new


def prepare_media():
    """Kjøres som PUID:PGID – da får mappene riktig eier, også på NFS (der root ikke får skrive)."""
    if not os.access(DATA, os.W_OK):
        warn(f"Kan ikke skrive til mediemappa som bruker {PUID}. Sjekk DATA_ROOT/delingen og rettighetene.")
        return
    music = music_dir()
    for d in (music, "downloads", "downloads/incomplete", "downloads/music",
              "downloads/usenet/incomplete", "downloads/usenet/complete",
              "downloads/usenet/complete/music", "downloads/usenet/completed/music"):
        p = os.path.join(DATA, d)
        if not os.path.isdir(p):
            try:
                os.makedirs(p, exist_ok=True)
                ok(f"laget {d}/")
            except OSError as e:
                warn(f"kunne ikke lage {d}/: {e}")
    ok(f"musikk: {music}/")

def run_as_user(fn):
    """Kjører fn i en egen prosess som PUID:PGID."""
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.setgid(PGID)
            os.setuid(PUID)
            fn()
        except Exception as e:
            warn(f"mediemappe: {e}")
            code = 1
        finally:
            sys.stdout.flush()
            os._exit(code)
    os.waitpid(pid, 0)


def main():
    step("Konfigmapper")
    for app in APPS:
        mkdir(os.path.join(CFG, app))
    ok(f"{len(APPS)} mapper klare")

    # Dashboard-maler (overskrives av autoconfig med nøkler og widgets)
    for f in os.listdir("/stack/homepage"):
        dst = os.path.join(CFG, "homepage", f)
        if not os.path.exists(dst):
            shutil.copy(os.path.join("/stack/homepage", f), dst)
            chown(dst)

    # Navidrome leser musikkmappa fra denne fila (mappa finnes automatisk)
    music = music_dir().replace('"', '\\"')
    write(os.path.join(CFG, "navidrome", "navidrome.toml"),
          "# Laget av init – endres automatisk. Egne innstillinger: bruk ND_-variabler i compose.yaml.\n"
          f'MusicFolder = "/media/{music}"\n', owner=(PUID, PGID))
    ok(f"Navidrome: musikk i /media/{music}")

    step("Mediedisk")
    run_as_user(prepare_media)
    log("\ninit ferdig.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # stopp aldri hele stacken på grunn av init
        warn(f"init: {e}")
    sys.exit(0)
