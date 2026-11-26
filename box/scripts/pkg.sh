#!/bin/bash
# lanpulse: package helper shared by setup.sh and kiosk-setup.sh. Source it; don't run it.
# Finds the distro's package manager (apt, dnf, zypper or pacman) and maps lanpulse's
# generic tool names to that distro's package names, so the box isn't tied to one distro.
#   pkg_refresh                          update the package lists once
#   pkg_install required|optional NAME   install by generic name; optional ones may be missing
#   compose ARGS                         docker compose (v2 plugin) or docker-compose, whichever exists

PKG_MGR=""
for _m in apt-get dnf zypper pacman; do command -v "$_m" >/dev/null 2>&1 && { PKG_MGR=$_m; break; }; done
unset _m

# generic name -> this distro's package name ("" = not packaged here)
pkg_name() {
  case "$PKG_MGR:$1" in
    apt-get:docker)    echo docker.io ;;
    apt-get:compose)   if apt-cache show docker-compose-v2 >/dev/null 2>&1; then echo docker-compose-v2; else echo docker-compose; fi ;;
    apt-get:dig)       echo dnsutils ;;
    apt-get:mtr)       echo mtr-tiny ;;
    apt-get:ping)      echo iputils-ping ;;
    apt-get:upnpc)     echo miniupnpc ;;
    apt-get:avahi)     echo avahi-daemon ;;
    apt-get:speedtest) echo speedtest-cli ;;
    apt-get:autoupdate) echo unattended-upgrades ;;

    dnf:docker)        echo moby-engine ;;
    dnf:compose)       echo docker-compose ;;
    dnf:dig)           echo bind-utils ;;
    dnf:iproute2)      echo iproute ;;
    dnf:ping)          echo iputils ;;
    dnf:snmp)          echo net-snmp-utils ;;
    dnf:upnpc)         echo miniupnpc ;;
    dnf:avahi)         echo avahi ;;
    dnf:sqlite3)       echo sqlite ;;
    dnf:speedtest)     echo speedtest-cli ;;
    dnf:unclutter)     echo unclutter-xfixes ;;
    dnf:autoupdate)    if dnf --version 2>/dev/null | grep -q '^dnf5'; then echo dnf5-plugin-automatic; else echo dnf-automatic; fi ;;

    zypper:docker)     echo docker ;;
    zypper:compose)    echo docker-compose ;;
    zypper:dig)        echo bind-utils ;;
    zypper:ping)       echo iputils ;;
    zypper:snmp)       echo net-snmp ;;
    zypper:upnpc)      echo miniupnpc ;;
    zypper:avahi)      echo avahi ;;
    zypper:speedtest)  echo speedtest-cli ;;
    zypper:iperf3)     echo iperf ;;
    zypper:firefox)    echo MozillaFirefox ;;
    zypper:autoupdate) echo "" ;;

    pacman:docker)     echo docker ;;
    pacman:compose)    echo docker-compose ;;
    pacman:dig)        echo bind ;;
    pacman:ping)       echo iputils ;;
    pacman:python3)    echo python ;;
    pacman:snmp)       echo net-snmp ;;
    pacman:upnpc)      echo miniupnpc ;;
    pacman:avahi)      echo avahi ;;
    pacman:sqlite3)    echo sqlite ;;
    pacman:speedtest)  echo speedtest-cli ;;
    pacman:autoupdate) echo "" ;;   # Arch expects updates to be watched by a person

    *) echo "$1" ;;
  esac
}

pkg_refresh() {
  case "$PKG_MGR" in
    apt-get) apt-get update -q ;;
    dnf)     dnf makecache -q ;;
    zypper)  zypper -n -q refresh ;;
    pacman)  pacman -Syu --noconfirm ;;   # Arch: never refresh without upgrading (partial upgrades break things)
  esac >/dev/null 2>&1 || echo "   (package list refresh failed, carrying on)"
}

_pkg_do() {
  case "$PKG_MGR" in
    apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" ;;
    dnf)     dnf install -y -q "$@" ;;
    zypper)  zypper -n -q install --no-recommends "$@" ;;
    pacman)  pacman -S --needed --noconfirm "$@" ;;
    *)       return 1 ;;
  esac >/dev/null 2>&1
}

# pkg_install required|optional NAME...  (all at once; if that fails, one by one to find the missing ones)
pkg_install() {
  local need=$1 n p; shift
  local names=() missing=()
  for n in "$@"; do p=$(pkg_name "$n"); [ -n "$p" ] && names+=("$p"); done
  [ ${#names[@]} -eq 0 ] && return 0
  if [ -z "$PKG_MGR" ]; then
    echo "   no apt/dnf/zypper/pacman here: install these yourself if missing: ${names[*]}"
    [ "$need" = optional ]; return
  fi
  _pkg_do "${names[@]}" && return 0
  for p in "${names[@]}"; do _pkg_do "$p" || missing+=("$p"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  if [ "$need" = required ]; then echo "!! could not install: ${missing[*]}"; return 1; fi
  echo "   (not available on this distro, skipped: ${missing[*]})"
}

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"; fi
}
compose_ok() { docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1; }
