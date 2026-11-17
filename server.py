"""AutoClipper app backend: settings, channel management, clip library, local HTTP API for the UI."""
from __future__ import annotations

import glob
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from types import SimpleNamespace

import autoclipper as ac

PRESETS = {
    "low": dict(ratio=4.5, min_users=6, min_score=20, min_hype_share=0.35),     # fewer, bigger moments
    "medium": dict(ratio=3.0, min_users=4, min_score=12, min_hype_share=0.25),
    "high": dict(ratio=2.2, min_users=3, min_score=8, min_hype_share=0.15),     # more clips
}
DEFAULTS = dict(out="", format="vertical", subs=True, model="small", pre=30, post=12, sensitivity="medium",
                cooldown=60, quality="720p60,720p,best", encoder="libx264", hype_words="", buffer=300,
                window=8, baseline=120, sigma=3.0, hype_weight=2.0, poll=60)
LIMITS = dict(pre=(10, 90), post=(4, 45), cooldown=(15, 600), buffer=(120, 1800), window=(4, 20),
              baseline=(60, 600), sigma=(1, 8), hype_weight=(0, 6), poll=(15, 600))
CHOICES = dict(format=("vertical", "horizontal"), sensitivity=tuple(PRESETS),
               model=("tiny", "base", "small", "medium"),
               encoder=("libx264", "h264_nvenc", "h264_videotoolbox", "h264_qsv", "h264_amf"))
SAFE_NAME = re.compile(r"^[\w@.-]+$")


def default_out():
    return os.path.join(os.path.expanduser("~"), "AutoClipper", "clips")


class Engine:
    def __init__(self, base=None):
        self.dir = base or ac.data_dir()
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "config.json")
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        self.channels = []  # [{"url":..., "enabled":bool}]
        self.workers = {}
        self.cfg = SimpleNamespace()
        self._load()
        self.apply()

    # -- persistence
    def _load(self):
        try:
            with open(self.path, encoding="utf8") as f:
                d = json.load(f)
            self.settings.update({k: v for k, v in d.get("settings", {}).items() if k in DEFAULTS})
            self.channels = [c for c in d.get("channels", []) if "url" in c]
        except (OSError, ValueError):
            pass

    def save(self):
        with open(self.path, "w", encoding="utf8") as f:
            json.dump(dict(settings=self.settings, channels=self.channels), f, indent=2)

    def apply(self):
        s, c = self.settings, self.cfg
        for k in ("format", "model", "pre", "post", "cooldown", "quality", "encoder", "buffer", "window",
                  "baseline", "sigma", "hype_weight", "poll"):
            setattr(c, k, s[k])
        for k, v in PRESETS.get(s["sensitivity"], PRESETS["medium"]).items():
            setattr(c, k, v)
        c.no_subs = not s["subs"]
        c.out = s["out"] or default_out()
        c.work = os.path.join(self.dir, "work")
        os.makedirs(c.work, exist_ok=True)
        ac.set_extra_hype(s["hype_words"])

    def set_settings(self, new):
        with self.lock:
            old_model, old_subs = self.settings["model"], self.settings["subs"]
            for k, v in new.items():
                if k not in DEFAULTS:
                    continue
                d = DEFAULTS[k]
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, (int, float)):
                    v = type(d)(float(v))
                    lo, hi = LIMITS.get(k, (None, None))
                    if lo is not None:
                        v = min(max(v, lo), hi)
                else:
                    v = str(v).strip()
                    if k in CHOICES and v not in CHOICES[k]:
                        continue
                self.settings[k] = v
            self.save()
            self.apply()
            if self.settings["subs"] and (not old_subs or old_model != self.settings["model"]):
                ac.preload_model(self.cfg)

    # -- channels
    def start_all(self):
        with self.lock:
            for c in self.channels:
                if c.get("enabled", True):
                    self._start(c["url"])
        if self.settings["subs"]:
            ac.preload_model(self.cfg)

    def _start(self, url):
        old = self.workers.get(url)
        if old and old.is_alive():
            return
        w = ac.Worker(self.cfg, url)
        self.workers[url] = w
        w.start()

    def _stop(self, url):
        w = self.workers.pop(url, None)
        if w:
            w.stop.set()

    def add_channel(self, url):
        url = ac.normalize_url(url)
        ac.parse_target(url)  # validates, raises ValueError
        with self.lock:
            if any(c["url"] == url for c in self.channels):
                raise ValueError("That channel is already in the list")
            self.channels.append(dict(url=url, enabled=True))
            self.save()
            self._start(url)

    def toggle_channel(self, url, enabled):
        with self.lock:
            for c in self.channels:
                if c["url"] == url:
                    c["enabled"] = bool(enabled)
            self.save()
            self._start(url) if enabled else self._stop(url)

    def remove_channel(self, url):
        with self.lock:
            self.channels = [c for c in self.channels if c["url"] != url]
            self.save()
            self._stop(url)

    def stop_all(self):
        for url in list(self.workers):
            self._stop(url)

    def state(self):
        chans = []
        with self.lock:
            for c in self.channels:
                platform, slug, name = ac.parse_target(c["url"])
                w = self.workers.get(c["url"])
                item = dict(url=c["url"], enabled=c.get("enabled", True), name=name, platform=platform)
                item.update(w.snapshot() if w else dict(status="stopped", live=False, score=[], hype=[],
                                                        marks=[], msgs_min=0, clip_count=0))
                chans.append(item)
        return dict(channels=chans, settings=self.settings, out=self.cfg.out, model=ac.MODEL,
                    ffmpeg=not ac.FFMPEG_NOTE, ffmpeg_note=ac.FFMPEG_NOTE)

    # -- clips
    def list_clips(self):
        items = []
        for p in glob.glob(os.path.join(self.cfg.out, "*.json")):
            name = os.path.basename(p)[:-5]
            mp4 = os.path.join(self.cfg.out, name + ".mp4")
            if not os.path.exists(mp4):
                continue
            try:
                with open(p, encoding="utf8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            m["name"] = name
            m["size_mb"] = round(os.path.getsize(mp4) / 1e6, 1)
            m.setdefault("detected_ts", os.path.getmtime(mp4))
            items.append(m)
        items.sort(key=lambda m: m["detected_ts"], reverse=True)
        return items

    def _clip_path(self, name, ext):
        if not SAFE_NAME.match(name):
            raise ValueError("bad clip name")
        return os.path.join(self.cfg.out, name + ext)

    def mark_clip(self, name, uploaded):
        p = self._clip_path(name, ".json")
        with open(p, encoding="utf8") as f:
            m = json.load(f)
        m["uploaded"] = bool(uploaded)
        with open(p, "w", encoding="utf8") as f:
            json.dump(m, f, ensure_ascii=False)

    def delete_clip(self, name):
        for ext in (".mp4", ".json", ".png"):
            try:
                os.remove(self._clip_path(name, ext))
            except OSError:
                pass

    def reveal(self, name=None):
        os.makedirs(self.cfg.out, exist_ok=True)
        if name:
            path = self._clip_path(name, ".mp4")
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", self.cfg.out])
        elif os.name == "nt":
            os.startfile(self.cfg.out)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.cfg.out])
        else:
            subprocess.Popen(["xdg-open", self.cfg.out])

    def logs(self, since):
        lines = [(n, l) for n, l in list(ac.LOGS) if n > since]
        return dict(seq=lines[-1][0] if lines else since, lines=[l for _, l in lines])


# --------------------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    engine: Engine = None
    protocol_version = "HTTP/1.1"
    quit_hook = None

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _host_ok(self):  # blocks DNS-rebinding style access
        return (self.headers.get("Host") or "").split(":")[0].lower() in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"{}")
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        e = self.engine
        try:
            if p in ("/", "/index.html"):
                with open(os.path.join(ac.resource_dir(), "ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif p == "/api/ping":
                self._json(dict(app="autoclipper"))
            elif p == "/api/state":
                self._json(e.state())
            elif p == "/api/clips":
                self._json(e.list_clips())
            elif p == "/api/logs":
                self._json(e.logs(int(q.get("since", ["0"])[0])))
            elif p.startswith("/clips/"):
                self.serve_clip(urllib.parse.unquote(p[7:]))
            else:
                self._json(dict(error="not found"), 404)
        except Exception as ex:
            self._json(dict(error=str(ex)), 500)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok() or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, b"{}")
        e = self.engine
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            p = self.path
            if p == "/api/channels":
                e.add_channel(d["url"])
            elif p == "/api/channels/toggle":
                e.toggle_channel(d["url"], d["enabled"])
            elif p == "/api/channels/remove":
                e.remove_channel(d["url"])
            elif p == "/api/settings":
                e.set_settings(d)
            elif p == "/api/clips/mark":
                e.mark_clip(d["name"], d["uploaded"])
            elif p == "/api/clips/delete":
                e.delete_clip(d["name"])
            elif p == "/api/reveal":
                e.reveal(d.get("name"))
            elif p == "/api/quit":
                self._json(dict(ok=True))
                if Handler.quit_hook:
                    threading.Timer(0.4, Handler.quit_hook).start()
                return
            else:
                return self._json(dict(error="not found"), 404)
            self._json(dict(ok=True))
        except (ValueError, KeyError) as ex:
            self._json(dict(error=str(ex)), 400)
        except Exception as ex:
            self._json(dict(error=str(ex)), 500)

    def serve_clip(self, fname):
        stem, ext = os.path.splitext(fname)
        if ext not in (".mp4", ".png"):
            return self._json(dict(error="not found"), 404)
        path = self.engine._clip_path(stem, ext)
        if not os.path.exists(path):
            return self._json(dict(error="not found"), 404)
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4" if ext == ".mp4" else "image/png")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def serve(engine, port=8765, quit_hook=None):
    Handler.engine, Handler.quit_hook = engine, quit_hook
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
