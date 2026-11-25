#!/bin/bash
# Turn the box's screen into a volunteer dashboard (Linux Mint Xfce / LightDM).
#   sudo kiosk-setup.sh [user] [url]
# Defaults: user "monitor", url http://localhost:8080/?kiosk (lanpulse dashboard)
#           use http://localhost:3001/status/dashboard for the plain Uptime Kuma page
#
# - creates a standard (non-admin, no docker) user that logs in automatically at boot
# - its session opens Firefox full screen on the status page, restarting it if it crashes
# - no screen lock, no screensaver, no screen blanking, no sleep, no update pop-ups
# - closing Firefox normally (Alt+F4) leaves a desktop icon to reopen the dashboard
# - Mint automatic updates are turned on, since nobody will click "install updates"
# Safe to re-run. The user's password is printed once and saved in /opt/monitor/.env.
set -euo pipefail
KUSER=${1:-monitor}
URL=${2:-http://localhost:8080/?kiosk}
APP=${APP:-/opt/monitor}
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== user $KUSER"
id "$KUSER" >/dev/null 2>&1 || { useradd -m -s /bin/bash -c "Network dashboard" "$KUSER"; echo "   created"; }
if ! grep -q '^KIOSK_PASSWORD=' "$APP/.env" 2>/dev/null; then
  PW=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-14)
  echo "$KUSER:$PW" | chpasswd
  echo "KIOSK_PASSWORD=$PW" >> "$APP/.env"
  echo "   password set: $PW (also in $APP/.env)"
else
  echo "   password unchanged (see KIOSK_PASSWORD in $APP/.env)"
fi
groupadd -f -r autologin; usermod -aG autologin "$KUSER"
for g in sudo adm docker lpadmin sambashare; do gpasswd -d "$KUSER" $g >/dev/null 2>&1 || true; done

echo "== auto-login"
mkdir -p /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/70-kiosk-autologin.conf <<EOF
[Seat:*]
autologin-user=$KUSER
autologin-user-timeout=0
autologin-session=xfce
EOF

echo "== packages"
apt-get install -y -q unclutter >/dev/null 2>&1 || echo "   (unclutter not available, cursor stays visible)"

H=$(getent passwd "$KUSER" | cut -d: -f6)
install -d -o "$KUSER" -g "$KUSER" "$H/.config" "$H/.config/autostart" "$H/.local" "$H/.local/bin" \
  "$H/.config/xfce4/xfconf/xfce-perchannel-xml" "$H/Desktop" "$H/.kiosk-firefox"

echo "== dashboard launcher"
cat > "$H/.local/bin/dashboard" <<EOF
#!/bin/bash
# Full-screen dashboard. Restarts Firefox if it crashes; a normal close (Alt+F4) exits.
URL="$URL"
xset s off -dpms s noblank 2>/dev/null || true
pgrep -u "\$USER" -x unclutter >/dev/null || (command -v unclutter >/dev/null && unclutter -idle 3 -root &)
# wait for Uptime Kuma to answer (it starts with the box)
for i in \$(seq 1 60); do curl -s -o /dev/null "\$URL" && break; sleep 3; done
while true; do
  rm -f "\$HOME/.kiosk-firefox/lock" "\$HOME/.kiosk-firefox/.parentlock"
  firefox --no-remote --profile "\$HOME/.kiosk-firefox" --kiosk "\$URL"
  rc=\$?
  [ \$rc -eq 0 ] && break      # closed on purpose
  sleep 5                      # crashed: reopen
done
EOF
chmod 755 "$H/.local/bin/dashboard"

# Firefox profile prefs: no first-run pages, no restore prompts, no pop-ups
cat > "$H/.kiosk-firefox/user.js" <<'EOF'
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_resumed_crashes", 0);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.translations.automaticallyPopup", false);
user_pref("full-screen-api.warning.timeout", 0);
user_pref("browser.tabs.warnOnClose", false);
user_pref("extensions.pocket.enabled", false);
user_pref("browser.newtabpage.enabled", false);
user_pref("signon.rememberSignons", false);
EOF

cat > "$H/.config/autostart/dashboard.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Network dashboard
Exec=$H/.local/bin/dashboard
X-GNOME-Autostart-enabled=true
EOF
cat > "$H/Desktop/Network dashboard.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Open network dashboard
Comment=Show the green/red network status screen
Exec=$H/.local/bin/dashboard
Icon=network-workgroup
Terminal=false
EOF
chmod 755 "$H/Desktop/Network dashboard.desktop"

echo "== no lock, screensaver, welcome or update pop-ups"
for f in light-locker xscreensaver xfce4-screensaver mintwelcome mintupdate; do
  printf "[Desktop Entry]\nType=Application\nName=%s\nHidden=true\nX-GNOME-Autostart-enabled=false\n" "$f" > "$H/.config/autostart/$f.desktop"
done
cat > "$H/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="blank-on-battery" type="int" value="0"/>
    <property name="inactivity-on-ac" type="uint" value="0"/>
    <property name="inactivity-on-battery" type="uint" value="0"/>
    <property name="lid-action-on-ac" type="uint" value="0"/>
    <property name="lid-action-on-battery" type="uint" value="0"/>
    <property name="lock-screen-suspend-hibernate" type="bool" value="false"/>
    <property name="logind-handle-lid-switch" type="bool" value="false"/>
  </property>
</channel>
EOF
cat > "$H/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="idle-activation" type="empty"><property name="enabled" type="bool" value="false"/></property>
  </property>
  <property name="lock" type="empty"><property name="enabled" type="bool" value="false"/></property>
</channel>
EOF
chown -R "$KUSER:$KUSER" "$H"

echo "== Mint automatic updates"
if command -v mintupdate-automation >/dev/null; then
  { mintupdate-automation upgrade enable && mintupdate-automation autoremove enable; } >/dev/null 2>&1 && echo "   enabled"
else
  echo "   mintupdate-automation not found (not Mint?), relying on unattended-upgrades"
fi

echo "== done. Reboot to start the dashboard: sudo reboot"
echo "   Admin access: SSH, or Alt+F4 the dashboard, then Menu -> Log out -> log in as the admin user"
