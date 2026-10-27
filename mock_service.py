#!/usr/bin/env python3
"""Mock web service untuk demo BPMN: inventory, payment, shipment, notification."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        if self.path == "/calls":
            return self._send(200, {"calls": CALLS})
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        CALLS.append({"path": self.path, "payload": payload})
        self._send(200, {"ok": True, "service": "mock-api", "path": self.path, "received": payload})

    do_GET = do_POST = _handle

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
