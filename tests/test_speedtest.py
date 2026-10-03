import http.client
import http.server
import shutil
import ssl
import subprocess
import threading
import time

import pytest

from callglance import speedtest
from callglance.speedtest import SpeedTest, call_capacity, grade_for


def test_grades_follow_waveform_bands():
    assert grade_for(None) is None
    assert grade_for(3) == "A+" and grade_for(29) == "A" and grade_for(45) == "B"
    assert grade_for(150) == "C" and grade_for(300) == "D" and grade_for(450) == "F"


def test_call_capacity():
    assert call_capacity(300, 40) == "hd"
    assert call_capacity(20, 2) == "video"
    assert call_capacity(5, 0.5) == "audio"
    assert call_capacity(0.05, 0.05) == "none"
    assert call_capacity(None, 3) == "unknown"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    chunk = b"\0" * 65536

    def log_message(self, *args):
        pass

    def do_GET(self):
        size = int(self.path.split("bytes=")[1])
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        while size > 0:
            n = min(size, len(self.chunk))
            self.wfile.write(self.chunk[:n])
            size -= n

    def do_POST(self):
        remaining = int(self.headers["Content-Length"])
        while remaining > 0:
            remaining -= len(self.rfile.read(min(remaining, 65536)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def https_server(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], cert
    server.shutdown()


def test_full_run_against_a_local_server(https_server, monkeypatch):
    port, cert = https_server
    client_ctx = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(speedtest, "_connect", lambda: http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=5, context=client_ctx))
    monkeypatch.setattr(speedtest, "PHASE_SECONDS", 1.2)
    monkeypatch.setattr(speedtest, "RAMP_SECONDS", 0.25)
    latencies = iter([12.0, 60.0, 90.0])
    states = []
    done = threading.Event()

    def update(state):
        states.append(state)
        if state.status in ("done", "error", "cancelled"):
            done.set()

    test = SpeedTest(lambda seconds: next(latencies), update)
    assert test.start()
    assert not test.start()  # one at a time
    assert done.wait(20)
    final = states[-1]
    assert final.status == "done", final.error
    assert final.download_mbps > 1 and final.upload_mbps > 1
    assert final.idle_latency_ms == 12.0 and final.bufferbloat_ms == 78.0 and final.grade == "C"
    assert final.capacity == "hd" and final.summary == "Enough for HD video calls"
    assert any("bufferbloat" in tip for tip in final.tips)
    assert any(s.phase == "download" for s in states) and any(s.phase == "upload" for s in states)


def test_errors_are_reported(monkeypatch):
    def refuse():
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(speedtest, "_connect", refuse)
    monkeypatch.setattr(speedtest, "PHASE_SECONDS", 0.5)
    done = threading.Event()
    states = []
    test = SpeedTest(lambda s: 10.0, lambda st: (states.append(st), st.status != "running"
                                                 and done.set()))
    test.start()
    assert done.wait(10)
    assert states[-1].status == "error" and "Connection refused" in states[-1].error


def test_cancel(https_server, monkeypatch):
    port, cert = https_server
    ctx = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(speedtest, "_connect", lambda: http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=5, context=ctx))
    done = threading.Event()
    states = []
    test = SpeedTest(lambda s: 10.0, lambda st: (states.append(st), st.status != "running"
                                                 and done.set()))
    test.start()
    time.sleep(0.6)
    test.cancel()
    assert done.wait(10)
    assert states[-1].status == "cancelled"
