#!/usr/bin/env python3
"""Bounded, opt-in smoke test against installed MGlogWH and Control Room.

Run as root on the sensor host after both services are installed and the
collector is already RUNNING. The test temporarily replaces the rule list,
creates one probe file and two new sensor runs, then restores the exact
configuration bytes and restarts the original policy. Existing runs are never
removed. A durable backup remains beside the config if restoration fails or
the process is killed before its finally block runs.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid


class SmokeFailure(Exception):
    pass


class API:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None
        self.csrf: str | None = None

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float = 10) -> dict:
        headers = {"Accept": "application/json"}
        payload = None
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
            if path != "/api/login":
                if not self.csrf:
                    raise SmokeFailure("Missing CSRF token")
                headers["X-CSRF-Token"] = self.csrf
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            cookie = response.getheader("Set-Cookie")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise SmokeFailure(f"Oversized API response: {path}")
            try:
                result = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SmokeFailure(f"Non-JSON API response: {path}") from exc
            if response.status != 200:
                message = result.get("error") if isinstance(result, dict) else None
                raise SmokeFailure(f"{path}: HTTP {response.status}: {message or 'request failed'}")
            if not isinstance(result, dict):
                raise SmokeFailure(f"Unexpected API response: {path}")
            if path == "/api/login":
                if not cookie or not cookie.startswith("mglogwh_session="):
                    raise SmokeFailure("Login did not set a session cookie")
                self.cookie = cookie.split(";", 1)[0]
                self.csrf = result.get("csrf_token")
                if not isinstance(self.csrf, str) or not self.csrf:
                    raise SmokeFailure("Login did not return a CSRF token")
            return result
        finally:
            connection.close()

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict, timeout: float = 10) -> dict:
        return self.request("POST", path, body, timeout)


def check_command(command: list[str], timeout: float = 10) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"Command failed: {command[0]}: {exc}") from exc
    if result.returncode:
        raise SmokeFailure(f"Command failed: {' '.join(command)}: "
                           f"{(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_config(config: Path, original: bytes, info: os.stat_result) -> Path:
    backup = config.with_name(f".config.controlroom-smoke-backup-{uuid.uuid4().hex}.json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(backup, flags, 0o600)
    try:
        os.fchown(descriptor, info.st_uid, info.st_gid)
        os.fchmod(descriptor, stat.S_IMODE(info.st_mode))
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    fsync_directory(config.parent)
    return backup


def restore_config(config: Path, backup: Path, original: bytes, candidate: bytes,
                   apply_attempted: bool) -> bool:
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise SmokeFailure(f"Config changed type/owner; backup retained: {backup}")
    current = config.read_bytes()
    if current not in (original, candidate):
        raise SmokeFailure(f"Config changed externally; backup retained: {backup}")
    if current == candidate:
        os.replace(backup, config)
        fsync_directory(config.parent)
    else:
        os.unlink(backup)
        fsync_directory(config.parent)
    if apply_attempted:
        check_command(["systemctl", "restart", "mglogwh.service"], timeout=135)
        check_command(["systemctl", "is-active", "--quiet", "mglogwh.service"])
    return True


def normalized_config_hash(sensor_root: Path, config: Path, data_dir: Path) -> str:
    environment = dict(os.environ, MGLOGWH_DATA_DIR=str(data_dir))
    command = [sys.executable, str(sensor_root / "manage.py"),
               "validate", "--config", str(config)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment,
                                timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"Collector validation failed: {exc}") from exc
    if result.returncode:
        raise SmokeFailure(f"Collector validation failed: {result.stderr.strip()[:500]}")
    try:
        normalized = json.loads(result.stdout)["config"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SmokeFailure("Collector validation returned no normalized config") from exc
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def probe_openat(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        os.write(descriptor, b"MGlogWH Control Room integration probe\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(3):
            # Supplying dir_fd makes CPython issue openat, even for this
            # absolute pathname. The sensor must match the literal path.
            fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd)
            try:
                os.read(fd, 1)
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)


def wait_for_event(api: API, run_id: str, cursor: int, path: str,
                   rule_id: int, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        params = urlencode({"run": run_id, "cursor": cursor, "limit": 500})
        page = api.get("/api/events?" + params)
        if page.get("run_id") != run_id:
            raise SmokeFailure("Event API returned a different run")
        for event in page.get("events", []):
            if (event.get("path") == path and event.get("rule_id") == rule_id and
                    event.get("priority") == 2):
                return event
        next_cursor = page.get("cursor")
        if type(next_cursor) is not int or next_cursor < cursor:
            raise SmokeFailure("Event API returned an invalid cursor")
        cursor = next_cursor
        if not page.get("has_more"):
            time.sleep(0.5)
    raise SmokeFailure("Probe event was not indexed within 20 seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="temporarily save a test rule and restart the collector")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path, default=Path("/etc/mglogwh-controlroom/token"))
    parser.add_argument("--sensor-root", type=Path, default=Path("/opt/mglogwh"))
    parser.add_argument("--config", type=Path, default=Path("/etc/mglogwh/config.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/mglogwh"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("pass --execute after checking the installed services")
    if os.geteuid() != 0:
        parser.error("run as root on the sensor host")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1..65535")

    config = args.config.absolute()
    data_dir = args.data_dir.absolute()
    sensor_root = args.sensor_root.absolute()
    token_file = args.token_file.absolute()
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise SmokeFailure("Config must be a root-owned regular 0600 file")
    if config.parent.resolve() != config.parent or config.resolve() != config:
        raise SmokeFailure("Config path must not contain symlinks")
    original = config.read_bytes()
    if len(original) > 64 * 1024:
        raise SmokeFailure("Config exceeds 64 KiB")
    check_command(["systemctl", "is-active", "--quiet", "mglogwh.service"])
    version = check_command([sys.executable, str(sensor_root / "manage.py"), "version"]).strip()
    if version != "MGlogWH 0.4.0":
        raise SmokeFailure(f"Expected installed MGlogWH 0.4.0, found {version}")

    api = API(args.port)
    token = token_file.read_text(encoding="ascii").strip()
    login = api.post("/api/login", {"token": token})
    if not login.get("authenticated") or login.get("demo"):
        raise SmokeFailure("Expected authenticated real Control Room, not demo")
    before = api.get("/api/overview")
    rules_before = api.get("/api/rules")
    before_run = before.get("run", {}).get("id")
    if (not before_run or before.get("collector", {}).get("state") != "RUNNING" or
            rules_before.get("pending_apply") is not False):
        raise SmokeFailure("Collector must be RUNNING with no pending rule changes")
    if before.get("data_root") != str(data_dir):
        raise SmokeFailure("Control Room is using a different data directory")
    old_revision = hashlib.sha256(original).hexdigest()
    if rules_before.get("revision") != old_revision:
        raise SmokeFailure("Config changed between local read and API read")
    original_hash = normalized_config_hash(sensor_root, config, data_dir)

    probe_path = Path(f"/tmp/mglogwh-controlroom-smoke-{uuid.uuid4().hex}.txt")
    rule_id = 248
    test_rules = [{
        "id": rule_id,
        "when": {"all": [
            {"field": "event_type", "op": "eq", "value": "openat"},
            {"field": "path", "op": "eq", "value": str(probe_path)},
        ]},
        "priority": "CRITICAL",
        "overload_policy": "QUARANTINE_EXEMPT",
    }]
    value = json.loads(original)
    value.pop("critical_rules", None)
    value["event_rules"] = test_rules
    candidate = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(candidate) > 64 * 1024:
        raise SmokeFailure("Test config would exceed 64 KiB")

    backup = backup_config(config, original, info)
    print(f"Config backup: {backup}", file=sys.stderr, flush=True)
    apply_attempted = False
    result: dict | None = None
    failure: BaseException | None = None
    restore_failure: BaseException | None = None
    try:
        saved = api.post("/api/rules", {
            "schema": "event_rules_v1", "rules_json": json.dumps(test_rules),
            "revision": old_revision,
        })
        revision = hashlib.sha256(candidate).hexdigest()
        if saved.get("revision") != revision or saved.get("schema") != "event_rules_v1":
            raise SmokeFailure("Rule save did not produce the expected config revision")
        pending = api.get("/api/rules")
        if pending.get("pending_apply") is not True:
            raise SmokeFailure("Rule save was not marked pending")
        expected_hash = normalized_config_hash(sensor_root, config, data_dir)

        apply_attempted = True
        applied = api.post("/api/apply", {"confirm": "restart", "revision": revision}, timeout=125)
        new_run = applied.get("run_id")
        if not applied.get("ok") or applied.get("demo") or not new_run or new_run == before_run:
            raise SmokeFailure("Apply did not confirm a new real run")
        manifest_path = data_dir / "runs" / new_run / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("config_sha256") != expected_hash:
            raise SmokeFailure("New run manifest has a different config hash")
        overview = api.get("/api/overview")
        if (overview.get("run", {}).get("id") != new_run or
                overview.get("collector", {}).get("state") != "RUNNING" or
                overview.get("config", {}).get("pending_apply") is not False):
            raise SmokeFailure("Overview did not show the applied running policy")
        cursor_page = api.get("/api/events?" + urlencode({"run": new_run, "limit": 1}))
        cursor = cursor_page.get("latest_rowid")
        if type(cursor) is not int or cursor < 0:
            raise SmokeFailure("Event API did not return a valid rowid")

        probe_openat(probe_path)
        event = wait_for_event(api, new_run, cursor, str(probe_path), rule_id)
        files = api.get("/api/files?" + urlencode({"run": new_run}))
        expected_run_path = str(data_dir / "runs" / new_run)
        names = {item.get("name") for item in files.get("files", [])}
        if (files.get("run_id") != new_run or files.get("path") != expected_run_path or
                not {"manifest.json", "critical.raw"} <= names):
            raise SmokeFailure("Files API did not expose the new run and critical raw file")
        result = {"collector_version": version,
                  "previous_run_id": before_run, "test_run_id": new_run,
                  "test_config_sha256": expected_hash, "probe_path": str(probe_path),
                  "event_rowid": event.get("rowid"), "event_rule_id": event.get("rule_id"),
                  "event_priority": event.get("priority"),
                  "files": sorted(names)}
    except BaseException as exc:
        failure = exc
    finally:
        try:
            restore_config(config, backup, original, candidate, apply_attempted)
        except BaseException as exc:
            restore_failure = exc

    if restore_failure:
        print(f"RESTORE FAILED: {restore_failure}", file=sys.stderr)
        if failure:
            print(f"SMOKE FAILED: {failure}", file=sys.stderr)
        return 1
    if failure:
        print(f"SMOKE FAILED: {failure}", file=sys.stderr)
        return 1
    assert result is not None
    result["original_config_restored"] = hashlib.sha256(config.read_bytes()).hexdigest() == old_revision
    result["collector_service_active"] = subprocess.run(
        ["systemctl", "is-active", "--quiet", "mglogwh.service"], check=False).returncode == 0
    deadline = time.monotonic() + 15
    result["restored_policy_running"] = False
    while time.monotonic() < deadline:
        overview = api.get("/api/overview")
        restored_run = overview.get("run", {}).get("id")
        if (restored_run not in (before_run, result["test_run_id"]) and
                overview.get("collector", {}).get("state") == "RUNNING" and
                overview.get("config", {}).get("pending_apply") is False):
            manifest = json.loads((data_dir / "runs" / restored_run / "manifest.json").read_bytes())
            if manifest.get("config_sha256") == original_hash:
                result["restored_run_id"] = restored_run
                result["restored_policy_running"] = True
                break
        time.sleep(0.5)
    result["status"] = "PASS" if all((result["original_config_restored"],
                                      result["collector_service_active"],
                                      result["restored_policy_running"])) else "FAIL"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    def interrupted(*_):
        raise SmokeFailure("Interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
