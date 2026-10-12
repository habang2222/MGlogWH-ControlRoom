#!/usr/bin/env bash
set -euo pipefail
umask 077

mode=${1:-}
if [[ $mode != wsl && $mode != linux ]]; then
  echo "Usage: sudo bash scripts/install-linux.sh|scripts/install-wsl.sh" >&2
  exit 2
fi
if (( EUID != 0 )); then
  echo "Run the installer with sudo." >&2
  exit 1
fi
if [[ $(uname -s) != Linux ]]; then
  echo "This installer requires Linux." >&2
  exit 1
fi
if ! /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Install /usr/bin/python3 3.12 or newer first." >&2
  exit 1
fi

is_wsl=false
if grep -qi microsoft /proc/sys/kernel/osrelease; then
  is_wsl=true
fi
if [[ $mode == wsl && $is_wsl != true ]]; then
  echo "This kernel is not WSL. Use scripts/install-linux.sh." >&2
  exit 1
fi
if [[ $mode == linux && $is_wsl == true ]]; then
  echo "WSL detected. Use scripts/install-wsl.sh." >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
install_root=/opt/mglogwh-controlroom
config_root=/etc/mglogwh-controlroom
launcher=/usr/local/bin/mglogwh-controlroom
unit=/etc/systemd/system/mglogwh-controlroom.service
stage=
backup=
rollback_ready=false
old_app_moved=false
new_app_installed=false
systemd_running=false
was_active=false
was_enabled=false
token_temp=
mode_temp=
token_created=false
config_preexisting=false

require_owned_dir() {
  local path=$1
  if [[ -L $path || ( -e $path && ! -d $path ) ]]; then
    echo "Unsafe directory: $path" >&2
    exit 1
  fi
  if [[ -d $path ]]; then
    local owner permissions
    owner=$(stat -c %u -- "$path")
    permissions=$(stat -c %a -- "$path")
    if [[ $owner != 0 ]] || (( (8#$permissions & 0022) != 0 )); then
      echo "Directory must be root-owned and not group/world writable: $path" >&2
      exit 1
    fi
  fi
}

require_owned_file() {
  local path=$1
  if [[ -L $path || ( -e $path && ! -f $path ) ]]; then
    echo "Unsafe file: $path" >&2
    exit 1
  fi
  if [[ -f $path && $(stat -c %u -- "$path") != 0 ]]; then
    echo "File must be root-owned: $path" >&2
    exit 1
  fi
}

remove_stage() {
  local path=$1
  if [[ $path == /opt/.mglogwh-controlroom.stage.* && -d $path && ! -L $path ]]; then
    rm -rf -- "$path"
  fi
}

cleanup() {
  local status=$?
  local rollback_ok=true
  trap - EXIT
  set +e
  if [[ -n $stage ]]; then
    remove_stage "$stage"
  fi
  if [[ -n $token_temp && -f $token_temp && ! -L $token_temp &&
        $token_temp == /etc/mglogwh-controlroom/.token.* ]]; then
    rm -f -- "$token_temp"
  fi
  if [[ -n $mode_temp && -f $mode_temp && ! -L $mode_temp &&
        $mode_temp == /etc/mglogwh-controlroom/.mode.* ]]; then
    rm -f -- "$mode_temp"
  fi
  if (( status != 0 )) && [[ $rollback_ready == true ]]; then
    if [[ $systemd_running == true ]]; then
      if systemctl is-active --quiet mglogwh-controlroom.service; then
        systemctl stop mglogwh-controlroom.service >/dev/null 2>&1 || rollback_ok=false
      fi
      if [[ $was_enabled != true ]]; then
        systemctl disable mglogwh-controlroom.service >/dev/null 2>&1 || true
      fi
    fi
    if [[ $new_app_installed == true && -d $install_root && ! -L $install_root &&
          $(realpath -e -- "$install_root") == "$install_root" ]]; then
      rm -rf -- "$install_root" || rollback_ok=false
    fi
    if [[ $old_app_moved == true && -d $backup/app && ! -L $backup/app ]]; then
      mv -T -- "$backup/app" "$install_root" || rollback_ok=false
    fi
    for item in mode launcher unit; do
      case $item in
        mode) destination=$config_root/mode ;;
        launcher) destination=$launcher ;;
        unit) destination=$unit ;;
      esac
      if [[ -f $backup/$item ]]; then
        cp -p -- "$backup/$item" "$destination" || rollback_ok=false
      else
        rm -f -- "$destination" || rollback_ok=false
      fi
    done
    if [[ $systemd_running == true ]]; then
      systemctl daemon-reload || rollback_ok=false
      if [[ $was_enabled == true ]]; then
        systemctl enable mglogwh-controlroom.service >/dev/null 2>&1 || rollback_ok=false
      fi
      if [[ $was_active == true ]]; then
        systemctl start mglogwh-controlroom.service || rollback_ok=false
      fi
    fi
    if [[ $rollback_ok == true ]]; then
      echo "Installer failed; previous Control Room installation was restored." >&2
    else
      echo "Automatic restore was incomplete. Previous files remain at: $backup" >&2
    fi
  fi
  if (( status != 0 )) && [[ $token_created == true && -f $config_root/token &&
        ! -L $config_root/token ]]; then
    rm -f -- "$config_root/token"
  fi
  if (( status != 0 )) && [[ $config_preexisting != true && -d $config_root &&
        ! -L $config_root ]]; then
    rmdir -- "$config_root" 2>/dev/null || true
  fi
  if [[ -n $backup && -d $backup && ! -L $backup &&
        $backup == /opt/.mglogwh-controlroom.backup.* ]]; then
    if (( status == 0 )) || [[ $rollback_ok == true ]]; then
      rm -rf -- "$backup"
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

for path in /opt /etc /usr/local /usr/local/bin /etc/systemd /etc/systemd/system \
            "$install_root" "$config_root"; do
  require_owned_dir "$path"
done
require_owned_file "$launcher"
require_owned_file "$unit"
require_owned_file "$config_root/token"
require_owned_file "$config_root/mode"

for path in "$repo_root/controlroom" "$repo_root/web"; do
  if [[ ! -d $path || -L $path ]]; then
    echo "Missing or symlinked source directory: $path" >&2
    exit 1
  fi
  if [[ -n $(find "$path" ! -type d ! -type f -print -quit) ]]; then
    echo "Source contains a symlink or non-regular file: $path" >&2
    exit 1
  fi
done
for path in "$repo_root/packaging/mglogwh-controlroom" \
            "$repo_root/packaging/mglogwh-controlroom.service"; do
  if [[ ! -f $path || -L $path ]]; then
    echo "Missing or symlinked source file: $path" >&2
    exit 1
  fi
done
for path in "$repo_root/controlroom/__init__.py" "$repo_root/controlroom/__main__.py" \
            "$repo_root/controlroom/backend.py" "$repo_root/controlroom/server.py" \
            "$repo_root/web/index.html" "$repo_root/web/app.css" "$repo_root/web/app.js"; do
  if [[ ! -f $path || -L $path ]]; then
    echo "Missing or symlinked application file: $path" >&2
    exit 1
  fi
done

stage=$(mktemp -d /opt/.mglogwh-controlroom.stage.XXXXXX)
cp -R -- "$repo_root/controlroom" "$repo_root/web" "$stage/"
find "$stage" -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf -- {} +
find "$stage" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
if [[ -n $(find "$stage" ! -type d ! -type f -print -quit) ]]; then
  echo "Staged source contains a symlink or non-regular file." >&2
  exit 1
fi
chown -R root:root -- "$stage"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +

if [[ -d $config_root ]]; then config_preexisting=true; fi
install -d -o root -g root -m 0700 "$config_root"
if [[ ! -e $config_root/token ]]; then
  token_temp=$(mktemp "$config_root/.token.XXXXXX")
  /usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$token_temp"
  chmod 0600 "$token_temp"
  mv -T -- "$token_temp" "$config_root/token"
  token_temp=
  token_created=true
else
  chmod 0600 "$config_root/token"
fi

if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
  systemd_running=true
  if systemctl is-active --quiet mglogwh-controlroom.service; then
    was_active=true
  fi
  if systemctl is-enabled --quiet mglogwh-controlroom.service; then
    was_enabled=true
  fi
fi

backup=$(mktemp -d /opt/.mglogwh-controlroom.backup.XXXXXX)
if [[ -f $launcher ]]; then cp -p -- "$launcher" "$backup/launcher"; fi
if [[ -f $unit ]]; then cp -p -- "$unit" "$backup/unit"; fi
if [[ -f $config_root/mode ]]; then cp -p -- "$config_root/mode" "$backup/mode"; fi
rollback_ready=true
if [[ $was_active == true ]]; then
  systemctl stop mglogwh-controlroom.service
fi
if ! /usr/bin/python3 -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 8765)); s.close()'; then
  echo "Port 8765 is in use. Stop a manually started Control Room before installing." >&2
  exit 1
fi

if [[ -d $install_root ]]; then
  mv -T -- "$install_root" "$backup/app"
  old_app_moved=true
fi
mv -T -- "$stage" "$install_root"
new_app_installed=true
stage=

mode_temp=$(mktemp "$config_root/.mode.XXXXXX")
printf '%s\n' "$mode" > "$mode_temp"
chmod 0644 "$mode_temp"
mv -T -- "$mode_temp" "$config_root/mode"
mode_temp=
for path in /usr/local/bin /etc/systemd /etc/systemd/system; do
  if [[ ! -d $path ]]; then
    install -d -o root -g root -m 0755 "$path"
  fi
done
install -o root -g root -m 0755 "$repo_root/packaging/mglogwh-controlroom" "$launcher"
install -o root -g root -m 0644 "$repo_root/packaging/mglogwh-controlroom.service" "$unit"

if [[ $systemd_running == true ]]; then
  systemctl daemon-reload
  if ! systemctl enable --now mglogwh-controlroom.service ||
     ! systemctl is-active --quiet mglogwh-controlroom.service; then
    echo "Service did not start. Inspect: sudo journalctl -u mglogwh-controlroom -n 100 --no-pager" >&2
    exit 1
  fi
  echo "MGlogWH Control Room is running at http://127.0.0.1:8765"
else
  echo "Installed MGlogWH Control Room. This environment has no running systemd."
  echo "Start it with: sudo mglogwh-controlroom"
fi
echo "Login token: sudo cat $config_root/token"
if [[ $mode == wsl ]]; then
  echo "Open the Windows browser with: bash scripts/open-wsl.sh"
fi
