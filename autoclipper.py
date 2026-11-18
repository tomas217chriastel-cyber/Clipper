"""
AutoClipper core engine.

Per channel: wait for live -> record rolling buffer -> read chat -> detect hype spike ->
cut [spike-30s, spike+12s] -> Whisper subtitles -> MP4 + metadata JSON.
Everything (streamlink, yt-dlp, ffmpeg) is used in-process / via bundled binaries,
so a frozen (PyInstaller) build needs nothing installed on the user's machine.
"""
from __future__ import annotations

import array
import collections
import glob
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
                "format": "best[height<=720]/best"}
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


class Recorder:
    """Source -> ffmpeg (stream copy) -> rolling 4s .ts segments named by wall-clock time."""

    def __init__(self, src, seg_dir):
        self.src, self.seg_dir = src, seg_dir
        self.p = self.fd = None
        self._stop = False

    def start(self):
        ff = [FFMPEG, "-nostdin", "-loglevel", "error"]
        piped = self.src.kind == "streamlink"
        if not piped:
            if self.src.headers:
                ff += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.src.headers.items())]
            ff += ["-i", self.src.url]
        else:
            ff += ["-i", "pipe:0"]
        ff += ["-c", "copy", "-f", "segment", "-segment_time", str(SEG), "-segment_format", "mpegts",
               "-reset_timestamps", "1", "-strftime", "1", os.path.join(self.seg_dir, "seg_%Y%m%d_%H%M%S.ts")]
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
        self._stop = True
        if self.p and self.p.poll() is None:
            self.p.terminate()
        if self.fd:
            try:
                self.fd.close()
            except Exception:
                pass


def seg_start(path):
    m = re.search(r"seg_(\d{8}_\d{6})\.ts$", path)
    return time.mktime(time.strptime(m.group(1), "%Y%m%d_%H%M%S"))


def list_segs(d):
    return sorted((seg_start(p), p) for p in glob.glob(os.path.join(d, "seg_*.ts")))


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


def audio_watch(seg_dir, meter, stop):
    """Feeds the entertainment meter with how loud the stream is (shouting, screaming, explosions...)."""
    done = set()
    warned = False
    while not stop.wait(2):
        segs = list_segs(seg_dir)
        for st, p in segs[:-1]:  # the newest segment is still being written
            if p in done:
                continue
            done.add(p)
            try:
                for i, db in enumerate(segment_loudness(p)):
                    meter.add_audio(int(st) + i, db)
            except Exception as e:
                if not warned:
                    log("audio", f"loudness unavailable, meter uses chat only: {e}")
                    warned = True
        live = {p for _, p in segs}
        done &= live


# --------------------------------------------------------------------------- subtitles + render

_model, _model_name, _mlock = None, None, threading.Lock()
MODEL = {"state": "idle", "error": ""}  # for the UI


def model_ref(name):
    for base in (os.path.join(resource_dir(), "models"), os.path.join(data_dir(), "models")):
        p = os.path.join(base, name)
        if os.path.isfile(os.path.join(p, "model.bin")):
            return p  # bundled / pre-downloaded -> works offline
    return name  # faster-whisper downloads it on first use


def get_model(cfg):
    global _model, _model_name
    with _mlock:
        if _model is None or _model_name != cfg.model:
            from faster_whisper import WhisperModel

            _model = WhisperModel(model_ref(cfg.model), device="auto", compute_type="auto",
                                  download_root=os.path.join(data_dir(), "models"))
            _model_name = cfg.model
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


def transcribe(cfg, wav):
    model = get_model(cfg)
    with _mlock:
        segs, _ = model.transcribe(wav, word_timestamps=True, vad_filter=True, beam_size=1)
        return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]


def _ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words, vertical, path):
    W, H, fs, mv = (1080, 1920, 88, 440) if vertical else (1920, 1080, 66, 80)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{mv},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 4 or w[0] - cur[-1][1] > 0.7):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w[2][-1] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    lines = []
    for i, ch in enumerate(chunks):
        start, end = ch[0][0], max(ch[-1][1], ch[0][0] + 0.35)
        if i + 1 < len(chunks):
            end = min(end + 0.15, chunks[i + 1][0][0])
        txt = " ".join(x[2] for x in ch).upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(max(end, start + 0.1))},Default,,0,0,0,,{txt}")
    with open(path, "w", encoding="utf8") as f:
        f.write(head + "\n".join(lines) + "\n")


def render(cfg, segs, t0, t1, out, tmp):
    """segs: [(start_ts, path)] copied into tmp. Returns transcript words. Falls back gracefully."""
    first = segs[0][0]
    off = max(0.0, t0 - first)
    dur = t1 - (first + off)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for _, p in segs:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/").replace("'", "'\\''"))
    base = [FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
            "-ss", f"{off:.2f}", "-t", f"{dur:.2f}"]
    vertical = cfg.format == "vertical"
    words = []
    if not cfg.no_subs:
        wav = os.path.join(tmp, "audio.wav")
        sh(base + ["-vn", "-ac", "1", "-ar", "16000", wav])
        try:
            words = transcribe(cfg, wav)
        except Exception as e:
            log("subs", f"transcription skipped: {e}")
    if words:
        build_ass(words, vertical, os.path.join(tmp, "subs.ass"))
    enc = ["-c:v", cfg.encoder] + (["-preset", "veryfast", "-crf", "21"] if cfg.encoder == "libx264" else ["-b:v", "8M"])
    enc += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]

    def attempt(use_subs, blur):
        if vertical:
            fc = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
                  f"crop=1080:1920,{blur}[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
        else:
            fc = ("[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                  "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v]")
        fc += ";[v]ass=subs.ass[out]" if use_subs else ";[v]null[out]"
        sh(base + ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"] + enc + [os.path.abspath(out)], cwd=tmp)

    variants = ([(True, "gblur=sigma=30")] if words else []) + [(False, "gblur=sigma=30"), (False, "boxblur=20:5")]
    last = None
    for use_subs, blur in variants:
        try:
            attempt(use_subs, blur)
            return words
        except RuntimeError as e:
            last = e
            log("render", f"retrying with simpler filters ({str(e)[:120]})")
    raise last


# --------------------------------------------------------------------------- per-channel worker


class Worker(threading.Thread):
    def __init__(self, cfg, url):
        super().__init__(daemon=True)
        self.cfg, self.url = cfg, normalize_url(url)
        self.platform, self.slug, self.name = parse_target(self.url)
        self.seg_dir = os.path.join(cfg.work, self.name)
        self.stop = threading.Event()
        self.meter = None
        self.live = False
        self.status = "checking"
        self.trigs = []
        self.clip_count = 0

    def run(self):
        while not self.stop.is_set():
            self.status = "checking"
            src = find_source(self.url, self.cfg.quality)
            if self.stop.is_set():
                break
            if not src:
                self.live, self.status = False, "offline"
                self.stop.wait(self.cfg.poll)
                continue
            self.live, self.status = True, "live"
            log(self.name, "LIVE - recording + watching chat")
            try:
                self.session(src)
            except Exception as e:
                log(self.name, f"session error: {e}")
            self.live, self.status = False, "offline"
            log(self.name, "recorder stopped / stream ended; re-checking soon")
            self.stop.wait(15)
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
        shutil.rmtree(self.seg_dir, ignore_errors=True)
        os.makedirs(self.seg_dir)
        self.meter = Meter(cfg)
        self.trigs = []
        stop_s = threading.Event()
        self.start_chat(stop_s, src)
        rec = Recorder(src, self.seg_dir)
        rec.start()
        threading.Thread(target=audio_watch, args=(self.seg_dir, self.meter, stop_s), daemon=True).start()
        last_trig = 0.0
        try:
            while not self.stop.is_set() and rec.alive():
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
                    self.trigs.append((trig, hit["reason"]))
                    log(self.name, f"{'SPIKE' if hit['reason'] == 'hype' else 'ENTERTAINMENT'} detected {hit}")
                    threading.Thread(target=self.clip, args=(trig, hit, self.meter), daemon=True).start()
                if now % 60 == 0:
                    n = sum(self.meter.msgs(s) for s in range(now - 59, now + 1))
                    log(self.name, f"chat: {n} msgs/min")
                if now % 30 == 0:
                    keep = max(cfg.buffer, cfg.pre + cfg.post + 60)
                    for st, p in list_segs(self.seg_dir):
                        if st < time.time() - keep:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
        finally:
            stop_s.set()
            rec.stop()

    def clip(self, trig, hit, meter):
        cfg = self.cfg
        tmp = tempfile.mkdtemp(prefix="ac_")
        try:
            pre, post = cfg.pre, cfg.post
            time.sleep(post + SEG + 2)  # let post-roll footage land on disk
            t0, t1 = trig - pre, trig + post
            picked = []
            for s, p in list_segs(self.seg_dir):
                if t0 - SEG - 3 <= s <= t1:
                    dst = os.path.join(tmp, os.path.basename(p))
                    shutil.copy(p, dst)
                    picked.append((s, dst))
            if not picked:
                log(self.name, "no footage in buffer for this spike (stream just started?)")
                return
            os.makedirs(cfg.out, exist_ok=True)
            name = re.sub(r"[^\w@.-]", "_", f"{self.name}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(trig))}")
            out = os.path.join(cfg.out, name + ".mp4")
            words = render(cfg, picked, t0, t1, out, tmp)
            a = int(trig) - 150
            rng = range(a, int(t1) + 20)
            emotes = meter.top_emotes(int(trig) - int(cfg.window), int(trig))
            text = " ".join(w[2] for w in words)
            title = (text[:70].rsplit(" ", 1)[0] if len(text) > 20 else f"{self.name} chat goes wild {' '.join(emotes[:1])}").strip()
            meta = dict(channel=self.name, platform=self.platform, source=self.url,
                        detected_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trig)),
                        detected_ts=trig, spike=hit, reason=hit.get("reason", "hype"), top_emotes=emotes,
                        transcript=text, suggested_title=title, uploaded=False,
                        series=dict(start=a, score=[meter.score(s) for s in rng], hype=[meter.hype(s) for s in rng],
                                    ent=[meter.ent(s) for s in rng],
                                    trig=int(trig) - a, clip_start=int(t0) - a, clip_end=int(t1) - a))
            with open(os.path.join(cfg.out, name + ".json"), "w", encoding="utf8") as f:
                json.dump(meta, f, ensure_ascii=False)
            self.clip_count += 1
            log(self.name, f"CLIP READY -> {out}")
        except Exception as e:
            log(self.name, f"clip failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def snapshot(self):
        m, now, N = self.meter, int(time.time()), 300
        rng = range(now - N + 1, now + 1)
        on = bool(m and self.live)
        ent = [m.ent(s, final_before=now - ENT_LAG) for s in rng] if on else []
        return dict(live=self.live, status=self.status, clip_count=self.clip_count,
                    score=[m.score(s) for s in rng] if on else [],
                    hype=[m.hype(s) for s in rng] if on else [],
                    ent=ent, ent_now=ent[-1] if ent else 0, ent_parts=m.ent_parts(now) if on else {},
                    ent_threshold=self.cfg.ent_threshold, trigger=self.cfg.trigger,
                    msgs_min=sum(m.msgs(s) for s in range(now - 59, now + 1)) if on else 0,
                    marks=[[int(t - (now - N + 1)), r] for t, r in self.trigs if now - N + 1 <= t <= now])
