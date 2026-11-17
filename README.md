# AutoClipper — desktop app

Hands-free clipper for **Twitch, Kick and YouTube** live streams. Paste channel links, leave the app open.
It waits for the stream to go live, records a rolling buffer, watches live chat, and when chat explodes
(XD / KEKW / LUL / LMAO / 💀 spam) it cuts **30s before the spike + 12s after**, adds subtitles and saves an MP4.
You only upload the finished clips to YouTube.

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

## Notes
- Kick uses Kick's unofficial chat/API; if Kick changes it, Kick chat may need an update.
- Subtitles run on your CPU/GPU; with `small`, a 40s clip takes roughly 20–60s on a normal PC.
- At start the app self-tests its ffmpeg; if the log says no working ffmpeg was found, set
  `AUTOCLIPPER_FFMPEG` to an ffmpeg path.
- Only clip streamers who allow it, and check YouTube's reused-content rules before monetizing.
