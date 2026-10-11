#!/usr/bin/env python3
"""Serve the site with full-quality video streaming.

    python3 serve.py              # serve on port 8000, open the printed address on your phone
    python3 serve.py --port 9000
    python3 serve.py --manifest   # write videos.json for a static host, then exit

Video files are sent byte-for-byte as they are on disk. Nothing is
re-encoded, resized or capped.

Python's built-in http.server ignores HTTP Range requests, which Safari
(every iPhone browser) requires before it will play a video, and which every
browser uses to seek. This server answers them.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_DIR = ROOT / "videos"
MANIFEST = ROOT / "videos.json"
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
CHUNK = 1024 * 1024


@lru_cache(maxsize=256)
def probe(path, mtime, size):
    """Read resolution/fps/codec/bitrate with ffprobe, if it's installed."""
    if not shutil.which("ffprobe"):
        return {}
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate:format=duration,bit_rate",
        "-of", "json", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}

    info = {}
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    if stream.get("width"):
        info["width"], info["height"] = stream["width"], stream["height"]
    if stream.get("codec_name"):
        info["codec"] = stream["codec_name"]
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        if den and float(den) > 0 and float(num) > 0:
            info["fps"] = round(float(num) / float(den), 3)
            break
    if fmt.get("duration"):
        info["duration"] = round(float(fmt["duration"]), 3)
    if fmt.get("bit_rate"):
        info["bitrate"] = int(fmt["bit_rate"])
    return info


def build_manifest():
    """List videos/ newest first, keeping any titles/captions already in videos.json."""
    existing = {}
    if MANIFEST.is_file():
        try:
            data = json.loads(MANIFEST.read_text())
            entries = data if isinstance(data, list) else data.get("videos", [])
            existing = {e.get("src"): e for e in entries if isinstance(e, dict)}
        except (OSError, json.JSONDecodeError):
            pass

    files = sorted(
        (p for p in VIDEO_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS and p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    manifest = []
    for path in files:
        st = path.stat()
        src = f"videos/{path.name}"
        title = re.sub(r"[_-]+", " ", path.stem).strip()
        entry = {"src": src, "title": title, "size": st.st_size}
        entry.update(probe(str(path), st.st_mtime, st.st_size))
        old = existing.get(src, {})
        for key in ("title", "caption"):
            if old.get(key):
                entry[key] = old[key]
        manifest.append(entry)

    # keep hand-added entries for videos hosted elsewhere (https://...)
    manifest += [e for src, e in existing.items() if src and "://" in src]
    return manifest


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/mp4",  # same container family; every browser accepts this type
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".js": "text/javascript",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def handle_request(self, send_body):
        path = Path(self.translate_path(self.path))
        # never expose .git, .gitignore, etc.
        if any(part.startswith(".") for part in path.relative_to(ROOT).parts):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if path == MANIFEST and not MANIFEST.is_file():
            body = json.dumps(build_manifest(), indent=2).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path.suffix.lower() not in VIDEO_EXTS or not path.is_file():
            if send_body:
                super().do_GET()
            else:
                super().do_HEAD()
            return

        self.send_video(path, send_body)

    def send_video(self, path, send_body):
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK

        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:  # suffix range: last N bytes
                start = max(size - int(match[2]), 0)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "public, max-age=3600")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return

        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser cancels requests whenever you scroll or seek

    def log_message(self, fmt, *args):
        if os.environ.get("VERBOSE"):
            super().log_message(fmt, *args)


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets phones on your Wi-Fi connect")
    parser.add_argument("--manifest", action="store_true", help="write videos.json and exit")
    args = parser.parse_args()

    VIDEO_DIR.mkdir(exist_ok=True)

    if args.manifest:
        manifest = build_manifest()
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Wrote {MANIFEST.name} with {len(manifest)} video(s).")
        if not shutil.which("ffprobe"):
            print("Tip: install ffmpeg so resolution/fps/codec get filled in automatically.")
        return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    count = len([p for p in VIDEO_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS])
    print(f"Serving {count} video(s) from {VIDEO_DIR} at full quality.")
    print(f"  This computer:  http://localhost:{args.port}")
    lan = lan_address()
    if lan and args.host == "0.0.0.0":
        print(f"  Phone (same Wi-Fi):  http://{lan}:{args.port}")
    if MANIFEST.is_file():
        print(f"Note: using the {MANIFEST.name} on disk. Delete it to auto-list the videos/ folder.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
