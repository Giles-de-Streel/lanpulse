#!/bin/bash
# Turn the box's screen into a volunteer dashboard. Tested on Linux Mint Xfce (LightDM); also handles
# GDM (GNOME) and SDDM (KDE) auto-login, and Firefox or Chromium/Chrome as the browser.
#   sudo kiosk-setup.sh [user] [url]
# Defaults: user "monitor", url http://localhost:8080/?kiosk (lanpulse dashboard)
#           use http://localhost:3001/status/dashboard for the plain Uptime Kuma page
#
# - creates a standard (non-admin, no docker) user that logs in automatically at boot
# - its session opens Firefox full screen on the status page, restarting it if it crashes
# - no screen lock, no screensaver, no screen blanking, no sleep, no update pop-ups
# - closing Firefox normally (Alt+F4) leaves a desktop icon to reopen the dashboard
# - automatic updates are turned on where the distro supports it, since nobody will click "install updates"
# Safe to re-run. The user's password is printed once and saved in /opt/monitor/.env.
set -euo pipefail
KUSER=${1:-monitor}
URL=${2:-http://localhost:8080/?kiosk}
APP=${APP:-/opt/monitor}
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
. "$(cd "$(dirname "$0")" && pwd)/pkg.sh"

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
for g in sudo wheel adm docker lpadmin sambashare; do gpasswd -d "$KUSER" $g >/dev/null 2>&1 || true; done

# which desktop session to log into: the first of these that's installed, else whatever is there
pick_session() {   # pick_session DIR...  -> session name (file name without .desktop)
  local s d
  for s in xfce cinnamon mate gnome-xorg gnome plasma plasmax11 lxqt; do
    for d in "$@"; do [ -f "$d/$s.desktop" ] && { echo "$s"; return; }; done
  done
  for d in "$@"; do for s in "$d"/*.desktop; do [ -f "$s" ] && { basename "$s" .desktop; return; }; done; done
}
DM=$(basename "$(readlink -f /etc/systemd/system/display-manager.service 2>/dev/null)" .service)
[ -n "$DM" ] && [ "$DM" != display-manager ] || DM=$(for d in lightdm gdm gdm3 sddm; do systemctl is-enabled -q $d 2>/dev/null && { echo $d; break; }; done; true)
echo "== auto-login (${DM:-no display manager found})"
case "$DM" in
  lightdm)
    SESSION=$(pick_session /usr/share/xsessions)
    mkdir -p /etc/lightdm/lightdm.conf.d
    cat > /etc/lightdm/lightdm.conf.d/70-kiosk-autologin.conf <<EOF
[Seat:*]
autologin-user=$KUSER
autologin-user-timeout=0
autologin-session=${SESSION:-xfce}
EOF
    ;;
  gdm|gdm3)
    CONF=/etc/gdm3/custom.conf; [ -d /etc/gdm3 ] || CONF=/etc/gdm/custom.conf
    mkdir -p "$(dirname "$CONF")"; [ -f "$CONF" ] || printf '[daemon]\n' > "$CONF"
    grep -q '^\[daemon\]' "$CONF" || printf '\n[daemon]\n' >> "$CONF"
    sed -i -E '/^#?\s*AutomaticLogin(Enable)?\s*=/d' "$CONF"
    sed -i "/^\[daemon\]/a AutomaticLoginEnable=True\nAutomaticLogin=$KUSER" "$CONF"
    ;;
  sddm)
    SESSION=$(pick_session /usr/share/xsessions /usr/share/wayland-sessions)
    mkdir -p /etc/sddm.conf.d
    cat > /etc/sddm.conf.d/70-kiosk-autologin.conf <<EOF
[Autologin]
User=$KUSER
Session=${SESSION:-plasma}.desktop
Relogin=true
EOF
    ;;
  *) echo "   set up automatic login for '$KUSER' in your login manager by hand" ;;
esac

echo "== packages"
pkg_install optional unclutter || true
command -v unclutter >/dev/null || echo "   (unclutter not available, cursor stays visible)"
if ! command -v firefox >/dev/null && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null \
   && ! command -v google-chrome >/dev/null; then
  pkg_install optional firefox || true
fi

H=$(getent passwd "$KUSER" | cut -d: -f6)
install -d -o "$KUSER" -g "$KUSER" "$H/.config" "$H/.config/autostart" "$H/.local" "$H/.local/bin" \
  "$H/.config/xfce4/xfconf/xfce-perchannel-xml" "$H/Desktop" "$H/.kiosk-firefox"

echo "== dashboard launcher"
cat > "$H/.local/bin/dashboard" <<EOF
#!/bin/bash
# Full-screen dashboard. Restarts the browser if it crashes; a normal close (Alt+F4) exits.
URL="$URL"
# no screen blanking or locking, whichever desktop this is (each line is a no-op elsewhere)
xset s off -dpms s noblank 2>/dev/null || true
if command -v gsettings >/dev/null; then
  gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing 2>/dev/null || true
  gsettings set org.gnome.settings-daemon.plugins.power idle-dim false 2>/dev/null || true
fi
for kw in kwriteconfig6 kwriteconfig5; do
  command -v \$kw >/dev/null && { \$kw --file kscreenlockerrc --group Daemon --key Autolock false; \$kw --file kscreenlockerrc --group Daemon --key LockOnResume false; break; }
done
[ -z "\${WAYLAND_DISPLAY:-}" ] && { pgrep -u "\$USER" -x unclutter >/dev/null || (command -v unclutter >/dev/null && unclutter -idle 3 -root &); }
# wait for the dashboard to answer (it starts with the box)
for i in \$(seq 1 60); do curl -s -o /dev/null "\$URL" && break; sleep 3; done
# Firefox if installed (a snap Firefox can't use a hidden profile folder), else Chromium / Chrome
PROFILE="\$HOME/.kiosk-firefox"
if command -v firefox >/dev/null && readlink -f "\$(command -v firefox)" | grep -q snap; then
  PROFILE="\$HOME/snap/firefox/common/kiosk-profile"; mkdir -p "\$PROFILE"; cp -f "\$HOME/.kiosk-firefox/user.js" "\$PROFILE/"
fi
CHROME=\$(command -v chromium || command -v chromium-browser || command -v google-chrome || command -v google-chrome-stable || true)
while true; do
  if command -v firefox >/dev/null; then
    rm -f "\$PROFILE/lock" "\$PROFILE/.parentlock"
    firefox --no-remote --profile "\$PROFILE" --kiosk "\$URL"
  elif [ -n "\$CHROME" ]; then
    "\$CHROME" --kiosk --no-first-run --noerrdialogs --disable-session-crashed-bubble --disable-infobars \
      --password-store=basic --user-data-dir="\$HOME/.kiosk-chromium" "\$URL"
  else
    echo "no Firefox or Chromium installed"; exit 1
  fi
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
// scrollbars always shown (not the fading overlay kind), so the devices list visibly scrolls
user_pref("widget.gtk.overlay-scrollbars.enabled", false);
user_pref("layout.testing.overlay-scrollbars.always-visible", true);
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

echo "== automatic updates"
if command -v mintupdate-automation >/dev/null; then
  { mintupdate-automation upgrade enable && mintupdate-automation autoremove enable; } >/dev/null 2>&1 && echo "   Mint automatic updates enabled"
elif [ "$PKG_MGR" = apt-get ]; then
  pkg_install optional autoupdate && { echo 'APT::Periodic::Update-Package-Lists "1";'; echo 'APT::Periodic::Unattended-Upgrade "1";'; } \
    > /etc/apt/apt.conf.d/20auto-upgrades && echo "   unattended-upgrades enabled (security updates)"
elif [ "$PKG_MGR" = dnf ]; then
  pkg_install optional autoupdate || true
  if systemctl list-unit-files dnf5-automatic.timer >/dev/null 2>&1; then
    mkdir -p /etc/dnf; printf '[commands]\napply_updates = yes\n' > /etc/dnf/automatic.conf
    systemctl enable --now dnf5-automatic.timer >/dev/null 2>&1 && echo "   dnf5-automatic enabled"
  elif systemctl list-unit-files dnf-automatic-install.timer >/dev/null 2>&1; then
    systemctl enable --now dnf-automatic-install.timer >/dev/null 2>&1 && echo "   dnf-automatic enabled"
  fi
else
  echo "   not set up on this distro: keep the box updated by hand"
fi

echo "== done. Reboot to start the dashboard: sudo reboot"
echo "   Admin access: SSH, or Alt+F4 the dashboard, then Menu -> Log out -> log in as the admin user"
