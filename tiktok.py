"""TikTok integration: Login Kit for Desktop (loopback redirect + PKCE) and the Content Posting API.

Two ways to send a clip:
  * "inbox"  - upload as a draft to the creator's TikTok inbox (scope video.upload). They tap the
               notification in the TikTok app and post it from there. Works for unaudited apps.
  * "direct" - post straight to the profile (scope video.publish). Until TikTok audits the developer
               app, such posts can only be private ("Only me").
Pure standard library, so nothing extra needs to be bundled.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com"
SCOPES = "user.info.basic,video.upload,video.publish"
REDIRECT_PATH = "/tiktok/callback/"
CHUNK = 10_000_000          # 10 MB chunks (TikTok: 5-64 MB, the last one may be bigger)
SINGLE_MAX = 60_000_000     # up to this size the file goes in one piece
PRIVACY = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")

FRIENDLY = {
    "unaudited_client_can_only_post_to_private_accounts":
        "TikTok has not audited your developer app yet: direct posts only work as 'Only me' and only to a "
        "private account. Use 'Send to TikTok drafts' instead, or request the audit in the TikTok developer portal.",
    "privacy_level_option_mismatch": "That visibility is not available for this TikTok account - pick another one.",
    "spam_risk_too_many_posts": "TikTok's daily posting limit for this account was reached. Try again tomorrow.",
    "spam_risk_too_many_pending_share":
        "TikTok allows at most 5 unfinished drafts per 24 hours - post or delete the ones in your TikTok inbox first.",
    "spam_risk_user_banned_from_posting": "TikTok does not allow this account to post right now.",
    "reached_active_user_cap": "Your TikTok developer app reached its daily user limit. Try again tomorrow.",
    "scope_not_authorized":
        "The TikTok login is missing a permission - add Content Posting API (with Direct Post) to your developer "
        "app, then Disconnect and Connect TikTok again.",
    "access_token_invalid": "TikTok login expired - Connect TikTok again.",
    "rate_limit_exceeded": "TikTok says: too many requests. Wait a minute and retry.",
}
FAIL_REASONS = {
    "file_format_check_failed": "TikTok rejected the file format",
    "duration_check_failed": "the clip is longer than this TikTok account may post",
    "frame_rate_check_failed": "TikTok rejected the frame rate",
    "picture_size_check_failed": "TikTok rejected the picture size",
    "publish_cancelled": "posting was cancelled in TikTok",
    "auth_removed": "the TikTok permission was removed - connect again",
    "spam_risk_text": "TikTok flagged the caption text",
}


class TikTokError(Exception):
    pass


def _err(status, body):
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return f"TikTok HTTP {status}: {str(body)[:200]}"
    e = d.get("error")
    if isinstance(e, dict):
        code = e.get("code", "")
        return FRIENDLY.get(code) or f"TikTok: {e.get('message') or code} ({code or status})"
    if e:  # OAuth endpoints use a flat shape
        return FRIENDLY.get(e) or f"TikTok: {d.get('error_description') or e} ({e})"
    return f"TikTok HTTP {status}"


def plan_chunks(size):
    """(chunk_size, total_chunk_count) by TikTok's rules: count = floor(size / chunk_size),
    the remainder rides along in the last chunk; small files go in one piece."""
    if size <= SINGLE_MAX:
        return size, 1
    return CHUNK, size // CHUNK


class TikTok:
    def __init__(self, folder):
        self.path = os.path.join(folder, "tiktok_token.json")
        self.tok = {}
        self._pending = {}  # state -> (verifier, redirect_uri)
        try:
            with open(self.path, encoding="utf8") as f:
                self.tok = json.load(f)
        except (OSError, ValueError):
            pass

    def connected(self):
        return bool(self.tok.get("refresh_token"))

    def account(self):
        return dict(nickname=self.tok.get("nickname", ""), username=self.tok.get("username", ""),
                    scope=self.tok.get("scope", ""))

    def _save(self):
        with open(self.path, "w", encoding="utf8") as f:
            json.dump(self.tok, f)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def disconnect(self, client_key="", client_secret=""):
        tok = self.tok.get("access_token")
        if tok and client_key and client_secret:
            try:  # tell TikTok too; the local logout happens either way
                self._form("/v2/oauth/revoke/", dict(client_key=client_key, client_secret=client_secret, token=tok))
            except TikTokError:
                pass
        self.tok = {}
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- OAuth (Login Kit for Desktop: loopback redirect + PKCE with a HEX sha256 challenge)
    def auth_url(self, client_key, redirect_uri):
        verifier = secrets.token_urlsafe(64)[:96]
        state = secrets.token_urlsafe(16)
        self._pending[state] = (verifier, redirect_uri)
        q = dict(client_key=client_key, scope=SCOPES, redirect_uri=redirect_uri, state=state, response_type="code",
                 code_challenge=hashlib.sha256(verifier.encode()).hexdigest(), code_challenge_method="S256")
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def _form(self, path, data):
        req = urllib.request.Request(API + path, data=urllib.parse.urlencode(data).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise TikTokError(_err(e.code, e.read()))
        except OSError as e:
            raise TikTokError(f"Cannot reach TikTok: {e}")
        d = json.loads(raw or b"{}")
        if d.get("error") and d.get("error") != "ok" and not isinstance(d.get("error"), dict):
            raise TikTokError(_err(200, raw))
        return d

    def _store(self, d):
        self.tok.update(access_token=d["access_token"], refresh_token=d["refresh_token"],  # refresh tokens rotate
                        expires_at=time.time() + int(d.get("expires_in", 7200)) - 120,
                        open_id=d.get("open_id", self.tok.get("open_id", "")),
                        scope=d.get("scope", self.tok.get("scope", "")))
        self._save()

    def finish(self, code, state, client_key, client_secret):
        if state not in self._pending:
            raise TikTokError("Login session expired - click Connect TikTok again")
        verifier, redirect = self._pending.pop(state)
        d = self._form("/v2/oauth/token/", dict(client_key=client_key, client_secret=client_secret, code=code,
                                                 grant_type="authorization_code", redirect_uri=redirect,
                                                 code_verifier=verifier))
        if not d.get("refresh_token"):
            raise TikTokError("TikTok did not return a login token - try connecting again")
        self.tok = {}
        self._store(d)
        try:  # remember who is connected (shown before every post, as TikTok requires)
            info = self.creator_info(client_key, client_secret)
            self.tok.update(nickname=info.get("creator_nickname", ""), username=info.get("creator_username", ""))
            self._save()
        except TikTokError:
            pass

    def access_token(self, client_key, client_secret):
        if not self.connected():
            raise TikTokError("TikTok is not connected")
        if self.tok.get("access_token") and self.tok.get("expires_at", 0) > time.time():
            return self.tok["access_token"]
        try:
            d = self._form("/v2/oauth/token/", dict(client_key=client_key, client_secret=client_secret,
                                                     grant_type="refresh_token", refresh_token=self.tok["refresh_token"]))
        except TikTokError as e:
            if "invalid_grant" in str(e) or "expired" in str(e).lower():
                self.disconnect()
                raise TikTokError("TikTok login expired - Connect TikTok again (TikTok logins last 180 days)")
            raise
        self._store(d)
        return self.tok["access_token"]

    # -- Content Posting API
    def _api(self, client_key, client_secret, path, body):
        token = self.access_token(client_key, client_secret)
        req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else b"",
                                     method="POST", headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json; charset=UTF-8"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            raise TikTokError(_err(e.code, e.read()))
        except OSError as e:
            raise TikTokError(f"Cannot reach TikTok: {e}")
        e = d.get("error") or {}
        if e.get("code") not in (None, "", "ok"):
            raise TikTokError(_err(200, json.dumps(d)))
        return d.get("data") or {}

    def creator_info(self, client_key, client_secret):
        d = self._api(client_key, client_secret, "/v2/post/publish/creator_info/query/", None)
        if d.get("creator_nickname"):
            self.tok.update(nickname=d["creator_nickname"], username=d.get("creator_username", ""))
            self._save()
        return d

    def status(self, client_key, client_secret, publish_id):
        return self._api(client_key, client_secret, "/v2/post/publish/status/fetch/", dict(publish_id=publish_id))

    def upload(self, client_key, client_secret, path, mode, post_info=None, progress=None, cancelled=None):
        """Starts the post and sends the file. Returns publish_id (track it with status())."""
        size = os.path.getsize(path)
        chunk, count = plan_chunks(size)
        src = dict(source="FILE_UPLOAD", video_size=size, chunk_size=chunk, total_chunk_count=count)
        if mode == "direct":
            d = self._api(client_key, client_secret, "/v2/post/publish/video/init/",
                          dict(post_info=post_info or {}, source_info=src))
        else:
            d = self._api(client_key, client_secret, "/v2/post/publish/inbox/video/init/", dict(source_info=src))
        pid, url = d.get("publish_id"), d.get("upload_url")
        if not (pid and url):
            raise TikTokError("TikTok did not return an upload address")
        u = urllib.parse.urlparse(url)
        target = u.path + ("?" + u.query if u.query else "")
        with open(path, "rb") as f:
            for i in range(count):
                if cancelled and cancelled():
                    raise TikTokError("Cancelled")
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1  # last chunk takes the remainder
                f.seek(start)
                data = f.read(end - start + 1)
                for attempt in range(6):
                    c = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
                        u.netloc, timeout=180)
                    try:
                        c.request("PUT", target, data, {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                                                        "Content-Range": f"bytes {start}-{end}/{size}"})
                        r = c.getresponse()
                        st, body = r.status, r.read()
                    except (OSError, http.client.HTTPException):
                        st, body = 503, b""
                    finally:
                        c.close()
                    if st in (200, 201, 206):
                        break
                    if st >= 500 and attempt < 5:
                        time.sleep(min(2 ** (attempt + 1), 30))
                        continue
                    if st == 403:
                        raise TikTokError("TikTok upload link expired (uploads must finish within an hour) - retry")
                    raise TikTokError(f"TikTok refused the upload (HTTP {st}) {body[:160].decode('utf8', 'replace')}")
                if progress:
                    progress(int((end + 1) * 100 / size))
        return pid
