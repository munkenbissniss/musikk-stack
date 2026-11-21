#!/bin/bash
# Monterer den eksterne mediedisken fast på /mnt/media.
# Kjør:  sudo ./linux/02-mount-disk.sh
#  - monterer automatisk ved oppstart (via UUID, tåler at disken mangler)
#  - Docker venter på disken, og /mnt/media er låst mot skriving når disken
#    ikke er montert, så containerne aldri fyller den interne SSD-en ved et uhell
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Kjør med sudo: sudo $0" >&2; exit 1; }
USER_NAME="${SUDO_USER:-$(logname)}"
U=$(id -u "$USER_NAME"); G=$(id -g "$USER_NAME")
MNT=/mnt/media

echo "Disker og partisjoner:"
lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL,MOUNTPOINTS
echo
read -r -p "Hvilken partisjon er mediedisken? (f.eks. sdb2): " P
DEV="/dev/${P#/dev/}"
[ -b "$DEV" ] || { echo "Finner ikke $DEV" >&2; exit 1; }
FSTYPE=$(blkid -s TYPE -o value "$DEV")
UUID=$(blkid -s UUID -o value "$DEV")
echo "Filsystem: $FSTYPE   UUID: $UUID"

BASE="nofail,x-systemd.device-timeout=30s"
case "$FSTYPE" in
  ext4|xfs|btrfs)
    OPTS="defaults,$BASE"; MOUNTTYPE="$FSTYPE"; OWN=1
    echo "  Perfekt – Linux-filsystem med hardlinks og riktige rettigheter." ;;
  exfat)
    OPTS="defaults,$BASE,uid=$U,gid=$G,umask=002"; MOUNTTYPE=exfat; OWN=0
    echo "  OK. exFAT fungerer, men støtter ikke hardlinks (nedlastinger kopieres)." ;;
  ntfs)
    OPTS="defaults,$BASE,uid=$U,gid=$G,umask=002"; MOUNTTYPE=ntfs3; OWN=0
    echo "  OK. NTFS brukes med kjernens ntfs3-driver." ;;
  hfsplus)
    echo "  ADVARSEL: HFS+ (Mac OS Extended). Linux kan bare skrive til den hvis"
    echo "  journalføring er slått av (gjøres på en Mac: diskutil disableJournal)."
    read -r -p "  Er journalføring slått av? [j/N] " J
    [[ "$J" =~ ^[jJyY]$ ]] || { echo "Avbrutt. Se README, steg 0." >&2; exit 1; }
    OPTS="defaults,$BASE,uid=$U,gid=$G,umask=002"; MOUNTTYPE=hfsplus; OWN=0 ;;
  apfs)
    echo "FEIL: APFS kan ikke skrives til fra Linux. Se README, steg 0." >&2; exit 1 ;;
  *)
    echo "FEIL: Ukjent filsystem '$FSTYPE'." >&2; exit 1 ;;
esac

# Monteringspunkt, låst mot skriving når disken ikke er montert
mountpoint -q "$MNT" && umount "$MNT"
mkdir -p "$MNT"
chattr +i "$MNT" 2>/dev/null || true

# fstab
cp /etc/fstab "/etc/fstab.bak.$(date +%s)"
sed -i "\|[[:space:]]$MNT[[:space:]]|d" /etc/fstab
echo "UUID=$UUID  $MNT  $MOUNTTYPE  $OPTS  0  2" >> /etc/fstab
systemctl daemon-reload
mount "$MNT"
echo "Montert: $(findmnt -no SOURCE,FSTYPE,SIZE "$MNT" 2>/dev/null || df -h "$MNT" | tail -1)"

if [ "$OWN" = 1 ]; then
  chown "$U:$G" "$MNT"
  echo "  Toppmappen eies nå av $USER_NAME. Eksisterende mapper med feil eier fikser du med:"
  echo "  sudo chown -R $USER_NAME:$USER_NAME $MNT/Filmer $MNT/Serier"
fi

# Docker skal vente på disken
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/wait-for-media.conf <<EOF
[Unit]
RequiresMountsFor=$MNT
EOF
systemctl daemon-reload
echo
echo "Ferdig. Disken monteres automatisk ved oppstart, og Docker starter etter den."
echo "Neste steg: docker compose run --rm setup"
