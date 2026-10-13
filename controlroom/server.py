"""Loopback HTTP server for the control room."""

from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .backend import BackendError, SensorBackend


COOKIE = "mglogwh_session"
SESSION_SECONDS = 12 * 3600
MAX_BODY = 256 * 1024  # JSON editor text is escaped once more inside the API envelope.
STATIC_FILES = {"/": "index.html", "/index.html": "index.html",
                "/app.css": "app.css", "/app.js": "app.js"}


def load_token(path: Path) -> str:
    """Create a private token once; refuse symlinks and shared token files."""
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
        raise BackendError("토큰 디렉터리는 다른 사용자가 변경할 수 없어야 합니다.", 503)
    if hasattr(os, "geteuid") and os.geteuid() == 0 and parent.st_uid != 0:
        raise BackendError("토큰 디렉터리는 root 소유여야 합니다.", 503)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        fd = None
    if fd is not None:
        try:
            value = secrets.token_urlsafe(36)
            os.write(fd, (value + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise BackendError("토큰 파일은 일반 파일이며 권한이 0600이어야 합니다.", 503)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise BackendError("토큰 파일 소유자가 실행 계정과 다릅니다.", 503)
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32 or len(token) > 256:
        raise BackendError("토큰 파일 내용이 올바르지 않습니다.", 503)
    return token


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 JSON 키: " + key)
        result[key] = value
    return result


def _json_nonfinite(value):
    raise ValueError("JSON에서는 NaN/Infinity를 사용할 수 없습니다: " + value)


class ControlRoomServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, backend: SensorBackend, token: str, web_dir: Path):
        host, port = address
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("서버는 127.0.0.1에만 바인딩할 수 있습니다.")
        if host == "localhost":
            host = "127.0.0.1"
        self.backend = backend
        self.token = token
        self.web_dir = Path(web_dir)
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        super().__init__((host, port), ControlRoomHandler)

    def new_session(self):
        sid, csrf = secrets.token_urlsafe(36), secrets.token_urlsafe(32)
        with self.sessions_lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value[1] >= now}
            if len(self.sessions) >= 1024:
                oldest = min(self.sessions, key=lambda key: self.sessions[key][1])
                del self.sessions[oldest]
            self.sessions[sid] = (csrf, time.monotonic() + SESSION_SECONDS)
        return sid, csrf

    def session(self, sid):
        with self.sessions_lock:
            value = self.sessions.get(sid)
            if value is None:
                return None
            if value[1] < time.monotonic():
                del self.sessions[sid]
                return None
            return value[0]

    def logout(self, sid):
        with self.sessions_lock:
            self.sessions.pop(sid, None)


class ControlRoomHandler(BaseHTTPRequestHandler):
    server: ControlRoomServer
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):
        # Polling every second would otherwise produce an unbounded access log.
        try:
            if int(code) < 400:
                return
        except (TypeError, ValueError):
            pass
        super().log_request(code, size)

    def log_error(self, format, *args):
        # An idle keep-alive socket reaching our ten-second limit is expected.
        if format.startswith("Request timed out:"):
            return
        super().log_error(format, *args)

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'self'")
        self.send_header("Vary", "Cookie")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status, value, extra=None):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data), extra)
        self.wfile.write(data)

    def _error(self, status, message):
        # A rejected POST may leave unread bytes. Never parse those as another request.
        self.close_connection = True
        self._json(status, {"error": message}, {"Connection": "close"})

    def _request_target(self):
        raw = urlsplit(self.path)
        if raw.scheme or raw.netloc or not raw.path.startswith("/"):
            raise BackendError("잘못된 요청 경로입니다.", 400)
        return raw.path, parse_qs(raw.query, keep_blank_values=True)

    def _check_host(self):
        host = self.headers.get("Host", "")
        if not host or "@" in host or "/" in host or "\\" in host:
            raise BackendError("허용되지 않은 Host입니다.", 403)
        try:
            parsed = urlsplit("http://" + host)
            name, port = parsed.hostname, parsed.port
        except ValueError as exc:
            raise BackendError("허용되지 않은 Host입니다.", 403) from exc
        if name not in ("127.0.0.1", "localhost") or port not in (None, self.server.server_port):
            raise BackendError("허용되지 않은 Host입니다.", 403)

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return
        try:
            parsed = urlsplit(origin)
            request_host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            allowed = (parsed.scheme == "http" and parsed.hostname == request_host and
                       parsed.port == self.server.server_port and not parsed.username and not parsed.password and
                       not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            raise BackendError("다른 출처의 요청은 허용하지 않습니다.", 403)

    def _sid(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(COOKIE)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _authenticated(self):
        return self.server.session(self._sid())

    def _require_session(self):
        csrf = self._authenticated()
        if csrf is None:
            raise BackendError("로그인이 필요합니다.", 401)
        return csrf

    def _require_csrf(self):
        csrf = self._require_session()
        supplied = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(csrf, supplied):
            raise BackendError("CSRF 토큰이 올바르지 않습니다.", 403)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise BackendError("Chunked 요청은 지원하지 않습니다.", 400)
        if self.headers.get_content_type() != "application/json":
            raise BackendError("JSON 요청이 필요합니다.", 415)
        size = self.headers.get("Content-Length")
        if size is None:
            raise BackendError("Content-Length가 필요합니다.", 411)
        try:
            length = int(size)
        except ValueError as exc:
            raise BackendError("Content-Length가 잘못됐습니다.", 400) from exc
        if not 0 <= length <= MAX_BODY:
            raise BackendError("요청이 너무 큽니다.", 413)
        try:
            value = json.loads(self.rfile.read(length), object_pairs_hook=_json_unique,
                               parse_constant=_json_nonfinite)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise BackendError(f"JSON을 읽지 못했습니다: {exc}", 400) from exc
        if not isinstance(value, dict):
            raise BackendError("JSON 객체가 필요합니다.", 400)
        return value

    @staticmethod
    def _single(query, name, default=None):
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1:
            raise BackendError(f"{name}은 한 번만 입력하세요.", 400)
        return values[0]

    def do_GET(self):
        try:
            self._check_host()
            path, query = self._request_target()
            if path in STATIC_FILES:
                return self._static(STATIC_FILES[path])
            if path == "/api/session":
                csrf = self._authenticated()
                return self._json(200, {"authenticated": csrf is not None,
                                        "demo": self.server.backend.demo, "mode": self.server.backend.mode,
                                        "csrf_token": csrf})
            self._require_session()
            backend = self.server.backend
            if path == "/api/overview":
                return self._json(200, backend.overview())
            if path == "/api/runs":
                return self._json(200, backend.runs())
            if path == "/api/rules":
                return self._json(200, backend.rules())
            if path == "/api/events":
                run_id = self._single(query, "run")
                raw_cursor = self._single(query, "cursor")
                raw_limit = self._single(query, "limit", "100")
                try:
                    cursor = int(raw_cursor) if raw_cursor is not None else None
                    limit = int(raw_limit)
                except ValueError as exc:
                    raise BackendError("cursor·limit는 정수여야 합니다.", 400) from exc
                return self._json(200, backend.events(run_id, cursor, limit))
            if path == "/api/files":
                return self._json(200, backend.files(self._single(query, "run")))
            if path == "/api/download":
                return self._download(self._single(query, "run"), self._single(query, "file"))
            raise BackendError("요청 경로가 없습니다.", 404)
        except BackendError as exc:
            self._error(exc.status, str(exc))
        except (OSError, ValueError) as exc:
            self.log_error("GET failed: %s", exc)
            self._error(503, "요청을 처리하지 못했습니다.")

    def do_POST(self):
        try:
            self._check_host()
            self._check_origin()
            path, query = self._request_target()
            if query:
                raise BackendError("POST 경로에 쿼리는 허용하지 않습니다.", 400)
            if path == "/api/login":
                value = self._body()
                token = value.get("token")
                if not isinstance(token, str) or not secrets.compare_digest(token, self.server.token):
                    raise BackendError("토큰이 올바르지 않습니다.", 401)
                sid, csrf = self.server.new_session()
                cookie = f"{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
                return self._json(200, {"authenticated": True, "demo": self.server.backend.demo,
                                        "mode": self.server.backend.mode, "csrf_token": csrf},
                                  {"Set-Cookie": cookie})
            self._require_csrf()
            if path == "/api/logout":
                self._body()
                self.server.logout(self._sid())
                return self._json(200, {"authenticated": False},
                                  {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            backend = self.server.backend
            if path == "/api/rules":
                value = self._body()
                if set(value) == {"rules", "revision"}:
                    # Preserve the original client contract for legacy installations.
                    return self._json(200, backend.save_rules(value["rules"], value["revision"]))
                if set(value) == {"schema", "rules_json", "revision"}:
                    if not isinstance(value["rules_json"], str):
                        raise BackendError("rules_json은 JSON 문자열이어야 합니다.", 400)
                    try:
                        rules = json.loads(value["rules_json"], object_pairs_hook=_json_unique,
                                           parse_constant=_json_nonfinite)
                    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
                        raise BackendError(f"규칙 JSON을 읽지 못했습니다: {exc}", 400) from exc
                    return self._json(200, backend.save_rules(rules, value["revision"], value["schema"]))
                raise BackendError("schema, rules_json, revision이 필요합니다.", 400)
            if path == "/api/apply":
                value = self._body()
                if set(value) != {"confirm", "revision"}:
                    raise BackendError("confirm과 revision이 필요합니다.", 400)
                return self._json(200, backend.apply(value["revision"], value["confirm"]))
            raise BackendError("요청 경로가 없습니다.", 404)
        except BackendError as exc:
            self._error(exc.status, str(exc))
        except (OSError, ValueError) as exc:
            self.log_error("POST failed: %s", exc)
            self._error(503, "요청을 처리하지 못했습니다.")

    def _static(self, name):
        path = self.server.web_dir / name
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise BackendError("화면 파일이 없습니다.", 404) from exc
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind == "application/javascript":
            kind += "; charset=utf-8"
        self._headers(200, kind, len(data))
        self.wfile.write(data)

    def _download(self, run_id, name):
        fd, size, filename = self.server.backend.download(run_id, name)
        try:
            self._headers(200, "application/octet-stream", size,
                          {"Content-Disposition": f'attachment; filename="{filename}"'})
            remaining = size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
