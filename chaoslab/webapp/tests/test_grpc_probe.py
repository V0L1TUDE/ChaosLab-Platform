"""gRPC health checks for gRPC-only services: validation (server/agent parity), the agent's stdlib HTTP/2 client
against a local fake gRPC server, and the evidence the Oracle receives. No exec is involved anywhere."""

import copy
import socket
import struct
import threading
import unittest

from harness import Client  # noqa: F401  (puts the agent and webapp on sys.path)
from test_probes import FakeForward, ProbeBase, Target, fake_kubectl

import agent as agent_mod  # noqa: E402
import safe_probes  # noqa: E402
from webapp import probe_spec  # noqa: E402

GRPC = {"health": {"type": "grpc", "port": 50051}, "logs": {"expected_log_pattern": "server started"}}

BAD_GRPC = [
    ({"health": {"type": "grpc", "port": 0}}, "port"),
    ({"health": {"type": "grpc", "port": "50051"}}, "port"),
    ({"health": {"type": "grpc", "port": 50051, "path": "/x"}}, "unsupported"),
    ({"health": {"type": "grpc", "port": 50051, "command": ["sh"]}}, "unsupported"),
    ({"health": {"type": "grpc", "port": 50051, "service": "a b"}}, "service"),
    ({"health": {"type": "grpc", "port": 50051, "service": "x/../y"}}, "service"),
    ({"health": {"type": "grpc", "port": 50051, "service": "a" * 101}}, "service"),
    ({"health": {"type": "grpc", "port": 50051, "service": 5}}, "service"),
    ({"health": {"type": "exec", "port": 50051}}, "type"),
    ({"health": {"type": "tcp", "port": 50051}}, "type"),
]


class GrpcValidationTests(unittest.TestCase):
    def test_good_specs(self):
        clean, err = safe_probes.validate_probe_spec({"health": {"type": "grpc", "port": 50051, "service": ""}})
        self.assertIsNone(err)
        self.assertEqual(clean["health"], {"type": "grpc", "port": 50051})
        clean, _ = safe_probes.validate_probe_spec({"health": {"type": "grpc", "port": 7070, "service": "hipstershop.CartService"}})
        self.assertEqual(clean["health"], {"type": "grpc", "port": 7070, "service": "hipstershop.CartService"})
        clean, _ = safe_probes.validate_probe_spec({"health": {"type": "http", "port": 80, "path": "/h"}})
        self.assertEqual(clean["health"], {"port": 80, "path": "/h", "expected_status": 200})     # http stays as before

    def test_rejections(self):
        for spec, needle in BAD_GRPC:
            clean, err = safe_probes.validate_probe_spec(copy.deepcopy(spec))
            self.assertIsNone(clean, spec)
            self.assertIn(needle, err, spec)

    def test_server_and_agent_validators_agree(self):
        corpus = [s for s, _ in BAD_GRPC] + [GRPC, {"health": {"type": "grpc", "port": 1, "service": "a.B_c"}},
                                             {"health": {"type": "http", "port": 80, "path": "/"}}]
        for spec in corpus:
            self.assertEqual(safe_probes.validate_probe_spec(copy.deepcopy(spec)), probe_spec.validate(copy.deepcopy(spec)), spec)

    def test_engine_block_and_agent_job(self):
        app = probe_spec.to_engine_application(probe_spec.validate(GRPC)[0])
        self.assertEqual(app["health_probe"], {"type": "grpc", "port": 50051})
        cfg = {"name": "chaoslab-exp-1-pay-pod-kill", "target": {"namespace": "shop", "label": {"app": "pay"}},
               "fault": {"type": "pod-kill", "mode": "one"}, "recovery": {"timeout_seconds": 30, "expected_ready_replicas": 1},
               "application": app}
        clean, err = agent_mod.validate_job({"job_id": 1, "job_token": "clj_x", "engine_config": cfg,
                                             "max_runtime_seconds": 300}, agent_mod.Policy(["shop"]))
        self.assertIsNone(err)
        self.assertEqual(clean["application"], {"enabled": False})      # the engine's exec-based gRPC prober stays off
        self.assertEqual(clean["probes"]["health"], {"type": "grpc", "port": 50051})


# --------------------------------------------------------------------------- a fake gRPC server (cleartext HTTP/2)
def frame(ftype, flags, stream, payload=b""):
    return struct.pack(">I", len(payload))[1:] + bytes([ftype, flags]) + struct.pack(">I", stream) + payload


class FakeGrpc:
    """Accepts one connection per call and answers the health check according to `mode`."""
    mode = "serving"
    requests = []

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            conn.settimeout(3)
            data = b""
            try:
                while not self._request_done(data):
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
            except OSError:
                return
            FakeGrpc.requests.append(data)
            mode = FakeGrpc.mode
            if mode == "http":                                  # a plain HTTP/1.1 server on that port
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if mode == "silent":
                try:
                    conn.recv(10)
                except OSError:
                    pass
                return
            out = frame(0x4, 0, 0) + frame(0x4, 0x1, 0)          # SETTINGS, SETTINGS ACK
            if mode == "ping":
                out += frame(0x6, 0, 0, b"12345678")
            out += frame(0x1, 0x4, 1, b"\x88")                    # HEADERS :status 200 (indexed)
            if mode == "unimplemented":
                out = out[:-len(frame(0x1, 0x4, 1, b"\x88"))] + frame(0x1, 0x5, 1, b"\x88")   # trailers-only
            else:
                status = {"serving": 1, "ping": 1, "not_serving": 2, "split": 1}[mode]
                msg = b"\x08" + bytes([status])
                grpc_msg = b"\x00" + struct.pack(">I", len(msg)) + msg
                if mode == "split":                               # the message arrives in two DATA frames
                    out += frame(0x0, 0, 1, grpc_msg[:3]) + frame(0x0, 0, 1, grpc_msg[3:])
                else:
                    out += frame(0x0, 0, 1, grpc_msg)
                out += frame(0x1, 0x5, 1, b"\x00\x0bgrpc-status\x010")
            conn.sendall(out)
            if mode == "ping":                                    # the client must answer our PING
                reply = b""
                try:
                    while True:                                   # until the client closes the connection
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        reply += chunk
                except OSError:
                    pass
                FakeGrpc.ping_reply = reply

    @staticmethod
    def _request_done(data):
        # preface (24) then frames: done once a DATA frame with END_STREAM on stream 1 has arrived
        if len(data) < 24:
            return False
        if not data.startswith(b"PRI * HTTP/2.0"):
            return b"\r\n\r\n" in data                         # not HTTP/2 at all: answer once headers are in
        i = 24
        while i + 9 <= len(data):
            length, ftype, flags = int.from_bytes(data[i:i + 3], "big"), data[i + 3], data[i + 4]
            if i + 9 + length > len(data):
                return False
            if ftype == 0 and flags & 0x1:
                return True
            i += 9 + length
        return False


class GrpcForward(FakeForward):
    def __enter__(self):
        return GrpcExecutionTests.server.port


class GrpcExecutionTests(ProbeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = FakeGrpc()

    def setUp(self):
        super().setUp()
        FakeGrpc.mode, FakeGrpc.requests = "serving", []

    def check(self, service=""):
        return safe_probes.grpc_health_check(self.server.port, service, timeout=3)

    def test_serving(self):
        self.assertEqual(self.check()["serving_status"], "SERVING")
        req = FakeGrpc.requests[-1]
        self.assertTrue(req.startswith(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"))
        self.assertIn(b"/grpc.health.v1.Health/Check", req)
        self.assertNotIn(b"GET ", req)

    def test_service_name_is_sent(self):
        self.check("hipstershop.PaymentService")
        self.assertIn(b"\x0a\x1ahipstershop.PaymentService", FakeGrpc.requests[-1])

    def test_not_serving_and_split_messages(self):
        FakeGrpc.mode = "not_serving"
        self.assertEqual(self.check()["serving_status"], "NOT_SERVING")
        FakeGrpc.mode = "split"
        self.assertEqual(self.check()["serving_status"], "SERVING")

    def test_ping_is_answered(self):
        FakeGrpc.mode, FakeGrpc.ping_reply = "ping", None
        self.assertEqual(self.check()["serving_status"], "SERVING")
        for _ in range(50):
            if FakeGrpc.ping_reply is not None:
                break
            threading.Event().wait(0.05)
        self.assertIn(frame(0x6, 0x1, 0, b"12345678"), FakeGrpc.ping_reply)     # PING ACK with the same payload

    def test_failures_are_errors_not_exceptions(self):
        FakeGrpc.mode = "unimplemented"
        self.assertIn("grpc.health.v1", self.check()["error"])
        FakeGrpc.mode = "http"
        self.assertIn("error", self.check())
        FakeGrpc.mode = "silent"
        r = safe_probes.grpc_health_check(self.server.port, "", timeout=0.5)
        self.assertIn("no gRPC response", r["error"])
        closed = socket.socket(); closed.bind(("127.0.0.1", 0)); port = closed.getsockname()[1]; closed.close()
        self.assertIn("error", safe_probes.grpc_health_check(port, "", timeout=1))

    def test_run_probes_evidence(self):
        ev = safe_probes.run_probes(GRPC, "shop", "pay", fake_kubectl(logs="server started on 50051\n"),
                                    forward_factory=GrpcForward)
        self.assertEqual(ev["probe_type"], "grpc")
        self.assertTrue(ev["health_check_passed"])
        self.assertEqual(ev["health_status"], "SERVING")
        self.assertTrue(ev["functional_test_skipped"] and ev["startup_log_detected"])
        self.assertEqual(GrpcForward.calls[-1], ("shop", "web-new", 50051))
        self.assertFalse(Target.saw_post)
        FakeGrpc.mode = "not_serving"
        ev = safe_probes.run_probes(GRPC, "shop", "pay", fake_kubectl(), forward_factory=GrpcForward)
        self.assertFalse(ev["health_check_passed"])
        self.assertEqual(ev["health_status"], "NOT_SERVING")


if __name__ == "__main__":
    unittest.main()
