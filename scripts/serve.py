#!/usr/bin/env python3
"""
Local launcher for the SPY Entry Risk Dashboard.

Serves the dashboard over http://127.0.0.1:8765 and forwards data requests
to Stooq and FRED from Python, where the browser CORS rules do not apply.
Standard library only. Nothing leaves this machine except the two data calls.

    python3 serve.py          (macOS, Linux)
    py serve.py               (Windows)

Stop with Ctrl-C.
"""

import http.server
import os
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
PAGE = "spy_entry_risk_dashboard.html"
ALLOW = ("stooq.com", "stooq.pl", "fred.stlouisfed.org")
HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        if parts.path == "/fetch":
            return self.relay(urllib.parse.parse_qs(parts.query).get("u", [""])[0])
        if parts.path == "/":
            self.path = "/" + PAGE
        return super().do_GET()

    def relay(self, target):
        host = (urllib.parse.urlparse(target).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in ALLOW):
            self.send_error(403, "host not on the allow list")
            return
        try:
            req = urllib.request.Request(target, headers={
                "User-Agent": "Mozilla/5.0 (compatible; spy-entry-risk/2)",
                "Accept": "text/csv,text/plain,*/*",
            })
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read()
        except Exception as exc:
            self.send_error(502, "upstream: %s" % str(exc)[:180])
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not os.path.exists(os.path.join(HERE, PAGE)):
        print("Put serve.py in the same folder as " + PAGE)
        sys.exit(1)
    url = "http://127.0.0.1:%d/" % PORT
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        print("Cannot bind port %d: %s" % (PORT, exc))
        sys.exit(1)
    print("Dashboard: " + url)
    print("Data relay: /fetch, limited to " + ", ".join(ALLOW))
    print("Ctrl-C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
