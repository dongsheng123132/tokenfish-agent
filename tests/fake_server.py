"""A minimal local fake HTTP server standing in for the TokenFish backend
(pay-server + new-api) during tests. Never talks to any real API.

Usage:
    server = FakeBackend()
    server.start()
    server.set_response("GET", "/api/usage/token/", 200, {...})
    ...
    server.stop()

Failure injection (for the "never double-charge" tests):
    server.set_drop(method, path)              # read the request, close the connection WITHOUT answering
    server.set_raw(method, path, status, raw)  # answer with raw bytes (e.g. non-JSON 2xx body)
    server.set_slow(method, path, delay, status, body)  # answer only after `delay` seconds
    server.count(method, path)                 # how many requests of that kind arrived
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeTokenFishBackend/0.1"

    def log_message(self, format, *args):  # noqa: A002 - silence test logs
        pass

    def _dispatch(self, method: str) -> None:
        backend: FakeBackend = self.server.backend  # type: ignore[attr-defined]
        path = self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body_raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(body_raw.decode("utf-8")) if body_raw else None
        except json.JSONDecodeError:
            body = None

        backend.record_request(method, path, dict(self.headers), body)

        match = backend.match(method, path)
        if match is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"type": "not_found"}}).encode("utf-8"))
            return

        status, response_body, handler_fn = match
        if handler_fn is not None:
            status, response_body = handler_fn(method, path, dict(self.headers), body)

        if status is None:
            # Simulate a lost response: the request was received (and recorded),
            # but the connection is closed without any answer.
            self.close_connection = True
            return

        if isinstance(response_body, (bytes, bytearray)):
            payload = bytes(response_body)
        else:
            payload = json.dumps(response_body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client already gave up (timeout tests)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


class FakeBackend:
    def __init__(self):
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._routes: dict[tuple[str, str], tuple[int, dict, Optional[Callable]]] = {}
        self.requests: list[dict] = []

    def start(self) -> str:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.backend = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_response(self, method: str, path: str, status: int, body: dict) -> None:
        self._routes[(method.upper(), path)] = (status, body, None)

    def set_handler(self, method: str, path: str, handler_fn: Callable) -> None:
        self._routes[(method.upper(), path)] = (0, {}, handler_fn)

    def set_drop(self, method: str, path: str) -> None:
        self.set_handler(method, path, lambda *_a: (None, None))

    def set_raw(self, method: str, path: str, status: int, raw: bytes) -> None:
        self._routes[(method.upper(), path)] = (status, raw, None)

    def set_slow(self, method: str, path: str, delay: float, status: int, body: dict) -> None:
        def slow(*_a):
            time.sleep(delay)
            return status, body

        self.set_handler(method, path, slow)

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r["method"] == method.upper() and r["path"] == path)

    def match(self, method: str, path: str):
        return self._routes.get((method.upper(), path))

    def record_request(self, method: str, path: str, headers: dict, body) -> None:
        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})
