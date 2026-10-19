#!/bin/sh
# Installs dependencies and the systemd service on Raspberry Pi OS (Bookworm).
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

sudo apt-get update
sudo apt-get install -y mpv ffmpeg python3-requests v4l-utils

# Formerly called dropbox-signage: replace the old service and keep the
# Dropbox link and the local copy by moving them to the new name.
if [ -f /etc/systemd/system/dropbox-signage.service ]; then
    sudo systemctl disable --now dropbox-signage.service
    sudo rm /etc/systemd/system/dropbox-signage.service
fi
for d in "$HOME_DIR/.config" "$HOME_DIR/.cache"; do
    if [ -d "$d/dropbox-signage" ] && [ ! -e "$d/monsterscreen" ]; then
        mv "$d/dropbox-signage" "$d/monsterscreen"
    fi
done
if [ -f "$DIR/settings.toml" ]; then
    sed -i 's|/dropbox-signage|/monsterscreen|g' "$DIR/settings.toml"
else
    cp "$DIR/settings.example.toml" "$DIR/settings.toml"
fi

sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" \
    "$DIR/systemd/monsterscreen.service" | sudo tee /etc/systemd/system/monsterscreen.service >/dev/null

# The setup page adds and removes wifi networks; allow that without a password.
sudo mkdir -p /etc/polkit-1/rules.d
sed -e "s|__USER__|$USER_NAME|g" "$DIR/polkit/monsterscreen-wifi.rules" \
    | sudo tee /etc/polkit-1/rules.d/50-monsterscreen-wifi.rules >/dev/null

# The login prompt on the screen would otherwise fight with the player.
sudo systemctl disable --now getty@tty1.service || true

sudo systemctl daemon-reload
sudo systemctl enable monsterscreen.service
sudo systemctl restart monsterscreen.service
IP="$(hostname -I | cut -d' ' -f1)"
echo "Klaar. Stel het scherm in via http://$IP:8080"
echo "Logboek bekijken: journalctl -u monsterscreen -f"
