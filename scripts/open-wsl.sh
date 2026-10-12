#!/usr/bin/env bash
set -euo pipefail

url=http://127.0.0.1:8765
if (( $# > 1 )); then
  echo "Usage: bash scripts/open-wsl.sh [token-file]" >&2
  exit 2
fi
if ! grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  echo "This helper is for WSL. Open $url in a browser on this computer." >&2
  exit 1
fi
if command -v powershell.exe >/dev/null 2>&1; then
  if ! powershell.exe -NoProfile -Command "Start-Process '$url'" >/dev/null; then
    echo "Could not start the Windows browser. Open $url manually."
  fi
else
  echo "Open $url in your Windows browser."
fi
if (( $# == 1 )); then
  printf 'Login token: cat %q\n' "$1"
else
  echo "Login token: sudo cat /etc/mglogwh-controlroom/token"
fi
