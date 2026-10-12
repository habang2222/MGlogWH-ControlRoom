"""Run the MGlogWH control room on a local loopback address."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .backend import BackendError, SensorBackend
from .server import ControlRoomServer, load_token


def main(argv=None):
    parser = argparse.ArgumentParser(description="MGlogWH local control room")
    parser.add_argument("--mode", choices=("wsl", "linux"), default="linux")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path, default=Path("/etc/mglogwh-controlroom/token"))
    parser.add_argument("--sensor-root", type=Path, default=Path("/opt/mglogwh"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/mglogwh"))
    parser.add_argument("--config", type=Path, default=Path("/etc/mglogwh/config.json"))
    parser.add_argument("--demo", action="store_true", help="show clearly labeled synthetic events")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port는 1..65535여야 합니다.")
    try:
        token = load_token(args.token_file)
        backend = SensorBackend(sensor_root=args.sensor_root, data_dir=args.data_dir,
                                config_path=args.config, mode=args.mode, demo=args.demo)
        web_dir = Path(__file__).resolve().parent.parent / "web"
        with ControlRoomServer((args.host, args.port), backend, token, web_dir) as server:
            print(f"MGlogWH Control Room: http://127.0.0.1:{server.server_port}/", flush=True)
            print(f"Login token: {args.token_file} (read locally; token is not printed)", flush=True)
            server.serve_forever(poll_interval=0.25)
    except (BackendError, OSError, ValueError) as exc:
        print(f"Control Room 오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
