"""
AutoClipper core engine.

Per channel: quick live check -> record rolling buffer (indexed to the exact second) -> read chat ->
detect hype spike / entertainment -> cut [spike-30s, spike+12s] -> Whisper subtitles (bundled fonts,
word highlight) -> MP4 + thumbnail + metadata JSON.
Everything (streamlink, yt-dlp, ffmpeg) is used in-process / via bundled binaries,
so a frozen (PyInstaller) build needs nothing installed on the user's machine.
"""
from __future__ import annotations

import array
import collections
import contextlib
import html as _html
import json
import math
import os
import random
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

APP = "AutoClipper"
SEG = 4  # seconds per rolling segment
FROZEN = getattr(sys, "frozen", False)


# --------------------------------------------------------------------------- paths / process helpers


def resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def popen_kw():
    # no flashing console windows when running as a GUI app on Windows
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def _ffmpeg_candidates():
    out = []
    env = os.environ.get("AUTOCLIPPER_FFMPEG")
    if env:
        out.append(env)
    try:
        import imageio_ffmpeg  # ships a static ffmpeg binary inside the wheel

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            if os.name != "nt":
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
            out.append(p)
    except Exception:
        pass
    w = shutil.which("ffmpeg")
    if w:
        out.append(w)
    return out


FFMPEG = "ffmpeg"  # resolved by find_ffmpeg() below
FFMPEG_NOTE = ""

LOGS = collections.deque(maxlen=800)
_seq = 0


def log(tag, msg):
    global _seq
    line = f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}"
    _seq += 1
    LOGS.append((_seq, line))
    try:
        print(line, flush=True)
    except Exception:
        pass


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", **popen_kw())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-800:])
    return r.stdout


def _ffmpeg_ok(path):
    """Self-test: encode a tiny MPEG-TS and decode it again (this is exactly what recording needs)."""
    d = tempfile.mkdtemp(prefix="ffchk_")
    try:
        ts = os.path.join(d, "t.ts")
        kw = dict(capture_output=True, timeout=40, **popen_kw())
        a = subprocess.run([path, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=160x120:r=10:d=1",
                            "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", ts], **kw)
        b = subprocess.run([path, "-loglevel", "error", "-i", ts, "-f", "null", "-"], **kw)
        return a.returncode == 0 and b.returncode == 0
    except Exception:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def find_ffmpeg():
    global FFMPEG_NOTE
    cands = _ffmpeg_candidates()
    for p in cands:
        if _ffmpeg_ok(p):
            return p
        log("ffmpeg", f"self-test failed for {p}; trying next candidate")
    FFMPEG_NOTE = "No working ffmpeg found - install ffmpeg or set AUTOCLIPPER_FFMPEG"
    log("ffmpeg", FFMPEG_NOTE)
    return cands[0] if cands else "ffmpeg"


FFMPEG = find_ffmpeg()


# --------------------------------------------------------------------------- hype detection

BASE_HYPE = frozenset({
    "xd", "xdd", "xddd", "lul", "lulw", "kekw", "kek", "keks", "omegalul", "lmao", "lmfao", "lol",
    "icant", "pog", "poggers", "pogchamp", "pogu", "omg", "wtf", "clip", "clipit", "monkas",
    "ahahaha", "hahaha", "kekl", "wutface", "lmaooo", "w", "rofl",
})
HYPE_WORDS = set(BASE_HYPE)
HYPE_RE = re.compile(r"^(?:x+d+|l+o+l+|l+m+f?a+o+|k+e+k+w*|(?:a?ha){2,}h?|o+m+g+|w+t+f+|p+o+g+(?:ge?rs|u|champ)?)$")
EMOJI_RE = re.compile("[\U0001F480\U0001F602\U0001F923\U0001F62D]")  # skull, joy, rofl, sob


def set_extra_hype(words_csv):
    HYPE_WORDS.clear()
    HYPE_WORDS.update(BASE_HYPE)
    HYPE_WORDS.update(w.strip().lower() for w in (words_csv or "").split(",") if w.strip())


def hype_tokens(text):
    toks = []
    for w in text.split():
        k = w.lower().strip(".,!?:;()[]")
        if k in HYPE_WORDS or HYPE_RE.match(k):
            toks.append(k)
    toks += EMOJI_RE.findall(text)
    return toks


ANY_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
PUNCT_RE = re.compile(r"[!?]{2,}")


def is_reaction(text, toks):
    """A message that *reacts* to something: hype words, SHOUTING, '!!!' / '???', or emoji."""
    if toks or PUNCT_RE.search(text) or ANY_EMOJI_RE.search(text):
        return True
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7


# Entertainment meter: how much is going on right now, 0-100. Mixes four signals:
ENT_WIN = 5            # seconds of chat looked at for each point of the meter
ENT_LAG = SEG + 3      # audio of a segment is known only once the segment is finished
ENT_WEIGHTS = dict(activity=0.35, crowd=0.20, reaction=0.25, audio=0.20)


def _clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


class Meter:
    """Per-second engagement buckets + spike detector + entertainment meter."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.sec = {}  # second -> [msgs, hype_msgs, set(users), reaction_msgs]
        self.emo = {}  # second -> Counter(hype tokens)
        self.aud = {}  # second -> stream audio loudness in dBFS
        self.ent_cache = {}  # second -> final entertainment value (audio already known)
        self.t0 = time.time()

    def add(self, user, text):
        toks = hype_tokens(text)
        react = is_reaction(text, toks)
        s = int(time.time())
        with self.lock:
            b = self.sec.setdefault(s, [0, 0, set(), 0])
            b[0] += 1
            b[1] += 1 if toks else 0
            b[2].add(user)
            b[3] += 1 if react else 0
            if toks:
                self.emo.setdefault(s, collections.Counter()).update(toks)

    def add_audio(self, s, db):
        with self.lock:
            self.aud[s] = db
            for k in [k for k in self.ent_cache if k >= s]:  # recompute with the new audio
                del self.ent_cache[k]

    # -- entertainment meter
    def _ent_parts(self, s):
        """Components (0..1) of the entertainment meter at second s. Caller holds the lock."""
        c = self.cfg
        win = range(s - ENT_WIN + 1, s + 1)
        lo = max(s - ENT_WIN - int(c.baseline) + 1, int(self.t0))  # only time we were actually watching chat
        if s - ENT_WIN + 1 - lo < 30:
            return {}  # warming up: no idea yet what "normal" looks like for this stream
        bs = [self.sec[k][0] if k in self.sec else 0 for k in range(lo, s - ENT_WIN + 1)]
        base = max(statistics.fmean(bs), 0.3)  # typical msgs/sec before this moment
        msgs = sum(self.sec[k][0] for k in win if k in self.sec)
        users = len(set().union(*[self.sec[k][2] for k in win if k in self.sec]))
        react = sum(self.sec[k][3] for k in win if k in self.sec)
        expect = base * ENT_WIN
        parts = dict(
            activity=_clamp((msgs / expect - 1) / 3),        # 4x the usual chat speed = full
            crowd=_clamp((users / expect - 0.8) / 2.2),      # many *different* people, not one spammer
            reaction=_clamp((react / msgs) / 0.6) * _clamp(msgs / 6) if msgs else 0.0,
        )
        # chat reacts a few seconds after the stream gets loud, so look at audio slightly earlier too
        loud = [self.aud[k] for k in range(s - 4, s + 1) if k in self.aud]
        if c.use_audio and loud:
            hist = sorted(v for k, v in self.aud.items() if lo <= k < s - 4)
            if len(hist) >= 20:
                parts["audio"] = _clamp((max(loud) - hist[len(hist) // 2] - 3) / 9)  # +12 dB over usual = full
        return parts

    def ent(self, s, final_before=None):
        """Entertainment value 0-100 for second s."""
        with self.lock:
            if s in self.ent_cache:
                return self.ent_cache[s]
            p = self._ent_parts(s)
            w = sum(ENT_WEIGHTS[k] for k in p)
            v = int(round(100 * sum(ENT_WEIGHTS[k] * x for k, x in p.items()) / w)) if w else 0
            if final_before is not None and s <= final_before:
                self.ent_cache[s] = v
            return v

    def ent_parts(self, s):
        with self.lock:
            return {k: round(v, 2) for k, v in self._ent_parts(s).items()}

    def check_ent(self, now):
        """Entertainment-meter trigger, evaluated ENT_LAG seconds behind so stream audio is known."""
        c = self.cfg
        if time.time() - self.t0 < 45 + ENT_LAG + ENT_WIN:
            return None
        e = now - ENT_LAG
        vals = [self.ent(s, final_before=e) for s in range(e - 2, e + 1)]
        with self.lock:
            msgs = sum(self.sec[k][0] for k in range(e - ENT_WIN + 1, e + 1) if k in self.sec)
        # sustained: the newest second over the line, the two before it close to it
        if vals[-1] >= c.ent_threshold and min(vals) >= c.ent_threshold * 0.85 and msgs >= c.min_users:
            return dict(at=e, entertainment=vals[-1], threshold=c.ent_threshold, parts=self.ent_parts(e))
        return None

    def score(self, s):
        b = self.sec.get(s)
        return 0.0 if not b else b[0] + self.cfg.hype_weight * b[1]

    def hype(self, s):
        b = self.sec.get(s)
        return b[1] if b else 0

    def msgs(self, s):
        b = self.sec.get(s)
        return b[0] if b else 0

    def prune(self, now):
        with self.lock:
            for d in (self.sec, self.emo, self.aud, self.ent_cache):
                for k in [k for k in d if k < now - 1500]:
                    del d[k]

    def top_emotes(self, a, b, n=3):
        c = collections.Counter()
        for s in range(a, b + 1):
            c.update(self.emo.get(s, {}))
        return [k for k, _ in c.most_common(n)]

    def check(self, now):
        c = self.cfg
        W = int(c.window)
        if time.time() - self.t0 < 45:  # warm-up: need a baseline first
            return None
        with self.lock:
            cur = range(now - W + 1, now + 1)
            sc = [self.score(s) for s in cur]
            base = [self.score(s) for s in range(now - W - int(c.baseline) + 1, now - W + 1)]
            msgs = sum(self.sec[s][0] for s in cur if s in self.sec)
            hyp = sum(self.hype(s) for s in cur)
            users = set().union(*[self.sec[s][2] for s in cur if s in self.sec])
        total = sum(sc)
        rate = total / W
        m = statistics.fmean(base)
        sd = statistics.pstdev(base)
        thr = max(m + c.sigma * sd, c.ratio * max(m, 0.3))
        share = hyp / msgs if msgs else 0.0
        if rate > thr and total >= c.min_score and len(users) >= c.min_users and share >= c.min_hype_share:
            return dict(rate=round(rate, 1), baseline=round(m, 2), threshold=round(thr, 1),
                        users=len(users), hype_share=round(share, 2), score=total)
        return None


# --------------------------------------------------------------------------- chat sources

TW_RE = re.compile(r"^:(\w+)!\S+ PRIVMSG #\S+ :(.*)$")


def twitch_chat(chan, emit, stop):
    while not stop.is_set():
        try:
            raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
            s.settimeout(5)

            def send(x):
                s.sendall((x + "\r\n").encode())

            send("PASS SCHMOOPIIE")
            send(f"NICK justinfan{random.randint(10000, 99999)}")
            send(f"JOIN #{chan}")
            buf, last = b"", time.time()
            while not stop.is_set() and time.time() - last < 360:
                try:
                    data = s.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    break
                last = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for l in lines:
                    line = l.decode("utf8", "replace")
                    if line.startswith("PING"):
                        send("PONG :tmi.twitch.tv")
                    else:
                        m = TW_RE.match(line)
                        if m:
                            emit(m.group(1), m.group(2))
            s.close()
        except Exception as e:
            log("twitch-chat", f"reconnecting: {e}")
        stop.wait(5)


def youtube_chat(video_id, emit, stop):
    import pytchat

    while not stop.is_set():
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            while chat.is_alive() and not stop.is_set():
                for c in chat.get().sync_items():
                    emit(c.author.name, c.message)
                stop.wait(1)
        except Exception as e:
            log("youtube-chat", f"reconnecting: {e}")
        stop.wait(5)


KICK_WS = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=7.6.0&flash=false"
KICK_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def kick_chatroom_id(slug):
    url = f"https://kick.com/api/v2/channels/{slug}"
    try:
        from curl_cffi import requests as cr  # bypasses Cloudflare TLS fingerprinting

        return cr.get(url, impersonate="chrome", timeout=20).json()["chatroom"]["id"]
    except ImportError:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.load(urllib.request.urlopen(req, timeout=20))["chatroom"]["id"]


def kick_chat(slug, emit, stop):
    from websockets.sync.client import connect

    while not stop.is_set():
        try:
            cid = kick_chatroom_id(slug)
            with connect(KICK_WS, open_timeout=20) as ws:
                ws.send(json.dumps({"event": "pusher:subscribe",
                                    "data": {"auth": "", "channel": f"chatrooms.{cid}.v2"}}))
                while not stop.is_set():
                    try:
                        raw = ws.recv(timeout=5)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    ev = msg.get("event", "")
                    if ev == "pusher:ping":
                        ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif ev.endswith("ChatMessageEvent"):
                        d = json.loads(msg["data"])
                        emit(d["sender"]["username"], KICK_EMOTE.sub(r"\1", d["content"]))
        except Exception as e:
            log("kick-chat", f"reconnecting: {e}")
        stop.wait(5)


# --------------------------------------------------------------------------- stream sources


def normalize_url(url):
    url = url.strip()
    return url if "://" in url else "https://" + url


def parse_target(url):
    u = urllib.parse.urlparse(normalize_url(url))
    host = u.netloc.lower().replace("www.", "")
    path = u.path.strip("/")
    if host.endswith("twitch.tv") and path:
        slug = path.split("/")[0].lower()
        return "twitch", slug, slug
    if host == "kick.com" and path:
        slug = path.split("/")[0].lower()
        return "kick", slug, "kick_" + slug
    if ("youtube.com" in host or host == "youtu.be") and (path or u.query):
        m = re.search(r"@([\w.-]+)", path) or re.search(r"(?:v=|youtu\.be/|live/)([\w-]{11})", url)
        return "youtube", path, "yt_" + (m.group(1) if m else re.sub(r"\W+", "_", path))
    raise ValueError("Unsupported link - use a Twitch, Kick or YouTube live URL")


class Source:
    def __init__(self, kind, stream=None, url=None, headers=None, vid=None):
        self.kind, self.stream, self.url, self.headers, self.vid = kind, stream, url, headers, vid


def find_source(url, quality):
    """Return a Source if the channel is live right now, else None. streamlink first, yt-dlp as fallback."""
    try:
        import streamlink

        s = streamlink.Streamlink()
        s.set_option("ffmpeg-ffmpeg", FFMPEG)
        streams = s.streams(url)
        if streams:
            for q in [x.strip() for x in quality.split(",") if x.strip()]:
                if q in streams:
                    return Source("streamlink", stream=streams[q])
            return Source("streamlink", stream=streams.get("best") or next(iter(streams.values())))
    except Exception:
        pass
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                "format": "best[height<=1080]/best"}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        if info and info.get("is_live"):
            u = info.get("url") or (info.get("formats") or [{}])[-1].get("url")
            if u:
                return Source("hls", url=u, headers=info.get("http_headers"), vid=info.get("id"))
    except Exception:
        pass
    return None


def yt_video_id(url, src):
    m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{11})", url)
    if m:
        return m.group(1)
    if src and src.vid:
        return src.vid
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
        return y.extract_info(url, download=False).get("id")




# --------------------------------------------------------------------------- live presence (fast "who is online" checks)
#
# streamlink/yt-dlp take several seconds per channel. These checks are one small web request each
# (one request for ALL Twitch channels), so the app knows within a second or two who is live, how many
# people are watching and what they are streaming - also for channels whose auto-clipping is paused.

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
TWITCH_CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web-player client id (no login needed)


def _ssl_ctx():
    try:
        import certifi  # bundled with streamlink/requests; the frozen app may not see the OS certificates

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http(url, data=None, headers=None, timeout=8):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read()


def twitch_presence(slugs):
    """All Twitch channels in ONE request -> {slug: info}."""
    slugs = [s for s in dict.fromkeys(slugs) if re.fullmatch(r"\w{1,40}", s)]
    if not slugs:
        return {}
    q = "query{" + " ".join(f'u{i}:user(login:"{s}"){{stream{{viewersCount title createdAt game{{name}}}}}}'
                            for i, s in enumerate(slugs)) + "}"
    raw = _http("https://gql.twitch.tv/gql", json.dumps({"query": q}).encode(),
                {"Client-Id": TWITCH_CID, "Content-Type": "application/json"})
    return parse_twitch_reply(json.loads(raw), slugs)


def parse_twitch_reply(reply, slugs):
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("unexpected Twitch reply")
    out = {}
    for i, s in enumerate(slugs):
        u = data.get(f"u{i}")
        st = (u or {}).get("stream")
        out[s] = dict(live=bool(st), exists=u is not None)
        if st:
            out[s].update(viewers=st.get("viewersCount"), title=st.get("title") or "",
                          category=(st.get("game") or {}).get("name") or "", since=st.get("createdAt") or "")
    return out


def kick_presence(slug):
    url = f"https://kick.com/api/v2/channels/{slug}"
    try:
        from curl_cffi import requests as cr  # bypasses Cloudflare TLS fingerprinting

        d = cr.get(url, impersonate="chrome", timeout=10).json()
    except ImportError:
        d = json.loads(_http(url, timeout=10))
    return parse_kick_reply(d)


def parse_kick_reply(d):
    if not isinstance(d, dict) or ("livestream" not in d and "slug" not in d):
        raise RuntimeError("unexpected Kick reply")
    ls = d.get("livestream")
    if not ls or ls.get("is_live") is False:
        return dict(live=False)
    cats = ls.get("categories") or []
    return dict(live=True, viewers=ls.get("viewer_count"), title=ls.get("session_title") or "",
                category=(cats[0] or {}).get("name", "") if cats else "", since=ls.get("created_at") or "")


def youtube_live_page(url):
    u = urllib.parse.urlparse(url)
    if u.netloc.endswith("youtu.be") or u.path.startswith(("/watch", "/live/")):
        return url
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[-1] in ("streams", "videos", "featured", "live", "shorts", "about", "community"):
        parts = parts[:-1]
    return "https://www.youtube.com/" + "/".join(parts) + "/live"


def parse_youtube_page(page):
    info = dict(live='"isLiveNow":true' in page)
    if info["live"]:
        m = re.search(r'<meta name="title" content="([^"]*)"', page)
        info["title"] = _html.unescape(m.group(1)) if m else ""
        i = page.find('"videoDetails"')
        m = re.search(r'"viewCount":"(\d+)"', page[i:i + 30000]) if i >= 0 else None
        info["viewers"] = int(m.group(1)) if m else None
        m = re.search(r'"startTimestamp":"([^"]+)"', page)
        info["since"] = m.group(1) if m else ""
        info["category"] = ""
    return info


def youtube_presence(url):
    page = _http(youtube_live_page(url), headers={"Cookie": "SOCS=CAI; CONSENT=YES+1"}, timeout=10)
    return parse_youtube_page(page.decode("utf8", "replace"))


def presence(url):
    """Quick live check of one channel -> dict(live=True/False, viewers, title, category, since)."""
    platform, slug, _ = parse_target(url)
    if platform == "kick":
        return kick_presence(slug)
    if platform == "youtube":
        return youtube_presence(normalize_url(url))
    return twitch_presence([slug]).get(slug, dict(live=False))


# --------------------------------------------------------------------------- recording


class SegIndex:
    """Finished recording segments and the real (wall-clock) moment each one shows.

    ffmpeg appends a line "file,start,end" to segments.csv the moment a segment is complete. We note
    when each line appeared and add up the exact segment lengths, so every second of footage maps to
    real time within a fraction of a second - however long the segments really are (they can only be
    cut on keyframes) and even when several segments arrive in the same clock second.
    """

    WINDOW = 30  # segments used to estimate the stream delay

    def __init__(self, seg_dir):
        self.dir, self.csv = seg_dir, os.path.join(seg_dir, "segments.csv")
        self.lock = threading.Lock()
        self.items = []  # dict(path, cum, dur, seen)
        self.cum, self.pos, self.buf = 0.0, 0, b""
        self.anchor = None  # wall time of footage second 0

    def poll(self):
        try:
            with open(self.csv, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return
        if not data:
            return
        now = time.time()
        with self.lock:
            self.pos += len(data)
            self.buf += data
            *lines, self.buf = self.buf.split(b"\n")
            added = False
            for ln in lines:
                parts = ln.decode("utf8", "replace").strip().rsplit(",", 2)
                if len(parts) != 3:
                    continue
                try:
                    dur = float(parts[2]) - float(parts[1])
                except ValueError:
                    continue
                if not 0 < dur < 120:
                    dur = float(SEG)
                name = os.path.basename(parts[0].strip().strip('"'))
                self.items.append(dict(path=os.path.join(self.dir, name), cum=self.cum, dur=dur, seen=now))
                self.cum += dur
                added = True
            if added:
                # seen - footage_end = network/buffer delay (+ when we happened to look). Delay only ever
                # adds, so a low percentile of recent values is the most accurate and ignores hiccups.
                offs = sorted(it["seen"] - (it["cum"] + it["dur"]) for it in self.items[-self.WINDOW:])
                self.anchor = offs[len(offs) // 4]

    def end_wall(self):
        with self.lock:
            return self.anchor + self.cum if self.anchor is not None else 0.0

    def finished(self):
        with self.lock:
            if self.anchor is None:
                return []
            return [(self.anchor + it["cum"], it["path"]) for it in self.items]

    def pieces(self, t0, t1):
        """[(wall_start, duration, path)] of every finished segment that overlaps [t0, t1]."""
        with self.lock:
            if self.anchor is None:
                return []
            a = self.anchor
            return [(a + it["cum"], it["dur"], it["path"]) for it in self.items
                    if a + it["cum"] + it["dur"] > t0 and a + it["cum"] < t1 and os.path.exists(it["path"])]

    def prune(self, before):
        with self.lock:
            if self.anchor is None:
                return
            keep = []
            for it in self.items:
                if self.anchor + it["cum"] + it["dur"] < before:
                    try:
                        os.remove(it["path"])
                    except OSError:
                        pass
                else:
                    keep.append(it)
            self.items = keep


class Recorder:
    """Source -> ffmpeg (stream copy) -> rolling ~4s .ts segments + segments.csv index."""

    def __init__(self, src, seg_dir):
        self.src, self.seg_dir = src, seg_dir
        self.p = self.fd = None
        self._stop = False
        self.index = SegIndex(seg_dir)

    def start(self):
        ff = [FFMPEG, "-nostdin", "-loglevel", "error"]
        piped = self.src.kind == "streamlink"
        if not piped:
            if self.src.headers:
                ff += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.src.headers.items())]
            ff += ["-i", self.src.url]
        else:
            ff += ["-i", "pipe:0"]
        # numbered files (never overwrite each other) + a CSV with each segment's exact length
        ff += ["-c", "copy", "-f", "segment", "-segment_time", str(SEG), "-segment_format", "mpegts",
               "-reset_timestamps", "1", "-segment_list", os.path.join(self.seg_dir, "segments.csv"),
               "-segment_list_type", "csv", os.path.join(self.seg_dir, "seg_%06d.ts")]
        errlog = open(os.path.join(self.seg_dir, "recorder.log"), "wb")
        self.p = subprocess.Popen(ff, stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=errlog, **popen_kw())
        if piped:
            self.fd = self.src.stream.open()
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while not self._stop:
                d = self.fd.read(65536)
                if not d:
                    break
                self.p.stdin.write(d)
        except Exception:
            pass
        finally:
            for c in (self.p.stdin, self.fd):
                try:
                    c.close()
                except Exception:
                    pass

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        """Ends recording so that ffmpeg still finishes (and indexes) the last segment."""
        self._stop = True
        for c in (self.fd, self.p.stdin if self.p else None):  # end of input -> ffmpeg finalizes cleanly
            try:
                if c:
                    c.close()
            except Exception:
                pass
        if not self.p:
            return
        for sig in (None, "terminate", "kill"):
            if sig:
                getattr(self.p, sig)()
            try:
                self.p.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                return


class Session:
    """One recording of a live stream. Its footage is kept until every clip that needs it has copied it."""

    def __init__(self, src, seg_dir):
        os.makedirs(seg_dir, exist_ok=True)
        self.dir = seg_dir
        self.rec = Recorder(src, seg_dir)
        self.index = self.rec.index
        self.lock = threading.Lock()
        self.refs, self.ended = 0, False

    def start(self):
        self.rec.start()
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while not self.ended:
            self.index.poll()
            time.sleep(0.25)

    def hold(self):
        with self.lock:
            self.refs += 1

    def release(self):
        with self.lock:
            self.refs -= 1
            gone = self.ended and self.refs <= 0
        if gone:
            shutil.rmtree(self.dir, ignore_errors=True)

    def end(self):
        self.rec.stop()
        self.index.poll()  # pick up the last segment ffmpeg wrote while shutting down
        with self.lock:
            self.ended = True
            gone = self.refs <= 0
        if gone:
            shutil.rmtree(self.dir, ignore_errors=True)


AUDIO_RATE = 8000


def segment_loudness(path):
    """Per-second loudness (dBFS) of a finished segment's audio track."""
    r = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-i", path, "-vn", "-ac", "1",
                        "-ar", str(AUDIO_RATE), "-f", "s16le", "-"], capture_output=True, timeout=30, **popen_kw())
    pcm = array.array("h")
    pcm.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    out = []
    for i in range(0, len(pcm) - AUDIO_RATE // 2 + 1, AUDIO_RATE):  # ignore a trailing half-second
        chunk = pcm[i:i + AUDIO_RATE]
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk))
        out.append(20 * math.log10(max(rms, 1.0) / 32768))
    return out


def audio_watch(sess, meter, stop):
    """Feeds the entertainment meter with how loud the stream is (shouting, screaming, explosions...)."""
    done = set()
    warned = False
    while not stop.wait(1):
        fin = sess.index.finished()
        for st, p in fin:
            if p in done:
                continue
            done.add(p)
            try:
                for i, db in enumerate(segment_loudness(p)):
                    meter.add_audio(int(round(st + i)), db)
            except Exception as e:
                if not warned:
                    log("audio", f"loudness unavailable, meter uses chat only: {e}")
                    warned = True
        if len(done) > 400:
            done &= {p for _, p in fin}


# --------------------------------------------------------------------------- subtitles

_model, _model_name, _mlock = None, None, threading.Lock()
MODEL = {"state": "idle", "error": ""}  # for the UI

FONTS = {  # name in Settings (= font family name) -> bundled file in fonts/
    "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf",
    "Montserrat Black": "Montserrat-Black.ttf",
    "Poppins ExtraBold": "Poppins-ExtraBold.ttf",
    "Archivo Black": "ArchivoBlack-Regular.ttf",
    "Anton": "Anton-Regular.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
}
DEFAULT_FONT = "Montserrat ExtraBold"
SUB_STYLES = ("karaoke", "plain", "word")
SUB_SIZES = dict(small=(70, 52), medium=(84, 62), large=(100, 74), xl=(122, 90))  # (vertical, horizontal)
SUB_POS = dict(bottom=((2, 240), (2, 50)), lower=((2, 560), (2, 130)),  # (alignment, margin) per format
               middle=((5, 0), (5, 0)), top=((8, 300), (8, 60)))
FRAMINGS = ("crop", "zoom", "fit", "blur")


def fonts_dir():
    return os.path.join(resource_dir(), "fonts")


def model_ref(name):
    for base in (os.path.join(resource_dir(), "models"), os.path.join(data_dir(), "models")):
        p = os.path.join(base, name)
        if os.path.isfile(os.path.join(p, "model.bin")):
            return p  # bundled / pre-downloaded -> works offline
    return name  # faster-whisper downloads it on first use


def render_jobs(cfg):
    return max(1, min(4, int(getattr(cfg, "render_jobs", 1) or 1)))


def get_model(cfg):
    """One Whisper model with one worker per parallel render, so several clips transcribe at the same time.
    The CPU cores are shared out between the workers instead of all fighting over the same ones."""
    global _model, _model_name
    jobs = render_jobs(cfg)
    key = (cfg.model, jobs)
    with _mlock:
        if _model is None or _model_name != key:
            from faster_whisper import WhisperModel

            phys = max(1, (os.cpu_count() or 4) // 2)
            _model = WhisperModel(model_ref(cfg.model), device="auto", compute_type="auto",
                                  cpu_threads=max(2, phys // jobs), num_workers=jobs,
                                  download_root=os.path.join(data_dir(), "models"))
            _model_name = key
        return _model


def preload_model(cfg):
    def run():
        MODEL.update(state="loading", error="")
        try:
            get_model(cfg)
            MODEL["state"] = "ready"
            log("subs", f"whisper '{cfg.model}' ready")
        except Exception as e:
            MODEL.update(state="error", error=str(e)[:200])
            log("subs", f"subtitle model unavailable: {e}")

    threading.Thread(target=run, daemon=True).start()


def clean_words(words):
    """Drops Whisper's typical junk: the same word looping over and over."""
    out = []
    for w in words:
        if len(out) >= 3 and all(o[2].lower() == w[2].lower() for o in out[-3:]):
            continue
        out.append(w)
    return out


def transcribe(cfg, wav):
    model = get_model(cfg)
    lang = getattr(cfg, "sub_language", "auto") or "auto"
    # no lock here: the model has a worker per render slot, so parallel clips transcribe in parallel
    segs, _ = model.transcribe(
        wav, language=None if lang == "auto" else lang, word_timestamps=True,
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=200),
        beam_size=5, best_of=5, condition_on_previous_text=False)  # no repeated/hallucinated lines
    words = []
    for s in segs:
        if s.no_speech_prob > 0.7 and s.avg_logprob < -0.8:  # "words" heard in music/noise
            continue
        for w in s.words or []:
            t = w.word.strip()
            if t and (getattr(w, "probability", None) is None or w.probability >= 0.15):
                words.append((w.start, w.end, t))
    return clean_words(words)


def _ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _hex(c, default="FFFFFF"):
    c = str(c or "").lstrip("#")
    return c.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", c) else default


def ass_style_color(c, alpha=0, default="FFFFFF"):
    h = _hex(c, default)
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}"


def ass_tag_color(c, default="FFFFFF"):
    h = _hex(c, default)
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def chunk_words(words, max_words, max_chars):
    chunks, cur = [], []
    for w in words:
        n = sum(len(x[2]) + 1 for x in cur) + len(w[2])
        if cur and (len(cur) >= max_words or n > max_chars or w[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w[2][-1] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _word_txt(t, upper):
    s = t.strip()
    s = s.rstrip(",.;:") or s  # keep ? and ! - they carry the emotion
    if upper:
        s = s.upper()
    return s.replace("\\", "/").replace("{", "(").replace("}", ")")


POP = r"{\fscx82\fscy82\t(0,90,\fscx106\fscy106)\t(90,170,\fscx100\fscy100)}"  # quick "pop in"


def build_ass(words, cfg, vertical, path):
    """Shorts-style subtitles: bundled bold sans-serif font, thick outline, 1-3 words at a time,
    the word being spoken highlighted in its own colour."""
    W, H = (1080, 1920) if vertical else (1920, 1080)
    k = 0 if vertical else 1
    fs = SUB_SIZES.get(getattr(cfg, "sub_size", "large"), SUB_SIZES["large"])[k]
    align, mv = SUB_POS.get(getattr(cfg, "sub_position", "lower"), SUB_POS["lower"])[k]
    font = getattr(cfg, "sub_font", DEFAULT_FONT)
    font = font if font in FONTS else DEFAULT_FONT
    mode = getattr(cfg, "sub_style", "karaoke")
    upper = getattr(cfg, "sub_upper", True)
    text_c, hl_c, out_c = (getattr(cfg, "sub_color", "#FFFFFF"), getattr(cfg, "sub_highlight", "#FFE600"),
                           getattr(cfg, "sub_outline", "#000000"))
    prim, outl = ass_style_color(text_c), ass_style_color(out_c, 0, "000000")
    shadow_c = ass_style_color(out_c, 0x70, "000000")
    hl, back = ass_tag_color(hl_c, "FFE600"), ass_tag_color(text_c)
    outline, shadow = max(3, round(fs * 0.075)), max(2, round(fs * 0.04))
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{fs},{prim},{prim},{outl},{shadow_c},0,0,0,0,100,100,1,0,1,"
        f"{outline},{shadow},{align},70,70,{mv},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    if mode == "word":
        chunks = [[w] for w in words]
    else:
        chunks = chunk_words(words, 3 if vertical else 6, 18 if vertical else 34)
    lines = []

    def dia(a, b, txt):
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(max(b, a + 0.05))},Default,,0,0,0,,{txt}")

    for i, ch in enumerate(chunks):
        start = ch[0][0]
        end = max(ch[-1][1], start + 0.35) + 0.2
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0][0])
        end = max(end, start + 0.1)
        txts = [_word_txt(w[2], upper) for w in ch]
        if mode == "karaoke":
            for j, w in enumerate(ch):
                a = start if j == 0 else w[0]
                b = ch[j + 1][0] if j + 1 < len(ch) else end
                if b - a < 0.02:
                    continue
                body = " ".join("{\\c%s}%s{\\c%s}" % (hl, t, back) if n == j else t for n, t in enumerate(txts))
                dia(a, b, (POP if j == 0 else "") + body)
        else:
            dia(start, end, POP + " ".join(txts))
    with open(path, "w", encoding="utf8") as f:
        f.write(head + "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- render


def frame_filter(framing, W, H):
    """How the stream picture fills the clip frame. Always keeps the real proportions (no stretching)."""
    sq = "scale='trunc(iw*sar/2)*2':ih,setsar=1"  # square pixels first -> correct aspect ratio
    lz = "flags=lanczos"  # sharpest scaler, so upscaled footage does not look soft
    if framing == "crop":  # fill the whole frame, cut the sides
        return f"[0:v]{sq},scale={W}:{H}:force_original_aspect_ratio=increase:{lz},crop={W}:{H},setsar=1[v]"
    if framing in ("fit", "zoom"):  # whole picture (zoom: trimmed to 4:3 so it is bigger), black bars
        trim = ",crop='min(iw,trunc(ih*4/3/2)*2)':ih" if framing == "zoom" and H > W else ""
        return (f"[0:v]{sq}{trim},scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:{lz},"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]")
    return (f"[0:v]{sq},split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"  # blur
            f"gblur=sigma=30[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:{lz}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")


def enc_args(encoder):
    if encoder == "libx264":
        # "veryfast" encodes about twice as fast as "fast"; crf 17 instead of 18 keeps the picture as sharp
        v = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-profile:v", "high"]
    elif encoder == "h264_nvenc":  # NVIDIA: constant-quality mode, looks like crf ~18 at a fraction of the time
        v = ["-c:v", encoder, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
             "-maxrate", "20M", "-bufsize", "30M", "-profile:v", "high"]
    else:
        v = ["-c:v", encoder, "-b:v", "12M", "-maxrate", "16M", "-bufsize", "24M"]
    return v + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart"]


HW_ENCODERS = (["h264_videotoolbox"] if sys.platform == "darwin" else ["h264_nvenc", "h264_qsv", "h264_amf"])
AUTO_ENCODER = {"state": "unchecked", "encoder": "libx264", "note": ""}  # what "auto" picked, for the UI
_auto_lock = threading.Lock()


def _encoder_works(enc):
    """Tries a 1-second vertical test encode (drivers that are too old or missing fail here, not mid-clip)."""
    d = tempfile.mkdtemp(prefix="encchk_")
    try:
        r = subprocess.run([FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=1080x1920:r=30:d=1"] + enc_args(enc)[:-10] +
                           ["-pix_fmt", "yuv420p", os.path.join(d, "t.mp4")],
                           capture_output=True, text=True, errors="replace", timeout=40, **popen_kw())
        lines = (r.stderr or "").strip().splitlines()
        why = next((l for l in lines if "driver" in l.lower() or "not available" in l.lower()
                    or "cannot load" in l.lower()), lines[0] if lines else "")
        return r.returncode == 0, [why.split("] ", 1)[-1]]
    except Exception as e:
        return False, [str(e)]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def pick_encoder(choice):
    """'auto' -> the first hardware encoder (GPU) that really works on this PC, else libx264."""
    if choice != "auto":
        return choice
    with _auto_lock:
        if AUTO_ENCODER["state"] == "unchecked":
            notes = []
            for enc in HW_ENCODERS:
                ok, err = _encoder_works(enc)
                if ok:
                    AUTO_ENCODER.update(state="checked", encoder=enc, note=f"using {enc} (graphics card)")
                    break
                notes.append(f"{enc}: {err[0][:120]}")
            else:
                hint = " - updating the graphics driver may enable it" if any("driver" in n.lower() for n in notes) else ""
                AUTO_ENCODER.update(state="checked", encoder="libx264",
                                    note=f"no working graphics-card encoder, using libx264 (CPU){hint}")
            log("render", f"auto encoder: {AUTO_ENCODER['note']}" + (f" [{'; '.join(notes)}]" if notes else ""))
        return AUTO_ENCODER["encoder"]


def render(cfg, pieces, t0, t1, out, tmp):
    """pieces: [(wall_start, duration, path)] already copied into tmp.
    Returns (words, real_start, real_end). Falls back to simpler settings rather than losing the clip."""
    first, last_end = pieces[0][0], pieces[-1][0] + pieces[-1][1]
    a, b = max(t0, first), min(t1, last_end)
    if b - a < 3:
        raise RuntimeError("less than 3 seconds of footage recorded for this moment")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf8") as f:
        for _, d, p in pieces:  # exact segment lengths -> frame-accurate cut points
            f.write("file '%s'\nduration %.6f\n" % (os.path.abspath(p).replace("\\", "/").replace("'", "'\\''"), d))
    base = [FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
            "-ss", f"{a - first:.3f}", "-t", f"{b - a:.3f}"]
    vertical = cfg.format == "vertical"
    W, H = (1080, 1920) if vertical else (1920, 1080)
    words = []
    if not cfg.no_subs:
        wav = os.path.join(tmp, "audio.wav")
        try:
            sh(base + ["-vn", "-ac", "1", "-ar", "16000", wav])
            words = transcribe(cfg, wav)
        except Exception as e:
            log("subs", f"transcription skipped: {e}")
    if words:
        build_ass(words, cfg, vertical, os.path.join(tmp, "subs.ass"))
        fd = os.path.join(tmp, "fonts")
        os.makedirs(fd, exist_ok=True)
        for fn in FONTS.values():
            src = os.path.join(fonts_dir(), fn)
            if os.path.exists(src):
                shutil.copy(src, fd)
    framing = getattr(cfg, "framing", "crop")

    def attempt(subs, frm, enc):
        fc = frame_filter(frm, W, H)
        fc += {2: ";[v]ass=subs.ass:fontsdir=fonts[out]", 1: ";[v]ass=subs.ass[out]", 0: ";[v]null[out]"}[subs]
        sh(base + ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"] + enc_args(enc) + [os.path.abspath(out)],
           cwd=tmp)

    enc0 = pick_encoder(cfg.encoder)
    encs = [enc0] + (["libx264"] if enc0 != "libx264" else [])
    variants = [(s, framing, e) for e in encs for s in ((2, 1, 0) if words else (0,))] + [(0, "fit", "libx264")]
    last = None
    for v in variants:
        try:
            attempt(*v)
            if v != variants[0]:
                log("render", f"rendered with fallback settings {v}")
            return words, a, b
        except RuntimeError as e:
            last = e
            log("render", f"retrying with simpler settings ({str(e)[:160]})")
    raise last


def thumbnail(mp4, at, png):
    try:
        sh([FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}", "-i", mp4,
            "-frames:v", "1", "-vf", "scale=480:-2", png])
    except Exception:
        pass


# --------------------------------------------------------------------------- per-channel worker

class RenderSlots:
    """How many clips render at the same time (Settings -> 'Clips rendered at the same time').
    The limit is read live, so changing the setting takes effect for the next clip in the queue."""

    def __init__(self):
        self.cond = threading.Condition()
        self.busy = 0
        self.waiting = 0

    @contextlib.contextmanager
    def take(self, cfg):
        with self.cond:
            self.waiting += 1
            try:
                while self.busy >= render_jobs(cfg):
                    self.cond.wait(1)
            finally:
                self.waiting -= 1
            self.busy += 1
        try:
            yield
        finally:
            with self.cond:
                self.busy -= 1
                self.cond.notify_all()

    def status(self):
        return dict(rendering=self.busy, queued=self.waiting)


RENDER_SLOTS = RenderSlots()


def safe_label(s):
    return re.sub(r"[^\w@.-]+", "_", s).strip("_.")[:60]


class Worker(threading.Thread):
    def __init__(self, cfg, url, label="", presence=None):
        super().__init__(daemon=True)
        self.cfg, self.url = cfg, normalize_url(url)
        self.platform, self.slug, self.name = parse_target(self.url)
        self.label = label or ""
        self.presence = presence if presence is not None else {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.meter = None
        self.live = False
        self.status = "checking"
        self.trigs = []  # dict(t, reason, status, file, error) - the lines on the graph and what became of them
        self.clip_count = 0

    @property
    def display(self):
        return self.label or self.name

    def halt(self):
        self.stop.set()
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            p = self.presence.get(self.url) or {}
            fresh = time.time() - p.get("checked", 0) < max(self.cfg.poll * 3, 90)
            if fresh and p.get("live") is False:  # quick check says offline -> don't bother streamlink
                self.live, self.status = False, "offline"
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.status = "checking"
            src = find_source(self.url, self.cfg.quality)
            if self.stop.is_set():
                break
            if not src:
                self.live, self.status = False, "offline"
                self.wake.wait(20 if p.get("live") else self.cfg.poll)
                self.wake.clear()
                continue
            self.live, self.status = True, "live"
            log(self.name, "LIVE - recording + watching chat")
            try:
                self.session(src)
            except Exception as e:
                log(self.name, f"session error: {e}")
            self.live, self.status = False, "offline"
            log(self.name, "recorder stopped / stream ended; re-checking soon")
            self.stop.wait(5)
        self.live, self.status = False, "stopped"

    def start_chat(self, stop_s, src):
        emit = lambda user, text: self.meter.add(user, text)  # noqa: E731
        if self.platform == "twitch":
            fn, args = twitch_chat, (self.slug, emit, stop_s)
        elif self.platform == "kick":
            fn, args = kick_chat, (self.slug, emit, stop_s)
        else:
            vid = yt_video_id(self.url, src)
            if not vid:
                raise RuntimeError("could not resolve YouTube live video id")
            fn, args = youtube_chat, (vid, emit, stop_s)
        threading.Thread(target=fn, args=args, daemon=True).start()

    def session(self, src):
        cfg = self.cfg
        sess = Session(src, os.path.join(cfg.work, self.name, f"{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(100, 999)}"))
        self.meter = Meter(cfg)
        stop_s = threading.Event()
        try:
            self.start_chat(stop_s, src)
            sess.start()
            threading.Thread(target=audio_watch, args=(sess, self.meter, stop_s), daemon=True).start()
            last_trig = 0.0
            while not self.stop.is_set() and sess.rec.alive():
                time.sleep(1)
                now = int(time.time()) - 1
                self.meter.prune(now)
                mode = getattr(cfg, "trigger", "both")
                hit = trig = None
                if time.time() - last_trig >= cfg.cooldown and mode in ("hype", "both"):
                    hit = self.meter.check(now)
                    if hit:
                        hit["reason"], trig = "hype", time.time()
                if not hit and mode in ("entertainment", "both"):
                    h = self.meter.check_ent(now)  # always evaluated, so the meter's history stays cached
                    if h and time.time() - last_trig >= cfg.cooldown and h["at"] + 1 > last_trig:
                        hit, trig = h, float(h.pop("at") + 1)
                        hit["reason"] = "entertainment"
                if hit:
                    last_trig = time.time()
                    tr = dict(t=trig, reason=hit["reason"], status="waiting", file="", error="")
                    self.trigs = self.trigs[-49:] + [tr]
                    log(self.name, f"{'SPIKE' if hit['reason'] == 'hype' else 'ENTERTAINMENT'} detected {hit}")
                    sess.hold()  # keep this footage on disk until the clip has copied it
                    threading.Thread(target=self.clip, args=(tr, hit, self.meter, sess), daemon=True).start()
                if now % 60 == 0:
                    n = sum(self.meter.msgs(s) for s in range(now - 59, now + 1))
                    log(self.name, f"chat: {n} msgs/min")
                if now % 30 == 0:
                    sess.index.prune(time.time() - max(cfg.buffer, cfg.pre + cfg.post + 90))
        finally:
            stop_s.set()
            sess.end()

    def clip(self, tr, hit, meter, sess):
        cfg = self.cfg
        trig = tr["t"]
        t0, t1 = trig - cfg.pre, trig + cfg.post
        tmp = tempfile.mkdtemp(prefix="ac_")
        held = True
        try:
            # 1) wait until the after-part is really recorded (or the stream/recorder ended)
            deadline = time.time() + cfg.post + 90
            while time.time() < deadline and sess.index.end_wall() < t1 + 0.3 and not sess.ended:
                time.sleep(0.5)
            # 2) copy the footage right away, so nothing (buffer cleanup, stream restart) can delete it
            pieces = []
            for st, d, p in sess.index.pieces(t0, t1):
                dst = os.path.join(tmp, os.path.basename(p))
                try:
                    shutil.copy(p, dst)
                    pieces.append((st, d, dst))
                except OSError:
                    pass
            sess.release()
            held = False
            if not pieces:
                raise RuntimeError("no footage recorded for this moment (stream had just started or the recorder stopped)")
            got = min(t1, pieces[-1][0] + pieces[-1][1]) - max(t0, pieces[0][0])
            if got < (t1 - t0) - 2:
                log(self.name, f"only {got:.0f}s of the {t1 - t0:.0f}s were recorded; making a shorter clip")
            # 3) render (several at once, as many as the 'render_jobs' setting allows)
            tr["status"] = "queued"
            with RENDER_SLOTS.take(cfg):
                tr["status"] = "rendering"
                os.makedirs(cfg.out, exist_ok=True)
                name = f"{safe_label(self.display) or self.name}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(trig))}"
                base, n = name, 2
                while os.path.exists(os.path.join(cfg.out, name + ".mp4")):
                    name, n = f"{base}_{n}", n + 1
                out = os.path.join(cfg.out, name + ".mp4")
                words, a, b = render(cfg, pieces, t0, t1, out, tmp)
                thumbnail(out, trig - a, os.path.join(cfg.out, name + ".png"))
            s0 = int(trig) - 150
            rng = range(s0, int(t1) + 20)
            emotes = meter.top_emotes(int(trig) - int(cfg.window), int(trig))
            text = " ".join(w[2] for w in words)
            title = (text[:70].rsplit(" ", 1)[0] if len(text) > 20 else
                     f"{self.display} chat goes wild {' '.join(emotes[:1])}").strip()
            meta = dict(channel=self.name, label=self.display, platform=self.platform, source=self.url,
                        detected_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trig)),
                        detected_ts=trig, spike=hit, reason=hit.get("reason", "hype"), top_emotes=emotes,
                        transcript=text, suggested_title=title, uploaded=False, duration=round(b - a, 1),
                        series=dict(start=s0, score=[meter.score(s) for s in rng], hype=[meter.hype(s) for s in rng],
                                    ent=[meter.ent(s) for s in rng],
                                    trig=int(trig) - s0, clip_start=int(a) - s0, clip_end=int(b) - s0))
            with open(os.path.join(cfg.out, name + ".json"), "w", encoding="utf8") as f:
                json.dump(meta, f, ensure_ascii=False)
            self.clip_count += 1
            tr.update(status="done", file=name)
            log(self.name, f"CLIP READY -> {out}")
        except Exception as e:
            tr.update(status="failed", error=str(e)[:240])
            log(self.name, f"CLIP FAILED for the moment at {time.strftime('%H:%M:%S', time.localtime(trig))}: {e}")
        finally:
            if held:
                sess.release()
            shutil.rmtree(tmp, ignore_errors=True)

    def snapshot(self):
        m, now, N = self.meter, int(time.time()), 300
        rng = range(now - N + 1, now + 1)
        on = bool(m and self.live)
        ent = [m.ent(s, final_before=now - ENT_LAG) for s in rng] if on else []
        recent = [dict(at=time.strftime("%H:%M:%S", time.localtime(t["t"])), reason=t["reason"],
                       status=t["status"], error=t["error"], file=t["file"]) for t in self.trigs[-8:]][::-1]
        return dict(live=self.live, status=self.status, clip_count=self.clip_count,
                    score=[m.score(s) for s in rng] if on else [],
                    hype=[m.hype(s) for s in rng] if on else [],
                    ent=ent, ent_now=ent[-1] if ent else 0, ent_parts=m.ent_parts(now) if on else {},
                    ent_threshold=self.cfg.ent_threshold, trigger=self.cfg.trigger,
                    msgs_min=sum(m.msgs(s) for s in range(now - 59, now + 1)) if on else 0,
                    marks=[[int(t["t"] - (now - N + 1)), t["reason"], t["status"]] for t in self.trigs
                           if now - N + 1 <= t["t"] <= now],
                    recent=recent)
