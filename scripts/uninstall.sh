#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo "Run with sudo: sudo bash scripts/uninstall.sh" >&2
  exit 1
fi
if [[ $(uname -s) != Linux ]]; then
  echo "This uninstaller requires Linux." >&2
  exit 1
fi

install_root=/opt/mglogwh-controlroom
config_root=/etc/mglogwh-controlroom
launcher=/usr/local/bin/mglogwh-controlroom
unit=/etc/systemd/system/mglogwh-controlroom.service

for path in /opt /etc /usr/local /usr/local/bin /etc/systemd /etc/systemd/system; do
  if [[ -L $path ]]; then
    echo "Refusing symlinked system directory: $path" >&2
    exit 1
  fi
done
for path in "$install_root" "$config_root"; do
  if [[ -L $path || ( -e $path && ! -d $path ) ]]; then
    echo "Refusing unsafe directory: $path" >&2
    exit 1
  fi
  if [[ -d $path && $(stat -c %u -- "$path") != 0 ]]; then
    echo "Refusing non-root-owned directory: $path" >&2
    exit 1
  fi
  if [[ -d $path && $(realpath -e -- "$path") != "$path" ]]; then
    echo "Refusing directory outside expected path: $path" >&2
    exit 1
  fi
done
for path in "$launcher" "$unit" "$config_root/token" "$config_root/mode"; do
  if [[ -L $path || ( -e $path && ! -f $path ) ]]; then
    echo "Refusing unsafe file: $path" >&2
    exit 1
  fi
  if [[ -f $path && $(stat -c %u -- "$path") != 0 ]]; then
    echo "Refusing non-root-owned file: $path" >&2
    exit 1
  fi
done

if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet mglogwh-controlroom.service; then
    systemctl stop mglogwh-controlroom.service
  fi
  if [[ -f $unit ]]; then
    systemctl disable mglogwh-controlroom.service
  fi
fi
rm -f -- "$unit" "$launcher" "$config_root/token" "$config_root/mode"
if [[ -d $install_root ]]; then
  rm -rf -- "$install_root"
fi
if [[ -d $config_root ]]; then
  rmdir -- "$config_root" 2>/dev/null || {
    echo "Keeping nonempty configuration directory: $config_root" >&2
  }
fi
if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
fi
echo "Removed MGlogWH Control Room. MGlogWH sensor, rules, and run data were not changed."
