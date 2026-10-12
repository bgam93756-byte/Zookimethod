#!/usr/bin/env python3
"""Upscale a video to 4K and interpolate it to 120 fps, then encode web MP4s.

    python3 tools/enhance.py clip.mp4               # -> videos/clip-4k120.mp4 (HEVC) + .av1.mp4
    python3 tools/enhance.py clip.mp4 --fast        # repeat frames instead of building new ones
    python3 tools/enhance.py clip.mp4 --codec h264  # one H.264 file instead

What it does, in order (settings measured against a true 4K 120fps clip;
see the README's "How enhancement works"):
  1. New in-between frames for 120 fps, built at the source resolution,
     where it is ~4x cheaper than at 4K. "best" tracks motion
     (minterpolate mci); "fast" repeats each frame, so motion looks like
     the original but the file is still a real 120 fps stream.
  2. Upscaling to 4K (short side 2160 px, aspect ratio kept) with Lanczos
     and a light unsharp mask.
  3. Encoding, in one pass, to HEVC (what iPhones and Macs decode in
     hardware) and AV1 (Chrome, Firefox, Android). The player picks the
     one the viewer's device can play. Bitrate is capped at 50 Mbps each.

Anything already at 4K or 120 fps keeps that part as it is; nothing is ever
scaled down. Upscaling can't recover detail the source never had, and
in-between frames are estimates, so fast, complex motion can warp.
"""

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

TARGET_SHORT_SIDE = 2160
TARGET_FPS = 120
# Only real video containers. Playlist formats (HLS, DASH, concat) would let an
# uploaded text file make ffmpeg read other files on this computer.
INPUT_FORMATS = "mov,matroska,webm"
WEB_VIDEO_CODECS = {"h264", "hevc", "av1", "vp9"}
WEB_AUDIO_CODECS = {"aac", "mp3"}
DEFAULT_CODECS = ("hevc", "av1")

INTERPOLATE = {
    "best": "minterpolate=fps={fps}:mi_mode=mci",  # best score and 2x faster than bidir/aobmc variants
    "fast": "fps={fps}",
}
SCALE = "scale={w}:{h}:flags=lanczos+accurate_rnd+full_chroma_int"
SHARPEN = "unsharp=3:3:0.5:3:3:0.0"  # as good as CAS on the metrics, at a quarter of the cost

ENCODERS = {
    "hevc": {
        "args": ["-c:v", "libx265", "-preset", "fast", "-crf", "22", "-tag:v", "hvc1",
                 "-x265-params", "vbv-maxrate=50000:vbv-bufsize=100000:log-level=error"],
        "type": 'video/mp4; codecs="hvc1.1.6.L156.B0"',   # Main, level 5.2: covers 4K 120fps
        "type10": 'video/mp4; codecs="hvc1.2.4.L156.B0"',  # Main 10
        "suffix": "",
        "cap": 50e6,
    },
    "av1": {
        "args": ["-c:v", "libsvtav1", "-preset", "10", "-crf", "40", "-maxrate", "50M", "-bufsize", "100M"],
        "type": 'video/mp4; codecs="av01.0.14M.08"',  # Main, level 5.2
        "type10": 'video/mp4; codecs="av01.0.14M.10"',
        "suffix": ".av1",
        "cap": 50e6,
    },
    "h264": {
        "args": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "60M", "-bufsize", "120M",
                 "-profile:v", "high"],
        "type": 'video/mp4; codecs="avc1.64003C"',  # High, level 6.0: many phones can't decode this
        "suffix": ".h264",
        "cap": 60e6,
    },
}


class EnhanceError(Exception):
    """A problem worth showing to the person who uploaded the video.

    code is a TikTok-style reason such as file_format_check_failed.
    """

    def __init__(self, message="", code="internal"):
        super().__init__(message)
        self.code = code


class Cancelled(EnhanceError):
    pass


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


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
    if not available():
        raise EnhanceError("ffmpeg isn't installed on the server, so videos can't be processed.")
    cmd = ["ffprobe", "-v", "error", "-format_whitelist", INPUT_FORMATS,
           "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise EnhanceError("That file isn't a video this server can read. Upload an MP4, MOV, MKV or WebM file.", "file_format_check_failed")

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not video.get("width"):
        raise EnhanceError("That file doesn't contain a video track.", "file_format_check_failed")

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
        raise EnhanceError("Couldn't work out the video's frame rate or length.", "file_format_check_failed")

    pix_fmt = video.get("pix_fmt") or ""
    transfer = video.get("color_transfer") or ""
    return {
        "width": width,
        "height": height,
        "rotation": rotation,
        "fps": fps,
        "duration": duration,
        "codec": video.get("codec_name"),
        "pix_fmt": pix_fmt,
        "ten_bit": "10" in pix_fmt or "12" in pix_fmt,
        "hdr": {"smpte2084": "pq", "arib-std-b67": "hlg"}.get(transfer),
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


def make_plan(info, enhance=True, quality="best", codecs=DEFAULT_CODECS):
    if quality not in INTERPOLATE:
        raise EnhanceError(f"Unknown quality {quality!r}.")
    codecs = list(dict.fromkeys(codecs))
    if not codecs or any(c not in ENCODERS for c in codecs):
        raise EnhanceError(f"Unknown codec in {codecs!r}.")

    out_w, out_h = target_size(info["width"], info["height"]) if enhance else (info["width"], info["height"])
    new_fps = target_fps(info["fps"]) if enhance else None
    upscale = (out_w, out_h) != (info["width"], info["height"])
    out_fps = _ratio(new_fps) if new_fps else info["fps"]
    playable = info["codec"] in WEB_VIDEO_CODECS
    deep = info["ten_bit"] or bool(info["hdr"])
    if deep and codecs == ["h264"]:
        codecs = ["hevc"]  # 10-bit H.264 barely plays anywhere; 8-bit would wash out HDR

    filters = []
    if new_fps:
        filters.append(INTERPOLATE[quality].format(fps=new_fps))
    if upscale:
        filters += [SCALE.format(w=out_w, h=out_h), SHARPEN]

    if upscale and new_fps:
        summary = "Enhancing to 4K 120fps"
    elif upscale:
        summary = "Upscaling to 4K"
    elif new_fps:
        summary = "Adding frames for 120fps"
    else:
        summary = "Optimizing for streaming" if playable else "Preparing for the web"

    if filters or not playable:
        # motion interpolation works in 8 bits; 10-bit/HDR sources go back to 10 bits after it
        filters.append("format=yuv420p10le" if deep else "format=yuv420p")
        color = []
        for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"), ("-colorspace", "color_space")):
            if info["color"].get(key):
                color += [flag, info["color"][key]]
        outputs = []
        for codec in codecs:
            enc = ENCODERS[codec]
            if deep and codec == "h264":
                continue
            outputs.append({
                "codec": codec,
                "args": enc["args"] + color,
                "type": enc["type10"] if deep else enc["type"],
                "suffix": enc["suffix"],
                "cap": enc["cap"],
            })
    else:
        # already 4K 120 (or enhancement off) and web-playable: copy the stream bit for bit
        outputs = [{
            "codec": info["codec"],
            "args": ["-c:v", "copy"] + (["-tag:v", "hvc1"] if info["codec"] == "hevc" else []),
            "type": None,
            "suffix": "",
            "cap": info["bitrate"] or 60e6,
        }]
        filters = []

    if info["audio_codec"] in WEB_AUDIO_CODECS:
        audio = ["-c:a", "copy"]
    elif info["audio_codec"]:
        audio = ["-c:a", "aac", "-b:a", "256k"]
    else:
        audio = []

    return {
        "filters": ",".join(filters),
        "outputs": outputs,
        "audio": audio,
        "enhanced": bool(new_fps or upscale),
        "summary": summary,
        "source_label": label(info["width"], info["height"], info["fps"]),
        "target_label": label(out_w, out_h, out_fps),
        "width": out_w,
        "height": out_h,
        "fps": out_fps,
        "duration": info["duration"],
        "estimated_bytes": int(info["duration"] * sum(o["cap"] for o in outputs) / 8),
    }


# ---------- running ----------

def build_command(src, dsts, plan):
    """One ffmpeg run: decode and filter once, encode every output from it."""
    if len(dsts) != len(plan["outputs"]):
        raise ValueError("one destination per planned output")
    # -t and -fs keep a file that lies about its length from filling the disk
    limit_bytes = int(plan["estimated_bytes"] * 1.2) + 200 * 1024 * 1024
    limits = ["-t", f"{plan['duration'] + 1:.3f}", "-fs", str(limit_bytes)]
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", "-loglevel", "error",
           "-format_whitelist", INPUT_FORMATS, "-i", str(src)]
    if plan["filters"]:
        n = len(dsts)
        labels = "".join(f"[o{i}]" for i in range(n))
        tail = f",split={n}{labels}" if n > 1 else "[o0]"
        cmd += ["-filter_complex", f"[0:v:0]{plan['filters']}{tail}"]
        video_maps = [["-map", f"[o{i}]"] for i in range(n)]
    else:
        video_maps = [["-map", "0:v:0"] for _ in dsts]
    for dst, out, vmap in zip(dsts, plan["outputs"], video_maps):
        cmd += [*vmap, "-map", "0:a:0?", *out["args"], *plan["audio"], *limits, "-movflags", "+faststart", str(dst)]
    return cmd


def run(src, dsts, plan, on_progress=None, cancel=None):
    """Run ffmpeg, reporting (fraction, eta_seconds, speed) as it goes."""
    dsts = [Path(d) for d in dsts]
    cmd = build_command(src, dsts, plan)
    env = {**os.environ, "SVT_LOG": "1"}  # SVT-AV1: errors only
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", env=env)
    errors = collections.deque(maxlen=50)  # only the last lines matter; a broken file can print millions
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()
    if cancel is not None:
        def stop_on_cancel():
            while proc.poll() is None:
                if cancel.wait(0.5):
                    proc.kill()  # SIGTERM makes ffmpeg flush its encoders first, which takes seconds at 4K
                    return
        threading.Thread(target=stop_on_cancel, daemon=True).start()

    def remove_outputs():
        for dst in dsts:
            dst.unlink(missing_ok=True)

    started = time.monotonic()
    total = max(plan["duration"], 0.001)
    last_report = 0.0
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and on_progress:
                fraction = min(int(value) / 1e6 / total, 0.999)
                now = time.monotonic()
                elapsed = now - started
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
        remove_outputs()
        raise
    reader.join(timeout=5)
    if proc.returncode != 0:
        remove_outputs()
        # the details go to the server's terminal, not to the uploader
        print(f"ffmpeg failed on {src}:\n" + "".join(errors), file=sys.stderr)
        raise EnhanceError("ffmpeg couldn't process this video. It may be damaged or use an unusual format.", "transcode_failed")
    if on_progress:
        on_progress(1.0, 0, None)


def sources(plan, srcs):
    """The manifest's "sources" list: one entry per encode, in the order the player should try them."""
    return [{"src": src, "type": out["type"], "codec": out["codec"]}
            for src, out in zip(srcs, plan["outputs"]) if out["type"]]


def make_poster(video, jpg, at=1.0):
    """Save a 1080-px-wide JPEG frame to show before the video loads."""
    for seek in (at, 0):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-format_whitelist", INPUT_FORMATS,
               "-ss", str(seek), "-i", str(video),
               "-frames:v", "1", "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(jpg)]
        if subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0 and Path(jpg).is_file():
            return
    raise EnhanceError("Couldn't make a poster frame.")


# ---------- command line ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input")
    parser.add_argument("-o", "--output", help="main output file (default: videos/<name>-4k120.mp4)")
    parser.add_argument("--fast", action="store_true", help="repeat frames instead of building new ones (much quicker)")
    parser.add_argument("--codec", choices=("both", "hevc", "av1", "h264"), default="both",
                        help="both = HEVC + AV1 (default), so every device gets one it can play")
    args = parser.parse_args()

    codecs = DEFAULT_CODECS if args.codec == "both" else (args.codec,)
    try:
        info = probe(args.input)
        plan = make_plan(info, quality="fast" if args.fast else "best", codecs=codecs)
    except EnhanceError as e:
        sys.exit(f"Error: {e}")

    main_out = Path(args.output) if args.output else Path(__file__).resolve().parent.parent / "videos" / f"{Path(args.input).stem}-4k120.mp4"
    main_out.parent.mkdir(parents=True, exist_ok=True)
    stem = main_out.name[: -len(main_out.suffix)] if main_out.suffix else main_out.name
    dsts = [main_out.with_name(f"{stem}{o['suffix']}{main_out.suffix or '.mp4'}") for o in plan["outputs"]]
    print(f"{plan['summary']}: {plan['source_label']} → {plan['target_label']}  ({info['duration']:.1f} s)")

    def show(fraction, eta, speed):
        bar = "#" * int(fraction * 30)
        eta_text = f"{eta // 60}:{eta % 60:02d} left" if eta else ""
        print(f"\r  [{bar:<30}] {fraction * 100:5.1f}%  {eta_text:<12}", end="", flush=True)

    try:
        run(args.input, dsts, plan, on_progress=show)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    except EnhanceError as e:
        sys.exit(f"\nError: {e}")
    print()
    for dst, out in zip(dsts, plan["outputs"]):
        size = dst.stat().st_size
        print(f"Saved {dst}  ({out['codec']}, {size / 1e6:.0f} MB, {size * 8 / max(info['duration'], 0.001) / 1e6:.0f} Mbps)")
    # a sidecar groups the encodes, so serve.py lists them as one video and the player picks per device
    if len(dsts) > 1:
        rel = [f"videos/{d.name}" if d.parent.name == "videos" else d.name for d in dsts]
        meta = {"title": Path(args.input).stem, "sources": sources(plan, rel),
                "enhanced_from": plan["source_label"] if plan["enhanced"] else None}
        main_out.with_suffix(".json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
