#!/usr/bin/env python3
"""Serve the site with full-quality video streaming.

    python3 serve.py              # serve on port 8000, open the printed address on your phone
    python3 serve.py --port 9000
    python3 serve.py --no-uploads # watch-only: no upload button, no processing
    python3 serve.py --manifest   # write videos.json for a static host, then exit

Video files are sent byte-for-byte as they are on disk. Nothing is
re-encoded, resized or capped.

Uploads use the private link printed at startup. Uploaded videos below 4K
or 120 fps are upscaled and interpolated to 4K 120fps (tools/enhance.py),
then appear in the feed.

Python's built-in http.server ignores HTTP Range requests, which Safari
(every iPhone browser) requires before it will play a video, and which every
browser uses to seek. This server answers them.
"""

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import traceback
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_DIR = ROOT / "videos"
UPLOAD_DIR = ROOT / ".uploads"
MANIFEST = ROOT / "videos.json"
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
CHUNK = 1024 * 1024
MAX_JSON_BODY = 64 * 1024
MAX_CONNECTIONS = 64
EXTRA_HOSTS = set()  # names added with --allow-host

sys.path.insert(0, str(ROOT / "tools"))
import uploads  # noqa: E402

UPLOADS = None  # an uploads.UploadStore when uploads are enabled
TOKEN = None


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
        # uploads leave a <name>.json next to the video with title, caption and poster
        sidecar = path.with_suffix(".json")
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text())
                entry.update({k: v for k, v in meta.items() if k in ("title", "caption", "poster", "enhanced_from") and v})
            except (OSError, json.JSONDecodeError):
                pass
        old = existing.get(src, {})
        for key in ("title", "caption"):
            if old.get(key):
                entry[key] = old[key]
        manifest.append(entry)

    # keep hand-added entries for videos hosted elsewhere (https://...)
    manifest += [e for src, e in existing.items() if src and "://" in src]
    return manifest


def host_allowed(header):
    """Refuse DNS-rebinding requests: a hostile web page can point its own domain
    at this computer, but it can't make the browser send an IP address or
    'localhost' as the Host header."""
    if not header:
        return True  # very old clients; browsers always send Host
    host = header.strip().lower()
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:
        host = host.rpartition(":")[0]
    host = host.rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")) or host in EXTRA_HOSTS:
        return True
    if host == socket.gethostname().lower():
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def content_length(headers):
    value = headers.get("Content-Length", "")
    return int(value) if re.fullmatch(r"[0-9]{1,15}", value) else None


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on simultaneous connections."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


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

    timeout = 120  # drop clients that stall mid-upload instead of holding a thread forever

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        if self.check_host():
            self.handle_request(send_body=True)

    def do_HEAD(self):
        if self.check_host():
            self.handle_request(send_body=False)

    def do_POST(self):
        if self.check_host():
            self.handle_api("POST")

    def do_PUT(self):
        if self.check_host():
            self.handle_api("PUT")

    def do_DELETE(self):
        if self.check_host():
            self.handle_api("DELETE")

    def check_host(self):
        if host_allowed(self.headers.get("Host")):
            return True
        self.close_connection = True
        self.send_error(HTTPStatus.FORBIDDEN, "Open this site by its IP address, or start serve.py with --allow-host NAME.")
        return False

    # ---------- upload API (see tools/uploads.py) ----------

    def handle_api(self, method):
        self.body_read = False
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/config" and method == "GET":
                self.send_json(200, {
                    "uploads": UPLOADS is not None,
                    "processing": uploads.enhance.available(),
                    "max_upload_bytes": UPLOADS.max_bytes if UPLOADS else 0,
                    "chunk": {"min": uploads.MIN_CHUNK, "max": uploads.MAX_CHUNK, "max_final": uploads.MAX_FINAL_CHUNK},
                })
                return
            if not path.startswith("/api/"):
                raise uploads.UploadError(405, "method_not_allowed", "Only the /api/ endpoints accept this method.")
            if UPLOADS is None:
                raise uploads.UploadError(404, "uploads_disabled", "Uploads are turned off on this server.")
            self.require_token()

            if path == "/api/auth" and method == "GET":  # lets the page check a key before saving it
                self.send_json(200, {"ok": True})
                return
            if path == "/api/upload/init" and method == "POST":
                self.send_json(200, UPLOADS.init(self.read_json()))
                return
            match = re.fullmatch(r"/api/upload/([0-9a-f]{24})", path)
            if match and method == "PUT":
                length = content_length(self.headers)
                if length is None:
                    raise uploads.UploadError(411, "length_required", "Each chunk needs a Content-Length header.")
                done, status = UPLOADS.put_chunk(match[1], self.headers.get("Content-Range"), length, self.rfile)
                self.body_read = True
                # like TikTok: tell the client how much has arrived, as a byte range
                received = status["received_prefix"]
                progress = f"bytes 0-{received - 1}/{status['video_size']}" if received else f"bytes */{status['video_size']}"
                self.send_json(201 if done else 206, status, headers={"Content-Range": progress})
                return
            if match and method == "GET":
                self.send_json(200, UPLOADS.status(match[1]))
                return
            if match and method == "DELETE":
                self.send_json(200, UPLOADS.cancel(match[1]))
                return
            raise uploads.UploadError(404, "not_found", "Unknown API endpoint.")
        except uploads.UploadError as e:
            if e.status == 401 or e.body_consumed:
                self.close_connection = True  # don't spend time reading a body we won't use
            else:
                self.drain_body()
            self.send_json(e.status, None, e.code, e.message)
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            try:
                self.send_json(500, None, "internal_error", "Something went wrong on the server.")
            except OSError:
                pass

    def require_token(self):
        sent = self.headers.get("Authorization", "")
        given = sent[7:].strip().encode("latin-1", "replace") if sent.startswith("Bearer ") else b""
        if not (TOKEN and hmac.compare_digest(given, TOKEN.encode())):
            raise uploads.UploadError(401, "access_token_invalid", "Open the upload link printed by serve.py to get access.")

    def read_json(self):
        length = content_length(self.headers)
        if length is None or length > MAX_JSON_BODY:
            raise uploads.UploadError(400, "invalid_params", "Send a JSON body under 64 KB.")
        self.body_read = True
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, RecursionError):
            raise uploads.UploadError(400, "invalid_params", "The request body isn't valid JSON.")
        if not isinstance(body, dict):
            raise uploads.UploadError(400, "invalid_params", "The request body must be a JSON object.")
        return body

    def drain_body(self):
        """Read an unused request body so the client sees our error instead of a reset connection."""
        remaining = content_length(self.headers)
        if self.command in ("GET", "HEAD") or self.body_read or remaining is None:
            return
        if remaining > uploads.MAX_FINAL_CHUNK + CHUNK:
            self.close_connection = True
            return
        try:
            while remaining > 0:
                data = self.rfile.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
        except OSError:
            self.close_connection = True

    def send_json(self, status, data, code="ok", message="", headers=None):
        # same envelope as TikTok's API: {"data": ..., "error": {"code", "message"}}
        body = json.dumps({"data": data, "error": {"code": code, "message": message}}).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- static files and video streaming ----------

    def handle_request(self, send_body):
        if self.path.startswith("/api/"):
            self.handle_api("GET")
            return
        path = Path(self.translate_path(self.path))
        # never expose .git, .uploads (the upload key), etc. The resolved path is
        # checked too: on Windows, 8.3 names like UPLOAD~1 point at .uploads.
        for candidate in (path, path.resolve()):
            try:
                parts = candidate.relative_to(ROOT).parts
            except ValueError:
                continue  # e.g. videos/ is a link to another drive
            if any(part.startswith(".") for part in parts):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

        # with uploads on, list videos/ live (keeping titles from any videos.json) so new posts show up
        if path == MANIFEST and (UPLOADS or not MANIFEST.is_file()):
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


def load_token(renew=False):
    """A random upload key, kept in .uploads/token so the upload link survives restarts."""
    path = UPLOAD_DIR / "token"
    if renew:
        path.unlink(missing_ok=True)
    if path.is_file() and path.read_text().strip():
        return path.read_text().strip()
    token = secrets.token_urlsafe(18)
    UPLOAD_DIR.mkdir(exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def main():
    global UPLOADS, TOKEN
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets phones on your Wi-Fi connect")
    parser.add_argument("--manifest", action="store_true", help="write videos.json and exit")
    parser.add_argument("--no-uploads", action="store_true", help="turn off uploading and processing")
    parser.add_argument("--max-upload-gb", type=float, default=20, help="largest accepted upload (default 20)")
    parser.add_argument("--new-key", action="store_true", help="replace the upload key, so old upload links stop working")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                        help="also answer requests for this host name (e.g. mypc.lan or a tunnel domain)")
    args = parser.parse_args()
    EXTRA_HOSTS.update(h.strip().lower().rstrip(".") for h in args.allow_host)

    VIDEO_DIR.mkdir(exist_ok=True)

    if args.manifest:
        manifest = build_manifest()
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Wrote {MANIFEST.name} with {len(manifest)} video(s).")
        if not shutil.which("ffprobe"):
            print("Tip: install ffmpeg so resolution/fps/codec get filled in automatically.")
        return

    if not args.no_uploads:
        TOKEN = load_token(renew=args.new_key)
        UPLOADS = uploads.UploadStore(UPLOAD_DIR, VIDEO_DIR, int(args.max_upload_gb * 1024**3))

    server = Server((args.host, args.port), Handler)
    count = len([p for p in VIDEO_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS])
    print(f"Serving {count} video(s) from {VIDEO_DIR} at full quality.")
    print(f"  This computer:  http://localhost:{args.port}")
    lan = lan_address()
    if lan and args.host == "0.0.0.0":
        print(f"  Phone (same Wi-Fi):  http://{lan}:{args.port}")
    if UPLOADS:
        host = lan if lan and args.host == "0.0.0.0" else "localhost"
        print(f"Your private upload link:  http://{host}:{args.port}/#upload={TOKEN}")
        print("  Anyone with this link can post videos. Only use it on Wi-Fi you trust (it isn't encrypted);")
        print("  run with --new-key to replace it.")
        if not uploads.enhance.available():
            print("Warning: ffmpeg isn't installed, so uploads are turned off until you install it.")
    if MANIFEST.is_file():
        print(f"Note: using the {MANIFEST.name} on disk. Delete it to auto-list the videos/ folder.")
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        if UPLOADS:
            UPLOADS.shutdown()  # stop ffmpeg; unfinished videos resume next time
        sys.exit(0)


if __name__ == "__main__":
    main()
