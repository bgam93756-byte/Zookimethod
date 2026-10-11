#!/usr/bin/env python3
"""Upscale a video to 4K and interpolate it to 120 fps, then encode a web MP4.

    python3 tools/enhance.py clip.mp4                 # -> videos/clip-4k120.mp4
    python3 tools/enhance.py clip.mp4 -o out.mp4
    python3 tools/enhance.py clip.mp4 --fast          # quicker, blends frames instead of tracking motion
    python3 tools/enhance.py clip.mp4 --codec hevc    # smaller file; plays on iPhone and most Android/Chrome

What it does, in order:
  1. Interpolation to 120 fps happens at the source resolution, where it is
     ~4x cheaper than at 4K. "best" tracks motion and builds the in-between
     frames (minterpolate); "fast" blends neighbouring frames.
  2. Upscaling to 4K (short side 2160 px, aspect ratio kept) with Lanczos,
     plus light contrast-adaptive sharpening.
  3. Encoding to H.264 (plays everywhere) or HEVC, capped so the file stays
     manageable while remaining a true 4K 120fps stream.

Anything already at 4K or 120 fps keeps that part as it is; nothing is ever
scaled down. Upscaling can't recover detail the source never had, and
in-between frames are estimates, so fast, complex motion can show warping.
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

TARGET_SHORT_SIDE = 2160
TARGET_FPS = 120
WEB_VIDEO_CODECS = {"h264", "hevc", "av1", "vp9"}
WEB_AUDIO_CODECS = {"aac", "mp3"}

# Tuned against a ground-truth 4K120 clip; see README "How enhancement works".
INTERPOLATE = {
    "best": "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
    "fast": "framerate=fps={fps}",
}
SCALE = "scale={w}:{h}:flags=lanczos+accurate_rnd+full_chroma_int"
SHARPEN = "cas=strength=0.4"
ENCODERS = {
    # rate caps keep a 4K120 upscale from ballooning: the source detail doesn't need more
    "h264": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "60M", "-bufsize", "120M",
             "-profile:v", "high", "-pix_fmt", "yuv420p"],
    "hevc": ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
             "-x265-params", "vbv-maxrate=40000:vbv-bufsize=80000:log-level=error"],
    "hevc10": ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-tag:v", "hvc1", "-pix_fmt", "yuv420p10le",
               "-x265-params", "vbv-maxrate=40000:vbv-bufsize=80000:log-level=error"],
}
CAP_BITRATE = {"h264": 60e6, "hevc": 40e6, "hevc10": 40e6}


class EnhanceError(Exception):
    """A problem worth showing to the person who uploaded the video."""


class Cancelled(EnhanceError):
    pass


# ---------- probing ----------

def _ratio(text):
    num, _, den = (text or "0/0").partition("/")
    try:
        num, den = float(num), float(den or 1)
    except ValueError:
        return 0.0
    return num / den if den else 0.0


def probe(path):
    """Describe the video as it will be displayed (after any rotation)."""
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise EnhanceError("ffmpeg isn't installed on the server, so videos can't be processed.")
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise EnhanceError("That file isn't a video ffmpeg can read.")

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not video.get("width"):
        raise EnhanceError("That file doesn't contain a video track.")

    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(round(float(side["rotation"]))) % 360
    width, height = video["width"], video["height"]
    if rotation in (90, 270):
        width, height = height, width

    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    if fps <= 0 or duration <= 0:
        raise EnhanceError("Couldn't work out the video's frame rate or length.")

    pix_fmt = video.get("pix_fmt") or ""
    transfer = video.get("color_transfer") or ""
    hdr = {"smpte2084": "pq", "arib-std-b67": "hlg"}.get(transfer)
    return {
        "width": width,
        "height": height,
        "rotation": rotation,
        "fps": fps,
        "duration": duration,
        "codec": video.get("codec_name"),
        "pix_fmt": pix_fmt,
        "ten_bit": "10" in pix_fmt or "12" in pix_fmt,
        "hdr": hdr,
        "color": {k: video.get(k) for k in ("color_primaries", "color_transfer", "color_space") if video.get(k)},
        "audio_codec": audio.get("codec_name") if audio else None,
        "bitrate": int((data.get("format") or {}).get("bit_rate") or 0),
    }


def label(width, height, fps):
    short = min(width, height)
    res = "8K" if short >= 4320 else "4K" if short >= 2160 else f"{short}p"
    rate = round(fps)
    return f"{res} {rate}fps" if short >= 2160 else f"{res}{rate}"


# ---------- planning ----------

def target_size(width, height):
    """Scale so the short side is 2160 px, keeping the aspect ratio and even dimensions."""
    short = min(width, height)
    if short >= TARGET_SHORT_SIDE:
        return width, height
    factor = TARGET_SHORT_SIDE / short
    w = int(round(width * factor / 2)) * 2
    h = int(round(height * factor / 2)) * 2
    return w, h


def target_fps(fps):
    if fps >= TARGET_FPS - 1:
        return None  # already 120 (or 119.88)
    # keep NTSC-style rates in their family so frames line up exactly: 59.94 -> 119.88
    ntsc = abs(fps * 1.001 - round(fps * 1.001)) < 0.01 and abs(fps - round(fps)) > 0.01
    return "120000/1001" if ntsc else str(TARGET_FPS)


def make_plan(info, enhance=True, quality="best", codec="h264"):
    if quality not in INTERPOLATE:
        raise EnhanceError(f"Unknown quality {quality!r}.")
    if codec not in ("h264", "hevc"):
        raise EnhanceError(f"Unknown codec {codec!r}.")

    out_w, out_h = target_size(info["width"], info["height"]) if enhance else (info["width"], info["height"])
    new_fps = target_fps(info["fps"]) if enhance else None
    upscale = (out_w, out_h) != (info["width"], info["height"])
    out_fps = _ratio(new_fps) if new_fps else info["fps"]
    playable = info["codec"] in WEB_VIDEO_CODECS

    # HDR and 10-bit sources stay 10-bit HEVC: 8-bit H.264 would wash out the colours
    if info["ten_bit"] or info["hdr"]:
        codec = "hevc10"

    filters = []
    if new_fps:
        filters.append(INTERPOLATE[quality].format(fps=new_fps))
    if upscale:
        filters.append(SCALE.format(w=out_w, h=out_h))
        filters.append(SHARPEN)

    source_label = label(info["width"], info["height"], info["fps"])
    target_label = label(out_w, out_h, out_fps)
    if upscale and new_fps:
        summary = "Enhancing to 4K 120fps"
    elif upscale:
        summary = "Upscaling to 4K"
    elif new_fps:
        summary = "Adding frames for 120fps"
    else:
        summary = "Preparing for the web" if not playable else "Optimizing for streaming"

    args = []
    if filters or not playable:
        args += ["-vf", ",".join(filters)] if filters else []
        args += ENCODERS[codec]
        # carry the colour tags over, so HDR and wide-gamut video isn't shown with the wrong colours
        for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"), ("-colorspace", "color_space")):
            if info["color"].get(key):
                args += [flag, info["color"][key]]
        bitrate = CAP_BITRATE[codec]
    else:
        # already 4K 120 (or enhancement off) and web-playable: copy the stream bit for bit
        args += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if info["codec"] == "hevc" else [])
        bitrate = info["bitrate"] or CAP_BITRATE["h264"]

    if info["audio_codec"] in WEB_AUDIO_CODECS:
        args += ["-c:a", "copy"]
    elif info["audio_codec"]:
        args += ["-c:a", "aac", "-b:a", "256k"]

    return {
        "args": args,
        "enhanced": bool(filters),
        "copy": "-c:v" in args and args[args.index("-c:v") + 1] == "copy",
        "summary": summary,
        "source_label": source_label,
        "target_label": target_label,
        "width": out_w,
        "height": out_h,
        "fps": out_fps,
        "duration": info["duration"],
        "estimated_bytes": int(info["duration"] * bitrate / 8),
    }


# ---------- running ----------

def run(src, dst, plan, on_progress=None, cancel=None):
    """Run ffmpeg, reporting (fraction, eta_seconds, speed) as it goes."""
    dst = Path(dst)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?", *plan["args"],
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(dst)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    errors = []
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()
    if cancel is not None:
        def stop_on_cancel():
            while proc.poll() is None:
                if cancel.wait(0.5):
                    proc.kill()  # SIGTERM makes ffmpeg flush its encoder first, which takes seconds at 4K
                    return
        threading.Thread(target=stop_on_cancel, daemon=True).start()

    started = time.monotonic()
    total = max(plan["duration"], 0.001)
    last_report = 0.0
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and on_progress:
                fraction = min(int(value) / 1e6 / total, 0.999)
                elapsed = time.monotonic() - started
                now = time.monotonic()
                if fraction > 0 and now - last_report >= 0.5:
                    last_report = now
                    eta = elapsed * (1 - fraction) / fraction
                    on_progress(fraction, round(eta), round(total * fraction / elapsed, 3))
        proc.wait()
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    except BaseException:
        proc.kill()
        proc.wait()
        dst.unlink(missing_ok=True)
        raise
    reader.join(timeout=5)
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        detail = "".join(errors).strip().splitlines()[-3:]
        raise EnhanceError("ffmpeg couldn't process this video" + (f": {' / '.join(detail)}" if detail else "."))
    if on_progress:
        on_progress(1.0, 0, None)


def make_poster(video, jpg, at=1.0):
    """Save a 1080-px-wide JPEG frame to show before the video loads."""
    for seek in (at, 0):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", str(seek), "-i", str(video),
               "-frames:v", "1", "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(jpg)]
        if subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0 and Path(jpg).is_file():
            return
    raise EnhanceError("Couldn't make a poster frame.")


# ---------- command line ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input")
    parser.add_argument("-o", "--output", help="default: videos/<name>-4k120.mp4")
    parser.add_argument("--fast", action="store_true", help="blend frames instead of tracking motion (much quicker)")
    parser.add_argument("--codec", choices=("h264", "hevc"), default="h264")
    args = parser.parse_args()

    try:
        info = probe(args.input)
        plan = make_plan(info, quality="fast" if args.fast else "best", codec=args.codec)
    except EnhanceError as e:
        sys.exit(f"Error: {e}")

    out = Path(args.output) if args.output else Path(__file__).resolve().parent.parent / "videos" / f"{Path(args.input).stem}-4k120.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"{plan['summary']}  ({info['duration']:.1f} s)")

    def show(fraction, eta, speed):
        bar = "#" * int(fraction * 30)
        eta_text = f"{eta // 60}:{eta % 60:02d} left" if eta else ""
        print(f"\r  [{bar:<30}] {fraction * 100:5.1f}%  {eta_text:<12}", end="", flush=True)

    try:
        run(args.input, out, plan, on_progress=show)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    except EnhanceError as e:
        sys.exit(f"\nError: {e}")
    size = out.stat().st_size
    print(f"\nSaved {out}  ({size / 1e6:.0f} MB, {size * 8 / max(info['duration'], 0.001) / 1e6:.0f} Mbps)")


if __name__ == "__main__":
    main()
