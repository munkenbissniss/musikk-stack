#!/bin/bash
# MIDLERTIDIG: monterer mediedisken fra en annen maskin (NFS) på /mnt/media,
# slik at alt er satt opp med de samme stiene som når den nye disken kommer.
# Kjør:  sudo ./linux/02-mount-nfs.sh
# Når den nye disken er på plass: sudo ./linux/05-move-to-new-disk.sh
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Kjør med sudo: sudo $0" >&2; exit 1; }
MNT=/mnt/media

apt-get install -y nfs-common
read -r -p "IP-adressen til maskinen med disken (f.eks. 192.168.1.20): " HOST
echo "Delinger på $HOST:"
showmount -e "$HOST" || { echo "Fikk ikke kontakt. Er NFS satt opp der (README steg 5B)?" >&2; exit 1; }
read -r -p "Hvilken sti skal monteres? (kopier fra listen over): " SRC

mountpoint -q "$MNT" && umount "$MNT"
mkdir -p "$MNT"
chattr +i "$MNT" 2>/dev/null || true

cp /etc/fstab "/etc/fstab.bak.$(date +%s)"
sed -i "\|[[:space:]]$MNT[[:space:]]|d" /etc/fstab
# Mellomrom i stien må skrives som \040 i fstab
SRC_FSTAB=$(printf '%s' "$SRC" | sed 's/ /\\040/g')
echo "$HOST:$SRC_FSTAB  $MNT  nfs  defaults,_netdev,nofail,x-systemd.mount-timeout=30s  0  0" >> /etc/fstab
systemctl daemon-reload
mount "$MNT"
echo "Montert: $(findmnt -no SOURCE,FSTYPE "$MNT")"

mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/wait-for-media.conf <<EOF
[Unit]
RequiresMountsFor=$MNT
EOF
systemctl daemon-reload

# Test at vi kan skrive
SUDO_U="${SUDO_USER:-$(logname)}"
if sudo -u "$SUDO_U" touch "$MNT/.skrivetest" 2>/dev/null; then
  sudo -u "$SUDO_U" rm -f "$MNT/.skrivetest"; echo "Skrivetest OK for $SUDO_U."
else
  echo "ADVARSEL: $SUDO_U kan ikke skrive til $MNT. Sjekk at brukeren eier mappene på"
  echo "den andre maskinen, og at UID er lik (id -u på begge maskiner)."
fi
echo
echo "Ferdig. Neste steg: docker compose run --rm setup"
