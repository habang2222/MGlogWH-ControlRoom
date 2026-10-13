"""Backend contract and security regressions; run with python3 -m unittest discover."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import threading
import unittest
from unittest import mock
import uuid
import zlib

from controlroom.backend import BackendError, SensorBackend
from controlroom.server import ControlRoomServer, load_token


SOURCE = os.environ.get("MGLOGWH_MANAGE")
FAKE_MANAGER = '''
import argparse
import json
from pathlib import Path

def decode_record(raw):
    return json.loads(raw)

def index_coverage(health, offsets):
    durable = health.get('durable', {})
    if not durable:
        return dict(coverage_state='BOUNDARY_UNKNOWN', lag_records=None, complete_to_durable=False)
    lag = sum(durable.get(lane, 0) - offsets.get(lane, 0) for lane in durable)
    complete = health.get('state') == 'STOPPED' and lag == 0
    return dict(coverage_state='INDEX_COMPLETE' if complete else 'INDEXING',
                lag_records=lag // 240, complete_to_durable=complete)

def status(run):
    health = json.loads((run / 'health.json').read_text())
    index = json.loads((run / 'index-health.json').read_text())
    return dict(state=health['state'], collector=health, index=dict(index, **index_coverage(health, index['offsets'])))

def validate_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
        raise ValueError('rules 1..8')
    for rule in rules:
        if (set(rule) != {'id','event_type','path','protected'} or
            rule['event_type'] not in ('openat','exec') or not rule['path'].startswith('/')):
            raise ValueError('invalid rule')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action')
    parser.add_argument('--config')
    args = parser.parse_args()
    value = json.loads(Path(args.config).read_text())
    if 'event_rules' in value:
        if 'critical_rules' in value:
            raise ValueError('both rule schemas')
        rules = value['event_rules']
        if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
            raise ValueError('event_rules 1..8')
        for rule in rules:
            if not {'id','when','priority','overload_policy'} <= set(rule):
                raise ValueError('invalid event rule')
    else:
        validate_rules(value['critical_rules'])
'''


def encode_record(priority, sequence, path, real):
    if not real:
        return json.dumps({"priority": priority, "sequence": sequence, "path": path,
                           "monotonic_ns": 100_000_000_000 + sequence,
                           "pid": 1234, "uid": 1000, "comm": "worker", "kind": 2,
                           "result": 0, "effective_policy": "CRITICAL" if priority == 2 else "NORMAL",
                           "rule_id": 1 if priority == 2 else 0}).encode()
    event = struct.Struct("<4Qq12I16s128s")
    quality = 1 << 16 if priority == 2 else 0
    values = (100_000_000_000 + sequence, 42, sequence, 1, 0,
              0x31514f45, 2, 2, priority, 1234, 1234, 1000, 1, 0, 0,
              len(path), quality, b"worker", path.encode())
    payload = event.pack(*values)
    return payload + struct.pack("<II", zlib.crc32(payload), 0)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sensor = self.root / "sensor"
        self.sensor.mkdir()
        self.real_manager = bool(SOURCE)
        if SOURCE:
            shutil.copy2(SOURCE, self.sensor / "manage.py")
        else:
            (self.sensor / "manage.py").write_text(FAKE_MANAGER)
        (self.sensor / "manage.py").chmod(0o644)
        self.data = self.root / "data"
        self.run_id = str(uuid.uuid4())
        self.run = self.data / "runs" / self.run_id
        self.run.mkdir(parents=True)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "bulk_rate": 500, "bulk_burst": 1000, "protected_path": "/tmp/decoy",
            "max_raw_mib": 512, "critical_reserve_mib": 64, "max_index_mib": 256,
            "minimum_free_mib": 1024, "critical_rules": [
                {"id": 1, "event_type": "openat", "path": "/tmp/decoy", "protected": False}],
        }))
        self.config.chmod(0o600)
        (self.data / "current.json").write_text(json.dumps({"run_id": self.run_id}))
        (self.run / "manifest.json").write_text(json.dumps({
            "created_utc": "2026-10-12T00:00:00+00:00",
            "clock_anchor": {"monotonic_ns": 100_000_000_000, "realtime_ns": 1_791_760_000_000_000_000},
        }))
        (self.run / "service.json").write_text(json.dumps({"state": "STOPPED"}))
        (self.run / "health.json").write_text(json.dumps({
            "state": "STOPPED", "durable": {"critical.raw": 240, "general.raw": 240, "quarantine.raw": 0},
            "general_quota_drop": 3, "queue_overflow": [0, 1, 0],
        }))
        (self.run / "index-health.json").write_text(json.dumps({
            "state": "INDEX_COMPLETE", "offsets": {"critical.raw": 240, "general.raw": 240, "quarantine.raw": 0},
            "indexed_records": 2,
        }))
        with sqlite3.connect(self.run / "index.sqlite") as db:
            db.execute("CREATE TABLE events (cgroup TEXT, seq INTEGER, monotonic_ns INTEGER, priority INTEGER, "
                       "pid INTEGER, uid INTEGER, kind INTEGER, path TEXT, payload BLOB, PRIMARY KEY(cgroup,seq))")
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                       ("42", 1, 100_000_000_001, 1, 1234, 1000, 2, "/tmp/one", encode_record(1, 1, "/tmp/one", self.real_manager)))
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                       ("42", 2, 100_000_000_002, 2, 1234, 1000, 2, "/tmp/two", encode_record(2, 2, "/tmp/two", self.real_manager)))
        self.backend = SensorBackend(sensor_root=self.sensor, data_dir=self.data,
                                     config_path=self.config, mode="linux")

    def tearDown(self):
        self.temp.cleanup()


class BackendTests(Fixture):
    def test_rowid_cursor_and_conservative_coverage(self):
        first = self.backend.events(None, None, 1)
        self.assertEqual([item["path"] for item in first["events"]], ["/tmp/two"])
        self.assertEqual(first["cursor"], 2)
        self.assertEqual(first["latest_rowid"], 2)
        self.assertFalse(first["has_more"])
        self.assertTrue(first["index"]["complete_to_durable"])
        self.assertTrue(first["events"][0]["time_approximate"])
        page = self.backend.events(None, 0, 1)
        self.assertEqual(page["cursor"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(self.backend.events(None, 1, 10)["cursor"], 2)
        self.assertEqual(self.backend.events(None, 2, 10)["events"], [])
        (self.run / "health.json").write_text(json.dumps({
            "state": "RUNNING", "durable": {"critical.raw": 480, "general.raw": 240, "quarantine.raw": 0},
        }))
        lagged = self.backend.events(None, 2, 10)
        self.assertEqual(lagged["index"]["lag_records"], 1)
        self.assertFalse(lagged["index"]["complete_to_durable"])
        self.assertIn("아직", lagged["message"])

    def test_counts_incrementally_and_rules_revision(self):
        self.assertEqual(self.backend.overview()["counts"], {"total": 2, "critical": 1, "normal": 1, "bulk": 0})
        rules = self.backend.rules()
        changed = [{"id": 2, "event_type": "exec", "path": "/usr/bin/sudo", "protected": True}]
        saved = self.backend.save_rules(changed, rules["revision"])
        self.assertNotEqual(saved["revision"], rules["revision"])
        self.assertEqual(saved["rules"], changed)
        self.assertEqual(self.backend.overview()["counts"]["total"], 2)
        with self.assertRaises(BackendError) as stale:
            self.backend.save_rules(changed, rules["revision"])
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(BackendError) as invalid:
            self.backend.save_rules([{"id": 3, "event_type": "exec", "path": "relative", "protected": False}],
                                    saved["revision"])
        self.assertEqual(invalid.exception.status, 400)
        self.assertEqual(self.backend.rules()["revision"], saved["revision"])

    def test_event_rule_migration_and_schema_guard(self):
        rule = {"id": 1, "when": {"all": [
            {"field": "event_type", "op": "eq", "value": "openat"},
            {"field": "path", "op": "eq", "value": "/tmp/decoy"}]},
            "priority": "CRITICAL", "overload_policy": "QUARANTINE",
            "quarantine": {"quarantine_sample_interval_ms": 100}}
        revision = self.backend.rules()["revision"]
        saved = self.backend.save_rules([rule], revision, "event_rules_v1")
        self.assertEqual(saved["schema"], "event_rules_v1")
        value = json.loads(self.config.read_text())
        self.assertEqual(value["event_rules"], [rule])
        self.assertNotIn("critical_rules", value)
        self.assertEqual(value["bulk_rate"], 500)
        with self.assertRaises(BackendError) as downgrade:
            self.backend.save_rules([], saved["revision"], "legacy")
        self.assertEqual(downgrade.exception.status, 409)
        with self.assertRaises(BackendError) as old_client:
            self.backend.save_rules([rule], saved["revision"])
        self.assertEqual(old_client.exception.status, 409)

    def test_implicit_legacy_rule_is_visible_for_migration(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                value = json.loads(self.config.read_text())
                if missing:
                    value.pop("critical_rules", None)
                else:
                    value["critical_rules"] = []
                self.config.write_text(json.dumps(value))
                current = self.backend.rules()
                self.assertEqual(current["schema"], "legacy")
                self.assertEqual(current["rules"], [
                    {"id": 1, "event_type": "openat", "path": value["protected_path"],
                     "protected": False}])

    def test_rule_stats_are_forwarded_without_guessing_missing_values(self):
        health = json.loads((self.run / "health.json").read_text())
        health.update(rule_stats=[{"rule_id": 1, "matched": 10, "selected": 7,
                                   "ring_failed": None, "collector_durable": [2, 0, 0]}],
                      rule_stats_scope="KERNEL_AND_COLLECTOR_SNAPSHOT",
                      rule_stats_snapshot_complete=False)
        (self.run / "health.json").write_text(json.dumps(health))
        overview = self.backend.overview()
        self.assertEqual(overview["rule_stats"][0]["selected"], 7)
        self.assertIsNone(overview["rule_stats"][0]["ring_failed"])
        self.assertFalse(overview["rule_stats_snapshot_complete"])

    def test_running_controlroom_uses_updated_sensor_manager(self):
        self.assertEqual(self.backend.events(None, None, 1)["events"][0]["path"], "/tmp/two")
        installed = self.sensor / "manage.py"
        replacement = self.sensor / "manage.py.updated"
        replacement.write_text(installed.read_text() +
                               "\ndef decode_record(raw):\n    return {'path': '/updated'}\n")
        replacement.chmod(0o644)
        os.replace(replacement, installed)
        self.assertEqual(self.backend.events(None, None, 1)["events"][0]["path"], "/updated")

    def test_file_allowlist_and_no_symlink_escape(self):
        names = {entry["name"]: entry for entry in self.backend.files(None)["files"]}
        self.assertTrue(names["manifest.json"]["downloadable"])
        with self.assertRaises(BackendError):
            self.backend.download(self.run_id, "../config.json")
        (self.run / "collector.log").symlink_to(self.config)
        with self.assertRaises(BackendError):
            self.backend.download(self.run_id, "collector.log")
        bad = self.data / "runs" / str(uuid.uuid4())
        bad.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(BackendError):
            self.backend.files(bad.name)

    def test_demo_without_sensor_and_unconfigured_live(self):
        missing = self.root / "missing"
        live = SensorBackend(sensor_root=missing, data_dir=missing, config_path=missing / "config.json",
                             mode="wsl")
        self.assertEqual(live.overview()["collector"]["state"], "UNCONFIGURED")
        self.assertIn("부재를 뜻하지", live.events(None, None, 20)["message"])
        demo = SensorBackend(sensor_root=missing, data_dir=missing, config_path=missing / "config.json",
                             mode="wsl", demo=True)
        first = demo.events(None, None, 10)
        self.assertTrue(first["events"][0]["synthetic"])
        self.assertEqual(demo.apply(demo.rules()["revision"], "restart")["demo"], True)
        changed = [{"id": 7, "event_type": "exec", "path": "/usr/bin/bash", "protected": False}]
        self.assertEqual(demo.save_rules(changed, demo.rules()["revision"])["rules"], changed)

    def test_demo_rejects_unsupported_advanced_policy_combination(self):
        demo = SensorBackend(sensor_root=self.sensor, data_dir=self.data,
                             config_path=self.config, mode="linux", demo=True)
        invalid = [{"id": 1, "when": {"field": "path", "op": "eq", "value": "/etc/shadow"},
                    "priority": "NORMAL", "overload_policy": "QUARANTINE"}]
        with self.assertRaises(BackendError):
            demo.save_rules(invalid, demo.rules()["revision"], "event_rules_v1")

    def test_apply_requires_explicit_confirmation_and_current_revision(self):
        revision = self.backend.rules()["revision"]
        with mock.patch("controlroom.backend.subprocess.run") as run:
            with self.assertRaises(BackendError):
                self.backend.apply(revision, "")
            with self.assertRaises(BackendError) as stale:
                self.backend.apply("stale", "restart")
            self.assertEqual(stale.exception.status, 409)
            run.assert_not_called()
            run.return_value.returncode = 0
            applied = self.backend.apply(revision, "restart")
            self.assertTrue(applied["ok"])
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][2], "validate")
            self.assertEqual(run.call_args_list[1].args[0], ["systemctl", "restart", "mglogwh.service"])


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.token = load_token(self.root / "private" / "token")
        self.server = ControlRoomServer(("127.0.0.1", 0), self.backend, self.token,
                                        Path(__file__).resolve().parents[1] / "web")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        values = {"Host": f"127.0.0.1:{self.server.server_port}", **(headers or {})}
        if body is not None:
            values["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=values)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(data) if data else None)
        conn.close()
        return result

    def test_auth_csrf_origin_and_host(self):
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"token": "bad"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"token": self.token},
                                      {"Origin": "http://evil.example"})[0], 403)
        status, headers, body = self.request("POST", "/api/login", {"token": self.token})
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request("POST", "/api/rules", {"rules": [], "revision": "x"},
                                      {"Cookie": cookie})[0], 403)
        self.assertEqual(self.request("GET", "/api/runs", headers={"Cookie": cookie,
                                      "Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/apply", {"confirm": "restart", "revision": "x"},
                                      {"Cookie": cookie, "X-CSRF-Token": body["csrf_token"],
                                       "Origin": "http://evil.example"})[0], 403)

    def test_advanced_editor_rejects_duplicate_json_keys(self):
        _, headers, session = self.request("POST", "/api/login", {"token": self.token})
        auth = {"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                "X-CSRF-Token": session["csrf_token"]}
        revision = self.backend.rules()["revision"]
        bad = '[{"id":1,"id":2,"when":{"field":"path","op":"eq","value":"/tmp/x"},' \
              '"priority":"CRITICAL","overload_policy":"QUARANTINE"}]'
        status, _, _ = self.request("POST", "/api/rules",
                                    {"schema": "event_rules_v1", "rules_json": bad,
                                     "revision": revision}, auth)
        self.assertEqual(status, 400)
        self.assertEqual(self.backend.rules()["schema"], "legacy")
        status, _, _ = self.request("POST", "/api/rules",
                                    {"schema": "event_rules_v1", "rules_json": '[{"id":NaN}]',
                                     "revision": revision}, auth)
        self.assertEqual(status, 400)
        good = json.dumps([{"id": 4, "when": {"field": "uid", "op": "eq", "value": 1000},
                            "priority": "CRITICAL", "overload_policy": "QUARANTINE_EXEMPT"}])
        status, _, response = self.request("POST", "/api/rules",
                                          {"schema": "event_rules_v1", "rules_json": good,
                                           "revision": revision}, auth)
        self.assertEqual(status, 200)
        self.assertEqual(response["schema"], "event_rules_v1")
        self.assertEqual(response["rules"][0]["when"]["field"], "uid")


if __name__ == "__main__":
    unittest.main()
