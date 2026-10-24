import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import launch


def test_find_port_skips_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        assert launch.find_port(port, attempts=5) != port


def _health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/_stcore/health" else 404)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_wait_until_ready_true_when_healthy():
    server = _health_server()
    try:
        assert launch.wait_until_ready(server.server_address[1], timeout=5)
    finally:
        server.shutdown()


def test_wait_until_ready_false_on_timeout():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not launch.wait_until_ready(port, timeout=1)


def test_wait_until_ready_gives_up_when_process_exited():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert not launch.wait_until_ready(1, timeout=30, process=process)
