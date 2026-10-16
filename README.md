# Zookimethod — full-quality video

TikTok re-encodes everything you upload down to 1080p and 60 fps or less. This
site doesn't. It plays the **exact file it was given**: 4K stays 4K, 120 fps
stays 120 fps, and the bitrate stays high. There's no adaptive quality and no
resolution ladder, so the viewer's device has to decode every pixel of every
frame.

If their phone can't handle that, it lags for real. Nothing fake is added.
The lag comes only from the file itself, and the site tells the viewer so.

- TikTok-style vertical feed (swipe / scroll / arrow keys)
- **Upload from your phone** with a private link. Videos below 4K 120fps are
  **upscaled to 4K and interpolated to 120 fps** on your computer, then posted.
- **Stats** panel: real resolution, video fps vs. frames actually shown,
  smoothness %, dropped frames, bitrate, codec, buffer, and what an enhanced
  video was made from
- Automatic warning when the viewer's device can't keep up, kept separate from
  "your internet is too slow" buffering
- **＋ Play file**: plays a video from the viewer's own device as-is. It is
  never uploaded, so it is never compressed.

## Quick start

You need Python 3 and [ffmpeg](https://ffmpeg.org/download.html) on the
computer that hosts the site.

```bash
python3 serve.py
```

It prints three addresses:

- **This computer**: open it here.
- **Phone (same Wi-Fi)**: open it on your phone to watch.
- **Your private upload link**: open this one on your phone to get the
  **＋ Upload** button. Anyone with the link can post, so keep it to yourself.
  It isn't encrypted, so only use it on Wi-Fi you trust. Run
  `python3 serve.py --new-key` to replace it.

You can also drop files straight into `videos/`; they appear in the feed as
they are.

Use `serve.py` instead of `python3 -m http.server`. The built-in server
ignores HTTP Range requests, and iPhones won't play video without them.

## Put it on the internet

Two ways. Both keep uploads and 4K 120fps enhancement working, and both use
HTTPS, so the upload key is encrypted on the way.

### Free, from your own computer

Install Cloudflare's free `cloudflared` (`brew install cloudflared` on Mac,
`winget install --id Cloudflare.cloudflared` on Windows,
[downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
for Linux), then:

```bash
python3 serve.py --public
```

It prints a public `https://….trycloudflare.com` address that anyone can open,
plus your private upload link on that address. You need no account and no
router settings.

- **It only works while your computer is on and the command is running.**
- **The address changes every time you start it.** For a permanent address,
  set up a free
  [named Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/)
  to your own domain and start with `--public-url https://your.domain`.
- **Viewers stream from your home internet.** Each viewer watching 4K 120fps
  needs about 50 Mbps of your *upload* speed, and many home connections have
  20–50 Mbps in total. So expect buffering beyond one or two viewers.

### 24/7 on a rented server

1. Rent a Linux server with **4+ CPU cores and 8 GB of RAM**: the enhancement
   needs about 4 GB while it runs. Examples: Hetzner CPX31 or CX32, or a
   DigitalOcean 4 vCPU / 8 GB droplet. Prefer a plan with lots of included
   traffic: 4K 120fps at about 45 Mbps is about 20 GB per viewer-hour.
2. Point a domain (or a free subdomain, e.g. from DuckDNS) at the server's IP
   address.
3. On the server, install Docker, then:

   ```bash
   git clone https://github.com/bgam93756-byte/Zookimethod.git
   cd Zookimethod/deploy
   DOMAIN=videos.example.com docker compose up -d
   docker compose logs zookimethod     # shows your private upload link
   ```

Caddy gets and renews the HTTPS certificate by itself. Videos and the upload
key are kept in `deploy/.data/`, so they survive restarts and updates. To
update, run `git pull && DOMAIN=videos.example.com docker compose up -d --build`.

## Uploading and enhancing to 4K 120fps

1. Open your private upload link on your phone and tap **＋ Upload**.
2. Pick a video, add a title and caption, and leave **Enhance to 4K 120fps**
   on.
3. The video uploads in chunks. If the connection drops, tap **Resume**. After
   a reload, pick the same video again to carry on where it stopped.
4. The server processes it. You see the stage, the %, and the time left. You
   can close the sheet; the job keeps going and shows up in the feed when
   it's done.

Each enhanced video is saved twice, as `name.mp4` (HEVC) and `name.av1.mp4`
(AV1). Each viewer's browser plays the one it can decode. Videos that are
already 4K 120fps are posted bit for bit, without re-encoding.

The same engine works from the command line:

```bash
python3 tools/enhance.py clip.mp4            # -> videos/clip-4k120.mp4 + .av1.mp4
python3 tools/enhance.py clip.mp4 --fast     # repeat frames instead of building new ones
python3 tools/enhance.py clip.mp4 --codec h264
```

### How enhancement works

Every setting was measured. A true 4K 120fps clip (a camera pan over a
detailed image, plus fast-moving objects) was turned into a typical 1080p 60fps
upload. Each method then rebuilt 4K 120fps from that upload and was scored
against the original with SSIM (1.0 = identical).

**1. New in-between frames (60 → 120 fps), built at the source resolution.**

| Method | SSIM of the new frames | Speed |
|---|---|---|
| Repeat each frame | 0.614 | very fast |
| Blend neighbouring frames | 0.646 (ghosting) | very fast |
| **Motion tracking, default settings (used)** | **0.939** | 2.8 fps |
| Motion tracking, bidirectional + overlapped blocks | 0.935 | 1.5 fps |

Motion tracking is ffmpeg's `minterpolate` in `mci` mode. It is the only
method that builds real in-between motion. `--fast` repeats frames instead,
which keeps a true 120 fps stream with no ghosting, but motion looks like the
original 60 fps.

**2. Upscaling to 4K.** The short side becomes 2160 px and the aspect ratio
is kept. Lanczos scored best of the plain scalers (bilinear 0.896, bicubic
0.909, Lanczos 0.911), with a light unsharp mask on top. A
contrast-adaptive sharpener (CAS) scored no better and was 8× slower.

**3. Encoding ("compress").** One pass produces both formats from the same
frames:

| Format | Who plays it | Bitrate | SSIM | Speed at 4K 120fps |
|---|---|---|---|---|
| **HEVC**, CRF 22, level 5.2 | iPhone, iPad, Mac (hardware), Chrome/Edge with HEVC hardware | ~45 Mbps | 0.982 | 5.1 fps |
| **AV1**, SVT preset 10, CRF 40 | Chrome, Edge, Firefox, Android (software if needed) | ~38 Mbps | 0.982 | 8.6 fps |
| H.264, CRF 18 (`--codec h264` only) | widest support, but 4K 120fps needs level 6.0, beyond many phones | ~80 Mbps | 0.986 | 5.8 fps |

Both are capped at 50 Mbps. That's still about 20× TikTok's 1080p bitrate.

**What to expect:**

- **It's slow.** On a 4-core computer with no graphics card, expect about 1
  minute of processing per second of video. A 30-second clip takes about half
  an hour. Faster computers do better. Uploads are processed one at a time.
- **It needs about 4 GB of free memory** while a video is processing.
- **Upscaling can't add detail the source never had.** A 1080p video upscaled
  to 4K looks like a clean 1080p video, not like native 4K.
- **In-between frames are estimates.** Fast or complex motion can warp, and
  the last few hundredths of a second of a clip aren't interpolated.
- **HDR is kept.** iPhone HDR (10-bit HLG) stays 10-bit HLG. The motion step
  works at 8-bit precision, so smooth gradients can band slightly.
- **Rotation, variable frame rates and 59.94 fps** are handled: output is
  upright, on an exact 120 (or 119.88) fps timeline.

## How TikTok handles uploads, and what this site does differently

The upload flow copies TikTok's documented
[Content Posting API](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide):

1. `POST /api/upload/init` with the size and chunk plan returns a
   `publish_id` and an `upload_url`.
2. `PUT` chunks of 5–64 MiB with `Content-Range: bytes FIRST-LAST/TOTAL`.
   The last chunk carries the remainder, and there are at most 1000 chunks.
   Each reply says how much has arrived: 206 means more to come, 201 means
   done.
3. `GET /api/upload/<id>` polls the status. Failures carry TikTok-style
   reasons such as `file_format_check_failed`.

Unlike TikTok, chunks can arrive in any order and be re-sent, which is what
makes resuming simple. Unlike TikTok, the status also shows processing
progress.

What TikTok does after the upload (researched October 2026 from TikTok's docs
and from ffprobe on videos served by tiktok.com):

- **TikTok rejects anything above 60 fps.** Its API fails 120 fps uploads with
  `frame_rate_check_failed`.
- **It serves at most 1080p.** Every normally processed video in the sample
  topped out at 1080×1920, and many at 720p. Each video is a ladder of MP4s
  (540p–1080p, H.264 and ByteDance's HEVC), and the app picks one per viewer
  based on device and network.
- **Bitrates are very low:** about 0.2–3 Mbps for 1080p. 60 fps survives only
  on some videos' 1080p version; everything else is 30 fps.
- **The app may shrink videos on the phone before uploading** unless "Upload
  HD" / "Allow high-quality uploads" is on.

So a 4K 120fps file gains nothing on TikTok, and an upscaled one gains even
less. For the best TikTok quality, upload a native 1080p 30 or 60 fps file at
a high bitrate, with high-quality uploads turned on, over Wi-Fi.

Some paid "method" tools claim to get TikTok to skip its compression. They
appear to exploit a quirk in TikTok's processing, which likely breaks TikTok's
terms, and at least one warns that 4K uploads can get accounts banned. This
project doesn't do that. It gets full quality by hosting the videos itself.

## Preparing videos by hand (without losing quality)

```bash
tools/prep.sh ~/Movies/clip.mov          # -> videos/clip.mp4
```

By default this is a **lossless remux**: the video and audio streams are
copied bit for bit into a web-friendly MP4 with the index at the front, so
playback starts right away. Resolution, fps and bitrate are untouched.

It re-encodes only when it has to. ProRes, for example, doesn't play in
browsers, so the script then makes a near-lossless H.264 file (or 10-bit HEVC
for HDR footage) at the same resolution and frame rate. You can force that
with `tools/prep.sh --encode clip.mov`.

Start from the **original** file. Move it off your phone with AirDrop or a
cable, not a copy that went through a messaging app, which is already
compressed. On iPhone, 4K 120fps needs Settings → Camera → Formats →
High Efficiency.

## A watch-only copy on a static host

Uploads and enhancement need `serve.py` running (see above). To publish a
watch-only copy on a static host instead, you need two things:

1. **The site** (`index.html`, `style.css`, `app.js`, `upload.js`,
   `videos.json`). Any static host works: GitHub Pages, Netlify, Cloudflare
   Pages. Without `serve.py` there's no upload button.
2. **The video files**, on storage that serves files as-is and supports Range
   requests. Cloudflare R2 has free egress, which matters here; Backblaze B2
   and S3 also work. Do **not** use YouTube, Vimeo, Instagram or TikTok links.
   They re-encode, which is the whole problem.

Then list your videos in `videos.json`. Generate it from the `videos/` folder
with `python3 serve.py --manifest`, then edit `src` to point at the hosted
files:

```json
[
  {
    "src": "https://your-bucket.example.com/clip.mp4",
    "title": "Night drive",
    "caption": "4K 120fps",
    "fps": 120
  }
]
```

To give one clip in several formats, use `sources` instead of `src`. The
first format the viewer's browser can play wins. Enhanced uploads already
have this in their `name.json`:

```json
{
  "title": "Night drive",
  "sources": [
    { "src": "https://.../clip.mp4", "type": "video/mp4; codecs=\"hvc1.1.6.L156.B0\"" },
    { "src": "https://.../clip.av1.mp4", "type": "video/mp4; codecs=\"av01.0.14M.08\"" }
  ]
}
```

Re-running `--manifest` keeps the titles and captions you wrote, plus any
`https://` entries.

If you reach `serve.py` through a domain name (a tunnel, or a name like
`mypc.lan`), start it with `--allow-host that.name`. It refuses unknown names
so that a web page you visit can't read your videos through DNS rebinding.

Bandwidth is the real cost. A 50 Mbps file is about 6 MB per second of video,
for every viewer. GitHub Pages also rejects files over 100 MB, which is only
seconds of 4K 120fps, so keep videos off GitHub. `.gitignore` already
excludes `videos/` and the upload staging area.

## What viewers will actually see

- **120 fps needs a 120 Hz screen.** On a 60 Hz screen the browser can show
  at most 60 frames per second. The stats panel counts that as 100% smooth,
  because it's the screen's limit and not lag. Some browsers also cap web
  pages at 60 Hz even on 120 Hz phones.
- **The phone still decodes all of 4K**, even though its screen has fewer
  pixels. That decoding work is what makes weaker phones stutter.
- **Formats:** enhanced uploads come in HEVC and AV1, so practically every
  current browser gets one it can play. Phones without a hardware decoder for
  that format decode in software and lag heavily. A video you drop into
  `videos/` yourself plays only where its format is supported; the slide
  says so otherwise.
- **Slow connections buffer.** The site tells "this device can't keep up"
  apart from "the connection can't keep up".

Keyboard: `↑`/`↓` or `j`/`k` to move, `space` to play or pause, `m` for sound,
`s` for stats, `f` for fullscreen.
