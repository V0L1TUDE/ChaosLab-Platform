"""
Test harness: boots the real server in-process on an ephemeral port with an isolated
temporary data directory. Env overrides MUST be set before `webapp.config` is imported.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import uuid

_TMP = tempfile.mkdtemp(prefix="chaoslab-test-")
os.environ["CHAOSLAB_DATA_DIR"] = _TMP
os.environ["CHAOSLAB_SIM_SPEED"] = "0.01"
os.environ["CHAOSLAB_LIVE_ENABLED"] = "false"   # tests never touch a real cluster
os.environ["CHAOSLAB_MAX_UPLOAD_MB"] = "1"

CHAOSLAB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if CHAOSLAB_DIR not in sys.path:
    sys.path.insert(0, CHAOSLAB_DIR)

from webapp import db, server  # noqa: E402
from webapp.http_core import create_server  # noqa: E402

_httpd = None
PORT = None


def start():
    global _httpd, PORT
    if _httpd:
        return PORT
    db.init_db()
    server.load_routes()
    server.run_startup_hooks()
    _httpd = create_server("127.0.0.1", 0)
    PORT = _httpd.server_address[1]
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    return PORT


class Client:
    """Tiny cookie-aware HTTP client."""

    def __init__(self):
        start()
        self.cookie = None

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        hdrs = dict(headers or {})
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie":
                pair = v.split(";")[0]
                self.cookie = pair if not pair.endswith("=") else None
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        parsed = json.loads(payload) if "json" in ctype and payload else payload
        return resp.status, parsed, resp

    def get(self, path): return self.request("GET", path)
    def post(self, path, body=None): return self.request("POST", path, body if body is not None else {})
    def delete(self, path): return self.request("DELETE", path)

    def upload(self, path, fields, filename, content, content_type="application/octet-stream"):
        boundary = "----chaoslab" + uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        if filename is not None:
            parts.append(
                (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: {content_type}\r\n\r\n').encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, raw=b"".join(parts),
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def register(self, name="Test User", email=None, password="correct-horse-1"):
        email = email or f"u{uuid.uuid4().hex[:8]}@example.com"
        status, data, _ = self.post("/api/auth/register", {"name": name, "email": email, "password": password})
        assert status == 201, (status, data)
        return data["user"], email, password
