#!/bin/bash
# Gjør den nyinstallerte MacBooken klar som server.
# Kjør:  sudo ./linux/01-prepare-host.sh
#  - oppdaterer systemet, setter tidssone
#  - ignorerer lokket og slår av dvale (serveren skal stå på hele tiden)
#  - skrur av skjermen etter 1 minutt
#  - viftestyring for MacBook (mbpfan)
#  - installerer Docker, Tailscale, avahi (<maskinnavn>.local) og verktøy
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Kjør med sudo: sudo $0" >&2; exit 1; }
USER_NAME="${SUDO_USER:-$(logname)}"

echo "== Oppdaterer systemet =="
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
timedatectl set-timezone Europe/Oslo

echo "== Lokk og dvale =="
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/10-server.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

echo "== Skjerm av etter 60 sek =="
if ! grep -q consoleblank /etc/default/grub; then
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 consoleblank=60"/' /etc/default/grub
  update-grub
fi

echo "== Verktøy =="
apt-get install -y curl ca-certificates unzip avahi-daemon intel-gpu-tools lm-sensors htop
if apt-get install -y mbpfan; then
  systemctl enable --now mbpfan
else
  echo "  (mbpfan ikke tilgjengelig – Macen styrer viftene selv, bare litt mindre aktivt)"
fi

echo "== Docker =="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker "$USER_NAME"
systemctl enable docker

echo "== Tailscale =="
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo
echo "Ferdig! Start på nytt nå:  sudo reboot"
echo "Logg inn igjen etterpå, og kjør:  sudo ./linux/02-mount-disk.sh"
