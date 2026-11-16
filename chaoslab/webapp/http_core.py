"""
ChaosLab Online MVP - minimal HTTP framework (stdlib only)
==========================================================
Router, request/response helpers, multipart parsing, static files, cookie auth,
CSRF (Origin) protection and security headers. Feature modules register routes
on the shared `router`.
"""

from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse

from . import auth, config

JSON_BODY_LIMIT = 256 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers, body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.params: Dict[str, str] = {}
        self.user: Optional[Dict[str, Any]] = None
        self.token: Optional[str] = None

    def qs(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[0] if values else default

    def json(self) -> Dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "Content-Type must be application/json.")
        try:
            data = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "Request body is not valid JSON.")
        if not isinstance(data, dict):
            raise HttpError(400, "JSON body must be an object.")
        return data

    def multipart(self) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        """Parse multipart/form-data -> (text fields, files{name: {filename, content_type, data}})."""
        ctype = self.headers.get("Content-Type") or ""
        if not ctype.lower().startswith("multipart/form-data"):
            raise HttpError(415, "Content-Type must be multipart/form-data.")
        raw = b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + self.body
        try:
            msg = BytesParser(policy=policy.HTTP).parsebytes(raw)
            fields: Dict[str, str] = {}
            files: Dict[str, Dict[str, Any]] = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if filename is not None:
                    files[name] = {"filename": filename, "content_type": part.get_content_type(), "data": payload}
                else:
                    fields[name] = payload.decode("utf-8", "replace")
            return fields, files
        except HttpError:
            raise
        except Exception:
            raise HttpError(400, "Malformed multipart/form-data body.")


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "application/json",
                 headers: Optional[List[Tuple[str, str]]] = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []


def json_response(data: Any, status: int = 200, headers: Optional[List[Tuple[str, str]]] = None) -> Response:
    return Response(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", headers)


class Router:
    def __init__(self):
        self.routes: List[Tuple[str, "re.Pattern", Callable, bool]] = []

    def add(self, method: str, pattern: str, handler: Callable, auth_required: bool = True):
        regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method, regex, handler, auth_required))

    def route(self, method: str, pattern: str, auth_required: bool = True):
        def deco(fn):
            self.add(method, pattern, fn, auth_required)
            return fn
        return deco

    def resolve(self, method: str, path: str):
        path_matched = False
        for m, regex, handler, auth_required in self.routes:
            match = regex.match(path)
            if match:
                path_matched = True
                if m == method:
                    return handler, auth_required, match.groupdict()
        if path_matched:
            raise HttpError(405, "Method not allowed.")
        return None


router = Router()


def _security_headers(is_report_html: bool = False) -> List[Tuple[str, str]]:
    headers = [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
    ]
    if is_report_html:
        headers += [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox"),
                    ("X-Frame-Options", "SAMEORIGIN")]
    else:
        headers += [("Content-Security-Policy",
                     "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                     "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-src 'self'; "
                     "frame-ancestors 'self'; base-uri 'none'; form-action 'self'"),
                    ("X-Frame-Options", "SAMEORIGIN")]
    return headers


def session_cookie_header(token: str, max_age: int) -> Tuple[str, str]:
    parts = [f"{config.SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if config.COOKIE_SECURE:
        parts.append("Secure")
    return ("Set-Cookie", "; ".join(parts))


class Handler(BaseHTTPRequestHandler):
    server_version = "ChaosLabOnline/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("CHAOSLAB_ACCESS_LOG"):
            super().log_message(fmt, *args)

    # -- dispatch ---------------------------------------------------------------------------
    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_DELETE(self): self._dispatch("DELETE")

    def _dispatch(self, method: str):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)

            if method == "GET" and (path == "/" or path.startswith("/static/")):
                return self._send(self._static(path))
            if method == "GET" and not path.startswith("/api/"):
                return self._send(self._static("/"))  # SPA fallback

            self._check_origin(method)
            body = self._read_body(method)
            req = Request(method, path, urllib.parse.parse_qs(parsed.query), self.headers, body)

            resolved = router.resolve(method, path)
            if not resolved:
                raise HttpError(404, "Endpoint not found.")
            handler, auth_required, params = resolved
            req.params = params

            cookies = SimpleCookie(self.headers.get("Cookie") or "")
            req.token = cookies[config.SESSION_COOKIE].value if config.SESSION_COOKIE in cookies else None
            req.user = auth.user_from_token(req.token)
            if auth_required and not req.user:
                raise HttpError(401, "Authentication required.")

            resp = handler(req)
            self._send(resp)
        except HttpError as e:
            self._send(json_response({"error": e.message}, e.status))
        except auth.AuthError as e:
            self._send(json_response({"error": str(e)}, e.status))
        except Exception:
            traceback.print_exc()
            self._send(json_response({"error": "Internal server error."}, 500))

    # -- helpers ----------------------------------------------------------------------------
    def _check_origin(self, method: str):
        """CSRF defence for state-changing requests: browsers always send Origin on these."""
        if method in ("POST", "DELETE"):
            origin = self.headers.get("Origin")
            if origin:
                host = urllib.parse.urlparse(origin).netloc
                if host != self.headers.get("Host"):
                    raise HttpError(403, "Cross-origin request rejected.")

    def _read_body(self, method: str) -> bytes:
        if method not in ("POST", "PUT", "PATCH"):
            return b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Invalid Content-Length.")
        ctype = (self.headers.get("Content-Type") or "").lower()
        limit = config.MAX_UPLOAD_BYTES + 1024 * 1024 if ctype.startswith("multipart/") else JSON_BODY_LIMIT
        if length < 0:
            raise HttpError(400, "Invalid Content-Length.")
        if length > limit:
            self.close_connection = True
            raise HttpError(413, f"Request body too large (limit {limit // (1024 * 1024) or 1} MB).")
        return self.rfile.read(length) if length else b""

    def _static(self, path: str) -> Response:
        rel = "index.html" if path == "/" else path[len("/static/"):]
        # Flat directory only: reject traversal / nested paths outright.
        if not rel or rel != os.path.basename(rel) or rel.startswith("."):
            return json_response({"error": "Not found."}, 404)
        full = os.path.join(config.STATIC_DIR, rel)
        if not os.path.isfile(full):
            return json_response({"error": "Not found."}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            return Response(200, f.read(), ctype)

    def _send(self, resp: Response):
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        is_report_html = resp.content_type.startswith("text/html") and any(h[0] == "X-Report" for h in resp.headers)
        for k, v in _security_headers(is_report_html):
            self.send_header(k, v)
        for k, v in resp.headers:
            if k != "X-Report":
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
