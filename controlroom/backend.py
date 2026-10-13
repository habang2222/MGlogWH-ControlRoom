"""Read-only run inspection and deliberate MGlogWH configuration changes."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


class BackendError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


RUN_FILES = frozenset({
    "manifest.json", "config.json", "health.json", "index-health.json",
    "service.json", "critical.raw", "general.raw", "quarantine.raw",
    "index.sqlite", "collector.log", "indexer.log", "service.log",
    "loss-ledger.jsonl", "loss-ledger.json", "metadata.jsonl",
})
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_CONFIG = 64 * 1024
RULE_SCHEMAS = {"legacy": "critical_rules", "event_rules_v1": "event_rules"}
RUN_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _json_file(path: Path, default):
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise BackendError(f"상태 파일이 너무 큽니다: {path.name}", 503)
        return json.loads(raw, parse_constant=_reject_nonfinite)
    except FileNotFoundError:
        return default
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise BackendError(f"손상된 JSON 파일: {path.name}", 503) from exc


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 설정 키: " + key)
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError("JSON에서 허용하지 않는 숫자: " + value)


def _iso_timestamp(ns: int | None) -> str | None:
    if type(ns) is not int:
        return None
    try:
        return dt.datetime.fromtimestamp(ns / 1e9, dt.UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class SensorBackend:
    def __init__(self, *, sensor_root: Path, data_dir: Path, config_path: Path,
                 mode: str, demo: bool = False):
        self.sensor_root = Path(sensor_root).absolute()
        self.data_dir = Path(data_dir).absolute()
        self.config_path = Path(config_path).absolute()
        self.mode = mode
        self.demo = demo
        self.write_lock = threading.Lock()
        self.demo_started = time.monotonic()
        self.demo_rules = [
            {"id": 1, "event_type": "openat", "path": "/srv/payment/keys", "protected": True},
            {"id": 2, "event_type": "exec", "path": "/usr/bin/sudo", "protected": False},
        ]
        self.demo_revision = _revision(json.dumps(self.demo_rules, sort_keys=True).encode())
        self.demo_schema = "legacy"
        self._manager = None
        self._manager_signature = None
        self.counts_lock = threading.Lock()
        self.counts_cache = {}

    def _manager_module(self):
        path = self.sensor_root / "manage.py"
        if self.sensor_root.resolve() != self.sensor_root:
            raise BackendError("연결된 MGlogWH 코드 경로는 허용하지 않습니다.", 503)
        try:
            info = path.lstat()
            directory = self.sensor_root.lstat()
        except FileNotFoundError as exc:
            raise BackendError("MGlogWH 수집기가 설치되지 않았습니다.", 503) from exc
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022:
            raise BackendError("신뢰할 수 없는 MGlogWH 설치 디렉터리입니다.", 503)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise BackendError("신뢰할 수 없는 MGlogWH manage.py 권한입니다.", 503)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and (info.st_uid != 0 or directory.st_uid != 0):
            raise BackendError("MGlogWH 설치 파일과 디렉터리는 root 소유여야 합니다.", 503)
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._manager is not None and self._manager_signature == signature:
            return self._manager
        spec = importlib.util.spec_from_file_location("mglogwh_installed_manage", path)
        if spec is None or spec.loader is None:
            raise BackendError("MGlogWH manage.py를 읽지 못했습니다.", 503)
        module = importlib.util.module_from_spec(spec)
        try:
            # Read source each time the installed file changes. Python's timestamp-based
            # bytecode cache can otherwise reuse old code for a same-size rapid update.
            source = spec.loader.get_source(spec.name)
            if source is None:
                raise ValueError("MGlogWH manage.py 소스를 읽지 못했습니다.")
            exec(compile(source, str(path), "exec"), module.__dict__)
        except Exception as exc:
            raise BackendError("MGlogWH manage.py 로드에 실패했습니다.", 503) from exc
        self._manager = module
        self._manager_signature = signature
        return module

    def _current_id(self) -> str | None:
        current = _json_file(self.data_dir / "current.json", {})
        run_id = current.get("run_id") if isinstance(current, dict) else None
        if run_id is None:
            return None
        self._check_run_id(run_id)
        return run_id

    @staticmethod
    def _check_run_id(run_id: str):
        if not isinstance(run_id, str) or not RUN_NAME.fullmatch(run_id):
            raise BackendError("잘못된 run ID입니다.", 400)
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("잘못된 run ID입니다.", 400) from exc

    def _run(self, run_id: str | None) -> Path | None:
        if run_id is None or run_id == "":
            run_id = self._current_id()
        if run_id is None:
            return None
        self._check_run_id(run_id)
        parent = self.data_dir / "runs"
        path = parent / run_id
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise BackendError("run 디렉터리를 찾지 못했습니다.", 404) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise BackendError("run 디렉터리가 아닙니다.", 404)
        if path.resolve() != path or parent.resolve() != parent:
            raise BackendError("연결된 run 경로는 허용하지 않습니다.", 403)
        return path

    def _config(self):
        if self.demo:
            return {"rules": list(self.demo_rules), "revision": self.demo_revision,
                    "schema": self.demo_schema}
        try:
            info = self.config_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise BackendError("설정 경로는 일반 파일이어야 합니다.", 503)
            if info.st_mode & 0o022:
                raise BackendError("설정 파일은 다른 사용자가 변경할 수 없어야 합니다.", 503)
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return {"rules": [], "revision": None, "schema": "legacy"}
        if len(raw) > MAX_CONFIG:
            raise BackendError("설정 파일이 64 KiB를 넘습니다.", 503)
        try:
            parsed = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise BackendError("설정 파일이 올바른 JSON이 아닙니다.", 503) from exc
        if not isinstance(parsed, dict):
            raise BackendError("설정 파일 형식이 올바르지 않습니다.", 503)
        if "event_rules" in parsed and "critical_rules" in parsed:
            raise BackendError("두 규칙 스키마가 동시에 설정됐습니다. MGlogWH 설정을 확인하세요.", 503)
        schema = "event_rules_v1" if "event_rules" in parsed else "legacy"
        rules = parsed.get(RULE_SCHEMAS[schema], [])
        if schema == "legacy" and rules == []:
            # MGlogWH creates this effective rule when critical_rules is absent
            # or empty. Expose the same rule so the editor can migrate it.
            rules = [{"id": 1, "event_type": "openat", "path": parsed.get("protected_path"),
                      "protected": False}]
        return {"rules": rules, "revision": _revision(raw), "raw": parsed,
                "schema": schema,
                "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}

    def rules(self):
        cfg = self._config()
        return {"rules": cfg["rules"], "revision": cfg["revision"], "schema": cfg["schema"],
                "applies": "NEXT_START_ONLY",
                "pending_apply": self._pending_apply(cfg),
                "validation": "DEMO_SHAPE_ONLY" if self.demo else "MGLOGWH_MANAGE_VALIDATE"}

    def _pending_apply(self, cfg):
        if self.demo:
            return False
        if cfg["revision"] is None:
            return None
        try:
            run = self._run(None)
            if run is None:
                return True
            manifest = _json_file(run / "manifest.json", {})
            recorded = manifest.get("config_sha256") if isinstance(manifest, dict) else None
            if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
                return None
            manager = self._manager_module()
            if not hasattr(manager, "config"):
                return None
            normalized = manager.config(self.config_path)
            canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            return hashlib.sha256(canonical).hexdigest() != recorded
        except (BackendError, OSError, ValueError, TypeError, KeyError, RecursionError):
            # A missing or invalid comparison cannot prove that the active run
            # already uses the settings shown by the editor.
            return None

    def _index_snapshot(self, run: Path):
        module = self._manager_module()
        index_health = _json_file(run / "index-health.json", {})
        health = _json_file(run / "health.json", {})
        if not isinstance(index_health, dict) or not isinstance(health, dict):
            raise BackendError("상태 파일 형식이 올바르지 않습니다.", 503)
        offsets = index_health.get("offsets", {})
        if not isinstance(offsets, dict):
            offsets = {}
        try:
            coverage = module.index_coverage(health, offsets)
        except (ValueError, KeyError, TypeError) as exc:
            raise BackendError(f"인덱스 경계가 잘못됐습니다: {exc}", 503) from exc
        state = index_health.get("state") if index_health.get("state") in (
            "FAILED", "INDEX_PARTIAL", "INDEX_PAUSED_STORAGE_PRESSURE") else coverage["coverage_state"]
        return health, dict(index_health, **coverage, state=state)

    def _status(self, run: Path | None):
        if run is None:
            return {"state": "UNCONFIGURED", "index": {"state": "UNAVAILABLE", "coverage_state": "BOUNDARY_UNKNOWN",
                                                    "complete_to_durable": False}}
        manager = self._manager_module()
        try:
            return manager.status(run)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BackendError(f"수집기 상태를 읽지 못했습니다: {exc}", 503) from exc

    def _counts(self, run: Path | None):
        if run is None:
            return {}
        db = self._open_index(run)
        if db is None:
            return {}
        try:
            info = (run / "index.sqlite").stat()
            identity = (info.st_dev, info.st_ino)
            db.execute("BEGIN")
            with self.counts_lock:
                cached = self.counts_cache.get(run.name)
                last = db.execute("SELECT COALESCE(MAX(rowid),0) FROM events").fetchone()[0]
                if cached and cached["identity"] == identity and last >= cached["last"]:
                    start = cached["last"]
                    values = dict(cached["values"])
                else:
                    start = 0
                    values = {0: 0, 1: 0, 2: 0}
                if last > start:
                    rows = db.execute("SELECT priority, COUNT(*) FROM events WHERE rowid>? AND rowid<=? GROUP BY priority",
                                      (start, last)).fetchall()
                    for priority, count in rows:
                        values[priority] = values.get(priority, 0) + count
                self.counts_cache[run.name] = {"identity": identity, "last": last, "values": values}
        except sqlite3.Error as exc:
            if "no such table" in str(exc).lower():
                return {}
            raise BackendError(f"인덱스를 읽지 못했습니다: {exc}", 503) from exc
        finally:
            db.close()
        return {"total": sum(values.values()), "critical": values.get(2, 0),
                "normal": values.get(1, 0), "bulk": values.get(0, 0)}

    @staticmethod
    def _open_index(run: Path):
        path = run / "index.sqlite"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise BackendError("인덱스가 일반 파일이 아닙니다.", 503)
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            return db
        except sqlite3.Error as exc:
            raise BackendError(f"인덱스를 열지 못했습니다: {exc}", 503) from exc

    def overview(self):
        if self.demo:
            count = self._demo_count()
            return {"demo": True, "mode": self.mode, "collector": {"state": "DEMO_SIMULATED", "dropped": 0},
                    "index": {"state": "DEMO_SIMULATED", "coverage_state": "SIMULATED", "complete_to_durable": False,
                    "lag_records": 0}, "loss": {"simulated": True, "total": 0, "scope": "SYNTHETIC_DEMO"},
                    "counts": {"total": count, "critical": (count + 3) // 4,
                               "normal": count // 2, "bulk": count - (count + 3) // 4 - count // 2},
                    "config": self.rules(), "run": {"id": "demo", "path": "(synthetic demo data)"},
                    "data_root": "(synthetic demo data)"}
        run = self._run(None)
        status = self._status(run)
        collector = status.get("collector", {})
        if not isinstance(collector, dict):
            collector = {}
        return {"demo": False, "mode": self.mode, "collector": dict(collector, state=status.get("state", "UNKNOWN")),
                "index": status.get("index", {}), "loss": self._loss_summary(collector),
                "counts": self._counts(run), "config": self.rules(),
                "run": {"id": run.name, "path": str(run)} if run else None, "data_root": str(self.data_dir),
                "storage": status.get("storage", {}),
                "rule_stats": status.get("rule_stats", collector.get("rule_stats")),
                "rule_stats_scope": status.get("rule_stats_scope", collector.get("rule_stats_scope")),
                "rule_stats_snapshot_complete": status.get("rule_stats_snapshot_complete",
                                                           collector.get("rule_stats_snapshot_complete")),
                "rule_engine_schema_version": status.get("rule_engine_schema_version"),
                "rule_version": status.get("rule_version", collector.get("rule_version")),
                "policy_version": status.get("policy_version", collector.get("policy_version"))}

    @staticmethod
    def _loss_summary(health: dict):
        keys = ("general_quota_drop", "quarantine_storage_drop", "queue_overflow",
                "metadata_dropped", "metadata_overflow", "exact_loss_available")
        result = {key: health[key] for key in keys if key in health}
        result.update(total=None, scope="PARTIAL_COUNTERS_ONLY")
        return result

    def runs(self):
        if self.demo:
            return {"runs": [{"id": "demo", "started_at": None, "state": "DEMO_SIMULATED",
                              "event_count": self._demo_count(), "path": "(synthetic demo data)"}], "current_run_id": "demo"}
        current = self._current_id()
        parent = self.data_dir / "runs"
        if not parent.is_dir():
            return {"runs": [], "current_run_id": current}
        entries = []
        for path in parent.iterdir():
            if not RUN_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            manifest = _json_file(path / "manifest.json", {})
            health = _json_file(path / "health.json", {})
            index = _json_file(path / "index-health.json", {})
            entries.append({"id": path.name, "started_at": manifest.get("created_utc") if isinstance(manifest, dict) else None,
                            "state": health.get("state", "UNKNOWN") if isinstance(health, dict) else "UNKNOWN",
                            "event_count": index.get("indexed_records", 0) if isinstance(index, dict) else 0,
                            "raw_schema": manifest.get("raw_schema") if isinstance(manifest, dict) else None,
                            "policy_version": manifest.get("policy_version") if isinstance(manifest, dict) else None,
                            "path": str(path)})
        entries.sort(key=lambda item: item["started_at"] or "", reverse=True)
        return {"runs": entries[:200], "current_run_id": current}

    def events(self, run_id: str | None, cursor: int | None, limit: int):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise BackendError("limit는 1..500이어야 합니다.", 400)
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise BackendError("cursor는 0 이상의 정수여야 합니다.", 400)
        if self.demo:
            return self._demo_events(cursor, limit)
        run = self._run(run_id)
        if run is None:
            return {"events": [], "cursor": cursor or 0, "run_id": None,
                    "index": {"state": "UNAVAILABLE", "coverage_state": "BOUNDARY_UNKNOWN",
                              "complete_to_durable": False}, "latest_rowid": None, "has_more": False,
                    "message": "저장된 run이 없습니다. 로그 부재를 뜻하지 않습니다."}
        health, index = self._index_snapshot(run)
        db = self._open_index(run)
        if db is None:
            return {"events": [], "cursor": cursor or 0, "run_id": run.name, "index": index,
                    "latest_rowid": None, "has_more": False,
                    "message": "인덱스가 아직 없습니다. 원본 로그 부재를 뜻하지 않습니다."}
        try:
            db.execute("BEGIN")
            latest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM events").fetchone()[0]
            if cursor is None:
                rows = db.execute("SELECT rowid,payload FROM events ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
                rows.reverse()
            else:
                rows = db.execute("SELECT rowid,payload FROM events WHERE rowid>? ORDER BY rowid LIMIT ?",
                                  (cursor, limit)).fetchall()
        except sqlite3.Error as exc:
            raise BackendError(f"인덱스를 읽지 못했습니다: {exc}", 503) from exc
        finally:
            db.close()
        manager = self._manager_module()
        manifest = _json_file(run / "manifest.json", {})
        anchor = manifest.get("clock_anchor", {}) if isinstance(manifest, dict) else {}
        converted = []
        try:
            for rowid, payload in rows:
                item = manager.decode_record(payload)
                item["rowid"] = rowid
                item["run_id"] = run.name
                item["wall_time"] = self._wall_time(item.get("monotonic_ns"), anchor)
                item["time_approximate"] = item["wall_time"] is not None
                converted.append(item)
        except (ValueError, TypeError, KeyError) as exc:
            raise BackendError(f"인덱스 레코드를 해독하지 못했습니다: {exc}", 503) from exc
        next_cursor = converted[-1]["rowid"] if converted else cursor or 0
        message = ("색인된 로그만 표시합니다. 색인 지연이나 수집 중인 원본이 있으면 목록에 아직 보이지 않을 수 있습니다."
                   if not index.get("complete_to_durable") or health.get("state") != "STOPPED" else
                   "중지된 run의 공표된 원본 경계까지 색인이 완료됐습니다.")
        return {"events": converted, "cursor": next_cursor, "run_id": run.name, "index": index,
                "latest_rowid": latest, "has_more": next_cursor < latest, "message": message}

    @staticmethod
    def _wall_time(monotonic_ns, anchor):
        if not isinstance(anchor, dict) or type(monotonic_ns) is not int:
            return None
        mono, real = anchor.get("monotonic_ns"), anchor.get("realtime_ns")
        if type(mono) is not int or type(real) is not int:
            return None
        return _iso_timestamp(real + monotonic_ns - mono)

    def files(self, run_id: str | None):
        if self.demo:
            return {"files": [], "path": "(synthetic demo data)", "run_id": "demo"}
        run = self._run(run_id)
        if run is None:
            return {"files": [], "path": str(self.data_dir / "runs"), "run_id": None}
        status = self._status(run)
        stopped = status.get("state") == "STOPPED"
        files = []
        for path in run.iterdir():
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            downloadable = path.name in RUN_FILES and info.st_size <= MAX_DOWNLOAD and (
                stopped or path.suffix not in (".raw", ".sqlite"))
            files.append({"name": path.name, "size": info.st_size,
                          "modified_at": dt.datetime.fromtimestamp(info.st_mtime, dt.UTC).isoformat(),
                          "downloadable": downloadable,
                          "url": f"/api/download?run={run.name}&file={path.name}" if downloadable else None})
        files.sort(key=lambda item: item["name"])
        return {"files": files, "path": str(run), "run_id": run.name}

    def download(self, run_id: str | None, name: str):
        if self.demo:
            raise BackendError("데모에는 내려받을 원본 파일이 없습니다.", 404)
        run = self._run(run_id)
        if run is None or not isinstance(name, str) or name not in RUN_FILES:
            raise BackendError("허용되지 않은 파일입니다.", 404)
        path = run / name
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise BackendError("파일을 찾지 못했습니다.", 404) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOWNLOAD:
            raise BackendError("내려받을 수 없는 파일입니다.", 403)
        if path.suffix in (".raw", ".sqlite") and self._status(run).get("state") != "STOPPED":
            raise BackendError("수집 중인 raw·인덱스 파일은 내려받을 수 없습니다.", 409)
        # O_NOFOLLOW blocks a path replaced with a symlink between lstat and open.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            actual = os.fstat(fd)
            if not stat.S_ISREG(actual.st_mode) or actual.st_size > MAX_DOWNLOAD:
                raise BackendError("내려받을 수 없는 파일입니다.", 403)
            return fd, actual.st_size, name
        except Exception:
            os.close(fd)
            raise

    def save_rules(self, rules, revision, schema=None):
        if schema is None:
            schema = self._config()["schema"]
            if schema != "legacy":
                raise BackendError("고급 규칙 저장에는 스키마를 명시해야 합니다.", 409)
        if not isinstance(schema, str) or schema not in RULE_SCHEMAS:
            raise BackendError("지원하지 않는 규칙 스키마입니다.", 400)
        if self.demo:
            with self.write_lock:
                if revision != self.demo_revision:
                    raise BackendError("규칙이 다른 곳에서 변경됐습니다. 새로고침 후 다시 시도하세요.", 409)
                if schema == "legacy":
                    if self.demo_schema != "legacy":
                        raise BackendError("고급 규칙을 레거시 규칙으로 덮어쓸 수 없습니다.", 409)
                    _validate_demo_rules(rules)
                else:
                    _validate_demo_event_rules(rules)
                self.demo_rules = rules
                self.demo_schema = schema
                self.demo_revision = _revision(json.dumps(rules, sort_keys=True).encode())
                return self.rules()
        with self.write_lock:
            cfg = self._config()
            if cfg["revision"] is None:
                raise BackendError("MGlogWH 설정 파일이 없습니다.", 503)
            if revision != cfg["revision"]:
                raise BackendError("규칙이 다른 곳에서 변경됐습니다. 새로고침 후 다시 시도하세요.", 409)
            if cfg["schema"] == "event_rules_v1" and schema == "legacy":
                raise BackendError("고급 규칙을 레거시 규칙으로 덮어쓸 수 없습니다.", 409)
            self._manager_module()  # Verify ownership and permissions before executing manage.py.
            directory = self.config_path.parent.lstat()
            if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022:
                raise BackendError("설정 디렉터리는 다른 사용자가 변경할 수 없어야 합니다.", 503)
            if hasattr(os, "geteuid") and os.geteuid() == 0 and (
                    directory.st_uid != 0 or cfg["uid"] != 0):
                raise BackendError("설정 디렉터리와 파일은 root 소유여야 합니다.", 503)
            value = dict(cfg["raw"])
            value.pop("critical_rules" if schema == "event_rules_v1" else "event_rules", None)
            value[RULE_SCHEMAS[schema]] = rules
            candidate = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(candidate) > MAX_CONFIG:
                raise BackendError("설정 파일은 64 KiB 이하여야 합니다.", 400)
            temp = None
            try:
                fd, temp = tempfile.mkstemp(prefix=".config.controlroom.", suffix=".json", dir=self.config_path.parent)
                os.fchmod(fd, 0o600)
                os.fchown(fd, cfg["uid"], cfg["gid"])
                with os.fdopen(fd, "wb") as stream:
                    stream.write(candidate)
                    stream.flush()
                    os.fsync(stream.fileno())
                cmd = [sys.executable, str(self.sensor_root / "manage.py"), "validate", "--config", temp]
                try:
                    environment = os.environ.copy()
                    environment["MGLOGWH_DATA_DIR"] = str(self.data_dir)
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False,
                                            env=environment)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise BackendError(f"MGlogWH 설정 검증을 실행하지 못했습니다: {exc}", 503) from exc
                if result.returncode:
                    raise BackendError((result.stderr or result.stdout).strip()[:1000] or "설정 검증 실패", 400)
                # Detect edits made by another process while MGlogWH validated the candidate.
                if self._config()["revision"] != revision:
                    raise BackendError("검증 중 설정이 변경됐습니다. 새로고침 후 다시 시도하세요.", 409)
                os.chmod(temp, min(cfg["mode"], 0o600))
                os.replace(temp, self.config_path)
                temp = None
                dirfd = os.open(self.config_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
                return self.rules()
            finally:
                if temp is not None:
                    try:
                        os.unlink(temp)
                    except FileNotFoundError:
                        pass

    def apply(self, revision: str, confirm: str):
        if confirm != "restart":
            raise BackendError("재시작을 확인해야 합니다.", 400)
        with self.write_lock:
            cfg = self._config()
            if cfg["revision"] is None:
                raise BackendError("MGlogWH 설정 파일이 없습니다.", 503)
            if revision != cfg["revision"]:
                raise BackendError("설정이 변경됐습니다. 새로고침 후 다시 시도하세요.", 409)
            if self.demo:
                return {"ok": True, "demo": True, "message": "데모에서는 서비스를 재시작하지 않습니다.",
                        "run_id": "demo"}
            self._manager_module()
            try:
                validation = subprocess.run([sys.executable, str(self.sensor_root / "manage.py"), "validate",
                                             "--config", str(self.config_path)], capture_output=True, text=True,
                                            timeout=10, check=False,
                                            env={**os.environ, "MGLOGWH_DATA_DIR": str(self.data_dir)})
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BackendError(f"현재 설정을 검증하지 못했습니다: {exc}", 503) from exc
            if validation.returncode:
                raise BackendError((validation.stderr or validation.stdout).strip()[:1000] or
                                   "현재 설정이 유효하지 않습니다.", 400)
            if self._config()["revision"] != revision:
                raise BackendError("검증 중 설정이 변경됐습니다. 새로고침 후 다시 시도하세요.", 409)
            try:
                result = subprocess.run(["systemctl", "restart", "mglogwh.service"],
                                        capture_output=True, text=True, timeout=90, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BackendError(f"MGlogWH 서비스를 재시작하지 못했습니다: {exc}", 503) from exc
            if result.returncode:
                raise BackendError((result.stderr or result.stdout).strip()[:1000] or "서비스 재시작 실패", 503)
            return {"ok": True, "demo": False, "run_id": self._current_id(),
                    "message": "MGlogWH 서비스 재시작을 요청했습니다. 새 run 상태를 확인하세요."}

    def _demo_count(self):
        return 36 + int((time.monotonic() - self.demo_started) * 2)

    def _demo_events(self, cursor: int | None, limit: int):
        count = self._demo_count()
        begin = max(1, count - limit + 1) if cursor is None else max(1, cursor + 1)
        paths = ("/srv/payment/keys", "/var/log/auth.log", "/usr/bin/sudo", "/etc/nginx/nginx.conf",
                 "/home/operator/report.txt", "/tmp/job-output")
        names = ("api-worker", "sshd", "sudo", "nginx", "backup", "cron")
        records = []
        now = time.time_ns()
        for i in range(begin, min(count, begin + limit - 1) + 1):
            priority = 2 if i % 4 == 0 else 1 if i % 2 == 0 else 0
            records.append({"rowid": i, "run_id": "demo", "monotonic_ns": 0,
                            "wall_time": _iso_timestamp(now - (count - i) * 500_000_000),
                            "time_approximate": True,
                            "priority": priority, "kind": 2 if i % 3 else 1,
                            "pid": 1200 + i % 700, "uid": 0 if priority == 2 else 1000,
                            "comm": names[i % len(names)], "path": paths[i % len(paths)],
                            "result": -13 if priority == 2 and i % 8 == 0 else 0,
                            "effective_policy": "CRITICAL" if priority == 2 else "NORMAL" if priority == 1 else "BULK",
                            "rule_id": 1 if priority == 2 else 0, "synthetic": True})
        return {"events": records, "cursor": records[-1]["rowid"] if records else cursor or 0,
                "run_id": "demo", "index": {"state": "DEMO_SIMULATED", "coverage_state": "SIMULATED",
                                               "complete_to_durable": False, "lag_records": 0},
                "latest_rowid": count, "has_more": bool(records and records[-1]["rowid"] < count),
                "message": "합성 데모 로그입니다. 실제 MGlogWH 수집 결과가 아닙니다."}


def _validate_demo_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
        raise BackendError("규칙은 1..8개여야 합니다.")
    ids, targets = set(), set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"id", "event_type", "path", "protected"}:
            raise BackendError("규칙 키가 올바르지 않습니다.")
        if type(rule["id"]) is not int or not 1 <= rule["id"] <= 255 or rule["id"] in ids:
            raise BackendError("규칙 ID가 올바르지 않습니다.")
        if rule["event_type"] not in ("openat", "exec") or type(rule["protected"]) is not bool:
            raise BackendError("규칙 종류·보호 값이 올바르지 않습니다.")
        path = rule["path"]
        if not isinstance(path, str) or not path.startswith("/") or len(path.encode()) > 126 or any(ord(c) < 32 for c in path) or ".." in Path(path).parts:
            raise BackendError("Linux 절대 경로가 필요합니다.")
        if (rule["event_type"], path) in targets:
            raise BackendError("중복 규칙 경로입니다.")
        ids.add(rule["id"]); targets.add((rule["event_type"], path))


def _validate_demo_event_rules(rules):
    """Validate the editor shape in demo; live saves always use MGlogWH validate."""
    if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
        raise BackendError("고급 규칙은 1..8개여야 합니다.")
    ids = set()

    def condition(node, depth, counter):
        counter[0] += 1
        if counter[0] > 24 or depth > 4 or not isinstance(node, dict):
            raise BackendError("조건은 규칙당 24개 노드·깊이 4 이하여야 합니다.")
        if set(node) == {"not"}:
            return condition(node["not"], depth + 1, counter)
        if set(node) in ({"all"}, {"any"}):
            children = next(iter(node.values()))
            if not isinstance(children, list) or not 2 <= len(children) <= 8:
                raise BackendError("all/any에는 2..8개 조건이 필요합니다.")
            for child in children:
                condition(child, depth + 1, counter)
            return
        if (set(node) != {"field", "op", "value"} or
                not isinstance(node["field"], str) or not isinstance(node["op"], str)):
            raise BackendError("조건은 field, op, value를 지정해야 합니다.")

    for rule in rules:
        if not isinstance(rule, dict) or not {"id", "when", "priority", "overload_policy"} <= set(rule):
            raise BackendError("고급 규칙 필수 키가 없습니다.")
        if set(rule) - {"id", "enabled", "precedence", "when", "priority", "overload_policy", "quarantine"}:
            raise BackendError("고급 규칙에 지원하지 않는 키가 있습니다.")
        rid = rule["id"]
        if type(rid) is not int or not 1 <= rid <= 255 or rid in ids:
            raise BackendError("고급 규칙 ID는 중복 없는 정수 1..255여야 합니다.")
        ids.add(rid)
        if "enabled" in rule and type(rule["enabled"]) is not bool:
            raise BackendError("enabled는 boolean이어야 합니다.")
        if "precedence" in rule and (type(rule["precedence"]) is not int or
                                      not 0 <= rule["precedence"] <= 1000):
            raise BackendError("precedence는 정수 0..1000이어야 합니다.")
        if rule["priority"] not in ("BULK", "NORMAL", "CRITICAL"):
            raise BackendError("지원하지 않는 priority입니다.")
        if rule["overload_policy"] not in ("NORMAL_COLLECTION", "QUARANTINE", "QUARANTINE_EXEMPT"):
            raise BackendError("지원하지 않는 overload_policy입니다.")
        if rule["priority"] != "CRITICAL" and rule["overload_policy"] != "NORMAL_COLLECTION":
            raise BackendError("BULK/NORMAL은 NORMAL_COLLECTION만 지원합니다.")
        if "quarantine" in rule and (rule["overload_policy"] != "QUARANTINE" or
                                     not isinstance(rule["quarantine"], dict) or not rule["quarantine"]):
            raise BackendError("quarantine override는 CRITICAL+QUARANTINE에서만 사용합니다.")
        condition(rule["when"], 1, [0])
