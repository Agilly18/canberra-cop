#!/usr/bin/env python3
"""Canberra COP PoC server.

Serves the static page and relays two feeds a browser can't reach directly:
- /esa — ACT ESA incidents (upstream sends no CORS headers)
- /tomtom/{z}/{x}/{y}.png — TomTom traffic-flow tiles (keeps the API key
  out of the page source; key lives in the gitignored .env file)
Run:  python3 serve.py  →  http://localhost:8899
"""
import json
import os
import re
import time
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler

ESA_FEED = "https://esa.act.gov.au/feeds/allincidents.json"
CACHE_SECONDS = 60  # ESA updates every 60 s; don't hammer them harder
TILE_CACHE_SECONDS = 120  # traffic tiles: save free-tier quota on map pans

_cache = {"time": 0.0, "body": b"[]"}
_tile_cache = {}


def load_env():
    env = {}
    try:
        with open(os.path.join(os.path.dirname(__file__), ".env")) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


TOMTOM_KEY = load_env().get("TOMTOM_API_KEY", "")


def esa_body():
    if time.time() - _cache["time"] > CACHE_SECONDS:
        req = urllib.request.Request(ESA_FEED, headers={"User-Agent": "canberra-cop-poc"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        json.loads(body)  # refuse to cache junk
        _cache.update(time=time.time(), body=body)
    return _cache["body"]


def tomtom_tile(z, x, y):
    key = f"{z}/{x}/{y}"
    hit = _tile_cache.get(key)
    if hit and time.time() - hit[0] < TILE_CACHE_SECONDS:
        return hit[1]
    url = (f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0/"
           f"{z}/{x}/{y}.png?key={TOMTOM_KEY}")
    with urllib.request.urlopen(url, timeout=10) as r:
        body = r.read()
    _tile_cache[key] = (time.time(), body)
    return body


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        m = re.fullmatch(r"/tomtom/(\d+)/(\d+)/(\d+)\.png", self.path)
        if m:
            if not TOMTOM_KEY:
                self.send_error(503, "no TOMTOM_API_KEY in .env")
                return
            try:
                body = tomtom_tile(*m.groups())
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(502, "tomtom unreachable")
            return
        if self.path.rstrip("/") == "/esa":
            try:
                body = esa_body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            except Exception:
                body = b'{"error": "esa feed unreachable"}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass  # keep the terminal quiet


if __name__ == "__main__":
    print("Canberra COP PoC → http://localhost:8899/poc.html  (Ctrl-C to stop)")
    HTTPServer(("0.0.0.0", 8899), Handler).serve_forever()
