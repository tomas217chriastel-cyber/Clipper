"""YouTube integration: OAuth 2.0 (desktop/loopback + PKCE) and resumable video upload with optional scheduled publishing.
Pure standard library, so nothing extra needs to be bundled."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_SCHEME, API_HOST = "https", "www.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB


class YouTubeError(Exception):
    pass


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _friendly(status, body):
    try:
        e = json.loads(body)
        err = e.get("error")
        if isinstance(err, dict):
            reasons = " ".join(x.get("reason", "") for x in err.get("errors", []))
            msg = err.get("message", "")
            if "quotaExceeded" in reasons or "uploadLimitExceeded" in reasons:
                return "YouTube upload limit reached (API quota or your channel's daily upload limit). Try again later."
            return f"{msg} ({reasons.strip() or status})"
        if err:
            return f"{err}: {e.get('error_description', '')}".strip()
    except (ValueError, AttributeError):
        pass
    return f"HTTP {status}: {body[:200].decode('utf8', 'replace') if isinstance(body, bytes) else body[:200]}"


def _conn(scheme, host, timeout=120):
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, timeout=timeout)


class YouTube:
    def __init__(self, folder):
        self.path = os.path.join(folder, "youtube_token.json")
        self.tok = {}
        self._pending = {}  # state -> (verifier, redirect_uri)
        try:
            with open(self.path, encoding="utf8") as f:
                self.tok = json.load(f)
        except (OSError, ValueError):
            pass

    def connected(self):
        return bool(self.tok.get("refresh_token"))

    def _save(self):
        with open(self.path, "w", encoding="utf8") as f:
            json.dump(self.tok, f)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def disconnect(self):
        self.tok = {}
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- OAuth
    def auth_url(self, client_id, redirect_uri):
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(16)
        self._pending[state] = (verifier, redirect_uri)
        q = dict(client_id=client_id, redirect_uri=redirect_uri, response_type="code", scope=SCOPE,
                 access_type="offline", prompt="consent", state=state,
                 code_challenge=_b64(hashlib.sha256(verifier.encode()).digest()), code_challenge_method="S256")
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def _post_form(self, data):
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise YouTubeError(_friendly(e.code, e.read()))
        except OSError as e:
            raise YouTubeError(f"Cannot reach Google: {e}")

    def finish(self, code, state, client_id, client_secret):
        if state not in self._pending:
            raise YouTubeError("Login session expired - click Connect again")
        verifier, redirect = self._pending.pop(state)
        d = self._post_form(dict(code=code, client_id=client_id, client_secret=client_secret, redirect_uri=redirect,
                                 grant_type="authorization_code", code_verifier=verifier))
        if not d.get("refresh_token"):
            raise YouTubeError("Google did not return a refresh token - disconnect, then connect again")
        self.tok = dict(refresh_token=d["refresh_token"], access_token=d["access_token"],
                        expires_at=time.time() + int(d.get("expires_in", 3600)) - 60)
        self._save()

    def access_token(self, client_id, client_secret):
        if not self.connected():
            raise YouTubeError("YouTube is not connected")
        if self.tok.get("access_token") and self.tok.get("expires_at", 0) > time.time():
            return self.tok["access_token"]
        try:
            d = self._post_form(dict(refresh_token=self.tok["refresh_token"], client_id=client_id,
                                     client_secret=client_secret, grant_type="refresh_token"))
        except YouTubeError as e:
            if "invalid_grant" in str(e):
                self.disconnect()
                raise YouTubeError("YouTube login expired - reconnect in the YouTube tab "
                                   "(set your Google consent screen to 'In production' to avoid weekly expiry)")
            raise
        self.tok.update(access_token=d["access_token"], expires_at=time.time() + int(d.get("expires_in", 3600)) - 60)
        self._save()
        return self.tok["access_token"]

    # -- upload
    def upload(self, client_id, client_secret, path, snippet, status, progress=None, cancelled=None):
        size = os.path.getsize(path)
        token = self.access_token(client_id, client_secret)
        body = json.dumps(dict(snippet=snippet, status=status)).encode()
        c = _conn(API_SCHEME, API_HOST, 60)
        try:
            c.request("POST", "/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", body, {
                "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
            r = c.getresponse()
            data = r.read()
        except OSError as e:
            raise YouTubeError(f"Cannot reach YouTube: {e}")
        finally:
            c.close()
        if r.status != 200:
            raise YouTubeError(_friendly(r.status, data))
        u = urllib.parse.urlparse(r.getheader("Location"))
        scheme, host, target = u.scheme, u.netloc, u.path + ("?" + u.query if u.query else "")

        def put(headers, payload):
            cc = _conn(scheme, host, 180)
            try:
                cc.request("PUT", target, payload, headers)
                rr = cc.getresponse()
                return rr.status, rr.read(), rr.getheader("Range")
            finally:
                cc.close()

        def parse_done(st, dat):
            return json.loads(dat)["id"] if st in (200, 201) else None

        pos, retries = 0, 0
        with open(path, "rb") as f:
            while True:
                if cancelled and cancelled():
                    raise YouTubeError("Cancelled")
                f.seek(pos)
                chunk = f.read(min(CHUNK, size - pos))
                end = pos + len(chunk) - 1
                try:
                    st, dat, rng = put({"Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                                        "Content-Range": f"bytes {pos}-{end}/{size}"}, chunk)
                except (OSError, http.client.HTTPException):
                    st, dat, rng = 503, b"", None
                vid = parse_done(st, dat)
                if vid:
                    return vid
                if st == 308:
                    pos = int(rng.split("-")[1]) + 1 if rng else 0
                    retries = 0
                    if progress:
                        progress(int(pos * 100 / size))
                elif st in (500, 502, 503, 504):
                    retries += 1
                    if retries > 6:
                        raise YouTubeError("Upload kept failing (network or YouTube error)")
                    time.sleep(min(2 ** retries, 30))
                    try:  # ask YouTube how much it already has, then resume from there
                        st2, dat2, rng2 = put({"Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
                        vid = parse_done(st2, dat2)
                        if vid:
                            return vid
                        pos = int(rng2.split("-")[1]) + 1 if st2 == 308 and rng2 else 0
                    except (OSError, http.client.HTTPException):
                        pass
                else:
                    raise YouTubeError(_friendly(st, dat))
