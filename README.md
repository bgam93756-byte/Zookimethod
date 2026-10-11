# Zookimethod — full-quality video

TikTok re-encodes everything you upload down to 1080p 60fps. This site doesn't.
It plays the **exact file you recorded**: 4K stays 4K, 120 fps stays 120 fps,
and the bitrate stays whatever your camera wrote. There's no transcoding, no
adaptive quality and no resolution ladder, so the viewer's device has to decode
every pixel of every frame.

If their phone can't handle that, it lags for real. Nothing fake is added.
The lag comes only from the file itself, and the site tells the viewer so.

- TikTok-style vertical feed (swipe / scroll / arrow keys)
- **Stats** panel: real resolution, video fps vs. frames actually shown,
  smoothness %, dropped frames, bitrate, codec and buffer
- Automatic warning when the viewer's device can't keep up, kept separate from
  "your internet is too slow" buffering
- **＋ Play file**: plays a video from the viewer's own device as-is. It is
  never uploaded, so it is never compressed.

## Quick start

```bash
cp ~/path/to/your-4k120-clip.mp4 videos/
python3 serve.py
```

Open the address it prints. To watch on your phone, join the same Wi-Fi and
open the "Phone" address. Python 3 is all you need. With ffmpeg installed, the
server also reads resolution, fps, codec and bitrate from each file.

Use `serve.py` instead of `python3 -m http.server`. The built-in server
ignores HTTP Range requests, and iPhones won't play video without them.

## Preparing videos (without losing quality)

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

## Putting it online

You need two things:

1. **The site** (`index.html`, `style.css`, `app.js`, `videos.json`). Any
   static host works: GitHub Pages, Netlify, Cloudflare Pages.
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
    "caption": "4K 120fps, straight off the camera",
    "fps": 120
  }
]
```

To give one clip in several formats, use `sources` instead of `src`. The
first format the viewer's browser can play wins:

```json
{
  "title": "Night drive",
  "sources": [
    { "src": "https://.../clip-hevc.mp4", "type": "video/mp4; codecs=\"hvc1\"" },
    { "src": "https://.../clip-h264.mp4", "type": "video/mp4; codecs=\"avc1.640033\"" }
  ]
}
```

Re-running `--manifest` keeps the titles and captions you wrote, plus any
`https://` entries.

Bandwidth is the real cost. A 150 Mbps file is about 19 MB per second of
video, for every viewer. GitHub Pages also rejects files over 100 MB, which is
only a few seconds of 4K 120fps, so keep videos off GitHub. `.gitignore`
already excludes `videos/`.

## What viewers will actually see

- **120 fps needs a 120 Hz screen.** On a 60 Hz screen the browser can show
  at most 60 frames per second. The stats panel counts that as 100% smooth,
  because it's the screen's limit and not lag. Some browsers also cap web
  pages at 60 Hz even on 120 Hz phones.
- **The phone still decodes all of 4K**, even though its screen has fewer
  pixels. That decoding work is what makes weaker phones stutter.
- **Codecs:** H.264 plays in every browser, but 4K 120fps H.264 is beyond many
  phones' hardware decoders, so those phones stutter or fall back to software
  decoding. HEVC, the iPhone default, plays in Safari and in Chrome or Edge on
  hardware that supports it, but not reliably in Firefox. AV1 needs a recent
  device. Some older phones won't play 4K 120fps at all; the slide then says
  the format isn't supported.
- **Slow connections buffer.** The site tells "this device can't keep up"
  apart from "the connection can't keep up".

Keyboard: `↑`/`↓` or `j`/`k` to move, `space` to play or pause, `m` for sound,
`s` for stats, `f` for fullscreen.
