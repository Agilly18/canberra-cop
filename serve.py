#!/usr/bin/env python3
"""Canberra COP PoC server.

Serves the static page and relays two feeds a browser can't reach directly:
- /esa — ACT ESA incidents (upstream sends no CORS headers)
- /tomtom/{z}/{x}/{y}.png — TomTom traffic-flow tiles (keeps the API key
  out of the page source; key lives in the gitignored .env file)
- /firms — NASA FIRMS fire hotspots near Canberra as GeoJSON (CSV upstream,
  key also in .env)
- /rfs — NSW RFS major incidents (GeoJSON upstream, no CORS headers)
- /news — Canberra headlines (RiotACT + Canberra Times RSS merged to JSON)
Run:  python3 serve.py  →  http://localhost:8899
"""
import csv
import io
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
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


_env = load_env()
TOMTOM_KEY = _env.get("TOMTOM_API_KEY", "")
FIRMS_KEY = _env.get("FIRMS_MAP_KEY", "")

# west,south,east,north box around the ACT and surrounds
FIRMS_BBOX = "148.2,-36.2,150.0,-34.4"
FIRMS_SENSORS = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT")
FIRMS_DAYS = 2
FIRMS_CACHE_SECONDS = 600  # satellites only pass a few times a day

_firms_cache = {"time": 0.0, "body": b""}


def firms_body():
    if time.time() - _firms_cache["time"] > FIRMS_CACHE_SECONDS:
        feats = []
        for sensor in FIRMS_SENSORS:
            url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                   f"{FIRMS_KEY}/{sensor}/{FIRMS_BBOX}/{FIRMS_DAYS}")
            with urllib.request.urlopen(url, timeout=15) as r:
                text = r.read().decode()
            for row in csv.DictReader(io.StringIO(text)):
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates":
                                 [float(row["longitude"]), float(row["latitude"])]},
                    "properties": {
                        "date": row["acq_date"],
                        "time": row["acq_time"].zfill(4),
                        "satellite": row["satellite"],
                        "frp": float(row["frp"] or 0),
                        "daynight": row["daynight"],
                        "confidence": row["confidence"],
                    },
                })
        body = json.dumps({"type": "FeatureCollection",
                           "features": feats}).encode()
        _firms_cache.update(time=time.time(), body=body)
    return _firms_cache["body"]


def esa_body():
    if time.time() - _cache["time"] > CACHE_SECONDS:
        req = urllib.request.Request(ESA_FEED, headers={"User-Agent": "canberra-cop-poc"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        json.loads(body)  # refuse to cache junk
        _cache.update(time=time.time(), body=body)
    return _cache["body"]


NEWS_FEEDS = (
    ("RiotACT", "https://the-riotact.com/feed"),
    ("Canberra Times", "https://www.canberratimes.com.au/rss.xml"),
)
NEWS_CACHE_SECONDS = 600
_news_cache = {"time": 0.0, "body": b"[]"}


def news_body():
    if time.time() - _news_cache["time"] > NEWS_CACHE_SECONDS:
        items = []
        for source, url in NEWS_FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "canberra-cop-poc"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    root = ET.fromstring(r.read())
                for it in root.findall(".//item"):
                    try:
                        ts = parsedate_to_datetime(it.findtext("pubDate", "")).timestamp()
                    except Exception:
                        ts = 0
                    items.append({"source": source, "ts": ts,
                                  "title": (it.findtext("title") or "").strip(),
                                  "link": (it.findtext("link") or "").strip()})
            except Exception:
                pass  # one dead feed shouldn't kill the panel
        items.sort(key=lambda i: i["ts"], reverse=True)
        _news_cache.update(time=time.time(),
                           body=json.dumps(items[:12]).encode())
    return _news_cache["body"]


RFS_FEED = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
_rfs_cache = {"time": 0.0, "body": b""}


def rfs_body():
    # RFS asks consumers to poll no more often than every 60 s
    if time.time() - _rfs_cache["time"] > 60:
        req = urllib.request.Request(RFS_FEED, headers={"User-Agent": "canberra-cop-poc"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        json.loads(body)  # refuse to cache junk
        _rfs_cache.update(time=time.time(), body=body)
    return _rfs_cache["body"]


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
        if self.path.rstrip("/") == "/news":
            try:
                body = news_body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(502, "news feeds unreachable")
            return
        if self.path.rstrip("/") == "/rfs":
            try:
                body = rfs_body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(502, "rfs unreachable")
            return
        if self.path.rstrip("/") == "/firms":
            if not FIRMS_KEY:
                self.send_error(503, "no FIRMS_MAP_KEY in .env")
                return
            try:
                body = firms_body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(502, "firms unreachable")
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
