"""Synthetic HTTP roles for Docker-only gateway regression; no real credentials."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        role = os.environ["BACKEND_ROLE"]
        path = urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        is_import = path.startswith("/api/account-import-jobs") or path == "/account-import.html"
        if role == "app" and self.path == "/v1/images/generations?stream=1":
            first, last = b"data: start\n\n", b"data: done\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(first) + len(last)))
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            time.sleep(3)
            self.wfile.write(last)
            return
        if path == "/version":
            status = 200
        elif is_import != (role == "importer"):
            status = 404
        elif path == "/api/dashboard":
            status = 401
        else:
            status = 200
        response = json.dumps({
            "role": role, "instance": os.environ["BACKEND_INSTANCE"],
            "path": self.path, "method": self.command, "body": body.decode(),
            "host": self.headers.get("Host"),
        }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
