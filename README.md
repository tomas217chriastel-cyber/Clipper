# AutoClipper — desktop app

Hands-free clipper for **Twitch, Kick and YouTube** live streams. Paste channel links, leave the app open.
It waits for the stream to go live, records a rolling buffer, watches live chat, and when chat explodes
(XD / KEKW / LUL / LMAO / 💀 spam) **or** the entertainment meter jumps, it cuts **30s before the spike + 12s after**,
adds subtitles and saves an MP4. You only upload the finished clips to YouTube.

**Entertainment meter (0–100)** — the cyan line on each channel. It mixes chat speed vs. normal for that stream,
how many *different* people are talking, how many messages are reactions (CAPS, `!!!`, `???`, emoji, hype words)
and how suddenly loud the stream audio gets. This catches moments where chat goes crazy without spamming XD.
In **Settings → Clip when…** choose hype spam, entertainment meter, or either (default), and set the trigger level.
The window updates live (pushed by the app twice a second, no refresh needed).

**What's new in this version**
- **Every detected moment becomes a clip.** Footage is indexed to the exact second (ffmpeg writes the real
  length of every recorded piece), copied the moment the after-part is recorded, and kept even if the stream
  drops or restarts. Clips render one at a time with automatic fallbacks (other encoder, simpler filters),
  and each trigger line on the graph gets a dot: yellow = in progress, green = saved, grey = failed. The
  list under each channel shows what happened to every trigger, and why if it failed.
- **Sharper picture, correct proportions, no blur.** *Settings → Picture in the clip*: fill the frame
  (crop the sides), bigger picture (4:3 trim + black bars), whole picture (black bars), or the old blurred
  background. The video is never stretched, is scaled with a sharp filter, and the app now records 1080p
  when the stream offers it.
- **Better subtitles.** More accurate Whisper settings (fewer made-up/repeated words, pick the spoken
  language), Shorts-style word highlight, 6 bundled bold sans-serif fonts (Montserrat, Poppins, Archivo
  Black, Anton, Bebas Neue), 16 colours + any custom colour for text, highlight and outline, 4 sizes,
  4 positions, and a live preview.
- **See who is online immediately.** A quick live check (one request for all Twitch channels) shows within
  a second or two who is live, how many are watching, what game/category and the stream title - also for
  paused channels. Live channels move to the top; *↻ Check now* re-checks on demand.
- **Rename channels.** Give every channel your own name (when adding it, or *✏️ Rename* / double-click
  the name). The name is used in the app, in clip file names and in suggested titles.
- Clips tab loads faster (thumbnails instead of loading every video).

The finished app is **self-contained**: Python, ffmpeg, streamlink, yt-dlp, chat clients and the Whisper
subtitle model are all bundled. Nothing to install on the machine that runs it.

## Get the app (pick one)

### A) No installs at all — build it on GitHub (easiest)
1. Create a free GitHub account, make a new **private** repository, upload everything from this folder
   (including the hidden `.github` folder).
2. Open the repo → **Actions** → *Build AutoClipper apps* → **Run workflow**.
3. After ~15–25 min download the artifact for your system: `AutoClipper-windows-…zip`,
   `AutoClipper-macos-…zip` or `AutoClipper-linux-…tar.gz`.

### B) Build on your own PC
Install Python 3.11 or 3.12 (only needed for building), then:
- Windows: double-click `build_windows.bat`
- Mac / Linux: `./build_mac_linux.sh`

Output is in `dist/`. Add `--model base` for a smaller app (less accurate subtitles) or `--model none`
to skip bundling the model (it downloads on first use). Build needs internet; the app itself does not
need installs.

### Run from source (developers)
`pip install -r requirements.txt` then `python app.py`

## Using it
- **Windows:** unzip, run `AutoClipper.exe` (SmartScreen: *More info → Run anyway*, the app is unsigned).
- **macOS:** unzip, right-click `AutoClipper.app` → *Open* the first time.
- **Linux:** extract, run `./AutoClipper/AutoClipper` (opens in your browser).

Then: **Channels** → paste a link → *Add channel*. Done. Keep the app open (minimise is fine);
closing it stops clipping. Clips show up in the **Clips** tab with a player, engagement graph, suggested
title, *Copy title*, *Show file* and an *uploaded* checkbox. Files are saved in `~/AutoClipper/clips`
(change it in Settings). Sensitivity too high/low? Change it in **Settings**.

Optional: `AutoClipper --headless https://twitch.tv/x` runs without a window (UI still at http://127.0.0.1:8765).

## YouTube upload + schedule

Link your channel once, then upload straight from the **Clips** tab (button **▶ YouTube**): you write the
title, description, tags, category, visibility, and either upload right away or pick a **date & time** to go live.
Uploads run in the background; the **YouTube** tab shows the queue (progress, retry, cancel, open in Studio).
Scheduled clips are uploaded immediately as *private* and YouTube itself publishes them at your chosen time,
so the app does not need to be running at that moment. After a successful upload the clip is ticked "uploaded".

**One-time setup (~10 min)** — Google makes every app use its own free OAuth client:
1. https://console.cloud.google.com → create a project.
2. *APIs & Services → Library* → enable **YouTube Data API v3**.
3. *OAuth consent screen*: user type **External**, app name + your email, add your Google account under
   **Test users**, then click **Publish app** (otherwise the login expires every 7 days).
4. *Credentials → Create credentials → OAuth client ID* → type **Desktop app**.
5. In AutoClipper → **YouTube** tab: paste Client ID + secret → **Save & connect YouTube** → log in.
   Google shows "hasn't verified this app": click *Advanced → Go to … (unsafe)*. It's your own app.

Good to know:
- Google's changelog says uploads from unverified projects are no longer forced to private, but some older
  docs still say so. Test with one clip; if it stays private after publish time, set it public in YouTube Studio
  or request Google's API audit (form: support.google.com/youtube/contact/yt_api_form).
- Default API allowance is about 100 uploads/day per project, and your channel has its own daily upload limit.
- The app stores your Client ID/secret and login token only on your PC (`config.json`, `youtube_token.json` in the app data folder).
- Uploads are marked "not made for kids". Check YouTube's reused-content/monetization rules for clips.

## TikTok

Connect once in the **TikTok** tab, then press **♪ TikTok** on a clip. Two ways:
- **Send to my TikTok drafts** (default, works right away): the clip lands in your TikTok inbox; open the
  notification in the TikTok app, check it and post. TikTok allows 5 unfinished drafts per 24 hours.
- **Post directly**: posts without opening TikTok. Until TikTok reviews (audits) your developer app, direct posts
  can only be "Only me" and the TikTok account must be private.

One-time setup (TikTok requires your own free developer app):
1. https://developers.tiktok.com → *Manage apps* → *Connect an app*, platform **Desktop**.
2. Add **Login Kit** and **Content Posting API** (enable *Direct Post* for direct posting).
3. Login Kit redirect URI: `http://127.0.0.1:*/tiktok/callback/`
4. Add your TikTok account as a sandbox target user (or submit the app for review), copy **Client key** + **Client secret**.
5. AutoClipper → **TikTok** tab → paste both → **Save & connect TikTok** → log in.

Logins last 180 days. Keys and the login token stay on your PC (`config.json`, `tiktok_token.json`).

## Faster rendering

*Settings → Advanced → Clips rendered at the same time* (default 2). The **Auto** video encoder uses the
graphics card (NVIDIA NVENC / Intel QSV / AMD AMF) when it works and falls back to the CPU otherwise; the
setting shows what it picked. NVIDIA needs a recent driver.

## Notes
- Kick uses Kick's unofficial chat/API; if Kick changes it, Kick chat may need an update.
- Subtitles run on your CPU/GPU; with `small`, a 40s clip takes roughly 20–60s on a normal PC.
- At start the app self-tests its ffmpeg; if the log says no working ffmpeg was found, set
  `AUTOCLIPPER_FFMPEG` to an ffmpeg path.
- Only clip streamers who allow it, and check YouTube's reused-content rules before monetizing.
