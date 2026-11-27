#!/bin/bash
# Turn on off-site backups:  sudo /opt/monitor/scripts/offsite-setup.sh <git-repo> [age-public-key]
#   git-repo        a PRIVATE repo, e.g. git@github.com:you/lanpulse-backups.git
#   age-public-key  age1... to encrypt to. Leave it out to create a new key pair: the private key is
#                   written to ./lanpulse-backup-key.txt for you to move OFF the box (password manager,
#                   another computer). Without it the backups can't be opened, so keep two copies.
# Prints the deploy key to add to the repo (GitHub: Settings > Deploy keys > Add, tick "Allow write").
set -euo pipefail
APP=${APP:-/opt/monitor}
REPO=${1:?usage: offsite-setup.sh <git-repo> [age-public-key]}
RCPT=${2:-}
command -v age >/dev/null || { echo "age isn't installed: run box/setup.sh first"; exit 1; }
if [ -z "$RCPT" ]; then
  umask 077
  age-keygen -o ./lanpulse-backup-key.txt 2>/dev/null
  RCPT=$(age-keygen -y ./lanpulse-backup-key.txt)
  echo "New key pair. Private key: $(pwd)/lanpulse-backup-key.txt  <- move it off this box now, keep two copies."
fi
KEY=$APP/config/offsite_key
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "lanpulse-backup@$(hostname)" -f "$KEY"
chmod 600 "$KEY"
ENVF=$APP/lanpulse.env
sed -i '/^BACKUP_AGE_RECIPIENT=/d;/^BACKUP_GIT_REPO=/d' "$ENVF"
printf 'BACKUP_AGE_RECIPIENT=%s\nBACKUP_GIT_REPO=%s\n' "$RCPT" "$REPO" >> "$ENVF"
echo "Saved to $ENVF. Deploy key for the repo (needs write access):"
cat "$KEY.pub"
echo "Then test with: sudo /opt/monitor/scripts/backup.sh"
